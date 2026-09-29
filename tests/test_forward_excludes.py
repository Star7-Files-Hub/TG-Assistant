"""转发「全局排除」名单的落盘读写（``tg_assistant.forward_excludes``）。

为什么这份名单值得单独一个文件、单独一组用例：它**不属于任何账号** ——
所有账号共用 ``data/forward_excludes.json`` 一份（用户原话：「将转发规则的黑名单及
排除的频道也做成全局的」）。而它的容错方向与账号配置**正好相反**：

* **读**坏了 = 空名单继续跑。它只会让引擎"少排除几条"，不会误伤好消息；
  反过来，因为一份写坏的 JSON 让**所有**账号的转发都起不来，代价完全不对称。
  （失败次数记在 ``load_errors`` 上，面板据此提示"名单没读到"，而不是让用户
  对着一个"看起来配好了"的界面发呆。）
* **写**坏了必须抛出来。面板保存要能报错 —— 悄悄吞掉的话，用户以为存上了、
  重启后名单消失，表现就是"排除又失效了"，而且没有任何线索。

用例按这两条取舍组织。归一化（``"-1003932130542"`` → ``int``）也单独钉住：
它是"名单明明存进去了却拦不住"这类最难查的 bug 的唯一入口 —— 面板传上来的是
字符串，不归一化就会被当成 username，引擎按数字 id 比对时永远匹配不上。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tg_assistant.config import ForwardExcludes
from tg_assistant.forward_excludes import VERSION, ForwardExcludeStore


def make_store(tmp_path: Path) -> ForwardExcludeStore:
    """落盘在 ``tmp_path/forward_excludes.json``（真实布局里是 ``data/`` 根下）。"""
    return ForwardExcludeStore(tmp_path / "forward_excludes.json")


def write_raw(path: Path, text: str) -> None:
    """绕过 store 直接写文件 —— 模拟用户手改 / 上一次写坏留下的内容。"""
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# 读：坏文件一律当空名单，但**要能看出来**读过一次失败
# --------------------------------------------------------------------------- #
class TestLoadTolerance:
    def test_missing_file_is_an_empty_list_without_counting_an_error(self, tmp_path):
        """文件不存在 = 还没配过，**不是**错误。

        「没配过」与「读坏了」在面板上必须区分开：前者显示"未设置"，
        后者要报警（此刻是按空名单在跑，用户以为配好的排除其实没生效）。
        混成一个计数的话，全新安装的账号一进来就会看到一条吓人的警告。
        """
        store = make_store(tmp_path)

        assert store.load() == ForwardExcludes()
        assert store.load_errors == 0, "文件不存在不该算一次读失败"

    def test_broken_json_is_an_empty_list_and_counts_an_error(self, tmp_path):
        """写坏的 JSON：空名单继续跑，但 ``load_errors`` 要涨。

        注意不能返回上一次的结果（那份可能存在 ``last_loaded`` 里）：名单被
        别的账号 / 手改覆盖时，引擎必须读到磁盘上的**现状**。
        """
        store = make_store(tmp_path)
        store.save(ForwardExcludes.model_validate({"exclude_chats": [-1001]}))
        write_raw(store.path, "{ 这不是合法 JSON")

        assert store.load() == ForwardExcludes(), "坏文件必须退回空名单，且不能残留旧值"
        assert store.load_errors == 1

    def test_broken_file_keeps_counting_until_it_is_rewritten(self, tmp_path):
        """失败次数是**累计**的，改回合法文件后不再涨（也不清零）。

        面板上显示的是"已失败 N 次"：清零的话，用户刷新页面后就看不出
        "曾经有一段时间名单根本没读到"，那段时间的漏拦就永远查不出来了。
        """
        store = make_store(tmp_path)
        write_raw(store.path, "{坏了")

        store.load()
        store.load()
        assert store.load_errors == 2, "每读一次失败都要记一笔"

        store.save(ForwardExcludes.model_validate({"exclude_users": [555]}))
        assert store.load() == ForwardExcludes.model_validate({"exclude_users": [555]})
        assert store.load_errors == 2, "恢复读取不该把历史失败次数清掉"

    @pytest.mark.parametrize(
        "raw",
        [
            # 顶层不是对象（有人按排除频道的形状直接写了个数组）
            '["-1001"]',
            # 名单字段是一个没法迭代的值：归一化时当场抛错
            '{"exclude_chats": 1.5}',
        ],
    )
    def test_wrong_shape_is_an_empty_list(self, tmp_path, raw):
        """形状对不上 = 空名单，而不是让调用方去接异常。

        坏文件在这里只意味着"少排除几条"；抛异常则会让每个读到它的账号
        转发停摆 —— 这个取舍写在模块说明里，这里钉住它真的成立。
        """
        store = make_store(tmp_path)
        write_raw(store.path, raw)

        assert store.load() == ForwardExcludes()
        assert store.load_errors == 1

    def test_single_string_is_accepted_as_one_ref(self, tmp_path):
        """``"exclude_chats": "@Foo"`` 是**合法**的单个引用，不算形状错误。

        与账号级那两份名单共用同一个模型：面板偶尔会传单个值（"就排除这一个
        频道"），账号级早就接受了这种写法。这里要是拒掉，两处行为就不一致，
        用户会看到"同一个值在账号级能填、在全局填不进去"。
        """
        store = make_store(tmp_path)
        write_raw(store.path, '{"exclude_chats": "@Foo"}')

        assert store.load().exclude_chats == ["foo"]
        assert store.load_errors == 0

    def test_hand_written_file_is_normalized_on_read(self, tmp_path):
        """手写的名单也要归一化 —— 用户完全可能直接编辑这个文件。

        线上取证的那两个值（``-1003932130542`` / ``8817602576``）是字符串形态
        存进来的，读回来必须是 ``int``，否则引擎按数字 id 比对时永远匹配不上。
        """
        store = make_store(tmp_path)
        write_raw(
            store.path,
            '{"exclude_chats": ["-1003932130542"], "exclude_users": ["@Foo", 8817602576]}',
        )

        loaded = store.load()

        assert loaded.exclude_chats == [-1003932130542]
        assert loaded.exclude_users == ["foo", 8817602576]
        assert store.load_errors == 0


# --------------------------------------------------------------------------- #
# 写：原子、可见、可迁移
# --------------------------------------------------------------------------- #
class TestSave:
    def test_round_trip_through_disk(self, tmp_path):
        """save → load 往返：磁盘上的内容才是唯一事实。"""
        store = make_store(tmp_path)
        excludes = ForwardExcludes.model_validate(
            {"exclude_chats": [-1003932130542], "exclude_users": [8817602576]}
        )

        store.save(excludes)

        # 换一个 store 实例再读一次：证明真的落盘了，不是内存里那份
        assert make_store(tmp_path).load() == excludes

    def test_saved_payload_carries_the_version_field(self, tmp_path):
        """落盘带 ``version``：将来改形状时能据此迁移，而不是靠猜。"""
        store = make_store(tmp_path)
        store.save(ForwardExcludes.model_validate({"exclude_chats": [-1001]}))

        payload = json.loads(store.path.read_text(encoding="utf-8"))

        assert payload["version"] == VERSION
        assert payload["exclude_chats"] == [-1001]
        assert payload["exclude_users"] == []

    def test_save_creates_the_parent_directory(self, tmp_path):
        """``data/`` 被清掉时也要能存上（部署脚本、手工清理都可能碰到）。"""
        path = tmp_path / "data" / "forward_excludes.json"
        store = ForwardExcludeStore(path)

        store.save(ForwardExcludes.model_validate({"exclude_chats": [-1001]}))

        assert path.exists()

    def test_save_is_atomic_and_leaves_no_tmp_file(self, tmp_path):
        """写成功之后不能留下 ``.tmp``，而且临时文件必须是**同目录**的兄弟。

        为什么要临时文件 + ``os.replace``：直接 ``write_text`` 到目标路径时，
        进程被杀 / 磁盘写满会留下一份**被截断的** JSON —— 而读方把它当"坏文件"
        → 空名单 → 所有账号的排除一起失效。
        用 ``os.replace`` 则只有"整份写完"这一个可见状态。
        临时文件放在同目录（不是 ``/tmp``）才能保证 replace 是原子的：
        跨文件系统的 replace 会退化成"复制 + 删除"，又回到会被截断的处境。
        """
        store = make_store(tmp_path)
        store.save(ForwardExcludes.model_validate({"exclude_chats": [-1001, "@Foo"]}))

        # ⚠️ 只查临时文件：``tmp_path`` 下还有夹具自己造的目录（日志 / data），
        # 它们的**存在是正常的**，不是残留。要证明的是"写完就没有 .tmp 了"。
        sibling = store.path.with_name(store.path.name + ".tmp")
        assert not sibling.exists(), "同目录那个 .tmp 没被 os.replace 掉"
        assert list(tmp_path.rglob("*.tmp")) == [], "目录树里还有 .tmp 残留"
        # 内容也得是完整的合法 JSON（原子写失败的典型表现就是半份 JSON）
        assert json.loads(store.path.read_text(encoding="utf-8"))["exclude_chats"] == [-1001, "foo"]

    def test_a_failed_save_does_not_truncate_the_existing_file(self, tmp_path):
        """写入失败时，磁盘上那份好名单必须**原样还在**。

        这是"原子写"真正要保证的事：失败时最坏也只是这一次改动没生效，
        而不是把上一份可用的名单写坏 —— 后者会被读成空名单，
        在用户眼里就是"刚才还好好的排除，突然全都失效了"。

        构造失败：把 ``.tmp`` 这个位置换成目录，写临时文件那一步必然失败。
        """
        store = make_store(tmp_path)
        good = ForwardExcludes.model_validate({"exclude_chats": [-1001]})
        store.save(good)
        before = store.path.read_bytes()
        store.path.with_name(store.path.name + ".tmp").mkdir()

        with pytest.raises(OSError):
            store.save(ForwardExcludes.model_validate({"exclude_chats": [-2002]}))

        assert store.path.read_bytes() == before, "失败的写入把原有的好名单截断了"

    def test_save_failure_is_raised_not_swallowed(self, tmp_path):
        """存不上必须抛异常（面板要能报错）。

        与读的容错**刻意相反**：读失败最多"少排除几条"，写失败若被吞掉，
        用户会得到"保存成功"的假象，重启后名单消失 —— 表现是"排除又失效了"，
        而且没有任何线索可查。
        """
        blocker = tmp_path / "blocker"
        blocker.write_text("我是个普通文件，不是目录", encoding="utf-8")
        store = ForwardExcludeStore(blocker / "forward_excludes.json")

        with pytest.raises(OSError):
            store.save(ForwardExcludes.model_validate({"exclude_chats": [-1001]}))


# --------------------------------------------------------------------------- #
# mtime：热重载唯一的触发依据
# --------------------------------------------------------------------------- #
class TestMtime:
    def test_mtime_is_none_without_a_file(self, tmp_path):
        """文件不存在时 mtime 是 ``None``，而不是抛异常。

        runner 每秒拿它跟上次记的值比：``None != 上次的 None`` 不成立就什么都不做，
        而"文件刚刚出现"（None → 浮点）必定触发一次重载。
        """
        assert make_store(tmp_path).mtime() is None

    def test_mtime_moves_when_the_file_changes(self, tmp_path):
        """文件一变，mtime 必须跟着变 —— 这是全局名单热重载的**唯一**依据。

        名单不在 ``config.json`` 里，改它不会动账号配置；runner 只盯 config.json 的
        话，面板改完全局名单不会触发任何重载，用户改完立刻去群里验证、看到没拦住
        只会以为功能坏了。
        """
        store = make_store(tmp_path)
        store.save(ForwardExcludes.model_validate({"exclude_chats": [-1001]}))
        first = store.mtime()
        assert isinstance(first, float)

        # 明确往前推，不靠 sleep 赌文件系统的时间精度（秒级精度下会假绿）
        stat = store.path.stat()
        os.utime(store.path, (stat.st_atime, stat.st_mtime + 10))

        assert store.mtime() == first + 10

    def test_mtime_survives_a_file_that_cannot_be_stat_ed(self, tmp_path):
        """名单路径是目录 / 被删掉时 ``stat`` 会抛：这里要吞掉并返回 ``None``。

        runner 的轮询不能被一个异常的名单文件打断（那会连 config.json 的重载
        也一起停掉）。
        """
        store = make_store(tmp_path)
        store.path.mkdir()  # 同名目录：stat 成功但读取会失败

        assert store.mtime() is not None
        assert store.load() == ForwardExcludes(), "读目录失败也要退回空名单"
        assert store.load_errors == 1


# --------------------------------------------------------------------------- #
# 纯内存模式（单测 / 离线场景）
# --------------------------------------------------------------------------- #
class TestMemoryOnlyStore:
    def test_no_path_means_memory_only(self):
        """``path=None``：不落盘、mtime 恒为 ``None``、失败计数不涨。

        转发引擎在没有 ``store``（单测 / 离线）时走这条路 —— 此时只有账号级名单
        生效，行为与加这个功能之前**完全一致**，不会因为读不到全局名单而报错。
        """
        store = ForwardExcludeStore()
        assert store.mtime() is None
        assert store.load() == ForwardExcludes()

        excludes = ForwardExcludes.model_validate({"exclude_chats": [-1001]})
        store.save(excludes)

        assert store.load() == excludes, "纯内存模式也要能存下来给本次运行用"
        assert store.load_errors == 0


# --------------------------------------------------------------------------- #
# 归一化：字符串 id → int、@ 前缀与大小写
# --------------------------------------------------------------------------- #
class TestNormalization:
    def test_numeric_id_strings_become_int(self, tmp_path):
        """``"-1003932130542"`` / ``"8817602576"`` → ``int``（线上取证的那两个值）。"""
        store = make_store(tmp_path)
        store.save(
            ForwardExcludes.model_validate(
                {"exclude_chats": ["-1003932130542"], "exclude_users": ["8817602576"]}
            )
        )

        loaded = store.load()

        assert loaded.exclude_chats == [-1003932130542]
        assert loaded.exclude_users == [8817602576]
        assert isinstance(loaded.exclude_chats[0], int)

    def test_at_prefix_and_case_are_normalized(self, tmp_path):
        """``"@Foo"`` → ``"foo"``：引擎按小写 username 比对。"""
        store = make_store(tmp_path)
        store.save(ForwardExcludes.model_validate({"exclude_chats": ["@Noisy_Channel"]}))

        assert store.load().exclude_chats == ["noisy_channel"]

    def test_duplicates_collapse_and_empty_entries_drop(self, tmp_path):
        """重复项去重、空值丢弃：面板连点两次保存不该让名单越滚越长。"""
        store = make_store(tmp_path)
        store.save(
            ForwardExcludes.model_validate(
                {"exclude_users": [555, "@Foo", "555", "", "  "]}
            )
        )

        assert store.load().exclude_users == [555, "foo"]
