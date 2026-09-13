"""Web 运行期管理器。

负责启动 / 停止后台账号任务，收集运行状态与日志，供 API 与 WebSocket 消费。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from tg_assistant.logging_setup import get_logger

log = get_logger("web.runtime")

#: 停止账号时的优雅退出等待上限（秒），超过才强杀。
STOP_TIMEOUT = 30.0


@dataclass
class AccountRuntime:
    """单个账号的运行状态。"""

    name: str
    running: bool = False
    error: Optional[str] = None
    started_at: Optional[float] = None
    stats: dict[str, Any] = field(default_factory=dict)
    pid: Optional[int] = None


class RuntimeManager:
    """管理 MultiRunner 的生命周期。"""

    def __init__(self, app_state: Any) -> None:
        self.state = app_state
        self.settings: Any = app_state.settings
        self.paths: Any = app_state.paths
        self.store: Any = app_state.store
        self._runner: Any = None
        self._runner_task: Optional[asyncio.Task[None]] = None
        self._log_handler: Optional[logging.Handler] = None
        self._recent_logs: list[dict[str, Any]] = []
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()
        self._shutdown_event = asyncio.Event()

    # ------------------------------------------------------------------ #
    async def startup(self) -> None:
        """启动时挂接日志广播。"""
        self._log_handler = _BroadcastHandler(self._emit_log)
        self._log_handler.setLevel(logging.DEBUG)
        root = logging.getLogger("tg-assistant")
        root.addHandler(self._log_handler)
        get_logger().info("Web 运行管理器已就绪", extra={"account": "-", "extra_fields": {}})

    async def shutdown(self) -> None:
        """停止全部账号并卸载日志。"""
        await self.stop()
        if self._log_handler is not None:
            root = logging.getLogger("tg-assistant")
            root.removeHandler(self._log_handler)
            self._log_handler = None
        # 通知所有订阅者退出
        for queue in list(self._subscribers):
            with contextlib.suppress(Exception):
                queue.put_nowait({"type": "shutdown"})

    # ------------------------------------------------------------------ #
    @property
    def is_running(self) -> bool:
        return self._runner_task is not None and not self._runner_task.done()

    def account_status(self) -> list[dict[str, Any]]:
        """全部账号的运行状态。"""
        registry = self.store.load_registry()
        result = []
        for record in registry.accounts:
            info = AccountRuntime(name=record.name)
            info.running = record.name in self._running_accounts()
            if info.running and self._runner is not None:
                for snapshot in self._runner.snapshot():
                    if snapshot.get("account") == record.name:
                        info.started_at = snapshot.get("uptime_s")
                        info.stats = snapshot
                        break
            result.append(
                {
                    "name": info.name,
                    "enabled": record.enabled,
                    "running": info.running,
                    "error": info.error,
                    "user": record.label,
                    "user_id": record.user_id,
                    "username": record.username,
                    "display_name": record.display_name,
                    "session_exists": self.store.has_session(record.name),
                    "proxy": record.proxy.to_url() if record.proxy else None,
                    "last_login": record.last_login_at,
                    "stats": info.stats,
                }
            )
        return result

    def get_account(self, name: str) -> Optional[dict[str, Any]]:
        record = self.store.get_account(name)
        if record is None:
            return None
        config = self.store.load_account_config(name, create=False)
        watched = config.watched_chats()
        source_count = sum(len(rule.sources) for rule in config.forward.active_rules)
        return {
            "name": record.name,
            "enabled": record.enabled,
            "user": record.label,
            "user_id": record.user_id,
            "username": record.username,
            "display_name": record.display_name,
            "phone": record.phone,
            "proxy": record.proxy.to_url() if record.proxy else None,
            "session_exists": self.store.has_session(record.name),
            "last_login": record.last_login_at,
            "config": config.model_dump(mode="json"),
            "source_count": source_count,
            "watched_chats": len(watched) if watched else 0,
        }

    # ------------------------------------------------------------------ #
    async def start(self, accounts: list[str] | None = None) -> dict[str, Any]:
        """启动指定账号（默认全部启用的账号）。"""
        async with self._lock:
            if self.is_running:
                return {"ok": False, "message": "已经在运行中，请先停止"}
            from tg_assistant.runner import MultiRunner

            self._runner = MultiRunner(self.store, self.settings)
            self._shutdown_event.clear()
            names = accounts or [r.name for r in self.store.load_registry().enabled_accounts]
            if not names:
                return {"ok": False, "message": "没有可运行的账号。先 login 并用 accounts enable 启用。"}
            self._runner_task = asyncio.create_task(
                self._supervise(names), name="web-runner"
            )
            # 通知 /ws/status 订阅者：已开始运行。
            self.push_event({"type": "status", "running": True, "accounts": names})
            return {"ok": True, "accounts": names}

    async def stop(self) -> dict[str, Any]:
        """停止全部账号。

        先请 MultiRunner 自己优雅退出（它要关 client、排空通知队列），
        只有超时才强杀 —— 直接 cancel 会跳过它的清理逻辑，把 pyrogram client
        和通知 worker 漏在后台，重新 start 时新旧 client 会争抢同一个 session 文件。
        """
        async with self._lock:
            runner = self._runner
            task = self._runner_task
            self._runner = None
            self._runner_task = None

        if runner is not None:
            runner.request_shutdown()

        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=STOP_TIMEOUT)
            except asyncio.TimeoutError:
                log.warning("账号停止超时，已强制取消", timeout_s=STOP_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("账号停止过程中出现异常", error=str(exc))

        self._shutdown_event.set()
        # 通知 /ws/status 订阅者：已停止。
        self.push_event({"type": "status", "running": False, "accounts": []})
        return {"ok": True}

    async def _supervise(self, names: list[str]) -> None:
        """在后台运行 MultiRunner，捕获异常，并把结束状态广播出去。"""
        assert self._runner is not None
        error: Optional[str] = None
        try:
            await self._runner.run(
                names,
                heartbeat=float(self.state.web_settings.heartbeat_interval),
                # 信号由 uvicorn 接管，这里不能再抢注
                install_signals=False,
            )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            error = str(exc)
            get_logger().exception("运行管理器异常退出", error=error)
        finally:
            # 正常退出和崩溃都要通知，否则前端会一直停在「运行中」。
            self.push_event(
                {"type": "runner", "running": False, "error": error, "accounts": names}
            )

    def _running_accounts(self) -> set[str]:
        if self._runner is None:
            return set()
        return {snapshot.get("account") for snapshot in self._runner.snapshot()}

    # ------------------------------------------------------------------ #
    # 日志广播
    def _emit_log(self, record: logging.LogRecord) -> None:
        """日志回调：格式化后推给所有 WebSocket 订阅者。"""
        entry = {
            "type": "log",
            "time": time.strftime("%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "account": getattr(record, "account", None) or "-",
            "msg": record.getMessage(),
        }
        # 保留最近 N 行
        self._recent_logs.append(entry)
        max_size = self.state.web_settings.log_history_size
        if len(self._recent_logs) > max_size:
            self._recent_logs = self._recent_logs[-max_size:]
        for queue in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(entry)

    def subscribe_logs(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        # 先塞历史日志，让新连接立刻看到上下文
        for entry in self._recent_logs:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(entry)
        self._subscribers.add(queue)
        return queue

    def unsubscribe_logs(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def push_event(self, event: dict[str, Any]) -> None:
        """推送自定义事件（登录进度、运行状态变更等）。"""
        for queue in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    @property
    def recent_logs(self) -> list[dict[str, Any]]:
        return list(self._recent_logs)


class _BroadcastHandler(logging.Handler):
    """把 logging 记录转成 dict 推给 RuntimeManager。"""

    def __init__(self, callback: Any) -> None:
        super().__init__()
        self.callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        # 日志系统不应因回调出错而中断（例如订阅队列已满）。
        with contextlib.suppress(Exception):  # pragma: no cover
            self.callback(record)


__all__ = ["AccountRuntime", "RuntimeManager"]
