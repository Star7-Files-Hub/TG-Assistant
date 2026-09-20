"""秒级正则转发引擎。

延迟从哪里来、怎么压下去：

1. **不在 handler 里做慢活**：pyrogram 的 handler worker 是串行执行同一 group 的 handler，
   在里面 ``await`` 网络请求会阻塞后续消息。这里 handler 只做纯内存匹配（微秒级），
   命中后立刻 ``create_task`` 派发，真正的发送在独立任务里跑。
2. **零 ``get_chat``**：所有会话/用户判断都用预构建的集合，不发网络请求。
3. **正则预编译**：配置加载时编译一次。
4. **相册聚合**：同一 ``media_group_id`` 的多条消息在 ``media_group_window`` 内攒齐后
   一次性 ``forward_messages(message_ids=[...])``，避免拆成多条丢失排版。
5. **去重**：两层 —— 账号内 ``(规则 id, chat_id, message_id)`` + TTL，防止编辑事件或
   重复更新造成二次转发；**跨账号** ``(chat_id, message_id, 目标)`` 共享表，防止多个账号
   都在同一个源群里时把同一条消息各发一遍。
6. **forward 失败自动降级 copy**：源会话受保护时 Telegram 会拒 forward，此时自动改用
   复制再发一次，而不是直接失败。

⚠️ **``copy`` 模式 = 按 ``file_id`` 重新发送一条新消息**，不是
``forward_messages(hide_sender_name=True)``。后者发出来的是**转发消息**，而 Telegram
**拒绝编辑转发消息**（实测 ``400 Bad Request: message can't be edited``）⇒
「先转发、再 edit 补上原文链接」这条路根本走不通。重发拿到的是全新消息，链接在**发送时**
就写进正文，一次 RPC 搞定、零编辑。

⚠️ **``forward`` 模式不动那条转发消息**：原文链接在它**下方**单独补发一条
（``🔗原文链接：<link>``）。``copy`` 才是写进当前消息正文。

每条转发都会记录 ``pipeline_ms``（消息在 Telegram 的时间戳到发送完成的总耗时）和
``handler_ms``（本进程内耗时），日志里直接能看出慢在哪一段。
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import ChatForwardsRestricted
from pyrogram.handlers import EditedMessageHandler, MessageHandler
from pyrogram.types import LinkPreviewOptions

from .client import SessionInvalid, with_flood_retry
from .config import AccountConfig, ChatRef, ForwardRule
from .logging_setup import AccountLogger
from .matching import (
    MAX_TEXT_LENGTH,
    CompiledMatcher,
    MatchResult,
    RefSet,
    apply_groups,
    build_variables,
    chat_identity,
    chat_kind,
    message_text,
    normalize_chat_kind,
    render_template,
    sender_of,
    truncate,
)
from .notify import BotNotifier, NotifyTask, build_notify_text


# --------------------------------------------------------------------------- #
# 去重
# --------------------------------------------------------------------------- #
class DedupeCache:
    """带 TTL 的一次性键集合。

    用惰性清理（每 N 次插入扫一遍）而不是后台定时任务，省掉一个 task。
    """

    __slots__ = ("_seen", "_ttl", "_ops")

    def __init__(self, ttl: float = 300.0) -> None:
        self._seen: dict[tuple[Any, ...], float] = {}
        self._ttl = ttl
        self._ops = 0

    def check_and_add(self, key: tuple[Any, ...]) -> bool:
        """首次出现返回 True；TTL 内重复出现返回 False。"""
        if self._ttl <= 0:
            return True
        now = time.monotonic()
        self._ops += 1
        if self._ops % 256 == 0:
            self._purge(now)
        seen_at = self._seen.get(key)
        if seen_at is not None and now - seen_at < self._ttl:
            return False
        self._seen[key] = now
        return True

    def _purge(self, now: float) -> None:
        expired = [k for k, t in self._seen.items() if now - t >= self._ttl]
        for key in expired:
            self._seen.pop(key, None)

    def __len__(self) -> int:
        return len(self._seen)


class CrossAccountDedupe:
    """跨账号去重：同一条源消息发往**同一个目标**，只允许一个账号发出去。

    为什么需要它：多个账号可能都在同一个源群里（规则也一样），于是同一条消息
    会被每个账号各转发一次，目标频道里就出现重复。账号内的 :class:`DedupeCache`
    是 **per-engine** 的，挡不住这种重复。

    键是 ``(源会话, 消息 id, 目标会话)`` 三元组，**必须带目标**：只按
    ``(源会话, 消息 id)`` 去重的话，「账号 A 发到 T1、账号 B 发到 T2」会被误判成
    重复，后一个账号**丢消息**（项目里早先那版 Postgres 去重就是两元组键）。

    为什么用进程内共享而不是数据库：单进程里所有账号的 handler 都跑在**同一个
    事件循环**上，而 :meth:`claim` 是**纯同步、无 await** 的 —— 在 handler 里天然
    原子，不可能出现两个账号同时抢到同一个名额。这比「每条消息连一次库」既快又准
    （那版实现是同步阻塞的，会把转发延迟从毫秒级推到几百毫秒）。

    TTL 按次传入、不在构造时固定：``dedupe_window`` 是**账号级**配置，多个账号
    可以配不同的值，共享表不能只有一个 TTL。
    """

    __slots__ = ("_seen", "_ops", "claimed", "rejected")

    def __init__(self) -> None:
        #: 键 -> 过期时刻（``time.monotonic()`` 基准）。
        self._seen: dict[tuple[Any, ...], float] = {}
        self._ops = 0
        #: 占用成功（= 由本账号发出）的次数。
        self.claimed = 0
        #: 因「别的账号已经发过」而跳过的次数。
        self.rejected = 0

    def claim(self, source_chat_id: Any, message_id: Any, target: Any, ttl: float) -> bool:
        """尝试占用「这条源消息发往这个目标」的名额。

        首次（或 TTL 过后）返回 ``True``，表示由本账号发送；TTL 内已被占用返回
        ``False``，调用方应当跳过。``ttl <= 0`` 表示关闭去重，恒返回 ``True``。
        """
        if ttl <= 0:
            self.claimed += 1
            return True
        now = time.monotonic()
        self._ops += 1
        if self._ops % 256 == 0:
            self._purge(now)
        key = (source_chat_id, message_id, str(target))
        deadline = self._seen.get(key)
        if deadline is not None and deadline > now:
            self.rejected += 1
            return False
        self._seen[key] = now + ttl
        self.claimed += 1
        return True

    def _purge(self, now: float) -> None:
        for key in [k for k, deadline in self._seen.items() if deadline <= now]:
            self._seen.pop(key, None)

    def __len__(self) -> int:
        return len(self._seen)

    def snapshot(self) -> dict[str, Any]:
        return {"size": len(self._seen), "claimed": self.claimed, "rejected": self.rejected}


# --------------------------------------------------------------------------- #
# 预编译规则
# --------------------------------------------------------------------------- #
@dataclass
class PreparedRule:
    """把 :class:`ForwardRule` 预处理成运行期高效结构。"""

    rule: ForwardRule
    matcher: CompiledMatcher
    sources: RefSet
    exclude_sources: RefSet
    from_users: RefSet
    exclude_users: RefSet
    #: 转发目标集合。**只用于"目标不能当来源"的防循环判断**，不参与其它筛选。
    targets: RefSet
    last_fired: float = 0.0
    stats: dict[str, int] = field(default_factory=lambda: {"matched": 0, "sent": 0, "failed": 0, "skipped": 0})

    @classmethod
    def build(cls, rule: ForwardRule) -> "PreparedRule":
        return cls(
            rule=rule,
            matcher=CompiledMatcher(rule.match),
            sources=RefSet(rule.sources),
            exclude_sources=RefSet(rule.exclude_sources),
            from_users=RefSet(rule.from_users),
            exclude_users=RefSet(rule.exclude_users),
            targets=RefSet(rule.targets),
        )

    @property
    def id(self) -> str:
        return self.rule.id

    @property
    def label(self) -> str:
        return self.rule.label

    def chat_allowed(
        self,
        chat_id: Optional[int],
        chat_username: Optional[str],
        kind: Optional[str] = None,
    ) -> tuple[bool, str]:
        """``kind`` 可以是 :func:`~tg_assistant.matching.chat_kind` 的返回值，
        也可以是 pyrogram 的原始 ``ChatType`` / 其 ``.value``。

        ``sources`` 为空表示"监听全部"，但**只限群组与频道**：
        私聊一律不参与转发。否则任何陌生人给账号发一条含关键词的私信，
        都会被原样转发到目标频道里去。

        这里走 :func:`~tg_assistant.matching.normalize_chat_kind` 而不是直接比较
        字符串：``private`` / ``bot`` / ``direct`` 都是 1:1 会话，
        逐个枚举容易漏（``direct`` 就漏过一次）。

        **本规则的目标会话永远不能当来源**，即使它被显式写进 ``sources``。
        原因：``sources=[]``（监听全部）时，转发到目标的那些新消息会被本账号
        重新监听到（新消息 = 新 id，去重缓存拦不住），再次命中同一条规则，
        于是每转发一次就多产生一次命中 —— 正反馈死循环，几秒内就能刷爆目标频道。
        详见 ``tests/test_forwarder.py::TestPreparedRule::test_target_never_acts_as_source``。
        """
        if self.targets and self.targets.matches(chat_id, chat_username):
            return False, "该会话是本规则的目标，不能作为来源（防转发循环）"
        if self.exclude_sources and self.exclude_sources.matches(chat_id, chat_username):
            return False, "来源在 exclude_sources 中"
        if self.sources:
            if not self.sources.matches(chat_id, chat_username):
                return False, "来源不在 sources 中"
            return True, ""
        if normalize_chat_kind(kind) == "private":
            return False, "未限定 sources 时只监听群组与频道，私聊不转发"
        return True, ""

    def sender_allowed(
        self, sender_id: Optional[int], username: Optional[str], is_self: bool
    ) -> tuple[bool, str]:
        if self.rule.ignore_self and is_self:
            return False, "忽略自己发送的消息（ignore_self）"
        if self.exclude_users and self.exclude_users.matches(sender_id, username, is_self=is_self):
            return False, "发送者在 exclude_users 中"
        if self.from_users and not self.from_users.matches(sender_id, username, is_self=is_self):
            return False, "发送者不在 from_users 中"
        return True, ""

    def interval_ok(self, now: float) -> tuple[bool, str]:
        if self.rule.min_interval <= 0:
            return True, ""
        elapsed = now - self.last_fired
        if elapsed < self.rule.min_interval:
            return False, f"触发过于频繁（{elapsed:.1f}s < min_interval {self.rule.min_interval}s）"
        return True, ""


# --------------------------------------------------------------------------- #
# 相册缓冲
# --------------------------------------------------------------------------- #
@dataclass
class MediaGroupBuffer:
    """同一相册的消息缓冲。"""

    chat_id: int
    group_id: str
    rule_id: str
    message_ids: list[int]
    first_message: Any
    match: MatchResult
    created_at: float = field(default_factory=time.monotonic)
    task: Optional[asyncio.Task[None]] = None


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #
class ForwardEngine:
    """把匹配到的消息转发到目标会话，并同步推送 bot 通知。"""

    #: handler group：转发用 0，抢红包用 1，互不干扰。
    HANDLER_GROUP = 0

    #: 热重载检查间隔（秒）。只决定「多久发现一次变更」，不影响正确性 ——
    #: 真正决定要不要重载的是 ``config.json`` 的 mtime。
    RELOAD_CHECK_INTERVAL = 1.0

    def __init__(
        self,
        client: Client,
        config: AccountConfig,
        alog: AccountLogger,
        notifier: Optional[BotNotifier] = None,
        *,
        store: Optional[Any] = None,
        account: Optional[str] = None,
        shared_dedupe: Optional[CrossAccountDedupe] = None,
    ) -> None:
        self.client = client
        self.config = config
        self.alog = alog.bind("forward")
        self.notifier = notifier
        self.rules: list[PreparedRule] = [
            PreparedRule.build(rule) for rule in config.forward.active_rules
        ]
        #: 账号级全局排除：这些会话任何规则都不监听（与每条规则的 exclude_sources 互补）。
        self._exclude_chats = RefSet(config.forward.exclude_chats)
        self.dedupe = DedupeCache(config.forward.dedupe_window)
        #: 跨账号去重表（同一条消息发往同一个目标只允许一个账号发）。
        #: 由 ``MultiRunner`` 创建、多个账号**共享同一个实例**；不传（单测 / 离线场景）
        #: 即关闭跨账号去重，行为与从前一致。
        self._shared_dedupe = shared_dedupe

        # ---- 规则热重载 ----
        #: 只有同时给了 ``store`` 与 ``account`` 才开启；单测 / 离线场景不传，
        #: 行为与从前完全一致（永不读盘）。
        self._store = store
        self._account = account
        self._config_file = self._resolve_config_file()
        #: 上次读到的 config.json mtime。``None`` = 还没读到过。
        self._rules_mtime: Optional[float] = self._config_mtime()
        self._last_reload_check = 0.0
        self._handlers: list[tuple[Any, int]] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._media_groups: dict[tuple[int, str], MediaGroupBuffer] = {}
        self._lock = asyncio.Lock()
        self.stats = {
            "received": 0,
            "excluded": 0,
            "matched": 0,
            "forwarded": 0,
            "failed": 0,
            "deduped": 0,
            #: 因**别的账号**已经把这条消息发往同一目标而跳过的次数。
            "cross_deduped": 0,
            #: 源会话禁止转发、自动改用复制的次数。
            "downgraded": 0,
        }

    # ------------------------------------------------------------------ #
    # 规则热重载（改完配置不用重启账号）
    # ------------------------------------------------------------------ #
    def _resolve_config_file(self) -> Optional[Any]:
        """账号 ``config.json`` 的路径；没给 store/account 时返回 ``None``（关闭热重载）。"""
        if self._store is None or self._account is None:
            return None
        try:
            return self._store.paths.account(self._account).config_file
        except Exception:  # pragma: no cover - 账号名异常时静默关闭热重载
            return None

    def _config_mtime(self) -> Optional[float]:
        if self._config_file is None:
            return None
        try:
            return self._config_file.stat().st_mtime
        except OSError:
            return None

    def _rules_signature(self) -> tuple[Any, ...]:
        """规则指纹。用来识别「mtime 变了但内容其实没变」（例如面板原样保存一次）。"""
        return (
            self.config.forward.enabled,
            tuple(rule.model_dump(mode="json") for rule in self.config.forward.active_rules),
            tuple(str(chat) for chat in self.config.forward.exclude_chats),
        )

    def reload_rules(self) -> bool:
        """重新从磁盘读账号配置，重建规则与 handler 过滤器。返回 True 表示确实变了。

        **为什么 handler 必须一起重建**：``sources`` 决定 handler 的过滤器
        （限定了来源用 ``filters.chat(chats)``，未限定用 ``filters.group | filters.channel``）。
        只换 ``self.rules`` 而不换过滤器的话，把 ``sources`` 从「指定群」改成 ``[]``
        （= 监听全部）之后，**新群的消息根本进不来** —— 规则配得再对也没用。
        """
        if self._store is None or self._account is None:
            return False
        try:
            config = self._store.load_account_config(self._account, create=False)
        except Exception as exc:
            # 配置被写坏（例如面板提交了非法正则）时**保留旧规则继续跑**，
            # 不能因为一次坏写就让转发整个停摆。
            self.alog.warning("规则热重载失败，继续沿用旧规则", error=str(exc))
            return False

        before = self._rules_signature()
        self.config = config
        self.rules = [PreparedRule.build(rule) for rule in config.forward.active_rules]
        self._exclude_chats = RefSet(config.forward.exclude_chats)
        if self._rules_signature() == before:
            return False

        self.alog.info(
            "转发规则已热重载（无需重启）",
            rules=len(self.rules),
            enabled=config.forward.enabled,
            exclude_chats=len(self._exclude_chats.ids) + len(self._exclude_chats.usernames),
            rule_ids=",".join(prepared.id for prepared in self.rules),
        )
        self._refresh_handlers()
        return True

    def _maybe_reload(self) -> None:
        """按 :attr:`RELOAD_CHECK_INTERVAL` 节流检查 mtime，变了才真去读盘。

        全程**没有 await**，所以在事件循环里是原子的 —— 不会出现
        「检查到一半规则被换掉」的中间态。
        """
        if self._config_file is None:
            return
        now = time.monotonic()
        if now - self._last_reload_check < self.RELOAD_CHECK_INTERVAL:
            return
        self._last_reload_check = now
        mtime = self._config_mtime()
        if mtime is None or mtime == self._rules_mtime:
            return
        self._rules_mtime = mtime
        self.reload_rules()

    def _refresh_handlers(self) -> None:
        """注销旧 handler，再按新规则重新注册。

        规则被全部禁用时 ``register()`` 会直接返回 —— 此时 handler 保持已注销状态，
        正是想要的（不监听）。之后重新启用会再次触发重载并重新注册。
        """
        for handler, group in self._handlers:
            with contextlib.suppress(Exception):
                self.client.remove_handler(handler, group)
        self._handlers.clear()
        self.register()

    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        return self.config.forward.enabled and bool(self.rules)

    def watched_chats(self) -> list[ChatRef]:
        """所有规则来源的并集；任一规则未限定来源则返回空（监听全部）。"""
        chats: list[ChatRef] = []
        for prepared in self.rules:
            if not prepared.rule.sources:
                return []
            for source in prepared.rule.sources:
                if source not in chats:
                    chats.append(source)
        return chats

    def register(self) -> None:
        """注册消息处理器。必须在 client 启动前或启动后立即调用。"""
        if not self.enabled:
            self.alog.info("转发功能未启用（无启用的规则）")
            return

        chats = self.watched_chats()
        if chats:
            message_filter: Any = filters.chat(chats)
        else:
            # 未限定来源 = 监听全部群组与频道。
            # 这里刻意用 filters.group | filters.channel 而不是 None：
            # None 会把私聊也收进来，陌生人发条含关键词的私信就会被转发出去。
            message_filter = filters.group | filters.channel
        handler = MessageHandler(self._on_message, message_filter)
        self._handlers.append(self.client.add_handler(handler, group=self.HANDLER_GROUP))

        if any(prepared.rule.include_edited for prepared in self.rules):
            edited = EditedMessageHandler(self._on_edited, message_filter)
            self._handlers.append(self.client.add_handler(edited, group=self.HANDLER_GROUP))

        self.alog.info(
            "转发引擎已注册",
            rules=len(self.rules),
            watched_chats=len(chats) or "全部群组/频道",
            exclude_chats=len(self._exclude_chats.ids) + len(self._exclude_chats.usernames),
            dedupe_window_s=self.config.forward.dedupe_window,
            rule_ids=",".join(prepared.id for prepared in self.rules),
        )
        for prepared in self.rules:
            self.alog.info(
                f"  规则 [{prepared.label}]",
                mode=prepared.rule.mode,
                match_mode=prepared.rule.match.mode,
                patterns=len(prepared.rule.match.patterns),
                sources=len(prepared.rule.sources) or "全部",
                targets=",".join(str(t) for t in prepared.rule.targets),
                notify=prepared.rule.notify,
                delay_s=prepared.rule.delay,
            )

    async def close(self) -> None:
        """注销 handler 并等待在途任务结束。"""
        for handler, group in self._handlers:
            with contextlib.suppress(Exception):
                self.client.remove_handler(handler, group)
        self._handlers.clear()

        for buffer in list(self._media_groups.values()):
            if buffer.task is not None:
                buffer.task.cancel()
        self._media_groups.clear()

        if self._tasks:
            self.alog.info("等待在途转发任务结束", pending=len(self._tasks))
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        self.alog.info("转发引擎已停止", **self.stats)

    # ------------------------------------------------------------------ #
    # handler：只做内存判断，命中后立刻派发
    # ------------------------------------------------------------------ #
    async def _on_message(self, client: Client, message: Any) -> None:
        del client
        self._handle(message, edited=False)

    async def _on_edited(self, client: Client, message: Any) -> None:
        del client
        self._handle(message, edited=True)

    def _handle(self, message: Any, *, edited: bool) -> None:
        started = time.perf_counter()
        # 先看磁盘上的规则有没有变（有节流，不是每条都 stat）。放在最前面，
        # 保证这一条消息就已经用上新规则。
        self._maybe_reload()
        self.stats["received"] += 1
        chat_id, chat_username, chat_title = chat_identity(message)
        message_id = getattr(message, "id", None)
        if chat_id is None or message_id is None:
            return

        is_service = bool(getattr(message, "service", None))
        kind = chat_kind(message)
        sender_id, sender_username, is_self, _is_bot = sender_of(message)
        now = time.monotonic()

        # 账号级全局排除：命中就直接丢弃，连规则都不看。
        # 放在规则循环**外面**是刻意的 —— 它是"所有规则都不监听"的语义，
        # 每条规则各判一次既浪费又容易漏（漏一条就等于没排除）。
        if self._exclude_chats and self._exclude_chats.matches(chat_id, chat_username):
            self.stats["excluded"] += 1
            self.alog.debug(
                "跳过消息",
                reason="会话在 forward.exclude_chats 中",
                chat=chat_title or chat_id,
                message_id=message_id,
            )
            return

        for prepared in self.rules:
            rule = prepared.rule
            if edited and not rule.include_edited:
                continue
            if is_service and not rule.include_service:
                continue

            allowed, reason = prepared.chat_allowed(chat_id, chat_username, kind)
            if not allowed:
                continue
            allowed, reason = prepared.sender_allowed(sender_id, sender_username, is_self)
            if not allowed:
                prepared.stats["skipped"] += 1
                self.alog.debug(
                    "跳过消息", rule=prepared.label, reason=reason, chat=chat_title, message_id=message_id
                )
                continue

            result = prepared.matcher.match(message)
            if not result:
                # 安全取预览：消息里可能含非法代理对，直接 str() 会抛 UnicodeEncodeError，
                # 而这里只是打日志，不该因为一条脏消息把整条转发链路带崩。
                try:
                    preview = truncate(str(message_text(message)), 60, "…")
                except Exception:
                    preview = "[无法解析的消息]"

                self.alog.debug(
                    "未命中规则",
                    rule=prepared.label,
                    reason=result.reason,
                    chat=chat_title,
                    message_id=message_id,
                    preview=preview,
                )
                continue

            allowed, reason = prepared.interval_ok(now)
            if not allowed:
                prepared.stats["skipped"] += 1
                self.alog.info("命中但被最小间隔限制", rule=prepared.label, reason=reason)
                continue

            # 账号内去重键必须带上规则 id：同一条消息可以合法地命中多条规则、
            # 转发到不同频道；只用 (chat_id, message_id) 会让第二条规则被误判为重复。
            # ⚠️ **跨账号去重不在这里** —— 它的键要带目标，而一条规则可以有多个目标，
            #    放在这里会「一条规则的多个目标共用一个名额」。见 ``_forward_one``。
            dedupe_key = (prepared.id, chat_id, message_id)
            if not self.dedupe.check_and_add(dedupe_key):
                self.stats["deduped"] += 1
                self.alog.debug(
                    "重复消息已忽略", rule=prepared.label, chat_id=chat_id, message_id=message_id
                )
                continue

            prepared.last_fired = now
            prepared.stats["matched"] += 1
            self.stats["matched"] += 1
            match_ms = (time.perf_counter() - started) * 1000
            try:
                preview = truncate(str(message_text(message)), 80, "…")
            except Exception:
                preview = "[无法解析的消息]"

            self.alog.info(
                "命中转发规则",
                rule=prepared.label,
                chat=chat_title or chat_id,
                message_id=message_id,
                keyword=result.keyword,
                groups=",".join(result.groups) if result.groups else "-",
                match_ms=round(match_ms, 3),
                edited=edited,
                preview=preview,
            )

            group_id = getattr(message, "media_group_id", None)
            if group_id and prepared.rule.media_group:
                self._buffer_media_group(prepared, message, result, str(group_id), chat_id, message_id)
            else:
                self._spawn(self._forward_one(prepared, message, result, started))
            # 一条消息可以命中多条规则，继续循环（不 break）

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------ #
    # 相册聚合
    # ------------------------------------------------------------------ #
    def _buffer_media_group(
        self,
        prepared: PreparedRule,
        message: Any,
        result: MatchResult,
        group_id: str,
        chat_id: int,
        message_id: int,
    ) -> None:
        key = (chat_id, f"{prepared.id}:{group_id}")
        buffer = self._media_groups.get(key)
        if buffer is None:
            buffer = MediaGroupBuffer(
                chat_id=chat_id,
                group_id=group_id,
                rule_id=prepared.id,
                message_ids=[message_id],
                first_message=message,
                match=result,
            )
            self._media_groups[key] = buffer
            buffer.task = asyncio.create_task(self._flush_media_group(prepared, key))
            self._tasks.add(buffer.task)
            buffer.task.add_done_callback(self._tasks.discard)
            self.alog.debug("开始聚合相册", rule=prepared.label, media_group_id=group_id)
        else:
            if message_id not in buffer.message_ids:
                buffer.message_ids.append(message_id)
            self.alog.debug(
                "相册新增一条",
                rule=prepared.label,
                media_group_id=group_id,
                count=len(buffer.message_ids),
            )

    async def _flush_media_group(self, prepared: PreparedRule, key: tuple[int, str]) -> None:
        window = prepared.rule.media_group_window
        # 等待窗口内的后续分片；相册的各条消息几乎同时到达，窗口 1.2s 足够
        await asyncio.sleep(window)
        buffer = self._media_groups.pop(key, None)
        if buffer is None:
            return
        buffer.message_ids.sort()
        self.alog.info(
            "相册聚合完成，开始转发",
            rule=prepared.label,
            media_group_id=buffer.group_id,
            count=len(buffer.message_ids),
            window_s=window,
        )
        await self._forward_one(
            prepared,
            buffer.first_message,
            buffer.match,
            time.perf_counter(),
            message_ids=buffer.message_ids,
        )

    # ------------------------------------------------------------------ #
    # 实际发送
    # ------------------------------------------------------------------ #
    async def _forward_one(
        self,
        prepared: PreparedRule,
        message: Any,
        result: MatchResult,
        started: float,
        message_ids: Optional[Sequence[int]] = None,
    ) -> None:
        rule = prepared.rule
        chat_id, _, chat_title = chat_identity(message)
        ids = list(message_ids) if message_ids else [message.id]

        if rule.delay > 0:
            await asyncio.sleep(rule.delay)

        variables = apply_groups(build_variables(message), result)
        variables["rule"] = rule.label
        variables["rule_id"] = rule.id

        delivered: list[tuple[ChatRef, int]] = []
        #: 跨账号去重窗口。与账号内去重同源（账号级 ``dedupe_window``）。
        dedupe_ttl = float(self.config.forward.dedupe_window)
        for target in rule.targets:
            # 跨账号去重：同一条源消息发往**同一个目标**，只允许一个账号发出去。
            # 键里带目标 ⇒ 必须放在 target 循环里（放 ``_handle`` 会让多个目标共用名额）。
            # ``claim`` 是同步的、无 await，在事件循环里天然原子 —— 两个账号的 handler
            # 不可能同时抢到同一个名额。不传共享表时整段跳过（单测 / 离线场景）。
            if self._shared_dedupe is not None and not self._shared_dedupe.claim(
                chat_id, ids[0], target, dedupe_ttl
            ):
                self.stats["cross_deduped"] += 1
                self.alog.debug(
                    "跨账号去重：该消息已由其它账号发往同一目标",
                    rule=rule.label,
                    source_chat=chat_title or chat_id,
                    message_id=ids[0],
                    target=target,
                )
                continue
            try:
                sent_ids, degraded = await self._send_to_target(
                    prepared, message, ids, target, variables
                )
            except SessionInvalid:
                raise
            except Exception as exc:
                prepared.stats["failed"] += 1
                self.stats["failed"] += 1
                self.alog.error(
                    "转发失败",
                    rule=rule.label,
                    target=target,
                    source_chat=chat_title or chat_id,
                    message_ids=",".join(map(str, ids)),
                    error=f"{type(exc).__name__}: {exc}",
                    hint=_forward_hint(exc),
                )
                continue

            prepared.stats["sent"] += 1
            self.stats["forwarded"] += 1
            for sent_id in sent_ids:
                delivered.append((target, sent_id))

            pipeline_ms = _pipeline_ms(message)
            self.alog.info(
                "转发成功",
                rule=rule.label,
                # 源会话受保护时 forward 会失败并自动降级成复制，这里显示**实际生效**的模式。
                mode="copy(降级)" if degraded else rule.mode,
                source_chat=chat_title or chat_id,
                target=target,
                message_ids=",".join(map(str, ids)),
                sent_ids=",".join(map(str, sent_ids)) or "-",
                handler_ms=round((time.perf_counter() - started) * 1000, 1),
                pipeline_ms=round(pipeline_ms, 1) if pipeline_ms is not None else "-",
            )

        if rule.notify and self.notifier is not None and delivered:
            self._submit_notify(prepared, variables, delivered)

    async def _send_to_target(
        self,
        prepared: PreparedRule,
        message: Any,
        ids: list[int],
        target: ChatRef,
        variables: dict[str, Any],
    ) -> tuple[list[int], bool]:
        """把消息发到 ``target``。

        返回 ``(已发送的消息 id, 是否发生了 forward→copy 降级)``。

        ``mode="forward"`` 遇到受保护源会话（Telegram 回 400 ``CHAT_FORWARDS_RESTRICTED``）
        时**自动改用复制**再发一次 —— 这就是「copy 做备选」。
        """
        rule = prepared.rule
        chat_id, _, chat_title = chat_identity(message)
        kwargs: dict[str, Any] = {}
        if rule.target_thread_id is not None:
            kwargs["message_thread_id"] = rule.target_thread_id
        if rule.silent:
            kwargs["disable_notification"] = True

        #: 原文链接（``build_variables`` 给的 t.me 永久链接）。
        link = str(variables.get("link") or "")
        #: 要不要附带原文链接。两种模式的**加法不同**：
        #: ``copy`` 写进**这条消息**的正文末尾（它是重发出来的新消息，正文可写）；
        #: ``forward`` 在**下方**单独补一条 —— 转发消息本身不可编辑，也不该动。
        want_link = bool(rule.include_source_link and link)

        def _with_link(base: str) -> str:
            """把「🔗原文链接：…」接到正文 / caption 末尾（前面空一行）。"""
            if not want_link or link in base:
                return base
            return f"{base}\n\n{SOURCE_LINK_PREFIX}{link}" if base else f"{SOURCE_LINK_PREFIX}{link}"

        async def _server_forward(hide_sender: bool) -> list[int]:
            """服务端转发。``hide_sender=True``（= raw ``drop_author``）去掉「转发自」抬头。

            单次 RPC 完成：天然保留相册分组与媒体，不需要下载再上传。
            """
            sent = await self.client.forward_messages(
                chat_id=target,
                from_chat_id=chat_id,
                message_ids=ids if len(ids) > 1 else ids[0],
                hide_sender_name=True if hide_sender else None,
                **kwargs,
            )
            return _sent_ids(sent)

        async def _server_copy() -> list[int]:
            """**真正的复制**：按 ``file_id`` 重新发送一条**新消息**。

            ⚠️ 不能拿 ``forward_messages(hide_sender_name=True)`` 当复制用。那样发出来的是
            **转发消息**，而 Telegram **拒绝编辑转发消息**（2026-09-20 实测
            ``400 Bad Request: message can't be edited``）⇒ 事后没法把原文链接追加进去。
            「重新发送」拿到的是全新消息，链接在**发送时**就写进正文 / caption ——
            一次 RPC 搞定，零编辑。

            - 文本：``send_message``（带上原 entities，保留粗体 / 链接等格式）
            - 媒体：``Message.copy``（内部 ``send_cached_media``，服务端按 file_id 复用，
              不下载不上传）
            - 相册：``copy_media_group``（服务端 ``SendMultiMedia``）。``captions`` 传 list 时
              **按下标取值、越界回落原 caption** ⇒ 只覆盖第一项就能给相册首图加链接
            """
            if len(ids) > 1:
                captions = None
                if want_link:
                    captions = [
                        truncate(
                            _with_link(getattr(message, "caption", None) or ""),
                            CAPTION_LIMIT,
                        )
                    ]
                sent = await self.client.copy_media_group(
                    chat_id=target,
                    from_chat_id=chat_id,
                    message_id=ids[0],
                    captions=captions,
                    **kwargs,
                )
                return _sent_ids(sent)

            text = getattr(message, "text", None)
            if text is not None:
                body = _with_link(text)
                trimmed = truncate(body, MAX_TEXT_LENGTH)
                if not trimmed.strip():
                    trimmed = "（原消息无文本内容）"
                sent = await self.client.send_message(
                    chat_id=target,
                    text=trimmed,
                    # 截断可能切断实体边界 ⇒ Telegram 回 ENTITY_BOUNDS_INVALID，
                    # 这种情况就丢掉 entities（宁可少点格式，也不能发不出去）
                    entities=getattr(message, "entities", None) if trimmed == body else None,
                    parse_mode=ParseMode.DISABLED,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    **kwargs,
                )
                return _sent_ids(sent)

            sent = await message.copy(
                chat_id=target,
                caption=truncate(
                    _with_link(getattr(message, "caption", None) or ""), CAPTION_LIMIT
                ),
                **kwargs,
            )
            return _sent_ids(sent)

        async def _forward_link_note() -> list[int]:
            """``forward`` 模式：**不动**那条转发消息，在它**下方**补发一条独立消息。

            为什么不能把链接写进转发消息本身：Telegram **拒绝编辑转发消息**
            （实测 ``400 Bad Request: message can't be edited``）。所以 forward 模式
            只能单独发一条 —— 这也正是小白要的格式：
            「转发的消息不用编辑，直接在下方加一条原文链接」。

            刻意**不抛出**：内容已经发出去了，链接没补上不该让整条转发记成失败。
            """
            try:
                sent = await with_flood_retry(
                    lambda: self.client.send_message(
                        chat_id=target,
                        text=f"{SOURCE_LINK_PREFIX}{link}",
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                        **kwargs,
                    ),
                    alog=self.alog,
                    action=f"补发原文链接到 {target}",
                    retries=2,
                    max_flood_wait=60.0,
                )
            except SessionInvalid:
                raise
            except Exception as exc:  # noqa: BLE001
                self.alog.warning(
                    "补发原文链接失败",
                    rule=rule.label,
                    target=target,
                    error=f"{type(exc).__name__}: {exc}",
                )
                return []
            return _sent_ids(sent)

        async def _copy_with_fallback() -> list[int]:
            """复制；复制失败就退化成 ``drop_author`` 转发（内容能到，但**丢原文链接**）。"""
            try:
                return await with_flood_retry(
                    _server_copy,
                    alog=self.alog,
                    action=f"复制到 {target}",
                    retries=2,
                    max_flood_wait=60.0,
                )
            except SessionInvalid:
                raise
            except Exception as exc:  # noqa: BLE001
                self.alog.warning(
                    "复制失败，退化为不带抬头的转发（原文链接会丢失）",
                    rule=rule.label,
                    target=target,
                    source_chat=chat_title or chat_id,
                    message_ids=",".join(map(str, ids)),
                    error=f"{type(exc).__name__}: {exc}",
                )
                return await with_flood_retry(
                    lambda: _server_forward(True),
                    alog=self.alog,
                    action=f"转发到 {target}",
                    retries=2,
                    max_flood_wait=60.0,
                )

        try:
            if rule.mode == "forward":
                sent = await with_flood_retry(
                    lambda: _server_forward(False),
                    alog=self.alog,
                    action=f"转发到 {target}",
                    retries=2,
                    max_flood_wait=60.0,
                )
                # 转发消息本身**不编辑**（Telegram 也拒绝编辑转发消息）——
                # 链接在它下方单独补一条。
                if want_link:
                    sent = [*sent, *await _forward_link_note()]
                return sent, False

            if rule.mode == "copy":
                return await _copy_with_fallback(), False

            # text 模式：按模板重发纯文本
            text = render_template(rule.template or "{text}", variables)
            if want_link and link not in text:
                text = f"{text}\n\n{SOURCE_LINK_PREFIX}{link}"
            text = truncate(text)
            if not text.strip():
                text = "（原消息无文本内容）"
            sent = await with_flood_retry(
                lambda: self.client.send_message(
                    chat_id=target,
                    text=text,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    **kwargs,
                ),
                alog=self.alog,
                action=f"发送到 {target}",
                retries=2,
                max_flood_wait=60.0,
            )
            return _sent_ids(sent), False
        except SessionInvalid:
            raise
        except Exception as exc:
            # 源会话禁止转发（受保护群/频道）⇒ 自动降级为复制。
            # Telegram 对受保护内容回 400 CHAT_FORWARDS_RESTRICTED，而「复制」是允许的
            # —— 这是唯一能靠换模式绕过去的错误，所以单独识别。
            # 代价：该源会话的每条消息都要先撞一次失败。受保护与否是**源会话的属性**，
            # 按 (规则, 源会话) 记忆能省掉这次 RPC，但源会话可能取消保护，
            # 所以这里选择每次都真试一遍 —— 正确性优先。
            if rule.mode != "forward" or not _is_forwards_restricted(exc):
                raise
            self.alog.warning(
                "源会话禁止转发，自动降级为复制",
                rule=rule.label,
                target=target,
                source_chat=chat_title or chat_id,
                message_ids=",".join(map(str, ids)),
                error=f"{type(exc).__name__}: {exc}",
            )
            sent = await _copy_with_fallback()
            self.stats["downgraded"] += 1
            return sent, True

    def _submit_notify(
        self,
        prepared: PreparedRule,
        variables: dict[str, Any],
        delivered: list[tuple[ChatRef, int]],
    ) -> None:
        assert self.notifier is not None
        config = self.notifier.config
        text = build_notify_text(config, variables)
        task = NotifyTask(
            event="forward",
            text=text,
            copy_from=delivered[0],
            copy_from_ids=delivered if len(delivered) > 1 else [],
            silent=config.silent,
            context={
                "rule": prepared.label,
                "source_chat": variables.get("chat_title"),
            },
        )
        if self.notifier.submit(task):
            self.alog.debug("已提交 bot 通知", rule=prepared.label, copy_from=str(delivered[0]))

    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict[str, Any]:
        """运行状态快照，供 ``status`` 命令与周期性日志使用。"""
        data: dict[str, Any] = {
            **self.stats,
            "dedupe_size": len(self.dedupe),
            "pending_tasks": len(self._tasks),
            "media_group_buffers": len(self._media_groups),
            "rules": {prepared.id: dict(prepared.stats) for prepared in self.rules},
        }
        if self._shared_dedupe is not None:
            # ⚠️ 这是**跨账号共享表**的数字（所有账号合计），不是本账号的。
            data["cross_dedupe"] = self._shared_dedupe.snapshot()
        return data


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
#: 「原文链接」那一行的前缀。``copy`` 模式发出来的是**新消息**（不带「转发自」抬头），
#: Telegram 不会附带任何回溯入口，所以只能自己把链接写进正文。
SOURCE_LINK_PREFIX = "🔗原文链接："

#: 媒体 caption 的硬上限（Telegram 限制），比正文的 4096 短得多。
CAPTION_LIMIT = 1024


def _sent_ids(sent: Any) -> list[int]:
    if sent is None:
        return []
    if isinstance(sent, (list, tuple)):
        return [item.id for item in sent if getattr(item, "id", None) is not None]
    message_id = getattr(sent, "id", None)
    return [message_id] if message_id is not None else []


def _pipeline_ms(message: Any) -> Optional[float]:
    """消息在 Telegram 服务端的时间戳到现在的耗时（含网络传输）。"""
    import datetime as _dt

    date = getattr(message, "date", None)
    if not isinstance(date, _dt.datetime):
        return None
    if date.tzinfo is None:
        date = date.replace(tzinfo=_dt.timezone.utc)
    return (_dt.datetime.now(_dt.timezone.utc) - date).total_seconds() * 1000


def _is_forwards_restricted(exc: Exception) -> bool:
    """是不是「源会话禁止转发」（受保护群 / 频道）。

    这是**唯一**能靠「改用复制」绕过去的错误：它说明**源**会话受保护，而复制是允许的，
    所以 ``mode="forward"`` 的规则遇到它要自动降级。

    以异常类型为主、错误文本兜底 —— pyrogram 各版本类名一致，但代理层或包装层
    可能把它换成别的类型，而文本里一定带 ``CHAT_FORWARDS_RESTRICTED``。
    """
    if isinstance(exc, ChatForwardsRestricted):
        return True
    return "forwards_restricted" in f"{type(exc).__name__}: {exc}".lower()


def _forward_hint(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    if "peer_id_invalid" in text or "peer id invalid" in text:
        return "目标会话未在本账号的 peer 缓存中：先让该账号加入/打开一次目标频道，或改用数字 chat_id"
    if "chat_write_forbidden" in text:
        return "本账号在目标会话没有发言权限"
    if "chat_admin_required" in text:
        return "需要管理员权限才能发送"
    if "channel_private" in text:
        return "目标频道私有且本账号未加入"
    if "forwards_restricted" in text or ("forbidden" in text and "forward" in text):
        return (
            "源会话是受保护内容，Telegram 不允许转发；已自动改用复制仍失败 ⇒ "
            "把规则改成 mode=\"copy\" 再试，或换一个源会话"
        )
    if "media_empty" in text:
        return "媒体已失效，可能源消息被删除"
    if "slowmode" in text:
        return "目标会话开启了慢速模式，适当增大 min_interval"
    return "查看上面的错误类型定位原因"


def random_jitter(base: float, jitter: float) -> float:
    if jitter <= 0:
        return base
    return base + random.uniform(0, jitter)


__all__ = [
    "CAPTION_LIMIT",
    "SOURCE_LINK_PREFIX",
    "CrossAccountDedupe",
    "DedupeCache",
    "ForwardEngine",
    "MediaGroupBuffer",
    "PreparedRule",
    "random_jitter",
]
