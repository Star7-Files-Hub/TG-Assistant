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
        self.run_kwargs: dict[str, Any] = {}
        self.started = asyncio.Event()
        self._stop = asyncio.Event()
        self.shutdown_requested = False
        _FakeMultiRunner.instances.append(self)

    async def run(self, names: list[str], **kwargs: Any) -> None:
        self.names = list(names)
        self.run_kwargs = dict(kwargs)
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


# --------------------------------------------------------------------------- #
# --web 模式下接管 CLI 建好的 runner
# --------------------------------------------------------------------------- #


def _adoption_manager(
    monkeypatch: pytest.MonkeyPatch,
    *,
    runner: Any,
    accounts: list[str] | None,
    enabled: list[str],
    run_options: dict[str, Any] | None = None,
) -> RuntimeManager:
    """造一个带外部 runner 的 manager（模拟 ``tg-assistant run --web``）。"""
    monkeypatch.setattr("tg_assistant.runner.MultiRunner", _FakeMultiRunner)

    registry = types.SimpleNamespace(
        enabled_accounts=[types.SimpleNamespace(name=n) for n in enabled]
    )
    state = types.SimpleNamespace(
        settings=types.SimpleNamespace(),
        paths=types.SimpleNamespace(),
        store=types.SimpleNamespace(load_registry=lambda: registry),
        web_settings=types.SimpleNamespace(heartbeat_interval=7.0, log_history_size=50),
    )
    return RuntimeManager(
        state,
        runner=runner,
        initial_accounts=accounts,
        run_options=run_options,
    )


async def test_adopt_external_runner_makes_status_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归：不接管的话 ``self._runner`` 永远是 None。

    表现是 ``/api/status`` 恒报 ``running: false`` —— 侧边栏那个「未运行」标签
    永远不变绿；同时优选 IP 复用不到已登录的 client，只能另建一个去抢同一个
    ``.session``，于是每 60 秒一次 ``database is locked``。
    """
    external = _FakeMultiRunner(None, None)
    manager = _adoption_manager(
        monkeypatch, runner=external, accounts=["小白", "SevenStar"], enabled=[]
    )
    assert not manager.is_running, "接管前不该是运行态"

    await asyncio.wait_for(manager.startup(), DEADLOCK_GUARD)

    assert manager.is_running is True
    await asyncio.wait_for(external.started.wait(), DEADLOCK_GUARD)
    assert external.names == ["小白", "SevenStar"]
    assert external is manager._runner, "必须是**同一个** runner 对象，不是新建的"

    await asyncio.wait_for(manager.stop(), DEADLOCK_GUARD)


async def test_adopt_uses_registry_when_no_accounts_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI 没给账号列表时退回「注册表里所有已启用账号」。"""
    external = _FakeMultiRunner(None, None)
    manager = _adoption_manager(
        monkeypatch, runner=external, accounts=None, enabled=["甲", "乙"]
    )

    await asyncio.wait_for(manager.startup(), DEADLOCK_GUARD)

    await asyncio.wait_for(external.started.wait(), DEADLOCK_GUARD)
    assert external.names == ["甲", "乙"]

    await asyncio.wait_for(manager.stop(), DEADLOCK_GUARD)


async def test_no_external_runner_stays_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 ``--web`` 路径（``runner=None``）不该被凭空启动。"""
    manager = _adoption_manager(monkeypatch, runner=None, accounts=None, enabled=["甲"])

    await asyncio.wait_for(manager.startup(), DEADLOCK_GUARD)

    assert not manager.is_running
    assert _FakeMultiRunner.instances == []


async def test_adopt_without_any_account_stays_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一个账号都没有时不启 runner —— 否则会白起一个空任务。"""
    external = _FakeMultiRunner(None, None)
    manager = _adoption_manager(monkeypatch, runner=external, accounts=None, enabled=[])

    await asyncio.wait_for(manager.startup(), DEADLOCK_GUARD)

    assert not manager.is_running
    assert external.names == []


async def test_adopted_runner_cannot_be_started_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """接管之后再点「启动」必须被拒，而不是拉起第二套 runner。

    这是「同一条消息被转发两次」的直接来源。
    """
    external = _FakeMultiRunner(None, None)
    manager = _adoption_manager(
        monkeypatch, runner=external, accounts=["小白"], enabled=[]
    )
    await asyncio.wait_for(manager.startup(), DEADLOCK_GUARD)
    await asyncio.wait_for(external.started.wait(), DEADLOCK_GUARD)

    result = await asyncio.wait_for(manager.start(["小白"]), DEADLOCK_GUARD)

    assert not result["ok"]
    assert "已经在运行中" in result["message"]
    assert len(_FakeMultiRunner.instances) == 1, "不该多出一个 runner"

    await asyncio.wait_for(manager.stop(), DEADLOCK_GUARD)


async def test_run_options_are_passed_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI 的 ``--heartbeat`` 等参数要透传给 ``MultiRunner.run()``。

    ``heartbeat`` 是延迟到 ``_supervise`` 里才补的：``cli.py`` 在
    ``create_app()`` 返回**之后**才把真正的 WebSettings 换进 ``app.state``，
    构造 ``RuntimeManager`` 时读会拿到默认值。
    """
    external = _FakeMultiRunner(None, None)
    manager = _adoption_manager(
        monkeypatch,
        runner=external,
        accounts=["小白"],
        enabled=[],
        run_options={"restart_delay": 3.0, "max_restarts": 5},
    )

    await asyncio.wait_for(manager.startup(), DEADLOCK_GUARD)
    await asyncio.wait_for(external.started.wait(), DEADLOCK_GUARD)

    assert external.run_kwargs["restart_delay"] == 3.0
    assert external.run_kwargs["max_restarts"] == 5
    # 来自 app.state.web_settings，且必须是**接管之后**读到的那个值。
    assert external.run_kwargs["heartbeat"] == 7.0
    assert external.run_kwargs["install_signals"] is False, (
        "Web 模式下信号由 uvicorn 处理，runner 不该再装一遍"
    )

    await asyncio.wait_for(manager.stop(), DEADLOCK_GUARD)


async def test_shutdown_stops_adopted_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """服务退出时要优雅停掉接管来的 runner（关 client、排空通知队列）。"""
    external = _FakeMultiRunner(None, None)
    manager = _adoption_manager(
        monkeypatch, runner=external, accounts=["小白"], enabled=[]
    )
    await asyncio.wait_for(manager.startup(), DEADLOCK_GUARD)
    await asyncio.wait_for(external.started.wait(), DEADLOCK_GUARD)

    await asyncio.wait_for(manager.shutdown(), DEADLOCK_GUARD)

    assert external.shutdown_requested
    assert not manager.is_running
