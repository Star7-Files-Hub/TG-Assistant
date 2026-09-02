"""配置持久化：账号注册表与单账号配置的原子读写。

要点：

- **原子写**：先写 ``*.tmp`` 再 ``os.replace``，避免进程被 kill 时留下半截 JSON。
- **权限收紧**：注册表与 session 文件设为 ``0600``，目录 ``0700``。
- **容错**：配置损坏时备份为 ``*.broken-<时间戳>`` 并抛出清晰错误，不静默覆盖用户数据。
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .config import AccountConfig, AccountRecord, AccountRegistry
from .logging_setup import get_logger
from .paths import Paths, validate_account_name

log = get_logger("store")


class ConfigError(RuntimeError):
    """配置文件读写/校验失败。"""


def _atomic_write_json(path: Path, payload: dict[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    # 尽量 fsync 目录，确保 rename 落盘（部分文件系统不支持，忽略失败）
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        backup = path.with_name(f"{path.name}.broken-{int(time.time())}")
        path.rename(backup)
        raise ConfigError(
            f"{path} 不是合法 JSON（第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}）；"
            f"已备份为 {backup.name}，请修正后重试"
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} 的顶层结构必须是 JSON 对象")
    return data


def _harden(path: Path, mode: int) -> None:
    # 某些挂载点（NFS/CIFS）不支持 chmod，权限收紧只能尽力而为
    with contextlib.suppress(OSError):
        os.chmod(path, mode)


class Store:
    """数据目录的读写门面。"""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths

    # ---------------- 初始化 ----------------
    def bootstrap(self) -> "Store":
        self.paths.ensure()
        _harden(self.paths.data_dir, stat.S_IRWXU)
        _harden(self.paths.accounts_dir, stat.S_IRWXU)
        return self

    # ---------------- 注册表 ----------------
    def load_registry(self) -> AccountRegistry:
        raw = _read_json(self.paths.registry_file)
        if raw is None:
            return AccountRegistry()
        try:
            return AccountRegistry.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError(f"{self.paths.registry_file} 校验失败:\n{exc}") from exc

    def save_registry(self, registry: AccountRegistry) -> None:
        _atomic_write_json(self.paths.registry_file, registry.model_dump(mode="json"))
        log.debug("账号注册表已保存 count=%d", len(registry.accounts))

    def upsert_account(self, record: AccountRecord) -> AccountRegistry:
        registry = self.load_registry()
        registry.upsert(record)
        self.save_registry(registry)
        return registry

    def get_account(self, name: str) -> AccountRecord | None:
        return self.load_registry().get(validate_account_name(name))

    def require_account(self, name: str) -> AccountRecord:
        record = self.get_account(name)
        if record is None:
            known = ", ".join(r.name for r in self.load_registry().accounts) or "（空）"
            raise ConfigError(f"账号 {name!r} 不存在。已注册账号: {known}")
        return record

    def delete_account(self, name: str, *, remove_data: bool = False) -> bool:
        safe = validate_account_name(name)
        registry = self.load_registry()
        removed = registry.remove(safe)
        if removed:
            self.save_registry(registry)
        if remove_data:
            account_paths = self.paths.account(safe)
            if account_paths.root.is_dir():
                for child in sorted(account_paths.root.rglob("*"), reverse=True):
                    child.unlink() if child.is_file() else child.rmdir()
                account_paths.root.rmdir()
                log.info("已删除账号数据目录 path=%s", account_paths.root)
        return removed

    # ---------------- 单账号配置 ----------------
    def load_account_config(self, name: str, *, create: bool = True) -> AccountConfig:
        account_paths = self.paths.account(name)
        raw = _read_json(account_paths.config_file)
        if raw is None:
            config = AccountConfig.default()
            if create:
                account_paths.ensure()
                self.save_account_config(name, config)
                log.info("已生成默认配置 account=%s path=%s", name, account_paths.config_file)
            return config
        try:
            return AccountConfig.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError(
                f"账号 {name} 的配置 {account_paths.config_file} 校验失败：\n{_format_validation_error(exc)}"
            ) from exc

    def save_account_config(self, name: str, config: AccountConfig) -> None:
        account_paths = self.paths.account(name).ensure()
        _harden(account_paths.root, stat.S_IRWXU)
        _atomic_write_json(account_paths.config_file, config.model_dump(mode="json"))
        log.debug("账号配置已保存 account=%s", name)

    # ---------------- 运行期状态 ----------------
    def load_state(self, name: str) -> dict[str, Any]:
        return _read_json(self.paths.account(name).state_file) or {}

    def save_state(self, name: str, state: dict[str, Any]) -> None:
        _atomic_write_json(self.paths.account(name).state_file, state)

    # ---------------- session 文件 ----------------
    def harden_session(self, name: str) -> None:
        session = self.paths.account(name).session_file
        if session.is_file():
            _harden(session, stat.S_IRUSR | stat.S_IWUSR)

    def has_session(self, name: str) -> bool:
        return self.paths.account(name).session_file.is_file()


def _format_validation_error(exc: ValidationError) -> str:
    """把 pydantic 报错渲染成人类可读的多行文本。"""
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)


__all__ = ["ConfigError", "Store"]
