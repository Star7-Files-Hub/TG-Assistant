"""秒级正则转发引擎。

延迟从哪里来、怎么压下去：

1. **不在 handler 里做慢活**：pyrogram 的 handler worker 是串行执行同一 group 的 handler，
   在里面 ``await`` 网络请求会阻塞后续消息。这里 handler 只做纯内存匹配（微秒级），
   命中后立刻 ``create_task`` 派发，真正的发送在独立任务里跑。
2. **零 ``get_chat``**：所有会话/用户判断都用预构建的集合，不发网络请求。
3. **正则预编译**：配置加载时编译一次。
4. **相册聚合**：同一 ``media_group_id`` 的多条消息在 ``media_group_window`` 内攒齐后
   一次性 ``forward_messages(message_ids=[...])``，避免拆成多条丢失排版。
5. **去重**：``(chat_id, message_id)`` + TTL，防止编辑事件或重复更新造成二次转发。

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
from pyrogram.handlers import EditedMessageHandler, MessageHandler
from pyrogram.types import LinkPreviewOptions

from .client import SessionInvalid, with_flood_retry
from .config import AccountConfig, ChatRef, ForwardRule
from .logging_setup import AccountLogger
from .matching import (
    CompiledMatcher,
    MatchResult,
    RefSet,
    apply_groups,
    build_variables,
    chat_identity,
    chat_kind,
    message_text,
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
        """``kind`` 是 :func:`~tg_assistant.matching.chat_kind` 的返回值。

        ``sources`` 为空表示"监听全部"，但**只限群组与频道**：
        私聊一律不参与转发。否则任何陌生人给账号发一条含关键词的私信，
        都会被原样转发到目标频道里去。

        这里同时接受 ``"bot"``（pyrogram 的 ``ChatType.BOT``，即与机器人的一对一
        会话）：调用方若直接把 ``message.chat.type.value`` 传进来也不会漏。
        """
        if self.exclude_sources and self.exclude_sources.matches(chat_id, chat_username):
            return False, "来源在 exclude_sources 中"
        if self.sources:
            if not self.sources.matches(chat_id, chat_username):
                return False, "来源不在 sources 中"
            return True, ""
        if kind in ("private", "bot"):
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

    def __init__(
        self,
        client: Client,
        config: AccountConfig,
        alog: AccountLogger,
        notifier: Optional[BotNotifier] = None,
    ) -> None:
        self.client = client
        self.config = config
        self.alog = alog.bind("forward")
        self.notifier = notifier
        self.rules: list[PreparedRule] = [
            PreparedRule.build(rule) for rule in config.forward.active_rules
        ]
        self.dedupe = DedupeCache(config.forward.dedupe_window)
        self._handlers: list[tuple[Any, int]] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._media_groups: dict[tuple[int, str], MediaGroupBuffer] = {}
        self._lock = asyncio.Lock()
        self.stats = {"received": 0, "matched": 0, "forwarded": 0, "failed": 0, "deduped": 0}

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
        self.stats["received"] += 1
        chat_id, chat_username, chat_title = chat_identity(message)
        message_id = getattr(message, "id", None)
        if chat_id is None or message_id is None:
            return

        is_service = bool(getattr(message, "service", None))
        kind = chat_kind(message)
        sender_id, sender_username, is_self, _is_bot = sender_of(message)
        now = time.monotonic()

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
                self.alog.debug(
                    "未命中规则",
                    rule=prepared.label,
                    reason=result.reason,
                    chat=chat_title,
                    message_id=message_id,
                    preview=truncate(message_text(message), 60, "…"),
                )
                continue

            allowed, reason = prepared.interval_ok(now)
            if not allowed:
                prepared.stats["skipped"] += 1
                self.alog.info("命中但被最小间隔限制", rule=prepared.label, reason=reason)
                continue

            # 去重键必须带上规则 id：同一条消息可以合法地命中多条规则、
            # 转发到不同频道；只用 (chat_id, message_id) 会让第二条规则被误判为重复。
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
            self.alog.info(
                "命中转发规则",
                rule=prepared.label,
                chat=chat_title or chat_id,
                message_id=message_id,
                keyword=result.keyword,
                groups=",".join(result.groups) if result.groups else "-",
                match_ms=round(match_ms, 3),
                edited=edited,
                preview=truncate(message_text(message), 80, "…"),
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
        for target in rule.targets:
            try:
                sent_ids = await self._send_to_target(prepared, message, ids, target, variables)
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
                mode=rule.mode,
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
    ) -> list[int]:
        rule = prepared.rule
        chat_id, _, _ = chat_identity(message)
        kwargs: dict[str, Any] = {}
        if rule.target_thread_id is not None:
            kwargs["message_thread_id"] = rule.target_thread_id
        if rule.silent:
            kwargs["disable_notification"] = True

        async def _do() -> list[int]:
            if rule.mode in {"forward", "copy"}:
                # 单次 RPC 完成：服务端转发，天然保留相册分组与媒体，不需要下载再上传。
                # copy 模式用 hide_sender_name(=raw drop_author) 去掉「转发自」抬头。
                sent = await self.client.forward_messages(
                    chat_id=target,
                    from_chat_id=chat_id,
                    message_ids=ids if len(ids) > 1 else ids[0],
                    hide_sender_name=True if rule.mode == "copy" else None,
                    **kwargs,
                )
                return _sent_ids(sent)

            # text 模式：按模板重发纯文本
            text = render_template(rule.template or "{text}", variables)
            if rule.include_source_link and variables.get("link"):
                link = str(variables["link"])
                if link not in text:
                    text = f"{text}\n\n🔗 {link}"
            text = truncate(text)
            if not text.strip():
                text = "（原消息无文本内容）"
            sent = await self.client.send_message(
                chat_id=target,
                text=text,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                **kwargs,
            )
            return _sent_ids(sent)

        try:
            return await with_flood_retry(
                _do,
                alog=self.alog,
                action=f"转发到 {target}",
                retries=2,
                max_flood_wait=60.0,
            )
        except SessionInvalid:
            raise
        except Exception as exc:
            if rule.mode != "copy" or len(ids) > 1:
                raise
            # 少数会话不允许 drop_author，退化为客户端复制（会多一次上传，但能发出去）
            self.alog.warning(
                "服务端复制失败，尝试客户端复制回退",
                target=target,
                error=f"{type(exc).__name__}: {exc}",
            )
            sent = await with_flood_retry(
                lambda: message.copy(chat_id=target, **kwargs),
                alog=self.alog,
                action=f"客户端复制到 {target}",
                retries=1,
                max_flood_wait=30.0,
            )
            return _sent_ids(sent)

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
        return {
            **self.stats,
            "dedupe_size": len(self.dedupe),
            "pending_tasks": len(self._tasks),
            "media_group_buffers": len(self._media_groups),
            "rules": {prepared.id: dict(prepared.stats) for prepared in self.rules},
        }


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
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
    if "forbidden" in text and "forward" in text:
        return "源频道禁止转发内容（受保护内容），请把规则改成 mode=\"text\""
    if "media_empty" in text:
        return "媒体已失效，可能源消息被删除"
    if "slowmode" in text:
        return "目标会话开启了慢速模式，适当增大 min_interval"
    return "查看上面的错误类型定位原因"


def random_jitter(base: float, jitter: float) -> float:
    if jitter <= 0:
        return base
    return base + random.uniform(0, jitter)


__all__ = ["DedupeCache", "ForwardEngine", "MediaGroupBuffer", "PreparedRule", "random_jitter"]
