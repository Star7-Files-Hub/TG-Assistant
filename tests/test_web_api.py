"""新增 REST 端点：会话清除、转发规则 CRUD、抢红包 / 通知设置、单账号启停。

这些端点都是从部署版回移的，其中「清除会话」有一个数据丢失 bug，这里重点钉住：
部署版用 ``shutil.rmtree(account_paths.session_dir)`` 清本地 session，
而 ``session_dir`` 就是账号根目录 —— 用户的 ``config.json``（转发规则）
会被一起删掉。
"""

from __future__ import annotations

import json
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


#: 第二个账号的名字。扇出语义至少要两个账号才测得出来。
NAME2 = "second"


@pytest.fixture
def two_accounts(app):
    """两个账号，用来验证「不选账号 = 全部账号」的扇出行为。"""
    store = app.state.store
    for name in (NAME, NAME2):
        store.upsert_account(AccountRecord(name=name, created_at=utc_now_iso()))
        store.load_account_config(name, create=True)
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
# 账号级「排除频道」
# --------------------------------------------------------------------------- #
def test_forward_exclude_chats_round_trip(client, app, account) -> None:
    """写进去能读回来，且 @ 前缀 / 大小写会被归一化。"""
    resp = client.put(
        f"/api/config/{NAME}/forward-exclude-chats",
        json={"exclude_chats": [-1002626018568, "@Noisy_Channel"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["exclude_chats"] == [-1002626018568, "noisy_channel"]

    # 总览接口也要带上，否则面板刷新后列表就"消失"了
    row = next(a for a in client.get("/api/rules").json()["accounts"] if a["name"] == NAME)
    assert row["exclude_chats"] == [-1002626018568, "noisy_channel"]


def test_forward_exclude_chats_can_be_cleared(client, app, account) -> None:
    assert (
        client.put(f"/api/config/{NAME}/forward-exclude-chats", json={"exclude_chats": [1]}).status_code
        == 200
    )
    for body in ({"exclude_chats": []}, {}, {"exclude_chats": None}):
        resp = client.put(f"/api/config/{NAME}/forward-exclude-chats", json=body)
        assert resp.status_code == 200, resp.text
        assert resp.json()["exclude_chats"] == []


def test_forward_exclude_chats_rejects_non_list(client, app, account) -> None:
    resp = client.put(f"/api/config/{NAME}/forward-exclude-chats", json={"exclude_chats": "nope"})
    assert resp.status_code == 400


def test_forward_exclude_chats_unknown_account_is_404(client, app) -> None:
    resp = client.put("/api/config/ghost/forward-exclude-chats", json={"exclude_chats": []})
    assert resp.status_code == 404


def test_forward_exclude_chats_does_not_clobber_rules(client, app, account) -> None:
    """改排除列表不能把已有规则顺手弄丢（整个 forward 对象是重建的）。"""
    assert client.post(f"/api/config/{NAME}/rules", json=_rule_payload()).status_code == 200
    assert (
        client.put(f"/api/config/{NAME}/forward-exclude-chats", json={"exclude_chats": [-100999]}).status_code
        == 200
    )
    body = client.get(f"/api/config/{NAME}/rules").json()
    assert [r["id"] for r in body["rules"]] == ["r1"]
    assert body["rules"][0]["targets"] == [-1001234567890]


# --------------------------------------------------------------------------- #
# 全局排除名单（所有账号共用一份，data/forward_excludes.json）
#
# 上面那两节是**账号级**的（存在各自 config.json 里）。这一节是用户要的那份
# 「全局」：写一次管所有账号。线上两个账号的名单本来完全一样
# （2026-09-29 取证：-1003932130542 / 8817602576），同一份名单存了两遍。
# --------------------------------------------------------------------------- #
def test_rules_overview_exposes_global_excludes(client, app, two_accounts) -> None:
    """总览接口顶层带一份 ``global_excludes`` —— 不在每个账号里各带一份。

    塞进每个账号就等于把"两个号存着同一份名单"这个毛病原样搬到接口里，
    面板也会被渲染成"每个账号一份"，用户又得填两遍。
    """
    body = client.get("/api/rules").json()

    assert body["global_excludes"] == {
        "exclude_chats": [],
        "exclude_users": [],
        # 读文件失败过几次。>0 说明磁盘上那份没读到、此刻按空名单在跑，
        # 面板据此提示 —— 否则用户会对着一个"看起来配好了"的界面发呆。
        "load_errors": 0,
    }


def test_rules_overview_has_global_excludes_without_any_account(client, app) -> None:
    """一个账号都没有时也要给得出这份名单。

    新装的环境里"先把这些群排除掉"正是第一件事；接口要是跟着账号列表一起
    返空，页面顶部的区块就会缺失（或者 JS 读到 undefined）。
    """
    assert client.get("/api/rules").json()["global_excludes"]["exclude_chats"] == []


def test_global_excludes_round_trip(client, app, account) -> None:
    """写进去能读回来（归一化），落盘在 ``data/forward_excludes.json``。

    ⚠️ 同时钉住"**没有**写进任何账号的 config.json" —— 那正是这个功能的全部意义。
    如果一个顺手实现把它塞进了本账号配置，功能看起来一样能用（单账号时），
    但第二个账号就漏了。
    """
    resp = client.put(
        "/api/forward-excludes",
        json={"exclude_chats": ["-1003932130542", "@Noisy_Channel"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["exclude_chats"] == [-1003932130542, "noisy_channel"], "数字 id 必须归一化成 int"
    assert body["exclude_users"] == [], "没提交的那个键要保持空，不能被清成一个错误的值"

    # 面板刷新走的是总览接口，它不带上的话顶部区块会"消失"
    assert client.get("/api/rules").json()["global_excludes"]["exclude_chats"] == [
        -1003932130542,
        "noisy_channel",
    ]

    # 真的落到了跨账号那份文件里
    path = account.paths.forward_excludes_file
    assert path.exists(), "全局名单没有落盘到 data/forward_excludes.json"
    assert json.loads(path.read_text(encoding="utf-8"))["exclude_chats"] == [
        -1003932130542,
        "noisy_channel",
    ]

    row = next(a for a in client.get("/api/rules").json()["accounts"] if a["name"] == NAME)
    assert row["exclude_chats"] == [], "全局名单被写进账号配置了 —— 那就不是跨账号一份"


def test_global_excludes_partial_update_keeps_the_other_key(client, app, account) -> None:
    """两个名单各改各的：面板一次只提交一个键，不能把另一个覆盖掉。

    刻意做成部分更新（而不是让前端先读后写整份）：两个键互相覆盖的话，
    表现就是"刚加完黑名单，排除频道就没了"，而且**没有任何报错**。
    """
    assert (
        client.put("/api/forward-excludes", json={"exclude_chats": [-100999]}).status_code
        == 200
    )
    body = client.put("/api/forward-excludes", json={"exclude_users": [555]}).json()

    assert body["exclude_chats"] == [-100999], "写黑名单把「排除频道」覆盖掉了"
    assert body["exclude_users"] == [555]
    # 再读一次，确认是磁盘上的状态而不只是本次响应拼出来的
    again = client.get("/api/rules").json()["global_excludes"]
    assert again["exclude_chats"] == [-100999]
    assert again["exclude_users"] == [555]


def test_global_excludes_can_be_cleared(client, app, account) -> None:
    """空数组 = 清空（与账号级那两个端点一致），用于"这个群现在要转了"。"""
    assert (
        client.put("/api/forward-excludes", json={"exclude_chats": [-100999]}).status_code
        == 200
    )

    resp = client.put("/api/forward-excludes", json={"exclude_chats": []})

    assert resp.status_code == 200, resp.text
    assert resp.json()["exclude_chats"] == []


def test_global_excludes_unknown_field_is_400(client, app, account) -> None:
    """不认识的字段要 400，并且**说出来是哪个**。

    静默忽略的话，前端把 ``exclude_chatz`` 拼错时会得到"保存成功"，
    名单却没变 —— 用户完全无从下手。
    """
    resp = client.put("/api/forward-excludes", json={"exclude_chatz": []})

    assert resp.status_code == 400
    assert "exclude_chatz" in resp.json()["detail"]
    assert not account.paths.forward_excludes_file.exists(), "400 时不该落盘"


def test_global_excludes_empty_body_is_400(client, app, account) -> None:
    """空 body 是客户端 bug，不能当"什么都不改"静默成功。

    返回 200 会让面板显示"已保存"，而用户刚输入的那一项其实一条都没进去。
    """
    resp = client.put("/api/forward-excludes", json={})

    assert resp.status_code == 400
    assert not account.paths.forward_excludes_file.exists()


def test_global_excludes_invalid_value_is_400_and_keeps_the_old_file(client, app, account) -> None:
    """归一化不了的形状要 400，且**磁盘上那份好名单原样不动**。

    注意单个字符串（``"@Foo"``）是**合法**的（与账号级一致：就排除这一个），
    所以"非法值"只能是根本没法归一化的形状 —— 这里是浮点数
    （归一化时要迭代它，当场抛错）。校验通过之前绝不能先写盘，
    否则用户手抖一次就把好名单换成了一份没用的。
    """
    assert (
        client.put("/api/forward-excludes", json={"exclude_chats": [-100999]}).status_code
        == 200
    )
    path = account.paths.forward_excludes_file
    before = path.read_bytes()

    resp = client.put("/api/forward-excludes", json={"exclude_chats": 1.5})

    assert resp.status_code == 400, resp.text
    assert path.read_bytes() == before, "校验失败却把旧名单改了"


def test_rules_overview_reports_global_excludes_load_errors(client, app, account) -> None:
    """读坏时面板要能看出来"此刻按空名单在跑"。

    只把 ``load_errors`` 记在 store 里、不往接口上带的话，这个数字谁都看不到，
    面板仍然显示一屏正常的"未设置"，而磁盘上那份名单其实压根没读到。
    """
    path = account.paths.forward_excludes_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 这不是合法 JSON", encoding="utf-8")

    body = client.get("/api/rules").json()["global_excludes"]

    assert body["load_errors"] == 1
    assert body["exclude_chats"] == [] and body["exclude_users"] == []


# --------------------------------------------------------------------------- #

def test_forward_exclude_users_round_trip(client, app, account) -> None:
    """写进去能读回来，且 @ 前缀 / 大小写会被归一化。"""
    resp = client.put(
        f"/api/config/{NAME}/forward-exclude-users",
        json={"exclude_users": [555, "@Spammer"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["exclude_users"] == [555, "spammer"]

    # 总览接口也要带上，否则面板刷新后黑名单就"消失"了
    row = next(a for a in client.get("/api/rules").json()["accounts"] if a["name"] == NAME)
    assert row["exclude_users"] == [555, "spammer"]


def test_forward_exclude_users_can_be_cleared(client, app, account) -> None:
    assert (
        client.put(f"/api/config/{NAME}/forward-exclude-users", json={"exclude_users": [555]}).status_code
        == 200
    )
    for body in ({"exclude_users": []}, {}, {"exclude_users": None}):
        resp = client.put(f"/api/config/{NAME}/forward-exclude-users", json=body)
        assert resp.status_code == 200, resp.text
        assert resp.json()["exclude_users"] == []


def test_forward_exclude_users_rejects_non_list(client, app, account) -> None:
    resp = client.put(f"/api/config/{NAME}/forward-exclude-users", json={"exclude_users": "nope"})
    assert resp.status_code == 400


def test_forward_exclude_users_unknown_account_is_404(client, app) -> None:
    resp = client.put("/api/config/ghost/forward-exclude-users", json={"exclude_users": []})
    assert resp.status_code == 404


def test_forward_exclude_users_is_independent_of_chats_and_rules(client, app, account) -> None:
    """三个东西互不覆盖。

    两个名单都是「整个 forward 对象重建」的写法，很容易在重建时把另一个顺手清掉 ——
    那会变成"加个黑名单，排除频道就没了"，而且**没有任何报错**。
    """
    assert client.post(f"/api/config/{NAME}/rules", json=_rule_payload()).status_code == 200
    assert (
        client.put(f"/api/config/{NAME}/forward-exclude-chats", json={"exclude_chats": [-100999]}).status_code
        == 200
    )
    assert (
        client.put(f"/api/config/{NAME}/forward-exclude-users", json={"exclude_users": [555]}).status_code
        == 200
    )
    row = next(a for a in client.get("/api/rules").json()["accounts"] if a["name"] == NAME)
    assert row["exclude_chats"] == [-100999], "写黑名单不能把「排除频道」清掉"
    assert row["exclude_users"] == [555]
    assert [r["id"] for r in row["rules"]] == ["r1"], "写黑名单不能把规则清掉"


# --------------------------------------------------------------------------- #
# 「已使用注册码」拦截
# --------------------------------------------------------------------------- #
def test_used_codes_round_trip(client, app, account) -> None:
    """写进去能读回来 —— 总览接口必须带上它，否则面板一刷新设置就"消失"。"""
    resp = client.put(
        f"/api/config/{NAME}/forward-used-codes",
        json={
            "enabled": True,
            "notice_keywords": ["码使用", "已使用"],
            "notice_pattern": r"使用[了]?\s*([A-Za-z0-9][^\s，。、]*)",
            "min_visible": 4,
            "ttl": 1800,
            "persist": True,
        },
    )
    assert resp.status_code == 200, resp.text

    row = next(a for a in client.get("/api/rules").json()["accounts"] if a["name"] == NAME)
    assert row["used_codes"]["min_visible"] == 4
    assert row["used_codes"]["ttl"] == 1800
    assert row["used_codes"]["notice_keywords"] == ["码使用", "已使用"]


def test_used_codes_ignore_shape_round_trip(client, app, account) -> None:
    """「忽略形状」也要能读写 —— 默认值就是线上那种不会被转发的形状。"""
    row = next(a for a in client.get("/api/rules").json()["accounts"] if a["name"] == NAME)
    assert row["used_codes"]["ignore_token_pattern"] == r"^\d+-\w+$"

    resp = client.put(
        f"/api/config/{NAME}/forward-used-codes", json={"ignore_token_pattern": ""}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["used_codes"]["ignore_token_pattern"] == ""
    row = next(a for a in client.get("/api/rules").json()["accounts"] if a["name"] == NAME)
    assert row["used_codes"]["ignore_token_pattern"] == ""


def test_used_codes_rejects_bad_ignore_pattern(client, app, account) -> None:
    resp = client.put(
        f"/api/config/{NAME}/forward-used-codes",
        json={"ignore_token_pattern": "([A-Za-z0-9"},
    )
    assert resp.status_code == 400


def test_metrics_endpoint_shape_and_auth(client, app, account) -> None:
    """数据大盘：三个口径一次返回，「天」要写明是北京时间。"""
    response = client.get("/api/metrics")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["timezone"] == "北京时间 (UTC+8)"
    assert set(payload["ranges"]) == {"day", "month", "total"}
    for name, view in payload["ranges"].items():
        assert view["label"], f"{name} 少了标题，面板上会显示成空白"
        assert set(view) >= {"forward", "red_packet", "reg_grab"}
    # 用户在需求里专门写了「按北京时间0点开始计算」，这个口径必须能被界面表达出来。
    assert "北京时间" in payload["timezone"]


def test_metrics_reads_history_from_disk_without_a_runner(client, app, account) -> None:
    """面板「还没点启动」时也要看得见历史 —— 这些数是从磁盘读的，不是运行期才有的。

    顺带钉住：接口惰性建出来的实例要落盘，重启进程后历史还在。
    """
    from tg_assistant.metrics import MetricsStore

    store = app.state.store
    seeded = MetricsStore(state_path=store.paths.metrics_file)
    seeded.record("forward")
    seeded.record("forward")
    seeded.record("red_packet")

    payload = client.get("/api/metrics").json()
    assert payload["ranges"]["day"]["forward"] == 2
    assert payload["ranges"]["day"]["red_packet"] == 1
    assert payload["ranges"]["total"]["forward"] == 2
    assert payload["ranges"]["month"]["reg_grab"] == 0


def test_used_codes_stats_present_without_runtime(client, app, account) -> None:
    """没跑起来时统计也要在，而且是 0 —— 面板拿到 ``undefined`` 会显示成字面量。"""
    row = next(a for a in client.get("/api/rules").json()["accounts"] if a["name"] == NAME)
    assert row["used_code_stats"] == {"learned": 0, "skipped": 0, "known": 0}


def test_used_codes_partial_update_keeps_other_fields(client, app, account) -> None:
    """只改一个字段不能把没传的字段打回默认值。

    面板只暴露 5 个控件（没有 ``persist``）—— 从零构造会把落盘开关悄悄重置成
    默认值，用户改个时长就丢了设置，而且毫无提示。
    """
    assert (
        client.put(
            f"/api/config/{NAME}/forward-used-codes",
            json={"enabled": True, "persist": False, "ttl": 7200},
        ).status_code
        == 200
    )
    resp = client.put(f"/api/config/{NAME}/forward-used-codes", json={"min_visible": 5})
    assert resp.status_code == 200, resp.text
    assert resp.json()["used_codes"]["persist"] is False, "没传 persist 不能被重置"
    assert resp.json()["used_codes"]["ttl"] == 7200, "没传 ttl 不能被重置"
    assert resp.json()["used_codes"]["min_visible"] == 5


def test_used_codes_accepts_nested_payload(client, app, account) -> None:
    resp = client.put(
        f"/api/config/{NAME}/forward-used-codes",
        json={"used_codes": {"enabled": False}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["used_codes"]["enabled"] is False


def test_used_codes_rejects_bad_regex_with_readable_error(client, app, account) -> None:
    """正则是最容易写错的一项，报错必须是人能看懂的中文，而不是 pydantic 那一大段。"""
    resp = client.put(
        f"/api/config/{NAME}/forward-used-codes",
        json={"notice_pattern": "([A-Za-z0-9"},
    )
    assert resp.status_code == 400
    assert "正则" in resp.json()["detail"]


def test_used_codes_rejects_out_of_range(client, app, account) -> None:
    assert (
        client.put(f"/api/config/{NAME}/forward-used-codes", json={"min_visible": 0}).status_code == 400
    )
    assert client.put(f"/api/config/{NAME}/forward-used-codes", json={"min_visible": 99}).status_code == 400


def test_used_codes_rejects_unknown_field(client, app, account) -> None:
    resp = client.put(f"/api/config/{NAME}/forward-used-codes", json={"nonsense": 1})
    assert resp.status_code == 400


def test_used_codes_unknown_account_is_404(client, app) -> None:
    resp = client.put("/api/config/ghost/forward-used-codes", json={"enabled": True})
    assert resp.status_code == 404


def test_used_codes_does_not_clobber_rules_or_excludes(client, app, account) -> None:
    """四个东西互不覆盖 —— 它们都是「整个 forward 对象重建」的写法。

    重建时顺手把兄弟字段清掉，会变成"加个已用码拦截，规则就没了"，而且**没有任何报错**。
    """
    assert client.post(f"/api/config/{NAME}/rules", json=_rule_payload()).status_code == 200
    assert (
        client.put(f"/api/config/{NAME}/forward-exclude-chats", json={"exclude_chats": [-100999]}).status_code
        == 200
    )
    assert (
        client.put(f"/api/config/{NAME}/forward-exclude-users", json={"exclude_users": [555]}).status_code
        == 200
    )
    assert client.put(f"/api/config/{NAME}/forward-used-codes", json={"min_visible": 5}).status_code == 200

    row = next(a for a in client.get("/api/rules").json()["accounts"] if a["name"] == NAME)
    assert [r["id"] for r in row["rules"]] == ["r1"], "写已用码设置不能把规则清掉"
    assert row["exclude_chats"] == [-100999]
    assert row["exclude_users"] == [555]
    assert row["used_codes"]["min_visible"] == 5


def test_used_codes_defaults_are_on(client, app, account) -> None:
    """默认开着 —— 用户没配过也该拦住已经用掉的码（这正是他提这个需求的场景）。"""
    row = next(a for a in client.get("/api/rules").json()["accounts"] if a["name"] == NAME)
    assert row["used_codes"]["enabled"] is True
    assert row["used_codes"]["min_visible"] == 3
    assert row["used_codes"]["ttl"] == 3600.0


# --------------------------------------------------------------------------- #
# 「已使用注册码」拦截 —— 全局（所有账号共用一份）
#
# 用户原话：「将……已使用注册码拦截做成全局，而不是账号级」。
# 线上三个账号（小白 / SevenStar / 只想睡觉）的这份策略一字不差完全相同，
# 同一份存了三遍、面板上还得填三遍 —— 下面钉住"写一次、所有账号一致"。
# --------------------------------------------------------------------------- #
def test_global_used_codes_round_trip(client, app, account) -> None:
    """写进全局端点 → 总览接口顶层 ``global_used_codes`` 必须带回来。"""
    resp = client.put(
        "/api/forward-used-codes",
        json={
            "enabled": True,
            "notice_keywords": ["码使用", "已使用"],
            "min_visible": 4,
            "ttl": 1800,
            "persist": True,
        },
    )
    assert resp.status_code == 200, resp.text

    payload = client.get("/api/rules").json()
    assert payload["global_used_codes"]["min_visible"] == 4
    assert payload["global_used_codes"]["ttl"] == 1800
    assert payload["global_used_codes"]["notice_keywords"] == ["码使用", "已使用"]


def test_global_used_codes_is_shared_across_accounts(client, app, two_accounts) -> None:
    """全局策略写一次，**每个**账号读到的都是同一份 —— 这就是"全局"的定义。"""
    assert (
        client.put("/api/forward-used-codes", json={"min_visible": 5}).status_code == 200
    )

    rows = {a["name"]: a for a in client.get("/api/rules").json()["accounts"]}
    assert rows[NAME]["used_codes"]["min_visible"] == 5
    assert rows[NAME2]["used_codes"]["min_visible"] == 5, "第二个账号没跟着一起生效"


def test_legacy_account_used_codes_endpoint_writes_global(client, app, account) -> None:
    """旧的账号级端点保留可用，但它**写的是全局那份**（不再各写各的）。

    老前端 / 老脚本还在调 ``/api/config/{name}/forward-used-codes``：让它继续能存，
    但存进全局配置 —— 否则同一个界面（老页面）在 A 账号保存只会改 A，
    与"一处配置、全账号一致"直接冲突。
    """
    resp = client.put(
        f"/api/config/{NAME}/forward-used-codes", json={"min_visible": 6}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json().get("scope") == "global", "旧端点要显式告诉调用方写的是全局"

    payload = client.get("/api/rules").json()
    assert payload["global_used_codes"]["min_visible"] == 6


def test_legacy_account_used_codes_endpoint_still_404s_unknown_account(client, app) -> None:
    """号名笔误照旧 404 —— 不因为"反正写全局"就把明显的错悄悄咽下去。"""
    resp = client.put("/api/config/ghost/forward-used-codes", json={"enabled": True})
    assert resp.status_code == 404


def test_global_used_codes_rejects_bad_regex_with_readable_error(client, app) -> None:
    """正则是最容易写错的一项，报错必须是人能看懂的中文，而不是 pydantic 那一大段。"""
    resp = client.put("/api/forward-used-codes", json={"notice_pattern": "([A-Za-z0-9"})
    assert resp.status_code == 400
    assert "正则" in resp.json()["detail"]


def test_global_used_codes_rejects_unknown_field(client, app) -> None:
    resp = client.put("/api/forward-used-codes", json={"nonsense": 1})
    assert resp.status_code == 400


def test_global_used_codes_partial_update_keeps_other_fields(client, app) -> None:
    """只改一个字段不能把没传的字段打回默认值（面板不暴露 ``persist``）。"""
    assert (
        client.put(
            "/api/forward-used-codes",
            json={"enabled": True, "persist": False, "ttl": 7200},
        ).status_code
        == 200
    )
    resp = client.put("/api/forward-used-codes", json={"min_visible": 5})
    assert resp.status_code == 200, resp.text
    assert resp.json()["used_codes"]["persist"] is False, "没传 persist 不能被重置"
    assert resp.json()["used_codes"]["ttl"] == 7200, "没传 ttl 不能被重置"
    assert resp.json()["used_codes"]["min_visible"] == 5


def test_global_used_codes_accepts_nested_payload(client, app) -> None:
    resp = client.put(
        "/api/forward-used-codes", json={"used_codes": {"enabled": False}}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["used_codes"]["enabled"] is False


def test_global_used_codes_does_not_touch_account_configs(client, app, account) -> None:
    """写全局策略**不能**顺手改账号 ``config.json``（规则 / 排除名单一个都不能动）。

    全局化最容易踩的坑就是"顺手把账号配置也重写一遍"，于是加个拦截、规则就没了。
    """
    assert client.post(f"/api/config/{NAME}/rules", json=_rule_payload()).status_code == 200
    assert (
        client.put(
            f"/api/config/{NAME}/forward-exclude-chats", json={"exclude_chats": [-100999]}
        ).status_code
        == 200
    )
    config_path = account.paths.account(NAME).config_file
    before = config_path.read_bytes()

    assert (
        client.put("/api/forward-used-codes", json={"min_visible": 5}).status_code == 200
    )

    assert config_path.read_bytes() == before, "写全局策略不该动到账号 config.json"
    row = next(a for a in client.get("/api/rules").json()["accounts"] if a["name"] == NAME)
    assert [r["id"] for r in row["rules"]] == ["r1"], "规则不能被清掉"
    assert row["exclude_chats"] == [-100999]


def test_global_used_codes_surfaces_load_errors(client, app) -> None:
    """>0 说明磁盘上那份策略没读到，此刻按缺省策略在跑 —— 面板要靠它报警。"""
    store = app.state.store
    path = store.paths.forward_used_codes_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 这不是合法 JSON", encoding="utf-8")

    payload = client.get("/api/rules").json()
    assert payload["global_used_codes"]["load_errors"] >= 1


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


def test_rules_test_reports_the_exclude_rule_that_blocked_it(client, app, account) -> None:
    """主条件命中了，但被排除规则拦下 —— 必须返回「没命中/被拦」并点名是哪条排除规则。

    🔴 用户反馈「为什么21点一直命中，我已经加了排除」：面板「测试」按钮原来只把主正则
    发给服务端，``exclude_patterns`` 一个字都没发，于是往「排除规则」里加了 21点
    之后测试按钮照样显示「匹配成功」—— 用户就认定"排除没生效"（引擎那边其实生效了）。
    """
    body = client.post(
        "/api/rules/test",
        json={
            "patterns": ["21点"],
            "exclude_patterns": ["21点"],
            "text": "今晚21点开抢",
            "mode": "regex",
        },
    ).json()

    assert body["match"] is False, "被排除规则命中的消息不能算命中（引擎不会转发）"
    assert body["excluded"] is True
    assert body["excluded_by"] == ["21点"], "必须点名是哪条排除规则拦下的"
    assert "21点" in body["message"]
    # 还要告诉用户"主条件本来匹配到了什么"，否则他只会觉得莫名其妙
    assert body["primary"]["full_match"] == "21点"
    assert body["primary"]["pattern"] == "21点"


def test_rules_test_does_not_blame_excludes_when_the_main_pattern_misses(
    client, app, account
) -> None:
    """主条件不命中时**不能**说"被排除" —— 那是误报，用户会去查一条无辜的排除规则。"""
    body = client.post(
        "/api/rules/test",
        json={
            "patterns": ["抢购"],
            "exclude_patterns": ["21点"],
            "text": "今晚21点开抢",
            "mode": "regex",
        },
    ).json()

    assert body["match"] is False
    assert "excluded" not in body
    assert body["reason"]


@pytest.mark.parametrize(
    ("patterns", "excludes", "text", "mode"),
    [
        (["21点"], ["21点"], "今晚21点开抢", "regex"),
        (["21点"], ["20点"], "今晚21点开抢", "regex"),
        (["21点"], [], "今晚21点开抢", "regex"),
        (["抢购"], ["21点"], "今晚21点开抢", "regex"),
        (["21点"], ["21点"], "今晚21点开抢", "contains"),
        (["21点"], ["21点"], "今晚21点开抢", "exact"),
        (["今晚21点开抢"], ["21点"], "今晚21点开抢", "exact"),
        (["(21)点"], ["21点"], "今晚21点开抢", "regex"),
        ([], ["21点"], "今晚21点开抢", "all"),
        ([], [], "今晚21点开抢", "all"),
    ],
)
def test_rules_tester_agrees_with_the_engine_including_excludes(
    client, app, account, patterns, excludes, text, mode
) -> None:
    """测试器的判定（含排除规则）必须和引擎**逐字一致** —— 包括"是谁排除的"。

    这条比 :func:`test_rules_tester_agrees_with_the_forward_engine` 更进一步：
    后者只比 ``match``，这里把 ``exclude_patterns`` 也压进来，并且断言"被拦下"这个
    结论和引擎的 ``reason`` 完全对应（引擎说不是排除导致的，测试器就不许说被排除）。
    """
    from tg_assistant.config import MatchConfig
    from tg_assistant.matching import CompiledMatcher

    tested = client.post(
        "/api/rules/test",
        json={"patterns": patterns, "exclude_patterns": excludes, "text": text, "mode": mode},
    ).json()
    engine = CompiledMatcher(
        MatchConfig(mode=mode, patterns=patterns, exclude_patterns=excludes, ignore_case=True)
    ).match_text(text)

    assert tested["match"] is engine.matched
    assert bool(tested.get("excluded")) is (engine.reason == "命中排除规则")
    assert tested.get("groups", []) == [group for group in engine.groups if group]
    if tested.get("excluded"):
        assert set(tested["excluded_by"]) <= set(excludes)
        assert tested["excluded_by"], "说了被排除，却没说清是哪一条"
    else:
        assert tested.get("reason", "") == engine.reason


def test_rules_test_keeps_the_legacy_pattern_argument_working(client, app, account) -> None:
    """老入参 ``pattern``（单个字符串）必须继续可用 —— 别的调用方还在用它。"""
    legacy = client.post(
        "/api/rules/test", json={"pattern": "21点", "text": "今晚21点开抢", "mode": "regex"}
    ).json()
    assert legacy["match"] is True

    # 老的 pattern + 新的 exclude_patterns 可以混用
    mixed = client.post(
        "/api/rules/test",
        json={
            "pattern": "21点",
            "exclude_patterns": ["21点"],
            "text": "今晚21点开抢",
            "mode": "regex",
        },
    ).json()
    assert mixed["match"] is False and mixed["excluded_by"] == ["21点"]

    # 列表入参写成字符串也认（老调用方可能顺手塞一个字符串）
    as_string = client.post(
        "/api/rules/test", json={"patterns": "21点", "text": "今晚21点开抢", "mode": "regex"}
    ).json()
    assert as_string["match"] is True

    # 空主正则（非 all 模式）仍然是那句"正则表达式为空"
    empty = client.post(
        "/api/rules/test", json={"pattern": "", "text": "x", "mode": "regex"}
    ).json()
    assert empty["match"] is False and "正则表达式为空" in empty["error"]


def test_rules_test_reports_a_broken_exclude_pattern(client, app, account) -> None:
    """排除规则也是用户手写的正则 —— 写错了要说清楚，不能悄悄当成"没命中"。"""
    body = client.post(
        "/api/rules/test",
        json={
            "patterns": ["21点"],
            "exclude_patterns": ["(["],
            "text": "今晚21点开抢",
            "mode": "regex",
        },
    ).json()
    assert body["match"] is False
    assert "无效" in body["error"]


def test_rules_test_global_and_scoped_agree_about_excludes(client, app, account) -> None:
    """两个入口共用 ``_run_match_test``，排除规则的判定也必须一模一样。"""
    payload = {
        "patterns": ["21点"],
        "exclude_patterns": ["21点"],
        "text": "今晚21点开抢",
        "mode": "regex",
    }
    global_body = client.post("/api/rules/test", json=payload).json()
    scoped_body = client.post(f"/api/config/{NAME}/rules/test", json=payload).json()
    assert global_body == scoped_body
    assert global_body["excluded_by"] == ["21点"]


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
# 全局转发规则（跨账号扇出）
#
# 页面原来要求「先在顶部选一个账号」才能建规则，现在改成在弹窗里勾账号、
# 一个都不勾就是全部账号。这一组用例钉住后端那半边语义。
# --------------------------------------------------------------------------- #
def test_global_rules_overview_lists_every_account(client, app, two_accounts) -> None:
    body = client.get("/api/rules").json()
    assert [a["name"] for a in body["accounts"]] == [NAME, NAME2]
    assert all(a["rules"] == [] for a in body["accounts"])
    assert all(a["forward_enabled"] is True for a in body["accounts"])
    # 状态点要用的字段必须一起给，否则分组标题画不出来
    assert all("running" in a and "session_exists" in a for a in body["accounts"])


def test_global_rules_create_fans_out_to_all_accounts(client, app, two_accounts) -> None:
    """不传 accounts = 全部账号，这是页面上的默认行为。"""
    resp = client.post("/api/rules", json={"rule": _rule_payload()})
    assert resp.status_code == 200, resp.text
    assert resp.json()["saved"] == [NAME, NAME2]
    for name in (NAME, NAME2):
        ids = [r["id"] for r in client.get(f"/api/config/{name}/rules").json()["rules"]]
        assert ids == ["r1"], f"{name} 没拿到规则"


def test_global_rules_create_with_explicit_accounts(client, app, two_accounts) -> None:
    resp = client.post("/api/rules", json={"rule": _rule_payload(), "accounts": [NAME2]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["saved"] == [NAME2]
    assert client.get(f"/api/config/{NAME}/rules").json()["rules"] == []


def test_global_rules_create_accepts_flat_payload(client, app, two_accounts) -> None:
    """也接受把规则字段平铺在顶层；accounts 不能被当成规则字段（StrictModel 会 400）。"""
    resp = client.post("/api/rules", json={**_rule_payload(), "accounts": [NAME]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["saved"] == [NAME]


def test_global_rules_create_unknown_account_is_404(client, app, two_accounts) -> None:
    resp = client.post("/api/rules", json={"rule": _rule_payload(), "accounts": ["ghost"]})
    assert resp.status_code == 404


def test_global_rules_create_without_any_account_is_400(client, app) -> None:
    """一个账号都没有时别写进一个空注册表，直接说清楚。"""
    resp = client.post("/api/rules", json={"rule": _rule_payload()})
    assert resp.status_code == 400


def test_global_rules_create_rejects_invalid_payload(client, app, two_accounts) -> None:
    assert client.post("/api/rules", json={"rule": {"id": "x"}}).status_code == 400


def test_global_rules_create_generates_a_missing_id(client, app, two_accounts) -> None:
    """规则 id 不必填（用户原话：「ID 不要必填」）。

    🔴 这条路径**必须**在入口补 id：它只单独校验一条规则（``ForwardRule``），不经过
    ``ForwardConfig`` 的校验器；而 ``rules.append(...)`` 是列表原地修改，父模型的
    ``validate_assignment`` 也不会被触发。少了这一步，空 id 会直接落盘 ——
    面板的规则卡片按 ``rule.id`` 跨账号合并，空 id 会让它们挤成同一张卡。
    """
    payload = {**_rule_payload(), "name": "Main Rule"}
    payload.pop("id")

    resp = client.post("/api/rules", json={"rule": payload})

    assert resp.status_code == 200, resp.text
    assert resp.json()["rule"]["id"] == "main-rule", "名称里的 ASCII 片段要拿来做 id"
    for name in (NAME, NAME2):
        ids = [r["id"] for r in client.get(f"/api/config/{name}/rules").json()["rules"]]
        assert ids == ["main-rule"], f"{name} 没拿到自动生成的 id"


def test_generated_rule_id_avoids_ids_in_every_target_account(
    client, app, two_accounts
) -> None:
    """生成要看**所有目标账号**里的已有 id。

    只看一个账号的话，扇出到另一个账号会被「已存在」逻辑跳过，用户看到"只写进去
    一半" —— 而这正是面板默认行为（不勾账号 = 写给全部）。
    """
    occupied = {**_rule_payload(rule_id="main-rule"), "name": "占位"}
    assert client.post("/api/rules", json={"rule": occupied, "accounts": [NAME2]}).status_code == 200

    payload = {**_rule_payload(), "name": "Main Rule"}
    payload.pop("id")
    resp = client.post("/api/rules", json={"rule": payload})

    assert resp.status_code == 200, resp.text
    assert resp.json()["saved"] == [NAME, NAME2], "两个账号都要写进去"
    assert resp.json()["rule"]["id"] == "main-rule-2", "撞名时追加序号，别复用已占用的"


def test_rule_update_with_a_blank_id_keeps_the_path_id(client, app, two_accounts) -> None:
    """空 id 的「改一改」要用**路径里的 id**。

    面板把 id 输入框锁住了，但真发上来空 id 时若照单全收，就把这条规则的主键抹成
    空串：卡片合并、事件日志里的 ``rule`` 字段、按 id 的增删改全对不上。
    """
    assert client.post("/api/rules", json={"rule": _rule_payload()}).status_code == 200

    payload = {**_rule_payload(), "id": "", "name": "改过的名字"}
    resp = client.put("/api/rules/r1", json={"rule": payload})

    assert resp.status_code == 200, resp.text
    assert resp.json()["rule"]["id"] == "r1"
    for name in (NAME, NAME2):
        rules = client.get(f"/api/config/{name}/rules").json()["rules"]
        assert [r["id"] for r in rules] == ["r1"], f"{name} 的主键被抹掉了"
        assert rules[0]["name"] == "改过的名字"


def test_global_rules_create_skips_accounts_that_already_have_the_id(
    client, app, two_accounts
) -> None:
    """某个账号里已有同名 id 时跳过它，别让整单失败。"""
    assert client.post(f"/api/config/{NAME}/rules", json=_rule_payload()).status_code == 200
    resp = client.post("/api/rules", json={"rule": _rule_payload()})
    assert resp.status_code == 200, resp.text
    assert resp.json()["saved"] == [NAME2]
    assert resp.json()["conflicts"] == [NAME]


def test_global_rules_create_all_conflicts_is_409(client, app, two_accounts) -> None:
    assert client.post("/api/rules", json={"rule": _rule_payload()}).status_code == 200
    dup = client.post("/api/rules", json={"rule": _rule_payload()})
    assert dup.status_code == 409
    assert "已存在" in dup.json()["detail"]


def test_global_rules_update_touches_every_owner(client, app, two_accounts) -> None:
    """规则当初是扇出写出去的，改一次要同步到所有副本，不能只改一半。"""
    client.post("/api/rules", json={"rule": _rule_payload()})
    resp = client.put("/api/rules/r1", json={"rule": {**_rule_payload(), "name": "改名了"}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == [NAME, NAME2]
    for name in (NAME, NAME2):
        assert client.get(f"/api/config/{name}/rules").json()["rules"][0]["name"] == "改名了"


def test_global_rules_update_can_be_scoped_to_one_account(client, app, two_accounts) -> None:
    client.post("/api/rules", json={"rule": _rule_payload()})
    resp = client.put(
        "/api/rules/r1",
        json={"rule": {**_rule_payload(), "name": "只改这个"}, "accounts": [NAME]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == [NAME]
    assert client.get(f"/api/config/{NAME2}/rules").json()["rules"][0]["name"] == "测试规则"


def test_global_rules_update_missing_is_404(client, app, two_accounts) -> None:
    assert client.put("/api/rules/nope", json={"rule": _rule_payload("nope")}).status_code == 404


def test_global_rules_rename_into_taken_id_is_409_and_writes_nothing(
    client, app, two_accounts
) -> None:
    """改名撞上同账号里已有的 id 要拒绝：重复 id 会让同一条消息被转发两次。

    顺带钉住「先整体校验、再统一落盘」：冲突发生在**后面**那个账号时，前面已经
    检查通过的账号也不能被改掉 —— 否则用户看到报错，却有一半配置已经变了。
    """
    client.post("/api/rules", json={"rule": _rule_payload("r2"), "accounts": [NAME]})
    client.post("/api/rules", json={"rule": _rule_payload("r2"), "accounts": [NAME2]})
    client.post("/api/rules", json={"rule": _rule_payload("r1"), "accounts": [NAME2]})

    resp = client.put("/api/rules/r2", json={"rule": _rule_payload("r1")})

    assert resp.status_code == 409
    assert [r["id"] for r in client.get(f"/api/config/{NAME}/rules").json()["rules"]] == ["r2"], (
        "冲突出在第二个账号上，第一个账号却已经被改掉了 —— 只改了一半"
    )
    assert sorted(r["id"] for r in client.get(f"/api/config/{NAME2}/rules").json()["rules"]) == [
        "r1",
        "r2",
    ]


def test_global_rules_delete_removes_from_every_owner(client, app, two_accounts) -> None:
    client.post("/api/rules", json={"rule": _rule_payload()})
    resp = client.delete("/api/rules/r1")
    assert resp.status_code == 200, resp.text
    assert resp.json()["removed"] == [NAME, NAME2]
    for name in (NAME, NAME2):
        assert client.get(f"/api/config/{name}/rules").json()["rules"] == []


def test_global_rules_delete_can_be_scoped_to_one_account(client, app, two_accounts) -> None:
    client.post("/api/rules", json={"rule": _rule_payload()})
    resp = client.delete(f"/api/rules/r1?accounts={NAME}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["removed"] == [NAME]
    assert [r["id"] for r in client.get(f"/api/config/{NAME2}/rules").json()["rules"]] == ["r1"]


def test_global_rules_delete_missing_is_404(client, app, two_accounts) -> None:
    assert client.delete("/api/rules/nope").status_code == 404


def test_global_rules_can_be_added_to_or_removed_from_one_account(
    client, app, two_accounts
) -> None:
    """规则卡片上「监听账号」那一下勾选，靠的就是这一对调用。

    面板已经改成「一条规则一张主卡片 + 勾选监听账号」，于是这两个端点的语义成了
    界面正确性的前提：
      * 勾上要走 ``POST /api/rules {accounts:[name]}`` —— 只写**还没有**这条规则的
        账号。PUT 是"覆盖已存在的副本"，账号里没有时它只算 missing，不写也不报错，
        界面会看起来勾上了、其实什么都没发生。
      * 取消要走 ``DELETE /api/rules/{id}?accounts=<name>`` —— 只移出那一个账号。
        不带 accounts 的 DELETE 是"从所有账号删掉"（删除键的语义），误用会把别的
        账号一起干掉。
    """
    client.post("/api/rules", json={"rule": _rule_payload("r1"), "accounts": [NAME]})

    def ids(name: str) -> list[str]:
        return sorted(
            r["id"] for r in client.get(f"/api/config/{name}/rules").json()["rules"]
        )

    # 勾上第二个账号
    add = client.post("/api/rules", json={"rule": _rule_payload("r1"), "accounts": [NAME2]})
    assert add.status_code == 200, add.text
    assert add.json()["saved"] == [NAME2]
    assert ids(NAME) == ["r1"] and ids(NAME2) == ["r1"]

    # 已经有的账号再勾一次：走 conflict（界面据 detail 报中文原因），不能重复写入
    again = client.post("/api/rules", json={"rule": _rule_payload("r1"), "accounts": [NAME]})
    assert again.status_code == 409, again.text
    assert again.json()["detail"]
    assert ids(NAME) == ["r1"], "重复写入会让同一条消息被转发两次"

    # 从第二个账号取消勾选：只动它
    off = client.delete(f"/api/rules/r1?accounts={NAME2}")
    assert off.status_code == 200, off.text
    assert off.json()["removed"] == [NAME2]
    assert ids(NAME) == ["r1"], "取消一个账号的监听把别的账号也删了"
    assert ids(NAME2) == []


def test_global_rules_can_be_fed_back_from_the_rules_overview(
    client, app, two_accounts
) -> None:
    """面板勾「监听账号」时，POST 的规则体就是 ``GET /api/rules`` 里那一个对象。

    所以要钉住这条回环：总览里序列化出来的规则必须能**原样**喂回 ``POST /api/rules``。
    多一个未知字段（以后给模型加字段时很容易发生）就是 422，而用户在界面上看到的
    只是"勾了没反应"—— 这是真实调用路径，和手写 payload 的用例不是一回事。
    """
    client.post("/api/rules", json={"rule": _rule_payload("r1"), "accounts": [NAME]})
    overview = client.get("/api/rules").json()
    rule = next(
        r
        for acc in overview["accounts"]
        if acc["name"] == NAME
        for r in acc["rules"]
        if r["id"] == "r1"
    )

    resp = client.post("/api/rules", json={"rule": rule, "accounts": [NAME2]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["saved"] == [NAME2]

    def copy_in(name: str) -> dict:
        return next(
            r
            for r in client.get(f"/api/config/{name}/rules").json()["rules"]
            if r["id"] == "r1"
        )

    # 两个账号里那份必须一模一样：不一样的话卡片上会一直挂着"内容不一致"的告警，
    # 而"编辑一次全都改"又会把差异悄悄抹掉。
    assert copy_in(NAME) == copy_in(NAME2)
    assert copy_in(NAME2)["id"] == "r1"


def test_edit_save_applies_the_account_diff(client, app, two_accounts) -> None:
    """编辑弹窗一次保存 = PUT(内容) → POST(新增) → DELETE(取消)，最终分布必须正好是勾选的那些。

    🔴 2026-09-29 用户反馈：「转发规则中新勾选账号无法勾选」——弹窗里的账号框被
    ``locked=true`` 设成 disabled，点不动；修好之后保存必须真的按**差集**落盘，
    否则界面显示"勾上了"而磁盘上没写进去。

    这里用真 store 按界面的顺序跑一遍（面板 JS 的差集逻辑由 jsdom 台子覆盖，
    这一条钉的是"这三个端点按这个顺序调用"的最终结果）：先 PUT 内容再增删，
    不能把取消掉的账号又写回来，也不能漏掉新增的那个。
    """
    third = "third"
    store = app.state.store
    store.upsert_account(AccountRecord(name=third, created_at=utc_now_iso()))
    store.load_account_config(third, create=True)

    def owners() -> list[str]:
        return [
            acc["name"]
            for acc in client.get("/api/rules").json()["accounts"]
            if any(r["id"] == "r1" for r in acc["rules"])
        ]

    # 初始：规则先写进两个账号（显式给 accounts —— 不给的话会扇出到全部账号，
    # 而这时 third 已经存在了）
    assert client.post(
        "/api/rules", json={"rule": _rule_payload("r1"), "accounts": [NAME, NAME2]}
    ).status_code == 200
    assert owners() == [NAME, NAME2]

    # 用户在弹窗里改了内容、勾上 third、勾掉 acct ⇒ adds=[third], removes=[acct]
    edited = {**_rule_payload("r1"), "name": "改过名"}
    assert client.put("/api/rules/r1", json={"rule": edited}).status_code == 200
    add = client.post("/api/rules", json={"rule": edited, "accounts": [third]})
    assert add.status_code == 200, add.text
    removed = client.delete(f"/api/rules/r1?accounts={NAME}")
    assert removed.status_code == 200, removed.text

    assert owners() == [NAME2, third], "差集没有落成界面上勾选的那两个账号"
    # 内容改动要落在**仍在监听**的账号上（被取消的那个已经没有副本了）
    for name in (NAME2, third):
        assert client.get(f"/api/config/{name}/rules").json()["rules"][0]["name"] == "改过名"
    assert client.get(f"/api/config/{NAME}/rules").json()["rules"] == []

    # 一次取消**多个**账号：界面会把它们拼成 ?accounts=a,b（逗号分隔）
    both = client.delete(f"/api/rules/r1?accounts={NAME2},{third}")
    assert both.status_code == 200, both.text
    assert both.json()["removed"] == [NAME2, third]
    assert owners() == []


def test_global_rules_test_needs_no_account(client, app) -> None:
    """页面去掉「先选账号」后凑不出账号名了，而匹配本身跟账号无关。"""
    resp = client.post(
        "/api/rules/test",
        json={"pattern": r"金额[:：]\s*(\d+)", "text": "今天金额：128 元", "mode": "regex"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["match"] is True
    assert resp.json()["groups"] == ["128"]


def test_global_rules_test_matches_the_per_account_endpoint(client, app, account) -> None:
    """两个入口必须给出完全一样的结果（同一个 _run_match_test）。"""
    payload = {"pattern": "([", "text": "abc", "mode": "regex"}
    global_body = client.post("/api/rules/test", json=payload).json()
    scoped_body = client.post(f"/api/config/{NAME}/rules/test", json=payload).json()
    assert global_body == scoped_body
    assert "无效" in global_body["error"]


@pytest.mark.parametrize(
    ("pattern", "text"),
    [
        # 用户报的那个场景：码独占一行，上下都是别的字
        (r"^(?!.*(.)\1{3})[A-Z0-9]{12}$", "🎁 注册码\nCZAMTIRLMX2U\n有效期 30 天"),
        (r"^MSKY-\d+-[A-Za-z]+_[0-9a-f]{10}$", "🎥 影库\nMSKY-30-Register_ab12cd34ef\n请尽快注册"),
        (r"^广告$", "正常内容\n广告\n正常内容"),
        (r"^abc$", "XYZ\nABC\nXYZ"),
        (r"抢到", "第一行\n恭喜你抢到\n第三行"),
        (r"^a.*b$", "a\nb"),
        (r"金额[:：]\s*(\d+)", "今天金额：128 元"),
    ],
)
def test_rules_tester_agrees_with_the_forward_engine(client, app, pattern, text) -> None:
    """面板上的「正则测试器」和**真正干活的转发引擎**必须给同一个答案。

    🔴 这两边原来是各写各的编译：测试器走 ``_run_match_test`` 里裸的
    ``re.compile(pattern, IGNORECASE)``，引擎走 ``CompiledMatcher`` 的
    ``USER_PATTERN_FLAGS | IGNORECASE``。引擎哪天调了标志（比如加
    ``re.MULTILINE``），测试器就会开始撒谎 —— 而用户理所当然地会信测试器，
    然后认定"你们的匹配坏了"，最后把一条完全正确的正则删掉。

    这条测试只断言**两边一致**，不钉具体结果：具体结果由
    ``test_matching.py::TestUserPatternsAreMultiline`` 负责。
    """
    from tg_assistant.config import MatchConfig
    from tg_assistant.matching import CompiledMatcher

    tested = client.post(
        "/api/rules/test", json={"pattern": pattern, "text": text, "mode": "regex"}
    ).json()
    engine = CompiledMatcher(
        MatchConfig(mode="regex", patterns=[pattern], ignore_case=True)
    ).match_text(text)
    assert tested["match"] is engine.matched, (
        f"测试器说 {tested['match']}，引擎说 {engine.matched}（{engine.reason}）"
        f" —— 用户会信测试器，所以两边必须一模一样"
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


def test_red_packet_edit_max_age_round_trips(client, app, account) -> None:
    """「编辑事件年龄上限」要能过 HTTP 一圈回来（面板按分钟填、配置存秒）。

    这个字段就是用来挡住"长驻红包被机器人反复编辑"的，存不进去等于没配。
    """
    payload = client.get(f"/api/config/{NAME}/red_packet").json()
    # 空配置里 tasks 是空的（没有默认任务这回事），所以显式给一条。
    payload["tasks"] = [{"id": "default", "chats": [], "edit_max_age": 600}]

    saved = client.put(f"/api/config/{NAME}/red_packet", json=payload)
    assert saved.status_code == 200, saved.text

    task = client.get(f"/api/config/{NAME}/red_packet").json()["tasks"][0]
    assert task["edit_max_age"] == 600.0
    assert task["id"] == "default"


def test_red_packet_rejects_negative_edit_max_age(client, app, account) -> None:
    payload = client.get(f"/api/config/{NAME}/red_packet").json()
    payload["tasks"] = [{"id": "default", "edit_max_age": -1}]

    resp = client.put(f"/api/config/{NAME}/red_packet", json=payload)

    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# 抢注（多任务）
# --------------------------------------------------------------------------- #
#: 一条「配好了」的抢注任务：有提取正则 + 至少一条步骤。
def _reg_grab_task(**overrides) -> dict:
    task = {
        "id": "main",
        "name": "主群",
        "enabled": True,
        "chats": [-1001234567890],
        "detect": {"code_pattern": r"(?:Register)_([A-Za-z0-9]{10})"},
        "steps": [{"type": "send", "chat": "@example_bot", "text": "/bind {code}"}],
    }
    task.update(overrides)
    return task


def test_reg_grab_get_marks_every_task_ready(client, app, account) -> None:
    """GET 要给**每条任务**附上 ready / problem / in_window，并给出服务端当前时间。"""
    url = f"/api/config/{NAME}/reg_grab"
    saved = client.put(
        url,
        json={
            "enabled": False,
            "max_concurrency": 2,
            "tasks": [
                _reg_grab_task(),
                # 没填提取正则 ⇒ 不该被算成「配好了」
                _reg_grab_task(id="tmp", name="临时", detect={}),
            ],
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["tasks"] == 2

    body = client.get(url).json()
    assert body["max_concurrency"] == 2
    assert len(body["server_now"]) == 5 and body["server_now"][2] == ":", (
        f"server_now 要形如 HH:MM，实际是 {body['server_now']!r}"
    )
    assert [task["id"] for task in body["tasks"]] == ["main", "tmp"]

    first, second = body["tasks"]
    assert first["ready"] is True
    assert first["problem"] is None
    # window.enabled 默认 False = 全天 ⇒ 恒在时段内
    assert first["in_window"] is True
    assert second["ready"] is False
    assert "注册码提取正则" in second["problem"]


def test_reg_grab_get_result_can_be_put_back(client, app, account) -> None:
    """🔴 GET 的结果（多塞了 ready/problem/in_window/server_now）原样 PUT 回去不能 400。

    模型是 ``extra="forbid"`` 的：只读字段漏掉一个，面板上「改一改再保存」
    就会变成用户看不懂的「保存失败」。
    """
    url = f"/api/config/{NAME}/reg_grab"
    assert client.put(url, json={"tasks": [_reg_grab_task()]}).status_code == 200

    body = client.get(url).json()
    assert "server_now" in body and body["tasks"][0]["ready"] is True

    again = client.put(url, json=body)
    assert again.status_code == 200, again.text

    after = client.get(url).json()
    assert after["tasks"] == body["tasks"], "往返一圈后配置被改动了"
    assert after["tasks"][0]["steps"][0]["text"] == "/bind {code}"


def test_reg_grab_enabled_needs_at_least_one_ready_task(client, app, account) -> None:
    """总开关：一条能干活的任务都没有时拒绝开启，并给出中文原因。"""
    url = f"/api/config/{NAME}/reg_grab/enabled"

    denied = client.put(url, json={"enabled": True})
    assert denied.status_code == 400, denied.text
    assert "任务" in denied.json()["detail"]

    # 只有「缺正则」的任务同样不算 —— 多任务下校验是「至少有一条 ready」。
    client.put(
        f"/api/config/{NAME}/reg_grab",
        json={"tasks": [_reg_grab_task(detect={})]},
    )
    assert client.put(url, json={"enabled": True}).status_code == 400

    # 补上一条配好的任务就能开
    client.put(
        f"/api/config/{NAME}/reg_grab",
        json={"tasks": [_reg_grab_task(detect={}), _reg_grab_task(id="ok")]},
    )
    opened = client.put(url, json={"enabled": True})
    assert opened.status_code == 200, opened.text
    assert opened.json()["enabled"] is True
    assert opened.json()["ready"] is True

    # 关闭不需要任何条件（哪怕开着开关、任务一条都没配好）
    client.put(
        f"/api/config/{NAME}/reg_grab",
        json={"enabled": True, "tasks": [_reg_grab_task(detect={})]},
    )
    assert client.put(url, json={"enabled": False}).status_code == 200
    assert client.get(f"/api/config/{NAME}/reg_grab").json()["enabled"] is False


def test_reg_grab_rejects_invalid_payload(client, app, account) -> None:
    url = f"/api/config/{NAME}/reg_grab"
    assert client.put(url, json={"enabled": "not-a-bool"}).status_code == 400
    # 重复 id 属于配置错误，要报 400 而不是 500
    duplicated = client.put(
        url, json={"tasks": [_reg_grab_task(), _reg_grab_task(name="重复")]}
    )
    assert duplicated.status_code == 400, duplicated.text
    assert "重复" in duplicated.json()["detail"]


# --------------------------------------------------------------------------- #
# 抢注：任务维度（跨账号）
# --------------------------------------------------------------------------- #
def _add_account(app, name: str) -> None:
    """临时再加一个账号 —— 「只从某几个账号移除」这类语义要 ≥3 个账号才验得出来。"""
    store = app.state.store
    store.upsert_account(AccountRecord(name=name, created_at=utc_now_iso()))
    store.load_account_config(name, create=True)


def _overview(client) -> dict:
    resp = client.get("/api/reg_grab/overview")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _task_ids(client, name: str) -> list[str]:
    return [task["id"] for task in client.get(f"/api/config/{name}/reg_grab").json()["tasks"]]


def test_reg_grab_overview_covers_every_account(client, app, two_accounts) -> None:
    """任务维度列表要**一次**拿到所有账号的快照（含账号级开关/并发 + 每条任务的 ready）。

    页面不再有"当前账号"，逐账号 N+1 拉配置不只是多几个请求：账号 A 落盘、B 还没
    落盘时会把"写了一半的状态"当快照渲染出来。
    """
    first_url = f"/api/config/{NAME}/reg_grab"
    second_url = f"/api/config/{NAME2}/reg_grab"
    assert client.put(
        first_url, json={"enabled": True, "max_concurrency": 2, "tasks": [_reg_grab_task()]}
    ).status_code == 200
    assert client.put(
        second_url,
        json={"tasks": [_reg_grab_task(), _reg_grab_task(id="tmp", name="临时", detect={})]},
    ).status_code == 200

    body = _overview(client)
    assert [each["name"] for each in body["accounts"]] == [NAME, NAME2]
    assert len(body["server_now"]) == 5 and body["server_now"][2] == ":"

    first, second = body["accounts"]
    assert first["reg_grab_enabled"] is True
    assert first["max_concurrency"] == 2
    assert [task["id"] for task in first["tasks"]] == ["main"]
    assert "running" in first and "session_exists" in first

    assert second["reg_grab_enabled"] is False
    assert second["max_concurrency"] == 1
    assert [task["id"] for task in second["tasks"]] == ["main", "tmp"]
    # 只读字段照旧带上：卡片要靠它显示「配置不完整」，不用自己重算一遍
    assert second["tasks"][0]["ready"] is True and second["tasks"][0]["in_window"] is True
    assert second["tasks"][1]["ready"] is False
    assert "注册码提取正则" in second["tasks"][1]["problem"]


def test_reg_grab_task_can_be_added_to_other_accounts(client, app, two_accounts) -> None:
    """POST 把一条任务写到别的账号；同 id 已存在的账号**跳过**并回 conflicts，全冲突才 409。"""
    created = client.post(
        "/api/reg_grab/tasks", json={"task": _reg_grab_task(), "accounts": [NAME, NAME2]}
    )
    assert created.status_code == 200, created.text
    assert created.json()["saved"] == [NAME, NAME2]
    assert created.json()["conflicts"] == []
    assert _task_ids(client, NAME) == ["main"] and _task_ids(client, NAME2) == ["main"]

    # 再发一次同一批：全都已存在 ⇒ 409（不报错的话用户会以为又写了一份）
    all_conflict = client.post(
        "/api/reg_grab/tasks", json={"task": _reg_grab_task(), "accounts": [NAME, NAME2]}
    )
    assert all_conflict.status_code == 409
    assert "都已存在" in all_conflict.json()["detail"]

    # 第三个账号：只写它，已经有的那个算冲突
    _add_account(app, "third")
    partial = client.post(
        "/api/reg_grab/tasks", json={"task": _reg_grab_task(), "accounts": [NAME, "third"]}
    )
    assert partial.status_code == 200, partial.text
    assert partial.json()["saved"] == ["third"]
    assert partial.json()["conflicts"] == [NAME]
    assert _task_ids(client, "third") == ["main"]

    # 一条**新**任务发给全部账号（accounts 缺省 = 全部）
    fresh = client.post("/api/reg_grab/tasks", json={"task": _reg_grab_task(id="second-task")})
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["saved"] == [NAME, NAME2, "third"]
    assert _task_ids(client, NAME) == ["main", "second-task"]


def test_reg_grab_task_post_generates_a_missing_id(client, app, two_accounts) -> None:
    """id 留空由服务端生成；同名再来一条也不能撞上（撞了就回一句莫名其妙的"都已存在"）。"""
    blank = {**_reg_grab_task(id="", name="main"), "id": ""}
    first = client.post("/api/reg_grab/tasks", json={"task": blank, "accounts": [NAME]})
    assert first.status_code == 200, first.text
    generated = first.json()["task"]["id"]
    assert generated, "空 id 没有被生成"
    assert _task_ids(client, NAME) == [generated]

    second = client.post("/api/reg_grab/tasks", json={"task": blank, "accounts": [NAME]})
    assert second.status_code == 200, second.text
    second_id = second.json()["task"]["id"]
    assert second_id != generated, "同名任务生成了同一个 id（紧接着会判成冲突）"
    assert _task_ids(client, NAME) == [generated, second_id]


def test_reg_grab_task_update_hits_every_owner(client, app, two_accounts) -> None:
    """PUT 更新**所有拥有者**里的那一份（卡片启停开关走它），并回 updated / missing。

    不发 N 个 per-account PUT 的理由：第 3 个账号失败时前 2 个已经落盘了 ——
    用户看到报错，却有一半账号已经变了。
    """
    assert client.post(
        "/api/reg_grab/tasks", json={"task": _reg_grab_task(), "accounts": [NAME, NAME2]}
    ).status_code == 200

    stopped = {**_reg_grab_task(), "enabled": False, "name": "改过名"}
    resp = client.put("/api/reg_grab/tasks/main", json={"task": stopped})
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == [NAME, NAME2]
    assert resp.json()["missing"] == []

    for name in (NAME, NAME2):
        tasks = client.get(f"/api/config/{name}/reg_grab").json()["tasks"]
        assert len(tasks) == 1, f"{name} 里冒出了多余的任务：{tasks}"
        assert tasks[0]["enabled"] is False and tasks[0]["name"] == "改过名"

    # body 直接就是任务本身也认（与 POST 的 {task: ...} 两种写法都支持）
    plain = client.put("/api/reg_grab/tasks/main", json={**stopped, "name": "裸 body"})
    assert plain.status_code == 200, plain.text
    assert plain.json()["updated"] == [NAME, NAME2]

    # 重名冲突：把 main 改成其它账号里已经占用的 id ⇒ 409（重复 id 会让同一条消息被处理两次）
    assert client.post(
        "/api/reg_grab/tasks", json={"task": _reg_grab_task(id="other"), "accounts": [NAME]}
    ).status_code == 200
    clash = client.put("/api/reg_grab/tasks/main", json={"task": _reg_grab_task(id="other")})
    assert clash.status_code == 409
    assert "无法改名" in clash.json()["detail"]

    # 不存在的任务 id ⇒ 404
    assert (
        client.put("/api/reg_grab/tasks/nope", json={"task": _reg_grab_task(id="nope")}).status_code
        == 404
    )


def test_reg_grab_task_delete_can_be_scoped_to_one_account(client, app, two_accounts) -> None:
    """DELETE ?accounts= 只移除指定账号；**不带 accounts 才是全删**（与规则页一致）。"""
    _add_account(app, "third")
    assert client.post("/api/reg_grab/tasks", json={"task": _reg_grab_task()}).status_code == 200
    assert _task_ids(client, NAME) == ["main"]
    assert _task_ids(client, "third") == ["main"]

    one = client.delete(f"/api/reg_grab/tasks/main?accounts={NAME}")
    assert one.status_code == 200, one.text
    assert one.json()["removed"] == [NAME]
    # 其它账号一点没受影响
    assert _task_ids(client, NAME) == []
    assert _task_ids(client, NAME2) == ["main"]
    assert _task_ids(client, "third") == ["main"]

    # 逗号分隔一次移除两个
    both = client.delete(f"/api/reg_grab/tasks/main?accounts={NAME2},third")
    assert both.status_code == 200, both.text
    assert both.json()["removed"] == [NAME2, "third"]
    assert _task_ids(client, NAME2) == [] and _task_ids(client, "third") == []

    # 哪儿都没有这条任务了 ⇒ 404（而不是假装成功）
    assert client.delete("/api/reg_grab/tasks/main").status_code == 404
    # 指定了一个不存在的账号 ⇒ 404，别静默忽略
    assert client.delete("/api/reg_grab/tasks/main?accounts=nobody").status_code == 404


def test_reg_grab_task_endpoints_ignore_readonly_fields(client, app, two_accounts) -> None:
    """GET 回来的任务带 ready / problem / in_window，页面原样回传时不能被判 400。"""
    assert client.post("/api/reg_grab/tasks", json={"task": _reg_grab_task()}).status_code == 200
    roundtrip = client.get(f"/api/config/{NAME}/reg_grab").json()["tasks"][0]
    assert {"ready", "problem", "in_window"} <= set(roundtrip)

    resp = client.put("/api/reg_grab/tasks/main", json={"task": {**roundtrip, "name": "往返"}})
    assert resp.status_code == 200, resp.text
    assert client.get(f"/api/config/{NAME}/reg_grab").json()["tasks"][0]["name"] == "往返"


def test_reg_grab_task_post_rejects_bad_input(client, app, two_accounts) -> None:
    """坏任务 / 不存在的账号要给 400 / 404（而不是 500），而且什么都别写进去。"""
    bad = client.post(
        "/api/reg_grab/tasks", json={"task": {**_reg_grab_task(), "steps": "not-a-list"}}
    )
    assert bad.status_code == 400
    assert "任务校验失败" in bad.json()["detail"]

    unknown = client.post(
        "/api/reg_grab/tasks", json={"task": _reg_grab_task(), "accounts": ["nobody"]}
    )
    assert unknown.status_code == 404

    assert _task_ids(client, NAME) == [] and _task_ids(client, NAME2) == []


def test_reg_grab_task_delete_without_accounts_removes_every_owner(
    client, app, two_accounts
) -> None:
    """**不带** accounts 的 DELETE 才是"从所有账号删掉"（卡片上的删除键）。

    带上 accounts 是"只从某个账号移除"（卡片上取消勾选一个账号）—— 两者语义差一个
    参数，混了就是"我只想让它在一个号上停掉，结果别的号也一起没了"。
    """
    assert client.post(
        "/api/reg_grab/tasks", json={"task": _reg_grab_task(), "accounts": [NAME, NAME2]}
    ).status_code == 200

    resp = client.delete("/api/reg_grab/tasks/main")
    assert resp.status_code == 200, resp.text
    assert resp.json()["removed"] == [NAME, NAME2]
    assert _task_ids(client, NAME) == [] and _task_ids(client, NAME2) == []


def test_reg_grab_task_post_needs_a_target_account(client, app) -> None:
    """一个账号都没有时 POST 要 400（"还没有任何账号"），别写进一个不存在的账号。"""
    # 注意：这个用例**故意**不要 ``two_accounts`` fixture —— 要的就是空注册表
    assert _overview(client)["accounts"] == []

    resp = client.post("/api/reg_grab/tasks", json={"task": _reg_grab_task()})
    assert resp.status_code == 400
    assert "账号" in resp.json()["detail"]


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
