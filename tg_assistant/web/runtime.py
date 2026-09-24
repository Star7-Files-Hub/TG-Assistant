"""Web 运行期管理器。

负责启动 / 停止后台账号任务，收集运行状态与日志，供 API 与 WebSocket 消费。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from tg_assistant.logging_setup import get_logger, render_extra

log = get_logger("web.runtime")

#: 停止账号时的优雅退出等待上限（秒），超过才强杀。
STOP_TIMEOUT = 30.0

#: 优选 IP 定时任务**失败后**的重试退避基数（秒），每次连续失败翻倍。
CF_RETRY_BACKOFF_BASE = 60.0


def cf_backoff_delay(failures: int, interval_sec: float) -> float:
    """连续失败 ``failures`` 次后，下次允许重试要再等多少秒。

    60s → 120s → 240s … 翻倍，上限 ``max(CF_RETRY_BACKOFF_BASE, interval_sec)``。

    为什么要封顶：失败**不写** ``cloudflare_ip_last_run``（那是「跑过」的语义，
    写它会让面板的「上次更新」说谎），所以失败账号会一直处于「已到期」状态。
    退避超过一个正常调度周期就没意义了 —— 到那个点本来也该再跑一次。
    """
    if failures <= 0:
        return 0.0
    cap = max(CF_RETRY_BACKOFF_BASE, interval_sec)
    return min(CF_RETRY_BACKOFF_BASE * (2 ** (failures - 1)), cap)


@dataclass
class AccountRuntime:
    """单个账号的运行状态。"""

    name: str
    running: bool = False
    error: Optional[str] = None
    started_at: Optional[float] = None
    stats: dict[str, Any] = field(default_factory=dict)
    pid: Optional[int] = None


class RuntimeManager:
    """管理 MultiRunner 的生命周期。

    runner 有两个来源：

    - ``tg-assistant web``：面板自己建（``runner=None``，走 :meth:`start`）；
    - ``tg-assistant run --web``：CLI 建好之后交给本类**接管**（``runner=<那个对象>``）。

    第二种必须接管而不是各建一个 —— 见 :meth:`_adopt_external_runner`。
    """

    def __init__(
        self,
        app_state: Any,
        *,
        runner: Any = None,
        initial_accounts: list[str] | None = None,
        run_options: dict[str, Any] | None = None,
    ) -> None:
        self.state = app_state
        self.settings: Any = app_state.settings
        self.paths: Any = app_state.paths
        self.store: Any = app_state.store
        self._runner: Any = None
        self._runner_task: Optional[asyncio.Task[None]] = None
        self._log_handler: Optional[logging.Handler] = None
        self._recent_logs: list[dict[str, Any]] = []
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()
        self._shutdown_event = asyncio.Event()
        #: CLI 预建、待接管的 runner 与其账号列表（``--web`` 模式）。
        self._adopted_runner: Any = runner
        self._adopted_accounts: Optional[list[str]] = list(initial_accounts) if initial_accounts else None
        #: 跨账号去重表：**整个 web 进程共用一张**。面板点「启动」会重建 MultiRunner，
        #: 把这张表传进去才不会让去重窗口被重置（``None`` 时 MultiRunner 会自建一张）。
        self._dedupe: Any = None
        #: 「频道 ↔ 群组 同内容」去重表 —— 与 ``_dedupe`` 一样跨面板重建复用。
        self._pair_dedupe: Any = None
        #: 「最近已转发的内容」去重表 —— 同上，跨面板重建复用。
        self._recent_dedupe: Any = None
        #: 透传给 ``MultiRunner.run()`` 的参数（heartbeat / restart_delay / max_restarts）。
        #: 刻意**不**在这里补 heartbeat：``cli.py`` 是在 ``create_app()`` 返回之后
        #: 才把真正的 WebSettings 换进 ``app.state`` 的，此刻读会拿到默认值。
        #: 兜底在 :meth:`_supervise` 里延迟处理。
        self._run_options: dict[str, Any] = dict(run_options or {})
        #: 优选 IP 定时任务的**失败退避**：账号名 → (连续失败次数, 下次允许执行的时间戳)。
        #: 只活在内存里，重启即清空 —— 它不是状态，是「别把日志刷爆」的节流器。
        #: 见 :func:`cf_backoff_delay`。
        self._cf_backoff: dict[str, tuple[int, float]] = {}

    # ------------------------------------------------------------------ #
    async def startup(self) -> None:
        """启动时挂接日志广播、优选 IP 定时任务，并接管 CLI 预建的 runner。"""
        self._log_handler = _BroadcastHandler(self._emit_log)
        self._log_handler.setLevel(logging.DEBUG)
        root = logging.getLogger("tg-assistant")
        root.addHandler(self._log_handler)
        self._cf_ip_task = asyncio.create_task(self._cloudflare_ip_loop(), name="cf-ip-scheduler")
        get_logger().info("Web 运行管理器已就绪", extra={"account": "-", "extra_fields": {}})
        await self._adopt_external_runner()

    async def _adopt_external_runner(self) -> None:
        """接管 ``tg-assistant run --web`` 里 CLI 建好的那个 MultiRunner。

        不接管的后果（都实测过）：

        1. ``self._runner`` 永远是 ``None`` → ``/api/status`` 恒报 ``running: false``，
           面板状态卡永远显示「未运行」；
        2. 优选 IP 复用不到已登录的 client（那段代码判断 ``self._runner is not None``），
           只能退化成 ``make_message_source_from_account()`` 另建一个 client，
           跟正在跑的 runner 抢同一个 ``.session`` → 每 60 秒一次 ``database is locked``；
        3. 面板点「启动」会**再拉起一套** runner，同一账号两个 client 同时登录，
           同一条消息被转发两次。
        """
        runner = self._adopted_runner
        self._adopted_runner = None
        if runner is None:
            return

        # 接管 runner 的同时接管它的跨账号去重表 —— 否则面板之后新起的账号会拿到
        # 另一张表，两个账号各去各的，等于没去重。
        self._dedupe = getattr(runner, "dedupe", None)
        # 「频道 ↔ 群组 同内容」去重表同理。
        self._pair_dedupe = getattr(runner, "pair_dedupe", None)
        # 「最近已转发的内容」去重表同理 —— 换表 = 刚发过的内容又能重发一遍。
        self._recent_dedupe = getattr(runner, "recent_dedupe", None)

        names = self._adopted_accounts
        if not names:
            names = [record.name for record in self.store.load_registry().enabled_accounts]
        if not names:
            get_logger().info(
                "没有可运行的账号，等待在面板中启动",
                extra={"account": "-", "extra_fields": {}},
            )
            return

        async with self._lock:
            await self._launch(runner, names)

    async def _launch(self, runner: Any, names: list[str]) -> None:
        """登记 runner 并起看护任务（必须在持锁时调用）。"""
        self._runner = runner
        self._shutdown_event.clear()
        self._runner_task = asyncio.create_task(self._supervise(runner, names), name="web-runner")
        # 通知 /ws/status 订阅者：已开始运行。
        self.push_event({"type": "status", "running": True, "accounts": names})

    async def shutdown(self) -> None:
        """停止全部账号并卸载日志。"""
        if hasattr(self, "_cf_ip_task") and self._cf_ip_task is not None:
            self._cf_ip_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cf_ip_task
            self._cf_ip_task = None
        await self.stop()
        if self._log_handler is not None:
            root = logging.getLogger("tg-assistant")
            root.removeHandler(self._log_handler)
            self._log_handler = None
        # 通知所有订阅者退出
        for queue in list(self._subscribers):
            with contextlib.suppress(Exception):
                queue.put_nowait({"type": "shutdown"})

    # ------------------------------------------------------------------ #
    # Cloudflare 优选 IP 定时更新
    # ------------------------------------------------------------------ #
    def _cf_backoff_allows(self, name: str, now: float) -> bool:
        """到期之后再过一道退避闸：``False`` 表示这次先别跑。"""
        failures, next_allowed = self._cf_backoff.get(name, (0, 0.0))
        return failures == 0 or now >= next_allowed

    def _cf_note_failure(self, name: str, now: float, interval_sec: float) -> float:
        """记一次失败，返回本次算出的退避秒数（供日志用）。"""
        failures = self._cf_backoff.get(name, (0, 0.0))[0] + 1
        delay = cf_backoff_delay(failures, interval_sec)
        self._cf_backoff[name] = (failures, now + delay)
        return delay

    def _cf_note_success(self, name: str) -> None:
        """跑成功就把退避清零，下次照常按 ``interval_hours`` 调度。"""
        self._cf_backoff.pop(name, None)

    async def _cloudflare_ip_loop(self) -> None:
        """后台循环：检查所有账号的 Cloudflare IP 定时配置，到期执行。"""
        from tg_assistant.cloudflare_ip import (
            fetch_and_update,
            make_message_source,
            make_message_source_from_account,
            persist_run,
        )
        from tg_assistant.proxy import resolve_proxy

        #: 每 60 秒检查一次哪些账号到期了
        CHECK_INTERVAL = 60.0

        while True:
            try:
                await asyncio.sleep(CHECK_INTERVAL)
            except asyncio.CancelledError:
                return

            try:
                registry = self.store.load_registry()
                now = time.time()
                for record in registry.accounts:  # noqa: BLE001 - 循环内单条失败不影响其它账号
                    if not record.enabled:
                        continue
                    try:
                        account_config = self.store.load_account_config(record.name, create=False)
                    except Exception:
                        continue
                    cf_config = account_config.cloudflare_ip
                    if not cf_config.enabled or cf_config.interval_hours <= 0:
                        # 关掉功能就把退避丢掉，别让它留着影响下次重新开启。
                        self._cf_backoff.pop(record.name, None)
                        continue

                    # 检查是否到期
                    state = self.store.load_state(record.name)
                    last_run = state.get("cloudflare_ip_last_run", 0)
                    interval_sec = cf_config.interval_hours * 3600
                    if now - last_run < interval_sec:
                        continue

                    # 失败退避闸。失败**不写** last_run，所以账号会一直「已到期」；
                    # 没有这道闸就是一个持续失败的账号每 60 秒重试一次，而每次重试
                    # 都可能另建 client 去抢同一个 .session —— 正是当初 189 条
                    # ``database is locked`` 的放大器。
                    if not self._cf_backoff_allows(record.name, now):
                        continue

                    log.info(
                        "Cloudflare IP 定时更新触发",
                        extra={"account": record.name, "extra_fields": {"interval_h": cf_config.interval_hours}},
                    )

                    # 获取消息源：**优先复用账号正在跑的那个 client**。
                    # 退化成 make_message_source_from_account() 会另建一个 client
                    # 去抢同一个 .session，结果是每 60 秒一次 database is locked
                    # （实测刷了整整一屏，优选 IP 永远更新不了）。
                    # 这条分支依赖 self._runner 不为 None —— 由
                    # _adopt_external_runner() 在 --web 模式下保证。
                    source = None
                    client_to_stop = None
                    running = self._running_accounts() if self._runner is not None else set()
                    if record.name in running and self._runner is not None:
                        for runner in self._runner.runners.values():
                            if runner.name == record.name and runner.client is not None:
                                source = make_message_source(runner.client)
                                break

                    if source is None:
                        try:
                            client, source = await make_message_source_from_account(
                                record.name, self.store, self.settings
                            )
                            client_to_stop = client
                        except Exception as exc:
                            delay = self._cf_note_failure(record.name, now, interval_sec)
                            log.warning(
                                "Cloudflare IP：无法创建 client",
                                extra={
                                    "account": record.name,
                                    "extra_fields": {
                                        "error": str(exc),
                                        "retry_in_s": round(delay, 1),
                                    },
                                },
                            )
                            continue

                    try:
                        proxy = resolve_proxy(record, self.settings)
                        summary = await fetch_and_update(cf_config, source, state, proxy)
                        # 「上次结果」和「上次更新」都由 persist_run 统一写 ——
                        # 手动触发 / 实时监听走的是同一个函数，三条路径不会各写各的。
                        persist_run(
                            self.store, record.name, state, summary, cf_config, now=now
                        )
                        self._cf_note_success(record.name)
                    except Exception as exc:
                        delay = self._cf_note_failure(record.name, now, interval_sec)
                        log.exception(
                            "Cloudflare IP 定时更新失败",
                            extra={
                                "account": record.name,
                                "extra_fields": {
                                    "error": str(exc),
                                    "retry_in_s": round(delay, 1),
                                },
                            },
                        )
                    finally:
                        if client_to_stop is not None:
                            with contextlib.suppress(Exception):
                                await client_to_stop.stop(block=True)

            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.exception(
                    "Cloudflare IP 定时循环异常",
                    extra={"account": "-", "extra_fields": {"error": str(exc)}},
                )

    # ------------------------------------------------------------------ #
    @property
    def is_running(self) -> bool:
        return self._runner_task is not None and not self._runner_task.done()

    def account_status(self, *, with_features: bool = False) -> list[dict[str, Any]]:
        """全部账号的运行状态。

        ``with_features``
            额外带上每个账号「开了哪些功能」。只有 ``/api/accounts`` 需要 ——
            面板要靠它把下拉框默认选到**真的配了这个功能**的账号上。
            ⚠️ ``/api/status`` 是 5 秒一次的轮询，走这个分支要读每个账号的
            config.json，没必要，所以默认关闭。
        """
        registry = self.store.load_registry()
        result = []
        for record in registry.accounts:
            info = AccountRuntime(name=record.name)
            info.running = record.name in self._running_accounts()
            if info.running and self._runner is not None:
                for snapshot in self._runner.snapshot():
                    if snapshot.get("account") == record.name:
                        info.started_at = snapshot.get("uptime_s")
                        info.stats = snapshot
                        break
            item: dict[str, Any] = {
                "name": info.name,
                "enabled": record.enabled,
                "running": info.running,
                "error": info.error,
                "user": record.label,
                "user_id": record.user_id,
                "username": record.username,
                "display_name": record.display_name,
                "session_exists": self.store.has_session(record.name),
                "proxy": record.proxy.to_url() if record.proxy else None,
                "last_login": record.last_login_at,
                "stats": info.stats,
            }
            if with_features:
                # 面板下拉框的默认选中项要靠这个。
                # 不这么做的话，多账号时永远选中注册表里的第一个 ——
                # 而功能常常只配在另一个账号上，于是用户打开页面看到的是
                # 空配置 + 「未运行」，很容易以为功能坏了（实测就是这么误判的）。
                item["features"] = self._feature_flags(record.name)
            result.append(item)
        return result

    def _feature_flags(self, name: str) -> dict[str, bool]:
        """某个账号开了哪些功能。配置读不出来时一律当「没开」。"""
        try:
            config = self.store.load_account_config(name, create=False)
        except Exception:  # pragma: no cover - 配置坏了不该拖垮账号列表
            log.warning("读取账号 %s 的配置失败，功能标记按全部关闭处理", name, exc_info=True)
            return {"cloudflare_ip": False, "notify": False, "red_packet": False, "reg_grab": False}
        return {
            "cloudflare_ip": bool(config.cloudflare_ip.enabled),
            "notify": bool(config.notify.enabled),
            "red_packet": bool(config.red_packet.enabled),
            "reg_grab": bool(config.reg_grab.enabled),
        }

    def get_account(self, name: str) -> Optional[dict[str, Any]]:
        record = self.store.get_account(name)
        if record is None:
            return None
        config = self.store.load_account_config(name, create=False)
        watched = config.watched_chats()
        source_count = sum(len(rule.sources) for rule in config.forward.active_rules)
        return {
            "name": record.name,
            "enabled": record.enabled,
            "user": record.label,
            "user_id": record.user_id,
            "username": record.username,
            "display_name": record.display_name,
            "phone": record.phone,
            "proxy": record.proxy.to_url() if record.proxy else None,
            "session_exists": self.store.has_session(record.name),
            "last_login": record.last_login_at,
            "config": config.model_dump(mode="json"),
            "source_count": source_count,
            "watched_chats": len(watched) if watched else 0,
        }

    # ------------------------------------------------------------------ #
    async def start(self, accounts: list[str] | None = None) -> dict[str, Any]:
        """启动指定账号（默认全部启用的账号）。"""
        async with self._lock:
            if self.is_running:
                return {"ok": False, "message": "已经在运行中，请先停止"}
            names = accounts or [r.name for r in self.store.load_registry().enabled_accounts]
            if not names:
                return {"ok": False, "message": "没有可运行的账号。先 login 并用 accounts enable 启用。"}

            from tg_assistant.forwarder import (
                ChannelGroupDedupe,
                CrossAccountDedupe,
                RecentContentDedupe,
            )
            from tg_assistant.runner import MultiRunner

            # 先确认有账号、再建 runner：原来是无条件建好之后才发现没账号就返回，
            # 于是 self._runner 留着一个从没跑过的对象。
            #
            # 把 runner 显式交给 _launch：它原来是回头读 self._runner，
            # 于是「start() 之后立刻 stop()」这种时序下，任务还没开始跑
            # self._runner 就已经被摘成 None 了，任务一启动就撞 assert。
            #
            # 去重表复用同一个实例：这个方法每被点一次「启动」就会重建 MultiRunner，
            # 每次都换新表的话，去重窗口会被清空 —— 刚发过的消息又能重发一遍。
            if self._dedupe is None:
                self._dedupe = CrossAccountDedupe()
            # 「频道 ↔ 群组 同内容」去重表同理：重建一次 MultiRunner 就换新表的话，
            # 刚发过的频道消息又能重发一遍，群组那条也就无从「顶替」。
            if self._pair_dedupe is None:
                self._pair_dedupe = ChannelGroupDedupe()
            # 「最近已转发的内容」去重表同理：换表 = 「前 5 条 / 一天内」的记录全没了。
            if self._recent_dedupe is None:
                self._recent_dedupe = RecentContentDedupe()
            await self._launch(
                MultiRunner(
                    self.store,
                    self.settings,
                    dedupe=self._dedupe,
                    pair_dedupe=self._pair_dedupe,
                    recent_dedupe=self._recent_dedupe,
                ),
                names,
            )
            return {"ok": True, "accounts": names}

    async def stop(self) -> dict[str, Any]:
        """停止全部账号。

        先请 MultiRunner 自己优雅退出（它要关 client、排空通知队列），
        只有超时才强杀 —— 直接 cancel 会跳过它的清理逻辑，把 pyrogram client
        和通知 worker 漏在后台，重新 start 时新旧 client 会争抢同一个 session 文件。
        """
        async with self._lock:
            runner, task = self._detach_runner()
        return await self._finish_stop(runner, task)

    async def stop_account(self, name: str) -> dict[str, Any]:
        """停止单个账号。

        ``MultiRunner`` 是「一批账号跑在同一个任务里」的结构，没法只摘掉其中一个，
        所以做法是：优雅停掉整批，再把其余的重新拉起。

        ⚠️ 重新拉起必须发生在**释放锁之后**：``asyncio.Lock`` 不可重入，
        在持锁状态下调用 :meth:`start` 会直接死锁（部署版就是这么写的，
        于是只要还有其他账号在跑，"停止单个账号"这个请求就会永久挂住，
        并且把那把锁一直占着，之后所有启停请求一起卡死）。
        """
        async with self._lock:
            if not self.is_running:
                return {"ok": False, "message": "没有在运行的账号"}
            running = sorted(self._running_accounts())
            if name not in running:
                return {"ok": False, "message": f"账号 {name} 未在运行"}
            remaining = [n for n in running if n != name]
            runner, task = self._detach_runner()

        await self._finish_stop(runner, task)

        if not remaining:
            return {"ok": True, "message": f"已停止账号 {name}"}
        result = await self.start(remaining)
        if not result.get("ok"):
            return result
        return {"ok": True, "message": f"已停止账号 {name}", "accounts": remaining}

    async def start_account(self, name: str) -> dict[str, Any]:
        """单独启动一个账号；**已经在跑的会被优雅重启**。

        ⚠️ 这里原来是「已在运行就返回 ``ok=False``」，2026-09-17 线上真踩到了：

        账号的转发规则只在**启动时读一次**（``runner.py`` 里的
        ``forward_rules=len(config.forward.active_rules)``），所以
        **改完规则必须重启账号才生效**。而用户表达「让改过的规则生效」的动作
        恰恰就是点那个按钮 —— 按钮叫「启动」，账号又正在跑，于是必然失败。
        用户看到的还是笼统的「启动失败」（原因见 ``rules.html`` 的 ``detailText``），
        只能一脸茫然。

        所以语义改成「启动 = 让当前配置生效」：已在运行就重启它。
        其余在跑的账号会被一并重启 —— 原因同 :meth:`stop_account`，
        ``MultiRunner`` 一次只接受一批账号。
        """
        async with self._lock:
            running = sorted(self._running_accounts()) if self.is_running else []
            restarted = name in running
            runner, task = self._detach_runner() if self.is_running else (None, None)

        if runner is not None or task is not None:
            await self._finish_stop(runner, task)

        # 去重：被重启的那个账号本来就在 running 里，直接拼会重复。
        names = list(dict.fromkeys([*running, name]))
        result = await self.start(names)
        if not result.get("ok"):
            return result
        if restarted:
            # 文案刻意不提「新规则已生效」——规则现在自己热重载（见
            # forwarder.ForwardEngine.reload_rules），点这个按钮只是为了
            # 重启账号本身（卡住 / 重连会话）。
            return {**result, "restarted": True, "message": f"已重启「{name}」"}
        return result

    def _detach_runner(self) -> tuple[Any, Optional[asyncio.Task[None]]]:
        """把当前 runner 与任务摘下来交给调用方（必须在持锁时调用）。"""
        runner = self._runner
        task = self._runner_task
        self._runner = None
        self._runner_task = None
        return runner, task

    async def _finish_stop(self, runner: Any, task: Optional[asyncio.Task[None]]) -> dict[str, Any]:
        """优雅停掉已摘下的 runner（不持锁 —— 这里要等最多 ``STOP_TIMEOUT`` 秒）。"""
        if runner is not None:
            runner.request_shutdown()

        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=STOP_TIMEOUT)
            except asyncio.TimeoutError:
                # ⚠️ 这里的 log 是 get_logger() 拿到的**普通** logging.Logger，
                # 只有结构化关键字参数是不支持的（那是 AccountLogger 的能力）。
                # 传了会当场抛 TypeError，把真正要报告的异常盖掉。
                log.warning(
                    "账号停止超时，已强制取消",
                    extra={"account": "-", "extra_fields": {"timeout_s": STOP_TIMEOUT}},
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "账号停止过程中出现异常",
                    extra={"account": "-", "extra_fields": {"error": str(exc)}},
                )

        self._shutdown_event.set()
        # 通知 /ws/status 订阅者：已停止。
        self.push_event({"type": "status", "running": False, "accounts": []})
        return {"ok": True}

    async def _supervise(self, runner: Any, names: list[str]) -> None:
        """在后台运行 MultiRunner，捕获异常，并把结束状态广播出去。

        ``runner`` 由调用方显式传入，**不要**回头读 ``self._runner``：
        ``stop()`` 会把它摘成 ``None``，而本任务可能还没开始跑。
        """
        error: Optional[str] = None
        options: dict[str, Any] = {
            "heartbeat": float(self.state.web_settings.heartbeat_interval)
        }
        options.update(self._run_options)
        try:
            await runner.run(
                names,
                # 信号由 uvicorn 接管，这里不能再抢注
                install_signals=False,
                **options,
            )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            error = str(exc)
            get_logger().exception("运行管理器异常退出", extra={"account": "-", "extra_fields": {"error": error}})
        finally:
            # 正常退出和崩溃都要通知，否则前端会一直停在「运行中」。
            self.push_event(
                {"type": "runner", "running": False, "error": error, "accounts": names}
            )

    def _running_accounts(self) -> set[str]:
        if self._runner is None:
            return set()
        return {snapshot.get("account") for snapshot in self._runner.snapshot()}

    def running_runner(self, name: str) -> Any:
        """某个账号**当前运行中**的 runner；没在跑就返回 ``None``。

        给「试发通知」这类需要复用已登录实例的接口用 —— 自己去建一个 client
        会跟正在跑的那个抢同一个 ``.session`` 文件。
        """
        if self._runner is None:
            return None
        return getattr(self._runner, "runners", {}).get(name)

    # ------------------------------------------------------------------ #
    # 日志广播
    def _emit_log(self, record: logging.LogRecord) -> None:
        """日志回调：格式化后推给所有 WebSocket 订阅者。"""
        entry = {
            "type": "log",
            "time": time.strftime("%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "account": getattr(record, "account", None) or "-",
            "msg": record.getMessage(),
            #: 结构化字段渲染成一行文本，前端直接追加。
            #: ⚠️ 必须带上：以前只推 ``msg``，于是面板上「命中转发规则」看不到
            #: rule/keyword、「Bot API xxx 被拒绝」看不到 description/hint ——
            #: 用户看到的就是一句没有任何线索的「命中转发规则」，
            #: 排查只能去服务器上翻文件日志。这里复用文件日志的同一套渲染，
            #: 保证两个界面看到的东西完全一致。
            "extra": render_extra(getattr(record, "extra_fields", {})).strip(),
        }
        # 保留最近 N 行
        self._recent_logs.append(entry)
        max_size = self.state.web_settings.log_history_size
        if len(self._recent_logs) > max_size:
            self._recent_logs = self._recent_logs[-max_size:]
        for queue in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(entry)

    def subscribe_logs(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        # 先塞历史日志，让新连接立刻看到上下文
        for entry in self._recent_logs:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(entry)
        self._subscribers.add(queue)
        return queue

    def unsubscribe_logs(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def push_event(self, event: dict[str, Any]) -> None:
        """推送自定义事件（登录进度、运行状态变更等）。"""
        for queue in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    @property
    def recent_logs(self) -> list[dict[str, Any]]:
        return list(self._recent_logs)


class _BroadcastHandler(logging.Handler):
    """把 logging 记录转成 dict 推给 RuntimeManager。"""

    def __init__(self, callback: Any) -> None:
        super().__init__()
        self.callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        # 日志系统不应因回调出错而中断（例如订阅队列已满）。
        with contextlib.suppress(Exception):  # pragma: no cover
            self.callback(record)


__all__ = ["AccountRuntime", "RuntimeManager"]
