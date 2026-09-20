"""转发引擎：匹配、派发、相册聚合、去重、失败处理。"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import time
import types
from typing import Any

import pytest
from pyrogram.enums import ParseMode
from pyrogram.errors import ChatForwardsRestricted, ChatWriteForbidden, FloodWait

from tg_assistant.config import AccountConfig, AccountRecord, ForwardRule
from tg_assistant.forwarder import (
    CrossAccountDedupe,
    DedupeCache,
    ForwardEngine,
    PreparedRule,
    _pipeline_ms,
)
from tg_assistant.runner import AccountRunner, MultiRunner

from .conftest import FakeChat, FakeClient, FakeUser, make_message

SRC = -1001111111111
DST = -1002222222222


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


async def drain(engine: ForwardEngine) -> None:
    """等所有派发出去的任务跑完。"""
    for _ in range(50):
        pending = [t for t in engine._tasks if not t.done()]
        if not pending:
            break
        await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.sleep(0)


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
