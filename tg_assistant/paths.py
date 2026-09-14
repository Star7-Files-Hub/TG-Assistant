"""数据目录布局与路径解析。

每个账号的数据完全隔离在 ``<data_dir>/accounts/<account>/`` 之下：

    data/
    ├── accounts.json                 # 账号注册表（名称/user_id/代理/启用状态）
    ├── accounts/
    │   └── <account>/
    │       ├── <account>.session     # Pyrogram 会话文件（账号独立）
    │       ├── config.json           # 该账号的转发/红包/通知配置（账号独立）
    │       └── state.json            # 运行期状态（去重游标等，可随时删除）
    └── logs/
        ├── tg-assistant.log          # 全量日志
        ├── error.log                 # ERROR 及以上
        ├── events.jsonl              # 结构化事件日志（便于 debug/统计）
        └── accounts/<account>.log    # 单账号日志
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

#: 账号名允许的字符：Unicode 字母/数字/下划线打头，其后可含 ``.`` ``-`` 与空格。
#:
#: 刻意允许非 ASCII（中文 / 日文 / 韩文）：部署版一直支持，而仓库初版只允许
#: ``[A-Za-z0-9]``。两边不一致的后果不只是"少个功能" ——
#: :meth:`Paths.iter_account_names` 对校验不过的目录名是**静默跳过**的，
#: 所以服务器上建的中文账号名在仓库版里会直接"消失"。
#:
#: 安全性：路径分隔符 ``/`` ``\`` 与 NUL 都不在字符集内，且首字符必须是
#: ``\w``，因此 ``.``、``..`` 这类目录穿越名一律不合法（见 tests/test_paths.py）。
ACCOUNT_NAME_RE = re.compile(r"^[\w][\w.\- ]{0,63}$", re.UNICODE)

DEFAULT_DATA_DIR = "./data"


class InvalidAccountName(ValueError):
    """账号名不合法。"""


def validate_account_name(name: str) -> str:
    """校验并归一化账号名。

    账号名会直接作为目录名与会话文件名使用，因此必须严格校验，
    防止 ``../`` 之类的路径穿越写到数据目录之外。
    """
    normalized = (name or "").strip()
    if not ACCOUNT_NAME_RE.match(normalized):
        raise InvalidAccountName(
            f"账号名 {name!r} 不合法：允许字母（含中文等 Unicode 字符）、数字、"
            "'.'、'_'、'-'、空格，首字符不能是符号或空格，长度 1-64。"
        )
    return normalized


@dataclass(frozen=True)
class AccountPaths:
    """单个账号的全部路径。"""

    name: str
    root: Path

    @property
    def session_dir(self) -> Path:
        """Pyrogram 的 workdir（会话文件所在目录）。"""
        return self.root

    @property
    def session_file(self) -> Path:
        return self.root / f"{self.name}.session"

    @property
    def config_file(self) -> Path:
        return self.root / "config.json"

    @property
    def state_file(self) -> Path:
        return self.root / "state.json"

    def ensure(self) -> "AccountPaths":
        self.root.mkdir(parents=True, exist_ok=True)
        return self


@dataclass(frozen=True)
class Paths:
    """全局数据目录布局。"""

    data_dir: Path

    @classmethod
    def from_env(cls, data_dir: str | os.PathLike[str] | None = None) -> "Paths":
        raw = str(data_dir or os.environ.get("TGA_DATA_DIR") or DEFAULT_DATA_DIR)
        return cls(data_dir=Path(raw).expanduser().resolve())

    # ---- 目录 ----
    @property
    def accounts_dir(self) -> Path:
        return self.data_dir / "accounts"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def account_log_dir(self) -> Path:
        return self.log_dir / "accounts"

    @property
    def qr_dir(self) -> Path:
        return self.data_dir / "qr"

    # ---- 文件 ----
    @property
    def registry_file(self) -> Path:
        return self.data_dir / "accounts.json"

    def account(self, name: str) -> AccountPaths:
        safe = validate_account_name(name)
        return AccountPaths(name=safe, root=self.accounts_dir / safe)

    def ensure(self) -> "Paths":
        for path in (
            self.data_dir,
            self.accounts_dir,
            self.log_dir,
            self.account_log_dir,
            self.qr_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return self

    def iter_account_names(self) -> list[str]:
        """扫描磁盘上已存在的账号目录（按名称排序）。"""
        if not self.accounts_dir.is_dir():
            return []
        names = []
        for child in self.accounts_dir.iterdir():
            if not child.is_dir():
                continue
            try:
                names.append(validate_account_name(child.name))
            except InvalidAccountName:
                continue
        return sorted(names)


__all__ = [
    "ACCOUNT_NAME_RE",
    "AccountPaths",
    "InvalidAccountName",
    "Paths",
    "validate_account_name",
]
