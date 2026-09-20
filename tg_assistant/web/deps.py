"""Web 依赖注入。

🔴 **参数类型必须是 ``HTTPConnection``，不能是 ``Request``。**

FastAPI 对这三类参数的注入条件是（``fastapi/dependencies/utils.py``）::

    if dependant.http_connection_param_name:
        values[...] = request                    # ← 无条件，HTTP / WS 都注入
    if dependant.request_param_name and isinstance(request, Request):
        values[...] = request                    # ← 仅 HTTP
    elif dependant.websocket_param_name and isinstance(request, WebSocket):
        values[...] = request                    # ← 仅 WS

WebSocket 路由里 FastAPI 传进来的那个 ``request`` 其实是 ``WebSocket`` 实例，
``isinstance(ws, Request)`` 为 ``False`` ⇒ 声明成 ``Request`` 的依赖在 WS 下
**根本不会被注入**，调用时直接::

    TypeError: get_runtime() missing 1 required positional argument: 'request'

后果是整个 WS 端点 500。实测（fastapi 0.141.1）``/ws/logs``、``/ws/status``、
``/ws/login/{name}`` **三个全挂** —— 面板的实时日志永远空白、状态推送失效、
扫码登录不可用。**老版本 FastAPI 对这个参数是无条件注入，所以这是升级带来的回归**
（``pyproject.toml`` 里 fastapi 只约束了 ``>=0.110,<1``，没锁上限）。

``HTTPConnection`` 是 ``Request`` 与 ``WebSocket`` 的公共基类，上面那个分支又是
无条件赋值，所以 HTTP 和 WS 两类路由都能拿到；``.app`` / ``.scope`` 照常可用。

由 ``tests/test_web_ws.py`` 钉住：端到端连三个 WS 端点，加一条静态契约禁止退回 ``Request``。
"""
from __future__ import annotations

from typing import Any

from starlette.requests import HTTPConnection


def get_state(conn: HTTPConnection) -> Any:
    return conn.app.state


def get_settings(conn: HTTPConnection) -> Any:
    return conn.app.state.settings


def get_paths(conn: HTTPConnection) -> Any:
    return conn.app.state.paths


def get_store(conn: HTTPConnection) -> Any:
    return conn.app.state.store


def get_runtime(conn: HTTPConnection) -> Any:
    return conn.app.state.runtime


def get_web_settings(conn: HTTPConnection) -> Any:
    return conn.app.state.web_settings


__all__ = [
    "get_state",
    "get_settings",
    "get_paths",
    "get_store",
    "get_runtime",
    "get_web_settings",
]
