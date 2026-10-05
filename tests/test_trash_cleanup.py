"""踩踏清理的存储与判定（💩 够人数就删）。

用户原话：「当有大于2用户点💩删信息，并将原文链接一同删除」，口径定为「含 2」。

为什么这些记录必须**落盘**：reaction 可能几小时后才出现，而进程随时会重启；
只放内存的话重启后那些已经转发出去的消息就再也没人管了。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

from tg_assistant.trash_cleanup import TrashWatchStore, should_delete


def _reactions(*pairs: tuple[str, int]):
    """伪造 ``message.reactions.reactions``（实测属性路径就是 ``.emoji`` / ``.count``）。"""
    return [SimpleNamespace(emoji=emoji, count=count) for emoji, count in pairs]


class TestShouldDelete:
    def test_threshold_is_inclusive(self):
        """用户口径是「含 2」⇒ count == threshold 就要删。"""
        assert should_delete(_reactions(("💩", 2)), 2, "💩")
        assert should_delete(_reactions(("💩", 3)), 2, "💩")

    def test_below_threshold_is_kept(self):
        assert not should_delete(_reactions(("💩", 1)), 2, "💩")

    def test_other_emoji_does_not_count(self):
        assert not should_delete(_reactions(("👍", 99)), 2, "💩")

    def test_variation_selector_is_normalized(self):
        """同一个 emoji 带不带变体选择符（U+FE0E/FE0F）必须等价。

        直接 ``==`` 比对的话，「配置里带、reaction 里不带」会**永远匹配不上**，
        而且完全不报错 —— 静默失效最难查。
        """
        assert should_delete(_reactions(("💩", 2)), 2, "💩\ufe0f")
        assert should_delete(_reactions(("💩\ufe0f", 2)), 2, "💩")
        assert should_delete(_reactions(("❤️", 2)), 2, "❤")

    def test_no_reactions_or_disabled_threshold(self):
        assert not should_delete(None, 2, "💩")
        assert not should_delete([], 2, "💩")
        # threshold <= 0 表示「永不删除」（面板最小给 1）
        assert not should_delete(_reactions(("💩", 99)), 0, "💩")
        # 表情为空时绝不能「随便匹配一个」—— 那会把所有消息都删掉
        assert not should_delete(_reactions(("💩", 99)), 2, "   ")


class TestTrashWatchStore:
    def test_roundtrip_and_forget(self, tmp_path: Path):
        path = tmp_path / "trash.json"
        store = TrashWatchStore(path, ttl=3600, limit=100)
        store.record(-1, 1, link_message_id=2, rule_id="r")

        assert store.get(-1, 1)["link_message_id"] == 2
        # 落盘再读回：重启后必须还在（reaction 可能几小时后才来）
        loaded = TrashWatchStore(path, ttl=3600, limit=100)
        assert loaded.get(-1, 1)["rule_id"] == "r"

        loaded.forget(-1, 1)
        assert loaded.get(-1, 1) is None
        assert TrashWatchStore(path, ttl=3600, limit=100).get(-1, 1) is None, "forget 也要落盘"

    def test_delete_ids_defaults_to_itself_and_can_cover_an_album(self, tmp_path: Path):
        store = TrashWatchStore(tmp_path / "t.json")
        store.record(-1, 1, link_message_id=9, rule_id="r")
        assert store.get(-1, 1)["delete_ids"] == [1]

        store.record(-1, 2, link_message_id=9, rule_id="r", delete_ids=[1, 2, 3])
        assert store.get(-1, 2)["delete_ids"] == [1, 2, 3], "相册要整组删"

    def test_ttl_expiry(self, tmp_path: Path):
        store = TrashWatchStore(tmp_path / "t.json", ttl=10)
        store.record(-1, 1, link_message_id=None, rule_id="r")
        store._items["-1:1"]["created_at"] = time.time() - 20

        store.prune()

        assert store.get(-1, 1) is None

    def test_limit_evicts_oldest(self, tmp_path: Path):
        # ⚠️ 先给一个大上限把三条都录进去，再收紧到 2 —— 直接用小上限的话，
        # ``record()`` 内部的 ``prune()`` 会在我们改时间戳**之前**就把刚录的那条
        # 当成最旧的淘汰掉，测不到「按 created_at 淘汰最旧」这条逻辑。
        store = TrashWatchStore(tmp_path / "t.json", limit=100)
        for i in (1, 2, 3):
            store.record(-1, i, link_message_id=None, rule_id="r")
            # 也不能用裸 i：那是 1970 年的时间戳，会先被 TTL 淘汰掉。
            store._items[f"-1:{i}"]["created_at"] = time.time() + i
        store.limit = 2

        store.prune()

        assert store.get(-1, 1) is None, "超出上限要淘汰最旧的"
        assert store.get(-1, 3) is not None

    def test_corrupt_file_does_not_raise(self, tmp_path: Path):
        path = tmp_path / "t.json"
        path.write_text("{ 这不是 JSON", encoding="utf-8")

        store = TrashWatchStore(path)

        assert store.items() == []

    def test_saved_file_is_json_with_expected_shape(self, tmp_path: Path):
        path = tmp_path / "t.json"
        TrashWatchStore(path).record(-100, 7, link_message_id=8, rule_id="r")

        data = json.loads(path.read_text(encoding="utf-8"))

        assert data["items"]["-100:7"]["message_id"] == 7

    def test_no_path_means_memory_only(self):
        """测试里可以不传路径（引擎的 store 路径由账号目录决定）。"""
        store = TrashWatchStore(None)
        store.record(-1, 1, link_message_id=None, rule_id="r")

        assert store.get(-1, 1) is not None
