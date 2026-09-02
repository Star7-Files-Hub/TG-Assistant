"""WebSocket 路由。

实时功能：扫码登录（二维码 + 进度）、运行日志流、运行状态变更。
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from ..deps import get_runtime, get_settings, get_store

router = APIRouter()


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
) -> None:
    """扫码登录 WebSocket。

    服务端推送：{"type": "qr", "ascii": "...", "png_url": "..."} →
    {"type": "status", "message": "..."} →
    {"type": "done", "user": {...}} / {"type": "error", "message": "..."}
    """
    await websocket.accept()
    from tg_assistant.paths import validate_account_name

    try:
        validate_account_name(name)
    except Exception as exc:
        await _ws_send(websocket, {"type": "error", "message": str(exc)})
        return

    if name in runtime._running_accounts():
        await _ws_send(websocket, {"type": "error", "message": "账号正在运行中，请先停止"})
        return

    from tg_assistant.runner import login_account
    from tg_assistant.qr_login import QrRenderer, QrCodePayload

    async def _renderer(payload: QrCodePayload) -> None:
        """二维码渲染回调：把二维码通过 WS 推给前端。"""
        await _ws_send(
            websocket,
            {
                "type": "qr",
                "ascii": payload.ascii_art(invert=True),
                "png": payload.save_png(),
                "expires_in": max(0, int(payload.expires_at - time.time())),
            },
        )

    try:
        await _ws_send(websocket, {"type": "status", "message": "正在准备登录..."})
        result = await login_account(
            name,
            store,
            settings,
            renderer=_renderer,
            timeout=float(settings.web_settings.login_timeout) if hasattr(settings, "web_settings") else 300.0,
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


def __parse_proxy(url: str) -> Any:
    from tg_assistant.config import ProxyConfig

    return ProxyConfig.from_url(url)


# --------------------------------------------------------------------------- #
# 实时日志 + 事件
# --------------------------------------------------------------------------- #
@router.websocket("/logs")
async def ws_logs(
    websocket: WebSocket,
    runtime=Depends(get_runtime),
) -> None:
    """日志流 WebSocket。

    连接后先收到历史日志，之后实时推送新日志与运行事件。
    """
    await websocket.accept()
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
) -> None:
    """运行状态推送。

    账号启动/停止/出错时主动推送快照。
    """
    await websocket.accept()
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


__all__ = ["router"]
