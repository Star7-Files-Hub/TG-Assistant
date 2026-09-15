"""三网分流（移动/电信/联通各写一条 A 记录）的测试。

背景：源频道 ``@cfyxip`` 是**一条消息只讲一个运营商**的格式，首行标注
``✅ Cloudflare 优选IP更新 (移动)``。于是：

* 抓取必须把 ``fetch_limit`` 条消息**全部**看一遍再聚合（原来的实现
  遇到第一条含 IP 的消息就 return，永远只能拿到最近发过的那一家）；
* 写入必须在同一个 ``domain``/``name`` 下写**三条** A 记录，靠 Cloudflare
  记录的 ``comment`` 区分（用户可能在后台删掉重建，靠顺序会错位）。

下面钉住这两点，以及「某家没抓到就跳过、绝不拿别家 IP 顶替」这条硬约束。
"""

from __future__ import annotations

import httpx
import pytest

from tg_assistant.cloudflare_ip import (
    ISP_COMMENT_PREFIX,
    ISP_LABELS,
    LAST_IP_BY_ISP_KEY,
    LAST_SPEED_BY_ISP_KEY,
    DNSUpdateResult,
    IPFetchResult,
    ISPBest,
    UpdateSummary,
    _plan_targets,
    _resolve_record,
    aggregate_messages,
    parse_isp,
    should_update_isp,
    threshold_for,
)
from tg_assistant.config import CloudflareDNSRecord, CloudflareIPConfig


def _message(isp: str, best_ip: str, speed: float, second_ip: str, second_speed: float) -> str:
    """造一条形如源频道的单运营商消息。"""
    return f"""✅ Cloudflare 优选IP更新 ({ISP_LABELS[isp]})

⚡️ 最快：{best_ip} {speed} MB/s (新加坡)
☁️ 最慢：{second_ip} 0.03 MB/s (新加坡)

🕙 更新时间：2026-09-15 16:19

{best_ip}    █████ {speed} MB/s  新加坡
{second_ip}    █     0.03 MB/s  新加坡
"""


MOBILE_TEXT = _message("mobile", "104.16.11.144", 27.96, "104.16.12.1", 1.0)
TELECOM_TEXT = _message("telecom", "172.64.147.52", 166.0, "104.18.35.237", 95.45)
UNICOM_TEXT = _message("unicom", "104.18.32.75", 80.85, "104.18.33.152", 2.01)


class TestParseIsp:
    """运营商标注只认首行。"""

    @pytest.mark.parametrize(
        ("label", "expected"),
        [("移动", "mobile"), ("电信", "telecom"), ("联通", "unicom")],
    )
    def test_fullwidth_parentheses(self, label, expected):
        assert parse_isp(f"✅ Cloudflare 优选IP更新 （{label}）\n正文") == expected

    def test_halfwidth_parentheses(self):
        assert parse_isp("✅ Cloudflare 优选IP更新 (电信)") == "telecom"

    def test_spaces_inside_parentheses(self):
        assert parse_isp("✅ Cloudflare 优选IP更新 ( 移动 )") == "mobile"

    def test_empty_text(self):
        assert parse_isp("") is None

    def test_no_label(self):
        assert parse_isp("✅ Cloudflare 优选IP更新\n⚡️ 最快：1.2.3.4 10 MB/s") is None

    def test_only_first_line_counts(self):
        """正文里出现「移动」不能当成标注。

        这正是「只看首行」的原因：正文是 IP 列表 + 城市名，扫全文很容易误伤。
        """
        text = "✅ Cloudflare 优选IP更新\n移动用户请用 1.2.3.4\n电信用户请用 5.6.7.8"
        assert parse_isp(text) is None

    def test_first_line_still_wins(self):
        text = "✅ Cloudflare 优选IP更新 (联通)\n移动的 IP 是 1.2.3.4"
        assert parse_isp(text) == "unicom"


class TestAggregateMessages:
    """一条消息一个运营商时，三家都要被认出来。"""

    def test_picks_each_isp_from_its_own_message(self):
        result = aggregate_messages([UNICOM_TEXT, TELECOM_TEXT, MOBILE_TEXT])

        assert set(result.best_by_isp) == {"mobile", "telecom", "unicom"}
        assert result.best_by_isp["mobile"].ip == "104.16.11.144"
        assert result.best_by_isp["telecom"].ip == "172.64.147.52"
        assert result.best_by_isp["unicom"].ip == "104.18.32.75"

    def test_speeds_are_per_isp(self):
        result = aggregate_messages([UNICOM_TEXT, TELECOM_TEXT, MOBILE_TEXT])
        assert result.best_by_isp["mobile"].speed == pytest.approx(27.96)
        assert result.best_by_isp["telecom"].speed == pytest.approx(166.0)
        assert result.best_by_isp["unicom"].speed == pytest.approx(80.85)

    def test_same_isp_takes_the_faster_one(self):
        """同一个运营商发了两条，取速度高的那条（不是最新的那条）。"""
        slow = _message("telecom", "1.1.1.1", 10.0, "2.2.2.2", 1.0)
        fast = _message("telecom", "3.3.3.3", 90.0, "4.4.4.4", 1.0)

        result = aggregate_messages([slow, fast])

        assert result.best_by_isp["telecom"].ip == "3.3.3.3"
        assert result.best_by_isp["telecom"].speed == pytest.approx(90.0)

    def test_unknown_isp_does_not_join_the_split(self):
        """认不出运营商的消息不进 best_by_isp，但它的 IP 仍进候选池。"""
        unknown = "优选 IP 汇总\n⚡️ 最快：9.9.9.9 5 MB/s\n9.9.9.9 █ 5 MB/s 东京"

        result = aggregate_messages([unknown, TELECOM_TEXT])

        assert set(result.best_by_isp) == {"telecom"}
        assert "9.9.9.9" in result.all_ips

    def test_empty_list(self):
        result = aggregate_messages([])
        assert result.fastest is None
        assert result.best_by_isp == {}
        assert not result.has_isp_split

    def test_no_isp_at_all_falls_back_to_newest(self):
        older = "无标注\n⚡️ 最快：1.1.1.1 1 MB/s"
        newer = "无标注\n⚡️ 最快：2.2.2.2 2 MB/s"

        result = aggregate_messages([newer, older])

        assert result.best_by_isp == {}
        assert result.fastest == "2.2.2.2"
        assert result.fastest_speed == pytest.approx(2.0)

    def test_all_speeds_union_keeps_the_max(self):
        """候选池是所有消息的并集，同名 IP 保留见过的最高速度。"""
        a = "无标注\n⚡️ 最快：7.7.7.7 5 MB/s\n7.7.7.7 █ 5 MB/s 东京"
        b = "无标注\n⚡️ 最快：7.7.7.7 9 MB/s\n7.7.7.7 █ 9 MB/s 东京"

        result = aggregate_messages([a, b])

        assert result.all_speeds["7.7.7.7"] == pytest.approx(9.0)

    def test_fastest_is_the_best_across_isps(self):
        """分流模式下 `fastest` 仍要有值 —— 面板「整体最快」在用它。"""
        result = aggregate_messages([MOBILE_TEXT, TELECOM_TEXT, UNICOM_TEXT])
        assert result.fastest == "172.64.147.52"
        assert result.fastest_speed == pytest.approx(166.0)

    def test_raw_text_is_the_newest_message_with_ip(self):
        result = aggregate_messages([UNICOM_TEXT, TELECOM_TEXT])
        assert result.raw_text == UNICOM_TEXT

    def test_messages_without_ip_are_skipped(self):
        result = aggregate_messages(["纯文字公告，没有 IP", TELECOM_TEXT])
        assert set(result.best_by_isp) == {"telecom"}


class TestThresholdFor:
    def test_falls_back_to_global(self):
        config = CloudflareIPConfig(min_speed_threshold=50.0)
        assert threshold_for(config, "mobile") == 50.0

    def test_uses_per_isp_value(self):
        config = CloudflareIPConfig(
            min_speed_threshold=50.0,
            min_speed_threshold_by_isp={"mobile": 20.0},
        )
        assert threshold_for(config, "mobile") == 20.0
        assert threshold_for(config, "telecom") == 50.0

    def test_none_isp_uses_global(self):
        config = CloudflareIPConfig(
            min_speed_threshold=50.0,
            min_speed_threshold_by_isp={"mobile": 20.0},
        )
        assert threshold_for(config, None) == 50.0

    def test_zero_per_isp_disables_the_limit(self):
        """显式配 0 表示「这家不设限」，不能回退到全局阈值。"""
        config = CloudflareIPConfig(
            min_speed_threshold=50.0,
            min_speed_threshold_by_isp={"mobile": 0},
        )
        assert threshold_for(config, "mobile") == 0.0


class TestShouldUpdateIsp:
    def test_threshold_is_per_isp(self):
        """移动最好只有 27 MB/s，不能拿电信的 100 去卡它。"""
        config = CloudflareIPConfig(
            min_speed_threshold=100.0,
            min_speed_threshold_by_isp={"mobile": 20.0},
        )
        best = ISPBest(isp="mobile", ip="104.16.11.144", speed=27.96)

        decision = should_update_isp(best, config, {})

        assert decision.should_update is True

    def test_last_speed_is_per_isp(self):
        """三家各记一份上次速度，互不干扰。"""
        config = CloudflareIPConfig(min_speed_threshold=0, only_update_if_faster=True)
        state = {LAST_SPEED_BY_ISP_KEY: {"mobile": 20.0, "telecom": 200.0}}

        mobile = should_update_isp(
            ISPBest(isp="mobile", ip="1.1.1.1", speed=27.96), config, state
        )
        telecom = should_update_isp(
            ISPBest(isp="telecom", ip="2.2.2.2", speed=166.0), config, state
        )

        assert mobile.should_update is True, "27.96 > 移动上次的 20"
        assert telecom.should_update is False, "166 < 电信上次的 200"

    def test_first_time_for_one_isp_passes_even_if_others_recorded(self):
        config = CloudflareIPConfig(min_speed_threshold=0, only_update_if_faster=True)
        state = {LAST_SPEED_BY_ISP_KEY: {"telecom": 999.0}}

        decision = should_update_isp(
            ISPBest(isp="mobile", ip="1.1.1.1", speed=5.0), config, state
        )

        assert decision.should_update is True, "移动还没有历史记录，属首次更新"

    def test_label_appears_in_reason(self):
        config = CloudflareIPConfig(min_speed_threshold=0)
        decision = should_update_isp(
            ISPBest(isp="unicom", ip="1.1.1.1", speed=5.0), config, {}
        )
        assert "[联通]" in decision.reason

    def test_below_threshold_skipped(self):
        config = CloudflareIPConfig(min_speed_threshold_by_isp={"unicom": 100.0})
        decision = should_update_isp(
            ISPBest(isp="unicom", ip="1.1.1.1", speed=80.85), config, {}
        )
        assert decision.should_update is False
        assert "低于阈值" in decision.reason

    def test_corrupt_state_is_ignored(self):
        """state 里那两份映射可能被手工改坏，别让它把整个循环打挂。"""
        config = CloudflareIPConfig(min_speed_threshold=0, only_update_if_faster=True)
        state = {LAST_SPEED_BY_ISP_KEY: "not-a-dict"}

        decision = should_update_isp(
            ISPBest(isp="mobile", ip="1.1.1.1", speed=5.0), config, state
        )

        assert decision.should_update is True


class TestPlanTargets:
    """每条记录要展开成三家，缺哪家就跳过哪家。"""

    def _config(self, *names: str) -> CloudflareIPConfig:
        return CloudflareIPConfig(
            records=[
                CloudflareDNSRecord(zone_id="z1", domain="yx.example.cc", name=n)
                for n in names
            ]
        )

    def test_split_expands_one_record_into_three(self):
        plan = _plan_targets(
            self._config("@"),
            None,
            {"mobile": "1.1.1.1", "telecom": "2.2.2.2", "unicom": "3.3.3.3"},
        )

        assert len(plan) == 3
        assert [isp for _, isp, _, _, _ in plan] == ["mobile", "telecom", "unicom"]
        assert [ip for _, _, ip, _, _ in plan] == ["1.1.1.1", "2.2.2.2", "3.3.3.3"]
        assert [c for _, _, _, c, _ in plan] == [
            f"{ISP_COMMENT_PREFIX}mobile",
            f"{ISP_COMMENT_PREFIX}telecom",
            f"{ISP_COMMENT_PREFIX}unicom",
        ]
        assert all(reason is None for _, _, _, _, reason in plan)

    def test_missing_isp_is_skipped_not_substituted(self):
        """🔴 硬约束：移动没抓到就跳过，**绝不能**写电信的 IP 进去。

        否则移动的用户会被解析到电信的优选 IP 上 —— 那是完全不同的网络。
        """
        plan = _plan_targets(
            self._config("@"),
            None,
            {"telecom": "2.2.2.2", "unicom": "3.3.3.3"},
        )

        mobile = next(item for item in plan if item[1] == "mobile")
        _, _, target_ip, comment, skip_reason = mobile

        assert target_ip is None, "不能拿别家的 IP 顶替"
        assert skip_reason and "移动" in skip_reason
        assert comment == f"{ISP_COMMENT_PREFIX}mobile"

        others = [item for item in plan if item[1] != "mobile"]
        assert all(item[2] is not None and item[4] is None for item in others)

    def test_no_split_keeps_single_target(self):
        plan = _plan_targets(self._config("@"), "9.9.9.9", None)

        assert len(plan) == 1
        _, isp, target_ip, comment, skip_reason = plan[0]
        assert isp is None
        assert target_ip == "9.9.9.9"
        assert comment is None, "不分流时不去碰用户自己写的备注"
        assert skip_reason is None

    def test_multiple_records_each_get_three(self):
        plan = _plan_targets(
            self._config("@", "www"),
            None,
            {"mobile": "1.1.1.1", "telecom": "2.2.2.2", "unicom": "3.3.3.3"},
        )

        assert len(plan) == 6
        assert {record.name for record, _, _, _, _ in plan} == {"@", "www"}

    def test_empty_ips_by_isp_dict_is_not_split_mode(self):
        """空字典当「不分流」处理 —— 别把 `{}` 当成「三家全跳过」。"""
        plan = _plan_targets(self._config("@"), "9.9.9.9", {})
        assert len(plan) == 1
        assert plan[0][2] == "9.9.9.9"

    def test_duplicate_records_are_deduped(self):
        """线上真实踩到：同一个 yx.7star.eu.cc 挂了两条 A，只有 proxied 不同。

        不去重的话分流时每条记录各展开三家 —— 同一域名下反复创建/覆盖同一条
        记录，最后留下哪条、proxied 取哪个全看顺序。
        """
        config = CloudflareIPConfig(
            records=[
                CloudflareDNSRecord(
                    zone_id="z1", domain="7star.eu.cc", name="yx", proxied=False
                ),
                CloudflareDNSRecord(
                    zone_id="z1", domain="7star.eu.cc", name="yx", proxied=True
                ),
            ]
        )

        plan = _plan_targets(config, None, {"mobile": "1.1.1.1", "telecom": "2.2.2.2"})

        assert len(plan) == 3, "去重后只该剩一条记录 × 三家"
        assert {isp for _, isp, _, _, _ in plan} == {"mobile", "telecom", "unicom"}
        assert plan[0][0].proxied is False, "保留第一条"

    def test_distinct_names_are_not_deduped(self):
        config = CloudflareIPConfig(
            records=[
                CloudflareDNSRecord(zone_id="z1", domain="a.cc", name="yx"),
                CloudflareDNSRecord(zone_id="z1", domain="a.cc", name="www"),
                CloudflareDNSRecord(zone_id="z1", domain="b.cc", name="yx"),
                CloudflareDNSRecord(zone_id="z2", domain="a.cc", name="yx"),
            ]
        )
        plan = _plan_targets(config, "9.9.9.9", None)
        assert len(plan) == 4

    def test_dedup_logs_a_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        config = CloudflareIPConfig(
            records=[
                CloudflareDNSRecord(zone_id="z1", domain="a.cc", name="yx"),
                CloudflareDNSRecord(zone_id="z1", domain="a.cc", name="yx", proxied=False),
            ]
        )
        with caplog.at_level("WARNING"):
            _plan_targets(config, "9.9.9.9", None)

        assert any("重复" in r.message for r in caplog.records)


def _cloudflare_transport(records: list[dict]) -> httpx.MockTransport:
    """桩掉 Cloudflare 的记录查询接口。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": records})

    return httpx.MockTransport(handler)


class TestResolveRecordAdoption:
    """首次开启分流时，认领同域名下那条没有标记的旧记录。"""

    async def _resolve(self, records: list[dict], comment: str | None):
        async with httpx.AsyncClient(transport=_cloudflare_transport(records)) as http:
            return await _resolve_record(
                http, "tok", "zone", "example.cc", "@", "A", comment=comment
            )

    async def test_no_comment_takes_first_record(self):
        result = await self._resolve([{"id": "r1"}, {"id": "r2"}], None)
        assert result == ("r1", False)

    async def test_no_comment_and_no_records(self):
        assert await self._resolve([], None) == (None, False)

    async def test_tagged_record_is_reused(self):
        records = [
            {"id": "other", "comment": f"{ISP_COMMENT_PREFIX}telecom"},
            {"id": "mine", "comment": f"{ISP_COMMENT_PREFIX}mobile"},
        ]
        assert await self._resolve(records, f"{ISP_COMMENT_PREFIX}mobile") == ("mine", False)

    async def test_single_untagged_record_is_adopted(self):
        """用户原来只有一条记录（没有标记）→ 认领它，不留游离记录。"""
        records = [
            {"id": "old", "comment": ""},
            {"id": "mine", "comment": f"{ISP_COMMENT_PREFIX}telecom"},
        ]
        result = await self._resolve(records, f"{ISP_COMMENT_PREFIX}mobile")
        assert result == ("old", True)

    async def test_multiple_untagged_records_are_not_guessed(self):
        """两条无标记记录时情况不明 → 宁可新建，也不去猜该动哪一条。"""
        records = [{"id": "a", "comment": ""}, {"id": "b", "comment": None}]
        assert await self._resolve(records, f"{ISP_COMMENT_PREFIX}mobile") == (None, False)

    async def test_whitespace_only_comment_counts_as_untagged(self):
        records = [{"id": "old", "comment": "   "}]
        assert await self._resolve(records, f"{ISP_COMMENT_PREFIX}mobile") == ("old", True)

    async def test_nothing_matches_creates_new(self):
        assert await self._resolve([], f"{ISP_COMMENT_PREFIX}mobile") == (None, False)


class TestSplitSummaryState:
    """`changed_count` 要把「跳过」排除掉，否则日志会虚报写入条数。"""

    def test_fetch_result_reports_split(self):
        result = aggregate_messages([MOBILE_TEXT, TELECOM_TEXT])
        assert result.has_isp_split is True

    def test_no_split_reports_false(self):
        assert IPFetchResult().has_isp_split is False


# --------------------------------------------------------------------------- #
# Cloudflare API v4 的响应字段名
# --------------------------------------------------------------------------- #

#: Cloudflare 真实响应的形状（注意是 ``success``，不是 ``ok``）。
CF_LIST_RESPONSE = {
    "result": [
        {
            "id": "rec-1",
            "name": "yx.example.cc",
            "type": "A",
            "content": "1.1.1.1",
            "proxiable": True,
            "proxied": False,
            "ttl": 1,
            "comment": None,
            "created_on": "2026-09-15T14:19:41.098209Z",
        }
    ],
    "success": True,
    "errors": [],
    "messages": [],
    "result_info": {"page": 1, "per_page": 100, "count": 1, "total_count": 1},
}


class TestCfOk:
    """🔴 回归：Cloudflare 返回的字段是 ``success``，代码原来读的是 ``ok``。"""

    def test_real_cloudflare_response_is_ok(self):
        from tg_assistant.cloudflare_ip import _cf_ok

        assert _cf_ok(CF_LIST_RESPONSE) is True

    def test_success_false_is_not_ok(self):
        from tg_assistant.cloudflare_ip import _cf_ok

        assert _cf_ok({"success": False, "errors": [{"message": "boom"}]}) is False

    def test_ok_field_still_accepted(self):
        """测试里的桩数据还在用 ``ok``，继续认。"""
        from tg_assistant.cloudflare_ip import _cf_ok

        assert _cf_ok({"ok": True}) is True
        assert _cf_ok({"ok": False}) is False

    def test_success_wins_over_ok(self):
        from tg_assistant.cloudflare_ip import _cf_ok

        assert _cf_ok({"success": False, "ok": True}) is False

    @pytest.mark.parametrize("bad", [None, [], "yes", 0, {}])
    def test_garbage_is_not_ok(self, bad):
        from tg_assistant.cloudflare_ip import _cf_ok

        assert _cf_ok(bad) is False


class TestCloudflareApiSuccessField:
    """端到端：拿真实形状的响应跑一遍查询与写入。"""

    def _transport(self, get_body: dict, write_body: dict, calls: list) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, str(request.url)))
            if request.method == "GET":
                return httpx.Response(200, json=get_body)
            return httpx.Response(200, json=write_body)

        return httpx.MockTransport(handler)

    async def test_resolve_reuses_existing_record(self):
        """回归：原来判失败 → 返回 None → **每次都新建一条记录**。"""
        from tg_assistant.cloudflare_ip import _resolve_record

        calls: list = []
        transport = self._transport(CF_LIST_RESPONSE, {}, calls)
        async with httpx.AsyncClient(transport=transport) as http:
            record_id, adopted = await _resolve_record(
                http, "tok", "zone", "example.cc", "yx", "A"
            )

        assert record_id == "rec-1", "应该复用已有记录，而不是返回 None 去新建"
        assert adopted is False

    async def test_update_reports_success(self):
        """回归：写成功却报「未知错误」（errors 是空数组，连原因都拼不出来）。"""
        from tg_assistant.cloudflare_ip import _update_single_record

        calls: list = []
        transport = self._transport(
            CF_LIST_RESPONSE,
            {"result": {"id": "rec-1"}, "success": True, "errors": []},
            calls,
        )
        async with httpx.AsyncClient(transport=transport) as http:
            result = await _update_single_record(
                http,
                "tok",
                CloudflareDNSRecord(zone_id="zone", domain="example.cc", name="yx"),
                "9.9.9.9",
            )

        assert result.ok is True, f"写成功却报失败: {result.error}"
        assert result.error is None
        assert result.record_id == "rec-1"
        assert any(m == "PUT" for m, _ in calls), "有记录就该 PUT，而不是 POST 新建"

    async def test_update_creates_when_no_record(self):
        from tg_assistant.cloudflare_ip import _update_single_record

        calls: list = []
        transport = self._transport(
            {"result": [], "success": True, "errors": []},
            {"result": {"id": "new-1"}, "success": True, "errors": []},
            calls,
        )
        async with httpx.AsyncClient(transport=transport) as http:
            result = await _update_single_record(
                http,
                "tok",
                CloudflareDNSRecord(zone_id="zone", domain="example.cc", name="yx"),
                "9.9.9.9",
            )

        assert result.ok is True
        assert result.record_id == "new-1"
        assert any(m == "POST" for m, _ in calls), "没有记录才该 POST 新建"

    async def test_update_surfaces_real_error_message(self):
        """真的失败时要把 Cloudflare 给的 message 带出来，别只说「未知错误」。"""
        from tg_assistant.cloudflare_ip import _update_single_record

        calls: list = []
        transport = self._transport(
            CF_LIST_RESPONSE,
            {
                "result": None,
                "success": False,
                "errors": [{"code": 1004, "message": "DNS Validation Error"}],
            },
            calls,
        )
        async with httpx.AsyncClient(transport=transport) as http:
            result = await _update_single_record(
                http,
                "tok",
                CloudflareDNSRecord(zone_id="zone", domain="example.cc", name="yx"),
                "9.9.9.9",
            )

        assert result.ok is False
        assert "DNS Validation Error" in (result.error or "")


class TestIspRows:
    """面板「测试抓取」里的三行预览。"""

    def test_always_three_rows_in_fixed_order(self):
        from tg_assistant.web.routers.api import _isp_rows

        config = CloudflareIPConfig(records=[
            CloudflareDNSRecord(zone_id="z", domain="yx.example.cc")
        ])
        rows = _isp_rows(config, aggregate_messages([TELECOM_TEXT]), {})

        assert [r["isp"] for r in rows] == ["mobile", "telecom", "unicom"]
        assert [r["label"] for r in rows] == ["移动", "电信", "联通"]

    def test_missing_isp_still_gets_a_row(self):
        """某家没抓到也要占位，否则用户会以为界面漏了一行。"""
        from tg_assistant.web.routers.api import _isp_rows

        config = CloudflareIPConfig(records=[
            CloudflareDNSRecord(zone_id="z", domain="yx.example.cc")
        ])
        rows = _isp_rows(config, aggregate_messages([TELECOM_TEXT]), {})

        mobile = next(r for r in rows if r["isp"] == "mobile")
        assert mobile["ip"] is None
        assert mobile["speed"] is None
        assert mobile["should_update"] is False
        assert "移动" in mobile["reason"]

    def test_row_carries_per_isp_threshold(self):
        from tg_assistant.web.routers.api import _isp_rows

        config = CloudflareIPConfig(
            min_speed_threshold=50.0,
            min_speed_threshold_by_isp={"mobile": 20.0},
            records=[CloudflareDNSRecord(zone_id="z", domain="yx.example.cc")],
        )
        rows = _isp_rows(config, aggregate_messages([MOBILE_TEXT, TELECOM_TEXT]), {})
        by_isp = {r["isp"]: r for r in rows}

        assert by_isp["mobile"]["threshold"] == 20.0
        assert by_isp["telecom"]["threshold"] == 50.0

    def test_row_reflects_per_isp_last_speed(self):
        from tg_assistant.web.routers.api import _isp_rows

        config = CloudflareIPConfig(min_speed_threshold=0, only_update_if_faster=True)
        state = {LAST_SPEED_BY_ISP_KEY: {"telecom": 999.0}}
        rows = _isp_rows(config, aggregate_messages([TELECOM_TEXT]), state)

        telecom = next(r for r in rows if r["isp"] == "telecom")
        assert telecom["should_update"] is False
        assert "未超过当前" in telecom["reason"]


class TestUpdateSplitByIsp:
    """一次分流更新的整体行为（写入结果 → state 记账）。"""

    def _config(self, **kwargs: object) -> CloudflareIPConfig:
        base: dict[str, object] = {
            "min_speed_threshold": 0,
            "only_update_if_faster": True,
            "records": [CloudflareDNSRecord(zone_id="z", domain="yx.example.cc")],
        }
        base.update(kwargs)
        return CloudflareIPConfig(**base)  # type: ignore[arg-type]

    async def test_all_three_written_and_recorded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import tg_assistant.cloudflare_ip as cf

        written: dict[str, str] = {}

        async def fake_update(config, ip, proxy=None, *, ips_by_isp=None):
            written.update(ips_by_isp or {})
            return [
                DNSUpdateResult(
                    domain="yx.example.cc",
                    name="@",
                    record_type="A",
                    ip=target,
                    ok=True,
                    isp=isp,
                )
                for isp, target in (ips_by_isp or {}).items()
            ]

        monkeypatch.setattr(cf, "update_dns_records", fake_update)

        fetched = aggregate_messages([MOBILE_TEXT, TELECOM_TEXT, UNICOM_TEXT])
        state: dict[str, object] = {}
        summary = UpdateSummary(fetched=fetched)

        await cf._update_split_by_isp(self._config(), fetched, state, None, summary)

        assert written == {
            "mobile": "104.16.11.144",
            "telecom": "172.64.147.52",
            "unicom": "104.18.32.75",
        }
        assert state[LAST_SPEED_BY_ISP_KEY] == {
            "mobile": pytest.approx(27.96),
            "telecom": pytest.approx(166.0),
            "unicom": pytest.approx(80.85),
        }
        assert state[LAST_IP_BY_ISP_KEY]["mobile"] == "104.16.11.144"
        assert summary.changed_count == 3

    async def test_failed_isp_is_not_recorded_in_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 写失败的那家不能记进 state。

        否则下一轮 ``only_update_if_faster`` 会拿这个「假的上次速度」把它挡住，
        这家就再也更新不了了。
        """
        import tg_assistant.cloudflare_ip as cf

        async def fake_update(config, ip, proxy=None, *, ips_by_isp=None):
            return [
                DNSUpdateResult(
                    domain="yx.example.cc",
                    name="@",
                    record_type="A",
                    ip=target,
                    ok=(isp != "mobile"),  # 移动写失败
                    isp=isp,
                    error=None if isp != "mobile" else "API 报错",
                )
                for isp, target in (ips_by_isp or {}).items()
            ]

        monkeypatch.setattr(cf, "update_dns_records", fake_update)

        fetched = aggregate_messages([MOBILE_TEXT, TELECOM_TEXT])
        state: dict[str, object] = {}
        summary = UpdateSummary(fetched=fetched)

        await cf._update_split_by_isp(self._config(), fetched, state, None, summary)

        speed_map = state[LAST_SPEED_BY_ISP_KEY]
        assert isinstance(speed_map, dict)
        assert "mobile" not in speed_map, "写失败的移动不该被记账"
        assert "telecom" in speed_map

    async def test_all_skipped_never_touches_dns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import tg_assistant.cloudflare_ip as cf

        async def must_not_be_called(*args, **kwargs):
            raise AssertionError("全被跳过时不该发起任何写入")

        monkeypatch.setattr(cf, "update_dns_records", must_not_be_called)

        config = self._config(min_speed_threshold_by_isp={"mobile": 999.0,
                                                          "telecom": 999.0,
                                                          "unicom": 999.0})
        fetched = aggregate_messages([MOBILE_TEXT, TELECOM_TEXT, UNICOM_TEXT])
        summary = UpdateSummary(fetched=fetched)

        await cf._update_split_by_isp(config, fetched, {}, None, summary)

        assert summary.results == []
        assert summary.skipped is True
        assert summary.skipped_reason
        assert len(summary.decisions) == 3

    async def test_missing_isp_reason_is_per_isp(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """只有电信的消息 → 移动/联通各自记「没抓到」，电信照常更新。"""
        import tg_assistant.cloudflare_ip as cf

        async def fake_update(config, ip, proxy=None, *, ips_by_isp=None):
            return [
                DNSUpdateResult(
                    domain="yx.example.cc",
                    name="@",
                    record_type="A",
                    ip=target,
                    ok=True,
                    isp=isp,
                )
                for isp, target in (ips_by_isp or {}).items()
            ]

        monkeypatch.setattr(cf, "update_dns_records", fake_update)

        fetched = aggregate_messages([TELECOM_TEXT])
        summary = UpdateSummary(fetched=fetched)

        await cf._update_split_by_isp(self._config(), fetched, {}, None, summary)

        assert summary.decisions["telecom"].should_update is True
        assert summary.decisions["mobile"].should_update is False
        assert "没有这个运营商的 IP" in summary.decisions["mobile"].reason
        assert summary.changed_count == 1
