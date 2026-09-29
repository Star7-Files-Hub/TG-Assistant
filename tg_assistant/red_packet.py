"""自动抢红包。

流程（全程不做阻塞式轮询）：

1. handler 收到消息 → 纯内存判断是不是红包（按钮关键词 / 正文正则）；
2. 命中后 ``create_task`` 派发，handler 立刻返回，不拖慢后续消息；
3. 按策略动手：

   - ``button``：找到匹配的内联按钮，调用 ``request_callback_answer``
     （等价于 Bot API 的 ``messages.GetBotCallbackAnswer``），
     Telegram 会**同步返回**一段提示文字，这是判断成功最快的依据；
   - ``keyword``：按模板发一条消息，例如 ``/grab {code}``，``{code}`` 由 ``code_pattern`` 提取；
   - ``auto``：有可点按钮就点，否则退化为发关键词。

4. 成功判定：先看 callback answer 文本，没结论就在 ``wait_timeout`` 内监听该会话的
   新消息/编辑事件（事件驱动，非轮询），命中成功/失败正则即定论；都没命中记 ``unknown``；
5. 抢到后（可配置为只在成功时）从回复列表里随机抽一条发出去，带随机延迟模拟真人。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from pyrogram import Client, filters
from pyrogram.errors import (
    BotResponseTimeout,
    DataInvalid,
    MessageIdInvalid,
    QueryIdInvalid,
)
from pyrogram.handlers import EditedMessageHandler, MessageHandler

from .client import SessionInvalid, with_flood_retry
from .metrics import MetricsStore
from .config import AccountConfig, RedPacketConfig, RedPacketTask
from .logging_setup import AccountLogger
from .matching import (
    DEFAULT_RED_PACKET_TEMPLATE,
    RefSet,
    build_variables,
    chat_identity,
    compile_patterns,
    compile_user_pattern,
    first_match,
    message_text,
    render_template,
    sender_of,
    truncate,
)
from .notify import BotNotifier, NotifyTask


class GrabResult(str, Enum):
    """抢包结果。"""

    SUCCESS = "success"
    FAILED = "failed"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"
    ERROR = "error"

    @property
    def icon(self) -> str:
        return {
            GrabResult.SUCCESS: "✅",
            GrabResult.FAILED: "❌",
            GrabResult.UNKNOWN: "❔",
            GrabResult.SKIPPED: "⏭️",
            GrabResult.ERROR: "⚠️",
        }[self]

    @property
    def label(self) -> str:
        return {
            GrabResult.SUCCESS: "抢到了",
            GrabResult.FAILED: "没抢到",
            GrabResult.UNKNOWN: "结果未知",
            GrabResult.SKIPPED: "已跳过",
            GrabResult.ERROR: "出错",
        }[self]


@dataclass
class GrabOutcome:
    """一次抢包的完整结果，用于日志与通知。"""

    result: GrabResult
    strategy: str
    detail: str
    chat_id: Optional[int]
    chat_title: Optional[str]
    message_id: Optional[int]
    cost_ms: float
    button_text: Optional[str] = None
    code: Optional[str] = None
    callback_text: Optional[str] = None
    evidence: Optional[str] = None
    replied: Optional[str] = None
    attempts: int = 1
    #: 是哪条任务干的 —— 多任务之后，日志里不写这个就分不清谁抢到的。
    task: Optional[str] = None


@dataclass
class PreparedTask:
    """一条抢红包任务 + 预编译好的正则与会话集合。

    正则编译一次、整个进程复用：红包是秒级的事情，每条消息现编译正则
    会在最不该慢的地方拖后腿。
    """

    config: RedPacketTask
    chats: RefSet
    exclude_chats: RefSet
    button_keywords: list[str]
    text_patterns: list[Any]
    code_pattern: Optional[Any]
    success_patterns: list[Any]
    failure_patterns: list[Any]

    @property
    def id(self) -> str:
        return self.config.id

    @property
    def label(self) -> str:
        return self.config.label

    @classmethod
    def build(cls, config: RedPacketTask) -> "PreparedTask":
        detect = config.detect
        return cls(
            config=config,
            chats=RefSet(config.chats),
            exclude_chats=RefSet(config.exclude_chats),
            button_keywords=[keyword.lower() for keyword in detect.button_keywords],
            text_patterns=compile_patterns(detect.text_patterns),
            code_pattern=(
                compile_user_pattern(detect.code_pattern) if detect.code_pattern else None
            ),
            success_patterns=compile_patterns(config.success.success_patterns),
            failure_patterns=compile_patterns(config.success.failure_patterns),
        )


# --------------------------------------------------------------------------- #
# 会话消息监听（事件驱动的成功判定）
# --------------------------------------------------------------------------- #
class ChatEventBus:
    """把某个会话的后续消息推给正在等待判定的任务。

    比轮询 ``get_chat_history`` 更快也更省 API：消息本来就会经过 handler，
    这里只是把它分发给等待者。
    """

    def __init__(self) -> None:
        self._waiters: dict[int, list[asyncio.Queue[Any]]] = {}

    def feed(self, chat_id: int, message: Any) -> None:
        queues = self._waiters.get(chat_id)
        if not queues:
            return
        for queue in queues:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(message)

    @contextlib.asynccontextmanager
    async def watch(self, chat_id: int, maxsize: int = 64) -> AsyncIterator[asyncio.Queue[Any]]:
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self._waiters.setdefault(chat_id, []).append(queue)
        try:
            yield queue
        finally:
            queues = self._waiters.get(chat_id)
            if queues:
                with contextlib.suppress(ValueError):
                    queues.remove(queue)
                if not queues:
                    self._waiters.pop(chat_id, None)

    @property
    def watching(self) -> int:
        return sum(len(v) for v in self._waiters.values())


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #
def _settled_key(raw: Any) -> Optional[tuple[int, int]]:
    """把落盘的 ``"<chat_id>:<message_id>"`` 解析回 ``(chat_id, message_id)``。

    看不懂就返回 ``None``（调用方跳过）—— 状态文件是数据，不是信任边界。
    """
    if isinstance(raw, str) and ":" in raw:
        chat, _, message = raw.partition(":")
    elif isinstance(raw, (list, tuple)) and len(raw) == 2:
        chat, message = raw
    else:
        return None
    try:
        return int(chat), int(message)
    except (TypeError, ValueError):
        return None


class RedPacketHunter:
    """抢红包引擎。"""

    #: 与转发引擎分开的 handler group，互不阻塞。
    HANDLER_GROUP = 1

    #: ``_settled`` 的容量上限。消息 id 不会复用，所以不需要过期时间，
    #: 这里只是防止一张长期运行的表无限长大（超了就留最近的一半）。
    SETTLED_MAX = 2048

    def __init__(
        self,
        client: Client,
        config: AccountConfig,
        alog: AccountLogger,
        notifier: Optional[BotNotifier] = None,
        metrics: Optional[Any] = None,
        settled_path: Optional[Any] = None,
    ) -> None:
        self.client = client
        self.config: RedPacketConfig = config.red_packet
        self.notify_config = config.notify
        self.alog = alog.bind("redpacket")
        self.notifier = notifier
        #: 数据大盘（多账号共享同一个实例）。不传就自己建个纯内存的 ——
        #: 这样调用处不必到处判空，单测 / 离线场景也不会因为没接线就崩。
        self.metrics = metrics if metrics is not None else MetricsStore()
        #: ``_settled`` 的落盘路径（账号级）。``None`` = 只留内存（单测 / 离线用）。
        self.settled_path = settled_path
        #: 热重载指纹：记下「当前生效的是哪一份配置」。mtime 变了但内容没变
        #: （面板原样保存一次）时据此跳过重建。
        self._applied_signature = self._apply_signature(config)

        # 只编译**启用**的任务：停用的任务连正则都不该编译。
        self.prepared: list[PreparedTask] = [
            PreparedTask.build(task) for task in self.config.active_tasks
        ]

        self._bus = ChatEventBus()
        self._handlers: list[tuple[Any, int]] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._semaphore = asyncio.Semaphore(self.config.max_concurrency)
        self._seen: dict[tuple[int, int], float] = {}
        #: 已得出**确定结论**的红包消息 → ``(记录时间, 结论)``。一旦某条消息判出
        #: 抢到 / 没抢到，就**再也不点它**。
        #:
        #: 🔴 为什么必须单独记一张表（``_seen`` 的 60 秒窗口不够）：
        #: 红包 bot 会**反复编辑同一条消息**（更新"已领 19/200 份"），每次编辑都是一次
        #: EditedMessage 事件。线上实测同一条 ``message_id`` 在 4 分钟、8 分钟后又被
        #: 编辑，于是又点一次、又白等 10 秒判定，机器人回「你已经领过这个红包啦」。
        #: 既浪费时机又像在刷按钮（风控最讨厌的行为）。
        #:
        #: 🔴 又为什么必须**落盘**：这张表原本只在内存里，而服务经常重启（每次改配置、
        #: 每次部署）。线上实测 2026-09-29 那条长驻红包：11:16 判出「你已经领过」并进了
        #: 这张表，13:50 一次重启就忘光，14:23 它又被编辑 → 又点一次；16:38~17:46 又重启
        #: 6 次，18:07 再点一次。重启是常态，所以「已经点过了」必须活过重启。
        #:
        #: 时间戳用 ``time.time()``（而不是 ``time.monotonic()``）：要跨进程比较、
        #: 落盘后再读回来，单调时钟没有跨进程意义。
        self._settled: dict[tuple[int, int], tuple[float, str]] = {}
        #: 从磁盘读回了多少条（心跳/排查用：重启后这个数字不为 0 才说明落盘生效了）。
        self.restored_settled = 0
        self._load_settled()
        self._reply_cooldown: dict[int, float] = {}
        self._me_id: Optional[int] = None
        self._me_names: list[str] = []
        self.stats = {
            "detected": 0,
            "attempted": 0,
            "success": 0,
            "failed": 0,
            "unknown": 0,
            "error": 0,
            "replied": 0,
            #: 发现了红包但**不在全局动手时段**内、于是没动手的次数。
            #: 「时段外发现红包」本该是常态（半夜照样有包，只是我们不抢），
            #: 这个数字 >0 才说明时段真的在起作用。
            "outside_window": 0,
            #: 因为"原消息太老"而被丢掉的编辑事件次数（``edit_max_age`` 挡下来的）。
            #: 线上那条长驻红包被反复编辑时，这个数字会涨 —— 它涨说明闸门在干活。
            "stale_edits": 0,
        }
        #: 每条任务各自的计数。多任务之后「一共抢到 3 个」说明不了是谁干的。
        self.task_stats: dict[str, dict[str, int]] = {
            task.id: dict.fromkeys(self.stats, 0) for task in self.config.active_tasks
        }

    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def watched_chats(self) -> list[Any]:
        """所有任务监听会话的并集；任一任务留空 ⇒ 空列表（= 不过滤，全监听）。"""
        return list(self.config.watched_chats)

    @staticmethod
    def _apply_signature(config: AccountConfig) -> tuple[Any, ...]:
        """热重载指纹：这些字段变了才需要重建。

        刻意**不含**运行期状态（计数、同码去重表、回复冷却）—— 那些必须活着，
        改一次配置不该把「一共抢到几个」清零。
        """
        return (
            config.red_packet.model_dump(mode="json"),
            config.notify.model_dump(mode="json"),
        )

    async def register(self) -> None:
        if not self.enabled:
            self.alog.info("抢红包功能未启用")
            return
        self._install_handlers()
        self._log_registered()

    def _install_handlers(self) -> None:
        """装上 handler；**先拆旧的**，保证过滤器跟着新的监听会话走。

        ``watched_chats()`` 决定 handler 的过滤器。只换 ``self.config`` 而不换
        过滤器的话，把监听从「某个群」改成另一个群之后，**新群的消息根本进不来**
        —— 配置改得再对也没用。这与转发引擎是同一类坑（见
        ``ForwardEngine.reload_rules`` 的注释）。
        """
        self._remove_handlers()

        me = getattr(self.client, "me", None)
        if me is not None:
            self._me_id = getattr(me, "id", None)
            self._me_names = [
                str(value).lower()
                for value in (
                    getattr(me, "username", None),
                    getattr(me, "first_name", None),
                    getattr(me, "last_name", None),
                )
                if value
            ]

        chats = self.watched_chats()
        message_filter = filters.chat(chats) if chats else None
        self._handlers.append(
            self.client.add_handler(
                MessageHandler(self._on_message, message_filter), group=self.HANDLER_GROUP
            )
        )
        if self.config.include_edited:
            self._handlers.append(
                self.client.add_handler(
                    EditedMessageHandler(self._on_edited, message_filter),
                    group=self.HANDLER_GROUP,
                )
            )

    def _remove_handlers(self) -> None:
        for handler, group in self._handlers:
            with contextlib.suppress(Exception):
                self.client.remove_handler(handler, group)
        self._handlers.clear()

    def _log_registered(self) -> None:
        chats = self.watched_chats()
        self.alog.info(
            "抢红包引擎已注册",
            tasks=len(self.prepared),
            disabled_tasks=len(self.config.tasks) - len(self.prepared),
            task_labels=" | ".join(task.label for task in self.prepared) or "-",
            strategies=",".join(task.config.strategy for task in self.prepared) or "-",
            watched_chats=len(chats) or "全部",
            max_concurrency=self.config.max_concurrency,
        )
        for task in self.prepared:
            if task.config.ready:
                continue
            # 配不全的任务照样注册（用户可能正在填），但必须**明确说出来**，
            # 否则用户只会看到"开了却没动静"。
            self.alog.warning(
                "任务配置不完整，不会动手",
                task=task.label,
                problem=task.config.problem,
            )

    async def apply_config(self, config: AccountConfig) -> bool:
        """把新配置应用到**正在运行**的引擎上（面板改完即生效，无需重启账号）。

        返回 True 表示确实变了。三类变更都必须照顾到：

        * **增删/修改任务** —— 重建 ``prepared`` 与 ``task_stats``、按新并发上限换信号量；
        * **开/关总开关** —— 注册或注销 handler。多任务之后这是常态：
          用户先关着把配置填好，填完再打开；
        * **改监听会话** —— handler 的过滤器必须跟着换（见 :meth:`_install_handlers`）。

        🔴 全程**不重建** ``self._bus`` / ``self._seen`` / 在途任务：正在抢的那个包
        不能因为用户随手点了保存就被打断。
        """
        signature = self._apply_signature(config)
        if signature == self._applied_signature:
            return False
        self._applied_signature = signature
        was_enabled = self.enabled

        self.config = config.red_packet
        self.notify_config = config.notify
        self.prepared = [
            PreparedTask.build(task) for task in self.config.active_tasks
        ]
        self._semaphore = asyncio.Semaphore(self.config.max_concurrency)
        # 计数按任务 id 继承：改配置不该让历史统计消失，只对**新增**的任务补零。
        self.task_stats = {
            task.id: self.task_stats.get(task.id) or dict.fromkeys(self.stats, 0)
            for task in self.config.active_tasks
        }

        if self.enabled:
            self._install_handlers()
        else:
            self._remove_handlers()

        self.alog.info(
            "抢红包配置已热重载（无需重启）",
            enabled=self.config.enabled,
            tasks=len(self.prepared),
            task_labels=" | ".join(task.label for task in self.prepared) or "-",
            watched_chats=len(self.watched_chats()) or "全部",
            max_concurrency=self.config.max_concurrency,
            turned_on=self.enabled and not was_enabled,
        )
        if self.enabled and not was_enabled:
            # 刚被打开：把「哪条任务配得不全」再喊一遍，用户正等着看它动没动。
            self._log_registered()
        return True

    async def close(self) -> None:
        self._remove_handlers()
        if self._tasks:
            self.alog.info("等待在途抢包任务结束", pending=len(self._tasks))
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        self.alog.info("抢红包引擎已停止", **self.stats)

    # ------------------------------------------------------------------ #
    async def _on_message(self, client: Client, message: Any) -> None:
        del client
        self._dispatch(message, edited=False)

    async def _on_edited(self, client: Client, message: Any) -> None:
        del client
        self._dispatch(message, edited=True)

    def _edit_age(self, task: PreparedTask, message: Any) -> Optional[float]:
        """这条编辑事件对应的消息有多老（秒）；**不该处理**就返回年龄，否则 ``None``。

        ``message.date`` 是**原消息的发布时间**（pyrogram 对编辑事件同样给原始时间，
        本次编辑的时间在 ``edit_date``）—— 正是我们要判的"老不老"。

        拿不到时间（测试替身、字段缺失、时钟异常）时**放行**：宁可多看一眼，
        也不要因为读不到时间就把真红包漏掉。``edit_max_age <= 0`` = 关掉闸门。
        """
        limit = task.config.edit_max_age
        if limit <= 0:
            return None
        date = getattr(message, "date", None)
        if date is None:
            return None
        try:
            age = time.time() - date.timestamp()
        except (AttributeError, OverflowError, OSError, ValueError):
            return None
        return age if age > limit else None

    def _dispatch(self, message: Any, *, edited: bool) -> None:
        chat_id, _, _ = chat_identity(message)
        if chat_id is None:
            return
        # 先喂给等待判定的任务（哪怕这条消息本身不是红包）
        self._bus.feed(chat_id, message)

        started = time.perf_counter()
        hit = self._match(message, chat_id)
        if hit is None:
            return

        task, button, code = hit
        message_id = getattr(message, "id", None)
        key = (chat_id, message_id or 0)
        now = time.monotonic()

        # 🔴 编辑事件的**年龄闸门**：一条几十分钟前的消息又被编辑，绝不可能是新红包。
        # 线上实测（2026-09-29，白嫖分享社 352733）：那是一条**长驻红包**，机器人一整天
        # 在改它的状态文本，于是同一条 8 小时前的消息被处理了 8 次，其中 7 次只换回
        # 「你已经领过这个红包啦」——每次都要白等一个 wait_timeout，还像在刷按钮。
        # ``include_edited`` 本身要保留（有些 bot 先发消息、随后编辑出按钮），
        # 所以闸门只针对**老消息**，不针对"编辑"这个动作。
        if edited:
            age = self._edit_age(task, message)
            if age is not None:
                self.stats["stale_edits"] += 1
                per_task = self.task_stats.get(task.id)
                if per_task is not None:
                    per_task["stale_edits"] += 1
                self.alog.debug(
                    "红包消息太老（编辑事件），跳过",
                    task=task.label,
                    chat_id=chat_id,
                    message_id=message_id,
                    age_s=round(age, 1),
                    max_age_s=task.config.edit_max_age,
                    stale_edits=self.stats["stale_edits"],
                )
                return

        # 🔴 已经判出确定结论的红包消息**再也不点**：bot 会反复编辑同一条消息
        # （更新"已领 19/200 份"），每次编辑都是一次事件。见 ``_settled`` 的注释。
        settled = self._settled.get(key)
        if settled is not None:
            self.alog.debug(
                "该红包已得出确定结论，不再重复点击",
                chat_id=chat_id,
                message_id=message_id,
                result=settled[1],
            )
            return

        last = self._seen.get(key)
        if last is not None and now - last < 60.0:
            self.alog.debug("红包消息已处理过，跳过", chat_id=chat_id, message_id=message_id)
            return
        self._seen[key] = now
        if len(self._seen) > 4096:
            cutoff = now - 300
            self._seen = {k: v for k, v in self._seen.items() if v > cutoff}

        _, _, chat_title = chat_identity(message)

        if not self.config.in_window:
            # 时段外：看得见，但不动手。
            # **不**计入 ``detected`` —— 那个数字的含义是"真正要抢的红包有几个"，
            # 把夜里的包混进去它就失去意义了（这也是抢注那边的口径）。
            self.stats["outside_window"] += 1
            per_task = self.task_stats.get(task.id)
            if per_task is not None:
                per_task["outside_window"] += 1
            self.alog.info(
                "发现红包但不在动手时段内，跳过",
                task=task.label,
                chat=chat_title or chat_id,
                message_id=message_id,
                button=button.get("text") if button else "-",
                window=self.config.window.describe(),
                outside_window=self.stats["outside_window"],
            )
            return

        self.stats["detected"] += 1
        self.task_stats[task.id]["detected"] += 1
        self.alog.info(
            "发现红包",
            task=task.label,
            chat=chat_title or chat_id,
            message_id=message_id,
            button=button.get("text") if button else "-",
            code=code or "-",
            edited=edited,
            detect_ms=round((time.perf_counter() - started) * 1000, 3),
            preview=truncate(message_text(message), 80, "…"),
        )

        job = asyncio.create_task(self._grab(task, message, button, code))
        self._tasks.add(job)
        job.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------ #
    def _match(
        self, message: Any, chat_id: int
    ) -> Optional[tuple[PreparedTask, Optional[dict[str, Any]], Optional[str]]]:
        """按任务顺序找**第一个**愿意处理这条消息的任务。

        🔴 只取第一个。同一条红包被两个任务同时命中就会点两次按钮 ——
        第二次多半报「已经领取过」，还可能触发风控。任务列表的顺序就是优先级。
        """
        for task in self.prepared:
            decision = self._should_grab_for(task, message, chat_id)
            if decision is not None:
                return task, decision[0], decision[1]
        return None

    def _should_grab(
        self, message: Any, chat_id: int
    ) -> Optional[tuple[Optional[dict[str, Any]], Optional[str]]]:
        """是否要抢；返回 ``(按钮信息, 提取到的口令)``，不抢则返回 None。

        「所有任务里有没有人愿意接」的入口；单条任务的判定见 :meth:`_should_grab_for`。
        """
        hit = self._match(message, chat_id)
        if hit is None:
            return None
        _, button, code = hit
        return button, code

    def _should_grab_for(
        self, task: PreparedTask, message: Any, chat_id: int
    ) -> Optional[tuple[Optional[dict[str, Any]], Optional[str]]]:
        """单条任务的判定。"""
        config = task.config
        _, chat_username, _ = chat_identity(message)
        if task.exclude_chats and task.exclude_chats.matches(chat_id, chat_username):
            return None
        if task.chats and not task.chats.matches(chat_id, chat_username):
            return None

        sender_id, _, is_self, is_bot = sender_of(message)
        if config.detect.ignore_self and is_self:
            return None
        if config.detect.only_from_bots and not is_bot:
            return None
        if getattr(message, "service", None):
            return None

        text = message_text(message)
        text_hit = True
        if task.text_patterns:
            text_hit = first_match(task.text_patterns, text) is not None

        button = self._find_button(task, message)

        if config.strategy == "button":
            if button is None:
                return None
            if task.text_patterns and not text_hit:
                return None
            return button, self._extract_code(task, text)

        if config.strategy == "keyword":
            if not text_hit:
                return None
            code = self._extract_code(task, text)
            if task.code_pattern is not None and code is None:
                self.alog.debug("正文未提取到口令，跳过", task=task.label, chat_id=chat_id)
                return None
            return None, code

        # auto：优先按钮
        if button is not None and (text_hit or not task.text_patterns):
            return button, self._extract_code(task, text)
        if config.detect.keyword_template and text_hit and (task.text_patterns or task.code_pattern):
            code = self._extract_code(task, text)
            if task.code_pattern is not None and code is None:
                return None
            return None, code
        del sender_id
        return None

    def _find_button(self, task: PreparedTask, message: Any) -> Optional[dict[str, Any]]:
        """在内联/回复键盘里找第一个像"领红包"的按钮。"""
        markup = getattr(message, "reply_markup", None)
        if markup is None:
            return None

        inline_rows = getattr(markup, "inline_keyboard", None) or []
        for row_index, row in enumerate(inline_rows):
            for col_index, button in enumerate(row):
                text = str(getattr(button, "text", "") or "")
                if not self._keyword_hit(task, text):
                    continue
                callback_data = getattr(button, "callback_data", None)
                if callback_data is None:
                    # url / web_app / switch_inline 类按钮无法用 callback 点击
                    self.alog.debug(
                        "按钮匹配但不是 callback 类型，无法点击",
                        button=text,
                        kind=_button_kind(button),
                    )
                    continue
                return {
                    "text": text,
                    "callback_data": callback_data,
                    "position": f"{row_index},{col_index}",
                    "kind": "inline",
                }

        reply_rows = getattr(markup, "keyboard", None) or []
        for row in reply_rows:
            for button in row:
                text = str(getattr(button, "text", None) or (button if isinstance(button, str) else ""))
                if text and self._keyword_hit(task, text):
                    return {"text": text, "callback_data": None, "position": "-", "kind": "reply"}
        return None

    def _keyword_hit(self, task: PreparedTask, text: str) -> bool:
        lowered = text.lower()
        return any(keyword in lowered for keyword in task.button_keywords)

    def _extract_code(self, task: PreparedTask, text: str) -> Optional[str]:
        if task.code_pattern is None:
            return None
        found = task.code_pattern.search(text)
        if not found:
            return None
        if found.groups():
            for group in found.groups():
                if group:
                    return group
        return found.group(0)

    # ------------------------------------------------------------------ #
    async def _grab(
        self,
        task: PreparedTask,
        message: Any,
        button: Optional[dict[str, Any]],
        code: Optional[str],
    ) -> None:
        started = time.perf_counter()
        chat_id, _, chat_title = chat_identity(message)
        message_id = getattr(message, "id", None)
        strategy = "button" if button and button.get("callback_data") is not None else "keyword"
        if button is not None and button.get("kind") == "reply":
            strategy = "keyboard-text"

        config = task.config
        delay = config.delay + (random.uniform(0, config.jitter) if config.jitter else 0.0)
        if delay > 0:
            self.alog.debug(
                "按配置延迟后再抢", task=task.label, delay_s=round(delay, 3), chat_id=chat_id
            )
            await asyncio.sleep(delay)

        outcome = await self._execute(task, message, button, code, strategy, started)

        self._record(task, outcome)
        self._settle(outcome)
        if self._should_reply(task, outcome.result):
            replied = await self._maybe_reply(task, message, outcome)
            if replied:
                outcome.replied = replied

        self._log_outcome(outcome, chat_title, message_id)
        if (
            config.notify
            and self.notifier is not None
            and self._should_notify(task, outcome.result)
        ):
            self._submit_notify(task, message, outcome)

    def _settle(self, outcome: GrabOutcome) -> None:
        """把**确定结论**记进 ``_settled``，之后同一条消息不再点。

        只有抢到 / 明确没抢到才算定论。``unknown``（机器人没回显、或回显看不懂）
        与 ``error`` 刻意**不**记 —— 那两种情况重试是有意义的（可能只是网络抖动），
        交给 ``_seen`` 的 60 秒窗口去挡短时间内的重复即可。
        """
        if outcome.result not in {GrabResult.SUCCESS, GrabResult.FAILED}:
            return
        if outcome.chat_id is None or outcome.message_id is None:
            return
        key = (outcome.chat_id, outcome.message_id)
        self._settled[key] = (time.time(), outcome.result.value)
        # 兜底裁剪：消息 id 不会复用，所以不需要 TTL，只防这张表无限长大。
        if len(self._settled) > self.SETTLED_MAX:
            newest = sorted(self._settled.items(), key=lambda kv: kv[1][0], reverse=True)
            self._settled = dict(newest[: self.SETTLED_MAX // 2])
        self._save_settled()

    # ------------------------------------------------------------------ #
    def _load_settled(self) -> None:
        """读回「已得出定论的红包消息」。任何异常都只当没有记录，绝不阻塞启动。

        读回来的表**也受** ``SETTLED_MAX`` 约束：磁盘上的文件可能是老版本（或手工编辑过）
        留下的超大表，直接全塞进内存就白瞎了这个上限。
        """
        if self.settled_path is None:
            return
        try:
            raw = json.loads(self.settled_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception:
            # 文件被写坏（断电 / 手工编辑）不能让账号起不来：最坏只是同一条红包
            # 消息可能被多点一次，比整个引擎拒绝启动轻得多。
            return
        if not isinstance(raw, dict):
            return
        entries = raw.get("settled")
        if not isinstance(entries, dict):
            return
        for raw_key, entry in entries.items():
            key = _settled_key(raw_key)
            if key is None:
                continue
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                result, when = str(entry[0]), entry[1]
            elif isinstance(entry, str):
                # 只写了结论（没有时间）的老格式：当作"很久以前"，裁剪时优先丢。
                result, when = entry, 0.0
            else:
                # 形状不对（``[]`` / 数字 / 嵌套对象）→ 跳过这一条，**不要**把
                # ``str(entry)`` 当成结论塞进去。
                continue
            try:
                stamp = float(when)
            except (TypeError, ValueError):
                stamp = 0.0
            self._settled[key] = (stamp, result)
        self.restored_settled = len(self._settled)
        if len(self._settled) > self.SETTLED_MAX:
            newest = sorted(self._settled.items(), key=lambda kv: kv[1][0], reverse=True)
            self._settled = dict(newest[: self.SETTLED_MAX // 2])

    def _save_settled(self) -> None:
        """原子写回。失败只吞掉 —— 抢红包永远不能被状态文件拖垮。"""
        if self.settled_path is None:
            return
        try:
            payload = {
                "version": 1,
                "settled": {
                    f"{chat_id}:{message_id}": [result, when]
                    for (chat_id, message_id), (when, result) in self._settled.items()
                },
            }
            self.settled_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.settled_path.with_name(self.settled_path.name + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(tmp, self.settled_path)
        except Exception:
            return

    def _should_notify(self, task: PreparedTask, result: GrabResult) -> bool:
        """要不要把这次结果推给用户 —— **依据就是机器人的回显**。

        回显已经被判定吃掉了（callback answer / 随后的消息变成 :class:`GrabResult`），
        所以策略直接按结果筛，不必再解析一遍文本。见 :data:`RedPacketNotifyPolicy`。
        """
        policy = task.config.notify_on
        if policy == "always":
            return True
        if policy == "success":
            return result is GrabResult.SUCCESS
        # attention：抢到 + 出错都值得看一眼，跳过"没抢到/已经领过/未知"的噪音。
        return result in {GrabResult.SUCCESS, GrabResult.ERROR}

    def _should_reply(self, task: PreparedTask, result: GrabResult) -> bool:
        """是否该发随机回复。

        - 确认抢到 → 回复；
        - ``only_on_success=False`` 时，结果未知也回复（部分 bot 不回显结果）；
        - 明确没抢到 / 出错 / 跳过 → 不回复（避免对着空气道谢）。
        """
        if result is GrabResult.SUCCESS:
            return True
        if result is GrabResult.UNKNOWN and not task.config.reply.only_on_success:
            return True
        return False

    async def _execute(
        self,
        task: PreparedTask,
        message: Any,
        button: Optional[dict[str, Any]],
        code: Optional[str],
        strategy: str,
        started: float,
    ) -> GrabOutcome:
        chat_id, _, chat_title = chat_identity(message)
        message_id = getattr(message, "id", None)
        assert chat_id is not None

        base = {
            "strategy": strategy,
            "chat_id": chat_id,
            "chat_title": chat_title,
            "message_id": message_id,
            "button_text": button.get("text") if button else None,
            "code": code,
            "task": task.label,
        }

        last_error: Optional[str] = None
        for attempt in range(1, task.config.max_attempts + 1):
            self.stats["attempted"] += 1
            try:
                # 先挂上监听，避免"抢完才开始听"导致漏掉瞬间返回的结果消息
                async with self._bus.watch(chat_id) as queue:
                    callback_text: Optional[str] = None
                    # 部分红包 bot 不返回 callback answer。这不算失败：
                    # 记下标记，稍后仍用后续消息判定（见下方 _judge）。
                    callback_timeout = False
                    # 信号量只保护"动手"这一小段：判定阶段要等最多 wait_timeout 秒，
                    # 若把它也圈进来，几个红包同时来就会互相排队，白白错过时机。
                    async with self._semaphore:
                        if strategy == "button" and button is not None:
                            try:
                                callback_text = await self._click_button(message, button)
                            except BotResponseTimeout:
                                callback_timeout = True
                                self.alog.warning(
                                    "点击按钮后机器人未响应",
                                    task=task.label,
                                    chat=chat_title or chat_id,
                                    message_id=message_id,
                                    attempt=attempt,
                                    hint="部分红包 bot 不返回 callback answer，将依据后续消息判定",
                                )
                        elif strategy == "keyboard-text" and button is not None:
                            await self._send_text(task, chat_id, str(button.get("text")), message)
                        else:
                            text = self._render_keyword(task, code, message)
                            if not text:
                                return GrabOutcome(
                                    result=GrabResult.SKIPPED,
                                    detail="keyword 策略缺少 keyword_template，无法发送",
                                    cost_ms=(time.perf_counter() - started) * 1000,
                                    attempts=attempt,
                                    **base,
                                )
                            await self._send_text(task, chat_id, text, message)

                    # 判定必须在 watch 上下文内完成：一旦退出这个 with，
                    # queue 就会从 bus 注销，_judge 再也收不到后续消息，
                    # 会直接返回 UNKNOWN。
                    verdict, evidence = await self._judge(task, callback_text, queue)

                detail = _verdict_detail(verdict, callback_text, evidence)
                if callback_timeout:
                    detail = detail or "机器人未在 10 秒内响应回调"
                return GrabOutcome(
                    result=verdict,
                    detail=detail,
                    cost_ms=(time.perf_counter() - started) * 1000,
                    callback_text=callback_text,
                    evidence=evidence,
                    attempts=attempt,
                    **base,
                )
            except SessionInvalid:
                raise
            except (QueryIdInvalid, DataInvalid, MessageIdInvalid) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                self.alog.warning(
                    "红包已失效或按钮数据过期",
                    task=task.label,
                    chat=chat_title or chat_id,
                    message_id=message_id,
                    error=last_error,
                    attempt=attempt,
                )
                return GrabOutcome(
                    result=GrabResult.FAILED,
                    detail="红包已失效（按钮数据过期或消息被删除）",
                    cost_ms=(time.perf_counter() - started) * 1000,
                    attempts=attempt,
                    **base,
                )
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt >= task.config.max_attempts:
                    self.alog.error(
                        "抢红包失败",
                        task=task.label,
                        chat=chat_title or chat_id,
                        message_id=message_id,
                        error=last_error,
                        attempts=attempt,
                        hint=_grab_hint(exc),
                    )
                    return GrabOutcome(
                        result=GrabResult.ERROR,
                        detail=last_error,
                        cost_ms=(time.perf_counter() - started) * 1000,
                        attempts=attempt,
                        **base,
                    )
                backoff = 0.3 * attempt
                self.alog.warning(
                    "抢红包出错，稍后重试",
                    error=last_error,
                    attempt=attempt,
                    retry_in_s=backoff,
                )
                await asyncio.sleep(backoff)

        return GrabOutcome(
            result=GrabResult.ERROR,
            detail=last_error or "未知错误",
            cost_ms=(time.perf_counter() - started) * 1000,
            attempts=task.config.max_attempts,
            **base,
        )

    async def _click_button(self, message: Any, button: dict[str, Any]) -> Optional[str]:
        """点击内联按钮，返回 Telegram 回显的提示文字（可能为空）。"""
        chat_id, _, _ = chat_identity(message)
        message_id = message.id
        callback_data = button["callback_data"]

        async def _do() -> Any:
            return await self.client.request_callback_answer(
                chat_id=chat_id,
                message_id=message_id,
                callback_data=callback_data,
                timeout=10,
            )

        answer = await with_flood_retry(
            _do,
            alog=self.alog,
            action="点击红包按钮",
            retries=1,
            max_flood_wait=10.0,
            expected_errors=(QueryIdInvalid, DataInvalid, MessageIdInvalid, BotResponseTimeout),
        )
        text = getattr(answer, "message", None)
        self.alog.info(
            "已点击红包按钮",
            button=button.get("text"),
            position=button.get("position"),
            callback_answer=truncate(str(text), 120, "…") if text else "（无提示）",
        )
        return str(text) if text else None

    async def _send_text(
        self, task: PreparedTask, chat_id: int, text: str, message: Any
    ) -> None:
        reply_to = (
            getattr(message, "id", None) if task.config.reply.reply_to_message else None
        )

        async def _do() -> Any:
            return await self.client.send_message(
                chat_id=chat_id,
                text=text,
                reply_to_message_id=reply_to,
            )

        await with_flood_retry(
            _do,
            alog=self.alog,
            action="发送抢红包关键词",
            retries=1,
            max_flood_wait=10.0,
        )
        self.alog.info("已发送抢红包关键词", task=task.label, text=text, chat_id=chat_id)

    def _render_keyword(
        self, task: PreparedTask, code: Optional[str], message: Any
    ) -> Optional[str]:
        template = task.config.detect.keyword_template
        if not template:
            return None
        variables = build_variables(message, {"code": code or ""})
        return render_template(template, variables).strip()

    # ------------------------------------------------------------------ #
    async def _judge(
        self,
        task: PreparedTask,
        callback_text: Optional[str],
        queue: Optional[asyncio.Queue[Any]],
    ) -> tuple[GrabResult, Optional[str]]:
        """判定结果。返回 ``(结论, 证据文本)``。"""
        if callback_text:
            verdict = self._classify(task, callback_text)
            if verdict is not None:
                return verdict, f"callback: {truncate(callback_text, 200, '…')}"

        timeout = task.config.success.wait_timeout
        if queue is None or timeout <= 0:
            return GrabResult.UNKNOWN, (
                f"callback: {truncate(callback_text, 200, '…')}" if callback_text else None
            )

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return GrabResult.UNKNOWN, None
            try:
                message = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return GrabResult.UNKNOWN, None

            text = message_text(message)
            if not text:
                continue
            if task.config.success.require_self_mention and not self._mentions_me(message, text):
                continue
            verdict = self._classify(task, text)
            if verdict is not None:
                return verdict, f"消息 {getattr(message, 'id', '?')}: {truncate(text, 200, '…')}"

    def _classify(self, task: PreparedTask, text: str) -> Optional[GrabResult]:
        """失败优先：'已被抢完' 里也含 '抢'，先判失败可避免误报成功。"""
        if first_match(task.failure_patterns, text):
            return GrabResult.FAILED
        if first_match(task.success_patterns, text):
            return GrabResult.SUCCESS
        return None

    def _mentions_me(self, message: Any, text: str) -> bool:
        lowered = text.lower()
        if self._me_id is not None and str(self._me_id) in text:
            return True
        for name in self._me_names:
            if name and name in lowered:
                return True
        entities = getattr(message, "entities", None) or []
        for entity in entities:
            user = getattr(entity, "user", None)
            if user is not None and getattr(user, "id", None) == self._me_id:
                return True
        return False

    # ------------------------------------------------------------------ #
    async def _maybe_reply(
        self, task: PreparedTask, message: Any, outcome: GrabOutcome
    ) -> Optional[str]:
        reply = task.config.reply
        if not reply.enabled or not reply.texts:
            return None
        chat_id = outcome.chat_id
        if chat_id is None:
            return None

        now = time.monotonic()
        if reply.cooldown > 0:
            last = self._reply_cooldown.get(chat_id)
            if last is not None and now - last < reply.cooldown:
                self.alog.info(
                    "回复处于冷却期，跳过",
                    task=task.label,
                    chat=outcome.chat_title or chat_id,
                    remaining_s=round(reply.cooldown - (now - last), 1),
                )
                return None

        text = random.choice(reply.texts)
        low, high = reply.delay_range
        delay = random.uniform(low, high) if high > low else low
        if delay > 0:
            await asyncio.sleep(delay)

        reply_to = outcome.message_id if reply.reply_to_message else None
        try:
            await with_flood_retry(
                lambda: self.client.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_to_message_id=reply_to,
                ),
                alog=self.alog,
                action="发送抢到后的回复",
                retries=1,
                max_flood_wait=30.0,
            )
        except SessionInvalid:
            raise
        except Exception as exc:
            self.alog.error(
                "回复失败",
                task=task.label,
                chat=outcome.chat_title or chat_id,
                text=text,
                error=f"{type(exc).__name__}: {exc}",
            )
            return None

        self._reply_cooldown[chat_id] = time.monotonic()
        self.stats["replied"] += 1
        self.alog.info(
            "已发送随机回复",
            task=task.label,
            chat=outcome.chat_title or chat_id,
            text=text,
            delay_s=round(delay, 2),
            pool_size=len(reply.texts),
        )
        return text

    # ------------------------------------------------------------------ #
    def _record(self, task: PreparedTask, outcome: GrabOutcome) -> None:
        bucket = {
            GrabResult.SUCCESS: "success",
            GrabResult.FAILED: "failed",
            GrabResult.UNKNOWN: "unknown",
            GrabResult.ERROR: "error",
        }.get(outcome.result)
        if bucket is None:
            return
        self.stats[bucket] += 1
        # 大盘**只记成功**：用户要的是「今天抢到了几个」，不是「今天点了几个」。
        if bucket == "success":
            self.metrics.record("red_packet")
        per_task = self.task_stats.get(task.id)
        if per_task is not None:
            per_task[bucket] += 1

    def _log_outcome(
        self, outcome: GrabOutcome, chat_title: Optional[str], message_id: Optional[int]
    ) -> None:
        fields = {
            "result": outcome.result.value,
            "task": outcome.task or "-",
            "chat": chat_title or outcome.chat_id,
            "message_id": message_id,
            "strategy": outcome.strategy,
            "cost_ms": round(outcome.cost_ms, 1),
            "attempts": outcome.attempts,
            "detail": outcome.detail,
        }
        if outcome.button_text:
            fields["button"] = outcome.button_text
        if outcome.code:
            fields["code"] = outcome.code
        if outcome.replied:
            fields["replied"] = outcome.replied
        if outcome.evidence:
            fields["evidence"] = outcome.evidence

        message = f"抢红包结果 {outcome.result.icon} {outcome.result.label}"
        if outcome.result in {GrabResult.SUCCESS, GrabResult.FAILED}:
            self.alog.info(message, **fields)
        elif outcome.result is GrabResult.ERROR:
            self.alog.error(message, **fields)
        else:
            self.alog.warning(message, **fields)

    def _submit_notify(self, task: PreparedTask, message: Any, outcome: GrabOutcome) -> None:
        assert self.notifier is not None
        variables = build_variables(
            message,
            {
                "task": task.label,
                "result_icon": outcome.result.icon + " ",
                "result_text": outcome.result.label,
                "strategy": outcome.strategy,
                "cost_ms": round(outcome.cost_ms, 1),
                "detail": outcome.detail,
                "button": outcome.button_text or "-",
                "code": outcome.code or "-",
                "replied": outcome.replied or "-",
            },
        )
        template = self.notify_config.template or DEFAULT_RED_PACKET_TEMPLATE
        text = render_template(template, variables)
        if outcome.replied:
            text += f"\n💬 已回复：{outcome.replied}"
        if self.notify_config.include_source_link and variables.get("link"):
            text += f"\n\n🔗 {variables['link']}"

        submitted = self.notifier.submit(
            NotifyTask(
                event="red_packet",
                text=text,
                context={
                    "result": outcome.result.value,
                    "chat": outcome.chat_title,
                    "task": task.label,
                },
            )
        )
        if submitted:
            self.alog.debug("已提交抢红包通知", result=outcome.result.value)

    def snapshot(self) -> dict[str, Any]:
        return {
            **self.stats,
            "pending_tasks": len(self._tasks),
            "watching_chats": self._bus.watching,
            "seen_cache": len(self._seen),
            #: 已定论、不再重复点的红包消息条数 —— 这个数字在涨，就说明
            #: 「同一红包被反复编辑」的浪费确实被挡住了。
            "settled": len(self._settled),
            #: 重启后从磁盘读回了几条已定论记录。刚重启时它不为 0，才说明落盘真的生效了
            #: （否则重启等于把"已经点过了"全忘掉，那条长驻红包又要被点一遍）。
            "settled_restored": self.restored_settled,
            # 任务维度的信息：面板/CLI 靠它显示"几条任务、哪条在干活"。
            "tasks": len(self.config.tasks),
            "active_tasks": len(self.prepared),
            "per_task": {k: dict(v) for k, v in self.task_stats.items()},
            # 全局动手时段：``in_window`` 是"现在动不动手"，面板与心跳都靠它回答
            # 「为什么没动静」。
            "window": self.config.window.describe(),
            "in_window": self.config.in_window,
        }


# --------------------------------------------------------------------------- #
def _button_kind(button: Any) -> str:
    for attr in ("url", "web_app", "login_url", "switch_inline_query", "callback_game"):
        if getattr(button, attr, None) is not None:
            return attr
    return "unknown"


def _verdict_detail(
    verdict: GrabResult, callback_text: Optional[str], evidence: Optional[str]
) -> str:
    if verdict is GrabResult.SUCCESS:
        return f"命中成功特征（{evidence or callback_text or '无证据文本'}）"
    if verdict is GrabResult.FAILED:
        return f"命中失败特征（{evidence or callback_text or '无证据文本'}）"
    if callback_text:
        return f"已点击但无法判定结果，机器人回显：{truncate(callback_text, 150, '…')}"
    return (
        "已动手但在等待窗口内没有收到可判定的消息。"
        "可在 red_packet.success 里补充该 bot 的成功/失败关键词，或增大 wait_timeout。"
    )


def _grab_hint(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    if "peer_id_invalid" in text:
        return "该会话不在 peer 缓存里，先在客户端打开一次这个群"
    if "chat_write_forbidden" in text or "chat_send_plain_forbidden" in text:
        return "本账号在该群被禁言或无发言权限"
    if "slowmode" in text:
        return "群开启了慢速模式，抢到后的回复可能发不出去"
    if "user_banned_in_channel" in text:
        return "本账号在该群被封禁"
    if "button_data_invalid" in text:
        return "按钮数据格式异常，可能不是标准 callback 按钮"
    return "查看错误类型定位原因"


__all__ = [
    "ChatEventBus",
    "GrabOutcome",
    "GrabResult",
    "PreparedTask",
    "RedPacketHunter",
]
