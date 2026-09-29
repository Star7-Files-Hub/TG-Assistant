"""数据大盘：按**北京时间的自然日**统计三个「成功」次数。

用户原话：「在仪表盘加一个数据大盘，记录总转发次数，总抢包次数，总抢注次数，
只记录成功的，需要可选天，月，总，天，按北京时间0点开始计算」。

🔴 **为什么不能按进程本地时间分日**：服务完全可能跑在 UTC 上（``TZ`` 没设、
或者设成了 UTC），那样「今天」会在北京时间早上 8 点换日 —— 用户看到的「今天」
整整错 8 小时，而这种错**不会报错**，只会让人对着数发懵。中国没有夏令时，
固定 ``+08:00`` 偏移就够，**不依赖 tzdata**（服务器上不一定装了）。

只记「成功」：转发 = 真的发出去了；抢红包 = 抢到了；抢注 = 注册成功。
失败/跳过都不计 —— 用户要的是「今天干成了多少」，不是「今天试了多少」。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

__all__ = ["MetricsStore", "BEIJING", "KINDS", "RANGES", "beijing_day"]

#: 北京时间 = UTC+8（无夏令时）。
BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")

#: 三个计数项。键名同时是 API / 面板用的名字。
KINDS = ("forward", "red_packet", "reg_grab")

#: 三个可选口径：天 / 月 / 总。
RANGES = ("day", "month", "total")


def beijing_day(when: Optional[float] = None) -> str:
    """``when``（墙上时间戳，默认此刻）是北京的哪一天 —— ``YYYY-MM-DD``。"""
    moment = datetime.fromtimestamp(time.time() if when is None else when, BEIJING)
    return moment.strftime("%Y-%m-%d")


class MetricsStore:
    """按天分桶的计数器。多个账号**共享同一个实例**（用户要的是总数）。

    ⚠️ 落盘格式故意做得很扁（``{"2026-09-29": {"forward": 12, ...}}``）：
    这是个长期积累、偶尔要人工看一眼的文件，可读性比紧凑重要。
    """

    #: 落盘格式版本。以后改结构时据此决定要不要读旧文件。
    VERSION = 1

    def __init__(self, *, state_path: Optional[Any] = None) -> None:
        #: ``{"2026-09-29": {"forward": 12, "red_packet": 3, "reg_grab": 1}}``
        self._days: dict[str, dict[str, int]] = {}
        #: 落盘位置。``None`` = 纯内存（单测 / 离线场景）。
        self.state_path = Path(state_path) if state_path is not None else None
        #: 启动时从磁盘恢复了多少天（诊断用）。
        self.restored = 0
        if self.state_path is not None:
            self._load()

    # ------------------------------------------------------------------ #
    # 记
    # ------------------------------------------------------------------ #
    def record(self, kind: str, *, when: Optional[float] = None) -> None:
        """记一次**成功**。``kind`` 不在 :data:`KINDS` 里就忽略（不抛异常）。

        每次记完**立刻落盘**：大盘的意义就是「历史」，进程被 kill / 断电丢掉几天的
        数据就没意义了。转发量级是每天几十次，同步写完全无所谓。
        """
        if kind not in KINDS:
            return
        day = beijing_day(when)
        bucket = self._days.setdefault(day, {})
        bucket[kind] = int(bucket.get(kind, 0)) + 1
        self._save()

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #
    def totals(self, *, now: Optional[float] = None) -> dict[str, dict[str, int]]:
        """三个口径各是多少 —— ``{"day": {...}, "month": {...}, "total": {...}}``。

        一次把三个都算出来：面板要能来回切天/月/总，一次拿全就不用每切一次再请求一遍，
        而且**三个数必然来自同一时刻**，不会出现"切到月、数字比总还大"这种自相矛盾。
        """
        today = beijing_day(now)
        month = today[:7]  # ``YYYY-MM``
        out: dict[str, dict[str, int]] = {
            name: {kind: 0 for kind in KINDS} for name in RANGES
        }
        for day, bucket in self._days.items():
            for kind in KINDS:
                value = int(bucket.get(kind, 0) or 0)
                if not value:
                    continue
                if day == today:
                    out["day"][kind] += value
                if day.startswith(month):
                    out["month"][kind] += value
                out["total"][kind] += value
        return out

    @property
    def days(self) -> int:
        """有记录的天数（诊断用）。"""
        return len(self._days)

    def snapshot(self) -> dict[str, Any]:
        """诊断用的一小块摘要（进心跳 / 状态接口）。"""
        return {
            "days": len(self._days),
            "today": beijing_day(),
            "restored": self.restored,
            "totals": self.totals(),
        }

    # ------------------------------------------------------------------ #
    # 落盘
    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        """读回磁盘上的记录。任何异常都只当"没有记录"，绝不阻塞启动。"""
        assert self.state_path is not None
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception:
            # 文件被写坏（断电 / 手工编辑）时**不能让服务起不来**：
            # 最坏只是大盘从今天重新开始数，比整个服务拒绝启动轻得多。
            return
        if not isinstance(raw, dict):
            return
        days = raw.get("days")
        if not isinstance(days, dict):
            return
        for day, bucket in days.items():
            if not isinstance(bucket, dict):
                continue
            cleaned: dict[str, int] = {}
            for kind in KINDS:
                try:
                    value = int(bucket.get(kind, 0) or 0)
                except (TypeError, ValueError):
                    continue
                if value:
                    cleaned[kind] = value
            if cleaned:
                self._days[str(day)] = cleaned
        self.restored = len(self._days)

    def _save(self) -> None:
        """原子写回。失败只吞掉 —— 转发/抢包永远不能被大盘拖垮。"""
        if self.state_path is None:
            return
        try:
            payload = {"version": self.VERSION, "days": self._days}
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(self.state_path.name + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(tmp, self.state_path)
        except Exception:
            return
