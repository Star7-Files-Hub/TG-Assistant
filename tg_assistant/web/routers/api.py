"""REST API 路由。

所有非实时操作（账号管理、配置 CRUD、代理检查、通知测试等）都走这里。
实时功能（日志流、扫码登录）走 WebSocket。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field

from tg_assistant.config import ProxyConfig
from tg_assistant.proxy import probe_proxy, summarize

from ..deps import get_paths, get_runtime, get_settings, get_store

router = APIRouter()


# --------------------------------------------------------------------------- #
# 状态
# --------------------------------------------------------------------------- #
@router.get("/status")
async def api_status(runtime=Depends(get_runtime)) -> dict[str, Any]:
    return {
        "running": runtime.is_running,
        "accounts": runtime.account_status(),
    }


# --------------------------------------------------------------------------- #
# 账号
# --------------------------------------------------------------------------- #
@router.get("/accounts")
async def api_accounts(runtime=Depends(get_runtime)) -> dict[str, Any]:
    return {"accounts": runtime.account_status()}


@router.get("/accounts/{name}")
async def api_account_detail(name: str, runtime=Depends(get_runtime)) -> dict[str, Any]:
    account = runtime.get_account(name)
    if account is None:
        raise HTTPException(status_code=404, detail=f"账号 {name} 不存在")
    return account


@router.delete("/accounts/{name}")
async def api_account_delete(
    name: str,
    purge: bool = Query(False, description="同时删除数据"),
    store=Depends(get_store),
    runtime=Depends(get_runtime),
) -> dict[str, Any]:
    record = store.get_account(name)
    if record is None:
        raise HTTPException(status_code=404, detail=f"账号 {name} 不存在")
    if record.name in runtime._running_accounts():
        raise HTTPException(status_code=409, detail="账号正在运行中，请先停止")
    store.delete_account(name, remove_data=purge)
    return {"ok": True, "name": name, "purged": purge}


@router.post("/accounts/{name}/enable")
async def api_account_enable(name: str, store=Depends(get_store)) -> dict[str, Any]:
    return _set_enabled(store, name, True)


@router.post("/accounts/{name}/disable")
async def api_account_disable(name: str, store=Depends(get_store)) -> dict[str, Any]:
    return _set_enabled(store, name, False)


def _set_enabled(store: Any, name: str, enabled: bool) -> dict[str, Any]:
    record = store.get_account(name)
    if record is None:
        raise HTTPException(status_code=404, detail=f"账号 {name} 不存在")
    store.upsert_account(record.model_copy(update={"enabled": enabled}))
    return {"ok": True, "name": name, "enabled": enabled}


@router.put("/accounts/{name}/proxy")
async def api_account_set_proxy(
    name: str,
    payload: dict[str, Any],
    store=Depends(get_store),
) -> dict[str, Any]:
    record = store.get_account(name)
    if record is None:
        raise HTTPException(status_code=404, detail=f"账号 {name} 不存在")
    proxy_url = payload.get("proxy")
    proxy = ProxyConfig.from_url(proxy_url) if proxy_url else None
    store.upsert_account(record.model_copy(update={"proxy": proxy}))
    return {"ok": True, "name": name, "proxy": proxy.to_url() if proxy else None}


@router.post("/accounts/login")
async def api_account_login(
    account: str = Form(...),
    proxy: str = Form(""),
    force: bool = Form(False),
    settings=Depends(get_settings),
    store=Depends(get_store),
    runtime=Depends(get_runtime),
) -> dict[str, Any]:
    """登录入口：返回 WebSocket URL，前端通过 WS 接收二维码与进度。"""
    from tg_assistant.paths import validate_account_name

    try:
        name = validate_account_name(account)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 实际登录在 WebSocket 里完成；这里只做参数校验并返回 ws 地址
    return {
        "ok": True,
        "account": name,
        "ws_url": f"/ws/login/{name}?proxy={proxy or ''}&force={'1' if force else '0'}",
    }


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
@router.get("/config/{name}")
async def api_config_get(name: str, runtime=Depends(get_runtime)) -> dict[str, Any]:
    account = runtime.get_account(name)
    if account is None:
        raise HTTPException(status_code=404, detail=f"账号 {name} 不存在")
    return account["config"]


@router.put("/config/{name}")
async def api_config_put(
    name: str,
    payload: dict[str, Any],
    store=Depends(get_store),
    runtime=Depends(get_runtime),
) -> dict[str, Any]:
    from tg_assistant.config import AccountConfig

    record = store.get_account(name)
    if record is None:
        raise HTTPException(status_code=404, detail=f"账号 {name} 不存在")
    try:
        config = AccountConfig.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"配置校验失败：{exc}") from exc
    store.save_account_config(name, config)
    return {"ok": True}


@router.post("/config/{name}/validate")
async def api_config_validate(
    name: str,
    payload: dict[str, Any],
    store=Depends(get_store),
) -> dict[str, Any]:
    from tg_assistant.config import AccountConfig

    record = store.get_account(name)
    if record is None:
        raise HTTPException(status_code=404, detail=f"账号 {name} 不存在")
    try:
        config = AccountConfig.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"配置校验失败：{exc}") from exc
    watched = config.watched_chats()
    source_count = sum(len(rule.sources) for rule in config.forward.active_rules)
    return {
        "ok": True,
        "watched_chats": len(watched) if watched else 0,
        "source_count": source_count,
        "needs_updates": config.needs_updates,
    }


@router.post("/config/{name}/init")
async def api_config_init(
    name: str,
    example: bool = Query(True),
    store=Depends(get_store),
) -> dict[str, Any]:
    """生成示例配置（覆盖）。"""
    record = store.get_account(name)
    if record is None:
        raise HTTPException(status_code=404, detail=f"账号 {name} 不存在")
    if example:
        config = _example_config()
    else:
        from tg_assistant.config import AccountConfig

        config = AccountConfig.default()
    store.save_account_config(name, config)
    return {"ok": True, "config": config.model_dump(mode="json")}


def _example_config() -> Any:
    from tg_assistant.config import AccountConfig

    return AccountConfig.model_validate(
        {
            "forward": {
                "enabled": True,
                "rules": [
                    {
                        "id": "demo-regex",
                        "name": "示例：抓取带金额的消息",
                        "enabled": False,
                        "sources": ["@source_channel_username"],
                        "targets": [-1001234567890],
                        "mode": "copy",
                        "match": {
                            "mode": "regex",
                            "patterns": [r"(?:金额|价格)[:：]\s*([\d.]+)"],
                            "exclude_patterns": ["测试"],
                            "fields": ["text", "caption"],
                        },
                        "notify": True,
                    }
                ],
            },
            "notify": {
                "enabled": False,
                "bot_token": "${TGA_BOT_TOKEN}",
                "chat_id": 0,
                "mode": "copy",
                "events": ["forward", "red_packet"],
            },
            "red_packet": {
                "enabled": False,
                "chats": [],
                "strategy": "auto",
                "detect": {
                    "button_keywords": ["领取", "抢", "红包", "🧧"],
                    "code_pattern": None,
                    "keyword_template": None,
                },
                "reply": {
                    "enabled": True,
                    "texts": ["谢谢老板", "xxlb", "感谢大哥"],
                    "only_on_success": True,
                    "delay_range": [0.8, 2.5],
                },
            },
        }
    )


# --------------------------------------------------------------------------- #
# 运行控制
# --------------------------------------------------------------------------- #
@router.post("/run/start")
async def api_run_start(
    payload: dict[str, Any] | None = None,
    runtime=Depends(get_runtime),
) -> dict[str, Any]:
    accounts = (payload or {}).get("accounts")
    result = await runtime.start(accounts)
    return result


@router.post("/run/stop")
async def api_run_stop(runtime=Depends(get_runtime)) -> dict[str, Any]:
    return await runtime.stop()


@router.get("/run/status")
async def api_run_status(runtime=Depends(get_runtime)) -> dict[str, Any]:
    return {"running": runtime.is_running, "accounts": runtime.account_status()}


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
@router.post("/proxy/check")
async def api_proxy_check(
    payload: dict[str, Any],
    settings=Depends(get_settings),
) -> dict[str, Any]:
    proxy_url = payload.get("proxy") or (settings.proxy.to_url() if settings.proxy else None)
    if not proxy_url:
        raise HTTPException(status_code=400, detail="未提供代理地址")
    try:
        proxy = ProxyConfig.from_url(proxy_url)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    results = await probe_proxy(proxy, timeout=float(payload.get("timeout", 8.0)))
    ok, report = summarize(results)
    return {"ok": ok, "report": report}


@router.get("/chats/{name}")
async def api_chats(
    name: str,
    limit: int = Query(50, ge=1, le=500),
    keyword: str = Query(""),
    store=Depends(get_store),
    settings=Depends(get_settings),
    runtime=Depends(get_runtime),
) -> dict[str, Any]:
    """列出账号的会话。"""
    from tg_assistant.client import build_client

    record = store.get_account(name)
    if record is None:
        raise HTTPException(status_code=404, detail=f"账号 {name} 不存在")
    if record.name in runtime._running_accounts():
        raise HTTPException(status_code=409, detail="账号正在运行中，请先停止再拉会话列表")

    async def _list() -> list[dict[str, Any]]:
        client = build_client(record, settings, store.paths, no_updates=True)
        await client.start()
        try:
            rows: list[dict[str, Any]] = []
            async for dialog in client.get_dialogs(limit=limit):
                chat = dialog.chat
                title = chat.title or " ".join(
                    filter(None, [chat.first_name, chat.last_name])
                ) or "-"
                username = chat.username or ""
                if keyword and keyword.lower() not in f"{title} {username}".lower():
                    continue
                rows.append(
                    {
                        "chat_id": chat.id,
                        "type": getattr(chat.type, "value", str(chat.type)),
                        "title": title[:40],
                        "username": ("@" + username) if username else None,
                    }
                )
            return rows
        finally:
            await client.stop(block=True)

    rows = await _list()
    return {"chats": rows}


@router.post("/notify/test")
async def api_notify_test(
    payload: dict[str, Any],
    store=Depends(get_store),
    settings=Depends(get_settings),
) -> dict[str, Any]:
    """发送测试通知。"""
    from tg_assistant.logging_setup import account_logger
    from tg_assistant.notify import BotNotifier, NotifyTask
    from tg_assistant.proxy import resolve_proxy

    name = payload.get("account", "")
    record = store.get_account(name)
    if record is None:
        raise HTTPException(status_code=404, detail=f"账号 {name} 不存在")
    config = store.load_account_config(name, create=False)
    if not config.notify.enabled:
        raise HTTPException(status_code=400, detail="该账号未启用通知")
    proxy = resolve_proxy(record, settings)

    notifier = BotNotifier(config.notify, account_logger(name), proxy)
    await notifier.start()
    ok, detail = await notifier.verify()
    if not ok:
        await notifier.stop()
        raise HTTPException(status_code=400, detail=f"bot 验证失败：{detail}")
    notifier.submit(
        NotifyTask(
            event="forward",
            text=f"✅ TG-Assistant 通知测试\n账号：{name}\n如果你看到这条消息，说明通知渠道已经打通。",
        )
    )
    await notifier.stop(drain_timeout=20)
    return {
        "ok": notifier.stats["sent"] == 1,
        "stats": dict(notifier.stats),
        "bot": detail,
    }


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
@router.get("/logs/history")
async def api_logs_history(
    limit: int = Query(200, ge=1, le=1000),
    runtime=Depends(get_runtime),
) -> dict[str, Any]:
    return {"logs": runtime.recent_logs[-limit:]}


__all__ = ["router"]
