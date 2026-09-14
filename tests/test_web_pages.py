"""页面路由：每个页面都要能渲染出来。

模板里的 Jinja 语法错误、引用了不存在的模板、或者 ``base.html`` 的 block
名字对不上，都不会被 API 测试发现 —— 只有真去 GET 一次页面才会暴露。
这个文件就是干这个的。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tg_assistant.config import AccountRecord, utc_now_iso
from tg_assistant.web import create_app
from tg_assistant.web.auth import COOKIE_NAME, cookie_value

NAME = "acct"
SECRET = "s3cret-key"

#: 不需要账号上下文的页面。
PAGES = ["/", "/login", "/accounts", "/logs", "/proxy", "/rules", "/red_packet", "/notify"]


@pytest.fixture
def app(tmp_path: Path):
    return create_app(tmp_path / "data")


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def account(app):
    store = app.state.store
    store.upsert_account(AccountRecord(name=NAME, created_at=utc_now_iso()))
    store.load_account_config(NAME, create=True)
    return store


@pytest.mark.parametrize("path", PAGES)
def test_page_renders(client, path: str) -> None:
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} -> {resp.status_code}\n{resp.text[:500]}"
    assert "<!DOCTYPE html>" in resp.text
    # base.html 的骨架必须在，否则说明模板没继承对
    assert 'id="run-status"' in resp.text, f"{path} 没有渲染 base.html 的骨架"
    assert "</html>" in resp.text


@pytest.mark.parametrize("path", ["/config/{name}", "/chats/{name}"])
def test_account_pages_render(client, account, path: str) -> None:
    resp = client.get(path.format(name=NAME))
    assert resp.status_code == 200, f"{path} -> {resp.status_code}\n{resp.text[:500]}"


@pytest.mark.parametrize("path", ["/config/{name}", "/chats/{name}"])
def test_account_pages_redirect_for_unknown_account(client, app, path: str) -> None:
    resp = client.get(path.format(name="ghost"), follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/accounts"


def test_nav_links_are_present(client) -> None:
    """新增页面的导航入口不能漏。"""
    html = client.get("/").text
    for href in ["/rules", "/red_packet", "/notify", "/login", "/accounts", "/logs", "/proxy"]:
        assert f'href="{href}"' in html, f"导航里缺少 {href}"


def test_every_nav_link_actually_resolves(client) -> None:
    """侧边栏里的每个站内链接都必须真的能打开。

    只断言 ``href="..."`` 字符串存在是不够的 —— 那样把链接指到一个不存在的
    路由上（点进去 404）也照样能通过。这里把 ``base.html`` 渲染出来的链接抓出来
    逐个 GET，把「导航与路由表脱节」变成会红的测试。
    """
    html = client.get("/").text
    hrefs = {
        h
        for h in re.findall(r'href="(/[^"#?]*)"', html)
        # 静态资源由 StaticFiles 挂载，不在本测试关心范围内
        if not h.startswith("/static/")
    }
    assert hrefs, "base.html 里一个站内链接都没解析出来，选择器可能失效了"

    for href in sorted(hrefs):
        resp = client.get(href, follow_redirects=False)
        assert resp.status_code in (200, 303), f"导航链接 {href} -> {resp.status_code}（死链？）"


# --------------------------------------------------------------------------- #
# 二维码目录的鉴权
# --------------------------------------------------------------------------- #
def test_qr_static_requires_auth(app, client) -> None:
    """``/qr`` 不能是免鉴权路径：一张二维码就是一份登录凭据。"""
    from tg_assistant.web import auth as auth_mod

    assert not auth_mod.is_public_path("/qr/acct.png"), (
        "/qr 被放进了免鉴权白名单 —— 任何人都能取到别人的登录二维码"
    )

    app.state.web_settings.secret_key = SECRET
    qr_dir = app.state.paths.qr_dir
    qr_dir.mkdir(parents=True, exist_ok=True)
    (qr_dir / f"{NAME}.png").write_bytes(b"\x89PNG")

    # 未带 Cookie → 中间件应当拦下（重定向到 /auth），而不是把图片发出去
    resp = client.get(f"/qr/{NAME}.png", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/auth")

    # 带上 Cookie → 正常取图
    client.cookies.set(COOKIE_NAME, cookie_value(SECRET))
    ok = client.get(f"/qr/{NAME}.png")
    assert ok.status_code == 200
    assert ok.content == b"\x89PNG"


def test_logout_via_post(app, client) -> None:
    """侧边栏用表单 POST 登出，路由必须存在（否则 405）。"""
    app.state.web_settings.secret_key = SECRET
    client.cookies.set(COOKIE_NAME, cookie_value(SECRET))

    resp = client.post("/auth/logout", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth"
    assert not resp.cookies.get(COOKIE_NAME)


# --------------------------------------------------------------------------- #
# 样式表覆盖率
# --------------------------------------------------------------------------- #
def _referenced_classes(html: str) -> set[str]:
    """取出模板 ``class="..."`` 里引用的类名。

    两处噪音必须先清掉，否则全是误报：

    * ``<script>`` 里的 JS 模板字面量，例如
      ``class="badge-${rule.match.mode}"`` —— 类名是运行时拼出来的，
      静态分析不可能知道它是什么，只能整段排除。
    * Jinja 表达式，例如
      ``class="nav-item {% if page == 'rules' %}active{% endif %}"`` ——
      不清掉会被切成 ``if`` / ``page`` / ``'rules'`` 之类的碎片。
    """
    stripped = re.sub(r"<script\b.*?</script>", " ", html, flags=re.S | re.I)
    stripped = re.sub(r"<style\b.*?</style>", " ", stripped, flags=re.S | re.I)
    stripped = re.sub(r"\{\{.*?\}\}", " ", stripped, flags=re.S)
    stripped = re.sub(r"\{%.*?%\}", " ", stripped, flags=re.S)
    classes: set[str] = set()
    for attr in re.findall(r'class="([^"]*)"', stripped):
        classes.update(tok for tok in attr.split() if tok)
    return classes


def test_every_template_class_is_defined_in_css() -> None:
    """模板引用的每个类都要在 style.css 里有定义。

    部署版的三个新页面引用了 ``nt-content`` / ``rp-header-left`` 这类
    样式表里根本没写的类（写 CSS 时漏了），线上因为退化成 block 恰好看着正常
    才没被发现。这个用例把「模板与样式表脱节」钉住。
    """
    web_dir = Path(__file__).resolve().parents[1] / "tg_assistant" / "web"
    css = (web_dir / "static" / "css" / "style.css").read_text(encoding="utf-8")
    defined = set(re.findall(r"\.([A-Za-z_][\w-]*)", css))

    missing: dict[str, list[str]] = {}
    for tpl in sorted((web_dir / "templates").glob("*.html")):
        used = _referenced_classes(tpl.read_text(encoding="utf-8"))
        gap = sorted(used - defined)
        if gap:
            missing[tpl.name] = gap

    assert not missing, f"这些模板引用了 style.css 里不存在的类：{missing}"
