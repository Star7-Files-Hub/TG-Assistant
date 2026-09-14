"""登录流程的生命周期顺序约束。

背景：``login_account()`` 在扫码成功后要把 ``session_string`` 备份到 PostgreSQL。
部署版把这一步放在了 ``finally``（也就是 ``client.stop()``）**之后**，看起来没问题，
实际上是死代码：

``Client.stop()`` → ``terminate()`` + ``disconnect()``，而 ``disconnect()`` 会执行
``await self.storage.close()``；``SQLiteStorage.close()`` 只是 ``self.conn.close()``，
**不会**把 ``self.conn`` 置空。于是之后任何一次 ``export_session_string()`` 都会走到
``self.conn.execute(...)``，抛 ``sqlite3.ProgrammingError: Cannot operate on a closed
database``（见 ``tests/test_client.py::test_export_after_close_is_impossible`` 的实测）。

部署版用 ``except Exception`` 把异常吞成一条 warning，所以它从来没备份成功过，
而日志上看起来只是"偶发失败"。

因此这里钉住两件事：
1. 导出必须发生在 ``stop()`` 之前；
2. 导出失败不能影响登录本身，也不能往 PG 里写 ``None``。
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from tg_assistant import client as client_mod
from tg_assistant import runner as runner_mod
from tg_assistant.paths import Paths
from tg_assistant.store import Store


class _FakeMe:
    id = 424242
    username = "alice"
    first_name = "张"
    last_name = "三"
    phone_number = "+8613800000000"


class _FakeLoginClient:
    """只记录生命周期调用顺序，不联网。

    ``connect()`` 同时把 ``is_initialized`` 置 True —— 与 pyrogram 一致
    （``connect`` 就是它的 initialize 入口），这样 ``finally`` 里才会走
    ``stop()`` 分支而不是 ``disconnect()`` 分支。
    """

    def __init__(self, events: list[str], *, export_error: Exception | None = None) -> None:
        self.name = "acct"
        self._events = events
        self._export_error = export_error
        self.is_initialized = False
        self.is_connected = False

    async def connect(self) -> None:
        self._events.append("connect")
        self.is_connected = True
        self.is_initialized = True

    async def get_me(self) -> Any:
        self._events.append("get_me")
        return _FakeMe()

    async def export_session_string(self) -> str:
        self._events.append("export")
        if self._export_error is not None:
            raise self._export_error
        return "SESSION-STRING"

    async def stop(self, block: bool = True) -> None:
        self._events.append("stop")
        self.is_initialized = False
        self.is_connected = False

    async def disconnect(self) -> None:
        self._events.append("disconnect")
        self.is_initialized = False
        self.is_connected = False


class _FakeQrSession:
    """替身扫码会话：``run()`` 直接返回，不做任何网络交互。"""

    def __init__(self, client: Any, alog: Any, **kwargs: Any) -> None:
        self.client = client

    async def run(self) -> Any:
        return object()


@pytest.fixture
def settings() -> Any:
    return types.SimpleNamespace(api_id=1, api_hash="hash", workers=1)


def _wire(monkeypatch: pytest.MonkeyPatch, fake_client: _FakeLoginClient) -> list[tuple[str, str]]:
    """把登录流程里所有会联网的依赖换成替身，并捕获 PG 备份调用。"""
    saved: list[tuple[str, str]] = []

    monkeypatch.setattr(runner_mod, "build_client", lambda *a, **k: fake_client)
    monkeypatch.setattr(runner_mod, "QrLoginSession", _FakeQrSession)
    monkeypatch.setattr(runner_mod, "resolve_proxy", lambda *a, **k: None)
    monkeypatch.setattr(
        runner_mod, "device_fingerprint", lambda *a, **k: {"device_model": "TestDevice"}
    )
    # runner 内部是「用时才 import」，所以补丁要打在 client 模块上
    monkeypatch.setattr(
        client_mod, "_save_pg_session_string", lambda name, value: saved.append((name, value))
    )
    return saved


async def test_session_export_happens_before_client_stop(
    monkeypatch: pytest.MonkeyPatch, store: Store, paths: Paths, settings: Any
) -> None:
    """回归：导出 session_string 必须在 ``stop()`` 之前。

    如果谁把导出挪到 ``finally`` 之后（部署版就是这么写的），
    ``export`` 会排在 ``stop`` 后面 —— 这一步在真机上必然抛
    ``ProgrammingError``，PG 备份静默失效。
    """
    events: list[str] = []
    fake_client = _FakeLoginClient(events)
    saved = _wire(monkeypatch, fake_client)

    result = await runner_mod.login_account(
        "acct", store, settings, renderer=None  # type: ignore[arg-type]
    )

    assert "export" in events, f"根本没有导出 session_string：{events}"
    assert "stop" in events, f"客户端没有被停止：{events}"
    assert events.index("export") < events.index("stop"), (
        "导出 session_string 必须发生在 client.stop() 之前；"
        f"实际顺序：{events}（stop 之后 storage 已关闭，导出必然失败）"
    )
    assert saved == [("acct", "SESSION-STRING")]
    assert result.user_id == _FakeMe.id


async def test_export_failure_does_not_break_login_or_write_pg(
    monkeypatch: pytest.MonkeyPatch, store: Store, paths: Paths, settings: Any
) -> None:
    """导出失败只降级为「不备份」，不能连累登录，也不能把 None 写进 PG。"""
    events: list[str] = []
    fake_client = _FakeLoginClient(events, export_error=RuntimeError("boom"))
    saved = _wire(monkeypatch, fake_client)

    result = await runner_mod.login_account(
        "acct", store, settings, renderer=None  # type: ignore[arg-type]
    )

    assert result.user_id == _FakeMe.id, "导出失败不应影响登录结果"
    assert saved == [], "导出失败时不应写入 PostgreSQL"
    assert "stop" in events, "导出失败也要照常停止客户端"
