"""Pyrogram 会话库调优：WAL 模式 + 拉长 busy timeout。

背景：kurigram 的 ``SQLiteStorage`` 默认 ``use_wal=False``，``open()`` 会执行
``PRAGMA journal_mode=DELETE``。DELETE（回滚日志）模式下**读会阻塞写**，
而转发开启后 pyrogram 会为每条消息写 peers 缓存 —— 只要还有一个残留连接，
写入就会抛 ``OperationalError: database is locked``。

实测（2000 次「写 peers + usernames + update_state」，另有一个残留读事务）：

* 不修：``journal_mode=delete``、``busy_timeout=1000`` → **15/15 全部失败**，
  每次都要等满 1 秒才抛错（约 1 次写入/秒）。
* 修后：``journal_mode=wal``、``busy_timeout=30000`` → **0 次失败**。

注意「残留**未提交写**事务」是另一回事：WAL 同一时刻只允许一个写者，
那种情况等多久都会失败，**不是**本补丁能解决的，要靠「一个会话文件只给一个进程开」。
"""

from __future__ import annotations

import sqlite3
import types
from pathlib import Path
from typing import Any

import pytest
from pyrogram.storage.sqlite_storage import SQLiteStorage

from tg_assistant.client import (
    SESSION_BUSY_TIMEOUT,
    _patch_sqlite_concurrent,
)
from tg_assistant.paths import Paths

#: ``PRAGMA busy_timeout`` 返回的是毫秒。
BUSY_MS = int(SESSION_BUSY_TIMEOUT * 1000)


def test_patch_is_idempotent() -> None:
    """重复调用不应把 connect / __init__ 层层包裹。"""
    _patch_sqlite_concurrent()
    connect_first = sqlite3.connect
    init_first = SQLiteStorage.__init__

    _patch_sqlite_concurrent()

    assert sqlite3.connect is connect_first
    assert SQLiteStorage.__init__ is init_first


def test_patch_forces_wal_on_new_storage(tmp_path: Path) -> None:
    """补丁把 ``use_wal`` 默认置为 True —— pyrogram 只在 open() 里读它。"""
    _patch_sqlite_concurrent()

    storage = SQLiteStorage("acct", workdir=tmp_path)

    assert storage.use_wal is True


def test_patch_raises_timeout_only_for_session_files(tmp_path: Path) -> None:
    """连接层补丁只认 ``.session``，不能影响进程里其它 sqlite 连接。"""
    _patch_sqlite_concurrent()

    session_db = tmp_path / "acct.session"
    other_db = tmp_path / "unrelated.db"

    with sqlite3.connect(str(session_db), timeout=1) as session_conn:
        assert session_conn.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_MS

    with sqlite3.connect(str(other_db), timeout=1) as other_conn:
        assert other_conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1000


@pytest.mark.asyncio
async def test_open_uses_wal_and_long_busy_timeout(tmp_path: Path) -> None:
    """打开会话库后：journal_mode 应为 WAL，busy_timeout 应被拉长。

    pyrogram 建连接时写死 ``timeout=1``（busy 只等 1 秒）。
    必须在**连接层**抬它，因为 ``open()`` 内部有个无条件 ``VACUUM``
    发生在任何 post-open PRAGMA 之前 —— 事后设 busy_timeout 救不到它。
    """
    _patch_sqlite_concurrent()

    storage = SQLiteStorage("acct", workdir=tmp_path)
    await storage.open()
    try:
        mode = storage.conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"

        busy = storage.conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert busy == BUSY_MS
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_wal_survives_reopen(tmp_path: Path) -> None:
    """WAL 是写在库头里的持久属性：重开后仍是 WAL。"""
    _patch_sqlite_concurrent()

    storage = SQLiteStorage("acct", workdir=tmp_path)
    await storage.open()
    await storage.close()

    reopened = SQLiteStorage("acct", workdir=tmp_path)
    await reopened.open()
    try:
        mode = reopened.conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_open_succeeds_with_lingering_connection(tmp_path: Path) -> None:
    """回归：有残留读事务时 open() 仍应成功。

    这是「一打开转发就锁数据库」的核心场景 ——

    * pyrogram 默认 ``use_wal=False`` → DELETE 模式，**读会阻塞写**；
      此时只要还有一个没关干净的连接（比如上一轮被强杀的 client）持有读事务，
      重开会话库就会抛 ``OperationalError: database is locked``。
    * 转发开启后 pyrogram 为每条消息写 peers 缓存，写入频率陡增，
      所以这个问题只在「打开转发」之后才暴露出来。
    """
    _patch_sqlite_concurrent()

    first = SQLiteStorage("acct", workdir=tmp_path)
    await first.open()
    await first.close()

    db_path = tmp_path / "acct.session"
    assert db_path.is_file()

    # 模拟残留连接：持有读事务不放
    lingering = sqlite3.connect(str(db_path), timeout=1, check_same_thread=False)
    lingering.execute("BEGIN")
    lingering.execute("SELECT number FROM version").fetchone()
    try:
        storage = SQLiteStorage("acct", workdir=tmp_path)
        await storage.open()
        try:
            mode = storage.conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert str(mode).lower() == "wal"

            busy = storage.conn.execute("PRAGMA busy_timeout").fetchone()[0]
            assert busy == BUSY_MS
        finally:
            await storage.close()
    finally:
        lingering.rollback()
        lingering.close()


@pytest.mark.asyncio
async def test_writes_survive_lingering_reader(tmp_path: Path) -> None:
    """回归：残留读事务存在时，高频写入不能抛 database is locked。

    这是直接复刻 ``handle_updates()`` 每条更新做的事
    （写 peers / usernames / update_state）。修复前这段必然抛错。
    """
    _patch_sqlite_concurrent()

    storage = SQLiteStorage("acct", workdir=tmp_path)
    await storage.open()
    db_path = str(storage.database)

    lingering = sqlite3.connect(db_path, timeout=1, check_same_thread=False)
    lingering.execute("BEGIN")
    lingering.execute("SELECT * FROM peers").fetchall()
    try:
        for i in range(200):
            await storage.update_peers([(i, i, "user", None)])
            await storage.update_usernames([(i, [f"u{i}"])])
            await storage.update_state((i, i, i, i, i))
            storage.conn.commit()

        rows = storage.conn.execute("SELECT COUNT(*) FROM peers").fetchone()[0]
        assert rows == 200, "写入应当全部落地，而不是被锁掉"
    finally:
        lingering.rollback()
        lingering.close()
        await storage.close()


@pytest.mark.asyncio
async def test_export_after_close_is_impossible(tmp_path: Path) -> None:
    """钉住上游约束：``storage.close()`` 之后无法再导出 session_string。

    这条不是测我们的代码，而是测**我们依赖的 pyrogram 行为** ——
    ``runner.login_account()`` 之所以必须在 ``client.stop()`` 之前导出，
    唯一原因就是这里。

    ``disconnect()`` → ``storage.close()`` → ``self.conn.close()``，
    但 ``close()`` 不会把 ``self.conn`` 置空，于是后续 ``export_session_string()``
    会走到 ``self.conn.execute(...)`` 抛 ``ProgrammingError``。

    哪天 pyrogram 改掉这个行为（比如 close 后置空再惰性重连），
    这条会失败 —— 那时就可以把导出挪到 stop 之后了。
    """
    storage = SQLiteStorage("acct", workdir=tmp_path)
    await storage.open()
    await storage.close()

    assert storage.conn is not None, "close() 只关连接、不置空 —— 这正是坑的来源"

    with pytest.raises(sqlite3.ProgrammingError):
        await storage.export_session_string()


def test_build_client_arms_the_patch_before_constructing(
    monkeypatch: pytest.MonkeyPatch, paths: Paths
) -> None:
    """回归：``build_client()`` 必须在**构造 Client 之前**把补丁打上。

    补丁之所以放在 ``build_client`` 而不是模块级，唯一依据是
    「``build_client`` 是全项目唯一构造 ``Client`` 的入口」——
    cli / runner / web 都走它。这一点必须被钉住，否则哪天有人挪掉那行调用，
    上面那些用例**会全部照绿**（它们都是自己显式调用
    ``_patch_sqlite_concurrent()`` 的），并发保护就静默失效，
    「一打开转发就锁库」会原样复发。

    同时校验**顺序**：若先构造 Client 再打补丁，``SQLiteStorage.__init__``
    的包裹就晚了 —— storage 对象已经建好，``use_wal`` 仍是 ``False``。
    """
    from tg_assistant import client as client_mod

    events: list[str] = []

    class _DummyClient:
        def __init__(self, **kwargs: Any) -> None:
            events.append("client")

    monkeypatch.setattr(
        client_mod, "_patch_sqlite_concurrent", lambda: events.append("patch")
    )
    monkeypatch.setattr(client_mod, "resolve_credentials", lambda *a, **k: (1, "hash"))
    monkeypatch.setattr(client_mod, "resolve_proxy", lambda *a, **k: None)
    monkeypatch.setattr(client_mod, "device_fingerprint", lambda *a, **k: {})
    monkeypatch.setattr(client_mod, "Client", _DummyClient)

    settings = types.SimpleNamespace(workers=1, sleep_threshold=60, ipv6=False)
    record = types.SimpleNamespace(name="acct")

    client_mod.build_client(record, settings, paths)

    assert events == ["patch", "client"], (
        "build_client 必须先调用 _patch_sqlite_concurrent() 再构造 Client；"
        f"实际顺序：{events}"
    )
