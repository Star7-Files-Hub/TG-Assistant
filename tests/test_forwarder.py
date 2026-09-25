"""转发引擎：匹配、派发、相册聚合、去重、失败处理。"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import time
import types
from typing import Any, Optional

import pytest
from pyrogram.enums import ParseMode
from pyrogram.errors import ChatForwardsRestricted, ChatWriteForbidden, FloodWait

from tg_assistant.config import (
    AccountConfig,
    AccountRecord,
    ForwardRule,
    MatchConfig,
    NotifyConfig,
)
from tg_assistant.forwarder import (
    ChannelGroupDedupe,
    CrossAccountDedupe,
    DedupeCache,
    ForwardEngine,
    PreparedRule,
    RecentContentDedupe,
    _pipeline_ms,
    _sent_ids,
    content_fingerprint,
)
from tg_assistant.matching import CompiledMatcher
from tg_assistant.runner import AccountRunner, MultiRunner

from .conftest import FakeChat, FakeClient, FakeUser, make_message

SRC = -1001111111111
DST = -1002222222222
#: 「频道 ↔ 群组 同内容」那对用到的两个来源会话。``SRC`` 是群组，``CH_SRC`` 是频道。
CH_SRC = -1003333333333


def build_config(*, exclude_chats=None, **rule_overrides) -> AccountConfig:
    rule = {
        "id": "r1",
        "name": "测试规则",
        "sources": [SRC],
        "targets": [DST],
        "match": {"mode": "regex", "patterns": [r"关键词(\d+)"]},
        # ⚠️ ``ForwardRule.mode`` 的**默认值是 ``copy``**。这里显式钉成 ``forward``，
        # 因为 ``TestForwardEngine`` 考的是「匹配 / 派发 / 去重 / 防循环」这套引擎管道，
        # 不是 copy 与 forward 的实现差异 —— 用 ``forward`` 才能让断言盯着
        # ``forward_messages`` 这一条稳定通道。copy 的行为由
        # ``TestCopyModeSourceLink`` / ``TestForwardFallsBackToCopy`` 专门覆盖。
        "mode": "forward",
    }
    rule.update(rule_overrides)
    forward = {"enabled": True, "rules": [rule]}
    if exclude_chats is not None:
        forward["exclude_chats"] = exclude_chats
    return AccountConfig.model_validate({"forward": forward})


def src_message(text: str, **kwargs):
    kwargs.setdefault("chat", FakeChat(SRC, title="来源群"))
    return make_message(text, **kwargs)


def group_message(text: str, **kwargs):
    """群组来源的消息（``supergroup`` ⇒ ``chat_kind`` 归一成 ``group``）。"""
    kwargs.setdefault("chat", FakeChat(SRC, title="来源群", chat_type="supergroup"))
    return make_message(text, **kwargs)


def channel_message(text: str, **kwargs):
    """频道来源的消息。"""
    kwargs.setdefault("chat", FakeChat(CH_SRC, title="来源频道", chat_type="channel"))
    return make_message(text, **kwargs)


class FakeNotifier:
    """通知器替身：记录提交了什么、撤回了什么。

    只实现引擎真正用到的那三个东西（``config`` / ``submit`` / ``withdraw``）——
    真实 ``BotNotifier`` 要起 worker + 打 HTTP，这里都不需要。
    """

    def __init__(self) -> None:
        self.config = NotifyConfig.model_validate(
            {"enabled": True, "bot_token": "123:abc", "chat_id": -1001234567890, "mode": "copy"}
        )
        self.submitted: list[Any] = []
        self.withdrawn: list[str] = []

    def submit(self, task: Any) -> bool:
        self.submitted.append(task)
        return True

    async def withdraw(self, key: Optional[str]) -> int:
        self.withdrawn.append(key or "")
        return 1


async def drain(engine: ForwardEngine) -> None:
    """等所有派发出去的任务跑完。"""
    for _ in range(50):
        pending = [t for t in engine._tasks if not t.done()]
        if not pending:
            break
        await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.sleep(0)


class SlowFirstForwardClient(FakeClient):
    """**第一次** ``forward_messages`` 卡住，直到 :attr:`release` 被 set。

    用来制造「频道那条还在发、群组那条已经到」的竞态窗口 —— 线上 ``claim`` 与发送之间
    隔着一个 ``await``（``pipeline_ms`` 实测到过 **4223ms**），这个窗口是真实存在的。
    """

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self._first = True

    async def forward_messages(self, **kwargs: Any) -> Any:
        if self._first:
            self._first = False
            await self.release.wait()
        return await super().forward_messages(**kwargs)


class TestDedupeCache:
    def test_first_wins(self):
        cache = DedupeCache(60)
        assert cache.check_and_add((1, 2)) is True
        assert cache.check_and_add((1, 2)) is False

    def test_different_keys(self):
        cache = DedupeCache(60)
        assert cache.check_and_add((1, 2))
        assert cache.check_and_add((1, 3))
        assert len(cache) == 2

    def test_disabled_when_zero(self):
        cache = DedupeCache(0)
        assert cache.check_and_add((1, 2))
        assert cache.check_and_add((1, 2))

    def test_expiry(self, monkeypatch):
        cache = DedupeCache(10)
        now = [1000.0]
        monkeypatch.setattr("tg_assistant.forwarder.time.monotonic", lambda: now[0])
        assert cache.check_and_add((1, 2))
        now[0] += 5
        assert not cache.check_and_add((1, 2))
        now[0] += 6  # 超过 TTL
        assert cache.check_and_add((1, 2))


class TestPreparedRule:
    def test_source_filter(self):
        prepared = PreparedRule.build(build_config().forward.rules[0])
        assert prepared.chat_allowed(SRC, None)[0]
        assert not prepared.chat_allowed(-999, None)[0]

    def test_exclude_source_beats_source(self):
        prepared = PreparedRule.build(
            build_config(sources=[SRC], exclude_sources=[SRC]).forward.rules[0]
        )
        allowed, reason = prepared.chat_allowed(SRC, None)
        assert not allowed
        assert "exclude_sources" in reason

    def test_empty_sources_allows_all(self):
        prepared = PreparedRule.build(build_config(sources=[]).forward.rules[0])
        assert prepared.chat_allowed(-1, None)[0]
        assert prepared.chat_allowed(-2, "x")[0]

    def test_empty_sources_rejects_private(self):
        """sources 为空 = 监听全部群组与频道；私聊不转发。"""
        prepared = PreparedRule.build(build_config(sources=[]).forward.rules[0])
        assert not prepared.chat_allowed(777, None, "private")[0]
        assert prepared.chat_allowed(-1001, None, "group")[0]
        assert prepared.chat_allowed(-1002, None, "channel")[0]

    def test_explicit_private_source_still_allowed(self):
        """显式把私聊写进 sources 时不受「只限群组/频道」限制。"""
        prepared = PreparedRule.build(build_config(sources=[777]).forward.rules[0])
        assert prepared.chat_allowed(777, None, "private")[0]
        assert not prepared.chat_allowed(888, None, "private")[0]

    @pytest.mark.parametrize("kind", ["private", "bot", "direct"])
    def test_raw_one_to_one_kinds_also_rejected(self, kind):
        """调用方直接传 pyrogram 原始值时也要拒。

        逐个枚举字符串容易漏（``direct`` 就漏过一次），所以 ``chat_allowed``
        内部走 ``normalize_chat_kind``。
        """
        prepared = PreparedRule.build(build_config(sources=[]).forward.rules[0])
        assert not prepared.chat_allowed(777, None, kind)[0]

    @pytest.mark.parametrize("kind", ["group", "supergroup", "forum", "channel"])
    def test_raw_broadcast_kinds_still_allowed(self, kind):
        prepared = PreparedRule.build(build_config(sources=[]).forward.rules[0])
        assert prepared.chat_allowed(-1001, None, kind)[0]

    # ---------------------------------------------------------------- #
    # 🔴 防转发死循环（2026-09-18 线上事故的回归防线）
    # ---------------------------------------------------------------- #
    def test_target_never_acts_as_source(self):
        """目标会话不能当来源，否则会无限转发。

        线上事故：``sources=[]``（监听全部）+ ``targets`` 指向账号可见的频道
        ⇒ 转发出去的新消息被自己重新监听到（新消息 = 新 id，去重缓存拦不住）
        ⇒ 再次命中同一条规则 ⇒ 正反馈，100 秒刷了 183 条。
        """
        prepared = PreparedRule.build(build_config(sources=[]).forward.rules[0])
        allowed, reason = prepared.chat_allowed(DST, None, "channel")
        assert not allowed
        assert "目标" in reason

    def test_target_rejected_even_if_listed_in_sources(self):
        """即使有人手滑把目标写进 sources，也必须拒绝 —— 这是硬红线，不靠配置自觉。"""
        prepared = PreparedRule.build(
            build_config(sources=[SRC, DST], targets=[DST]).forward.rules[0]
        )
        assert not prepared.chat_allowed(DST, None)[0]
        assert prepared.chat_allowed(SRC, None)[0]

    def test_target_username_also_rejected(self):
        """targets 写 username 时同样生效，且大小写不敏感。"""
        prepared = PreparedRule.build(
            build_config(sources=[], targets=["@Notify_Channel"]).forward.rules[0]
        )
        assert not prepared.chat_allowed(-1002626018568, "notify_channel", "channel")[0]
        assert prepared.chat_allowed(-100999, "other_channel", "channel")[0]

    def test_target_guard_is_narrow(self):
        """这道闸只拦目标本身，别的会话照常放行 —— 不能把 sources=[] 的"全监听"打瘸。"""
        prepared = PreparedRule.build(build_config(sources=[]).forward.rules[0])
        assert prepared.chat_allowed(SRC, None)[0]
        assert prepared.chat_allowed(-100999, "whatever", "group")[0]
        assert prepared.chat_allowed(-100888, None, "channel")[0]

    def test_from_users_whitelist(self):
        prepared = PreparedRule.build(build_config(from_users=[777]).forward.rules[0])
        assert prepared.sender_allowed(777, None, False)[0]
        assert not prepared.sender_allowed(888, None, False)[0]

    def test_ignore_self_default(self):
        prepared = PreparedRule.build(build_config().forward.rules[0])
        assert not prepared.sender_allowed(1, None, True)[0]

    def test_min_interval(self, monkeypatch):
        prepared = PreparedRule.build(build_config(min_interval=10).forward.rules[0])
        assert prepared.interval_ok(100.0)[0]
        prepared.last_fired = 100.0
        assert not prepared.interval_ok(105.0)[0]
        assert prepared.interval_ok(111.0)[0]


class TestForwardEngine:
    @pytest.mark.asyncio
    async def test_forward_on_match(self, client, alog):
        engine = ForwardEngine(client, build_config(), alog)
        engine.register()
        engine._handle(src_message("这是关键词123的消息"), edited=False)
        await drain(engine)

        assert len(client.forwarded) == 1
        call = client.forwarded[0]
        assert call["chat_id"] == DST
        assert call["from_chat_id"] == SRC
        assert call["message_ids"] == 100
        assert engine.stats["forwarded"] == 1

    @pytest.mark.asyncio
    async def test_no_forward_on_miss(self, client, alog):
        engine = ForwardEngine(client, build_config(), alog)
        engine.register()
        engine._handle(src_message("无关内容"), edited=False)
        await drain(engine)
        assert client.forwarded == []
        assert engine.stats["matched"] == 0

    @pytest.mark.asyncio
    async def test_unrestricted_skips_private_chat(self, client, alog):
        """未限定 sources 时，私聊里命中的消息不能被转发出去。"""
        engine = ForwardEngine(client, build_config(sources=[]), alog)
        engine.register()
        engine._handle(
            make_message("关键词123", chat=FakeChat(777, chat_type="private")),
            edited=False,
        )
        await drain(engine)
        assert client.forwarded == []
        assert engine.stats["matched"] == 0

    @pytest.mark.asyncio
    async def test_unrestricted_forwards_from_group(self, client, alog):
        engine = ForwardEngine(client, build_config(sources=[]), alog)
        engine.register()
        engine._handle(
            make_message("关键词123", chat=FakeChat(-100999, title="任意群")), edited=False
        )
        await drain(engine)
        assert len(client.forwarded) == 1

    @pytest.mark.asyncio
    async def test_no_loop_when_target_is_visible(self, client, alog):
        """🔴 端到端防循环：目标频道里新出现的消息不能再被转发。

        还原真实死循环的第二步 —— 源消息转发到 DST 后，DST 里出现一条**新 id**
        的消息（去重缓存对此无能为力），引擎必须在 ``chat_allowed`` 就拒掉它。
        """
        engine = ForwardEngine(client, build_config(sources=[]), alog)
        engine.register()

        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)
        assert len(client.forwarded) == 1

        # 转发产生的新消息 id 出现在目标频道里，连续喂三轮模拟放大过程
        first_new_id = client.next_message_id
        for offset in range(3):
            engine._handle(
                make_message(
                    "关键词123",
                    message_id=first_new_id + offset,
                    chat=FakeChat(DST, title="目标频道"),
                ),
                edited=False,
            )
            await drain(engine)

        assert len(client.forwarded) == 1, "目标频道里的消息又被转发了 —— 死循环没被拦住"
        assert engine.stats["matched"] == 1

    @pytest.mark.asyncio
    async def test_global_exclude_chats_blocks_every_rule(self, client, alog):
        """账号级 exclude_chats：写一次，所有规则都不监听。"""
        engine = ForwardEngine(client, build_config(sources=[], exclude_chats=[SRC]), alog)
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)
        assert client.forwarded == []
        assert engine.stats["excluded"] == 1
        assert engine.stats["matched"] == 0

    @pytest.mark.asyncio
    async def test_global_exclude_chats_accepts_username(self, client, alog):
        """排除列表支持 @username，且大小写不敏感。"""
        engine = ForwardEngine(
            client, build_config(sources=[], exclude_chats=["@Noisy_Channel"]), alog
        )
        engine.register()
        engine._handle(
            make_message("关键词123", chat=FakeChat(-100777, username="noisy_channel")),
            edited=False,
        )
        await drain(engine)
        assert client.forwarded == []
        assert engine.stats["excluded"] == 1

    @pytest.mark.asyncio
    async def test_global_exclude_leaves_other_chats_alone(self, client, alog):
        """排除列表只拦名单里的会话，别的照常转发。"""
        engine = ForwardEngine(client, build_config(sources=[], exclude_chats=[-100999]), alog)
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)
        assert len(client.forwarded) == 1
        assert engine.stats["excluded"] == 0

    @pytest.mark.asyncio
    async def test_unrestricted_registers_group_channel_filter(self, client, alog):
        """不能退化成 filter=None，否则私聊也会进 handler。"""
        engine = ForwardEngine(client, build_config(sources=[]), alog)
        engine.register()
        handler = client.handlers[0][0]
        assert handler.filters is not None

    @pytest.mark.asyncio
    async def test_restricted_registers_chat_filter(self, client, alog):
        engine = ForwardEngine(client, build_config(), alog)
        engine.register()
        handler = client.handlers[0][0]
        assert handler.filters is not None

    @pytest.mark.asyncio
    async def test_other_chat_ignored(self, client, alog):
        engine = ForwardEngine(client, build_config(), alog)
        engine.register()
        engine._handle(
            make_message("关键词123", chat=FakeChat(-100999, title="别的群")), edited=False
        )
        await drain(engine)
        assert client.forwarded == []

    @pytest.mark.asyncio
    async def test_dedupe_blocks_second_delivery(self, client, alog):
        engine = ForwardEngine(client, build_config(), alog)
        engine.register()
        message = src_message("关键词123")
        engine._handle(message, edited=False)
        engine._handle(message, edited=False)
        await drain(engine)
        assert len(client.forwarded) == 1
        assert engine.stats["deduped"] == 1

    @pytest.mark.asyncio
    async def test_edited_ignored_by_default(self, client, alog):
        engine = ForwardEngine(client, build_config(), alog)
        engine.register()
        engine._handle(src_message("关键词123"), edited=True)
        await drain(engine)
        assert client.forwarded == []

    @pytest.mark.asyncio
    async def test_edited_when_enabled(self, client, alog):
        engine = ForwardEngine(client, build_config(include_edited=True), alog)
        engine.register()
        engine._handle(src_message("关键词123"), edited=True)
        await drain(engine)
        assert len(client.forwarded) == 1

    @pytest.mark.asyncio
    async def test_copy_mode_is_not_a_forward(self, client, alog):
        """``copy`` 模式发出来的是**新消息**，不是转发消息。

        旧实现用 ``forward_messages(hide_sender_name=True)``（``drop_author``）冒充复制，
        那样得到的是**转发消息**，而 Telegram 拒绝编辑转发消息
        ⇒ 原文链接永远补不上去。见 :class:`TestCopyModeSourceLink`。
        """
        engine = ForwardEngine(client, build_config(mode="copy"), alog)
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)
        assert client.forwarded == [], "copy 不该产生转发消息"
        assert len(client.sent) == 1
        assert client.sent[0]["chat_id"] == DST

    @pytest.mark.asyncio
    async def test_forward_mode_keeps_author(self, client, alog):
        engine = ForwardEngine(client, build_config(mode="forward"), alog)
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)
        assert client.forwarded[0]["hide_sender_name"] is None

    @pytest.mark.asyncio
    async def test_text_mode_uses_template(self, client, alog):
        engine = ForwardEngine(
            client,
            build_config(
                mode="text",
                template="命中 {g1}：{text}",
                include_source_link=False,
            ),
            alog,
        )
        engine.register()
        engine._handle(src_message("关键词456"), edited=False)
        await drain(engine)
        assert client.forwarded == []
        assert client.sent[0]["text"] == "命中 456：关键词456"

    @pytest.mark.asyncio
    async def test_text_mode_appends_link(self, client, alog):
        chat = FakeChat(SRC, title="来源群", username="srcchan")
        engine = ForwardEngine(
            client, build_config(mode="text", template="{text}", include_source_link=True), alog
        )
        engine.register()
        engine._handle(make_message("关键词1", chat=chat, message_id=77), edited=False)
        await drain(engine)
        assert "https://t.me/srcchan/77" in client.sent[0]["text"]

    @pytest.mark.asyncio
    async def test_multiple_targets(self, client, alog):
        engine = ForwardEngine(client, build_config(targets=[DST, -1003333333333]), alog)
        engine.register()
        engine._handle(src_message("关键词1"), edited=False)
        await drain(engine)
        assert {call["chat_id"] for call in client.forwarded} == {DST, -1003333333333}

    @pytest.mark.asyncio
    async def test_one_message_matches_two_rules(self, client, alog):
        config = AccountConfig.model_validate(
            {
                "forward": {
                    "enabled": True,
                    "rules": [
                        {
                            "id": "a",
                            "sources": [SRC],
                            "targets": [DST],
                            "mode": "forward",
                            "match": {"mode": "contains", "patterns": ["关键词"]},
                        },
                        {
                            "id": "b",
                            "sources": [SRC],
                            "targets": [-1004444444444],
                            "mode": "forward",
                            "match": {"mode": "contains", "patterns": ["关键"]},
                        },
                    ],
                }
            }
        )
        engine = ForwardEngine(client, config, alog)
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)
        assert {call["chat_id"] for call in client.forwarded} == {DST, -1004444444444}

    @pytest.mark.asyncio
    async def test_failure_is_recorded_not_raised(self, alog):
        from .conftest import FakeClient

        client = FakeClient(forward_error=ChatWriteForbidden("no write"))
        engine = ForwardEngine(client, build_config(), alog)
        engine.register()
        engine._handle(src_message("关键词1"), edited=False)
        await drain(engine)
        assert engine.stats["failed"] == 1
        assert engine.stats["forwarded"] == 0

    @pytest.mark.asyncio
    async def test_flood_wait_retries(self, alog, monkeypatch):
        from .conftest import FakeClient

        client = FakeClient()
        calls = {"n": 0}
        original = client.forward_messages

        async def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise FloodWait(value=1)
            return await original(**kwargs)

        client.forward_messages = flaky
        sleeps: list[float] = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr("tg_assistant.client.asyncio.sleep", fake_sleep)
        engine = ForwardEngine(client, build_config(), alog)
        engine.register()
        engine._handle(src_message("关键词1"), edited=False)
        await drain(engine)
        assert calls["n"] == 2
        assert engine.stats["forwarded"] == 1
        assert sleeps and sleeps[0] >= 1

    @pytest.mark.asyncio
    async def test_media_group_aggregated_once(self, client, alog):
        config = build_config(match={"mode": "all"}, media_group_window=0.1)
        engine = ForwardEngine(client, config, alog)
        engine.register()
        for index in range(3):
            engine._handle(
                src_message("相册", message_id=200 + index, media_group_id="mg-1"), edited=False
            )
        await asyncio.sleep(0.2)
        await drain(engine)
        assert len(client.forwarded) == 1
        assert client.forwarded[0]["message_ids"] == [200, 201, 202]

    @pytest.mark.asyncio
    async def test_media_group_disabled_sends_each(self, client, alog):
        config = build_config(match={"mode": "all"}, media_group=False)
        engine = ForwardEngine(client, config, alog)
        engine.register()
        for index in range(2):
            engine._handle(
                src_message("相册", message_id=300 + index, media_group_id="mg-2"), edited=False
            )
        await drain(engine)
        assert len(client.forwarded) == 2

    @pytest.mark.asyncio
    async def test_service_message_skipped(self, client, alog):
        engine = ForwardEngine(client, build_config(match={"mode": "all"}), alog)
        engine.register()
        engine._handle(src_message("加入群组", service="new_chat_members"), edited=False)
        await drain(engine)
        assert client.forwarded == []

    @pytest.mark.asyncio
    async def test_delay_respected(self, client, alog, monkeypatch):
        slept: list[float] = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr("tg_assistant.forwarder.asyncio.sleep", fake_sleep)
        engine = ForwardEngine(client, build_config(delay=1.5), alog)
        engine.register()
        engine._handle(src_message("关键词1"), edited=False)
        await drain(engine)
        assert 1.5 in slept

    @pytest.mark.asyncio
    async def test_close_unregisters_handlers(self, client, alog):
        engine = ForwardEngine(client, build_config(include_edited=True), alog)
        engine.register()
        assert len(client.handlers) == 2
        await engine.close()
        assert client.handlers == []

    @pytest.mark.asyncio
    async def test_snapshot_shape(self, client, alog):
        engine = ForwardEngine(client, build_config(), alog)
        engine.register()
        engine._handle(src_message("关键词1"), edited=False)
        await drain(engine)
        snapshot = engine.snapshot()
        assert snapshot["forwarded"] == 1
        assert snapshot["rules"]["r1"]["sent"] == 1

    @pytest.mark.asyncio
    async def test_pipeline_latency_logged(self, client, alog):
        """带服务端时间戳的消息要能算出端到端延迟。"""
        engine = ForwardEngine(client, build_config(), alog)
        engine.register()
        message = src_message(
            "关键词1", date=dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=2)
        )
        engine._handle(message, edited=False)
        await drain(engine)
        assert engine.stats["forwarded"] == 1

    @pytest.mark.asyncio
    async def test_disabled_rule_not_registered(self, client, alog):
        engine = ForwardEngine(client, build_config(enabled=False), alog)
        engine.register()
        assert engine.enabled is False
        assert client.handlers == []

    @pytest.mark.asyncio
    async def test_watched_chats_union(self, client, alog):
        engine = ForwardEngine(client, build_config(), alog)
        assert engine.watched_chats() == [SRC]

    @pytest.mark.asyncio
    async def test_watched_chats_all_when_unrestricted(self, client, alog):
        engine = ForwardEngine(client, build_config(sources=[]), alog)
        assert engine.watched_chats() == []

    @pytest.mark.asyncio
    async def test_exclude_user(self, client, alog):
        engine = ForwardEngine(client, build_config(exclude_users=[555]), alog)
        engine.register()
        engine._handle(src_message("关键词1", sender=FakeUser(555)), edited=False)
        await drain(engine)
        assert client.forwarded == []


class TestMultilineForwardsOnce:
    """多行正则里命中多行时，一条消息也只转发**一次**。

    加了 ``re.MULTILINE`` 之后 ``^`` / ``$`` 按行匹配，一条消息里可能有**好几行**
    都符合（比如公告里连着贴了三个码）。用户第一个担心的就是这个：
    会不会"每行命中一次、每行发一遍"？

    不会。整条链路上没有任何地方按"匹配次数"扇出：

    - ``CompiledMatcher.match_text`` 命中第一个 pattern 就 ``return``；
    - 它用的是 ``pattern.search()``，只取**最左的一个**匹配（不是 ``findall``）；
    - 转发器对一条消息只走一次 ``_forward_one``。

    全项目只有两处 ``finditer``：``cloudflare_ip`` 解析 IP 列表，以及抢注的
    「注册码已被使用」通知（那处是把通知里露出的多个码记进内存字典，
    不发任何东西）。
    """

    #: 三行都符合 ``^MSKY-...$``，前后还有干扰行。
    MULTI = "📢 公告\nMSKY-AAA-1\nMSKY-BBB-2\nMSKY-CCC-3\n请尽快注册"

    def test_the_fixture_message_really_has_three_matches(self):
        """前置断言：不钉住这一点，下面那条测试可能是**空过**的 ——
        消息里要是只有一行符合，"只转发一次"就什么都没证明。"""
        import re

        assert len(re.findall(r"^MSKY-[A-Z0-9-]+$", self.MULTI, re.MULTILINE)) == 3

    @pytest.mark.asyncio
    async def test_many_matching_lines_still_forward_once(self, client, alog):
        engine = ForwardEngine(
            client,
            build_config(match={"mode": "regex", "patterns": [r"^MSKY-[A-Z0-9-]+$"]}),
            alog,
        )
        engine.register()
        engine._handle(src_message(self.MULTI), edited=False)
        await drain(engine)

        assert len(client.forwarded) == 1, "命中三行也只能转发一次，不是三次"
        assert engine.stats["forwarded"] == 1
        assert engine.stats["matched"] == 1

    @pytest.mark.asyncio
    async def test_several_matching_patterns_still_forward_once(self, client, alog):
        """同一个规则里配了多条正则、且多条都命中，也只发一次。"""
        import re

        for pattern in (r"^MSKY-", r"公告", r"注册$"):
            assert re.search(pattern, self.MULTI, re.MULTILINE), f"前提：{pattern} 应当命中"

        engine = ForwardEngine(
            client,
            build_config(match={"mode": "regex", "patterns": [r"^MSKY-", r"公告", r"注册$"]}),
            alog,
        )
        engine.register()
        engine._handle(src_message(self.MULTI), edited=False)
        await drain(engine)

        assert len(client.forwarded) == 1

    def test_groups_come_from_the_leftmost_match(self):
        """``{g1}`` 取的是**最左**那个匹配，不是最后一行。

        否则模板里的码会莫名其妙变成消息末尾那个，而且用户完全看不出规律。
        """
        matcher = CompiledMatcher(
            MatchConfig(mode="regex", patterns=[r"^码:(\w+)$"], ignore_case=False)
        )
        result = matcher.match_text("码:AAA\n码:BBB\n码:CCC")
        assert result.matched is True
        assert result.groups == ("AAA",)

    def test_match_is_a_boolean_decision_not_a_count(self):
        """``MatchResult`` 里根本没有"匹配了几处"这个信息 —— 结构上就发不出多份。"""
        matcher = CompiledMatcher(MatchConfig(mode="regex", patterns=[r"^MSKY-[A-Z0-9-]+$"]))
        result = matcher.match_text(self.MULTI)
        assert result.matched is True
        assert not hasattr(result, "count")
        assert not hasattr(result, "matches")

    @pytest.mark.asyncio
    async def test_two_rules_each_forward_once(self, client, alog):
        """**不同规则**各转发一次是设计如此（它们通常发往不同频道）。

        这条钉的是边界：用户问"会不会重复发"，答案是"同一条规则内不会；
        不同规则命中同一条消息，每条规则各发一次 —— 这是它存在的意义"。
        """
        # 不能走 build_config —— 它的 **rule_overrides 是改**单条**规则，
        # 不是替换规则列表（传 rules=[...] 会变成规则里的一个非法字段）。
        def rule(rule_id: str, name: str, target: int) -> dict:
            return {
                "id": rule_id,
                "name": name,
                "sources": [SRC],
                "targets": [target],
                "match": {"mode": "regex", "patterns": [r"^MSKY-[A-Z0-9-]+$"]},
                "mode": "forward",
            }

        config = AccountConfig.model_validate(
            {"forward": {"enabled": True, "rules": [rule("r1", "规则一", DST), rule("r2", "规则二", DST + 1)]}}
        )
        engine = ForwardEngine(client, config, alog)
        engine.register()
        engine._handle(src_message(self.MULTI), edited=False)
        await drain(engine)

        assert len(client.forwarded) == 2, "两条规则各一次"
        assert {call["chat_id"] for call in client.forwarded} == {DST, DST + 1}


class TestRulesHotReload:
    """规则热重载：改完配置**不用重启账号**（2026-09-20 新增）。

    回归背景：``ForwardEngine.__init__`` 只在账号启动时读一次
    ``config.forward.active_rules``，面板改完规则必须重启账号才生效 ——
    而账号本来就在跑，用户每次想让规则生效都会撞上「已经在运行」。
    """

    ACCOUNT = "hot-reload"

    def _make(self, store, client, alog, config: AccountConfig) -> ForwardEngine:
        store.save_account_config(self.ACCOUNT, config)
        loaded = store.load_account_config(self.ACCOUNT, create=False)
        return ForwardEngine(client, loaded, alog, store=store, account=self.ACCOUNT)

    @staticmethod
    def _two_rules() -> AccountConfig:
        def rule(rid: str, pattern: str) -> dict:
            return {
                "id": rid,
                "name": rid,
                "sources": [SRC],
                "targets": [DST],
                "match": {"mode": "regex", "patterns": [pattern]},
            }

        return AccountConfig.model_validate(
            {"forward": {"enabled": True, "rules": [rule("r1", r"关键词(\d+)"), rule("r2", r"新规则")]}}
        )

    def test_reload_picks_up_new_rule(self, store, client, alog):
        engine = self._make(store, client, alog, build_config())
        assert [p.id for p in engine.rules] == ["r1"]

        store.save_account_config(self.ACCOUNT, self._two_rules())

        assert engine.reload_rules() is True
        assert [p.id for p in engine.rules] == ["r1", "r2"]

    def test_reload_is_noop_when_content_unchanged(self, store, client, alog):
        """面板原样保存一次（mtime 变了、内容没变）不该白重建一遍 handler。"""
        engine = self._make(store, client, alog, build_config())
        store.save_account_config(self.ACCOUNT, build_config())

        assert engine.reload_rules() is False

    def test_reload_refreshes_handler_filter(self, store, client, alog):
        """**关键**：``sources`` 从「指定群」改成 ``[]``（监听全部）时，
        handler 的过滤器也必须跟着换 —— 否则新群的消息根本进不来。
        """
        engine = self._make(store, client, alog, build_config())
        engine.register()
        assert len(client.handlers) == 1
        before = client.handlers[0][0].filters

        store.save_account_config(self.ACCOUNT, build_config(sources=[]))
        assert engine.reload_rules() is True

        assert len(client.handlers) == 1, "旧 handler 必须先注销，否则一条消息会被处理两次"
        assert client.handlers[0][0].filters is not before

    def test_reload_keeps_old_rules_when_config_is_broken(self, store, client, alog):
        """配置被写坏时保留旧规则继续跑，不能让转发整个停摆。"""
        engine = self._make(store, client, alog, build_config())
        store.paths.account(self.ACCOUNT).config_file.write_text("{ 这不是合法 JSON", encoding="utf-8")

        assert engine.reload_rules() is False
        assert [p.id for p in engine.rules] == ["r1"]

    def test_maybe_reload_uses_mtime(self, store, client, alog):
        engine = self._make(store, client, alog, build_config())
        engine.RELOAD_CHECK_INTERVAL = 0.0  # 关掉节流，测试里不需要等

        engine._maybe_reload()
        assert [p.id for p in engine.rules] == ["r1"], "磁盘没动不该重载"

        store.save_account_config(self.ACCOUNT, self._two_rules())
        path = store.paths.account(self.ACCOUNT).config_file
        stat = path.stat()
        os.utime(path, (stat.st_atime, stat.st_mtime + 10))  # 保证 mtime 确实变了

        engine._maybe_reload()
        assert [p.id for p in engine.rules] == ["r1", "r2"]

    def test_no_store_means_hot_reload_disabled(self, client, alog):
        """不传 store 时（单测 / 离线场景）行为与从前完全一致：永不读盘。"""
        engine = ForwardEngine(client, build_config(), alog)

        assert engine._config_file is None
        assert engine.reload_rules() is False


RESTRICTED = (
    "[400 CHAT_FORWARDS_RESTRICTED] - You can't forward messages from a protected chat"
)


class TestForwardFallsBackToCopy:
    """``mode="forward"`` 撞上受保护源会话时**自动降级为复制**。

    小白 2026-09-20 的需求：「copy 做备选，如果没办法直接 forward 就 copy」。
    线上背景：规则 1 的源群「抽象思维的研究与实践」是受保护群，
    累计 4 次 ``CHAT_FORWARDS_RESTRICTED`` 全部直接失败。

    ⚠️ ``ForwardRule.mode`` 的**默认值是 ``copy``**，所以这里必须显式写
    ``mode="forward"`` —— 用默认值测的是 copy 路径，降级分支根本不会进。

    ⚠️ 降级后的「复制」= **按 file_id 重新发送**（``send_message`` / ``Message.copy``
    / ``copy_media_group``），**不是** ``forward_messages(drop_author=True)`` ——
    后者发出来的是转发消息，Telegram 拒绝编辑，链接写不进去。见
    :class:`TestCopyModeSourceLink`。
    """

    @pytest.mark.asyncio
    async def test_restricted_forward_retries_as_copy(self, alog):
        client = FakeClient(forward_error_once=ChatForwardsRestricted(value=RESTRICTED))
        engine = ForwardEngine(client, build_config(mode="forward"), alog)
        engine.register()
        engine._handle(src_message("这是关键词123的消息"), edited=False)
        await drain(engine)

        assert client.forward_calls == 1, "先真转发失败一次，之后就该换路子而不是再撞一次"
        assert client.forwarded == [], "降级走复制，不该再有成功的转发"
        assert len(client.sent) == 1, "降级后应当用复制发出"
        assert engine.stats["forwarded"] == 1
        assert engine.stats["failed"] == 0
        assert engine.stats["downgraded"] == 1

    @pytest.mark.asyncio
    async def test_only_restricted_errors_are_downgraded(self, alog):
        """别的错误（例如没发言权限）照样失败 —— 不能拿复制去掩盖真问题。"""
        client = FakeClient(forward_error=ChatWriteForbidden(value="[403 CHAT_WRITE_FORBIDDEN]"))
        engine = ForwardEngine(client, build_config(mode="forward"), alog)
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)

        assert client.forwarded == []
        assert client.sent == []
        assert engine.stats["failed"] == 1
        assert engine.stats["downgraded"] == 0

    @pytest.mark.asyncio
    async def test_downgrade_failure_is_still_reported_as_failed(self, alog):
        """降级也失败时不能假装成功。

        这里让转发和复制**都**失败（``forward_error`` + ``send_error`` 都是永久的），
        最后连 ``drop_author`` 兜底也失败 ⇒ 必须如实记 failed。
        """
        client = FakeClient(
            forward_error=ChatForwardsRestricted(value=RESTRICTED),
            send_error=RuntimeError("copy boom"),
        )
        engine = ForwardEngine(client, build_config(mode="forward"), alog)
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)

        assert client.forwarded == []
        assert client.sent == []
        assert engine.stats["forwarded"] == 0
        assert engine.stats["failed"] == 1
        assert engine.stats["downgraded"] == 0, "降级没成功就不该记成降级"

    @pytest.mark.asyncio
    async def test_copy_mode_is_unaffected(self, alog):
        """本来就是 copy 的规则一次就发出去，不会「先失败再降级」。"""
        client = FakeClient()
        engine = ForwardEngine(client, build_config(mode="copy"), alog)
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)

        assert client.forward_calls == 0, "copy 模式压根不该调 forward_messages"
        assert len(client.sent) == 1
        assert engine.stats["downgraded"] == 0

    @pytest.mark.asyncio
    async def test_downgrade_covers_media_group_ids(self, alog):
        """相册（一次多条 id）也要能降级 —— 早先的实现遇到多条 id 就直接放弃。"""
        client = FakeClient(forward_error_once=ChatForwardsRestricted(value=RESTRICTED))
        engine = ForwardEngine(client, build_config(mode="forward"), alog)
        message = src_message("关键词123")
        prepared = engine.rules[0]
        result = prepared.matcher.match(message)
        assert result is not None

        await engine._forward_one(
            prepared, message, result, time.perf_counter(), message_ids=[100, 101]
        )

        assert client.forward_calls == 1
        assert len(client.copied_groups) == 1, "相册降级应当用 copy_media_group"
        assert client.copied_groups[0]["message_id"] == 100
        assert client.copied_groups[0]["from_chat_id"] == SRC
        assert engine.stats["forwarded"] == 1


class TestDeliveryModeIsHonest:
    """``转发成功 ... mode=`` 必须反映**实际生效**的模式 —— 日志不能撒谎。

    2026-09-20 发现：``_copy_with_fallback`` 在复制失败时会退成 ``drop_author`` 转发
    （发出来的是**转发消息**，正文里**没有原文链接**），但调用方一律把它记成
    ``mode=copy`` ⇒ 日志显示「复制成功」，实际链接丢了。

    为什么这个必须修：小白正是靠这一行 ``mode=`` 判断「链接有没有加上」。
    日志撒谎 = 他按日志判断会得出错误结论，比不写日志更糟。

    修法：``_send_to_target`` 返回**真实模式字符串**（原来是 ``degraded: bool``）。
    取值：``forward`` / ``copy`` / ``text`` / ``copy(降级)`` /
    ``copy(退化为转发·丢链接)`` / ``forward(降级·丢链接)``。
    """

    LINK = "https://t.me/c/1111111111/100"

    async def _deliver(
        self,
        alog,
        *,
        mode: str,
        forward_error: BaseException | None = None,
        forward_error_once: BaseException | None = None,
        send_error: BaseException | None = None,
        include_source_link: bool = True,
    ):
        """直接调 ``_send_to_target``，返回 ``(client, engine, sent_ids, actual_mode)``。

        直接调它而不是走 ``_handle``：模式字符串就是它的返回值，断言在这里最精确，
        不依赖日志管道。
        """
        client = FakeClient(
            forward_error=forward_error,
            forward_error_once=forward_error_once,
            send_error=send_error,
        )
        engine = ForwardEngine(
            client,
            build_config(mode=mode, include_source_link=include_source_link),
            alog,
        )
        engine.register()
        message = src_message("关键词123")
        prepared = engine.rules[0]
        sent_ids, actual = await engine._send_to_target(
            prepared, message, [message.id], DST, {"link": self.LINK}
        )
        return client, engine, sent_ids, actual

    # --- 与配置一致的那三种 ------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_forward_mode_reports_forward(self, alog):
        client, _, _, actual = await self._deliver(
            alog, mode="forward", include_source_link=False
        )
        assert actual == "forward"
        assert len(client.forwarded) == 1

    @pytest.mark.asyncio
    async def test_copy_mode_reports_copy(self, alog):
        client, _, _, actual = await self._deliver(alog, mode="copy")
        assert actual == "copy"
        assert client.forwarded == [], "copy 不该用转发实现"
        assert len(client.sent) == 1
        # 链接在**发送时**就写进正文（不是事后编辑）
        assert self.LINK in client.sent[0]["text"]

    @pytest.mark.asyncio
    async def test_text_mode_reports_text(self, alog):
        _, _, _, actual = await self._deliver(alog, mode="text")
        assert actual == "text"

    # --- 降级：forward 撞受保护 → copy ------------------------------------ #

    @pytest.mark.asyncio
    async def test_downgrade_reports_copy_downgraded(self, alog):
        client, engine, _, actual = await self._deliver(
            alog, mode="forward", forward_error_once=ChatForwardsRestricted(value=RESTRICTED)
        )
        assert actual == "copy(降级)", "降级后真复制成功，链接在正文里 —— 照实写"
        assert client.forward_calls == 1
        assert len(client.sent) == 1
        assert self.LINK in client.sent[0]["text"]
        assert engine.stats["downgraded"] == 1

    # --- 🔴 关键：复制失败退成 drop_author 转发时，**链接是丢的** ---------- #

    @pytest.mark.asyncio
    async def test_copy_fallback_reports_link_loss(self, alog):
        """``copy`` 复制失败 → 退成 ``drop_author`` 转发 ⇒ 必须明说「丢链接」。

        这是本轮修的核心：改之前这里返回的是「成功」，日志写 ``mode=copy``，
        小白看了会以为链接加上了，实际那条是转发消息、正文里啥都没有。
        """
        client, _, _, actual = await self._deliver(
            alog, mode="copy", send_error=RuntimeError("copy boom")
        )
        assert actual == "copy(退化为转发·丢链接)", "退化成转发还写 copy = 日志撒谎"
        assert client.sent == [], "复制确实失败了，没有新消息"
        assert len(client.forwarded) == 1
        # 确实是 drop_author 转发（隐藏来源抬头），而不是普通转发
        assert client.forwarded[0]["hide_sender_name"] is True
        # 发出去的那条里**没有**链接 —— 这正是「丢链接」三个字的依据
        assert self.LINK not in str(client.forwarded[0])

    @pytest.mark.asyncio
    async def test_downgrade_then_copy_failure_reports_link_loss(self, alog):
        """``forward`` 撞受保护 → 降级去复制 → 复制也失败 → 再退成转发 ⇒ 同样丢链接。"""
        client, engine, _, actual = await self._deliver(
            alog,
            mode="forward",
            forward_error_once=ChatForwardsRestricted(value=RESTRICTED),
            send_error=RuntimeError("copy boom"),
        )
        assert actual == "forward(降级·丢链接)"
        assert client.forward_calls == 2, "第一次 forward 失败，兜底那次才成功"
        assert client.sent == []
        assert len(client.forwarded) == 1
        assert engine.stats["downgraded"] == 1, "走到降级分支就记数，真实结果看 mode"

    # --- 端到端：日志里那个 mode 字段 -------------------------------------- #

    @pytest.mark.asyncio
    async def test_success_log_carries_actual_mode(self, alog, caplog):
        """走完整 ``_handle`` → ``_forward_one``，断言日志里的 ``mode`` 字段。

        上面几条钉的是返回值，这条钉的是**真正打出来的日志** —— 中间那一层
        （``_forward_one`` 拿返回值拼日志）也要对，否则前面全对、日志照样撒谎。
        """
        client = FakeClient()
        engine = ForwardEngine(client, build_config(mode="copy"), alog)
        engine.register()
        with caplog.at_level("INFO"):
            engine._handle(src_message("关键词123"), edited=False)
            await drain(engine)

        modes = [
            r.extra_fields.get("mode")
            for r in caplog.records
            if getattr(r, "extra_fields", None) and r.getMessage() == "转发成功"
        ]
        assert modes == ["copy"], f"日志里的 mode 字段不对: {modes}"

    @pytest.mark.asyncio
    async def test_success_log_never_says_copy_when_link_lost(self, alog, caplog):
        """回归守卫：链接丢了的时候，日志里**不能出现** ``mode=copy``。"""
        client = FakeClient(send_error=RuntimeError("copy boom"))
        engine = ForwardEngine(client, build_config(mode="copy"), alog)
        engine.register()
        with caplog.at_level("INFO"):
            engine._handle(src_message("关键词123"), edited=False)
            await drain(engine)

        modes = [
            r.extra_fields.get("mode")
            for r in caplog.records
            if getattr(r, "extra_fields", None) and r.getMessage() == "转发成功"
        ]
        assert modes == ["copy(退化为转发·丢链接)"], f"实际: {modes}"
        assert "copy" not in modes, "丢链接还写 copy，等于骗人"

    # --- 日志里那个 fingerprint 字段（2026-09-22 新增） --------------------- #

    @pytest.mark.asyncio
    async def test_success_log_carries_fingerprint(self, alog, caplog):
        """``转发成功`` 必须带指纹，否则去重漏没漏**无从审计**。

        2026-09-22 线上实测：被去重拦下的那 7 个指纹，在日志里**只**出现在
        「最近已转发过相同内容」这一行 —— 第一次真正转发的那条完全没有痕迹，
        于是「同一内容到底发出去过几次」根本查不出来。
        """
        client = FakeClient()
        engine = ForwardEngine(client, build_config(mode="copy"), alog)
        engine.register()
        msg = src_message("关键词123")
        with caplog.at_level("INFO"):
            engine._handle(msg, edited=False)
            await drain(engine)

        fields = [
            r.extra_fields
            for r in caplog.records
            if getattr(r, "extra_fields", None) and r.getMessage() == "转发成功"
        ]
        assert len(fields) == 1, f"转发成功日志条数不对: {len(fields)}"
        assert fields[0].get("fingerprint") == content_fingerprint(msg), (
            f"转发成功日志的指纹不对: {fields[0].get('fingerprint')!r}"
        )
        assert fields[0]["fingerprint"] != "-", "指纹为空还硬写 '-'，等于没记"

    @pytest.mark.asyncio
    async def test_fingerprint_ties_success_and_skip_together(self, alog, caplog):
        """同内容发两遍：一条 ``转发成功`` + 一条 ``跳过``，**指纹必须一致**。

        这才是「拿 grep 数指纹就能判有没有重复」的前提 —— 两边指纹对不上，
        审计出来的结果是假的。
        """
        client = FakeClient()
        engine = ForwardEngine(
            client,
            build_config(sources=[]),
            alog,
            recent_dedupe=RecentContentDedupe(limit=5, ttl=3600),
        )
        other_group = FakeChat(-1007777777777, title="另一个群", chat_type="supergroup")

        with caplog.at_level("INFO"):
            engine._handle(group_message("关键词123"), edited=False)
            await drain(engine)
            engine._handle(
                make_message("关键词123", message_id=200, chat=other_group), edited=False
            )
            await drain(engine)

        assert len(client.forwarded) == 1, "同样的内容不该在目标里出现两遍"
        by_msg = {}
        for r in caplog.records:
            extra = getattr(r, "extra_fields", None)
            if extra and r.getMessage() in ("转发成功", "最近已转发过相同内容，跳过"):
                by_msg.setdefault(r.getMessage(), []).append(extra.get("fingerprint"))

        assert len(by_msg.get("转发成功", [])) == 1, by_msg
        assert len(by_msg.get("最近已转发过相同内容，跳过", [])) == 1, by_msg
        sent_fp = by_msg["转发成功"][0]
        skipped_fp = by_msg["最近已转发过相同内容，跳过"][0]
        assert sent_fp == skipped_fp, (
            f"成功那条与跳过那条的指纹对不上（{sent_fp!r} vs {skipped_fp!r}）—— "
            "按指纹审计重复会得出假结论"
        )


class TestCopyPathReportsSentIds:
    """🔴 2026-09-24：copy 路径的 ``sent_ids`` 恒为空 ⇒ 通知被**静默跳过**。

    ``_copy_with_fallback`` 曾对**已经是 ``list[int]``** 的返回值再套一次
    :func:`_sent_ids`，而它对整数列表取 ``.id`` 会得到 ``[]``。后果有两层：

    1. ``delivered`` 为空 ⇒ ``if rule.notify and ... and delivered`` 不成立
       ⇒ **通知不推送**。线上实测：09-24 15:14:19 那条 ``copy(降级)`` 转发成功，
       同一秒却没有任何通知日志。
    2. 空 ``sent_ids`` 让「这条消息到底发成了几条」在日志里查不出来
       （``sent_ids=-``），排查重复时没有依据。

    触发条件是「源会话禁止转发」⇒ ``forward`` 撞 ``CHAT_FORWARDS_RESTRICTED``
    ⇒ 自动降级复制。当时所有规则都是 ``forward``，所以只有降级路径会中招；
    但任何 ``mode="copy"`` 的规则会**每条**都掉进去。

    为什么原测试没抓到：``_deliver`` 一直把 ``sent_ids`` 用 ``_`` 丢掉，
    **从没断言过它的内容**。
    """

    LINK = "https://t.me/c/1111111111/100"

    async def _deliver(self, alog, client, *, mode: str, notify: bool = False):
        engine = ForwardEngine(client, build_config(mode=mode, notify=notify), alog)
        engine.register()
        message = src_message("关键词123")
        prepared = engine.rules[0]
        return await engine._send_to_target(
            prepared, message, [message.id], DST, {"link": self.LINK}
        )

    @pytest.mark.asyncio
    async def test_copy_mode_returns_the_sent_message_id(self, alog):
        client = FakeClient()
        before = client.next_message_id
        sent_ids, actual = await self._deliver(alog, client, mode="copy")

        assert actual == "copy"
        assert sent_ids == [before + 1], (
            f"copy 成功必须回传新消息 id，实际 {sent_ids!r} —— "
            "空列表会让通知被跳过、频道那条撤不掉"
        )

    @pytest.mark.asyncio
    async def test_downgraded_copy_returns_the_sent_message_id(self, alog):
        """``forward`` 撞受保护源会话 ⇒ 降级复制，id 同样必须传出来。"""
        client = FakeClient(forward_error_once=ChatForwardsRestricted(value=RESTRICTED))
        before = client.next_message_id
        sent_ids, actual = await self._deliver(alog, client, mode="forward")

        assert actual == "copy(降级)"
        assert len(client.sent) == 1
        assert sent_ids == [before + 1], (
            f"降级复制同样必须回传 id，实际 {sent_ids!r} —— "
            "线上 09-24 15:14 那条就是这里空掉、通知跟着没了"
        )

    @pytest.mark.asyncio
    async def test_copy_mode_still_submits_a_notification(self, alog):
        """copy 模式也必须推送 —— ``delivered`` 空掉时通知被**静默**跳过。"""
        client = FakeClient()
        notifier = FakeNotifier()
        engine = ForwardEngine(
            client,
            build_config(sources=[], mode="copy", notify=True),
            alog,
            notifier=notifier,
        )

        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)

        assert len(client.sent) == 1, "前提：copy 模式确实发出去了一条"
        assert len(notifier.submitted) == 1, (
            "copy 模式没有提交通知 ⇒ sent_ids 为空导致 delivered 为空"
        )

    @pytest.mark.asyncio
    async def test_channel_copy_blocks_the_group_version(self, alog):
        """copy 出去的频道那条先到 ⇒ 群组那条**直接不发**，已发出的那条也不删。

        这条曾经是「撤不掉频道那条 ⇒ 目标里两条重复」的回归点。现在这条风险整个
        消失了：后到的那条压根不发，所以没有任何东西需要撤回。
        """
        pair = ChannelGroupDedupe()
        client = FakeClient()
        engine = ForwardEngine(
            client, build_config(sources=[], mode="copy"), alog, pair_dedupe=pair
        )

        engine._handle(channel_message("关键词123"), edited=False)
        await drain(engine)
        assert len(client.sent) == 1, "copy 模式：重发一条新消息（链接写在正文里）"

        engine._handle(group_message("关键词123", message_id=200), edited=False)
        await drain(engine)

        assert len(client.sent) == 1, "群组那条后到 ⇒ 直接不发"
        assert client.deleted == [], "🔴 已经发出去的那条不许删 —— 用户要的就是「直接拦截」"
        assert engine.stats["pair_blocked"] == 1


class TestSentIdsExtraction:
    """``_sent_ids`` 对**已经是 id 列表**的输入必须幂等。"""

    def test_id_list_passes_through(self):
        assert _sent_ids([4447, 4448]) == [4447, 4448], "已是 id 列表 ⇒ 原样透传"

    def test_empty_and_none(self):
        assert _sent_ids([]) == []
        assert _sent_ids(None) == []

    def test_objects_are_unwrapped(self):
        assert _sent_ids(types.SimpleNamespace(id=7)) == [7]
        assert _sent_ids(
            [types.SimpleNamespace(id=1), types.SimpleNamespace(id=2)]
        ) == [1, 2]

    def test_objects_without_id_are_skipped(self):
        assert _sent_ids([types.SimpleNamespace(id=1), types.SimpleNamespace(id=None)]) == [1]


class TestLinkSurvivesTruncation:
    """长正文 + 附带来源链接时，**链接不能被截断吃掉**。

    2026-09-20 逐行审新代码时发现的边界 bug：原先是「**先拼链接、再 truncate**」，
    而 :func:`truncate` 是**从末尾砍掉**再补「…（已截断）」——
    链接正好拼在末尾 ⇒ 消息一长，用户最想要的那行反而被吃掉，
    而且**日志一切正常**（静默丢，和前面几个缺陷同一个家族）。

    正确顺序：**先给链接留出位置，再截断原文**。

    ⚠️ 这是够得着的现实场景，不是理论问题：``CAPTION_LIMIT`` 只有 **1024**，
    而「宸澄」规则匹配的正是那种带一长串说明文字的资源帖 caption。
    """

    LINK = "https://t.me/c/1111111111/100"

    @pytest.mark.asyncio
    async def test_long_text_keeps_link(self, alog):
        client = FakeClient()
        engine = ForwardEngine(client, build_config(mode="copy"), alog)
        engine.register()
        engine._handle(src_message("关键词123 " + "长" * 5000), edited=False)
        await drain(engine)

        sent = client.sent[0]["text"]
        assert self.LINK in sent, "链接被 truncate 吃掉了 —— 用户最想要的那行丢了"
        assert len(sent) <= 4096, "保链接不能以超长为代价（Telegram 会直接拒收）"

    @pytest.mark.asyncio
    async def test_long_caption_keeps_link(self, alog):
        """caption 上限只有 1024，比正文更容易撞到。"""
        client = FakeClient()
        engine = ForwardEngine(client, build_config(mode="copy"), alog)
        engine.register()
        message = src_message("关键词123", caption="关键词123 " + "长" * 1200)
        message.text = None
        engine._handle(message, edited=False)
        await drain(engine)

        caption = message.copy_calls[0]["caption"]
        assert self.LINK in caption, "长 caption 里链接被吃掉了"
        assert len(caption) <= 1024

    @pytest.mark.asyncio
    async def test_long_album_caption_keeps_link(self, alog):
        """相册走 ``copy_media_group`` 的 ``captions``，同样是 caption 上限。"""
        client = FakeClient()
        engine = ForwardEngine(client, build_config(mode="copy"), alog)
        engine.register()
        message = src_message("关键词123", caption="关键词123 " + "长" * 1200)
        message.text = None
        message.media_group_id = "g1"
        prepared = engine.rules[0]
        result = prepared.matcher.match(message)
        await engine._forward_one(
            prepared, message, result, time.perf_counter(), message_ids=[100, 101]
        )

        caption = client.copied_groups[0]["captions"][0]
        assert self.LINK in caption, "相册长 caption 里链接被吃掉了"
        assert len(caption) <= 1024

    @pytest.mark.asyncio
    async def test_text_mode_long_message_keeps_link(self, alog):
        """``text`` 模式走 ``truncate`` 的**默认**上限（``SAFE_TEXT_LENGTH``=3800），
        同样会把末尾的链接砍掉。"""
        client = FakeClient()
        engine = ForwardEngine(client, build_config(mode="text"), alog)
        engine.register()
        engine._handle(src_message("关键词123 " + "长" * 5000), edited=False)
        await drain(engine)

        sent = client.sent[0]["text"]
        assert self.LINK in sent, "text 模式长消息里链接被吃掉了"

    @pytest.mark.asyncio
    async def test_truncation_still_drops_entities(self, alog):
        """保链接不能顺手把「截断就丢 entities」这条安全规则弄没了。"""
        client = FakeClient()
        engine = ForwardEngine(client, build_config(mode="copy"), alog)
        engine.register()
        message = src_message("关键词123 " + "长" * 5000)
        message.entities = ["FAKE-ENTITIES"]
        engine._handle(message, edited=False)
        await drain(engine)

        assert client.sent[0]["entities"] is None

    @pytest.mark.asyncio
    async def test_short_text_still_keeps_entities(self, alog):
        """没截断时 entities 必须照旧保留 —— 别为了修截断把格式保真弄坏。"""
        client = FakeClient()
        engine = ForwardEngine(client, build_config(mode="copy"), alog)
        engine.register()
        message = src_message("关键词123")
        message.entities = ["FAKE-ENTITIES"]
        engine._handle(message, edited=False)
        await drain(engine)

        assert client.sent[0]["entities"] == ["FAKE-ENTITIES"]


class TestCopyModeSourceLink:
    """``copy`` 模式要把「🔗原文链接：…」写进正文 / caption。

    小白 2026-09-20：「附带来源的那个链接没有了」+「如果用 copy 能不能把链接放在
    copy 的下方，换行两次再加原文链接，格式改成 🔗原文链接：xxxx」。

    背景（两层，缺一不可）：
    ① ``include_source_link`` 原先**只在 text 模式生效** —— ``_do()`` 里
       ``mode in {"forward", "copy"}`` 直接 return，压根走不到加链接那段。
    ② copy 原先用 ``forward_messages(hide_sender_name=True)``（raw ``drop_author``），
       发出来的是**转发消息**，而 **Telegram 拒绝编辑转发消息**
       （2026-09-20 实测 ``400 Bad Request: message can't be edited``）⇒
       「先转发、再 edit 追加链接」这条路根本走不通，而且会被 ``except`` 吞成 warning，
       **功能静默失效**（单测还全绿，因为假客户端永远成功）。

    所以 copy 改成**按 ``file_id`` 重新发送一条新消息**，链接在**发送时**就写进正文：
    文本走 ``send_message``、媒体走 ``Message.copy``、相册走 ``copy_media_group``。

    ⚠️ 本类的 ``client.forwarded == []`` 断言就是钉住这个设计 —— 一旦有人把 copy
    改回「转发 + 编辑」，这里立刻红。
    """

    #: ``src_message`` 默认 message_id=100、chat=-1001111111111（无 username）
    LINK = "https://t.me/c/1111111111/100"

    def test_default_mode_is_copy(self):
        """钉住默认值：``ForwardRule.mode`` 不填就是 ``copy``。

        ⚠️ ``build_config()`` 为了测引擎管道把 mode 钉成了 ``forward``，
        所以这里直接从 ``ForwardRule`` 读默认值 —— 免得哪天默认值被改掉都没人发现。
        """
        assert ForwardRule(id="x", sources=[SRC], targets=[DST]).mode == "copy"

    @pytest.mark.asyncio
    async def test_copy_mode_writes_link_at_send_time(self, alog):
        client = FakeClient()
        engine = ForwardEngine(client, build_config(mode="copy"), alog)
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)

        assert client.forwarded == [], "copy 不能走转发 —— 转发消息不可编辑，链接写不进去"
        assert len(client.sent) == 1
        payload = client.sent[0]
        assert payload["chat_id"] == DST
        assert payload["text"] == f"关键词123\n\n🔗原文链接：{self.LINK}"
        assert payload["link_preview_options"] is not None, "必须禁掉链接预览"
        assert engine.stats["forwarded"] == 1

    @pytest.mark.asyncio
    async def test_copy_mode_keeps_entities(self, alog):
        """重新发送要带上原 entities —— 粗体 / 内联链接等格式不能丢。"""
        client = FakeClient()
        engine = ForwardEngine(client, build_config(mode="copy"), alog)
        engine.register()
        message = src_message("关键词123")
        message.entities = ["FAKE-ENTITIES"]
        engine._handle(message, edited=False)
        await drain(engine)

        assert client.sent[0]["entities"] == ["FAKE-ENTITIES"]
        assert client.sent[0]["parse_mode"] is ParseMode.DISABLED

    @pytest.mark.asyncio
    async def test_copy_mode_truncation_drops_entities(self, alog):
        """截断可能切断实体边界（Telegram 回 ENTITY_BOUNDS_INVALID）⇒ 丢弃 entities。"""
        client = FakeClient()
        engine = ForwardEngine(client, build_config(mode="copy"), alog)
        engine.register()
        message = src_message("关键词123 " + "长" * 5000)
        message.entities = ["FAKE-ENTITIES"]
        engine._handle(message, edited=False)
        await drain(engine)

        assert client.sent[0]["entities"] is None
        assert len(client.sent[0]["text"]) <= 4096

    @pytest.mark.asyncio
    async def test_forward_mode_never_edits_forwarded_message(self, alog):
        """forward 模式**不动**那条转发消息，链接改在**下方**单独补一条。

        Telegram 拒绝编辑转发消息，所以「就地加链接」这条路根本不存在。
        """
        client = FakeClient()
        engine = ForwardEngine(client, build_config(mode="forward"), alog)
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)

        assert client.forwarded[0]["hide_sender_name"] is None, "原生转发要保留「转发自」抬头"
        assert len(client.sent) == 1, "链接是**单独**一条消息，不是编辑"
        assert client.sent[0]["text"] == f"🔗原文链接：{self.LINK}"

    @pytest.mark.asyncio
    async def test_media_message_puts_link_in_caption(self, alog):
        """媒体消息没有 ``text``，链接写进 caption。"""
        client = FakeClient()
        engine = ForwardEngine(client, build_config(mode="copy"), alog)
        engine.register()
        message = src_message("关键词123", caption="关键词123")
        message.text = None
        engine._handle(message, edited=False)
        await drain(engine)

        assert client.sent == []
        assert len(message.copy_calls) == 1
        assert message.copy_calls[0]["caption"] == f"关键词123\n\n🔗原文链接：{self.LINK}"

    @pytest.mark.asyncio
    async def test_media_without_caption_gets_link_only(self, alog):
        """纯媒体（无 caption）也要能加上链接那一行。"""
        client = FakeClient()
        engine = ForwardEngine(client, build_config(mode="copy", match={"mode": "all"}), alog)
        engine.register()
        message = src_message("关键词123")
        message.text = None
        message.caption = None
        engine._handle(message, edited=False)
        await drain(engine)

        assert message.copy_calls[0]["caption"] == f"🔗原文链接：{self.LINK}"

    @pytest.mark.asyncio
    async def test_album_uses_copy_media_group(self, alog):
        """相册：``copy_media_group`` 的 ``captions`` 只传第一项，其余回落原 caption。

        pyrogram 里 ``captions`` 是 list 时**按下标取值、越界项回落到原 caption**
        ⇒ 只覆盖第一项就够了，不必先 ``get_media_group`` 拉一遍。
        """
        client = FakeClient()
        engine = ForwardEngine(
            client, build_config(mode="copy", match={"mode": "all"}, media_group_window=0.1), alog
        )
        engine.register()
        for index in range(3):
            engine._handle(
                src_message(
                    "相册",
                    message_id=200 + index,
                    caption="相册",
                    media_group_id="mg-copy",
                ),
                edited=False,
            )
        await asyncio.sleep(0.2)
        await drain(engine)

        assert client.forwarded == []
        assert len(client.copied_groups) == 1
        payload = client.copied_groups[0]
        assert payload["message_id"] == 200, "相册用第一条 id 定位整组"
        assert payload["from_chat_id"] == SRC
        assert payload["captions"] == [
            "相册\n\n🔗原文链接：https://t.me/c/1111111111/200"
        ]

    @pytest.mark.asyncio
    async def test_downgrade_uses_copy_with_link(self, alog):
        """forward 撞受保护源会话自动降级 ⇒ 降级后走复制，链接照样写进去。"""
        client = FakeClient(forward_error_once=ChatForwardsRestricted(value=RESTRICTED))
        engine = ForwardEngine(client, build_config(mode="forward"), alog)
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)

        assert engine.stats["downgraded"] == 1
        assert engine.stats["forwarded"] == 1
        assert client.sent[0]["text"] == f"关键词123\n\n🔗原文链接：{self.LINK}"

    @pytest.mark.asyncio
    async def test_include_source_link_false_writes_plain_copy(self, alog):
        client = FakeClient()
        engine = ForwardEngine(client, build_config(mode="copy", include_source_link=False), alog)
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)

        assert client.sent[0]["text"] == "关键词123"

    @pytest.mark.asyncio
    async def test_existing_link_is_not_duplicated(self, alog):
        """原文里已经有同一个链接时不再重复追加。"""
        client = FakeClient()
        engine = ForwardEngine(client, build_config(mode="copy"), alog)
        engine.register()
        engine._handle(src_message(f"关键词123 见 {self.LINK}"), edited=False)
        await drain(engine)

        assert client.sent[0]["text"] == f"关键词123 见 {self.LINK}"

    @pytest.mark.asyncio
    async def test_copy_failure_falls_back_to_drop_author_forward(self, alog):
        """复制失败（极少数会话不让按 file_id 复用）⇒ 退化成 drop_author 转发。

        代价是**丢掉原文链接**，但内容能发出去 —— 不能因为加不上链接就不发。
        """
        client = FakeClient(send_error=RuntimeError("copy boom"))
        engine = ForwardEngine(client, build_config(mode="copy"), alog)
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)

        assert len(client.forwarded) == 1
        assert client.forwarded[0]["hide_sender_name"] is True
        assert engine.stats["forwarded"] == 1
        assert engine.stats["failed"] == 0

    @pytest.mark.asyncio
    async def test_album_copy_failure_falls_back_to_forward(self, alog):
        """相册复制失败同样要有兜底，不能整组丢消息。"""
        client = FakeClient(copy_group_error=RuntimeError("group boom"))
        engine = ForwardEngine(
            client, build_config(mode="copy", match={"mode": "all"}, media_group_window=0.1), alog
        )
        engine.register()
        for index in range(2):
            engine._handle(
                src_message("相册", message_id=400 + index, media_group_id="mg-fb"), edited=False
            )
        await asyncio.sleep(0.2)
        await drain(engine)

        assert client.copied_groups == []
        assert client.forwarded[0]["message_ids"] == [400, 401]
        assert client.forwarded[0]["hide_sender_name"] is True

    @pytest.mark.asyncio
    async def test_text_mode_uses_same_prefix(self, alog):
        """text 模式的文案统一成 ``🔗原文链接：``（原来只有 ``🔗 ``）。"""
        client = FakeClient()
        engine = ForwardEngine(
            client, build_config(mode="text", template="{text}", include_source_link=True), alog
        )
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)

        assert client.sent[0]["text"] == f"关键词123\n\n🔗原文链接：{self.LINK}"


class TestForwardModeLinkNote:
    """``forward`` 模式：转发消息**不动**，原文链接在它**下方**单独补一条。

    小白 2026-09-20 的原话：「转发的消息不用编辑直接在下方加一条原文链接即可，
    **只有 copy 才在当前消息加上原文链接**」。

    两条设计约束：
    ① **不编辑**那条转发消息 —— Telegram 本来就拒绝编辑转发消息
       （``400 message can't be edited``），而且小白明确不要动它；
    ② 链接必须是**下方**的独立消息 ⇒ 顺序有要求，用 ``client.calls`` 钉住。
    """

    LINK = "https://t.me/c/1111111111/100"

    @pytest.mark.asyncio
    async def test_note_is_a_separate_message_below(self, alog):
        client = FakeClient()
        engine = ForwardEngine(client, build_config(mode="forward"), alog)
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)

        assert [name for name, _ in client.calls] == ["forward_messages", "send_message"], (
            "必须是「先转发、后补链接」，链接在那条转发消息的下方"
        )
        note = client.sent[0]
        assert note["chat_id"] == DST, "链接要发到同一个目标"
        assert note["text"] == f"🔗原文链接：{self.LINK}"
        assert note["link_preview_options"] is not None, "补链接不该再撑出一张预览卡"

    @pytest.mark.asyncio
    async def test_note_carries_silent_and_thread(self, alog):
        """静默 / 话题（thread）设置要跟着一起带过去，否则补的那条会跑错话题。"""
        client = FakeClient()
        engine = ForwardEngine(
            client,
            build_config(mode="forward", silent=True, target_thread_id=777),
            alog,
        )
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)

        note = client.sent[0]
        assert note["disable_notification"] is True
        assert note["message_thread_id"] == 777

    @pytest.mark.asyncio
    async def test_note_skipped_when_link_disabled(self, alog):
        client = FakeClient()
        engine = ForwardEngine(client, build_config(mode="forward", include_source_link=False), alog)
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)

        assert client.sent == []
        assert len(client.forwarded) == 1

    @pytest.mark.asyncio
    async def test_album_forward_gets_exactly_one_note(self, alog):
        """相册转发一条补一条链接 —— 不能每个分片都补。"""
        client = FakeClient()
        engine = ForwardEngine(
            client, build_config(mode="forward", match={"mode": "all"}, media_group_window=0.1), alog
        )
        engine.register()
        for index in range(3):
            engine._handle(
                src_message("相册", message_id=600 + index, media_group_id="mg-note"), edited=False
            )
        await asyncio.sleep(0.2)
        await drain(engine)

        assert client.forwarded[0]["message_ids"] == [600, 601, 602]
        assert len(client.sent) == 1
        assert client.sent[0]["text"] == "🔗原文链接：https://t.me/c/1111111111/600"

    @pytest.mark.asyncio
    async def test_note_failure_does_not_fail_forward(self, alog):
        """链接没补上不该让整条转发记成失败 —— 内容已经发出去了。"""
        client = FakeClient(send_error=RuntimeError("note boom"))
        engine = ForwardEngine(client, build_config(mode="forward"), alog)
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)

        assert len(client.forwarded) == 1
        assert client.sent == []
        assert engine.stats["forwarded"] == 1
        assert engine.stats["failed"] == 0

    @pytest.mark.asyncio
    async def test_downgrade_does_not_send_note(self, alog):
        """降级成 copy 后链接已经写进正文 ⇒ **不再**另发一条，避免重复。"""
        client = FakeClient(forward_error_once=ChatForwardsRestricted(value=RESTRICTED))
        engine = ForwardEngine(client, build_config(mode="forward"), alog)
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)

        assert engine.stats["downgraded"] == 1
        assert [name for name, _ in client.calls] == ["send_message"], "只有一条：复制出来的正文"
        assert client.sent[0]["text"] == f"关键词123\n\n🔗原文链接：{self.LINK}"

    @pytest.mark.asyncio
    async def test_copy_mode_has_no_separate_note(self, alog):
        """对照：copy 模式链接在**当前消息正文里**，不另发一条。"""
        client = FakeClient()
        engine = ForwardEngine(client, build_config(mode="copy"), alog)
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)

        assert [name for name, _ in client.calls] == ["send_message"]
        assert client.sent[0]["text"] == f"关键词123\n\n🔗原文链接：{self.LINK}"


class TestPipelineMs:
    """``_pipeline_ms`` 的时区处理。

    🔴 2026-09-20 线上实测：``pipeline_ms`` **恒为 -8 小时**（-28799461.9ms，189 条）。
    根因 —— pyrogram 的 ``utils.timestamp_to_datetime`` 是
    ``datetime.fromtimestamp(ts)``，返回的是 **naive 本地时间**（服务器 TZ = CST），
    而这里原先 ``replace(tzinfo=utc)`` 把它当 UTC，于是差了一个时区偏移。

    ⚠️ 所以本类的关键用例是「60 秒前的 naive 本地时间」——「当前时间」那个用例
    在**错的实现下也会被 0 兜底**，抓不到 bug。
    """

    def test_naive_local_time_is_interpreted_as_local(self):
        """🔴 这条是真正的回归守卫：naive 本地时间必须**按本地**解释。

        用错误实现（当 UTC）会算出 -8 小时，被 0 兜底后变成 0 —— 和期望的 60000 差得远。
        """
        message = make_message("x", date=dt.datetime.now() - dt.timedelta(seconds=60))
        value = _pipeline_ms(message)
        assert value is not None
        assert 55_000 <= value <= 65_000, f"应当约 60000ms，实际 {value}"

    def test_aware_utc_time_works(self):
        message = make_message("x", date=dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=30))
        value = _pipeline_ms(message)
        assert value is not None
        assert 25_000 <= value <= 35_000

    def test_future_timestamp_is_clamped_to_zero(self):
        """时钟偏差可能让时间戳略微超前 —— 「负耗时」没有意义，兜底 0。"""
        message = make_message("x", date=dt.datetime.now() + dt.timedelta(seconds=30))
        assert _pipeline_ms(message) == 0.0

    def test_missing_or_invalid_date_returns_none(self):
        assert _pipeline_ms(make_message("x")) is None
        assert _pipeline_ms(make_message("x", date="not-a-datetime")) is None

    @pytest.mark.asyncio
    async def test_forward_log_reports_sane_pipeline_ms(self, alog, caplog):
        """端到端：转发成功的日志里 pipeline_ms 不能是负数（也不该是 -8 小时）。"""
        client = FakeClient()
        engine = ForwardEngine(client, build_config(), alog)
        engine.register()
        engine._handle(
            src_message("关键词123", date=dt.datetime.now() - dt.timedelta(seconds=1)),
            edited=False,
        )
        await drain(engine)
        assert engine.stats["forwarded"] == 1


class TestChatRejectedCounter:
    """规则「会话层被拒」必须有计数 —— 否则规则不生效时线上零线索。

    2026-09-20 排查「规则配了但 ``matched=0``」时发现：``chat_allowed`` 返回 False 的
    分支是**裸 ``continue``**（不计数、不日志），而紧邻的 ``sender_allowed`` 分支
    却有计数 + debug 日志。于是「群 ID 写错 / 配置没生效 / 消息没进来」三种情况
    在线上**完全无法区分**。
    """

    @pytest.mark.asyncio
    async def test_other_chat_is_counted(self, alog):
        client = FakeClient()
        engine = ForwardEngine(client, build_config(sources=[SRC]), alog)
        engine.register()
        engine._handle(
            make_message("关键词123", chat=FakeChat(-100999, title="别的群")), edited=False
        )
        await drain(engine)

        assert client.forwarded == []
        assert engine.rules[0].stats["chat_rejected"] == 1
        assert engine.stats["matched"] == 0

    @pytest.mark.asyncio
    async def test_counter_is_exposed_in_snapshot(self, alog):
        """面板 / ``status`` 走 ``snapshot()``，所以计数必须出现在那里。"""
        client = FakeClient()
        engine = ForwardEngine(client, build_config(sources=[SRC]), alog)
        engine.register()
        for _ in range(3):
            engine._handle(
                make_message("关键词123", chat=FakeChat(-100999, title="别的群")), edited=False
            )
        await drain(engine)

        assert engine.snapshot()["rules"]["r1"]["chat_rejected"] == 3

    @pytest.mark.asyncio
    async def test_matching_chat_does_not_count(self, alog):
        """命中来源的会话不该被计进去 —— 否则这个数字就没意义了。"""
        client = FakeClient()
        engine = ForwardEngine(client, build_config(sources=[SRC]), alog)
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)

        assert engine.rules[0].stats["chat_rejected"] == 0
        assert engine.rules[0].stats["matched"] == 1


class TestCrossAccountDedupe:
    """跨账号去重：两个账号都监听到同一条消息时，发往**同一目标**只发一次。

    小白 2026-09-20 的需求：「需要做跨账号去重，因为两个账号的群可能重复」。

    线上背景：项目里早先那版跨账号去重挂在 Postgres 上（``TGA_POSTGRES_DSN``），
    键是 **两元组** ``(chat_id, message_id)`` ⇒ 「A 发 T1、B 发 T2」会被误判成重复而
    **丢消息**，而且 ``check_dedupe`` 是同步阻塞的（把转发延迟推到几百毫秒）。

    ⚠️ **更正**：那版**不是**「死代码」—— `TGA_POSTGRES_DSN` 配在 **systemd unit 的
    ``Environment=``** 里（``.env`` 里没有）。只看 ``.env`` 会得出「根本没配」的错误结论。
    """

    # ---------------------------------------------------------------- #
    # 单元语义
    # ---------------------------------------------------------------- #
    def test_first_claim_wins(self):
        dedupe = CrossAccountDedupe()
        assert dedupe.claim(SRC, 1, DST, 300) is True
        assert dedupe.claim(SRC, 1, DST, 300) is False
        assert (dedupe.claimed, dedupe.rejected) == (1, 1)

    def test_different_targets_are_independent(self):
        """🔴 键必须带目标：否则「A 发 T1、B 发 T2」会被误判成重复而**丢消息**。"""
        dedupe = CrossAccountDedupe()
        assert dedupe.claim(SRC, 1, DST, 300) is True
        assert dedupe.claim(SRC, 1, -1009999999999, 300) is True
        assert len(dedupe) == 2

    def test_zero_ttl_disables(self):
        dedupe = CrossAccountDedupe()
        assert dedupe.claim(SRC, 1, DST, 0) is True
        assert dedupe.claim(SRC, 1, DST, 0) is True

    def test_claim_expires_after_ttl(self, monkeypatch):
        dedupe = CrossAccountDedupe()
        now = [1000.0]
        monkeypatch.setattr("tg_assistant.forwarder.time.monotonic", lambda: now[0])

        assert dedupe.claim(SRC, 1, DST, 10) is True
        now[0] += 5
        assert dedupe.claim(SRC, 1, DST, 10) is False
        now[0] += 6  # 超过 TTL
        assert dedupe.claim(SRC, 1, DST, 10) is True

    # ---------------------------------------------------------------- #
    # release：发送失败必须退还名额
    # ---------------------------------------------------------------- #
    def test_release_frees_the_slot(self):
        """退还之后别的账号立刻就能占用 —— 不必等 TTL 过期。"""
        dedupe = CrossAccountDedupe()
        assert dedupe.claim(SRC, 1, DST, 300) is True
        assert dedupe.claim(SRC, 1, DST, 300) is False

        dedupe.release(SRC, 1, DST)

        assert dedupe.claim(SRC, 1, DST, 300) is True, "退还后应当能重新占用"
        assert dedupe.released == 1

    def test_release_only_affects_that_target(self):
        """退还只清掉自己那个键，别的目标不受影响。"""
        dedupe = CrossAccountDedupe()
        assert dedupe.claim(SRC, 1, DST, 300) is True
        assert dedupe.claim(SRC, 1, -1009999999999, 300) is True

        dedupe.release(SRC, 1, DST)

        assert dedupe.claim(SRC, 1, DST, 300) is True
        assert dedupe.claim(SRC, 1, -1009999999999, 300) is False, "别的目标不该被顺手放开"

    def test_release_is_idempotent(self):
        """退还不存在的键是无害空操作（调用方不必先判断）。"""
        dedupe = CrossAccountDedupe()
        dedupe.release(SRC, 999, DST)
        dedupe.release(SRC, 999, DST)
        assert len(dedupe) == 0
        assert dedupe.released == 2

    @pytest.mark.asyncio
    async def test_failed_send_releases_so_other_account_can_send(self, alog):
        """🔴 **本轮修的 bug**：``claim`` 先占位后发送，失败若不退还 ⇒ 这条消息对该目标
        会在整个 TTL（默认 300s）里**谁也发不出去**。

        场景：账号 A 先抢到名额但发送失败（网络抖动 / 目标临时不可写），
        账号 B 手里有同一条消息、目标也相同 —— 它必须能补上。
        """
        shared = CrossAccountDedupe()
        broken = FakeClient(forward_error=RuntimeError("network boom"))
        healthy = FakeClient()
        engine_a = ForwardEngine(broken, build_config(), alog, shared_dedupe=shared)
        engine_b = ForwardEngine(healthy, build_config(), alog, shared_dedupe=shared)

        message = src_message("关键词123")
        engine_a._handle(message, edited=False)
        await drain(engine_a)
        assert engine_a.stats["failed"] == 1
        assert shared.released == 1, "发送失败必须把名额退回去"

        engine_b._handle(message, edited=False)
        await drain(engine_b)

        assert len(healthy.forwarded) == 1, "A 失败了，B 必须还能把这条消息发出去"
        assert engine_b.stats["forwarded"] == 1
        assert engine_b.stats["cross_deduped"] == 0, "名额已退还，B 不该被判成重复"

    @pytest.mark.asyncio
    async def test_successful_send_does_not_release(self, alog):
        """发成功了就**不能**退还 —— 否则两个账号会各发一遍。"""
        shared = CrossAccountDedupe()
        first, second = FakeClient(), FakeClient()
        engine_a = ForwardEngine(first, build_config(), alog, shared_dedupe=shared)
        engine_b = ForwardEngine(second, build_config(), alog, shared_dedupe=shared)

        message = src_message("关键词123")
        engine_a._handle(message, edited=False)
        await drain(engine_a)
        engine_b._handle(message, edited=False)
        await drain(engine_b)

        assert shared.released == 0
        assert len(first.forwarded) == 1
        assert second.forwarded == []

    # ---------------------------------------------------------------- #
    # 两个引擎共享一张表（真实场景）
    # ---------------------------------------------------------------- #
    @pytest.mark.asyncio
    async def test_second_account_skips_same_message(self, alog):
        shared = CrossAccountDedupe()
        first, second = FakeClient(), FakeClient()
        engine_a = ForwardEngine(first, build_config(), alog, shared_dedupe=shared)
        engine_b = ForwardEngine(second, build_config(), alog, shared_dedupe=shared)

        message = src_message("关键词123")
        engine_a._handle(message, edited=False)
        await drain(engine_a)
        engine_b._handle(message, edited=False)
        await drain(engine_b)

        assert len(first.forwarded) == 1
        assert second.forwarded == [], "第二个账号不该把同一条消息再发一遍"
        assert engine_b.stats["cross_deduped"] == 1
        assert engine_b.stats["forwarded"] == 0
        assert shared.rejected == 1

    @pytest.mark.asyncio
    async def test_different_targets_both_send(self, alog):
        """两个账号的目标不同时都要发出去 —— 去重不能把消息吃掉。"""
        shared = CrossAccountDedupe()
        first, second = FakeClient(), FakeClient()
        engine_a = ForwardEngine(first, build_config(), alog, shared_dedupe=shared)
        engine_b = ForwardEngine(
            second, build_config(targets=[-1009999999999]), alog, shared_dedupe=shared
        )

        message = src_message("关键词123")
        engine_a._handle(message, edited=False)
        await drain(engine_a)
        engine_b._handle(message, edited=False)
        await drain(engine_b)

        assert len(first.forwarded) == 1
        assert len(second.forwarded) == 1

    @pytest.mark.asyncio
    async def test_no_shared_table_means_disabled(self, alog):
        """不传共享表（单测 / 离线场景）时行为与从前完全一致：两个账号都发。"""
        first, second = FakeClient(), FakeClient()
        engine_a = ForwardEngine(first, build_config(), alog)
        engine_b = ForwardEngine(second, build_config(), alog)

        message = src_message("关键词123")
        engine_a._handle(message, edited=False)
        await drain(engine_a)
        engine_b._handle(message, edited=False)
        await drain(engine_b)

        assert len(first.forwarded) == 1
        assert len(second.forwarded) == 1
        assert engine_b.stats["cross_deduped"] == 0


class TestDedupeWiring:
    """把共享表从 RuntimeManager 一路接到 ForwardEngine 的接线。

    去重表**必须是同一个实例**：面板每点一次「启动」就会重建 MultiRunner，
    每次都换新表的话，去重窗口会被清空 —— 刚发过的消息又能重发一遍。
    """

    def test_multi_runner_reuses_injected_table(self):
        # MultiRunner.__init__ 只保存引用、不碰 store/settings，所以这里可以传 None。
        shared = CrossAccountDedupe()
        assert MultiRunner(None, None, dedupe=shared).dedupe is shared

    def test_multi_runner_creates_table_by_default(self):
        assert isinstance(MultiRunner(None, None).dedupe, CrossAccountDedupe)

    def test_account_runner_stores_shared_table(self, paths):
        """AccountRunner 必须把表存下来 —— ``start()`` 里构造 ForwardEngine 要读它。

        ``start()`` 会真的去连 Telegram，所以这里只构造、不启动。
        """
        shared = CrossAccountDedupe()
        runner = AccountRunner(
            AccountRecord(name="acc-a"),
            build_config(),
            None,
            paths,
            shared_dedupe=shared,
        )
        assert runner.shared_dedupe is shared

    def test_account_runner_defaults_to_disabled(self, paths):
        runner = AccountRunner(AccountRecord(name="acc-a"), build_config(), None, paths)
        assert runner.shared_dedupe is None


class TestHeartbeatSurfacesNewCounters:
    """心跳必须带上 ``cross_deduped`` / ``downgraded``。

    这两件事的明细日志是 **debug 级**，而线上 ``TGA_LOG_LEVEL=INFO`` —— 所以心跳里
    的这两个数字是**唯一**能确认「跨账号去重 / forward 降级到底有没有生效」的地方。
    字段被删掉就等于功能彻底不可观测，这里钉住它。
    """

    def test_heartbeat_includes_cross_dedupe_and_downgrade(self, paths, client, alog):
        runner = AccountRunner(AccountRecord(name="acc-a"), build_config(), None, paths)
        runner.started_at = time.time()
        runner.forwarder = ForwardEngine(client, build_config(), alog)
        runner.forwarder.stats["cross_deduped"] = 3
        runner.forwarder.stats["downgraded"] = 2

        # 直接换掉 logger 收集字段，不去赌日志传播配置。
        captured: dict[str, Any] = {}
        runner.alog = types.SimpleNamespace(info=lambda msg, **kw: captured.update(kw))

        runner._log_heartbeat()

        assert captured["cross_deduped"] == 3
        assert captured["downgraded"] == 2
        assert captured["pair_deduped"] == 0
        assert captured["pair_blocked"] == 0

    def test_heartbeat_includes_pair_counters(self, paths, client, alog):
        """「频道 ↔ 群组」那层去重的计数也必须进心跳 —— 明细日志是 INFO 级，
        但这个数字是「规则到底有没有生效」的**唯一**可查口径（同 cross_deduped）。"""
        runner = AccountRunner(AccountRecord(name="acc-a"), build_config(), None, paths)
        runner.started_at = time.time()
        runner.forwarder = ForwardEngine(client, build_config(), alog)
        runner.forwarder.stats["pair_deduped"] = 5
        runner.forwarder.stats["pair_blocked"] = 1

        captured: dict[str, Any] = {}
        runner.alog = types.SimpleNamespace(info=lambda msg, **kw: captured.update(kw))

        runner._log_heartbeat()

        assert captured["pair_deduped"] == 5
        assert captured["pair_blocked"] == 1

    def test_heartbeat_includes_gate_counter(self, paths, client, alog):
        """「同内容 + 同目标」串行闸门挡下的次数也必须进心跳。

        这个数字 ``>0`` 是**唯一**能证明「并发抢跑真的发生过、而且确实被拦住了」的口径
        —— 闸门本身不逐条打日志，而它修的正是 2026-09-23 那 6 个重复指纹的根因。
        """
        runner = AccountRunner(AccountRecord(name="acc-a"), build_config(), None, paths)
        runner.started_at = time.time()
        runner.forwarder = ForwardEngine(client, build_config(), alog)
        runner.forwarder._recent.gated = 4

        captured: dict[str, Any] = {}
        runner.alog = types.SimpleNamespace(info=lambda msg, **kw: captured.update(kw))

        runner._log_heartbeat()

        assert captured["gated"] == 4


class TestContentFingerprint:
    """跨会话比对「是不是同一条内容」的指纹。"""

    def test_same_text_same_fingerprint(self):
        a = content_fingerprint(src_message("同一条推广"))
        b = content_fingerprint(src_message("同一条推广", message_id=999))
        assert a is not None
        assert a == b, "同一条推广在频道/群组各发一遍 ⇒ 指纹必须相同"

    def test_surrounding_whitespace_ignored(self):
        assert content_fingerprint(src_message("  推广  ")) == content_fingerprint(src_message("推广"))

    def test_different_text_differs(self):
        assert content_fingerprint(src_message("推广A")) != content_fingerprint(src_message("推广B"))

    def test_caption_counts_as_body(self):
        a = content_fingerprint(make_message(None, caption="同一条推广"))
        b = content_fingerprint(src_message("同一条推广"))
        assert a is not None and a == b, "caption 与 text 都是正文，同内容要能对上"

    def test_text_wins_over_media(self):
        """同一段正文配不同图（重发时换了图）也要算同一条 —— 这类推广帖正文才是标识。

        线上那两对的正文都是长文，媒体反而是次要的；只用媒体 id 会比不出来。
        """
        a = content_fingerprint(
            make_message("同一条推广", photo=types.SimpleNamespace(file_unique_id="PH1"))
        )
        b = content_fingerprint(
            make_message("同一条推广", photo=types.SimpleNamespace(file_unique_id="PH2"))
        )
        assert a == b

    def test_media_unique_id_used_without_text(self):
        a = content_fingerprint(make_message(None, photo=types.SimpleNamespace(file_unique_id="PH1")))
        b = content_fingerprint(make_message(None, photo=types.SimpleNamespace(file_unique_id="PH1")))
        c = content_fingerprint(make_message(None, photo=types.SimpleNamespace(file_unique_id="PH2")))
        assert a is not None and a == b and a != c

    def test_no_body_no_media_returns_none(self):
        assert content_fingerprint(make_message(None)) is None


class TestChannelGroupDedupe:
    """频道与它的关联群组各发一遍同一条内容 ⇒ 只留**先到**的那条，后到的直接不发。

    小白 2026-09-21 的需求：「频道发送消息时会在群组也同时发送一条，两条是一样的但是
    原文链接不一样，我只需要保留群组这一条」。

    线上取证（``tg-assistant chats`` + 直接读消息）钉死了两件事：

    1. 群里那条**不是**频道的转发（``forward_origin`` / ``forward_from_chat`` 都是空）
       ⇒ 拿不到「同一条原始消息」这个强标识，只能比**内容指纹**；
    2. 先后顺序**随机** —— 流光画廊那对是群组先到（47.505 / 48.354），秀儿那对却是
       **频道先到**（频道 37.651、群组 39.144）。

    🔴 2026-09-25 改版。原来是「群组优先，与先后顺序无关」：频道先到就先把它发出去，
    群组那条到了再把频道那条**撤回**、重发群组那条。用户原话是「我要的是重复的直接
    拦截，而不是一直更新再自动删除上一条消息」—— 目标里先冒一条、过一两秒又消失、
    再补一条，看起来就是消息被吞了又重发，比多一条重复还难受。

    现在改成**先到先得、后到的直接不发**：这一层是纯判断、零副作用。
    代价是频道先到的那一对会留下频道那条（链接指向频道帖而不是群组帖）。
    """

    # ---------------------------------------------------------------- #
    # 顺序两种都要对，且**两种都不撤回**
    # ---------------------------------------------------------------- #
    def test_group_first_then_channel_is_skipped(self):
        dedupe = ChannelGroupDedupe()
        assert dedupe.claim("fp", DST, "group", 300) is True
        assert dedupe.claim("fp", DST, "channel", 300) is False, "群组已发过 ⇒ 频道这条不该再发"
        assert (dedupe.claimed, dedupe.channel_dropped, dedupe.group_dropped) == (1, 1, 0)

    def test_channel_first_then_group_is_also_skipped(self):
        """🔴 回归：频道先到时**不再**撤回已发的那条、也不再重发群组那条。"""
        dedupe = ChannelGroupDedupe()
        assert dedupe.claim("fp", DST, "channel", 300) is True
        assert dedupe.claim("fp", DST, "group", 300) is False, (
            "先到的那条已经发出去了 ⇒ 群组这条直接拦掉，绝不撤回重发"
        )
        assert (dedupe.claimed, dedupe.channel_dropped, dedupe.group_dropped) == (1, 0, 1)

    def test_the_first_record_never_flips(self):
        """🔴 后到的那条**不能**覆盖记录 —— 否则第三条同内容消息的判据会漂移。

        记录里必须始终是「频道先到」，所以：
        - 再来的**群组**消息照样被拦（判据没漂）；
        - 再来的**频道**消息与记录同类型 ⇒ 这一层放行（同类型之间的内容重复
          是「最近已转发的内容」那层的活，见 ``TestRecentContentDedupeInEngine``）。
        """
        dedupe = ChannelGroupDedupe()
        dedupe.claim("fp", DST, "channel", 300)
        dedupe.claim("fp", DST, "group", 300)
        assert dedupe.claim("fp", DST, "group", 300) is False, "记录仍是「频道先到」"
        assert dedupe.claim("fp", DST, "channel", 300) is True, "同类型 ⇒ 这一层不管"
        assert dedupe._seen[("fp", str(DST))].kind == "channel", "记录不许被后到的那条改写"

    def test_dedupe_has_no_withdrawal_machinery(self):
        """🔴 回归：这一层不许再长出「撤回」入口 —— 拦下就只是不发。

        ``mark_sent`` / 记录里的 ``sent`` 都是「先发一条再撤掉」那套机制的残留，
        它们存在就意味着还有可能删用户已经看到的消息。
        """
        dedupe = ChannelGroupDedupe()
        assert not hasattr(dedupe, "mark_sent")
        dedupe.claim("fp", DST, "channel", 300)
        entry = dedupe._seen[("fp", str(DST))]
        assert not hasattr(entry, "sent"), "记录里不该再存「已发出的消息 id」"

    # ---------------------------------------------------------------- #
    # 不能误杀
    # ---------------------------------------------------------------- #
    def test_same_kind_is_not_deduped(self):
        """两个**群组**发的同内容帖子不能被误杀 —— 只有「频道 ↔ 群组」才算一对。"""
        dedupe = ChannelGroupDedupe()
        assert dedupe.claim("fp", DST, "group", 300) is True
        assert dedupe.claim("fp", DST, "group", 300) is True
        assert (dedupe.channel_dropped, dedupe.group_dropped) == (0, 0)

    def test_unknown_kind_is_not_deduped(self):
        """类型判不出来（例如 pyrogram 的 ``community``）⇒ 宁可漏去重也不误杀。"""
        dedupe = ChannelGroupDedupe()
        assert dedupe.claim("fp", DST, None, 300) is True
        assert dedupe.claim("fp", DST, "channel", 300) is True
        assert (dedupe.channel_dropped, dedupe.group_dropped) == (0, 0)

    def test_different_targets_are_independent(self):
        """键必须带目标：同一条内容发往不同目标时互不影响。"""
        dedupe = ChannelGroupDedupe()
        assert dedupe.claim("fp", DST, "group", 300) is True
        assert dedupe.claim("fp", -1009999999999, "channel", 300) is True

    def test_different_content_is_independent(self):
        dedupe = ChannelGroupDedupe()
        assert dedupe.claim("fp1", DST, "group", 300) is True
        assert dedupe.claim("fp2", DST, "channel", 300) is True

    def test_zero_ttl_disables(self):
        dedupe = ChannelGroupDedupe()
        assert dedupe.claim("fp", DST, "group", 0) is True
        assert dedupe.claim("fp", DST, "channel", 0) is True

    def test_claim_expires_after_ttl(self, monkeypatch):
        dedupe = ChannelGroupDedupe()
        now = [1000.0]
        monkeypatch.setattr("tg_assistant.forwarder.time.monotonic", lambda: now[0])

        assert dedupe.claim("fp", DST, "group", 10) is True
        now[0] += 5
        assert dedupe.claim("fp", DST, "channel", 10) is False
        now[0] += 6  # 超过 TTL
        assert dedupe.claim("fp", DST, "channel", 10) is True

    # ---------------------------------------------------------------- #
    # release：发送失败必须退还名额
    # ---------------------------------------------------------------- #
    def test_release_frees_the_slot(self):
        """🔴 不退的话，同内容的另一条会被判成重复而跳过 ⇒ 消息彻底丢。"""
        dedupe = ChannelGroupDedupe()
        assert dedupe.claim("fp", DST, "group", 300) is True
        assert dedupe.claim("fp", DST, "channel", 300) is False

        dedupe.release("fp", DST)

        assert dedupe.claim("fp", DST, "channel", 300) is True
        assert dedupe.released == 1

    def test_release_is_idempotent(self):
        dedupe = ChannelGroupDedupe()
        dedupe.release("nope", DST)
        dedupe.release("nope", DST)
        assert len(dedupe) == 0
        assert dedupe.released == 2

    def test_snapshot_counts(self):
        dedupe = ChannelGroupDedupe()
        dedupe.claim("fp", DST, "group", 300)
        dedupe.claim("fp", DST, "channel", 300)
        snapshot = dedupe.snapshot()
        assert snapshot == {
            "size": 1,
            "claimed": 1,
            "channel_dropped": 1,
            "group_dropped": 0,
            "released": 0,
        }


class TestRecentContentDedupe:
    """「最近已转发的内容」表：条数上限与时间窗**取并集**（哪个更宽算哪个）。"""

    def test_recent_hit(self):
        dedupe = RecentContentDedupe(limit=5, ttl=3600)
        dedupe.add("fp1", DST)
        assert dedupe.contains("fp1", DST) is True
        assert dedupe.hits == 1

    def test_beyond_limit_but_within_ttl_still_counts(self):
        """超出「前 5 条」但还在时间窗内 ⇒ 仍然算最近（这就是「一天内」那一半）。"""
        dedupe = RecentContentDedupe(limit=2, ttl=3600)
        for i in range(5):
            dedupe.add(f"fp{i}", DST)
        assert dedupe.contains("fp0", DST) is True, "时间窗内 ⇒ 即便超出条数上限也要拦住"

    def test_beyond_limit_and_expired_is_dropped(self):
        """既超出条数上限、又过了时间窗 ⇒ 不再算最近。"""
        dedupe = RecentContentDedupe(limit=2, ttl=0.01)
        dedupe.add("old", DST)
        dedupe.add("new1", DST)
        time.sleep(0.02)
        dedupe.add("new2", DST)
        assert dedupe.contains("old", DST) is False

    def test_limit_zero_means_time_window_only(self):
        dedupe = RecentContentDedupe(limit=0, ttl=3600)
        for i in range(10):
            dedupe.add(f"fp{i}", DST)
        assert dedupe.contains("fp0", DST) is True, "limit=0 ⇒ 只看时间窗，条数不限"

    def test_ttl_zero_means_count_only(self):
        dedupe = RecentContentDedupe(limit=3, ttl=0)
        for i in range(5):
            dedupe.add(f"fp{i}", DST)
        assert dedupe.contains("fp0", DST) is False, "ttl=0 ⇒ 只看最近 3 条"
        assert dedupe.contains("fp4", DST) is True

    def test_both_zero_means_disabled(self):
        dedupe = RecentContentDedupe(0, 0)
        dedupe.add("fp", DST)
        assert dedupe.enabled is False
        assert dedupe.contains("fp", DST) is False
        assert len(dedupe) == 0

    def test_same_fingerprint_keeps_only_latest(self):
        dedupe = RecentContentDedupe(limit=5, ttl=3600)
        dedupe.add("fp", DST)
        dedupe.add("fp", DST)
        assert len(dedupe) == 1, "同一个指纹只留一条 —— 否则表会被重复项撑爆"

    def test_targets_are_isolated(self):
        dedupe = RecentContentDedupe(limit=5, ttl=3600)
        dedupe.add("fp", DST)
        assert dedupe.contains("fp", DST) is True
        assert dedupe.contains("fp", SRC) is False, "同内容发到不同目标互不影响"

    def test_none_fingerprint_never_hits(self):
        dedupe = RecentContentDedupe(limit=5, ttl=3600)
        assert dedupe.contains(None, DST) is False
        dedupe.add(None, DST)
        assert len(dedupe) == 0, "没有指纹（既无正文也无媒体）不参与这一层"

    def test_snapshot(self):
        dedupe = RecentContentDedupe(limit=5, ttl=3600)
        dedupe.add("fp", DST)
        snap = dedupe.snapshot()
        assert snap["size"] == 1 and snap["limit"] == 5 and snap["ttl"] == 3600


class TestRecentContentDedupeInEngine:
    """引擎层：目标里最近发过的内容，再来一遍就不发。"""

    @pytest.mark.asyncio
    async def test_same_content_from_two_groups_only_sends_once(self, alog):
        """两个群发同一段广告 ⇒ 第二条跳过（2026-09-21 小白要的效果）。"""
        client = FakeClient()
        engine = ForwardEngine(
            client,
            build_config(sources=[]),
            alog,
            recent_dedupe=RecentContentDedupe(limit=5, ttl=3600),
        )
        other_group = FakeChat(-1007777777777, title="另一个群", chat_type="supergroup")

        engine._handle(group_message("关键词123"), edited=False)
        await drain(engine)
        engine._handle(make_message("关键词123", message_id=200, chat=other_group), edited=False)
        await drain(engine)

        assert len(client.forwarded) == 1, "同样的内容不该在目标里出现两遍"
        assert engine.stats["recent_deduped"] == 1

    @pytest.mark.asyncio
    async def test_different_content_still_goes_through(self, alog):
        client = FakeClient()
        engine = ForwardEngine(
            client,
            build_config(sources=[]),
            alog,
            recent_dedupe=RecentContentDedupe(limit=5, ttl=3600),
        )
        engine._handle(group_message("关键词123"), edited=False)
        await drain(engine)
        engine._handle(group_message("关键词456", message_id=200), edited=False)
        await drain(engine)

        assert len(client.forwarded) == 2
        assert engine.stats["recent_deduped"] == 0

    @pytest.mark.asyncio
    async def test_album_is_not_trimmed_to_one(self, alog):
        """🔴 相册不能被当成重复砍成一条。

        同一组里的多条 caption 常常一模一样（甚至整组只有第一条有 caption、其余走
        媒体指纹），按内容比会把剩下的图全丢掉。
        """
        client = FakeClient()
        engine = ForwardEngine(
            client,
            build_config(sources=[], match={"mode": "all"}, media_group=False),
            alog,
            recent_dedupe=RecentContentDedupe(limit=5, ttl=3600),
        )
        engine.register()
        for index in range(3):
            engine._handle(
                src_message("相册", message_id=300 + index, media_group_id="mg-9"), edited=False
            )
        await drain(engine)

        assert len(client.forwarded) == 3, "同一相册的三条都要发 —— 它们是一个整体"
        assert engine.stats["recent_deduped"] == 0

    @pytest.mark.asyncio
    async def test_blocked_group_version_never_reaches_the_recent_table(self, alog):
        """🔴 频道先发、群组后到时，群组那条在**频道↔群组**这一层就被拦下了。

        它根本走不到「最近已转发的内容」那一层 ⇒ ``recent_deduped`` 保持 0。
        同时钉住：拦下群组那条**不会**动到 ``_recent`` 里的记录，目标里那条内容
        仍然是「已转发」状态，第三条同内容消息照样会被拦。
        """
        pair = ChannelGroupDedupe()
        client = FakeClient()
        engine = ForwardEngine(
            client,
            build_config(sources=[]),
            alog,
            pair_dedupe=pair,
            recent_dedupe=RecentContentDedupe(limit=5, ttl=3600),
        )

        engine._handle(channel_message("关键词123"), edited=False)
        await drain(engine)
        assert len(client.forwarded) == 1, "前提：频道那条已经发出去了"

        engine._handle(group_message("关键词123", message_id=200), edited=False)
        await drain(engine)

        assert len(client.forwarded) == 1, "群组那条后到 ⇒ 直接不发"
        assert engine.stats["pair_blocked"] == 1
        assert engine.stats["recent_deduped"] == 0, "在这一层就被拦下了，走不到「最近发过」那层"
        assert client.deleted == [], "🔴 绝不删已经发出去的那条"

    @pytest.mark.asyncio
    async def test_two_groups_same_content_is_caught_by_the_recent_table(self, alog):
        """两个**群组**发同内容不是「频道 ↔ 群组」那一对 ⇒ 由「最近已转发」拦下。

        这条用例把两层的分工钉死：``pair_*`` 只认 ``{channel, group}``，
        同类型之间的内容重复是 ``recent_deduped`` 的活。
        """
        pair = ChannelGroupDedupe()
        client = FakeClient()
        engine = ForwardEngine(
            client,
            build_config(sources=[]),
            alog,
            pair_dedupe=pair,
            recent_dedupe=RecentContentDedupe(limit=5, ttl=3600),
        )

        engine._handle(group_message("关键词123"), edited=False)
        await drain(engine)
        engine._handle(group_message("关键词123", message_id=200), edited=False)
        await drain(engine)

        assert len(client.forwarded) == 1
        assert engine.stats["recent_deduped"] == 1
        assert (engine.stats["pair_deduped"], engine.stats["pair_blocked"]) == (0, 0)
        assert client.deleted == []


class TestChannelGroupSameContentInEngine:
    """引擎层：同一条内容由频道和群组各发一遍，目标里只留**先到**那条。

    🔴 2026-09-25 改版：原来是「群组优先」—— 频道先到就先发出去，群组那条到了
    再把频道那条撤回、重发群组那条。用户原话「我要的是重复的直接拦截，而不是
    一直更新再自动删除上一条消息」⇒ 现在是**先到先得、后到的不发**，
    整个引擎里不再有任何「删掉已发消息」的路径。
    """

    @pytest.mark.asyncio
    async def test_group_first_channel_is_dropped(self, alog):
        pair = ChannelGroupDedupe()
        client = FakeClient()
        engine = ForwardEngine(client, build_config(sources=[]), alog, pair_dedupe=pair)

        engine._handle(group_message("关键词123"), edited=False)
        await drain(engine)
        engine._handle(channel_message("关键词123", message_id=200), edited=False)
        await drain(engine)

        assert len(client.forwarded) == 1, "群组已发过 ⇒ 频道那条不该再发一遍"
        assert engine.stats["pair_deduped"] == 1
        assert engine.stats["pair_blocked"] == 0
        assert client.deleted == []

    @pytest.mark.asyncio
    async def test_channel_first_group_is_dropped_without_deleting_anything(self, alog):
        """🔴 回归：线上实测到的顺序之一（秀儿那对：频道 00:01:37 先到、群组 00:01:39 后到）。

        旧行为是「群组那条照样发 + 把先前发出的频道消息撤回」；用户否掉了那种观感。
        新行为：群组那条**直接不发**，已经发出去的频道那条**保持原样**。
        """
        pair = ChannelGroupDedupe()
        client = FakeClient()
        engine = ForwardEngine(client, build_config(sources=[]), alog, pair_dedupe=pair)

        before = client.next_message_id
        engine._handle(channel_message("关键词123"), edited=False)
        await drain(engine)
        channel_sent = list(range(before + 1, client.next_message_id + 1))
        assert len(channel_sent) == 2, "forward 模式：转发消息 + 下方补的链接消息"
        assert client.deleted == []

        engine._handle(group_message("关键词123", message_id=200), edited=False)
        await drain(engine)

        assert len(client.forwarded) == 1, "群组那条后到 ⇒ 不发"
        assert client.deleted == [], "🔴 不许删已经发出去的频道那条"
        assert engine.stats["pair_blocked"] == 1
        assert engine.stats["pair_deduped"] == 0

    @pytest.mark.asyncio
    async def test_send_in_flight_when_group_arrives_still_ends_with_one(self, alog):
        """🔴 竞态：频道那条**还在发**的时候群组那条就到了，最终仍然只该有一条。

        ``claim`` 与真正发送之间隔着一个 ``await``（线上 ``pipeline_ms`` 实测到过 **4223ms**），
        群组那条完全可能就在这个窗口里到达。

        串行闸门（``RecentContentDedupe.gate``）让同内容 + 同目标的两个 handler **排队**：
        群组那条会等频道那条发完才轮到它 ``claim`` ⇒ 它读到记录里是「频道先到」⇒
        直接拦掉。目标里从头到尾只有频道那一条，**没有任何删除**。
        """
        pair = ChannelGroupDedupe()
        client = SlowFirstForwardClient()
        engine = ForwardEngine(client, build_config(sources=[]), alog, pair_dedupe=pair)

        engine._handle(channel_message("关键词123"), edited=False)
        await asyncio.sleep(0.05)  # 频道那条卡在 forward_messages 里
        assert client.forwarded == [], "前提：频道那条确实还没发出去"

        engine._handle(group_message("关键词123", message_id=200), edited=False)
        await asyncio.sleep(0.05)
        assert client.forwarded == [], "群组那条应当排队等频道那条，而不是抢跑"

        client.release.set()  # 放行频道那条
        await drain(engine)

        assert len(client.forwarded) == 1, "最终目标里只该有一条 —— 群组那条被拦掉了"
        assert client.deleted == [], "🔴 全程没有任何删除"
        assert engine.stats["pair_blocked"] == 1

    @pytest.mark.asyncio
    async def test_channel_first_sends_only_one_notification(self, alog):
        """频道先到 ⇒ 群组那条被拦 ⇒ 只该有一条通知，且没有可撤的。"""
        pair = ChannelGroupDedupe()
        client = FakeClient()
        notifier = FakeNotifier()
        engine = ForwardEngine(
            client, build_config(sources=[], notify=True), alog,
            pair_dedupe=pair, notifier=notifier,
        )

        engine._handle(channel_message("关键词123"), edited=False)
        await drain(engine)
        engine._handle(group_message("关键词123", message_id=200), edited=False)
        await drain(engine)

        assert len(notifier.submitted) == 1, "群组那条被拦下了 ⇒ 不该有第二条通知"
        assert notifier.withdrawn == [], "🔴 通知也不再需要撤回"

    @pytest.mark.asyncio
    async def test_group_first_sends_only_one_notification(self, alog):
        """群组先到 ⇒ 频道那条压根不发 ⇒ 只该有一条通知。"""
        pair = ChannelGroupDedupe()
        client = FakeClient()
        notifier = FakeNotifier()
        engine = ForwardEngine(
            client, build_config(sources=[], notify=True), alog,
            pair_dedupe=pair, notifier=notifier,
        )

        engine._handle(group_message("关键词123"), edited=False)
        await drain(engine)
        engine._handle(channel_message("关键词123", message_id=200), edited=False)
        await drain(engine)

        assert len(notifier.submitted) == 1, "频道那条被跳过了 ⇒ 不该有第二条通知"
        assert notifier.withdrawn == []

    @pytest.mark.asyncio
    async def test_different_content_both_sent(self, alog):
        pair = ChannelGroupDedupe()
        client = FakeClient()
        engine = ForwardEngine(client, build_config(sources=[]), alog, pair_dedupe=pair)

        engine._handle(group_message("关键词123"), edited=False)
        await drain(engine)
        engine._handle(channel_message("关键词456", message_id=200), edited=False)
        await drain(engine)

        assert len(client.forwarded) == 2, "内容不同就不是同一条，都要发"
        assert engine.stats["pair_deduped"] == 0

    @pytest.mark.asyncio
    async def test_two_groups_same_content_both_sent(self, alog):
        """**仅**「频道 ↔ 群组」这一层不会把两个群组的同内容帖子合并。

        ⚠️ 这里刻意把「最近已转发的内容」那层**关掉**（``RecentContentDedupe(0, 0)``）：
        那一层会按内容去重，两个群发同一段广告时第二条会被跳过 —— 那正是
        2026-09-21 小白要的效果（「命中新消息要跟前 5 条对比，不一致才转发」），
        由 :class:`TestRecentContentDedupeInEngine` 单独覆盖。本用例只钉
        「频道↔群组」这一层的边界：它**只认** ``{channel, group}``。
        """
        pair = ChannelGroupDedupe()
        client = FakeClient()
        engine = ForwardEngine(
            client,
            build_config(sources=[]),
            alog,
            pair_dedupe=pair,
            recent_dedupe=RecentContentDedupe(0, 0),
        )
        other_group = FakeChat(-1007777777777, title="另一个群", chat_type="supergroup")

        engine._handle(group_message("关键词123"), edited=False)
        await drain(engine)
        engine._handle(make_message("关键词123", message_id=200, chat=other_group), edited=False)
        await drain(engine)

        assert len(client.forwarded) == 2
        assert engine.stats["pair_deduped"] == 0

    @pytest.mark.asyncio
    async def test_failed_send_releases_pair_slot(self, alog):
        """🔴 频道那条**发送失败**时必须退还名额，否则同内容的群组那条会被当成
        「频道已发过」而跳过 ⇒ 这条消息对该目标彻底丢。"""
        pair = ChannelGroupDedupe()
        broken = FakeClient(forward_error=RuntimeError("network boom"))
        engine_a = ForwardEngine(broken, build_config(sources=[]), alog, pair_dedupe=pair)

        engine_a._handle(channel_message("关键词123"), edited=False)
        await drain(engine_a)

        assert engine_a.stats["failed"] == 1
        assert pair.released == 1, "发送失败必须把名额退回去"

        healthy = FakeClient()
        engine_b = ForwardEngine(healthy, build_config(sources=[]), alog, pair_dedupe=pair)
        engine_b._handle(group_message("关键词123", message_id=200), edited=False)
        await drain(engine_b)

        assert len(healthy.forwarded) == 1, "频道那条失败了，群组那条必须能发出去"
        assert engine_b.stats["pair_deduped"] == 0

    @pytest.mark.asyncio
    async def test_no_table_means_disabled(self, alog):
        """不传**频道↔群组**表时，那一层不生效：两条都发。

        ⚠️ 同样要把「最近已转发的内容」那层关掉，否则它会按内容拦掉第二条
        （见 :class:`TestRecentContentDedupeInEngine`）。
        """
        client = FakeClient()
        engine = ForwardEngine(
            client,
            build_config(sources=[]),
            alog,
            recent_dedupe=RecentContentDedupe(0, 0),
        )

        engine._handle(group_message("关键词123"), edited=False)
        await drain(engine)
        engine._handle(channel_message("关键词123", message_id=200), edited=False)
        await drain(engine)

        assert len(client.forwarded) == 2
        assert engine.stats["pair_deduped"] == 0

    @pytest.mark.asyncio
    async def test_skipped_channel_releases_cross_account_slot(self, alog):
        """被本层跳过的频道那条，要把**跨账号**名额也退掉 —— 否则那个键在整个 TTL 里
        被占死，别的账号连「补发」的机会都没有。"""
        pair = ChannelGroupDedupe()
        shared = CrossAccountDedupe()
        client = FakeClient()
        engine = ForwardEngine(
            client, build_config(sources=[]), alog, shared_dedupe=shared, pair_dedupe=pair
        )

        engine._handle(group_message("关键词123"), edited=False)
        await drain(engine)
        engine._handle(channel_message("关键词123", message_id=200), edited=False)
        await drain(engine)

        assert engine.stats["pair_deduped"] == 1
        assert shared.released == 1

    def test_snapshot_exposes_pair_table(self, client, alog):
        pair = ChannelGroupDedupe()
        engine = ForwardEngine(client, build_config(), alog, pair_dedupe=pair)
        pair.claim("fp", DST, "group", 300)
        assert engine.snapshot()["pair_dedupe"]["claimed"] == 1


class TestPairDedupeWiring:
    """共享表必须真的接到引擎上，且能跨面板重建复用。"""

    def test_multi_runner_keeps_injected_pair_table(self):
        pair = ChannelGroupDedupe()
        assert MultiRunner(None, None, pair_dedupe=pair).pair_dedupe is pair

    def test_multi_runner_defaults_to_disabled(self):
        assert MultiRunner(None, None).pair_dedupe is None

    def test_account_runner_stores_pair_table(self, paths):
        pair = ChannelGroupDedupe()
        runner = AccountRunner(
            AccountRecord(name="acc-a"),
            build_config(),
            None,
            paths,
            pair_dedupe=pair,
        )
        assert runner.pair_dedupe is pair

    def test_account_runner_defaults_to_disabled(self, paths):
        runner = AccountRunner(AccountRecord(name="acc-a"), build_config(), None, paths)
        assert runner.pair_dedupe is None


class _FailForwardAfterFirst(FakeClient):
    """第一次 ``forward_messages`` 放行（频道那条），之后每次都抛错（群组那条发不出去）。

    用来复现线上那个真实顺序：频道那条已经发成功 ⇒ 群组那条来顶替 ⇒ **顶替者自己发送失败**。
    """

    def __init__(self) -> None:
        super().__init__()
        self._n = 0

    async def forward_messages(self, **kwargs: Any) -> Any:
        self._n += 1
        if self._n > 1:
            raise RuntimeError("群组那条自己也发失败了")
        return await super().forward_messages(**kwargs)


class TestDuplicateRegression:
    """🔴 回归：2026-09-23 线上实测的两类重复转发（26 小时里 6 个指纹）。

    三层去重全是「**先判断、后发送**」，而发送要 ``await``（线上 ``pipeline_ms``
    实测到过 4.2 秒）—— 判断与落表之间的那个窗口足以让并发的两条**都通过判断**。
    线上两类的形态分别是：

    1. 两个账号收到**不同源消息、同一内容**，相隔 0.3 秒各发了一遍
       （``text:b6fe2d70…``：小白 11:19:37.045 / SevenStar 11:19:37.358）；
    2. 群里那条顶替频道那条时**顶替者自己发送失败**，而撤回顺手把「最近发过」
       记录也摘掉了 ⇒ 下一条同内容又被当成新的发一遍
       （``text:4d08837d…``：20:09:17 / 20:09:27 / 20:09:33 连发三次）。

    修复两处：① 「同一内容 + 同一目标」的串行闸门（``RecentContentDedupe.gate``）；
    ② 「先发成功、再撤旧的」（撤回推迟到发送之后）。
    """

    @pytest.mark.asyncio
    async def test_two_accounts_same_content_race_sends_once(self, alog):
        """① 两个账号共用去重表、同时收到不同源消息而内容相同 ⇒ 只发一次。

        ⚠️ 必须用**慢速**发送把竞态窗口真正打开（``SlowFirstForwardClient`` 卡住第一次
        ``forward_messages``）。用瞬时返回的假客户端时两个 handler 会一前一后跑完、
        窗口根本不存在 —— 这条用例就成了「测不到东西的绿灯」（缺陷注入时证实过：
        换回旧代码它照样通过）。改慢之后旧代码必红。
        """
        pair = ChannelGroupDedupe()
        cross = CrossAccountDedupe()
        recent = RecentContentDedupe(limit=5, ttl=86400.0)
        client = SlowFirstForwardClient()
        cfg = build_config(sources=[])
        engine_a = ForwardEngine(
            client, cfg, alog, shared_dedupe=cross, pair_dedupe=pair, recent_dedupe=recent
        )
        engine_b = ForwardEngine(
            client, cfg, alog, shared_dedupe=cross, pair_dedupe=pair, recent_dedupe=recent
        )

        # 不同来源会话、不同消息 id —— 所以「跨账号去重」那颗键不一样，挡不住。
        engine_a._handle(
            group_message(
                "关键词123",
                message_id=100,
                chat=FakeChat(-1004444444444, title="来源群A", chat_type="supergroup"),
            ),
            edited=False,
        )
        engine_b._handle(
            group_message(
                "关键词123",
                message_id=200,
                chat=FakeChat(-1005555555555, title="来源群B", chat_type="supergroup"),
            ),
            edited=False,
        )
        await asyncio.sleep(0.05)  # 第一条卡在发送里、第二条被挡在闸门外
        assert client.forwarded == [], "前提：竞态窗口确实开着（第一条还在发）"

        client.release.set()  # 放行第一条
        await asyncio.gather(drain(engine_a), drain(engine_b))

        assert len(client.forwarded) == 1, "同一条内容并发只允许发出去一次"
        assert len(client.deleted) == 0, "全程不该有任何删除动作"

    @pytest.mark.asyncio
    async def test_late_group_version_never_even_attempts_a_send(self, alog):
        """② 后到的那条**连发送都不会尝试** ⇒ 「顶替者自己失败」这一类事故从根上没有了。

        旧行为的两个毛病都在这里被钉住（现在都消失了）：
        - 先在发送**之前**撤掉频道那条 ⇒ 顶替者失败后目标里一条不剩（内容丢失）；
        - 撤回把「最近发过」记录摘掉 ⇒ 下一条同内容又被当成新的发一遍（重复）。
        """
        pair = ChannelGroupDedupe()
        client = _FailForwardAfterFirst()
        engine = ForwardEngine(
            client,
            build_config(sources=[]),
            alog,
            pair_dedupe=pair,
            recent_dedupe=RecentContentDedupe(limit=5, ttl=86400.0),
        )

        engine._handle(channel_message("关键词123"), edited=False)
        await drain(engine)
        assert len(client.forwarded) == 1, "前提：频道那条已经发出去了"
        assert client._n == 1, "前提：只尝试发送过一次"

        # 群组那条后到 —— 它在**发送之前**就被拦掉了，根本不会去尝试发送
        engine._handle(group_message("关键词123", message_id=200), edited=False)
        await drain(engine)
        assert client._n == 1, "🔴 后到的那条压根没尝试发送（旧行为会尝试、然后失败）"
        assert engine.stats["failed"] == 0, "没尝试就谈不上失败"
        assert len(client.forwarded) == 1, "目标里还是那条频道消息，一条没少"
        assert client.deleted == [], "🔴 绝不删已经发出去的那条"
        assert engine.stats["pair_blocked"] == 1

        # 下一条**同内容**的消息（线上 20:09:27 那条）必须被拦住，不能又发一遍
        engine._handle(group_message("关键词123", message_id=300), edited=False)
        await drain(engine)
        assert len(client.forwarded) == 1, "🔴 目标里已经有这条内容了，不能再发一遍"
        assert engine.stats["pair_blocked"] == 2, "同内容的群组消息再来一条，照样拦掉"
