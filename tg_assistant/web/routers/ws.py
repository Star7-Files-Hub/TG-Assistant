"""WebSocket 路由。

实时功能：扫码登录（二维码 + 进度）、运行日志流、运行状态变更。

三个端点都受 Web 鉴权保护（见 :mod:`..auth`）：未通过校验时先推一条 error
再以 1008 关闭连接，前端据此跳转到 ``/auth``。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from .. import auth
from ..deps import get_paths, get_runtime, get_settings, get_store, get_web_settings

logger = logging.getLogger(__name__)

router = APIRouter()

#: 未授权关闭 WebSocket 时使用的状态码（policy violation）。
WS_UNAUTHORIZED_CODE = 1008

#: 等待前端提交 2FA 密码的上限（秒）。超时后按「没有密码」处理，
#: 让 pyrogram 自己报错，而不是把连接无限期挂着。
PASSWORD_WAIT_TIMEOUT = 300.0


async def _reject_unauthorized(websocket: WebSocket, web_settings: Any) -> bool:
    """鉴权未通过则告知前端并关闭连接；返回 True 表示已拒绝。"""
    if auth.is_authorized(websocket, web_settings):
        return False
    await _ws_send(
        websocket, {"type": "error", "message": "未授权：请先刷新页面并输入访问密钥"}
    )
    with contextlib.suppress(Exception):
        await websocket.close(code=WS_UNAUTHORIZED_CODE)
    return True


# --------------------------------------------------------------------------- #
# 扫码登录
# --------------------------------------------------------------------------- #
@router.websocket("/login/{name}")
async def ws_login(
    websocket: WebSocket,
    name: str,
    proxy: str = Query(""),
    force: bool = Query(False),
    store=Depends(get_store),
    settings=Depends(get_settings),
    runtime=Depends(get_runtime),
    paths=Depends(get_paths),
    web_settings=Depends(get_web_settings),
) -> None:
    """扫码登录 WebSocket。

    服务端推送：{"type": "qr", "ascii": "...", "png": "..."} →
    {"type": "status", "message": "..."} →
    {"type": "done", "user": {...}} / {"type": "error", "message": "..."}
    """
    await websocket.accept()
    if await _reject_unauthorized(websocket, web_settings):
        return

    from tg_assistant.paths import validate_account_name

    try:
        validate_account_name(name)
    except Exception as exc:
        await _ws_send(websocket, {"type": "error", "message": str(exc)})
        return

    if name in runtime._running_accounts():
        await _ws_send(websocket, {"type": "error", "message": "账号正在运行中，请先停止"})
        return

    from tg_assistant.qr_login import QrCodePayload
    from tg_assistant.runner import login_account

    async def _renderer(payload: QrCodePayload) -> None:
        """二维码渲染回调：把二维码通过 WS 推给前端。

        前端优先用 ``png_url`` 显示图片，拿不到再退回 ``ascii``。
        PNG 走 ``/qr/<name>.png`` 静态挂载，因此这里只发**URL**，不发服务器上的
        绝对路径 —— 旧实现把 ``png_path`` 一起发出去，等于把数据目录结构
        泄露给了浏览器，而浏览器根本用不了那个路径。
        """
        png_url: Optional[str] = None
        if payload.url:
            try:
                payload.save_png(paths.qr_dir / f"{name}.png")
                # 带时间戳：同名文件每次刷新内容都变，不加的话浏览器会拿缓存里的旧码。
                png_url = f"/qr/{name}.png?t={int(time.time())}"
            except Exception as exc:
                logger.warning("保存二维码 PNG 失败（不影响扫码）: %s", exc)

        await _ws_send(
            websocket,
            {
                "type": "qr",
                "ascii": payload.ascii_art(invert=True),
                "png_url": png_url,
                "expires_in": max(0, int(payload.expires_at - time.time())),
            },
        )

    # ---- 2FA 密码：由前端通过同一条 WS 送回来 ----
    # ``login_account`` 的 password_provider 是「服务端问、前端答」的异步回调，
    # 所以需要一个并行的读循环把前端发来的密码塞进队列。
    # 用 Queue 而不是一次性 Event：密码输错时 pyrogram 会再问一次，
    # 一次性 Event 在第二轮会直接挂到超时。
    passwords: asyncio.Queue[str] = asyncio.Queue()

    async def _password_provider(hint: Optional[str] = None) -> Optional[str]:
        await _ws_send(
            websocket,
            {
                "type": "need_2fa",
                "hint": hint or "",
                "message": "该账号已开启两步验证，请输入 2FA 密码",
            },
        )
        try:
            return await asyncio.wait_for(passwords.get(), timeout=PASSWORD_WAIT_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("等待 2FA 密码超时")
            return None

    async def _password_listener() -> None:
        """把前端发来的 2FA 密码转交给 provider。"""
        try:
            while True:
                data = await websocket.receive_json()
                if data.get("type") == "2fa_password":
                    await passwords.put(str(data.get("password") or ""))
        except (WebSocketDisconnect, RuntimeError):
            return

    listener = asyncio.create_task(_password_listener(), name="ws-login-2fa")

    try:
        await _ws_send(websocket, {"type": "status", "message": "正在准备登录..."})
        result = await login_account(
            name,
            store,
            settings,
            renderer=_renderer,
            password_provider=_password_provider,
            timeout=float(web_settings.login_timeout),
            proxy_override=__parse_proxy(proxy) if proxy else None,
            force=force,
        )
        await _ws_send(
            websocket,
            {
                "type": "done",
                "account": result.account,
                "user_id": result.user_id,
                "username": result.username,
                "display_name": result.display_name,
            },
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await _ws_send(websocket, {"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        if not listener.done():
            listener.cancel()


def __parse_proxy(url: str) -> Any:
    from tg_assistant.config import ProxyConfig

    return ProxyConfig.from_url(url)


# --------------------------------------------------------------------------- #
# 验证码登录
# --------------------------------------------------------------------------- #
@router.websocket("/login-code/{name}")
async def ws_login_code(
    websocket: WebSocket,
    name: str,
    proxy: str = Query(""),
    force: bool = Query(False),
    store=Depends(get_store),
    settings=Depends(get_settings),
    runtime=Depends(get_runtime),
    web_settings=Depends(get_web_settings),
) -> None:
    """验证码登录 WebSocket（手机号 + 短信验证码 + 可选 2FA）。

    与 ``/ws/login/{name}`` 的区别：扫码登录由服务端一次跑完，这里需要多轮往返，
    由前端驱动 ——

    ``send_code`` → ``code_sent`` → ``verify`` → ``need_2fa``（可选）
    → ``verify`` → ``done``。任意阶段都可以发 ``cancel`` 中止。

    无论成功、失败还是取消，``finally`` 都会 ``session.close()``：
    验证码登录的 client 是**跨多轮**活着的，不主动放掉就会一直占着
    ``<name>.session`` 的写锁，下次启动直接 ``database is locked``。
    """
    await websocket.accept()
    if await _reject_unauthorized(websocket, web_settings):
        return

    from tg_assistant.paths import validate_account_name

    try:
        validate_account_name(name)
    except Exception as exc:
        await _ws_send(websocket, {"type": "error", "message": str(exc)})
        return

    if name in runtime._running_accounts():
        await _ws_send(websocket, {"type": "error", "message": "账号正在运行中，请先停止"})
        return

    from tg_assistant.runner import CodeLoginSession, LoginResult

    session = CodeLoginSession(
        name=name,
        store=store,
        settings=settings,
        proxy_override=__parse_proxy(proxy) if proxy else None,
        force=force,
    )

    async def _send_done(result: LoginResult) -> None:
        await _ws_send(
            websocket,
            {
                "type": "done",
                "account": result.account,
                "user_id": result.user_id,
                "username": result.username,
                "display_name": result.display_name,
            },
        )

    try:
        first = await session.step(None)
        if isinstance(first, LoginResult):  # pragma: no cover - 防御性分支
            await _send_done(first)
            return
        await _ws_send(websocket, {"type": "status", "message": first})

        while True:
            data = await websocket.receive_json()
            kind = data.get("type")

            if kind == "cancel":
                await _ws_send(websocket, {"type": "error", "message": "已取消"})
                return

            if kind == "send_code":
                phone = str(data.get("phone") or "").strip()
                if not phone:
                    await _ws_send(websocket, {"type": "error", "message": "手机号不能为空"})
                    continue
                if session.step_index == 2:
                    message = await session.resend_code(phone)
                else:
                    message = await session.step(phone)
                await _ws_send(websocket, {"type": "code_sent", "message": message})

            elif kind == "verify":
                code = str(data.get("code") or "").strip()
                if not code:
                    await _ws_send(websocket, {"type": "error", "message": "验证码不能为空"})
                    continue
                outcome = await session.step((code, str(data.get("password") or "")))
                if isinstance(outcome, LoginResult):
                    await _send_done(outcome)
                    return
                # 验证码通过了，但账号开了两步验证 —— 还差密码。
                await _ws_send(websocket, {"type": "need_2fa", "message": outcome})

            else:
                await _ws_send(
                    websocket, {"type": "error", "message": f"未知消息类型：{kind}"}
                )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await _ws_send(websocket, {"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        await session.close()


# --------------------------------------------------------------------------- #
# 实时日志 + 事件
# --------------------------------------------------------------------------- #
@router.websocket("/logs")
async def ws_logs(
    websocket: WebSocket,
    runtime=Depends(get_runtime),
    web_settings=Depends(get_web_settings),
) -> None:
    """日志流 WebSocket。

    连接后先收到历史日志，之后实时推送新日志与运行事件。
    """
    await websocket.accept()
    if await _reject_unauthorized(websocket, web_settings):
        return

    queue = runtime.subscribe_logs()
    try:
        while True:
            try:
                entry = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                # 发心跳保活
                await websocket.send_json({"type": "ping"})
                continue
            if entry.get("type") == "shutdown":
                break
            await websocket.send_json(entry)
    except WebSocketDisconnect:
        pass
    finally:
        runtime.unsubscribe_logs(queue)


# --------------------------------------------------------------------------- #
# 状态推送
# --------------------------------------------------------------------------- #
@router.websocket("/status")
async def ws_status(
    websocket: WebSocket,
    runtime=Depends(get_runtime),
    web_settings=Depends(get_web_settings),
) -> None:
    """运行状态推送。

    账号启动/停止/出错时主动推送快照。
    """
    await websocket.accept()
    if await _reject_unauthorized(websocket, web_settings):
        return

    queue = runtime.subscribe_logs()
    try:
        while True:
            try:
                entry = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping"})
                continue
            if entry.get("type") == "shutdown":
                break
            # 只推送状态/事件类消息，日志交给 /ws/logs
            if entry.get("type") in {"status", "runner", "account"}:
                await websocket.send_json(entry)
    except WebSocketDisconnect:
        pass
    finally:
        runtime.unsubscribe_logs(queue)


# --------------------------------------------------------------------------- #
async def _ws_send(websocket: WebSocket, data: dict[str, Any]) -> None:
    """发送 JSON，忽略已断开的连接。"""
    with contextlib.suppress(RuntimeError, WebSocketDisconnect):
        await websocket.send_json(data)


__all__ = ["WS_UNAUTHORIZED_CODE", "router"]
