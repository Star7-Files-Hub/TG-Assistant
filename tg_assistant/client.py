"""Pyrogram 客户端工厂与多账号隔离。

隔离策略：每个账号一个目录，``workdir=data/accounts/<name>/``、``name=<name>``，
于是 session 文件落在 ``data/accounts/<name>/<name>.session``，
peer 缓存、更新状态都随之独立，删目录即彻底清除该账号。

另外提供：

- :func:`build_client` —— 统一构造参数（代理、伪装、workers、FloodWait 阈值）。
- :func:`with_flood_retry` —— FloodWait / 网络抖动的统一重试，避免各处散落 try/except。
- :class:`ClientBundle` —— 客户端 + 账号记录 + 配置 + 日志器的运行期集合。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import random
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, TypeVar

from pyrogram import Client
from pyrogram.errors import (
    AuthKeyUnregistered,
    FloodPremiumWait,
    FloodWait,
    InternalServerError,
    RPCError,
    ServiceUnavailable,
    Unauthorized,
    UserDeactivated,
)

from .config import AccountConfig, AccountRecord, Settings
from .logging_setup import AccountLogger, account_logger
from .paths import Paths
from .proxy import resolve_proxy

T = TypeVar("T")

#: 伪装用的设备型号池。按账号名做稳定哈希取值，
#: 保证同一账号每次登录设备信息一致（否则 Telegram 会当成新设备反复告警）。
_DEVICE_MODELS = (
    "Desktop",
    "PC 64bit",
    "MacBook Pro",
    "ThinkPad X1",
    "Linux Desktop",
)
_SYSTEM_VERSIONS = (
    "Windows 10",
    "Windows 11",
    "macOS 14.5",
    "Ubuntu 22.04",
    "Debian 12",
)


def _stable_choice(seed: str, options: tuple[str, ...]) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return options[digest[0] % len(options)]


def device_fingerprint(record: AccountRecord) -> dict[str, str]:
    """给账号生成稳定的客户端标识。"""
    from . import __version__

    return {
        "device_model": record.device_model or _stable_choice(record.name, _DEVICE_MODELS),
        "system_version": record.system_version
        or _stable_choice(record.name + "-sys", _SYSTEM_VERSIONS),
        "app_version": record.app_version or f"TG-Assistant {__version__}",
        "lang_code": record.lang_code or "zh",
    }


class MissingApiCredentials(RuntimeError):
    """缺少 api_id / api_hash。"""


def resolve_credentials(
    record: Optional[AccountRecord], settings: Settings
) -> tuple[int, str]:
    """账号级凭据优先，其次全局设置。"""
    api_id = (record.api_id if record else None) or settings.api_id
    api_hash = (record.api_hash if record else None) or settings.api_hash
    if not api_id or not api_hash:
        raise MissingApiCredentials(
            "缺少 api_id / api_hash。请在 my.telegram.org 申请后设置环境变量 "
            "TGA_API_ID、TGA_API_HASH，或用 --api-id/--api-hash 传入。"
        )
    return int(api_id), str(api_hash)


def build_client(
    record: AccountRecord,
    settings: Settings,
    paths: Paths,
    *,
    no_updates: bool = True,
    in_memory: bool = False,
    session_string: Optional[str] = None,
) -> Client:
    """按账号构造 pyrogram Client。

    Parameters
    ----------
    no_updates:
        ``True`` 时不启动更新流（登录、发消息等一次性操作用）；
        转发/抢红包必须为 ``False``。
    """
    api_id, api_hash = resolve_credentials(record, settings)
    account_paths = paths.account(record.name).ensure()
    proxy = resolve_proxy(record, settings)
    fingerprint = device_fingerprint(record)

    # 必须在构造 Client 之前打：补丁会在 ``SQLiteStorage.__init__`` 里把
    # ``use_wal`` 置为 True，而 pyrogram 是在 ``open()`` 里读这个属性的。
    #
    # 放在这里而不是模块级：``build_client`` 是全项目**唯一**构造 ``Client``
    # 的入口（cli / runner / web 都走它），所以不存在漏打的分支；
    # 同时避免 ``import tg_assistant.client`` 就去全局 patch ``sqlite3.connect``。
    # 这个「顺序 + 必被调用」的约定由 tests/test_client.py 钉住。
    _patch_sqlite_concurrent()

    client = Client(
        name=record.name,
        api_id=api_id,
        api_hash=api_hash,
        workdir=str(account_paths.session_dir),
        proxy=proxy.to_pyrogram() if proxy else None,
        no_updates=no_updates,
        in_memory=in_memory,
        session_string=session_string,
        workers=settings.workers,
        sleep_threshold=settings.sleep_threshold,
        ipv6=settings.ipv6,
        # 跳过历史积压更新：秒级转发只关心"现在"，补历史会在启动时造成风暴。
        skip_updates=True,
        hide_password=True,
        **fingerprint,
    )
    return client


# --------------------------------------------------------------------------- #
# 会话库并发调优：避免 "database is locked"
# --------------------------------------------------------------------------- #
#: 会话库的 busy 等待时间（秒）。pyrogram 默认 ``timeout=1``，即只等 1 秒；
#: 转发开启后 pyrogram 会高频写 ``peers`` / ``usernames`` / ``update_state``，
#: 1 秒远远不够 —— 实测残留读事务时 15 次写入 15 次失败，每次都要等满 1 秒。
SESSION_BUSY_TIMEOUT = 30.0

_sqlite_patched = False


def _patch_sqlite_concurrent() -> None:
    """让 pyrogram 的 SQLite 会话库真正扛得住并发。只打一次。

    kurigram/pyrogram 的 ``SQLiteStorage`` 有两个默认值在转发场景下会致命：

    1. ``use_wal=False`` → ``open()`` 执行 ``PRAGMA journal_mode=DELETE``。
       DELETE（回滚日志）模式下**读会阻塞写**。转发开启后 pyrogram 要为每条消息
       写 ``peers``/``usernames`` 缓存，此时只要还存在任何一个残留连接
       （例如上一轮没关干净的 client），写入就会被卡死并抛
       ``OperationalError: database is locked``。
    2. 建连接时写死 ``timeout=1``，busy 只等 1 秒。短暂争用会直接失败而不是等一下。

    两个必须避开的坑：

    - **不能只在 ``open()`` 返回后补 PRAGMA。** ``open()`` 内部有一个无条件
      ``VACUUM``，它发生在任何 post-open PRAGMA 之前，事后设 ``busy_timeout``
      救不到它。所以只能从 ``sqlite3.connect`` 这一层把 timeout 抬上去。
    - **不能让存储先 DELETE 再切 WAL。** 那样每次 ``open()`` 要抢三次排他锁
      （DELETE / VACUUM / WAL），比不补还容易锁死。要让存储**一开始就是 WAL**。

    实测（2000 次「写 peers + usernames + update_state」）：
    残留读事务时未修复 15/15 失败，修复后 0 失败。
    但**残留未提交写事务时仍然会失败** —— 那种情况等多久都没用，
    因为 WAL 同一时刻只允许一个写者。所以别让两个进程开同一个会话文件。
    """
    global _sqlite_patched
    if _sqlite_patched:
        return

    try:
        import sqlite3

        from pyrogram.storage.sqlite_storage import SQLiteStorage
    except ImportError:  # pragma: no cover - 存储实现换了也不该拖垮整个程序
        return

    # ---- 1) 连接层：只针对会话库，把 busy 等待抬到 30 秒 -------------------
    original_connect = sqlite3.connect

    def _connect_with_busy_timeout(database: Any, *args: Any, **kwargs: Any) -> Any:
        with contextlib.suppress(Exception):
            if isinstance(database, str) and database.endswith(".session"):
                current = float(kwargs.get("timeout") or 0)
                kwargs["timeout"] = max(current, SESSION_BUSY_TIMEOUT)
        return original_connect(database, *args, **kwargs)

    sqlite3.connect = _connect_with_busy_timeout  # type: ignore[assignment]

    # ---- 2) 存储层：默认走 WAL --------------------------------------------
    # ``use_wal`` 只在 ``SQLiteStorage.open()`` 里被读取，所以在 ``__init__``
    # 之后赋值即可，不必碰签名（避免位置参数与关键字参数冲突）。
    original_init = SQLiteStorage.__init__

    def _init_with_wal(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.use_wal = True

    SQLiteStorage.__init__ = _init_with_wal  # type: ignore[method-assign]
    _sqlite_patched = True


#: 可以重试的瞬时错误。
TRANSIENT_ERRORS = (
    InternalServerError,
    ServiceUnavailable,
    OSError,
    asyncio.TimeoutError,
    ConnectionError,
)

#: 不可恢复的授权错误，遇到应停止该账号并提示重新登录。
FATAL_AUTH_ERRORS = (AuthKeyUnregistered, UserDeactivated, Unauthorized)


class SessionInvalid(RuntimeError):
    """会话失效，需要重新扫码登录。"""


async def with_flood_retry(
    func: Callable[[], Awaitable[T]],
    *,
    alog: AccountLogger,
    action: str,
    retries: int = 3,
    max_flood_wait: float = 120.0,
    base_delay: float = 0.5,
    expected_errors: tuple[type[BaseException], ...] = (),
) -> T:
    """统一的 FloodWait / 瞬时错误重试。

    - ``FloodWait``：按 Telegram 要求 sleep 后重试；超过 ``max_flood_wait`` 直接放弃并告警。
    - 瞬时网络/服务端错误：指数退避 + 抖动。
    - 授权类错误：立即转成 :class:`SessionInvalid`，不做无意义重试。
    - ``expected_errors``：调用方会自行处理的业务性错误（如红包过期），
      只记 DEBUG 后原样抛出，避免刷 ERROR 日志。
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return await func()
        except (FloodWait, FloodPremiumWait) as exc:
            wait = float(getattr(exc, "value", None) or getattr(exc, "seconds", 0) or 0)
            if wait > max_flood_wait or attempt > retries:
                alog.error(
                    f"{action} 触发 FloodWait 且超出等待上限，放弃本次操作",
                    wait_seconds=wait,
                    limit=max_flood_wait,
                    attempt=attempt,
                )
                raise
            alog.warning(
                f"{action} 触发 FloodWait，等待后重试",
                wait_seconds=wait,
                attempt=attempt,
            )
            await asyncio.sleep(wait + 0.5)
        except FATAL_AUTH_ERRORS as exc:
            alog.error(f"{action} 失败：会话已失效，需要重新扫码登录", error=str(exc))
            raise SessionInvalid(str(exc)) from exc
        except expected_errors as exc:
            alog.debug(
                f"{action} 返回预期内的业务错误，交由上层处理",
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        except TRANSIENT_ERRORS as exc:
            if attempt > retries:
                alog.error(
                    f"{action} 重试 {retries} 次后仍失败",
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.3)
            alog.warning(
                f"{action} 出现瞬时错误，{delay:.1f}s 后重试",
                error=f"{type(exc).__name__}: {exc}",
                attempt=attempt,
            )
            await asyncio.sleep(delay)
        except RPCError as exc:
            alog.error(
                f"{action} 被 Telegram 拒绝",
                error=f"{type(exc).__name__}: {exc}",
                attempt=attempt,
            )
            raise


@dataclass
class ClientBundle:
    """一个账号的运行期上下文。"""

    record: AccountRecord
    config: AccountConfig
    client: Client
    alog: AccountLogger
    me_id: Optional[int] = None
    me_username: Optional[str] = None
    me_name: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.record.name

    def describe(self) -> str:
        parts = [self.record.name]
        if self.me_username:
            parts.append(f"@{self.me_username}")
        elif self.me_name:
            parts.append(self.me_name)
        if self.me_id:
            parts.append(f"id={self.me_id}")
        return " ".join(parts)


def make_account_logger(name: str, module: str | None = None) -> AccountLogger:
    return account_logger(name, module)


__all__ = [
    "ClientBundle",
    "FATAL_AUTH_ERRORS",
    "MissingApiCredentials",
    "SessionInvalid",
    "TRANSIENT_ERRORS",
    "build_client",
    "device_fingerprint",
    "make_account_logger",
    "resolve_credentials",
    "with_flood_retry",
]
