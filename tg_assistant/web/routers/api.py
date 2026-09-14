"""REST API 路由。

所有非实时操作（账号管理、配置 CRUD、代理检查、通知测试等）都走这里。
实时功能（日志流、扫码登录）走 WebSocket。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query

from tg_assistant.config import ProxyConfig
from tg_assistant.paths import InvalidAccountName, validate_account_name
from tg_assistant.proxy import probe_proxy, summarize

from ..deps import get_runtime, get_settings, get_store

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


@router.post("/accounts/{name}/clear-session")
async def api_account_clear_session(
    name: str,
    store=Depends(get_store),
    runtime=Depends(get_runtime),
) -> dict[str, Any]:
    """清除账号的会话数据（本地 session 文件 + PostgreSQL 备份 + 二维码），用于重新登录。

    回移自部署版，但修掉了它的两个 bug：

    * 它用 ``shutil.rmtree(account_paths.session_dir)`` 清本地文件，
      而 ``session_dir`` 就是账号根目录 —— 于是 ``config.json``（转发规则）
      和 ``state.json`` 会一起被删掉。
    * 它删的是 PG 表 ``pyrogram_sessions``，而 :func:`_save_pg_session_string`
      写的是 ``tg_sessions`` —— 一张从未被写过的表，等于没删。
    """
    from tg_assistant.client import _delete_pg_session_string

    try:
        validated = validate_account_name(name)
    except InvalidAccountName as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    record = store.get_account(validated)
    if record is None:
        raise HTTPException(status_code=404, detail=f"账号 {validated} 不存在")
    if record.name in runtime._running_accounts():
        raise HTTPException(status_code=409, detail="账号正在运行中，请先停止再清除会话")

    pg_cleared = _delete_pg_session_string(validated)
    removed = store.clear_session(validated)

    return {
        "ok": True,
        "message": "会话数据已清除，请重新登录",
        "removed_files": removed,
        "postgres_cleared": pg_cleared,
    }


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
    try:
        name = validate_account_name(account)
    except InvalidAccountName as exc:
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
# 转发规则 CRUD
# --------------------------------------------------------------------------- #
def _require_account(store: Any, name: str) -> Any:
    """取账号记录，不存在直接 404。账号名非法由应用级异常处理器翻成 400。"""
    record = store.get_account(name)
    if record is None:
        raise HTTPException(status_code=404, detail=f"账号 {name} 不存在")
    return record


@router.get("/config/{name}/rules")
async def api_rules_list(name: str, store=Depends(get_store)) -> dict[str, Any]:
    """列出账号的全部转发规则。"""
    _require_account(store, name)
    config = store.load_account_config(name, create=False)
    return {
        "rules": [rule.model_dump(mode="json") for rule in config.forward.rules],
        "enabled": config.forward.enabled,
    }


@router.post("/config/{name}/rules")
async def api_rules_add(
    name: str,
    payload: dict[str, Any],
    store=Depends(get_store),
) -> dict[str, Any]:
    """新增一条转发规则。"""
    from tg_assistant.config import ForwardRule

    _require_account(store, name)
    config = store.load_account_config(name, create=False)
    try:
        rule = ForwardRule.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"规则校验失败：{exc}") from exc

    if any(existing.id == rule.id for existing in config.forward.rules):
        raise HTTPException(status_code=409, detail=f"规则 id {rule.id!r} 已存在")

    config.forward.rules.append(rule)
    store.save_account_config(name, config)
    return {"ok": True, "rule": rule.model_dump(mode="json")}


@router.put("/config/{name}/rules/{rule_id}")
async def api_rules_update(
    name: str,
    rule_id: str,
    payload: dict[str, Any],
    store=Depends(get_store),
) -> dict[str, Any]:
    """按 id 覆盖一条转发规则。"""
    from tg_assistant.config import ForwardRule

    _require_account(store, name)
    config = store.load_account_config(name, create=False)
    try:
        updated = ForwardRule.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"规则校验失败：{exc}") from exc

    for index, rule in enumerate(config.forward.rules):
        if rule.id == rule_id:
            config.forward.rules[index] = updated
            break
    else:
        raise HTTPException(status_code=404, detail=f"规则 {rule_id!r} 不存在")

    store.save_account_config(name, config)
    return {"ok": True, "rule": updated.model_dump(mode="json")}


@router.delete("/config/{name}/rules/{rule_id}")
async def api_rules_delete(
    name: str,
    rule_id: str,
    store=Depends(get_store),
) -> dict[str, Any]:
    """删除一条转发规则。"""
    _require_account(store, name)
    config = store.load_account_config(name, create=False)
    before = len(config.forward.rules)
    config.forward.rules = [rule for rule in config.forward.rules if rule.id != rule_id]
    if len(config.forward.rules) == before:
        raise HTTPException(status_code=404, detail=f"规则 {rule_id!r} 不存在")

    store.save_account_config(name, config)
    return {"ok": True}


@router.post("/config/{name}/rules/test")
async def api_rules_test(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """在服务端试跑匹配逻辑，供规则编辑页做实时预览。

    纯计算，不落盘、不依赖账号是否已登录。
    """
    import re

    pattern = str(payload.get("pattern", ""))
    text = str(payload.get("text", ""))
    mode = str(payload.get("mode", "regex"))
    ignore_case = bool(payload.get("ignore_case", True))

    if not pattern:
        return {"match": False, "error": "正则表达式为空"}
    if not text:
        return {"match": False, "error": "测试文本为空"}

    if mode == "regex":
        try:
            compiled = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            return {"match": False, "error": f"正则表达式无效: {exc}"}
        found = compiled.search(text)
        if not found:
            return {"match": False}
        groups = found.groups()
        return {
            "match": True,
            "full_match": found.group(0),
            "groups": [group for group in groups if group] if groups else [],
            "span": list(found.span()),
        }

    if mode == "contains":
        matched = pattern.lower() in text.lower() if ignore_case else pattern in text
        return {"match": matched}

    if mode == "exact":
        matched = pattern.lower() == text.lower() if ignore_case else pattern == text
        return {"match": matched}

    return {"match": True, "message": "all 模式始终匹配"}


@router.put("/config/{name}/forward-enabled")
async def api_forward_set_enabled(
    name: str,
    payload: dict[str, Any],
    store=Depends(get_store),
) -> dict[str, Any]:
    """转发功能总开关。"""
    _require_account(store, name)
    config = store.load_account_config(name, create=False)
    config.forward.enabled = bool(payload.get("enabled", True))
    store.save_account_config(name, config)
    return {"ok": True, "enabled": config.forward.enabled}


# --------------------------------------------------------------------------- #
# 抢红包设置
# --------------------------------------------------------------------------- #
@router.get("/config/{name}/red_packet")
async def api_red_packet_get(name: str, store=Depends(get_store)) -> dict[str, Any]:
    _require_account(store, name)
    return store.load_account_config(name, create=False).red_packet.model_dump(mode="json")


@router.put("/config/{name}/red_packet")
async def api_red_packet_put(
    name: str,
    payload: dict[str, Any],
    store=Depends(get_store),
) -> dict[str, Any]:
    from tg_assistant.config import RedPacketConfig

    _require_account(store, name)
    config = store.load_account_config(name, create=False)
    try:
        config.red_packet = RedPacketConfig.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"配置校验失败：{exc}") from exc
    store.save_account_config(name, config)
    return {"ok": True}


@router.put("/config/{name}/red_packet/enabled")
async def api_red_packet_enabled(
    name: str,
    payload: dict[str, Any],
    store=Depends(get_store),
) -> dict[str, Any]:
    _require_account(store, name)
    config = store.load_account_config(name, create=False)
    config.red_packet.enabled = bool(payload.get("enabled", False))
    store.save_account_config(name, config)
    return {"ok": True, "enabled": config.red_packet.enabled}


# --------------------------------------------------------------------------- #
# 通知设置
# --------------------------------------------------------------------------- #
def _mask_bot_token(data: dict[str, Any]) -> dict[str, Any]:
    """bot_token 只回显前缀，避免面板把它整串读回去。"""
    token = data.get("bot_token")
    if token:
        data["bot_token"] = f"{token[:8]}***"
    return data


@router.get("/config/{name}/notify")
async def api_notify_get(name: str, store=Depends(get_store)) -> dict[str, Any]:
    _require_account(store, name)
    config = store.load_account_config(name, create=False)
    return _mask_bot_token(config.notify.model_dump(mode="json"))


@router.put("/config/{name}/notify")
async def api_notify_put(
    name: str,
    payload: dict[str, Any],
    store=Depends(get_store),
) -> dict[str, Any]:
    """保存通知配置。

    ⚠️ 面板读回来的 ``bot_token`` 是脱敏的（``12345678***``）。若原样提交回来，
    这里会把脱敏值当成新 token 存下去，把真 token 覆盖掉。
    所以：脱敏形态的值一律忽略，保留原有 token。
    """
    from tg_assistant.config import NotifyConfig

    _require_account(store, name)
    config = store.load_account_config(name, create=False)

    submitted = dict(payload)
    token = str(submitted.get("bot_token") or "")
    if token.endswith("***"):
        submitted["bot_token"] = config.notify.bot_token

    try:
        config.notify = NotifyConfig.model_validate(submitted)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"配置校验失败：{exc}") from exc

    store.save_account_config(name, config)
    return {"ok": True, "bot_token": _mask_bot_token({"bot_token": config.notify.bot_token})["bot_token"]}


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


@router.post("/run/start/{name}")
async def api_run_start_one(name: str, runtime=Depends(get_runtime)) -> dict[str, Any]:
    """单独启动一个账号。

    走 :meth:`RuntimeManager.start_account`，而不是 ``start([name])`` ——
    后者在已有账号运行时只会返回「已经在运行中」，等于这个端点没法用。
    """
    record = runtime.store.get_account(name)
    if record is None:
        raise HTTPException(status_code=404, detail=f"账号 {name} 不存在")
    return await runtime.start_account(name)


@router.post("/run/stop/{name}")
async def api_run_stop_one(name: str, runtime=Depends(get_runtime)) -> dict[str, Any]:
    """单独停止一个账号（其余账号会被优雅重启）。"""
    record = runtime.store.get_account(name)
    if record is None:
        raise HTTPException(status_code=404, detail=f"账号 {name} 不存在")
    return await runtime.stop_account(name)


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
