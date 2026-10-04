"""配置解析与校验。"""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from tg_assistant.config import (
    AccountConfig,
    ForwardConfig,
    ForwardRule,
    MatchConfig,
    NotifyConfig,
    ProxyConfig,
    RedPacketConfig,
    RedPacketTask,
    RegGrabConfig,
    RegGrabTask,
    Settings,
    expand_env,
    mask_phone,
    parse_chat_ref,
)


class TestProxyConfig:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("socks5://127.0.0.1:1080", ("socks5", "127.0.0.1", 1080, None, None)),
            ("127.0.0.1:1080", ("socks5", "127.0.0.1", 1080, None, None)),
            ("socks5h://1.2.3.4:9050", ("socks5", "1.2.3.4", 9050, None, None)),
            ("http://proxy.local:8080", ("http", "proxy.local", 8080, None, None)),
            ("https://proxy.local:8080", ("http", "proxy.local", 8080, None, None)),
            (
                "socks5://u:p@10.0.0.1:1080",
                ("socks5", "10.0.0.1", 1080, "u", "p"),
            ),
            # 老式 host:port:user:pass 写法
            ("10.0.0.1:1080:u:p", ("socks5", "10.0.0.1", 1080, "u", "p")),
        ],
    )
    def test_from_url(self, url, expected):
        proxy = ProxyConfig.from_url(url)
        assert (
            proxy.scheme,
            proxy.hostname,
            proxy.port,
            proxy.username,
            proxy.password,
        ) == expected

    def test_url_encoded_credentials(self):
        """密码里有 @ / : 时必须百分号编码，解析后要还原。"""
        proxy = ProxyConfig.from_url("socks5://user:p%40ss%3A1@1.2.3.4:1080")
        assert proxy.username == "user"
        assert proxy.password == "p@ss:1"

    @pytest.mark.parametrize(
        "url", ["", "   ", "socks5://:1080", "ftp://host:21", "socks5://host"]
    )
    def test_invalid_urls(self, url):
        with pytest.raises(ValueError):
            ProxyConfig.from_url(url)

    def test_to_pyrogram_shape(self):
        proxy = ProxyConfig.from_url("socks5://u:p@1.2.3.4:1080")
        payload = proxy.to_pyrogram()
        assert payload == {
            "scheme": "socks5",
            "hostname": "1.2.3.4",
            "port": 1080,
            "username": "u",
            "password": "p",
        }

    def test_to_url_hides_password(self):
        proxy = ProxyConfig.from_url("socks5://u:secret@1.2.3.4:1080")
        assert "secret" not in proxy.to_url()
        assert proxy.to_url().startswith("socks5://u:***@1.2.3.4:1080")

    def test_env_expansion(self, monkeypatch):
        monkeypatch.setenv("MY_PROXY_HOST", "192.168.1.1")
        proxy = ProxyConfig(hostname="${MY_PROXY_HOST}", port=1080)
        assert proxy.hostname == "192.168.1.1"


class TestExpandEnv:
    def test_plain(self):
        assert expand_env("hello") == "hello"

    def test_var(self, monkeypatch):
        monkeypatch.setenv("TGA_TEST_X", "42")
        assert expand_env("${TGA_TEST_X}") == "42"

    def test_default(self, monkeypatch):
        monkeypatch.delenv("TGA_TEST_MISSING", raising=False)
        assert expand_env("${TGA_TEST_MISSING:-fallback}") == "fallback"

    def test_missing_without_default_stays_literal(self, monkeypatch):
        """保留原样而不是变成空串，方便在校验阶段报"你没设这个变量"。"""
        monkeypatch.delenv("TGA_TEST_NOPE", raising=False)
        assert expand_env("${TGA_TEST_NOPE}") == "${TGA_TEST_NOPE}"


class TestParseChatRef:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (-1001234567890, -1001234567890),
            ("-1001234567890", -1001234567890),
            ("123456", 123456),
            ("@mychannel", "mychannel"),
            ("mychannel", "mychannel"),
            ("https://t.me/mychannel", "mychannel"),
            ("t.me/mychannel", "mychannel"),
            ("https://t.me/c/1234567890/55", -1001234567890),
            ("me", "me"),
        ],
    )
    def test_parse(self, raw, expected):
        assert parse_chat_ref(raw) == expected

    def test_blank_becomes_none(self):
        """空串在配置里应被丢弃（由 _normalize_refs 过滤），而不是当成用户名。"""
        assert parse_chat_ref("") is None
        assert parse_chat_ref("   ") is None
        assert parse_chat_ref(None) is None

    def test_blank_entries_dropped_from_rule(self):
        rule = ForwardRule(
            id="r", sources=["", "@src", "  "], targets=["me"], match={"mode": "all"}
        )
        assert rule.sources == ["src"]


class TestMatchConfig:
    def test_regex_compiles(self):
        match = MatchConfig(mode="regex", patterns=[r"\d+元"])
        assert match.patterns == [r"\d+元"]

    def test_bad_regex_rejected(self):
        with pytest.raises(ValidationError) as info:
            MatchConfig(mode="regex", patterns=["(unclosed"])
        assert "正则" in str(info.value)

    def test_regex_requires_patterns(self):
        with pytest.raises(ValidationError):
            MatchConfig(mode="regex", patterns=[])

    def test_all_mode_needs_no_patterns(self):
        assert MatchConfig(mode="all").patterns == []

    def test_unknown_field_rejected(self):
        """extra="forbid"：写错字段名要立刻报错，而不是被静默忽略。"""
        with pytest.raises(ValidationError):
            MatchConfig(mode="all", patern=["typo"])


class TestForwardRule:
    def test_requires_targets(self):
        with pytest.raises(ValidationError):
            ForwardRule(id="r", targets=[], match={"mode": "all"})

    def test_normalizes_chat_refs(self):
        rule = ForwardRule(
            id="r",
            sources=["@src", "https://t.me/c/1234567890/9"],
            targets=["-1009876543210"],
            match={"mode": "all"},
        )
        assert rule.sources == ["src", -1001234567890]
        assert rule.targets == [-1009876543210]

    def test_label_falls_back_to_id(self):
        assert ForwardRule(id="abc", targets=["me"], match={"mode": "all"}).label == "abc"
        rule = ForwardRule(id="abc", name="订单", targets=["me"], match={"mode": "all"})
        assert rule.label == "订单"

    def test_text_mode_requires_template_or_default(self):
        rule = ForwardRule(id="r", targets=["me"], mode="text", match={"mode": "all"})
        assert rule.mode == "text"

    def test_only_from_bots_defaults_false_and_accepts_true(self):
        base = ForwardRule(id="r", targets=["me"], match={"mode": "all"})
        assert base.only_from_bots is False
        assert ForwardRule(
            id="r2", targets=["me"], match={"mode": "all"}, only_from_bots=True
        ).only_from_bots is True


class TestAutoRuleId:
    """转发规则的 id 也不必填（用户原话：「ID 不要必填」）。

    和红包/抢注共用同一个 ``_auto_task_id``；**已存在的规则 id 永不改动**
    （生成只发生在「这条还没有 id」时），所以事件日志里的 ``rule`` 字段、
    面板卡片、历史统计都还对得上。
    """

    @staticmethod
    def _rule(**overrides) -> dict:
        base = {"targets": [-1001234567890], "match": {"mode": "all"}}
        base.update(overrides)
        return base

    def test_blank_id_is_generated_from_the_name(self):
        config = ForwardConfig(rules=[self._rule(name="Main Rule")])
        assert config.rules[0].id == "main-rule", "名称里的 ASCII 片段要拿来做 id"

    def test_missing_id_key_also_generates(self):
        """「缺省」也得能用：面板新增一条规则时发上来的就是不带 id 的对象。"""
        config = ForwardConfig(rules=[self._rule()])
        assert config.rules[0].id

    def test_chinese_name_falls_back_to_a_short_random_id(self):
        config = ForwardConfig(rules=[self._rule(name="卷毛鼠")])
        assert re.fullmatch(r"task-[0-9a-f]{8}", config.rules[0].id), config.rules[0].id

    def test_explicit_id_wins(self):
        config = ForwardConfig(rules=[self._rule(id="404", name="别的名字")])
        assert config.rules[0].id == "404"

    def test_same_name_twice_does_not_collide(self):
        """复制一条规则改改是常规操作：两条同名规则不能生成同一个 id。"""
        first, second = ForwardConfig(
            rules=[self._rule(name="Main"), self._rule(name="Main")]
        ).rules
        assert first.id == "main"
        assert second.id != first.id and second.id.startswith("main")

    def test_generated_id_avoids_an_explicit_one(self):
        config = ForwardConfig(rules=[self._rule(id="main"), self._rule(name="Main")])
        assert config.rules[1].id != "main"

    def test_survives_a_dump_validate_round_trip(self):
        """🔴 重新加载后必须**不变**：生成只在「这条规则还没有 id」时发生。"""
        config = ForwardConfig(rules=[self._rule(name="主群"), self._rule(name="备用群")])
        ids = [rule.id for rule in config.rules]
        again = ForwardConfig.model_validate(config.model_dump(mode="json"))
        assert [rule.id for rule in again.rules] == ids

    def test_duplicate_explicit_ids_are_still_rejected(self):
        """自动生成不能顺手把「用户手打的重复 id」也放过去。"""
        with pytest.raises(ValidationError, match="重复"):
            ForwardConfig(rules=[self._rule(id="same"), self._rule(id="same")])

    def test_id_is_trimmed_and_must_be_a_string(self):
        assert ForwardConfig(rules=[self._rule(id="  padded  ")]).rules[0].id == "padded"
        with pytest.raises(ValidationError):
            ForwardConfig(rules=[self._rule(id=123)])


class TestForwardConfig:
    """账号级排除频道：写一次，所有规则都不监听。"""

    def test_exclude_chats_defaults_to_empty(self):
        assert ForwardConfig().exclude_chats == []

    def test_exclude_chats_normalizes_refs(self):
        config = ForwardConfig(exclude_chats=["@Noisy_Channel", "-1001234567890", None])
        # None 会被丢弃，@ 与大小写被归一化，数字字符串转 int
        assert config.exclude_chats == ["noisy_channel", -1001234567890]

    def test_exclude_chats_accepts_bare_string(self):
        assert ForwardConfig(exclude_chats="@only_one").exclude_chats == ["only_one"]

    def test_exclude_chats_round_trips(self):
        config = ForwardConfig(exclude_chats=[-100999, "@a_b"])
        again = ForwardConfig.model_validate(config.model_dump(mode="json"))
        assert again.exclude_chats == config.exclude_chats

    # ---- 发送者黑名单（账号级）----
    def test_exclude_users_defaults_to_empty(self):
        assert ForwardConfig().exclude_users == []

    def test_exclude_users_normalizes_refs(self):
        config = ForwardConfig(exclude_users=["@Spammer", "-1001234567890", None])
        # 与「排除频道」共用同一套归一化：@ 与大小写被抹平、数字字符串转 int、None 丢弃
        assert config.exclude_users == ["spammer", -1001234567890]

    def test_exclude_users_accepts_bare_string(self):
        assert ForwardConfig(exclude_users="@only_one").exclude_users == ["only_one"]

    def test_exclude_users_accepts_bare_int(self):
        """用户从面板 / CLI 只填一个数字时不该报错 —— 和 exclude_chats 一致。"""
        assert ForwardConfig(exclude_users=555).exclude_users == [555]

    def test_exclude_users_round_trips(self):
        config = ForwardConfig(exclude_users=[555, "@a_b"])
        again = ForwardConfig.model_validate(config.model_dump(mode="json"))
        assert again.exclude_users == config.exclude_users

    def test_exclude_users_and_chats_are_independent(self):
        """两个名单不能互相串 —— 串了就会「拉黑一个人结果屏蔽了一个群」。"""
        config = ForwardConfig(exclude_chats=[-100999], exclude_users=[555])
        assert config.exclude_chats == [-100999]
        assert config.exclude_users == [555]

    def test_exclude_chats_does_not_affect_rules(self):
        """排除列表是账号级的，不该和规则校验互相干扰。"""
        config = ForwardConfig(
            exclude_chats=[-100999],
            rules=[ForwardRule(id="r", targets=["me"], match={"mode": "all"})],
        )
        assert [r.id for r in config.active_rules] == ["r"]


class TestNotifyConfig:
    def test_enabled_requires_token_and_chat(self):
        with pytest.raises(ValidationError) as info:
            NotifyConfig(enabled=True)
        assert "bot_token" in str(info.value)

    def test_disabled_allows_empty(self):
        assert NotifyConfig(enabled=False).bot_token is None

    def test_wants_event(self):
        config = NotifyConfig(
            enabled=True, bot_token="1:x" * 4, chat_id=1, events=["forward"]
        )
        assert config.wants("forward")
        assert not config.wants("red_packet")

    def test_token_from_env(self, monkeypatch):
        monkeypatch.setenv("TGA_TEST_BOT_TOKEN", "123456:ABCDEF")
        config = NotifyConfig(
            enabled=True, bot_token="${TGA_TEST_BOT_TOKEN}", chat_id=-100123
        )
        assert config.bot_token == "123456:ABCDEF"

    def test_unexpanded_env_rejected(self, monkeypatch):
        """占位符没被替换说明用户忘了设环境变量，此时启动会失败得很难懂。"""
        monkeypatch.delenv("TGA_NOT_SET_TOKEN", raising=False)
        with pytest.raises(ValidationError):
            NotifyConfig(enabled=True, bot_token="${TGA_NOT_SET_TOKEN}", chat_id=1)


def rp_task(**overrides) -> dict:
    """一条抢红包任务的最小可用配置。"""
    task = {"id": "t", **overrides}
    return task


class TestRedPacketTask:
    """任务级的校验与 ready 判定。"""

    def test_defaults_are_conservative(self):
        task = RedPacketTask(id="t")
        assert task.enabled is True
        assert task.strategy == "auto"
        assert task.reply.only_on_success is True
        assert task.max_attempts >= 1
        assert task.ready is True

    def test_reply_enabled_without_texts_is_not_ready(self):
        """🔴 不再抛异常 —— 多任务下一条没填全不该让整份配置存不下去。

        单任务时代这里是 ``pytest.raises(ValidationError)``；改成
        「存得下、跑不动、界面标黄」是为了让用户能先开开关再回头填词。
        """
        task = RedPacketTask(id="t", reply={"enabled": True, "texts": []})
        assert task.ready is False
        assert "回复语" in (task.problem or "")

    def test_delay_range_must_be_ordered(self):
        with pytest.raises(ValidationError):
            RedPacketTask(id="t", reply={"enabled": True, "texts": ["a"], "delay_range": [3, 1]})

    def test_keyword_strategy_without_template_is_not_ready(self):
        task = RedPacketTask(id="t", strategy="keyword")
        assert task.ready is False
        assert "关键词" in (task.problem or "")

    def test_bad_success_pattern(self):
        with pytest.raises(ValidationError):
            RedPacketTask(id="t", success={"success_patterns": ["(("]})

    def test_blank_id_is_normalized_not_rejected(self):
        """id 不再必填（用户纠正：「ID 不要必填，名称填了就行」）。

        这一层只管**归一化**：``"   "``/``None`` 都变成空串，交给
        ``RedPacketConfig`` 按名称自动生成一个（见 ``TestAutoTaskId``）——
        字段校验器看不到兄弟任务，生成必须放在父模型里，否则两条同名任务会
        生成同一个 id 再撞唯一性校验。
        """
        assert RedPacketTask(id="   ").id == ""
        assert RedPacketTask(id=None).id == ""
        assert RedPacketTask().id == ""
        with pytest.raises(ValidationError):
            RedPacketTask(id=123)

    def test_label_falls_back_to_id(self):
        assert RedPacketTask(id="a").label == "a"
        assert RedPacketTask(id="a", name="主频道").label == "主频道"


class TestRedPacketConfig:
    """账号级：任务列表 + 并发上限。"""

    def test_defaults_are_conservative(self):
        config = RedPacketConfig()
        assert config.enabled is False
        assert config.tasks == []
        assert config.active_tasks == []
        assert config.max_concurrency >= 1

    def test_duplicate_task_ids_rejected(self):
        with pytest.raises(ValidationError):
            RedPacketConfig(tasks=[{"id": "same"}, {"id": "same"}])

    def test_active_tasks_skips_disabled(self):
        config = RedPacketConfig(
            tasks=[{"id": "a"}, {"id": "b", "enabled": False}, {"id": "c"}]
        )
        assert [t.id for t in config.active_tasks] == ["a", "c"]

    def test_watched_chats_unions_tasks(self):
        config = RedPacketConfig(
            tasks=[{"id": "a", "chats": [-1]}, {"id": "b", "chats": [-2]}]
        )
        assert config.watched_chats == [-1, -2]

    def test_watched_chats_empty_when_any_task_watches_all(self):
        """🔴 一条「全监听」的任务必须把整体拉成全监听。

        只做简单并集的话，那条留空的会被无声忽略 —— 用户会以为
        「我都留空了怎么还是只监听那两个频道」。
        """
        config = RedPacketConfig(tasks=[{"id": "a", "chats": [-1]}, {"id": "b", "chats": []}])
        assert config.watched_chats == []

    def test_include_edited_is_any(self):
        config = RedPacketConfig(
            tasks=[{"id": "a", "include_edited": False}, {"id": "b", "include_edited": True}]
        )
        assert config.include_edited is True
        assert RedPacketConfig(tasks=[{"id": "a"}]).include_edited is True


class TestRedPacketLegacyMigration:
    """旧版扁平配置必须能**无缝**读进来。"""

    def test_flat_config_becomes_one_task(self):
        """🔴 服务器上跑着的 config.json 就是旧结构。

        直接因为 extra="forbid" 报错会让**整份账号配置加载失败** ——
        连带转发、通知、抢注一起停摆，而不只是抢红包不可用。
        """
        config = RedPacketConfig.model_validate(
            {
                "enabled": True,
                "chats": [-100],
                "strategy": "keyword",
                "detect": {"keyword_template": "/grab {code}"},
                "delay": 1.5,
                "max_concurrency": 5,
                "reply": {"enabled": True, "texts": ["xxlb"]},
            }
        )
        assert len(config.tasks) == 1
        task = config.tasks[0]
        assert task.id == "default"
        assert task.label == "默认任务"
        assert task.chats == [-100]
        assert task.strategy == "keyword"
        assert task.detect.keyword_template == "/grab {code}"
        assert task.delay == 1.5
        assert task.reply.texts == ["xxlb"]
        # 账号级字段留在外层，没被卷进任务里。
        assert config.max_concurrency == 5
        assert config.enabled is True

    def test_new_shape_is_left_alone(self):
        config = RedPacketConfig.model_validate(
            {"tasks": [{"id": "a", "chats": [-1]}], "max_concurrency": 7}
        )
        assert [t.id for t in config.tasks] == ["a"]
        assert config.max_concurrency == 7

    def test_empty_config_does_not_invent_a_task(self):
        assert RedPacketConfig.model_validate({}).tasks == []
        assert RedPacketConfig.model_validate({"enabled": True}).tasks == []

    def test_account_config_loads_a_legacy_file(self):
        """从 AccountConfig 那一层进来也要能迁移（真实加载路径）。"""
        account = AccountConfig.model_validate(
            {"red_packet": {"enabled": True, "chats": [-5], "delay": 2.0}}
        )
        assert len(account.red_packet.tasks) == 1
        assert account.red_packet.tasks[0].delay == 2.0

    def test_migrated_config_is_stable_across_round_trips(self):
        """迁移只发生一次：再存再读不会又多出一条任务。"""
        first = RedPacketConfig.model_validate({"chats": [-1], "delay": 1.0})
        dumped = first.model_dump(mode="json")
        assert "chats" not in dumped, "旧字段不该被再写出去"
        second = RedPacketConfig.model_validate(dumped)
        assert len(second.tasks) == 1


class TestRegGrabConfig:
    """账号级：任务列表 + 并发上限（对齐抢红包）。"""

    def test_defaults_are_conservative(self):
        config = RegGrabConfig()
        assert config.enabled is False
        assert config.tasks == []
        assert config.active_tasks == []
        assert config.max_concurrency >= 1

    def test_duplicate_task_ids_rejected(self):
        with pytest.raises(ValidationError):
            RegGrabConfig(tasks=[{"id": "same"}, {"id": "same"}])

    def test_active_tasks_skips_disabled(self):
        config = RegGrabConfig(
            tasks=[{"id": "a"}, {"id": "b", "enabled": False}, {"id": "c"}]
        )
        assert [t.id for t in config.active_tasks] == ["a", "c"]

    def test_watched_chats_unions_tasks(self):
        config = RegGrabConfig(
            tasks=[{"id": "a", "chats": [-1]}, {"id": "b", "chats": [-2]}]
        )
        assert config.watched_chats == [-1, -2]

    def test_watched_chats_empty_when_any_task_watches_all(self):
        config = RegGrabConfig(tasks=[{"id": "a", "chats": [-1]}, {"id": "b", "chats": []}])
        assert config.watched_chats == []

    def test_include_edited_is_any(self):
        config = RegGrabConfig(
            tasks=[{"id": "a", "include_edited": False}, {"id": "b", "include_edited": True}]
        )
        assert config.include_edited is True
        assert RegGrabConfig(tasks=[{"id": "a"}]).include_edited is True

    def test_task_ready_needs_code_pattern_and_steps(self):
        empty = RegGrabTask(id="a")
        assert empty.ready is False
        assert empty.problem is not None
        ready = RegGrabTask(
            id="b",
            detect={"code_pattern": r"([A-Z]{4}-\d+)"},
            steps=[{"type": "send", "text": "/bind {code}"}],
        )
        assert ready.ready is True
        assert ready.problem is None


class TestRegGrabLegacyMigration:
    """旧版扁平配置必须能**无缝**读进来。"""

    def test_flat_config_becomes_one_task(self):
        """🔴 服务器上跑着的 config.json 就是旧结构。

        直接因为 extra="forbid" 报错会让**整份账号配置加载失败** ——
        连带转发、通知、抢红包一起停摆，而不只是抢注不可用。
        """
        config = RegGrabConfig.model_validate(
            {
                "enabled": True,
                "chats": [-100],
                "detect": {"code_pattern": r"([A-Z]{4}-\d+)"},
                "steps": [{"type": "send", "text": "/bind {code}"}],
                "delay": 1.5,
                "code_ttl": 1200,
                "max_concurrency": 5,
                "window": {"enabled": True, "start": "09:00", "end": "22:00"},
            }
        )
        assert len(config.tasks) == 1
        task = config.tasks[0]
        assert task.id == "default"
        assert task.label == "默认任务"
        assert task.chats == [-100]
        assert task.detect.code_pattern == r"([A-Z]{4}-\d+)"
        assert task.delay == 1.5
        assert task.code_ttl == 1200
        assert task.window.start == "09:00"
        # 账号级字段留在外层，没被卷进任务里。
        assert config.max_concurrency == 5
        assert config.enabled is True

    def test_new_shape_is_left_alone(self):
        config = RegGrabConfig.model_validate(
            {"tasks": [{"id": "a", "chats": [-1]}], "max_concurrency": 7}
        )
        assert [t.id for t in config.tasks] == ["a"]
        assert config.max_concurrency == 7

    def test_empty_config_does_not_invent_a_task(self):
        assert RegGrabConfig.model_validate({}).tasks == []
        assert RegGrabConfig.model_validate({"enabled": True}).tasks == []

    def test_account_config_loads_a_legacy_file(self):
        """从 AccountConfig 那一层进来也要能迁移（真实加载路径）。"""
        account = AccountConfig.model_validate(
            {"reg_grab": {"enabled": True, "chats": [-5], "delay": 2.0}}
        )
        assert len(account.reg_grab.tasks) == 1
        assert account.reg_grab.tasks[0].delay == 2.0

    def test_migrated_config_is_stable_across_round_trips(self):
        """迁移只发生一次：再存再读不会又多出一条任务。"""
        first = RegGrabConfig.model_validate({"chats": [-1], "delay": 1.0})
        dumped = first.model_dump(mode="json")
        assert "chats" not in dumped, "旧字段不该被再写出去"
        second = RegGrabConfig.model_validate(dumped)
        assert len(second.tasks) == 1


class TestAccountConfig:
    def test_default_is_valid_and_idle(self):
        config = AccountConfig.default()
        assert config.needs_updates is False

    def test_needs_updates_with_rule(self):
        config = AccountConfig.model_validate(
            {
                "forward": {
                    "rules": [
                        {"id": "r", "targets": ["me"], "match": {"mode": "all"}}
                    ]
                }
            }
        )
        assert config.needs_updates is True

    def test_duplicate_rule_ids_rejected(self):
        with pytest.raises(ValidationError):
            AccountConfig.model_validate(
                {
                    "forward": {
                        "rules": [
                            {"id": "same", "targets": ["me"], "match": {"mode": "all"}},
                            {"id": "same", "targets": ["me"], "match": {"mode": "all"}},
                        ]
                    }
                }
            )

    def test_watched_chats_union(self):
        config = AccountConfig.model_validate(
            {
                "forward": {
                    "rules": [
                        {
                            "id": "r",
                            "sources": [-100111],
                            "targets": ["me"],
                            "match": {"mode": "all"},
                        }
                    ]
                },
                "red_packet": {"enabled": True, "chats": [-100222]},
            }
        )
        assert set(config.watched_chats()) == {-100111, -100222}

    def test_watched_chats_empty_means_all(self):
        """任一功能没限定会话，就必须监听全部。"""
        config = AccountConfig.model_validate(
            {
                "forward": {
                    "rules": [
                        {"id": "r", "targets": ["me"], "match": {"mode": "all"}}
                    ]
                },
                "red_packet": {"enabled": True, "chats": [-100222]},
            }
        )
        assert config.watched_chats() == []

    def test_round_trip(self):
        config = AccountConfig.model_validate(
            {
                "forward": {
                    "rules": [
                        {
                            "id": "r1",
                            "sources": ["@src"],
                            "targets": [-100999],
                            "match": {"mode": "regex", "patterns": ["a(b)c"]},
                        }
                    ]
                }
            }
        )
        again = AccountConfig.model_validate(config.model_dump(mode="json"))
        assert again == config


class TestSettings:
    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("TGA_API_ID", "12345")
        monkeypatch.setenv("TGA_API_HASH", "abcdef")
        monkeypatch.setenv("TGA_PROXY", "socks5://127.0.0.1:1080")
        monkeypatch.setenv("TGA_WORKERS", "16")
        settings = Settings.from_env()
        assert settings.api_id == 12345
        assert settings.api_hash == "abcdef"
        assert settings.proxy is not None
        assert settings.proxy.port == 1080
        assert settings.workers == 16

    def test_overrides_win(self, monkeypatch):
        monkeypatch.setenv("TGA_API_ID", "1")
        settings = Settings.from_env(api_id=999)
        assert settings.api_id == 999

    def test_none_override_falls_back_to_env(self, monkeypatch):
        monkeypatch.setenv("TGA_LOG_LEVEL", "DEBUG")
        assert Settings.from_env(log_level=None).log_level == "DEBUG"


def test_mask_phone():
    assert mask_phone("+8613800138000") == "86****8000"
    assert mask_phone(None) is None
    assert mask_phone("123") == "***"
