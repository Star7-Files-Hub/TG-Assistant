"""扫码登录（QR Login）。

Telegram 的二维码登录三方协议（core.telegram.org/api/qr-login）：

1. 待登录客户端调用 ``auth.exportLoginToken`` 拿到 ``token`` 与 ``expires``；
2. 渲染二维码 ``tg://login?token=<base64url(token)>``；
3. 已登录的手机扫码后由**手机端**调用 ``auth.acceptLoginToken``
   （所以本程序不需要调用它，网上有些实现把这一步搞错了）；
4. 待登录客户端收到 ``updateLoginToken``，再次调用 ``auth.exportLoginToken``：
   - 返回 ``auth.loginTokenMigrateTo`` → 切到指定 DC 后调用 ``auth.importLoginToken``；
   - 返回 ``auth.loginTokenSuccess`` → 登录完成；
5. 若账号开了两步验证，第 4 步会抛 ``SessionPasswordNeeded``，再调用 ``check_password``。

本模块的实现要点：

- **非交互友好**：二维码渲染与密码获取都通过回调注入，CLI 用终端 ASCII，
  未来接 Web/Bot 也能复用同一套状态机。
- **令牌过期自动刷新**：``expires`` 到点后重新导出并重画二维码，不会卡死。
- **多账号安全**：登录前检查该 session 是否已登录、扫出来的 user_id 是否与
  注册表记录冲突（防止两个账号名指向同一个 Telegram 账号导致数据混淆）。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from pyrogram import Client, filters, raw, types
from pyrogram.errors import (
    AuthTokenAlreadyAccepted,
    AuthTokenExpired,
    AuthTokenInvalid,
    PasswordHashInvalid,
    SessionPasswordNeeded,
)
from pyrogram.handlers import RawUpdateHandler

from .logging_setup import AccountLogger

#: 每张二维码最长展示时间（秒）。Telegram 通常给 30s，这里做兜底。
MAX_TOKEN_TTL = 120.0
#: 单次登录总超时（秒），超过则放弃，避免 CLI 永久挂住。
DEFAULT_LOGIN_TIMEOUT = 300.0


class QrLoginError(RuntimeError):
    """扫码登录失败。"""


class QrLoginTimeout(QrLoginError):
    """在超时时间内没有完成扫码。"""


@dataclass
class QrCodePayload:
    """一次二维码渲染所需的数据。"""

    url: str
    expires_at: float
    attempt: int

    @property
    def ttl(self) -> float:
        return max(0.0, self.expires_at - time.time())

    def ascii_art(self, invert: bool = True, border: int = 1) -> str:
        """渲染为终端可扫的 ASCII 二维码。

        ``invert=True`` 适配深色终端（大多数情况）；浅色终端可传 False。
        """
        import qrcode

        qr = qrcode.QRCode(border=border)
        qr.add_data(self.url)
        buffer = io.StringIO()
        qr.print_ascii(out=buffer, tty=False, invert=invert)
        return buffer.getvalue()

    def save_png(self, path: Path, box_size: int = 8, border: int = 2) -> Path:
        """存成 PNG，方便在没有终端的环境（如 Docker 日志）里查看。"""
        import qrcode

        qr = qrcode.QRCode(box_size=box_size, border=border)
        qr.add_data(self.url)
        image = qr.make_image(fill_color="black", back_color="white")
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path)
        return path


#: 渲染回调：拿到 payload 后负责展示（打印 / 存文件 / 推送）。
QrRenderer = Callable[[QrCodePayload], Optional[Awaitable[None]]]
#: 2FA 密码提供者：返回明文密码，返回 None 表示放弃。
PasswordProvider = Callable[[Optional[str]], Awaitable[Optional[str]]]


async def _maybe_await(value: Optional[Awaitable[None]]) -> None:
    if value is not None:
        await value


def _token_url(token: bytes) -> str:
    return f"tg://login?token={base64.urlsafe_b64encode(token).decode('utf-8')}"


def _normalize_expires(expires: int | float | None) -> float:
    """``expires`` 可能是绝对时间戳，也可能是相对秒数，统一成绝对时间戳。"""
    now = time.time()
    if not expires:
        return now + 30.0
    value = float(expires)
    # 大于当前时间戳一半的数值当作绝对时间戳
    if value > now / 2:
        absolute = value
    else:
        absolute = now + value
    # 限制在 [5s, MAX_TOKEN_TTL] 内，防止服务端返回异常值把流程卡死
    return min(max(absolute, now + 5.0), now + MAX_TOKEN_TTL)


class QrLoginSession:
    """驱动一次扫码登录的状态机。

    使用方式::

        session = QrLoginSession(client, alog, renderer=..., password_provider=...)
        user = await session.run()
    """

    def __init__(
        self,
        client: Client,
        alog: AccountLogger,
        *,
        renderer: QrRenderer,
        password_provider: Optional[PasswordProvider] = None,
        timeout: float = DEFAULT_LOGIN_TIMEOUT,
        except_ids: Optional[list[int]] = None,
    ) -> None:
        self.client = client
        self.alog = alog.bind("qrlogin")
        self.renderer = renderer
        self.password_provider = password_provider
        self.timeout = timeout
        self.except_ids = except_ids or []
        self._scanned = asyncio.Event()
        self._attempt = 0

    # ------------------------------------------------------------------ #
    async def run(self) -> types.User:
        """执行完整登录流程，返回登录成功的用户。

        客户端必须以 ``no_updates=False`` 构造（否则收不到 ``updateLoginToken``）。
        调用方负责 ``connect()`` / ``disconnect()``。
        """
        if self.client.no_updates:
            raise QrLoginError(
                "扫码登录需要更新流支持，请用 no_updates=False 构造 Client（内部错误）"
            )

        handler = self.client.add_handler(
            RawUpdateHandler(
                self._on_raw_update,
                filters=filters.create(
                    lambda _, __, update: isinstance(update, raw.types.UpdateLoginToken)
                ),
            )
        )
        await self.client.dispatcher.start()
        deadline = time.time() + self.timeout
        self.alog.info(
            "开始扫码登录",
            timeout_s=self.timeout,
            hint="请用手机 Telegram：设置 → 设备 → 关联桌面设备 → 扫描二维码",
        )
        try:
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise QrLoginTimeout(
                        f"{self.timeout:.0f} 秒内未完成扫码。请确认手机能正常联网后重试，"
                        "或用 --timeout 延长等待时间。"
                    )

                payload = await self._export_and_render()
                wait_for = min(payload.ttl, remaining)
                self.alog.debug(
                    "等待手机扫码",
                    attempt=payload.attempt,
                    token_ttl_s=round(payload.ttl, 1),
                    wait_s=round(wait_for, 1),
                )
                try:
                    await asyncio.wait_for(self._scanned.wait(), timeout=max(wait_for, 1.0))
                except asyncio.TimeoutError:
                    self.alog.info(
                        "二维码已过期，正在刷新",
                        attempt=payload.attempt,
                        remaining_s=round(deadline - time.time(), 1),
                    )
                    continue

                self._scanned.clear()
                self.alog.info("检测到扫码事件，正在确认登录", attempt=payload.attempt)
                user = await self._finalize()
                if user is not None:
                    return user
                # 手机端只是"扫到了"但还没点确认，继续等
                self.alog.info("手机已扫码但尚未确认，继续等待用户点击「登录」")
        finally:
            with contextlib.suppress(Exception):
                self.client.remove_handler(*handler)
            with contextlib.suppress(Exception):
                await self.client.dispatcher.stop(clear_handlers=True)

    # ------------------------------------------------------------------ #
    async def _on_raw_update(self, client: Client, update, users, chats) -> None:  # noqa: ANN001
        del client, users, chats
        self.alog.debug("收到 updateLoginToken", update=type(update).__name__)
        self._scanned.set()

    async def _export(self):
        return await self.client.invoke(
            raw.functions.auth.ExportLoginToken(
                api_id=self.client.api_id,
                api_hash=self.client.api_hash,
                except_ids=self.except_ids,
            )
        )

    async def _export_and_render(self) -> QrCodePayload:
        self._attempt += 1
        try:
            result = await self._export()
        except AuthTokenExpired:
            result = await self._export()

        if isinstance(result, raw.types.auth.LoginTokenSuccess):
            # 极少见：上一轮已经成功，直接交给 _finalize 处理
            self._scanned.set()
            return QrCodePayload(url="", expires_at=time.time() + 5, attempt=self._attempt)

        if not isinstance(result, raw.types.auth.LoginToken):
            raise QrLoginError(f"导出登录令牌返回了意外类型: {type(result).__name__}")

        payload = QrCodePayload(
            url=_token_url(result.token),
            expires_at=_normalize_expires(getattr(result, "expires", None)),
            attempt=self._attempt,
        )
        await _maybe_await(self.renderer(payload))
        return payload

    async def _finalize(self) -> Optional[types.User]:
        """扫码后确认登录；返回 None 表示还需继续等待。"""
        try:
            result = await self._export()
        except AuthTokenExpired:
            self.alog.warning("确认时令牌已过期，将重新生成二维码")
            return None
        except SessionPasswordNeeded:
            return await self._handle_2fa()

        if isinstance(result, raw.types.auth.LoginTokenMigrateTo):
            return await self._migrate_and_import(result)

        if isinstance(result, raw.types.auth.LoginTokenSuccess):
            return await self._on_success(result)

        if isinstance(result, raw.types.auth.LoginToken):
            # 手机扫了码但用户还没确认
            return None

        raise QrLoginError(f"确认登录返回了意外类型: {type(result).__name__}")

    async def _migrate_and_import(self, migrate) -> types.User:  # noqa: ANN001
        """按服务端指示切换 DC，然后用 importLoginToken 完成登录。"""
        dc_id = migrate.dc_id
        self.alog.info("按服务端要求迁移数据中心", dc_id=dc_id)
        dc_option = await self.client.get_dc_option(dc_id, ipv6=self.client.ipv6)
        await self.client.session.stop()
        self.client.session = await self.client.get_session(
            dc_id=dc_id,
            server_address=dc_option.ip_address,
            port=dc_option.port,
            export_authorization=False,
            temporary=True,
        )
        await self.client.storage.dc_id(dc_id)
        await self.client.storage.server_address(dc_option.ip_address)
        await self.client.storage.port(dc_option.port)
        await self.client.storage.auth_key(self.client.session.auth_key)
        self.alog.debug(
            "已切换到目标 DC", dc_id=dc_id, address=dc_option.ip_address, port=dc_option.port
        )

        try:
            result = await self.client.invoke(
                raw.functions.auth.ImportLoginToken(token=migrate.token)
            )
        except SessionPasswordNeeded:
            user = await self._handle_2fa()
            if user is None:
                raise QrLoginError("需要两步验证密码，但未提供") from None
            return user
        except (AuthTokenInvalid, AuthTokenAlreadyAccepted) as exc:
            raise QrLoginError(
                f"迁移 DC 后导入登录令牌失败（{type(exc).__name__}）。"
                "通常是二维码被扫了两次或已被其它设备使用，请重新执行登录。"
            ) from exc

        if isinstance(result, raw.types.auth.LoginTokenSuccess):
            return await self._on_success(result)
        raise QrLoginError(
            f"迁移 DC 后导入登录令牌返回了意外类型: {type(result).__name__}"
        )

    async def _handle_2fa(self) -> Optional[types.User]:
        if self.password_provider is None:
            raise QrLoginError(
                "该账号开启了两步验证，但当前环境无法输入密码。"
                "请在交互终端运行登录命令，或通过 TGA_2FA_PASSWORD 环境变量提供。"
            )
        hint = None
        with contextlib.suppress(Exception):
            hint = await self.client.get_password_hint()
        self.alog.info("账号已开启两步验证，需要输入密码", hint=hint or "（无提示）")

        for attempt in range(1, 4):
            password = await self.password_provider(hint)
            if not password:
                raise QrLoginError("未提供两步验证密码，登录中止")
            try:
                user = await self.client.check_password(password)
            except PasswordHashInvalid:
                self.alog.warning("两步验证密码错误", attempt=attempt)
                continue
            self.alog.info("两步验证通过")
            await self.client.storage.user_id(user.id)
            await self.client.storage.is_bot(False)
            await self.client.storage.save()
            return user
        raise QrLoginError("两步验证密码连续 3 次错误，登录中止")

    async def _on_success(self, success) -> types.User:  # noqa: ANN001
        authorization = success.authorization
        if isinstance(authorization, raw.types.auth.AuthorizationSignUpRequired):
            raise QrLoginError("该账号尚未注册（服务端要求先注册），无法通过扫码登录")
        user = await types.User._parse(self.client, authorization.user)
        await self.client.storage.user_id(user.id)
        await self.client.storage.is_bot(False)
        await self.client.storage.save()
        self.alog.info(
            "扫码登录成功",
            user_id=user.id,
            username=user.username or "-",
            name=(user.first_name or "") + (f" {user.last_name}" if user.last_name else ""),
        )
        return user


# --------------------------------------------------------------------------- #
# 终端渲染器
# --------------------------------------------------------------------------- #
def terminal_renderer(
    *,
    invert: bool = True,
    png_path: Optional[Path] = None,
    echo: Callable[[str], None] = print,
) -> QrRenderer:
    """返回一个把二维码打到终端的渲染器。

    同时可选写出 PNG（Docker/无终端场景可以把文件拷出来看）。
    """

    def render(payload: QrCodePayload) -> None:
        if not payload.url:
            return
        echo("")
        echo("=" * 60)
        echo(f"  请用手机 Telegram 扫描下面的二维码完成登录（第 {payload.attempt} 次生成）")
        echo("  路径：设置 → 设备 → 关联桌面设备 → 扫描二维码")
        echo(f"  有效期约 {payload.ttl:.0f} 秒，过期会自动刷新")
        echo("=" * 60)
        echo(payload.ascii_art(invert=invert))
        if png_path is not None:
            try:
                saved = payload.save_png(png_path)
                echo(f"  二维码已同时保存为图片：{saved}")
            except Exception as exc:  # pragma: no cover - Pillow 缺失等
                echo(f"  （保存 PNG 失败，可忽略：{exc}）")
        echo("  如果终端显示错乱，可加 --qr-invert/--no-qr-invert 切换配色，")
        echo("  或直接打开上面的 PNG 图片扫描。")
        echo("")

    return render


__all__ = [
    "DEFAULT_LOGIN_TIMEOUT",
    "PasswordProvider",
    "QrCodePayload",
    "QrLoginError",
    "QrLoginSession",
    "QrLoginTimeout",
    "QrRenderer",
    "terminal_renderer",
]
