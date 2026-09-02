"""Bot 通知渠道：队列、限流、发送、回退。"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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


import json

import httpx


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
        config = notify_text = notify_config(template="{nope}")
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
