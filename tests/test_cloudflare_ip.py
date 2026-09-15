"""Cloudflare 优选 IP 自动更新模块测试。"""

from __future__ import annotations

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
