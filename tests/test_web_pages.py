"""页面路由：每个页面都要能渲染出来。

模板里的 Jinja 语法错误、引用了不存在的模板、或者 ``base.html`` 的 block
名字对不上，都不会被 API 测试发现 —— 只有真去 GET 一次页面才会暴露。
这个文件就是干这个的。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import tg_assistant
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


def test_nav_group_renamed_to_listen_tasks(client) -> None:
    """侧边栏分组「任务配置」改名「监听任务」，并且**不再出现旧名字**。"""
    html = client.get("/").text

    assert "监听任务" in html, "分组没改名"
    assert "任务配置" not in html, "旧名字还在 —— 改名要改干净，别两处并存"


def test_nav_group_is_collapsible(client) -> None:
    """「监听任务」是一个**能展开/收起的子菜单**：按钮和子菜单的 id 必须对得上。

    JS 是靠 ``nav-group-<id>`` / ``nav-submenu-<id>`` 这两个 id 配对的，
    任何一边写错都会变成「点了没反应」，单看模板不容易发现。
    """
    html = client.get("/").text

    assert 'id="nav-group-listen"' in html
    assert 'aria-controls="nav-submenu-listen"' in html
    # 子菜单 id 挂在按钮的 data-submenu 上（JS 不拼字符串，拼错会静默失效）
    assert 'data-submenu="nav-submenu-listen"' in html
    assert "toggleNavGroup(this)" in html

    submenu = re.search(
        r'<div class="nav-submenu" id="nav-submenu-listen">(.*?)\n\s*</div>', html, re.S
    )
    assert submenu is not None, "子菜单必须带 id —— JS 靠 id 找到它再切 .collapsed"
    body = submenu.group(1)
    assert 'href="/rules"' in body, "转发规则挪出子菜单了"
    assert 'href="/red_packet"' in body, "抢红包挪出子菜单了"
    assert 'href="/reg_grab"' in body, "抢注任务挪出子菜单了"


def test_nav_group_title_aligns_with_main_menu(client) -> None:
    """分组标题的文字必须和主菜单项的**文字对齐**。

    🔴 坑：主菜单项是「18px 图标 + 10px 间距 + 文字」，文字从 ``12+18+10=40px``
    处开始；而分组标题原先只有一个裸 ``<span>``，文字从 ``12px`` 开始 ——
    整整左移 28px，肉眼看就是「监听任务没和主菜单对齐」。所以它必须也带一个
    同尺寸的前导图标，且不能用 ``space-between`` 把三个子元素均匀撑开。
    """
    html = client.get("/").text
    button = re.search(r'<button[^>]*id="nav-group-listen".*?</button>', html, re.S)
    assert button is not None, "找不到分组标题按钮"
    body = button.group(0)

    leading = re.search(r'<svg class="nav-group-leading"', body)
    assert leading is not None, "分组标题没有前导图标 —— 文字会比主菜单左移一个图标宽度"
    assert leading.start() < body.index("<span>监听任务</span>"), "前导图标必须在文字前面"

    css_path = Path(tg_assistant.__file__).parent / "web" / "static" / "css" / "style.css"
    css = css_path.read_text(encoding="utf-8")

    def icon_size(selector: str) -> tuple[str, str]:
        block = re.search(re.escape(selector) + r"\s*\{(.*?)\}", css, re.S)
        assert block is not None, f"style.css 里找不到 {selector}"
        width = re.search(r"width:\s*([^;]+);", block.group(1))
        height = re.search(r"height:\s*([^;]+);", block.group(1))
        assert width and height, f"{selector} 没写死宽高"
        return (width.group(1).strip(), height.group(1).strip())

    assert icon_size(".nav-group-leading") == icon_size(".nav-item svg"), (
        "前导图标尺寸必须和主菜单图标一致，否则文字仍然对不齐"
    )

    toggle = re.search(r"\.nav-group-toggle\s*\{(.*?)\}", css, re.S)
    item = re.search(r"\.nav-item\s*\{(.*?)\}", css, re.S)
    assert toggle and item
    assert "gap: 10px" in toggle.group(1), "图标与文字的间距要和主菜单一致"
    assert "gap: 10px" in item.group(1)
    assert "space-between" not in toggle.group(1), (
        "space-between 会把文字从 40px 处撑开，改用 chevron 的 margin-left:auto"
    )


def test_nav_collapsed_class_really_hides_submenu() -> None:
    """``.collapsed`` 必须真的藏得住子菜单。

    🔴 坑：``.nav-submenu`` 自带 ``display: flex``，如果 ``.collapsed`` 写在它**前面**，
    会被同权重的 flex 盖掉 ⇒ 「收起了但内容还在」这种最尴尬的半失效状态。
    """
    css_path = Path(tg_assistant.__file__).parent / "web" / "static" / "css" / "style.css"
    css = css_path.read_text(encoding="utf-8")

    base = css.index(".nav-submenu {")
    collapsed = css.index(".nav-submenu.collapsed {")
    assert base < collapsed, ".collapsed 必须排在 .nav-submenu 之后，否则 display:flex 会盖掉它"
    assert "display: none" in css[collapsed : collapsed + 120]


@pytest.mark.parametrize("path", ["/config/{name}", "/chats/{name}"])
def test_account_pages_redirect_for_unknown_account(client, app, path: str) -> None:
    resp = client.get(path.format(name="ghost"), follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/accounts"


def test_nav_links_are_present(client) -> None:
    """新增页面的导航入口不能漏。"""
    html = client.get("/").text
    for href in [
        "/rules",
        "/red_packet",
        "/reg_grab",
        "/notify",
        "/login",
        "/accounts",
        "/logs",
        "/proxy",
    ]:
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


def test_shared_form_selectors_cover_every_page_prefix() -> None:
    """共享表单选择器必须**三个前缀齐全** —— 少一个就是整页控件没样式。

    真实事故：``.rp-input-group input, .nt-input-group input,`` 这一行列了 rp/nt，
    下一行只写了 ``.rg-input-group textarea`` —— ``.rg-input-group input`` 谁都没提。
    后果是抢注页所有输入框退回浏览器默认样式（深色主题上一片惨白），
    而 :func:`test_every_template_class_is_defined_in_css` **查不出来** ——
    类名 ``rg-input-group`` 本身是定义过的，缺的是元素选择器。
    """
    web_dir = Path(__file__).resolve().parents[1] / "tg_assistant" / "web"
    css = (web_dir / "static" / "css" / "style.css").read_text(encoding="utf-8")
    # 三个「同构页面」前缀：抢红包 / 通知 / 抢注。
    prefixes = ("rp", "nt", "rg")

    for element in ("input", "select", "textarea"):
        for prefix in prefixes:
            selector = f".{prefix}-input-group {element}"
            assert selector in css, f"style.css 里没有 {selector} —— 这个页面的 {element} 会没样式"
        # 焦点态同样要齐全，否则点了输入框只有部分页面有高亮。
        for prefix in prefixes:
            selector = f".{prefix}-input-group {element}:focus"
            assert selector in css, f"style.css 里没有 {selector}"


def test_every_master_toggle_shows_its_checked_state() -> None:
    """每个 ``*-master-toggle`` 都必须有 ``:checked + .toggle-slider`` 规则。

    🔴 真实事故：``:checked`` 那两条只写了 ``.rules-master-toggle``，而 ``input``
    又被 ``display: none`` 藏了起来 —— 于是抢红包 / 抢注 / 通知 / 优选IP 四个页面的
    开关**永远是灰的**，滑块也不动。功能其实是好的（label 点击照样切换隐藏的
    checkbox），但**零视觉反馈**，用户根本看不出开着还是关着。原话：
    「开关都不会动，开着还是关闭看不出来」。

    这里按模板**实际用到的类名**逐个检查，所以以后新增页面也跑不掉。
    """
    web_dir = Path(__file__).resolve().parents[1] / "tg_assistant" / "web"
    css = (web_dir / "static" / "css" / "style.css").read_text(encoding="utf-8")

    # 按**结构**探测，而不是猜类名：`<label class="X"><input type=checkbox>
    # <span class="toggle-slider">` —— X 就是那个需要 :checked 规则的包装类。
    #
    # 一开始是按 `endswith("master-toggle")` 找的，于是新加的
    # `.rp-task-toggle`（任务卡片上的小开关）**完全逃过检查** ——
    # 它同样是"input 被 display:none 藏起来、全靠 :checked 改滑块"的写法。
    prefixes: set[str] = set()
    for tpl in sorted((web_dir / "templates").glob("*.html")):
        html = tpl.read_text(encoding="utf-8")
        for match in re.finditer(
            r'<label class="([^"]+)"[^>]*>\s*'
            r'<input[^>]*type="checkbox"[^>]*>\s*'
            r'<span class="toggle-slider">',
            html,
        ):
            prefixes.add(match.group(1).strip())
    assert prefixes, "前提：至少有一个页面用了滑块式开关"

    # 先把注释剥掉：`/* ... */` 里没有花括号，会被下面的正则当成选择器的一部分
    # 粘在后面 —— `.rp-task-toggle` 那条前面刚好有注释，于是精确匹配失败，
    # 测试报了个**假**失败。注释不是选择器。
    css = re.sub(r"/\*.*?\*/", " ", css, flags=re.S)

    # 把 CSS 拆成「选择器块」，再按逗号拆成**单条**选择器，然后精确比对。
    #
    # 🔴 这里必须精确匹配，不能子串匹配：`.x input:checked + .toggle-slider::after`
    # 天然包含 `.x input:checked + .toggle-slider` —— 用子串查的话，就算
    # background 那条被删了也照样"找得到"，测试成了摆设（第一版就是这么写的，
    # 故意删掉 `.rg-` 那行仍然是 43 passed）。
    selectors: set[str] = set()
    for block in re.findall(r"([^{}]+)\{", css):
        for selector in block.split(","):
            selectors.add(" ".join(selector.split()))

    for prefix in sorted(prefixes):
        assert f".{prefix} input:checked + .toggle-slider" in selectors, (
            f"{prefix} 没有 `:checked + .toggle-slider` 规则 ⇒ 这个页面的开关永远是灰的"
        )
        assert f".{prefix} input:checked + .toggle-slider::after" in selectors, (
            f"{prefix} 的滑块不会位移 ⇒ 看不出开没开"
        )


def test_static_version_follows_the_file_without_a_restart(tmp_path) -> None:
    """🔴 改了静态文件、但服务没重启时，版本号必须跟着变。

    否则页面继续带**旧**的 ``?v=``，浏览器照旧吃缓存里的旧 CSS —— 用户看到的
    还是没修的样子，而服务端一切正常、日志里毫无报错。

    真实事故：部署时「先重启、后落盘 CSS」，页面 ``style.css?v=1790345286``
    而文件 mtime 是 ``1790347538``，版本号比文件还旧。
    """
    from tg_assistant.web import _build_template_env

    (tmp_path / "templates").mkdir()
    static = tmp_path / "static"
    static.mkdir()
    css = static / "style.css"
    css.write_text("a{}", encoding="utf-8")
    os.utime(css, (1_000_000, 1_000_000))

    env = _build_template_env(tmp_path / "templates")
    get_version = env.globals["static_version"]
    assert callable(get_version), "必须是可调用的 —— 存成常量就又回到启动时缓存了"
    first = get_version()

    os.utime(css, (1_000_500, 1_000_500))
    assert get_version() != first, "文件变了，版本号没变 ⇒ 浏览器会一直用旧 CSS"

    # 模板也必须真的**调用**它，否则惰性求值白做。
    base = (Path(__file__).resolve().parents[1] / "tg_assistant" / "web" / "templates" / "base.html")
    html = base.read_text(encoding="utf-8")
    assert "{{ static_version() }}" in html, "base.html 里必须写成 static_version()"
    assert "{{ static_version }}" not in html, "漏了括号就渲染成函数对象本身了"


# --------------------------------------------------------------------------- #
# 模板内联 JS 的语法
# --------------------------------------------------------------------------- #
_WEB_DIR = Path(__file__).resolve().parents[1] / "tg_assistant" / "web"

def _inline_scripts(html: str) -> list[str]:
    """页面里所有**内联** ``<script>`` 的内容（带 ``src=`` 的跳过）。"""
    return re.findall(
        r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, flags=re.S | re.I
    )


#: 一个花括号，或一条 ``const|let|var NAME`` 声明。
_JS_TOKEN_RE = re.compile(r"[{}]|\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)")
_JS_FUNC_RE = re.compile(r"\bfunction\s+([A-Za-z_$\w]*)\s*\([^)]*\)\s*\{")


def _strip_js_literals(js: str) -> str:
    """去掉注释与字符串字面量。

    不去的话，字符串里的 ``"const account"`` 会被当成真声明；
    注释掉的一行同理。
    """
    out: list[str] = []
    i, n = 0, len(js)
    while i < n:
        char = js[i]
        if char == "/" and i + 1 < n and js[i + 1] == "/":
            end = js.find("\n", i)
            i = n if end < 0 else end
        elif char == "/" and i + 1 < n and js[i + 1] == "*":
            end = js.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif char in "\"'`":
            quote, i = char, i + 1
            while i < n:
                if js[i] == "\\":
                    i += 2
                    continue
                if js[i] == quote:
                    i += 1
                    break
                i += 1
        else:
            out.append(char)
            i += 1
    return "".join(out)


def _js_body_end(js: str, start: int) -> int:
    """``start`` 是函数 ``{`` 之后的下标；返回配对 ``}`` 的下标。"""
    depth, i = 1, start
    while i < len(js) and depth:
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
        i += 1
    return i - 1


def _mask_js_functions(js: str) -> str:
    """把每个 ``function`` 的函数体换成等长空白，只留签名。

    扫「顶层」时必须这么做：不抠掉的话，各函数内部的声明会全部落到
    同一个花括号深度上，``data`` / ``res`` 这种每个函数都在用的局部变量名
    会被误报 —— 实测能报出 9 处假命中。
    """
    out = list(js)
    for match in _JS_FUNC_RE.finditer(js):
        for index in range(match.end(), _js_body_end(js, match.end())):
            out[index] = " "
    return "".join(out)


def _duplicate_js_declarations(body: str) -> list[tuple[int, str, int]]:
    """同一作用域、**同一花括号深度**上重复声明的名字。"""
    depth, seen = 0, {}
    for match in _JS_TOKEN_RE.finditer(body):
        token = match.group(0)
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
        else:
            seen.setdefault(depth, []).append(match.group(1))
    found = []
    for depth, names in seen.items():
        for name in sorted(set(names)):
            if names.count(name) > 1:
                found.append((depth, name, names.count(name)))
    return found


def test_no_duplicate_declarations_in_inline_scripts() -> None:
    """模板内联 JS 里不许在同一作用域重复声明同一个名字。

    🔴 真实事故：``login.html`` 的 ``sendCode()`` 先
    ``const account = document.getElementById('code-account')...``，
    后面又 ``const account = data.account;``。重复声明 ``const`` 是
    **SyntaxError**，而浏览器是**整块**解析 ``<script>`` 的 —— 于是登录页那
    12659 个字符的脚本一行都不执行：扫码、验证码、2FA、切换标签的按钮
    全部变成哑巴，控制台里只有一句
    ``Identifier 'account' has already been declared``。

    这种错**不会**被任何「页面打得开吗」的测试发现 —— 页面 200、HTML 完全正常、
    HTTP 状态码一个不差，只有真去点一下才知道。所以这里做静态检查。

    纯 Python 实现：生产服务器上没有 node，不能为了这条检查给部署环境加依赖。
    更严格的「整块语法检查」见 :func:`test_inline_script_is_valid_javascript`。
    """
    problems: list[str] = []
    for template in sorted(_WEB_DIR.joinpath("templates").glob("*.html")):
        html = template.read_text(encoding="utf-8")
        for code in _inline_scripts(html):
            if not code.strip():
                continue
            clean = _strip_js_literals(code)
            # 顶层：先抠掉所有函数体。
            for depth, name, count in _duplicate_js_declarations(_mask_js_functions(clean)):
                problems.append(f"{template.name} 顶层(深度{depth})：`{name}` 声明了 {count} 次")
            # 每个函数体：再抠掉它里面的嵌套函数。
            for match in _JS_FUNC_RE.finditer(clean):
                body = clean[match.end() : _js_body_end(clean, match.end())]
                scope = match.group(1) or "<匿名函数>"
                for depth, name, count in _duplicate_js_declarations(_mask_js_functions(body)):
                    problems.append(
                        f"{template.name} {scope}()(深度{depth})：`{name}` 声明了 {count} 次"
                    )

    assert not problems, (
        "同一作用域里重复声明会让**整块**脚本解析失败、页面上所有按钮失效：\n  "
        + "\n  ".join(problems)
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 才能做 JS 语法检查")
@pytest.mark.parametrize(
    "template", sorted(p.name for p in _WEB_DIR.joinpath("templates").glob("*.html"))
)
def test_inline_script_is_valid_javascript(template: str) -> None:
    """页面里的内联 JS 必须**能解析**。

    🔴 真实事故：``login.html`` 的 ``sendCode()`` 里声明了两次 ``const account``。
    同一作用域里重复声明 const 是 SyntaxError，而浏览器是**整块**解析脚本的 ——
    于是登录页那 12659 个字符的脚本一行都不执行：扫码、验证码、2FA、切换标签
    的按钮全部变成哑巴，控制台里只有一句
    ``Identifier 'account' has already been declared``。

    这种错**不会**被任何「页面打得开吗」的测试发现 —— 页面 200、HTML 完全正常、
    HTTP 状态码一个不差，只有真去点一下才知道。所以这里用 node 把每块脚本
    解析一遍。

    服务器上没装 node 时跳过：不要为了这条检查去给生产环境加依赖。
    """
    html = (_WEB_DIR / "templates" / template).read_text(encoding="utf-8")
    blocks = _inline_scripts(html)
    assert blocks, f"{template} 里一块内联脚本都没有？选择器可能失效了"

    for index, code in enumerate(blocks):
        if not code.strip():
            continue
        # Jinja 表达式会让 JS 解析失败，先换成占位符。本项目模板的 script 块里
        # 目前一个 Jinja 标签都没有，这一步只是防止将来误报。
        code = re.sub(r"\{\{.*?\}\}", "0", code, flags=re.S)
        code = re.sub(r"\{%.*?%\}", "", code, flags=re.S)
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(code)
            path = handle.name
        try:
            proc = subprocess.run(
                ["node", "--check", path], capture_output=True, text=True, timeout=30
            )
        finally:
            os.unlink(path)
        assert proc.returncode == 0, (
            f"{template} 第 {index + 1} 块内联 JS 解析失败 —— "
            f"整块脚本一行都不会执行：\n{proc.stderr}"
        )


def test_time_inputs_render_in_dark_mode() -> None:
    """``<input type="time">`` 要显式声明 ``color-scheme: dark``。

    不声明时，原生时钟图标/下拉在深色输入框上几乎是黑的 —— 肉眼看不见，
    而且只有点开才发现，属于「截图里根本看不出来」的那类问题。
    """
    web_dir = Path(__file__).resolve().parents[1] / "tg_assistant" / "web"
    css = (web_dir / "static" / "css" / "style.css").read_text(encoding="utf-8")
    reg_grab = (web_dir / "templates" / "reg_grab.html").read_text(encoding="utf-8")

    assert 'type="time"' in reg_grab, "前提：抢注页确实用了原生时间控件"
    assert "color-scheme: dark" in css, "原生时间控件在深色主题下会看不见图标"


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
        used = set(re.findall(r"getElementById\(\s*['\"]([^'\"]+)['\"]\s*\)", html))
        gap = sorted(used - declared - dynamic - base_ids)
        if gap:
            missing[tpl.name] = gap
        # 拼接式查找（``getElementById('metric-' + kind)``）静态判不出完整 id，
        # 但**能**判前缀：至少要有一个已声明的 id 以它开头。
        # 只放过这一种写法，不是"看不懂就跳过" —— 前缀根本不存在同样是错的。
        for prefix in re.findall(r"getElementById\(\s*['\"]([^'\"]+)['\"]\s*\+", html):
            if not any(each.startswith(prefix) for each in declared | dynamic):
                missing.setdefault(tpl.name, []).append(f"{prefix}*（拼接式，没有任何 id 以此开头）")

    assert not missing, f"这些模板引用了不存在的元素 id（JS 会抛 null）: {missing}"


def test_dashboard_shows_the_metrics_board() -> None:
    """仪表盘上的「数据大盘」：三个成功计数 + 天/月/总 + **北京时间**那行说明。

    用户原话：「在仪表盘加一个数据大盘，记录总转发次数，总抢包次数，总抢注次数，
    只记录成功的，需要可选天，月，总，天，按北京时间0点开始计算」。

    这里钉的是**用户能不能看懂这几个数是怎么算的**：「天」如果只写个「今天」，
    用户根本不知道它从几点起算（服务跑在 UTC 上时会差 8 小时）。
    """
    tpl = (
        Path(__file__).resolve().parents[1]
        / "tg_assistant"
        / "web"
        / "templates"
        / "dashboard.html"
    )
    html = tpl.read_text(encoding="utf-8")

    assert "数据大盘" in html
    for element_id in ("metric-forward", "metric-red_packet", "metric-reg_grab"):
        assert f'id="{element_id}"' in html, f"{element_id} 没了 ⇒ 大盘上那个数永远是空的"
    for label in ("总转发次数", "总抢包次数", "总抢注次数"):
        assert label in html, f"少了计数项 {label}"
    for window in ("day", "month", "total"):
        assert f'data-range="{window}"' in html, f"少了「{window}」这个口径的切换按钮"
    assert "北京时间" in html, "用户明确要求「按北京时间0点开始计算」，页面上必须写出来"
    assert "只统计成功" in html, "用户要求「只记录成功的」，得让用户知道别的没算进来"


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


def test_rules_page_exposes_both_sender_lists() -> None:
    """面板必须能配「发送者黑名单」—— 后端早就有了，但曾经**根本没有入口**。

    🔴 2026-09-26 用户原话：「给转发模块加上一个黑名单功能，当检测到是黑名单内人员
    发送的符合正则的消息，不予转发」。当时 ``ForwardRule.exclude_users``、
    ``PreparedRule.sender_allowed`` 全都实现好了、也有测试，但面板里只有白名单
    ``from_users``，**账户级连字段都没有** —— 功能在、用户摸不到，等于没有。

    所以这里钉住三层：弹窗里的规则级输入框、分组标题下的账号级名单、
    以及保存时真的把字段带上（漏了就是"界面上加了、存盘就丢"）。
    """
    web_dir = Path(__file__).resolve().parents[1] / "tg_assistant" / "web"
    html = (web_dir / "templates" / "rules.html").read_text(encoding="utf-8")

    # 规则级（弹窗内，紧挨白名单）
    assert 'id="exclude-users-input"' in html, "弹窗里缺少规则级黑名单输入框"
    assert 'id="exclude-users-field"' in html
    assert "exclude_users: tagInputs.exclude_users" in html, "保存时没带上黑名单字段"
    assert "tagInputs.exclude_users = " in html, "编辑规则时没把已有黑名单回填"

    # 账号级（分组标题下，和「排除频道」并排）
    assert "'exclude-users'" in html, "缺少账号级黑名单的名单定义"
    assert "forward-exclude-users" in html, "账号级黑名单没有对应的保存端点"
    assert "excludeListRow(acc, 'exclude-users')" in html, "账号级黑名单没有渲染出来"


def test_rules_page_warns_that_caret_is_per_line() -> None:
    """面板必须讲清楚「整条消息」要用 ``\\A`` / ``\\Z``。

    🔴 2026-09-26 用户反馈：他写了
    ``^(?=[\\s\\S]*本期尊贵赞助商)(?![\\s\\S]*(?:…抽奖即将开奖提醒))[\\s\\S]*$``
    来排除「抽奖即将开奖提醒」，结果**照转**。根因是多行模式下 ``^`` 在每一行都成立，
    引擎会退到后面某行重新匹配，让 ``(?!…)`` 看不见前面的关键词。

    这类坑的共同点是**不报错、只是结果不对**，用户没有任何线索可循 ——
    所以这段提示语本身就是功能的一部分，不能被当成装饰删掉。
    """
    web_dir = Path(__file__).resolve().parents[1] / "tg_assistant" / "web"
    html = (web_dir / "templates" / "rules.html").read_text(encoding="utf-8")

    assert "\\A" in html and "\\Z" in html, "缺少 \\A / \\Z 的写法说明"
    assert "整条消息" in html, "没点明 \\A / \\Z 是给「整条消息」用的"
    assert "form-hint-warn" in html, (
        "这种会静默出错的坑要用独立样式，不能混在普通提示里"
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


# --------------------------------------------------------------------------- #
# 下拉框默认选中项
# --------------------------------------------------------------------------- #

#: 页面 -> 它该用的功能标记（`/api/accounts` 的 `features` 键）。
FEATURE_PAGES = {
    "cloudflare_ip.html": "cloudflare_ip",
    "notify.html": "notify",
    "red_packet.html": "red_packet",
    # ⚠️ reg_grab.html / rules.html **故意**不在这里：这两页已经没有「当前账号」这个
    # 概念了 —— 它们按任务维度渲染**所有**账号（同一张卡片带「监听账号」勾选），
    # 页面上根本没有账号下拉框，也就无所谓"默认选中哪个账号"。
    # 「每个账号都得出现」这条约束由 test_reg_grab_page_is_a_task_list_with_an_editor_modal
    # 里的 renderAccountGroup 断言接手。
}


@pytest.mark.parametrize(("template", "feature"), sorted(FEATURE_PAGES.items()))
def test_feature_pages_pick_a_configured_account(template: str, feature: str) -> None:
    """功能页的账号下拉不能无脑选第一个账号。

    多账号时功能往往只配在其中一个账号上，默认选中没配的那个，
    用户打开页面看到的是空配置 + 状态卡「未运行」，会以为功能坏了。
    （实测：优选 IP 配在 SevenStar 上，页面默认选中了小白。）
    """
    web_dir = Path(__file__).resolve().parents[1] / "tg_assistant" / "web"
    html = (web_dir / "templates" / template).read_text(encoding="utf-8")

    assert f"pickDefaultAccount(data.accounts, '{feature}')" in html, (
        f"{template} 没用 pickDefaultAccount(data.accounts, '{feature}') 选默认账号"
    )
    assert "data.accounts[0].name" not in html, (
        f"{template} 还在无脑选第一个账号"
    )


def test_pick_default_account_helper_exists() -> None:
    web_dir = Path(__file__).resolve().parents[1] / "tg_assistant" / "web"
    js = (web_dir / "static" / "js" / "app.js").read_text(encoding="utf-8")

    assert "function pickDefaultAccount(" in js
    # 认得 /api/accounts 的 features 字段
    assert "a.features" in js


def test_account_status_defaults_to_cheap() -> None:
    """``account_status()`` 默认不读配置文件 —— `/api/status` 是 5 秒一次的轮询。"""
    src = (
        Path(__file__).resolve().parents[1]
        / "tg_assistant" / "web" / "runtime.py"
    ).read_text(encoding="utf-8")

    assert "def account_status(self, *, with_features: bool = False)" in src


def test_cloudflare_status_card_is_split_aware() -> None:
    """分流模式下「上次结果」要按**条数**说，不能拿单个运营商的速度冒充整体结果。

    线上踩过：面板显示「上次结果: 97.96 MB/s（失败）」，而实际三条记录全写成功
    —— 那 97.96 只是联通一家的速度，而且是**开启分流之前**那次调度留下的陈旧值。
    """
    web_dir = Path(__file__).resolve().parents[1] / "tg_assistant" / "web"
    html = (web_dir / "templates" / "cloudflare_ip.html").read_text(encoding="utf-8")

    assert "lr.ok_count" in html
    assert "lr.failed_count" in html
    assert "lr.split_by_isp" in html, "分流与不分流必须走不同文案"
    # ⚠️ 「三家都没更快所以跳过」是正常结果，不能写成「有异常」——
    # 实测三家一起被跳过时面板会显示成故障，用户据此以为功能坏了。
    assert "'已跳过'" in html, "全跳过时要显示「已跳过」，不能报成异常/失败"
    assert "有异常" not in html
    # 「写 1 条、跳 2 家」时也要把跳过数说出来，否则看着像只处理了一家。
    # ⚠️ 断言要**具体到那一行**：`lr.skipped_count` 在模板里出现两处，
    # 只断言名字存在的话，把其中一处删掉测试照样过（注入验证时真漏网过）。
    assert "${lr.skipped_count} 条跳过" in html, "部分跳过时要把跳过条数一并报出来"
    assert "if (lr.skipped_count" in html, "跳过条数要真的参与条件判断，不能是死代码"
    # ⚠️ 判「已跳过」不能只看 lr.skipped —— 那个来自 skipped_reason，
    # 只有「一家都没通过」那条路径才会写；部分跳过时它是 None。
    assert "lr.skipped || lr.skipped_count" in html, (
        "部分跳过（skipped_count>0 但没有 skipped_reason）也要判成「已跳过」"
    )
    # 跳过原因行原来只在 lr.skipped 时显示，部分跳过时会漏掉原因。
    assert "if (lr.skipped_reason)" in html


# --------------------------------------------------------------------------- #
# 「测试抓取」/「立即触发」：按钮不能永远停在「抓取中...」
# --------------------------------------------------------------------------- #
def _cf_html() -> str:
    web_dir = Path(__file__).resolve().parents[1] / "tg_assistant" / "web"
    return (web_dir / "templates" / "cloudflare_ip.html").read_text(encoding="utf-8")


def _js_function_body(html: str, name: str) -> str:
    """截出 ``async function <name>(...) { ... }`` 的函数体（按大括号配平）。

    ⚠️ 不能用「从头截到下一个 ``function``」那种土办法：这两个函数体里都有
    ``} catch (err) {``，而且都是文件里最后两个函数，不配平根本截不对。
    """
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\(", html)
    assert match, f"模板里找不到 {name}()"
    start = html.index("{", match.end())
    depth = 0
    for index in range(start, len(html)):
        char = html[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return html[start : index + 1]
    raise AssertionError(f"{name}() 的大括号没配平")


@pytest.mark.parametrize(
    "func,spinner",
    [("testFetch", "抓取中..."), ("triggerUpdate", "更新中...")],
)
def test_cloudflare_buttons_always_clear_the_spinner(func: str, spinner: str) -> None:
    """🔴 回归：线上「测试抓取」一直停在「抓取中...」，只能刷新页面。

    原因是这两个函数**既没有 ``try/catch`` 也没有超时** —— 只要请求不返回
    （服务重启、后端卡住、网络中断），就没人去把 spinner 换掉。
    所以这里钉三条：

    1. 请求走带超时的 ``postJson()``（``AbortController``）；
    2. 异常有 ``catch``，并且会渲染成一行明确的提示；
    3. ``box.innerHTML = html`` 必须在 ``try`` **外面** —— 放进 try 里的话，
       异常一抛它就执行不到，spinner 照样留着。
    """
    body = _js_function_body(_cf_html(), func)

    assert spinner in body, f"{func}() 没有先渲染「{spinner}」占位"
    assert "postJson(" in body, f"{func}() 没用带超时的 postJson()，请求会永远挂着"
    assert "} catch (err) {" in body, f"{func}() 没有 catch，异常时 spinner 永远留在页面上"
    assert "requestErrorHtml(err)" in body, f"{func}() 的异常分支没有给用户任何文案"

    # 3. spinner 的清除必须无条件执行：出现在 catch 之后
    catch_at = body.index("} catch (err) {")
    clear_at = body.rindex("box.innerHTML = html;")
    assert clear_at > catch_at, (
        f"{func}() 把「换掉 spinner」写进了 try 里 —— 抛异常时它就执行不到，"
        "按钮会永远转圈"
    )


def test_post_json_has_a_timeout_and_clears_it() -> None:
    """``postJson`` 是这两个按钮唯一的请求出口，超时逻辑只许写在这里。"""
    body = _js_function_body(_cf_html(), "postJson")

    assert "AbortController" in body
    assert "ctrl.abort()" in body, "建了 AbortController 却没人 abort —— 等于没有超时"
    assert "signal: ctrl.signal" in body, "没把 signal 传给 fetch，abort 不会生效"
    assert "clearTimeout(timer)" in body, "正常返回后要清掉定时器"
    assert "finally" in body, "清定时器要放 finally，抛异常时也得清"


def test_frontend_timeout_outlasts_the_backend_one() -> None:
    """前端兜底超时必须**长于**后端超时之和。

    反过来的话前端先 abort，用户看到的是笼统的「请求超时」，
    而后端那条更精确的 504（「账号可能正被其它任务占用，稍后重试」）
    永远没机会显示 —— 排查时又得回到「服务端日志一片空白」的境地。
    """
    root = Path(__file__).resolve().parents[1] / "tg_assistant"
    api_src = (root / "web" / "routers" / "api.py").read_text(encoding="utf-8")

    front = re.search(r"CF_REQUEST_TIMEOUT_MS\s*=\s*(\d+)", _cf_html())
    assert front, "cloudflare_ip.html 里找不到 CF_REQUEST_TIMEOUT_MS"
    client_timeout = re.search(r"^CLIENT_TIMEOUT\s*=\s*([\d.]+)", api_src, re.M)
    fetch_timeout = re.search(r"^FETCH_TIMEOUT\s*=\s*([\d.]+)", api_src, re.M)
    assert client_timeout and fetch_timeout, "api.py 里的超时常量改名了？"

    backend_worst = float(client_timeout.group(1)) + float(fetch_timeout.group(1))
    frontend = int(front.group(1)) / 1000
    assert frontend > backend_worst, (
        f"前端 {frontend:.0f}s 撑不到后端最坏情况 {backend_worst:.0f}s"
        f"（建 client {client_timeout.group(1)}s + 抓取 {fetch_timeout.group(1)}s）"
    )


# --------------------------------------------------------------------------- #
# 规则页的「启动」按钮：账号已在运行时必须能重启（不是报错）
# --------------------------------------------------------------------------- #
def _rules_html() -> str:
    web_dir = Path(__file__).resolve().parents[1] / "tg_assistant" / "web"
    return (web_dir / "templates" / "rules.html").read_text(encoding="utf-8")


def test_rules_start_button_becomes_restart_when_running() -> None:
    """🔴 回归：账号在运行时点「启动」必然失败（线上就是这么踩的）。

    历史上转发规则只在账号**启动时**读一次，所以「改完规则 → 点启动」是用户
    表达「让新规则生效」的唯一动作。而账号正在跑时后端原来直接返回
    ``ok=False / "账号 X 已在运行"``，于是这条最自然的操作路径必然失败。

    ⚠️ 规则现已**自动热重载**（`5ecb201`），改完不用再点这个按钮；
    但它在运行中仍必须显示成「重启」—— 重启账号本身（卡住 / 重连会话）
    依然是它的职责，而且用户已经习惯了这个位置。

    这里钉前端那一半：按钮在运行中必须显示成「重启」，
    否则用户根本不知道该点哪儿（页面上只有「启动」和「停止」两个键）。
    """
    html = _rules_html()

    assert "acc.running ? '重启' : '启动'" in html, (
        "运行中的账号，按钮文案必须变成「重启」——否则用户只会反复点「启动」然后失败"
    )
    assert "重启该账号（改规则不用点这里，会自动生效）" in html, "按钮 title 没说明重启的目的"

    # 图标也要跟着换：播放三角 ≠ 重启。
    # ⚠️ 断言必须**限定在按钮那一块**里 —— 页面顶部的「刷新」按钮用的是同一个
    # polyline，全文搜的话这条断言等于没写。
    start_at = html.index('data-action="start"')
    button = html[start_at : html.index("</button>", start_at)]
    assert "acc.running" in button, "按钮没有按运行状态分支"
    assert 'polyline points="23 4 23 10 17 10"' in button, "运行中应该显示「重启」图标"
    assert 'polygon points="5 3 19 12 5 21 5 3"' in button, "未运行时应该显示「启动」图标"


def test_rules_start_account_shows_backend_message() -> None:
    """成功时要把后端的 ``message``（「已重启「X」」）显示出来。"""
    body = _js_function_body(_rules_html(), "startAccount")

    assert "data.message" in body, "成功分支没用后端的 message，用户看不到「已重启」"


def test_detail_text_falls_back_to_message() -> None:
    """🔴 回归：``/api/run/*`` 的原因在 ``message`` 里，前端却只读 ``detail``。

    线上症状就是用户的原话 ——「转发规则启动失败了」：他看到的四个字
    **就是** ``detailText()`` 返回空串之后的兜底文案 ``'启动失败'``，
    真正的理由（「已经在运行中，请先停止」）被前端丢掉了。
    """
    body = _js_function_body(_rules_html(), "detailText")

    assert "data.detail" in body, "FastAPI 的 HTTPException 走的是 detail"
    assert "data.message" in body, (
        "/api/run/* 返回 {ok, message}，只读 detail 会让所有这类错误退化成"
        "兜底文案「启动失败」，用户看不到原因"
    )


# --------------------------------------------------------------------------- #
# 规则弹窗的标签输入框：字段名（下划线）与 DOM id（连字符）必须对得上
# --------------------------------------------------------------------------- #
def test_rules_tag_input_ids_match_the_dom() -> None:
    """🔴 回归：4 个多词字段的标签输入框曾经**完全失效**。

    ``tagInputs`` 的键是接口字段名（``exclude_sources``），DOM id 却是
    ``exclude-sources-field``。而 ``initTagInputs`` / ``renderTags`` 直接拿
    字段名拼 id，于是 ``getElementById`` 返回 ``null``，又被
    ``if (!input) return`` / ``if (!list) return`` **静默吞掉** —— 控制台一声不吭。

    用户侧症状（原话）：「排除来源也要像转发目标那样，回车添加」。
    实际受影响的四个：排除来源、发送者白名单、发送者黑名单、排除规则；
    单字字段（sources / targets / patterns）因为拼出来正好一样而幸免 ——
    这也解释了为什么只有"某些"输入框看起来是好的。

    连带后果比「回车没反应」更严重：``renderTags`` 拼的是同一个错 id，所以
    编辑一条已有规则时这几个标签**根本不显示**，用户既看不到已配的值，
    也点不到 × 删掉它。
    """
    html = _rules_html()

    # 1) 从 JS 里取出 tagInputs 的全部键（下划线形态）
    block = re.search(r"const tagInputs = \{(.*?)\};", html, re.S)
    assert block, "找不到 tagInputs 定义"
    keys = re.findall(r"(\w+)\s*:", block.group(1))
    assert len(keys) >= 7, f"tagInputs 的键没解析出来：{keys}"

    # 2) 每个键都必须在 HTML 里有对应的连字符 id
    for key in keys:
        dom = key.replace("_", "-")
        assert f'id="{dom}-field"' in html, (
            f'tagInputs 有 {key}，但 HTML 里没有 id="{dom}-field" —— 回车添加会静默失效'
        )
        assert f'id="{dom}-list"' in html, (
            f'tagInputs 有 {key}，但 HTML 里没有 id="{dom}-list" —— 标签根本不显示'
        )

    # 3) JS 必须走 id 转换，不许再拿字段名直接拼 —— 第 2 步只查 HTML，
    #    单独把 JS 改回错写法时它照样通过，所以这一步不能省。
    assert "${tagDomId(field)}-field" in html
    assert "${tagDomId(field)}-list" in html
    assert "${field}-field" not in html, "又拿字段名直接拼 id 了（下划线 ≠ 连字符）"
    assert "${field}-list" not in html, "又拿字段名直接拼 id 了（下划线 ≠ 连字符）"


# --------------------------------------------------------------------------- #
# 优选 IP：「更新后发送通知」复选框
# --------------------------------------------------------------------------- #
def test_cloudflare_page_has_the_notify_checkbox() -> None:
    """面板上要有「更新后发送通知」复选框（``cf-notify``）。"""
    html = _cf_html()

    assert 'id="cf-notify"' in html, "面板缺少通知开关复选框"
    assert 'type="checkbox"' in html
    assert "更新后发送通知" in html, "复选框没有说明文案"
    # 默认勾上 = 与改动前行为一致（跟 cf-real-time 一个写法）
    assert re.search(r'<input id="cf-notify" type="checkbox" checked', html)


def test_cloudflare_page_loads_and_saves_the_notify_flag() -> None:
    """复选框必须真的接上读写 —— 只画一个不接线的框等于没做。

    ⚠️ 断言限定在 ``loadCfConfig()`` / ``saveCfConfig()`` 的函数体里：
    模板别处也可能出现 ``cf-notify`` / ``notify``，全文搜的话把读写删掉
    测试照样过。
    """
    html = _cf_html()

    load_body = _js_function_body(html, "loadCfConfig")
    assert "getElementById('cf-notify').checked = cfData.notify !== false" in load_body, (
        "加载配置时没把 notify 反映到复选框上（老配置没有该字段时要默认勾选）"
    )

    save_body = _js_function_body(html, "saveCfConfig")
    assert "notify: document.getElementById('cf-notify').checked" in save_body, (
        "保存时没把复选框的值写进请求体"
    )


def test_cloudflare_notify_toggle_roundtrips_through_the_api(account, client) -> None:
    """后端接口：默认 True（老前端不带该字段也照旧），提交 False 能写回。"""
    url = f"/api/config/{NAME}/cloudflare_ip"

    initial = client.get(url)
    assert initial.status_code == 200, initial.text
    assert initial.json()["notify"] is True, "默认必须是 True，保持现有行为"

    payload = dict(initial.json())
    payload["notify"] = False
    saved = client.put(url, json=payload)
    assert saved.status_code == 200, saved.text

    assert client.get(url).json()["notify"] is False, "notify=False 没写回配置"

    # 状态接口也要带上它（跟 real_time_listen 一样由面板读）
    status = client.get(f"{url}/status")
    assert status.status_code == 200, status.text
    assert status.json()["notify"] is False


# --------------------------------------------------------------------------- #
# 抢注页：任务维度（一条任务一张卡片，账号在弹窗里勾）
# --------------------------------------------------------------------------- #
def _reg_grab_html() -> str:
    web_dir = Path(__file__).resolve().parents[1] / "tg_assistant" / "web"
    return (web_dir / "templates" / "reg_grab.html").read_text(encoding="utf-8")


def test_reg_grab_page_is_a_task_list_with_an_editor_modal() -> None:
    """抢注页要跟规则页一样是「任务卡片列表 + 弹窗编辑」，而且按**任务**为维度。

    页面顶部原来有个「先选账号」的下拉框：不选账号什么都看不到，而一条任务往往
    要写给好几个账号。规则页走的就是这条路（转置 + 弹窗勾账号），抢注页对齐它。
    """
    html = _reg_grab_html()

    for dom in (
        'id="rg-task-list"',
        'id="rg-modal"',
        'id="rg-task-id"',
        'id="rg-task-name"',
        'id="rg-task-enabled"',
        # 账号在弹窗里勾（不勾 = 全部账号），跟规则页同一套交互
        'id="rg-accounts"',
        'id="rg-accounts-summary"',
        # 账号级设置（抢注总开关 / 并发上限 / 试发通知）收在折叠区里
        'id="rg-account-settings"',
    ):
        assert dom in html, f"抢注页缺少 {dom}"
    assert "新建任务" in html

    # 弹窗里要能配「每任务」的那几块：识别正则 / 步骤链 / 时段
    for dom in (
        'id="rg-code-pattern"',
        'id="rg-steps"',
        'id="rg-window-enabled"',
        'id="rg-window-start"',
        'id="rg-window-end"',
    ):
        assert dom in html, f"弹窗里缺少 {dom}"

    # 「先选账号」那套必须退场：页面顶部不再有账号下拉，也不再有「整页保存」
    assert 'id="account-selector"' not in html, "抢注页还留着「先选账号」的下拉框"
    assert "function switchAccount" not in html, "按账号切换的老逻辑还在"
    assert "function saveRegGrab" not in html, "单任务时代的整页保存函数还在"

    for func in (
        "renderTasks",
        "renderTaskCard",
        "openTaskModal",
        "collectTask",
        "saveTask",
        "setTaskEnabled",
        "setTaskOwner",
        "deleteTask",
        "saveConcurrency",
        "toggleRegGrab",
    ):
        assert f"function {func}(" in html, f"抢注页缺少 {func}()"

    # 任务维度 = 把「账号 → 任务」**转置**成「任务 → 账号」。不做这一步同一条任务会
    # 在页面上出现 N 次（规则页就是这么改的：用户原话「以任务为维度，而不是以账号」）。
    collect = _js_function_body(html, "collectTasks")
    assert "entry.owners.push(acc.name)" in collect, "collectTasks() 没有把任务合并成一条"
    assert "byId" in collect, "collectTasks() 没有按 id 合并"

    # 账号级开关不能因为"页面没有账号下拉框"就没地方改：每个账号一张小卡片。
    group = _js_function_body(html, "renderAccountGroup")
    for marker in (
        'data-role="rg-account-enabled"',
        'data-role="rg-concurrency"',
        'data-action="test-notify"',
    ):
        assert marker in group, f"账号设置里缺少 {marker}"


def test_reg_grab_page_uses_the_task_dimension_endpoints() -> None:
    """四个任务维度端点都得真的被调用 —— 少一个就会出现「界面上改了、磁盘没变」。"""
    html = _reg_grab_html()

    assert "'/api/reg_grab/overview'" in html, "没有用总览接口"
    assert "'/api/reg_grab/tasks'" in html, "没有用任务增删接口"
    # 路径里的 id 要过 encodeURIComponent：id 是用户自由输入的，空格 / # / / 都能把 URL 弄坏
    assert "/api/reg_grab/tasks/${encodeURIComponent(" in html

    save = _js_function_body(html, "saveTask")
    assert "saveNewTask(task)" in save, "新建没有走 POST"
    assert "saveEditedTask(task, adds, removes)" in save, "编辑没有落成「改 + 增 + 删」的差集"
    for name in ("adds", "removes"):
        assert name in save, f"编辑保存没算 {name} 差集"

    # 编辑 = PUT（所有现有副本）+ POST（新勾的账号）+ DELETE（取消勾选的账号）。
    # 只 PUT 的话，新勾的账号里根本没有这条任务，服务端算 missing：界面显示"勾上了"，
    # 磁盘上什么也没发生 —— 比锁着更糟。
    edited = _js_function_body(html, "saveEditedTask")
    assert "putJson(`/api/reg_grab/tasks/${encodeURIComponent(id)}`" in edited
    assert "postJson('/api/reg_grab/tasks'" in edited
    assert "`/api/reg_grab/tasks/${encodeURIComponent(id)}?accounts=${encodeURIComponent(name)}`" in edited

    # 启停 / 删除是**任务级**的：不带 accounts = 改所有监听账号里的这一份
    enabled = _js_function_body(html, "setTaskEnabled")
    assert "putJson(`/api/reg_grab/tasks/${encodeURIComponent(taskId)}`" in enabled
    removed = _js_function_body(html, "deleteTask")
    assert "deleteJson(`/api/reg_grab/tasks/${encodeURIComponent(taskId)}`" in removed


def test_reg_grab_task_card_actions_are_delegated() -> None:
    """卡片上的编辑 / 删除 / 启停 / 监听账号**不许**把 ``task.id`` 拼进内联 handler。

    ``RegGrabTask.id`` 是用户自由输入的，只要含一个单引号就能把
    ``onclick="openTaskModal('...')"`` 截断 —— 而 HTML 转义救不了内联 handler：
    浏览器会先把 ``&#39;`` 解码回单引号，再交给 JS 解析。所以跟规则页一样走
    ``data-*`` + 事件委托。
    """
    html = _reg_grab_html()
    body = _js_function_body(html, "renderTaskCard")

    assert 'data-action="edit"' in body
    assert 'data-action="delete"' in body
    assert 'data-role="rg-task-enabled"' in body
    assert 'data-task-id="${attr(task.id)}"' in body
    assert "onclick=" not in body, "卡片又把 task.id 拼进内联 handler 了"
    assert "onchange=" not in body, "卡片又把 task.id 拼进内联 handler 了"

    # 「监听账号」那一排勾选框：账号名在 value 上，task.id 在 data-* 上
    owners = _js_function_body(html, "taskOwnersRow")
    assert 'data-role="rg-owner"' in owners
    assert 'data-task-id="${attr(entry.id)}"' in owners
    assert "onclick=" not in owners, "监听账号又把 task.id 拼进内联 handler 了"

    # 委托本身要接线：点按钮 / 拨开关 / 勾账号 / 改并发都得有人接
    init = _js_function_body(html, "bindListActions")
    assert "addEventListener('click'" in init
    assert "addEventListener('change'" in init
    click = _js_function_body(html, "onListClick")
    assert "dataset.taskId" in click
    change = _js_function_body(html, "onListChange")
    assert "input.dataset.role === 'rg-owner'" in change
    assert "card.dataset.taskId" in change

    # 勾上 = POST 写进那个账号；取消 = DELETE 从那个账号移除。两者不能混：
    # PUT 对"账号里没有这条任务"算 missing，勾了也不会有任何变化。
    owner = _js_function_body(html, "setTaskOwner")
    assert "postJson('/api/reg_grab/tasks'" in owner, "勾选没有走 POST"
    assert "deleteJson(" in owner, "取消勾选没有走 DELETE"
    assert "?accounts=" in owner


def test_reg_grab_tag_input_ids_match_the_dom() -> None:
    """字段名（下划线）与 DOM id（连字符）必须对得上 —— 跟规则页同一个坑。

    ``exclude_chats`` / ``text_patterns`` 直接拿字段名拼 id 会得到
    ``rg-exclude_chats-field``（不存在），``getElementById`` 返回 ``null`` 又被
    ``if (!input) return`` 静默吞掉：回车加不进标签、已有标签也不显示。
    """
    html = _reg_grab_html()

    block = re.search(r"const tagFields = \[(.*?)\];", html, re.S)
    assert block, "找不到 tagFields 定义"
    keys = re.findall(r"'(\w+)'", block.group(1))
    assert len(keys) >= 3, f"tagFields 的键没解析出来：{keys}"

    for key in keys:
        dom = key.replace("_", "-")
        assert f'id="rg-{dom}-field"' in html, (
            f"tagFields 有 {key}，但 HTML 里没有 id=\"rg-{dom}-field\" —— 回车添加会静默失效"
        )
        assert f'id="rg-{dom}-list"' in html, (
            f"tagFields 有 {key}，但 HTML 里没有 id=\"rg-{dom}-list\" —— 标签根本不显示"
        )

    assert "${tagDomId(field)}-field" in html
    assert "${tagDomId(field)}-list" in html
    assert "${field}-field" not in html, "又拿字段名直接拼 id 了（下划线 ≠ 连字符）"
    assert "${field}-list" not in html, "又拿字段名直接拼 id 了（下划线 ≠ 连字符）"

    # 中文输入法选字那一下也会派发 Enter —— 不挡住就会把没上屏的拼音当标签加进去。
    tag_input = _js_function_body(html, "initTagInput")
    assert "e.isComposing" in tag_input, "标签输入没挡输入法的回车"


def test_reg_grab_task_payload_drops_the_readonly_fields() -> None:
    """GET 多塞的 ``ready`` / ``problem`` / ``in_window`` 必须在提交前丢掉。

    模型是 ``extra="forbid"`` 的，原样 PUT 回去就是 422（"保存失败"，而且看不出原因）。
    比对"各账号里的副本是不是分叉"时也要先摘掉它们 —— 那三个是服务端**现算**的，
    不摘的话形状相同的两份也会被判成分叉。
    """
    html = _reg_grab_html()

    strip = _js_function_body(html, "stripReadonly")
    assert "{ready, problem, in_window, ...rest}" in strip

    collect = _js_function_body(html, "collectTasks")
    assert "stripReadonly(" in collect, "判断副本是否分叉时没摘只读字段"

    # 提交的字段一个都不能少（少一个 = 静默把用户的配置改回默认值）
    task = _js_function_body(html, "collectTask")
    for key in ("id:", "name:", "enabled:", "chats:", "detect:", "steps:", "window:"):
        assert key in task, f"collectTask() 没提交 {key}"

    # 账号级并发上限走账号自己的配置端点，必须**带上该账号的任务列表**：
    # 那个端点不带 tasks 就是"把任务列表清空"。
    concurrency = _js_function_body(html, "saveConcurrency")
    assert "tasks: (acc.tasks || []).map(stripReadonly)" in concurrency
    assert "/api/config/" in concurrency


def test_red_packet_page_edit_age_is_filled_in_minutes_stored_in_seconds() -> None:
    """面板按**分钟**填、配置存**秒** —— 换算写错就会静默把闸门改成 30 倍或 1/30。

    这个闸门就是用来挡住"长驻红包被反复编辑"的（线上 8 小时被点 8 次），
    阈值静默错掉等于没修，所以把两个方向的换算都钉住。
    """
    html = (_WEB_DIR / "templates" / "red_packet.html").read_text(encoding="utf-8")

    assert 'id="rp-edit-max-age"' in html
    assert "编辑事件年龄上限（分钟）" in html
    # 载入：秒 → 分钟
    assert "document.getElementById('rp-edit-max-age').value = Math.round(editMaxAge / 60)" in html
    # 提交：分钟 → 秒
    collect = _js_function_body(html, "collectTask")
    assert "edit_max_age" in collect
    assert "* 60" in collect


def test_red_packet_page_does_not_block_a_blank_task_id() -> None:
    """任务 ID 不是必填（用户原话：「不是说了ID不要必填吗」）—— 面板不许再拦。

    自动生成必须在**后端**做：前端自己编一个的话，两条同名任务会各自编出同一个 id，
    后端查重后用户只看到一句「保存失败」，根本猜不到是名字重了。
    """
    html = (_WEB_DIR / "templates" / "red_packet.html").read_text(encoding="utf-8")

    assert "任务 ID（可留空，自动生成）" in html
    assert "任务 ID 不能为空" not in html, "前端不该再拦 id 必填"
    assert '任务 ID <span class="required">*</span>' not in html, "id 不该再带必填星号"
    save = _js_function_body(html, "saveTask")
    assert "if (!task.id)" not in save, "id 留空时保存路径不能提前 return"
    assert "task.id &&" in save, "留空时不该拿空串去撞「ID 已存在」"


def test_red_packet_page_new_task_inherits_the_account_window() -> None:
    """新建任务的时段初值来自账号级默认值，**包括那个开关**。

    只继承起止时间、开关却默认关闭的话，账号默认的「08:00~23:00 才动手」对每条新
    任务都等于白设：用户新建一条就得到「全天抢包」，而半夜精准点按钮正是最像脚本的
    特征 —— 这种失效是静默的，所以钉住它。
    """
    html = (_WEB_DIR / "templates" / "red_packet.html").read_text(encoding="utf-8")

    modal = _js_function_body(html, "openTaskModal")
    assert "defaultWin" in modal, "新建任务要拿账号级 window 当初值"
    assert "defaultWin.enabled" in modal, "开关也要继承，不能硬编码成关闭"


def test_rules_page_does_not_block_a_blank_rule_id() -> None:
    """规则 ID 也不必填（用户原话：「ID 不要必填」）—— 面板不许再拦。

    生成同样在后端入口做（``POST /api/rules`` 只单独校验一条规则，不经过
    ``ForwardConfig`` 的校验器），前端自己编 id 会让同名规则互相撞车。
    """
    html = (_WEB_DIR / "templates" / "rules.html").read_text(encoding="utf-8")

    assert "规则 ID（可留空，自动生成）" in html
    assert "请输入规则 ID" not in html, "前端不该再拦 id 必填"
    assert '规则 ID <span class="required">*</span>' not in html, "id 不该再带必填星号"
    save = _js_function_body(html, "saveRule")
    assert "if (!id)" not in save, "id 留空时保存路径不能提前 return"


def test_rules_page_exposes_only_from_bots() -> None:
    """转发规则可以**可选**地只抓机器人消息（用户要求）。

    字段名必须是 ``only_from_bots``、且放在规则对象**顶层**：后端是
    ``extra="forbid"``，名字写错或塞进 ``match`` 里都会直接 400。
    编辑时要能回填（老规则没这个字段按未勾选），新建时要复位成不勾 ——
    少了复位，上一条规则勾过之后新建的规则会"继承"这个开关。
    """
    html = (_WEB_DIR / "templates" / "rules.html").read_text(encoding="utf-8")

    assert 'id="rule-only-bots"' in html
    assert "只抓取机器人消息" in html
    assert "only_from_bots" in _js_function_body(html, "saveRule"), "保存时要带进 payload"
    assert "only_from_bots" in _js_function_body(html, "editRule"), "编辑时要回填"
    assert "rule-only-bots" in _js_function_body(html, "openAddRule"), "新建时要复位"


def test_rules_page_exposes_trash_cleanup() -> None:
    """规则面板要能配「被踩 💩 达到人数就删」（用户要求）。

    三个字段同样必须在规则对象**顶层**且名字精确（后端 ``extra="forbid"``）。
    另外两点容易漏：阈值必须是**数字**（发字符串会被 pydantic 拒掉），
    老规则缺字段时回填要跟后端默认值一致 —— 否则用户打开编辑器再点保存，
    就把默认的「开启 / 💩 / 2」改成别的东西了。
    """
    html = (_WEB_DIR / "templates" / "rules.html").read_text(encoding="utf-8")

    for element_id in ("rule-trash-cleanup", "rule-trash-emoji", "rule-trash-threshold"):
        assert f'id="{element_id}"' in html, f"缺少 {element_id} 控件"

    save = _js_function_body(html, "saveRule")
    for field in ("trash_cleanup", "trash_emoji", "trash_threshold"):
        assert field in save, f"保存时要带 {field}"
    assert "parseInt" in save, "阈值必须是数字，不能把字符串发上去"

    edit = _js_function_body(html, "editRule")
    assert "trash_cleanup" in edit and "trash_threshold" in edit, "编辑时要回填"

    add = _js_function_body(html, "openAddRule")
    assert "rule-trash-cleanup" in add and "rule-trash-threshold" in add, "新建时要复位成默认值"


# --------------------------------------------------------------------------- #
# 规则页：「全局排除」区块（所有账号共用一份）
# --------------------------------------------------------------------------- #
def test_rules_page_has_a_global_exclude_block() -> None:
    """规则页顶部要有「全局排除」区块 —— 跨账号那份名单的**面板入口**。

    用户原话：「将转发规则的黑名单及排除的频道也做成全局的」＋
    「我设置的群id就不要转给我了」。后端 ``PUT /api/forward-excludes`` 做完了而面板上
    没有入口的话，功能等于不存在 —— 这个项目已经踩过一次：账号级黑名单的字段、
    引擎判定、测试全都在，**面板里连输入框都没有**，用户根本摸不到。

    区块画在 ``#rules-list`` **外面**（它不属于任何一个账号分组），
    所以它跟"有没有账号 / 有没有规则"无关。
    """
    html = _rules_html()

    for dom in (
        'id="rules-global"',
        'id="rules-global-body"',
        'id="rules-global-state"',
    ):
        assert dom in html, f"规则页缺少 {dom}"
    assert "全局排除" in html, "区块没有标题，用户不知道这是干什么的"
    assert "所有账号" in html, "没讲清这是跨账号共用的一份"
    # 用户最关心的那句「我设置的群id就不要转给我了」：排除之后**通知也不会再发**
    # （通知是转发成功之后才发的），页面上必须说出来，否则他会以为"至少还能收到提醒"。
    assert "也不会再推送给你" in html

    # 作用域标记：保存时要靠它决定走哪个端点（没有它就会去账号级端点找一个空账号）
    assert 'data-global="1"' in html
    # 区块在所有账号分组之前（也就是外面），不是某个账号分组的一部分
    assert html.index('id="rules-global"') < html.index('id="rules-list"')

    # 一个账号都没有的时候正是最该先把"这些群不要转给我"填上的时候 ⇒
    # 渲染必须放在两个提前 return **之前**。
    body = _js_function_body(html, "renderRules")
    assert "renderGlobalExcludes();" in body
    assert body.index("renderGlobalExcludes();") < body.index("if (accounts.length === 0)"), (
        "全局区块跟着「没有账号」一起不渲染了 —— 新环境里用户第一步就是填它"
    )


def test_rules_global_block_saves_through_the_global_endpoint() -> None:
    """全局那两行走 ``/api/forward-excludes``（没有账号参数），账号级走老端点。

    两种作用域的数据来源、端点、DOM 标记都不一样，混成一条路径的话，
    要么全局保存失败（账号名是空的），要么删 × 时去 accounts 里找一个叫 "" 的账号
    然后**静默什么都不做**。
    """
    html = _rules_html()

    save = _js_function_body(html, "saveExcludeList")
    assert "/api/forward-excludes" in save
    assert "isGlobal" in save
    assert "/api/config/" in save, "账号级那条老端点不能被顺手删掉"

    # 作用域从 DOM 标记读，不靠"账号存不存在"去猜（全局那份本来就没有账号）
    assert "dataset.global" in _js_function_body(html, "onRuleListClick")
    assert "dataset.global" in _js_function_body(html, "onExcludeListKeydown")

    # 两个名单、两种作用域共用同一段标签 HTML（抄成四份的话下次改样式必漏一处）
    row = _js_function_body(html, "excludeListRow")
    assert 'data-global="1"' in row
    assert "data-account=" in row
    render = _js_function_body(html, "renderGlobalExcludes")
    assert "excludeListRow(null, 'exclude-chats', true)" in render
    assert "excludeListRow(null, 'exclude-users', true)" in render

    # 区块在 #rules-list 外面 ⇒ 事件委托要单独接一次，否则回车 / 点 × 没人响应
    init = _js_function_body(html, "init")
    assert "getElementById('rules-global')" in init
    assert "addEventListener('click'" in init
    assert "addEventListener('keydown'" in init


def test_rules_global_state_warns_when_the_name_list_could_not_be_read() -> None:
    """名单文件读不到时页面必须**说出来**，而不是显示一屏正常的"未设置"。

    ``forward_excludes.json`` 写坏时引擎按**空名单**继续跑（少排除几条好过所有账号
    转不起来）。代价是"用户以为配好了、实际没生效"—— 所以这个失败必须在页面上
    可见（状态里报"已失败 N 次"），否则他会对着一个干净的界面发呆，
    然后在群里看到消息照样被转出去。
    """
    html = _rules_html()

    state = _js_function_body(html, "renderGlobalState")
    assert "globalExcludesErrors" in state, "状态没读失败次数"
    assert "空名单" in state, "提示文案没说清此刻是按什么在跑"
    assert "rules-global-state-warn" in state, "这种要用户注意的状态得用独立样式"

    load = _js_function_body(html, "loadAll")
    assert "data.global_excludes" in load, "总览响应里那一份没被读进来"
    assert "load_errors" in load, "失败次数没有传到状态上"


def test_rules_page_has_a_global_used_codes_block() -> None:
    """规则页顶部要有「全局已使用注册码拦截」区块 —— 它的**面板入口**。

    用户原话：「将……已使用注册码拦截做成全局，而不是账号级」。后端 ``PUT
    /api/forward-used-codes`` 做完了而面板上没有入口的话，功能等于不存在 ——
    这个项目已经踩过一次（账号级黑名单字段/引擎/测试都在，面板里连输入框都没有）。

    区块画在 ``#rules-list`` **外面**（它不属于任何一个账号分组），
    所以它跟"有没有账号 / 有没有规则"无关。
    """
    html = _rules_html()

    for dom in (
        'id="rules-used-codes-global"',
        'id="rules-used-codes-body"',
        'id="rules-used-codes-state"',
    ):
        assert dom in html, f"规则页缺少 {dom}"
    assert "全局已使用注册码拦截" in html, "区块没有标题，用户不知道这是干什么的"
    assert "所有账号" in html, "没讲清这是跨账号共用的一份"
    # 区块在所有账号分组之前（也就是外面），不是某个账号分组的一部分
    assert html.index('id="rules-used-codes-global"') < html.index('id="rules-list"')

    # 渲染挂在全局排除那一次里（同一个时机：跟有没有账号无关，放在提前 return 之前）
    render = _js_function_body(html, "renderGlobalExcludes")
    assert "renderGlobalUsedCodes();" in render, "全局已用码区块没被渲染出来"

    # 事件委托要单独接一次（它在 #rules-list 外面），否则点「保存」没人响应
    init = _js_function_body(html, "init")
    assert "getElementById('rules-used-codes-global')" in init


def test_rules_global_used_codes_saves_through_the_global_endpoint() -> None:
    """全局那行走 ``/api/forward-used-codes``（没有账号参数）。

    它和账号级旧入口（``/api/config/{name}/forward-used-codes``）的区别写在
    ``saveUsedCodes`` 里：全局那份 DOM 上只有 ``data-global="1"``、没有账号，
    保存时不能因为账号名为空就直接 return（那样点「保存」会静默什么都不做）。
    """
    html = _rules_html()

    save = _js_function_body(html, "saveUsedCodes")
    assert "/api/forward-used-codes" in save
    assert "dataset.global" in save, "作用域要从 DOM 标记读，不靠账号名猜"
    assert "isGlobal" in save, "没区分全局 / 账号级两条路径"
    assert "/api/config/" in save, "账号级那条老端点不能被顺手删掉"

    row = _js_function_body(html, "usedCodesRow")
    assert 'data-global="1"' in row, "全局行没有作用域标记，保存时会走错端点"
    assert "data-account=" in row

    load = _js_function_body(html, "loadAll")
    assert "data.global_used_codes" in load, "总览响应顶层那份没被读进来"

    # 状态也要能报"文件读不到"（此刻按缺省策略在跑），与全局排除名单同一套处置
    state = _js_function_body(html, "renderGlobalUsedCodesState")
    assert "globalUsedCodesErrors" in state, "状态没读失败次数"
    assert "默认策略" in state, "提示文案没说清此刻是按什么在跑"


def test_rules_page_collapses_long_match_lists() -> None:
    """匹配条件 ≥ 3 条时折叠成 ``<details>``，而不是把卡片铺满一屏。

    用户原话：「正则规则不用全部展示，点击编辑或加个倒三角打开」。
    线上那条规则配了 **20 条**正则（2026-09-29 取证），全铺出来一张卡片就是一屏，
    真正有用的信息（来源 / 目标 / 模式）全被挤到看不见的地方。

    阈值定在 3：1～2 条时不折叠 —— 为了两条条件多一次点击只会更碍事。
    """
    html = _rules_html()

    assert "PATTERN_COLLAPSE_THRESHOLD = 3" in html
    patterns = _js_function_body(html, "renderRulePatterns")
    # 折叠的判定必须真的用上阈值（< 阈值就照原样铺开）
    assert "patterns.length < PATTERN_COLLAPSE_THRESHOLD" in patterns
    assert '<details class="rule-patterns-details">' in patterns
    # 收起时给预览 + 条数：只留一个数字的话，用户判断不出这条规则到底匹配什么
    assert "rule-patterns-count" in patterns
    assert "rule-pattern-preview" in patterns

    # 卡片必须走这个函数：只定义不调用的话，长规则照样全铺出来
    card = _js_function_body(html, "renderRuleCard")
    assert "renderRulePatterns(match, patterns)" in card



# --------------------------------------------------------------------------- #
# 转发规则页：以**任务**为维度展示（同一条规则不重复出现）
# --------------------------------------------------------------------------- #
def test_rules_page_renders_one_card_per_rule_not_per_account() -> None:
    """同一条规则在 N 个账号里各存一份，页面上只能出现**一次**。

    🔴 用户原话：「展示以及任务是以任务为维度，而不是以账号，同样的规则不做二次
    展现，一条主规则，选择监听账号即可」。规则当初就是扇出写出去的（``POST
    /api/rules`` 不带 accounts 写全部账号），按账号渲染会让同一条规则重复出现 N 次：
    用户数不清自己有几条任务，也不知道改一处会不会影响别处。

    所以渲染入口必须先做一次「账号 → 规则」的**转置**，再按 rule.id 出卡片。
    """
    html = _rules_html()

    render = _js_function_body(html, "renderRules")
    assert "collectRules()" in render, "规则列表没有按 rule.id 合并"
    assert "entries.map(renderRuleCard)" in render, "主卡片不是按规则维度渲染的"
    assert "accounts.map(renderAccountGroup)" not in render, (
        "renderRules 又回到按账号渲染了 —— 同一条规则会重复出现"
    )
    assert "renderAccountSettings()" in render, "账号级设置没有单独的落点"
    # 有账号但一条规则都没有时，账号级设置必须照样渲染：老代码在这一步整天提前
    # return，排除名单连入口都没有 —— 而"先把我不要的群填上"正是新环境的第一步。
    assert "return" not in render[render.index("collectRules()"):], (
        "规则为空时又提前 return 了，账号级设置会跟着一起不渲染"
    )

    collect = _js_function_body(html, "collectRules")
    assert "byId.get(rule.id)" in collect, "没有按 rule.id 去重"
    assert "entry.owners.push(acc.name)" in collect, "没有聚出「这条规则落在哪些账号」"
    # 各账号里那份内容可能分叉（手改过某个账号的 config.json / 走过单账号接口）。
    # 分叉必须能被卡片说出来，否则显示一份、别的账号按另一份跑。
    assert "entry.divergent" in collect

    card = _js_function_body(html, "renderRuleCard")
    assert "entry.owners" in card
    assert "data-rule-id" in card

    # 账号卡片里**不许**再有规则卡片：规则已经搬到主视图了
    group = _js_function_body(html, "renderAccountGroup")
    assert "renderRuleCard" not in group, "账号卡片里还在渲染规则 —— 同一条规则会重复出现"


def test_rules_page_card_can_move_a_rule_between_accounts() -> None:
    """卡片上的「监听账号」勾选 = 把这条规则写进 / 移出某个账号，其余账号不受影响。

    两个端点绝不能弄混：
      * 勾上要用 ``POST /api/rules``（只写缺这条规则的账号）。用 PUT 的话，账号里
        没有这条规则时它只算 ``missing`` —— **不写也不报错**，界面看起来勾上了、
        其实什么都没发生。
      * 取消要用 ``DELETE /api/rules/{id}?accounts=<name>``。不带 accounts 的
        DELETE 是"从所有账号删掉"（那是删除键的语义），混用会把其它账号一起干掉。
    """
    html = _rules_html()
    set_owner = _js_function_body(html, "setRuleOwner")

    assert "'/api/rules'" in set_owner and "method: 'POST'" in set_owner, (
        "勾上监听账号没有走 POST /api/rules（用 PUT 时该账号没有这条规则就会静默不写）"
    )
    assert "?accounts=" in set_owner and "method: 'DELETE'" in set_owner, (
        "取消监听账号没有走 DELETE ?accounts=（不带 accounts 会把其它账号一起删了）"
    )
    # 取消最后一个监听账号 = 这条规则没有任何账号在跑（等于删除），必须先问
    assert "confirm(" in set_owner and "最后一个监听账号" in set_owner
    # 成功失败都要重拉：界面不能停在"看起来成功了"的状态
    assert set_owner.count("await loadAll()") >= 2

    # 账号名从 checkbox 的 value 上取 —— 主卡片不在任何 [data-account] 里面，
    # 靠 closest('[data-account]') 取账号会拿到 null（那样"勾了没反应"）。
    change = _js_function_body(html, "onRuleListChange")
    assert "rule-owner" in change and "input.value" in change

    row = _js_function_body(html, "ruleOwnersRow")
    assert 'data-role="rule-owner"' in row
    assert 'value="${escapeHtml(acc.name)}"' in row
    assert "监听账号" in row
    assert "checked" in row, "卡片刻不出哪些账号在监听"


def test_rules_page_account_scoped_settings_stay_out_of_the_rule_cards() -> None:
    """账号级的东西（转发总开关 / 排除名单）单独一区，别混进规则卡片。

    规则改成按 rule.id 合并之后，这些设置不再属于任何一条规则。混在卡片里的话，
    用户又会以为"这个排除名单只对当前这条规则生效"—— 而"账号级"正是它最容易被
    误解的地方（弹窗里那份才是规则级的）。

    ⚠️ 「已使用注册码拦截」**不**在这一节里了：它已经全局化（见
    ``test_rules_page_has_a_global_used_codes_block``），所以这里**断言它不在**
    账号卡片里 —— 否则用户会看到"每个账号一套已用码设置"这种已经不成立的界面。
    """
    html = _rules_html()

    settings = _js_function_body(html, "renderAccountSettings")
    assert "rules-account-settings" in settings
    assert "accounts.map(renderAccountGroup)" in settings
    assert "各账号单独设置" in settings, "没有标题，用户不知道这一节是干什么的"
    # 每次 loadAll 都会重建 innerHTML：展开状态必须带过去，
    # 否则用户改一次名单（保存后会重拉）这一节就自己合上了。
    assert "document.querySelector('.rules-account-settings')" in settings
    assert "open" in settings

    group = _js_function_body(html, "renderAccountGroup")
    for needle in ("forward-enabled", "excludeChatsRow(acc)"):
        assert needle in group, f"账号卡片里少了 {needle}"
    assert "usedCodesRow(acc)" not in group, (
        "已用码拦截已全局化，不该再出现在账号卡片里（会让人以为每号一套）"
    )

    # 这一节是 #rules-list 的子元素 ⇒ 已有的委托（click / change / keydown）照旧覆盖它
    init = _js_function_body(html, "init")
    assert "getElementById('rules-list')" in init
    assert "addEventListener('change', onRuleListChange)" in init


def test_rules_page_says_a_person_name_can_never_match() -> None:
    """人名 / 中文昵称不能在输入框里被**静默接受** —— 它永远匹配不到。

    🔴 用户原话：「转发规则编辑内想加人加不上」。必然发生的那条根因：
    ``config.parse_chat_ref()`` 对"非数字、非 @"的输入返回 ``text.lower()``，当成
    username 存进 ``RefSet``；而引擎只比 sender_id 与 username，Telegram 的
    @username 又只允许 ASCII ⇒ 中文名**标签显示、保存成功、规则永远不生效**
    （2026-09-29 jsdom 实测：保存请求体里原样带着 "张三"，toast 还报"已保存"）。

    静默接受是最坏的选择：用户以为配好了，然后在群里等一条永远不来的转发。
    所以这里钉住三层：判断逻辑、被拒时不吃掉输入、以及用户看得见的提示语。
    """
    html = _rules_html()

    reason = _js_function_body(html, "refRejectReason")
    assert "NUMERIC_ID_RULE" in reason and "USERNAME_RULE" in reason
    assert "TME_LINK_RULE" in reason, "频道还要接受 t.me 链接"
    assert "人名 / 昵称" in reason and "@username" in reason, "拒绝原因是中文的吗？"

    # 适用范围要卡准：引用类字段才校验
    fields = re.search(r"const REF_FIELDS = \{(.*?)\};", html, re.S)
    assert fields, "找不到 REF_FIELDS"
    for key in ("sources", "exclude_sources", "targets", "from_users", "exclude_users"):
        assert f"{key}:" in fields.group(1), f"{key} 不在校验范围里"
    for key in ("patterns", "exclude_patterns"):
        assert f"{key}:" not in fields.group(1), (
            f"{key} 是**正则**不是引用 —— (?:Whitelist)_([A-Za-z0-9]{{10}}) / 张三 都可能"
            "是合法正则，拿这套规则去拦会误杀"
        )

    # 被拒时不清空输入框：用户得能在原值上改一个字符，而不是重打一遍
    assert "if (addTag(field, this.value.trim())) this.value = '';" in html, (
        "校验失败时把输入框清空了 —— 用户刚打的内容会凭空消失"
    )
    add_tag = _js_function_body(html, "addTag")
    assert "return false;" in add_tag and "toast(" in add_tag

    # 账号级排除名单复用同一个 helper，不写第二套判断
    excl = _js_function_body(html, "onExcludeListKeydown")
    assert "refRejectReason(EXCLUDE_LISTS[role].key" in excl
    assert "return;   // 不清空输入框" in excl or "不清空输入框" in excl

    # 提示要写在用户看得见的地方（弹窗里两个名单下面各一行）
    assert "只能填数字 ID" in html and "匹配不到" in html


def test_rules_page_warns_about_unmatchable_values_already_saved() -> None:
    """已经被静默存进去的人名，打开编辑器时要被**指出来**（但只提示、不拦保存）。

    光"以后不再收坏人名"还不够：修复前被坑过的用户，配置里已经躺着「张三」了，
    他打开编辑器看到的还是一堆正常的标签，永远不知道那些人从来没生效过。
    所以编辑一条老规则时，把匹配不到的值点名报出来。

    ⚠️ 只提示不拦：拦了就是"我的老规则打不开 / 存不了"，那是比原 bug 更糟的结果。
    """
    html = _rules_html()

    assert 'id="rule-refs-warn"' in html, "缺少提示条的位置"
    warn = _js_function_body(html, "warnAboutUnmatchableRefs")
    assert "refRejectReason(field, value)" in warn, "没复用同一套判断（会跟输入时的口径不一致）"
    assert "form-hint-warn" in html
    # 只提示：不能 return false / 不能 toast 之后拦掉保存
    assert "return false" not in warn

    edit = _js_function_body(html, "editRule")
    assert "warnAboutUnmatchableRefs(rule)" in edit, "编辑时没有检查已存的值"
    # 新建时必须清掉，否则会把上一条规则的告警带到新表单上
    assert "warnAboutUnmatchableRefs({})" in _js_function_body(html, "openAddRule")


def test_rules_page_ignores_the_enter_that_commits_an_ime_candidate() -> None:
    """中文输入法**选字/上屏**那一下的回车不是"提交"，退格也不是"删除"。

    用户是中国用户：打「张三」时按回车先是在选字，浏览器照样派发 keydown(Enter)，
    只多带一个 ``isComposing=true``（老浏览器是 keyCode 229）。修复前这一下会把还
    没上屏的拼音当成标签加进去**并清空输入框** —— 字没了、进去一个半截值，主观
    感受就是"想加人加不上"（2026-09-29 jsdom 实测：tagInputs 里出现 "zhangsan"、
    输入框被清空）。

    守卫必须放在所有分支**之前**：上屏那一下的退格同样不该顺手删掉一个已有标签。
    """
    html = _rules_html()

    cases = (("initTagInputs", "e"), ("onExcludeListKeydown", "event"))
    for name, var in cases:
        body = _js_function_body(html, name)
        assert f"{var}.isComposing || {var}.keyCode === 229" in body, (
            f"{name} 缺少输入法守卫：选字那一下的回车会被当成提交"
        )
        assert body.index("isComposing") < body.index("'Enter'"), (
            f"{name} 里的守卫放在了回车分支之后 —— 等于没防"
        )


def test_rules_page_edit_modal_can_change_listening_accounts() -> None:
    """编辑弹窗里的账号勾选必须**可点**，而且保存时要按「差集」真落地。

    🔴 2026-09-29 用户反馈：「转发规则中新勾选账号无法勾选」。根因是编辑流程里传了
    ``renderAccountPicker(editingOwners, true)``，``locked=true`` 把弹窗里的账号
    框全设成 ``disabled`` —— 物理上点不动，点了也不产生任何请求（面板日志里确实
    没有任何用户发起的 ``POST /api/rules``）。

    只解锁 UI 同样不行：保存时若不落地差集，界面会显示"勾上了"而磁盘上根本没写
    进去 —— 那比锁着更糟。所以这里两类断言一起钉：可点 + 差集落地 + 失败回读。
    """
    html = _rules_html()

    # 1) 不能再锁
    assert "renderAccountPicker(editingOwners, true)" not in html, "编辑弹窗的账号框又被锁上了"
    assert "disabled" not in _js_function_body(html, "renderAccountPicker"), (
        "账号勾选框里又出现了 disabled —— 用户会点不动"
    )
    # 卡片底部那行勾选保持可用（别为了修这个把它一起改成只读）
    assert 'data-role="rule-owner"' in _js_function_body(html, "ruleOwnersRow")
    assert "setRuleOwner" in html

    # 2) 保存时落地差集：新增 POST / 取消 DELETE ?accounts= / 取消最后一个先 confirm
    save = _js_function_body(html, "saveEditedRule")
    assert "selectedRuleAccounts()" in save
    assert "adds" in save and "removes" in save, "没有算差集"
    assert "postJson('/api/rules', {rule: rule, accounts: adds})" in save, (
        "新增监听账号没有走 POST /api/rules（用 PUT 的话账号里没有这条规则时不会写）"
    )
    assert "?accounts=" in save and "deleteJson(" in save, (
        "取消监听账号没有走 DELETE ?accounts=（不带 accounts 会从所有账号删掉）"
    )
    assert "confirm(" in save, "取消最后一个监听账号要先问"
    assert "await loadAll()" in save, "保存后必须回读磁盘真实状态"
    # 失败也要回读：否则弹窗还停在"我勾上了"的样子，用户会以为已经生效
    assert "failEditSave" in save
    resync = _js_function_body(html, "reloadAndResyncEdit")
    assert "renderAccountPicker(" in resync, "保存失败后弹窗里的勾选没有回读真实状态"
    assert "findRule(ruleId)" in resync

    # 3) 「将写入：…」要说差集，不能再说"全部账号"（编辑时那个框不是这个意思）
    summary = _js_function_body(html, "updateAccountSummary")
    assert "新增监听" in summary and "取消监听" in summary
    assert "editingOwners" in summary
    # 编辑分支里不能再出现"全部账号"的说法（那个框是既有监听账号的勾选状态）
    edit_branch = summary[summary.index("if (editingRuleId)"):summary.index("return;")]
    assert "全部账号" not in edit_branch, "编辑分支还在说「全部账号」—— 与实际做的事对不上"


def test_rules_page_tester_counts_exclude_patterns() -> None:
    """「测试」按钮必须把排除规则一起算 —— 这是"排除没用"误判的来源。

    🔴 用户反馈「为什么21点一直命中，我已经加了排除」。根因是 ``testRegex()`` 只发
    ``tagInputs.patterns.join('|')``：``exclude_patterns`` 一个字都没发给服务端，
    所以加了排除规则之后测试结果照样显示「匹配成功」。

    判定本身（谁排除了谁）由远端的 pytest 钉住；这里钉页面这一半：请求要带两个列表、
    结果要分「被拦下 / 匹配成功 / 不匹配」三种，并说明这个测试只覆盖文本级。
    """
    html = _rules_html()
    body = _js_function_body(html, "testRegex")

    assert "patterns: tagInputs.patterns" in body, "主正则没有发列表"
    assert "exclude_patterns: tagInputs.exclude_patterns" in body, (
        "排除规则没发给服务端 —— 用户会再次误判成「排除没用」"
    )
    assert "join('|')" not in body, "又在页面里自己拼主正则（捕获组编号会和引擎对不上）"

    # 三种结果都要处理，且"被拦下"不能被渲染成"匹配成功"
    assert "data.excluded" in body, "没有处理「被排除规则拦下」的返回"
    assert "excluded_by" in body and "拦下它的是" in body, "没把肇事的那条排除规则打出来"
    assert body.index("data.excluded") < body.index("data.match"), (
        "要先判 excluded 再判 match（excluded 时 match 也是 false，顺序反了就只剩「不匹配」）"
    )
    # 只覆盖文本级的说明（来源/发送者/注册码/去重都要点名）
    for word in ("只看文本级判定", "已使用注册码拦截", "去重与频率限制", "真实会话上下文"):
        assert word in body, f"测试结果区没有说明「{word}」不参与判定"
