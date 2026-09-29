"""抢注任务：监听到注册码后按**步骤链**自动操作。

流程（全程不做阻塞式轮询）：

1. handler 收到消息 → 纯内存判断有没有注册码（预筛正则 + 提取正则）；
2. 命中后 ``create_task`` 派发，handler 立刻返回，不拖慢后续消息；
3. 按 ``steps`` 顺序执行步骤链：

   - ``send``：往目标会话发一条消息，模板支持 ``{code}`` 等变量；
   - ``click``：点掉某条消息上的内联按钮（按钮文字正则匹配）；
   - ``wait``：空等若干秒，给机器人留出处理时间；
   - ``wait_reply``：等目标会话的下一条消息，命中正则才算成功
     （事件驱动，复用 :class:`~tg_assistant.red_packet.ChatEventBus`）。

4. 同一条注册码在 ``code_ttl`` 内只处理一次 —— 同一条码经常被多个群同时转发出来，
   不去重就会把同一个码抢好几遍。

5. 「使用通知」反查：群里常有人用掉码之后由机器人播报
   ``🎟️ 注册码使用 - jf [7002057019] 使用了 MSKY-30-Register_f1t░░░░░░░``。
   尾部被遮罩、只有前几位可见，但足够把**已经用掉的码**认出来 —— 命中的码直接剔除，
   不跑步骤链，也不发通知（见 :func:`_is_used`）。

关于「目标会话」的默认值：链上维护一个 ``chain_target``，初始是**注册码所在的会话**；
``send`` 执行完会把它更新成刚刚发送到的会话。这样「发 /bind 给机器人 → 等机器人回执」
这种最常见的链，只有第一步需要写会话，后面的 ``wait_reply`` / ``click`` 自动跟着走。
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
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

from .client import with_flood_retry
from .config import (
    REG_GRAB_STEP_LABELS,
    AccountConfig,
    RegGrabConfig,
    RegGrabStep,
    RegGrabTask,
)
from .logging_setup import AccountLogger
from .matching import (
    DEFAULT_REG_GRAB_TEMPLATE,
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
from .red_packet import ChatEventBus


# --------------------------------------------------------------------------- #
# 结果类型
# --------------------------------------------------------------------------- #
class StepResult(str, Enum):
    """单步结果。"""

    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"

    @property
    def icon(self) -> str:
        return {"ok": "✅", "failed": "❌", "skipped": "⏭"}[self.value]


class ChainResult(str, Enum):
    """整条步骤链的结果。"""

    SUCCESS = "success"
    #: 链跑完了，但有 ``optional`` 步骤失败。
    PARTIAL = "partial"
    #: 关键步骤失败，链提前中止。
    FAILED = "failed"
    #: 压根没动手（配置不全等）。
    SKIPPED = "skipped"

    @property
    def icon(self) -> str:
        return {"success": "✅ ", "partial": "⚠️ ", "failed": "❌ ", "skipped": "⏭ "}[self.value]

    @property
    def label(self) -> str:
        return {
            "success": "成功",
            "partial": "部分成功",
            "failed": "失败",
            "skipped": "已跳过",
        }[self.value]


@dataclass
class StepOutcome:
    """一步的执行结果。"""

    index: int
    type: str
    label: str
    result: StepResult
    detail: str = ""
    cost_ms: float = 0.0


@dataclass
class ChainOutcome:
    """一条注册码的整条链结果。"""

    result: ChainResult
    code: str
    chat_id: Optional[int] = None
    chat_title: Optional[str] = None
    message_id: Optional[int] = None
    steps: list[StepOutcome] = field(default_factory=list)
    detail: str = ""
    cost_ms: float = 0.0
    #: 是哪条任务干的 —— 多任务之后，日志里不写这个就分不清是哪条链跑的。
    task: Optional[str] = None


# --------------------------------------------------------------------------- #
# 按钮小工具
# --------------------------------------------------------------------------- #
def iter_inline_buttons(message: Any) -> list[Any]:
    """取出消息上所有内联按钮（回复键盘不算 —— 那个不能点）。"""
    markup = getattr(message, "reply_markup", None)
    if markup is None:
        return []
    rows = getattr(markup, "inline_keyboard", None) or []
    buttons: list[Any] = []
    for row in rows:
        for button in row or []:
            buttons.append(button)
    return buttons


def button_label(button: Any) -> str:
    return str(getattr(button, "text", "") or "")


def step_label(step: RegGrabStep) -> str:
    """步骤在日志/界面里的名字：优先用备注，否则用类型中文名。"""
    return step.name.strip() or REG_GRAB_STEP_LABELS.get(step.type, step.type)


#: 遮罩字符：使用通知里的码尾部会被 ``░▒▓*•`` 之类盖掉。
#: 只剥**结尾**那一段（遮罩都在尾部），不碰中间 —— 免得把
#: ``..._f1t░░░abc`` 这种奇怪格式硬拼成一个不存在的码。
_MASK_TAIL = re.compile(r"[^A-Za-z0-9]+$")


def visible_code_part(token: str) -> str:
    """从使用通知里那个被遮罩的码中取出**可见部分**。

    ``MSKY-30-Register_f1t░░░░░░░`` → ``MSKY-30-Register_f1t``

    没有遮罩时原样返回（通知偶尔会印完整码）。
    """
    return _MASK_TAIL.sub("", (token or "").strip())


def code_value_of(token: str) -> str:
    """取码的**值**部分 —— 最后一个 ``_`` 之后那一段（统一小写）。

    两边必须按同一口径切，否则永远比不上：

    * 通知里是 ``MSKY-30-Register_f1t``，值是 ``f1t``；
    * 配置里的 ``code_pattern`` 通常只抓 ``f1tAbCdEfGh``，值就是它本身；
      没写捕获组时抓到 ``Register_f1tAbCdEfGh``，切完同样是 ``f1tAbCdEfGh``。

    这个口径假设「码的值里不含 ``_``」（``MSKY-30-Register_<10位字母数字>``
    这种格式成立）。若某天码里真的带下划线，``used_pattern`` 换成能直接圈出
    值部分的正则即可。
    """
    return (token or "").rsplit("_", 1)[-1].strip().lower()


# --------------------------------------------------------------------------- #
# 单个任务（预编译）
# --------------------------------------------------------------------------- #
@dataclass
class PreparedTask:
    """一条抢注任务 + 预编译好的正则、步骤链与会话集合。

    正则/步骤只编译一次、整个进程复用：抢注是秒级的事情，每条消息现编译正则
    会在最不该慢的地方拖后腿（对齐 :class:`~tg_assistant.red_packet.PreparedTask`）。
    """

    config: RegGrabTask
    chats: RefSet
    exclude_chats: RefSet
    text_patterns: list[Any]
    code_pattern: Optional[Any]
    used_pattern: Optional[Any]
    steps: list[RegGrabStep]
    #: 与 ``steps`` 一一对应的按钮正则（``click`` 用）。
    button_patterns: list[Optional[re.Pattern[str]]]
    #: 与 ``steps`` 一一对应的回执正则（``wait_reply`` 用）。
    reply_patterns: list[Optional[re.Pattern[str]]]

    @property
    def id(self) -> str:
        return self.config.id

    @property
    def label(self) -> str:
        return self.config.label

    @classmethod
    def build(cls, config: RegGrabTask) -> "PreparedTask":
        detect = config.detect
        steps = list(config.steps)
        return cls(
            config=config,
            chats=RefSet(config.chats),
            exclude_chats=RefSet(config.exclude_chats),
            text_patterns=compile_patterns(detect.text_patterns),
            code_pattern=(
                compile_user_pattern(detect.code_pattern) if detect.code_pattern else None
            ),
            used_pattern=(
                compile_user_pattern(detect.used_pattern) if detect.used_pattern else None
            ),
            steps=steps,
            button_patterns=[
                compile_user_pattern(step.button)
                if step.type == "click" and step.button
                else None
                for step in steps
            ],
            reply_patterns=[
                compile_user_pattern(step.pattern)
                if step.type == "wait_reply" and step.pattern
                else None
                for step in steps
            ],
        )


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #
class RegGrabHunter:
    """抢注引擎。"""

    #: 与转发（0）/ 抢红包（1）分开的 handler group，互不阻塞。
    HANDLER_GROUP = 2

    def __init__(
        self,
        client: Client,
        config: AccountConfig,
        alog: AccountLogger,
        notifier: Optional[BotNotifier] = None,
    ) -> None:
        self.client = client
        self.config: RegGrabConfig = config.reg_grab
        self.notify_config = config.notify
        self.alog = alog.bind("reggrab")
        self.notifier = notifier

        # 只编译**启用**的任务：停用的任务连正则都不该编译。
        self.prepared: list[PreparedTask] = [
            PreparedTask.build(task) for task in self.config.active_tasks
        ]

        self._bus = ChatEventBus()
        self._handlers: list[tuple[Any, int]] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._semaphore = asyncio.Semaphore(self.config.max_concurrency)
        #: 注册码 → 最近一次处理时间，用来避免同一条码被重复抢（账号级共享）。
        self._seen_codes: dict[str, float] = {}
        #: 「已被使用的码值」→ 记录时间（账号级共享）。使用通知里只有前几位可见，
        #: 所以存的是可见前缀；判重时按前缀比对（见 ``_is_used``）。
        self._used: dict[str, float] = {}
        #: 每个会话最近一条消息，供 ``click`` 在没有 ``wait_reply`` 时兜底找按钮。
        self._recent: dict[int, Any] = {}
        #: ``@username`` → chat_id 的缓存（``wait_reply`` 需要数字 id 才能订阅）。
        self._chat_ids: dict[str, int] = {}
        self._me_id: Optional[int] = None
        self._me_names: list[str] = []
        self.stats = {
            "detected": 0,
            "started": 0,
            "success": 0,
            "partial": 0,
            "failed": 0,
            "skipped": 0,
            "duplicate_code": 0,
            "usage_notices": 0,
            "used_skipped": 0,
            #: 发现了注册码，但因为不在**监听时段**内而没有动手的次数。
            "outside_window": 0,
            "steps_ok": 0,
            "steps_failed": 0,
        }
        #: 每条任务各自的计数。多任务之后「一共抢到 3 个」说明不了是哪条任务干的。
        self.task_stats: dict[str, dict[str, int]] = {
            task.id: dict.fromkeys(self.stats, 0) for task in self.config.active_tasks
        }

    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def watched_chats(self) -> list[Any]:
        """所有启用任务监听会话的并集；任一任务留空 ⇒ 空列表（= 不过滤，全监听）。"""
        return list(self.config.watched_chats)

    def _watch_chats(self) -> list[Any]:
        """handler 过滤器要覆盖的会话。

        = 所有任务的来源会话 + 步骤链里点名要操作的会话（通常是机器人私聊）。
        后者**必须**也收得到消息，否则 ``wait_reply`` 永远等不到回执。
        触发判断仍然只看每条任务的 ``chats``（见 :meth:`_should_grab_for`），
        所以多收不会误触发。

        返回空列表 = 不加过滤器（全部消息都进得来）。任一启用任务的 ``chats``
        留空时，:attr:`RegGrabConfig.watched_chats` 已返回 ``[]``，这里照样放行全部。
        """
        base = list(self.config.watched_chats)
        if not base:
            return []
        chats: list[Any] = list(base)
        for task in self.prepared:
            for step in task.steps:
                if step.chat is not None and step.chat not in chats:
                    chats.append(step.chat)
        return chats

    async def register(self) -> None:
        if not self.enabled:
            self.alog.info("抢注任务未启用")
            return

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

        chats = self._watch_chats()
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

        self.alog.info(
            "抢注引擎已注册",
            tasks=len(self.prepared),
            disabled_tasks=len(self.config.tasks) - len(self.prepared),
            task_labels=" | ".join(task.label for task in self.prepared) or "-",
            watched_chats=len(self.watched_chats()) or "全部",
            handler_chats=len(chats) or "全部",
            max_concurrency=self.config.max_concurrency,
        )
        for task in self.prepared:
            if not task.config.ready:
                # 配不全的任务照样注册（用户可能正在填），但必须**明确说出来**，
                # 否则用户只会看到"开了却没动静"。
                self.alog.warning(
                    "抢注任务配置不完整，不会执行",
                    task=task.label,
                    problem=task.config.problem,
                )
                continue
            self.alog.info(
                "抢注任务已就绪",
                task=task.label,
                chats=len(task.config.chats) or "全部",
                code_pattern=task.config.detect.code_pattern,
                text_patterns=len(task.text_patterns),
                used_pattern=task.config.detect.used_pattern or "（未启用使用通知判定）",
                used_min_len=task.config.detect.used_min_len,
                steps=len(task.steps),
                step_chain=" → ".join(step_label(step) for step in task.steps),
                delay_s=task.config.delay,
                jitter_s=task.config.jitter,
                delay_range_s=(
                    f"{task.config.delay:.1f}~{task.config.delay + task.config.jitter:.1f}"
                ),
                code_ttl_s=task.config.code_ttl,
                include_edited=task.config.include_edited,
                window=task.config.window.describe(),
                # 🔴 必须用 ``self._in_window(task)``（走可注入的 ``self._now()``），
                # **不能**用 ``task.config.in_window`` —— 后者是 ``window.contains()``，
                # 直接读真实时钟。两个时钟在线上一致，但在测试里不一致，于是这行日志
                # 会和实际判定各说各话。这行字存在的唯一意义就是回答
                # 「为什么没动静」，说错了比没有更坏。
                in_window=self._in_window(task),
            )

    async def close(self) -> None:
        for handler, group in self._handlers:
            with contextlib.suppress(Exception):
                self.client.remove_handler(handler, group)
        self._handlers.clear()
        if self._tasks:
            self.alog.info("等待在途抢注任务结束", pending=len(self._tasks))
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        self.alog.info("抢注引擎已停止", **self.stats)

    # ------------------------------------------------------------------ #
    async def _on_message(self, client: Client, message: Any) -> None:
        del client
        self._dispatch(message, edited=False)

    async def _on_edited(self, client: Client, message: Any) -> None:
        del client
        self._dispatch(message, edited=True)

    def _now(self) -> datetime:
        """当前**本地**时间。抽成方法是为了测试能注入固定时刻。

        ⚠️ 用 ``datetime.now()``（本地时区）而不是 ``utcnow()``：服务进程的
        ``TZ=Asia/Shanghai``，面板上填的 ``08:00`` 就是北京时间。换成 UTC 会让
        时段整体偏 8 小时 —— 而且是「看起来一切正常」的那种错。
        """
        return datetime.now()

    def _in_window(self, task: PreparedTask) -> bool:
        """此刻是否允许该任务动手。``window.enabled=False`` 时恒为 ``True``（全天）。"""
        return task.config.window.contains(self._now())

    def _dispatch(self, message: Any, *, edited: bool) -> None:
        chat_id, _, _ = chat_identity(message)
        if chat_id is None:
            return

        # 先记下「这个会话最近一条消息」，再喂给等待回执的步骤 —— 顺序不能反：
        # wait_reply 收到消息后若紧接着 click，click 要找的就是这条。
        self._recent[chat_id] = message
        if len(self._recent) > 256:
            self._recent.pop(next(iter(self._recent)), None)
        self._bus.feed(chat_id, message)

        # 「使用通知」要先记下来：它自己提不出码（尾部被遮罩），
        # 但它决定了**后面出现的码还要不要抢**。
        self._note_usage(message, chat_id)

        started = time.perf_counter()
        # 只在**此刻处于自己监听时段内**的任务里找命中。
        #
        # 🔴 时段是「这条任务此刻到底算不算数」的一部分，不是命中之后的额外过滤。
        #    若先取「首命中」再判时段，排在前面的任务只要 ``chats`` 覆盖到了这条
        #    消息，就能在自己时段外把码**吃掉**，排在后面、本正处于自己时段内的
        #    任务永远轮不到 —— 用户专门为夜班建的那条任务会一直不工作，还没有任何
        #    提示。这正是「时段外的目击不该占用机会」那条规则的跨任务版本。
        #
        # 为什么要有这个开关：抢注是秒级响应的行为，半夜三点还能精准抢到码是脚本
        # 最好认的特征之一。把动手时间限制在人类活动时段能明显降低被识别的概率。
        #
        # 时段判定必须在下文「记去重名额」**之前**：时段外把名额占掉的话，时段内
        # 同一条码再来时会被当成重复而跳过，等于白白错过一次机会。
        hit = self._match(message, chat_id)
        if hit is None:
            # 没任务可动手。如果其实是「命中了但都不在自己的时段内」，要说清楚，
            # 否则用户看到的就是「明明发现了码却什么也没发生」。
            self._report_out_of_window(message, chat_id)
            return

        task, code = hit
        _, _, chat_title = chat_identity(message)

        # 群里已经播报过「这个码被用掉了」—— 再抢就是白跑一趟，
        # 还会在群里多留一次脚本痕迹，直接剔除。
        used = self._is_used(code, task)
        if used is not None:
            self.stats["used_skipped"] += 1
            self.task_stats[task.id]["used_skipped"] += 1
            self.alog.info(
                "该注册码群里已有使用通知，剔除",
                task=task.label,
                code=code,
                used_visible=used,
                chat=chat_title or chat_id,
                message_id=getattr(message, "id", None),
            )
            return

        now = time.monotonic()
        key = code.strip().lower()
        last = self._seen_codes.get(key)
        if last is not None and now - last < task.config.code_ttl:
            self.stats["duplicate_code"] += 1
            self.task_stats[task.id]["duplicate_code"] += 1
            self.alog.debug(
                "该注册码最近已处理过，跳过",
                task=task.label,
                code=code,
                chat_id=chat_id,
                ttl_s=task.config.code_ttl,
            )
            return
        self._seen_codes[key] = now
        if len(self._seen_codes) > 4096:
            cutoff = now - max(task.config.code_ttl, 300.0)
            self._seen_codes = {k: v for k, v in self._seen_codes.items() if v > cutoff}

        self.stats["detected"] += 1
        self.task_stats[task.id]["detected"] += 1
        message_id = getattr(message, "id", None)
        self.alog.info(
            "发现注册码",
            task=task.label,
            chat=chat_title or chat_id,
            message_id=message_id,
            code=code,
            edited=edited,
            steps=len(task.steps),
            detect_ms=round((time.perf_counter() - started) * 1000, 3),
            preview=truncate(message_text(message), 80, "…"),
        )

        job = asyncio.create_task(self._run(task, message, code, started))
        self._tasks.add(job)
        job.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------ #
    def _match(self, message: Any, chat_id: int) -> Optional[tuple[PreparedTask, str]]:
        """按任务顺序找第一条**命中且此刻处于自己监听时段内**的任务。

        🔴 **只取第一条**：同一条注册码可能同时落进多条任务的监听范围，若每条都
        去跑一遍步骤链，等于对同一个码重复 ``/bind``，机器人只会回「已注册」，
        还平白多留脚本痕迹。任务列表顺序即优先级。

        ⚠️ 时段外的任务**不算命中**（直接跳过、顺延给后面的任务），理由见
        :meth:`_dispatch` 里那段注释 —— 否则前序任务能在时段外把码吃掉，让后面
        正处于自己时段内的任务永远轮不到。
        """
        for task in self.prepared:
            code = self._should_grab_for(task, message, chat_id)
            if code is None:
                continue
            if not self._in_window(task):
                continue
            return task, code
        return None

    def _match_ignoring_window(
        self, message: Any, chat_id: int
    ) -> Optional[tuple[PreparedTask, str]]:
        """第一条命中该消息的任务，**不管它在不在时段内**。

        只用来回答「这条码其实有人能抢，只是都不在时段内」这一种情况（提示与计数），
        不参与是否动手的决策。
        """
        for task in self.prepared:
            code = self._should_grab_for(task, message, chat_id)
            if code is not None:
                return task, code
        return None

    def _report_out_of_window(self, message: Any, chat_id: int) -> None:
        """没有可动手的任务时，若原因是「命中但不在时段内」，记一笔并说清楚。"""
        hit = self._match_ignoring_window(message, chat_id)
        if hit is None:
            return
        task, code = hit
        self.stats["outside_window"] += 1
        self.task_stats[task.id]["outside_window"] += 1
        _, _, chat_title = chat_identity(message)
        self.alog.info(
            "发现注册码，但不在监听时段内，跳过",
            task=task.label,
            chat=chat_title or chat_id,
            message_id=getattr(message, "id", None),
            code=code,
            window=task.config.window.describe(),
        )

    def _should_grab(self, message: Any, chat_id: int) -> Optional[str]:
        """兼容旧接口：返回第一条命中任务提取到的注册码（没有则 ``None``）。

        ⚠️ 这里**不看时段** —— 它回答的是「正则能不能把码提出来」。是否动手由
        :meth:`_match` 决定；两者分开，才能把「不匹配」和「匹配但不在时段内」
        讲成两件不同的事。
        """
        hit = self._match_ignoring_window(message, chat_id)
        return hit[1] if hit is not None else None

    def _should_grab_for(
        self, task: PreparedTask, message: Any, chat_id: int
    ) -> Optional[str]:
        """判断这条消息要不要按**这条任务**抢注；要的话返回提取到的注册码。"""
        config = task.config
        _, chat_username, _ = chat_identity(message)
        if task.exclude_chats and task.exclude_chats.matches(chat_id, chat_username):
            return None
        if task.chats and not task.chats.matches(chat_id, chat_username):
            return None

        _, _, is_self, is_bot = sender_of(message)
        if config.detect.ignore_self and is_self:
            return None
        if config.detect.only_from_bots and not is_bot:
            return None
        if getattr(message, "service", None):
            return None

        text = message_text(message)
        if task.text_patterns and first_match(task.text_patterns, text) is None:
            return None
        return self._extract_code(task, text)

    @staticmethod
    def _extract_code(task: PreparedTask, text: str) -> Optional[str]:
        """按任务的 ``code_pattern`` 提取注册码：第一个非空捕获组，没有捕获组就用整个匹配。"""
        pattern = task.code_pattern
        if pattern is None:
            return None
        found = pattern.search(text)
        if not found:
            return None
        if found.groups():
            for group in found.groups():
                if group:
                    return group
        return found.group(0)

    # ------------------------------------------------------------------ #
    def _note_usage(self, message: Any, chat_id: int) -> None:
        """记下「注册码已被使用」通知里露出的那几位（账号级共享 ``_used``）。

        通知形如 ``🎟️ 注册码使用 - jf [7002057019] 使用了 MSKY-30-Register_f1t░░░░░░░``：
        尾部被遮罩，只留前几位。存进 ``_used`` 供后面的码比对。

        多任务后各任务可能有各自的 ``used_pattern`` / ``used_min_len``；这里对每条
        启用任务的正则各扫一遍，命中的可见前缀都汇进账号级 ``_used``。同一条通知里
        同一个值只记一次（避免多任务重复计数）。
        """
        text = message_text(message)
        if not text:
            return

        now = time.monotonic()
        recorded: set[str] = set()
        for task in self.prepared:
            pattern = task.used_pattern
            if pattern is None:
                continue
            min_len = task.config.detect.used_min_len
            for found in pattern.finditer(text):
                token = (
                    next((group for group in found.groups() if group), None)
                    or found.group(0)
                )
                visible = visible_code_part(token)
                value = code_value_of(visible)
                if len(value) < min_len:
                    # 只露一两位时几乎任何码都能「对得上」，宁可不记。
                    self.alog.debug(
                        "使用通知可见位数太少，忽略",
                        task=task.label,
                        token=visible,
                        visible=value,
                        min_len=min_len,
                    )
                    continue
                if value in recorded:
                    continue
                recorded.add(value)
                is_new = value not in self._used
                self._used[value] = now
                self.stats["usage_notices"] += 1
                self.task_stats[task.id]["usage_notices"] += 1
                if is_new:
                    _, _, chat_title = chat_identity(message)
                    self.alog.info(
                        "记录注册码使用通知",
                        task=task.label,
                        visible=visible,
                        code_prefix=value,
                        chat=chat_title or chat_id,
                        message_id=getattr(message, "id", None),
                    )

    def _is_used(self, code: str, task: PreparedTask) -> Optional[str]:
        """这个码是不是已经出现在使用通知里了？是的话返回匹配到的可见前缀。

        ``_used`` 是账号级共享的，但判定口径（``used_min_len`` / ``code_ttl``）取自
        命中的那条任务。通知里只有前几位可见，所以按**前缀**比对（``code`` 以可见
        部分开头，或者反过来 —— 通知印了完整码而配置只抓了前几位）。
        """
        if not self._used:
            return None

        min_len = task.config.detect.used_min_len
        probe = code_value_of(code)
        if len(probe) < min_len:
            return None

        now = time.monotonic()
        ttl = task.config.code_ttl
        if ttl > 0:
            cutoff = now - ttl
            for key in [k for k, seen in self._used.items() if seen < cutoff]:
                del self._used[key]

        for value in self._used:
            if len(value) < min_len:
                continue
            if probe.startswith(value) or value.startswith(probe):
                return value
        return None

    # ------------------------------------------------------------------ #
    async def _run(
        self, task: PreparedTask, message: Any, code: str, started: float
    ) -> ChainOutcome:
        """执行一条注册码的完整步骤链，返回整条链的结果。"""
        delay = task.config.delay + (
            random.uniform(0, task.config.jitter) if task.config.jitter else 0.0
        )
        if delay > 0:
            self.alog.debug(
                "按配置延迟后再抢注", task=task.label, delay_s=round(delay, 3), code=code
            )
            await asyncio.sleep(delay)

        chat_id, _, chat_title = chat_identity(message)
        variables = build_variables(message, {"code": code})
        outcome = ChainOutcome(
            result=ChainResult.SUCCESS,
            code=code,
            chat_id=chat_id,
            chat_title=chat_title,
            message_id=getattr(message, "id", None),
            task=task.label,
        )
        self.stats["started"] += 1
        self.task_stats[task.id]["started"] += 1

        # 延迟可能刚好把动手时刻推到了时段之外（比如 22:59:59 派发、23:00:01 才跑）。
        # 抢注的价值全在「准点」，多等 2 秒也抢不到，但半夜动手会留下脚本痕迹 ⇒ 直接放弃。
        if not self._in_window(task):
            outcome.result = ChainResult.SKIPPED
            outcome.detail = (
                f"延迟结束时已不在监听时段（{task.config.window.describe()}），已放弃"
            )
            outcome.cost_ms = (time.perf_counter() - started) * 1000
            self.stats["outside_window"] += 1
            self.task_stats[task.id]["outside_window"] += 1
            self._record(task, outcome)
            self._log_outcome(outcome)
            return outcome

        # 延迟期间群里可能已经冒出使用通知 —— 这段等待正好是个免费的检测窗口：
        # 真被别人抢了，在这里刹车就行，不必等步骤链跑完才发现白干。
        used = self._is_used(code, task)
        if used is not None:
            outcome.result = ChainResult.SKIPPED
            outcome.detail = f"延迟期间群里出现了使用通知（可见 {used}），已放弃"
            outcome.cost_ms = (time.perf_counter() - started) * 1000
            self.stats["used_skipped"] += 1
            self.task_stats[task.id]["used_skipped"] += 1
            self._record(task, outcome)
            self._log_outcome(outcome)
            return outcome

        #: 链上的「当前消息」：初始是注册码那条；wait_reply 会把它换成收到的回执。
        current = message
        #: 链上的「当前目标会话」：初始是注册码所在会话；send 会把它换成刚发到的会话。
        chain_target: Any = chat_id

        async with self._semaphore:
            for index, step in enumerate(task.steps, start=1):
                variables["step"] = step_label(step)
                variables["step_index"] = index
                if step.delay > 0:
                    await asyncio.sleep(step.delay)

                step_started = time.perf_counter()
                target = step.chat if step.chat is not None else chain_target
                try:
                    detail, current, chain_target = await self._run_step(
                        task, index, step, message, current, chain_target, variables
                    )
                    result = StepResult.OK
                except Exception as exc:  # noqa: BLE001 - 单步失败不能拖垮整条链的记账
                    result = StepResult.FAILED
                    detail = self._failure_detail(exc)
                cost_ms = (time.perf_counter() - step_started) * 1000

                outcome.steps.append(
                    StepOutcome(
                        index=index,
                        type=step.type,
                        label=step_label(step),
                        result=result,
                        detail=detail,
                        cost_ms=cost_ms,
                    )
                )
                if result is StepResult.OK:
                    self.stats["steps_ok"] += 1
                    self.task_stats[task.id]["steps_ok"] += 1
                    self.alog.info(
                        "抢注步骤完成",
                        task=task.label,
                        step=index,
                        total=len(task.steps),
                        type=REG_GRAB_STEP_LABELS.get(step.type, step.type),
                        name=step_label(step),
                        target=str(target),
                        cost_ms=round(cost_ms, 1),
                        detail=truncate(detail, 200, "…"),
                    )
                    continue

                self.stats["steps_failed"] += 1
                self.task_stats[task.id]["steps_failed"] += 1
                self.alog.warning(
                    "抢注步骤失败",
                    task=task.label,
                    step=index,
                    total=len(task.steps),
                    type=REG_GRAB_STEP_LABELS.get(step.type, step.type),
                    name=step_label(step),
                    target=str(target),
                    optional=step.optional,
                    cost_ms=round(cost_ms, 1),
                    detail=truncate(detail, 200, "…"),
                )
                if step.optional:
                    outcome.result = ChainResult.PARTIAL
                    continue
                outcome.result = ChainResult.FAILED
                break

        outcome.cost_ms = (time.perf_counter() - started) * 1000
        outcome.detail = self._summarize(outcome)
        self._record(task, outcome)
        self._log_outcome(outcome)
        # SKIPPED 只记日志不推通知：码已经被别人用掉这种事，推给用户纯属噪音。
        if (
            task.config.notify
            and self.notifier is not None
            and outcome.result is not ChainResult.SKIPPED
        ):
            self._submit_notify(task, message, outcome)
        return outcome

    async def _run_step(
        self,
        task: PreparedTask,
        index: int,
        step: RegGrabStep,
        source: Any,
        current: Any,
        chain_target: Any,
        variables: dict[str, Any],
    ) -> tuple[str, Any, Any]:
        """执行一步，返回 ``(说明, 新的当前消息, 新的目标会话)``。"""
        target = step.chat if step.chat is not None else chain_target

        if step.type == "send":
            return await self._step_send(step, variables, target, current)

        if step.type == "click":
            detail, message = await self._step_click(task, index, step, target, current)
            return detail, message, chain_target

        if step.type == "wait":
            if step.seconds > 0:
                await asyncio.sleep(step.seconds)
            return f"等待 {step.seconds}s", current, chain_target

        if step.type == "wait_reply":
            detail, message = await self._step_wait_reply(task, index, step, target)
            return detail, message, chain_target

        raise ValueError(f"未知步骤类型 {step.type!r}")

    # ------------------------------------------------------------------ #
    async def _step_send(
        self,
        step: RegGrabStep,
        variables: dict[str, Any],
        target: Any,
        current: Any,
    ) -> tuple[str, Any, Any]:
        """发一条消息；链目标会话随之切到发送目标。"""
        text = render_template(step.text or "", variables).strip()
        if not text:
            raise ValueError("渲染后内容为空，检查模板里的变量名是否写错")

        # 先把 @username 解析成数字 id：后面的 wait_reply 必须靠数字 id 才能订阅消息。
        # 解析不到也不拦着发送（只发不等的链照样能用），只是记一条警告。
        resolved = await self._resolve_chat_id(target)
        if resolved is None:
            self.alog.warning(
                "无法解析目标会话，将直接把消息发给原样引用",
                chat=str(target),
                hint="先手动在客户端打开一次这个会话，或直接填数字 id",
            )
            send_to: Any = target
            new_target: Any = target
        else:
            send_to = resolved
            new_target = resolved

        async def _do() -> Any:
            return await self.client.send_message(chat_id=send_to, text=text)

        sent = await with_flood_retry(
            _do,
            alog=self.alog,
            action="发送抢注消息",
            retries=1,
            max_flood_wait=10.0,
        )
        self.alog.info("已发送抢注消息", chat=str(send_to), text=truncate(text, 120, "…"))
        return f"已发送到 {send_to}：{truncate(text, 80, '…')}", sent, new_target

    async def _step_click(
        self,
        task: PreparedTask,
        index: int,
        step: RegGrabStep,
        target: Any,
        current: Any,
    ) -> tuple[str, Any]:
        """点掉一个内联按钮：先在「当前消息」上找，再退回目标会话最近一条消息。"""
        pattern = task.button_patterns[index - 1]
        if pattern is None:  # pragma: no cover - 配置校验已经拦住了
            raise ValueError("click 步骤缺少按钮正则")

        message = self._pick_button_message(pattern, target, current)
        if message is None:
            raise ValueError(
                "当前消息和目标会话最近的消息上都没有匹配的内联按钮"
                "（可在这一步之前加一个「等待回复」把机器人的回执接住）"
            )
        button = self._match_button(message, pattern)
        if button is None:  # pragma: no cover - _pick_button_message 已保证能找到
            raise ValueError("按钮在匹配后又消失了")

        chat_id, _, _ = chat_identity(message)
        callback_data = getattr(button, "callback_data", None)
        if callback_data is None:
            raise ValueError(f"按钮「{button_label(button)}」不是可点击的 callback 按钮（多半是个链接）")

        async def _do() -> Any:
            return await self.client.request_callback_answer(
                chat_id=chat_id,
                message_id=message.id,
                callback_data=callback_data,
                timeout=10,
            )

        answer = await with_flood_retry(
            _do,
            alog=self.alog,
            action="点击抢注按钮",
            retries=1,
            max_flood_wait=10.0,
            expected_errors=(QueryIdInvalid, DataInvalid, MessageIdInvalid, BotResponseTimeout),
        )
        text = getattr(answer, "message", None)
        shown = f"，回显：{truncate(str(text), 80, '…')}" if text else ""
        self.alog.info("已点击抢注按钮", button=button_label(button), chat_id=chat_id)
        return f"已点击「{button_label(button)}」{shown}", message

    async def _step_wait_reply(
        self,
        task: PreparedTask,
        index: int,
        step: RegGrabStep,
        target: Any,
    ) -> tuple[str, Any]:
        """等目标会话的下一条消息；命中正则才算成功。"""
        pattern = task.reply_patterns[index - 1]
        chat_id = await self._resolve_chat_id(target)
        if chat_id is None:
            raise ValueError(
                f"无法解析会话 {target!r}，等不到它的回复"
                "（先手动在客户端打开一次这个会话，或直接填数字 id）"
            )

        timeout = step.timeout
        deadline = time.monotonic() + timeout
        async with self._bus.watch(chat_id) as queue:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(self._reply_timeout_hint(step, timeout, pattern))
                try:
                    incoming = await asyncio.wait_for(queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    raise TimeoutError(self._reply_timeout_hint(step, timeout, pattern)) from None

                # 自己发出去的那条不算回执 —— 否则 "/bind MSKY-..." 会被自己的消息命中。
                if sender_of(incoming)[2]:
                    continue
                text = message_text(incoming)
                if pattern is None or pattern.search(text):
                    self.alog.info(
                        "等到目标会话的回复",
                        chat_id=chat_id,
                        message_id=getattr(incoming, "id", None),
                        preview=truncate(text, 120, "…"),
                    )
                    return f"收到回复：{truncate(text, 80, '…')}", incoming

    @staticmethod
    def _reply_timeout_hint(
        step: RegGrabStep, timeout: float, pattern: Optional[re.Pattern[str]]
    ) -> str:
        if pattern is None:
            return f"{timeout}s 内没有等到任何回复"
        return f"{timeout}s 内没有等到匹配 /{pattern.pattern}/ 的回复"

    # ------------------------------------------------------------------ #
    def _pick_button_message(
        self, pattern: re.Pattern[str], target: Any, current: Any
    ) -> Optional[Any]:
        """按「当前消息 → 目标会话最近一条消息」的顺序找带匹配按钮的消息。"""
        if current is not None and self._match_button(current, pattern) is not None:
            return current

        chat_id = target if isinstance(target, int) else self._chat_ids.get(str(target))
        if chat_id is None:
            return None
        recent = self._recent.get(chat_id)
        if recent is not None and self._match_button(recent, pattern) is not None:
            return recent
        return None

    @staticmethod
    def _match_button(message: Any, pattern: re.Pattern[str]) -> Optional[Any]:
        for button in iter_inline_buttons(message):
            if pattern.search(button_label(button)):
                return button
        return None

    async def _resolve_chat_id(self, ref: Any) -> Optional[int]:
        """把会话引用解析成数字 id；``@username`` 的结果会缓存下来。"""
        if isinstance(ref, int):
            return ref
        if ref is None:
            return None
        key = str(ref)
        cached = self._chat_ids.get(key)
        if cached is not None:
            return cached
        try:
            chat = await self.client.get_chat(ref)
        except Exception as exc:  # noqa: BLE001 - 解析失败只影响这一条链
            self.alog.warning(
                "解析会话失败",
                chat=key,
                error=f"{type(exc).__name__}: {exc}",
            )
            return None
        chat_id = getattr(chat, "id", None)
        if isinstance(chat_id, int):
            self._chat_ids[key] = chat_id
            return chat_id
        return None

    # ------------------------------------------------------------------ #
    @staticmethod
    def _failure_detail(exc: Exception) -> str:
        text = f"{type(exc).__name__}: {exc}"
        lowered = text.lower()
        if "chat_write_forbidden" in lowered or "chat_send_plain_forbidden" in lowered:
            return text + " —— 本账号在该会话没有发言权限"
        if "peer_id_invalid" in lowered:
            return text + " —— 该会话不在 peer 缓存里，先在客户端打开一次"
        if "user_banned" in lowered:
            return text + " —— 本账号已被封禁"
        if "button_data_invalid" in lowered:
            return text + " —— 按钮数据异常，可能不是标准 callback 按钮"
        return text

    @staticmethod
    def _summarize(outcome: ChainOutcome) -> str:
        total = len(outcome.steps)
        ok = sum(1 for step in outcome.steps if step.result is StepResult.OK)
        failed = [step for step in outcome.steps if step.result is StepResult.FAILED]
        if outcome.result is ChainResult.SUCCESS:
            return f"全部 {total} 步执行成功"
        if outcome.result is ChainResult.PARTIAL:
            return f"{ok}/{total} 步成功，{len(failed)} 步失败（已按配置忽略）"
        if failed:
            first = failed[0]
            return f"第 {first.index} 步「{first.label}」失败：{truncate(first.detail, 200, '…')}"
        return "没有可执行的步骤"

    def _record(self, task: PreparedTask, outcome: ChainOutcome) -> None:
        key = {
            ChainResult.SUCCESS: "success",
            ChainResult.PARTIAL: "partial",
            ChainResult.FAILED: "failed",
            ChainResult.SKIPPED: "skipped",
        }[outcome.result]
        self.stats[key] += 1
        self.task_stats[task.id][key] += 1

    def _log_outcome(self, outcome: ChainOutcome) -> None:
        chain = " → ".join(
            f"{step.index}.{step.label}:{step.result.value}" for step in outcome.steps
        )
        fields: dict[str, Any] = {
            "task": outcome.task or "-",
            "code": outcome.code,
            "chat": outcome.chat_title or outcome.chat_id,
            "message_id": outcome.message_id,
            "steps": len(outcome.steps),
            "chain": chain or "-",
            "cost_ms": round(outcome.cost_ms, 1),
            "detail": truncate(outcome.detail, 240, "…"),
        }
        message = f"抢注{outcome.result.label}：{outcome.result.icon}{outcome.code}"
        if outcome.result is ChainResult.SUCCESS:
            self.alog.info(message, **fields)
        elif outcome.result is ChainResult.SKIPPED:
            # 被别人抢先 / 码已被用掉 —— 这是预期内的情况，不是错误。
            self.alog.info(message, **fields)
        elif outcome.result is ChainResult.PARTIAL:
            self.alog.warning(message, **fields)
        else:
            self.alog.error(message, **fields)

    def _submit_notify(
        self, task: PreparedTask, source: Any, outcome: ChainOutcome
    ) -> None:
        assert self.notifier is not None
        variables = build_variables(
            source,
            {
                "task": task.label,
                "result_icon": outcome.result.icon,
                "result_text": outcome.result.label,
                "code": outcome.code,
                "cost_ms": round(outcome.cost_ms, 1),
                "detail": outcome.detail,
                "steps": len(outcome.steps),
                "chain": " → ".join(f"{s.index}.{s.label}:{s.result.icon}" for s in outcome.steps),
            },
        )
        template = self.notify_config.template or DEFAULT_REG_GRAB_TEMPLATE
        text = render_template(template, variables)
        if self.notify_config.include_source_link and variables.get("link"):
            text += f"\n\n🔗 {variables['link']}"

        submitted = self.notifier.submit(
            NotifyTask(
                event="reg_grab",
                text=text,
                context={
                    "task": task.id,
                    "result": outcome.result.value,
                    "code": outcome.code,
                },
            )
        )
        if not submitted:
            self.alog.debug(
                "抢注通知未提交（通知未启用或未勾选 reg_grab 事件）",
                task=task.label,
                result=outcome.result.value,
            )

    def snapshot(self) -> dict[str, Any]:
        cfg = self.config
        return {
            **self.stats,
            "pending_tasks": len(self._tasks),
            "watching_chats": self._bus.watching,
            "seen_codes": len(self._seen_codes),
            "used_codes": len(self._used),
            "tasks": len(cfg.tasks),
            "active_tasks": len(self.prepared),
            "per_task": {k: dict(v) for k, v in self.task_stats.items()},
            #: 每条启用任务当前是否在监听时段内 —— 面板/CLI 用它解释「为什么现在没动静」。
            "windows": {task.id: self._in_window(task) for task in self.prepared},
            #: 只要有任意一条任务在时段内就算「在时段内」，兼容旧面板/CLI 的单值读取。
            "in_window": any(self._in_window(task) for task in self.prepared),
        }


__all__ = [
    "ChainOutcome",
    "ChainResult",
    "PreparedTask",
    "RegGrabHunter",
    "StepOutcome",
    "StepResult",
    "button_label",
    "code_value_of",
    "iter_inline_buttons",
    "step_label",
    "visible_code_part",
]
