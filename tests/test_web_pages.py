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
