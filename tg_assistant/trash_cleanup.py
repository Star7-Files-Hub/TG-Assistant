"""频道转发的 💩 reaction 轮询清理。

reaction 更新事件在 kurigram layer 中没有频道对应的 UpdateChannelMessageReactions，
因此不能依赖事件回调，只能定期批量读取消息。记录必须落盘，因为 reaction 可能在数小时后出现且进程会重启。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def _normalize_emoji(value: Any) -> str:
    """比对前去掉「变体选择符」（U+FE0E / U+FE0F）。

    同一个 emoji 在不同键盘/输入法下可能带或不带变体选择符（``❤️`` 与 ``❤``）。
    直接 ``==`` 比对的话，「配置里带、reaction 里不带」会**永远匹配不上**，
    而且完全不报错 —— 正是最该避免的静默失效。
    """
    return str(value or "").replace("\ufe0e", "").replace("\ufe0f", "").strip()


def should_delete(reactions: Any, threshold: int, emoji: str) -> bool:
    """根据 ``message.reactions.reactions`` 里的 emoji/count 判断是否达到阈值。

    ``reactions`` 是 pyrogram 的 ``MessageReactions.reactions``（每项有 ``emoji`` 与
    ``count``，已在本机 2.2.25 上实测确认）。``count`` 就是「多少个**不同用户**」——
    同一个用户对同一条消息重复点同一个 emoji 不会叠加。

    ``threshold <= 0`` 表示**永不删除**（面板最小给 1）。
    """
    if not reactions or threshold <= 0:
        return False
    wanted = _normalize_emoji(emoji)
    if not wanted:
        return False
    for reaction in reactions:
        if _normalize_emoji(getattr(reaction, "emoji", None)) != wanted:
            continue
        if int(getattr(reaction, "count", 0) or 0) >= threshold:
            return True
    return False


class TrashWatchStore:
    TTL = 7 * 86400.0
    LIMIT = 5000

    def __init__(self, path: str | Path | None = None, *, ttl: float = TTL, limit: int = LIMIT) -> None:
        self.path = Path(path) if path else None
        self.ttl = ttl
        self.limit = limit
        self._items: dict[str, dict[str, Any]] = {}
        self.load()

    def record(
        self,
        chat_id: int,
        message_id: int,
        *,
        link_message_id: int | None,
        rule_id: str,
        delete_ids: list[int] | None = None,
    ) -> None:
        """记下「目标频道这条消息该连同哪些一起删」。

        ``delete_ids`` 是给**相册**准备的：一条相册会转发成多条消息，用户在任意一条上
        点踩都该整组删掉；缺省时退化成「这条 + 链接那条」。
        """
        self._items[f"{chat_id}:{message_id}"] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "link_message_id": link_message_id,
            "delete_ids": [int(i) for i in (delete_ids or [message_id])],
            "rule_id": rule_id,
            "created_at": time.time(),
        }
        self.prune()
        self.save()

    def get(self, chat_id: int, message_id: int) -> dict[str, Any] | None:
        return self._items.get(f"{chat_id}:{message_id}")

    def forget(self, chat_id: int, message_id: int) -> None:
        self._items.pop(f"{chat_id}:{message_id}", None)
        self.save()

    def prune(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._items = {k: v for k, v in self._items.items() if now - float(v.get("created_at", now)) <= self.ttl}
        if len(self._items) > self.limit:
            keys = sorted(self._items, key=lambda k: self._items[k].get("created_at", 0))
            for key in keys[: len(self._items) - self.limit]:
                del self._items[key]

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + f".tmp.{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"items": self._items}, f, ensure_ascii=False, indent=2)
            f.write("\n"); f.flush(); os.fsync(f.fileno())
        os.chmod(tmp, 0o600); os.replace(tmp, self.path)

    def load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            with open(self.path, encoding="utf-8") as f: data = json.load(f)
            self._items = data.get("items", {}) if isinstance(data, dict) else {}
            self.prune(); self.save()
        except (OSError, ValueError, TypeError):
            self._items = {}

    def items(self) -> list[dict[str, Any]]:
        self.prune()
        return list(self._items.values())


__all__ = ["TrashWatchStore", "should_delete"]
