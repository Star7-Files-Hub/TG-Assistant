"""运行编排：登录、启动、多账号并发、优雅退出。

一个账号一条 :class:`AccountRunner`，内部持有独立的 Client / 转发引擎 / 抢红包引擎 /
通知器。多账号由 :class:`MultiRunner` 并发拉起，互不影响：某个账号会话失效或代理挂了，
其余账号继续跑，并在日志里明确指出是哪个账号出了问题。
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time
from dataclasses import dataclass
from typing import Any, Optional

from pyrogram import Client
from pyrogram.errors import AuthKeyUnregistered, RPCError, Unauthorized, UserDeactivated

from .client import (
    ClientBundle,
    MissingApiCredentials,
    SessionInvalid,
    build_client,
    device_fingerprint,
    make_account_logger,
)
from .config import (
    AccountConfig,
    AccountRecord,
    Settings,
    mask_phone,
    utc_now_iso,
)
from .forwarder import ForwardEngine
from .logging_setup import get_logger
from .notify import BotNotifier, NotifyTask
from .paths import Paths
from .proxy import resolve_proxy
from .qr_login import (
    DEFAULT_LOGIN_TIMEOUT,
    PasswordProvider,
    QrLoginError,
    QrLoginSession,
    QrRenderer,
)
from .red_packet import RedPacketHunter
from .store import ConfigError, Store

log = get_logger("runner")


class AccountBusy(RuntimeError):
    """同一账号的 session 已被其它进程占用。"""


# --------------------------------------------------------------------------- #
# 登录
# --------------------------------------------------------------------------- #
@dataclass
class LoginResult:
    account: str
    user_id: int
    username: Optional[str]
    display_name: Optional[str]
    phone: Optional[str]


async def login_account(
    name: str,
    store: Store,
    settings: Settings,
    *,
    renderer: QrRenderer,
    password_provider: Optional[PasswordProvider] = None,
    timeout: float = DEFAULT_LOGIN_TIMEOUT,
    proxy_override: Optional[Any] = None,
    api_id: Optional[int] = None,
    api_hash: Optional[str] = None,
    force: bool = False,
) -> LoginResult:
    """扫码登录一个账号并写入注册表。

    已登录且未指定 ``force`` 时直接返回现有身份，不会重复弹二维码。
    """
    paths = store.paths
    record = store.get_account(name) or AccountRecord(name=name, created_at=utc_now_iso())
    if proxy_override is not None:
        record = record.model_copy(update={"proxy": proxy_override})
    if api_id is not None:
        record = record.model_copy(update={"api_id": api_id})
    if api_hash is not None:
        record = record.model_copy(update={"api_hash": api_hash})

    alog = make_account_logger(name, "login")
    account_paths = paths.account(name).ensure()

    if account_paths.session_file.is_file() and not force:
        alog.info("检测到已有会话，先尝试直接复用", session=str(account_paths.session_file))
        existing = await _probe_existing_session(record, settings, paths, alog)
        if existing is not None:
            updated = _merge_identity(record, existing)
            store.upsert_account(updated)
            store.harden_session(name)
            alog.info("会话有效，无需重新扫码", user=updated.label)
            return LoginResult(
                account=name,
                user_id=existing["id"],
                username=existing.get("username"),
                display_name=existing.get("name"),
                phone=existing.get("phone"),
            )
        alog.warning("已有会话不可用，将重新扫码登录")

    # 扫码登录必须开启更新流才能收到 updateLoginToken
    client = build_client(record, settings, paths, no_updates=False)
    proxy = resolve_proxy(record, settings)
    alog.info(
        "准备扫码登录",
        proxy=proxy.to_url() if proxy else "直连",
        api_id=record.api_id or settings.api_id,
        device=device_fingerprint(record)["device_model"],
        session=str(account_paths.session_file),
    )

    try:
        await client.connect()
    except OSError as exc:
        raise QrLoginError(
            f"无法连接 Telegram：{exc}。"
            + ("请检查代理是否可用（tg-assistant proxy-check）。" if proxy else "国内网络通常必须配置 SOCKS5 代理（--proxy）。")
        ) from exc

    #: 在客户端停止前导出，用于随后的 PostgreSQL 备份。
    session_string: Optional[str] = None
    try:
        session = QrLoginSession(
            client,
            alog,
            renderer=renderer,
            password_provider=password_provider,
            timeout=timeout,
        )
        user = await session.run()
        me = await client.get_me()
        # ⚠️ 必须在 ``stop()`` 之前导出：``stop()`` → ``disconnect()`` 会执行
        # ``storage.close()``，而 ``SQLiteStorage.close()`` 只是关掉 sqlite 连接、
        # 不置空 ``self.conn``。之后再调 ``export_session_string()`` 会抛
        # ``sqlite3.ProgrammingError: Cannot operate on a closed database``
        # （部署版把这一步放在 finally 之后，所以那里的 PG 备份其实从未成功过）。
        try:
            session_string = await client.export_session_string()
        except Exception as exc:  # noqa: BLE001 - 导出失败不能影响登录
            alog.warning("导出 session string 失败，将跳过 PostgreSQL 备份", error=str(exc))
    finally:
        with contextlib.suppress(Exception):
            if client.is_initialized:
                await client.stop(block=True)
            elif client.is_connected:
                await client.disconnect()

    identity = {
        "id": me.id,
        "username": me.username,
        "name": " ".join(filter(None, [me.first_name, me.last_name])) or None,
        "phone": mask_phone(getattr(me, "phone_number", None)),
    }
    _guard_duplicate(store, name, identity["id"], alog)
    updated = _merge_identity(record, identity)
    updated = updated.model_copy(update={"last_login_at": utc_now_iso()})
    store.upsert_account(updated)
    store.harden_session(name)
    store.load_account_config(name, create=True)
    # 把 session_string 备份到 PostgreSQL（未配置 TGA_POSTGRES_DSN 时是空操作）。
    # 这样即使本机 .session 文件丢了，也能从库里恢复登录态。
    if session_string:
        try:
            from .client import _save_pg_session_string

            _save_pg_session_string(name, session_string)
            alog.info("会话已备份到 PostgreSQL")
        except Exception as exc:  # noqa: BLE001 - 备份失败不能影响登录
            alog.warning("备份会话到 PostgreSQL 失败", error=str(exc))
    del user

    alog.info(
        "账号登录完成，数据目录已就绪",
        user=updated.label,
        data_dir=str(account_paths.root),
        config=str(account_paths.config_file),
    )
    return LoginResult(
        account=name,
        user_id=identity["id"],
        username=identity["username"],
        display_name=identity["name"],
        phone=identity["phone"],
    )


async def _probe_existing_session(
    record: AccountRecord,
    settings: Settings,
    paths: Paths,
    alog: Any,
) -> Optional[dict[str, Any]]:
    """尝试用现有 session 拉一次 ``get_me``，验证是否仍然有效。"""
    client = build_client(record, settings, paths, no_updates=True)
    try:
        await client.start()
        me = await client.get_me()
        return {
            "id": me.id,
            "username": me.username,
            "name": " ".join(filter(None, [me.first_name, me.last_name])) or None,
            "phone": mask_phone(getattr(me, "phone_number", None)),
        }
    except (AuthKeyUnregistered, Unauthorized, UserDeactivated) as exc:
        alog.warning("会话已失效", error=f"{type(exc).__name__}: {exc}")
        return None
    except OSError as exc:
        alog.warning("网络不可用，无法验证已有会话", error=str(exc))
        return None
    except RPCError as exc:
        alog.warning("验证已有会话时被 Telegram 拒绝", error=f"{type(exc).__name__}: {exc}")
        return None
    finally:
        with contextlib.suppress(Exception):
            if client.is_initialized:
                await client.stop(block=True)
            elif client.is_connected:
                await client.disconnect()


def _merge_identity(record: AccountRecord, identity: dict[str, Any]) -> AccountRecord:
    return record.model_copy(
        update={
            "user_id": identity.get("id"),
            "username": identity.get("username"),
            "display_name": identity.get("name"),
            "phone": identity.get("phone") or record.phone,
        }
    )


def _guard_duplicate(store: Store, name: str, user_id: int, alog: Any) -> None:
    """同一个 Telegram 账号不应挂在两个账号名下，否则数据会混。"""
    for other in store.load_registry().accounts:
        if other.name != name and other.user_id == user_id:
            alog.warning(
                "该 Telegram 账号已经以另一个名字登录过",
                existing_account=other.name,
                user_id=user_id,
                hint="两个账号名指向同一个账号会导致会话互相踢下线，建议只保留一个",
            )


# --------------------------------------------------------------------------- #
# 单账号运行
# --------------------------------------------------------------------------- #
class AccountRunner:
    """驱动单个账号的完整运行期。"""

    def __init__(
        self,
        record: AccountRecord,
        config: AccountConfig,
        settings: Settings,
        paths: Paths,
    ) -> None:
        self.record = record
        self.config = config
        self.settings = settings
        self.paths = paths
        self.alog = make_account_logger(record.name, "runner")
        self.client: Optional[Client] = None
        self.notifier: Optional[BotNotifier] = None
        self.forwarder: Optional[ForwardEngine] = None
        self.hunter: Optional[RedPacketHunter] = None
        self.bundle: Optional[ClientBundle] = None
        self.started_at: Optional[float] = None
        self._stopped = asyncio.Event()

    @property
    def name(self) -> str:
        return self.record.name

    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        session_file = self.paths.account(self.name).session_file
        if not session_file.is_file():
            raise ConfigError(
                f"账号 {self.name} 尚未登录（找不到 {session_file}）。"
                f"请先运行：tg-assistant login -a {self.name}"
            )

        needs_updates = self.config.needs_updates
        self.client = build_client(
            self.record, self.settings, self.paths, no_updates=not needs_updates
        )
        proxy = resolve_proxy(self.record, self.settings)
        self.alog.info(
            "正在启动账号",
            proxy=proxy.to_url() if proxy else "直连",
            updates=needs_updates,
            workers=self.settings.workers,
            forward_rules=len(self.config.forward.active_rules),
            red_packet=self.config.red_packet.enabled,
            notify=self.config.notify.enabled,
        )

        with self.alog.latency("连接并登录 Telegram"):
            await self.client.start()
        me = await self.client.get_me()
        self.bundle = ClientBundle(
            record=self.record,
            config=self.config,
            client=self.client,
            alog=self.alog,
            me_id=me.id,
            me_username=me.username,
            me_name=" ".join(filter(None, [me.first_name, me.last_name])) or None,
        )
        self.alog.info(
            "账号已在线",
            user=self.bundle.describe(),
            dc=getattr(self.client, "session", None) and getattr(self.client.session, "dc_id", "?"),
        )

        if self.config.notify.enabled:
            self.notifier = BotNotifier(self.config.notify, self.alog, proxy)
            await self.notifier.start()

        self.forwarder = ForwardEngine(self.client, self.config, self.alog, self.notifier)
        self.forwarder.register()

        self.hunter = RedPacketHunter(self.client, self.config, self.alog, self.notifier)
        await self.hunter.register()

        self.started_at = time.time()
        if not needs_updates:
            self.alog.warning(
                "当前配置没有任何需要实时监听的功能（转发规则为空且未开启抢红包），"
                "程序会保持在线但不会做任何事"
            )

    async def stop(self) -> None:
        self.alog.info("正在停止账号")
        if self.forwarder is not None:
            await self.forwarder.close()
        if self.hunter is not None:
            await self.hunter.close()
        if self.notifier is not None:
            await self.notifier.stop()
        if self.client is not None:
            with contextlib.suppress(Exception):
                if self.client.is_initialized:
                    await self.client.stop(block=True)
                elif self.client.is_connected:
                    await self.client.disconnect()
        uptime = time.time() - self.started_at if self.started_at else 0
        self.alog.info("账号已停止", uptime_s=round(uptime, 1))
        self._stopped.set()

    async def run_forever(self, heartbeat: float = 300.0) -> None:
        """保持运行并周期性输出心跳统计。"""
        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=heartbeat)
            except asyncio.TimeoutError:
                self._log_heartbeat()

    def _log_heartbeat(self) -> None:
        uptime = time.time() - self.started_at if self.started_at else 0
        fields: dict[str, Any] = {"uptime_s": round(uptime)}
        if self.forwarder is not None:
            snapshot = self.forwarder.snapshot()
            fields.update(
                {
                    "recv": snapshot["received"],
                    "matched": snapshot["matched"],
                    "forwarded": snapshot["forwarded"],
                    "fwd_failed": snapshot["failed"],
                    "deduped": snapshot["deduped"],
                }
            )
        if self.hunter is not None:
            snapshot = self.hunter.snapshot()
            fields.update(
                {
                    "rp_detected": snapshot["detected"],
                    "rp_success": snapshot["success"],
                    "rp_failed": snapshot["failed"],
                    "rp_unknown": snapshot["unknown"],
                    "rp_replied": snapshot["replied"],
                }
            )
        if self.notifier is not None:
            fields.update({f"notify_{k}": v for k, v in self.notifier.stats.items()})
        self.alog.info("心跳", **fields)

    def snapshot(self) -> dict[str, Any]:
        return {
            "account": self.name,
            "user": self.bundle.describe() if self.bundle else None,
            "uptime_s": round(time.time() - self.started_at) if self.started_at else 0,
            "forward": self.forwarder.snapshot() if self.forwarder else None,
            "red_packet": self.hunter.snapshot() if self.hunter else None,
            "notify": dict(self.notifier.stats) if self.notifier else None,
        }


# --------------------------------------------------------------------------- #
# 多账号编排
# --------------------------------------------------------------------------- #
class MultiRunner:
    """并发运行多个账号，单账号故障不影响其它账号。"""

    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings
        self.runners: dict[str, AccountRunner] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._shutdown = asyncio.Event()

    async def run(
        self,
        names: list[str],
        *,
        heartbeat: float = 300.0,
        restart_delay: float = 15.0,
        max_restarts: int = 5,
        install_signals: bool = True,
    ) -> int:
        """启动全部账号并阻塞直到收到退出信号。返回退出码。

        ``install_signals=False`` 供 Web 模式使用：那里 SIGINT/SIGTERM 由
        uvicorn 接管，再注册一次会把 uvicorn 的处理器顶掉，导致 Ctrl-C
        只停账号、Web 服务赖着不走。
        """
        if not names:
            log.error("没有要运行的账号。先执行 login，或用 --account 指定账号")
            return 2

        if install_signals:
            self._install_signal_handlers()
        log.info(
            "启动多账号运行",
            extra={
                "account": "-",
                "extra_fields": {
                    "accounts": ",".join(names),
                    "count": len(names),
                    "heartbeat_s": heartbeat,
                },
            },
        )

        for name in names:
            self._tasks[name] = asyncio.create_task(
                self._supervise(name, heartbeat, restart_delay, max_restarts),
                name=f"account:{name}",
            )

        try:
            await self._shutdown.wait()
            log.info("收到退出信号，开始优雅关闭", extra={"account": "-", "extra_fields": {}})
            return 0
        finally:
            # 放在 finally 里：即使外层直接 cancel 这个任务（Web 停止运行时的兜底路径），
            # 也必须把 client / 通知 worker 收干净，否则会泄漏，
            # 而且下次启动时新旧 client 会争抢同一个 session 文件。
            # shield 保证清理本身不会被二次取消打断。
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(self._cleanup())

    async def _cleanup(self) -> None:
        """取消在途账号任务并关闭所有 client。"""
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

        if self.runners:
            await asyncio.gather(
                *(runner.stop() for runner in self.runners.values()), return_exceptions=True
            )
        log.info("全部账号已停止", extra={"account": "-", "extra_fields": {}})

    async def _supervise(
        self, name: str, heartbeat: float, restart_delay: float, max_restarts: int
    ) -> None:
        """看护单个账号：崩溃自动重启，会话失效则停止并提示重新登录。"""
        alog = make_account_logger(name, "runner")
        restarts = 0
        while not self._shutdown.is_set():
            runner: Optional[AccountRunner] = None
            try:
                record = self.store.require_account(name)
                if not record.enabled:
                    alog.warning("账号已被禁用，跳过", hint="用 accounts enable 重新启用")
                    return
                config = self.store.load_account_config(name)
                runner = AccountRunner(record, config, self.settings, self.store.paths)
                self.runners[name] = runner
                await runner.start()
                await runner.run_forever(heartbeat=heartbeat)
                return
            except asyncio.CancelledError:
                if runner is not None:
                    with contextlib.suppress(Exception):
                        await runner.stop()
                raise
            except (SessionInvalid, AuthKeyUnregistered, Unauthorized, UserDeactivated) as exc:
                alog.error(
                    "账号会话失效，已停止该账号",
                    error=f"{type(exc).__name__}: {exc}",
                    hint=f"重新登录：tg-assistant login -a {name} --force",
                )
                await self._notify_error(runner, name, f"会话失效：{exc}")
                if runner is not None:
                    with contextlib.suppress(Exception):
                        await runner.stop()
                return
            except (ConfigError, MissingApiCredentials) as exc:
                alog.error("配置问题导致无法启动", error=str(exc))
                return
            except Exception as exc:
                restarts += 1
                if runner is not None:
                    with contextlib.suppress(Exception):
                        await runner.stop()
                if restarts > max_restarts:
                    alog.error(
                        "重启次数超过上限，放弃该账号",
                        restarts=restarts,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    await self._notify_error(runner, name, f"重启超限：{exc}")
                    return
                delay = restart_delay * min(restarts, 4)
                alog.exception(
                    "账号异常退出，稍后重启",
                    error=f"{type(exc).__name__}: {exc}",
                    restart_in_s=delay,
                    restarts=restarts,
                )
                try:
                    await asyncio.wait_for(self._shutdown.wait(), timeout=delay)
                    return
                except asyncio.TimeoutError:
                    continue

    async def _notify_error(
        self, runner: Optional[AccountRunner], name: str, detail: str
    ) -> None:
        if runner is None or runner.notifier is None:
            return
        if not runner.notifier.config.wants("error"):
            return
        runner.notifier.submit(
            NotifyTask(event="error", text=f"⚠️ 账号 <b>{name}</b> 出现问题\n{detail}")
        )

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.add_signal_handler(sig, self._shutdown.set)

    def request_shutdown(self) -> None:
        self._shutdown.set()

    def snapshot(self) -> list[dict[str, Any]]:
        return [runner.snapshot() for runner in self.runners.values()]


__all__ = [
    "AccountBusy",
    "AccountRunner",
    "LoginResult",
    "MultiRunner",
    "login_account",
]
