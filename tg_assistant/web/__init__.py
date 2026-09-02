"""TG-Assistant Web 界面。

提供与 CLI 完全对应的功能，并通过 WebSocket 实现实时日志、扫码登录、状态推送。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from .routers import api, views, ws
from .runtime import RuntimeManager
from .settings import WebSettings


def create_app(
    data_dir: Path,
    api_id: int | None = None,
    api_hash: str | None = None,
    proxy_url: str | None = None,
    log_level: str = "INFO",
) -> FastAPI:
    """创建 FastAPI 应用。

    把数据目录、全局设置等注入到 app.state，供各 router 通过 Depends 获取。
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
    app.state.runtime = RuntimeManager(app.state)

    # 静态文件与模板
    web_dir = Path(__file__).parent
    app.state.template_env = _build_template_env(web_dir / "templates")

    # 注册路由
    app.include_router(views)
    app.include_router(api, prefix="/api", tags=["api"])
    app.include_router(ws, prefix="/ws", tags=["websocket"])

    # 静态文件
    from fastapi.staticfiles import StaticFiles

    static_dir = web_dir / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.on_event("startup")
    async def _startup() -> None:
        await app.state.runtime.startup()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await app.state.runtime.shutdown()

    return app


def _build_template_env(template_dir: Path):
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "xml"]),
        enable_async=True,
    )
    env.filters["boolicon"] = lambda v: "✅" if v else "❌"
    return env


__all__ = ["create_app"]
