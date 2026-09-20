"""转发引擎：匹配、派发、相册聚合、去重、失败处理。"""

from __future__ import annotations

import asyncio
import datetime as dt
import os

import pytest
from pyrogram.errors import ChatWriteForbidden, FloodWait

from tg_assistant.config import AccountConfig
from tg_assistant.forwarder import DedupeCache, ForwardEngine, PreparedRule

from .conftest import FakeChat, FakeUser, make_message

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
