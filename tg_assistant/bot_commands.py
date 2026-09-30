"""``/status`` 指令：让**共用的通知机器人**回一份本项目的面板状态。

用户原话：「给机器人加上 /status 用于查看面板状态，注意，此机器人在多个项目共用，
只注册 status」。两个约束贯穿整个模块，改动前务必先读懂：

🔴 **为什么绝不走 getUpdates / 轮询 / webhook / setMyCommands** ——
这个 bot 的 token 被**多个项目**共用。任何 ``getUpdates`` 都会推进**全局唯一**的
update offset，把别的项目该收到的更新吞掉；``setMyCommands`` / ``deleteWebhook``
会改**全局**的命令菜单和推送方式，直接把别人的机器人搞坏。所以这里**一条**
Bot API 拉取类调用都不发。

✅ **正确姿势**：``/status`` 是账号（小白本人）在**和这个 bot 的私聊**里打出来的。
账号自己的 pyrogram 会话本来就会把这条**自己发出的**消息当成一条更新收到
（``message.outgoing == True``、``chat.id`` = 这个 bot 的 id）。我们只在这条更新上
做判断，命中就用**现成的** :class:`~tg_assistant.notify.BotNotifier` 把面板文本
发回**这个私聊**。全程只是「收自己发的消息 + 回一条消息」，不碰任何全局状态。

🔴 **回话的 chat_id 不能用 ``message.chat.id``**：账号会话里和 bot 的私聊，
``chat.id`` 是 **bot 自己的 id**（判定条件本身就是 ``chat.id == bot_id``）。
可回复是走 **Bot API** 发的 —— 对 bot 而言那个 id 就是它自己，实测被拒：

    403 Forbidden: the bot can't send messages to the bot

Bot API 要的是**对方**（= 本账号）的 user id，所以必须用注入的 ``self_id``。

🔴 **为什么只认 /status、其它一律沉默**：同一个 bot 还挂着别的项目。只要有一丁点
「顺手也支持一下 /start /help」的想法，就会和别的项目抢命令、互相覆盖回复。
所以匹配不上时**什么都不做**（只留一行 debug），绝不注册第二个命令、绝不回默认话术。
（例外见下：进入「正则测试」后，本会话里的普通文本会被当成样本，这是用户明确要的。）

🔴 **按钮菜单为什么用「回复键盘」而不是 inline 按钮**：inline 按钮的点击（callback
query）**只会送到 bot 侧**，账号自己的会话根本看不到；要收到就得 ``getUpdates``
轮询这个**多项目共用**的 token —— 正是上面禁止的事。而回复键盘（ReplyKeyboardMarkup）
的点击会**以本账号名义发一条普通消息**，账号会话照样收得到（``outgoing=True``），
完全复用同一条通路，一个字节的全局状态都不碰。所以这里只能用回复键盘。

🔍 **正则测试**：在私聊里点「🔍 正则测试」后，随后发来的文本会被拿去跑**本账号当前
启用的转发规则**（以及可选的 ``/re <正则>`` 自定义正则），结果**只回这个私聊**。
它只读配置、只算匹配，**绝不转发**：转发引擎的过滤器是 ``filters.group | filters.channel``，
私聊消息连它的 handler 都进不去（见 ``tests/test_forwarder.py`` 的私聊用例）。

本模块**不依赖实时的 Telegram 连接**：匹配逻辑是纯函数，发送与取数都通过注入的
可调用对象完成，因此可以完全离线单测（见 ``tests/test_bot_status.py``）。
"""

from __future__ import annotations

import contextlib
import html
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from pyrogram import filters
from pyrogram.handlers import MessageHandler

__all__ = [
    "STATUS_HANDLER_GROUP",
    "BTN_PANEL",
    "BTN_TEST",
    "BTN_HIDE",
    "ProbeResult",
    "RuleProbe",
    "StatusData",
    "bot_id_from_token",
    "build_hide_markup",
    "build_menu_markup",
    "build_probe_text",
    "build_status_text",
    "build_test_hint_text",
    "is_own_bot_dm",
    "is_status_command",
    "StatusCommand",
]

#: handler 分组号。转发=0 / 抢红包=1 / 抢注=2 已被占用，``/status`` 用 3，
#: 避免和它们抢同一组内的先后顺序（pyrogram 按组号分别分发）。
STATUS_HANDLER_GROUP = 3

#: 回复键盘上的按钮文字。点一下 = 客户端**以本账号名义发一条普通消息**，
#: 所以下面的 ``_on_message`` 只要按文本分发就行（不需要任何 callback 处理）。
BTN_PANEL = "📊 面板状态"
BTN_TEST = "🔍 正则测试"
BTN_HIDE = "🙈 收起菜单"

#: 自定义正则的命令前缀：``/re <正则>`` 设，``/re`` 清空。
RE_PREFIX = "/re"


def bot_id_from_token(token: Optional[str]) -> Optional[int]:
    """从 ``123456789:AAE...`` 里取出 bot 的数字 id（冒号前那一段）。

    这是**唯一**能在不联网的前提下拿到 bot id 的办法（``getMe`` 是网络调用，
    而且我们连一次多余的 Bot API 都不想发）。取不出来就返回 ``None`` ——
    上层据此**跳过注册**，绝不让一个残缺的 token 把整个账号带崩。
    """
    if not token or ":" not in token:
        return None
    head = token.split(":", 1)[0].strip()
    try:
        return int(head)
    except (TypeError, ValueError):
        return None


def _chat_is_private(chat: Any) -> bool:
    """会话是不是私聊（含「和 bot 的私聊」）。

    pyrogram 里和 bot 的私聊 ``chat.type`` 是 ``ChatType.BOT``、和真人的是
    ``ChatType.PRIVATE`` —— 两个都算，别的（群/频道）一律不算。用 ``.value``
    读枚举字符串，测试里用鸭子替身也能直接命中。
    """
    kind = getattr(getattr(chat, "type", None), "value", None)
    return kind in {"private", "bot"}


def is_own_bot_dm(message: Any, *, bot_id: Optional[int]) -> bool:
    """这条消息是不是「**本账号自己**在和这个 bot 的私聊里发出的」。

    这是整个模块的唯一入口条件（``/status`` 与菜单都建立在它之上），四条同时满足：

    1. 有 ``bot_id``（token 能解析），且这条消息就发生在**和这个 bot 的私聊**里
       （``chat.id == bot_id``）—— 只认发起的那个会话，绝不响应别的通知对象。
    2. 是私聊（``ChatType.PRIVATE`` / ``ChatType.BOT``）。
    3. 是**账号自己发出**的消息（``outgoing`` 为真）—— 机器人**发给**账号的通知回执
       是 incoming，必须排除，否则会自己回自己、甚至递归刷屏。
    """
    if bot_id is None:
        return False
    chat = getattr(message, "chat", None)
    if chat is None or getattr(chat, "id", None) != bot_id:
        return False
    if not _chat_is_private(chat):
        return False
    # 兼容两种属性名：pyrogram 的 ``Message.outgoing``，以及历史上被叫作 ``out`` 的字段。
    outgoing = getattr(message, "outgoing", None)
    if outgoing is None:
        outgoing = getattr(message, "out", None)
    return bool(outgoing)


def is_status_command(
    message: Any,
    *,
    bot_id: Optional[int],
    bot_username: Optional[str] = None,
) -> bool:
    """这条更新是不是「本项目该响应的 ``/status``」。

    在 :func:`is_own_bot_dm` 的基础上再加一条：文本正好是 ``/status`` 或
    ``/status@<bot 用户名>``（Telegram 客户端在群里常自动补 ``@bot``，私聊里
    一般不补，但两种都认）。多一个字都不算。
    """
    if not is_own_bot_dm(message, bot_id=bot_id):
        return False
    text = getattr(message, "text", None)
    if not text:
        return False
    chat = getattr(message, "chat", None)
    allowed = {"/status"}
    username = getattr(chat, "username", None) or bot_username
    if username:
        allowed.add(f"/status@{username}")
    return text.strip() in allowed


@dataclass
class StatusData:
    """面板状态快照。**所有字段都有默认值**：任何一处取不到都退化成 0/关，
    绝不因为某个引擎没起来就让 ``/status`` 抛异常（用户宁可看到 0 也不要没有回音）。
    """

    account_label: str = ""
    username: Optional[str] = None
    running: bool = False
    forward_on: bool = False
    forward_rules: int = 0
    red_packet_on: bool = False
    red_packet_tasks: int = 0
    reg_grab_on: bool = False
    reg_grab_tasks: int = 0
    exclude_chats: int = 0
    exclude_users: int = 0
    #: 形如 ``{"day": {"forward": 1, "red_packet": 0, "reg_grab": 0}, "month": {...}, "total": {...}}``。
    metrics: dict[str, dict[str, int]] = field(default_factory=dict)


def _switch(on: bool) -> str:
    return "✅开" if on else "⛔关"


def _metric_line(metrics: dict[str, dict[str, int]], rng: str) -> str:
    bucket = (metrics or {}).get(rng, {}) or {}
    return (
        f"转发 {int(bucket.get('forward', 0) or 0)} · "
        f"抢包 {int(bucket.get('red_packet', 0) or 0)} · "
        f"抢注 {int(bucket.get('reg_grab', 0) or 0)}"
    )


def build_status_text(data: StatusData) -> str:
    """把 :class:`StatusData` 渲成一屏能看完的面板文本（HTML，分区带 emoji）。

    刻意做短：手机上一屏读完最实用。账号名做 HTML 转义（名字里可能带 ``<>&``），
    其余都是我们自己拼的数字，安全。
    """
    label = html.escape(data.account_label or "账号")
    if data.username:
        label = f"{label} (@{html.escape(data.username)})"
    lines = [
        f"🤖 <b>{label}</b> · {'🟢运行中' if data.running else '🔴未运行'}",
        f"📤 转发 {_switch(data.forward_on)} · {data.forward_rules} 条规则",
        f"🧧 抢红包 {_switch(data.red_packet_on)} · {data.red_packet_tasks} 个任务",
        f"📝 抢注 {_switch(data.reg_grab_on)} · {data.reg_grab_tasks} 个任务",
        f"🚫 全局排除 · 频道 {data.exclude_chats} · 发送者 {data.exclude_users}",
        "📊 <b>数据大盘</b>（北京时间，仅计成功）",
        f"今日 {_metric_line(data.metrics, 'day')}",
        f"本月 {_metric_line(data.metrics, 'month')}",
        f"累计 {_metric_line(data.metrics, 'total')}",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 按钮菜单（回复键盘）
# --------------------------------------------------------------------------- #
def build_menu_markup() -> dict[str, Any]:
    """回复键盘的 ``reply_markup``（**Bot API 的纯 dict**）。

    刻意不 import pyrogram 的 ``ReplyKeyboardMarkup``：一是离线单测不用拉起
    pyrogram 类型，二是这份 dict 原样塞进 ``sendMessage`` 即可，少一层转换。
    ``resize_keyboard`` 让键盘贴合内容（否则在手机上占掉半个屏幕）。
    """
    return {
        "keyboard": [
            [{"text": BTN_PANEL}, {"text": BTN_TEST}],
            [{"text": BTN_HIDE}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def build_hide_markup() -> dict[str, Any]:
    """收起菜单。想再要回菜单，发一次 ``/status`` 就行。"""
    return {"remove_keyboard": True}


# --------------------------------------------------------------------------- #
# 正则测试
# --------------------------------------------------------------------------- #
@dataclass
class RuleProbe:
    """**一条**启用规则的试跑明细（命中和没命中的都记）。

    ``reason`` 直接来自引擎的 :class:`~tg_assistant.matching.MatchResult` ——
    它才是回答「这条为什么没被转发」的关键：是**所有正则都没命中**，还是
    **本来能命中但被排除项挡了**（``blocked_by`` 给出是哪几条）。只回一句
    「未命中」，用户除了再问一次什么也做不了。
    """

    rule_id: str
    rule_name: Optional[str] = None
    matched: bool = False
    reason: str = ""
    #: 命中的那条正则原文（``matched`` 为真时有意义）。
    pattern: str = ""
    groups: list[str] = field(default_factory=list)
    #: 挡下这条消息的排除项原文（``reason == "命中排除规则"`` 时有值）。
    blocked_by: list[str] = field(default_factory=list)


@dataclass
class ProbeResult:
    """一次正则试跑的结果（纯数据，渲染交给 :func:`build_probe_text`）。

    所有字段都有默认值：试跑出任何岔子都退化成「没命中」，绝不因为某条正则写错
    就让用户收不到回音（与 :class:`StatusData` 同样的口径）。
    """

    sample: str = ""
    #: 在**哪个账号**上试跑。每个账号的规则是各自独立的（例如「宸澄」只挂在
    #: SevenStar 上），不写清楚就会出现「在小白这边测 SevenStar 转发过的消息」这种
    #: 白忙一场的误会。
    account_label: str = ""
    #: 用户用 ``/re`` 设的自定义正则（没设就是 None）。
    custom_pattern: Optional[str] = None
    custom_error: Optional[str] = None
    custom_matched: bool = False
    custom_groups: list[str] = field(default_factory=list)
    #: 每条**启用**规则的试跑明细（含没命中的，用来解释原因）。
    rules: list[RuleProbe] = field(default_factory=list)
    #: 当前启用的规则总数 —— 用来区分「没命中」和「压根没启用规则」。
    enabled_rules: int = 0

    @property
    def hits(self) -> list[RuleProbe]:
        return [item for item in self.rules if item.matched]


def _clip(value: Any, limit: int = 160) -> str:
    """截断 + HTML 转义。样本和正则都是用户原样发来的，拼进 HTML 前必须转义。"""
    text = "" if value is None else str(value)
    if len(text) > limit:
        text = text[:limit] + "…"
    return html.escape(text)


def _group_list(groups: list[str], limit: int = 5) -> str:
    return " | ".join(_clip(item, 60) for item in groups[:limit])


def build_test_hint_text(custom_pattern: Optional[str] = None) -> str:
    """进入/停留在「正则测试」时的提示。"""
    lines = [
        "🔍 <b>正则测试</b>（只在这个会话回复，<b>不会转发</b>到任何群/频道）",
        "",
        "把要测的<b>文本</b>直接发给我，我告诉你本账号当前的转发规则会不会命中。",
        f"想测自定义正则：发 <code>{RE_PREFIX} 你的正则</code>（只发 {RE_PREFIX} = 清空）。",
        f"退出：发 <code>/status</code>，或点「{BTN_HIDE}」。",
    ]
    if custom_pattern:
        lines += ["", f"当前自定义正则：<code>{_clip(custom_pattern)}</code>"]
    return "\n".join(lines)


def build_probe_text(result: ProbeResult) -> str:
    """把试跑结果渲成回话（HTML）。"""
    lines = [
        f"🔍 <b>正则测试</b> · {_clip(result.account_label or '本账号', 30)}（仅本会话，未转发）",
        f"📄 样本：<code>{_clip(result.sample, 300)}</code>",
    ]
    if not result.sample.strip():
        # 空样本几乎总是「这条消息没有正文」：纯图片/贴纸/文件，或说明文字没取到。
        # 不明说，用户只会看到「没命中」，然后来问为什么。
        lines.append(
            "⚠️ <b>样本是空的</b>：这条消息没有正文/说明文字（纯图片、贴纸、文件？），"
            "空文本当然什么都匹配不到。请把**文字**发过来。"
        )
    lines.append("")
    if result.custom_pattern:
        if result.custom_error:
            lines.append(
                f"🧪 自定义正则 <code>{_clip(result.custom_pattern)}</code>\n"
                f"     ⚠️ 无效：<code>{_clip(result.custom_error, 120)}</code>"
            )
        else:
            flag = "✅ 命中" if result.custom_matched else "⛔ 未命中"
            lines.append(f"🧪 自定义正则 <code>{_clip(result.custom_pattern)}</code> → {flag}")
            if result.custom_matched:
                lines.append(
                    f"     捕获组：<code>{_group_list(result.custom_groups)}</code>"
                    if result.custom_groups
                    else "     （无捕获组）"
                )
        lines.append("")

    hits = result.hits
    if hits:
        lines.append(f"📤 <b>会被转发</b>：命中 {len(hits)} 条规则")
        for hit in hits[:5]:
            name = f"（{_clip(hit.rule_name, 40)}）" if hit.rule_name else ""
            lines.append(f"• 规则 <b>{_clip(hit.rule_id, 40)}</b>{name}")
            if hit.pattern:
                lines.append(f"   正则：<code>{_clip(hit.pattern, 120)}</code>")
            if hit.groups:
                lines.append(f"   捕获组：<code>{_group_list(hit.groups)}</code>")
        if len(hits) > 5:
            lines.append(f"…另有 {len(hits) - 5} 条")
    elif result.enabled_rules == 0:
        lines.append("📤 <b>不会转发</b>：本账号当前没有启用任何转发规则")
    else:
        lines.append(f"📤 <b>不会转发</b>：{result.enabled_rules} 条启用规则都没命中")
        # 没命中时把「为什么」摊开 —— 尤其要指出**是不是被排除项挡的**：
        # 这正是「明明转发过、现在测却不命中」最常见的原因（规则后来加了排除）。
        for miss in result.rules[:5]:
            name = f"（{_clip(miss.rule_name, 40)}）" if miss.rule_name else ""
            lines.append(f"   • 规则 <b>{_clip(miss.rule_id, 40)}</b>{name}")
            lines.append(f"     原因：{_clip(miss.reason or '未命中', 100)}")
            if miss.blocked_by:
                lines.append(
                    f"     挡下它的排除项：<code>{_group_list(miss.blocked_by, 3)}</code>"
                )
    lines += [
        "",
        "（正则口径与引擎一致：忽略大小写 + 多行。只读配置，不转发。）",
        "（注意：这里只测**文本**，不含来源群/发送者/已用码/去重这些转发时的门槛。）",
    ]
    return "\n".join(lines)


class StatusCommand:
    """把 ``/status`` 与「正则测试」菜单挂到账号自己的 pyrogram 会话上。

    依赖全部**注入**，方便离线单测、也方便和 runner 解耦：

    - ``gather``：无参、返回 :class:`StatusData` 的同步取数函数（读实时配置/快照/大盘）。
    - ``probe``：``(样本文本, 自定义正则或 None) -> ProbeResult`` 的同步试跑函数。
      实现放在 runner（只有它拿得到实时 config），匹配交给引擎同一个
      :class:`~tg_assistant.matching.CompiledMatcher` —— 测试器和引擎各写一套 ``re``
      是这个项目踩过的坑：标志一改两边答案就不一样，而用户只会信测试器。
    - ``send``：``async (chat_id, text, reply_markup=None)`` 的发送函数。runner 把它接到
      现成的 :class:`~tg_assistant.notify.BotNotifier` 上，复用其代理/重试/限速，
      而且**只发回发起的那个会话**（不用 ``BotNotifier.submit`` —— 那个会广播给所有
      通知对象）。
    - ``self_id``：**本账号自己的 user id**（runner 从 ``get_me()`` 拿到）。它是回话
      真正要用的 chat_id —— 见模块开头「回话的 chat_id 不能用 ``message.chat.id``」。
      拿不到时退回 ``message.chat.id``（旧行为，会 403，但至少不会把异常吞了）。
    """

    def __init__(
        self,
        *,
        bot_token: Optional[str],
        gather: Callable[[], StatusData],
        send: Callable[..., Awaitable[None]],
        alog: Any,
        bot_username: Optional[str] = None,
        self_id: Optional[int] = None,
        probe: Optional[Callable[[str, Optional[str]], ProbeResult]] = None,
    ) -> None:
        self.bot_id = bot_id_from_token(bot_token)
        self.gather = gather
        self.probe = probe
        self.send = send
        self.alog = alog
        self.bot_username = bot_username
        self.self_id = self_id
        self._handlers: list[tuple[Any, int]] = []
        #: 「正则测试」是否已激活。激活后本会话里**普通文本**会被当成样本，
        #: 这是用户明确要的例外（其余时候匹配不上就沉默）。
        self._testing = False
        #: 用户用 ``/re`` 设的自定义正则；None = 只测转发规则。
        self._custom_pattern: Optional[str] = None

    def register(self, client: Any) -> None:
        """挂上 handler。token 解析不出 bot id 就**直接跳过**，绝不半挂着。"""
        if self.bot_id is None:
            self.alog.warning("notify.bot_token 无法解析出 bot id，跳过 /status 注册")
            return
        # ``filters.private`` 已经把群/频道挡在外面（含和 bot 的私聊），
        # 剩下的「是不是自己发的、文本对不对、是不是这个 bot」交给 is_status_command。
        handler = MessageHandler(self._on_message, filters.private)
        self._handlers.append(client.add_handler(handler, group=STATUS_HANDLER_GROUP))
        # 把 self_id 一起打出来：它是回话真正的目标，错了会 403 静默失效。
        # 打出来才能在**不发消息**的前提下核对「这个账号的 /status 会回到哪」。
        self.alog.info(
            "已注册 /status 指令",
            bot_id=self.bot_id,
            self_id=self.self_id,
            group=STATUS_HANDLER_GROUP,
        )

    def unregister(self, client: Any) -> None:
        for handler, group in self._handlers:
            with contextlib.suppress(Exception):
                client.remove_handler(handler, group)
        self._handlers.clear()

    async def _on_message(self, client: Any, message: Any) -> None:
        """私聊总入口：只认「本账号自己在和这个 bot 的私聊里发出的消息」。

        按钮点一下就是发一条普通文本（见模块开头「为什么用回复键盘」），所以这里
        **没有任何 callback 分支** —— 菜单项和 ``/status`` 走的是同一条通路。
        """
        if not is_own_bot_dm(message, bot_id=self.bot_id):
            # 这个 handler 会看到全部私聊消息（含 bot 发给账号的通知回执）——
            # 绝大多数都不是给我们的。默默放过：不回复、不报错、更不注册别的命令，
            # 免得和共用这个 bot 的其它项目互相干扰。
            self.alog.debug("忽略非 /status 的私聊消息")
            return
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        if chat_id is None:
            return
        # 🔴 回话目标不能直接用 chat_id：这里的 chat_id 是**账号视角**的会话 id，
        # 也就是 bot 自己的 id（判定条件就是 chat.id == bot_id）。可回复走 Bot API，
        # 对 bot 而言那是它自己 —— 实测 403 "the bot can't send messages to the bot"。
        # Bot API 要的是对方（= 本账号）的 user id，所以优先用注入的 self_id。
        target = self.self_id if self.self_id is not None else chat_id
        if self.self_id is None:
            self.alog.warning(
                "拿不到本账号 user id，回话可能失败",
                bot_id=self.bot_id,
                chat_id=chat_id,
                hint="runner 应从 get_me() 传入 self_id",
            )
        # 🔴 必须和引擎一样读「正文 **或** 说明文字」：带图消息的正文在 ``caption``
        # 里（``text`` 是 None），而引擎的 ``fields=['text','caption']`` 两者都看。
        # 只读 text 的话，用户转发一张带文字的图来测，测的就是**空字符串**，
        # 结果当然是「没命中」—— 实测踩过这个坑。
        text = str(
            getattr(message, "text", None) or getattr(message, "caption", None) or ""
        ).strip()
        is_status = is_status_command(
            message, bot_id=self.bot_id, bot_username=self.bot_username
        )
        await self._dispatch(target, text, is_status=is_status)

    async def _dispatch(self, target: int, text: str, *, is_status: bool) -> None:
        """按文本分发菜单项。顺序即优先级：命令/按钮 → 测试模式 → 沉默。"""
        if is_status or text == BTN_PANEL:
            # 面板：顺手退出测试模式，并把菜单挂上（回复键盘会一直留着，
            # 所以「点过一次就一直在」是符合预期的；想收起点 BTN_HIDE）。
            self._testing = False
            await self._reply(target, self._status_text(), build_menu_markup())
            return
        if text == BTN_TEST:
            self._testing = True
            await self._reply(
                target, build_test_hint_text(self._custom_pattern), build_menu_markup()
            )
            return
        if text == BTN_HIDE:
            self._testing = False
            await self._reply(
                target, "🙈 菜单已收起。想再要回来，发一次 <code>/status</code>。", build_hide_markup()
            )
            return
        if text == RE_PREFIX or text.startswith(RE_PREFIX + " "):
            self._custom_pattern = text[len(RE_PREFIX) :].strip() or None
            self._testing = True
            await self._reply(
                target, build_test_hint_text(self._custom_pattern), build_menu_markup()
            )
            return
        if self._testing:
            await self._probe_and_reply(target, text)
            return
        # 没进测试模式、又不是命令：和以前一样沉默（共用 bot，绝不抢别人的命令）。
        self.alog.debug("忽略非 /status 的私聊消息")

    def _status_text(self) -> str:
        try:
            return build_status_text(self.gather())
        except Exception as exc:  # 取数/渲染出错绝不能把账号的更新循环带崩
            self.alog.warning(
                "生成 /status 面板失败", error=f"{type(exc).__name__}: {exc}"
            )
            return "⚠️ 取面板数据失败，请看服务日志。"

    async def _probe_and_reply(self, target: int, text: str) -> None:
        """跑一次正则测试并回话。**只读配置、只发这个私聊，绝不转发。**"""
        if self.probe is None:
            await self._reply(target, "⚠️ 正则测试没接线（runner 未注入 probe）。")
            return
        try:
            result = self.probe(text, self._custom_pattern)
        except Exception as exc:
            self.alog.warning("正则试跑失败", error=f"{type(exc).__name__}: {exc}")
            await self._reply(target, "⚠️ 试跑失败，请看服务日志。")
            return
        await self._reply(target, build_probe_text(result), build_menu_markup())

    async def _reply(
        self, target: int, text: str, markup: Optional[dict[str, Any]] = None
    ) -> None:
        try:
            await self.send(target, text, markup)
        except Exception as exc:
            self.alog.warning("回复失败", error=f"{type(exc).__name__}: {exc}")
