"""「已使用注册码」拦截：学使用通知 → 转发前拦掉废码。

线上来由（2026-09-29）：规则 1 在 12:47:49 转发了一条
``CineTrail-30-Register_gCKHLMOCCM``。这条码 12:31 就以猜码形式
（``gCKHL**CCM``）出现过，期间早有人用掉了 —— 转发出去的是条废码。

用户原话：「刚刚有一条已经使用的注册码转发了，看看能不能规避，已经使用的不转发」。
"""

from __future__ import annotations

import json
import time

import pytest

from tg_assistant.config import AccountConfig, ForwardUsedCodes
from tg_assistant.forwarder import ForwardEngine
from tg_assistant.used_codes import (
    UsedCodeStore,
    code_value_of,
    visible_code_part,
    visible_value_of,
)

from .conftest import FakeChat, make_message
from .test_forwarder import SRC, build_config, drain, src_message


def write_global_used_codes(store, **overrides) -> ForwardUsedCodes:
    """把全局策略写进 ``data/forward_used_codes.json``（拦截已全局化，只有这份算数）。

    从一个**缺省值**出发再覆盖，而不是从零构造：缺省值改了测试要跟着改，
    否则测的是一份不存在的策略（与上面的 :func:`store` 同一个理由）。
    """
    from tg_assistant.forward_used_codes import ForwardUsedCodesStore

    payload = ForwardUsedCodes().model_dump(mode="json")
    payload.update(overrides)
    guard = ForwardUsedCodes.model_validate(payload)
    ForwardUsedCodesStore(store.paths.forward_used_codes_file).save(guard)
    return guard

#: 线上那条通知的真实形状（见 reg_grab 模块文档）。
NOTICE = "🎟️ 注册码使用 - jf [7002057019] 使用了 CineTrail-30-Register_gCKH░░░░░░░"
#: 记下来的 key 就是**可见的那截 token**（小写）。
PREFIX = "cinetrail-30-register_gckh"
#: 后来又被发出来的**完整**码。
FULL_CODE = "CineTrail-30-Register_gCKHLMOCCM"


def store(**overrides) -> UsedCodeStore:
    # 两条正则都取**生产默认值**，不在这里另抄一份：默认值改了测试要跟着改，
    # 否则测的是一份不存在的策略。
    defaults = ForwardUsedCodes()
    item = UsedCodeStore()
    options = {
        "enabled": True,
        "keywords": ["码使用"],
        "pattern": defaults.notice_pattern,
        "min_visible": 3,
        "ttl": 3600.0,
        "persist": False,
        "ignore_pattern": defaults.ignore_token_pattern,
    }
    options.update(overrides)
    item.configure(**options)
    return item


class TestUsedCodeStore:
    def test_learns_visible_token_from_online_notice(self):
        item = store()
        assert item.learn(NOTICE) == [PREFIX]  # 遮罩被剥掉，只留可见的那截
        assert item.known == 1

    def test_notice_itself_counts_as_used(self):
        """通知里就含它自己那截 token —— 所以通知本身也不会被转发出去。"""
        item = store()
        item.learn(NOTICE)
        assert item.is_used(NOTICE) == PREFIX

    def test_full_code_is_blocked_after_notice(self):
        item = store()
        item.learn(NOTICE)
        assert item.is_used(FULL_CODE) == PREFIX

    def test_unrelated_code_still_passes(self):
        """别的码**必须**照常转发 —— 这是这个功能最大的误伤风险。"""
        item = store()
        item.learn(NOTICE)
        assert item.is_used("CineTrail-30-Register_zzzzzzzzzz") is None

    def test_bare_value_mention_is_not_enough(self):
        """只提到那几位值、没提到完整 token 的不算命中（token 比裸值精确）。"""
        item = store()
        item.learn(NOTICE)
        assert item.is_used("这条消息提到了 gckh 但没提任何码") is None

    def test_short_visibility_is_not_recorded(self):
        """只露 2 位时几乎任何码都能对上，宁可不记。"""
        item = store()
        assert item.learn("注册码使用 - 使用了 X-30-Register_ab░░░░░░░░") == []
        assert item.known == 0

    def test_keyword_gate_rejects_chatter(self):
        """没有「码使用」关键词的句子不算通知，哪怕正则命中。"""
        item = store()
        assert item.learn("这个插件怎么使用 1panel 啊") == []
        assert item.known == 0

    def test_keyword_gate_can_be_disabled(self):
        item = store(keywords=[])
        assert item.learn("使用了 MSKY-30-Register_abcdefghij") == [
            "msky-30-register_abcdefghij"
        ]

    def test_learning_twice_counts_once(self):
        item = store()
        item.learn(NOTICE)
        assert item.learn(NOTICE) == []
        assert item.learned == 1

    def test_ttl_expiry(self):
        item = store(ttl=0.0)  # 0 = 永不过期
        item.learn(NOTICE)
        assert item.is_used(FULL_CODE) == PREFIX

        expired = store(ttl=0.05)
        expired.learn(NOTICE)
        expired._entries[PREFIX] = time.time() - 10.0  # 直接把它做旧
        assert expired.is_used(FULL_CODE) is None
        assert expired.known == 0

    def test_disabled_guard_learns_nothing(self):
        item = store(enabled=False)
        assert item.learn(NOTICE) == []
        assert item.is_used(FULL_CODE) is None

    def test_bad_pattern_does_not_raise(self):
        """正则写坏了只该等于"这个功能不生效"，不该让引擎起不来。"""
        item = store(pattern="([A-Za-z0-9")
        assert item.learn(NOTICE) == []

    def test_snapshot_shape(self):
        item = store()
        item.learn(NOTICE)
        snap = item.snapshot()
        assert snap["known"] == 1
        assert snap["learned"] == 1
        assert snap["enabled"] is True
        assert snap["persisted"] is False


class TestUsedCodeStorePersists:
    """码被用掉是**永久事实**，重启后忘掉 = 重启后又能转发一条废码。"""

    def test_round_trip(self, tmp_path):
        path = tmp_path / "used_codes.json"
        first = store(persist=True)
        first.state_path = path
        first.learn(NOTICE)
        assert path.exists()

        second = store(persist=True, state_path=path)
        assert second.known == 1
        assert second.is_used(FULL_CODE) == PREFIX
        assert second.restored == 1

    def test_expired_entries_are_not_restored(self, tmp_path):
        path = tmp_path / "used_codes.json"
        path.write_text(
            json.dumps({"version": 1, "entries": {PREFIX: time.time() - 99999}}),
            encoding="utf-8",
        )
        item = store(persist=True, state_path=path)
        assert item.known == 0

    def test_corrupt_file_does_not_break_startup(self, tmp_path):
        path = tmp_path / "used_codes.json"
        path.write_text("{ 这不是 json", encoding="utf-8")
        item = store(persist=True, state_path=path)
        assert item.known == 0
        assert item.learn(NOTICE) == [PREFIX]  # 之后照常工作

    def test_persist_off_writes_nothing(self, tmp_path):
        path = tmp_path / "used_codes.json"
        item = store(persist=False, state_path=path)
        item.learn(NOTICE)
        assert not path.exists()


class TestRealNoticeShapes:
    """线上真实收到的**两种**通知形状（2026-09-29 生产日志取证，共 9 条样本）。

    关键区别不是「哪种能切出来」，而是**哪种真会被转发**：

    * 形状 A ``7017826500-2cIE`` —— 用生产配置逐条规则试跑，**20 条规则没有一条**
      命中它（对照组 ``CineTrail-30-Register_gCKHLMOCCM`` 命中规则 1），也就是说
      它压根不会被转发 ⇒ 学它没有意义；
    * 形状 B ``ChaPanda-30-Register_Ayq`` —— 规则 1 命中、确实会被转发 ⇒ 必须学。

    用户原话：「``7017826500-2cIEq8ZKmN`` 这种无需转发，不用学习」。
    """

    #: 形状 A：``<用户id>-<可见值>░░░``（没有下划线）
    SHAPE_A = "· 🎟️ 注册码使用 - Spike [7017826500] 使用了 7017826500-2cIE░░░░░░░"
    #: 形状 B：``<名字>-<天数>-Register_<可见值>░``
    SHAPE_B = "· 🎟️ 注册码使用 - Cam [7716606614] 使用了 ChaPanda-30-Register_Ayq░"

    def test_shape_a_is_not_learned(self):
        """默认就不学 —— 这种形状没有任何规则会转发它。"""
        item = store()
        assert item.learn(self.SHAPE_A) == []
        assert item.known == 0

    def test_shape_a_is_not_blocked(self):
        item = store()
        item.learn(self.SHAPE_A)
        assert item.is_used("7017826500-2cIEq8ZKmN") is None

    def test_shape_a_can_be_learned_if_the_ignore_is_cleared(self):
        """面板上把「忽略形状」清空就又能学了 —— 留个退路，不是死写。"""
        item = store(ignore_pattern="")
        assert item.learn(self.SHAPE_A) == ["7017826500-2cie"]

    def test_shape_b_is_learned(self):
        item = store()
        assert item.learn(self.SHAPE_B) == ["chapanda-30-register_ayq"]

    def test_shape_b_full_code_is_blocked(self):
        item = store()
        item.learn(self.SHAPE_B)
        assert item.is_used("ChaPanda-30-Register_AyqXyZ123") == "chapanda-30-register_ayq"

    def test_other_channel_same_suffix_is_not_blocked(self):
        """另一个频道出现 ``Ayq`` 但 token 不同 —— 不许拦（token 带名字，正是为了这个）。"""
        item = store()
        item.learn(self.SHAPE_B)
        assert item.is_used("Iris-90-Register_AyqZZ") is None

    def test_only_shape_b_is_learned_when_both_arrive(self):
        item = store()
        item.learn(self.SHAPE_A)
        item.learn(self.SHAPE_B)
        assert item.known == 1
        assert item.is_used("ChaPanda-30-Register_AyqXyZ123") is not None
        assert item.is_used("7017826500-2cIEq8ZKmN") is None


class TestDefaultIgnorePattern:
    """默认「忽略形状」必须精确命中线上那种**不会被转发**的形状，且不误伤真码。

    用的都是生产日志里真实出现过的 token（2026-09-29 学到的那批）。
    """

    #: 线上学到过的形状 A token（``<用户id>-<值>``）
    SHAPE_A_TOKENS = [
        "7017826500-2cie",
        "8593864818-uwic",
        "8643208201-atjx",
        "595080793-fper",
        "1876596720-g0ky",
    ]
    #: 线上学到过的形状 B token（``<名字>-<天数>-<Register|Renew>_<值>``）
    SHAPE_B_TOKENS = [
        "chapanda-30-register_ayq",
        "cinetrail-30-register_gckh",
        "wenjian-30-audio-register_l10",
        "niubi-365-register_ip0",
        "guaipro-30-register_umf",
        "iris-30-register_13f",
        "alphao-93-renew_lo7",
        "guai-30-register_bzo",
        "infantry-30-register_xw1",
    ]

    def _pattern(self):
        import re

        return re.compile(ForwardUsedCodes().ignore_token_pattern)

    def test_matches_every_real_shape_a_token(self):
        pat = self._pattern()
        for token in self.SHAPE_A_TOKENS:
            assert pat.search(token), f"{token} 应该被忽略（它不会被任何规则转发）"

    def test_does_not_match_any_real_shape_b_token(self):
        pat = self._pattern()
        for token in self.SHAPE_B_TOKENS:
            assert not pat.search(token), f"{token} 是真会被转发的码，不能忽略"

    def test_empty_pattern_ignores_nothing(self):
        item = store(ignore_pattern="")
        assert item.learn("· 🎟️ 注册码使用 - x [1] 使用了 7017826500-2cIE░░░░░░░") == [
            "7017826500-2cie"
        ]
        assert item.known == 1

    def test_broken_pattern_is_not_fatal(self):
        """正则写错只等于这条不生效，不能把转发引擎带崩。"""
        item = store(ignore_pattern="([A-Za-z0-9")
        assert item.learn("· 🎟️ 注册码使用 - x [1] 使用了 ChaPanda-30-Register_Ayq░░░") == [
            "chapanda-30-register_ayq"
        ]


class TestJunkTokensAreRejected:
    """不是「码」的东西不能进记忆 —— 子串比对下它会把正常消息静默拦掉。

    🔴 线上事故（2026-09-29 15:36，云海Emby 交流群）：那个群的「使用通知」写法不同，
    学到了裸词 ``emby``。它会让**任何**提到 emby 的消息被拦，
    比如抽奖帖里的「🎫 加入-云海Emby 交流群」。
    """

    def test_bare_word_is_not_learned(self):
        item = store()
        assert item.learn("· 🎟️ 注册码使用 - hikai [1] 使用了 emby") == []
        assert item.known == 0

    def test_bare_word_would_have_blocked_unrelated_content(self):
        """说清楚为什么要挡它 —— 正反两面都钉住，证明这个判据不是可有可无的。"""
        junk = "🎰 双节抽奖\n🎫 加入-云海Emby 交流群"
        item = store()
        item.learn("· 🎟️ 注册码使用 - hikai [1] 使用了 emby")  # 被 `_plausible` 拒掉
        assert item.is_used(junk) is None
        # 反证：手工把 emby 塞进记忆，这条正常抽奖帖立刻就被拦了。
        item._entries["emby"] = time.time()
        assert item.is_used(junk) == "emby"

    def test_junk_and_wrong_shape_are_purged_when_loaded_from_disk(self, tmp_path):
        """老版本已经写进磁盘的垃圾条目 / 不该记的形状，重启时必须被清掉。

        ⚠️ 这里是**两段式**的，别只测一半：``_load()`` 跑在 ``configure()`` 之前，
        所以「像不像码」（``emby``）在第一段清掉，而「忽略形状」（形状A）只能等策略
        灌进来之后在第二段清 —— 2026-09-29 线上验证时正是第二段漏了，形状A 留了下来。
        """
        path = tmp_path / "used_codes.json"
        stamp = time.time()  # 同一时刻，TTL 排除掉，只看形状判据
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "entries": {
                        "emby": stamp,  # 垃圾：裸词
                        "8643208201-atjx": stamp,  # 形状A：不会被任何规则转发
                        "chapanda-30-register_ayq": stamp,  # 真码
                    },
                }
            ),
            encoding="utf-8",
        )
        item = UsedCodeStore(state_path=path)
        item.configure(
            enabled=True,
            keywords=["码使用"],
            pattern=ForwardUsedCodes().notice_pattern,
            min_visible=3,
            ttl=3600.0,
            persist=True,
            ignore_pattern=ForwardUsedCodes().ignore_token_pattern,
        )
        assert sorted(item._entries) == ["chapanda-30-register_ayq"]
        assert item.is_used("这条消息提到了 emby 群") is None
        assert item.is_used("8643208201-atjx") is None
        assert item.is_used("ChaPanda-30-Register_AyqXyZ123") is not None
        # 顺手写回磁盘：不然垃圾会一直躺着，每次重启都要重清一遍。
        on_disk = json.loads(path.read_text(encoding="utf-8"))["entries"]
        assert sorted(on_disk) == ["chapanda-30-register_ayq"]


class TestSharedCodeSplitting:
    """两边必须按同一口径切 —— 口径不同就永远比不上，而且是**静默**比不上。"""

    def test_mask_is_stripped(self):
        assert visible_code_part("MSKY-30-Register_f1t░░░░░░░") == "MSKY-30-Register_f1t"

    def test_value_is_after_last_underscore(self):
        assert code_value_of("MSKY-30-Register_f1t") == "f1t"
        assert code_value_of("Register_f1tAbCdEfGh") == "f1tabcdefgh"

    def test_visible_value_is_trailing_alnum_run(self):
        # 形状 A：没有下划线，也要能切出真正有区分度的那几位。
        assert visible_value_of("7017826500-2cIE") == "2cie"
        # 形状 B：名字部分（``ChaPanda-30-Register_``）对所有码都一样，不算进去。
        assert visible_value_of("ChaPanda-30-Register_Ayq") == "ayq"
        assert visible_value_of("Iris-90-Register_3CQ") == "3cq"
        assert visible_value_of("") == ""

    def test_reg_grab_reexports_are_the_same_objects(self):
        """抢注引擎从 ``used_codes`` 再导出，两处必须是同一个函数。"""
        from tg_assistant import reg_grab

        assert reg_grab.visible_code_part is visible_code_part
        assert reg_grab.code_value_of is code_value_of


class TestForwarderBlocksUsedCode:
    """端到端：引擎先学通知，再拒绝转发那条完整码。"""

    #: 接真实 store 时用的账号名（只是给路径解析一个合法名字，配置内容不重要）。
    ACCOUNT = "uc-acct"

    def engine(self, client, alog, **guard_overrides):
        """不接 store 的引擎：全局策略退化成纯内存那份（``ForwardUsedCodes`` 缺省值）。

        ⚠️ ``guard_overrides`` 改的是**账号配置**里的 ``forward.used_codes``。
        那份已经不再驱动引擎（拦截已全局化），所以只对"验证它确实失效"的用例有意义；
        "让某个开关真的生效"要走 :meth:`engine_with_global`。
        """
        config = build_config(match={"mode": "regex", "patterns": [r"Register_[A-Za-z0-9]{10}"]})
        guard = config.forward.used_codes
        for key, value in guard_overrides.items():
            setattr(guard, key, value)
        return ForwardEngine(client, config, alog)

    def engine_with_global(self, store, client, alog) -> ForwardEngine:
        """接上真实 store 的引擎：全局策略的路径由 store 决定（``data/forward_used_codes.json``）。

        不接 store 时全局策略是纯内存的、永远等于缺省值 —— 那样就测不出
        "面板改了全局策略、引擎跟着变"这条主线。
        """
        config = build_config(match={"mode": "regex", "patterns": [r"Register_[A-Za-z0-9]{10}"]})
        return ForwardEngine(client, config, alog, store=store, account=self.ACCOUNT)

    @pytest.mark.asyncio
    async def test_notice_then_full_code_not_forwarded(self, client, alog):
        engine = self.engine(client, alog)
        engine.register()

        engine._handle(src_message(NOTICE), edited=False)
        await drain(engine)
        # 通知里的码是遮罩的，本就不匹配规则；但**学**这一步必须发生。
        assert client.forwarded == []
        assert engine.stats["used_learned"] == 1

        engine._handle(make_message(FULL_CODE, message_id=200, chat=FakeChat(SRC, title="来源群")), edited=False)
        await drain(engine)

        assert client.forwarded == []
        assert engine.stats["used_skipped"] == 1
        assert engine.stats["forwarded"] == 0

    @pytest.mark.asyncio
    async def test_notice_that_matches_a_rule_is_still_not_forwarded(self, client, alog):
        """使用通知本身不是"码"，规则就算命中它也不该转发出去。

        用户现在的规则是靠正则里手写 ``(?!.*码使用)`` 挡的 —— 20 条正则里只有
        3 条写了，漏掉的那些就会把通知当码转发出去。这里让**规则故意命中**通知，
        验证学到的前缀本身就足以挡住它。
        """
        config = build_config(match={"mode": "regex", "patterns": [r"注册码使用"]})
        engine = ForwardEngine(client, config, alog)
        engine.register()

        engine._handle(src_message(NOTICE), edited=False)
        await drain(engine)

        assert client.forwarded == []
        assert engine.stats["used_skipped"] == 1

    @pytest.mark.asyncio
    async def test_unrelated_code_is_still_forwarded(self, client, alog):
        """学了通知之后，**别的**码必须照常转发（误伤是这里最大的风险）。"""
        engine = self.engine(client, alog)
        engine.register()

        engine._handle(src_message(NOTICE), edited=False)
        await drain(engine)
        engine._handle(
            make_message("CineTrail-30-Register_zzzzzzzzzz", message_id=200, chat=FakeChat(SRC, title="来源群")),
            edited=False,
        )
        await drain(engine)

        assert len(client.forwarded) == 1
        assert engine.stats["used_skipped"] == 0

    @pytest.mark.asyncio
    async def test_guard_can_be_switched_off(self, store, client, alog):
        """全局策略关掉之后不再学、也不再拦。

        🔴 关它要写**全局**那份（``data/forward_used_codes.json``）——
        改账号配置里的 ``forward.used_codes`` 已经没有任何效果（见下一个用例）。
        """
        write_global_used_codes(store, enabled=False)
        engine = self.engine_with_global(store, client, alog)
        engine.register()

        engine._handle(src_message(NOTICE), edited=False)
        await drain(engine)
        engine._handle(make_message(FULL_CODE, message_id=200, chat=FakeChat(SRC, title="来源群")), edited=False)
        await drain(engine)

        assert len(client.forwarded) == 1
        assert engine.stats["used_learned"] == 0

    @pytest.mark.asyncio
    async def test_account_level_used_codes_is_ignored(self, store, client, alog):
        """账号配置里的 ``forward.used_codes`` **不再驱动引擎** —— 全局那份才是唯一来源。

        用户原话：「做成全局，而不是账号级」。这条钉住"老字段彻底失效"：
        线上三个 ``config.json`` 里那三份旧配置还在，如果不钉住，
        以后有人改了账号级那份、发现没反应，会以为是 bug 而不是"它已经不看了"。
        """
        config = build_config(match={"mode": "regex", "patterns": [r"Register_[A-Za-z0-9]{10}"]})
        config.forward.used_codes.enabled = False  # 账号级关掉（应当被忽略）
        store.save_account_config(self.ACCOUNT, config)
        loaded = store.load_account_config(self.ACCOUNT, create=False)

        engine = ForwardEngine(client, loaded, alog, store=store, account=self.ACCOUNT)

        assert engine.used_codes.enabled is True, (
            "账号级 used_codes 关掉不该影响引擎 —— 全局那份（缺省 = 开）才是唯一来源"
        )

    @pytest.mark.asyncio
    async def test_blocked_message_does_not_burn_dedupe_slot(self, client, alog):
        """被拦下的这条不该在去重表里留下记录（否则会把后面该发的那条顶掉）。"""
        engine = self.engine(client, alog)
        engine.register()

        engine._handle(src_message(NOTICE), edited=False)
        await drain(engine)
        engine._handle(make_message(FULL_CODE, message_id=200, chat=FakeChat(SRC, title="来源群")), edited=False)
        await drain(engine)

        assert engine.dedupe.check_and_add(("r1", SRC, 200)) is True

    @pytest.mark.asyncio
    async def test_snapshot_surfaces_counters(self, client, alog):
        engine = self.engine(client, alog)
        engine.register()
        engine._handle(src_message(NOTICE), edited=False)
        await drain(engine)

        snap = engine.snapshot()
        assert snap["used_learned"] == 1
        assert snap["used_skipped"] == 0
        assert snap["used_codes"]["known"] == 1

    @pytest.mark.asyncio
    async def test_hot_reload_applies_new_guard_config(self, store, client, alog):
        """改**全局** ``forward_used_codes.json`` 必须热重载生效（面板改完立刻管用）。

        策略不在账号 ``config.json`` 里 —— 如果指纹里不带它，``apply_config`` 会判定
        "没变化"直接返回，用户在面板上关掉拦截却毫无反应，只能重启账号。
        """
        write_global_used_codes(store, enabled=True)
        engine = self.engine_with_global(store, client, alog)
        engine.register()
        assert engine.used_codes.enabled is True

        write_global_used_codes(store, enabled=False)
        assert engine.apply_config(engine.config.model_copy(deep=True)) is True
        assert engine.used_codes.enabled is False

    @pytest.mark.asyncio
    async def test_reload_keeps_policy_when_only_rules_change(self, client, alog):
        """只改规则时，已学到的记忆不能被清掉。"""
        engine = self.engine(client, alog)
        engine.register()
        engine._handle(src_message(NOTICE), edited=False)
        await drain(engine)
        assert engine.used_codes.known == 1

        config = engine.config.model_copy(deep=True)
        config.forward.rules[0].name = "改了个名字"
        engine.apply_config(config)
        assert engine.used_codes.known == 1
        assert engine.used_codes.is_used(FULL_CODE) == PREFIX


class TestUsedCodesConfig:
    def test_defaults(self):
        guard = ForwardUsedCodes()
        assert guard.enabled is True
        assert guard.notice_keywords == ["码使用"]
        assert guard.min_visible == 3
        assert guard.ttl == 3600.0
        assert guard.persist is True

    def test_keywords_accept_comma_string(self):
        guard = ForwardUsedCodes(notice_keywords="码使用, 已使用")
        assert guard.notice_keywords == ["码使用", "已使用"]

    def test_bad_regex_rejected(self):
        with pytest.raises(ValueError):
            ForwardUsedCodes(notice_pattern="([A-Za-z0-9")

    def test_forward_config_carries_the_guard(self):
        config = AccountConfig.model_validate({"forward": {"enabled": True}})
        assert config.forward.used_codes.enabled is True

    def test_unknown_key_rejected(self):
        with pytest.raises(ValueError):
            ForwardUsedCodes(nonsense=1)
