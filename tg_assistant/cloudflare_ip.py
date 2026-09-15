"""从 Telegram 频道抓取优选 IP，自动更新 Cloudflare DNS 记录。

核心决策流程（:func:`should_update`）：

1. 从频道消息解析出最快 IP 及其速度（MB/s）；
2. 速度 < ``min_speed_threshold`` → 放弃；
3. ``only_update_if_faster`` 开启时，与「上次更新时记录的速度」对比，
   ≤ 现在 → 放弃；
4. 通过全部检查 → 执行 DNS 更新，并把新速度写入 ``state.json`` 供下次对比。

调用方只需把 ``state`` 字典传来、决策通过后自己落盘即可，本模块不直接碰
``store``，保持轻量、可单独测试。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

import httpx

from .config import CloudflareDNSRecord, CloudflareIPConfig
from .logging_setup import get_logger
from .proxy import ProxyConfig, httpx_proxy

log = get_logger("cloudflare_ip")

#: 匹配 IPv4 地址
_IPV4_PATTERN = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")

#: 匹配「最快」行：⚡️ 最快：<IP> <speed> MB/s ...
_FASTEST_PATTERN = re.compile(
    r"⚡️\s*最快[：:]\s*(\d{1,3}(?:\.\d{1,3}){3})\s+([\d.]+)\s*MB/s"
)

#: 匹配每行里 IP + 速度（用于提取全部 IP 作候选）
_LINE_IP_PATTERN = re.compile(
    r"^(\d{1,3}(?:\.\d{1,3}){3})\s+[█\s\w]*\s+([\d.]+)\s*MB/s",
    re.MULTILINE,
)


@dataclass
class IPFetchResult:
    """从频道消息里解析出的 IP 结果。"""

    fastest: Optional[str] = None
    fastest_speed: Optional[float] = None
    all_ips: list[str] = None  # type: ignore[assignment]
    all_speeds: dict[str, float] = None  # type: ignore[assignment]
    raw_text: Optional[str] = None
    message_id: Optional[int] = None

    def __post_init__(self) -> None:
        if self.all_ips is None:
            self.all_ips = []
        if self.all_speeds is None:
            self.all_speeds = {}

    @property
    def has_ip(self) -> bool:
        return self.fastest is not None


@dataclass
class DNSUpdateResult:
    """单条 DNS 记录的更新结果。"""

    domain: str
    name: str
    record_type: str
    ip: str
    ok: bool
    record_id: Optional[str] = None
    error: Optional[str] = None


@dataclass
class UpdateSummary:
    """一次完整更新操作的汇总。"""

    fetched: IPFetchResult
    results: list[DNSUpdateResult] = None  # type: ignore[assignment]
    updated_at: Optional[str] = None
    skipped_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if self.results is None:
            self.results = []

    @property
    def all_ok(self) -> bool:
        return bool(self.results) and all(r.ok for r in self.results)

    @property
    def changed_count(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def skipped(self) -> bool:
        return self.skipped_reason is not None


@dataclass
class IPUpdateDecision:
    """决策结果：是否应该更新，以及原因。"""

    should_update: bool
    ip: Optional[str] = None
    speed: Optional[float] = None
    reason: str = ""
    current_speed: Optional[float] = None


def _is_valid_ipv4(ip: str) -> bool:
    """简单校验 IPv4 地址合法性。"""
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        try:
            num = int(part)
        except ValueError:
            return False
        if num < 0 or num > 255:
            return False
    return True


def parse_ips_from_text(text: str) -> IPFetchResult:
    """从频道消息文本中解析 IP 列表。

    优先识别 ``⚡️ 最快：<IP> <speed> MB/s`` 标记的行；
    如果没有这个标记，退化成取消息里出现的第一个合法 IPv4。
    """
    result = IPFetchResult(raw_text=text)

    if not text:
        return result

    # 优先：找「最快」+ 速度
    fastest_match = _FASTEST_PATTERN.search(text)
    if fastest_match:
        candidate = fastest_match.group(1)
        speed = float(fastest_match.group(2))
        if _is_valid_ipv4(candidate):
            result.fastest = candidate
            result.fastest_speed = speed

    # 收集所有 IP + 速度（按出现顺序、去重）
    seen: set[str] = set()
    for match in _LINE_IP_PATTERN.finditer(text):
        ip = match.group(1)
        speed = float(match.group(2))
        if ip not in seen and _is_valid_ipv4(ip):
            seen.add(ip)
            result.all_ips.append(ip)
            result.all_speeds[ip] = speed

    # 兜底：用通用正则再扫一遍（有些消息格式不标准，没有 MB/s 行）
    if not result.all_ips:
        for match in _IPV4_PATTERN.finditer(text):
            ip = match.group(1)
            if ip not in seen and _is_valid_ipv4(ip):
                seen.add(ip)
                result.all_ips.append(ip)

    # 没找到「最快」标记时，退化取列表第一个
    if result.fastest is None and result.all_ips:
        result.fastest = result.all_ips[0]
        result.fastest_speed = result.all_speeds.get(result.fastest)

    return result


def should_update(
    fetched: IPFetchResult,
    config: CloudflareIPConfig,
    state: dict[str, Any],
) -> IPUpdateDecision:
    """决策：这次解析出的 IP 是否值得更新 DNS。

    ``state`` 是 ``store.load_state(account)`` 的返回值，用于读取
    ``cloudflare_ip_last_speed`` 等历史数据。
    """
    if not fetched.has_ip:
        return IPUpdateDecision(False, reason="频道消息未解析到任何 IP")

    ip = fetched.fastest
    speed = fetched.fastest_speed or 0.0

    # 阈值检查
    if config.min_speed_threshold > 0 and speed < config.min_speed_threshold:
        return IPUpdateDecision(
            False,
            ip=ip,
            speed=speed,
            reason=f"速度 {speed:.2f} MB/s 低于阈值 {config.min_speed_threshold:.2f} MB/s",
        )

    # 对比检查
    if config.only_update_if_faster:
        current_speed = state.get("cloudflare_ip_last_speed")
        if current_speed is not None and speed <= float(current_speed):
            return IPUpdateDecision(
                False,
                ip=ip,
                speed=speed,
                current_speed=float(current_speed),
                reason=f"速度 {speed:.2f} MB/s 未超过当前 {float(current_speed):.2f} MB/s",
            )

    # 通过
    reason_parts = [f"IP={ip}, 速度={speed:.2f} MB/s"]
    if config.min_speed_threshold > 0:
        reason_parts.append(f"≥ 阈值 {config.min_speed_threshold:.2f}")
    if config.only_update_if_faster:
        current = state.get("cloudflare_ip_last_speed")
        if current is not None:
            reason_parts.append(f"> 当前 {float(current):.2f}")
        else:
            reason_parts.append("（首次更新）")

    return IPUpdateDecision(True, ip=ip, speed=speed, reason="，".join(reason_parts))


async def fetch_ips_from_channel(
    source_channel: Any,
    fetch_limit: int,
    message_source: Callable,
) -> IPFetchResult:
    """从 Telegram 频道拉取最近消息，返回解析出的 IP。"""
    texts: list[str] = []
    try:
        texts = await message_source(source_channel, fetch_limit)
    except Exception as exc:
        log.error("从频道抓取消息失败: %s", exc)
        return IPFetchResult()

    if not texts:
        log.warning("频道没有返回任何消息")
        return IPFetchResult()

    # 从新到旧逐条找，第一条包含 IP 的就采用
    for text in reversed(texts):
        parsed = parse_ips_from_text(text)
        if parsed.has_ip:
            return parsed

    return IPFetchResult(raw_text=texts[0] if texts else None)


async def _resolve_record(
    http: httpx.AsyncClient,
    api_token: str,
    zone_id: str,
    domain: str,
    name: str,
    record_type: str,
) -> Optional[str]:
    """查询现有 DNS 记录 ID。返回 None 表示记录不存在。"""
    fqdn = domain if name == "@" else f"{name}.{domain}"
    url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records"
    params = {"name": fqdn, "type": record_type}

    response = await http.get(url, params=params)
    data = response.json()

    if not data.get("ok"):
        log.warning("查询 DNS 记录失败: %s", data.get("errors"))
        return None

    records = data.get("result", [])
    if records:
        return records[0].get("id")
    return None


async def _update_single_record(
    http: httpx.AsyncClient,
    api_token: str,
    record: CloudflareDNSRecord,
    ip: str,
) -> DNSUpdateResult:
    """更新（或创建）一条 Cloudflare DNS 记录。"""
    fqdn = record.domain if record.name == "@" else f"{record.name}.{record.domain}"
    url = f"https://api.cloudflare.com/client/v4/zones/{record.zone_id}/dns_records"
    payload: dict[str, Any] = {
        "type": record.record_type,
        "name": fqdn,
        "content": ip,
        "ttl": record.ttl,
        "proxied": record.proxied,
    }

    record_id = await _resolve_record(
        http, api_token, record.zone_id, record.domain, record.name, record.record_type
    )

    try:
        if record_id:
            resp = await http.put(f"{url}/{record_id}", json=payload)
        else:
            resp = await http.post(url, json=payload)

        data = resp.json()

        if data.get("ok"):
            log.info("DNS 记录更新成功: %s %s → %s", fqdn, record.record_type, ip)
            return DNSUpdateResult(
                domain=record.domain,
                name=record.name,
                record_type=record.record_type,
                ip=ip,
                ok=True,
                record_id=data.get("result", {}).get("id", record_id),
            )
        else:
            errors = data.get("errors", [])
            error_msg = "; ".join(e.get("message", str(e)) for e in errors) or "未知错误"
            log.error("DNS 记录更新失败 %s: %s", fqdn, error_msg)
            return DNSUpdateResult(
                domain=record.domain,
                name=record.name,
                record_type=record.record_type,
                ip=ip,
                ok=False,
                error=error_msg,
            )
    except httpx.HTTPError as exc:
        log.error("Cloudflare API 请求失败 %s: %s", fqdn, exc)
        return DNSUpdateResult(
            domain=record.domain,
            name=record.name,
            record_type=record.record_type,
            ip=ip,
            ok=False,
            error=f"网络错误: {exc}",
        )


async def update_dns_records(
    config: CloudflareIPConfig,
    ip: str,
    proxy: Optional[ProxyConfig] = None,
) -> list[DNSUpdateResult]:
    """把 ``ip`` 写入配置中的所有 Cloudflare DNS 记录。"""
    results: list[DNSUpdateResult] = []
    proxy_url = httpx_proxy(proxy)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        proxy=proxy_url,
        headers={
            "Authorization": f"Bearer {config.api_token}",
            "Content-Type": "application/json",
        },
    ) as http:
        for record in config.records:
            result = await _update_single_record(
                http, config.api_token, record, ip
            )
            results.append(result)

    return results


async def fetch_and_update(
    config: CloudflareIPConfig,
    message_source: Callable,
    state: dict[str, Any],
    proxy: Optional[ProxyConfig] = None,
) -> UpdateSummary:
    """完整流程：抓取频道 IP → 决策 → 条件更新 DNS 记录。

    ``message_source``::

        async def source(channel: ChatRef, limit: int) -> list[str]

    ``state`` 是 ``store.load_state(account)`` 的返回值。
    更新成功后会自动把速度写入 state，但不会替你 ``save_state``。
    """
    from .config import utc_now_iso

    summary = UpdateSummary(
        fetched=IPFetchResult(),
        updated_at=utc_now_iso(),
    )

    # 1. 抓 IP
    fetched = await fetch_ips_from_channel(
        config.source_channel,
        config.fetch_limit,
        message_source,
    )
    summary.fetched = fetched

    if not fetched.has_ip:
        summary.skipped_reason = "未能从频道解析到任何 IP"
        log.error(summary.skipped_reason)
        return summary

    # 2. 决策
    decision = should_update(fetched, config, state)
    if not decision.should_update:
        summary.skipped_reason = decision.reason
        log.info("Cloudflare IP 决策跳过: %s", decision.reason)
        return summary

    log.info("Cloudflare IP 决策通过: %s（%s）", decision.ip, decision.reason)

    # 3. 更新 DNS
    results = await update_dns_records(config, decision.ip, proxy)
    summary.results = results

    if summary.all_ok:
        state["cloudflare_ip_last_speed"] = decision.speed
        state["cloudflare_ip_last_ip"] = decision.ip
        log.info(
            "DNS 更新完成: %d/%d 成功，速度 %.2f MB/s 已记录",
            summary.changed_count,
            len(results),
            decision.speed or 0,
        )
    else:
        log.error(
            "DNS 更新部分失败: %d/%d 成功",
            summary.changed_count,
            len(results),
        )

    return summary


def make_message_source(client: Any) -> Callable:
    """把一个已登录的 pyrogram Client 包装成 ``message_source`` callable。"""

    async def _source(channel: Any, limit: int) -> list[str]:
        texts: list[str] = []
        async for message in client.get_chat_history(channel, limit=limit):
            text = message.text or message.caption or ""
            if text:
                texts.append(text)
        return texts

    return _source


async def make_message_source_from_account(
    name: str,
    store: Any,
    settings: Any,
) -> tuple[Any, Callable]:
    """临时创建一个无更新流的 Client 作为消息源。

    返回 ``(client, source)`` 二元组，调用方负责 ``client.stop()``。
    """
    from .client import build_client

    record = store.require_account(name)
    client = build_client(record, settings, store.paths, no_updates=True)
    await client.start()
    source = make_message_source(client)
    return client, source


__all__ = [
    "DNSUpdateResult",
    "IPFetchResult",
    "IPUpdateDecision",
    "UpdateSummary",
    "fetch_and_update",
    "fetch_ips_from_channel",
    "make_message_source",
    "make_message_source_from_account",
    "parse_ips_from_text",
    "should_update",
    "update_dns_records",
]
