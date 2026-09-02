"""Web 相关设置。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WebSettings:
    """Web 运行期状态。"""

    host: str = "0.0.0.0"
    port: int = 8080
    # 实时日志广播的最近 N 行
    log_history_size: int = 200
    # 扫码登录超时（秒）
    login_timeout: float = 300.0
    # 共享密钥（用于简单鉴权，留空则不校验）
    secret_key: str = ""
    # 是否允许跨域（开发用）
    cors_origins: list[str] = field(default_factory=lambda: ["*"])
