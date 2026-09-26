"""抢注任务：配置校验、注册码提取、步骤链执行、去重、通知。"""

from __future__ import annotations

import asyncio
import random
import re
import time
from datetime import datetime

import pytest
from pydantic import ValidationError

from tg_assistant.config import (
    AccountConfig,
    RegGrabConfig,
    RegGrabStep,
    RegGrabWindow,
)
from tg_assistant.reg_grab import (
    ChainResult,
    RegGrabHunter,
    StepResult,
    code_value_of,
    iter_inline_buttons,
    step_label,
    visible_code_part,
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
BOT_CHAT = -1001234500000
CODE = "MSKY-30-Register_Ex5I0Fx5Bg"
CODE_VALUE = "Ex5I0Fx5Bg"
PATTERN = r"Register_([A-Za-z0-9]{10})"


class FakeNotifier:
    """只记录 submit 的内容，不真的发通知。"""

    def __init__(self) -> None:
        self.tasks: list[object] = []

    def submit(self, task: object) -> bool:
        self.tasks.append(task)
        return True


def rg_config(**overrides) -> AccountConfig:
    base: dict = {
        "reg_grab": {
            "enabled": True,
            "detect": {"code_pattern": PATTERN},
            "steps": [{"type": "send", "text": "/bind {code}", "chat": "@testbot"}],
            "delay": 0,
            "jitter": 0,
        }
    }
    base["reg_grab"].update(overrides)
    return AccountConfig.model_validate(base)


def rg_message(text: str = CODE, *, chat=None, markup=None, sender=None, **kwargs):
    kwargs.setdefault("chat", chat or FakeChat(CHAT, title="码群"))
    kwargs.setdefault("markup", markup)
    if sender is not None:
        kwargs["sender"] = sender
    return make_message(text, **kwargs)


def bot_client(**kwargs) -> FakeClient:
    """登记好 ``@testbot → BOT_CHAT`` 的客户端。"""
    client = FakeClient(**kwargs)
    client.chat_map["testbot"] = FakeChat(BOT_CHAT, title="TestBot")
    return client


def button_markup(*buttons: tuple[str, bytes | None]):
    return FakeMarkup([[FakeButton(text, callback_data=data) for text, data in buttons]])


def build(config: AccountConfig, client=None, alog=None, notifier=None) -> RegGrabHunter:
    return RegGrabHunter(client or bot_client(), config, alog, notifier)


# --------------------------------------------------------------------------- #
class TestConfig:
    """配置模型：该拦的拦住，不该拦的别拦。"""

    def test_default_is_disabled_and_not_ready(self) -> None:
        config = AccountConfig()
        assert config.reg_grab.enabled is False
        assert config.reg_grab.ready is False

    def test_ready_requires_pattern_and_steps(self) -> None:
        assert RegGrabConfig(enabled=True, detect={"code_pattern": PATTERN}).ready is False
        assert (
            RegGrabConfig(
                enabled=True,
                detect={"code_pattern": PATTERN},
                steps=[{"type": "wait", "seconds": 1}],
            ).ready
            is True
        )
        assert RegGrabConfig(enabled=False, detect={"code_pattern": PATTERN}).ready is False

    def test_send_step_requires_text(self) -> None:
        with pytest.raises(ValidationError, match="发送内容"):
            RegGrabStep(type="send", chat="@bot")

    def test_click_step_requires_button(self) -> None:
        with pytest.raises(ValidationError, match="按钮文字"):
            RegGrabStep(type="click")

    def test_invalid_code_pattern_rejected(self) -> None:
        with pytest.raises(ValidationError, match="正则无效"):
            RegGrabConfig(detect={"code_pattern": "([unclosed"})

    def test_invalid_step_button_regex_rejected(self) -> None:
        with pytest.raises(ValidationError, match="正则无效"):
            RegGrabStep(type="click", button="[unclosed")

    def test_used_pattern_must_be_valid_regex(self) -> None:
        with pytest.raises(ValidationError, match="正则无效"):
            RegGrabConfig(detect={"code_pattern": PATTERN, "used_pattern": "([unclosed"})

    def test_used_pattern_has_a_default(self) -> None:
        """开箱即用：不填也能认出「使用了 XXX」这种通知。"""
        assert RegGrabConfig().detect.used_pattern == r"使用[了]?\s*([A-Za-z0-9][^\s，。、]*)"

    def test_default_used_pattern_skips_the_header_word(self) -> None:
        """🔴 回归：通知里 ``使用`` 出现两次，标题那个「注册码使用 - jf」不能被抓成码。

        默认正则若写成 ``使用[了]?\\s*(\\S+)``，第一个匹配就是标题里的 ``-``，
        可见部分为空 —— 虽然会被 ``used_min_len`` 挡掉，但那是撞运气，不是设计。
        """
        pattern = re.compile(RegGrabConfig().detect.used_pattern)
        tokens = [next((g for g in m.groups() if g), m.group(0)) for m in pattern.finditer(USAGE_NOTICE)]
        assert tokens == ["MSKY-30-Register_f1t░░░░░░░"], tokens

    def test_blank_used_pattern_disables_the_check(self) -> None:
        assert RegGrabConfig(detect={"used_pattern": "   "}).detect.used_pattern is None

    def test_used_min_len_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            RegGrabConfig(detect={"used_min_len": 0})

    def test_blank_strings_become_none(self) -> None:
        step = RegGrabStep(type="wait", text="   ", button="", pattern="")
        assert step.text is None and step.button is None and step.pattern is None

    def test_step_chat_is_normalized(self) -> None:
        assert RegGrabStep(type="send", text="x", chat="@MyBot").chat == "mybot"
        assert RegGrabStep(type="send", text="x", chat="-1001234567890").chat == -1001234567890
        assert RegGrabStep(type="send", text="x", chat="https://t.me/c/123/5").chat == -100123

    def test_chats_are_normalized(self) -> None:
        config = RegGrabConfig(chats=["@MyGroup", -1001234567890])
        assert config.chats == ["mygroup", -1001234567890]

    def test_enabled_toggle_does_not_raise_on_incomplete_config(self) -> None:
        """面板总开关就是一次赋值 —— 模型校验不能把它拦死（否则 500）。"""
        config = AccountConfig()
        config.reg_grab.enabled = True
        assert config.reg_grab.enabled is True
        assert config.reg_grab.ready is False

    def test_needs_updates_and_watched_chats_include_reg_grab(self) -> None:
        config = AccountConfig.model_validate(
            {"reg_grab": {"enabled": True, "chats": [-1001234567890]}}
        )
        assert config.needs_updates is True
        assert -1001234567890 in config.watched_chats()

    def test_empty_chats_means_watch_everything(self) -> None:
        config = AccountConfig.model_validate({"reg_grab": {"enabled": True, "chats": []}})
        assert config.watched_chats() == []


# --------------------------------------------------------------------------- #
class TestDetection:
    def test_extracts_capture_group(self, alog) -> None:
        hunter = build(rg_config(), alog=alog)
        assert hunter._should_grab(rg_message(), CHAT) == CODE_VALUE

    def test_multi_line_code_list_is_detected(self, alog) -> None:
        """回归：多行码列表里的第一条也要能提取到。

        这正是线上漏掉 ``MSKY-30-Register_Ex5I0Fx5Bg`` 的那种消息形态。
        """
        hunter = build(rg_config(), alog=alog)
        text = "MSKY-30-Register_Ex5I0Fx5Bg\nMSKY-30-Register_JbrMDOxx38"
        assert hunter._should_grab(rg_message(text), CHAT) == CODE_VALUE

    def test_whole_match_when_no_capture_group(self, alog) -> None:
        hunter = build(rg_config(detect={"code_pattern": r"[A-Z]+-\d+-\w+"}), alog=alog)
        assert hunter._should_grab(rg_message(CODE), CHAT) == CODE

    def test_no_code_returns_none(self, alog) -> None:
        hunter = build(rg_config(), alog=alog)
        assert hunter._should_grab(rg_message("今天天气不错"), CHAT) is None

    def test_exclude_chats_blocks(self, alog) -> None:
        hunter = build(rg_config(exclude_chats=[CHAT]), alog=alog)
        assert hunter._should_grab(rg_message(), CHAT) is None

    def test_chats_whitelist_blocks_other_chats(self, alog) -> None:
        hunter = build(rg_config(chats=[-1007777777777]), alog=alog)
        assert hunter._should_grab(rg_message(), CHAT) is None

    def test_ignore_self_blocks_own_message(self, alog) -> None:
        hunter = build(rg_config(), alog=alog)
        msg = rg_message(sender=FakeUser(1, username="me", is_self=True))
        assert hunter._should_grab(msg, CHAT) is None

    def test_only_from_bots(self, alog) -> None:
        hunter = build(rg_config(detect={"code_pattern": PATTERN, "only_from_bots": True}), alog=alog)
        assert hunter._should_grab(rg_message(), CHAT) is None
        bot_msg = rg_message(sender=FakeUser(9, is_bot=True))
        assert hunter._should_grab(bot_msg, CHAT) == CODE_VALUE

    def test_text_patterns_prefilter(self, alog) -> None:
        hunter = build(
            rg_config(detect={"code_pattern": PATTERN, "text_patterns": ["注册码"]}), alog=alog
        )
        assert hunter._should_grab(rg_message(), CHAT) is None
        assert hunter._should_grab(rg_message("新的注册码 " + CODE), CHAT) == CODE_VALUE

    def test_service_message_ignored(self, alog) -> None:
        hunter = build(rg_config(), alog=alog)
        msg = rg_message(service=object())
        assert hunter._should_grab(msg, CHAT) is None

    def test_watch_chats_includes_step_targets(self, alog) -> None:
        """步骤链里点名的机器人私聊也要收得到消息，否则 wait_reply 永远等不到回执。"""
        hunter = build(rg_config(chats=[CHAT]), alog=alog)
        assert hunter._watch_chats() == [CHAT, "testbot"]

    def test_watch_chats_empty_means_no_filter(self, alog) -> None:
        hunter = build(rg_config(chats=[]), alog=alog)
        assert hunter._watch_chats() == []


# --------------------------------------------------------------------------- #
class TestSteps:
    """步骤链的每一步。"""

    @pytest.mark.asyncio
    async def test_send_renders_code_and_targets_chat(self, alog) -> None:
        client = bot_client()
        hunter = build(rg_config(), client=client, alog=alog)
        outcome = await hunter._run(rg_message(), CODE_VALUE, time.perf_counter())

        assert outcome.result is ChainResult.SUCCESS
        assert len(client.sent) == 1
        assert client.sent[0]["text"] == f"/bind {CODE_VALUE}"
        assert client.sent[0]["chat_id"] == BOT_CHAT

    @pytest.mark.asyncio
    async def test_send_without_chat_goes_to_source_chat(self, alog) -> None:
        client = FakeClient()
        hunter = build(rg_config(steps=[{"type": "send", "text": "收到 {code}"}]), client=client, alog=alog)
        await hunter._run(rg_message(), CODE_VALUE, time.perf_counter())
        assert client.sent[0]["chat_id"] == CHAT

    @pytest.mark.asyncio
    async def test_send_falls_back_when_chat_cannot_be_resolved(self, alog) -> None:
        """解析不到会话也要把消息发出去 —— 只发不等的链照样能用。"""
        client = FakeClient()
        hunter = build(rg_config(), client=client, alog=alog)
        outcome = await hunter._run(rg_message(), CODE_VALUE, time.perf_counter())
        assert outcome.result is ChainResult.SUCCESS
        assert client.sent[0]["chat_id"] == "testbot"

    @pytest.mark.asyncio
    async def test_wait_step_sleeps(self, alog) -> None:
        client = FakeClient()
        hunter = build(rg_config(steps=[{"type": "wait", "seconds": 0.05}]), client=client, alog=alog)
        started = time.perf_counter()
        outcome = await hunter._run(rg_message(), CODE_VALUE, started)
        assert outcome.result is ChainResult.SUCCESS
        assert outcome.cost_ms >= 50

    @pytest.mark.asyncio
    async def test_wait_reply_matches_bot_reply(self, alog) -> None:
        client = bot_client()
        hunter = build(
            rg_config(
                steps=[
                    {"type": "send", "text": "/bind {code}", "chat": "@testbot"},
                    {"type": "wait_reply", "pattern": "成功", "timeout": 1.0},
                ]
            ),
            client=client,
            alog=alog,
        )

        async def feed() -> None:
            await asyncio.sleep(0.05)
            hunter._bus.feed(
                BOT_CHAT,
                make_message(
                    "✅ 绑定成功", chat=FakeChat(BOT_CHAT, title="TestBot"), sender=FakeUser(9, is_bot=True)
                ),
            )

        feeder = asyncio.create_task(feed())
        outcome = await hunter._run(rg_message(), CODE_VALUE, time.perf_counter())
        await feeder

        assert outcome.result is ChainResult.SUCCESS
        assert [step.type for step in outcome.steps] == ["send", "wait_reply"]

    @pytest.mark.asyncio
    async def test_wait_reply_ignores_own_messages(self, alog) -> None:
        """自己发出去的 ``/bind MSKY-...`` 不能被当成机器人的回执。"""
        client = bot_client()
        hunter = build(
            rg_config(
                steps=[
                    {"type": "send", "text": "/bind {code}", "chat": "@testbot"},
                    {"type": "wait_reply", "pattern": "Ex5I0Fx5Bg", "timeout": 0.25},
                ]
            ),
            client=client,
            alog=alog,
        )

        async def feed() -> None:
            await asyncio.sleep(0.05)
            hunter._bus.feed(
                BOT_CHAT,
                make_message(
                    f"/bind {CODE}",
                    chat=FakeChat(BOT_CHAT, title="TestBot"),
                    sender=FakeUser(1, username="me", is_self=True),
                ),
            )

        feeder = asyncio.create_task(feed())
        outcome = await hunter._run(rg_message(), CODE_VALUE, time.perf_counter())
        await feeder

        assert outcome.result is ChainResult.FAILED
        assert "没有等到匹配" in outcome.steps[-1].detail

    @pytest.mark.asyncio
    async def test_wait_reply_timeout_fails_chain(self, alog) -> None:
        client = bot_client()
        hunter = build(
            rg_config(
                steps=[
                    {"type": "send", "text": "/bind {code}", "chat": "@testbot"},
                    {"type": "wait_reply", "pattern": "成功", "timeout": 0.1},
                ]
            ),
            client=client,
            alog=alog,
        )
        outcome = await hunter._run(rg_message(), CODE_VALUE, time.perf_counter())
        assert outcome.result is ChainResult.FAILED
        assert outcome.steps[-1].result is StepResult.FAILED

    @pytest.mark.asyncio
    async def test_click_uses_current_message(self, alog) -> None:
        client = bot_client(callback_answer="领取成功")
        hunter = build(
            rg_config(steps=[{"type": "click", "button": "领取", "chat": "@testbot"}]),
            client=client,
            alog=alog,
        )
        msg = rg_message(markup=button_markup(("🎁 领取", b"claim")))
        outcome = await hunter._run(msg, CODE_VALUE, time.perf_counter())

        assert outcome.result is ChainResult.SUCCESS
        assert client.callbacks
        assert client.callbacks[0]["callback_data"] == b"claim"

    @pytest.mark.asyncio
    async def test_click_falls_back_to_recent_message_in_target_chat(self, alog) -> None:
        """当前消息没按钮时，退回目标会话最近一条带按钮的消息。"""
        client = bot_client(callback_answer="ok")
        hunter = build(
            rg_config(
                steps=[
                    {"type": "send", "text": "/bind {code}", "chat": "@testbot"},
                    {"type": "click", "button": "确认"},
                ]
            ),
            client=client,
            alog=alog,
        )
        # 机器人先回了一条带按钮的消息（经过 handler 就会进 _recent）
        hunter._dispatch(
            make_message(
                "请确认",
                chat=FakeChat(BOT_CHAT, title="TestBot"),
                sender=FakeUser(9, is_bot=True),
                markup=button_markup(("✅ 确认绑定", b"confirm")),
            ),
            edited=False,
        )
        outcome = await hunter._run(rg_message(), CODE_VALUE, time.perf_counter())

        assert outcome.result is ChainResult.SUCCESS
        assert client.callbacks[0]["callback_data"] == b"confirm"

    @pytest.mark.asyncio
    async def test_click_without_any_button_fails(self, alog) -> None:
        client = bot_client()
        hunter = build(
            rg_config(steps=[{"type": "click", "button": "领取", "chat": "@testbot"}]),
            client=client,
            alog=alog,
        )
        outcome = await hunter._run(rg_message(), CODE_VALUE, time.perf_counter())
        assert outcome.result is ChainResult.FAILED
        assert "按钮" in outcome.steps[0].detail

    @pytest.mark.asyncio
    async def test_optional_step_failure_is_partial(self, alog) -> None:
        client = bot_client()
        hunter = build(
            rg_config(
                steps=[
                    {"type": "click", "button": "领取", "chat": "@testbot", "optional": True},
                    {"type": "send", "text": "兜底 {code}", "chat": "@testbot"},
                ]
            ),
            client=client,
            alog=alog,
        )
        outcome = await hunter._run(rg_message(), CODE_VALUE, time.perf_counter())

        assert outcome.result is ChainResult.PARTIAL
        assert len(outcome.steps) == 2
        assert client.sent[0]["text"] == f"兜底 {CODE_VALUE}"

    @pytest.mark.asyncio
    async def test_required_step_failure_aborts_chain(self, alog) -> None:
        client = bot_client()
        hunter = build(
            rg_config(
                steps=[
                    {"type": "click", "button": "领取", "chat": "@testbot"},
                    {"type": "send", "text": "兜底 {code}", "chat": "@testbot"},
                ]
            ),
            client=client,
            alog=alog,
        )
        outcome = await hunter._run(rg_message(), CODE_VALUE, time.perf_counter())

        assert outcome.result is ChainResult.FAILED
        assert len(outcome.steps) == 1
        assert client.sent == []

    @pytest.mark.asyncio
    async def test_step_delay_is_honoured(self, alog) -> None:
        client = bot_client()
        hunter = build(
            rg_config(steps=[{"type": "send", "text": "{code}", "chat": "@testbot", "delay": 0.05}]),
            client=client,
            alog=alog,
        )
        started = time.perf_counter()
        await hunter._run(rg_message(), CODE_VALUE, started)
        assert time.perf_counter() - started >= 0.05


# --------------------------------------------------------------------------- #
class TestDedupe:
    """同一条码只抢一次。"""

    @pytest.mark.asyncio
    async def test_same_code_twice_only_runs_once(self, alog) -> None:
        client = bot_client()
        hunter = build(rg_config(), client=client, alog=alog)
        msg = rg_message()
        hunter._dispatch(msg, edited=False)
        hunter._dispatch(msg, edited=False)
        await asyncio.gather(*list(hunter._tasks))

        assert hunter.stats["detected"] == 1
        assert hunter.stats["duplicate_code"] == 1
        assert len(client.sent) == 1

    @pytest.mark.asyncio
    async def test_different_codes_both_run(self, alog) -> None:
        client = bot_client()
        hunter = build(rg_config(), client=client, alog=alog)
        hunter._dispatch(rg_message("MSKY-30-Register_Ex5I0Fx5Bg"), edited=False)
        hunter._dispatch(rg_message("MSKY-30-Register_JbrMDOxx38"), edited=False)
        await asyncio.gather(*list(hunter._tasks))

        assert hunter.stats["detected"] == 2
        assert len(client.sent) == 2

    @pytest.mark.asyncio
    async def test_code_ttl_zero_disables_dedupe(self, alog) -> None:
        client = bot_client()
        hunter = build(rg_config(code_ttl=0), client=client, alog=alog)
        msg = rg_message()
        hunter._dispatch(msg, edited=False)
        hunter._dispatch(msg, edited=False)
        await asyncio.gather(*list(hunter._tasks))

        assert hunter.stats["detected"] == 2

    @pytest.mark.asyncio
    async def test_same_code_in_two_chats_only_runs_once(self, alog) -> None:
        """同一条码常被多个群同时转发出来。"""
        client = bot_client()
        hunter = build(rg_config(), client=client, alog=alog)
        hunter._dispatch(rg_message(chat=FakeChat(CHAT, title="码群A")), edited=False)
        hunter._dispatch(rg_message(chat=FakeChat(-1005555555555, title="码群B")), edited=False)
        await asyncio.gather(*list(hunter._tasks))

        assert hunter.stats["duplicate_code"] == 1
        assert len(client.sent) == 1


# --------------------------------------------------------------------------- #
class TestNotify:
    @pytest.mark.asyncio
    async def test_notify_submitted_with_reg_grab_event(self, alog) -> None:
        notifier = FakeNotifier()
        hunter = build(rg_config(), alog=alog, notifier=notifier)
        await hunter._run(rg_message(), CODE_VALUE, time.perf_counter())

        assert len(notifier.tasks) == 1
        task = notifier.tasks[0]
        assert task.event == "reg_grab"
        assert CODE_VALUE in task.text

    @pytest.mark.asyncio
    async def test_notify_disabled_submits_nothing(self, alog) -> None:
        notifier = FakeNotifier()
        hunter = build(rg_config(notify=False), alog=alog, notifier=notifier)
        await hunter._run(rg_message(), CODE_VALUE, time.perf_counter())
        assert notifier.tasks == []


# --------------------------------------------------------------------------- #
#: 线上真实形态的使用通知：尾部被遮罩，只露出 3 位。
USAGE_NOTICE = "🎟️ 注册码使用 - jf [7002057019] 使用了 MSKY-30-Register_f1t░░░░░░░"


class TestUsageNotice:
    """「已被使用」通知判定：对得上的码直接剔除。"""

    def test_visible_code_part_strips_trailing_mask(self) -> None:
        assert visible_code_part("MSKY-30-Register_f1t░░░░░░░") == "MSKY-30-Register_f1t"
        assert visible_code_part("MSKY-30-Register_f1t。") == "MSKY-30-Register_f1t"
        # 没有遮罩时原样返回
        assert visible_code_part("MSKY-30-Register_f1tAbCdEfGh") == "MSKY-30-Register_f1tAbCdEfGh"

    def test_code_value_of_takes_last_segment(self) -> None:
        """两边必须按同一口径切：通知带前缀、捕获组不带，切完都要落到码值上。"""
        assert code_value_of("MSKY-30-Register_f1t") == "f1t"
        assert code_value_of("f1tAbCdEfGh") == "f1tabcdefgh"
        assert code_value_of("Register_f1tAbCdEfGh") == "f1tabcdefgh"

    def test_notice_is_recorded(self, alog) -> None:
        hunter = build(rg_config(), alog=alog)
        hunter._dispatch(rg_message(USAGE_NOTICE), edited=False)

        assert hunter.stats["usage_notices"] == 1
        assert hunter._is_used("f1tAbCdEfGh") == "f1t"
        # 通知本身提不出码（尾部被遮罩），不该被当成一条要抢的码
        assert hunter.stats["detected"] == 0

    def test_matching_code_is_dropped(self, alog) -> None:
        client = bot_client()
        hunter = build(rg_config(), client=client, alog=alog)
        hunter._dispatch(rg_message(USAGE_NOTICE), edited=False)
        hunter._dispatch(rg_message("MSKY-30-Register_f1tAbCdEfGh"), edited=False)

        assert hunter.stats["used_skipped"] == 1
        assert hunter.stats["detected"] == 0
        assert client.sent == [], "已经被用掉的码不该再跑步骤链"

    @pytest.mark.asyncio
    async def test_other_code_still_runs(self, alog) -> None:
        client = bot_client()
        hunter = build(rg_config(), client=client, alog=alog)
        hunter._dispatch(rg_message(USAGE_NOTICE), edited=False)
        hunter._dispatch(rg_message("MSKY-30-Register_Ex5I0Fx5Bg"), edited=False)
        await asyncio.gather(*list(hunter._tasks))

        assert hunter.stats["used_skipped"] == 0
        assert hunter.stats["success"] == 1

    def test_short_visible_part_is_ignored(self, alog) -> None:
        """可见位数不够就不判定 —— 只露 1~2 位时几乎任何码都能「对得上」。"""
        hunter = build(
            rg_config(detect={"code_pattern": PATTERN, "used_min_len": 5}), alog=alog
        )
        hunter._dispatch(rg_message(USAGE_NOTICE), edited=False)

        assert hunter._is_used("f1tAbCdEfGh") is None

    def test_disabled_when_pattern_blank(self, alog) -> None:
        hunter = build(rg_config(detect={"code_pattern": PATTERN, "used_pattern": None}), alog=alog)
        hunter._dispatch(rg_message(USAGE_NOTICE), edited=False)

        assert hunter.stats["usage_notices"] == 0
        assert hunter._is_used("f1tAbCdEfGh") is None

    @pytest.mark.asyncio
    async def test_notice_during_delay_aborts_chain(self, alog) -> None:
        """反脚本延迟正好是检测窗口：延迟期间冒出通知，链在第一步之前就刹车。"""
        client = bot_client()
        hunter = build(rg_config(delay=0.2, jitter=0), client=client, alog=alog)

        async def feed_notice() -> None:
            await asyncio.sleep(0.05)
            hunter._dispatch(rg_message(USAGE_NOTICE), edited=False)

        feeder = asyncio.create_task(feed_notice())
        outcome = await hunter._run(
            rg_message("MSKY-30-Register_f1tAbCdEfGh"), "f1tAbCdEfGh", time.perf_counter()
        )
        await feeder

        assert outcome.result is ChainResult.SKIPPED
        assert "使用通知" in outcome.detail
        assert client.sent == []
        assert hunter.stats["used_skipped"] == 1

    @pytest.mark.asyncio
    async def test_skip_does_not_notify(self, alog) -> None:
        """码被别人用掉是预期内的事，推给用户纯属噪音。"""
        notifier = FakeNotifier()
        client = bot_client()
        hunter = build(
            rg_config(delay=0.2, jitter=0), client=client, alog=alog, notifier=notifier
        )

        async def feed_notice() -> None:
            await asyncio.sleep(0.05)
            hunter._dispatch(rg_message(USAGE_NOTICE), edited=False)

        feeder = asyncio.create_task(feed_notice())
        await hunter._run(
            rg_message("MSKY-30-Register_f1tAbCdEfGh"), "f1tAbCdEfGh", time.perf_counter()
        )
        await feeder

        assert notifier.tasks == []


# --------------------------------------------------------------------------- #
def at(hour: int, minute: int = 0) -> datetime:
    """构造一个「本地时间」用于时段判定 —— 只看时分，日期无关紧要。"""
    return datetime(2026, 1, 1, hour, minute)


class TestWindow:
    """监听时段：只在人类活动时段动手，避免半夜秒抢暴露脚本。"""

    def test_disabled_by_default_means_all_day(self) -> None:
        window = RegGrabWindow()
        assert window.enabled is False
        assert window.describe() == "全天"
        for hour in (0, 3, 12, 23):
            assert window.contains(at(hour)) is True, "开关关着 ⇒ 任何时刻都允许"

    def test_normal_range_is_left_closed_right_open(self) -> None:
        window = RegGrabWindow(enabled=True, start="08:00", end="23:00")
        assert window.contains(at(7, 59)) is False
        assert window.contains(at(8, 0)) is True, "起点含在内"
        assert window.contains(at(22, 59)) is True
        assert window.contains(at(23, 0)) is False, "终点不含 —— 否则相邻两段会重叠"

    def test_adjacent_ranges_do_not_overlap(self) -> None:
        morning = RegGrabWindow(enabled=True, start="08:00", end="09:00")
        noon = RegGrabWindow(enabled=True, start="09:00", end="10:00")
        assert morning.contains(at(9, 0)) is False
        assert noon.contains(at(9, 0)) is True

    def test_crossing_midnight(self) -> None:
        """``start > end`` ⇒ 跨零点（如 22:00 ~ 06:00）。"""
        window = RegGrabWindow(enabled=True, start="22:00", end="06:00")
        assert window.contains(at(23, 30)) is True
        assert window.contains(at(0, 0)) is True
        assert window.contains(at(5, 59)) is True
        assert window.contains(at(6, 0)) is False
        assert window.contains(at(12, 0)) is False
        assert "跨零点" in window.describe()

    def test_start_equal_end_is_rejected(self) -> None:
        """🔴 不许用「相等」猜语义 —— 想全天就关开关，别让人以为配上了。"""
        with pytest.raises(ValidationError, match="不能相同"):
            RegGrabWindow(enabled=True, start="08:00", end="08:00")

    def test_bad_format_is_rejected_loudly(self) -> None:
        """🔴 不静默兜底：解析失败就当 0 点的话，配错了会「看起来一切正常」。"""
        for bad in ("8点", "晚上8点", "", "25:00", "08:70", "0800"):
            with pytest.raises(ValidationError):
                RegGrabWindow(start=bad)

    def test_clock_is_normalized(self) -> None:
        assert RegGrabWindow(start="8:5").start == "08:05"
        assert RegGrabWindow(start="8：00", end="23:00").start == "08:00", "中文冒号也认"
        assert RegGrabWindow(start= 8 * 60, end=23 * 60).start == "08:00", "纯分钟数也认"

    def test_window_default_in_config_is_off(self) -> None:
        config = RegGrabConfig()
        assert config.window.enabled is False
        assert config.in_window is True

    def test_ready_ignores_the_window(self) -> None:
        """``ready`` 是「配好了没有」，不是「现在能不能动手」—— 两者混在一起，
        面板会出现「明明配好了却显示未就绪」，而且随着时间跳变。"""
        config = RegGrabConfig(
            enabled=True,
            detect={"code_pattern": PATTERN},
            steps=[{"type": "wait", "seconds": 1}],
            window={"enabled": True, "start": "08:00", "end": "09:00"},
        )
        assert config.ready is True
        assert config.window.contains(at(3)) is False, "凌晨不在时段内"


# --------------------------------------------------------------------------- #
class TestWindowInEngine:
    """引擎层：时段外只看着不动，而且**不占去重名额**。"""

    @staticmethod
    def _at(hunter, hour: int, minute: int = 0):
        hunter._now = lambda: at(hour, minute)
        return hunter

    def test_outside_window_does_not_grab(self, alog) -> None:
        client = bot_client()
        hunter = self._at(
            build(rg_config(window={"enabled": True, "start": "08:00", "end": "23:00"}),
                  client=client, alog=alog),
            3,
        )
        hunter._dispatch(rg_message(), edited=False)

        assert hunter.stats["outside_window"] == 1
        assert hunter.stats["detected"] == 0, "时段外连「发现」都不该记"
        assert client.sent == [], "半夜不该动手"
        assert list(hunter._tasks) == []

    @pytest.mark.asyncio
    async def test_inside_window_still_grabs(self, alog) -> None:
        client = bot_client()
        hunter = self._at(
            build(rg_config(window={"enabled": True, "start": "08:00", "end": "23:00"}),
                  client=client, alog=alog),
            12,
        )
        hunter._dispatch(rg_message(), edited=False)
        await asyncio.gather(*list(hunter._tasks))

        assert hunter.stats["outside_window"] == 0
        assert hunter.stats["detected"] == 1
        assert hunter.stats["success"] == 1, "白天照常抢"

    @pytest.mark.asyncio
    async def test_disabled_window_grabs_around_the_clock(self, alog) -> None:
        """开关关着 ⇒ 任何时刻都照抢（不改变老行为）。"""
        client = bot_client()
        hunter = self._at(build(rg_config(), client=client, alog=alog), 3)
        hunter._dispatch(rg_message(), edited=False)
        await asyncio.gather(*list(hunter._tasks))
        assert hunter.stats["detected"] == 1

    @pytest.mark.asyncio
    async def test_outside_window_does_not_consume_the_dedupe_slot(self, alog) -> None:
        """🔴 时段外**不能**把「同码去重」名额占掉。

        占了的话，时段内同一条码再来时会被当成重复而跳过 —— 白等一整天。
        """
        config = rg_config(window={"enabled": True, "start": "08:00", "end": "23:00"})
        client = bot_client()
        hunter = self._at(build(config, client=client, alog=alog), 3)
        hunter._dispatch(rg_message(), edited=False)
        assert hunter.stats["outside_window"] == 1
        assert hunter._seen_codes == {}, "时段外不许记去重名额"

        self._at(hunter, 12)
        hunter._dispatch(rg_message(), edited=False)
        await asyncio.gather(*list(hunter._tasks))
        assert hunter.stats["detected"] == 1, "时段内同一条码必须还能抢"

    @pytest.mark.asyncio
    async def test_window_closing_during_delay_aborts_chain(self, alog) -> None:
        """延迟把动手时刻推到了时段之外（22:59:59 派发、23:00:01 才跑）⇒ 放弃。

        抢注的价值全在「准点」，多等 2 秒也抢不到；但半夜动手会留下脚本痕迹。
        """
        client = bot_client()
        hunter = build(
            rg_config(delay=0.2, jitter=0, window={"enabled": True, "start": "08:00", "end": "23:00"}),
            client=client, alog=alog,
        )
        hunter._now = lambda: at(22, 59)

        async def close_window() -> None:
            await asyncio.sleep(0.05)
            hunter._now = lambda: at(23, 0)

        closer = asyncio.create_task(close_window())
        outcome = await hunter._run(rg_message(), CODE_VALUE, time.perf_counter())
        await closer

        assert outcome.result is ChainResult.SKIPPED
        assert "监听时段" in outcome.detail
        assert client.sent == [], "时段外不许动手"
        assert hunter.stats["outside_window"] == 1

    def test_snapshot_exposes_the_window_state(self, alog) -> None:
        """面板/CLI 靠这两个字段解释「为什么现在没动静」。"""
        hunter = self._at(
            build(rg_config(window={"enabled": True, "start": "08:00", "end": "23:00"}), alog=alog),
            3,
        )
        snap = hunter.snapshot()
        assert snap["in_window"] is False
        assert snap["window"] == "08:00~23:00"
        assert snap["outside_window"] == 0

    @pytest.mark.asyncio
    async def test_register_logs_the_window(self) -> None:
        """注册那行必须带上时段与「此刻在不在时段内」—— 排查「为什么没动静」全看它。"""
        lines: list[str] = []

        class _Log:
            def bind(self, *args, **kwargs):
                return self

            def info(self, msg, **kw):
                lines.append(msg + " " + " ".join(f"{k}={v}" for k, v in kw.items()))

            def warning(self, msg, **kw):
                lines.append(msg)

            def error(self, msg, **kw):
                lines.append(msg)

            def debug(self, msg, **kw):
                pass

        hunter = self._at(
            build(rg_config(window={"enabled": True, "start": "22:00", "end": "06:00"}), alog=_Log()),
            3,
        )
        await hunter.register()

        text = "\n".join(lines)
        assert "22:00~06:00（跨零点）" in text
        assert "in_window=True" in text, "凌晨 3 点落在 22:00~06:00 内 ⇒ 应当是 True"
        # 🔴 日志必须和**判定**同源。原来这行读的是 ``self.config.in_window``
        # （真实时钟），而放行与否读的是 ``self._in_window()``（可注入时钟）——
        # 两者在线上一致、在测试里不一致，于是这个用例会**随一天中的时刻时好时坏**：
        # 只有真实时间恰好落在 22:00~06:00 内才通过。把同源性钉死，别再靠运气。
        assert f"in_window={hunter._in_window()}" in text, (
            "日志里的 in_window 必须来自 _in_window()，不能用另一个时钟"
        )


# --------------------------------------------------------------------------- #
class TestDelay:
    """反脚本延迟：默认就该有一点，别秒回。"""

    def test_default_delay_is_not_instant(self) -> None:
        config = RegGrabConfig()
        assert config.delay >= 0.5
        assert config.jitter >= 1.0
        assert config.delay + config.jitter >= 1.5

    def test_jitter_makes_delay_vary(self) -> None:
        """两次的延迟不能一模一样 —— 整齐一致本身就是机器人特征。"""
        seen = {
            round(
                RegGrabConfig(delay=0.0, jitter=5.0).delay
                + random.uniform(0, RegGrabConfig(delay=0.0, jitter=5.0).jitter),
                6,
            )
            for _ in range(20)
        }
        assert len(seen) > 1

    @pytest.mark.asyncio
    async def test_delay_is_honoured(self, alog) -> None:
        client = bot_client()
        hunter = build(rg_config(delay=0.15, jitter=0), client=client, alog=alog)
        started = time.perf_counter()
        await hunter._run(rg_message(), CODE_VALUE, started)
        assert time.perf_counter() - started >= 0.15


# --------------------------------------------------------------------------- #
class TestRegistration:
    @pytest.mark.asyncio
    async def test_register_adds_handlers(self, alog) -> None:
        client = bot_client()
        hunter = build(rg_config(), client=client, alog=alog)
        await hunter.register()
        assert len(client.handlers) == 2  # 消息 + 编辑
        await hunter.close()
        assert client.handlers == []

    @pytest.mark.asyncio
    async def test_incomplete_config_registers_nothing(self, alog) -> None:
        """开关开着但没配好：不注册 handler，只留一条警告。"""
        client = bot_client()
        hunter = build(rg_config(detect={"code_pattern": None}), client=client, alog=alog)
        await hunter.register()
        assert client.handlers == []

    @pytest.mark.asyncio
    async def test_disabled_registers_nothing(self, alog) -> None:
        client = bot_client()
        hunter = build(rg_config(enabled=False), client=client, alog=alog)
        await hunter.register()
        assert client.handlers == []

    @pytest.mark.asyncio
    async def test_include_edited_false_skips_edited_handler(self, alog) -> None:
        client = bot_client()
        hunter = build(rg_config(include_edited=False), client=client, alog=alog)
        await hunter.register()
        assert len(client.handlers) == 1


# --------------------------------------------------------------------------- #
class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_happy_path(self, alog) -> None:
        client = bot_client()
        notifier = FakeNotifier()
        hunter = build(rg_config(), client=client, alog=alog, notifier=notifier)
        await hunter.register()

        hunter._dispatch(rg_message(), edited=False)
        await asyncio.gather(*list(hunter._tasks))

        assert hunter.stats["detected"] == 1
        assert hunter.stats["success"] == 1
        assert hunter.stats["steps_ok"] == 1
        assert notifier.tasks

    @pytest.mark.asyncio
    async def test_snapshot_has_counters(self, alog) -> None:
        hunter = build(rg_config(), alog=alog)
        snapshot = hunter.snapshot()
        for key in ("detected", "success", "failed", "duplicate_code", "pending_tasks"):
            assert key in snapshot


# --------------------------------------------------------------------------- #
class TestTestNotifyEndpoint:
    """「试发通知」接口：把用户会踩的坑都翻译成中文原因，而不是静默失败。"""

    @staticmethod
    def _app_with_account(tmp_path, **notify_overrides):
        from tg_assistant.config import AccountRecord, utc_now_iso
        from tg_assistant.web import create_app

        app = create_app(tmp_path / "data")
        store = app.state.store
        store.upsert_account(AccountRecord(name="acct", created_at=utc_now_iso()))
        config = store.load_account_config("acct", create=True)
        # ⚠️ 顺序不能反：模型开了 validate_assignment，enabled=True 会立刻校验
        # 「必须有 bot_token」，所以依赖项要先赋值。
        config.notify.bot_token = "123456:TEST-TOKEN"
        config.notify.chat_id = 5608153118
        config.notify.events = ["forward", "red_packet", "reg_grab"]
        config.notify.enabled = True
        for key, value in notify_overrides.items():
            setattr(config.notify, key, value)
        store.save_account_config("acct", config)
        return app, store

    def test_disabled_notify_is_rejected(self, tmp_path) -> None:
        from fastapi.testclient import TestClient

        app, _ = self._app_with_account(tmp_path, enabled=False)
        with TestClient(app) as client:
            res = client.post("/api/config/acct/reg_grab/test_notify")
        assert res.status_code == 400
        assert "通知没启用" in res.json()["detail"]

    def test_missing_event_is_rejected(self, tmp_path) -> None:
        """最常见的坑：机器人配好了，但「抢注通知」那个勾没打。"""
        from fastapi.testclient import TestClient

        app, _ = self._app_with_account(tmp_path, events=["forward"])
        with TestClient(app) as client:
            res = client.post("/api/config/acct/reg_grab/test_notify")
        assert res.status_code == 400
        assert "抢注通知" in res.json()["detail"]

    def test_not_running_is_rejected(self, tmp_path) -> None:
        from fastapi.testclient import TestClient

        app, _ = self._app_with_account(tmp_path)
        with TestClient(app) as client:
            res = client.post("/api/config/acct/reg_grab/test_notify")
        assert res.status_code == 400
        assert "没在运行" in res.json()["detail"]

    def test_get_exposes_the_window_and_the_server_clock(self, tmp_path) -> None:
        """面板要靠 ``server_now`` 对照时区 —— 只给两个时间框，用户本地时区
        不一致时是发现不了的（填 08:00 以为是本地时间，实际按北京时间算）。"""
        from fastapi.testclient import TestClient

        app, _ = self._app_with_account(tmp_path)
        with TestClient(app) as client:
            res = client.get("/api/config/acct/reg_grab")

        assert res.status_code == 200
        data = res.json()
        assert data["window"] == {"enabled": False, "start": "08:00", "end": "23:00"}
        assert data["in_window"] is True, "时段开关没开 ⇒ 恒为「可抢」"
        assert re.fullmatch(r"\d{2}:\d{2}", data["server_now"]), data["server_now"]

    def test_get_then_put_round_trips(self, tmp_path) -> None:
        """🔴 回归：GET 的结果原样 PUT 回去必须成功。

        GET 额外带了 ``ready`` / ``in_window`` / ``server_now`` 三个只读字段，
        而 ``RegGrabConfig`` 是 ``extra="forbid"`` 的 —— 不把它们丢掉就是 400，
        用户只看到「保存失败」，根本猜不到是这三个字段惹的。
        """
        from fastapi.testclient import TestClient

        app, store = self._app_with_account(tmp_path)
        with TestClient(app) as client:
            data = client.get("/api/config/acct/reg_grab").json()
            data["window"] = {"enabled": True, "start": "22:00", "end": "06:00"}
            res = client.put("/api/config/acct/reg_grab", json=data)

        assert res.status_code == 200, res.text
        saved = store.load_account_config("acct", create=False).reg_grab.window
        assert saved.enabled is True
        assert (saved.start, saved.end) == ("22:00", "06:00")

    def test_put_rejects_an_impossible_window(self, tmp_path) -> None:
        """开始 == 结束 ⇒ 400，且提示里要说清「想全天就关开关」。"""
        from fastapi.testclient import TestClient

        app, _ = self._app_with_account(tmp_path)
        with TestClient(app) as client:
            data = client.get("/api/config/acct/reg_grab").json()
            data["window"] = {"enabled": True, "start": "08:00", "end": "08:00"}
            res = client.put("/api/config/acct/reg_grab", json=data)

        assert res.status_code == 400
        assert "不能相同" in res.json()["detail"]

    def test_submits_through_the_running_notifier(self, tmp_path) -> None:
        """有实例在跑时走的是同一个 notifier、同一个 reg_grab 事件。"""
        import types

        from fastapi.testclient import TestClient

        app, _ = self._app_with_account(tmp_path)
        submitted: list[object] = []

        class _Notifier:
            def submit(self, task: object) -> bool:
                submitted.append(task)
                return True

        app.state.runtime.running_runner = lambda name: types.SimpleNamespace(
            notifier=_Notifier()
        )
        with TestClient(app) as client:
            res = client.post("/api/config/acct/reg_grab/test_notify")

        assert res.status_code == 200, res.text
        assert len(submitted) == 1
        assert submitted[0].event == "reg_grab"
        assert "测试" in submitted[0].text


# --------------------------------------------------------------------------- #
class TestHelpers:
    def test_iter_inline_buttons_flattens_rows(self) -> None:
        msg = rg_message(markup=FakeMarkup([[FakeButton("a"), FakeButton("b")], [FakeButton("c")]]))
        assert [button.text for button in iter_inline_buttons(msg)] == ["a", "b", "c"]

    def test_iter_inline_buttons_ignores_reply_keyboard(self) -> None:
        msg = rg_message(markup=FakeMarkup([[FakeButton("a")]], inline=False))
        assert iter_inline_buttons(msg) == []

    def test_step_label_prefers_name(self) -> None:
        assert step_label(RegGrabStep(type="wait", seconds=1)) == "等待"
        assert step_label(RegGrabStep(type="wait", seconds=1, name="等它处理")) == "等它处理"
