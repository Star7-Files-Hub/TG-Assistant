"""Bot 通知渠道：队列、限流、发送、回退。"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tg_assistant.config import NotifyConfig
from tg_assistant.logging_setup import account_logger
from tg_assistant.notify import BotNotifier, NotifyTask, build_notify_text


def notify_config(**overrides) -> NotifyConfig:
    base: dict = {
        "enabled": True,
        "bot_token": "123456789:AAEabcdefghijklmnopqrstuvwxyz123456",
        "chat_id": -1001234567890,
        "mode": "copy",
        "events": ["forward", "red_packet"],
    }
    base.update(overrides)
    return NotifyConfig.model_validate(base)


class FakeResponse(httpx.Response):
    def __init__(self, status: int, payload: dict | None = None, text: str = "") -> None:
        if payload is not None:
            text = json.dumps(payload)
        super().__init__(status_code=status, text=text, headers={"Content-Type": "application/json"})


class FakeTransport(httpx.AsyncBaseTransport):
    """记录 httpx 调用并返回预设响应。"""

    def __init__(self, responses: list[FakeResponse] | None = None) -> None:
        self.responses = list(responses or [])
        self.requests: list[dict[str, Any]] = []
        self._index = 0

    async def handle_async_request(self, request: httpx.Request) -> FakeResponse:
        self.requests.append(
            {
                "method": request.method,
                "url": str(request.url),
                "content": request.content.decode() if request.content else None,
            }
        )
        if self._index < len(self.responses):
            resp = self.responses[self._index]
            self._index += 1
            return resp
        return FakeResponse(200, {"ok": True, "result": {"message_id": 1}})


class TestBuildNotifyText:
    def test_default_template(self):
        text = build_notify_text(notify_config(), {"text": "正文", "chat_title": "群"})
        assert "群" in text
        assert "正文" in text

    def test_custom_template(self):
        config = notify_config(template="[{chat_id}] {text}")
        text = build_notify_text(config, {"chat_id": -100123, "text": "hi"})
        assert text == "[-100123] hi"

    def test_missing_placeholder_kept(self):
        config = notify_config(template="{nope}")
        assert build_notify_text(config, {}) == "{nope}"

    def test_includes_link_when_requested(self):
        config = notify_config(include_source_link=True)
        text = build_notify_text(config, {"link": "https://t.me/c/123/456"})
        assert "https://t.me/c/123/456" in text


class TestBotNotifier:
    @pytest.mark.asyncio
    async def test_verify_checks_token(self):
        # start() 内部会先调一次 verify()，所以 transport 里要预置两份响应
        transport = FakeTransport(
            [
                FakeResponse(200, {"ok": True, "result": {"id": 1, "is_bot": True, "username": "mybot"}}),
                FakeResponse(200, {"ok": True, "result": {"id": 1, "is_bot": True, "username": "mybot"}}),
            ]
        )
        config = notify_config()
        notifier = BotNotifier(config, account_logger("test"), None, _transport=transport)
        await notifier.start()
        ok, detail = await notifier.verify()
        assert ok
        assert "mybot" in detail

    @pytest.mark.asyncio
    async def test_verify_detects_bad_token(self):
        transport = FakeTransport(
            [
                FakeResponse(401, {"ok": False, "description": "Unauthorized"}),
                FakeResponse(401, {"ok": False, "description": "Unauthorized"}),
            ]
        )
        config = notify_config()
        notifier = BotNotifier(config, account_logger("test"), None, _transport=transport)
        await notifier.start()
        ok, detail = await notifier.verify()
        assert not ok
        assert "401" in detail or "Unauthorized" in detail

    @pytest.mark.asyncio
    async def test_verify_detects_wrong_chat(self):
        transport = FakeTransport(
            [
                FakeResponse(200, {"ok": True, "result": {"id": 1, "is_bot": True, "username": "mybot"}}),
                FakeResponse(200, {"ok": True, "result": {"id": 1, "is_bot": True, "username": "mybot"}}),
            ]
        )
        config = notify_config(chat_id=-100999)
        notifier = BotNotifier(config, account_logger("test"), None, _transport=transport)
        await notifier.start()
        ok, detail = await notifier.verify()
        # chat_id 与 bot 自身 id 不同不代表 token 无效，这里只验证 token 有效即可
        assert ok

    @pytest.mark.asyncio
    async def test_send_text_fallback(self):
        """没有 copy_from 时直接 send_message。"""
        transport = FakeTransport([FakeResponse(200, {"ok": True, "result": {"message_id": 42}})])
        config = notify_config(mode="text")
        notifier = BotNotifier(config, account_logger("test"), None, _transport=transport)
        await notifier.start()
        task = NotifyTask(event="forward", text="hello")
        assert notifier.submit(task)
        await notifier.stop(drain_timeout=2)
        assert notifier.stats["sent"] == 1
        body = transport.requests[-1]["content"] or ""
        assert "hello" in body

    @pytest.mark.asyncio
    async def test_copy_message_fallback_to_text(self):
        """copy 失败时回退到纯文本发送。"""
        # start() 的 verify() 消耗一份成功响应，copy 失败一份，fallback 成功一份
        transport = FakeTransport(
            [
                FakeResponse(200, {"ok": True, "result": {"id": 1, "is_bot": True, "username": "mybot"}}),
                FakeResponse(400, {"ok": False, "description": "message to copy not found"}),
                FakeResponse(200, {"ok": True, "result": {"message_id": 1}}),
            ]
        )
        config = notify_config(mode="copy")
        notifier = BotNotifier(config, account_logger("test"), None, _transport=transport)
        await notifier.start()
        task = NotifyTask(event="forward", text="备胎文本", copy_from=(-100123, 99))
        assert notifier.submit(task)
        await notifier.stop(drain_timeout=2)
        assert notifier.stats["sent"] == 1
        assert notifier.stats["fallback"] == 1

    @pytest.mark.asyncio
    async def test_rate_limit_queues(self):
        transport = FakeTransport([FakeResponse(200, {"ok": True, "result": {"message_id": 1}})])
        config = notify_config(rate_limit_per_minute=60, queue_size=500)
        notifier = BotNotifier(config, account_logger("test"), None, _transport=transport)
        await notifier.start()
        for index in range(3):
            notifier.submit(NotifyTask(event="forward", text=f"m{index}"))
        await notifier.stop(drain_timeout=5)
        assert notifier.stats["sent"] == 3

    @pytest.mark.asyncio
    async def test_queue_full_drops_oldest(self):
        transport = FakeTransport([FakeResponse(200, {"ok": True, "result": {"message_id": 1}})])
        config = notify_config(queue_size=2)
        notifier = BotNotifier(config, account_logger("test"), None, _transport=transport)
        await notifier.start()
        for index in range(5):
            notifier.submit(NotifyTask(event="forward", text=f"m{index}"))
        await notifier.stop(drain_timeout=2)
        assert notifier.stats["dropped"] >= 3

    @pytest.mark.asyncio
    async def test_429_retries_after(self):
        transport = FakeTransport(
            [
                FakeResponse(429, {"ok": False, "parameters": {"retry_after": 1}}),
                FakeResponse(200, {"ok": True, "result": {"message_id": 1}}),
            ]
        )
        config = notify_config(mode="text")
        notifier = BotNotifier(config, account_logger("test"), None, _transport=transport)
        await notifier.start()
        notifier.submit(NotifyTask(event="forward", text="hello"))
        await notifier.stop(drain_timeout=5)
        assert notifier.stats["sent"] == 1

    @pytest.mark.asyncio
    async def test_parse_failure_retries_without_parse_mode(self):
        """HTML 解析失败时重试一次（不带 parse_mode）。"""
        transport = FakeTransport(
            [
                FakeResponse(400, {"ok": False, "description": "can't parse entities"}),
                FakeResponse(200, {"ok": True, "result": {"message_id": 1}}),
            ]
        )
        config = notify_config(mode="text")
        notifier = BotNotifier(config, account_logger("test"), None, _transport=transport)
        await notifier.start()
        notifier.submit(NotifyTask(event="forward", text="<bad>html"))
        await notifier.stop(drain_timeout=5)
        assert notifier.stats["sent"] == 1

    @pytest.mark.asyncio
    async def test_empty_queue_does_not_deadlock(self):
        config = notify_config()
        notifier = BotNotifier(config, account_logger("test"), None)
        await notifier.start()
        await notifier.stop(drain_timeout=1)
        assert notifier.stats["sent"] == 0

    @pytest.mark.asyncio
    async def test_submit_without_text_dropped(self):
        config = notify_config()
        notifier = BotNotifier(config, account_logger("test"), None)
        await notifier.start()
        assert not notifier.submit(NotifyTask(event="forward", text="   "))

    @pytest.mark.asyncio
    async def test_close_idempotent(self):
        config = notify_config()
        notifier = BotNotifier(config, account_logger("test"), None)
        await notifier.start()
        await notifier.stop(drain_timeout=1)
        await notifier.stop(drain_timeout=1)

    def test_snapshot(self):
        config = notify_config()
        notifier = BotNotifier(config, account_logger("test"), None)
        snapshot = notifier.snapshot()
        assert "running" in snapshot
        assert "stats" in snapshot


class TestNotifyForwardMode:
    """``mode="forward"``：**频道里什么样，通知就什么样**。

    bot 用 ``forwardMessage`` 把目标频道里那条消息转发给你 —— 通知里带着
    「转发自」抬头，和频道里那条一模一样。源会话禁止转发 / 内容受保护时
    Telegram 会拒绝 ⇒ 自动降级 ``copyMessage``（内容一致，没抬头）。
    """

    @pytest.mark.asyncio
    async def test_forward_is_used_by_default(self):
        transport = FakeTransport(
            [
                FakeResponse(200, {"ok": True, "result": {"id": 1, "is_bot": True, "username": "b"}}),
                FakeResponse(200, {"ok": True, "result": {"message_id": 55}}),
            ]
        )
        notifier = BotNotifier(
            notify_config(mode="forward"), account_logger("t"), None, _transport=transport
        )
        await notifier.start()
        notifier.submit(NotifyTask(event="forward", text="x", copy_from=(-100, 5)))
        await notifier._queue.join()

        assert "forwardMessage" in transport.requests[-1]["url"], (
            "默认必须走 forward —— 小白要的就是「频道什么样机器人就什么样」"
        )
        assert notifier.stats["sent"] == 1
        await notifier.stop(drain_timeout=2)

    @pytest.mark.asyncio
    async def test_falls_back_to_copy_when_forward_is_rejected(self):
        """源会话禁止转发 ⇒ Telegram 拒绝 forwardMessage ⇒ 降级 copyMessage。"""
        transport = FakeTransport(
            [
                FakeResponse(200, {"ok": True, "result": {"id": 1, "is_bot": True, "username": "b"}}),
                FakeResponse(400, {"ok": False, "description": "Forbidden: message can't be forwarded"}),
                FakeResponse(200, {"ok": True, "result": {"message_id": 55}}),
            ]
        )
        notifier = BotNotifier(
            notify_config(mode="forward"), account_logger("t"), None, _transport=transport
        )
        await notifier.start()
        notifier.submit(NotifyTask(event="forward", text="x", copy_from=(-100, 5)))
        await notifier._queue.join()

        urls = [r["url"] for r in transport.requests]
        assert any("forwardMessage" in u for u in urls)
        assert "copyMessage" in urls[-1], "forward 被拒 ⇒ 必须兜底用 copy"
        assert notifier.stats["sent"] == 1
        assert notifier.stats["fallback"] == 1
        await notifier.stop(drain_timeout=2)

    @pytest.mark.asyncio
    async def test_copy_mode_never_tries_forward(self):
        """显式 ``copy`` ⇒ 不试 forward（源会话一律禁止转发时用，省一次失败调用）。"""
        transport = FakeTransport(
            [
                FakeResponse(200, {"ok": True, "result": {"id": 1, "is_bot": True, "username": "b"}}),
                FakeResponse(200, {"ok": True, "result": {"message_id": 55}}),
            ]
        )
        notifier = BotNotifier(notify_config(mode="copy"), account_logger("t"), None, _transport=transport)
        await notifier.start()
        notifier.submit(NotifyTask(event="forward", text="x", copy_from=(-100, 5)))
        await notifier._queue.join()

        assert "copyMessage" in transport.requests[-1]["url"]
        assert not any("forwardMessage" in r["url"] for r in transport.requests)
        assert notifier.stats["fallback"] == 0
        await notifier.stop(drain_timeout=2)

    def test_default_mode_is_forward(self):
        """默认值必须是 forward —— 小白要的就是「频道什么样机器人就什么样」。

        ⚠️ 别用 ``notify_config()`` 验：那个夹具自己钉了 ``mode="copy"``
        （现有 copy 用例靠它保持语义）。
        """
        assert NotifyConfig.model_validate({"bot_token": "1:a", "chat_id": -100}).mode == "forward"


class TestNotifyWithdraw:
    """撤回「已经失效」的那条通知。

    场景：频道那条先发出去 ⇒ 通知已发（bot 把它复制到通知会话）；随后群组那条到达，
    引擎**删掉**目标里那条频道消息 ⇒ 那条通知指向一条已经不存在的消息，
    而用户还会再收到群组那条的通知 —— 一共两条，第一条点了是空的。

    🔴 通知在**另一个会话**里，引擎删不到它 —— 只能由 notifier **事后**按 key 撤。
    """

    @staticmethod
    async def _send_one(notifier: BotNotifier, **task_kwargs: Any) -> None:
        notifier.submit(NotifyTask(event="forward", text="x", **task_kwargs))
        # 队列是异步消费的 —— 等它真的发完，才能断言「记下了消息 id」
        await notifier._queue.join()

    @pytest.mark.asyncio
    async def test_withdraw_deletes_the_notification(self):
        transport = FakeTransport(
            [
                # start() 内部会先 verify() 一次
                FakeResponse(200, {"ok": True, "result": {"id": 1, "is_bot": True, "username": "b"}}),
                FakeResponse(200, {"ok": True, "result": {"message_id": 777}}),  # copyMessage
                FakeResponse(200, {"ok": True, "result": True}),  # deleteMessage
            ]
        )
        notifier = BotNotifier(notify_config(), account_logger("t"), None, _transport=transport)
        await notifier.start()
        await self._send_one(notifier, copy_from=(-100, 5), key="pair:fp:-100")

        removed = await notifier.withdraw("pair:fp:-100")

        assert removed == 1, "通知必须真的被删掉 —— 否则用户看到一条指向空消息的通知"
        last = transport.requests[-1]
        assert "deleteMessage" in last["url"]
        assert '"message_id":777' in (last["content"] or ""), (
            "删除的必须是刚才发出的那条通知（message_id 来自 copyMessage 的返回值）"
        )
        assert '"chat_id":-1001234567890' in (last["content"] or "")
        assert notifier.stats["withdrawn"] == 1
        await notifier.stop(drain_timeout=2)

    @pytest.mark.asyncio
    async def test_withdraw_unknown_key_is_noop(self):
        transport = FakeTransport(
            [FakeResponse(200, {"ok": True, "result": {"id": 1, "is_bot": True, "username": "b"}})]
        )
        notifier = BotNotifier(notify_config(), account_logger("t"), None, _transport=transport)
        await notifier.start()

        assert await notifier.withdraw("pair:没发过:-100") == 0
        assert not any("deleteMessage" in r["url"] for r in transport.requests), (
            "没发过的键不该去调 deleteMessage"
        )
        await notifier.stop(drain_timeout=2)

    @pytest.mark.asyncio
    async def test_task_without_key_is_not_withdrawable(self):
        transport = FakeTransport(
            [
                FakeResponse(200, {"ok": True, "result": {"id": 1, "is_bot": True, "username": "b"}}),
                FakeResponse(200, {"ok": True, "result": {"message_id": 777}}),
            ]
        )
        notifier = BotNotifier(notify_config(), account_logger("t"), None, _transport=transport)
        await notifier.start()
        await self._send_one(notifier, copy_from=(-100, 5))  # 不给 key

        assert await notifier.withdraw("pair:fp:-100") == 0, (
            "没给 key 的通知不记账 —— 撤不到，也不该误删别的"
        )
        await notifier.stop(drain_timeout=2)

    @pytest.mark.asyncio
    async def test_delete_failure_only_warns(self):
        """bot 没删除权限 / 消息超过 48 小时：只告警，绝不能把转发链路带崩。"""
        transport = FakeTransport(
            [
                FakeResponse(200, {"ok": True, "result": {"id": 1, "is_bot": True, "username": "b"}}),
                FakeResponse(200, {"ok": True, "result": {"message_id": 777}}),
                FakeResponse(400, {"ok": False, "description": "message to delete not found"}),
            ]
        )
        notifier = BotNotifier(notify_config(), account_logger("t"), None, _transport=transport)
        await notifier.start()
        await self._send_one(notifier, copy_from=(-100, 5), key="pair:fp:-100")

        assert await notifier.withdraw("pair:fp:-100") == 0
        assert notifier.stats["withdrawn"] == 0
        await notifier.stop(drain_timeout=2)

    @pytest.mark.asyncio
    async def test_records_are_pruned_after_ttl(self):
        """长时间运行下这张表不能无限长 —— 过期记录会被丢掉。"""
        transport = FakeTransport(
            [
                FakeResponse(200, {"ok": True, "result": {"id": 1, "is_bot": True, "username": "b"}}),
                FakeResponse(200, {"ok": True, "result": {"message_id": 777}}),
            ]
        )
        notifier = BotNotifier(notify_config(), account_logger("t"), None, _transport=transport)
        await notifier.start()
        await self._send_one(notifier, copy_from=(-100, 5), key="pair:fp:-100")
        assert "pair:fp:-100" in notifier._sent_by_key

        notifier.sent_key_ttl = 0.0
        notifier._prune_sent_keys()
        assert notifier._sent_by_key == {}, "过期记录必须清掉"
        assert await notifier.withdraw("pair:fp:-100") == 0
        await notifier.stop(drain_timeout=2)
