"""Web 控制台：鉴权、非法账号名、.env 加载。

这里用 ``TestClient``（httpx + ASGITransport）在进程内直接打 ASGI app，
不需要真的起 uvicorn。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tg_assistant.config import load_env_file
from tg_assistant.web import create_app
from tg_assistant.web.auth import COOKIE_NAME, cookie_value

SECRET = "s3cret-key"


@pytest.fixture
def app(tmp_path: Path):
    return create_app(tmp_path / "data")


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------------------- #
# 非法账号名：应当 400 / 重定向，而不是 500
# --------------------------------------------------------------------------- #
def test_api_invalid_account_name_returns_400(client):
    """回归：store.get_account() 抛 InvalidAccountName，之前会漏成 500。"""
    resp = client.get("/api/accounts/@bad name")
    assert resp.status_code == 400
    assert "不合法" in resp.json()["detail"]


def test_api_unknown_account_name_returns_404(client):
    """名字合法但不存在 → 仍是 404，不要和 400 混在一起。"""
    resp = client.get("/api/accounts/nobody")
    assert resp.status_code == 404


def test_page_invalid_account_name_redirects_to_accounts(client):
    """页面请求拿到非法名字时回账号列表，而不是抛 500。"""
    resp = client.get("/config/@bad name", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/accounts"


def test_api_config_invalid_account_name_returns_400(client):
    """同一个处理器要覆盖所有走账号名的入口。"""
    resp = client.put("/api/config/@bad name", json={})
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# 鉴权
# --------------------------------------------------------------------------- #
def test_disabled_auth_allows_everything(client):
    """未设密钥时不校验（此时应只监听回环地址）。"""
    assert client.get("/api/status").status_code == 200


def test_page_redirects_to_auth_when_secret_set(app, client):
    app.state.web_settings.secret_key = SECRET
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth"


def test_api_returns_401_when_secret_set(app, client):
    app.state.web_settings.secret_key = SECRET
    resp = client.get("/api/status")
    assert resp.status_code == 401


def test_static_and_auth_page_stay_public(app, client):
    app.state.web_settings.secret_key = SECRET
    assert client.get("/auth").status_code == 200
    # 登录页自身要能加载样式，否则页面裸奔
    assert client.get("/static/css/style.css").status_code == 200


def test_cookie_grants_access(app, client):
    app.state.web_settings.secret_key = SECRET
    client.cookies.set(COOKIE_NAME, cookie_value(SECRET))
    assert client.get("/api/status").status_code == 200


def test_bearer_token_grants_access(app, client):
    """脚本调用方式：Authorization: Bearer <明文密钥>。"""
    app.state.web_settings.secret_key = SECRET
    resp = client.get("/api/status", headers={"Authorization": f"Bearer {SECRET}"})
    assert resp.status_code == 200


def test_wrong_cookie_is_rejected(app, client):
    app.state.web_settings.secret_key = SECRET
    client.cookies.set(COOKIE_NAME, cookie_value("wrong"))
    assert client.get("/api/status").status_code == 401


def test_submit_sets_cookie_and_redirects(app, client):
    app.state.web_settings.secret_key = SECRET
    resp = client.post(
        "/auth", data={"secret": SECRET, "next": "/logs"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/logs"
    assert resp.cookies.get(COOKIE_NAME) == cookie_value(SECRET)


def test_submit_wrong_secret_shows_error(app, client):
    app.state.web_settings.secret_key = SECRET
    resp = client.post("/auth", data={"secret": "nope", "next": "/"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/auth?")
    assert "error=1" in resp.headers["location"]
    assert resp.cookies.get(COOKIE_NAME) is None


def test_submit_blocks_open_redirect(app, client):
    """``next=//evil.com`` 不能被当成站外跳转。"""
    app.state.web_settings.secret_key = SECRET
    resp = client.post(
        "/auth", data={"secret": SECRET, "next": "//evil.com"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_logout_clears_cookie(app, client):
    """回归：/auth/logout 必须免鉴权，否则中间件会先把它拦回 /auth。"""
    app.state.web_settings.secret_key = SECRET
    client.cookies.set(COOKIE_NAME, cookie_value(SECRET))
    resp = client.get("/auth/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth"
    assert not resp.cookies.get(COOKIE_NAME)


def test_cookie_value_is_not_the_secret():
    assert cookie_value("plain") != "plain"


# --------------------------------------------------------------------------- #
# .env 加载
# --------------------------------------------------------------------------- #
def test_load_env_file_reads_values(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("TGA_TEST_DOTENV=hello\n", encoding="utf-8")
    monkeypatch.delenv("TGA_TEST_DOTENV", raising=False)

    assert load_env_file(env_file) == str(env_file)
    assert os.environ["TGA_TEST_DOTENV"] == "hello"


def test_load_env_file_does_not_override_existing(tmp_path, monkeypatch):
    """真实环境变量优先，和 docker compose / systemd EnvironmentFile 语义一致。"""
    monkeypatch.setenv("TGA_TEST_DOTENV", "real")
    env_file = tmp_path / ".env"
    env_file.write_text("TGA_TEST_DOTENV=fromfile\n", encoding="utf-8")

    load_env_file(env_file)
    assert os.environ["TGA_TEST_DOTENV"] == "real"


def test_load_env_file_missing_returns_none(tmp_path):
    assert load_env_file(tmp_path / "nope.env") is None
