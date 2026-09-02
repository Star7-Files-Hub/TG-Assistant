"""日志系统。

设计目标（debug 友好）：

1. **控制台彩色 + 文件纯文本**：控制台带颜色便于肉眼扫读，文件不含 ANSI 转义。
2. **账号维度隔离**：每条日志带 ``account`` 字段，并额外写入
   ``logs/accounts/<account>.log``，排查单账号问题时不必在混合日志里翻找。
3. **结构化事件流**：``logs/events.jsonl`` 每行一个 JSON，字段固定，
   便于用 ``jq`` 统计"今天抢了多少红包 / 转发延迟分布"。
4. **敏感信息脱敏**：api_hash、bot_token、phone、session_string、2FA 密码
   在写出前统一替换成掩码，避免日志泄漏凭据。
5. **耗时埋点**：``log_latency`` 上下文管理器统一输出 ``cost=12.3ms``，
   秒级转发的性能问题可以直接从日志看出来。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sys
import time
from collections.abc import Iterator, Mapping
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

LOGGER_NAME = "tg-assistant"

#: 单文件最大 8MB，保留 10 份，够查最近几天。
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 10

_CONSOLE_COLORS = {
    "DEBUG": "\033[36m",       # 青
    "INFO": "\033[32m",        # 绿
    "WARNING": "\033[33m",     # 黄
    "ERROR": "\033[31m",       # 红
    "CRITICAL": "\033[1;41m",  # 红底
}
_COLOR_RESET = "\033[0m"
_DIM = "\033[2m"

#: 需要脱敏的键名（大小写不敏感）。
_SENSITIVE_KEYS = (
    "api_hash",
    "bot_token",
    "token",
    "password",
    "session_string",
    "auth_key",
    "phone",
    "phone_number",
    "sendkey",
)

_SENSITIVE_PATTERNS: tuple[tuple[re.Pattern[str], Any], ...] = (
    # tg://login?token=...（先处理，避免被下面的 key=value 规则切成两段）
    (re.compile(r"(tg://login\?token=)([A-Za-z0-9_\-=]+)"), r"\1***"),
    # Bot token: 123456789:AAE...  -> 123456789:***
    (re.compile(r"\b(\d{6,12}):([A-Za-z0-9_-]{20,})"), r"\1:***"),
    # key=value 形式的敏感字段（value 里排除 * 以免二次遮罩已脱敏的内容）
    (
        re.compile(
            r"(?i)\b(" + "|".join(_SENSITIVE_KEYS) + r")\s*[=:]\s*['\"]?([^\s'\",;)*]{4,})"
        ),
        lambda m: f"{m.group(1)}={_mask(m.group(2))}",
    ),
)


def _mask(value: str) -> str:
    """保留首尾各 2 位，中间用 * 代替，便于对照但不泄露。"""
    if len(value) <= 6:
        return "***"
    return f"{value[:2]}***{value[-2:]}"


def scrub(text: str) -> str:
    """对一段文本做敏感信息脱敏。"""
    if not text:
        return text
    result = text
    for pattern, repl in _SENSITIVE_PATTERNS:
        result = pattern.sub(repl, result)  # type: ignore[arg-type]
    return result


class ScrubFilter(logging.Filter):
    """在日志真正写出前对消息与参数脱敏。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - 参数不匹配时不要因为日志把主流程搞崩
            return True
        scrubbed = scrub(message)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = ()
        return True


class AccountContextFilter(logging.Filter):
    """确保每条记录都有 ``account`` 与 ``extra_fields``，格式化时不必判空。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "account"):
            record.account = "-"
        if not hasattr(record, "extra_fields"):
            record.extra_fields = {}
        return True


class AccountFilter(logging.Filter):
    """只放行指定账号的日志（用于单账号日志文件）。"""

    def __init__(self, account: str) -> None:
        super().__init__()
        self.account = account

    def filter(self, record: logging.LogRecord) -> bool:
        return getattr(record, "account", "-") == self.account


class MinLevelFilter(logging.Filter):
    def __init__(self, min_level: int) -> None:
        super().__init__()
        self.min_level = min_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= self.min_level


def _render_extra(fields: Mapping[str, Any]) -> str:
    if not fields:
        return ""
    parts = []
    for key, value in fields.items():
        if isinstance(value, float):
            rendered = f"{value:.1f}"
        else:
            rendered = str(value)
        if len(rendered) > 200:
            rendered = rendered[:200] + "…"
        parts.append(f"{key}={rendered}")
    return " " + " ".join(parts)


class PlainFormatter(logging.Formatter):
    """文件用格式：定宽等级 + 账号 + 模块位置 + 消息 + 结构化字段。"""

    default_time_format = "%Y-%m-%d %H:%M:%S"
    default_msec_format = "%s.%03d"

    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"{self.formatTime(record)} "
            f"{record.levelname:<7} "
            f"[{getattr(record, 'account', '-')}] "
            f"{record.name}:{record.lineno} "
            f"{record.getMessage()}"
        )
        base += _render_extra(getattr(record, "extra_fields", {}))
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        if record.stack_info:
            base += "\n" + self.formatStack(record.stack_info)
        return base


class ColorFormatter(PlainFormatter):
    """控制台格式：等级着色，附加字段暗色，其余同 PlainFormatter。"""

    def __init__(self, use_color: bool = True) -> None:
        super().__init__()
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        account = getattr(record, "account", "-")
        level = record.levelname
        extra = _render_extra(getattr(record, "extra_fields", {}))
        head = f"{self.formatTime(record)}"
        body = f"[{account}] {record.getMessage()}"
        location = f"{record.name}:{record.lineno}"

        if self.use_color:
            color = _CONSOLE_COLORS.get(level, "")
            line = (
                f"{_DIM}{head}{_COLOR_RESET} "
                f"{color}{level:<7}{_COLOR_RESET} "
                f"{body}"
                f"{_DIM}{extra}{_COLOR_RESET} "
                f"{_DIM}({location}){_COLOR_RESET}"
            )
        else:
            line = f"{head} {level:<7} {body}{extra} ({location})"

        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        if record.stack_info:
            line += "\n" + self.formatStack(record.stack_info)
        return line


class JsonlFormatter(logging.Formatter):
    """结构化事件行：固定字段 + extra_fields 展开。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": round(record.created, 3),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
            + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "account": getattr(record, "account", "-"),
            "msg": scrub(record.getMessage()),
        }
        for key, value in getattr(record, "extra_fields", {}).items():
            if key in payload:
                key = f"f_{key}"
            payload[key] = _jsonable(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _supports_color(stream: Any) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TGA_FORCE_COLOR") == "1":
        return True
    return bool(getattr(stream, "isatty", lambda: False)())


def _rotating_handler(
    path: Path,
    level: int,
    formatter: logging.Formatter,
    max_bytes: int,
    backup_count: int,
) -> RotatingFileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
        delay=True,
    )
    handler.setLevel(level)
    handler.setFormatter(formatter)
    _attach_common_filters(handler)
    return handler


def _attach_common_filters(handler: logging.Handler) -> None:
    """把补字段与脱敏挂在 handler 上。

    注意：挂在 logger 上的 filter 只对**直接**用该 logger 打的日志生效，
    子 logger（``tg-assistant.forward`` 等）的记录不会经过父 logger 的 filter。
    handler 级 filter 才会对所有传播上来的记录生效。
    """
    handler.addFilter(AccountContextFilter())
    handler.addFilter(ScrubFilter())


def configure_logging(
    log_dir: str | Path,
    level: str | int = "INFO",
    *,
    accounts: list[str] | None = None,
    console: bool = True,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    pyrogram_level: str | int | None = None,
) -> logging.Logger:
    """配置根 logger（``tg-assistant``）与全部 handler。

    幂等：重复调用会先清空既有 handler，避免重复输出。

    Parameters
    ----------
    log_dir:
        日志目录，会自动创建。
    level:
        主日志级别，字符串或 logging 级别数值。
    accounts:
        需要额外产出独立日志文件的账号列表。
    console:
        是否输出到 stderr。Docker 里保留 True，日志由 docker logs 收集。
    pyrogram_level:
        pyrogram 自身日志级别；默认 WARNING，设为 ``DEBUG`` 可看 MTProto 细节。
    """
    level_no = _coerce_level(level)
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level_no)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        with contextlib.suppress(Exception):
            handler.close()

    if console:
        stream = sys.stderr
        console_handler = logging.StreamHandler(stream)
        console_handler.setLevel(level_no)
        console_handler.setFormatter(ColorFormatter(use_color=_supports_color(stream)))
        _attach_common_filters(console_handler)
        logger.addHandler(console_handler)

    plain = PlainFormatter()
    logger.addHandler(
        _rotating_handler(log_path / "tg-assistant.log", level_no, plain, max_bytes, backup_count)
    )
    error_handler = _rotating_handler(
        log_path / "error.log", logging.ERROR, plain, max_bytes, backup_count
    )
    error_handler.addFilter(MinLevelFilter(logging.ERROR))
    logger.addHandler(error_handler)

    logger.addHandler(
        _rotating_handler(
            log_path / "events.jsonl", level_no, JsonlFormatter(), max_bytes, backup_count
        )
    )

    for account in accounts or []:
        account_handler = _rotating_handler(
            log_path / "accounts" / f"{account}.log",
            level_no,
            plain,
            max_bytes,
            backup_count,
        )
        account_handler.addFilter(AccountFilter(account))
        logger.addHandler(account_handler)

    pyro_level = _coerce_level(
        pyrogram_level if pyrogram_level is not None else os.environ.get("TGA_PYROGRAM_LOG_LEVEL", "WARNING")
    )
    for name in ("pyrogram", "pyrogram.session", "pyrogram.connection"):
        pyro_logger = logging.getLogger(name)
        pyro_logger.setLevel(pyro_level)
        pyro_logger.handlers = list(logger.handlers)
        pyro_logger.propagate = False

    return logger


def _coerce_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    resolved = logging.getLevelName(str(level).strip().upper())
    return resolved if isinstance(resolved, int) else logging.INFO


def get_logger(module: str | None = None) -> logging.Logger:
    """取得子 logger，例如 ``get_logger("forward")`` -> ``tg-assistant.forward``。"""
    if not module:
        return logging.getLogger(LOGGER_NAME)
    return logging.getLogger(f"{LOGGER_NAME}.{module}")


class AccountLogger:
    """绑定账号名的 logger 包装。

    所有业务代码都用它输出日志，这样 ``account`` 字段永远不会漏，
    并且额外字段通过关键字直接传入::

        alog.info("命中转发规则", rule="rule-1", cost_ms=8.2)
    """

    __slots__ = ("_logger", "account")

    def __init__(self, logger: logging.Logger, account: str) -> None:
        self._logger = logger
        self.account = account

    def bind(self, module: str) -> "AccountLogger":
        return AccountLogger(get_logger(module), self.account)

    def _log(self, level: int, msg: str, *args: Any, **fields: Any) -> None:
        if not self._logger.isEnabledFor(level):
            return
        exc_info = fields.pop("exc_info", None)
        stacklevel = fields.pop("stacklevel", 3)
        self._logger.log(
            level,
            msg,
            *args,
            exc_info=exc_info,
            stacklevel=stacklevel,
            extra={"account": self.account, "extra_fields": fields},
        )

    def debug(self, msg: str, *args: Any, **fields: Any) -> None:
        self._log(logging.DEBUG, msg, *args, **fields)

    def info(self, msg: str, *args: Any, **fields: Any) -> None:
        self._log(logging.INFO, msg, *args, **fields)

    def warning(self, msg: str, *args: Any, **fields: Any) -> None:
        self._log(logging.WARNING, msg, *args, **fields)

    def error(self, msg: str, *args: Any, **fields: Any) -> None:
        self._log(logging.ERROR, msg, *args, **fields)

    def exception(self, msg: str, *args: Any, **fields: Any) -> None:
        fields.setdefault("exc_info", True)
        self._log(logging.ERROR, msg, *args, **fields)

    @contextlib.contextmanager
    def latency(self, action: str, level: int = logging.INFO, **fields: Any) -> Iterator[dict[str, Any]]:
        """记录一段操作的耗时。

        用法::

            with alog.latency("转发消息", chat_id=123) as ctx:
                await do_forward()
                ctx["message_id"] = mid

        无论成功失败都会输出一条带 ``cost_ms`` 的日志；异常会以 ERROR 记录后原样抛出。
        """
        extra: dict[str, Any] = dict(fields)
        started = time.perf_counter()
        try:
            yield extra
        except Exception as exc:
            extra["cost_ms"] = round((time.perf_counter() - started) * 1000, 2)
            extra["error"] = f"{type(exc).__name__}: {exc}"
            self._log(logging.ERROR, f"{action} 失败", **extra)
            raise
        else:
            extra["cost_ms"] = round((time.perf_counter() - started) * 1000, 2)
            self._log(level, f"{action} 完成", **extra)


def account_logger(account: str, module: str | None = None) -> AccountLogger:
    return AccountLogger(get_logger(module), account)


__all__ = [
    "AccountLogger",
    "LOGGER_NAME",
    "account_logger",
    "configure_logging",
    "get_logger",
    "scrub",
]
