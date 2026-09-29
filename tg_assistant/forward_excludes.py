"""转发「全局排除」名单的读写（``data/forward_excludes.json``）。

为什么要单独一个模块、而不是塞进某个账号的 ``config.json``：这份名单**不属于任何账号**。
它是所有账号共用的一份事实，与 ``data/dedupe.json`` / ``data/metrics.json`` 同类。
放进账号配置就必然出现"改 A 账号不影响 B 账号"—— 那正是用户要摆脱的东西：

    线上两个账号（小白 / SevenStar）的排除频道与黑名单**一模一样**
    （2026-09-29 取证：``-1003932130542`` / ``8817602576``），
    同一份名单存了两遍，面板上还得一个账号填一次。

容错策略：**坏文件 = 空名单**，不抛异常。
这份名单只会让引擎"少排除几条"，不会误伤好消息；反过来，
因为一份写坏的 JSON 让所有账号的转发都起不来，代价完全不对称。
（读取失败会记在 :attr:`ForwardExcludeStore.load_errors` 上，面板能看出"没读到"。）

写入则**必须让失败可见**（面板保存失败要报错，不能悄悄吞掉）——
否则用户以为存上了，重启后名单消失，表现就是"排除又失效了"。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from .config import ForwardExcludes

#: 落盘格式版本。留一个字段时间：将来改形状时能据此迁移，而不是靠猜。
VERSION = 1


class ForwardExcludeStore:
    """全局排除名单的落盘读写。

    ``path=None`` = 纯内存（单测 / 离线场景）：此时 :meth:`load` 返回内存里那份，
    :meth:`save` 只更新内存，:meth:`mtime` 恒为 ``None``。
    """

    def __init__(self, path: Optional[Any] = None) -> None:
        self.path: Optional[Path] = Path(path) if path is not None else None
        #: 最近一次读到 / 写过的名单。
        self.last_loaded: ForwardExcludes = ForwardExcludes()
        #: 读文件失败的次数（文件坏 / 形状不对）。面板据此提示"名单没读到"。
        self.load_errors = 0

    # ------------------------------------------------------------------ 读
    def load(self) -> ForwardExcludes:
        """读回名单。文件不存在 / 坏 / 形状不对都返回**空名单**。"""
        if self.path is None:
            return self.last_loaded
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.last_loaded = ForwardExcludes()
            return self.last_loaded
        except Exception:
            # 坏 JSON、权限、是目录…… 一律当空名单（见模块说明的取舍）
            self.load_errors += 1
            self.last_loaded = ForwardExcludes()
            return self.last_loaded
        self.last_loaded = self._parse(raw)
        return self.last_loaded

    def _parse(self, raw: Any) -> ForwardExcludes:
        if not isinstance(raw, dict):
            self.load_errors += 1
            return ForwardExcludes()
        try:
            return ForwardExcludes.model_validate(
                {
                    "exclude_chats": raw.get("exclude_chats") or [],
                    "exclude_users": raw.get("exclude_users") or [],
                }
            )
        except Exception:
            # 形状对不上（例如手写成一个字符串）同样退回空名单
            self.load_errors += 1
            return ForwardExcludes()

    def mtime(self) -> Optional[float]:
        """文件 mtime，供热重载轮询用；读不到返回 ``None``。"""
        if self.path is None:
            return None
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None

    # ------------------------------------------------------------------ 写
    def save(self, excludes: ForwardExcludes) -> None:
        """原子写入（``.tmp`` + ``os.replace``）。

        ⚠️ 失败**抛异常**，不吞：面板保存必须能报错（见模块说明）。
        """
        self.last_loaded = excludes
        if self.path is None:
            return
        payload = {"version": VERSION, **excludes.model_dump(mode="json")}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(tmp, self.path)


__all__ = ["VERSION", "ForwardExcludeStore"]
