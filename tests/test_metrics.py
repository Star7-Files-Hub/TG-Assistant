"""数据大盘：按**北京时间自然日**分桶的成功计数。

用户原话：「在仪表盘加一个数据大盘，记录总转发次数，总抢包次数，总抢注次数，
只记录成功的，需要可选天，月，总，天，按北京时间0点开始计算」。
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import pytest

from tg_assistant.metrics import BEIJING, KINDS, MetricsStore, beijing_day


def utc(*args) -> float:
    """按 UTC 造一个时间戳（用来钉死「北京时间的哪一天」）。"""
    return datetime(*args, tzinfo=timezone.utc).timestamp()


#: 北京时间 2026-09-29 23:59 —— 还是 29 号。
BEIJING_29_LATE = utc(2026, 9, 29, 15, 59)
#: 北京时间 2026-09-30 00:01 —— 已经是 30 号了（两者只差 2 分钟）。
BEIJING_30_EARLY = utc(2026, 9, 29, 16, 1)


class TestBeijingDay:
    def test_offset_is_eight_hours(self):
        assert BEIJING.utcoffset(None).total_seconds() == 8 * 3600

    def test_day_boundary_is_beijing_midnight(self):
        """🔴 本文件最关键的一条。

        这两个时间戳只差 2 分钟，但**跨了北京时间的 0 点** ⇒ 必须落在两天。
        服务跑在 UTC 上时，用本地时间分日的实现会把它们算成同一天。
        """
        assert beijing_day(BEIJING_29_LATE) == "2026-09-29"
        assert beijing_day(BEIJING_30_EARLY) == "2026-09-30"

    def test_independent_of_process_timezone(self, monkeypatch):
        """把进程时区改成 UTC（线上服务器的真实情况）也得是北京时间那一天。"""
        original = os.environ.get("TZ")
        try:
            monkeypatch.setenv("TZ", "UTC")
            time.tzset()
            assert beijing_day(BEIJING_29_LATE) == "2026-09-29"
            assert beijing_day(BEIJING_30_EARLY) == "2026-09-30"
        finally:
            if original is None:
                monkeypatch.delenv("TZ", raising=False)
            else:
                monkeypatch.setenv("TZ", original)
            time.tzset()


class TestRecordAndTotals:
    def test_only_success_is_recorded_by_callers(self):
        """大盘本身不判成功与否，调用方只在这三种情况下调 —— 这里钉住键名。"""
        store = MetricsStore()
        for kind in KINDS:
            store.record(kind, when=BEIJING_29_LATE)
        assert store.totals(now=BEIJING_29_LATE)["day"] == {
            "forward": 1,
            "red_packet": 1,
            "reg_grab": 1,
        }

    def test_unknown_kind_is_ignored(self):
        store = MetricsStore()
        store.record("failed", when=BEIJING_29_LATE)
        store.record("", when=BEIJING_29_LATE)
        assert store.totals(now=BEIJING_29_LATE)["total"] == {
            "forward": 0,
            "red_packet": 0,
            "reg_grab": 0,
        }

    def test_day_month_total_are_three_windows(self):
        store = MetricsStore()
        # 上个月：只进「总计」
        store.record("forward", when=utc(2026, 8, 15, 4, 0))
        # 本月的另一天：进「本月」和「总计」
        store.record("forward", when=utc(2026, 9, 10, 4, 0))
        # 今天：三个口径都进
        store.record("forward", when=BEIJING_29_LATE)
        store.record("forward", when=BEIJING_29_LATE)

        totals = store.totals(now=BEIJING_29_LATE)
        assert totals["day"]["forward"] == 2
        assert totals["month"]["forward"] == 3
        assert totals["total"]["forward"] == 4

    def test_yesterday_is_not_today(self):
        store = MetricsStore()
        store.record("red_packet", when=utc(2026, 9, 29, 15, 59))  # 北京 29 号 23:59
        totals = store.totals(now=utc(2026, 9, 29, 16, 1))  # 北京 30 号 00:01
        assert totals["day"]["red_packet"] == 0, "过了北京时间 0 点，昨天的不算今天"
        assert totals["month"]["red_packet"] == 1
        assert totals["total"]["red_packet"] == 1

    def test_kinds_are_counted_separately(self):
        store = MetricsStore()
        for _ in range(3):
            store.record("red_packet", when=BEIJING_29_LATE)
        store.record("reg_grab", when=BEIJING_29_LATE)
        day = store.totals(now=BEIJING_29_LATE)["day"]
        assert (day["forward"], day["red_packet"], day["reg_grab"]) == (0, 3, 1)


class TestPersistence:
    def test_survives_a_restart(self, tmp_path):
        """进程重启（面板点一次「启动」也会重建）后历史必须还在 —— 否则「总计」归零。"""
        path = tmp_path / "metrics.json"
        first = MetricsStore(state_path=path)
        first.record("forward", when=BEIJING_29_LATE)
        first.record("forward", when=BEIJING_29_LATE)
        first.record("reg_grab", when=BEIJING_29_LATE)

        reborn = MetricsStore(state_path=path)
        assert reborn.restored == 1, "只恢复了一天"
        assert reborn.totals(now=BEIJING_29_LATE)["total"] == {
            "forward": 2,
            "red_packet": 0,
            "reg_grab": 1,
        }

    def test_file_is_human_readable(self, tmp_path):
        """这个文件是给人看的（偶尔要手工确认），键名 / 日期都得直白。"""
        import json

        path = tmp_path / "metrics.json"
        store = MetricsStore(state_path=path)
        store.record("forward", when=BEIJING_29_LATE)
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw == {"version": 1, "days": {"2026-09-29": {"forward": 1}}}

    def test_missing_file_is_not_an_error(self, tmp_path):
        store = MetricsStore(state_path=tmp_path / "nope.json")
        assert store.totals()["total"]["forward"] == 0

    def test_broken_file_does_not_raise(self, tmp_path):
        """文件被写坏时**不能让服务起不来** —— 最坏只是大盘重新开始数。"""
        path = tmp_path / "metrics.json"
        path.write_text("{ 这不是 JSON", encoding="utf-8")
        store = MetricsStore(state_path=path)
        assert store.restored == 0
        store.record("forward")
        assert store.totals()["total"]["forward"] == 1

    @pytest.mark.parametrize(
        "payload",
        [
            "[]",
            '{"version":1}',
            '{"version":1,"days":[]}',
            '{"version":1,"days":{"2026-09-29":"bad"}}',
            '{"version":1,"days":{"2026-09-29":{"forward":"x"}}}',
        ],
    )
    def test_shapes_that_must_not_crash(self, tmp_path, payload):
        path = tmp_path / "metrics.json"
        path.write_text(payload, encoding="utf-8")
        store = MetricsStore(state_path=path)
        assert store.totals()["total"]["forward"] == 0

    def test_memory_only_store_never_touches_disk(self, tmp_path):
        store = MetricsStore()
        assert store.state_path is None
        store.record("forward")
        assert store.totals()["total"]["forward"] == 1
        assert store.snapshot()["days"] == 1

    def test_write_failure_is_swallowed(self, tmp_path):
        """落盘失败只吞掉 —— 转发/抢包永远不能被大盘拖垮。"""
        path = tmp_path / "metrics.json"
        # 同名目录 ⇒ 读是 IsADirectoryError、写是 os.replace 失败，两次异常都得被吞掉。
        path.mkdir()
        store = MetricsStore(state_path=path)
        assert store.restored == 0
        store.record("forward")
        assert store.totals()["total"]["forward"] == 1, "内存里的计数照常，只是没落盘"
