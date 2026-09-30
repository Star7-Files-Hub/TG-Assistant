"""REST API 路由。

所有非实时操作（账号管理、配置 CRUD、代理检查、通知测试等）都走这里。
实时功能（日志流、扫码登录）走 WebSocket。
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query

from tg_assistant.config import ProxyConfig
from tg_assistant.logging_setup import get_logger
from tg_assistant.paths import InvalidAccountName, validate_account_name
from tg_assistant.metrics import RANGES, beijing_day
from tg_assistant.proxy import probe_proxy, summarize

from ..deps import get_runtime, get_settings, get_store

log = get_logger("web.api")

#: 「测试抓取」/「立即触发」里，临时新建 Telegram client 的超时（秒）。
CLIENT_TIMEOUT = 30.0
#: 抓取频道消息 / 写入 DNS 的超时（秒）。
FETCH_TIMEOUT = 45.0
# ⚠️ 前端 ``CF_REQUEST_TIMEOUT_MS``（cloudflare_ip.html）必须 **大于这两者之和**。
# 否则前端先 abort，用户看到的是笼统的「请求超时」，后端那条更精确的 504
# （「账号可能正被其它任务占用」）永远没机会显示出来。
# 有静态契约钉住这个大小关系（tests/test_web_pages.py）。

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


#: 数据大盘三个口径的按钮文案。**故意只写「今天 / 本月 / 总计」**：
#: 「按北京时间 0 点起算」这层信息由同一张卡片上常驻的那行小字承担
#: （见 ``dashboard.html`` 的 ``metrics-hint``），塞进按钮里会在窄屏折行、
#: 而且和那行小字说的是同一件事。
_METRICS_LABELS = {"day": "今天", "month": "本月", "total": "总计"}


@router.get("/metrics")
async def api_metrics(runtime=Depends(get_runtime)) -> dict[str, Any]:
    """数据大盘：转发 / 抢红包 / 抢注的**成功**次数，天 / 月 / 总三个口径。

    一次把三个口径都返回，面板切换口径时不必再请求一遍，也就不会出现
    「切到月，数字比总还大」这种自相矛盾。
    """
    totals = runtime.metrics().totals()
    return {
        "timezone": "北京时间 (UTC+8)",
        "today": beijing_day(),
        "ranges": {
            name: {"label": _METRICS_LABELS[name], **totals[name]} for name in RANGES
        },
    }


# --------------------------------------------------------------------------- #
# 账号
# --------------------------------------------------------------------------- #
@router.get("/accounts")
async def api_accounts(runtime=Depends(get_runtime)) -> dict[str, Any]:
    # 带上每个账号的功能开关：面板下拉框要靠它默认选到「真的配了这个功能」的账号。
    return {"accounts": runtime.account_status(with_features=True)}


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
                "events": ["forward", "red_packet", "reg_grab"],
            },
            "red_packet": {
                "enabled": False,
                "max_concurrency": 3,
                # 多条互不影响的任务，按顺序匹配：一条红包只被**第一个**命中的任务抢。
                "tasks": [
                    {
                        "id": "main",
                        "name": "主频道",
                        "enabled": True,
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
                    }
                ],
            },
            "reg_grab": {
                "enabled": False,
                "chats": [],
                "detect": {
                    # 示例：MSKY-30-Register_XXXXXXXXXX 这类注册码，捕获组就是要的码
                    "code_pattern": r"(?:Register|Renew|Whitelist)_([A-Za-z0-9]{10})",
                    "text_patterns": [],
                },
                # 监听时段：只在人类活动时段动手，避免半夜秒抢暴露脚本。
                # enabled=False 表示全天可抢（默认）。start > end 表示跨零点。
                "window": {"enabled": True, "start": "08:00", "end": "23:00"},
                "steps": [
                    {
                        "type": "send",
                        "name": "把码发给机器人",
                        "chat": "@example_bot",
                        "text": "/bind {code}",
                    },
                    {"type": "wait", "name": "等它处理", "seconds": 1.5},
                    {
                        "type": "wait_reply",
                        "name": "看回执",
                        "pattern": "成功|失败|已使用",
                        "timeout": 15,
                    },
                ],
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

    🔴 这里回答的是**整条规则的文本级判定**，不是"主正则匹配到了没有"：
    以前只拿 ``pattern`` 跑一遍主正则，用户在「排除规则」里加了 ``21点`` 之后，
    测试按钮照样显示「匹配成功」，于是用户认定"排除没用"（真实原因只是测试器
    压根没把 ``exclude_patterns`` 算进去）。所以现在：

    * 判定**全部**交给 :class:`tg_assistant.matching.CompiledMatcher`（引擎本人）——
      测试器和引擎各写一套 ``re`` 是这个面板踩过的坑：标志一改两边答案就不一样，
      而用户只会信测试器。这里连"哪条排除规则命中的"都是用引擎逐条重跑问出来的
      （:func:`_hit_exclude_patterns`），没有第二套匹配实现。
    * 入参向后兼容：老的 ``pattern``（单个字符串）继续可用，新的 ``patterns`` /
      ``exclude_patterns`` 列表优先。页面以前自己用 ``|`` 拼主正则，那会让捕获组
      编号和引擎（逐条模式、取命中那条的组）对不上，现在也一并修好了。
    """
    import re

    from tg_assistant.config import MatchConfig
    from tg_assistant.matching import CompiledMatcher, compile_user_pattern

    def _as_list(value: Any) -> list[str]:
        """``patterns`` / ``exclude_patterns`` 允许传字符串或列表（老入参是字符串）。"""
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value else []
        return [str(item) for item in value if str(item).strip()]

    patterns = _as_list(payload.get("patterns"))
    if "patterns" not in payload:
        patterns = _as_list(payload.get("pattern", ""))
    exclude_patterns = _as_list(payload.get("exclude_patterns"))
    text = str(payload.get("text", ""))
    mode = str(payload.get("mode", "regex"))
    ignore_case = bool(payload.get("ignore_case", True))

    if mode not in ("regex", "contains", "exact", "all"):
        # 老实现里未知 mode 落到「始终匹配」（页面只可能发这四个），保持这个口径
        return {"match": True, "message": "all 模式始终匹配"}
    # all 模式本来就"不做文本判断"，主正则可以是空的（引擎允许）
    if not patterns and mode != "all":
        return {"match": False, "error": "正则表达式为空"}
    if not text:
        return {"match": False, "error": "测试文本为空"}

    if mode == "regex":
        # 逐条预编译只是为了把"哪一条写错了"说清楚：CompiledMatcher 一次编译整组，
        # 抛出来的 re.error 里不带是哪条。编译仍用引擎同一个 compile_user_pattern。
        for raw in patterns + exclude_patterns:
            try:
                compile_user_pattern(raw, ignore_case=ignore_case)
            except re.error as exc:
                return {"match": False, "error": f"正则表达式无效: {exc}"}

    config = MatchConfig(
        mode=mode,  # type: ignore[arg-type]
        patterns=patterns,
        exclude_patterns=exclude_patterns,
        ignore_case=ignore_case,
    )
    result = CompiledMatcher(config).match_text(text)

    if result.matched:
        out: dict[str, Any] = {"match": True}
        if mode == "all":
            out["message"] = "all 模式始终匹配"
        else:
            # 引擎告诉我们命中的是**哪一条**（result.keyword），这里只负责把那条的
            # 捕获组/span 取出来给界面看 —— 搜索顺序依然是引擎定的。
            if result.keyword:
                out["matched_pattern"] = result.keyword
            if mode == "regex" and result.keyword:
                found = compile_user_pattern(result.keyword, ignore_case=ignore_case).search(text)
                if found is not None:
                    groups = found.groups()
                    out["full_match"] = found.group(0)
                    out["groups"] = [group for group in groups if group] if groups else []
                    out["span"] = list(found.span())
            if result.groups:
                out.setdefault("groups", [group for group in result.groups if group])
        return out

    if result.reason == "命中排除规则":
        # ⚠️ 只有**主条件已经命中**、排除规则才可能被引擎检查到（regex/contains/exact
        # 都是先命中主模式再判排除；all 模式则总是先判排除）。所以这里报"被拦下"
        # 永远不会把"主正则本来就不命中"的消息误报成"被排除"。
        hits = _hit_exclude_patterns(config, text)
        named = "、".join(f"「{pattern}」" for pattern in hits)
        if mode == "all":
            message = f"该规则是 all 模式（来源对了就算命中），但文本命中排除规则{named} ⇒ 不会转发"
        else:
            message = f"主条件命中，但被排除规则{named}拦下 ⇒ 不会转发"
        out = {
            "match": False,
            "excluded": True,
            "excluded_by": hits,
            "reason": result.reason,
            "message": message,
            "primary": _primary_hit(config, text, ignore_case=ignore_case),
        }
        return out

    out = {"match": False}
    if result.reason:
        out["reason"] = result.reason
    return out


def _hit_exclude_patterns(config: Any, text: str) -> list[str]:
    """到底哪几条排除规则命中了。

    做法是**让引擎自己回答**：逐条把它从排除列表里去掉再跑一遍，如果结果从
    "被排除"变成"命中"，去掉的那条就是肇事者。不去给每条排除单独建一个
    "只有它"的匹配器 —— 那等于把 ``mode`` 各分支的语义（regex/contains/exact/all
    对排除的处理并不一样）在 api.py 里再抄一遍，正是要避免的事。
    """
    from tg_assistant.matching import CompiledMatcher

    hits: list[str] = []
    for index, raw in enumerate(config.exclude_patterns):
        rest = [p for i, p in enumerate(config.exclude_patterns) if i != index]
        probe = config.model_copy(update={"exclude_patterns": rest})
        if CompiledMatcher(probe).match_text(text).matched:
            hits.append(raw)
    return hits


def _primary_hit(config: Any, text: str, *, ignore_case: bool) -> dict[str, Any]:
    """被排除规则拦下时，顺手把"主条件本来匹配到了什么"取出来。

    用户看到「被 21点 拦下」的第一反应往往是"那它到底匹配到哪儿了" —— 不给出来
    就得自己猜。同样走引擎（把排除列表清空再跑一次），主模式一条都不命中时返回空。
    """
    from tg_assistant.matching import CompiledMatcher, compile_user_pattern

    primary: dict[str, Any] = {}
    if config.mode == "all":
        return primary
    probe = config.model_copy(update={"exclude_patterns": []})
    hit = CompiledMatcher(probe).match_text(text)
    if not hit.matched:
        return primary
    if hit.keyword:
        primary["pattern"] = hit.keyword
    if hit.groups:
        primary["groups"] = [group for group in hit.groups if group]
    if config.mode == "regex" and hit.keyword:
        found = compile_user_pattern(hit.keyword, ignore_case=ignore_case).search(text)
        if found is not None:
            primary["full_match"] = found.group(0)
            primary["span"] = list(found.span())
    return primary


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


@router.put("/config/{name}/forward-exclude-chats")
async def api_forward_set_exclude_chats(
    name: str,
    payload: dict[str, Any],
    store=Depends(get_store),
) -> dict[str, Any]:
    """账号级「排除频道」列表：这些会话**所有规则**都不监听。

    与每条规则自己的 ``exclude_sources`` 互补：这里是写一次管全部的全局名单。

    转发目标**不需要**填在这里 —— ``PreparedRule.chat_allowed`` 会自动把目标会话
    排除掉（否则 ``sources=[]`` 全监听时，转发出去的新消息会被自己重新捕获，
    形成无限转发循环）。这个列表是留给"目标之外、同样不想监听"的会话的。
    """
    from tg_assistant.config import ForwardConfig

    _require_account(store, name)
    raw = payload.get("exclude_chats")
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="exclude_chats 必须是数组")

    config = store.load_account_config(name, create=False)
    # 交给模型校验（自动去 @ / 去空白 / 拒绝非法引用），避免在这里手写一遍归一化 ——
    # 「同一个判断抄两份」是这个项目踩过的坑。
    try:
        config.forward = ForwardConfig.model_validate(
            {**config.forward.model_dump(), "exclude_chats": raw}
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"排除频道校验失败：{exc}") from exc
    store.save_account_config(name, config)
    return {"ok": True, "exclude_chats": list(config.forward.exclude_chats)}


@router.put("/config/{name}/forward-exclude-users")
async def api_forward_set_exclude_users(
    name: str,
    payload: dict[str, Any],
    store=Depends(get_store),
) -> dict[str, Any]:
    """账号级「发送者黑名单」：这些人发的消息**所有规则**都不转发。

    与每条规则自己的 ``exclude_users`` 互补：这里是写一次管全部的全局名单。

    判定条件是「在黑名单里 **且** 命中规则」—— 转发结果上等价于"黑名单里的人
    发什么都不转发"（没命中的消息本来也不会转发），但日志只在"本来真要发出去"
    的那几条上留痕，不会被这个人的闲聊刷屏。
    """
    from tg_assistant.config import ForwardConfig

    _require_account(store, name)
    raw = payload.get("exclude_users")
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="exclude_users 必须是数组")

    config = store.load_account_config(name, create=False)
    # 与「排除频道」走同一套模型校验（自动去 @ / 去空白 / 拒绝非法引用），
    # 不在路由里手写第二份归一化 —— 「同一个判断抄两份」是这个项目踩过的坑。
    try:
        config.forward = ForwardConfig.model_validate(
            {**config.forward.model_dump(), "exclude_users": raw}
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"黑名单校验失败：{exc}") from exc
    store.save_account_config(name, config)
    return {"ok": True, "exclude_users": list(config.forward.exclude_users)}


@router.put("/forward-excludes")
async def api_set_forward_excludes(
    payload: dict[str, Any],
    store=Depends(get_store),
) -> dict[str, Any]:
    """**全局**排除名单（所有账号共用一份）：排除频道（群 id）+ 发送者黑名单。

    用户原话：「将转发规则的黑名单及排除的频道也做成全局的」、
    「我设置的群id就不要转给我了」。

    🔴 **为什么要跨账号一份**：2026-09-29 线上取证 —— 两个账号（小白 / SevenStar）
    的这两份名单**一模一样**（``-1003932130542`` / ``8817602576``）：同一份名单存了两遍，
    面板上还得一个账号填一次。加一个账号就多填一遍，漏填一个账号 = 那个号照转。

    与 ``/config/{name}/forward-exclude-*``（账号级）是**并集**关系，不是替换：
    这里写一次管所有账号（典型：那个刷屏频道），账号级那份留给"只有这个号要排除的"。

    命中这里的群 id 之后**既不转发、也不会推给你** —— 通知是转发成功之后才发的
    （见 ``ForwardEngine._send``），没有转发就没有通知。

    只更新 body 里出现的键（``exclude_chats`` / ``exclude_users``）：
    面板一次改一个名单，不必先读后写整份，免得两个名单互相覆盖。
    """
    from tg_assistant.config import ForwardExcludes
    from tg_assistant.forward_excludes import ForwardExcludeStore

    allowed = {"exclude_chats", "exclude_users"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise HTTPException(status_code=400, detail=f"不认识的字段：{'、'.join(unknown)}")
    if not payload:
        raise HTTPException(
            status_code=400, detail="至少要给一个名单（exclude_chats / exclude_users）"
        )

    exclude_store = ForwardExcludeStore(store.paths.forward_excludes_file)
    current = exclude_store.load()
    # 交给模型校验（数字 id 归一化成 int、去 @、去空白、拒绝重复）——
    # 与账号级那两份共用同一个类型，避免"同一个判断抄两份"。
    try:
        excludes = ForwardExcludes.model_validate(
            {**current.model_dump(mode="json"), **payload}
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"排除名单校验失败：{exc}") from exc
    exclude_store.save(excludes)
    return {"ok": True, **excludes.model_dump(mode="json")}


def _save_global_used_codes(store, raw: dict[str, Any]) -> dict[str, Any]:
    """在**现有全局配置**上做增量，校验后写回 ``data/forward_used_codes.json``。

    抽出来是因为新的全局端点和旧的账号级兼容端点都要走这同一套逻辑，
    「同一个判断抄两份」是这个项目踩过的坑。
    """
    from tg_assistant.config import ForwardUsedCodes
    from tg_assistant.forward_used_codes import ForwardUsedCodesStore
    from tg_assistant.matching import compile_user_pattern

    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="used_codes 必须是对象")

    # 正则**先**单独编译一次，为的是错误信息：pydantic 会把校验失败糊成一大段
    # 英文（还带 input_value / type 之类），而这一项恰恰是用户手写、最容易写错的。
    # 范围（min_visible / ttl）留给模型判，不在路由里手写第二份阈值。
    candidate = raw.get("notice_pattern")
    if isinstance(candidate, str) and candidate:
        try:
            compile_user_pattern(candidate)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"通知正则表达式无效：{exc}") from exc

    used_store = ForwardUsedCodesStore(store.paths.forward_used_codes_file)
    current = used_store.load()
    # 🔴 在**现有值**上做增量，而不是拿 payload 从零构造：面板不一定把 persist 等
    # 每个字段都传上来，从零构造会把没传的字段悄悄打回默认值。顺带支持「只改一个字段」。
    base = current.model_dump()
    try:
        parsed = ForwardUsedCodes.model_validate({**base, **raw})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"已使用注册码配置校验失败：{exc}") from exc
    used_store.save(parsed)
    return parsed.model_dump(mode="json")


@router.put("/forward-used-codes")
async def api_set_forward_used_codes(
    payload: dict[str, Any],
    store=Depends(get_store),
) -> dict[str, Any]:
    """**全局**「已使用注册码」拦截设置（所有账号共用一份）。

    用户原话：「将……已使用注册码拦截做成全局，而不是账号级」。

    这些码频道会发「🎟️ 注册码使用 - jf [7002057019] 使用了 MSKY-30-Register_f1t░░░░░░░」，
    码尾被遮罩 —— 已经用掉的码再转出去，别人拿到的是一串用不了的字符。让引擎记住
    用过的码、命中就不再发；且**任一账号**认出的已用码，**所有账号**都不再转。

    🔴 **为什么跨账号一份**：2026-09-30 线上取证 —— 三个账号（小白 / SevenStar /
    只想睡觉）的这份策略**一字不差完全相同**，同一份策略存了三遍。写在
    ``data/forward_used_codes.json``，与「全局排除名单」同类。**不再**读账号级
    ``forward.used_codes``（那字段只为旧配置能加载而保留）。

    请求体两种写法都收：平铺字段，或 ``{"used_codes": {...}}``。
    """
    nested = payload.get("used_codes") if isinstance(payload, dict) else None
    raw: Any = payload if nested is None else nested
    saved = _save_global_used_codes(store, raw)
    return {"ok": True, "used_codes": saved}


@router.put("/config/{name}/forward-used-codes")
async def api_forward_set_used_codes(
    name: str,
    payload: dict[str, Any],
    store=Depends(get_store),
) -> dict[str, Any]:
    """【已弃用·转全局】账号级「已使用注册码」拦截设置的旧入口。

    「已用码拦截」已改成**全局一份**（见 :func:`api_set_forward_used_codes`）。
    这个带 ``{name}`` 的旧端点保留只为兼容老前端 / 老脚本：它**忽略账号名**、
    把配置写进全局那份 ``data/forward_used_codes.json`` —— 语义正是用户要的
    「一处配置、全账号一致」，不会再各写各的。返回体里带 ``scope="global"`` 提示。
    """
    # 账号名仍校验一下，好让"账号不存在"这类明显笔误照旧 404（而不是静默写全局）。
    _require_account(store, name)

    nested = payload.get("used_codes") if isinstance(payload, dict) else None
    raw: Any = payload if nested is None else nested
    saved = _save_global_used_codes(store, raw)
    return {"ok": True, "scope": "global", "used_codes": saved}


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
    # 「已使用注册码」拦截策略：**所有账号共用一份**（``data/forward_used_codes.json``），
    # 放在循环外只读一次盘。读失败时回退 ``ForwardUsedCodes`` 缺省值（= 启用 + 默认正则）。
    from tg_assistant.forward_used_codes import ForwardUsedCodesStore

    used_codes_store = ForwardUsedCodesStore(store.paths.forward_used_codes_file)
    global_used_codes = used_codes_store.load()
    accounts: list[dict[str, Any]] = []
    for record in store.load_registry().accounts:
        config = store.load_account_config(record.name, create=False)
        info = status.get(record.name, {})
        # 「已使用注册码」的运行统计由转发引擎放在 snapshot 的 forward 段里。
        # ⚠️ 三层都要防御：账号没在跑时 ``stats`` 是空 dict；引擎没起时 ``forward``
        # 是 ``None``；核心（forwarder）还没部署时那三个键**根本不存在** ——
        # 整个「转发规则」页共用这一个响应，为三个计数器把接口打成 500
        # 等于整页白屏，代价完全不对称。
        forward_stats = (info.get("stats") or {}).get("forward") or {}
        # 计数器的名字变过：最早定的是 ``used_learned`` / ``used_skipped`` /
        # ``used_known`` 三个平铺键，后来引擎把 ``UsedCodeStore.snapshot()``
        # （含 ``known`` / ``learned``）整个塞进了 ``forward["used_codes"]``。
        # 两种形状都认：顶层键读不到就退回嵌套快照，都取不到才是 0 ——
        # 「已记住」永远显示 0 时，用户会以为拦截没用（其实只是名字对不上）。
        used_store = forward_stats.get("used_codes") or {}
        accounts.append(
            {
                "name": record.name,
                "username": record.username,
                "display_name": record.display_name,
                "enabled": record.enabled,
                "running": bool(info.get("running")),
                "session_exists": bool(info.get("session_exists")),
                "forward_enabled": config.forward.enabled,
                "exclude_chats": list(config.forward.exclude_chats),
                "exclude_users": list(config.forward.exclude_users),
                # 「已用码拦截」已全局化，这里回填的也是**那份全局配置**（不是账号级）。
                # 保留这个键只为兼容老前端 / 老测试：它读到的就是实际生效的策略，
                # 语义上没有撒谎。新面板读的是响应顶层 ``global_used_codes``。
                "used_codes": global_used_codes.model_dump(mode="json"),
                # learned = 累计学到多少条（只增）；known = **当前**还记着多少条
                # （会因 TTL 到期而减少）。两者的名字很像，别配反了。
                "used_code_stats": {
                    "learned": forward_stats.get("used_learned")
                    or used_store.get("learned")
                    or 0,
                    "skipped": forward_stats.get("used_skipped") or 0,
                    "known": forward_stats.get("used_known")
                    or used_store.get("known")
                    or 0,
                },
                "rules": [rule.model_dump(mode="json") for rule in config.forward.rules],
            }
        )
    # 「全局排除」名单：**所有账号共用一份**（``data/forward_excludes.json``）。
    # 放在响应顶层而不是塞进每个账号：它本来就只有一份，塞进去就等于把
    # "两个号存着同一份名单"这个毛病原样搬到接口里（面板会渲染成页面顶部一个区块）。
    from tg_assistant.forward_excludes import ForwardExcludeStore

    exclude_store = ForwardExcludeStore(store.paths.forward_excludes_file)
    global_excludes = exclude_store.load()
    # 「已使用注册码」拦截策略：同样**所有账号共用一份**（``data/forward_used_codes.json``，
    # 已在上面读过一次）。放在响应顶层、与 ``global_excludes`` 并列 —— 面板把它渲染成
    # 页面顶部一个区块，不再一个账号一份。
    return {
        "accounts": accounts,
        "global_excludes": {
            **global_excludes.model_dump(mode="json"),
            # 读文件失败过几次。>0 说明磁盘上那份名单没读到，此刻是**按空名单**在跑 ——
            # 面板据此提示，而不是让用户对着一个"看起来配好了"的界面发呆。
            "load_errors": exclude_store.load_errors,
        },
        "global_used_codes": {
            **global_used_codes.model_dump(mode="json"),
            # 同理：>0 说明磁盘上那份策略没读到，此刻是**按缺省策略**在跑。
            "load_errors": used_codes_store.load_errors,
        },
    }


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
#: GET 会在每条任务上多塞的**只读**字段：PUT 时直接丢掉。
#:
#: 不丢的话 ``RedPacketTask`` 是 ``extra="forbid"`` 的，把 GET 的结果原样
#: PUT 回来会 400「Extra inputs are not permitted」，用户只看到"保存失败"。
_RED_PACKET_TASK_READONLY = ("ready", "problem")

#: GET 会在**顶层**多塞的只读字段：PUT 时丢掉。
#:
#: ``in_window`` 是「此刻在不在全局动手时段内」，``server_now`` 是「服务端现在几点」
#: —— 两者都随时间跳变，每次 GET 现算、不落盘；``RedPacketConfig`` 是
#: ``extra="forbid"`` 的，不丢就 400。
_RED_PACKET_READONLY = ("in_window", "server_now")


@router.get("/config/{name}/red_packet")
async def api_red_packet_get(name: str, store=Depends(get_store)) -> dict[str, Any]:
    _require_account(store, name)
    config = store.load_account_config(name, create=False).red_packet
    data = config.model_dump(mode="json")
    # 面板要按任务显示「配好了没有」，不用自己重复一遍判断逻辑。
    for item, task in zip(data["tasks"], config.tasks):
        item["ready"] = task.ready
        item["problem"] = task.problem
    # 全局时段：面板要能直接回答「为什么一晚上没动静」，以及给时间框一个
    # 「服务端现在几点」的对照（面板填的是服务端时区 Asia/Shanghai）。
    data["in_window"] = config.in_window
    data["server_now"] = datetime.now().strftime("%H:%M")
    return data


@router.put("/config/{name}/red_packet")
async def api_red_packet_put(
    name: str,
    payload: dict[str, Any],
    store=Depends(get_store),
) -> dict[str, Any]:
    from tg_assistant.config import RedPacketConfig

    _require_account(store, name)
    config = store.load_account_config(name, create=False)
    # 只读字段直接丢掉，让「GET 回来改一改再 PUT 回去」这种用法也能 work。
    body = {key: value for key, value in payload.items() if key not in _RED_PACKET_READONLY}
    body["tasks"] = [
        {key: value for key, value in task.items() if key not in _RED_PACKET_TASK_READONLY}
        for task in body.get("tasks", [])
    ]
    try:
        config.red_packet = RedPacketConfig.model_validate(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"配置校验失败：{exc}") from exc
    store.save_account_config(name, config)
    return {"ok": True, "tasks": len(config.red_packet.tasks)}


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
# 抢注任务
# --------------------------------------------------------------------------- #
#: GET 会在每条任务上多塞的**只读**字段：PUT 时直接丢掉。
#:
#: 不丢的话 ``RegGrabTask`` 是 ``extra="forbid"`` 的，把 GET 的结果原样
#: PUT 回来会 400「Extra inputs are not permitted」，用户只看到「保存失败」。
_REG_GRAB_TASK_READONLY = ("ready", "problem", "in_window")

#: GET 会在**顶层**多塞的只读字段：一样要在 PUT 时丢掉。
#:
#: ``server_now`` 是「服务端现在几点」，纯展示用；``RegGrabConfig`` 同样是
#: ``extra="forbid"``，带上去就 400。
_REG_GRAB_READONLY = ("server_now",)


@router.get("/config/{name}/reg_grab")
async def api_reg_grab_get(name: str, store=Depends(get_store)) -> dict[str, Any]:
    _require_account(store, name)
    config = store.load_account_config(name, create=False).reg_grab
    data = config.model_dump(mode="json")
    # 面板要按任务显示「配好了没有」和「此刻在不在时段内」，不用自己重复一遍判断逻辑。
    # 时段会随时间跳变 ⇒ 每次 GET 现算，不落盘。
    for item, task in zip(data["tasks"], config.tasks):
        item["ready"] = task.ready
        item["problem"] = task.problem
        item["in_window"] = task.in_window
    # 时间框里填的是服务端时区（Asia/Shanghai），用户本地时区不一致时，
    # 光看那两个时间框是发现不了的 —— 必须有个「现在几点」对照。
    data["server_now"] = datetime.now().strftime("%H:%M")
    return data


@router.put("/config/{name}/reg_grab")
async def api_reg_grab_put(
    name: str,
    payload: dict[str, Any],
    store=Depends(get_store),
) -> dict[str, Any]:
    from tg_assistant.config import RegGrabConfig

    _require_account(store, name)
    config = store.load_account_config(name, create=False)
    # 只读字段直接丢掉，让「GET 回来改一改再 PUT 回去」这种用法也能 work。
    body = {key: value for key, value in payload.items() if key not in _REG_GRAB_READONLY}
    body["tasks"] = [
        (
            {key: value for key, value in task.items() if key not in _REG_GRAB_TASK_READONLY}
            if isinstance(task, dict)
            else task
        )
        for task in body.get("tasks", [])
    ]
    try:
        config.reg_grab = RegGrabConfig.model_validate(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"配置校验失败：{exc}") from exc
    store.save_account_config(name, config)
    return {"ok": True, "tasks": len(config.reg_grab.tasks)}


@router.put("/config/{name}/reg_grab/enabled")
async def api_reg_grab_enabled(
    name: str,
    payload: dict[str, Any],
    store=Depends(get_store),
) -> dict[str, Any]:
    """总开关。

    开启时**拒绝**「一条配好的任务都没有」的配置 —— 否则用户打开开关后什么都
    不发生，日志里也只有一条容易被忽略的警告，很难判断到底哪里没填。

    ⚠️ 校验只到「至少有一条 :attr:`RegGrabTask.ready` 的任务」这一层，不再
    逐字段去挑刺：多任务下用户经常先把开关打开、再一条条慢慢填，卡得太死
    反而每次都要先编一条假的出来。
    """
    _require_account(store, name)
    config = store.load_account_config(name, create=False)
    enabled = bool(payload.get("enabled", False))
    ready = any(task.ready for task in config.reg_grab.tasks)
    if enabled and not ready:
        raise HTTPException(
            status_code=400,
            detail=(
                "还没有任何一条配置完整的抢注任务（每条都要有「注册码提取正则」"
                "和至少一条步骤），开了也不会执行。请先配好一条再打开开关。"
            ),
        )
    config.reg_grab.enabled = enabled
    store.save_account_config(name, config)
    return {"ok": True, "enabled": config.reg_grab.enabled, "ready": ready}


@router.post("/config/{name}/reg_grab/test_notify")
async def api_reg_grab_test_notify(
    name: str,
    store=Depends(get_store),
    runtime=Depends(get_runtime),
) -> dict[str, Any]:
    """用这个账号配置的机器人发一条测试通知。

    抢注是低频事件，等真抢到一次才知道通知通不通太慢；这里直接把同一条
    通路（同一个 notifier、同一个 ``reg_grab`` 事件）跑一遍，成没成一目了然。
    """
    from tg_assistant.matching import DEFAULT_REG_GRAB_TEMPLATE, render_template
    from tg_assistant.notify import NotifyTask

    _require_account(store, name)
    config = store.load_account_config(name, create=False)

    if not config.notify.enabled:
        raise HTTPException(
            status_code=400, detail="通知没启用。请先到「通知设置」里填好机器人 Token 并打开开关。"
        )
    if not config.notify.wants("reg_grab"):
        raise HTTPException(
            status_code=400,
            detail="通知事件里没勾「抢注通知」，这类消息不会发出去。请到「通知设置」里勾上。",
        )

    runner = runtime.running_runner(name)
    notifier = getattr(runner, "notifier", None) if runner is not None else None
    if notifier is None:
        raise HTTPException(
            status_code=400, detail=f"账号 {name} 当前没在运行，机器人还没起来，发不了测试通知。"
        )

    variables = {
        "result_icon": "🧪",
        "result_text": "测试通知",
        "code": "TEST-0000-Test_abcdef1234",
        "cost_ms": 0,
        "detail": "这是一条测试消息。能看到它，说明抢注结果能通过这个机器人推给你。",
        "steps": 0,
        "chain": "-",
        "chat_title": "测试",
        "sender": "系统",
        "text": "试发通知",
        "link": "",
    }
    text = render_template(config.notify.template or DEFAULT_REG_GRAB_TEMPLATE, variables)
    submitted = notifier.submit(
        NotifyTask(
            event="reg_grab",
            text=text,
            context={"result": "test", "code": variables["code"], "test": True},
        )
    )
    if not submitted:
        raise HTTPException(
            status_code=400, detail="通知没能进队列（可能队列已满或刚被限流），稍后再试。"
        )
    return {"ok": True, "detail": "测试通知已提交，去机器人那边看看收到没有。"}


@router.post("/reg_grab/test_extract")
async def api_reg_grab_test_extract(payload: dict[str, Any]) -> dict[str, Any]:
    """试跑「注册码提取正则」，把**到底抓到了什么**完整回给面板。

    为什么另开一个端点而不是复用 ``/rules/test``：转发页在用它，它返回的是
    「匹配到了没有 + 捕获组列表」，而抢注要回答的是一个更具体的问题 ——
    「引擎会把哪个值当成 ``{code}``，它是从第几个捕获组来的」。
    用户原话「我想要 XYING-Whitelist_4rLuucEgs5，但只匹配到 4rLuucEgs5」，
    答案就在 ``from_group`` 上：他的正则把捕获组只括在了后缀上。

    也不需要账号上下文：提取跟账号毫无关系，页面就不用先选账号。

    ⚠️ 这里**不改提取语义**（第一个非空捕获组，没有捕获组才用整段），只是把它
    显示出来 —— 改语义会一次性改坏所有靠捕获组拼 ``{code}`` 的老任务。
    """
    import re

    from tg_assistant.reg_grab import extract_code_detail

    pattern = str(payload.get("pattern", "") or "")
    text = str(payload.get("text", "") or "")
    if not pattern:
        return {"matched": False, "error": "正则表达式为空", "code": None}
    if not text:
        return {"matched": False, "error": "测试文本为空", "code": None}
    try:
        # 走引擎同一个 ``extract_code_detail``：编译标志、取组规则都跟真正动手时
        # 一模一样，否则测试器说"能抓"、引擎却抓不到，用户只会更困惑。
        return extract_code_detail(pattern, text)
    except re.error as exc:
        # 正则写错是用户最常见的输入错误，要给一句能看懂的话而不是 500。
        return {"matched": False, "error": f"正则表达式无效: {exc}", "code": None}


# --------------------------------------------------------------------------- #
# 抢注任务：任务维度（跨账号）
# --------------------------------------------------------------------------- #
#: 一条抢注任务在哪些账号里有副本。
#:
#: 与 :func:`_rule_owners` 同一个道理：任务的 id 是主键，面板改成任务维度之后
#: 「这条任务被写给了谁」必须由配置本身回答，而不是靠页面记住"当前账号"。
def _reg_grab_task_owners(store: Any, task_id: str) -> list[str]:
    owners: list[str] = []
    for record in store.load_registry().accounts:
        config = store.load_account_config(record.name, create=False)
        if any(task.id == task_id for task in config.reg_grab.tasks):
            owners.append(record.name)
    return owners


def _validate_reg_grab_task(
    payload: dict[str, Any],
    *,
    taken: set[str] | None = None,
    fallback_id: str | None = None,
) -> Any:
    """校验请求里的抢注任务，并给**留空的 id** 补一个。

    id 的生成规则在 :meth:`RegGrabConfig._assign_ids` 里 —— 只有父模型同时看得见
    所有任务，才能保证「同一次保存里生成的那些互不重复」。这里借它生成一个，
    而不是在 api.py 里再写一遍「名称取 ASCII 片段、否则随机」：两套规则迟早漂移，
    然后前端按一套猜、服务端按另一套存。

    ``taken`` 是**目标账号里已被占用的 id**（占用者用占位任务塞进父模型，让生成
    规则自己避让）。不传的话，同名的第二条任务会生成同一个 id，接着被判成"已存在"
    回 409 —— 用户看到的就是一句莫名其妙的"都已存在"。

    ``fallback_id`` 给 PUT 用：URL 里的 id 就是这条任务的身份，body 里留空时直接用
    它，不能另生成一个（那等于悄悄改名）。

    PUT 的 body 允许两种写法：``{task: {...}}``（与 POST 一致，页面对称）
    或整个 body 就是任务本身（``{id: ..., steps: [...]}``）。
    """
    from tg_assistant.config import RegGrabConfig, RegGrabTask

    raw = payload.get("task")
    if not isinstance(raw, dict):
        raw = payload
    # GET 会在任务上多塞 ready / problem / in_window，页面把卡片原样回传时它们会跟着
    # 回来；``RegGrabTask`` 是 extra="forbid" 的，不丢就 400。
    body = {key: value for key, value in raw.items() if key not in _REG_GRAB_TASK_READONLY}
    if fallback_id is not None and not str(body.get("id") or "").strip():
        body = {**body, "id": fallback_id}
    try:
        task = RegGrabTask.model_validate(body)
        if not task.id:
            placeholders = [RegGrabTask(id=each) for each in sorted(taken or ())]
            task = RegGrabConfig(tasks=[*placeholders, task]).tasks[-1]
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"任务校验失败：{exc}") from exc
    return task


@router.get("/reg_grab/overview")
async def api_reg_grab_overview(
    store=Depends(get_store),
    runtime=Depends(get_runtime),
) -> dict[str, Any]:
    """所有账号的抢注配置总览，供「任务维度」列表一次渲染完。

    页面不再有"当前账号"之后，逐账号拉配置不只是多几个请求：账号 A 已经落盘、
    账号 B 还没落盘时，页面会把**写了一半的状态**当成一份快照渲染出来。一次给全
    就没有这个中间态（与 ``GET /api/rules`` 同一个考虑）。
    """
    status = {item["name"]: item for item in runtime.account_status()}
    accounts: list[dict[str, Any]] = []
    for record in store.load_registry().accounts:
        config = store.load_account_config(record.name, create=False)
        info = status.get(record.name, {})
        reg_grab = config.reg_grab
        tasks: list[dict[str, Any]] = []
        for task in reg_grab.tasks:
            item = task.model_dump(mode="json")
            # 面板要按任务显示「配好了没有」「此刻在不在时段内」，不用自己重复一遍
            # 判断逻辑；时段随时间跳变 ⇒ 每次 GET 现算，不落盘。
            item["ready"] = task.ready
            item["problem"] = task.problem
            item["in_window"] = task.in_window
            tasks.append(item)
        accounts.append(
            {
                "name": record.name,
                "username": record.username,
                "display_name": record.display_name,
                "enabled": record.enabled,
                "running": bool(info.get("running")),
                "session_exists": bool(info.get("session_exists")),
                # 账号级开关与并发：字段名带前缀，免得和上面的账号自身 enabled 混淆
                "reg_grab_enabled": reg_grab.enabled,
                "max_concurrency": reg_grab.max_concurrency,
                "tasks": tasks,
            }
        )
    return {
        "accounts": accounts,
        # 时间框里填的是服务端时区（Asia/Shanghai），跟哪个账号无关 ⇒ 顶层一份。
        "server_now": datetime.now().strftime("%H:%M"),
    }


@router.post("/reg_grab/tasks")
async def api_reg_grab_task_create(
    payload: dict[str, Any],
    store=Depends(get_store),
) -> dict[str, Any]:
    """新增抢注任务；``accounts`` 为空表示写入**全部账号**（扇出）。

    同 id 已存在的账号会被**跳过**而不是整单失败 —— 否则「把这条任务发给全部
    账号」在某个账号里已经手工加过时就完全用不了了。全都冲突才返回 409。
    """
    targets = _resolve_rule_accounts(store, payload.get("accounts"))
    if not targets:
        raise HTTPException(status_code=400, detail="还没有任何账号，无法保存抢注任务")
    # 目标账号里已占用的 id 交给生成规则避让（见 _validate_reg_grab_task 的 taken）。
    # 空 id 不算"占用"：它本身就会被配置层的生成规则补上。
    taken: set[str] = set()
    for name in targets:
        taken.update(
            task.id
            for task in store.load_account_config(name, create=False).reg_grab.tasks
            if task.id
        )
    task = _validate_reg_grab_task(payload, taken=taken)

    saved: list[str] = []
    conflicts: list[str] = []
    for name in targets:
        config = store.load_account_config(name, create=False)
        if any(existing.id == task.id for existing in config.reg_grab.tasks):
            conflicts.append(name)
            continue
        config.reg_grab.tasks.append(task)
        store.save_account_config(name, config)
        saved.append(name)

    if not saved:
        raise HTTPException(
            status_code=409,
            detail=f"任务 id {task.id!r} 在这些账号里都已存在：{'、'.join(conflicts)}",
        )
    return {
        "ok": True,
        "saved": saved,
        "conflicts": conflicts,
        "task": task.model_dump(mode="json"),
    }


@router.put("/reg_grab/tasks/{task_id}")
async def api_reg_grab_task_update(
    task_id: str,
    payload: dict[str, Any],
    store=Depends(get_store),
) -> dict[str, Any]:
    """覆盖一条抢注任务；改的是**所有拥有它的账号**里的那一份。

    卡片上的启停开关走这里。为什么不发 N 个 per-account PUT：第 3 个账号失败时
    前 2 个已经落盘了 —— 用户看到报错，却有一半账号已经变了，界面与磁盘不一致。
    （与 ``PUT /api/rules/{id}`` 同一个语义。）
    """
    task = _validate_reg_grab_task(payload, fallback_id=task_id)
    owners = _reg_grab_task_owners(store, task_id)
    if not owners:
        raise HTTPException(status_code=404, detail=f"抢注任务 {task_id!r} 不存在")

    # 改 id 等于换一条任务：新 id 在该账号里已被占用时直接拒绝。写出重复 id 的后果
    # 不是「多一条任务」而是**同一条消息被处理两次**。
    if task.id != task_id:
        for name in owners:
            config = store.load_account_config(name, create=False)
            if any(existing.id == task.id for existing in config.reg_grab.tasks):
                raise HTTPException(
                    status_code=409,
                    detail=f"账号 {name} 里已存在 id {task.id!r} 的抢注任务，无法改名",
                )

    updated: list[str] = []
    missing: list[str] = []
    for name in owners:
        config = store.load_account_config(name, create=False)
        index = next(
            (i for i, existing in enumerate(config.reg_grab.tasks) if existing.id == task_id),
            None,
        )
        if index is None:
            missing.append(name)
            continue
        config.reg_grab.tasks[index] = task
        store.save_account_config(name, config)
        updated.append(name)

    if not updated:
        raise HTTPException(status_code=404, detail=f"抢注任务 {task_id!r} 不存在")
    return {
        "ok": True,
        "updated": updated,
        "missing": missing,
        "task": task.model_dump(mode="json"),
    }


@router.delete("/reg_grab/tasks/{task_id}")
async def api_reg_grab_task_delete(
    task_id: str,
    accounts: str | None = Query(
        None, description="只从这些账号删除（逗号分隔）；缺省 = 所有拥有它的账号"
    ),
    store=Depends(get_store),
) -> dict[str, Any]:
    """删除抢注任务；``accounts`` 缺省表示从**所有拥有它的账号**里删掉。

    面板上的「删除」= 全删（与规则页一致），只想让某个账号不跑这条任务时用
    卡片上的「监听账号」取消那一个账号。
    """
    if accounts:
        targets = _resolve_rule_accounts(store, accounts)
    else:
        targets = _reg_grab_task_owners(store, task_id)

    removed: list[str] = []
    missing: list[str] = []
    for name in targets:
        config = store.load_account_config(name, create=False)
        before = len(config.reg_grab.tasks)
        config.reg_grab.tasks = [task for task in config.reg_grab.tasks if task.id != task_id]
        if len(config.reg_grab.tasks) == before:
            missing.append(name)
            continue
        store.save_account_config(name, config)
        removed.append(name)

    if not removed:
        raise HTTPException(status_code=404, detail=f"抢注任务 {task_id!r} 不存在")
    return {"ok": True, "removed": removed, "missing": missing}


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

    ``notify``（「更新后发送通知」复选框）和 ``real_time_listen`` 一样，
    由面板提交、经下面的 ``model_validate`` 整体写回 —— 不需要单独分支；
    请求体里没带该字段时沿用模型默认值 ``True``。
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


async def _cf_make_source(
    name: str,
    store: Any,
    settings: Any,
    runtime: Any,
    *,
    what: str,
) -> tuple[Any, Any]:
    """给优选 IP 的「测试抓取」/「立即触发」拿一个能读频道消息的 source。

    返回 ``(source, client_to_stop)``。``client_to_stop is None`` 表示复用了
    正在运行的账号 client，调用方**不要**去 stop 它。

    ⚠️ 日志和超时缺一不可，两条都是踩出来的：

    * **日志**：线上出现过前端一直停在「抓取中...」、而服务端**一条日志都没有**
      —— 连「请求到底有没有到服务端」都判断不了。所以这里进出都打日志。
    * **超时**：临时新建的 client 要抢同一个 ``.session``，一旦卡在连接/登录上，
      请求就永远不返回，前端按钮也就永远停在「抓取中...」，只能刷新页面。
    """
    from tg_assistant.cloudflare_ip import (
        make_message_source,
        make_message_source_from_account,
    )

    running = runtime._running_accounts() if hasattr(runtime, "_running_accounts") else set()
    if name in running and runtime._runner is not None:
        for runner in runtime._runner.runners.values():
            if runner.name == name and runner.client is not None:
                log.info(
                    "%s：复用正在运行的 client",
                    what,
                    extra={"account": name, "extra_fields": {}},
                )
                return make_message_source(runner.client), None

    log.info(
        "%s：没有可复用的 client，临时新建一个",
        what,
        extra={"account": name, "extra_fields": {"timeout_s": CLIENT_TIMEOUT}},
    )
    try:
        client, source = await asyncio.wait_for(
            make_message_source_from_account(name, store, settings),
            timeout=CLIENT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        log.warning(
            "%s：临时新建 client 超时",
            what,
            extra={"account": name, "extra_fields": {"timeout_s": CLIENT_TIMEOUT}},
        )
        raise HTTPException(
            status_code=504,
            detail=(
                f"创建 Telegram client 超时（{CLIENT_TIMEOUT:.0f} 秒）。"
                "账号可能正被其它任务占用，稍后重试。"
            ),
        ) from None
    return source, client


@router.post("/config/{name}/cloudflare_ip/trigger")
async def api_cloudflare_ip_trigger(
    name: str,
    store=Depends(get_store),
    settings=Depends(get_settings),
    runtime=Depends(get_runtime),
) -> dict[str, Any]:
    """手动触发一次优选 IP 抓取 + DNS 更新。"""
    from tg_assistant.cloudflare_ip import (
        fetch_and_update,
        persist_run,
        summary_to_last_result,
    )

    started = time.monotonic()
    log.info("收到「立即触发」请求", extra={"account": name, "extra_fields": {}})

    _require_account(store, name)
    account_config = store.load_account_config(name, create=False)
    cf_config = account_config.cloudflare_ip

    if not cf_config.api_token or not cf_config.source_channel:
        raise HTTPException(status_code=400, detail="缺少 api_token 或 source_channel")

    source, client_to_stop = await _cf_make_source(
        name, store, settings, runtime, what="立即触发"
    )

    try:
        from tg_assistant.proxy import resolve_proxy

        record = store.require_account(name)
        proxy = resolve_proxy(record, settings)
        state = store.load_state(name)

        try:
            summary = await asyncio.wait_for(
                fetch_and_update(cf_config, source, state, proxy),
                timeout=FETCH_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log.warning(
                "「立即触发」抓取/更新超时",
                extra={"account": name, "extra_fields": {"timeout_s": FETCH_TIMEOUT}},
            )
            raise HTTPException(
                status_code=504,
                detail=f"抓取或写入超时（{FETCH_TIMEOUT:.0f} 秒），稍后重试。",
            ) from None
        # ⚠️ 「上次结果」和「上次更新」都要落盘，且必须和定时调度、实时监听
        # 共用 persist_run —— 少写一处，用户点完「立即触发」看到的就是
        # 上一次**调度**留下的陈旧结果，会以为功能没生效。
        persist_run(store, name, state, summary, cf_config)
    finally:
        if client_to_stop is not None:
            with contextlib.suppress(Exception):
                await client_to_stop.stop(block=True)

    # ⚠️ ``ok`` / ``skipped`` / 各种计数都**从这里取**，别在这再拼一份 ——
    # 面板读的是 ``persist_run`` 落盘的同一份数据，两处各算各的迟早会打架
    # （原来这里自己写了一遍 ``summary.all_ok and not summary.skipped``，
    # 于是「写了 1 条、跳过 2 家」时接口说失败、面板说未写入，而实际写成功了）。
    shared = summary_to_last_result(summary, cf_config)
    log.info(
        "「立即触发」完成",
        extra={
            "account": name,
            "extra_fields": {
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                "reused_client": client_to_stop is None,
                "ok_count": shared["ok_count"],
                "failed_count": shared["failed_count"],
                "skipped_count": shared["skipped_count"],
            },
        },
    )
    return {
        "ok": shared["ok"],
        "skipped": shared["skipped"],
        "skipped_reason": shared["skipped_reason"],
        "ok_count": shared["ok_count"],
        "failed_count": shared["failed_count"],
        "skipped_count": shared["skipped_count"],
        "records_count": shared["records_count"],
        "split_by_isp": cf_config.split_by_isp,
        "fastest_ip": summary.fetched.fastest,
        "fastest_speed": summary.fetched.fastest_speed,
        "all_ips": summary.fetched.all_ips,
        "all_speeds": summary.fetched.all_speeds,
        "current_speed": state.get("cloudflare_ip_last_speed"),
        "decisions": shared["decisions"],
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
    """测试抓取频道消息并解析 IP，展示决策结果（不更新 DNS）。

    ⚠️ 进出都要记日志、两条路径都要有超时。线上出现过前端一直停在
    「抓取中...」而服务端**一条日志都没有**，连请求到没到都判断不了。
    """
    from tg_assistant.cloudflare_ip import (
        fetch_ips_from_channel,
        should_update,
    )

    started = time.monotonic()
    log.info("收到「测试抓取」请求", extra={"account": name, "extra_fields": {}})

    _require_account(store, name)
    account_config = store.load_account_config(name, create=False)
    cf_config = account_config.cloudflare_ip

    if not cf_config.source_channel:
        raise HTTPException(status_code=400, detail="缺少 source_channel")

    source, client_to_stop = await _cf_make_source(
        name, store, settings, runtime, what="测试抓取"
    )

    try:
        try:
            fetched = await asyncio.wait_for(
                fetch_ips_from_channel(
                    cf_config.source_channel,
                    cf_config.fetch_limit,
                    source,
                ),
                timeout=FETCH_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log.warning(
                "「测试抓取」抓取频道超时",
                extra={"account": name, "extra_fields": {"timeout_s": FETCH_TIMEOUT}},
            )
            raise HTTPException(
                status_code=504,
                detail=f"抓取频道超时（{FETCH_TIMEOUT:.0f} 秒），稍后重试。",
            ) from None
    finally:
        if client_to_stop is not None:
            with contextlib.suppress(Exception):
                await client_to_stop.stop(block=True)

    # 决策预览
    state = store.load_state(name)
    decision = should_update(fetched, cf_config, state) if fetched.has_ip else None

    log.info(
        "「测试抓取」完成",
        extra={
            "account": name,
            "extra_fields": {
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                "reused_client": client_to_stop is None,
                "has_ip": fetched.has_ip,
                "ips": len(fetched.all_ips or []),
            },
        },
    )

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
        "notify": cf_config.notify,
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
    """启动一个账号；**已经在跑的会被重启**（让改过的转发规则生效）。

    走 :meth:`RuntimeManager.start_account`，而不是 ``start([name])`` ——
    后者在已有账号运行时只会返回「已经在运行中」，等于这个端点没法用。

    ⚠️ 账号的转发规则只在启动时读一次，所以「改完规则 → 点启动」必须能真的
    把规则重新加载进去。这条路径以前返回 ``ok=False``，线上真的让用户以为
    「功能坏了」。
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
