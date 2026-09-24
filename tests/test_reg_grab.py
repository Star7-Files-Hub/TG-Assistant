"""抢注任务：配置校验、注册码提取、步骤链执行、去重、通知。"""

from __future__ import annotations

import asyncio
import random
import re
import time

import pytest
from pydantic import ValidationError

from tg_assistant.config import AccountConfig, RegGrabConfig, RegGrabStep
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
