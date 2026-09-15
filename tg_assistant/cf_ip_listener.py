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
    ISP_LABELS,
    fetch_and_update,
    make_message_source,
    parse_ips_from_text,
    persist_run,
    should_update,
    summary_to_last_result,
)
from .config import CloudflareIPConfig
from .logging_setup import AccountLogger

#: 频道消息最小长度（太短的肯定不含 IP 列表）
_MIN_MESSAGE_LENGTH = 50

#: 快速预检：消息里是否含有看起来像 IP 的字符串
_QUICK_IP_PRESEARCH = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}")


def _record_label(result: Any) -> str:
    """``yx.7star.eu.cc`` 这种给人看的域名写法。"""
    return result.domain if result.name == "@" else f"{result.name}.{result.domain}"


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
        if self.config.split_by_isp:
            # ⚠️ 三网分流时**不能**拿「这一条消息」做预检。这类频道一条消息
            # 只讲一个运营商，用它去卡阈值会把另外两家一起挡掉 ——
            # 比如这条是联通的 3 MB/s，而电信刚发了 166 MB/s 的好 IP，
            # 预检不过就直接 return，电信那条永远不会更新。
            # 这里直接走完整抓取（它会拉 fetch_limit 条消息、逐家决策）。
            self.alog.info(
                "三网分流：跳过单条预检，改为抓取最近 %d 条逐家决策",
                self.config.fetch_limit,
            )
        else:
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

        # 落盘：三条路径（调度 / 手动触发 / 这里）统一走 persist_run，
        # 否则频道一发新消息就更新了 DNS，面板却还显示上一次的结果和时间。
        # ⚠️ 上面「单条预检不过就 return」的分支**故意不落盘** ——
        # 频道每条消息都写一次 state.json 没必要，那次跳过交给定时调度记录。
        persist_run(self.store, self.account_name, state, summary, self.config)

        # 通知。⚠️ 成败用**和面板同一个判据**（``summary_to_last_result`` 的 ``ok``），
        # 别用 ``summary.all_ok`` —— 它要求每条结果都 ok，而被跳过的条目 ok=False，
        # 于是「写了 1 条、跳过 2 家」会给用户发一条**失败**通知。
        shared = summary_to_last_result(summary, self.config)
        if summary.skipped:
            self.alog.info("更新跳过: %s", summary.skipped_reason)
        elif shared["ok"]:
            self._notify_success(summary)
        else:
            self._notify_failure(summary)

    def _notify_success(self, summary: Any) -> None:
        """成功通知。三网分流与不分流共用 —— 都从 ``summary.results`` 取。"""
        changed = [r for r in summary.results if r.ok and not r.skipped]
        domains = ", ".join(_record_label(r) for r in changed)
        self.alog.info(
            "DNS 更新成功: %s → %s",
            ", ".join(f"{ISP_LABELS.get(r.isp, '整体')}={r.ip}" for r in changed),
            domains,
        )
        if self.notifier is None:
            return

        from .notify import NotifyTask

        lines = "\n".join(
            f"{ISP_LABELS.get(r.isp, '') + ' ' if r.isp else ''}<code>{r.ip}</code>"
            for r in changed
        )
        self.notifier.submit(
            NotifyTask(
                event="forward",
                text=f"⚡ Cloudflare IP 已更新\n{lines}\n域名: {domains}",
            )
        )

    def _notify_failure(self, summary: Any) -> None:
        failed = [r for r in summary.results if not r.ok]
        self.alog.error(
            "DNS 更新部分失败: %d/%d",
            summary.changed_count,
            len(summary.results),
        )
        if self.notifier is None:
            return

        from .notify import NotifyTask

        errors = "; ".join(
            f"{ISP_LABELS.get(r.isp, '')}{_record_label(r)}: {r.error}" for r in failed
        )
        self.notifier.submit(
            NotifyTask(
                event="error",
                text=f"⚠️ Cloudflare IP 更新失败\n{errors}",
            )
        )


__all__ = ["CFIPListener"]
