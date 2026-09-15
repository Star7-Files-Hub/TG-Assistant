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
import sys
import time
import types
from typing import Any

import pytest

from tg_assistant.config import AccountRecord
from tg_assistant.web.runtime import RuntimeManager, cf_backoff_delay

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


# --------------------------------------------------------------------------- #
# 账号列表里的「开了哪些功能」
# --------------------------------------------------------------------------- #


def _manager_with_configs(monkeypatch: pytest.MonkeyPatch, configs: dict[str, Any]):
    """造一个 store：按名字返回给定配置（不存在的名字抛错）。"""
    monkeypatch.setattr("tg_assistant.runner.MultiRunner", _FakeMultiRunner)
    registry = types.SimpleNamespace(
        accounts=[AccountRecord(name=n) for n in configs]
    )

    def load_config(name: str, create: bool = False):
        cfg = configs.get(name)
        if cfg is None:
            raise FileNotFoundError(name)
        return cfg

    state = types.SimpleNamespace(
        settings=types.SimpleNamespace(),
        paths=types.SimpleNamespace(),
        store=types.SimpleNamespace(
            load_registry=lambda: registry,
            load_account_config=load_config,
            has_session=lambda name: True,
        ),
        web_settings=types.SimpleNamespace(heartbeat_interval=1.0, log_history_size=50),
    )
    return RuntimeManager(state)


def _config(*, cf: bool = False, notify: bool = False, red_packet: bool = False):
    return types.SimpleNamespace(
        cloudflare_ip=types.SimpleNamespace(enabled=cf),
        notify=types.SimpleNamespace(enabled=notify),
        red_packet=types.SimpleNamespace(enabled=red_packet),
    )


def test_account_status_without_features_stays_cheap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/api/status`` 是 5 秒一次轮询的，默认不该去读配置文件。"""
    called: list[str] = []

    def load_config(name: str, create: bool = False):
        called.append(name)
        return _config(cf=True)

    monkeypatch.setattr("tg_assistant.runner.MultiRunner", _FakeMultiRunner)
    registry = types.SimpleNamespace(accounts=[AccountRecord(name="甲")])
    state = types.SimpleNamespace(
        settings=types.SimpleNamespace(),
        paths=types.SimpleNamespace(),
        store=types.SimpleNamespace(
            load_registry=lambda: registry,
            load_account_config=load_config,
            has_session=lambda name: True,
        ),
        web_settings=types.SimpleNamespace(heartbeat_interval=1.0, log_history_size=50),
    )
    manager = RuntimeManager(state)

    items = manager.account_status()

    assert called == [], "默认不该读配置"
    assert "features" not in items[0]


def test_account_status_reports_feature_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：面板要靠这个把下拉框默认选到真的配了功能的账号。

    不这么做的话多账号时永远选中第一个 —— 而功能常配在另一个账号上，
    用户打开页面看到空配置 + 「未运行」，会以为功能坏了（实测误判过）。
    """
    manager = _manager_with_configs(
        monkeypatch,
        {
            "小白": _config(),
            "SevenStar": _config(cf=True),
        },
    )

    items = manager.account_status(with_features=True)
    by_name = {i["name"]: i for i in items}

    assert by_name["小白"]["features"] == {
        "cloudflare_ip": False,
        "notify": False,
        "red_packet": False,
    }
    assert by_name["SevenStar"]["features"]["cloudflare_ip"] is True


def test_broken_config_does_not_break_the_account_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """某个账号的配置读不出来时，列表仍要能返回，标记按「全没开」处理。"""
    manager = _manager_with_configs(
        monkeypatch,
        {
            "好的": _config(notify=True),
            "坏的": None,  # load_account_config 会抛 FileNotFoundError
        },
    )

    items = manager.account_status(with_features=True)
    by_name = {i["name"]: i for i in items}

    assert by_name["好的"]["features"]["notify"] is True
    assert by_name["坏的"]["features"] == {
        "cloudflare_ip": False,
        "notify": False,
        "red_packet": False,
    }


# --------------------------------------------------------------------------- #
# 优选 IP 定时任务的「失败退避」
#
# 背景：失败**不写** ``cloudflare_ip_last_run``（那是「跑过」的语义，写它会让
# 面板的「上次更新」说谎）。代价是失败账号会一直处于「已到期」状态 —— 没有退避
# 就是一个持续失败的账号每 60 秒重试一次，而每次重试都可能另建 client 去抢
# 同一个 ``.session``，正是当初 189 条 ``database is locked`` 的放大器。
# --------------------------------------------------------------------------- #


class _VirtualTime:
    """假时钟：``sleep(s)`` 把时间推进 ``s`` 秒，而不是真的睡。"""

    def __init__(self) -> None:
        self.now = 1_700_000_000.0
        self.ticks = 0
        self.limit: int | None = None

    async def sleep(self, seconds: float) -> None:
        self.ticks += 1
        if self.limit is not None and self.ticks > self.limit:
            # 循环只在 ``await asyncio.sleep`` 上捕获 CancelledError，用它收尾最干净。
            raise asyncio.CancelledError
        self.now += seconds


def _patch_loop_clock(monkeypatch: pytest.MonkeyPatch, clock: _VirtualTime) -> None:
    """把调度循环里的 ``asyncio.sleep`` / ``time.time`` 换成假时钟。

    用 ``__getattr__`` 代理其余属性，避免把整个 asyncio 模块换掉（循环里只用到
    ``sleep`` 和 ``CancelledError``，但换掉整个模块太容易踩到别的东西）。
    """
    runtime_mod = sys.modules["tg_assistant.web.runtime"]

    class _TimeShim:
        def __getattr__(self, item: str) -> Any:
            return getattr(time, item)

        @staticmethod
        def time() -> float:
            return clock.now

    class _AsyncioShim:
        def __getattr__(self, item: str) -> Any:
            return getattr(asyncio, item)

        @staticmethod
        async def sleep(seconds: float) -> None:
            await clock.sleep(seconds)

    monkeypatch.setattr(runtime_mod, "time", _TimeShim())
    monkeypatch.setattr(runtime_mod, "asyncio", _AsyncioShim())


class _LoopStore:
    """只提供调度循环用到的那几个方法。"""

    def __init__(self, cf_config: Any) -> None:
        self.cf_config = cf_config
        self.state: dict[str, Any] = {}
        self.saved = 0

    def load_registry(self) -> Any:
        return types.SimpleNamespace(
            accounts=[types.SimpleNamespace(name="acct", enabled=True)]
        )

    def load_account_config(self, name: str, create: bool = False) -> Any:
        return types.SimpleNamespace(cloudflare_ip=self.cf_config)

    def load_state(self, name: str) -> dict[str, Any]:
        return dict(self.state)

    def save_state(self, name: str, state: dict[str, Any]) -> None:
        self.state = dict(state)
        self.saved += 1


def _loop_manager(monkeypatch: pytest.MonkeyPatch, store: _LoopStore) -> RuntimeManager:
    state = types.SimpleNamespace(
        settings=types.SimpleNamespace(),
        paths=types.SimpleNamespace(),
        store=store,
        web_settings=types.SimpleNamespace(heartbeat_interval=1.0, log_history_size=50),
    )
    return RuntimeManager(state)


def _cf_config(*, enabled: bool = True, interval_hours: float = 1.0) -> Any:
    return types.SimpleNamespace(
        enabled=enabled, interval_hours=interval_hours, split_by_isp=False
    )


def test_cf_backoff_delay_doubles_then_caps() -> None:
    """60s → 120s → 240s 翻倍，封顶一个调度周期。"""
    assert cf_backoff_delay(0, 3600.0) == 0.0, "没失败过就不该退避"
    assert cf_backoff_delay(1, 3600.0) == 60.0
    assert cf_backoff_delay(2, 3600.0) == 120.0
    assert cf_backoff_delay(3, 3600.0) == 240.0
    assert cf_backoff_delay(4, 3600.0) == 480.0
    # 封顶：退避超过一个正常调度周期就没意义了（到点本来也该再跑）。
    assert cf_backoff_delay(20, 3600.0) == 3600.0
    # 周期比基数还短时，至少退避一个基数，否则「退避」等于没退。
    assert cf_backoff_delay(5, 600.0) == 600.0


def test_cf_backoff_gate_and_reset(manager: RuntimeManager) -> None:
    """退避闸本身：到期后仍要放行过，失败次数累积，成功清零。"""
    assert manager._cf_backoff_allows("a", 1000.0) is True

    assert manager._cf_note_failure("a", 1000.0, 3600.0) == 60.0
    assert manager._cf_backoff_allows("a", 1030.0) is False, "还没到点就不该再跑"
    assert manager._cf_backoff_allows("a", 1060.0) is True

    assert manager._cf_note_failure("a", 1060.0, 3600.0) == 120.0, "第二次失败应翻倍"

    manager._cf_note_success("a")
    assert manager._cf_backoff_allows("a", 1061.0) is True, "成功后必须立刻恢复"
    assert "a" not in manager._cf_backoff


async def test_cf_loop_backs_off_after_repeated_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归：持续失败时不能每 60 秒重试一次。

    6 次检查（每次间隔 60 秒）本该跑 6 次；有退避后只在第 1、2、4 次跑
    （第 3、5、6 次被 120s / 240s 的退避挡住）。
    """
    clock = _VirtualTime()
    _patch_loop_clock(monkeypatch, clock)
    store = _LoopStore(_cf_config(interval_hours=1.0))
    manager = _loop_manager(monkeypatch, store)

    attempts: list[float] = []

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        attempts.append(clock.now)
        raise RuntimeError("session 被占用")

    monkeypatch.setattr(
        "tg_assistant.cloudflare_ip.make_message_source_from_account", _boom
    )

    clock.limit = 6
    await asyncio.wait_for(manager._cloudflare_ip_loop(), DEADLOCK_GUARD)

    assert len(attempts) == 3, f"退避没生效，重试了 {len(attempts)} 次（期望 3 次）"
    # 失败不落盘：「上次更新」必须保持原样，否则面板会显示成「刚跑过」。
    assert store.saved == 0
    assert "cloudflare_ip_last_run" not in store.state
    assert manager._cf_backoff["acct"][0] == 3


async def test_cf_loop_backs_off_when_update_itself_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第二条失败路径（``fetch_and_update`` 抛异常）同样要退避。

    这条分支上 client 是建成功了的 —— 如果只给「建 client 失败」加退避，
    一个抓不到 IP / 写 DNS 被拒的账号仍会每 60 秒新建一个 client 抢 .session。
    """
    clock = _VirtualTime()
    _patch_loop_clock(monkeypatch, clock)
    store = _LoopStore(_cf_config(interval_hours=1.0))
    manager = _loop_manager(monkeypatch, store)

    stopped: list[bool] = []

    class _FakeClient:
        async def stop(self, block: bool = False) -> None:
            stopped.append(True)

    async def _fake_source(*args: Any, **kwargs: Any) -> Any:
        return _FakeClient(), object()

    attempts: list[float] = []

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        attempts.append(clock.now)
        raise RuntimeError("Cloudflare 拒绝了写入")

    monkeypatch.setattr(
        "tg_assistant.cloudflare_ip.make_message_source_from_account", _fake_source
    )
    monkeypatch.setattr("tg_assistant.cloudflare_ip.fetch_and_update", _boom)
    monkeypatch.setattr("tg_assistant.proxy.resolve_proxy", lambda *a, **k: None)

    clock.limit = 6
    await asyncio.wait_for(manager._cloudflare_ip_loop(), DEADLOCK_GUARD)

    assert len(attempts) == 3, f"退避没生效，重试了 {len(attempts)} 次（期望 3 次）"
    assert len(stopped) == 3, "临时 client 每次都必须停掉，不能泄漏"
    assert store.saved == 0


async def test_cf_loop_drops_backoff_when_feature_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """功能被关掉就把退避丢掉，别让它留着影响下次重新开启。"""
    clock = _VirtualTime()
    _patch_loop_clock(monkeypatch, clock)
    store = _LoopStore(_cf_config(enabled=False))
    manager = _loop_manager(monkeypatch, store)
    manager._cf_backoff["acct"] = (5, clock.now + 99999)

    clock.limit = 1
    await asyncio.wait_for(manager._cloudflare_ip_loop(), DEADLOCK_GUARD)

    assert "acct" not in manager._cf_backoff
