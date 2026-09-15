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


# --------------------------------------------------------------------------- #
# 登录页 ↔ 登录接口的字段契约
# --------------------------------------------------------------------------- #
def _login_fetch_bodies(html: str) -> list[str]:
    """取出登录页里每个 ``/api/accounts/login`` 调用提交的字段串。"""
    bodies = []
    for m in re.finditer(r"fetch\('/api/accounts/login'", html):
        chunk = html[m.start() : m.start() + 400]
        params = re.search(r"URLSearchParams\(\{([^}]*)\}\)", chunk)
        assert params, f"找不到 URLSearchParams 构造，选择器可能失效了：\n{chunk[:200]}"
        bodies.append(params.group(1))
    return bodies


def test_login_page_sends_account_to_login_api() -> None:
    """登录页调 ``/api/accounts/login`` 必须带 ``account``。

    这个端点的 ``account`` 是**必填** Form 字段 —— 仓库版坚持让用户自己起名
    （部署版是服务端随机生成 ``tg_xxxx``，那个名字没有任何意义）。所以前端
    一旦漏传就是 422，而这只会在浏览器里真点一下才暴露：API 测试、页面渲染
    测试、导航测试全都抓不到。这个坑真的踩过一次，所以钉在这里。
    """
    web_dir = Path(__file__).resolve().parents[1] / "tg_assistant" / "web"
    html = (web_dir / "templates" / "login.html").read_text(encoding="utf-8")

    bodies = _login_fetch_bodies(html)
    assert len(bodies) >= 2, "登录页应当同时有扫码与验证码两个登录入口"

    for body in bodies:
        keys = [part.split(":")[0].strip() for part in body.split(",")]
        assert "account" in keys, (
            f"登录页调用 /api/accounts/login 没带 account，会 422。实际提交字段：{body!r}"
        )


def test_login_page_has_account_inputs_matching_the_js() -> None:
    """JS 用 ``getElementById('qr-account'/'code-account')`` 取值，元素必须存在。

    缺了元素不会在渲染阶段报错，而是用户点「获取二维码」时抛
    ``Cannot read properties of null`` —— 页面上只表现为按钮没反应。
    """
    web_dir = Path(__file__).resolve().parents[1] / "tg_assistant" / "web"
    html = (web_dir / "templates" / "login.html").read_text(encoding="utf-8")

    for element_id in ["qr-account", "code-account"]:
        assert f'id="{element_id}"' in html, f"登录页缺少 #{element_id} 输入框"
        assert f"getElementById('{element_id}')" in html, (
            f"#{element_id} 存在但没有 JS 读取它 —— 是死元素？"
        )


def test_every_get_element_by_id_target_exists() -> None:
    """每个 ``getElementById('x')`` 都要有对应的 ``id="x"``。

    这是页面模板里最容易出、也最难发现的一类 bug：元素缺失时模板照样渲染
    （测试全绿），只有 JS 跑起来才抛 ``Cannot read properties of null``。

    ``rules.html`` 就踩过：当时的 ``loadRules()`` 第一句读 ``#forward-enabled``，
    而那段 HTML 根本没写 —— 于是 ``loadRules()`` 直接抛异常，
    **后面的 ``renderRules() 一次都没执行过，规则列表永远是空的**。
    CSS 里 ``.rules-master-toggle`` 和 JS 里的 ``toggleForward()`` 都早就写好了，
    只有元素丢了 —— 光看单侧根本发现不了。
    """
    web_dir = Path(__file__).resolve().parents[1] / "tg_assistant" / "web"
    tpl_dir = web_dir / "templates"
    base_ids = set(re.findall(r'id="([^"]+)"', (tpl_dir / "base.html").read_text(encoding="utf-8")))

    missing: dict[str, list[str]] = {}
    for tpl in sorted(tpl_dir.glob("*.html")):
        html = tpl.read_text(encoding="utf-8")
        declared = set(re.findall(r'id="([^"]+)"', html))
        # JS 动态创建的节点（createElement 后 .id = 'x' / setAttribute('id','x')）
        dynamic = set(re.findall(r"\.id\s*=\s*['\"]([^'\"]+)['\"]", html))
        dynamic |= set(
            re.findall(r"setAttribute\(\s*['\"]id['\"]\s*,\s*['\"]([^'\"]+)['\"]", html)
        )
        used = set(re.findall(r"getElementById\(\s*['\"]([^'\"]+)['\"]", html))
        gap = sorted(used - declared - dynamic - base_ids)
        if gap:
            missing[tpl.name] = gap

    assert not missing, f"这些模板引用了不存在的元素 id（JS 会抛 null）: {missing}"


def test_every_inline_handler_is_defined() -> None:
    """``onclick="foo()"`` 里的 ``foo`` 必须真的定义了。

    错拼一个函数名不会让模板渲染失败，页面上表现为**按钮点了完全没反应**
    （只有控制台里一条 ``foo is not defined``）。跨版本搬模板时很容易漏掉
    某个函数 —— 部署版的 ``accounts.html`` 就引用过 ``reloginAccount()``，
    而仓库版把它删了。
    """
    web_dir = Path(__file__).resolve().parents[1] / "tg_assistant" / "web"
    tpl_dir = web_dir / "templates"
    base = (tpl_dir / "base.html").read_text(encoding="utf-8")
    app_js = (web_dir / "static" / "js" / "app.js").read_text(encoding="utf-8")

    pattern = r"""on(?:click|change|input|submit|keydown|keypress)\s*=\s*['"]([A-Za-z_$][\w$]*)\(\)"""
    missing: dict[str, list[str]] = {}
    for tpl in sorted(tpl_dir.glob("*.html")):
        html = tpl.read_text(encoding="utf-8")
        handlers = set(re.findall(pattern, html))
        if not handlers:
            continue
        pool = "\n".join([html, base, app_js])
        defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", pool))
        defined |= set(
            re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function|\()", pool)
        )
        defined |= set(re.findall(r"window\.([A-Za-z_$][\w$]*)\s*=", pool))
        gap = sorted(h for h in handlers if h not in defined)
        if gap:
            missing[tpl.name] = gap

    assert not missing, f"这些模板绑定了未定义的处理函数（按钮会没反应）: {missing}"


# --------------------------------------------------------------------------- #
# 转发规则页：不再要求「先选账号」
# --------------------------------------------------------------------------- #
def test_rules_page_does_not_require_picking_an_account_first() -> None:
    """规则页不能退回「先在顶部选账号才能建规则」的旧交互。

    需求原话是「去掉转发规则选择账号创建，可以在添加转发任务的时候在弹窗内选择，
    如不选择就默认全部账号」。回归的表现有好几种：顶部又多一个账号下拉框、新建按钮
    初始被隐藏、或者保存又走回「某一个账号」的旧接口 —— 这里把这几条一起钉住。
    """
    web_dir = Path(__file__).resolve().parents[1] / "tg_assistant" / "web"
    html = (web_dir / "templates" / "rules.html").read_text(encoding="utf-8")

    assert 'id="account-selector"' not in html, "页面顶部的账号下拉框又回来了"
    assert 'id="rule-accounts"' in html, "弹窗里缺少账号选择器"
    assert "不勾选任何账号" in html, "缺少「不勾选 = 全部账号」的提示"
    assert "'/api/rules'" in html, "保存没有走全局扇出接口"
    assert "data-account=" in html, "规则列表没有按账号分组渲染"
    assert re.search(r'id="btn-add-rule"[^>]*display:\s*none', html) is None, (
        "新建按钮又被默认藏起来了（旧版要选完账号才显示）"
    )


def test_rules_page_only_binds_static_inline_handlers() -> None:
    """规则页的内联 handler 只允许是零参数的静态调用。

    ``rule.id`` 是用户在弹窗里自由输入的字符串（``ForwardRule.id`` 没有任何字符
    校验）。把它拼进内联 handler 的字符串参数里，只要 id 含一个单引号就会把属性
    截断，甚至能注入 JS —— HTML 转义救不了内联 handler，因为浏览器会先把 ``&#39;``
    解码回单引号再交给 JS 解析。

    所以动态卡片上的编辑 / 删除 / 启停、以及标签上的删除按钮，一律走 ``data-*``
    + 事件委托。这里故意**不要求**括号里是空的：真正危险的就是「带参数」的那种，
    只匹配 ``foo()`` 会把它们全漏掉（这个漏洞第一次写这个用例时就踩到了）。
    """
    web_dir = Path(__file__).resolve().parents[1] / "tg_assistant" / "web"
    html = (web_dir / "templates" / "rules.html").read_text(encoding="utf-8")

    pattern = r"""on(?:click|change|input|submit|keydown|keypress)\s*=\s*['"]([A-Za-z_$][\w$]*)\s*\("""
    handlers = set(re.findall(pattern, html))

    assert handlers == {"loadAll", "openAddRule", "closeRuleModal", "saveRule", "testRegex"}, (
        f"规则页的内联 handler 白名单变了（可能又把数据拼进了 handler）：{sorted(handlers)}"
    )
    # 动态内容靠这两个属性找目标
    assert "data-action" in html and "data-rule-id" in html
    assert "data-remove-tag" in html
