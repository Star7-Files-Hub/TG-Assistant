"""Bot 通知渠道。

**为什么需要**：自己的频道不会给自己发通知，所以用一个 bot 把同样的内容再发一遍。

**如何保证"与秒转频道的消息保持一致"**：转发成功后我们已经拿到了目标频道里那条消息的
``message_id``，于是让 bot 调用 Bot API 的 ``copyMessage``
（``from_chat_id=目标频道, message_id=刚转发的消息``）。这样文字、图片、按钮布局
都由 Telegram 服务端复制，内容与频道里**完全一致**，不存在二次拼装造成的差异。

要求：把 bot 拉进目标频道（管理员或有发言权限即可），否则 ``copyMessage`` 会失败。
失败时自动降级为文本推送，保证通知不丢。

**限流**：Bot API 对同一群约 20 条/分钟，超了会返回 429。这里用令牌桶 + 单 worker 串行
发送，并遵守 ``retry_after``，避免把 bot 打进惩罚状态。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from .config import ChatRef, NotifyConfig, ProxyConfig
from .logging_setup import AccountLogger
from .matching import (
    DEFAULT_NOTIFY_TEMPLATE,
    SAFE_TEXT_LENGTH,
    render_template,
    truncate,
)
from .proxy import httpx_proxy


@dataclass
class NotifyTask:
    """一条待推送的通知。

    ``copy_from`` 存在时优先用 ``copyMessage`` 复制原消息；
    否则退化为 ``sendMessage`` 发送 ``text``。
    """

    event: str
    text: str
    copy_from: Optional[tuple[ChatRef, int]] = None
    copy_from_ids: list[tuple[ChatRef, int]] = field(default_factory=list)
    parse_mode: Optional[str] = "HTML"
    silent: bool = False
    created_at: float = field(default_factory=time.time)
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def age_ms(self) -> float:
        return (time.time() - self.created_at) * 1000


class BotNotifier:
    """异步限流的 Bot 通知发送器。

    生命周期::

        notifier = BotNotifier(config, alog, proxy)
        await notifier.start()
        notifier.submit(NotifyTask(...))     # 非阻塞，热路径安全
        await notifier.stop()
    """

    def __init__(
        self,
        config: NotifyConfig,
        alog: AccountLogger,
        proxy: Optional[ProxyConfig] = None,
        _transport: Any = None,
    ) -> None:
        self.config = config
        self.alog = alog.bind("notify")
        self.proxy = proxy if config.use_proxy else None
        self._queue: asyncio.Queue[NotifyTask] = asyncio.Queue(maxsize=config.queue_size)
        self._worker: Optional[asyncio.Task[None]] = None
        self._http: Optional[httpx.AsyncClient] = None
        self._transport = _transport
        self._send_times: list[float] = []
        self._stopping = False
        self.stats = {"sent": 0, "failed": 0, "dropped": 0, "fallback": 0}

    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        return self.config.enabled and bool(self.config.bot_token) and self.config.chat_id is not None

    @property
    def _base_url(self) -> str:
        return f"{self.config.api_base.rstrip('/')}/bot{self.config.bot_token}"

    async def start(self) -> None:
        if not self.enabled:
            self.alog.info("Bot 通知未启用，跳过启动")
            return
        proxy_url = httpx_proxy(self.proxy)
        if self._transport is not None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(self.config.timeout),
                proxy=proxy_url,
                follow_redirects=True,
                transport=self._transport,
            )
        else:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(self.config.timeout),
                proxy=proxy_url,
                follow_redirects=True,
            )
        self._stopping = False
        self._worker = asyncio.create_task(self._run(), name="notify-worker")
        ok, detail = await self.verify()
        self.alog.info(
            "Bot 通知已启动",
            bot=detail,
            target=self.config.chat_id,
            mode=self.config.mode,
            rate_limit=f"{self.config.rate_limit_per_minute}/min",
            proxy=self.proxy.to_url() if self.proxy else "直连",
            healthy=ok,
        )

    async def stop(self, drain_timeout: float = 5.0) -> None:
        if self._worker is None:
            return
        self._stopping = True
        try:
            await asyncio.wait_for(self._queue.join(), timeout=drain_timeout)
        except asyncio.TimeoutError:
            self.alog.warning(
                "退出前仍有通知未发完，已放弃",
                pending=self._queue.qsize(),
            )
        self._worker.cancel()
        # 退出路径上不再关心 worker 怎么结束的
        with contextlib.suppress(BaseException):
            await self._worker
        self._worker = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        self.alog.info("Bot 通知已停止", **self.stats)

    # ------------------------------------------------------------------ #
    async def verify(self) -> tuple[bool, str]:
        """调用 ``getMe`` 验证 token 是否可用。失败只告警，不阻断主流程。"""
        if self._http is None:
            return False, "未初始化"
        try:
            response = await self._http.get(f"{self._base_url}/getMe")
            payload = response.json()
        except Exception as exc:
            self.alog.warning(
                "无法验证 bot token（通知可能发不出去）",
                error=f"{type(exc).__name__}: {exc}",
                hint="国内机器请确认 notify.use_proxy=true 且代理可用",
            )
            return False, "验证失败"
        if not payload.get("ok"):
            self.alog.error(
                "bot token 无效",
                description=payload.get("description"),
                hint="检查 BotFather 给的 token 是否完整、是否被 revoke",
            )
            return False, str(payload.get("description"))
        info = payload.get("result", {})
        return True, f"@{info.get('username')} (id={info.get('id')})"

    def submit(self, task: NotifyTask) -> bool:
        """把任务放进队列。热路径调用，永不阻塞、永不抛错。"""
        if not self.enabled or not self.config.wants(task.event):
            return False
        if not (task.text and task.text.strip()) and not task.copy_from:
            return False
        try:
            self._queue.put_nowait(task)
            return True
        except asyncio.QueueFull:
            # 丢最旧的一条腾位置，保证新消息优先（旧通知的价值随时间衰减）
            try:
                dropped = self._queue.get_nowait()
                self._queue.task_done()
                self.stats["dropped"] += 1
                self.alog.warning(
                    "通知队列已满，丢弃最早的一条",
                    dropped_event=dropped.event,
                    queue_size=self._queue.qsize(),
                )
                self._queue.put_nowait(task)
                return True
            except Exception:
                self.stats["dropped"] += 1
                self.alog.error("通知队列已满且无法腾出空间，本条通知丢弃", event=task.event)
                return False

    def snapshot(self) -> dict[str, Any]:
        """运行状态快照。"""
        return {
            "running": self._worker is not None and not self._worker.done(),
            "queue_size": self._queue.qsize(),
            "stats": dict(self.stats),
        }

    # ------------------------------------------------------------------ #
    async def _run(self) -> None:
        assert self._http is not None
        while True:
            task = await self._queue.get()
            try:
                await self._throttle()
                await self._deliver(task)
            except asyncio.CancelledError:
                self._queue.task_done()
                raise
            except Exception as exc:  # 通知失败绝不影响主业务
                self.stats["failed"] += 1
                self.alog.exception(
                    "推送通知时发生未预期错误",
                    event=task.event,
                    error=f"{type(exc).__name__}: {exc}",
                )
                self._queue.task_done()
            else:
                self._queue.task_done()

    async def _throttle(self) -> None:
        """滑动窗口限流：保证 60 秒内不超过 ``rate_limit_per_minute`` 条。"""
        limit = self.config.rate_limit_per_minute
        now = time.monotonic()
        self._send_times = [t for t in self._send_times if now - t < 60.0]
        if len(self._send_times) >= limit:
            wait = 60.0 - (now - self._send_times[0]) + 0.05
            if wait > 0:
                self.alog.debug("通知限流等待", wait_s=round(wait, 2), window_count=len(self._send_times))
                await asyncio.sleep(wait)
                now = time.monotonic()
                self._send_times = [t for t in self._send_times if now - t < 60.0]
        self._send_times.append(time.monotonic())

    async def _deliver(self, task: NotifyTask) -> None:
        started = time.perf_counter()
        use_copy = self.config.mode == "copy" and (task.copy_from or task.copy_from_ids)

        if use_copy:
            ok = await self._send_copies(task)
            if ok:
                self.stats["sent"] += 1
                self.alog.info(
                    "通知已发送（复制原消息，内容与频道一致）",
                    event=task.event,
                    queue_delay_ms=round(task.age_ms, 1),
                    cost_ms=round((time.perf_counter() - started) * 1000, 1),
                    **task.context,
                )
                return
            self.stats["fallback"] += 1
            self.alog.warning(
                "复制消息失败，降级为文本通知",
                event=task.event,
                hint="确认 bot 已加入目标频道且有读取/发送权限",
            )

        ok = await self._send_text(task)
        if ok:
            self.stats["sent"] += 1
            self.alog.info(
                "通知已发送（文本模式）",
                event=task.event,
                queue_delay_ms=round(task.age_ms, 1),
                cost_ms=round((time.perf_counter() - started) * 1000, 1),
                **task.context,
            )
        else:
            self.stats["failed"] += 1

    async def _send_copies(self, task: NotifyTask) -> bool:
        pairs = task.copy_from_ids or ([task.copy_from] if task.copy_from else [])
        if not pairs:
            return False
        all_ok = True
        for from_chat, message_id in pairs:
            payload: dict[str, Any] = {
                "chat_id": self.config.chat_id,
                "from_chat_id": from_chat,
                "message_id": message_id,
                "disable_notification": task.silent or self.config.silent,
            }
            if self.config.message_thread_id is not None:
                payload["message_thread_id"] = self.config.message_thread_id
            ok, _ = await self._call("copyMessage", payload)
            all_ok = all_ok and ok
            if not ok:
                break
        return all_ok

    async def _send_text(self, task: NotifyTask) -> bool:
        text = truncate(task.text, SAFE_TEXT_LENGTH)
        payload: dict[str, Any] = {
            "chat_id": self.config.chat_id,
            "text": text,
            "disable_notification": task.silent or self.config.silent,
            "link_preview_options": {"is_disabled": True},
        }
        if task.parse_mode:
            payload["parse_mode"] = task.parse_mode
        if self.config.message_thread_id is not None:
            payload["message_thread_id"] = self.config.message_thread_id

        ok, description = await self._call("sendMessage", payload)
        if not ok and description and "can't parse entities" in description.lower():
            # 模板里混入了裸 < > &，去掉 parse_mode 再发一次，宁可少格式也别丢消息
            payload.pop("parse_mode", None)
            self.alog.warning("HTML 解析失败，改用纯文本重发", description=description)
            ok, description = await self._call("sendMessage", payload)
        return ok

    async def _call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        attempts: int = 3,
    ) -> tuple[bool, Optional[str]]:
        """调用 Bot API，处理 429 与网络抖动。"""
        assert self._http is not None
        url = f"{self._base_url}/{method}"
        for attempt in range(1, attempts + 1):
            try:
                response = await self._http.post(url, json=payload)
            except httpx.HTTPError as exc:
                if attempt >= attempts:
                    self.alog.error(
                        f"Bot API {method} 网络失败",
                        error=f"{type(exc).__name__}: {exc}",
                        attempt=attempt,
                        hint="检查代理是否可用（notify.use_proxy）",
                    )
                    return False, str(exc)
                await asyncio.sleep(0.5 * attempt)
                continue

            try:
                data = response.json()
            except ValueError:
                self.alog.error(
                    f"Bot API {method} 返回了非 JSON 响应",
                    status=response.status_code,
                    body=response.text[:200],
                )
                return False, response.text[:200]

            if data.get("ok"):
                return True, None

            description = str(data.get("description") or "")
            if response.status_code == 429:
                retry_after = float(
                    (data.get("parameters") or {}).get("retry_after") or 1.0
                )
                self.alog.warning(
                    "Bot API 触发限流，按要求等待后重试",
                    method=method,
                    retry_after=retry_after,
                    attempt=attempt,
                )
                await asyncio.sleep(retry_after + 0.2)
                continue

            level_hint = _hint_for(description)
            self.alog.error(
                f"Bot API {method} 被拒绝",
                status=response.status_code,
                description=description,
                hint=level_hint,
                attempt=attempt,
            )
            return False, description
        return False, "重试次数用尽"


def _hint_for(description: str) -> str:
    text = description.lower()
    if "chat not found" in text:
        return "chat_id 不对，或 bot 从未与该用户/群产生过会话（先给 bot 发一条 /start）"
    if "bot was blocked" in text:
        return "你把 bot 拉黑了，去 Telegram 里解除拉黑"
    if "not enough rights" in text or "chat_write_forbidden" in text:
        return "bot 在目标聊天没有发言权限"
    if "message to copy not found" in text:
        return "bot 看不到源消息，需要把 bot 加进转发目标频道"
    if "unauthorized" in text:
        return "bot token 无效或已被 revoke"
    if "message_thread_not_found" in text:
        return "message_thread_id 不存在（话题群里话题被删了？）"
    return "详见 Bot API 文档"


def build_notify_text(
    config: NotifyConfig,
    variables: dict[str, Any],
    *,
    default_template: str = DEFAULT_NOTIFY_TEMPLATE,
) -> str:
    template = config.template or default_template
    text = render_template(template, variables)
    if config.include_source_link and variables.get("link"):
        link = str(variables["link"])
        if link not in text:
            text = f"{text}\n\n🔗 {link}"
    return text


__all__ = ["BotNotifier", "NotifyTask", "build_notify_text"]
