"""REST API 路由。

所有非实时操作（账号管理、配置 CRUD、代理检查、通知测试等）都走这里。
实时功能（日志流、扫码登录）走 WebSocket。
"""
from __future__ import annotations

import contextlib
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


def _run_match_test(payload: dict[str, Any]) -> dict[str, Any]:
    """试跑一次匹配：纯计算，不落盘、不依赖账号是否已登录。

    抽成独立函数是因为有两个入口 —— 带账号路径的旧地址，以及页面去掉
    「先选账号」之后新增的全局地址（见 :func:`api_rules_test_global`）。
    匹配逻辑本身跟账号毫无关系，两边必须给出完全一样的结果。
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


@router.post("/config/{name}/rules/test")
async def api_rules_test(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """在服务端试跑匹配逻辑，供规则编辑页做实时预览。

    ``name`` 只是为了和其余 ``/config/{name}/...`` 端点保持同一种路径形状，
    并不参与计算 —— 所以新页面用不着它（见 :func:`api_rules_test_global`）。
    """
    return _run_match_test(payload)


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
# 全局转发规则（跨账号）
#
# 规则本身是**按账号存**的（``data/accounts/<name>/config.json`` 的
# ``forward.rules``），从来就没有全局规则表。这一组端点存在的意义是让页面
# 不必「先选账号才能建规则」：新建时不指定账号就**扇出写入每个账号**，
# 改 / 删则自动找到拥有这条规则的所有账号。
#
# 写入语义（页面上的行为都从这里推导）：
#   * 不传 accounts（或传空） → 作用于**全部账号**（新建）/ **所有拥有者**（改删）
#   * 显式传 accounts          → 只作用于这些账号，名字不存在直接 404
# --------------------------------------------------------------------------- #
def _rule_body(payload: dict[str, Any]) -> dict[str, Any]:
    """从全局请求体里取出规则本身。

    推荐写法 ``{"rule": {...}, "accounts": [...]}``；也接受把规则字段平铺在顶层
    （少一层嵌套）。``accounts`` 永远不算规则字段 —— 否则 ``ForwardRule`` 是
    ``StrictModel``，多一个未知字段会把整个请求判成 400。
    """
    body = payload.get("rule")
    if isinstance(body, dict):
        return body
    return {key: value for key, value in payload.items() if key != "accounts"}


def _validate_rule(body: dict[str, Any]) -> Any:
    from tg_assistant.config import ForwardRule

    try:
        return ForwardRule.model_validate(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"规则校验失败：{exc}") from exc


def _resolve_rule_accounts(store: Any, raw: Any) -> list[str]:
    """把请求里的 ``accounts`` 解析成账号名列表。

    ``None`` / 空列表 / 空字符串 / 全是空白的字符串都表示**全部账号** —— 这就是
    页面上的默认行为（弹窗里一个账号都不勾 = 写给全部账号）。

    显式给了名字就必须都存在：静默忽略一个拼错的账号名，用户会以为规则写进去了。
    """
    if raw is None:
        names: list[str] = []
    elif isinstance(raw, str):
        names = [part.strip() for part in raw.split(",") if part.strip()]
    elif isinstance(raw, (list, tuple, set)):
        names = [str(part).strip() for part in raw if str(part).strip()]
    else:
        raise HTTPException(status_code=400, detail="accounts 必须是账号名数组")

    registry = store.load_registry()
    if not names:
        return [record.name for record in registry.accounts]

    known = {record.name for record in registry.accounts}
    for name in names:
        validate_account_name(name)  # 非法名由应用级异常处理器翻成 400
        if name not in known:
            raise HTTPException(status_code=404, detail=f"账号 {name} 不存在")

    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return unique


def _rule_owners(store: Any, rule_id: str) -> list[str]:
    """找出配置里含有这条规则 id 的所有账号。"""
    owners: list[str] = []
    for record in store.load_registry().accounts:
        config = store.load_account_config(record.name, create=False)
        if any(rule.id == rule_id for rule in config.forward.rules):
            owners.append(record.name)
    return owners


@router.get("/rules")
async def api_rules_overview(
    store=Depends(get_store),
    runtime=Depends(get_runtime),
) -> dict[str, Any]:
    """所有账号的规则总览，供「转发规则」页一次渲染完。

    顺带带上 ``running`` / ``session_exists``：分组标题要画状态点，放在同一个
    响应里就不会出现「两个接口的数据对不上」的瞬间。
    """
    status = {item["name"]: item for item in runtime.account_status()}
    accounts: list[dict[str, Any]] = []
    for record in store.load_registry().accounts:
        config = store.load_account_config(record.name, create=False)
        info = status.get(record.name, {})
        accounts.append(
            {
                "name": record.name,
                "username": record.username,
                "display_name": record.display_name,
                "enabled": record.enabled,
                "running": bool(info.get("running")),
                "session_exists": bool(info.get("session_exists")),
                "forward_enabled": config.forward.enabled,
                "rules": [rule.model_dump(mode="json") for rule in config.forward.rules],
            }
        )
    return {"accounts": accounts}


@router.post("/rules")
async def api_rules_create_global(
    payload: dict[str, Any],
    store=Depends(get_store),
) -> dict[str, Any]:
    """新增规则；``accounts`` 为空表示写入**全部账号**（扇出）。

    同名 id 已存在的账号会被**跳过**而不是整单失败 —— 否则「把新规则发给全部
    账号」在某个账号里已经手工加过时就完全用不了了。全都冲突才返回 409。
    """
    targets = _resolve_rule_accounts(store, payload.get("accounts"))
    if not targets:
        raise HTTPException(status_code=400, detail="还没有任何账号，无法保存转发规则")
    rule = _validate_rule(_rule_body(payload))

    saved: list[str] = []
    conflicts: list[str] = []
    for name in targets:
        config = store.load_account_config(name, create=False)
        if any(existing.id == rule.id for existing in config.forward.rules):
            conflicts.append(name)
            continue
        config.forward.rules.append(rule)
        store.save_account_config(name, config)
        saved.append(name)

    if not saved:
        raise HTTPException(
            status_code=409,
            detail=f"规则 id {rule.id!r} 在这些账号里都已存在：{'、'.join(conflicts)}",
        )
    return {
        "ok": True,
        "saved": saved,
        "conflicts": conflicts,
        "rule": rule.model_dump(mode="json"),
    }


@router.put("/rules/{rule_id}")
async def api_rules_update_global(
    rule_id: str,
    payload: dict[str, Any],
    store=Depends(get_store),
) -> dict[str, Any]:
    """覆盖一条规则；``accounts`` 为空表示**所有拥有它的账号**一起改。

    规则当初是扇出写出去的，改一次就该同步到所有副本 —— 只改一半会让各个账号
    的行为悄悄不一致，而页面上看起来「我明明改了」。
    """
    raw_accounts = payload.get("accounts")
    if raw_accounts:
        targets = _resolve_rule_accounts(store, raw_accounts)
    else:
        targets = _rule_owners(store, rule_id)
        if not targets:
            raise HTTPException(status_code=404, detail=f"规则 {rule_id!r} 不存在")
    rule = _validate_rule(_rule_body(payload))

    # 先整体校验、再统一落盘：边检查边写的话，第 3 个账号撞车时前 2 个已经写完了，
    # 留下一个「只改了一半」的配置 —— 用户看到报错，却有一半账号已经变了。
    plans: list[tuple[str, Any, int]] = []
    missing: list[str] = []
    for name in targets:
        config = store.load_account_config(name, create=False)
        index = next(
            (i for i, existing in enumerate(config.forward.rules) if existing.id == rule_id),
            None,
        )
        if index is None:
            missing.append(name)
            continue
        # 改 id 等于改名：新 id 在该账号里被占用时直接拒绝。写出重复 id 的后果
        # 不是「多一条规则」而是**同一条消息被转发两次** —— runner 是逐条跑的。
        if rule.id != rule_id and any(
            existing.id == rule.id for existing in config.forward.rules
        ):
            raise HTTPException(
                status_code=409,
                detail=f"账号 {name} 里已存在 id {rule.id!r} 的规则，无法改名",
            )
        plans.append((name, config, index))

    for name, config, index in plans:
        config.forward.rules[index] = rule
        store.save_account_config(name, config)

    if not plans:
        raise HTTPException(
            status_code=404,
            detail=f"这些账号里都没有规则 {rule_id!r}：{'、'.join(missing)}",
        )
    return {
        "ok": True,
        "updated": [name for name, _, _ in plans],
        "missing": missing,
        "rule": rule.model_dump(mode="json"),
    }


@router.delete("/rules/{rule_id}")
async def api_rules_delete_global(
    rule_id: str,
    accounts: str | None = Query(
        None, description="只从这些账号删除（逗号分隔）；缺省 = 所有拥有它的账号"
    ),
    store=Depends(get_store),
) -> dict[str, Any]:
    """删除规则；``accounts`` 缺省表示从**所有拥有它的账号**里删掉。"""
    if accounts:
        targets = _resolve_rule_accounts(store, accounts)
    else:
        targets = _rule_owners(store, rule_id)

    removed: list[str] = []
    missing: list[str] = []
    for name in targets:
        config = store.load_account_config(name, create=False)
        before = len(config.forward.rules)
        config.forward.rules = [rule for rule in config.forward.rules if rule.id != rule_id]
        if len(config.forward.rules) == before:
            missing.append(name)
            continue
        store.save_account_config(name, config)
        removed.append(name)

    if not removed:
        raise HTTPException(status_code=404, detail=f"规则 {rule_id!r} 不存在")
    return {"ok": True, "removed": removed, "missing": missing}


@router.post("/rules/test")
async def api_rules_test_global(payload: dict[str, Any]) -> dict[str, Any]:
    """试跑匹配逻辑，不需要账号上下文。

    页面去掉「先选账号」之后就凑不出 ``/config/{name}/rules/test`` 里的 ``name``
    了，而匹配本身跟账号毫无关系，所以补一个不带动路径的入口。
    """
    return _run_match_test(payload)


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
# Cloudflare 优选 IP 自动更新
# --------------------------------------------------------------------------- #
def _mask_api_token(data: dict[str, Any]) -> dict[str, Any]:
    """api_token 只回显前缀，避免面板把它整串读回去。"""
    token = data.get("api_token")
    if token:
        data["api_token"] = f"{token[:6]}***"
    return data


def _isp_rows(config: Any, fetched: Any, state: dict[str, Any]) -> list[dict[str, Any]]:
    """三网分流：每个运营商一行的决策预览（不写 DNS）。

    按 ``ISP_KEYS`` 固定顺序输出，面板就能稳定地按 移动/电信/联通 排三行 ——
    某一家这次没抓到也要占位显示，否则用户会以为「漏了」。
    """
    from tg_assistant.cloudflare_ip import ISP_LABELS, should_update_isp, threshold_for
    from tg_assistant.config import ISP_KEYS

    rows: list[dict[str, Any]] = []
    for isp in ISP_KEYS:
        label = ISP_LABELS[isp]
        threshold = threshold_for(config, isp)
        best = fetched.best_by_isp.get(isp)
        if best is None:
            rows.append(
                {
                    "isp": isp,
                    "label": label,
                    "ip": None,
                    "speed": None,
                    "threshold": threshold,
                    "should_update": False,
                    "reason": f"最近 {config.fetch_limit} 条消息里没有{label}的 IP",
                }
            )
            continue
        decision = should_update_isp(best, config, state)
        rows.append(
            {
                "isp": isp,
                "label": label,
                "ip": best.ip,
                "speed": best.speed,
                "threshold": threshold,
                "should_update": decision.should_update,
                "reason": decision.reason,
                "message_id": best.message_id,
            }
        )
    return rows


@router.get("/config/{name}/cloudflare_ip")
async def api_cloudflare_ip_get(name: str, store=Depends(get_store)) -> dict[str, Any]:
    _require_account(store, name)
    config = store.load_account_config(name, create=False)
    return _mask_api_token(config.cloudflare_ip.model_dump(mode="json"))


@router.put("/config/{name}/cloudflare_ip")
async def api_cloudflare_ip_put(
    name: str,
    payload: dict[str, Any],
    store=Depends(get_store),
) -> dict[str, Any]:
    """保存 Cloudflare IP 配置。

    ⚠️ 面板读回来的 ``api_token`` 是脱敏的（``A1b2C3***``）。
    若原样提交回来，会把脱敏值当成新 token 存下去。
    所以：脱敏形态的值一律忽略，保留原有 token。
    """
    from tg_assistant.config import CloudflareIPConfig

    _require_account(store, name)
    config = store.load_account_config(name, create=False)

    submitted = dict(payload)
    token = str(submitted.get("api_token") or "")
    if token.endswith("***"):
        submitted["api_token"] = config.cloudflare_ip.api_token

    try:
        config.cloudflare_ip = CloudflareIPConfig.model_validate(submitted)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"配置校验失败：{exc}") from exc

    store.save_account_config(name, config)
    return {"ok": True}


@router.put("/config/{name}/cloudflare_ip/enabled")
async def api_cloudflare_ip_enabled(
    name: str,
    payload: dict[str, Any],
    store=Depends(get_store),
) -> dict[str, Any]:
    _require_account(store, name)
    config = store.load_account_config(name, create=False)
    config.cloudflare_ip.enabled = bool(payload.get("enabled", False))
    store.save_account_config(name, config)
    return {"ok": True, "enabled": config.cloudflare_ip.enabled}


@router.post("/config/{name}/cloudflare_ip/trigger")
async def api_cloudflare_ip_trigger(
    name: str,
    store=Depends(get_store),
    settings=Depends(get_settings),
    runtime=Depends(get_runtime),
) -> dict[str, Any]:
    """手动触发一次优选 IP 抓取 + DNS 更新。"""
    from tg_assistant.cloudflare_ip import (
        ISP_LABELS,
        fetch_and_update,
        make_message_source,
        make_message_source_from_account,
    )

    _require_account(store, name)
    account_config = store.load_account_config(name, create=False)
    cf_config = account_config.cloudflare_ip

    if not cf_config.api_token or not cf_config.source_channel:
        raise HTTPException(status_code=400, detail="缺少 api_token 或 source_channel")

    # 1. 尝试复用已在运行的账号 client
    source = None
    client_to_stop = None
    running = runtime._running_accounts() if hasattr(runtime, "_running_accounts") else set()
    if name in running and runtime._runner is not None:
        for runner in runtime._runner.runners.values():
            if runner.name == name and runner.client is not None:
                source = make_message_source(runner.client)
                break

    # 2. 没在跑就临时起一个 client
    if source is None:
        client, source = await make_message_source_from_account(name, store, settings)
        client_to_stop = client

    try:
        from tg_assistant.proxy import resolve_proxy

        record = store.require_account(name)
        proxy = resolve_proxy(record, settings)
        state = store.load_state(name)

        summary = await fetch_and_update(cf_config, source, state, proxy)
        # 落盘（速度已在 fetch_and_update 内部写入 state）
        store.save_state(name, state)
    finally:
        if client_to_stop is not None:
            with contextlib.suppress(Exception):
                await client_to_stop.stop(block=True)

    return {
        "ok": summary.all_ok and not summary.skipped,
        "skipped": summary.skipped,
        "skipped_reason": summary.skipped_reason,
        "split_by_isp": cf_config.split_by_isp,
        "fastest_ip": summary.fetched.fastest,
        "fastest_speed": summary.fetched.fastest_speed,
        "all_ips": summary.fetched.all_ips,
        "all_speeds": summary.fetched.all_speeds,
        "current_speed": state.get("cloudflare_ip_last_speed"),
        "decisions": [
            {
                "isp": isp,
                "label": ISP_LABELS.get(isp, isp),
                "should_update": d.should_update,
                "ip": d.ip,
                "speed": d.speed,
                "reason": d.reason,
            }
            for isp, d in summary.decisions.items()
        ],
        "results": [
            {
                "domain": r.domain,
                "name": r.name,
                "type": r.record_type,
                "ip": r.ip,
                "ok": r.ok,
                "error": r.error,
                "isp": r.isp,
                "skipped": r.skipped,
                "adopted": r.adopted,
            }
            for r in summary.results
        ],
        "updated_at": summary.updated_at,
    }


@router.post("/config/{name}/cloudflare_ip/test")
async def api_cloudflare_ip_test(
    name: str,
    store=Depends(get_store),
    settings=Depends(get_settings),
    runtime=Depends(get_runtime),
) -> dict[str, Any]:
    """测试抓取频道消息并解析 IP，展示决策结果（不更新 DNS）。"""
    from tg_assistant.cloudflare_ip import (
        fetch_ips_from_channel,
        make_message_source,
        make_message_source_from_account,
        should_update,
    )

    _require_account(store, name)
    account_config = store.load_account_config(name, create=False)
    cf_config = account_config.cloudflare_ip

    if not cf_config.source_channel:
        raise HTTPException(status_code=400, detail="缺少 source_channel")

    source = None
    client_to_stop = None
    running = runtime._running_accounts() if hasattr(runtime, "_running_accounts") else set()
    if name in running and runtime._runner is not None:
        for runner in runtime._runner.runners.values():
            if runner.name == name and runner.client is not None:
                source = make_message_source(runner.client)
                break

    if source is None:
        client, source = await make_message_source_from_account(name, store, settings)
        client_to_stop = client

    try:
        fetched = await fetch_ips_from_channel(
            cf_config.source_channel,
            cf_config.fetch_limit,
            source,
        )
    finally:
        if client_to_stop is not None:
            with contextlib.suppress(Exception):
                await client_to_stop.stop(block=True)

    # 决策预览
    state = store.load_state(name)
    decision = should_update(fetched, cf_config, state) if fetched.has_ip else None

    return {
        "ok": fetched.has_ip,
        "split_by_isp": cf_config.split_by_isp,
        "fastest_ip": fetched.fastest,
        "fastest_speed": fetched.fastest_speed,
        "all_ips": fetched.all_ips,
        "all_speeds": fetched.all_speeds,
        #: 三网分流时每家的决策预览（不分流时也返回，方便对照看抓到了什么）。
        "isp_rows": _isp_rows(cf_config, fetched, state),
        "decision": {
            "should_update": decision.should_update if decision else False,
            "reason": decision.reason if decision else "未解析到 IP",
            "current_speed": state.get("cloudflare_ip_last_speed"),
            "min_speed_threshold": cf_config.min_speed_threshold,
            "only_update_if_faster": cf_config.only_update_if_faster,
        } if decision else None,
        "raw_text_preview": (
            (fetched.raw_text[:300] + "...")
            if fetched.raw_text and len(fetched.raw_text) > 300
            else fetched.raw_text
        ),
    }


@router.get("/config/{name}/cloudflare_ip/status")
async def api_cloudflare_ip_status(
    name: str,
    store=Depends(get_store),
    runtime=Depends(get_runtime),
) -> dict[str, Any]:
    """获取当前 Cloudflare IP 更新状态（上次更新结果、实时监听状态）。"""

    _require_account(store, name)
    account_config = store.load_account_config(name, create=False)
    cf_config = account_config.cloudflare_ip
    state = store.load_state(name)

    # 检查实时监听是否在线
    listener_running = False
    running = runtime._running_accounts() if hasattr(runtime, "_running_accounts") else set()
    if name in running and runtime._runner is not None:
        for runner in runtime._runner.runners.values():
            if runner.name == name:
                listener_running = runner.cf_ip_listener is not None and runner.cf_ip_listener.is_running
                break

    last_result = state.get("cloudflare_ip_last_result", {})
    return {
        "enabled": cf_config.enabled,
        "real_time_listen": cf_config.real_time_listen,
        "listener_running": listener_running,
        "split_by_isp": cf_config.split_by_isp,
        "min_speed_threshold": cf_config.min_speed_threshold,
        "min_speed_threshold_by_isp": cf_config.min_speed_threshold_by_isp,
        "only_update_if_faster": cf_config.only_update_if_faster,
        "current_ip": state.get("cloudflare_ip_last_ip"),
        "current_speed": state.get("cloudflare_ip_last_speed"),
        #: 三网分流时各家上次写入的速度 / IP。
        "last_speed_by_isp": state.get("cloudflare_ip_last_speed_by_isp") or {},
        "last_ip_by_isp": state.get("cloudflare_ip_last_ip_by_isp") or {},
        "last_run": state.get("cloudflare_ip_last_run"),
        "last_result": last_result,
        "source_channel": cf_config.source_channel,
        "records_count": len(cf_config.records),
    }


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
