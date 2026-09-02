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

    return Client(
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
