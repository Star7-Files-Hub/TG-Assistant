"""转发「已使用注册码拦截」**全局配置**的落盘读写（``tg_assistant.forward_used_codes``）。

为什么这份策略值得单独一个文件、单独一组用例：它**不属于任何账号** ——
所有账号共用 ``data/forward_used_codes.json`` 一份。用户原话：
「将……已使用注册码拦截做成全局，而不是账号级」。

线上的取证是：三个账号（小白 / SevenStar / 只想睡觉）的 ``forward.used_codes``
**一字不差完全相同**，同一份策略存了三遍，面板上还得一个账号填一次。

它的容错方向与 :mod:`tests.test_forward_excludes` 那位"兄弟"**完全一致**
（本来就是照它写的），所以这里的用例也照那套组织：

* **读**坏了 = 缺省策略继续跑（默认开 + 默认正则），记一笔 ``load_errors``。
  一份写坏的 JSON 不该让所有账号的转发都起不来。
* **写**坏了必须抛出来。面板保存要能报错，否则用户以为存上了、重启后策略回到默认，
  表现就是"拦截又失效了"，而且没有任何线索。

📌 与「全局排除名单」的唯一区别：那份与账号级取**并集**，这份是**替换** ——
策略只有一份，账号配置里那个 ``forward.used_codes`` 不再参与（只为旧配置能加载而保留）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tg_assistant.config import ForwardUsedCodes
from tg_assistant.forward_used_codes import VERSION, ForwardUsedCodesStore


def make_store(tmp_path: Path) -> ForwardUsedCodesStore:
    """落盘在 ``tmp_path/forward_used_codes.json``（真实布局里是 ``data/`` 根下）。"""
    return ForwardUsedCodesStore(tmp_path / "forward_used_codes.json")


def write_raw(path: Path, text: str) -> None:
    """绕过 store 直接写文件 —— 模拟用户手改 / 上一次写坏留下的内容。"""
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# 读：坏文件一律当缺省策略，但**要能看出来**读过一次失败
# --------------------------------------------------------------------------- #
class TestLoadTolerance:
    def test_missing_file_is_defaults_without_counting_an_error(self, tmp_path):
        """文件不存在 = 还没配过，**不是**错误。

        「没配过」与「读坏了」在面板上必须区分开：前者显示"未设置"（用默认策略），
        后者要报警（此刻是按缺省策略在跑，用户以为配好的拦截其实没生效）。
        """
        store = make_store(tmp_path)

        assert store.load() == ForwardUsedCodes()
        assert store.load_errors == 0, "文件不存在不该算一次读失败"

    def test_broken_json_is_defaults_and_counts_an_error(self, tmp_path):
        """写坏的 JSON：缺省策略继续跑，但 ``load_errors`` 要涨，且**不能残留旧值**。"""
        store = make_store(tmp_path)
        store.save(ForwardUsedCodes.model_validate({"min_visible": 5}))
        write_raw(store.path, "{ 这不是合法 JSON")

        assert store.load() == ForwardUsedCodes(), "坏文件必须退回缺省，且不能残留旧值"
        assert store.load_errors == 1

    def test_broken_file_keeps_counting_until_it_is_rewritten(self, tmp_path):
        """失败次数是**累计**的，改回合法文件后不再涨（也不清零）。"""
        store = make_store(tmp_path)
        write_raw(store.path, "{坏了")

        store.load()
        store.load()
        assert store.load_errors == 2, "每读一次失败都要记一笔"

        store.save(ForwardUsedCodes.model_validate({"min_visible": 7}))
        assert store.load().min_visible == 7
        assert store.load_errors == 2, "恢复读取不该把历史失败次数清掉"

    def test_version_field_in_the_file_is_ignored_by_the_model(self, tmp_path):
        """落盘带 ``version``，读回来时不能被 ``StrictModel``（``extra="forbid"``）拒掉。

        这是最容易踩的坑：save 时混进去的 ``version`` 不在模型字段里，
        解析前必须先摘掉，否则"自己写的文件自己读不回来" → 每次都退回缺省。
        """
        store = make_store(tmp_path)
        store.save(ForwardUsedCodes.model_validate({"min_visible": 9}))

        # 确认文件里真的有 version
        assert json.loads(store.path.read_text(encoding="utf-8"))["version"] == VERSION

        # 换实例重新读：不能因为多了 version 就退回缺省、更不该记成一次失败
        reloaded = make_store(tmp_path).load()
        assert reloaded.min_visible == 9
        assert store.load_errors == 0

    @pytest.mark.parametrize(
        "raw",
        [
            # 顶层不是对象
            '[{"enabled": true}]',
            # 字段类型不合法：min_visible 越界（模型会拒）
            '{"min_visible": 0}',
            # 未知字段：StrictModel 的 extra="forbid" 会拒
            '{"nonsense": 1}',
            # 正则编译不了（模型自己的校验器会拒）
            '{"notice_pattern": "([A-Za-z0-9"}',
        ],
    )
    def test_wrong_shape_is_defaults(self, tmp_path, raw):
        """形状 / 取值对不上 = 缺省策略，而不是让调用方去接异常。"""
        store = make_store(tmp_path)
        write_raw(store.path, raw)

        assert store.load() == ForwardUsedCodes()
        assert store.load_errors == 1

    def test_hand_written_file_is_loaded(self, tmp_path):
        """用户完全可能直接编辑这个文件 —— 手写的合法内容必须生效。"""
        store = make_store(tmp_path)
        write_raw(
            store.path,
            '{"enabled": false, "notice_keywords": ["码使用", "已使用"], "min_visible": 4}',
        )

        loaded = store.load()

        assert loaded.enabled is False
        assert loaded.notice_keywords == ["码使用", "已使用"]
        assert loaded.min_visible == 4
        assert store.load_errors == 0


# --------------------------------------------------------------------------- #
# 写：原子、可见、可迁移
# --------------------------------------------------------------------------- #
class TestSave:
    def test_round_trip_through_disk(self, tmp_path):
        """save → load 往返：磁盘上的内容才是唯一事实。"""
        store = make_store(tmp_path)
        cfg = ForwardUsedCodes.model_validate(
            {"enabled": True, "min_visible": 4, "ttl": 1800, "ignore_token_pattern": ""}
        )

        store.save(cfg)

        # 换一个 store 实例再读一次：证明真的落盘了，不是内存里那份
        assert make_store(tmp_path).load() == cfg

    def test_saved_payload_carries_the_version_field(self, tmp_path):
        """落盘带 ``version``：将来改形状时能据此迁移，而不是靠猜。"""
        store = make_store(tmp_path)
        store.save(ForwardUsedCodes.model_validate({"min_visible": 5}))

        payload = json.loads(store.path.read_text(encoding="utf-8"))

        assert payload["version"] == VERSION
        assert payload["min_visible"] == 5
        assert payload["enabled"] is True  # 缺省字段也一并落盘，面板读得到

    def test_save_creates_the_parent_directory(self, tmp_path):
        """``data/`` 被清掉时也要能存上（部署脚本、手工清理都可能碰到）。"""
        path = tmp_path / "data" / "forward_used_codes.json"
        store = ForwardUsedCodesStore(path)

        store.save(ForwardUsedCodes())

        assert path.exists()

    def test_save_is_atomic_and_leaves_no_tmp_file(self, tmp_path):
        """写成功之后不能留下 ``.tmp``，临时文件必须是**同目录**的兄弟。

        直接 ``write_text`` 到目标路径时，进程被杀 / 磁盘写满会留下一份被截断的
        JSON —— 读方当"坏文件" → 缺省策略 → 三个账号的拦截一起回到默认。
        """
        store = make_store(tmp_path)
        store.save(ForwardUsedCodes.model_validate({"min_visible": 6}))

        sibling = store.path.with_name(store.path.name + ".tmp")
        assert not sibling.exists(), "同目录那个 .tmp 没被 os.replace 掉"
        assert list(tmp_path.rglob("*.tmp")) == [], "目录树里还有 .tmp 残留"
        assert json.loads(store.path.read_text(encoding="utf-8"))["min_visible"] == 6

    def test_a_failed_save_does_not_truncate_the_existing_file(self, tmp_path):
        """写入失败时，磁盘上那份好配置必须**原样还在**。"""
        store = make_store(tmp_path)
        good = ForwardUsedCodes.model_validate({"min_visible": 5})
        store.save(good)
        before = store.path.read_bytes()
        store.path.with_name(store.path.name + ".tmp").mkdir()

        with pytest.raises(OSError):
            store.save(ForwardUsedCodes.model_validate({"min_visible": 8}))

        assert store.path.read_bytes() == before, "失败的写入把原有的好配置截断了"

    def test_save_failure_is_raised_not_swallowed(self, tmp_path):
        """存不上必须抛异常（面板要能报错）。与读的容错**刻意相反**。"""
        blocker = tmp_path / "blocker"
        blocker.write_text("我是个普通文件，不是目录", encoding="utf-8")
        store = ForwardUsedCodesStore(blocker / "forward_used_codes.json")

        with pytest.raises(OSError):
            store.save(ForwardUsedCodes())


# --------------------------------------------------------------------------- #
# mtime：热重载唯一的触发依据
# --------------------------------------------------------------------------- #
class TestMtime:
    def test_mtime_is_none_without_a_file(self, tmp_path):
        """文件不存在时 mtime 是 ``None``，而不是抛异常。"""
        assert make_store(tmp_path).mtime() is None

    def test_mtime_moves_when_the_file_changes(self, tmp_path):
        """文件一变，mtime 必须跟着变 —— 这是全局策略热重载的**唯一**依据。

        策略不在 ``config.json`` 里，改它不会动账号配置；runner 只盯 config.json 的
        话，面板改完全局策略不会触发任何重载，用户改完立刻去群里验证、看到没拦住
        只会以为功能坏了。
        """
        store = make_store(tmp_path)
        store.save(ForwardUsedCodes())
        first = store.mtime()
        assert isinstance(first, float)

        # 明确往前推，不靠 sleep 赌文件系统时间精度（秒级会假绿）
        stat = store.path.stat()
        os.utime(store.path, (stat.st_atime, stat.st_mtime + 10))

        assert store.mtime() == first + 10

    def test_mtime_survives_a_file_that_cannot_be_stat_ed(self, tmp_path):
        """路径是目录 / 被删掉时 ``stat`` 会抛：这里要吞掉并返回 ``None``。

        runner 的轮询不能被一个异常的策略文件打断（那会连 config.json 的重载也停掉）。
        """
        store = make_store(tmp_path)
        store.path.mkdir()  # 同名目录：stat 成功但读取会失败

        assert store.mtime() is not None
        assert store.load() == ForwardUsedCodes(), "读目录失败也要退回缺省"
        assert store.load_errors == 1


# --------------------------------------------------------------------------- #
# 纯内存模式（单测 / 离线场景）
# --------------------------------------------------------------------------- #
class TestMemoryOnlyStore:
    def test_no_path_means_memory_only(self):
        """``path=None``：不落盘、mtime 恒为 ``None``、失败计数不涨。

        转发引擎在没有 ``store``（单测 / 离线）时走这条路 —— 此时用 ``ForwardUsedCodes``
        缺省策略，行为可预期，不会因为读不到全局配置而报错。
        """
        store = ForwardUsedCodesStore()
        assert store.mtime() is None
        assert store.load() == ForwardUsedCodes()

        cfg = ForwardUsedCodes.model_validate({"min_visible": 4})
        store.save(cfg)

        assert store.load() == cfg, "纯内存模式也要能存下来给本次运行用"
        assert store.load_errors == 0
