"""代理支持。

国内机器直连 Telegram 基本不通，因此代理是刚需。这里提供：

- :func:`resolve_proxy` —— 账号级代理优先，回退全局代理。
- :func:`probe_proxy` —— 部署时先探测代理本身可用、再探测能否穿透到 Telegram DC，
  失败信息精确到"代理不可达 / 认证失败 / 代理能连但到不了 Telegram"，
  免得启动后只看到一句 "Connection timeout" 无从下手。
- :func:`httpx_proxy` —— Bot 通知走同一个代理。

依赖 pyrogram 自带的 ``python-socks``，不需要额外装 PySocks。
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import struct
import time
from dataclasses import dataclass
from typing import Optional

from .config import AccountRecord, ProxyConfig, Settings
from .logging_setup import get_logger

log = get_logger("proxy")

#: Telegram 生产环境 DC 地址（IPv4）。探测时任取其一即可。
TELEGRAM_DC_ENDPOINTS: tuple[tuple[str, int], ...] = (
    ("149.154.167.51", 443),   # DC2 Amsterdam
    ("149.154.175.53", 443),   # DC1 Miami
    ("91.108.56.130", 443),    # DC5 Singapore
)


def resolve_proxy(
    record: Optional[AccountRecord],
    settings: Optional[Settings] = None,
) -> Optional[ProxyConfig]:
    """账号代理优先，其次全局代理。"""
    if record is not None and record.proxy is not None:
        return record.proxy
    if settings is not None and settings.proxy is not None:
        return settings.proxy
    return None


def httpx_proxy(proxy: Optional[ProxyConfig]) -> Optional[str]:
    """转成 httpx 的 ``proxy=`` 参数。socks5 需要 ``httpx[socks]``。"""
    if proxy is None:
        return None
    return proxy.to_httpx_url()


@dataclass
class ProbeResult:
    ok: bool
    stage: str
    detail: str
    latency_ms: Optional[float] = None

    def render(self) -> str:
        prefix = "OK" if self.ok else "FAIL"
        latency = f" {self.latency_ms:.0f}ms" if self.latency_ms is not None else ""
        return f"[{prefix}]{latency} {self.stage}: {self.detail}"


async def _tcp_connect(host: str, port: int, timeout: float) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)


async def _probe_tcp(proxy: ProxyConfig, timeout: float) -> ProbeResult:
    """第一步：能否 TCP 连上代理端口。"""
    started = time.perf_counter()
    try:
        reader, writer = await _tcp_connect(proxy.hostname, proxy.port, timeout)
    except asyncio.TimeoutError:
        return ProbeResult(False, "TCP", f"连接 {proxy.hostname}:{proxy.port} 超时（{timeout}s）")
    except OSError as exc:
        return ProbeResult(False, "TCP", f"连接 {proxy.hostname}:{proxy.port} 失败: {exc}")
    writer.close()
    with contextlib.suppress(Exception):  # pragma: no cover
        await writer.wait_closed()
    del reader
    return ProbeResult(
        True,
        "TCP",
        f"{proxy.hostname}:{proxy.port} 可达",
        (time.perf_counter() - started) * 1000,
    )


_SOCKS5_ERRORS = {
    0x01: "通用 SOCKS 服务器故障",
    0x02: "规则不允许该连接",
    0x03: "网络不可达",
    0x04: "主机不可达",
    0x05: "连接被拒绝",
    0x06: "TTL 过期",
    0x07: "不支持的命令",
    0x08: "不支持的地址类型",
}


async def _probe_socks5(proxy: ProxyConfig, target: tuple[str, int], timeout: float) -> ProbeResult:
    """手写一次 SOCKS5 握手，把失败原因还原成中文。

    比直接依赖库的异常更清楚：能区分"认证失败"和"代理到不了 Telegram"。
    """
    host, port = target
    started = time.perf_counter()
    try:
        reader, writer = await _tcp_connect(proxy.hostname, proxy.port, timeout)
    except (asyncio.TimeoutError, OSError) as exc:
        return ProbeResult(False, "SOCKS5", f"无法连接代理: {exc}")

    try:
        use_auth = bool(proxy.username)
        methods = b"\x00\x02" if use_auth else b"\x00"
        writer.write(b"\x05" + bytes([len(methods)]) + methods)
        await writer.drain()

        greeting = await asyncio.wait_for(reader.readexactly(2), timeout=timeout)
        if greeting[0] != 0x05:
            return ProbeResult(False, "SOCKS5", f"不是 SOCKS5 服务（返回版本 {greeting[0]}）")
        method = greeting[1]
        if method == 0xFF:
            return ProbeResult(
                False,
                "SOCKS5",
                "代理拒绝了所有认证方式（可能需要用户名/密码，或凭据不被接受）",
            )
        if method == 0x02:
            if not use_auth:
                return ProbeResult(False, "SOCKS5", "代理要求用户名/密码认证，但配置里没有提供")
            user = (proxy.username or "").encode()
            pwd = (proxy.password or "").encode()
            writer.write(b"\x01" + bytes([len(user)]) + user + bytes([len(pwd)]) + pwd)
            await writer.drain()
            auth_reply = await asyncio.wait_for(reader.readexactly(2), timeout=timeout)
            if auth_reply[1] != 0x00:
                return ProbeResult(False, "SOCKS5", "用户名或密码错误（认证被拒绝）")
        elif method != 0x00:
            return ProbeResult(False, "SOCKS5", f"代理要求不支持的认证方式 0x{method:02x}")

        # CONNECT 到目标（用 IPv4 字面量，避免代理端 DNS 差异）
        try:
            addr = socket.inet_aton(host)
            request = b"\x05\x01\x00\x01" + addr + struct.pack(">H", port)
        except OSError:
            encoded = host.encode()
            request = b"\x05\x01\x00\x03" + bytes([len(encoded)]) + encoded + struct.pack(">H", port)
        writer.write(request)
        await writer.drain()

        reply = await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
        if reply[1] != 0x00:
            reason = _SOCKS5_ERRORS.get(reply[1], f"未知错误 0x{reply[1]:02x}")
            return ProbeResult(
                False,
                "SOCKS5",
                f"代理无法连到 Telegram {host}:{port} —— {reason}（代理本身可用，但出口到不了 Telegram）",
            )
        # 读掉绑定地址，保持协议完整
        atyp = reply[3]
        if atyp == 0x01:
            await asyncio.wait_for(reader.readexactly(4 + 2), timeout=timeout)
        elif atyp == 0x03:
            length = (await asyncio.wait_for(reader.readexactly(1), timeout=timeout))[0]
            await asyncio.wait_for(reader.readexactly(length + 2), timeout=timeout)
        elif atyp == 0x04:
            await asyncio.wait_for(reader.readexactly(16 + 2), timeout=timeout)

        latency = (time.perf_counter() - started) * 1000
        return ProbeResult(True, "SOCKS5", f"可穿透到 Telegram {host}:{port}", latency)
    except asyncio.IncompleteReadError:
        return ProbeResult(False, "SOCKS5", "代理提前关闭了连接（协议不匹配？确认这是 SOCKS5 而非 HTTP 代理）")
    except asyncio.TimeoutError:
        return ProbeResult(False, "SOCKS5", f"握手超时（{timeout}s）")
    except OSError as exc:
        return ProbeResult(False, "SOCKS5", f"握手出错: {exc}")
    finally:
        writer.close()
        with contextlib.suppress(Exception):  # pragma: no cover
            await writer.wait_closed()


async def _probe_http_connect(proxy: ProxyConfig, target: tuple[str, int], timeout: float) -> ProbeResult:
    """HTTP 代理用 CONNECT 隧道探测。"""
    host, port = target
    started = time.perf_counter()
    try:
        reader, writer = await _tcp_connect(proxy.hostname, proxy.port, timeout)
    except (asyncio.TimeoutError, OSError) as exc:
        return ProbeResult(False, "HTTP", f"无法连接代理: {exc}")
    try:
        request = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        if proxy.username:
            import base64

            token = base64.b64encode(
                f"{proxy.username}:{proxy.password or ''}".encode()
            ).decode()
            request += f"Proxy-Authorization: Basic {token}\r\n"
        request += "\r\n"
        writer.write(request.encode())
        await writer.drain()
        status_line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        text = status_line.decode("latin-1").strip()
        if " 200 " not in text and not text.endswith(" 200"):
            return ProbeResult(False, "HTTP", f"代理拒绝 CONNECT: {text or '（空响应）'}")
        latency = (time.perf_counter() - started) * 1000
        return ProbeResult(True, "HTTP", f"CONNECT 隧道到 {host}:{port} 建立成功", latency)
    except asyncio.TimeoutError:
        return ProbeResult(False, "HTTP", f"CONNECT 超时（{timeout}s）")
    except OSError as exc:
        return ProbeResult(False, "HTTP", f"CONNECT 出错: {exc}")
    finally:
        writer.close()
        with contextlib.suppress(Exception):  # pragma: no cover
            await writer.wait_closed()


async def probe_proxy(
    proxy: Optional[ProxyConfig],
    *,
    timeout: float = 8.0,
    targets: tuple[tuple[str, int], ...] = TELEGRAM_DC_ENDPOINTS,
) -> list[ProbeResult]:
    """完整探测流程，返回逐级结果，供 CLI 打印。

    未配置代理时直接探测直连 Telegram，方便判断"到底要不要挂代理"。
    """
    results: list[ProbeResult] = []

    if proxy is None:
        for host, port in targets[:2]:
            started = time.perf_counter()
            try:
                _, writer = await _tcp_connect(host, port, timeout)
                writer.close()
                results.append(
                    ProbeResult(
                        True,
                        "直连",
                        f"Telegram {host}:{port} 可直连（无需代理）",
                        (time.perf_counter() - started) * 1000,
                    )
                )
                return results
            except (asyncio.TimeoutError, OSError) as exc:
                results.append(ProbeResult(False, "直连", f"{host}:{port} 不可达: {exc}"))
        results.append(
            ProbeResult(False, "结论", "直连 Telegram 失败，请配置 SOCKS5 代理（--proxy 或 TGA_PROXY）")
        )
        return results

    tcp = await _probe_tcp(proxy, timeout)
    results.append(tcp)
    if not tcp.ok:
        results.append(
            ProbeResult(False, "结论", f"代理 {proxy.to_url()} 的端口不可达，请检查地址/端口/防火墙")
        )
        return results

    for host, port in targets:
        if proxy.scheme in {"socks5", "socks4"}:
            if proxy.scheme == "socks4":
                results.append(
                    ProbeResult(True, "SOCKS4", "跳过深度探测（仅支持 SOCKS5 深度探测），将在登录时验证")
                )
                return results
            result = await _probe_socks5(proxy, (host, port), timeout)
        else:
            result = await _probe_http_connect(proxy, (host, port), timeout)
        results.append(result)
        if result.ok:
            return results

    results.append(
        ProbeResult(
            False,
            "结论",
            "代理端口可达但无法连到任何 Telegram DC —— 通常是代理出口被墙或不允许 443",
        )
    )
    return results


def summarize(results: list[ProbeResult]) -> tuple[bool, str]:
    ok = bool(results) and all(r.ok for r in results if r.stage != "结论")
    ok = ok and not any(r.stage == "结论" and not r.ok for r in results)
    return ok, "\n".join(r.render() for r in results)


__all__ = [
    "ProbeResult",
    "TELEGRAM_DC_ENDPOINTS",
    "httpx_proxy",
    "probe_proxy",
    "resolve_proxy",
    "summarize",
]
