"""Cloudflare 优选 IP 自动更新模块测试。"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from tg_assistant.cloudflare_ip import (
    DNSUpdateResult,
    IPFetchResult,
    UpdateSummary,
    _is_valid_ipv4,
    parse_ips_from_text,
    should_update,
)
from tg_assistant.config import (
    AccountConfig,
    CloudflareDNSRecord,
    CloudflareIPConfig,
)

SAMPLE_CHANNEL_MESSAGE = """✅ Cloudflare 优选IP更新 (电信)

⚡️ 最快：172.64.147.52 111.14 MB/s (新加坡)
☁️ 最慢：104.18.44.223 0.03 MB/s (新加坡)

🕙 更新时间：2026-09-15 16:19

172.64.147.52    █████ 111.14 MB/s  新加坡
104.18.35.237    ████  95.45 MB/s  新加坡
104.18.37.74     ████  84.93 MB/s  新加坡
104.18.38.98     ████  82.97 MB/s  新加坡
172.64.155.252   ████  79.91 MB/s  新加坡
104.18.32.75     ███   70.13 MB/s  新加坡
172.64.150.135   █     2.37 MB/s  新加坡
104.18.33.152    █     2.01 MB/s  新加坡
172.64.159.58    █     0.82 MB/s  新加坡
104.18.44.223    █     0.03 MB/s  新加坡
"""


class TestIsValidIPv4:
    @pytest.mark.parametrize(
        ("ip", "expected"),
        [
            ("192.168.1.1", True),
            ("10.0.0.1", True),
            ("255.255.255.255", True),
            ("0.0.0.0", True),
            ("172.64.147.52", True),
            ("256.1.1.1", False),
            ("1.2.3", False),
            ("1.2.3.4.5", False),
            ("abc.def.ghi.jkl", False),
            ("", False),
        ],
    )
    def test_validation(self, ip, expected):
        assert _is_valid_ipv4(ip) is expected


class TestParseIPsFromText:
    def test_extracts_fastest_ip(self):
        result = parse_ips_from_text(SAMPLE_CHANNEL_MESSAGE)
        assert result.fastest == "172.64.147.52"

    def test_extracts_fastest_speed(self):
        result = parse_ips_from_text(SAMPLE_CHANNEL_MESSAGE)
        assert result.fastest_speed == pytest.approx(111.14)

    def test_extracts_all_ips(self):
        result = parse_ips_from_text(SAMPLE_CHANNEL_MESSAGE)
        assert len(result.all_ips) == 10
        assert result.all_ips[0] == "172.64.147.52"

    def test_empty_text(self):
        result = parse_ips_from_text("")
        assert result.fastest is None
        assert result.all_ips == []
        assert not result.has_ip

    def test_no_fastest_marker_falls_back_to_first_ip(self):
        text = "Some IPs:\n10.0.0.1\n10.0.0.2\n10.0.0.3"
        result = parse_ips_from_text(text)
        assert result.fastest == "10.0.0.1"
        assert result.all_ips == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]

    def test_no_ips_at_all(self):
        result = parse_ips_from_text("Hello world, no IPs here!")
        assert result.fastest is None
        assert result.all_ips == []
        assert not result.has_ip

    def test_raw_text_preserved(self):
        result = parse_ips_from_text(SAMPLE_CHANNEL_MESSAGE)
        assert result.raw_text == SAMPLE_CHANNEL_MESSAGE

    def test_speeds_dict_populated(self):
        result = parse_ips_from_text(SAMPLE_CHANNEL_MESSAGE)
        assert "172.64.147.52" in result.all_speeds
        assert result.all_speeds["172.64.147.52"] == pytest.approx(111.14)


class TestShouldUpdate:
    def test_first_update_always_passes(self):
        config = CloudflareIPConfig(min_speed_threshold=0, only_update_if_faster=True)
        fetched = IPFetchResult(fastest="1.2.3.4", fastest_speed=50.0)
        decision = should_update(fetched, config, {})
        assert decision.should_update is True
        assert "首次更新" in decision.reason

    def test_below_threshold_skipped(self):
        config = CloudflareIPConfig(min_speed_threshold=10.0)
        fetched = IPFetchResult(fastest="1.2.3.4", fastest_speed=5.0)
        decision = should_update(fetched, config, {})
        assert decision.should_update is False
        assert "低于阈值" in decision.reason

    def test_above_threshold_passes(self):
        config = CloudflareIPConfig(min_speed_threshold=10.0)
        fetched = IPFetchResult(fastest="1.2.3.4", fastest_speed=50.0)
        decision = should_update(fetched, config, {})
        assert decision.should_update is True

    def test_not_faster_skipped(self):
        config = CloudflareIPConfig(only_update_if_faster=True)
        fetched = IPFetchResult(fastest="1.2.3.4", fastest_speed=50.0)
        state = {"cloudflare_ip_last_speed": 100.0}
        decision = should_update(fetched, config, state)
        assert decision.should_update is False
        assert "未超过当前" in decision.reason

    def test_faster_passes(self):
        config = CloudflareIPConfig(only_update_if_faster=True)
        fetched = IPFetchResult(fastest="1.2.3.4", fastest_speed=100.0)
        state = {"cloudflare_ip_last_speed": 50.0}
        decision = should_update(fetched, config, state)
        assert decision.should_update is True

    def test_equal_speed_skipped(self):
        """速度相等时不算更快，应跳过。"""
        config = CloudflareIPConfig(only_update_if_faster=True)
        fetched = IPFetchResult(fastest="1.2.3.4", fastest_speed=50.0)
        state = {"cloudflare_ip_last_speed": 50.0}
        decision = should_update(fetched, config, state)
        assert decision.should_update is False

    def test_no_comparison_when_disabled(self):
        config = CloudflareIPConfig(only_update_if_faster=False)
        fetched = IPFetchResult(fastest="1.2.3.4", fastest_speed=10.0)
        state = {"cloudflare_ip_last_speed": 100.0}
        decision = should_update(fetched, config, state)
        assert decision.should_update is True

    def test_no_ip_skipped(self):
        config = CloudflareIPConfig()
        fetched = IPFetchResult()
        decision = should_update(fetched, config, {})
        assert decision.should_update is False
        assert "未解析到" in decision.reason


class TestCloudflareIPConfig:
    def test_default_disabled(self):
        config = CloudflareIPConfig()
        assert config.enabled is False
        assert config.api_token is None
        assert config.records == []
        assert config.min_speed_threshold == 0
        assert config.only_update_if_faster is True
        assert config.real_time_listen is True

    def test_enabled_requires_token(self):
        with pytest.raises(ValidationError, match="api_token"):
            CloudflareIPConfig(enabled=True, source_channel="@test", records=[
                CloudflareDNSRecord(zone_id="abc", domain="example.com")
            ])

    def test_enabled_requires_source_channel(self):
        with pytest.raises(ValidationError, match="source_channel"):
            CloudflareIPConfig(enabled=True, api_token="tok", records=[
                CloudflareDNSRecord(zone_id="abc", domain="example.com")
            ])

    def test_enabled_requires_records(self):
        with pytest.raises(ValidationError, match="records"):
            CloudflareIPConfig(enabled=True, api_token="tok", source_channel="@test")

    def test_valid_config(self):
        config = CloudflareIPConfig(
            enabled=True,
            api_token="A1b2C3d4E5f6G7h8I9j0",
            source_channel="@CFIP",
            records=[
                CloudflareDNSRecord(zone_id="zone123", domain="example.com", name="www"),
            ],
            interval_hours=6,
            min_speed_threshold=10.0,
            only_update_if_faster=True,
            real_time_listen=True,
        )
        assert config.enabled is True
        assert config.min_speed_threshold == 10.0

    def test_env_expansion(self):
        import os
        os.environ["TEST_CF_TOKEN"] = "my-secret-token"
        try:
            config = CloudflareIPConfig(api_token="${TEST_CF_TOKEN}")
            assert config.api_token == "my-secret-token"
        finally:
            del os.environ["TEST_CF_TOKEN"]

    def test_unexpanded_env_raises(self):
        import os
        if "NONEXISTENT_CF_TOKEN_XYZ" in os.environ:
            del os.environ["NONEXISTENT_CF_TOKEN_XYZ"]
        with pytest.raises(ValidationError, match="没有被替换"):
            CloudflareIPConfig(
                enabled=True,
                api_token="${NONEXISTENT_CF_TOKEN_XYZ}",
                source_channel="@test",
                records=[CloudflareDNSRecord(zone_id="z", domain="d.com")],
            )


class TestCloudflareDNSRecord:
    def test_defaults(self):
        record = CloudflareDNSRecord(zone_id="abc", domain="example.com")
        assert record.name == "@"
        assert record.record_type == "A"
        assert record.proxied is True
        assert record.ttl == 1


class TestSplitByIspConfig:
    """三网分流相关字段的默认值与校验。"""

    def test_defaults_are_off(self):
        config = CloudflareIPConfig()
        assert config.split_by_isp is False
        assert config.min_speed_threshold_by_isp == {}

    def test_fetch_limit_defaults_to_20(self):
        """源频道一条消息只讲一个运营商，5 条很容易凑不齐三家。"""
        assert CloudflareIPConfig().fetch_limit == 20

    def test_accepts_known_isps(self):
        config = CloudflareIPConfig(
            min_speed_threshold_by_isp={"mobile": 20, "telecom": 100.0, "unicom": 60}
        )
        assert config.min_speed_threshold_by_isp == {
            "mobile": 20.0,
            "telecom": 100.0,
            "unicom": 60.0,
        }

    def test_none_becomes_empty(self):
        config = CloudflareIPConfig(min_speed_threshold_by_isp=None)
        assert config.min_speed_threshold_by_isp == {}

    def test_rejects_unknown_isp_key(self):
        """写错键名要当场报错，别静默忽略 —— 否则用户以为配上了。"""
        with pytest.raises(ValidationError, match="不是运营商标识"):
            CloudflareIPConfig(min_speed_threshold_by_isp={"ct": 50.0})

    def test_rejects_non_numeric_value(self):
        with pytest.raises(ValidationError, match="不是数字"):
            CloudflareIPConfig(min_speed_threshold_by_isp={"mobile": "快"})

    def test_rejects_negative_value(self):
        with pytest.raises(ValidationError, match="不能为负数"):
            CloudflareIPConfig(min_speed_threshold_by_isp={"mobile": -1})

    def test_rejects_non_dict(self):
        with pytest.raises(ValidationError, match="必须是"):
            CloudflareIPConfig(min_speed_threshold_by_isp=["mobile"])

    def test_roundtrip_preserves_split(self):
        original = CloudflareIPConfig(
            enabled=True,
            api_token="tok",
            source_channel="@cfyxip",
            records=[CloudflareDNSRecord(zone_id="z", domain="yx.example.cc")],
            split_by_isp=True,
            min_speed_threshold_by_isp={"mobile": 20.0, "telecom": 100.0},
        )
        restored = CloudflareIPConfig.model_validate(original.model_dump(mode="json"))

        assert restored.split_by_isp is True
        assert restored.min_speed_threshold_by_isp == {"mobile": 20.0, "telecom": 100.0}


class TestAccountConfigIntegration:
    def test_default_has_empty_cloudflare_ip(self):
        config = AccountConfig.default()
        assert config.cloudflare_ip.enabled is False

    def test_cloudflare_ip_in_needs_updates(self):
        config = AccountConfig(
            cloudflare_ip=CloudflareIPConfig(
                enabled=True,
                api_token="tok",
                source_channel="@CFIP",
                records=[CloudflareDNSRecord(zone_id="z", domain="example.com")],
            )
        )
        assert config.needs_updates is True

    def test_serialization_roundtrip(self):
        original = CloudflareIPConfig(
            enabled=True,
            api_token="test-token-123",
            source_channel="@CFIP",
            fetch_limit=10,
            interval_hours=6,
            min_speed_threshold=5.0,
            only_update_if_faster=True,
            real_time_listen=False,
            records=[
                CloudflareDNSRecord(zone_id="zone1", domain="a.com", name="@"),
                CloudflareDNSRecord(zone_id="zone2", domain="b.com", name="www", record_type="AAAA"),
            ],
        )
        data = original.model_dump(mode="json")
        restored = CloudflareIPConfig.model_validate(data)
        assert restored.enabled == original.enabled
        assert restored.min_speed_threshold == original.min_speed_threshold
        assert restored.only_update_if_faster == original.only_update_if_faster
        assert restored.real_time_listen == original.real_time_listen
        assert len(restored.records) == 2
        assert restored.records[1].record_type == "AAAA"


class TestNotifyToggleConfig:
    """「更新后发送通知」开关（``CloudflareIPConfig.notify``）。"""

    def test_default_is_on(self):
        """默认开 = 改动前的行为不变。"""
        assert CloudflareIPConfig().notify is True

    def test_old_config_without_the_field_still_notifies(self):
        """🔴 老 config.json 里根本没有 ``notify`` 这个键。

        加载后必须仍是 True —— 否则升级一次就静默地把通知全关了。
        """
        legacy = {
            "enabled": True,
            "api_token": "tok",
            "source_channel": "@cfyxip",
            "records": [{"zone_id": "zone", "domain": "yx.example.cc"}],
            "real_time_listen": True,
        }
        config = CloudflareIPConfig.model_validate(legacy)

        assert "notify" not in legacy
        assert config.notify is True

    def test_explicit_off_is_preserved(self):
        assert CloudflareIPConfig(notify=False).notify is False

    def test_roundtrip_preserves_off(self):
        original = CloudflareIPConfig(notify=False)
        restored = CloudflareIPConfig.model_validate(original.model_dump(mode="json"))

        assert restored.notify is False

    def test_legacy_config_file_without_the_key_loads_as_on(self, store):
        """真·老 ``config.json``：文件里没有 ``notify`` 键，加载后仍必须是 True。"""
        import json

        from tg_assistant.config import AccountRecord, utc_now_iso

        store.upsert_account(AccountRecord(name="acct", created_at=utc_now_iso()))
        store.save_account_config("acct", store.load_account_config("acct", create=True))

        path = store.paths.account("acct").config_file
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["cloudflare_ip"].pop("notify", None)
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        # 先确认文件里真的没有这个键（不然这条测试等于没测老配置）
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert "notify" not in on_disk["cloudflare_ip"]

        reloaded = store.load_account_config("acct", create=False)
        assert reloaded.cloudflare_ip.notify is True


class TestUpdateSummary:
    def test_skipped_property(self):
        s = UpdateSummary(fetched=IPFetchResult(), skipped_reason="too slow")
        assert s.skipped is True

    def test_not_skipped(self):
        s = UpdateSummary(fetched=IPFetchResult(fastest="1.2.3.4"))
        assert s.skipped is False

    def test_all_ok_with_results(self):
        s = UpdateSummary(
            fetched=IPFetchResult(fastest="1.2.3.4"),
            results=[
                DNSUpdateResult(domain="a.com", name="@", record_type="A", ip="1.2.3.4", ok=True),
            ],
        )
        assert s.all_ok is True

    def test_all_ok_false_when_skipped(self):
        s = UpdateSummary(fetched=IPFetchResult(), skipped_reason="test")
        assert s.all_ok is False


# --------------------------------------------------------------------------- #
# 通知开关（notify）：监听器发通知前先看它
# --------------------------------------------------------------------------- #


class _RecordingNotifier:
    """只记下 ``submit`` 进来的通知，不真的发。"""

    def __init__(self) -> None:
        self.tasks: list[Any] = []

    def submit(self, task: Any) -> None:
        self.tasks.append(task)


class _RecordingLog:
    """包一层真的 :class:`AccountLogger`，顺手记下日志文案。

    ⚠️ 不用 ``caplog``：``configure_logging()`` 把 ``tg-assistant`` 的
    ``propagate`` 关成了 ``False``，记录到不了 root handler。
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.messages: list[str] = []

    def bind(self, module: str) -> "_RecordingLog":
        return self

    def _record(self, msg: str, args: tuple[Any, ...]) -> None:
        self.messages.append(msg % args if args else msg)

    def debug(self, msg: str, *args: Any, **fields: Any) -> None:
        self.inner.debug(msg, *args, **fields)

    def info(self, msg: str, *args: Any, **fields: Any) -> None:
        self._record(msg, args)
        self.inner.info(msg, *args, **fields)

    def error(self, msg: str, *args: Any, **fields: Any) -> None:
        self._record(msg, args)
        self.inner.error(msg, *args, **fields)


def _ok_summary() -> UpdateSummary:
    return UpdateSummary(
        fetched=IPFetchResult(fastest="172.64.147.52", fastest_speed=111.14),
        results=[
            DNSUpdateResult(
                domain="example.cc",
                name="yx",
                record_type="A",
                ip="172.64.147.52",
                ok=True,
            )
        ],
    )


def _failed_summary() -> UpdateSummary:
    return UpdateSummary(
        fetched=IPFetchResult(fastest="172.64.147.52", fastest_speed=111.14),
        results=[
            DNSUpdateResult(
                domain="example.cc",
                name="yx",
                record_type="A",
                ip=None,
                ok=False,
                error="boom",
            )
        ],
    )


class TestListenerNotifyToggle:
    """🔴 回归：``notify=False`` 只表示「不发通知」。

    最容易写错的地方是把它当成功能总闸（在 ``_on_message`` 开头就直接 return）
    —— 那样用户只是想安静点，结果 IP 再也不更新了。
    所以这里既要钉住「一条通知都不发」，也要钉住「更新 / 落盘 / 日志照旧」。
    """

    @staticmethod
    def _listener(store, alog, *, notify: bool, notifier=None):
        from tg_assistant.cf_ip_listener import CFIPListener
        from tg_assistant.config import AccountRecord, utc_now_iso

        store.upsert_account(AccountRecord(name="acct", created_at=utc_now_iso()))
        config = store.load_account_config("acct", create=True)
        cf = config.cloudflare_ip
        cf.api_token = "tok"
        cf.source_channel = "-1003372470551"
        cf.records = [CloudflareDNSRecord(zone_id="zone", domain="example.cc", name="yx")]
        # 三网分流：跳过「只拿这一条消息做预检」的分支，直接走完整抓取，
        # 免得预检把要测的那条路径挡掉。
        cf.split_by_isp = True
        cf.enabled = True
        cf.notify = notify
        store.save_account_config("acct", config)

        return CFIPListener(
            "acct",
            config.cloudflare_ip,
            client=object(),
            alog=alog,
            store=store,
            settings=object(),
            notifier=notifier,
        )

    @staticmethod
    def _patch_fetch(monkeypatch, summary: UpdateSummary):
        """把抓取/代理换掉，返回「fetch 被调用了几次」。"""
        import tg_assistant.cf_ip_listener as listener_mod
        import tg_assistant.proxy as proxy_mod

        calls: list[int] = []

        monkeypatch.setattr(listener_mod, "make_message_source", lambda c: object())
        monkeypatch.setattr(proxy_mod, "resolve_proxy", lambda *a, **k: None)

        async def fake_fetch(config, source, state, proxy=None):
            calls.append(1)
            return summary

        monkeypatch.setattr(listener_mod, "fetch_and_update", fake_fetch)
        return calls

    @staticmethod
    def _message():
        import types

        return types.SimpleNamespace(text=SAMPLE_CHANNEL_MESSAGE, id=42)

    async def _trigger(self, listener):
        import types

        await listener._on_message(types.SimpleNamespace(), self._message())

    # --- 关闭：一条都不发 -------------------------------------------------- #
    async def test_off_success_path_submits_nothing(self, store, alog, monkeypatch):
        calls = self._patch_fetch(monkeypatch, _ok_summary())
        notifier = _RecordingNotifier()
        listener = self._listener(store, alog, notify=False, notifier=notifier)

        await self._trigger(listener)

        assert calls, "关了通知也必须照常抓取、更新 DNS"
        assert notifier.tasks == [], "notify=False 时成功路径不该发任何通知"

    async def test_off_failure_path_submits_nothing(self, store, alog, monkeypatch):
        self._patch_fetch(monkeypatch, _failed_summary())
        notifier = _RecordingNotifier()
        log = _RecordingLog(alog)
        listener = self._listener(store, log, notify=False, notifier=notifier)

        await self._trigger(listener)

        assert notifier.tasks == [], "notify=False 时失败路径不该发任何通知"
        assert any("DNS 更新部分失败" in m for m in log.messages), (
            "不发通知不等于不出日志：失败日志必须照旧"
        )
        assert store.load_state("acct").get("cloudflare_ip_last_result") is not None

    async def test_off_keeps_persist_run_and_logs(self, store, alog, monkeypatch):
        """落盘与日志不受通知开关影响 —— 「不发通知」不是「不出日志」。"""
        self._patch_fetch(monkeypatch, _ok_summary())
        notifier = _RecordingNotifier()
        log = _RecordingLog(alog)
        listener = self._listener(store, log, notify=False, notifier=notifier)

        await self._trigger(listener)

        state = store.load_state("acct")
        assert state.get("cloudflare_ip_last_run"), "实时监听必须照常落盘运行时间"
        assert state["cloudflare_ip_last_result"]["ok_count"] == 1
        assert any("DNS 更新成功" in m for m in log.messages), (
            "通知关了也要留下成功日志"
        )

    # --- 打开：照发，文案逐字不变 ------------------------------------------ #
    async def test_on_success_notifies_with_unchanged_text(self, store, alog, monkeypatch):
        self._patch_fetch(monkeypatch, _ok_summary())
        notifier = _RecordingNotifier()
        listener = self._listener(store, alog, notify=True, notifier=notifier)

        await self._trigger(listener)

        assert len(notifier.tasks) == 1
        task = notifier.tasks[0]
        assert task.event == "forward"
        # ⚡ 这条文案**逐字**不变（开关只决定发不发，不改内容）。
        assert task.text == (
            "⚡ Cloudflare IP 已更新\n"
            "<code>172.64.147.52</code>\n"
            "域名: yx.example.cc"
        )

    async def test_on_failure_notifies_with_unchanged_text(self, store, alog, monkeypatch):
        self._patch_fetch(monkeypatch, _failed_summary())
        notifier = _RecordingNotifier()
        listener = self._listener(store, alog, notify=True, notifier=notifier)

        await self._trigger(listener)

        assert len(notifier.tasks) == 1
        task = notifier.tasks[0]
        assert task.event == "error"
        # ⚠️ 这条文案同样逐字不变。
        assert task.text == "⚠️ Cloudflare IP 更新失败\nyx.example.cc: boom"

    async def test_off_without_notifier_is_still_fine(self, store, alog, monkeypatch):
        """没配通知渠道 + 开关打开 = 老行为（静默 return），关掉也一样不炸。"""
        self._patch_fetch(monkeypatch, _ok_summary())
        listener = self._listener(store, alog, notify=True, notifier=None)

        await self._trigger(listener)

        state = store.load_state("acct")
        assert state["cloudflare_ip_last_result"]["ok_count"] == 1
