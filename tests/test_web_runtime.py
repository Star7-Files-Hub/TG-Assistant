"""``RuntimeManager`` 的单账号启停。

重点钉住一个 P0：**不能在持有 ``self._lock`` 时调用 ``start()``**。
``asyncio.Lock`` 不可重入，部署版的 ``stop_account`` 就是这么写的 ——

    async with self._lock:
        ...
        return await self.start(remaining)   # start() 里又 async with self._lock

于是只要"还有其他账号在跑"，这个请求就永久挂住，并且那把锁再也不会释放，
之后所有启停请求一起卡死。所以下面每个用例都套了 ``asyncio.wait_for``：
死锁的表现就是超时，而不是某个断言失败。
"""

from __future__ import annotations

import asyncio
import types
from typing import Any

import pytest

from tg_assistant.web.runtime import RuntimeManager

#: 单个用例里允许的最长等待；死锁会以超时形式暴露。
DEADLOCK_GUARD = 5.0


class _FakeMultiRunner:
    """替代真实的 MultiRunner：只记录账号集合，可被优雅停止。"""

    instances: list["_FakeMultiRunner"] = []

    def __init__(self, store: Any, settings: Any) -> None:
        self.store = store
        self.settings = settings
        self.names: list[str] = []
        self.started = asyncio.Event()
        self._stop = asyncio.Event()
        self.shutdown_requested = False
        _FakeMultiRunner.instances.append(self)

    async def run(self, names: list[str], **kwargs: Any) -> None:
        self.names = list(names)
        self.started.set()
        await self._stop.wait()

    def request_shutdown(self) -> None:
        self.shutdown_requested = True
        self._stop.set()

    def snapshot(self) -> list[dict[str, Any]]:
        return [{"account": name} for name in self.names]


@pytest.fixture(autouse=True)
def _reset_instances():
    _FakeMultiRunner.instances.clear()
    yield
    _FakeMultiRunner.instances.clear()


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> RuntimeManager:
    monkeypatch.setattr("tg_assistant.runner.MultiRunner", _FakeMultiRunner)

    state = types.SimpleNamespace(
        settings=types.SimpleNamespace(),
        paths=types.SimpleNamespace(),
        store=types.SimpleNamespace(load_registry=lambda: types.SimpleNamespace(accounts=[])),
        web_settings=types.SimpleNamespace(heartbeat_interval=1.0, log_history_size=50),
    )
    return RuntimeManager(state)


async def _started(manager: RuntimeManager, names: list[str]) -> _FakeMultiRunner:
    result = await asyncio.wait_for(manager.start(names), DEADLOCK_GUARD)
    assert result["ok"], result
    runner = _FakeMultiRunner.instances[-1]
    await asyncio.wait_for(runner.started.wait(), DEADLOCK_GUARD)
    return runner


async def test_stop_account_does_not_deadlock(manager: RuntimeManager) -> None:
    """回归：停止单个账号时，剩下的账号要能重新拉起来，而且不能死锁。

    部署版在持锁状态下调用 ``start()``，只要还有其他账号在跑就会永久挂住。
    """
    await _started(manager, ["a", "b"])

    result = await asyncio.wait_for(manager.stop_account("a"), DEADLOCK_GUARD)

    assert result["ok"], result
    assert result["accounts"] == ["b"]
    restarted = _FakeMultiRunner.instances[-1]
    await asyncio.wait_for(restarted.started.wait(), DEADLOCK_GUARD)
    assert restarted.names == ["b"], "剩下的账号没有被重新拉起"

    await asyncio.wait_for(manager.stop(), DEADLOCK_GUARD)


async def test_stop_last_account_leaves_nothing_running(manager: RuntimeManager) -> None:
    """只剩一个账号时，停掉它就是全停，不需要再重启任何东西。"""
    await _started(manager, ["solo"])

    result = await asyncio.wait_for(manager.stop_account("solo"), DEADLOCK_GUARD)

    assert result["ok"], result
    assert not manager.is_running
    assert len(_FakeMultiRunner.instances) == 1, "不该多拉起一个空的 runner"


async def test_stop_account_requires_it_to_be_running(manager: RuntimeManager) -> None:
    await _started(manager, ["a"])

    result = await asyncio.wait_for(manager.stop_account("ghost"), DEADLOCK_GUARD)

    assert not result["ok"]
    assert "未在运行" in result["message"]
    assert manager.is_running, "拒绝一个请求不应该影响正在跑的账号"

    await asyncio.wait_for(manager.stop(), DEADLOCK_GUARD)


async def test_stop_account_without_runner(manager: RuntimeManager) -> None:
    result = await asyncio.wait_for(manager.stop_account("a"), DEADLOCK_GUARD)
    assert not result["ok"]


async def test_start_account_joins_running_batch(manager: RuntimeManager) -> None:
    """单独启动一个账号时，已经在跑的要一并重启，并且带上新账号。"""
    await _started(manager, ["a"])

    result = await asyncio.wait_for(manager.start_account("b"), DEADLOCK_GUARD)

    assert result["ok"], result
    restarted = _FakeMultiRunner.instances[-1]
    await asyncio.wait_for(restarted.started.wait(), DEADLOCK_GUARD)
    assert restarted.names == ["a", "b"]

    await asyncio.wait_for(manager.stop(), DEADLOCK_GUARD)


async def test_start_account_rejects_already_running(manager: RuntimeManager) -> None:
    await _started(manager, ["a"])

    result = await asyncio.wait_for(manager.start_account("a"), DEADLOCK_GUARD)

    assert not result["ok"]
    assert "已在运行" in result["message"]
    assert len(_FakeMultiRunner.instances) == 1, "重复启动不该把账号重启一遍"

    await asyncio.wait_for(manager.stop(), DEADLOCK_GUARD)


async def test_start_account_when_idle(manager: RuntimeManager) -> None:
    result = await asyncio.wait_for(manager.start_account("a"), DEADLOCK_GUARD)

    assert result["ok"], result
    assert result["accounts"] == ["a"]

    await asyncio.wait_for(manager.stop(), DEADLOCK_GUARD)


async def test_stop_uses_graceful_shutdown(manager: RuntimeManager) -> None:
    """停止必须走 ``request_shutdown()`` 的优雅路径，而不是直接 cancel。

    直接 cancel 会跳过 MultiRunner 的清理（关 client、排空通知队列），
    重新 start 时新旧 client 会争抢同一个 session 文件。
    """
    runner = await _started(manager, ["a"])

    await asyncio.wait_for(manager.stop(), DEADLOCK_GUARD)

    assert runner.shutdown_requested, "应当先请 MultiRunner 自己退出"
