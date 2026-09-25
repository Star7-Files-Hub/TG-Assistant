"""配置模型（pydantic v2）。

配置分三层：

1. :class:`Settings` —— 进程级设置，来自环境变量 / CLI 参数
   （api_id、api_hash、全局代理、数据目录、日志级别）。
2. :class:`AccountRecord` —— 账号注册表条目（``data/accounts.json``），
   记录账号身份、独立代理、是否启用。
3. :class:`AccountConfig` —— 单账号业务配置（``data/accounts/<name>/config.json``），
   包含转发规则、通知渠道、抢红包设置。三者互不干扰，删除某个账号目录即彻底清除该账号数据。

所有字符串字段都支持 ``${ENV_VAR}`` 占位符，避免把 bot_token 之类的凭据写进 JSON。
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

CONFIG_VERSION = 1

#: 聊天引用：数字 id 或 @username
ChatRef = Union[int, str]

MatchMode = Literal["regex", "contains", "exact", "all"]
ForwardMode = Literal["forward", "copy", "text"]
NotifyMode = Literal["forward", "copy", "text"]
RedPacketStrategy = Literal["auto", "button", "keyword"]

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_TME_PATTERN = re.compile(
    r"^(?:https?://)?(?:www\.)?t(?:elegram)?\.me/(?:c/(\d+)|([A-Za-z0-9_]{4,32}))(?:/\d+)?/?$"
)


def expand_env(value: str) -> str:
    """展开 ``${VAR}`` 与 ``${VAR:-default}``。变量不存在且无默认值时保留原样。"""

    def _replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        env = os.environ.get(name)
        if env is not None:
            return env
        if default is not None:
            return default
        return match.group(0)

    return _ENV_PATTERN.sub(_replace, value)


def _looks_unexpanded(value: str | None) -> bool:
    """判断字符串里是否还残留未替换的 ``${VAR}``。

    :func:`expand_env` 对"变量不存在且无默认值"的情况保留原样，
    以便在校验阶段给出"你忘了设这个环境变量"这类明确报错，
    而不是拿着字面量 ``${TGA_BOT_TOKEN}`` 去请求 Telegram 再报 401。
    """
    return bool(value) and _ENV_PATTERN.search(value) is not None


def parse_chat_ref(value: ChatRef | None) -> ChatRef | None:
    """把各种写法的会话引用归一化。

    支持：``-1001234567890``、``"1234567890"``、``"@channel"``、``"channel"``、
    ``"https://t.me/channel"``、``"https://t.me/c/1234567890/5"``。
    ``t.me/c/<id>`` 形式会补上 ``-100`` 前缀转成真实 chat_id。
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value

    text = expand_env(str(value)).strip()
    if not text:
        return None

    match = _TME_PATTERN.match(text)
    if match:
        if match.group(1):
            return int(f"-100{match.group(1)}")
        text = match.group(2)

    if text.startswith("@"):
        text = text[1:]

    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text.lower()


def _normalize_refs(values: Sequence[ChatRef] | None) -> list[ChatRef]:
    if not values:
        return []
    result: list[ChatRef] = []
    for item in values:
        parsed = parse_chat_ref(item)
        if parsed is not None and parsed not in result:
            result.append(parsed)
    return result


class StrictModel(BaseModel):
    """禁止未知字段，配置写错立刻报错而不是被静默忽略。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# --------------------------------------------------------------------------- #
# 代理
# --------------------------------------------------------------------------- #
class ProxyConfig(StrictModel):
    """SOCKS5 / HTTP 代理。

    ``scheme`` 取值与 pyrogram 一致：``socks5``、``socks4``、``http``。
    也可以直接用 :meth:`from_url` 解析 ``socks5://user:pass@host:1080``。
    """

    scheme: Literal["socks5", "socks4", "http"] = "socks5"
    hostname: str
    port: int = Field(ge=1, le=65535)
    username: Optional[str] = None
    password: Optional[str] = None

    @field_validator("hostname", "username", "password", mode="before")
    @classmethod
    def _expand(cls, value: Any) -> Any:
        if isinstance(value, str):
            expanded = expand_env(value).strip()
            return expanded or None
        return value

    @field_validator("hostname")
    @classmethod
    def _require_hostname(cls, value: str) -> str:
        if not value:
            raise ValueError("代理 hostname 不能为空")
        return value

    @classmethod
    def from_url(cls, url: str) -> "ProxyConfig":
        """解析代理 URL。缺省 scheme 视为 socks5，也接受 ``host:port:user:pass``。"""
        raw = expand_env(str(url)).strip()
        if not raw:
            raise ValueError("代理地址为空")

        if "://" not in raw:
            parts = raw.split(":")
            if len(parts) == 2:
                raw = f"socks5://{parts[0]}:{parts[1]}"
            elif len(parts) == 4:
                host, port, user, pwd = parts
                raw = f"socks5://{user}:{pwd}@{host}:{port}"
            else:
                raw = f"socks5://{raw}"

        from urllib.parse import unquote, urlparse

        parsed = urlparse(raw)
        scheme = (parsed.scheme or "socks5").lower()
        if scheme in {"socks5h", "socks"}:
            scheme = "socks5"
        if scheme == "https":
            scheme = "http"
        if scheme not in {"socks5", "socks4", "http"}:
            raise ValueError(f"不支持的代理协议: {parsed.scheme}（支持 socks5/socks4/http）")
        if not parsed.hostname:
            raise ValueError(f"代理地址缺少主机名: {url}")
        if not parsed.port:
            raise ValueError(f"代理地址缺少端口: {url}")

        return cls(
            scheme=scheme,  # type: ignore[arg-type]
            hostname=parsed.hostname,
            port=parsed.port,
            username=unquote(parsed.username) if parsed.username else None,
            password=unquote(parsed.password) if parsed.password else None,
        )

    def to_pyrogram(self) -> dict[str, Any]:
        """转成 pyrogram ``Client(proxy=...)`` 需要的字典。"""
        proxy: dict[str, Any] = {
            "scheme": self.scheme,
            "hostname": self.hostname,
            "port": self.port,
        }
        if self.username:
            proxy["username"] = self.username
        if self.password:
            proxy["password"] = self.password
        return proxy

    def to_url(self, hide_credentials: bool = True) -> str:
        auth = ""
        if self.username:
            password = "***" if hide_credentials else (self.password or "")
            auth = f"{self.username}:{password}@"
        return f"{self.scheme}://{auth}{self.hostname}:{self.port}"

    def to_httpx_url(self) -> str:
        """httpx 用的代理 URL（含真实凭据）。"""
        auth = ""
        if self.username:
            from urllib.parse import quote

            auth = f"{quote(self.username, safe='')}:{quote(self.password or '', safe='')}@"
        return f"{self.scheme}://{auth}{self.hostname}:{self.port}"

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        return self.to_url()


# --------------------------------------------------------------------------- #
# 转发
# --------------------------------------------------------------------------- #
class MatchConfig(StrictModel):
    """消息匹配规则。

    - ``mode="regex"``：``patterns`` 为正则，任一命中即算命中；
      捕获组可在模板里用 ``{g1}``/``{group1}`` 引用。
    - ``mode="contains"`` / ``"exact"``：按子串 / 全等比较。
    - ``mode="all"``：不做文本判断，只要来源匹配就命中（配合 ``from_users`` 用）。
    """

    mode: MatchMode = "regex"
    patterns: list[str] = Field(default_factory=list)
    exclude_patterns: list[str] = Field(default_factory=list)
    ignore_case: bool = True
    #: 参与匹配的字段；``buttons`` 会把内联按钮文本也纳入匹配范围。
    fields: list[Literal["text", "caption", "buttons"]] = Field(
        default_factory=lambda: ["text", "caption"]
    )
    #: 命中所需的最小文本长度，用于过滤空消息。
    min_length: int = 0

    @field_validator("patterns", "exclude_patterns", mode="before")
    @classmethod
    def _as_list(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return value

    @model_validator(mode="after")
    def _check(self) -> "MatchConfig":
        if self.mode != "all" and not self.patterns:
            raise ValueError(f"match.mode={self.mode} 时必须提供至少一个 patterns")
        if self.mode == "regex":
            for pattern in [*self.patterns, *self.exclude_patterns]:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"正则表达式无效 {pattern!r}: {exc}") from exc
        if not self.fields:
            raise ValueError("match.fields 不能为空")
        return self


class ForwardRule(StrictModel):
    """一条转发规则：来源 + 匹配 + 目标。"""

    id: str
    name: Optional[str] = None
    enabled: bool = True

    #: 监听的来源会话；为空表示监听全部会话（谨慎使用，量大时开销高）。
    sources: list[ChatRef] = Field(default_factory=list)
    exclude_sources: list[ChatRef] = Field(default_factory=list)
    #: 转发目标，可以是多个频道/群/用户。
    targets: list[ChatRef] = Field(min_length=1)
    target_thread_id: Optional[int] = None

    match: MatchConfig = Field(default_factory=lambda: MatchConfig(mode="all"))

    #: 发送者白名单/黑名单；支持 id、@username、``me``。
    from_users: list[ChatRef] = Field(default_factory=list)
    exclude_users: list[ChatRef] = Field(default_factory=list)
    ignore_self: bool = True
    #: 是否处理编辑后的消息（默认不处理，避免同一条消息重复转发）。
    include_edited: bool = False
    include_service: bool = False

    #: forward=原生转发(带「转发自」抬头) / copy=按 file_id 重新发送(全新消息，不带来源) /
    #: text=按模板重发纯文本
    mode: ForwardMode = "copy"
    #: ``mode="text"`` 时的模板，可用变量见 README。
    template: Optional[str] = None
    #: 是否附带来源链接（默认开）。两种模式的加法不同：
    #: ``forward`` 不动转发消息，在它**下方**单独补发一条 ``🔗原文链接：<链接>``；
    #: ``copy`` / ``text`` 写进**当前消息**的正文末尾（前面空一行）。
    include_source_link: bool = True
    #: 相册（media group）聚合转发；聚合窗口内的多张图会作为一组发送。
    media_group: bool = True
    media_group_window: float = Field(default=1.2, ge=0.1, le=10.0)
    #: 转发前等待秒数，0 表示立即（秒级转发默认 0）。
    delay: float = Field(default=0.0, ge=0.0, le=600.0)
    #: 同一规则最小触发间隔（秒），防止刷屏；0 = 不限。
    min_interval: float = Field(default=0.0, ge=0.0)
    #: 是否同时走 bot 通知渠道。
    notify: bool = True
    #: 目标消息静音发送。
    silent: bool = False

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("规则 id 不能为空")
        return cleaned

    @field_validator(
        "sources", "exclude_sources", "targets", "from_users", "exclude_users", mode="before"
    )
    @classmethod
    def _normalize(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, (str, int)):
            value = [value]
        return _normalize_refs(value)

    @model_validator(mode="after")
    def _check_mode(self) -> "ForwardRule":
        if self.mode == "text" and not self.template:
            # text 模式没给模板时退化为「原文 + 来源」，仍然可用。
            object.__setattr__(self, "template", "{text}")
        return self

    @property
    def label(self) -> str:
        return self.name or self.id


class ForwardConfig(StrictModel):
    enabled: bool = True
    rules: list[ForwardRule] = Field(default_factory=list)
    #: 消息去重窗口（秒）。同一 (chat_id, message_id) 在窗口内只处理一次。
    dedupe_window: float = Field(default=300.0, ge=0.0)
    #: **同内容去重**：命中新消息后，先和目标里**最近已转发的内容**比一比，
    #: 一样就跳过（小白原话：「命中新消息要跟前 5 条对比，不一致才进行转发，
    #: 或者一天内而不是前 x 条」）。
    #:
    #: 两个条件取**并集** —— 既看最近 ``recent_dedupe_limit`` 条，也看
    #: ``recent_dedupe_window`` 秒内的全部（先到哪个算哪个，谁更宽算谁）。
    #: 0 = 关掉这一层。
    recent_dedupe_limit: int = Field(default=5, ge=0)
    recent_dedupe_window: float = Field(default=86400.0, ge=0.0)
    #: 全局排除的会话：**所有规则**都不监听这些会话，写一次管全部。
    #:
    #: 与每条规则自己的 ``exclude_sources`` 的区别：这里是账号级的，
    #: 适合放"永远不该被转发"的会话 —— 尤其是**转发目标频道本身**。
    #: 注意转发目标已经由代码自动排除（见 ``PreparedRule.chat_allowed``），
    #: 不需要在这里重复填；这个列表是给"目标之外、但同样不想监听"的会话用的。
    exclude_chats: list[ChatRef] = Field(default_factory=list)

    @field_validator("exclude_chats", mode="before")
    @classmethod
    def _normalize_exclude_chats(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, (str, int)):
            value = [value]
        return _normalize_refs(value)

    @model_validator(mode="after")
    def _unique_ids(self) -> "ForwardConfig":
        seen: set[str] = set()
        for rule in self.rules:
            if rule.id in seen:
                raise ValueError(f"转发规则 id 重复: {rule.id}")
            seen.add(rule.id)
        return self

    @property
    def active_rules(self) -> list[ForwardRule]:
        return [rule for rule in self.rules if rule.enabled]


# --------------------------------------------------------------------------- #
# 通知
# --------------------------------------------------------------------------- #
class NotifyConfig(StrictModel):
    """Bot 通知渠道。

    自己的频道不会给自己推送通知，因此用一个 bot 把同样的内容再发给你（或指定群）。

    - ``mode="forward"``（**默认**）：让 bot 用 ``forwardMessage`` 转发目标频道里那条消息，
      通知里的样子**和频道里那条一模一样**（带「转发自」抬头）。源会话禁止转发 /
      内容受保护时自动降级 ``copyMessage``。
    - ``mode="copy"``：只用 ``copyMessage`` 复制（不带「转发自」抬头）。
      适合源会话一律禁止转发、不想每次都试一次 forward 的场景。
    - ``mode="text"``：bot 直接按模板发文本，不依赖频道权限，媒体退化为文字说明。

    ``forward``/``copy`` 都失败时会自动回退到 ``text``，保证通知不丢。
    """

    enabled: bool = False
    bot_token: Optional[str] = None
    #: 接收通知的对象：你的用户 id（需先和 bot 私聊过一次）或群 id。
    #: 兼容旧配置；新配置建议用 ``chat_ids``。
    chat_id: Optional[ChatRef] = None
    #: 接收通知的多个对象：你的用户 id（需先和 bot 私聊过一次）或群 id。
    #: 与 ``chat_id`` 是**并集**关系（``chat_id`` 排在最前）。
    chat_ids: list[ChatRef] = Field(default_factory=list)
    message_thread_id: Optional[int] = None
    #: 默认 ``forward``：通知要**和频道里那条长得一样**（小白原话：「频道什么样机器人
    #: 就什么样，能使用 forward 就 forward，不能才用 copy 兜底」）。
    mode: NotifyMode = "forward"
    #: 通知里附带原始来源链接。
    include_source_link: bool = True
    #: 文本模式模板；None 表示使用内置模板。
    template: Optional[str] = None
    #: Bot API 每分钟最多发送条数（Telegram 官方限制约 20 条/分钟/群）。
    rate_limit_per_minute: int = Field(default=18, ge=1, le=60)
    #: 待发队列上限，超出丢弃最旧的并记 WARNING。
    queue_size: int = Field(default=500, ge=1)
    #: 静音推送（仍会进历史，但不响铃）。
    silent: bool = False
    #: 需要推送的事件类型。
    events: list[Literal["forward", "red_packet", "reg_grab", "error"]] = Field(
        default_factory=lambda: ["forward", "red_packet", "reg_grab"]
    )
    #: Bot API 基础地址，可指向自建 Bot API server。
    api_base: str = "https://api.telegram.org"
    #: Bot API 请求是否走代理（国内机器通常需要）。
    use_proxy: bool = True
    timeout: float = Field(default=15.0, gt=0)

    @field_validator("bot_token", "template", mode="before")
    @classmethod
    def _expand_str(cls, value: Any) -> Any:
        if isinstance(value, str):
            expanded = expand_env(value)
            return expanded or None
        return value

    @field_validator("chat_id", mode="before")
    @classmethod
    def _normalize_chat(cls, value: Any) -> Any:
        return parse_chat_ref(value)

    @field_validator("chat_ids", mode="before")
    @classmethod
    def _normalize_chats(cls, value: Any) -> Any:
        # 部署版漏了这个 validator，导致 chat_ids 里的字符串 id 不会被归一成 int，
        # 与 sources / exclude_sources 的处理方式不一致。
        return _normalize_refs(value)

    @model_validator(mode="after")
    def _check(self) -> "NotifyConfig":
        if self.enabled:
            if not self.bot_token:
                raise ValueError("notify.enabled=true 时必须提供 bot_token（可用 ${TGA_BOT_TOKEN}）")
            if _looks_unexpanded(self.bot_token):
                raise ValueError(
                    f"notify.bot_token 里的环境变量没有被替换：{self.bot_token}。"
                    "请在 .env 或环境里设置该变量，或直接写死 token。"
                )
            if self.chat_id is None and not self.chat_ids:
                raise ValueError("notify.enabled=true 时必须提供 chat_id 或 chat_ids")
            if ":" not in self.bot_token:
                raise ValueError(
                    "notify.bot_token 格式不对，应形如 123456789:AAE...（从 @BotFather 获取）"
                )
        return self

    def wants(self, event: str) -> bool:
        return self.enabled and event in self.events


# --------------------------------------------------------------------------- #
# 抢红包
# --------------------------------------------------------------------------- #
class RedPacketDetect(StrictModel):
    """红包识别规则。"""

    #: 内联按钮文字包含任一关键词即视为红包按钮。
    button_keywords: list[str] = Field(
        default_factory=lambda: ["领取", "抢", "红包", "开", "拆", "grab", "claim", "open", "🧧"]
    )
    #: 消息正文命中任一正则才尝试抢（为空表示只要有红包按钮就抢）。
    text_patterns: list[str] = Field(default_factory=list)
    #: 从正文提取"口令/编号"的正则，第一个捕获组作为 ``{code}``。
    code_pattern: Optional[str] = None
    #: keyword 策略下要发送的内容模板，例如 ``"/grab {code}"`` 或 ``"抢"``。
    keyword_template: Optional[str] = None
    #: 只处理机器人发的红包（多数红包由 bot 发出，可减少误触）。
    only_from_bots: bool = False
    #: 忽略自己发的消息。
    ignore_self: bool = True

    @field_validator("button_keywords", "text_patterns", mode="before")
    @classmethod
    def _as_list(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return value

    @model_validator(mode="after")
    def _check(self) -> "RedPacketDetect":
        for pattern in self.text_patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"red_packet.detect.text_patterns 正则无效 {pattern!r}: {exc}") from exc
        if self.code_pattern:
            try:
                re.compile(self.code_pattern)
            except re.error as exc:
                raise ValueError(f"red_packet.detect.code_pattern 正则无效: {exc}") from exc
        return self


class RedPacketSuccess(StrictModel):
    """抢红包成功/失败判定。

    判定依据按优先级：
    1. 点按钮后 Telegram 返回的 callback answer 文本（最快，无需等消息）；
    2. 抢包后指定时间窗内该会话的新消息 / 消息编辑内容；
    3. 若都没有命中关键词，则记为 ``unknown``（不会误报成功）。
    """

    success_patterns: list[str] = Field(
        default_factory=lambda: [
            r"抢到",
            r"领取成功",
            r"恭喜",
            r"获得\s*[\d.]+",
            r"\+\s*[\d.]+",
            r"成功领取",
            r"已领取",
            r"领取\d+",
        ]
    )
    failure_patterns: list[str] = Field(
        default_factory=lambda: [
            r"已被(抢|领)完",
            r"手慢",
            r"红包已过期",
            r"已领完",
            r"来晚了",
            r"已经(领取|抢)过",
            r"重复领取",
            r"不能领取",
            r"无效",
            r"感谢参与",
            r"未抢到",
            r"抢光了",
        ]
    )
    #: 等待后续消息判定结果的时间窗（秒）。0 表示只看 callback answer。
    wait_timeout: float = Field(default=8.0, ge=0.0, le=120.0)
    #: 判定时是否要求消息里出现自己的名字/用户名（更精确，但部分红包 bot 不提名）。
    require_self_mention: bool = False

    @model_validator(mode="after")
    def _check(self) -> "RedPacketSuccess":
        for group in (self.success_patterns, self.failure_patterns):
            for pattern in group:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"red_packet.success 正则无效 {pattern!r}: {exc}") from exc
        return self


class RedPacketReply(StrictModel):
    """抢到后的随机回复。"""

    enabled: bool = False
    #: 回复语列表，随机抽一条，例如 ["谢谢老板", "xxlb"]。
    texts: list[str] = Field(default_factory=list)
    #: 仅在确认抢到后回复（推荐）。False 则只要点了就回复。
    only_on_success: bool = True
    #: 回复前的随机延迟区间（秒），模拟真人。
    delay_range: tuple[float, float] = (0.8, 2.5)
    #: 回复是否引用红包消息。
    reply_to_message: bool = True
    #: 同一会话回复冷却（秒），避免连环红包时刷屏。
    cooldown: float = Field(default=0.0, ge=0.0)

    @field_validator("texts", mode="before")
    @classmethod
    def _as_list(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return value

    @field_validator("delay_range", mode="before")
    @classmethod
    def _as_range(cls, value: Any) -> Any:
        if isinstance(value, (int, float)):
            return (float(value), float(value))
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return (float(value[0]), float(value[1]))
        return value

    @model_validator(mode="after")
    def _check(self) -> "RedPacketReply":
        low, high = self.delay_range
        if low < 0 or high < low:
            raise ValueError("red_packet.reply.delay_range 必须满足 0 <= low <= high")
        if self.enabled and not self.texts:
            raise ValueError("red_packet.reply.enabled=true 时 texts 不能为空")
        return self


class RedPacketConfig(StrictModel):
    enabled: bool = False
    #: 监听的会话；为空表示所有会话。
    chats: list[ChatRef] = Field(default_factory=list)
    exclude_chats: list[ChatRef] = Field(default_factory=list)
    strategy: RedPacketStrategy = "auto"
    detect: RedPacketDetect = Field(default_factory=RedPacketDetect)
    success: RedPacketSuccess = Field(default_factory=RedPacketSuccess)
    reply: RedPacketReply = Field(default_factory=RedPacketReply)
    #: 点击前固定延迟（秒）；0 = 最快。
    delay: float = Field(default=0.0, ge=0.0, le=60.0)
    #: 附加随机抖动（秒），躲避"整齐一致"的机器人特征。
    jitter: float = Field(default=0.0, ge=0.0, le=10.0)
    #: 单条红包最多点几次（首次失败可能是网络抖动）。
    max_attempts: int = Field(default=2, ge=1, le=5)
    #: 同一账号并发抢包上限。
    max_concurrency: int = Field(default=3, ge=1, le=20)
    #: 是否推送通知。
    notify: bool = True
    #: 也处理消息编辑事件（有些红包 bot 通过编辑消息挂出按钮）。
    include_edited: bool = True

    @field_validator("chats", "exclude_chats", mode="before")
    @classmethod
    def _normalize(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, (str, int)):
            value = [value]
        return _normalize_refs(value)

    @model_validator(mode="after")
    def _check(self) -> "RedPacketConfig":
        if self.strategy == "keyword" and not self.detect.keyword_template:
            raise ValueError('strategy="keyword" 时必须设置 detect.keyword_template')
        return self


# --------------------------------------------------------------------------- #
# 抢注任务（监听注册码 → 按步骤链自动操作）
# --------------------------------------------------------------------------- #
#: 步骤链里支持的动作：
#:
#: - ``send``：往指定会话发一条消息，模板支持 ``{code}`` 等变量；
#: - ``click``：点掉某条消息上的内联按钮（按按钮文字正则匹配）；
#: - ``wait``：空等若干秒（给机器人留出处理时间）；
#: - ``wait_reply``：等目标会话的下一条消息，命中正则才算成功。
RegGrabStepType = Literal["send", "click", "wait", "wait_reply"]

#: 步骤类型的中文名。只用于日志与界面提示，落盘一律用英文 key。
REG_GRAB_STEP_LABELS: dict[str, str] = {
    "send": "发送消息",
    "click": "点击按钮",
    "wait": "等待",
    "wait_reply": "等待回复",
}


def _check_regex(pattern: str, field: str) -> None:
    """配置里的正则写错要在加载时报出来，而不是等到跑起来才炸。"""
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"{field} 正则无效 {pattern!r}: {exc}") from exc


class RegGrabStep(StrictModel):
    """抢注步骤链里的一步。

    ``type`` 决定哪些字段有意义，用不到的字段留空即可 —— 校验只针对当前类型
    真正需要的那几个字段，这样界面里切换类型时不会因为残留字段报错。
    """

    type: RegGrabStepType
    #: 备注。只用于界面和日志里辨认这一步，不影响执行。
    name: str = ""
    #: 执行这一步之前的固定等待（秒）。
    delay: float = Field(default=0.0, ge=0.0, le=600.0)
    #: 这一步失败时是否继续往下走。默认 False = 立即中止整条链。
    optional: bool = False

    # ---- send ----
    #: 目标会话；留空 = 注册码所在的那个会话。支持 id / @username / t.me 链接。
    chat: Optional[ChatRef] = None
    #: 发送内容模板。可用变量见 :func:`matching.build_variables`，
    #: 其中 ``{code}`` 是正则提取出来的注册码。
    text: Optional[str] = None

    # ---- click ----
    #: 按钮文字正则（忽略大小写）。在「当前消息」上找，找不到再退回目标会话
    #: 最近一条带按钮的消息。
    button: Optional[str] = None

    # ---- wait ----
    #: 等待秒数。
    seconds: float = Field(default=0.0, ge=0.0, le=600.0)

    # ---- wait_reply ----
    #: 判定正则；留空表示收到任意新消息就算成功。
    pattern: Optional[str] = None
    #: 等待超时（秒）。
    timeout: float = Field(default=15.0, ge=0.0, le=300.0)

    @field_validator("chat", mode="before")
    @classmethod
    def _normalize_chat(cls, value: Any) -> Any:
        if isinstance(value, str):
            return parse_chat_ref(value)
        return value

    @field_validator("text", "button", "pattern", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        """界面里清空一个输入框会提交空串，这里统一当成「没填」。"""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _check(self) -> "RegGrabStep":
        if self.type == "send" and not (self.text or "").strip():
            raise ValueError("send 步骤必须填写发送内容")
        if self.type == "click":
            if not (self.button or "").strip():
                raise ValueError("click 步骤必须填写按钮文字")
            _check_regex(self.button, "reg_grab.steps[].button")
        if self.type == "wait_reply" and self.pattern:
            _check_regex(self.pattern, "reg_grab.steps[].pattern")
        return self


class RegGrabDetect(StrictModel):
    """注册码识别规则。"""

    #: 提取注册码的正则。**第一个捕获组**作为 ``{code}``；没有捕获组时用整个匹配。
    code_pattern: Optional[str] = None
    #: 正文预筛正则：命中任意一条才去提取（为空表示不预筛，直接试提取）。
    text_patterns: list[str] = Field(default_factory=list)
    #: 「注册码已被使用」通知的正则，**第一个捕获组**是被遮罩的码。
    #:
    #: 形如 ``🎟️ 注册码使用 - jf [7002057019] 使用了 MSKY-30-Register_f1t░░░░░░░``：
    #: 尾部被遮罩，只有前几位可见。用它反查「这个码是不是已经被人用了」——
    #: 已经被用掉的码再抢就是白跑一趟，还会在群里留下脚本痕迹。
    #:
    #: ⚠️ 捕获组必须以字母数字开头（``[A-Za-z0-9]``）：这句通知里 ``使用`` 出现两次，
    #: 标题里的「注册码使用 - jf」会先被匹配上，用 ``\S+`` 就会把那个 ``-`` 当成码。
    used_pattern: Optional[str] = r"使用[了]?\s*([A-Za-z0-9][^\s，。、]*)"
    #: 使用通知里可见部分至少要有这么多位才拿来做比对。
    #: 只露出 1~2 位时几乎任何码都能「对得上」，宁可放过也不能误杀。
    used_min_len: int = Field(default=3, ge=1, le=32)
    #: 忽略自己发的消息。
    ignore_self: bool = True
    #: 只处理机器人发的消息。
    only_from_bots: bool = False

    @field_validator("text_patterns", mode="before")
    @classmethod
    def _as_list(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return value

    @field_validator("code_pattern", "used_pattern", mode="before")
    @classmethod
    def _blank(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _check(self) -> "RegGrabDetect":
        for pattern in self.text_patterns:
            _check_regex(pattern, "reg_grab.detect.text_patterns")
        if self.code_pattern:
            _check_regex(self.code_pattern, "reg_grab.detect.code_pattern")
        if self.used_pattern:
            _check_regex(self.used_pattern, "reg_grab.detect.used_pattern")
        return self


#: ``HH:MM`` 时钟字面量（也接受 ``H:MM``、中文冒号、以及纯分钟数）。
_CLOCK_RE = re.compile(r"^\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*$")


def _parse_clock(value: Any, field: str) -> int:
    """把 ``"HH:MM"`` 解析成「当天第几分钟」；格式不对直接报错。

    🔴 **不做静默兜底**（比如解析失败就当 0 点）。时间窗这种地方，静默兜底是最危险的：
    配错了要么整天不抢、要么半夜照抢，而两种情况都「看起来一切正常」——
    用户只会觉得「功能没生效」，排查时毫无线索。
    """
    if isinstance(value, bool):  # bool 是 int 的子类，先挡掉
        raise ValueError(f"{field} 要写成 HH:MM（例如 08:00），当前是 {value!r}")
    if isinstance(value, int):
        if 0 <= value <= 1439:
            return value
        raise ValueError(f"{field} 的分钟数要在 0~1439 之间，当前是 {value!r}")
    match = _CLOCK_RE.match(str(value))
    if match is None:
        raise ValueError(f"{field} 要写成 HH:MM（例如 08:00），当前是 {value!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"{field} 不是合法时间（00:00 ~ 23:59），当前是 {value!r}")
    return hour * 60 + minute


def _format_clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


class RegGrabWindow(StrictModel):
    """抢注的**监听时段**：只在这段时间里动手，其余时间看着不动。

    为什么需要它：抢注是秒级响应的行为，**半夜三点还能精准抢到码**是脚本最好认的
    特征之一。把动手时间限制在人类活动时段，能明显降低被识别的概率。

    语义（``enabled=False`` 时 :meth:`contains` 恒为 ``True``，即全天可抢）：

    - ``start`` / ``end`` 用 ``HH:MM``，**本地时区**（服务进程 ``TZ=Asia/Shanghai``）；
    - ``start < end``（如 ``08:00`` ~ ``23:00``）⇒ 当天的这一段；
    - ``start > end``（如 ``22:00`` ~ ``06:00``）⇒ **跨零点**，从 start 到次日 end；
    - ``start == end`` 直接报错 —— 想全天就把开关关掉，不要用「相等」去猜语义。

    区间**左闭右开** ``[start, end)``：``08:00~23:00`` = 08:00:00 到 22:59:59。
    这样 ``08:00~09:00`` 和 ``09:00~10:00`` 首尾相接不会重叠。
    """

    enabled: bool = False
    start: str = "08:00"
    end: str = "23:00"

    @field_validator("start", "end", mode="before")
    @classmethod
    def _normalize_clock(cls, value: Any, info: ValidationInfo) -> str:
        return _format_clock(_parse_clock(value, f"reg_grab.window.{info.field_name}"))

    @model_validator(mode="after")
    def _check_range(self) -> RegGrabWindow:
        if self.start == self.end:
            raise ValueError(
                f"reg_grab.window 的开始和结束时间不能相同（都是 {self.start}）——"
                " 想全天可抢请把 window.enabled 关掉"
            )
        return self

    def contains(self, moment: Optional[datetime] = None) -> bool:
        """``moment``（默认「现在」）是否落在监听时段内。"""
        if not self.enabled:
            return True
        moment = moment or datetime.now()
        minute = moment.hour * 60 + moment.minute
        start = _parse_clock(self.start, "reg_grab.window.start")
        end = _parse_clock(self.end, "reg_grab.window.end")
        if start < end:
            return start <= minute < end
        return minute >= start or minute < end  # 跨零点

    def describe(self) -> str:
        """给人看的一句话，用于日志 / 面板 / CLI。"""
        if not self.enabled:
            return "全天"
        suffix = "（跨零点）" if self.start > self.end else ""
        return f"{self.start}~{self.end}{suffix}"


class RegGrabConfig(StrictModel):
    """抢注任务：监听到符合正则的注册码后，按**步骤链**自动操作。

    与抢红包的区别：红包是「点一下就完事」，抢注是**多步流程** ——
    典型用法是把 ``/bind {code}`` 发给某个机器人，再等它的回执。

    ⚠️ 这里**故意不校验**「enabled 时必须配好正则和步骤」：``StrictModel`` 开了
    ``validate_assignment``，而面板的总开关就是 ``config.reg_grab.enabled = True``
    这种赋值 —— 写成模型级校验会让「先开开关再填内容」直接 500。
    完整性交给 :attr:`ready` 判断，由 API 与引擎各自给出提示。
    """

    enabled: bool = False
    #: 监听的会话；为空表示所有会话。
    chats: list[ChatRef] = Field(default_factory=list)
    exclude_chats: list[ChatRef] = Field(default_factory=list)
    detect: RegGrabDetect = Field(default_factory=RegGrabDetect)
    #: 步骤链，按顺序执行。
    steps: list[RegGrabStep] = Field(default_factory=list)
    #: 动手前的固定延迟（秒）。
    delay: float = Field(default=0.5, ge=0.0, le=60.0)
    #: 附加随机抖动（秒）：实际延迟 = ``delay`` + ``random(0, jitter)``。
    #:
    #: 默认给一点（0.5~2 秒），原因有两个：
    #: 1. 秒回是最显眼的脚本特征，随机延迟能把它磨掉；
    #: 2. 这段等待正好是「使用通知」的检测窗口 —— 真被别人抢了，
    #:    通知会在这期间冒出来，链还没开始就能刹车（见 ``_is_used``）。
    jitter: float = Field(default=1.5, ge=0.0, le=10.0)
    #: 同一个注册码在这个时间窗内只处理一次（秒）。同一条码被多个群转发出来时，
    #: 靠它避免重复抢。
    code_ttl: float = Field(default=3600.0, ge=0.0, le=86400.0)
    #: 并发上限。同一条码内部的步骤是串行的，这里限制的是「同时处理几条码」。
    max_concurrency: int = Field(default=1, ge=1, le=20)
    #: 是否推送通知。
    notify: bool = True
    #: 也处理消息编辑事件（有些码是通过编辑消息补上的）。
    include_edited: bool = True
    #: 监听时段：只在这段时间里动手，其余时间看着不动（避免半夜秒抢暴露脚本）。
    window: RegGrabWindow = Field(default_factory=RegGrabWindow)

    @field_validator("chats", "exclude_chats", mode="before")
    @classmethod
    def _normalize(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, (str, int)):
            value = [value]
        return _normalize_refs(value)

    @property
    def ready(self) -> bool:
        """配置是否完整到能干活：开了开关、有提取正则、且至少一条步骤。

        ⚠️ 这里**不看**监听时段：时段是「现在能不能动手」，``ready`` 是「配好了没有」。
        把两者混在一起，面板上会出现「明明配好了却显示未就绪」，而且随着时间跳变。
        当前是否在时段内由 :meth:`in_window` 单独回答。
        """
        return bool(self.enabled and self.detect.code_pattern and self.steps)

    @property
    def in_window(self) -> bool:
        """此刻是否落在监听时段内（``window.enabled=False`` 时恒为 ``True``）。"""
        return self.window.contains()


# --------------------------------------------------------------------------- #
# Cloudflare 优选 IP 自动更新
# --------------------------------------------------------------------------- #
#: 支持的运营商标识。落盘和 API 一律用这几个英文 key，
#: 中文名（移动/电信/联通）只出现在界面与日志里，见 ``cloudflare_ip.ISP_LABELS``。
ISP_KEYS: tuple[str, ...] = ("mobile", "telecom", "unicom")


class CloudflareDNSRecord(StrictModel):
    """一条要更新的 DNS 记录。"""

    #: Cloudflare Zone ID
    zone_id: str
    #: 域名（如 example.com）
    domain: str
    #: 记录名（如 ``@`` 表示根域、``www`` 表示 ``www.example.com``）
    name: str = "@"
    #: 记录类型：A 或 AAAA
    record_type: Literal["A", "AAAA"] = "A"
    #: 是否启用 Cloudflare 代理（小黄云），默认开启
    proxied: bool = True
    #: TTL（秒），1 = 自动
    ttl: int = Field(default=1, ge=1, le=86400)


class CloudflareIPConfig(StrictModel):
    """从 Telegram 频道抓取优选 IP 并自动更新 Cloudflare DNS 记录。

    两种触发模式：

    - **实时监听**：账号在线时，源频道一有新消息就立刻解析、决策、更新。
      需要在 ``source_channel`` 所在会话有读消息权限（频道公开或已加入）。
    - **定时轮询**：通过 ``interval_hours`` 周期性拉取频道最近消息，
      适用于不需要「秒级跟进」、或者账号不长期在线的场景。

    决策逻辑（:func:`cloudflare_ip.should_update`）：

    1. 解析出频道消息里「最快」IP 及其速度；
    2. 速度低于 ``min_speed_threshold`` → 跳过；
    3. ``only_update_if_faster`` 开启时，对比上次更新记录的速度，
       新 IP 不比现在快 → 跳过；
    4. 通过所有检查 → 执行 DNS 更新。
    """

    enabled: bool = False
    #: Cloudflare API Token（Zone:DNS 编辑权限）
    api_token: Optional[str] = None
    #: 源 Telegram 频道：抓取该频道最近消息中的优选 IP
    source_channel: Optional[ChatRef] = None
    #: 抓取最近多少条消息。这类频道是「一条消息只讲一个运营商」的格式
    #: （如 ``@cfyxip`` 首行标 ``(移动)``/``(电信)``/``(联通)``），
    #: 所以要够多才能凑齐三网 —— 默认 20 条。
    fetch_limit: int = Field(default=20, ge=1, le=100)
    #: 要更新的 DNS 记录列表
    records: list[CloudflareDNSRecord] = Field(default_factory=list)
    #: 定时检查间隔（小时），0 = 仅手动触发 / 实时监听
    interval_hours: float = Field(default=0.0, ge=0.0, le=168.0)
    #: 最低速度阈值（MB/s）。频道解析出来的最快 IP 低于此值时不更新。
    #: 0 = 不限制。三网分流时作为**兜底值**，某个运营商没在
    #: ``min_speed_threshold_by_isp`` 里单独配才用它。
    min_speed_threshold: float = Field(default=0.0, ge=0.0)
    #: 三网分流：同一个域名下**每个运营商各写一条 A 记录**，
    #: 用 Cloudflare 记录的 comment 标记区分（移动/电信/联通各一条），
    #: 客户端按自己所在的网自动选到最近的那条。
    #:
    #: 关闭时维持原行为：所有记录都写「整体最快」的那一个 IP。
    split_by_isp: bool = False
    #: 按运营商分别设的最低速度阈值（MB/s），键取 ``ISP_KEYS``。
    #:
    #: 为什么必须分开设：三网测速差距极大 —— 同一天里电信能跑到 166 MB/s、
    #: 联通 80 MB/s，而移动最好只有 27 MB/s。共用一个阈值的话，
    #: 移动那条记录会永远不更新。
    min_speed_threshold_by_isp: dict[str, float] = Field(default_factory=dict)
    #: 开启后，更新前会对比当前 DNS 记录对应 IP 的「上次更新速度」，
    #: 只有新 IP 速度 > 当前速度才更新；否则保留现有记录。
    only_update_if_faster: bool = True
    #: 实时监听：账号在线时是否监听源频道的新消息。
    #: 关闭后只走定时轮询 / 手动触发。
    real_time_listen: bool = True

    @field_validator("min_speed_threshold_by_isp", mode="before")
    @classmethod
    def _check_isp_thresholds(cls, value: Any) -> Any:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("min_speed_threshold_by_isp 必须是 {运营商: 阈值} 形式")
        cleaned: dict[str, float] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key).strip()
            if key not in ISP_KEYS:
                raise ValueError(
                    f"min_speed_threshold_by_isp 里的键 {key!r} 不是运营商标识，"
                    f"只能是 {'、'.join(ISP_KEYS)} 之一"
                )
            try:
                number = float(raw_value)
            except (TypeError, ValueError):
                raise ValueError(f"运营商 {key} 的阈值 {raw_value!r} 不是数字") from None
            if number < 0:
                raise ValueError(f"运营商 {key} 的阈值不能为负数")
            cleaned[key] = number
        return cleaned

    @field_validator("api_token", mode="before")
    @classmethod
    def _expand_token(cls, value: Any) -> Any:
        if isinstance(value, str):
            expanded = expand_env(value).strip()
            return expanded or None
        return value

    @field_validator("source_channel", mode="before")
    @classmethod
    def _normalize_channel(cls, value: Any) -> Any:
        return parse_chat_ref(value)

    @model_validator(mode="after")
    def _check(self) -> "CloudflareIPConfig":
        if self.enabled:
            if not self.api_token:
                raise ValueError(
                    "cloudflare_ip.enabled=true 时必须提供 api_token"
                    "（可用 ${TGA_CF_API_TOKEN}）"
                )
            if _looks_unexpanded(self.api_token):
                raise ValueError(
                    f"cloudflare_ip.api_token 里的环境变量没有被替换：{self.api_token}。"
                    "请在 .env 或环境里设置该变量，或直接写 token。"
                )
            if self.source_channel is None:
                raise ValueError(
                    "cloudflare_ip.enabled=true 时必须提供 source_channel"
                    "（Telegram 频道 @username 或 chat_id）"
                )
            if not self.records:
                raise ValueError(
                    "cloudflare_ip.enabled=true 时必须配置至少一条 records"
                )
        return self


# --------------------------------------------------------------------------- #
# 账号业务配置
# --------------------------------------------------------------------------- #
class AccountConfig(StrictModel):
    """单账号业务配置（``config.json``）。"""

    version: int = CONFIG_VERSION
    forward: ForwardConfig = Field(default_factory=ForwardConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    red_packet: RedPacketConfig = Field(default_factory=RedPacketConfig)
    reg_grab: RegGrabConfig = Field(default_factory=RegGrabConfig)
    cloudflare_ip: CloudflareIPConfig = Field(default_factory=CloudflareIPConfig)

    @classmethod
    def default(cls) -> "AccountConfig":
        return cls()

    @property
    def needs_updates(self) -> bool:
        """是否需要实时更新流（决定 Client 的 no_updates 取值）。"""
        return (
            bool(self.forward.active_rules)
            or self.red_packet.enabled
            or self.reg_grab.enabled
            or self.cloudflare_ip.enabled
        )

    def watched_chats(self) -> list[ChatRef]:
        """所有需要监听的会话；含空列表规则时返回 ``[]`` 表示"监听全部"。"""
        chats: list[ChatRef] = []
        for rule in self.forward.active_rules:
            if not rule.sources:
                return []
            chats.extend(rule.sources)
        if self.red_packet.enabled:
            if not self.red_packet.chats:
                return []
            chats.extend(self.red_packet.chats)
        if self.reg_grab.enabled:
            if not self.reg_grab.chats:
                return []
            chats.extend(self.reg_grab.chats)
        deduped: list[ChatRef] = []
        for chat in chats:
            if chat not in deduped:
                deduped.append(chat)
        return deduped


# --------------------------------------------------------------------------- #
# 账号注册表
# --------------------------------------------------------------------------- #
class AccountRecord(StrictModel):
    """``data/accounts.json`` 中的一条账号记录。

    不保存 2FA 密码；session 文件本身即凭据，文件权限设为 0600。
    """

    name: str
    enabled: bool = True
    user_id: Optional[int] = None
    username: Optional[str] = None
    display_name: Optional[str] = None
    phone: Optional[str] = None  # 仅存脱敏后的尾号
    api_id: Optional[int] = None
    api_hash: Optional[str] = None
    proxy: Optional[ProxyConfig] = None
    #: 客户端伪装参数，多账号建议各不相同。
    device_model: Optional[str] = None
    app_version: Optional[str] = None
    system_version: Optional[str] = None
    lang_code: str = "zh"
    created_at: Optional[str] = None
    last_login_at: Optional[str] = None
    note: Optional[str] = None

    @field_validator("api_hash", mode="before")
    @classmethod
    def _expand_hash(cls, value: Any) -> Any:
        if isinstance(value, str):
            return expand_env(value).strip() or None
        return value

    @property
    def label(self) -> str:
        parts = [self.name]
        if self.username:
            parts.append(f"@{self.username}")
        elif self.display_name:
            parts.append(self.display_name)
        if self.user_id:
            parts.append(f"id={self.user_id}")
        return " ".join(parts)


class AccountRegistry(StrictModel):
    version: int = CONFIG_VERSION
    accounts: list[AccountRecord] = Field(default_factory=list)

    def get(self, name: str) -> Optional[AccountRecord]:
        for record in self.accounts:
            if record.name == name:
                return record
        return None

    def upsert(self, record: AccountRecord) -> None:
        for index, existing in enumerate(self.accounts):
            if existing.name == record.name:
                self.accounts[index] = record
                return
        self.accounts.append(record)

    def remove(self, name: str) -> bool:
        before = len(self.accounts)
        self.accounts = [r for r in self.accounts if r.name != name]
        return len(self.accounts) != before

    @property
    def enabled_accounts(self) -> list[AccountRecord]:
        return [r for r in self.accounts if r.enabled]


# --------------------------------------------------------------------------- #
# .env 加载
# --------------------------------------------------------------------------- #
try:  # pragma: no cover - 依赖缺失时走下面的降级分支
    from dotenv import load_dotenv as _load_dotenv
except ImportError:  # pragma: no cover
    _load_dotenv = None


def load_env_file(path: Optional[Union[str, "os.PathLike[str]"]] = None) -> Optional[str]:
    """加载 ``.env``，返回实际读取的文件路径；没读到则返回 ``None``。

    查找顺序：显式 ``path`` → 环境变量 ``TGA_ENV_FILE`` → 当前工作目录下的 ``.env``。

    **不覆盖已存在的环境变量**，与 docker compose 的 ``env_file`` / systemd 的
    ``EnvironmentFile`` 语义保持一致，优先级为：
    真实环境变量 > ``.env`` > 代码默认值。

    这样做是为了 ``deploy.sh`` 里手工执行的 ``tg-assistant login`` /
    ``config init``：它们不经过 systemd，读不到 ``EnvironmentFile``，
    之前会直接报「缺少 api_id / api_hash」。
    """
    if path is not None:
        candidate = Path(path).expanduser()
    elif os.environ.get("TGA_ENV_FILE"):
        candidate = Path(os.environ["TGA_ENV_FILE"]).expanduser()
    else:
        candidate = Path.cwd() / ".env"

    if not candidate.is_file():
        return None

    if _load_dotenv is None:
        raise RuntimeError(
            f"发现环境变量文件 {candidate}，但缺少 python-dotenv 依赖，无法加载。"
            '请重新安装：pip install -e ".[speed]"（或单独 pip install python-dotenv）'
        )

    _load_dotenv(dotenv_path=candidate, override=False)
    return str(candidate)


# --------------------------------------------------------------------------- #
# 进程设置
# --------------------------------------------------------------------------- #
class Settings(StrictModel):
    """来自环境变量 / CLI 的进程级设置。"""

    data_dir: str = "./data"
    api_id: Optional[int] = None
    api_hash: Optional[str] = None
    proxy: Optional[ProxyConfig] = None
    log_level: str = "INFO"
    pyrogram_log_level: str = "WARNING"
    #: pyrogram 更新处理 worker 数；秒级转发建议 >= 4。
    workers: int = Field(default=8, ge=1, le=64)
    #: FloodWait 自动等待阈值（秒），超过则抛错交给上层重试。
    sleep_threshold: int = Field(default=30, ge=0)
    ipv6: bool = False

    @classmethod
    def from_env(cls, **overrides: Any) -> "Settings":
        """读取 ``TGA_*`` 环境变量；``overrides`` 中的非 None 值优先。"""
        # 先补上 .env（已存在的环境变量优先），再统一从 os.environ 取值。
        load_env_file()
        env = os.environ
        proxy: Optional[ProxyConfig] = None
        proxy_url = overrides.pop("proxy_url", None) or env.get("TGA_PROXY")
        if proxy_url:
            proxy = ProxyConfig.from_url(proxy_url)

        api_id_raw = overrides.pop("api_id", None) or env.get("TGA_API_ID")
        data: dict[str, Any] = {
            "data_dir": overrides.pop("data_dir", None) or env.get("TGA_DATA_DIR") or "./data",
            "api_id": int(api_id_raw) if api_id_raw else None,
            "api_hash": overrides.pop("api_hash", None) or env.get("TGA_API_HASH") or None,
            "proxy": proxy,
            "log_level": overrides.pop("log_level", None) or env.get("TGA_LOG_LEVEL") or "INFO",
            "pyrogram_log_level": env.get("TGA_PYROGRAM_LOG_LEVEL") or "WARNING",
            "workers": int(env.get("TGA_WORKERS") or 8),
            "sleep_threshold": int(env.get("TGA_SLEEP_THRESHOLD") or 30),
            "ipv6": (env.get("TGA_IPV6") or "0") == "1",
        }
        data.update({k: v for k, v in overrides.items() if v is not None})
        return cls.model_validate(data)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def mask_phone(phone: str | None) -> Optional[str]:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if len(digits) <= 4:
        return "*" * len(digits)
    return f"{digits[:2]}****{digits[-4:]}"


__all__ = [
    "AccountConfig",
    "AccountRecord",
    "AccountRegistry",
    "CloudflareDNSRecord",
    "CloudflareIPConfig",
    "CONFIG_VERSION",
    "ChatRef",
    "ForwardConfig",
    "ForwardMode",
    "ForwardRule",
    "MatchConfig",
    "MatchMode",
    "NotifyConfig",
    "ProxyConfig",
    "RedPacketConfig",
    "RedPacketDetect",
    "RedPacketReply",
    "RedPacketStrategy",
    "RedPacketSuccess",
    "Settings",
    "StrictModel",
    "ValidationError",
    "expand_env",
    "load_env_file",
    "mask_phone",
    "parse_chat_ref",
    "utc_now_iso",
]
