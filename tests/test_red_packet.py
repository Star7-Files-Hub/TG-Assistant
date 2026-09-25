"""抢红包引擎：检测、点击、判定、回复。"""

from __future__ import annotations

import asyncio
import time

import pytest
from pyrogram.errors import BotResponseTimeout, QueryIdInvalid

from tg_assistant.config import AccountConfig
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
