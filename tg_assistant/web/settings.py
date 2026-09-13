"""Web 相关设置。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class WebSettings:
    """Web 运行期状态。"""

    host: str = "127.0.0.1"
    port: int = 8080
    # 实时日志广播的最近 N 行
    log_history_size: int = 200
    # 扫码登录超时（秒）
    login_timeout: float = 300.0
    # 运行心跳日志间隔（秒）
    heartbeat_interval: float = 300.0
    # 共享密钥（Cookie 鉴权用；留空则不校验，此时只应监听 127.0.0.1）
    secret_key: str = ""
