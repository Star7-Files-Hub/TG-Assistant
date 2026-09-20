"""WebSocket 端点的端到端回归测试。

背景（2026-09-20 线上事故）：``tg_assistant/web/deps.py`` 里的依赖函数把参数
声明成了 ``Request``。FastAPI 只在 ``isinstance(request, Request)`` 时才注入它
（``fastapi/dependencies/utils.py``），而 WebSocket 路由传进来的是 ``WebSocket``
实例 —— 于是三个 WS 端点在**依赖解析阶段**就抛::

    TypeError: get_runtime() missing 1 required positional argument: 'request'

uvicorn 直接回 **500**。表现是：面板「实时日志」永远空白（用户原话「是摆设」）、
状态推送失效、扫码登录不可用。

老版本 FastAPI 对这个参数是**无条件注入**，所以这是升级带来的回归；
``pyproject.toml`` 里 fastapi 只写了 ``>=0.110,<1``，没锁上限。这个文件之前
**一条 WS 测试都没有**，回归才会一路绿灯。

修法：改用 ``HTTPConnection``（``Request`` 与 ``WebSocket`` 的公共基类），
FastAPI 对它是**无条件**赋值。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tg_assistant.web import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPS_PY = REPO_ROOT / "tg_assistant" / "web" / "deps.py"


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(create_app(tmp_path / "data")) as c:
        yield c


# --------------------------------------------------------------------------- #
# 端到端：三个 WS 端点都必须能握手
#
# 依赖解析失败发生在握手阶段，会直接从 ``websocket_connect`` 抛出来，
# 所以「能进 with 块」本身就是断言。
# --------------------------------------------------------------------------- #
def test_ws_logs_connects_and_replays_history(client):
    """实时日志页依赖它；历史日志应当作为首帧回放。"""
    with client.websocket_connect("/ws/logs") as ws:
        first = ws.receive_json()

    assert first["type"] == "log"
    assert "msg" in first


def test_ws_status_connects(client):
    """只验证握手 —— 这个端点只在有状态事件时才推帧，等首帧要 30 秒（心跳 ping）。"""
    with client.websocket_connect("/ws/status"):
        pass


def test_ws_login_connects(client):
    """扫码登录页依赖它。

    这里刻意用**非法账号名**让它走 ``validate_account_name`` 的早退分支：
    否则它会真的去连 Telegram，测试要干等网络超时（实测 3 分钟）。
    依赖解析失败发生在更早的握手阶段，所以这个用例照样能抓住回归。
    """
    with client.websocket_connect("/ws/login/@bad") as ws:
        first = ws.receive_json()

    assert first["type"] == "error"


# --------------------------------------------------------------------------- #
# 静态契约：不许再退回 Request
# --------------------------------------------------------------------------- #
def test_deps_only_use_http_connection():
    """用 AST 只看函数签名的注解 —— 该文件的注释/docstring 里也写着 ``Request``，
    拿原始文本断言会被自己的文档绊倒。
    """
    tree = ast.parse(DEPS_PY.read_text(encoding="utf-8"))
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
            continue
        for arg in node.args.args:
            ann = arg.annotation
            assert isinstance(ann, ast.Name) and ann.id == "HTTPConnection", (
                f"{node.name}() 的参数 {arg.arg!r} 注解是 {ast.dump(ann)}；"
                "必须是 HTTPConnection —— 声明成 Request 会让 WS 端点 500"
            )
            checked += 1

    assert checked >= 6, f"只检查到 {checked} 个依赖，deps.py 可能被改动了"
