"""抢红包引擎：检测、点击、判定、回复。"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from pyrogram.errors import BotResponseTimeout, QueryIdInvalid

from tg_assistant.config import (
    AccountConfig,
    RedPacketSuccess,
    RedPacketTask,
    TimeWindow,
    _ADDED_FAILURE_PATTERNS,
    _DEFAULT_FAILURE_PATTERNS,
    _LEGACY_FAILURE_PATTERNS,
)
from tg_assistant.metrics import MetricsStore
from tg_assistant.red_packet import (
    ChatEventBus,
    GrabOutcome,
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


def rp_multi(*tasks: dict, **overrides) -> AccountConfig:
    """多任务配置。``tasks`` 的顺序**就是优先级**。"""
    base: dict = {"red_packet": {"enabled": True, "tasks": list(tasks)}}
    base["red_packet"].update(overrides)
    return AccountConfig.model_validate(base)


class CapturingLog:
    """把日志收进列表 —— 用来断言"该说的说出来了"。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def bind(self, *args, **kwargs):
        return self

    def _add(self, msg, **kw):
        self.lines.append(msg + " " + " ".join(f"{k}={v}" for k, v in kw.items()))

    def info(self, msg, **kw):
        self._add(msg, **kw)

    def warning(self, msg, **kw):
        self._add(msg, **kw)

    def error(self, msg, **kw):
        self._add(msg, **kw)

    def debug(self, msg, **kw):
        pass

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def task_of(hunter: RedPacketHunter):
    """这条引擎的**第一条任务**。

    引擎里所有动作现在都挂在某条具体任务上（延迟、重试次数、成功判定、
    回复语……全都是任务级的），所以测试里要把「哪条任务」显式带出来。
    这些用例都只配了一条任务。
    """
    return hunter.prepared[0]


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
        assert hunter._find_button(task_of(hunter), msg) is None


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
        assert hunter._classify(task_of(hunter), text) is expected


class TestExecution:
    @pytest.mark.asyncio
    async def test_button_success(self, alog):
        client = FakeClient(callback_answer="恭喜抢到 1 元")
        hunter = RedPacketHunter(client, rp_config(), alog)
        msg = rp_message("红包", markup=button_markup(("领取", b"x")))
        outcome = await hunter._execute(
            task_of(hunter),
            msg,
            hunter._find_button(task_of(hunter), msg),
            None,
            "button",
            asyncio.get_event_loop().time(),
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
            task_of(hunter),
            msg,
            hunter._find_button(task_of(hunter), msg),
            None,
            "button",
            asyncio.get_event_loop().time(),
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
            task_of(hunter),
            msg,
            hunter._find_button(task_of(hunter), msg),
            None,
            "button",
            asyncio.get_event_loop().time(),
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
            task_of(hunter),
            msg,
            hunter._find_button(task_of(hunter), msg),
            None,
            "button",
            asyncio.get_event_loop().time(),
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
        button = hunter._find_button(task_of(hunter), msg)
        assert button is not None
        await hunter._execute(
            task_of(hunter), msg, button, None, "keyboard-text", asyncio.get_event_loop().time()
        )
        assert client.sent
        assert client.sent[0]["text"] == "抢"


class TestReply:
    def test_should_reply_logic(self, alog):
        hunter = RedPacketHunter(
            FakeClient(), rp_config(reply={"only_on_success": True}), alog
        )
        assert hunter._should_reply(task_of(hunter), GrabResult.SUCCESS)
        assert not hunter._should_reply(task_of(hunter), GrabResult.UNKNOWN)
        assert not hunter._should_reply(task_of(hunter), GrabResult.FAILED)

        hunter = RedPacketHunter(
            FakeClient(), rp_config(reply={"only_on_success": False}), alog
        )
        assert hunter._should_reply(task_of(hunter), GrabResult.SUCCESS)
        assert hunter._should_reply(task_of(hunter), GrabResult.UNKNOWN)

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
            task_of(hunter),
            msg,
            hunter._find_button(task_of(hunter), msg),
            None,
            "button",
            asyncio.get_event_loop().time(),
        )
        assert outcome.result is GrabResult.SUCCESS
        replied = await hunter._maybe_reply(task_of(hunter), msg, outcome)
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
            task_of(hunter),
            msg,
            hunter._find_button(task_of(hunter), msg),
            None,
            "button",
            asyncio.get_event_loop().time(),
        )
        replied = await hunter._maybe_reply(task_of(hunter), msg, outcome)
        assert replied == "谢谢"
        # 第二次被冷却挡住
        outcome2 = await hunter._execute(
            task_of(hunter),
            msg,
            hunter._find_button(task_of(hunter), msg),
            None,
            "button",
            asyncio.get_event_loop().time(),
        )
        replied2 = await hunter._maybe_reply(task_of(hunter), msg, outcome2)
        assert replied2 is None


class TestMultipleTasks:
    """多任务：各条互不影响，但一条红包只被**第一个**命中的任务抢。"""

    def test_first_matching_task_wins(self, alog):
        """🔴 两个任务都命中时只取第一个。

        两个都动手就会点两次按钮，第二次多半报「已经领取过」，
        还可能因为"秒点两次"触发风控。列表顺序就是优先级。
        """
        hunter = RedPacketHunter(
            FakeClient(),
            rp_multi({"id": "a", "strategy": "button"}, {"id": "b", "strategy": "button"}),
            alog,
        )
        msg = rp_message("红包", markup=button_markup(("领取", b"x")))
        task, _, _ = hunter._match(msg, CHAT)
        assert task.id == "a"

    def test_disabled_task_is_skipped(self, alog):
        hunter = RedPacketHunter(
            FakeClient(),
            rp_multi(
                {"id": "a", "enabled": False, "strategy": "button"},
                {"id": "b", "strategy": "button"},
            ),
            alog,
        )
        assert [t.id for t in hunter.prepared] == ["b"]
        msg = rp_message("红包", markup=button_markup(("领取", b"x")))
        assert hunter._match(msg, CHAT)[0].id == "b"

    def test_chat_filter_is_per_task(self, alog):
        other = -1008888888888
        hunter = RedPacketHunter(
            FakeClient(),
            rp_multi(
                {"id": "a", "chats": [CHAT], "strategy": "button"},
                {"id": "b", "chats": [other], "strategy": "button"},
            ),
            alog,
        )
        here = rp_message("红包", markup=button_markup(("领取", b"x")))
        assert hunter._match(here, CHAT)[0].id == "a"
        there = rp_message(
            "红包", chat=FakeChat(other, title="别的群"), markup=button_markup(("领取", b"x"))
        )
        assert hunter._match(there, other)[0].id == "b"

    def test_strategy_is_per_task(self, alog):
        """A 用按钮、B 用关键词 —— 同一个引擎里两套策略互不干扰。"""
        hunter = RedPacketHunter(
            FakeClient(),
            rp_multi(
                {"id": "btn", "strategy": "button"},
                {
                    "id": "kw",
                    "strategy": "keyword",
                    "detect": {"keyword_template": "抢", "text_patterns": ["红包"]},
                },
            ),
            alog,
        )
        # 有按钮 ⇒ 第一个任务（button 策略）接走
        with_button = rp_message("红包", markup=button_markup(("领取", b"x")))
        assert hunter._match(with_button, CHAT)[0].id == "btn"
        # 没按钮 ⇒ 第一个接不了，落到关键词任务
        task, button, _ = hunter._match(rp_message("红包来了"), CHAT)
        assert task.id == "kw"
        assert button is None

    def test_exclude_chats_is_per_task(self, alog):
        """A 任务排除了这个群，消息要落到没排除的 B 任务上。"""
        hunter = RedPacketHunter(
            FakeClient(),
            rp_multi(
                {"id": "a", "exclude_chats": [CHAT], "strategy": "button"},
                {"id": "b", "strategy": "button"},
            ),
            alog,
        )
        msg = rp_message("红包", markup=button_markup(("领取", b"x")))
        assert hunter._match(msg, CHAT)[0].id == "b"

    def test_watched_chats_unions_tasks(self, alog):
        other = -1008888888888
        hunter = RedPacketHunter(
            FakeClient(),
            rp_multi({"id": "a", "chats": [CHAT]}, {"id": "b", "chats": [other]}),
            alog,
        )
        # 这条只看监听范围，不涉及"能不能接住某条消息"，所以策略用默认的即可。
        assert sorted(hunter.watched_chats()) == sorted([CHAT, other])

    def test_any_task_watching_all_means_watch_everything(self, alog):
        """一条留空 ⇒ 整体全监听，否则那条留空的会被无声忽略。"""
        hunter = RedPacketHunter(
            FakeClient(),
            rp_multi({"id": "a", "chats": [CHAT]}, {"id": "b", "chats": []}),
            alog,
        )
        assert hunter.watched_chats() == []

    @pytest.mark.asyncio
    async def test_dispatch_grabs_only_once(self, alog):
        """端到端：两个任务都能接，实际只点了一次按钮。"""
        client = FakeClient(callback_answer="抢到了")
        hunter = RedPacketHunter(
            client,
            rp_multi(
                {"id": "a", "strategy": "button", "success": {"wait_timeout": 0}},
                {"id": "b", "strategy": "button", "success": {"wait_timeout": 0}},
            ),
            alog,
        )
        msg = rp_message("红包", markup=button_markup(("领取", b"x")))
        hunter._dispatch(msg, edited=False)
        await asyncio.gather(*list(hunter._tasks))

        assert len(client.callbacks) == 1, "点两次按钮会触发风控"
        assert hunter.stats["detected"] == 1
        assert hunter.task_stats["a"]["success"] == 1
        assert hunter.task_stats["b"]["success"] == 0

    @pytest.mark.asyncio
    async def test_delay_is_per_task(self, alog):
        """延迟是任务级的：这条任务配了 0.2 秒就必须真的等。"""
        client = FakeClient(callback_answer="抢到了")
        hunter = RedPacketHunter(
            client,
            rp_multi(
                {"id": "off", "enabled": False},
                {"id": "slow", "delay": 0.2, "jitter": 0, "success": {"wait_timeout": 0}},
            ),
            alog,
        )
        task = hunter.prepared[0]
        assert task.id == "slow"
        msg = rp_message("红包", markup=button_markup(("领取", b"x")))

        started = time.perf_counter()
        await hunter._grab(task, msg, hunter._find_button(task, msg), None)
        assert time.perf_counter() - started >= 0.2

    def test_per_task_stats_are_tracked(self, alog):
        """多任务之后「一共抢到 3 个」说明不了是谁干的 —— 必须分任务记。"""
        hunter = RedPacketHunter(FakeClient(), rp_multi({"id": "a"}, {"id": "b"}), alog)
        task = hunter.prepared[0]
        outcome = GrabOutcome(
            result=GrabResult.SUCCESS,
            strategy="button",
            detail="",
            chat_id=CHAT,
            chat_title="红包群",
            message_id=1,
            cost_ms=1.0,
            task=task.label,
        )
        hunter._record(task, outcome)

        assert hunter.stats["success"] == 1
        assert hunter.task_stats["a"]["success"] == 1
        assert hunter.task_stats["b"]["success"] == 0

    def test_success_is_counted_in_metrics(self, alog):
        """大盘只记**成功**：线上那条被点了 4 次、只抢到 1 个，就该记 1。"""
        metrics = MetricsStore()
        hunter = RedPacketHunter(FakeClient(), rp_multi({"id": "a"}), alog, metrics=metrics)
        task = hunter.prepared[0]
        base = dict(
            strategy="button",
            detail="",
            chat_id=CHAT,
            chat_title="红包群",
            message_id=1,
            cost_ms=1.0,
            task=task.label,
        )
        hunter._record(task, GrabOutcome(result=GrabResult.SUCCESS, **base))
        assert metrics.totals()["total"]["red_packet"] == 1
        hunter._record(task, GrabOutcome(result=GrabResult.FAILED, **base))
        hunter._record(task, GrabOutcome(result=GrabResult.UNKNOWN, **base))
        assert metrics.totals()["total"]["red_packet"] == 1, "失败 / 判定不了都不算成功"

    def test_snapshot_reports_tasks(self, alog):
        hunter = RedPacketHunter(
            FakeClient(), rp_multi({"id": "a"}, {"id": "b", "enabled": False}), alog
        )
        snap = hunter.snapshot()
        assert snap["tasks"] == 2
        assert snap["active_tasks"] == 1
        assert set(snap["per_task"]) == {"a"}

    @pytest.mark.asyncio
    async def test_register_logs_tasks_and_warns_about_broken_ones(self):
        """配不全的任务照样注册，但必须**明确说出来** —— 否则用户只看到"开了没动静"。"""
        log = CapturingLog()
        hunter = RedPacketHunter(
            FakeClient(),
            rp_multi({"id": "good"}, {"id": "bad", "strategy": "keyword"}),
            log,
        )
        await hunter.register()

        assert "tasks=2" in log.text
        assert "任务配置不完整" in log.text
        assert "bad" in log.text

    @pytest.mark.asyncio
    async def test_include_edited_is_any(self, alog):
        """只要有一条任务要处理编辑事件，就整体注册那个 handler。"""
        client = FakeClient()
        hunter = RedPacketHunter(
            client,
            rp_multi({"id": "a", "include_edited": False}, {"id": "b", "include_edited": True}),
            alog,
        )
        await hunter.register()
        groups = sorted(group for _, group in client.handlers)
        assert groups == [hunter.HANDLER_GROUP, hunter.HANDLER_GROUP]


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


# --------------------------------------------------------------------------- #
# 「你已经领过这个红包啦」：判定 + 不再重复点 + 通知策略
#
# 全部来自一次线上实测（白嫖分享社 · 积分红包 · message_id 352733）：
#
#   10:18:19  发现红包  edited=True          → 点 → callback「🧧 抢到 50 积分！」 → success
#   10:22:40  同一条消息又被 bot **编辑**了  → 又点 → callback「你已经领过这个红包啦。」
#   10:26:48  还是同一条消息，第三次       → 又点 → 又是「你已经领过这个红包啦。」
#
# 三个问题都在这里钉死：回显判不出失败、同一条消息反复点、还有给用户的噪音通知。
# --------------------------------------------------------------------------- #
class FakeNotifier:
    """只记录 submit 的内容，不真的发通知。"""

    def __init__(self) -> None:
        self.tasks: list[object] = []

    def submit(self, task: object) -> bool:
        self.tasks.append(task)
        return True


def window_after_now() -> tuple[str, str]:
    """造一个「现在必定不在其中」的时段：起点在 2 分钟后。

    左闭右开 + 只有一分钟宽，所以连跨零点的边界情况也稳（23:59 → 00:01~00:02）。
    """
    now = datetime.now()
    return (
        (now + timedelta(minutes=2)).strftime("%H:%M"),
        (now + timedelta(minutes=3)).strftime("%H:%M"),
    )


def window_around_now() -> tuple[str, str]:
    """造一个「现在必定在其中」的时段：前后各 2 分钟。"""
    now = datetime.now()
    return (
        (now - timedelta(minutes=2)).strftime("%H:%M"),
        (now + timedelta(minutes=2)).strftime("%H:%M"),
    )


def grab_button():
    return button_markup(("🧧 抢积分", b"grab"))


class TestAlreadyClaimedEcho:
    """机器人回「你已经领过这个红包啦。」—— 这是**失败**，不是未知。"""

    def test_echo_is_classified_as_failure(self, alog):
        """默认失败特征必须认得这句线上原文。

        原来的 ``已经(领取|抢)过`` 匹配不上它（"领过" 既不是 "领取过" 也不是
        "抢过"），于是被判成 unknown：统计算错，而且"结果未知"的通知会一直
        打扰用户 —— 这条用例就是那个 bug 的墓碑。
        """
        hunter = RedPacketHunter(FakeClient(), rp_config(), alog)
        task = hunter.prepared[0]
        assert hunter._classify(task, "你已经领过这个红包啦。") is GrabResult.FAILED

    @pytest.mark.parametrize(
        "text",
        [
            "你已经领过这个红包啦。",
            "您已领过该红包",
            "已经领取过了",
            "已经抢过了",
        ],
    )
    def test_variants_are_all_failures(self, alog, text):
        hunter = RedPacketHunter(FakeClient(), rp_config(), alog)
        assert hunter._classify(hunter.prepared[0], text) is GrabResult.FAILED

    @pytest.mark.asyncio
    async def test_end_to_end_is_failed_not_unknown(self, alog):
        client = FakeClient(callback_answer="你已经领过这个红包啦。")
        hunter = RedPacketHunter(client, rp_config(), alog)
        await hunter.register()
        hunter._dispatch(rp_message("🧧", markup=grab_button()), edited=True)
        await asyncio.gather(*list(hunter._tasks))

        assert hunter.stats["failed"] == 1
        assert hunter.stats["unknown"] == 0, "这句回显不该落进「未知」"


class TestLegacyFailurePatternUpgrade:
    """老配置里那份「旧默认清单」必须被升级 —— 光改默认值是没用的。

    面板保存会把**整份**配置（含默认清单）写盘，所以老用户的 ``config.json``
    里存着旧的默认值；而 pydantic 只在字段**缺失**时才用默认值。
    线上就是这么撞出来的：代码里加了「已领过」，用户那份配置里没有，
    机器人回「你已经领过这个红包啦」照样被判成「未知」。
    """

    def test_legacy_default_is_upgraded(self):
        success = RedPacketSuccess(failure_patterns=list(_LEGACY_FAILURE_PATTERNS))
        for pattern in _ADDED_FAILURE_PATTERNS:
            assert pattern in success.failure_patterns

    def test_upgrade_keeps_user_additions(self):
        """用户在旧默认上自己加过东西 ⇒ 升级时一条都不能丢。"""
        custom = list(_LEGACY_FAILURE_PATTERNS) + [r"我的自定义特征"]
        success = RedPacketSuccess(failure_patterns=custom)
        assert r"我的自定义特征" in success.failure_patterns
        for pattern in _ADDED_FAILURE_PATTERNS:
            assert pattern in success.failure_patterns

    def test_user_deletion_is_respected(self):
        """清单缺了旧默认里的一条 ⇒ 说明用户刻意删过，原样保留不擅自补。"""
        trimmed = [p for p in _LEGACY_FAILURE_PATTERNS if p != r"无效"]
        success = RedPacketSuccess(failure_patterns=trimmed)
        assert success.failure_patterns == trimmed

    def test_upgrade_is_idempotent(self):
        """面板每保存一次都会跑一遍，不能越加越长。"""
        once = RedPacketSuccess(failure_patterns=list(_LEGACY_FAILURE_PATTERNS))
        twice = RedPacketSuccess(failure_patterns=list(once.failure_patterns))
        assert twice.failure_patterns == once.failure_patterns

    def test_fresh_default_carries_new_patterns(self):
        assert RedPacketSuccess().failure_patterns == list(_DEFAULT_FAILURE_PATTERNS)

    def test_legacy_config_still_classifies_online_echo(self, alog):
        """端到端复刻线上现场：**磁盘上是旧清单**的老配置也要判得出失败。

        只改默认值的话，这条用例会红。
        """
        config = rp_config(
            success={
                "failure_patterns": list(_LEGACY_FAILURE_PATTERNS),
                "wait_timeout": 0.05,
            }
        )
        hunter = RedPacketHunter(FakeClient(), config, alog)
        assert hunter._classify(
            hunter.prepared[0], "你已经领过这个红包啦。"
        ) is GrabResult.FAILED


class TestSettledNotClickedTwice:
    """同一条红包消息一旦判出**确定结论**，就再也不点。"""

    @pytest.mark.asyncio
    async def test_success_settles_and_stops_reclicking(self, alog):
        """抢到之后 bot 还在编辑同一条消息 ⇒ 不能又去点一次。"""
        client = FakeClient(callback_answer="🧧 抢到 50 积分！")
        hunter = RedPacketHunter(client, rp_config(), alog)
        await hunter.register()
        msg = rp_message("🧧", markup=grab_button())

        hunter._dispatch(msg, edited=True)
        await asyncio.gather(*list(hunter._tasks))
        assert len(client.callbacks) == 1
        assert hunter._settled, "抢到是定论，必须记进 _settled"

        # 模拟「几分钟后 bot 又编辑了同一条消息」：把 60 秒的 _seen 窗口抹掉，
        # 逼这次派发只能靠 _settled 挡住 —— 否则这条用例证明不了新逻辑。
        hunter._seen.clear()
        hunter._dispatch(msg, edited=True)
        await asyncio.gather(*list(hunter._tasks))

        assert len(client.callbacks) == 1, "已经抢到的红包不该再点第二次"
        assert hunter.stats["detected"] == 1

    @pytest.mark.asyncio
    async def test_failure_also_settles(self, alog):
        """「已经领过」「已被抢完」同样是定论 —— 之后也不该再点。"""
        client = FakeClient(callback_answer="你已经领过这个红包啦。")
        hunter = RedPacketHunter(client, rp_config(), alog)
        await hunter.register()
        msg = rp_message("🧧", markup=grab_button())

        hunter._dispatch(msg, edited=True)
        await asyncio.gather(*list(hunter._tasks))
        hunter._seen.clear()
        hunter._dispatch(msg, edited=True)
        await asyncio.gather(*list(hunter._tasks))

        assert len(client.callbacks) == 1

    @pytest.mark.asyncio
    async def test_unknown_does_not_settle(self, alog):
        """机器人没回显 ⇒ **不**记定论，还允许重试（可能只是网络抖动）。"""
        client = FakeClient(callback_answer="")
        hunter = RedPacketHunter(client, rp_config(), alog)
        await hunter.register()
        hunter._dispatch(rp_message("🧧", markup=grab_button()), edited=True)
        await asyncio.gather(*list(hunter._tasks))

        assert hunter.stats["unknown"] == 1
        assert hunter._settled == {}

    def test_settled_cache_is_capped(self, alog):
        """长期运行不能把这张表撑爆 —— 超了就留最近的一半。"""
        hunter = RedPacketHunter(FakeClient(), rp_config(), alog)
        for i in range(hunter.SETTLED_MAX + 10):
            hunter._settled[(CHAT, i)] = (float(i), "success")
        hunter._settle(
            GrabOutcome(
                result=GrabResult.SUCCESS,
                strategy="button",
                detail="-",
                chat_id=CHAT,
                chat_title="群",
                message_id=10**9,
                cost_ms=1.0,
            )
        )
        assert len(hunter._settled) <= hunter.SETTLED_MAX


class TestNotifyPolicy:
    """「根据机器人的回显决定要不要通知我」。"""

    def test_policy_matrix(self, alog):
        hunter = RedPacketHunter(FakeClient(), rp_config(), alog)
        task = hunter.prepared[0]
        expected = {
            "success": {
                GrabResult.SUCCESS: True,
                GrabResult.FAILED: False,
                GrabResult.UNKNOWN: False,
                GrabResult.ERROR: False,
            },
            "attention": {
                GrabResult.SUCCESS: True,
                GrabResult.FAILED: False,
                GrabResult.UNKNOWN: False,
                GrabResult.ERROR: True,
            },
            "always": {result: True for result in GrabResult},
        }
        for policy, table in expected.items():
            task.config.notify_on = policy
            for result, want in table.items():
                assert hunter._should_notify(task, result) is want, f"{policy}/{result}"

    def test_default_policy_is_success_only(self, alog):
        """默认只在抢到时打扰我 —— 每次都推只会让人把通知整个关掉。"""
        hunter = RedPacketHunter(FakeClient(), rp_config(), alog)
        assert hunter.prepared[0].config.notify_on == "success"

    @pytest.mark.asyncio
    async def test_already_claimed_does_not_notify_by_default(self, alog):
        """线上那条噪音：机器人回「你已经领过」时**不该**推给我。"""
        notifier = FakeNotifier()
        client = FakeClient(callback_answer="你已经领过这个红包啦。")
        hunter = RedPacketHunter(client, rp_config(), alog, notifier)
        await hunter.register()
        hunter._dispatch(rp_message("🧧", markup=grab_button()), edited=True)
        await asyncio.gather(*list(hunter._tasks))

        assert hunter.stats["failed"] == 1
        assert notifier.tasks == [], "「已经领过」是噪音，不该打扰用户"

    @pytest.mark.asyncio
    async def test_success_does_notify(self, alog):
        notifier = FakeNotifier()
        client = FakeClient(callback_answer="🧧 抢到 50 积分！")
        hunter = RedPacketHunter(client, rp_config(), alog, notifier)
        await hunter.register()
        hunter._dispatch(rp_message("🧧", markup=grab_button()), edited=True)
        await asyncio.gather(*list(hunter._tasks))

        assert hunter.stats["success"] == 1
        assert len(notifier.tasks) == 1

    @pytest.mark.asyncio
    async def test_always_policy_keeps_old_behaviour(self, alog):
        notifier = FakeNotifier()
        client = FakeClient(callback_answer="你已经领过这个红包啦。")
        hunter = RedPacketHunter(client, rp_config(notify_on="always"), alog, notifier)
        await hunter.register()
        hunter._dispatch(rp_message("🧧", markup=grab_button()), edited=True)
        await asyncio.gather(*list(hunter._tasks))

        assert len(notifier.tasks) == 1

    @pytest.mark.asyncio
    async def test_task_switch_still_wins(self, alog):
        """「推送通知」总开关关掉时，再宽的策略也不该发。"""
        notifier = FakeNotifier()
        client = FakeClient(callback_answer="🧧 抢到 50 积分！")
        hunter = RedPacketHunter(
            client, rp_config(notify=False, notify_on="always"), alog, notifier
        )
        await hunter.register()
        hunter._dispatch(rp_message("🧧", markup=grab_button()), edited=True)
        await asyncio.gather(*list(hunter._tasks))

        assert notifier.tasks == []


class TestGlobalWindow:
    """全局动手时段：账号级，所有任务共用。"""

    def test_window_defaults_to_all_day(self, alog):
        hunter = RedPacketHunter(FakeClient(), rp_config(), alog)
        assert hunter.config.window.enabled is False
        assert hunter.config.in_window is True
        assert hunter.config.window.describe() == "全天"

    def test_in_window_matches_contains(self, alog):
        config = rp_config(window={"enabled": True, "start": "08:00", "end": "23:00"})
        assert config.red_packet.in_window == config.red_packet.window.contains()
        assert config.red_packet.window.describe() == "08:00~23:00"

    def test_start_equal_end_is_rejected(self, alog):
        """想全天就关开关，不许用「相等」去猜语义。"""
        with pytest.raises(Exception):
            rp_config(window={"enabled": True, "start": "08:00", "end": "08:00"})

    def test_legacy_flat_config_still_migrates_with_window(self, alog):
        """新增顶层 ``window`` 不能把旧扁平配置的迁移带坏。"""
        config = rp_config(window={"enabled": True, "start": "09:00", "end": "18:00"})
        assert [task.id for task in config.red_packet.tasks] == ["default"]
        assert config.red_packet.window.start == "09:00"

    @pytest.mark.asyncio
    async def test_outside_window_does_not_click(self, alog):
        """时段外：看得见，但绝不动手，也不算「发现红包」。"""
        start, end = window_after_now()
        client = FakeClient(callback_answer="🧧 抢到 50 积分！")
        hunter = RedPacketHunter(
            client, rp_config(window={"enabled": True, "start": start, "end": end}), alog
        )
        await hunter.register()
        hunter._dispatch(rp_message("🧧", markup=grab_button()), edited=True)
        await asyncio.sleep(0)

        assert client.callbacks == [], "时段外不该点按钮"
        assert hunter.stats["outside_window"] == 1
        assert hunter.stats["detected"] == 0, "时段外不算「发现红包」"
        assert hunter.task_stats["default"]["outside_window"] == 1
        assert hunter.snapshot()["in_window"] is False

    @pytest.mark.asyncio
    async def test_inside_window_still_grabs(self, alog):
        """时段内一切照旧 —— 加了闸门不能把正常抢包也拦住。"""
        start, end = window_around_now()
        client = FakeClient(callback_answer="🧧 抢到 50 积分！")
        hunter = RedPacketHunter(
            client, rp_config(window={"enabled": True, "start": start, "end": end}), alog
        )
        await hunter.register()
        hunter._dispatch(rp_message("🧧", markup=grab_button()), edited=True)
        await asyncio.gather(*list(hunter._tasks))

        assert hunter.stats["success"] == 1
        assert hunter.stats["outside_window"] == 0
        assert hunter.snapshot()["in_window"] is True

    def test_snapshot_exposes_window(self, alog):
        hunter = RedPacketHunter(FakeClient(), rp_config(), alog)
        snap = hunter.snapshot()
        assert snap["window"] == "全天"
        assert snap["in_window"] is True
        assert snap["outside_window"] == 0
        assert snap["settled"] == 0


class TestPerTaskWindow:
    """动手时段改成**任务级**（2026-10-02 用户纠正：「窗口期应该根据任务来，而不是全局」）。

    账号级 ``window`` 只剩两个作用：旧配置的迁移来源、新任务的默认值。
    引擎判定必须走**每条任务自己的** window —— 比如 A 频道只在白天抢、B 频道全天抢。
    """

    CHAT_NIGHT = -1007777777777
    CHAT_DAY = -1008888888888

    @staticmethod
    def multi(*tasks: dict, window: dict | None = None) -> AccountConfig:
        """多条任务的账号配置，可选带账号级 ``window``。

        ⚠️ 不能用 :func:`rp_config`：它的 base 里带着旧扁平字段（strategy/delay/…），
        而只要 payload 里出现了 ``tasks`` 键，旧配置迁移就整体跳过 —— 那些顶层字段
        会直接撞上 ``extra="forbid"``，测试连构造都过不去。
        """
        base = {"success": {"wait_timeout": 0.05}}
        payload: dict = {"enabled": True, "tasks": [{**base, **task} for task in tasks]}
        if window is not None:
            payload["window"] = window
        return AccountConfig.model_validate({"red_packet": payload})

    def test_task_without_window_inherits_account_default(self):
        """线上就是这么一份配置：账号级 08:00~23:00 + 一条没有 window 的任务。

        迁移必须把时段填进任务里，否则「半夜不抢」这个保护会**静默消失**。
        """
        config = rp_config(window={"enabled": True, "start": "08:00", "end": "23:00"})
        task = config.red_packet.tasks[0]
        assert task.window is not None
        assert task.window.describe() == "08:00~23:00"
        assert task.in_window == task.window.contains()

    def test_explicit_task_window_wins(self):
        config = self.multi(
            {"id": "a", "window": {"enabled": True, "start": "20:00", "end": "23:00"}},
        )
        config.red_packet.window = TimeWindow(enabled=True, start="09:00", end="18:00")
        assert config.red_packet.tasks[0].window.describe() == "20:00~23:00"

    def test_account_default_does_not_overwrite_task_window(self):
        """账号默认值只负责「填空」，不许覆盖任务自己写的时段。"""
        config = self.multi(
            {"id": "a", "window": {"enabled": True, "start": "20:00", "end": "23:00"}},
            window={"enabled": True, "start": "09:00", "end": "18:00"},
        )
        assert config.red_packet.tasks[0].window.describe() == "20:00~23:00"
        assert config.red_packet.window.describe() == "09:00~18:00"

    def test_inherited_windows_are_independent_copies(self):
        """深拷贝：两条任务不能共享同一个（可赋值的）模型实例。"""
        config = self.multi(
            {"id": "a"},
            {"id": "b"},
            window={"enabled": True, "start": "09:00", "end": "18:00"},
        )
        first, second = config.red_packet.tasks
        assert first.window is not second.window
        first.window = TimeWindow(enabled=True, start="01:00", end="02:00")
        assert second.window.describe() == "09:00~18:00", "改一条不能连带改另一条"

    def test_aggregate_in_window_is_any_active_task(self):
        inside = window_around_now()
        outside = window_after_now()
        config = self.multi(
            {"id": "night", "window": {"enabled": True, "start": outside[0], "end": outside[1]}},
            {"id": "day", "window": {"enabled": True, "start": inside[0], "end": inside[1]}},
        )
        assert config.red_packet.tasks[0].in_window is False
        assert config.red_packet.tasks[1].in_window is True
        assert config.red_packet.in_window is True, "只要有一条任务能动，账号就还在动手"

    def test_bare_task_without_window_is_all_day(self):
        """单独 new 出来的任务 ``window`` 是 None —— 属性不能抛 AttributeError。"""
        assert RedPacketTask(id="x").in_window is True

    @pytest.mark.asyncio
    async def test_each_task_uses_its_own_window(self, alog):
        """两条任务两个频道：时段外那条不动手，时段内那条照抢。"""
        inside = window_around_now()
        outside = window_after_now()
        client = FakeClient(callback_answer="🧧 抢到 50 积分！")
        hunter = RedPacketHunter(
            client,
            self.multi(
                {
                    "id": "night",
                    "chats": [self.CHAT_NIGHT],
                    "window": {"enabled": True, "start": outside[0], "end": outside[1]},
                },
                {
                    "id": "day",
                    "chats": [self.CHAT_DAY],
                    "window": {"enabled": True, "start": inside[0], "end": inside[1]},
                },
            ),
            alog,
        )
        await hunter.register()

        hunter._dispatch(
            rp_message("🧧", markup=grab_button(), chat=FakeChat(self.CHAT_NIGHT, title="夜间群")),
            edited=True,
        )
        await asyncio.sleep(0)
        assert client.callbacks == [], "时段外的那条任务不该点按钮"
        assert hunter.task_stats["night"]["outside_window"] == 1
        assert hunter.task_stats["day"]["outside_window"] == 0, "别把别的任务的账记到它头上"

        hunter._dispatch(
            rp_message("🧧", markup=grab_button(), chat=FakeChat(self.CHAT_DAY, title="白天群")),
            edited=True,
        )
        await asyncio.gather(*list(hunter._tasks))
        assert hunter.stats["success"] == 1
        assert hunter.task_stats["day"]["detected"] == 1


class TestTimeWindowReuse:
    """抢注与抢红包共用同一个时段模型，别再各写一份。"""

    def test_reg_grab_window_alias_is_the_same_class(self):
        from tg_assistant.config import RegGrabWindow

        assert RegGrabWindow is TimeWindow

    def test_cross_midnight(self):
        window = TimeWindow(enabled=True, start="22:00", end="06:00")
        assert window.contains(datetime(2026, 9, 29, 23, 30)) is True
        assert window.contains(datetime(2026, 9, 29, 2, 0)) is True
        assert window.contains(datetime(2026, 9, 29, 12, 0)) is False

    def test_right_edge_is_exclusive(self):
        window = TimeWindow(enabled=True, start="08:00", end="23:00")
        assert window.contains(datetime(2026, 9, 29, 22, 59)) is True
        assert window.contains(datetime(2026, 9, 29, 23, 0)) is False


# --------------------------------------------------------------------------- #
# 「老消息被反复编辑」的防护（线上 2026-09-29 白嫖分享社 352733）
# --------------------------------------------------------------------------- #
def _ago(**kwargs) -> datetime:
    """``Message.date`` 的替身：多久以前（UTC、带 tzinfo，和 pyrogram 一致）。"""
    return datetime.now(timezone.utc) - timedelta(**kwargs)


class TestEditAgeGate:
    """``edit_max_age``：**编辑**事件只看"原消息有多新"。

    线上现场：一条长驻红包（机器人一整天编辑它的状态文本）在 8 小时里被处理 8 次，
    同一条 ``edited=True`` 事件反复进流程，其中 7 次点下去只换回「你已经领过」。
    """

    @pytest.mark.asyncio
    async def test_stale_edit_is_skipped(self, alog):
        client = FakeClient(callback_answer="🧧 抢到 50 积分！")
        hunter = RedPacketHunter(client, rp_config(edit_max_age=1800), alog)
        await hunter.register()
        stale = rp_message("🧧", markup=grab_button(), date=_ago(hours=8))

        hunter._dispatch(stale, edited=True)
        await asyncio.gather(*list(hunter._tasks))

        assert client.callbacks == [], "8 小时前的消息被编辑，不该动手"
        assert hunter.stats["stale_edits"] == 1
        assert hunter.stats["detected"] == 0, "被闸门挡下的不算「发现红包」"
        assert sum(v["stale_edits"] for v in hunter.task_stats.values()) == 1
        assert hunter.snapshot()["stale_edits"] == 1

    @pytest.mark.asyncio
    async def test_fresh_edit_is_still_grabbed(self, alog):
        """有些 bot 先发消息、几秒后才编辑出按钮 —— 这条路径不能被闸门切断。"""
        client = FakeClient(callback_answer="🧧 抢到 50 积分！")
        hunter = RedPacketHunter(client, rp_config(edit_max_age=1800), alog)
        await hunter.register()

        hunter._dispatch(
            rp_message("🧧", markup=grab_button(), date=_ago(seconds=5)), edited=True
        )
        await asyncio.gather(*list(hunter._tasks))

        assert len(client.callbacks) == 1
        assert hunter.stats["stale_edits"] == 0

    @pytest.mark.asyncio
    async def test_new_message_event_is_not_gated(self, alog):
        """闸门只管**编辑**：新消息事件照旧处理（它的发布时间本来就是现在）。"""
        client = FakeClient(callback_answer="🧧 抢到 50 积分！")
        hunter = RedPacketHunter(client, rp_config(edit_max_age=60), alog)
        await hunter.register()

        hunter._dispatch(
            rp_message("🧧", markup=grab_button(), date=_ago(hours=3)), edited=False
        )
        await asyncio.gather(*list(hunter._tasks))

        assert len(client.callbacks) == 1
        assert hunter.stats["stale_edits"] == 0

    @pytest.mark.asyncio
    async def test_zero_disables_the_gate(self, alog):
        """``0`` = 不限年龄（保留「只勾处理编辑事件」的老行为）。"""
        client = FakeClient(callback_answer="🧧 抢到 50 积分！")
        hunter = RedPacketHunter(client, rp_config(edit_max_age=0), alog)
        await hunter.register()

        hunter._dispatch(
            rp_message("🧧", markup=grab_button(), date=_ago(days=3)), edited=True
        )
        await asyncio.gather(*list(hunter._tasks))

        assert len(client.callbacks) == 1
        assert hunter.stats["stale_edits"] == 0

    def test_age_comes_from_the_original_post_time(self, alog):
        hunter = RedPacketHunter(FakeClient(), rp_config(edit_max_age=60), alog)
        task = hunter.prepared[0]

        age = hunter._edit_age(task, make_message("🧧", date=_ago(seconds=90)))
        assert age is not None and 85 <= age <= 95
        # 边界之内 → ``None``（放行）
        assert hunter._edit_age(task, make_message("🧧", date=_ago(seconds=30))) is None

    def test_missing_date_fails_open(self, alog):
        """读不到时间就**放行**：宁可多看一眼，也不要因为读不到时间漏掉真红包。"""
        hunter = RedPacketHunter(FakeClient(), rp_config(edit_max_age=60), alog)
        task = hunter.prepared[0]

        assert hunter._edit_age(task, make_message("🧧")) is None
        assert hunter._edit_age(task, make_message("🧧", date="不是时间")) is None


class TestSettledSurvivesRestart:
    """「这条红包已经点过了」必须活过重启（``settled_path`` 落盘）。

    线上实测：11:16 判出「你已经领过」并记进内存表，13:50 一次重启就忘光，
    14:23 那条长驻红包又被点了一遍；16:38~17:46 又重启 6 次，18:07 再点一遍。
    """

    @pytest.mark.asyncio
    async def test_settled_is_remembered_after_restart(self, alog, tmp_path):
        path = tmp_path / "red_packet_settled.json"
        message = rp_message("🧧", markup=grab_button())

        first = FakeClient(callback_answer="你已经领过这个红包啦。")
        hunter = RedPacketHunter(first, rp_config(), alog, settled_path=path)
        await hunter.register()
        hunter._dispatch(message, edited=True)
        await asyncio.gather(*list(hunter._tasks))

        assert len(first.callbacks) == 1
        assert path.exists(), "判出定论之后必须写盘"

        # 「重启」：全新的 client + 全新的 hunter，读同一个文件。
        second = FakeClient(callback_answer="你已经领过这个红包啦。")
        reborn = RedPacketHunter(second, rp_config(), alog, settled_path=path)

        assert reborn.restored_settled == 1
        assert reborn.snapshot()["settled_restored"] == 1

        await reborn.register()
        reborn._dispatch(message, edited=True)
        await asyncio.gather(*list(reborn._tasks))

        assert second.callbacks == [], "重启后不该把已经点过的红包再点一次"
        assert reborn.stats["detected"] == 0

    @pytest.mark.asyncio
    async def test_unknown_is_not_persisted(self, alog, tmp_path):
        """``unknown`` 刻意不算定论 ⇒ 也不该落盘（重试是有意义的）。"""
        path = tmp_path / "red_packet_settled.json"
        client = FakeClient(callback_answer="")
        hunter = RedPacketHunter(client, rp_config(), alog, settled_path=path)
        await hunter.register()

        hunter._dispatch(rp_message("🧧", markup=grab_button()), edited=True)
        await asyncio.gather(*list(hunter._tasks))

        assert hunter.stats["unknown"] == 1
        assert not path.exists()

    def test_broken_file_is_tolerated(self, alog, tmp_path):
        """状态文件被写坏不能让账号起不来 —— 最坏只是可能多点一次。"""
        path = tmp_path / "red_packet_settled.json"
        path.write_text("{ 这不是 json", encoding="utf-8")

        hunter = RedPacketHunter(FakeClient(), rp_config(), alog, settled_path=path)

        assert hunter._settled == {}
        assert hunter.restored_settled == 0

    def test_directory_instead_of_file_is_tolerated(self, alog, tmp_path):
        path = tmp_path / "red_packet_settled.json"
        path.mkdir()

        hunter = RedPacketHunter(FakeClient(), rp_config(), alog, settled_path=path)

        assert hunter._settled == {}

    def test_garbage_entries_are_skipped(self, alog, tmp_path):
        """状态文件是**数据**，不是信任边界：看不懂的条目跳过，别把整份丢掉。"""
        path = tmp_path / "red_packet_settled.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "settled": {
                        f"{CHAT}:352733": ["failed", 1759000000.0],
                        "看不懂": "failed",
                        f"{CHAT}:不是数字": ["failed", 1.0],
                        f"{CHAT}:352734": "success",  # 老格式：只落了结论
                        f"{CHAT}:352735": ["failed", "不是数字"],
                        f"{CHAT}:352736": [],
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        hunter = RedPacketHunter(FakeClient(), rp_config(), alog, settled_path=path)

        assert set(hunter._settled) == {
            (CHAT, 352733),
            (CHAT, 352734),
            (CHAT, 352735),
        }
        assert hunter._settled[(CHAT, 352733)] == (1759000000.0, "failed")
        assert hunter._settled[(CHAT, 352734)] == (0.0, "success"), "没时间的当最旧"

    def test_oversized_file_is_trimmed_on_load(self, alog, tmp_path):
        """磁盘上的表可能比上限大（老版本 / 手工编辑过）：读回来也要裁。"""
        path = tmp_path / "red_packet_settled.json"
        biggest = RedPacketHunter.SETTLED_MAX * 2
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "settled": {
                        f"{CHAT}:{i}": ["success", float(i)] for i in range(biggest)
                    },
                }
            ),
            encoding="utf-8",
        )

        hunter = RedPacketHunter(FakeClient(), rp_config(), alog, settled_path=path)

        assert len(hunter._settled) <= RedPacketHunter.SETTLED_MAX // 2
        assert (CHAT, biggest - 1) in hunter._settled, "留下来的应该是最新的那些"

    def test_write_failure_is_swallowed(self, alog, tmp_path):
        """落盘失败不能让抢红包崩：内存里照样记着。"""
        path = tmp_path / "red_packet_settled.json"
        path.mkdir()  # 同名目录 —— 写文件必然失败

        hunter = RedPacketHunter(FakeClient(), rp_config(), alog, settled_path=path)
        hunter._settle(_outcome(message_id=7))

        assert hunter._settled[(CHAT, 7)][1] == "success"
        assert path.is_dir(), "文件不该把目录顶掉"

    def test_without_path_it_stays_in_memory(self, alog, tmp_path):
        """不传路径（单测 / 离线）不该产生任何文件。"""
        hunter = RedPacketHunter(FakeClient(), rp_config(), alog)

        assert hunter.settled_path is None

        hunter._settle(_outcome(message_id=8))

        assert hunter._settled[(CHAT, 8)][1] == "success"
        # 只断言"没有落盘文件"—— 这条用例要证的是「不传路径就纯内存」，
        # 不去管别的组件在 tmp_path 里建了什么目录（那样太脆）。
        assert not (tmp_path / "red_packet_settled.json").exists()

    def test_saved_shape_is_readable_and_keeps_the_verdict(self, alog, tmp_path):
        """落盘内容要能被人看懂（排查现场时直接看这个文件）。"""
        path = tmp_path / "red_packet_settled.json"
        hunter = RedPacketHunter(FakeClient(), rp_config(), alog, settled_path=path)

        hunter._settle(_outcome(message_id=9))

        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["version"] == 1
        assert raw["settled"][f"{CHAT}:9"][0] == "success"
        assert isinstance(raw["settled"][f"{CHAT}:9"][1], float)


class TestEditMaxAgeConfig:
    """``edit_max_age`` 是**任务级**字段：老式扁平配置也要能带进来。"""

    def test_default_is_30_minutes(self):
        assert RedPacketTask(id="default").edit_max_age == 1800.0

    def test_flat_config_carries_the_field_into_the_task(self):
        """面板/接口收到的扁平写法必须落到任务上，否则 ``extra="forbid"`` 会 400。"""
        config = rp_config(edit_max_age=600)

        assert config.red_packet.tasks[0].edit_max_age == 600

    def test_negative_is_rejected(self):
        with pytest.raises(ValidationError):
            rp_config(edit_max_age=-1)


def _outcome(*, message_id: int) -> GrabOutcome:
    return GrabOutcome(
        result=GrabResult.SUCCESS,
        strategy="button",
        detail="-",
        chat_id=CHAT,
        chat_title="红包群",
        message_id=message_id,
        cost_ms=1.0,
    )
