"""转发「已使用注册码拦截」的**全局配置**读写（``data/forward_used_codes.json``）。

为什么要单独一个模块、而不是留在每个账号的 ``config.json`` 里：这份策略
**不属于任何账号**。2026-09-30 取证 —— 三个账号（小白 / SevenStar / 只想睡觉）的
``forward.used_codes`` 一字不差完全相同，同一份策略被存了三遍，面板上还得一个账号
填一次。用户原话：「将……已使用注册码拦截做成全局，而不是账号级」。

与 :mod:`tg_assistant.forward_excludes` 是同一套做法（同一个容错/原子写取舍）：

* **坏文件 = 缺省策略**，不抛异常。读不到只会让引擎回到"默认开、按默认正则学"，
  不会因为一份写坏的 JSON 让所有账号的转发都起不来（读失败记在 :attr:`load_errors`）。
* 写入则**必须让失败可见**（面板保存失败要报错），否则用户以为存上了、重启后策略
  回到默认，表现就是"拦截又失效了"。

与「全局排除」名单的区别只有一点：那份与账号级取并集，这份是**替换** ——
策略只有一份，账号目录里那份 ``forward.used_codes`` 不再参与（保留只为旧配置能加载）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from .config import ForwardUsedCodes

#: 落盘格式版本。将来改形状时据此迁移，而不是靠猜。
VERSION = 1


class ForwardUsedCodesStore:
    """全局「已用码拦截」配置的落盘读写。

    ``path=None`` = 纯内存（单测 / 离线场景）：:meth:`load` 返回内存里那份，
    :meth:`save` 只更新内存，:meth:`mtime` 恒为 ``None``。
    """

    def __init__(self, path: Optional[Any] = None) -> None:
        self.path: Optional[Path] = Path(path) if path is not None else None
        #: 最近一次读到 / 写过的配置。缺省 = ``ForwardUsedCodes`` 的默认值。
        self.last_loaded: ForwardUsedCodes = ForwardUsedCodes()
        #: 读文件失败的次数（文件坏 / 形状不对）。面板据此提示"配置没读到，用的是默认"。
        self.load_errors = 0

    # ------------------------------------------------------------------ 读
    def load(self) -> ForwardUsedCodes:
        """读回配置。文件不存在 / 坏 / 形状不对都返回**缺省策略**。"""
        if self.path is None:
            return self.last_loaded
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.last_loaded = ForwardUsedCodes()
            return self.last_loaded
        except Exception:
            # 坏 JSON、权限、是目录…… 一律回退缺省（见模块说明的取舍）
            self.load_errors += 1
            self.last_loaded = ForwardUsedCodes()
            return self.last_loaded
        self.last_loaded = self._parse(raw)
        return self.last_loaded

    def _parse(self, raw: Any) -> ForwardUsedCodes:
        if not isinstance(raw, dict):
            self.load_errors += 1
            return ForwardUsedCodes()
        # 落盘时混进去的 ``version`` 不是模型字段，StrictModel 会拒收，先摘掉。
        payload = {k: v for k, v in raw.items() if k != "version"}
        try:
            return ForwardUsedCodes.model_validate(payload)
        except Exception:
            # 形状对不上（例如把某个数字字段写成字符串）同样回退缺省
            self.load_errors += 1
            return ForwardUsedCodes()

    def mtime(self) -> Optional[float]:
        """文件 mtime，供热重载轮询用；读不到返回 ``None``。"""
        if self.path is None:
            return None
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None

    # ------------------------------------------------------------------ 写
    def save(self, config: ForwardUsedCodes) -> None:
        """原子写入（``.tmp`` + ``os.replace``）。

        ⚠️ 失败**抛异常**，不吞：面板保存必须能报错（见模块说明）。
        """
        self.last_loaded = config
        if self.path is None:
            return
        payload = {"version": VERSION, **config.model_dump(mode="json")}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(tmp, self.path)


__all__ = ["VERSION", "ForwardUsedCodesStore"]
