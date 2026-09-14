"""Web 控制台访问鉴权。

密钥来源（按优先级）：

1. CLI ``tg-assistant web --secret <KEY>``
2. 环境变量 ``TGA_WEB_SECRET``

密钥为空时**不校验**（此时务必只监听回环地址，见 ``WebSettings.host``）。

通过校验后写入 HttpOnly Cookie。Cookie 里存的不是密钥本身，而是
``<签发时间戳>.<HMAC-SHA256 摘要>`` —— 时间戳参与签名，所以：

* Cookie 泄露也无法反推出密钥；
* 篡改时间戳会让摘要失配，无法把 Cookie 续命；
* Cookie 自带有效期（``COOKIE_MAX_AGE``），不会永久有效。

同时支持 ``Authorization: Bearer <KEY>`` 供脚本/命令行调用。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

#: 鉴权 Cookie 名。
COOKIE_NAME = "tga_web_auth"
#: Cookie 有效期（秒），默认 7 天。
COOKIE_MAX_AGE = 7 * 24 * 3600
_COOKIE_SALT = b"tg-assistant/web-console/v1"
#: 允许的时钟偏移（秒）：Cookie 时间戳略晚于本机时钟时不算过期。
_CLOCK_SKEW = 60

#: 免鉴权路径：登录页自身、登出、图标。
#: 登出必须放行 —— 否则 Cookie 已失效时用户点「退出」会被中间件拦回登录页，
#: 看起来就像按钮坏了。
_PUBLIC_EXACT = frozenset({"/auth", "/auth/logout", "/favicon.ico"})
#: 免鉴权前缀：静态资源（否则登录页连样式都加载不出来）。
_PUBLIC_PREFIXES = ("/static/",)

router = APIRouter(tags=["auth"])


# --------------------------------------------------------------------------- #
# 校验逻辑
# --------------------------------------------------------------------------- #
def _digest(secret: str, issued_at: int) -> str:
    """对 ``<密钥, 签发时间>`` 做 HMAC-SHA256。"""
    message = _COOKIE_SALT + b"|" + str(int(issued_at)).encode("ascii")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def cookie_value(secret: str, issued_at: Optional[int] = None) -> str:
    """由密钥派生出带签发时间的 Cookie 值（不可逆）。

    形如 ``<时间戳>.<摘要>``。时间戳参与签名，因此它既不能在不被发现的情况下被改写，
    又能让 Cookie 具备有效期 —— 泄露的 Cookie 不会永久可用。
    """
    ts = int(time.time() if issued_at is None else issued_at)
    return f"{ts}.{_digest(secret, ts)}"


def cookie_is_valid(value: Optional[str], secret: str, max_age: int = COOKIE_MAX_AGE) -> bool:
    """该 Cookie 值是否由 ``secret`` 签发，且未超过 ``max_age``。"""
    if not value or not secret:
        return False
    raw_ts, _, digest = value.partition(".")
    if not raw_ts or not digest:
        return False
    try:
        issued_at = int(raw_ts)
    except ValueError:
        return False
    if not hmac.compare_digest(digest, _digest(secret, issued_at)):
        return False
    age = time.time() - issued_at
    return -_CLOCK_SKEW <= age <= max_age


def secret_of(web_settings: Any) -> str:
    """取出当前配置的密钥；未配置返回空串。"""
    return str(getattr(web_settings, "secret_key", "") or "")


def auth_required(web_settings: Any) -> bool:
    """是否启用了鉴权。"""
    return bool(secret_of(web_settings))


def is_public_path(path: str) -> bool:
    """该路径是否无需鉴权。"""
    if path in _PUBLIC_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES)


def _token_from(conn: Any) -> Optional[str]:
    """从 Cookie 或 ``Authorization`` 头取令牌。"""
    try:
        token = conn.cookies.get(COOKIE_NAME)
    except Exception:  # pragma: no cover - 某些 WebSocket 实现无 cookies
        token = None
    if token:
        return token

    headers = getattr(conn, "headers", None) or {}
    try:
        header = headers.get("authorization") or ""
    except Exception:  # pragma: no cover
        return None
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


def is_authorized(conn: Any, web_settings: Any) -> bool:
    """请求或 WebSocket 是否已通过鉴权。未设密钥时恒为 True。

    接受两种凭据：

    - 浏览器 Cookie 里由密钥签发的、带有效期的摘要（正常页面访问）；
    - 明文密钥本身，即 ``Authorization: Bearer <secret>``（脚本/命令行调用）。

    明文密钥不做有效期检查 —— 它本来就是长期凭据，持有者随时可以重新登录。
    """
    secret = secret_of(web_settings)
    if not secret:
        return True
    token = _token_from(conn)
    if not token:
        return False
    if cookie_is_valid(token, secret):
        return True
    return hmac.compare_digest(token, secret)


def _safe_next(value: str) -> str:
    """只允许站内跳转，挡掉开放重定向。"""
    candidate = (value or "/").strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    return candidate


def _esc(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# --------------------------------------------------------------------------- #
# 登录页
# --------------------------------------------------------------------------- #
_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>访问验证 · TG-Assistant</title>
<link rel="stylesheet" href="/static/css/style.css">
</head>
<body>
<div class="container" style="max-width:440px;margin:12vh auto;">
  <div class="card">
    <h1 style="margin-top:0;">🔒 访问验证</h1>
    <p class="muted">请输入启动 Web 控制台时用 <code>--secret</code> 设置的访问密钥。</p>
    {error}
    <form method="post" action="/auth">
      <input type="hidden" name="next" value="{next_url}">
      <div class="form-row">
        <label for="secret">访问密钥</label>
        <input id="secret" type="password" name="secret" autofocus
               autocomplete="current-password" required>
      </div>
      <button class="btn btn-primary" type="submit">进入控制台</button>
    </form>
    <p class="muted" style="margin-bottom:0;">
      忘了密钥？重启时用 <code>--secret</code> 重新指定，或设置环境变量
      <code>TGA_WEB_SECRET</code>。
    </p>
  </div>
</div>
</body>
</html>
"""


@router.get("/auth", response_class=HTMLResponse)
async def auth_page(request: Request, next: str = "/", error: str = "") -> Response:
    """展示密钥输入页；已通过校验则直接跳回目标页。"""
    web_settings = request.app.state.web_settings
    if not auth_required(web_settings):
        return RedirectResponse(url=_safe_next(next), status_code=303)
    if is_authorized(request, web_settings):
        return RedirectResponse(url=_safe_next(next), status_code=303)

    banner = '<p class="badge badge-red">密钥不正确，请重试</p>' if error else ""
    return HTMLResponse(
        _PAGE.format(error=banner, next_url=_esc(_safe_next(next))),
        status_code=200,
    )


@router.post("/auth")
async def auth_submit(
    request: Request,
    secret: str = Form(""),
    next: str = Form("/"),
) -> Response:
    """校验密钥；通过则下发 Cookie 并跳转。"""
    web_settings = request.app.state.web_settings
    expected = secret_of(web_settings)
    target = _safe_next(next)

    # 未配置密钥时直接放行（等价于关闭鉴权）
    if not expected or hmac.compare_digest(secret.strip(), expected):
        response = RedirectResponse(url=target, status_code=303)
        if expected:
            response.set_cookie(
                COOKIE_NAME,
                cookie_value(expected),
                max_age=COOKIE_MAX_AGE,
                httponly=True,
                samesite="lax",
                path="/",
            )
        return response

    return RedirectResponse(
        url=f"/auth?next={quote(target)}&error=1", status_code=303
    )


def _logout_response() -> Response:
    """清掉 Cookie 并回到登录页。"""
    response = RedirectResponse(url="/auth", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@router.get("/auth/logout")
async def auth_logout() -> Response:
    """清除 Cookie 并回到登录页。"""
    return _logout_response()


@router.post("/auth/logout")
async def auth_logout_post() -> Response:
    """同 :func:`auth_logout`，供 ``<form method="post">`` 调用。

    侧边栏用表单而不是链接：GET 登出可以被任意一个 ``<img src="/auth/logout">``
    触发，等于任何人都能把你踢下线。POST 至少挡掉了这种「零成本」的触发方式。
    """
    return _logout_response()


__all__ = [
    "COOKIE_MAX_AGE",
    "COOKIE_NAME",
    "auth_required",
    "cookie_is_valid",
    "cookie_value",
    "is_authorized",
    "is_public_path",
    "router",
    "secret_of",
]
