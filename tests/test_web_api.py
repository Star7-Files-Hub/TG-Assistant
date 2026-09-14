"""新增 REST 端点：会话清除、转发规则 CRUD、抢红包 / 通知设置、单账号启停。

这些端点都是从部署版回移的，其中「清除会话」有一个数据丢失 bug，这里重点钉住：
部署版用 ``shutil.rmtree(account_paths.session_dir)`` 清本地 session，
而 ``session_dir`` 就是账号根目录 —— 用户的 ``config.json``（转发规则）
会被一起删掉。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tg_assistant.config import AccountRecord, utc_now_iso
from tg_assistant.web import create_app

NAME = "acct"


@pytest.fixture
def app(tmp_path: Path):
    return create_app(tmp_path / "data")


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def account(app):
    """注册一个账号，并造出 session 文件 + 用户改过的配置。"""
    store = app.state.store
    store.upsert_account(AccountRecord(name=NAME, created_at=utc_now_iso()))
    store.load_account_config(NAME, create=True)

    paths = store.paths.account(NAME)
    paths.session_file.write_bytes(b"stale")
    paths.session_file.with_name(f"{NAME}.session-wal").write_bytes(b"wal")
    store.paths.qr_dir.mkdir(parents=True, exist_ok=True)
    (store.paths.qr_dir / f"{NAME}.png").write_bytes(b"png")

    return store


# --------------------------------------------------------------------------- #
# 清除会话
# --------------------------------------------------------------------------- #
def test_clear_session_keeps_account_config(client, app, account) -> None:
    """回归：清除会话不能连账号配置一起删掉。"""
    paths = account.paths.account(NAME)
    config_before = paths.config_file.read_bytes()
    assert config_before

    resp = client.post(f"/api/accounts/{NAME}/clear-session")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert paths.config_file.read_bytes() == config_before, (
        "config.json 被删了 —— 用户的转发规则会丢失"
    )
    assert not paths.session_file.exists(), "session 文件必须清掉"
    assert not paths.session_file.with_name(f"{NAME}.session-wal").exists()
    assert not (account.paths.qr_dir / f"{NAME}.png").exists(), "二维码图片也要清掉"
    assert account.has_session(NAME) is False


def test_clear_session_unknown_account(client, app) -> None:
    resp = client.post("/api/accounts/ghost/clear-session")
    assert resp.status_code == 404


def test_clear_session_rejects_bad_name(client, app) -> None:
    """非法账号名要给 400，而不是 500。"""
    resp = client.post("/api/accounts/..%2Fevil/clear-session")
    assert resp.status_code in (400, 404), resp.text


# --------------------------------------------------------------------------- #
# 转发规则 CRUD
# --------------------------------------------------------------------------- #
def _rule_payload(rule_id: str = "r1") -> dict:
    return {
        "id": rule_id,
        "name": "测试规则",
        "enabled": True,
        "sources": ["@src"],
        "targets": [-1001234567890],
        "mode": "copy",
        "match": {"mode": "regex", "patterns": [r"金额[:：]\s*(\d+)"], "fields": ["text"]},
    }


def test_rules_crud_round_trip(client, app, account) -> None:
    assert client.get(f"/api/config/{NAME}/rules").json()["rules"] == []

    created = client.post(f"/api/config/{NAME}/rules", json=_rule_payload())
    assert created.status_code == 200, created.text
    assert created.json()["rule"]["id"] == "r1"

    listed = client.get(f"/api/config/{NAME}/rules").json()
    assert [r["id"] for r in listed["rules"]] == ["r1"]

    updated = client.put(
        f"/api/config/{NAME}/rules/r1", json={**_rule_payload(), "name": "改名了"}
    )
    assert updated.status_code == 200, updated.text
    assert client.get(f"/api/config/{NAME}/rules").json()["rules"][0]["name"] == "改名了"

    deleted = client.delete(f"/api/config/{NAME}/rules/r1")
    assert deleted.status_code == 200
    assert client.get(f"/api/config/{NAME}/rules").json()["rules"] == []


def test_rules_add_rejects_duplicate_id(client, app, account) -> None:
    assert client.post(f"/api/config/{NAME}/rules", json=_rule_payload()).status_code == 200
    dup = client.post(f"/api/config/{NAME}/rules", json=_rule_payload())
    assert dup.status_code == 409


def test_rules_add_rejects_invalid_payload(client, app, account) -> None:
    resp = client.post(f"/api/config/{NAME}/rules", json={"id": "x"})
    assert resp.status_code == 400


def test_rules_update_and_delete_missing_are_404(client, app, account) -> None:
    assert client.put(f"/api/config/{NAME}/rules/nope", json=_rule_payload("nope")).status_code == 404
    assert client.delete(f"/api/config/{NAME}/rules/nope").status_code == 404


def test_rules_list_unknown_account_is_404(client, app) -> None:
    assert client.get("/api/config/ghost/rules").status_code == 404


def test_forward_enabled_toggle(client, app, account) -> None:
    resp = client.put(f"/api/config/{NAME}/forward-enabled", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    assert client.get(f"/api/config/{NAME}/rules").json()["enabled"] is False


# --------------------------------------------------------------------------- #
# 规则试跑
# --------------------------------------------------------------------------- #
def test_rules_test_regex_match_and_groups(client, app, account) -> None:
    resp = client.post(
        f"/api/config/{NAME}/rules/test",
        json={"pattern": r"金额[:：]\s*(\d+)", "text": "今天金额：128 元", "mode": "regex"},
    )
    body = resp.json()
    assert body["match"] is True
    assert body["groups"] == ["128"]


def test_rules_test_no_match(client, app, account) -> None:
    resp = client.post(
        f"/api/config/{NAME}/rules/test",
        json={"pattern": "不存在的词", "text": "完全无关的内容", "mode": "regex"},
    )
    assert resp.json()["match"] is False


def test_rules_test_invalid_regex_reports_error(client, app, account) -> None:
    resp = client.post(
        f"/api/config/{NAME}/rules/test",
        json={"pattern": "([", "text": "abc", "mode": "regex"},
    )
    body = resp.json()
    assert body["match"] is False
    assert "无效" in body["error"]


def test_rules_test_contains_and_exact_modes(client, app, account) -> None:
    contains = client.post(
        f"/api/config/{NAME}/rules/test",
        json={"pattern": "HELLO", "text": "say hello now", "mode": "contains"},
    )
    assert contains.json()["match"] is True

    exact = client.post(
        f"/api/config/{NAME}/rules/test",
        json={"pattern": "hello", "text": "HELLO", "mode": "exact"},
    )
    assert exact.json()["match"] is True


def test_rules_test_requires_pattern_and_text(client, app, account) -> None:
    assert (
        client.post(f"/api/config/{NAME}/rules/test", json={"pattern": "", "text": "x"}).json()["match"]
        is False
    )
    assert (
        client.post(f"/api/config/{NAME}/rules/test", json={"pattern": "x", "text": ""}).json()["match"]
        is False
    )


# --------------------------------------------------------------------------- #
# 抢红包
# --------------------------------------------------------------------------- #
def test_red_packet_get_put_and_toggle(client, app, account) -> None:
    current = client.get(f"/api/config/{NAME}/red_packet")
    assert current.status_code == 200

    payload = current.json()
    payload["enabled"] = True
    saved = client.put(f"/api/config/{NAME}/red_packet", json=payload)
    assert saved.status_code == 200, saved.text

    toggled = client.put(f"/api/config/{NAME}/red_packet/enabled", json={"enabled": False})
    assert toggled.status_code == 200
    assert toggled.json()["enabled"] is False
    assert client.get(f"/api/config/{NAME}/red_packet").json()["enabled"] is False


def test_red_packet_rejects_invalid_payload(client, app, account) -> None:
    resp = client.put(f"/api/config/{NAME}/red_packet", json={"enabled": "not-a-bool"})
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# 通知
# --------------------------------------------------------------------------- #
REAL_TOKEN = "123456789:REAL-SECRET-TOKEN"


def _notify_payload() -> dict:
    return {
        "enabled": True,
        "bot_token": REAL_TOKEN,
        "chat_id": -1001234567890,
        "mode": "copy",
        "events": ["forward"],
    }


def test_notify_get_masks_token(client, app, account) -> None:
    assert client.put(f"/api/config/{NAME}/notify", json=_notify_payload()).status_code == 200

    body = client.get(f"/api/config/{NAME}/notify").json()

    assert body["bot_token"] != REAL_TOKEN
    assert body["bot_token"].endswith("***")
    assert REAL_TOKEN.startswith(body["bot_token"][:8])


def test_notify_put_keeps_real_token_when_masked_value_round_trips(client, app, account) -> None:
    """回归：面板把脱敏后的 token 原样提交回来时，要保留真 token 并正常保存。

    面板的通知页是「GET 整份配置 → 编辑 → PUT 整份配置」的用法，
    而 GET 回显的 ``bot_token`` 是 ``12345678***``。若不做处理，PUT 会撞上
    ``NotifyConfig`` 的 token 格式校验，返回 400 —— 数据不会丢，但用户
    一按保存就报「格式不对」，而那个值明明是他刚从页面上读到的。

    所以这里要求：**接受**脱敏值并保留原有 token（而不是报错）。
    """
    client.put(f"/api/config/{NAME}/notify", json=_notify_payload())

    masked = client.get(f"/api/config/{NAME}/notify").json()
    assert masked["bot_token"].endswith("***")

    resp = client.put(f"/api/config/{NAME}/notify", json=masked)

    assert resp.status_code == 200, resp.text
    stored = account.load_account_config(NAME, create=False)
    assert stored.notify.bot_token == REAL_TOKEN, "真 token 被脱敏值覆盖了"


def test_notify_put_accepts_a_new_token(client, app, account) -> None:
    client.put(f"/api/config/{NAME}/notify", json=_notify_payload())

    fresh = {**_notify_payload(), "bot_token": "999:BRAND-NEW"}
    assert client.put(f"/api/config/{NAME}/notify", json=fresh).status_code == 200

    assert account.load_account_config(NAME, create=False).notify.bot_token == "999:BRAND-NEW"


# --------------------------------------------------------------------------- #
# 单账号启停
# --------------------------------------------------------------------------- #
def test_run_start_one_unknown_account_is_404(client, app) -> None:
    assert client.post("/api/run/start/ghost").status_code == 404


def test_run_stop_one_unknown_account_is_404(client, app) -> None:
    assert client.post("/api/run/stop/ghost").status_code == 404


def test_run_stop_one_when_nothing_running(client, app, account) -> None:
    """没有账号在跑时停止单个账号：返回 ok=False，但不能 500、也不能挂住。"""
    resp = client.post(f"/api/run/stop/{NAME}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


# --------------------------------------------------------------------------- #
# 登录入口的字段契约
# --------------------------------------------------------------------------- #
def test_login_api_requires_account_field(client, app) -> None:
    """``account`` 是必填的 —— 前端漏传必须是 422，而不是静默生成一个随机名。

    部署版是服务端随机生成 ``tg_<hex>``，前端因此可以不传 account；仓库版改成
    由用户指定账号名后，这个字段就成了必填。把「必填」这件事钉住，
    免得哪天有人为了"兼容"又把它改成可选、悄悄退回随机命名。
    """
    resp = client.post("/api/accounts/login", data={"proxy": ""})

    assert resp.status_code == 422, resp.text


def test_login_api_returns_ws_url_for_valid_account(client, app) -> None:
    resp = client.post("/api/accounts/login", data={"account": NAME, "proxy": "", "force": "true"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["account"] == NAME
    assert body["ws_url"].startswith(f"/ws/login/{NAME}?")


def test_login_api_rejects_illegal_account_name(client, app) -> None:
    """非法账号名要给 400（而不是 500）—— 走的是全局 InvalidAccountName 处理器。"""
    resp = client.post("/api/accounts/login", data={"account": "../escape"})

    assert resp.status_code == 400, resp.text
