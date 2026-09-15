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

from .config import ISP_KEYS, CloudflareDNSRecord, CloudflareIPConfig
from .logging_setup import get_logger
from .proxy import ProxyConfig, httpx_proxy

log = get_logger("cloudflare_ip")

#: 运营商标识 → 中文名。落盘和 API 一律用英文 key，中文只出现在界面与日志里。
ISP_LABELS: dict[str, str] = {
    "mobile": "移动",
    "telecom": "电信",
    "unicom": "联通",
}

#: 频道消息首行的运营商标注，如「✅ Cloudflare 优选IP更新 (联通)」。
#: 这类频道是**一条消息只讲一个运营商**，所以想凑齐三网必须多看几条消息。
_ISP_LABEL_PATTERN = re.compile(r"[（(]\s*(移动|电信|联通)\s*[)）]")

#: 中文名 → 标识
_ISP_BY_LABEL = {label: key for key, label in ISP_LABELS.items()}

#: 写进 Cloudflare 记录 comment 的标记，用来在同一域名下区分三网的 A 记录。
#: 用 comment 而不是靠顺序：用户随时可能在 Cloudflare 后台删掉再重建记录，
#: 靠顺序会错位；comment 是记录自身的属性，重建后照样能重新认领。
ISP_COMMENT_PREFIX = "tg-assistant:isp="

#: state.json 里按运营商记录的上次速度 / IP（供 only_update_if_faster 比较）。
LAST_SPEED_BY_ISP_KEY = "cloudflare_ip_last_speed_by_isp"
LAST_IP_BY_ISP_KEY = "cloudflare_ip_last_ip_by_isp"

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
class IPUpdateDecision:
    """决策结果：是否应该更新，以及原因。"""

    should_update: bool
    ip: Optional[str] = None
    speed: Optional[float] = None
    reason: str = ""
    current_speed: Optional[float] = None


@dataclass
class ISPBest:
    """某个运营商在本次抓取里的最优结果。"""

    isp: str
    ip: str
    speed: float
    message_id: Optional[int] = None

    @property
    def label(self) -> str:
        return ISP_LABELS.get(self.isp, self.isp)


@dataclass
class IPFetchResult:
    """从频道消息里解析出的 IP 结果。"""

    fastest: Optional[str] = None
    fastest_speed: Optional[float] = None
    all_ips: list[str] = None  # type: ignore[assignment]
    all_speeds: dict[str, float] = None  # type: ignore[assignment]
    raw_text: Optional[str] = None
    message_id: Optional[int] = None
    #: 这条消息属于哪个运营商（``mobile``/``telecom``/``unicom``）；认不出是 ``None``。
    isp: Optional[str] = None
    #: 按运营商聚合出的最优结果，键是运营商标识。
    #: 只有 :func:`fetch_ips_from_channel` 会填 —— 单条消息解析
    #: （:func:`parse_ips_from_text`）只知道自己那一条，聚合是调用方的事。
    best_by_isp: dict[str, ISPBest] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.all_ips is None:
            self.all_ips = []
        if self.all_speeds is None:
            self.all_speeds = {}
        if self.best_by_isp is None:
            self.best_by_isp = {}

    @property
    def has_ip(self) -> bool:
        return self.fastest is not None

    @property
    def has_isp_split(self) -> bool:
        """是否至少认出了一个运营商的归属。"""
        return bool(self.best_by_isp)


@dataclass
class DNSUpdateResult:
    """单条 DNS 记录的更新结果。"""

    domain: str
    name: str
    record_type: str
    ip: Optional[str]
    ok: bool
    record_id: Optional[str] = None
    error: Optional[str] = None
    #: 三网分流时这条记录属于哪个运营商。
    isp: Optional[str] = None
    #: 本次**没有**动这条记录（拿不到该运营商的 IP、或本来就没配），
    #: 原因写在 ``error`` 里。
    skipped: bool = False
    #: 这条记录原本没有运营商标记，被本次更新「认领」成了某个运营商那条
    #: （避免同域名下残留一条游离的旧记录）。
    adopted: bool = False


@dataclass
class UpdateSummary:
    """一次完整更新操作的汇总。"""

    fetched: IPFetchResult
    results: list[DNSUpdateResult] = None  # type: ignore[assignment]
    updated_at: Optional[str] = None
    skipped_reason: Optional[str] = None
    #: 三网分流时，每个运营商各自的决策结果（键是运营商标识）。
    decisions: dict[str, IPUpdateDecision] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.results is None:
            self.results = []
        if self.decisions is None:
            self.decisions = {}

    @property
    def all_ok(self) -> bool:
        return bool(self.results) and all(r.ok for r in self.results)

    @property
    def changed_count(self) -> int:
        return sum(1 for r in self.results if r.ok and not r.skipped)

    @property
    def skipped(self) -> bool:
        return self.skipped_reason is not None


def summary_to_last_result(
    summary: "UpdateSummary", config: "CloudflareIPConfig"
) -> dict[str, Any]:
    """把一次更新的汇总压成存进 state、给面板「上次结果」用的字典。

    ⚠️ **定时调度（``web/runtime.py``）和手动「立即触发」（``web/routers/api.py``）
    必须共用这一个函数。** 原来只有调度器写 ``cloudflare_ip_last_result``，
    手动触发不写 —— 用户点完「立即触发」明明写成功了，面板上还挂着上一次调度
    留下的「失败」，看起来就像功能没生效。

    分流模式下 ``ok`` 只代表「三家都没被跳过」，所以另外给出
    ``ok_count`` / ``skipped_count`` / ``failed_count``，让面板能说清
    「3 条写成功」还是「1 条失败」，而不是拿单个运营商的速度冒充整体结果。
    """
    return {
        "ok": summary.all_ok and not summary.skipped,
        "skipped": summary.skipped,
        "skipped_reason": summary.skipped_reason,
        "ip": summary.fetched.fastest,
        "speed": summary.fetched.fastest_speed,
        "updated_at": summary.updated_at,
        "records_count": len(summary.results),
        "ok_count": sum(1 for r in summary.results if r.ok and not r.skipped),
        "failed_count": sum(1 for r in summary.results if not r.ok and not r.skipped),
        "skipped_count": sum(1 for r in summary.results if r.skipped),
        "split_by_isp": bool(config.split_by_isp),
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
    }


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


def _cf_ok(data: Any) -> bool:
    """判断 Cloudflare API v4 的响应是不是成功。

    🔴 **Cloudflare 返回的字段叫 ``success``，不叫 ``ok``。**

    这里原来两处都写成 ``data.get("ok")``（``_resolve_record`` 与
    ``_update_single_record``），于是恒为 ``None``、恒判失败。线上实测后果：

    * ``_resolve_record`` 永远返回 ``(None, False)`` → **每次都新建一条 A 记录，
      永远不去更新已有的那条**（记录会越堆越多，而且老的脏记录一直生效）；
    * ``_update_single_record`` 里记录其实**写成功了**，却报「未知错误」，
      面板显示 0/1 成功 —— 因为 ``errors`` 是空数组，连错误原因都拼不出来。

    实测响应：``{"result": [...], "success": true, "errors": [], ...}``。

    两个字段都认（``success`` 优先）是为了兼容测试里的桩数据，
    不改变「只要不是明确的成功就当失败」这个保守判定。
    """
    if not isinstance(data, dict):
        return False
    if "success" in data:
        return bool(data["success"])
    return bool(data.get("ok"))


def parse_isp(text: str) -> Optional[str]:
    """认出这条频道消息属于哪个运营商；认不出返回 ``None``。

    只看**首行**。这类频道的格式是「✅ Cloudflare 优选IP更新 (联通)」，
    标注固定在第一行；扫全文的话，正文里的城市名之类可能误伤。
    """
    if not text:
        return None
    first_line = text.split("\n", 1)[0]
    match = _ISP_LABEL_PATTERN.search(first_line)
    if not match:
        return None
    return _ISP_BY_LABEL.get(match.group(1))


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

    result.isp = parse_isp(text)

    return result


def threshold_for(config: CloudflareIPConfig, isp: Optional[str]) -> float:
    """取某个运营商的最低速度阈值；没单独配就回退到全局值。

    为什么需要按运营商分别设：三网的测速差距非常大 —— 同一天里电信能跑到
    166 MB/s、联通 80 MB/s，而移动最好只有 27 MB/s。共用一个阈值的话，
    移动那条记录会永远不更新。
    """
    if isp is not None:
        configured = config.min_speed_threshold_by_isp.get(isp)
        if configured is not None:
            return float(configured)
    return config.min_speed_threshold


def _decide(
    *,
    ip: Optional[str],
    speed: float,
    threshold: float,
    only_if_faster: bool,
    current_speed: Optional[float],
    label: str = "",
) -> IPUpdateDecision:
    """阈值 + 「是否更快」两条检查，分流与不分流共用。"""
    prefix = f"{label} " if label else ""

    if ip is None:
        return IPUpdateDecision(False, reason=f"{prefix}没有可用的 IP")

    if threshold > 0 and speed < threshold:
        return IPUpdateDecision(
            False,
            ip=ip,
            speed=speed,
            reason=f"{prefix}速度 {speed:.2f} MB/s 低于阈值 {threshold:.2f} MB/s",
        )

    if only_if_faster and current_speed is not None and speed <= float(current_speed):
        return IPUpdateDecision(
            False,
            ip=ip,
            speed=speed,
            current_speed=float(current_speed),
            reason=f"{prefix}速度 {speed:.2f} MB/s 未超过当前 {float(current_speed):.2f} MB/s",
        )

    reason_parts = [f"{prefix}IP={ip}", f"速度={speed:.2f} MB/s"]
    if threshold > 0:
        reason_parts.append(f"≥ 阈值 {threshold:.2f}")
    if only_if_faster:
        if current_speed is not None:
            reason_parts.append(f"> 当前 {float(current_speed):.2f}")
        else:
            reason_parts.append("（首次更新）")

    return IPUpdateDecision(True, ip=ip, speed=speed, reason="，".join(reason_parts))


def should_update(
    fetched: IPFetchResult,
    config: CloudflareIPConfig,
    state: dict[str, Any],
) -> IPUpdateDecision:
    """决策：这次解析出的「整体最快 IP」是否值得更新 DNS（不分流时用）。

    ``state`` 是 ``store.load_state(account)`` 的返回值，用于读取
    ``cloudflare_ip_last_speed`` 等历史数据。
    """
    if not fetched.has_ip:
        return IPUpdateDecision(False, reason="频道消息未解析到任何 IP")

    return _decide(
        ip=fetched.fastest,
        speed=fetched.fastest_speed or 0.0,
        threshold=config.min_speed_threshold,
        only_if_faster=config.only_update_if_faster,
        current_speed=state.get("cloudflare_ip_last_speed"),
    )


def should_update_isp(
    best: ISPBest,
    config: CloudflareIPConfig,
    state: dict[str, Any],
) -> IPUpdateDecision:
    """决策：某个运营商的这次结果是否值得更新它对应的那条 DNS 记录。

    与 :func:`should_update` 的区别是阈值和「上次速度」都**按运营商各算一份** ——
    否则移动永远过不了电信能轻松越过的阈值，或者三家互相把对方的记录比下去。
    """
    speed_map = state.get(LAST_SPEED_BY_ISP_KEY) or {}
    current = speed_map.get(best.isp) if isinstance(speed_map, dict) else None

    return _decide(
        ip=best.ip,
        speed=best.speed,
        threshold=threshold_for(config, best.isp),
        only_if_faster=config.only_update_if_faster,
        current_speed=float(current) if current is not None else None,
        label=f"[{best.label}]",
    )


def aggregate_messages(texts: list[str]) -> IPFetchResult:
    """把若干条频道消息聚合成一次抓取结果（纯函数，便于单测）。

    ``texts`` 按**从新到旧**排列，与 ``client.get_chat_history()`` 的顺序一致。

    聚合规则：

    - 每条消息先用 :func:`parse_ips_from_text` 解析出它自己那个「⚡️ 最快」IP；
    - 首行认得出运营商的，参与该运营商的最优评选（同运营商取速度最高的那条）；
    - 认不出运营商的（格式变了、或是三网汇总帖），只进候选池、不参与评选，
      但会作为「最新一条含 IP 的消息」用于兜底；
    - ``all_ips`` / ``all_speeds`` 是**所有消息里出现过的 IP 的并集**，
      同名 IP 保留见过的最高速度 —— 这是给「测试抓取」页展示用的候选池。
    """
    result = IPFetchResult()
    newest: Optional[IPFetchResult] = None
    all_speeds: dict[str, float] = {}

    for text in texts:
        parsed = parse_ips_from_text(text)
        if not parsed.has_ip:
            continue

        if newest is None:
            newest = parsed

        for ip, speed in parsed.all_speeds.items():
            if speed > all_speeds.get(ip, float("-inf")):
                all_speeds[ip] = speed

        isp = parsed.isp
        if isp is None:
            continue

        speed = parsed.fastest_speed or 0.0
        current = result.best_by_isp.get(isp)
        if current is None or speed > current.speed:
            result.best_by_isp[isp] = ISPBest(
                isp=isp,
                ip=parsed.fastest,  # type: ignore[arg-type]
                speed=speed,
                message_id=parsed.message_id,
            )

    result.all_speeds = all_speeds
    result.all_ips = list(all_speeds)

    if result.best_by_isp:
        top = max(result.best_by_isp.values(), key=lambda b: b.speed)
        result.fastest = top.ip
        result.fastest_speed = top.speed
    elif newest is not None:
        result.fastest = newest.fastest
        result.fastest_speed = newest.fastest_speed
        result.message_id = newest.message_id

    # raw_text 一律给「最新一条含 IP 的消息」，供面板预览用 ——
    # 聚合之后已经没有「那一条消息」了，给最新的最容易对照。
    result.raw_text = newest.raw_text if newest is not None else (texts[0] if texts else None)

    return result


async def fetch_ips_from_channel(
    source_channel: Any,
    fetch_limit: int,
    message_source: Callable,
) -> IPFetchResult:
    """从 Telegram 频道拉取最近消息，按运营商聚合出各自最快的 IP。

    ⚠️ 这里必须把 ``fetch_limit`` 条消息**全部**看一遍。
    原来只取「第一条含 IP 的消息」就返回，而这类频道（如 ``@cfyxip``）
    是**一条消息只讲一个运营商**的格式，于是永远只会拿到最近发过消息的
    那一家，另外两家一个都轮不到 —— 三网分流根本无从谈起。
    """
    texts: list[str] = []
    try:
        texts = await message_source(source_channel, fetch_limit)
    except Exception as exc:
        log.error("从频道抓取消息失败: %s", exc)
        return IPFetchResult()

    if not texts:
        log.warning("频道没有返回任何消息")
        return IPFetchResult()

    return aggregate_messages(texts)


async def _resolve_record(
    http: httpx.AsyncClient,
    api_token: str,
    zone_id: str,
    domain: str,
    name: str,
    record_type: str,
    *,
    comment: Optional[str] = None,
) -> tuple[Optional[str], bool]:
    """查询现有 DNS 记录 ID。

    返回 ``(record_id, adopted)``：

    - ``record_id`` 为 ``None`` 表示没有可复用的记录，调用方应该新建；
    - ``adopted`` 表示这次用的是**同域名下那条没有运营商标记的旧记录** ——
      首次开启三网分流时会出现这种情况（用户原来只有一条记录），
      认领它就不会在同域名下多留一条游离的旧记录。
      只有「恰好一条」无标记记录时才认领：多条说明情况不明，宁可新建，
      也不去猜该动哪一条。
    """
    fqdn = domain if name == "@" else f"{name}.{domain}"
    url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records"
    params = {"name": fqdn, "type": record_type}

    response = await http.get(url, params=params)
    data = response.json()

    if not _cf_ok(data):
        log.warning("查询 DNS 记录失败: %s", data.get("errors"))
        return None, False

    records = data.get("result", [])
    if comment is None:
        if records:
            return records[0].get("id"), False
        return None, False

    tagged = [r for r in records if (r.get("comment") or "") == comment]
    if tagged:
        return tagged[0].get("id"), False

    untagged = [r for r in records if not (r.get("comment") or "").strip()]
    if len(untagged) == 1:
        log.info(
            "复用同域名下已有的无标记记录作为 %s 那条: %s %s",
            comment,
            fqdn,
            untagged[0].get("id"),
        )
        return untagged[0].get("id"), True

    if len(untagged) > 1:
        log.warning(
            "%s 下已有 %d 条没有运营商标记的记录，无法判断该复用哪一条，"
            "本次新建。建议在 Cloudflare 后台清理后重试。",
            fqdn,
            len(untagged),
        )
    return None, False


async def _update_single_record(
    http: httpx.AsyncClient,
    api_token: str,
    record: CloudflareDNSRecord,
    ip: str,
    *,
    isp: Optional[str] = None,
    comment: Optional[str] = None,
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
    # 只有三网分流才带 comment：不分流时不去碰用户自己写的备注。
    if comment is not None:
        payload["comment"] = comment

    record_id, adopted = await _resolve_record(
        http,
        api_token,
        record.zone_id,
        record.domain,
        record.name,
        record.record_type,
        comment=comment,
    )

    def _fail(error: str) -> DNSUpdateResult:
        return DNSUpdateResult(
            domain=record.domain,
            name=record.name,
            record_type=record.record_type,
            ip=ip,
            ok=False,
            error=error,
            isp=isp,
        )

    try:
        if record_id:
            resp = await http.put(f"{url}/{record_id}", json=payload)
        else:
            resp = await http.post(url, json=payload)

        data = resp.json()

        if _cf_ok(data):
            log.info("DNS 记录更新成功: %s %s → %s", fqdn, record.record_type, ip)
            return DNSUpdateResult(
                domain=record.domain,
                name=record.name,
                record_type=record.record_type,
                ip=ip,
                ok=True,
                record_id=data.get("result", {}).get("id", record_id),
                isp=isp,
                adopted=adopted,
            )
        errors = data.get("errors", [])
        error_msg = "; ".join(e.get("message", str(e)) for e in errors) or "未知错误"
        log.error("DNS 记录更新失败 %s: %s", fqdn, error_msg)
        return _fail(error_msg)
    except httpx.HTTPError as exc:
        log.error("Cloudflare API 请求失败 %s: %s", fqdn, exc)
        return _fail(f"网络错误: {exc}")


def _dedupe_records(
    records: list[CloudflareDNSRecord],
) -> list[CloudflareDNSRecord]:
    """按 ``(zone_id, domain, name, record_type)`` 去掉配置里重复的记录。

    线上真实踩到过：配置里挂着**两条** ``7star.eu.cc / yx / A``，只有
    ``proxied`` 不一样（一条 false 一条 true）。不分流时写两遍同一份 IP
    还看不出问题，分流时会变成每条记录各展开三家 —— 同一个域名下反复
    创建/覆盖同一条记录，最后留下哪条、``proxied`` 取哪个全看顺序。

    保留**第一条**并告警。顺带一提：优选 IP 场景下 ``proxied`` 必须是
    ``false``（DNS only）—— 开了代理的话 Cloudflare 返回的是它自己的
    任播地址，写进去的优选 IP 根本不会生效。
    """
    seen: dict[tuple[str, str, str, str], CloudflareDNSRecord] = {}
    for record in records:
        key = (record.zone_id, record.domain, record.name, record.record_type)
        if key in seen:
            kept = seen[key]
            log.warning(
                "DNS 记录配置里有重复项 %s.%s (%s)：proxied=%s 与 proxied=%s 冲突，"
                "本次只按第一条（proxied=%s）处理，建议到面板里删掉多余的那条",
                record.name,
                record.domain,
                record.record_type,
                kept.proxied,
                record.proxied,
                kept.proxied,
            )
            continue
        seen[key] = record
    return list(seen.values())


def _plan_targets(
    config: CloudflareIPConfig,
    ip: Optional[str],
    ips_by_isp: Optional[dict[str, str]],
) -> list[tuple[CloudflareDNSRecord, Optional[str], Optional[str], Optional[str], Optional[str]]]:
    """算出每条记录该写哪个 IP。

    返回 ``(record, isp, target_ip, comment, skip_reason)`` 五元组列表：
    ``skip_reason`` 非空表示这次不动这条记录。

    ``ips_by_isp`` 有值时按三网分流处理 —— 同一个 ``domain``/``name`` 下
    每个运营商各写一条 A 记录，靠 ``comment`` 区分。
    """
    plan = []
    for record in _dedupe_records(config.records):
        if not ips_by_isp:
            plan.append((record, None, ip, None, None))
            continue

        for isp in ISP_KEYS:
            comment = f"{ISP_COMMENT_PREFIX}{isp}"
            target = ips_by_isp.get(isp)
            if target is None:
                # 拿不到就跳过 —— **绝不能拿别家的 IP 顶替**，
                # 否则移动的用户会被解析到电信的 IP 上去。
                plan.append(
                    (
                        record,
                        isp,
                        None,
                        comment,
                        f"本次抓取没有拿到{ISP_LABELS[isp]}的 IP，保留现有记录",
                    )
                )
            else:
                plan.append((record, isp, target, comment, None))
    return plan


async def update_dns_records(
    config: CloudflareIPConfig,
    ip: Optional[str],
    proxy: Optional[ProxyConfig] = None,
    *,
    ips_by_isp: Optional[dict[str, str]] = None,
) -> list[DNSUpdateResult]:
    """把 IP 写入配置中的 Cloudflare DNS 记录。

    ``ip``
        兜底 IP：写给所有记录（不分流模式）。
    ``ips_by_isp``
        三网分流模式：``{"mobile": "1.2.3.4", ...}``。每条记录会展开成
        三条同域名、同类型的 A 记录，各自带一个运营商标记的 comment。
    """
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
        for record, isp, target_ip, comment, skip_reason in _plan_targets(
            config, ip, ips_by_isp
        ):
            if skip_reason is not None:
                results.append(
                    DNSUpdateResult(
                        domain=record.domain,
                        name=record.name,
                        record_type=record.record_type,
                        ip=None,
                        ok=False,
                        error=skip_reason,
                        isp=isp,
                        skipped=True,
                    )
                )
                continue
            results.append(
                await _update_single_record(
                    http,
                    config.api_token,
                    record,
                    target_ip,  # type: ignore[arg-type]
                    isp=isp,
                    comment=comment,
                )
            )

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

    # 2. 决策 + 更新
    if config.split_by_isp:
        return await _update_split_by_isp(config, fetched, state, proxy, summary)

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


async def _update_split_by_isp(
    config: CloudflareIPConfig,
    fetched: IPFetchResult,
    state: dict[str, Any],
    proxy: Optional[ProxyConfig],
    summary: UpdateSummary,
) -> UpdateSummary:
    """三网分流：每个运营商各自决策，再一次性写它自己那条 A 记录。

    三家**互不牵连**：移动没过阈值 / 没抓到，不影响电信和联通照常更新。
    这也是为什么不复用 :func:`should_update` —— 那个是「一个 IP 决定一切」。
    """
    decisions: dict[str, IPUpdateDecision] = {}
    for isp in ISP_KEYS:
        best = fetched.best_by_isp.get(isp)
        if best is None:
            decisions[isp] = IPUpdateDecision(
                False,
                reason=(
                    f"[{ISP_LABELS[isp]}] 最近 {config.fetch_limit} 条消息里"
                    f"没有这个运营商的 IP"
                ),
            )
            continue
        decisions[isp] = should_update_isp(best, config, state)
    summary.decisions = decisions

    for isp, decision in decisions.items():
        log.info(
            "Cloudflare IP 三网分流 %s: %s（%s）",
            ISP_LABELS[isp],
            "更新" if decision.should_update else "跳过",
            decision.reason,
        )

    approved = {isp: d.ip for isp, d in decisions.items() if d.should_update and d.ip}
    if not approved:
        summary.skipped_reason = "；".join(d.reason for d in decisions.values())
        return summary

    summary.results = await update_dns_records(config, None, proxy, ips_by_isp=approved)

    # 只把**真的写成功**的那几家记进 state，否则失败的运营商会被误标成
    # 「当前速度已经是新的」，下一轮 only_update_if_faster 就会把它挡住。
    speed_map = state.get(LAST_SPEED_BY_ISP_KEY)
    if not isinstance(speed_map, dict):
        speed_map = {}
    ip_map = state.get(LAST_IP_BY_ISP_KEY)
    if not isinstance(ip_map, dict):
        ip_map = {}

    for result in summary.results:
        if not result.ok or result.skipped or result.isp is None:
            continue
        decision = decisions.get(result.isp)
        if decision is None or not decision.should_update:
            continue
        speed_map[result.isp] = decision.speed
        ip_map[result.isp] = decision.ip

    state[LAST_SPEED_BY_ISP_KEY] = speed_map
    state[LAST_IP_BY_ISP_KEY] = ip_map

    # 旧的整体字段一并维护：面板状态卡和「上次速度」展示还在用。
    changed = [r for r in summary.results if r.ok and not r.skipped]
    if changed:
        top = max(
            (decisions[r.isp] for r in changed if r.isp in decisions),
            key=lambda d: d.speed or 0.0,
        )
        state["cloudflare_ip_last_speed"] = top.speed
        state["cloudflare_ip_last_ip"] = top.ip

    log.info(
        "Cloudflare IP 三网分流完成: %d 条记录写成功、%d 条跳过、%d 条失败",
        len(changed),
        sum(1 for r in summary.results if r.skipped),
        sum(1 for r in summary.results if not r.ok and not r.skipped),
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
    "ISP_COMMENT_PREFIX",
    "ISP_LABELS",
    "LAST_IP_BY_ISP_KEY",
    "LAST_SPEED_BY_ISP_KEY",
    "DNSUpdateResult",
    "IPFetchResult",
    "ISPBest",
    "IPUpdateDecision",
    "UpdateSummary",
    "aggregate_messages",
    "fetch_and_update",
    "fetch_ips_from_channel",
    "make_message_source",
    "make_message_source_from_account",
    "parse_ips_from_text",
    "parse_isp",
    "should_update",
    "should_update_isp",
    "threshold_for",
    "update_dns_records",
]
