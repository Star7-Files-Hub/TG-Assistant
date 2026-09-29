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

🔴 **为什么只认 /status、其它一律沉默**：同一个 bot 还挂着别的项目。只要有一丁点
「顺手也支持一下 /start /help」的想法，就会和别的项目抢命令、互相覆盖回复。
所以匹配不上时**什么都不做**（只留一行 debug），绝不注册第二个命令、绝不回默认话术。

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
    "StatusData",
    "bot_id_from_token",
    "is_status_command",
    "build_status_text",
    "StatusCommand",
]

#: handler 分组号。转发=0 / 抢红包=1 / 抢注=2 已被占用，``/status`` 用 3，
#: 避免和它们抢同一组内的先后顺序（pyrogram 按组号分别分发）。
STATUS_HANDLER_GROUP = 3


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


def is_status_command(
    message: Any,
    *,
    bot_id: Optional[int],
    bot_username: Optional[str] = None,
) -> bool:
    """这条更新是不是「本项目该响应的 ``/status``」。

    全部条件同时满足才算命中，任何一条不满足都返回 ``False``（上层随即沉默）：

    1. 有 ``bot_id``（token 能解析），且这条消息就发生在**和这个 bot 的私聊**里
       （``chat.id == bot_id``）—— 只回**发起的那个**会话，绝不广播给别的通知对象。
    2. 是私聊（``ChatType.PRIVATE`` / ``ChatType.BOT``）。
    3. 是**账号自己发出**的消息（``outgoing`` 为真）—— ``/status`` 只可能由小白本人
       在这个私聊里打出来；机器人**发给**账号的通知回执是 incoming，必须排除，
       否则会自己回自己、甚至递归刷屏。
    4. 文本正好是 ``/status`` 或 ``/status@<bot 用户名>``（Telegram 客户端在群里
       常自动补 ``@bot``，私聊里一般不补，但两种都认）。多一个字都不算。
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
    if not outgoing:
        return False
    text = getattr(message, "text", None)
    if not text:
        return False
    stripped = text.strip()
    allowed = {"/status"}
    username = getattr(chat, "username", None) or bot_username
    if username:
        allowed.add(f"/status@{username}")
    return stripped in allowed


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


class StatusCommand:
    """把 ``/status`` handler 挂到账号自己的 pyrogram 会话上，命中就回一份面板。

    依赖全部**注入**，方便离线单测、也方便和 runner 解耦：

    - ``gather``：无参、返回 :class:`StatusData` 的同步取数函数（读实时配置/快照/大盘）。
    - ``send``：``async (chat_id: int, text: str) -> None`` 的发送函数。runner 把它接到
      现成的 :class:`~tg_assistant.notify.BotNotifier` 上，复用其代理/重试/限速，
      而且**只发回发起的那个会话**（不用 ``BotNotifier.submit`` —— 那个会广播给所有
      通知对象）。
    """

    def __init__(
        self,
        *,
        bot_token: Optional[str],
        gather: Callable[[], StatusData],
        send: Callable[[int, str], Awaitable[None]],
        alog: Any,
        bot_username: Optional[str] = None,
    ) -> None:
        self.bot_id = bot_id_from_token(bot_token)
        self.gather = gather
        self.send = send
        self.alog = alog
        self.bot_username = bot_username
        self._handlers: list[tuple[Any, int]] = []

    def register(self, client: Any) -> None:
        """挂上 handler。token 解析不出 bot id 就**直接跳过**，绝不半挂着。"""
        if self.bot_id is None:
            self.alog.warning("notify.bot_token 无法解析出 bot id，跳过 /status 注册")
            return
        # ``filters.private`` 已经把群/频道挡在外面（含和 bot 的私聊），
        # 剩下的「是不是自己发的、文本对不对、是不是这个 bot」交给 is_status_command。
        handler = MessageHandler(self._on_message, filters.private)
        self._handlers.append(client.add_handler(handler, group=STATUS_HANDLER_GROUP))
        self.alog.info("已注册 /status 指令", bot_id=self.bot_id, group=STATUS_HANDLER_GROUP)

    def unregister(self, client: Any) -> None:
        for handler, group in self._handlers:
            with contextlib.suppress(Exception):
                client.remove_handler(handler, group)
        self._handlers.clear()

    async def _on_message(self, client: Any, message: Any) -> None:
        if not is_status_command(
            message, bot_id=self.bot_id, bot_username=self.bot_username
        ):
            # 这个 handler 会看到全部私聊消息（含 bot 发给账号的通知回执）——
            # 绝大多数都不是 /status。默默放过：不回复、不报错、更不注册别的命令，
            # 免得和共用这个 bot 的其它项目互相干扰。
            self.alog.debug("忽略非 /status 的私聊消息")
            return
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        if chat_id is None:
            return
        try:
            text = build_status_text(self.gather())
        except Exception as exc:  # 取数/渲染出错绝不能把账号的更新循环带崩
            self.alog.warning(
                "生成 /status 面板失败", error=f"{type(exc).__name__}: {exc}"
            )
            return
        try:
            await self.send(chat_id, text)
        except Exception as exc:
            self.alog.warning(
                "回复 /status 失败", error=f"{type(exc).__name__}: {exc}"
            )
