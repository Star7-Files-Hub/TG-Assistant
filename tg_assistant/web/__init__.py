"""TG-Assistant Web 界面。

提供与 CLI 完全对应的功能，并通过 WebSocket 实现实时日志、扫码登录、状态推送。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from . import auth
from .routers import api, views, ws
from .runtime import RuntimeManager
from .settings import WebSettings


def create_app(
    data_dir: Path,
    api_id: int | None = None,
    api_hash: str | None = None,
    proxy_url: str | None = None,
    log_level: str = "INFO",
    *,
    runner: object | None = None,
    initial_accounts: list[str] | None = None,
    run_options: dict | None = None,
) -> FastAPI:
    """创建 FastAPI 应用。

    把数据目录、全局设置等注入到 app.state，供各 router 通过 Depends 获取。

    ``runner`` / ``initial_accounts`` / ``run_options``
        供 ``tg-assistant run --web`` 使用：CLI 已经建好了 MultiRunner，
        这里把它交给 :class:`RuntimeManager` **接管**（而不是让面板另建一个，
        那样会有两套对象抢同一个 session 文件）。``tg-assistant web``
        不传，面板自己建。
    """
    from tg_assistant.config import Settings
    from tg_assistant.logging_setup import configure_logging
    from tg_assistant.paths import Paths
    from tg_assistant.store import Store

    settings = Settings.from_env(
        data_dir=str(data_dir),
        api_id=api_id,
        api_hash=api_hash,
        proxy_url=proxy_url,
        log_level=log_level,
    )
    paths = Paths.from_env(settings.data_dir)
    store = Store(paths).bootstrap()
    configure_logging(paths.log_dir, settings.log_level, accounts=paths.iter_account_names())

    app = FastAPI(
        title="TG-Assistant",
        version="0.1.0",
        description="多账号 Telegram 助手 Web 控制台",
    )

    # 注入全局依赖
    app.state.settings = settings
    app.state.paths = paths
    app.state.store = store
    app.state.web_settings = WebSettings()
    app.state.runtime = RuntimeManager(
        app.state,
        runner=runner,
        initial_accounts=initial_accounts,
        run_options=run_options,
    )

    # 静态文件与模板
    web_dir = Path(__file__).parent
    app.state.template_env = _build_template_env(web_dir / "templates")

    # 注册路由
    app.include_router(auth.router)
    app.include_router(views)
    app.include_router(api, prefix="/api", tags=["api"])
    app.include_router(ws, prefix="/ws", tags=["websocket"])

    # ---- 访问鉴权 ----
    # 未配置 secret_key 时整体放行（此时应只监听回环地址）；
    # 配置后：HTML 请求跳转到 /auth，API 请求返回 401，WebSocket 在各自路由里校验。
    @app.middleware("http")
    async def _web_auth_guard(request: Request, call_next):
        web_settings = app.state.web_settings
        if (
            not auth.auth_required(web_settings)
            or auth.is_public_path(request.url.path)
            or auth.is_authorized(request, web_settings)
        ):
            return await call_next(request)

        if request.url.path.startswith("/api/"):
            return JSONResponse(
                {"detail": "未授权：请先访问 /auth 输入访问密钥"}, status_code=401
            )
        return RedirectResponse(url="/auth", status_code=303)

    # ---- 非法账号名 ----
    # store.get_account() / paths.account() 会抛 InvalidAccountName（ValueError 子类），
    # 不拦的话 FastAPI 一律返回 500 —— 但这其实是客户端传错了名字。
    # 在这里统一翻译，所有走账号名的入口（api.py / views.py）一次覆盖：
    # API 请求给 400，页面请求回账号列表。
    from tg_assistant.paths import InvalidAccountName

    @app.exception_handler(InvalidAccountName)
    async def _invalid_account_name(request: Request, exc: Exception):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return RedirectResponse(url="/accounts", status_code=303)

    # 静态文件
    from fastapi.staticfiles import StaticFiles

    static_dir = web_dir / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # 二维码图片目录（扫码登录页显示 /qr/<name>.png）。
    # ⚠️ 刻意**不**放进 is_public_path 白名单：一张二维码就等于一份登录凭据，
    # 谁能拿到它谁就能扫出一个已登录的会话。页面本身在鉴权后面，
    # 所以浏览器带 Cookie 取图完全没问题。
    qr_dir = paths.qr_dir
    qr_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/qr", StaticFiles(directory=str(qr_dir)), name="qr")

    @app.on_event("startup")
    async def _startup() -> None:
        await app.state.runtime.startup()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await app.state.runtime.shutdown()

    return app


def _static_version(static_dir: Path) -> str:
    """静态资源版本号：取目录里最新的 mtime。

    ``/static`` 由 StaticFiles 直接发盘上的文件，没有 Cache-Control，
    浏览器会按 Last-Modified 做「启发式缓存」—— 改完 CSS 后用户仍可能
    看到旧样式，然后以为没修好。把版本号拼进 URL 就彻底绕开这个坑。

    调用方（:func:`_build_template_env`）把它包成 lambda **每次渲染现算**，
    所以「改了文件但没重启」也能立刻生效 —— 这一点很要紧，见那里的注释。
    """
    try:
        newest = max(p.stat().st_mtime for p in static_dir.rglob("*") if p.is_file())
    except (OSError, ValueError):  # pragma: no cover - 目录缺失/为空时不该拖垮启动
        return "0"
    return str(int(newest))


def _build_template_env(template_dir: Path):
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "xml"]),
        enable_async=True,
    )
    env.filters["boolicon"] = lambda v: "✅" if v else "❌"
    # 惰性求值：每次渲染现算，而不是启动时算一次存进 globals。
    #
    # 🔴 这里踩过一次：启动时缓存的话，只要部署顺序是「先重启、后落盘静态文件」
    # （同步脚本很常见），页面就会继续带着**旧**的 `?v=`，浏览器照旧吃缓存里的
    # 旧 CSS —— 用户看到的还是没修的样子，而服务端一切正常、日志里毫无报错。
    # 实测踩中时的现象：页面 `style.css?v=1790345286`，而文件 mtime 是 `1790347538`。
    #
    # 静态目录只有 2 个文件，每次渲染 rglob 一遍的开销可以忽略。
    env.globals["static_version"] = lambda: _static_version(template_dir.parent / "static")
    return env


__all__ = ["create_app"]
