"""消息匹配与模板渲染。

秒级转发的关键是**热路径不做慢操作**：

- 正则在加载配置时一次性 ``re.compile``，运行期只做 ``search``；
- 会话归属判断用 ``set`` 命中，不调用 ``client.get_chat``（那是网络请求，会把延迟从毫秒拉到几百毫秒）；
- 文本抽取只碰 ``message.text`` / ``caption`` / 按钮文字这些已在内存里的字段。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Optional

from .config import ChatRef, MatchConfig

#: Telegram 单条消息长度上限（文本）。
MAX_TEXT_LENGTH = 4096
#: 附带说明后留给正文的安全长度。
SAFE_TEXT_LENGTH = 3800


# --------------------------------------------------------------------------- #
# 消息字段抽取
# --------------------------------------------------------------------------- #
def message_text(message: Any) -> str:
    """取消息正文：优先 ``text``，其次 ``caption``。"""
    return getattr(message, "text", None) or getattr(message, "caption", None) or ""


def button_texts(message: Any) -> list[str]:
    """取出消息上所有按钮的文字（内联键盘 + 回复键盘）。"""
    markup = getattr(message, "reply_markup", None)
    if markup is None:
        return []
    texts: list[str] = []
    for attr in ("inline_keyboard", "keyboard"):
        rows = getattr(markup, attr, None) or []
        for row in rows:
            for button in row:
                text = getattr(button, "text", None) or (button if isinstance(button, str) else None)
                if text:
                    texts.append(str(text))
    return texts


def match_fields_text(message: Any, fields: Sequence[str]) -> str:
    """把配置里指定的字段拼成一段待匹配文本。"""
    parts: list[str] = []
    for field in fields:
        if field == "text":
            value = getattr(message, "text", None)
            if value:
                parts.append(value)
        elif field == "caption":
            value = getattr(message, "caption", None)
            if value:
                parts.append(value)
        elif field == "buttons":
            parts.extend(button_texts(message))
    return "\n".join(parts)


def sender_of(message: Any) -> tuple[Optional[int], Optional[str], bool, bool]:
    """返回 ``(sender_id, username, is_self, is_bot)``。

    频道消息没有 ``from_user``，此时回退到 ``sender_chat``。
    """
    user = getattr(message, "from_user", None)
    if user is not None:
        return (
            getattr(user, "id", None),
            (getattr(user, "username", None) or "").lower() or None,
            bool(getattr(user, "is_self", False)),
            bool(getattr(user, "is_bot", False)),
        )
    chat = getattr(message, "sender_chat", None)
    if chat is not None:
        return (
            getattr(chat, "id", None),
            (getattr(chat, "username", None) or "").lower() or None,
            False,
            False,
        )
    return None, None, False, False


def chat_identity(message: Any) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """返回 ``(chat_id, username, title)``。"""
    chat = getattr(message, "chat", None)
    if chat is None:
        return None, None, None
    return (
        getattr(chat, "id", None),
        (getattr(chat, "username", None) or "").lower() or None,
        getattr(chat, "title", None) or getattr(chat, "first_name", None),
    )


#: pyrogram ``ChatType.value`` → 归并后的会话类别。
#: 必须覆盖 ``pyrogram.enums.ChatType`` 的**全部**取值：漏掉的类型会归一成 ``None``，
#: 让下游 ``kind == "..."`` 的判断静默失效。见 tests 里的全覆盖测试。
_CHAT_KIND = {
    # 1:1 会话。``direct`` 是频道/商务直聊，同样是 1:1，不能当群组放行。
    "private": "private",
    "bot": "private",
    "direct": "private",
    # 群组。``forum`` 是论坛型超级群，pyrogram 的 ``filters.group`` 也算它。
    "group": "group",
    "supergroup": "group",
    "forum": "group",
    "channel": "channel",
}


def normalize_chat_kind(value: Any) -> Optional[str]:
    """把会话类型统一成 ``private`` / ``group`` / ``channel``，无法判断时返回 ``None``。

    入参可以是 ``pyrogram.enums.ChatType`` 枚举本身、它的 ``.value``（普通字符串，
    测试替身就是这种），或者已经归一过的字符串 —— 三种写法都认，
    这样调用方不必先自己归一，也就不会因为漏判某个原始值而放行不该放行的会话。
    """
    raw = getattr(value, "value", value)
    if not isinstance(raw, str):
        return None
    return _CHAT_KIND.get(raw.lower())


def chat_kind(message: Any) -> Optional[str]:
    """返回 ``private`` / ``group`` / ``channel``，无法判断时返回 ``None``。"""
    chat = getattr(message, "chat", None)
    return normalize_chat_kind(getattr(chat, "type", None))


def message_link(message: Any) -> Optional[str]:
    """构造消息永久链接。私有群/频道用 ``t.me/c/<短id>/<msg_id>``。"""
    chat_id, username, _ = chat_identity(message)
    message_id = getattr(message, "id", None)
    if message_id is None:
        return None
    if username:
        return f"https://t.me/{username}/{message_id}"
    if chat_id is not None and str(chat_id).startswith("-100"):
        return f"https://t.me/c/{str(chat_id)[4:]}/{message_id}"
    return None


def media_kind(message: Any) -> Optional[str]:
    """返回媒体类型名（photo/video/document/...），纯文本返回 None。"""
    media = getattr(message, "media", None)
    if media is None:
        return None
    value = getattr(media, "value", None) or str(media)
    return str(value).split(".")[-1].lower()


# --------------------------------------------------------------------------- #
# 会话 / 用户集合
# --------------------------------------------------------------------------- #
class RefSet:
    """会话或用户引用集合，同时支持数字 id 与 username 命中。

    ``me`` 会被单独识别，用于"仅自己发送的消息"这类规则。
    """

    __slots__ = ("ids", "usernames", "has_me", "empty")

    def __init__(self, refs: Iterable[ChatRef] | None = None) -> None:
        self.ids: set[int] = set()
        self.usernames: set[str] = set()
        self.has_me = False
        for ref in refs or []:
            if isinstance(ref, int):
                self.ids.add(ref)
            else:
                text = str(ref).strip().lstrip("@").lower()
                if text in {"me", "self"}:
                    self.has_me = True
                elif text:
                    self.usernames.add(text)
        self.empty = not (self.ids or self.usernames or self.has_me)

    def __bool__(self) -> bool:
        return not self.empty

    def matches(
        self,
        identifier: Optional[int],
        username: Optional[str] = None,
        *,
        is_self: bool = False,
    ) -> bool:
        if self.has_me and is_self:
            return True
        if identifier is not None and identifier in self.ids:
            return True
        if username and username.lower() in self.usernames:
            return True
        return False


# --------------------------------------------------------------------------- #
# 编译后的匹配器
# --------------------------------------------------------------------------- #
@dataclass
class MatchResult:
    matched: bool
    keyword: Optional[str] = None
    groups: tuple[str, ...] = ()
    named: dict[str, str] | None = None
    reason: str = ""

    def __bool__(self) -> bool:
        return self.matched


class CompiledMatcher:
    """预编译的匹配器，运行期零编译开销。"""

    __slots__ = ("config", "_patterns", "_excludes", "_flags")

    def __init__(self, config: MatchConfig) -> None:
        self.config = config
        self._flags = USER_PATTERN_FLAGS | (re.IGNORECASE if config.ignore_case else 0)
        if config.mode == "regex":
            self._patterns = [(p, re.compile(p, self._flags)) for p in config.patterns]
            self._excludes = [(p, re.compile(p, self._flags)) for p in config.exclude_patterns]
        else:
            self._patterns = [(p, None) for p in config.patterns]  # type: ignore[list-item]
            self._excludes = [(p, None) for p in config.exclude_patterns]  # type: ignore[list-item]

    def match(self, message: Any) -> MatchResult:
        text = match_fields_text(message, self.config.fields)
        return self.match_text(text)

    def match_text(self, text: str) -> MatchResult:
        config = self.config
        if config.min_length and len(text) < config.min_length:
            return MatchResult(False, reason=f"文本长度 {len(text)} < min_length {config.min_length}")

        if config.mode == "all":
            if self._exclude_hit(text):
                return MatchResult(False, reason="命中排除规则")
            return MatchResult(True, keyword="*")

        if not text:
            return MatchResult(False, reason="消息无可匹配文本")

        haystack = text.lower() if config.ignore_case else text

        if config.mode == "regex":
            for raw, pattern in self._patterns:
                found = pattern.search(text)  # type: ignore[union-attr]
                if found:
                    if self._exclude_hit(text):
                        return MatchResult(False, reason="命中排除规则")
                    return MatchResult(
                        True,
                        keyword=raw,
                        groups=tuple(g if g is not None else "" for g in found.groups()),
                        named={k: (v or "") for k, v in (found.groupdict() or {}).items()},
                    )
            return MatchResult(False, reason="所有正则均未命中")

        for raw, _ in self._patterns:
            needle = raw.lower() if config.ignore_case else raw
            if config.mode == "exact":
                # 两端空白不应影响判定：用户配 "签到" 时，收到 " 签到 " 显然也算命中
                hit = haystack.strip() == needle.strip()
            else:
                hit = needle in haystack
            if hit:
                if self._exclude_hit(text):
                    return MatchResult(False, reason="命中排除规则")
                return MatchResult(True, keyword=raw)
        return MatchResult(False, reason=f"所有关键词均未命中（mode={config.mode}）")

    def _exclude_hit(self, text: str) -> bool:
        if not self._excludes:
            return False
        if self.config.mode == "regex":
            return any(pattern.search(text) for _, pattern in self._excludes)  # type: ignore[union-attr]
        haystack = text.lower() if self.config.ignore_case else text
        for raw, _ in self._excludes:
            needle = raw.lower() if self.config.ignore_case else raw
            if needle in haystack:
                return True
        return False


#: 用户写的正则一律按**逐行**语义编译（``re.MULTILINE``）。
#:
#: 不加这个标志时 ``^`` / ``$`` 只认**整段文本**的首尾，而 Telegram 消息几乎
#: 都是多行的::
#:
#:     🎁 注册码
#:     MSKY-30-Register_ab12cd34ef
#:     有效期 30 天
#:
#: 于是用户照着「一行一个码」写的 ``^[A-Z0-9]{12}$`` 一条都匹配不上 ——
#: 而这条正则在任何在线正则测试工具里都是好的（那些工具默认就按行匹配），
#: 用户完全看不出哪里错了。这不是"少了个高级选项"，是**默认行为错了**。
#:
#: 只加 ``MULTILINE``，不加 ``DOTALL``：``.`` 跨行会把整条消息吞成一个
#: 匹配，``.*`` 这种写法立刻变得不可控。
USER_PATTERN_FLAGS = re.MULTILINE


def compile_user_pattern(pattern: str, *, ignore_case: bool = False) -> re.Pattern[str]:
    """编译一条**用户写的**正则（统一带上 :data:`USER_PATTERN_FLAGS`）。"""
    flags = USER_PATTERN_FLAGS | (re.IGNORECASE if ignore_case else 0)
    return re.compile(pattern, flags)


def compile_patterns(patterns: Sequence[str], ignore_case: bool = True) -> list[re.Pattern[str]]:
    flags = USER_PATTERN_FLAGS | (re.IGNORECASE if ignore_case else 0)
    return [re.compile(p, flags) for p in patterns]


def first_match(patterns: Sequence[re.Pattern[str]], text: str) -> Optional[re.Match[str]]:
    for pattern in patterns:
        found = pattern.search(text)
        if found:
            return found
    return None


# --------------------------------------------------------------------------- #
# 模板渲染
# --------------------------------------------------------------------------- #
DEFAULT_FORWARD_TEMPLATE = "{text}"

DEFAULT_NOTIFY_TEMPLATE = (
    "🔔 <b>{rule}</b>\n"
    "来源：{chat_title}\n"
    "发送者：{sender}\n"
    "时间：{time}\n"
    "———\n"
    "{text}"
)

DEFAULT_RED_PACKET_TEMPLATE = (
    "🧧 <b>抢红包 {result_icon}{result_text}</b>\n"
    "群组：{chat_title}\n"
    "方式：{strategy}\n"
    "耗时：{cost_ms}ms\n"
    "———\n"
    "{detail}"
)

DEFAULT_REG_GRAB_TEMPLATE = (
    "🎯 <b>抢注 {result_icon}{result_text}</b>\n"
    "群组：{chat_title}\n"
    "注册码：<code>{code}</code>\n"
    "耗时：{cost_ms}ms\n"
    "———\n"
    "{detail}"
)


class SafeDict(dict):
    """``str.format_map`` 用：未知占位符原样保留，不抛 KeyError。"""

    def __missing__(self, key: str) -> str:  # pragma: no cover - 简单分支
        return "{" + key + "}"


def render_template(template: str, variables: dict[str, Any]) -> str:
    """渲染模板；模板里写错变量名不会导致崩溃，只会原样输出。"""
    try:
        return template.format_map(SafeDict(variables))
    except (IndexError, ValueError):
        # 模板里有 `{}` 或 `{0}` 之类的位置参数时退化为原样返回
        return template


def build_variables(message: Any, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造模板变量表。

    可用变量：``text``/``chat_title``/``chat_id``/``chat_username``/``sender``/
    ``sender_id``/``sender_username``/``message_id``/``link``/``time``/``date``/
    ``media``，以及正则捕获组 ``g1..gN``、命名组本身的键。
    """
    import datetime as _dt

    chat_id, chat_username, chat_title = chat_identity(message)
    sender_id, sender_username, is_self, is_bot = sender_of(message)
    user = getattr(message, "from_user", None)
    sender_name = None
    if user is not None:
        first = getattr(user, "first_name", None) or ""
        last = getattr(user, "last_name", None) or ""
        sender_name = (f"{first} {last}").strip() or None
    if not sender_name:
        sender_chat = getattr(message, "sender_chat", None)
        sender_name = getattr(sender_chat, "title", None) if sender_chat else None

    raw_date = getattr(message, "date", None)
    if isinstance(raw_date, _dt.datetime):
        stamp = raw_date.astimezone()
    else:
        stamp = _dt.datetime.now()

    sender_label = sender_name or (f"@{sender_username}" if sender_username else None)
    if sender_label is None:
        sender_label = str(sender_id) if sender_id else "未知"

    variables: dict[str, Any] = {
        "text": message_text(message),
        "chat_id": chat_id if chat_id is not None else "",
        "chat_title": chat_title or (f"@{chat_username}" if chat_username else str(chat_id or "")),
        "chat_username": chat_username or "",
        "sender": sender_label,
        # sender 是"最合适的显示名"，sender_name 是纯姓名（可能为空），两者都给出，
        # 方便用户在模板里精确控制。
        "sender_name": sender_name or "",
        "sender_id": sender_id if sender_id is not None else "",
        "sender_username": sender_username or "",
        "sender_is_bot": "是" if is_bot else "否",
        "sender_is_self": "是" if is_self else "否",
        "message_id": getattr(message, "id", ""),
        "link": message_link(message) or "",
        "time": stamp.strftime("%Y-%m-%d %H:%M:%S"),
        "date": stamp.strftime("%Y-%m-%d"),
        "media": media_kind(message) or "",
    }
    if extra:
        variables.update(extra)
    return variables


def apply_groups(variables: dict[str, Any], result: MatchResult) -> dict[str, Any]:
    """把正则捕获组塞进变量表：``{g1}``/``{group1}`` 与命名组。"""
    for index, value in enumerate(result.groups, start=1):
        variables[f"g{index}"] = value
        variables[f"group{index}"] = value
    if result.named:
        for key, value in result.named.items():
            variables.setdefault(key, value)
    variables.setdefault("keyword", result.keyword or "")
    return variables


def truncate(text: str, limit: int = SAFE_TEXT_LENGTH, suffix: str = "…（已截断）") -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(suffix)] + suffix


__all__ = [
    "CompiledMatcher",
    "DEFAULT_FORWARD_TEMPLATE",
    "DEFAULT_NOTIFY_TEMPLATE",
    "DEFAULT_RED_PACKET_TEMPLATE",
    "DEFAULT_REG_GRAB_TEMPLATE",
    "MAX_TEXT_LENGTH",
    "MatchResult",
    "RefSet",
    "SAFE_TEXT_LENGTH",
    "apply_groups",
    "build_variables",
    "button_texts",
    "chat_identity",
    "USER_PATTERN_FLAGS",
    "compile_patterns",
    "compile_user_pattern",
    "first_match",
    "match_fields_text",
    "media_kind",
    "message_link",
    "message_text",
    "render_template",
    "sender_of",
    "truncate",
]
