"""转发引擎：匹配、派发、相册聚合、去重、失败处理。"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import time
import types
from typing import Any

import pytest
from pyrogram.errors import ChatForwardsRestricted, ChatWriteForbidden, FloodWait

from tg_assistant.config import AccountConfig, AccountRecord
from tg_assistant.forwarder import CrossAccountDedupe, DedupeCache, ForwardEngine, PreparedRule
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
    async def test_copy_mode_drops_author(self, client, alog):
        engine = ForwardEngine(client, build_config(mode="copy"), alog)
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)
        assert client.forwarded[0]["hide_sender_name"] is True

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
                            "match": {"mode": "contains", "patterns": ["关键词"]},
                        },
                        {
                            "id": "b",
                            "sources": [SRC],
                            "targets": [-1004444444444],
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
    """

    @pytest.mark.asyncio
    async def test_restricted_forward_retries_as_copy(self, alog):
        client = FakeClient(forward_error_once=ChatForwardsRestricted(value=RESTRICTED))
        engine = ForwardEngine(client, build_config(mode="forward"), alog)
        engine.register()
        engine._handle(src_message("这是关键词123的消息"), edited=False)
        await drain(engine)

        assert client.forward_calls == 2, "应当先真转发失败一次，再以复制重试一次"
        assert len(client.forwarded) == 1, "失败的调用不进 forwarded"
        assert client.forwarded[0]["hide_sender_name"] is True, "第二次必须去掉「转发自」"
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
        assert engine.stats["failed"] == 1
        assert engine.stats["downgraded"] == 0

    @pytest.mark.asyncio
    async def test_downgrade_failure_is_still_reported_as_failed(self, alog):
        """降级也失败时不能假装成功。"""
        client = FakeClient(forward_error=ChatForwardsRestricted(value=RESTRICTED))
        engine = ForwardEngine(client, build_config(mode="forward"), alog)
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)

        assert client.forwarded == []
        assert engine.stats["forwarded"] == 0
        assert engine.stats["failed"] == 1
        assert engine.stats["downgraded"] == 0

    @pytest.mark.asyncio
    async def test_copy_mode_is_unaffected(self, alog):
        """本来就是 copy 的规则一次就带 ``hide_sender_name``，不会「先失败再降级」。"""
        client = FakeClient()
        engine = ForwardEngine(client, build_config(mode="copy"), alog)
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)

        assert len(client.forwarded) == 1
        assert client.forwarded[0]["hide_sender_name"] is True
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

        assert client.forward_calls == 2
        assert len(client.forwarded) == 1
        assert client.forwarded[0]["message_ids"] == [100, 101]
        assert client.forwarded[0]["hide_sender_name"] is True
        assert engine.stats["forwarded"] == 1


class TestCopyModeSourceLink:
    """``copy`` 模式也要带上「🔗原文链接：…」。

    小白 2026-09-20：「附带来源的那个链接没有了」+「如果用 copy 能不能把链接放在
    copy 的下方，换行两次再加原文链接，格式改成 🔗原文链接：xxxx」。

    背景：``include_source_link`` 原先**只在 text 模式生效** —— ``_do()`` 里
    ``mode in {"forward", "copy"}`` 直接 return，压根走不到加链接那段。
    而 copy（含 forward 撞受保护源会话后的自动降级）用 ``drop_author`` 去掉了
    「转发自」抬头，Telegram 不再附带任何回溯入口 ⇒ 必须自己写进正文。

    ⚠️ Telegram 不支持给已发出的消息**追加**文本，只能整体重写一遍
    （``edit_message_text`` / ``edit_message_caption``）。
    """

    #: ``src_message`` 默认 message_id=100、chat=-1001111111111（无 username）
    LINK = "https://t.me/c/1111111111/100"

    @pytest.mark.asyncio
    async def test_copy_mode_appends_source_link(self, alog):
        client = FakeClient()
        engine = ForwardEngine(client, build_config(mode="copy"), alog)
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)

        assert len(client.edited) == 1, "copy 模式应当编辑一次，把链接补进正文"
        payload = client.edited[0]
        assert payload["chat_id"] == DST
        assert payload["text"] == f"关键词123\n\n🔗原文链接：{self.LINK}"
        assert payload["link_preview_options"] is not None, "必须禁掉链接预览"

    @pytest.mark.asyncio
    async def test_forward_mode_does_not_edit(self, alog):
        """forward 模式**不需要**补链接：Telegram 的「转发自」抬头本身就是回溯入口。"""
        client = FakeClient()
        engine = ForwardEngine(client, build_config(mode="forward"), alog)
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)

        assert client.edited == []
        assert client.forwarded[0]["hide_sender_name"] is None

    @pytest.mark.asyncio
    async def test_downgrade_also_appends_link(self, alog):
        """forward 撞受保护源会话自动降级成 copy ⇒ 同样要补链接。"""
        client = FakeClient(forward_error_once=ChatForwardsRestricted(value=RESTRICTED))
        engine = ForwardEngine(client, build_config(mode="forward"), alog)
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)

        assert engine.stats["downgraded"] == 1
        assert len(client.edited) == 1
        assert client.edited[0]["text"].endswith(f"🔗原文链接：{self.LINK}")

    @pytest.mark.asyncio
    async def test_include_source_link_false_skips_edit(self, alog):
        client = FakeClient()
        engine = ForwardEngine(
            client, build_config(mode="copy", include_source_link=False), alog
        )
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)

        assert client.edited == []

    @pytest.mark.asyncio
    async def test_media_message_uses_edit_caption(self, alog):
        """媒体消息没有 ``text``，只能改 caption。"""
        client = FakeClient()
        engine = ForwardEngine(client, build_config(mode="copy"), alog)
        engine.register()
        message = src_message("关键词123", caption="关键词123")
        message.text = None
        engine._handle(message, edited=False)
        await drain(engine)

        assert client.edited == []
        assert len(client.edited_captions) == 1
        assert client.edited_captions[0]["caption"] == f"关键词123\n\n🔗原文链接：{self.LINK}"

    @pytest.mark.asyncio
    async def test_edit_failure_does_not_fail_forward(self, alog):
        """链接没加上不该让整条转发记成失败 —— 内容已经发出去了。"""
        client = FakeClient()
        client.edit_error = RuntimeError("edit boom")
        engine = ForwardEngine(client, build_config(mode="copy"), alog)
        engine.register()
        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)

        assert engine.stats["forwarded"] == 1
        assert engine.stats["failed"] == 0

    @pytest.mark.asyncio
    async def test_existing_link_is_not_duplicated(self, alog):
        """原文里已经有同一个链接时不再重复追加。"""
        client = FakeClient()
        engine = ForwardEngine(client, build_config(mode="copy"), alog)
        engine.register()
        engine._handle(src_message(f"关键词123 见 {self.LINK}"), edited=False)
        await drain(engine)

        assert client.edited == [], "链接已在正文里，不该再编辑一次"

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


class TestCrossAccountDedupe:
    """跨账号去重：两个账号都监听到同一条消息时，发往**同一目标**只发一次。

    小白 2026-09-20 的需求：「需要做跨账号去重，因为两个账号的群可能重复」。

    线上背景：项目里早先那版跨账号去重挂在 Postgres 上（``TGA_POSTGRES_DSN``），
    而线上**根本没配 DSN** ⇒ ``check_dedupe`` 恒返回 False，功能等于关闭。
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
