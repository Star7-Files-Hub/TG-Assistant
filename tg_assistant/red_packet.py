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
import random
import re
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
from .config import AccountConfig, RedPacketConfig
from .logging_setup import AccountLogger
from .matching import (
    DEFAULT_RED_PACKET_TEMPLATE,
    RefSet,
    build_variables,
    chat_identity,
    compile_patterns,
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
class RedPacketHunter:
    """抢红包引擎。"""

    #: 与转发引擎分开的 handler group，互不阻塞。
    HANDLER_GROUP = 1

    def __init__(
        self,
        client: Client,
        config: AccountConfig,
        alog: AccountLogger,
        notifier: Optional[BotNotifier] = None,
    ) -> None:
        self.client = client
        self.config: RedPacketConfig = config.red_packet
        self.notify_config = config.notify
        self.alog = alog.bind("redpacket")
        self.notifier = notifier

        self._chats = RefSet(self.config.chats)
        self._exclude_chats = RefSet(self.config.exclude_chats)
        self._button_keywords = [k.lower() for k in self.config.detect.button_keywords]
        self._text_patterns = compile_patterns(self.config.detect.text_patterns)
        self._code_pattern = (
            re.compile(self.config.detect.code_pattern) if self.config.detect.code_pattern else None
        )
        self._success_patterns = compile_patterns(self.config.success.success_patterns)
        self._failure_patterns = compile_patterns(self.config.success.failure_patterns)

        self._bus = ChatEventBus()
        self._handlers: list[tuple[Any, int]] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._semaphore = asyncio.Semaphore(self.config.max_concurrency)
        self._seen: dict[tuple[int, int], float] = {}
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
        }

    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def watched_chats(self) -> list[Any]:
        return list(self.config.chats)

    async def register(self) -> None:
        if not self.enabled:
            self.alog.info("抢红包功能未启用")
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

        self.alog.info(
            "抢红包引擎已注册",
            strategy=self.config.strategy,
            watched_chats=len(chats) or "全部",
            button_keywords=",".join(self.config.detect.button_keywords),
            text_patterns=len(self._text_patterns),
            keyword_template=self.config.detect.keyword_template or "-",
            delay_s=self.config.delay,
            jitter_s=self.config.jitter,
            max_attempts=self.config.max_attempts,
            reply_enabled=self.config.reply.enabled,
            reply_count=len(self.config.reply.texts),
            wait_timeout_s=self.config.success.wait_timeout,
        )

    async def close(self) -> None:
        for handler, group in self._handlers:
            with contextlib.suppress(Exception):
                self.client.remove_handler(handler, group)
        self._handlers.clear()
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

    def _dispatch(self, message: Any, *, edited: bool) -> None:
        chat_id, _, _ = chat_identity(message)
        if chat_id is None:
            return
        # 先喂给等待判定的任务（哪怕这条消息本身不是红包）
        self._bus.feed(chat_id, message)

        started = time.perf_counter()
        decision = self._should_grab(message, chat_id)
        if decision is None:
            return

        button, code = decision
        message_id = getattr(message, "id", None)
        key = (chat_id, message_id or 0)
        now = time.monotonic()
        last = self._seen.get(key)
        if last is not None and now - last < 60.0:
            self.alog.debug("红包消息已处理过，跳过", chat_id=chat_id, message_id=message_id)
            return
        self._seen[key] = now
        if len(self._seen) > 4096:
            cutoff = now - 300
            self._seen = {k: v for k, v in self._seen.items() if v > cutoff}

        self.stats["detected"] += 1
        _, _, chat_title = chat_identity(message)
        self.alog.info(
            "发现红包",
            chat=chat_title or chat_id,
            message_id=message_id,
            button=button.get("text") if button else "-",
            code=code or "-",
            edited=edited,
            detect_ms=round((time.perf_counter() - started) * 1000, 3),
            preview=truncate(message_text(message), 80, "…"),
        )

        task = asyncio.create_task(self._grab(message, button, code))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------ #
    def _should_grab(
        self, message: Any, chat_id: int
    ) -> Optional[tuple[Optional[dict[str, Any]], Optional[str]]]:
        """判断是否要抢；返回 ``(按钮信息, 提取到的口令)``，不抢则返回 None。"""
        config = self.config
        _, chat_username, _ = chat_identity(message)
        if self._exclude_chats and self._exclude_chats.matches(chat_id, chat_username):
            return None
        if self._chats and not self._chats.matches(chat_id, chat_username):
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
        if self._text_patterns:
            text_hit = first_match(self._text_patterns, text) is not None

        button = self._find_button(message)

        if config.strategy == "button":
            if button is None:
                return None
            if self._text_patterns and not text_hit:
                return None
            return button, self._extract_code(text)

        if config.strategy == "keyword":
            if not text_hit:
                return None
            code = self._extract_code(text)
            if self._code_pattern is not None and code is None:
                self.alog.debug("正文未提取到口令，跳过", chat_id=chat_id)
                return None
            return None, code

        # auto：优先按钮
        if button is not None and (text_hit or not self._text_patterns):
            return button, self._extract_code(text)
        if config.detect.keyword_template and text_hit and (self._text_patterns or self._code_pattern):
            code = self._extract_code(text)
            if self._code_pattern is not None and code is None:
                return None
            return None, code
        del sender_id
        return None

    def _find_button(self, message: Any) -> Optional[dict[str, Any]]:
        """在内联/回复键盘里找第一个像"领红包"的按钮。"""
        markup = getattr(message, "reply_markup", None)
        if markup is None:
            return None

        inline_rows = getattr(markup, "inline_keyboard", None) or []
        for row_index, row in enumerate(inline_rows):
            for col_index, button in enumerate(row):
                text = str(getattr(button, "text", "") or "")
                if not self._keyword_hit(text):
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
                if text and self._keyword_hit(text):
                    return {"text": text, "callback_data": None, "position": "-", "kind": "reply"}
        return None

    def _keyword_hit(self, text: str) -> bool:
        lowered = text.lower()
        return any(keyword in lowered for keyword in self._button_keywords)

    def _extract_code(self, text: str) -> Optional[str]:
        if self._code_pattern is None:
            return None
        found = self._code_pattern.search(text)
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

        delay = self.config.delay + (random.uniform(0, self.config.jitter) if self.config.jitter else 0.0)
        if delay > 0:
            self.alog.debug("按配置延迟后再抢", delay_s=round(delay, 3), chat_id=chat_id)
            await asyncio.sleep(delay)

        outcome = await self._execute(message, button, code, strategy, started)

        self._record(outcome)
        if self._should_reply(outcome.result):
            replied = await self._maybe_reply(message, outcome)
            if replied:
                outcome.replied = replied

        self._log_outcome(outcome, chat_title, message_id)
        if self.config.notify and self.notifier is not None:
            self._submit_notify(message, outcome)

    def _should_reply(self, result: GrabResult) -> bool:
        """是否该发随机回复。

        - 确认抢到 → 回复；
        - ``only_on_success=False`` 时，结果未知也回复（部分 bot 不回显结果）；
        - 明确没抢到 / 出错 / 跳过 → 不回复（避免对着空气道谢）。
        """
        if result is GrabResult.SUCCESS:
            return True
        if result is GrabResult.UNKNOWN and not self.config.reply.only_on_success:
            return True
        return False

    async def _execute(
        self,
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
        }

        last_error: Optional[str] = None
        for attempt in range(1, self.config.max_attempts + 1):
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
                                    chat=chat_title or chat_id,
                                    message_id=message_id,
                                    attempt=attempt,
                                    hint="部分红包 bot 不返回 callback answer，将依据后续消息判定",
                                )
                        elif strategy == "keyboard-text" and button is not None:
                            await self._send_text(chat_id, str(button.get("text")), message)
                        else:
                            text = self._render_keyword(code, message)
                            if not text:
                                return GrabOutcome(
                                    result=GrabResult.SKIPPED,
                                    detail="keyword 策略缺少 keyword_template，无法发送",
                                    cost_ms=(time.perf_counter() - started) * 1000,
                                    attempts=attempt,
                                    **base,
                                )
                            await self._send_text(chat_id, text, message)

                    # 判定必须在 watch 上下文内完成：一旦退出这个 with，
                    # queue 就会从 bus 注销，_judge 再也收不到后续消息，
                    # 会直接返回 UNKNOWN。
                    verdict, evidence = await self._judge(callback_text, queue)

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
                if attempt >= self.config.max_attempts:
                    self.alog.error(
                        "抢红包失败",
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
            attempts=self.config.max_attempts,
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

    async def _send_text(self, chat_id: int, text: str, message: Any) -> None:
        reply_to = getattr(message, "id", None) if self.config.reply.reply_to_message else None

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
        self.alog.info("已发送抢红包关键词", text=text, chat_id=chat_id)

    def _render_keyword(self, code: Optional[str], message: Any) -> Optional[str]:
        template = self.config.detect.keyword_template
        if not template:
            return None
        variables = build_variables(message, {"code": code or ""})
        return render_template(template, variables).strip()

    # ------------------------------------------------------------------ #
    async def _judge(
        self,
        callback_text: Optional[str],
        queue: Optional[asyncio.Queue[Any]],
    ) -> tuple[GrabResult, Optional[str]]:
        """判定结果。返回 ``(结论, 证据文本)``。"""
        if callback_text:
            verdict = self._classify(callback_text)
            if verdict is not None:
                return verdict, f"callback: {truncate(callback_text, 200, '…')}"

        timeout = self.config.success.wait_timeout
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
            if self.config.success.require_self_mention and not self._mentions_me(message, text):
                continue
            verdict = self._classify(text)
            if verdict is not None:
                return verdict, f"消息 {getattr(message, 'id', '?')}: {truncate(text, 200, '…')}"

    def _classify(self, text: str) -> Optional[GrabResult]:
        """失败优先：'已被抢完' 里也含 '抢'，先判失败可避免误报成功。"""
        if first_match(self._failure_patterns, text):
            return GrabResult.FAILED
        if first_match(self._success_patterns, text):
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
    async def _maybe_reply(self, message: Any, outcome: GrabOutcome) -> Optional[str]:
        reply = self.config.reply
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
                chat=outcome.chat_title or chat_id,
                text=text,
                error=f"{type(exc).__name__}: {exc}",
            )
            return None

        self._reply_cooldown[chat_id] = time.monotonic()
        self.stats["replied"] += 1
        self.alog.info(
            "已发送随机回复",
            chat=outcome.chat_title or chat_id,
            text=text,
            delay_s=round(delay, 2),
            pool_size=len(reply.texts),
        )
        return text

    # ------------------------------------------------------------------ #
    def _record(self, outcome: GrabOutcome) -> None:
        if outcome.result is GrabResult.SUCCESS:
            self.stats["success"] += 1
        elif outcome.result is GrabResult.FAILED:
            self.stats["failed"] += 1
        elif outcome.result is GrabResult.UNKNOWN:
            self.stats["unknown"] += 1
        elif outcome.result is GrabResult.ERROR:
            self.stats["error"] += 1

    def _log_outcome(
        self, outcome: GrabOutcome, chat_title: Optional[str], message_id: Optional[int]
    ) -> None:
        fields = {
            "result": outcome.result.value,
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

    def _submit_notify(self, message: Any, outcome: GrabOutcome) -> None:
        assert self.notifier is not None
        variables = build_variables(
            message,
            {
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
                context={"result": outcome.result.value, "chat": outcome.chat_title},
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


__all__ = ["ChatEventBus", "GrabOutcome", "GrabResult", "RedPacketHunter"]
