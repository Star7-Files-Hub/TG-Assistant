"""Pyrogram 会话库调优：WAL 模式 + busy timeout。

背景：kurigram 的 ``SQLiteStorage`` 默认 ``use_wal=False``，``open()`` 会执行
``PRAGMA journal_mode=DELETE``。DELETE（回滚日志）模式下**读会阻塞写**，
而转发开启后 pyrogram 会为每条消息写 peers 缓存 —— 只要还有一个残留连接，
写入就会抛 ``OperationalError: database is locked``。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pyrogram.storage.sqlite_storage import SQLiteStorage

from tg_assistant.client import (
    SESSION_BUSY_TIMEOUT_MS,
    _patch_sqlite_storage,
    _use_wal_storage,
)


class _FakeClient:
    """只需要一个带 .storage 属性的对象。"""

    def __init__(self, storage: SQLiteStorage) -> None:
        self.storage = storage


def test_use_wal_storage_sets_flag(tmp_path: Path) -> None:
    storage = SQLiteStorage("acct", workdir=tmp_path)
    assert storage.use_wal is False, "pyrogram 默认就是 False，这里只是确认前提"

    _use_wal_storage(_FakeClient(storage))

    assert storage.use_wal is True


def test_use_wal_storage_tolerates_missing_storage() -> None:
    """存储实现换了（没有 use_wal）也不该炸。"""

    class _Bare:
        pass

    _use_wal_storage(_Bare())


@pytest.mark.asyncio
async def test_open_uses_wal_and_long_busy_timeout(tmp_path: Path) -> None:
    """打开会话库后：journal_mode 应为 WAL，busy_timeout 应被调大。

    pyrogram 建连接时写死 ``timeout=1``（busy 只等 1 秒），
    这里验证我们的补丁把它提到了 ``SESSION_BUSY_TIMEOUT_MS``。
    """
    _patch_sqlite_storage()

    storage = SQLiteStorage("acct", workdir=tmp_path)
    _use_wal_storage(_FakeClient(storage))

    await storage.open()
    try:
        mode = storage.conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"

        busy = storage.conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert busy == SESSION_BUSY_TIMEOUT_MS
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_patch_is_idempotent(tmp_path: Path) -> None:
    """重复调用不应把 open 层层包裹（否则每层都会多一次 PRAGMA）。"""
    _patch_sqlite_storage()
    first = SQLiteStorage.open
    _patch_sqlite_storage()
    assert SQLiteStorage.open is first


@pytest.mark.asyncio
async def test_wal_survives_reopen(tmp_path: Path) -> None:
    """WAL 是写在库头里的持久属性：重开后仍是 WAL。

    这很重要 —— 只要曾经切到 WAL，之后即使有残留连接也不会再退回 DELETE 模式。
    """
    _patch_sqlite_storage()

    storage = SQLiteStorage("acct", workdir=tmp_path)
    _use_wal_storage(_FakeClient(storage))
    await storage.open()
    await storage.close()

    reopened = SQLiteStorage("acct", workdir=tmp_path)
    _use_wal_storage(_FakeClient(reopened))
    await reopened.open()
    try:
        mode = reopened.conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_open_succeeds_with_lingering_connection(tmp_path: Path) -> None:
    """回归：有残留连接时 open() 仍应成功。

    这是「一打开转发就锁数据库」的核心场景 ——

    * pyrogram 默认 ``use_wal=False`` → DELETE 模式，**读会阻塞写**；
      此时只要还有一个没关干净的连接（比如上一轮被强杀的 client）持有读事务，
      重开会话库就会抛 ``OperationalError: database is locked``。
    * 转发开启后 pyrogram 为每条消息写 peers 缓存，写入频率陡增，
      所以这个问题只在「打开转发」之后才暴露出来。

    实测：把上面两处默认值修掉（WAL + busy_timeout）后，同样的残留连接下
    ``open()`` 可以正常完成。
    """
    _patch_sqlite_storage()

    # 1. 先无竞争地打开一次，让库切到 WAL
    first = SQLiteStorage("acct", workdir=tmp_path)
    _use_wal_storage(_FakeClient(first))
    await first.open()
    await first.close()

    db_path = tmp_path / "acct.session"
    assert db_path.is_file()

    # 2. 模拟残留连接：持有读事务不放
    lingering = sqlite3.connect(str(db_path), timeout=1, check_same_thread=False)
    lingering.execute("BEGIN")
    lingering.execute("SELECT number FROM version").fetchone()
    try:
        # 3. 再次打开 —— 修复前这里会抛 database is locked
        storage = SQLiteStorage("acct", workdir=tmp_path)
        _use_wal_storage(_FakeClient(storage))
        await storage.open()
        try:
            mode = storage.conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert str(mode).lower() == "wal"

            busy = storage.conn.execute("PRAGMA busy_timeout").fetchone()[0]
            assert busy == SESSION_BUSY_TIMEOUT_MS
        finally:
            await storage.close()
    finally:
        lingering.rollback()
        lingering.close()
