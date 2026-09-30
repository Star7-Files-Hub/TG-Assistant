"""运行编排：登录、启动、多账号并发、优雅退出。

一个账号一条 :class:`AccountRunner`，内部持有独立的 Client / 转发引擎 / 抢红包引擎 /
通知器。多账号由 :class:`MultiRunner` 并发拉起，互不影响：某个账号会话失效或代理挂了，
其余账号继续跑，并在日志里明确指出是哪个账号出了问题。
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import signal
import time
from dataclasses import dataclass
from typing import Any, Optional

from pyrogram import Client
from pyrogram.errors import (
    AuthKeyUnregistered,
    BadRequest,
    PasswordHashInvalid,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    PhoneNumberInvalid,
    RPCError,
    SessionPasswordNeeded,
    Unauthorized,
    UserDeactivated,
)

from .client import (
    ClientBundle,
    MissingApiCredentials,
    SessionInvalid,
    build_client,
    device_fingerprint,
    make_account_logger,
)
from .config import (
    AccountConfig,
    AccountRecord,
    Settings,
    mask_phone,
    utc_now_iso,
)
from .bot_commands import ProbeResult, RuleHit, StatusCommand, StatusData
from .forwarder import CrossAccountDedupe, ForwardEngine
from .logging_setup import get_logger
from .matching import CompiledMatcher, compile_user_pattern
from .notify import BotNotifier, NotifyTask
from .paths import Paths
from .proxy import resolve_proxy
from .qr_login import (
    DEFAULT_LOGIN_TIMEOUT,
    PasswordProvider,
    QrLoginError,
    QrLoginSession,
    QrRenderer,
)
from .red_packet import RedPacketHunter
from .reg_grab import RegGrabHunter
from .store import ConfigError, Store, clear_session_files

log = get_logger("runner")


class AccountBusy(RuntimeError):
    """同一账号的 session 已被其它进程占用。"""


# --------------------------------------------------------------------------- #
# 登录
# --------------------------------------------------------------------------- #
@dataclass
class LoginResult:
    account: str
    user_id: int
    username: Optional[str]
    display_name: Optional[str]
    phone: Optional[str]


async def login_account(
    name: str,
    store: Store,
    settings: Settings,
    *,
    renderer: QrRenderer,
    password_provider: Optional[PasswordProvider] = None,
    timeout: float = DEFAULT_LOGIN_TIMEOUT,
    proxy_override: Optional[Any] = None,
    api_id: Optional[int] = None,
    api_hash: Optional[str] = None,
    force: bool = False,
) -> LoginResult:
    """扫码登录一个账号并写入注册表。

    已登录且未指定 ``force`` 时直接返回现有身份，不会重复弹二维码。
    """
    paths = store.paths
    record = store.get_account(name) or AccountRecord(name=name, created_at=utc_now_iso())
    if proxy_override is not None:
        record = record.model_copy(update={"proxy": proxy_override})
    if api_id is not None:
        record = record.model_copy(update={"api_id": api_id})
    if api_hash is not None:
        record = record.model_copy(update={"api_hash": api_hash})

    alog = make_account_logger(name, "login")
    account_paths = paths.account(name).ensure()

    if account_paths.session_file.is_file() and not force:
        alog.info("检测到已有会话，先尝试直接复用", session=str(account_paths.session_file))
        existing = await _probe_existing_session(record, settings, paths, alog)
        if existing is not None:
            updated = _merge_identity(record, existing)
            store.upsert_account(updated)
            store.harden_session(name)
            alog.info("会话有效，无需重新扫码", user=updated.label)
            return LoginResult(
                account=name,
                user_id=existing["id"],
                username=existing.get("username"),
                display_name=existing.get("name"),
                phone=existing.get("phone"),
            )
        alog.warning("已有会话不可用，将重新扫码登录")

    # 扫码登录必须开启更新流才能收到 updateLoginToken
    client = build_client(record, settings, paths, no_updates=False)
    proxy = resolve_proxy(record, settings)
    alog.info(
        "准备扫码登录",
        proxy=proxy.to_url() if proxy else "直连",
        api_id=record.api_id or settings.api_id,
        device=device_fingerprint(record)["device_model"],
        session=str(account_paths.session_file),
    )

    try:
        await client.connect()
    except OSError as exc:
        raise QrLoginError(
            f"无法连接 Telegram：{exc}。"
            + ("请检查代理是否可用（tg-assistant proxy-check）。" if proxy else "国内网络通常必须配置 SOCKS5 代理（--proxy）。")
        ) from exc

    #: 在客户端停止前导出，用于随后的 PostgreSQL 备份。
    session_string: Optional[str] = None
    try:
        session = QrLoginSession(
            client,
            alog,
            renderer=renderer,
            password_provider=password_provider,
            timeout=timeout,
        )
        user = await session.run()
        me = await client.get_me()
        # ⚠️ 必须在 ``stop()`` 之前导出：``stop()`` → ``disconnect()`` 会执行
        # ``storage.close()``，而 ``SQLiteStorage.close()`` 只是关掉 sqlite 连接、
        # 不置空 ``self.conn``。之后再调 ``export_session_string()`` 会抛
        # ``sqlite3.ProgrammingError: Cannot operate on a closed database``
        # （部署版把这一步放在 finally 之后，所以那里的 PG 备份其实从未成功过）。
        try:
            session_string = await client.export_session_string()
        except Exception as exc:  # noqa: BLE001 - 导出失败不能影响登录
            alog.warning("导出 session string 失败，将跳过 PostgreSQL 备份", error=str(exc))
    finally:
        with contextlib.suppress(Exception):
            if client.is_initialized:
                await client.stop(block=True)
            elif client.is_connected:
                await client.disconnect()

    identity = {
        "id": me.id,
        "username": me.username,
        "name": " ".join(filter(None, [me.first_name, me.last_name])) or None,
        "phone": mask_phone(getattr(me, "phone_number", None)),
    }
    _guard_duplicate(store, name, identity["id"], alog)
    updated = _merge_identity(record, identity)
    updated = updated.model_copy(update={"last_login_at": utc_now_iso()})
    store.upsert_account(updated)
    store.harden_session(name)
    store.load_account_config(name, create=True)
    # 把 session_string 备份到 PostgreSQL（未配置 TGA_POSTGRES_DSN 时是空操作）。
    # 这样即使本机 .session 文件丢了，也能从库里恢复登录态。
    if session_string:
        try:
            from .client import _save_pg_session_string

            _save_pg_session_string(name, session_string)
            alog.info("会话已备份到 PostgreSQL")
        except Exception as exc:  # noqa: BLE001 - 备份失败不能影响登录
            alog.warning("备份会话到 PostgreSQL 失败", error=str(exc))
    del user

    alog.info(
        "账号登录完成，数据目录已就绪",
        user=updated.label,
        data_dir=str(account_paths.root),
        config=str(account_paths.config_file),
    )
    return LoginResult(
        account=name,
        user_id=identity["id"],
        username=identity["username"],
        display_name=identity["name"],
        phone=identity["phone"],
    )


async def _probe_existing_session(
    record: AccountRecord,
    settings: Settings,
    paths: Paths,
    alog: Any,
) -> Optional[dict[str, Any]]:
    """尝试用现有 session 拉一次 ``get_me``，验证是否仍然有效。"""
    client = build_client(record, settings, paths, no_updates=True)
    try:
        await client.start()
        me = await client.get_me()
        return {
            "id": me.id,
            "username": me.username,
            "name": " ".join(filter(None, [me.first_name, me.last_name])) or None,
            "phone": mask_phone(getattr(me, "phone_number", None)),
        }
    except (AuthKeyUnregistered, Unauthorized, UserDeactivated) as exc:
        alog.warning("会话已失效", error=f"{type(exc).__name__}: {exc}")
        return None
    except OSError as exc:
        alog.warning("网络不可用，无法验证已有会话", error=str(exc))
        return None
    except RPCError as exc:
        alog.warning("验证已有会话时被 Telegram 拒绝", error=f"{type(exc).__name__}: {exc}")
        return None
    finally:
        with contextlib.suppress(Exception):
            if client.is_initialized:
                await client.stop(block=True)
            elif client.is_connected:
                await client.disconnect()


def _merge_identity(record: AccountRecord, identity: dict[str, Any]) -> AccountRecord:
    return record.model_copy(
        update={
            "user_id": identity.get("id"),
            "username": identity.get("username"),
            "display_name": identity.get("name"),
            "phone": identity.get("phone") or record.phone,
        }
    )


def _guard_duplicate(store: Store, name: str, user_id: int, alog: Any) -> None:
    """同一个 Telegram 账号不应挂在两个账号名下，否则数据会混。"""
    for other in store.load_registry().accounts:
        if other.name != name and other.user_id == user_id:
            alog.warning(
                "该 Telegram 账号已经以另一个名字登录过",
                existing_account=other.name,
                user_id=user_id,
                hint="两个账号名指向同一个账号会导致会话互相踢下线，建议只保留一个",
            )


# --------------------------------------------------------------------------- #
# 验证码登录
# --------------------------------------------------------------------------- #
#: 正在进行验证码登录的账号名。
#: 同一个账号并发登录会抢同一个 ``.session`` 文件，必然撞 ``database is locked``，
#: 所以在进程内先挡一道。
_CODE_LOGIN_ACTIVE: set[str] = set()


def _clear_session_files(account_paths: Any, alog: Any) -> None:
    """删掉该账号的 session 文件及其 sqlite 附属文件（只删 session，不删目录）。

    实现在 :func:`tg_assistant.store.clear_session_files` —— 那里有一条必须
    一直生效的告诫：``AccountPaths.session_dir`` 就等于账号根目录，
    对它 ``rmtree`` 会连 ``config.json``（转发规则）一起抹掉。
    """
    for name in clear_session_files(account_paths):
        alog.info("已清理旧的 session 文件", file=name)


class CodeLoginSession:
    """验证码登录（手机号 + 短信验证码 + 可选 2FA）。

    用 ``step()`` 逐步推进，方便在 WebSocket 上分多轮交互：

    * ``await step(None)`` → 提示串：请给手机号
    * ``await step("+8613800000000")`` → 提示串：验证码已发送
    * ``await step(("12345", "2fa密码"))`` → :class:`LoginResult`（完成）
      或提示串（该账号开了两步验证，还缺密码）

    与扫码登录不同，验证码登录随时可能失败或被用户取消，所以 :meth:`close`
    是**必需**的：那个已经连上的 client 会一直占着 ``<name>.session``，
    不放掉的话下次启动就是 ``database is locked``。
    """

    def __init__(
        self,
        name: str,
        store: Store,
        settings: Settings,
        *,
        proxy_override: Optional[Any] = None,
        api_id: Optional[int] = None,
        api_hash: Optional[str] = None,
        force: bool = False,
    ) -> None:
        self.name = name
        self.store = store
        self.settings = settings
        self.paths = store.paths
        # ``force`` 只是为了和 :class:`QrLoginSession` 的构造签名对齐，实际**不生效**：
        # :meth:`_start` 每次都会先 ``_clear_session_files()`` 再新建 client，
        # 所以验证码登录天生就是"强制重新登录"，没有需要跳过的既有会话。
        self._force = force
        self._step = 0
        self._client: Optional[Client] = None
        self._phone: Optional[str] = None
        self._sent: Any = None

        record = store.get_account(name) or AccountRecord(name=name, created_at=utc_now_iso())
        if proxy_override is not None:
            record = record.model_copy(update={"proxy": proxy_override})
        if api_id is not None:
            record = record.model_copy(update={"api_id": api_id})
        if api_hash is not None:
            record = record.model_copy(update={"api_hash": api_hash})

        self._record = record
        self.alog = make_account_logger(name, "login")
        self.account_paths = self.paths.account(name).ensure()

    async def close(self) -> None:
        """释放底层 client 与并发占位（幂等）。取消 / 出错 / 完成时都要调用。"""
        _CODE_LOGIN_ACTIVE.discard(self.name)
        client, self._client = self._client, None
        if client is None:
            return
        with contextlib.suppress(Exception):
            if client.is_initialized:
                await client.stop(block=True)
            elif client.is_connected:
                await client.disconnect()

    @property
    def step_index(self) -> int:
        """当前步骤：0=未开始，1=待发码，2=待验证码，3=待 2FA，4=已结束。"""
        return self._step

    async def resend_code(self, phone: str) -> str:
        """重新发送验证码（仅在「等待验证码」阶段可用）。

        用户输错/超时后要能重发。这里必须显式回到第 1 步 —— 直接把手机号丢给
        :meth:`step` 会在第 2 步被当成 ``(验证码, 密码)`` 解包，行为不可预期。
        """
        if self._step != 2:
            raise QrLoginError("当前不在等待验证码的阶段，无法重发")
        self._step = 1
        self._sent = None
        return await self._send_code(phone)

    async def step(self, data: Any) -> Any:
        """推进登录流程一步。

        ``data`` 的含义取决于当前步骤：``None`` → 手机号 → ``(验证码, 2FA 密码)``。
        返回 ``str`` 表示还要继续，返回 :class:`LoginResult` 表示登录完成。
        """
        if self._step == 4:
            raise QrLoginError("本次登录会话已结束，请重新发起")
        if self._step == 0:
            return await self._start()
        if self._step == 1:
            return await self._send_code(data)
        if self._step == 2:
            return await self._verify_code(data)
        return await self._submit_password(self._split_verify(data)[1])

    # ---------------- 各步骤 ----------------
    async def _start(self) -> str:
        if self.name in _CODE_LOGIN_ACTIVE:
            raise QrLoginError("该账号已有一个验证码登录正在进行，请勿重复发起")
        _CODE_LOGIN_ACTIVE.add(self.name)

        # 先清掉可能残留的旧 session：它多半是上次没走完的登录留下的坏会话，
        # 留着会让 pyrogram 直接拿它去复用。
        _clear_session_files(self.account_paths, self.alog)

        self._client = build_client(self._record, self.settings, self.paths, no_updates=True)
        proxy = resolve_proxy(self._record, self.settings)
        self.alog.info(
            "准备验证码登录",
            proxy=proxy.to_url() if proxy else "直连",
            session=str(self.account_paths.session_file),
        )
        try:
            await self._client.connect()
        except OSError as exc:
            raise QrLoginError(
                f"无法连接 Telegram：{exc}。"
                + (
                    "请检查代理是否可用（tg-assistant proxy-check）。"
                    if proxy
                    else "国内网络通常必须配置 SOCKS5 代理（--proxy）。"
                )
            ) from exc

        self._step = 1
        return "请输入手机号（带国家代码，如 +8613800000000）"

    async def _send_code(self, data: Any) -> str:
        phone = str(data or "").strip()
        if not phone:
            raise QrLoginError("手机号不能为空")

        self._phone = phone
        self.alog.info("正在发送验证码", phone=mask_phone(phone))
        try:
            self._sent = await self._client.send_code(phone)
        except PhoneNumberInvalid as exc:
            raise QrLoginError("手机号格式不正确，请检查国家代码和号码") from exc
        except BadRequest as exc:
            raise QrLoginError(f"发送验证码失败：{exc}") from exc

        self.alog.info("验证码已发送")
        self._step = 2
        return "验证码已发送，请输入验证码"

    async def _verify_code(self, data: Any) -> Any:
        code, password = self._split_verify(data)
        if not code:
            raise QrLoginError("验证码不能为空")

        self.alog.info("正在验证验证码")
        try:
            await self._client.sign_in(self._phone, self._sent.phone_code_hash, code)
        except PhoneCodeExpired as exc:
            # 退回上一步重新发码。
            self._step = 1
            self._sent = None
            raise QrLoginError("验证码已过期，请重新获取") from exc
        except PhoneCodeInvalid as exc:
            raise QrLoginError("验证码错误，请重新输入") from exc
        except SessionPasswordNeeded:
            # 验证码本身已经通过了，只差 2FA 密码。
            # ⚠️ 这里**不能**重放 sign_in —— 同一个验证码只能用一次，
            # 部署版就是每次 verify 都重新 sign_in，所以 2FA 那一步必然报
            # "验证码错误"。
            self._step = 3
            if not password:
                return "该账号开启了两步验证，请输入 2FA 密码"
            return await self._submit_password(password)

        return await self._finalize()

    async def _submit_password(self, password: str) -> Any:
        if not password:
            raise QrLoginError("2FA 密码不能为空")

        self.alog.info("正在验证 2FA 密码")
        try:
            await self._client.check_password(password)
        except PasswordHashInvalid as exc:
            # 部署版这里捕的是 ``PhoneCodeInvalid`` —— 捕错了类型，
            # 于是密码输错会直接冒到上层，前端只能看到
            # "PasswordHashInvalid: ..." 这种原文。
            raise QrLoginError("2FA 密码错误，请重试") from exc

        return await self._finalize()

    # ---------------- 收尾 ----------------
    async def _finalize(self) -> LoginResult:
        """登录成功：写入身份信息并释放 client。"""
        me = await self._client.get_me()
        identity = {
            "id": me.id,
            "username": me.username,
            "name": " ".join(filter(None, [me.first_name, me.last_name])) or None,
            "phone": mask_phone(getattr(me, "phone_number", None)),
        }
        _guard_duplicate(self.store, self.name, identity["id"], self.alog)
        updated = _merge_identity(self._record, identity)
        updated = updated.model_copy(update={"last_login_at": utc_now_iso()})
        self.store.upsert_account(updated)
        self.store.harden_session(self.name)
        self.store.load_account_config(self.name, create=True)

        self._step = 4
        self.alog.info("验证码登录成功", user=updated.label)
        await self.close()

        return LoginResult(
            account=self.name,
            user_id=identity["id"],
            username=identity["username"],
            display_name=identity["name"],
            phone=identity["phone"],
        )

    @staticmethod
    def _split_verify(data: Any) -> tuple[str, str]:
        """把 ``(验证码, 2FA 密码)`` 拆开；也容忍只传验证码。"""
        if isinstance(data, (tuple, list)):
            code = str(data[0] or "").strip() if len(data) > 0 else ""
            password = str(data[1] or "") if len(data) > 1 else ""
            return code, password
        return str(data or "").strip(), ""


# --------------------------------------------------------------------------- #
# 单账号运行
# --------------------------------------------------------------------------- #
class AccountRunner:
    """驱动单个账号的完整运行期。"""

    #: 配置热重载的轮询间隔（秒）。
    #:
    #: 1 秒是「面板点保存 → 群里立刻生效」的手感上限，同时把 ``stat()`` 的开销压到
    #: 可以忽略。**刻意不靠"下一条消息顺带检查"**（转发引擎早期就是这么做的）：
    #: 那样在零流量的群里等于永远不重载，而且一旦 handler 过滤器把新群挡在外面，
    #: 新群的消息永远进不来 ⇒ 检查永远不触发 ⇒ 过滤器永远不更新，死锁。
    CONFIG_RELOAD_INTERVAL = 1.0

    def __init__(
        self,
        record: AccountRecord,
        config: AccountConfig,
        settings: Settings,
        paths: Paths,
        store: Optional[Any] = None,
        shared_dedupe: Optional[Any] = None,
        pair_dedupe: Optional[Any] = None,
        recent_dedupe: Optional[Any] = None,
        metrics: Optional[Any] = None,
    ) -> None:
        self.record = record
        self.config = config
        self.settings = settings
        self.paths = paths
        self.store = store
        #: 跨账号去重表（多账号共享同一个实例）。``None`` = 关闭跨账号去重。
        self.shared_dedupe = shared_dedupe
        #: 「频道 ↔ 群组 同内容」去重表（同样多账号共享）。``None`` = 关闭这一层。
        self.pair_dedupe = pair_dedupe
        #: 「最近已转发的内容」去重表（同样多账号共享）。``None`` = 引擎按账号配置自建。
        self.recent_dedupe = recent_dedupe
        #: 数据大盘（同样多账号共享同一个实例）。``None`` = 各引擎自建纯内存的，
        #: 那种情况下计数不会汇总到面板上 —— 正常路径（CLI / 面板）一定会传。
        self.metrics = metrics
        self.alog = make_account_logger(record.name, "runner")
        self.client: Optional[Client] = None
        self.notifier: Optional[BotNotifier] = None
        self.forwarder: Optional[ForwardEngine] = None
        self.hunter: Optional[RedPacketHunter] = None
        self.reg_grab: Optional[RegGrabHunter] = None
        self.cf_ip_listener: Optional[Any] = None
        #: ``/status`` 指令处理器。仅在启用了通知（有可回话的 bot）时才挂上。
        self.status_command: Optional[StatusCommand] = None
        self.bundle: Optional[ClientBundle] = None
        self.started_at: Optional[float] = None
        self._stopped = asyncio.Event()
        #: 配置热重载用：账号 ``config.json`` 的路径与上次看到/已应用的 mtime。
        #: 三个引擎共用这一份读盘结果（见 :meth:`_reload_config_if_changed`）。
        self._config_file: Optional[Any] = None
        self._config_mtime: Optional[float] = None
        #: 「全局排除」名单（``data/forward_excludes.json``，所有账号共用一份）的
        #: 路径与 mtime。它**不在** config.json 里，所以必须单独盯一份 mtime ——
        #: 只盯 config.json 的话，面板改完全局名单不会触发任何重载，
        #: 用户改完立刻去群里验证，看到没拦住只会以为功能坏了。
        self._global_excludes_file: Optional[Any] = None
        self._global_excludes_mtime: Optional[float] = None
        #: 「已用码拦截」的全局配置（``data/forward_used_codes.json``，跨账号一份）的
        #: 路径与 mtime。同样**不在** config.json 里，必须单独盯一份 mtime ——
        #: 只盯 config.json 的话，面板改完全局已用码策略不会触发任何重载。
        self._global_used_codes_file: Optional[Any] = None
        self._global_used_codes_mtime: Optional[float] = None

    @property
    def name(self) -> str:
        return self.record.name

    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        session_file = self.paths.account(self.name).session_file
        if not session_file.is_file():
            raise ConfigError(
                f"账号 {self.name} 尚未登录（找不到 {session_file}）。"
                f"请先运行：tg-assistant login -a {self.name}"
            )

        needs_updates = self.config.needs_updates
        # 🔴 ``no_updates`` 一律给 False，**不再**按启动时的配置裁剪。
        #
        # 它是"客户端建好就定死"的开关：一旦按启动快照关掉更新流，之后无论怎么
        # 改配置都收不到任何消息 —— 而热重载的承诺恰恰是"面板上打开某个功能，
        # 立刻生效"。用户很自然会「先把抢红包关着填配置，填完再打开」，
        # 若账号启动时刚好什么都没开（``needs_updates`` 为 False），
        # 打开之后就会毫无动静、且没有任何线索 —— 只能重启账号。
        #
        # 代价是空闲账号也会处理更新流；与"配置改完即生效"相比这个代价是值得的。
        self.client = build_client(self.record, self.settings, self.paths, no_updates=False)
        proxy = resolve_proxy(self.record, self.settings)
        self.alog.info(
            "正在启动账号",
            proxy=proxy.to_url() if proxy else "直连",
            updates=needs_updates,
            workers=self.settings.workers,
            forward_rules=len(self.config.forward.active_rules),
            red_packet=self.config.red_packet.enabled,
            reg_grab=self.config.reg_grab.enabled,
            notify=self.config.notify.enabled,
        )

        with self.alog.latency("连接并登录 Telegram"):
            await self.client.start()
        me = await self.client.get_me()
        self.bundle = ClientBundle(
            record=self.record,
            config=self.config,
            client=self.client,
            alog=self.alog,
            me_id=me.id,
            me_username=me.username,
            me_name=" ".join(filter(None, [me.first_name, me.last_name])) or None,
        )
        self.alog.info(
            "账号已在线",
            user=self.bundle.describe(),
            dc=getattr(self.client, "session", None) and getattr(self.client.session, "dc_id", "?"),
        )

        if self.config.notify.enabled:
            self.notifier = BotNotifier(self.config.notify, self.alog, proxy)
            await self.notifier.start()

        # store + account 一并交给引擎，开启**规则热重载**：面板改完规则
        # （写的是同一个 config.json）最迟 RELOAD_CHECK_INTERVAL 秒后自动生效，
        # 不需要再重启账号。
        self.forwarder = ForwardEngine(
            self.client,
            self.config,
            self.alog,
            self.notifier,
            store=self.store,
            account=self.name,
            # 跨账号去重表由 MultiRunner 持有并注入 —— 多个账号共用**同一个实例**，
            # 否则两个账号都在同一个源群里时会把同一条消息各发一遍。
            shared_dedupe=self.shared_dedupe,
            # 同理：「频道 ↔ 群组 同内容」去重表也要共享 —— 频道那条由账号 A 发出去、
            # 群组那条由账号 B 收到时，B 得知道「这条内容已经发过了」才能拦下来。
            pair_dedupe=self.pair_dedupe,
            recent_dedupe=self.recent_dedupe,
            metrics=self.metrics,
        )
        self.forwarder.register()

        self.hunter = RedPacketHunter(
            self.client,
            self.config,
            self.alog,
            self.notifier,
            metrics=self.metrics,
            # 「这条红包已经点过了」必须活过重启：改配置/部署都会重启账号，
            # 只留内存的话那条长驻红包每重启一次就被再点一次（线上实测 8 小时 8 次）。
            settled_path=self.paths.account(self.name).red_packet_settled_file,
        )
        await self.hunter.register()

        self.reg_grab = RegGrabHunter(
            self.client, self.config, self.alog, self.notifier, metrics=self.metrics
        )
        await self.reg_grab.register()

        # ``/status`` 指令：只有启用了通知（有一个能回话的 bot）才有意义 ——
        # 指令是小白在「和这个 bot 的私聊」里打出来的，回复也要经这个 bot 发回去。
        # 没通知就没这个私聊，直接不挂（也就不会白收全部私聊消息）。
        if self.notifier is not None and self.config.notify.enabled:
            self.status_command = StatusCommand(
                bot_token=self.config.notify.bot_token,
                gather=self._status_snapshot,
                send=self._reply_status,
                # 正则测试的试跑接线：读实时 config、用引擎同一个匹配器，
                # 结果只回私聊（绝不转发）。
                probe=self._probe_text,
                alog=self.alog,
                # 回话要发给**本账号自己**：账号会话里和 bot 的私聊，chat.id 是 bot
                # 自己的 id，而 Bot API 认的是对方的 user id。少了它就会 403
                # "the bot can't send messages to the bot"（线上实测静默失效过）。
                self_id=getattr(self.bundle, "me_id", None),
            )
            self.status_command.register(self.client)

        # Cloudflare 优选 IP 实时监听
        if self.config.cloudflare_ip.enabled and self.config.cloudflare_ip.real_time_listen:
            from tg_assistant.cf_ip_listener import CFIPListener

            self.cf_ip_listener = CFIPListener(
                account_name=self.name,
                config=self.config.cloudflare_ip,
                client=self.client,
                alog=self.alog,
                store=self.store,
                settings=self.settings,
                notifier=self.notifier,
            )
            self.cf_ip_listener.register()

        self.started_at = time.time()

        # 配置热重载：记住 config.json 的位置。``_config_mtime`` 刻意留 ``None`` ——
        # 启动后的第一次检查必定读一次盘，这样"启动过程中配置刚好被改过"
        # 也不会漏掉；内容没变时各引擎的指纹比较会直接返回 False，不会白重建。
        if self.store is not None:
            with contextlib.suppress(Exception):
                self._config_file = self.store.paths.account(self.name).config_file
                self._config_mtime = None
                # 全局排除名单（跨账号一份）：同样留 None，首次检查必读一次盘。
                self._global_excludes_file = self.store.paths.forward_excludes_file
                self._global_excludes_mtime = None
                # 全局已用码策略（跨账号一份）：同理，留 None 首次必读。
                self._global_used_codes_file = self.store.paths.forward_used_codes_file
                self._global_used_codes_mtime = None

        if not needs_updates:
            self.alog.warning(
                "当前配置没有任何需要实时监听的功能（转发规则为空、未开启抢红包、"
                "也未开启抢注任务），程序会保持在线但不会做任何事"
            )

    async def stop(self) -> None:
        self.alog.info("正在停止账号")
        if self.status_command is not None and self.client is not None:
            self.status_command.unregister(self.client)
            self.status_command = None
        if self.cf_ip_listener is not None:
            await self.cf_ip_listener.unregister()
            self.cf_ip_listener = None
        if self.forwarder is not None:
            await self.forwarder.close()
        if self.hunter is not None:
            await self.hunter.close()
        if self.reg_grab is not None:
            await self.reg_grab.close()
        if self.notifier is not None:
            await self.notifier.stop()
        if self.client is not None:
            with contextlib.suppress(Exception):
                if self.client.is_initialized:
                    await self.client.stop(block=True)
                elif self.client.is_connected:
                    await self.client.disconnect()
        uptime = time.time() - self.started_at if self.started_at else 0
        self.alog.info("账号已停止", uptime_s=round(uptime, 1))
        self._stopped.set()

    async def run_forever(self, heartbeat: float = 300.0) -> None:
        """保持运行、周期性输出心跳统计，并**顺便**驱动配置热重载。

        循环改成按 :attr:`CONFIG_RELOAD_INTERVAL` 小步醒来：心跳仍按 ``heartbeat``
        的节奏打（用单调时钟记账，不会因为检查间隔而被拖长），中间的空档用来
        看 ``config.json`` 有没有被面板改过。
        """
        next_heartbeat = time.monotonic() + max(heartbeat, 0.0)
        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(
                    self._stopped.wait(), timeout=self.CONFIG_RELOAD_INTERVAL
                )
            except asyncio.TimeoutError:
                pass
            if self._stopped.is_set():
                return
            try:
                await self._reload_config_if_changed()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 热重载出任何岔子都**不能**把账号带下去（``_supervise`` 会把它
                # 当成崩溃然后重启账号，用户会莫名其妙掉线）。记一笔就好。
                self.alog.warning(
                    "配置热重载出错，已跳过这一轮",
                    error=f"{type(exc).__name__}: {exc}",
                )
            if time.monotonic() >= next_heartbeat:
                self._log_heartbeat()
                next_heartbeat = time.monotonic() + max(heartbeat, 0.0)

    # ------------------------------------------------------------------ #
    # 配置热重载
    async def _reload_config_if_changed(self) -> bool:
        """``config.json``（或全局排除名单）变了就把新配置应用到所有引擎。

        返回 True 表示确实应用了。

        🔴 **只为整个账号读一次盘**，再把同一份 ``AccountConfig`` 分发给三个引擎。
        让每个引擎各自去读的话，一次面板保存会触发 3 次读盘 + 解析，更糟的是
        它们可能读到**不同版本**（用户连点两次保存时），于是三个功能的生效状态
        对不上，排查起来毫无头绪。

        「全局排除」名单（``data/forward_excludes.json``）与「全局已用码策略」
        （``data/forward_used_codes.json``）也在这里盯 —— 两者都跨账号一份、都不在
        账号配置里，只盯 ``config.json`` 的话，面板改完它们**不会**触发任何重载，
        用户改完立刻去群里验证、看到没生效只会以为功能坏了。
        """
        if self._config_file is None or self.store is None:
            return False
        try:
            mtime = self._config_file.stat().st_mtime
        except OSError:
            return False
        global_mtime: Optional[float] = None
        if self._global_excludes_file is not None:
            with contextlib.suppress(OSError):
                global_mtime = self._global_excludes_file.stat().st_mtime
        used_codes_mtime: Optional[float] = None
        if self._global_used_codes_file is not None:
            with contextlib.suppress(OSError):
                used_codes_mtime = self._global_used_codes_file.stat().st_mtime
        if (
            mtime == self._config_mtime
            and global_mtime == self._global_excludes_mtime
            and used_codes_mtime == self._global_used_codes_mtime
        ):
            return False
        # 先记下 mtime 再解析：解析失败时不会每一轮都重试同一次坏写，
        # 用户改回一个合法配置就会重新触发（mtime 又变了）。
        self._config_mtime = mtime
        self._global_excludes_mtime = global_mtime
        self._global_used_codes_mtime = used_codes_mtime
        try:
            config = self.store.load_account_config(self.name, create=False)
        except Exception as exc:
            # 面板写坏了配置（非法正则等）时**保留旧配置继续跑** ——
            # 一次坏写不该让转发/抢红包/抢注一起停摆。
            self.alog.warning(
                "配置热重载失败，继续沿用旧配置",
                error=f"{type(exc).__name__}: {exc}",
                hint="改回一份合法配置即可自动恢复",
            )
            return False
        return await self._apply_config(config)

    async def _apply_config(self, config: AccountConfig) -> bool:
        """把新配置分发给三个引擎。返回 True 表示至少有一个真的变了。"""
        changed: list[str] = []
        if self.forwarder is not None and self.forwarder.apply_config(config):
            changed.append("转发")
        if self.hunter is not None and await self.hunter.apply_config(config):
            changed.append("抢红包")
        if self.reg_grab is not None and await self.reg_grab.apply_config(config):
            changed.append("抢注")
        if not changed:
            # 内容其实没变（面板原样保存一次）。mtime 已更新，不会反复重试。
            return False
        self.config = config
        self.alog.info(
            "配置已热重载（无需重启账号）",
            changed="、".join(changed),
            forward_rules=len(self.forwarder.rules) if self.forwarder is not None else 0,
            red_packet_tasks=len(self.hunter.prepared) if self.hunter is not None else 0,
            reg_grab_tasks=len(self.reg_grab.prepared) if self.reg_grab is not None else 0,
        )
        return True

    def _log_heartbeat(self) -> None:
        uptime = time.time() - self.started_at if self.started_at else 0
        fields: dict[str, Any] = {"uptime_s": round(uptime)}
        if self.forwarder is not None:
            snapshot = self.forwarder.snapshot()
            fields.update(
                {
                    "recv": snapshot["received"],
                    "matched": snapshot["matched"],
                    "forwarded": snapshot["forwarded"],
                    "fwd_failed": snapshot["failed"],
                    "deduped": snapshot["deduped"],
                    # 跨账号去重跳过 / forward 自动降级 copy 的次数。
                    # 这两个数字是判断「去重生效没有」「降级生效没有」的**唯一可见口径** ——
                    # 明细日志是 debug 级，线上 TGA_LOG_LEVEL=INFO 看不到。
                    "cross_deduped": snapshot["cross_deduped"],
                    # 「同一内容由频道和群组各发一遍」时，先到的那条留下、后到的直接不发。
                    # 两个数字只差「谁先到」：频道后到 / 群组后到。
                    # 🔴 两者都只是**不发**，不会撤回任何已发出的消息。
                    "pair_deduped": snapshot["pair_deduped"],
                    "pair_blocked": snapshot["pair_blocked"],
                    # 目标里**最近已经转发过相同内容**而被跳过的次数。
                    "recent_deduped": snapshot["recent_deduped"],
                    # 「同内容发往同目标」的**串行闸门**挡下的次数（累计、跨账号共享）。
                    # 这个数字 >0 就说明确实有过并发抢跑、而且被拦住了 —— 否则
                    # 「去重到底有没有挡住并发」在日志里没有任何可直接观察的口径。
                    "gated": (snapshot.get("recent_dedupe") or {}).get("gated", 0),
                    # 启动时从磁盘恢复的去重条数 / 这张表有没有接上落盘。
                    # 🔴 没有这两个数字，「重启后一天内不重复」到底成不成立在线上
                    # **无法自证** —— 而它正是 2026-09-26 那次重复转发的根因
                    # （内存表被一次重启清零）。``dedupe_restored=0`` 配
                    # ``dedupe_persisted=False`` 就是"重启必然失忆"的现场。
                    "dedupe_restored": (snapshot.get("recent_dedupe") or {}).get("restored", 0),
                    "dedupe_persisted": (snapshot.get("recent_dedupe") or {}).get(
                        "persisted", False
                    ),
                    "downgraded": snapshot["downgraded"],
                    # 「已使用注册码」拦截：学到多少条、拦下多少次、现在记着多少条。
                    # 🔴 ``used_known`` 一直是 0 就说明**通知根本没被认出来** ——
                    # 这是「拦不住已用码」时唯一能直接看出问题在哪的数字。
                    "used_learned": snapshot.get("used_learned", 0),
                    "used_skipped": snapshot.get("used_skipped", 0),
                    "used_known": (snapshot.get("used_codes") or {}).get("known", 0),
                }
            )
        if self.hunter is not None:
            snapshot = self.hunter.snapshot()
            fields.update(
                {
                    "rp_detected": snapshot["detected"],
                    "rp_success": snapshot["success"],
                    "rp_failed": snapshot["failed"],
                    "rp_unknown": snapshot["unknown"],
                    "rp_replied": snapshot["replied"],
                    # 「时段外看见红包但没动手」的次数 + 当前是否在时段内。
                    # 半夜照样有红包，这个数字 >0 才说明全局时段真的在起作用；
                    # ``rp_in_window=False`` 能直接回答「为什么一晚上没动静」。
                    "rp_window_skip": snapshot["outside_window"],
                    "rp_in_window": snapshot["in_window"],
                    # 「老消息被编辑」挡下来的次数（``edit_max_age``）—— 这个数字在涨，
                    # 说明那条长驻红包的反复编辑确实没再进流程。
                    "rp_stale_edit_skip": snapshot["stale_edits"],
                    # 重启后从磁盘读回了几条"已经点过了"。刚重启时它是 0，
                    # 就说明落盘没生效 —— 那条长驻红包又要被点一遍。
                    "rp_settled": snapshot["settled"],
                    "rp_settled_restored": snapshot["settled_restored"],
                }
            )
        if self.reg_grab is not None:
            snapshot = self.reg_grab.snapshot()
            fields.update(
                {
                    "rg_detected": snapshot["detected"],
                    "rg_success": snapshot["success"],
                    "rg_partial": snapshot["partial"],
                    "rg_failed": snapshot["failed"],
                    # 同一条码被多个群刷出来时挡下的次数 —— 这个数字能直接说明
                    # code_ttl 去重到底有没有在工作。
                    "rg_dup": snapshot["duplicate_code"],
                    # 使用通知的判定效果：收到多少条通知、据此剔除了多少个码。
                    # 「通知收到不少但剔除一直是 0」通常意味着可见位数不够或正则没对上。
                    "rg_notices": snapshot["usage_notices"],
                    "rg_used_skip": snapshot["used_skipped"],
                    # 监听时段挡下的次数 + 当前是否在时段内。
                    # 「时段外发现码」本该是常态（半夜照样有码，只是我们不抢），
                    # 这个数字 >0 才说明时段真的在起作用。
                    "rg_window_skip": snapshot["outside_window"],
                    "rg_in_window": snapshot["in_window"],
                }
            )
        if self.notifier is not None:
            fields.update({f"notify_{k}": v for k, v in self.notifier.stats.items()})
        self.alog.info("心跳", **fields)

    def snapshot(self) -> dict[str, Any]:
        return {
            "account": self.name,
            "user": self.bundle.describe() if self.bundle else None,
            "uptime_s": round(time.time() - self.started_at) if self.started_at else 0,
            "forward": self.forwarder.snapshot() if self.forwarder else None,
            "red_packet": self.hunter.snapshot() if self.hunter else None,
            "reg_grab": self.reg_grab.snapshot() if self.reg_grab else None,
            "notify": dict(self.notifier.stats) if self.notifier else None,
        }

    # ------------------------------------------------------------------ #
    # /status 指令的取数 + 回话
    # ------------------------------------------------------------------ #
    def _status_snapshot(self) -> StatusData:
        """给 ``/status`` 攒一份面板数据。

        每处取值都用 ``getattr`` / try 兜底：某个引擎没起来、或快照少了个键，
        都退化成 0/关，绝不让一条 ``/status`` 因此抛异常（用户宁可看到 0）。
        实时口径：开关/规则数直接读**当前 config**（热重载后会变），
        大盘读**共享的 MetricsStore**（跨账号汇总的总数）。
        """
        cfg = self.config
        data = StatusData(
            account_label=self.name,
            running=self.client is not None,
            forward_on=bool(cfg.forward.enabled),
            forward_rules=len(cfg.forward.active_rules),
            red_packet_on=bool(cfg.red_packet.enabled),
            red_packet_tasks=len(cfg.red_packet.active_tasks),
            reg_grab_on=bool(cfg.reg_grab.enabled),
            reg_grab_tasks=len(cfg.reg_grab.active_tasks),
        )
        with contextlib.suppress(Exception):
            if self.bundle is not None:
                data.username = getattr(self.bundle, "me_username", None)
        with contextlib.suppress(Exception):
            fwd = self.forwarder.snapshot() if self.forwarder else {}
            data.exclude_chats = int(fwd.get("global_exclude_chats", 0) or 0)
            data.exclude_users = int(fwd.get("global_exclude_users", 0) or 0)
        with contextlib.suppress(Exception):
            if self.metrics is not None:
                data.metrics = self.metrics.totals()
        return data

    async def _reply_status(
        self, chat_id: int, text: str, reply_markup: Optional[dict[str, Any]] = None
    ) -> None:
        """把面板/菜单/测试结果**只**发回发起的那个私聊。

        复用 :class:`BotNotifier` 的底层 ``_call`` —— 拿到它的代理/重试/429 处理，
        但**绕开** ``submit``：``submit`` 会广播给所有配置的通知对象，而这里的语义是
        「谁问，回给谁」，只能发这一个 chat_id。

        ``reply_markup`` 是**按钮菜单**（回复键盘）的 Bot API 原生 dict，由
        :mod:`tg_assistant.bot_commands` 直接给出，这里不做任何转换。

        🔴 必须自己看 ``_call`` 的返回值：它**不抛异常**，只返回 ``(ok, 错误, result)``。
        不看就等于把失败彻底吞掉 —— 线上实测过一次：回话被 Bot API 403 拒了，
        用户界面上「没有回复」，日志里也一片安静，排查只能靠翻更底层的 ERROR 行。
        """
        if self.notifier is None:
            return
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        ok, description, _ = await self.notifier._call("sendMessage", payload)
        if not ok:
            self.alog.warning(
                "回复 /status 失败",
                chat_id=chat_id,
                error=description or "未知错误",
            )

    def _probe_text(self, text: str, pattern: Optional[str] = None) -> ProbeResult:
        """正则测试：在**本地**跑一遍匹配，只回答「会不会命中」。

        - 规则部分用引擎同一个 :class:`CompiledMatcher`（连 ``MatchConfig`` 都是
          规则自己的那一份），所以「测试器说命中」和「真转发时命中」不可能不一致。
        - 自定义正则用引擎同一个 :func:`compile_user_pattern`（同样的
          ``MULTILINE | IGNORECASE``），避免用户按 Python 默认口径试出假结果。
        - 只读配置、只算匹配：**不发消息、不写 used_codes、不转发**。
        """
        result = ProbeResult(sample=text, custom_pattern=pattern)
        rules = list(self.config.forward.rules)
        result.enabled_rules = sum(1 for rule in rules if rule.enabled)
        for rule in rules:
            if not rule.enabled:
                continue
            try:
                outcome = CompiledMatcher(rule.match).match_text(text)
            except Exception as exc:  # 单条规则炸了不能连累其它规则
                self.alog.warning(
                    "正则试跑时规则出错",
                    rule=rule.id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            if outcome.matched:
                result.hits.append(
                    RuleHit(
                        rule_id=str(rule.id),
                        rule_name=getattr(rule, "name", None),
                        pattern=outcome.keyword or "",
                        groups=[g for g in outcome.groups if g],
                    )
                )
        if pattern:
            try:
                found = compile_user_pattern(pattern, ignore_case=True).search(text)
            except re.error as exc:
                result.custom_error = str(exc)
            else:
                result.custom_matched = found is not None
                if found is not None:
                    result.custom_groups = [g for g in found.groups() if g]
        return result


# --------------------------------------------------------------------------- #
# 多账号编排
# --------------------------------------------------------------------------- #
class MultiRunner:
    """并发运行多个账号，单账号故障不影响其它账号。"""

    def __init__(
        self,
        store: Store,
        settings: Settings,
        dedupe: Optional[CrossAccountDedupe] = None,
        pair_dedupe: Optional[Any] = None,
        recent_dedupe: Optional[Any] = None,
        metrics: Optional[Any] = None,
    ) -> None:
        self.store = store
        self.settings = settings
        #: 跨账号去重表：本实例下**所有账号共享同一个**。
        #: 允许外部传入，是因为面板点「启动」会重建 MultiRunner —— 由 RuntimeManager
        #: 拿着同一张表传进来，去重窗口才不会因为一次重启而被清空。
        self.dedupe = dedupe if dedupe is not None else CrossAccountDedupe()
        #: 「频道 ↔ 群组 同内容」去重表，同样是所有账号共享同一个实例、同样要跨面板重建复用。
        self.pair_dedupe = pair_dedupe
        #: 「最近已转发的内容」去重表，同样是所有账号共享同一个实例。
        self.recent_dedupe = recent_dedupe
        #: 数据大盘，同样是所有账号共享同一个实例（用户要的是「总」次数）。
        #: 允许外部传入的理由和上面两张表一样：面板点「启动」会重建 MultiRunner。
        self.metrics = metrics
        self.runners: dict[str, AccountRunner] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._shutdown = asyncio.Event()

    async def run(
        self,
        names: list[str],
        *,
        heartbeat: float = 300.0,
        restart_delay: float = 15.0,
        max_restarts: int = 5,
        install_signals: bool = True,
    ) -> int:
        """启动全部账号并阻塞直到收到退出信号。返回退出码。

        ``install_signals=False`` 供 Web 模式使用：那里 SIGINT/SIGTERM 由
        uvicorn 接管，再注册一次会把 uvicorn 的处理器顶掉，导致 Ctrl-C
        只停账号、Web 服务赖着不走。
        """
        if not names:
            log.error("没有要运行的账号。先执行 login，或用 --account 指定账号")
            return 2

        if install_signals:
            self._install_signal_handlers()
        log.info(
            "启动多账号运行",
            extra={
                "account": "-",
                "extra_fields": {
                    "accounts": ",".join(names),
                    "count": len(names),
                    "heartbeat_s": heartbeat,
                },
            },
        )

        for name in names:
            self._tasks[name] = asyncio.create_task(
                self._supervise(name, heartbeat, restart_delay, max_restarts),
                name=f"account:{name}",
            )

        try:
            await self._shutdown.wait()
            log.info("收到退出信号，开始优雅关闭", extra={"account": "-", "extra_fields": {}})
            return 0
        finally:
            # 放在 finally 里：即使外层直接 cancel 这个任务（Web 停止运行时的兜底路径），
            # 也必须把 client / 通知 worker 收干净，否则会泄漏，
            # 而且下次启动时新旧 client 会争抢同一个 session 文件。
            # shield 保证清理本身不会被二次取消打断。
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(self._cleanup())

    async def _cleanup(self) -> None:
        """取消在途账号任务并关闭所有 client。"""
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

        if self.runners:
            await asyncio.gather(
                *(runner.stop() for runner in self.runners.values()), return_exceptions=True
            )
        log.info("全部账号已停止", extra={"account": "-", "extra_fields": {}})

    async def _supervise(
        self, name: str, heartbeat: float, restart_delay: float, max_restarts: int
    ) -> None:
        """看护单个账号：崩溃自动重启，会话失效则停止并提示重新登录。"""
        alog = make_account_logger(name, "runner")
        restarts = 0
        while not self._shutdown.is_set():
            runner: Optional[AccountRunner] = None
            try:
                record = self.store.require_account(name)
                if not record.enabled:
                    alog.warning("账号已被禁用，跳过", hint="用 accounts enable 重新启用")
                    return
                config = self.store.load_account_config(name)
                runner = AccountRunner(
                    record,
                    config,
                    self.settings,
                    self.store.paths,
                    store=self.store,
                    shared_dedupe=self.dedupe,
                    pair_dedupe=self.pair_dedupe,
                    recent_dedupe=self.recent_dedupe,
                    metrics=self.metrics,
                )
                self.runners[name] = runner
                await runner.start()
                await runner.run_forever(heartbeat=heartbeat)
                return
            except asyncio.CancelledError:
                if runner is not None:
                    with contextlib.suppress(Exception):
                        await runner.stop()
                raise
            except (SessionInvalid, AuthKeyUnregistered, Unauthorized, UserDeactivated) as exc:
                alog.error(
                    "账号会话失效，已停止该账号",
                    error=f"{type(exc).__name__}: {exc}",
                    hint=f"重新登录：tg-assistant login -a {name} --force",
                )
                await self._notify_error(runner, name, f"会话失效：{exc}")
                if runner is not None:
                    with contextlib.suppress(Exception):
                        await runner.stop()
                return
            except (ConfigError, MissingApiCredentials) as exc:
                alog.error("配置问题导致无法启动", error=str(exc))
                return
            except Exception as exc:
                restarts += 1
                if runner is not None:
                    with contextlib.suppress(Exception):
                        await runner.stop()
                if restarts > max_restarts:
                    alog.error(
                        "重启次数超过上限，放弃该账号",
                        restarts=restarts,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    await self._notify_error(runner, name, f"重启超限：{exc}")
                    return
                delay = restart_delay * min(restarts, 4)
                alog.exception(
                    "账号异常退出，稍后重启",
                    error=f"{type(exc).__name__}: {exc}",
                    restart_in_s=delay,
                    restarts=restarts,
                )
                try:
                    await asyncio.wait_for(self._shutdown.wait(), timeout=delay)
                    return
                except asyncio.TimeoutError:
                    continue

    async def _notify_error(
        self, runner: Optional[AccountRunner], name: str, detail: str
    ) -> None:
        if runner is None or runner.notifier is None:
            return
        if not runner.notifier.config.wants("error"):
            return
        runner.notifier.submit(
            NotifyTask(event="error", text=f"⚠️ 账号 <b>{name}</b> 出现问题\n{detail}")
        )

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.add_signal_handler(sig, self._shutdown.set)

    def request_shutdown(self) -> None:
        self._shutdown.set()

    def snapshot(self) -> list[dict[str, Any]]:
        return [runner.snapshot() for runner in self.runners.values()]


__all__ = [
    "AccountBusy",
    "AccountRunner",
    "LoginResult",
    "MultiRunner",
    "login_account",
]
