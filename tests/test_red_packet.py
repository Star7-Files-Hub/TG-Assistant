"""抢红包引擎：检测、点击、判定、回复。"""

from __future__ import annotations

import asyncio

import pytest
from pyrogram.errors import BotResponseTimeout, QueryIdInvalid

from tg_assistant.config import AccountConfig
from tg_assistant.red_packet import (
    ChatEventBus,
    GrabResult,
    RedPacketHunter,
)

from .conftest import (
    FakeButton,
    FakeChat,
    FakeClient,
    FakeMarkup,
    FakeUser,
    make_message,
)

CHAT = -1009999999999


def rp_config(**overrides) -> AccountConfig:
    base: dict = {
        "red_packet": {
            "enabled": True,
            "strategy": "auto",
            "delay": 0,
            "jitter": 0,
            "success": {"wait_timeout": 0.05},
        }
    }
    base["red_packet"].update(overrides)
    return AccountConfig.model_validate(base)


def rp_message(text: str = "🧧 红包来了", *, markup=None, sender=None, **kwargs):
    kwargs.setdefault("chat", FakeChat(CHAT, title="红包群"))
    kwargs.setdefault("markup", markup)
    if sender is not None:
        kwargs["sender"] = sender
    return make_message(text, **kwargs)


def button_markup(*buttons: tuple[str, bytes | None]):
    return FakeMarkup([[FakeButton(text, callback_data=data) for text, data in buttons]])


class TestDetection:
    def test_button_strategy_finds_inline_button(self, alog):
        hunter = RedPacketHunter(FakeClient(), rp_config(strategy="button"), alog)
        msg = rp_message("点击领取", markup=button_markup(("领取红包", b"grab")))
        assert hunter._should_grab(msg, CHAT) is not None

    def test_button_strategy_requires_text_match_when_patterns_set(self, alog):
        hunter = RedPacketHunter(
            FakeClient(),
            rp_config(
                strategy="button",
                detect={"text_patterns": ["红包"], "button_keywords": ["领取"]},
            ),
            alog,
        )
        # 按钮匹配但正文没有"红包" → 不抢
        msg = rp_message("点这里", markup=button_markup(("领取", b"x")))
        assert hunter._should_grab(msg, CHAT) is None

    def test_keyword_strategy(self, alog):
        hunter = RedPacketHunter(
            FakeClient(),
            rp_config(
                strategy="keyword",
                detect={
                    "code_pattern": r"/grab\s+(\d+)",
                    "keyword_template": "/grab {code}",
                    "text_patterns": ["/grab"],
                },
            ),
            alog,
        )
        msg = rp_message("口令 /grab 666")
        assert hunter._should_grab(msg, CHAT) == (None, "666")

    def test_keyword_strategy_without_code_pattern(self, alog):
        hunter = RedPacketHunter(
            FakeClient(),
            rp_config(
                strategy="keyword",
                detect={"keyword_template": "抢", "text_patterns": ["红包"]},
            ),
            alog,
        )
        assert hunter._should_grab(rp_message("红包来了"), CHAT) == (None, None)

    def test_auto_strategy_prefers_button(self, alog):
        hunter = RedPacketHunter(FakeClient(), rp_config(strategy="auto"), alog)
        msg = rp_message("红包", markup=button_markup(("🧧 领取", b"x")))
        button, code = hunter._should_grab(msg, CHAT)
        assert button is not None
        assert button["kind"] == "inline"

    def test_auto_falls_back_to_keyword(self, alog):
        hunter = RedPacketHunter(
            FakeClient(),
            rp_config(
                strategy="auto",
                detect={
                    "code_pattern": r"/grab\s+(\d+)",
                    "keyword_template": "/grab {code}",
                    "text_patterns": ["/grab"],
                },
            ),
            alog,
        )
        assert hunter._should_grab(rp_message("/grab 99"), CHAT) == (None, "99")

    def test_ignores_self_when_configured(self, alog):
        hunter = RedPacketHunter(
            FakeClient(), rp_config(detect={"ignore_self": True}), alog
        )
        msg = rp_message("红包", sender=FakeUser(1, is_self=True))
        assert hunter._should_grab(msg, CHAT) is None

    def test_only_from_bots(self, alog):
        """只处理 bot 发的红包；非 bot 直接跳过。"""
        hunter = RedPacketHunter(
            FakeClient(),
            rp_config(
                strategy="keyword",
                detect={
                    "only_from_bots": True,
                    "keyword_template": "抢",
                    "text_patterns": ["红包"],
                },
            ),
            alog,
        )
        # 非 bot 发送者 → 跳过
        assert hunter._should_grab(rp_message("红包"), CHAT) is None
        # bot 发送者 → 命中
        assert (
            hunter._should_grab(rp_message("红包", sender=FakeUser(5, is_bot=True)), CHAT)
            is not None
        )

    def test_exclude_chat(self, alog):
        hunter = RedPacketHunter(FakeClient(), rp_config(exclude_chats=[CHAT]), alog)
        assert hunter._should_grab(rp_message("红包"), CHAT) is None

    def test_url_button_skipped(self, alog):
        hunter = RedPacketHunter(FakeClient(), rp_config(), alog)
        markup = FakeMarkup([[FakeButton("领取", url="https://t.me/x")]])
        msg = rp_message("红包", markup=markup)
        assert hunter._find_button(msg) is None


class TestClassification:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("恭喜你抢到 5.20 元", GrabResult.SUCCESS),
            ("已领取", GrabResult.SUCCESS),
            ("红包已被抢完了", GrabResult.FAILED),
            ("手慢了，红包没了", GrabResult.FAILED),
            ("感谢参与", GrabResult.FAILED),
            ("今天天气不错", None),
        ],
    )
    def test_classify(self, text, expected, alog):
        hunter = RedPacketHunter(FakeClient(), rp_config(), alog)
        assert hunter._classify(text) is expected


class TestExecution:
    @pytest.mark.asyncio
    async def test_button_success(self, alog):
        client = FakeClient(callback_answer="恭喜抢到 1 元")
        hunter = RedPacketHunter(client, rp_config(), alog)
        msg = rp_message("红包", markup=button_markup(("领取", b"x")))
        outcome = await hunter._execute(
            msg, hunter._find_button(msg), None, "button", asyncio.get_event_loop().time()
        )
        assert outcome.result is GrabResult.SUCCESS
        assert outcome.callback_text == "恭喜抢到 1 元"
        assert outcome.evidence is not None
        assert "callback" in outcome.evidence

    @pytest.mark.asyncio
    async def test_button_expired(self, alog):
        client = FakeClient(callback_error=QueryIdInvalid("QUERY_ID_INVALID"))
        hunter = RedPacketHunter(client, rp_config(), alog)
        msg = rp_message("红包", markup=button_markup(("领取", b"x")))
        outcome = await hunter._execute(
            msg, hunter._find_button(msg), None, "button", asyncio.get_event_loop().time()
        )
        assert outcome.result is GrabResult.FAILED
        assert "失效" in outcome.detail

    @pytest.mark.asyncio
    async def test_bot_response_timeout_without_followup_is_unknown(self, alog):
        """bot 不回 callback answer，且窗口内也没有后续消息 → 只能记 unknown。"""
        client = FakeClient(callback_error=BotResponseTimeout("timeout"))
        hunter = RedPacketHunter(client, rp_config(success={"wait_timeout": 0.05}), alog)
        msg = rp_message("红包", markup=button_markup(("领取", b"x")))
        outcome = await hunter._execute(
            msg, hunter._find_button(msg), None, "button", asyncio.get_event_loop().time()
        )
        assert outcome.result is GrabResult.UNKNOWN

    @pytest.mark.asyncio
    async def test_bot_response_timeout_falls_back_to_followup_message(self, alog):
        """回归：超时后必须仍能靠后续消息判定。

        旧实现超时分支里调用 ``_judge(None, None)``，queue 传 None 会立刻返回
        UNKNOWN，导致「依据后续消息判定」形同虚设 —— 这个用例在旧实现下会失败。
        """
        client = FakeClient(callback_error=BotResponseTimeout("timeout"))
        hunter = RedPacketHunter(client, rp_config(success={"wait_timeout": 1.0}), alog)
        msg = rp_message("红包", markup=button_markup(("领取", b"x")))

        async def feed_result() -> None:
            # 等点击失败、_judge 进入等待后，再把 bot 的结果消息喂进会话事件总线
            await asyncio.sleep(0.05)
            hunter._bus.feed(CHAT, rp_message("恭喜抢到 1 元"))

        feeder = asyncio.create_task(feed_result())
        outcome = await hunter._execute(
            msg, hunter._find_button(msg), None, "button", asyncio.get_event_loop().time()
        )
        await feeder
        assert outcome.result is GrabResult.SUCCESS
        assert outcome.evidence is not None

    @pytest.mark.asyncio
    async def test_keyboard_reply_strategy(self, alog):
        client = FakeClient()
        hunter = RedPacketHunter(
            client, rp_config(strategy="keyword", detect={"keyword_template": "抢"})
        , alog)
        msg = rp_message("红包", markup=FakeMarkup([[FakeButton("抢")]], inline=False))
        button = hunter._find_button(msg)
        assert button is not None
        await hunter._execute(msg, button, None, "keyboard-text", asyncio.get_event_loop().time())
        assert client.sent
        assert client.sent[0]["text"] == "抢"


class TestReply:
    def test_should_reply_logic(self, alog):
        hunter = RedPacketHunter(
            FakeClient(), rp_config(reply={"only_on_success": True}), alog
        )
        assert hunter._should_reply(GrabResult.SUCCESS)
        assert not hunter._should_reply(GrabResult.UNKNOWN)
        assert not hunter._should_reply(GrabResult.FAILED)

        hunter = RedPacketHunter(
            FakeClient(), rp_config(reply={"only_on_success": False}), alog
        )
        assert hunter._should_reply(GrabResult.SUCCESS)
        assert hunter._should_reply(GrabResult.UNKNOWN)

    @pytest.mark.asyncio
    async def test_reply_sent_on_success(self, alog):
        client = FakeClient(callback_answer="抢到了")
        hunter = RedPacketHunter(
            client,
            rp_config(
                reply={"enabled": True, "texts": ["谢谢老板"], "delay_range": [0, 0]}
            ),
            alog,
        )
        msg = rp_message("红包", markup=button_markup(("领取", b"x")))
        outcome = await hunter._execute(
            msg, hunter._find_button(msg), None, "button", asyncio.get_event_loop().time()
        )
        assert outcome.result is GrabResult.SUCCESS
        replied = await hunter._maybe_reply(msg, outcome)
        assert replied == "谢谢老板"

    @pytest.mark.asyncio
    async def test_reply_cooldown(self, alog):
        client = FakeClient(callback_answer="抢到了")
        hunter = RedPacketHunter(
            client,
            rp_config(
                reply={
                    "enabled": True,
                    "texts": ["谢谢"],
                    "delay_range": [0, 0],
                    "cooldown": 60,
                }
            ),
            alog,
        )
        msg = rp_message("红包", markup=button_markup(("领取", b"x")))
        outcome = await hunter._execute(
            msg, hunter._find_button(msg), None, "button", asyncio.get_event_loop().time()
        )
        replied = await hunter._maybe_reply(msg, outcome)
        assert replied == "谢谢"
        # 第二次被冷却挡住
        outcome2 = await hunter._execute(
            msg, hunter._find_button(msg), None, "button", asyncio.get_event_loop().time()
        )
        replied2 = await hunter._maybe_reply(msg, outcome2)
        assert replied2 is None


class TestChatEventBus:
    @pytest.mark.asyncio
    async def test_feed_reaches_watcher(self):
        bus = ChatEventBus()
        async with bus.watch(1) as queue:
            bus.feed(1, "m1")
            msg = await asyncio.wait_for(queue.get(), timeout=1)
            assert msg == "m1"

    @pytest.mark.asyncio
    async def test_feed_ignored_without_watcher(self):
        bus = ChatEventBus()
        bus.feed(1, "nobody listening")  # 不抛错，不阻塞
        assert bus.watching == 0

    @pytest.mark.asyncio
    async def test_other_chat_not_received(self):
        bus = ChatEventBus()
        async with bus.watch(1) as queue:
            bus.feed(2, "wrong chat")
            bus.feed(1, "right chat")
            msg = await asyncio.wait_for(queue.get(), timeout=1)
            assert msg == "right chat"

    @pytest.mark.asyncio
    async def test_watcher_cleanup(self):
        bus = ChatEventBus()
        async with bus.watch(1):
            assert bus.watching == 1
        assert bus.watching == 0


class TestEndToEnd:
    """模拟完整流程：收到红包消息 → 检测 → 点击 → 判定 → 回复 → 通知。"""

    @pytest.mark.asyncio
    async def test_happy_path(self, alog):
        client = FakeClient(callback_answer="恭喜抢到 10 元")
        hunter = RedPacketHunter(
            client,
            rp_config(
                reply={"enabled": True, "texts": ["谢谢大哥"], "delay_range": [0, 0]}
            ),
            alog,
        )
        await hunter.register()
        hunter._dispatch(
            rp_message("🧧 点击领取", markup=button_markup(("领取", b"grab"))), edited=False
        )
        await asyncio.gather(*list(hunter._tasks))
        assert hunter.stats["detected"] == 1
        assert hunter.stats["success"] == 1
        assert hunter.stats["replied"] == 1

    @pytest.mark.asyncio
    async def test_failed_no_reply(self, alog):
        client = FakeClient(callback_answer="红包已被抢完")
        hunter = RedPacketHunter(
            client,
            rp_config(
                reply={"enabled": True, "texts": ["谢谢大哥"], "delay_range": [0, 0]}
            ),
            alog,
        )
        await hunter.register()
        hunter._dispatch(
            rp_message("🧧", markup=button_markup(("领取", b"grab"))), edited=False
        )
        await asyncio.gather(*list(hunter._tasks))
        assert hunter.stats["failed"] == 1
        assert hunter.stats["replied"] == 0

    @pytest.mark.asyncio
    async def test_seen_cache_prevents_double_grab(self, alog):
        client = FakeClient(callback_answer="抢到了")
        hunter = RedPacketHunter(client, rp_config(), alog)
        await hunter.register()
        msg = rp_message("🧧", markup=button_markup(("领取", b"grab")))
        hunter._dispatch(msg, edited=False)
        hunter._dispatch(msg, edited=False)
        await asyncio.gather(*list(hunter._tasks))
        assert hunter.stats["detected"] == 1

    @pytest.mark.asyncio
    async def test_close_clears_handlers(self, alog):
        client = FakeClient()
        hunter = RedPacketHunter(client, rp_config(), alog)
        await hunter.register()
        assert client.handlers
        await hunter.close()
        assert client.handlers == []
