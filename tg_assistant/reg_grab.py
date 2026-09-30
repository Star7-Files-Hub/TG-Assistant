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
from .metrics import MetricsStore
from .config import (
    DEFAULT_REG_GRAB_LINK_PATTERN,
    REG_GRAB_MASK_CHARS,
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

#: 「使用通知」里的码怎么切、怎么比对，两个引擎共用同一套口径（见该模块的说明）。
#: 这里再导出，既有的调用方 ``from .reg_grab import code_value_of`` 与测试不受影响。
from .used_codes import code_value_of, visible_code_part  # noqa: F401


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
# 步骤控制：故意跳过 ≠ 失败
# --------------------------------------------------------------------------- #
class _StepSkipped(Exception):
    """这一步按配置**故意没做**，不是失败。

    典型场景（``open_link``）：执行账号不是当前账号、payload 已经点过、
    消息里根本没有可点的深链。用一个独立异常而不是给 ``_run_step`` 加一个
    "跳过了"的返回值：那个签名是 ``(说明, 当前消息, 目标会话)``，加第四位会传染到
    每一个步骤；异常只在真正需要跳过的步骤里抛出。

    跳过**不**把整条链标成 PARTIAL：用户要的语义是"这一步没做"，而不是"部分成功"。
    """


class OpenLinkThrottled(RuntimeError):
    """点开深链被限流、且等不到下一个额度窗口（**这是失败**，要通知用户）。

    和 :class:`_StepSkipped` 分开：跳过是"本来就不该我做"，限流放弃是"本来该抢却
    没抢到" —— 后者必须推一条通知，否则用户以为一切正常。
    """


#: 点开深链的限流窗口（秒）。用户拍板「每分钟最多 N 次」，所以窗口固定 60 秒。
_OPEN_LINK_WINDOW = 60.0
#: 排队等待上限（秒）。抢注是秒级的事：等一分钟这条码早没了，而且会把并发槽
#: 一直占着；超过这个上限就放弃并通知。
_OPEN_LINK_MAX_WAIT = 30.0
#: 链接带遮罩字符（``░▒▓█``，连出现两次以上）就**不点**：那种码多半已经被用掉，
#: 点它只是白跑一趟还多留一次痕迹。默认深层正则里已有尾部守卫，这里是兜底 ——
#: 用户自定义 ``link_pattern`` 时守卫不能跟着一起消失。
_MASKED_PAYLOAD = re.compile(f"[{REG_GRAB_MASK_CHARS}]{{2,}}")
#: 只有 payload 里含它才点（用户拍板：``-Register_`` 那种一律不点）。
_OPEN_LINK_MARKER = "-Renew_"


# --------------------------------------------------------------------------- #
# 注册码提取（引擎与面板共用同一条口径）
# --------------------------------------------------------------------------- #
def _ensure_pattern(pattern: str | re.Pattern[str]) -> re.Pattern[str]:
    """把「用户写的正则字符串」或「预编译好的正则」统一成正则对象。

    引擎里正则只编译一次、整个进程复用；面板的「测试提取」拿到的却是字符串，
    所以两种入参都得接受。关键是编译时**必须**用 ``compile_user_pattern``
    （与 :meth:`PreparedTask.build` 同一份 flags）—— 面板要是自己 ``re.compile``
    一套标志，它会信誓旦旦地说「能匹配」，而真正动手的引擎匹配不上。
    """
    if isinstance(pattern, str):
        return compile_user_pattern(pattern)
    return pattern


def _code_and_group(found: re.Match[str]) -> tuple[str, int]:
    """「抓取规则」的**唯一**实现处：第一个非空捕获组；一个非空组都没有才用整段。

    返回 ``(注册码, 来自第几个捕获组)``，组号 ``0`` 表示整段匹配。

    🔴 这条规则是对外契约，不是实现细节，别"顺手"改成整段匹配：线上有任务的正则
    把捕获组只括在后缀上（``(?:Whitelist)_([A-Za-z0-9]{10})`` ⇒ 只拿到
    ``4rLuucEgs5``），用户的困惑来自**正则**，不是引擎抓错了。改成整段匹配会一次性
    改坏所有靠捕获组拼 ``{code}`` 的老任务。正确做法是把这件事显示清楚
    —— 见 :func:`extract_code_detail` 与抢注面板的「测试提取」。
    """
    for index, group in enumerate(found.groups(), start=1):
        if group:
            return group, index
    return found.group(0), 0


def extract_code(pattern: Optional[str | re.Pattern[str]], text: str) -> Optional[str]:
    """从文本里提取注册码；没匹配上返回 ``None``。

    抓的是**第一个非空捕获组**（``(?:...)`` 是「不捕获组」，不算），正则里一个
    捕获组都没写时才退回整个匹配。
    """
    if pattern is None:
        return None
    found = _ensure_pattern(pattern).search(text)
    if not found:
        return None
    return _code_and_group(found)[0]


def extract_code_detail(
    pattern: Optional[str | re.Pattern[str]], text: str
) -> dict[str, Any]:
    """试提取的**详细**结果，供面板把「到底抓到了什么」摊开给用户看。

    返回 ``{"matched", "full", "groups", "code", "from_group"}``：
    ``full`` 是整段匹配、``groups`` 是全部捕获组（``None`` 原样保留，用 ``(x)?``
    写的可选组也在列表里，面板按序号渲染即可）、``code`` 是引擎真正会用的值、
    ``from_group`` 是它来自第几个捕获组（``0`` = 整段匹配）。没匹配上时 ``full``
    与 ``code`` 都是 ``None``、``groups`` 是空列表。

    ``code`` 走的就是 :func:`extract_code` 用的 :func:`_code_and_group` —— 面板与
    引擎不可能给出两个不一样的答案（这正是用户投诉「抓到的跟我想要的不一样」时
    最需要的保证）。

    正则写错时 ``re.error`` 会往外抛，由调用方（HTTP 层）翻成一句人话。
    """
    compiled = _ensure_pattern(pattern) if pattern is not None else None
    found = compiled.search(text) if compiled is not None else None
    if found is None:
        return {
            "matched": False,
            "full": None,
            "groups": [],
            "code": None,
            "from_group": None,
        }
    code, from_group = _code_and_group(found)
    return {
        "matched": True,
        "full": found.group(0),
        "groups": list(found.groups()),
        "code": code,
        "from_group": from_group,
    }


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
    #: 与 ``steps`` 一一对应的深链正则（``open_link`` 用）。留空时用
    #: :data:`~tg_assistant.config.DEFAULT_REG_GRAB_LINK_PATTERN` —— 解析放在这里，
    #: 用户配置里就只留一个"没填"的空值，默认值将来要改也不用动已存的配置。
    link_patterns: list[Optional[re.Pattern[str]]]
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
            link_patterns=[
                # 留空 ⇒ 用默认深链正则（只认 -Renew_、带遮罩守卫）。编译标志与
                # 其它用户正则一致：多行模式、区分大小写（深链是 URL，别放宽）。
                compile_user_pattern(step.link_pattern or DEFAULT_REG_GRAB_LINK_PATTERN)
                if step.type == "open_link"
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
        metrics: Optional[Any] = None,
    ) -> None:
        self.client = client
        self.config: RegGrabConfig = config.reg_grab
        self.notify_config = config.notify
        self.alog = alog.bind("reggrab")
        self.notifier = notifier
        #: 数据大盘（多账号共享同一个实例）。不传就自己建个纯内存的，理由同上。
        self.metrics = metrics if metrics is not None else MetricsStore()
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
        #: 当前账号名（来自 ``AccountLogger``）。只有一个账号名可以比对，
        #: 「这一步由谁点」的判定就靠它。
        #:
        #: 取不到时（测试里的日志替身没有 ``account``）**当成未知**：指定了执行账号的
        #: 步骤一律跳过 —— 宁可不动手，也不能让"不该点的账号"去点。
        self.account: Optional[str] = getattr(alog, "account", None)
        #: 已经点过的 payload → 点它的时间（账号级共享）。
        #:
        #: 为什么账号级而不是任务级：同一条码常被多个群转发、多任务也可能都配了
        #: ``open_link`` —— 谁点到就是谁点到了，绝不能两个任务各点一次。
        #: 记录时机在**发出去之后**：发送失败时不记，否则一次网络抖动就把这条码
        #: 永久拉黑了。
        self._clicked_links: dict[str, float] = {}
        #: 限流用的滑动窗口：``"任务id:步骤序号"`` → 最近的发送时刻列表。
        self._link_hits: dict[str, list[float]] = {}
        #: 从深链里学到的机器人会话（**不带 @ 的裸用户名**，与配置层同口径）。
        #: 它们必须进 handler 过滤器，否则机器人的回执会被 ``filters.chat(...)``
        #: 挡在外面，后面的「等待回复」永远等不到 —— 用户看到的就是"点了但没下文"。
        self._link_chats: set[str] = set()
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
            #: 步骤被**故意跳过**的次数（open_link 最常见的几种跳过原因）。
            "steps_skipped": 0,
            #: 深链：真正点开的次数 / 跳过次数 / 限流放弃次数 / 发送失败次数。
            "links_clicked": 0,
            "links_skipped": 0,
            "links_throttled": 0,
            "links_failed": 0,
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
        # 深链步骤的目标机器人**配置里写不出来**（每条消息里的机器人可能不同），
        # 只能等真正点开一次之后才知道是谁；学到之后必须补进过滤器，否则它的回执
        # 进不来。见 ``_link_chats``。
        for chat in sorted(self._link_chats):
            if chat not in chats:
                chats.append(chat)
        return chats

    @staticmethod
    def _apply_signature(config: AccountConfig) -> tuple[Any, ...]:
        """热重载指纹：这些字段变了才需要重建。

        刻意**不含**运行期状态（计数、同码去重表、``_recent`` / ``_chat_ids`` 缓存）
        —— 那些必须活着，改一次配置不该把历史统计清零、也不该把已知的 chat_id 丢掉。
        """
        return (
            config.reg_grab.model_dump(mode="json"),
            config.notify.model_dump(mode="json"),
        )

    async def register(self) -> None:
        if not self.enabled:
            self.alog.info("抢注任务未启用")
            return
        self._install_handlers()
        self._log_registered()

    def _install_handlers(self) -> None:
        """装上 handler；**先拆旧的**，保证过滤器跟着新的监听会话走。

        ``_watch_chats()`` 决定 handler 的过滤器（来源会话 + 步骤链点名的会话）。
        只换 ``self.config`` 而不换过滤器的话，新加的群/机器人私聊的消息
        **根本进不来** —— 配置改得再对也没用。这与转发引擎是同一类坑
        （见 ``ForwardEngine.reload_rules`` 的注释）。
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

    def _remove_handlers(self) -> None:
        for handler, group in self._handlers:
            with contextlib.suppress(Exception):
                self.client.remove_handler(handler, group)
        self._handlers.clear()

    def _log_registered(self) -> None:
        chats = self._watch_chats()
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

    async def apply_config(self, config: AccountConfig) -> bool:
        """把新配置应用到**正在运行**的引擎上（面板改完即生效，无需重启账号）。

        返回 True 表示确实变了。三类变更都必须照顾到：

        * **增删/修改任务** —— 重建 ``prepared`` 与 ``task_stats``、按新并发上限换信号量；
        * **开/关总开关** —— 注册或注销 handler。多任务之后这是常态：
          用户先关着把配置填好，填完再打开；
        * **改监听会话 / 步骤链** —— handler 的过滤器必须跟着换
          （见 :meth:`_install_handlers`）。

        🔴 全程**不重建** ``self._bus`` / ``_seen_codes`` / ``_used`` / ``_recent``
        与在途任务：正在执行的那条链不能因为用户随手点了保存就被打断，
        已经处理过的码也不该因此变成"没见过"而被重复抢。
        """
        signature = self._apply_signature(config)
        if signature == self._applied_signature:
            return False
        self._applied_signature = signature
        was_enabled = self.enabled

        self.config = config.reg_grab
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
            "抢注配置已热重载（无需重启）",
            enabled=self.config.enabled,
            tasks=len(self.prepared),
            task_labels=" | ".join(task.label for task in self.prepared) or "-",
            watched_chats=len(self.watched_chats()) or "全部",
            handler_chats=len(self._watch_chats()) or "全部",
            max_concurrency=self.config.max_concurrency,
            turned_on=self.enabled and not was_enabled,
        )
        if self.enabled and not was_enabled:
            # 刚被打开：把每条任务的时段/就绪状态再喊一遍，用户正等着看它动没动。
            self._log_registered()
        return True

    async def close(self) -> None:
        self._remove_handlers()
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
        """按任务的 ``code_pattern`` 提取注册码。

        真正的规则只有一份，在模块级 :func:`extract_code` 里（第一个非空捕获组，
        没有捕获组才用整个匹配）。抽出去是为了让面板的「测试提取」跑**同一条**
        逻辑 —— 在路由里另抄一遍，"测试说能抓、实际抓不到"是迟早的事。
        """
        return extract_code(task.code_pattern, text)

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
                except _StepSkipped as skip:
                    # 「这一步故意没做」不是失败：照样记账、照样往下走，但不把整条链
                    # 标成 PARTIAL —— 用户要的语义是"没做"，不是"部分成功"。
                    result = StepResult.SKIPPED
                    detail = str(skip)
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
                if result is StepResult.SKIPPED:
                    self.stats["steps_skipped"] += 1
                    self.task_stats[task.id]["steps_skipped"] += 1
                    if step.type == "open_link":
                        self.stats["links_skipped"] += 1
                        self.task_stats[task.id]["links_skipped"] += 1
                    # 跳过**必须**留痕：用户事后问"为什么没点"时，这条就是答案。
                    self.alog.info(
                        "抢注步骤跳过",
                        task=task.label,
                        step=index,
                        total=len(task.steps),
                        type=REG_GRAB_STEP_LABELS.get(step.type, step.type),
                        name=step_label(step),
                        reason=detail,
                        cost_ms=round(cost_ms, 1),
                    )
                    continue
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

        # 每一步都被跳过 ⇒ 这条链其实**一步都没做**（典型：open_link 指定的执行账号
        # 不是当前账号；那条任务会由那个账号自己的引擎去执行）。按 SKIPPED 收尾，
        # 别给用户推一条"成功"通知 —— 他什么都没做，通知只会是噪音。
        # ⚠️ 只在**有步骤**时改写：一条步骤都没有的任务维持老行为（改它属于另一件事）。
        if (
            outcome.result is ChainResult.SUCCESS
            and outcome.steps
            and not any(step.result is StepResult.OK for step in outcome.steps)
        ):
            outcome.result = ChainResult.SKIPPED

        outcome.cost_ms = (time.perf_counter() - started) * 1000
        outcome.detail = self._detail_with_steps(outcome)
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

        if step.type == "open_link":
            # 目标会话是**消息里的那个机器人**，不是配置里写死的会话 ⇒ 这一步
            # 会把它设成新的链目标，后面接一个「等待回复」就能接住机器人的回执。
            detail, bot_chat = await self._step_open_link(
                task, index, step, source, current
            )
            return detail, current, bot_chat

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

    async def _step_open_link(
        self,
        task: PreparedTask,
        index: int,
        step: RegGrabStep,
        source: Any,
        current: Any,
    ) -> tuple[str, str]:
        """点开消息里的 ``t.me`` 深链：等价于给那个机器人发 ``/start <payload>``。

        为什么不能直接用 ``send`` 步骤：``send`` 的目标会话是**配置里写死的**，
        而深链里的机器人每条消息都可能不同（这条是 ``potdot_eco_bot``，别的品牌又是
        另一个），所以机器人名与 payload 只能从消息里现取。

        🔴 宁可漏点也不能点错：下面每一道闸门不通过都走 :class:`_StepSkipped`
        （不算失败），只有"该发却没发出去"和"限流等到超时"才算失败：

        1. 消息里没有匹配的深链（默认正则只认 payload 含 ``-Renew_`` 的）；
        2. 链接带遮罩字符、或 payload 不含 ``-Renew_``（用户自定义正则也绕不过这两条）；
        3. 这一步指定的执行账号不是当前账号（**谁设置谁点**）；
        4. 这个 payload 已经点过（账号级记忆，``code_ttl`` 内只点一次）；
        5. 限流：超过 ``max_per_minute`` 就排队等下一个额度窗口，等超过 30 秒放弃
           —— 这条**算失败**（会通知用户），因为它是"本来该抢却没抢到"。
        """
        pattern = task.link_patterns[index - 1]
        if pattern is None:  # pragma: no cover - 配置校验已经拦住了
            raise ValueError("open_link 步骤缺少深链正则")

        # 先看当前消息、再退回最初那条：链上前面若有「等待回复」，深链通常就在机器人
        # 刚回的那条里；而"群里直接贴深链"是最常见的用法，那就是最初那条。
        found, token = self._find_link(pattern, current, source)
        if found is None:
            raise _StepSkipped(
                "消息里没有可点开的续期深链"
                "（默认只认 t.me/机器人?start=…-Renew_…，-Register_ 那种不点）"
            )
        bot = found.group(1)
        payload = found.group(2)

        if _OPEN_LINK_MARKER not in payload:
            raise _StepSkipped(
                f"payload 里没有 {_OPEN_LINK_MARKER}，按设置不点（只点续期深链）"
            )
        if _MASKED_PAYLOAD.search(token):
            raise _StepSkipped(
                f"链接尾部带遮罩字符（{REG_GRAB_MASK_CHARS}），这条码多半已经被人用掉，不点"
            )

        wanted = (step.account or "").strip()
        mine = (self.account or "").strip()
        if wanted and wanted != mine:
            raise _StepSkipped(
                f"这一步设置由账号「{wanted}」执行，当前账号是「{mine or '未知'}」，跳过"
                "（那个账号自己的抢注任务要在同样的会话里监听，才会由它来点）"
            )

        if self._link_clicked(payload, task):
            raise _StepSkipped(
                f"payload 已经点过了（{task.config.code_ttl:.0f} 秒内只点一次）"
            )

        waited = await self._open_link_slot(task, index, step)
        if waited > 0:
            self.alog.info(
                "点开深链：等到了下一个额度窗口，继续点",
                task=task.label,
                step=index,
                waited_s=round(waited, 1),
            )

        chat = f"@{bot}"
        text = f"/start {payload}"

        # 解析成数字 id 只是为了给**后面**的步骤（wait_reply）用；发送本身照原样用
        # ``@username``（与人工点开这条深链最接近的写法）。解析不到也不拦着发送。
        resolved = await self._resolve_chat_id(chat)
        if resolved is None:
            self.alog.warning(
                "无法解析深链机器人，后续步骤可能等不到它的回复",
                chat=chat,
                hint="先手动在客户端打开一次这个机器人的会话",
            )

        async def _do() -> Any:
            return await self.client.send_message(chat_id=chat, text=text)

        try:
            sent = await with_flood_retry(
                _do,
                alog=self.alog,
                action="点开抢注深链",
                retries=1,
                max_flood_wait=10.0,
            )
        except Exception as exc:  # noqa: BLE001 - 记一笔再往上抛（由链统一记失败）
            self.stats["links_failed"] += 1
            self.task_stats[task.id]["links_failed"] += 1
            self.alog.warning(
                "点开深链发送失败",
                task=task.label,
                step=index,
                chat=chat,
                payload=truncate(payload, 80, "…"),
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

        # 只有**发出去之后**才记去重：发送失败时留着重试的机会。
        self._clicked_links[payload] = self._clock()
        self.stats["links_clicked"] += 1
        self.task_stats[task.id]["links_clicked"] += 1
        self.alog.info(
            "已点开抢注深链（等价于给机器人发 /start）",
            task=task.label,
            step=index,
            bot=chat,
            payload=truncate(payload, 80, "…"),
            waited_s=round(waited, 1),
            message_id=getattr(sent, "id", None),
        )
        # 把机器人记进 handler 过滤器：否则它的回执会被 ``filters.chat(...)`` 挡在
        # 外面，后接的「等待回复」永远等不到。只在**新**机器人时重装一次。
        # ⚠️ 记的是**不带 @ 的裸用户名**：监听会话在配置层就是这么归一化的
        # （见 ``parse_chat_ref``），保持一致，别让同一个会话出现两种写法。
        self._learn_link_chat(bot)
        # 链目标换成解析好的数字 id（解析不到就退回 @username）：紧跟其后的
        # 「等待回复」要靠它订阅这个机器人的消息，同 ``_step_send`` 的理由。
        target = resolved if resolved is not None else chat
        return f"已给 {chat} 发 /start {truncate(payload, 60, '…')}", target

    @staticmethod
    def _find_link(
        pattern: re.Pattern[str], *messages: Any
    ) -> tuple[Optional[re.Match[str]], str]:
        """在这些消息里按先后顺序找第一条匹配的深链。

        返回 ``(匹配对象, 链接所在的整段文本)``；都没有则 ``(None, "")``。

        ⚠️ 第二个返回值是**匹配起点到下一个空白**之间的那一整段，不是捕获组：遮罩
        字符（``░▒▓█``）不在 URL 字符集里，用户自定义的正则很可能一到遮罩处就停下，
        只看捕获组会得出"没被遮罩"的错误结论。
        """
        for message in messages:
            if message is None:
                continue
            text = message_text(message) or ""
            found = pattern.search(text)
            if found:
                token = re.split(r"\s", text[found.start() :], maxsplit=1)[0]
                return found, token
        return None, ""

    def _learn_link_chat(self, chat: str) -> None:
        """把深链机器人加进"要收它消息"的集合，并在需要时重装 handler。

        ⚠️ 这一步不是锦上添花：``_install_handlers`` 用 ``filters.chat(watched)``
        只放行关心的会话，深链里的机器人**配置里写不出来**，不补进去的话
        「等待回复」收不到回执，用户看到的是"点了但没下文"。
        只在发现新机器人时重装（add/remove 之间的极短窗口理论上可能漏一条消息，
        代价远小于"回执永远收不到"）。
        """
        if chat in self._link_chats:
            return
        self._link_chats.add(chat)
        if self.enabled:
            self._install_handlers()

    def _link_clicked(self, payload: str, task: PreparedTask) -> bool:
        """这个 payload 是不是已经点过了？``code_ttl`` 内算点过，之后可以重试。"""
        now = self._clock()
        ttl = task.config.code_ttl
        if ttl > 0:
            cutoff = now - ttl
            for key in [k for k, seen in self._clicked_links.items() if seen < cutoff]:
                del self._clicked_links[key]
        return payload in self._clicked_links

    async def _open_link_slot(
        self, task: PreparedTask, index: int, step: RegGrabStep
    ) -> float:
        """取一个发送额度；必要时排队等下一个窗口。返回实际等待秒数。

        ``max_per_minute=0`` = 不限。超限时**排队**而不是直接丢掉 —— 抢注的窗口很窄，
        差几秒就是别人的了；但等待超过 :data:`_OPEN_LINK_MAX_WAIT` 就放弃并抛
        :class:`OpenLinkThrottled`（走失败路径，会给用户推通知）。
        """
        limit = step.max_per_minute
        if limit <= 0:
            return 0.0

        key = f"{task.id}:{index}"
        waited = 0.0
        while True:
            now = self._clock()
            hits = [
                seen
                for seen in self._link_hits.get(key, [])
                if now - seen < _OPEN_LINK_WINDOW
            ]
            self._link_hits[key] = hits
            if len(hits) < limit:
                # 额度记在**发送之前**：限流要保护的是"这个账号发起请求的频率"，
                # 发失败也算发过（失败本身有另外的计数与通知）。
                hits.append(now)
                return waited
            wait_s = _OPEN_LINK_WINDOW - (now - min(hits))
            if waited + wait_s > _OPEN_LINK_MAX_WAIT:
                self.stats["links_throttled"] += 1
                self.task_stats[task.id]["links_throttled"] += 1
                self.alog.warning(
                    "点开深链触发限流，等待超过上限已放弃",
                    task=task.label,
                    step=index,
                    limit=limit,
                    window_s=_OPEN_LINK_WINDOW,
                    need_wait_s=round(wait_s, 1),
                    waited_s=round(waited, 1),
                    max_wait_s=_OPEN_LINK_MAX_WAIT,
                )
                raise OpenLinkThrottled(
                    f"点开深链限流：每分钟最多 {limit} 次，还要等 {wait_s:.0f} 秒"
                    f"（上限 {_OPEN_LINK_MAX_WAIT:.0f} 秒），已放弃"
                )
            self.alog.info(
                "点开深链触发限流，排队等下一个额度窗口",
                task=task.label,
                step=index,
                limit=limit,
                wait_s=round(wait_s, 1),
            )
            await self._sleep(wait_s)
            waited += wait_s

    def _clock(self) -> float:
        """单调时钟。抽成方法是为了测试能注入可控时间源（限流窗口不必真等 60 秒）。"""
        return time.monotonic()

    async def _sleep(self, seconds: float) -> None:
        """等待。抽成方法是为了测试能跳过真实等待（排队最多 30 秒）。"""
        await asyncio.sleep(seconds)

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
        """把会话引用解析成数字 id；``@username`` 的结果会缓存下来。

        🔴 查表前要**归一化**（小写、去掉前导 ``@``），与
        :func:`~tg_assistant.matching.parse_chat_ref` 同一口径。配置里写的
        ``@testbot`` 早就被校验器归一化成 ``testbot`` 了，但**运行时拼出来**的引用
        （比如 ``open_link`` 从消息里抠出的 ``"@potdot_eco_bot"``）没有经过那一层：
        不在这里归一化的话 ``get_chat("@potdot_eco_bot")`` 查不到 peer，后接的
        ``wait_reply`` 必然解析失败 —— 表现就是"链接点了，但等不到回复"。
        """
        if isinstance(ref, int):
            return ref
        if ref is None:
            return None
        key = str(ref).strip().lstrip("@").lower()
        cached = self._chat_ids.get(key)
        if cached is not None:
            return cached
        try:
            chat = await self.client.get_chat(key)
        except Exception as exc:  # noqa: BLE001 - 解析失败只影响这一条链
            self.alog.warning(
                "解析会话失败",
                chat=str(ref),
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
        """整条链的一句话汇总（**不含**每步的说明，那些由 :meth:`_detail_with_steps` 拼）。"""
        total = len(outcome.steps)
        ok = sum(1 for step in outcome.steps if step.result is StepResult.OK)
        failed = [step for step in outcome.steps if step.result is StepResult.FAILED]
        skipped = [step for step in outcome.steps if step.result is StepResult.SKIPPED]
        if outcome.result is ChainResult.SUCCESS:
            if skipped:
                # "全部成功"在这里是假话：有步骤被跳过了，必须说出来。
                return f"{ok}/{total} 步成功，{len(skipped)} 步跳过"
            return f"全部 {total} 步执行成功"
        if outcome.result is ChainResult.PARTIAL:
            return f"{ok}/{total} 步成功，{len(failed)} 步失败（已按配置忽略）"
        if outcome.result is ChainResult.SKIPPED and skipped and not failed:
            # 具体原因在下面按步拼接，这里就不重复写了（同一条原因说两遍反而难读）。
            return f"{len(skipped)}/{total} 步跳过"
        if failed:
            first = failed[0]
            return f"第 {first.index} 步「{first.label}」失败：{truncate(first.detail, 200, '…')}"
        return "没有可执行的步骤"

    @classmethod
    def _detail_with_steps(cls, outcome: ChainOutcome) -> str:
        """链级说明 = 一句话汇总 + 每一步自己的说明。

        为什么必须把每一步的说明带出来：用户能在**面板和通知**里看到的只有
        ``ChainOutcome.detail``。只写"全部 1 步执行成功"的话，他看不到究竟点了哪条
        链接、发的是什么、哪一步被跳过了 —— 而抢注最需要确认的恰恰是这个
        （等出事再去翻日志就晚了）。
        """
        summary = cls._summarize(outcome)
        parts = [
            f"{step.index}.{step.label}：{step.detail}"
            for step in outcome.steps
            if step.detail
        ]
        if not parts:
            return summary
        return f"{summary}；" + "；".join(parts)

    def _record(self, task: PreparedTask, outcome: ChainOutcome) -> None:
        key = {
            ChainResult.SUCCESS: "success",
            ChainResult.PARTIAL: "partial",
            ChainResult.FAILED: "failed",
            ChainResult.SKIPPED: "skipped",
        }[outcome.result]
        self.stats[key] += 1
        # 大盘**只记成功**：部分成功（PARTIAL）不算 —— 注册没成就是没成。
        if key == "success":
            self.metrics.record("reg_grab")
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
    "extract_code",
    "extract_code_detail",
    "iter_inline_buttons",
    "step_label",
    "visible_code_part",
]
