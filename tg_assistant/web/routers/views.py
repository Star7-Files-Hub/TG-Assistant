"""页面路由（返回 HTML）。"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..deps import get_state

router = APIRouter()


async def _render(request: Request, template_name: str, **context: Any) -> HTMLResponse:
    """渲染模板并返回 HTML。"""
    env = request.app.state.template_env
    template = env.get_template(template_name)
    html = await template.render_async(
        request=request,
        page=template_name.replace(".html", ""),
        **context,
    )
    return HTMLResponse(html)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return await _render(request, "dashboard.html")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    state = get_state(request)
    return await _render(request, "login.html", settings=state.web_settings)


@router.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request) -> HTMLResponse:
    return await _render(request, "accounts.html")


@router.get("/config/{name}", response_class=HTMLResponse)
async def config_page(request: Request, name: str) -> HTMLResponse:
    state = get_state(request)
    account = state.runtime.get_account(name)
    if account is None:
        return RedirectResponse(url="/accounts", status_code=303)
    return await _render(request, "config.html", account=account)


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request) -> HTMLResponse:
    return await _render(request, "logs.html")


@router.get("/proxy", response_class=HTMLResponse)
async def proxy_page(request: Request) -> HTMLResponse:
    return await _render(request, "proxy.html")


@router.get("/chats/{name}", response_class=HTMLResponse)
async def chats_page(request: Request, name: str) -> HTMLResponse:
    state = get_state(request)
    account = state.runtime.get_account(name)
    if account is None:
        return RedirectResponse(url="/accounts", status_code=303)
    return await _render(request, "chats.html", account=account)


from typing import Any  # noqa: E402 - 放在文件末尾避免循环导入

__all__ = ["router"]
