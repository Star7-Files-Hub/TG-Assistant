"""Cloudflare 优选 IP 实时监听。

作为 pyrogram 的 :class:`MessageHandler` 挂到 ``AccountRunner`` 上，
监听源频道的新消息。频道一有新消息就触发解析 → 决策 → 更新。

设计要点：

- 只监听 ``config.cloudflare_ip.source_channel`` 这一个会话；
- 监听开启/关闭随账号启停，不需要独立生命周期；
- 更新结果通过 ``notifier``（如有）推送给用户，没有就只写日志；
- 消息处理是幂等的：决策不通过时静默跳过，不会重复更新。
"""

from __future__ import annotations

import re
from typing import Any, Optional

from pyrogram import filters
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message

from .cloudflare_ip import (
    fetch_and_update,
    make_message_source,
    parse_ips_from_text,
    should_update,
)
from .config import CloudflareIPConfig
from .logging_setup import AccountLogger

#: 频道消息最小长度（太短的肯定不含 IP 列表）
_MIN_MESSAGE_LENGTH = 50

#: 快速预检：消息里是否含有看起来像 IP 的字符串
_QUICK_IP_PRESEARCH = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}")


class CFIPListener:
    """Cloudflare 优选 IP 实时监听器。"""

    def __init__(
        self,
        account_name: str,
        config: CloudflareIPConfig,
        client: Any,
        alog: AccountLogger,
        store: Any,
        settings: Any,
        notifier: Optional[Any] = None,
    ) -> None:
        self.account_name = account_name
        self.config = config
        self.client = client
        self.alog = alog.bind("cf_ip")
        self.store = store
        self.settings = settings
        self.notifier = notifier
        self._handler: Optional[MessageHandler] = None
        self._running = False

    # ------------------------------------------------------------------ #
    def register(self) -> None:
        """挂载消息处理器到 client。幂等。"""
        if self._handler is not None:
            return

        channel = self.config.source_channel
        if channel is None:
            return

        chat_filter = filters.chat(channel)
        self._handler = MessageHandler(
            self._on_message,
            chat_filter & filters.text & ~filters.outgoing,
        )
        self.client.add_handler(self._handler)
        self._running = True
        self.alog.info("Cloudflare IP 实时监听已启动", channel=channel)

    async def unregister(self) -> None:
        """卸载消息处理器。"""
        if self._handler is not None:
            self.client.remove_handler(self._handler)
            self._handler = None
        self._running = False
        self.alog.info("Cloudflare IP 实时监听已停止")

    @property
    def is_running(self) -> bool:
        return self._running and self._handler is not None

    # ------------------------------------------------------------------ #
    async def _on_message(self, client: Any, message: Message) -> None:
        """频道新消息回调。"""
        text = message.text or ""
        if len(text) < _MIN_MESSAGE_LENGTH:
            return

        if not _QUICK_IP_PRESEARCH.search(text):
            return

        self.alog.info(
            "收到频道新消息，开始解析",
            message_id=message.id,
            text_len=len(text),
        )

        state = self.store.load_state(self.account_name)

        # 解析
        fetched = parse_ips_from_text(text)
        if not fetched.has_ip:
            self.alog.debug("频道消息未解析到 IP，跳过")
            return

        fetched.message_id = message.id

        # 决策
        decision = should_update(fetched, self.config, state)
        if not decision.should_update:
            self.alog.info("决策跳过: %s", decision.reason)
            return

        self.alog.info("决策通过: %s", decision.reason)

        # 更新
        from .proxy import resolve_proxy

        record = self.store.require_account(self.account_name)
        proxy = resolve_proxy(record, self.settings)
        source = make_message_source(client)

        summary = await fetch_and_update(self.config, source, state, proxy)

        # 落盘
        self.store.save_state(self.account_name, state)

        # 通知
        if summary.skipped:
            self.alog.info("更新跳过: %s", summary.skipped_reason)
        elif summary.all_ok:
            self._notify_success(decision, summary)
        else:
            self._notify_failure(summary)

    def _notify_success(self, decision: Any, summary: Any) -> None:
        self.alog.info(
            "DNS 更新成功: %s → %s (%.2f MB/s)",
            decision.ip,
            ", ".join(
                r.name == "@" and r.domain or f"{r.name}.{r.domain}"
                for r in summary.results
            ),
            decision.speed or 0,
        )
        if self.notifier is not None:
            from .notify import NotifyTask

            domains = ", ".join(
                r.name == "@" and r.domain or f"{r.name}.{r.domain}"
                for r in summary.results
            )
            self.notifier.submit(
                NotifyTask(
                    event="forward",
                    text=(
                        f"⚡ Cloudflare IP 已更新\n"
                        f"IP: <code>{decision.ip}</code>\n"
                        f"速度: {decision.speed:.2f} MB/s\n"
                        f"域名: {domains}"
                    ),
                )
            )

    def _notify_failure(self, summary: Any) -> None:
        failed = [r for r in summary.results if not r.ok]
        self.alog.error(
            "DNS 更新部分失败: %d/%d",
            summary.changed_count,
            len(summary.results),
        )
        if self.notifier is not None:
            from .notify import NotifyTask

            errors = "; ".join(
                f"{r.name}.{r.domain}: {r.error}" for r in failed
            )
            self.notifier.submit(
                NotifyTask(
                    event="error",
                    text=f"⚠️ Cloudflare IP 更新失败\n{errors}",
                )
            )


__all__ = ["CFIPListener"]
