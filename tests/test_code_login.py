"""验证码登录（``CodeLoginSession``）。

这个流程是从部署版回移过来的，回移时修掉了三个真实缺陷，这里逐一钉住：

1. **不能删账号目录**：部署版用 ``shutil.rmtree(account_paths.session_dir)`` 清 session，
   而 ``session_dir`` 就是账号根目录 —— 于是每次验证码登录都会把 ``config.json``
   （转发规则）和 ``state.json`` 一起抹掉。只能删 session 及其 sqlite 附属文件。
2. **必须释放 client**：验证码登录的 client 跨多轮活着，不主动 ``close()``
   就会一直占着 ``<name>.session`` 的写锁，下次启动撞 ``database is locked``。
3. **2FA 那一步不能重放 sign_in**：验证码是一次性的；部署版每次 verify 都重新
   ``sign_in``，所以开了两步验证的账号必然在第二步报"验证码错误"。
   另外它捕的是 ``PhoneCodeInvalid``，而 ``check_password`` 抛的是
   ``PasswordHashInvalid`` —— 捕错了类型，错误会直接冒到前端。
"""

from __future__ import annotations

import types
from typing import Any

import pytest
from pyrogram.errors import PasswordHashInvalid, PhoneCodeInvalid, SessionPasswordNeeded

from tg_assistant import runner as runner_mod
from tg_assistant.paths import Paths
from tg_assistant.store import Store


class _FakeMe:
    id = 424242
    username = "alice"
    first_name = "张"
    last_name = "三"
    phone_number = "+8613800000000"


class _Sent:
    phone_code_hash = "hash-abc"


class _FakeCodeClient:
    """记录调用顺序，不联网。"""

    def __init__(
        self,
        events: list[str],
        *,
        sign_in_error: Exception | None = None,
        check_password_error: Exception | None = None,
    ) -> None:
        self.name = "acct"
        self._events = events
        self._sign_in_error = sign_in_error
        self._check_password_error = check_password_error
        self.is_initialized = False
        self.is_connected = False
        self.sign_in_calls: list[tuple[Any, ...]] = []

    async def connect(self) -> None:
        self._events.append("connect")
        self.is_connected = True
        self.is_initialized = True

    async def send_code(self, phone: str) -> Any:
        self._events.append("send_code")
        return _Sent()

    async def sign_in(self, phone: str, phone_code_hash: str, code: str) -> Any:
        self._events.append("sign_in")
        self.sign_in_calls.append((phone, phone_code_hash, code))
        if self._sign_in_error is not None:
            raise self._sign_in_error
        return object()

    async def check_password(self, password: str) -> Any:
        self._events.append("check_password")
        if self._check_password_error is not None:
            raise self._check_password_error
        return object()

    async def get_me(self) -> Any:
        self._events.append("get_me")
        return _FakeMe()

    async def stop(self, block: bool = True) -> None:
        self._events.append("stop")
        self.is_initialized = False
        self.is_connected = False

    async def disconnect(self) -> None:
        self._events.append("disconnect")
        self.is_initialized = False
        self.is_connected = False


@pytest.fixture
def settings() -> Any:
    return types.SimpleNamespace(api_id=1, api_hash="hash")


def _wire(monkeypatch: pytest.MonkeyPatch, fake_client: _FakeCodeClient) -> None:
    monkeypatch.setattr(runner_mod, "build_client", lambda *a, **k: fake_client)
    monkeypatch.setattr(runner_mod, "resolve_proxy", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _reset_active_guard():
    """``_CODE_LOGIN_ACTIVE`` 是模块级集合，用例之间必须隔离。"""
    runner_mod._CODE_LOGIN_ACTIVE.clear()
    yield
    runner_mod._CODE_LOGIN_ACTIVE.clear()


def _seed_account(store: Store, paths: Paths, name: str) -> tuple[Any, bytes]:
    """造一个"已经配好规则"的账号目录，返回目录与 config.json 的原始字节。"""
    account = paths.account(name).ensure()
    store.load_account_config(name, create=True)
    config_before = account.config_file.read_bytes()

    account.state_file.write_text('{"cursor": 7}', encoding="utf-8")
    account.session_file.write_bytes(b"stale-session")
    account.session_file.with_name(f"{name}.session-wal").write_bytes(b"wal")
    return account, config_before


async def test_start_clears_session_but_keeps_account_files(
    monkeypatch: pytest.MonkeyPatch, store: Store, paths: Paths, settings: Any
) -> None:
    """回归：清 session 只能删 session，账号目录里的配置必须原封不动。

    部署版在这里 ``shutil.rmtree`` 了整个账号目录，用户的转发规则会被清空，
    随后 ``load_account_config(create=True)`` 再补回一份默认配置 —— 静默重置。
    """
    account, config_before = _seed_account(store, paths, "acct")
    _wire(monkeypatch, _FakeCodeClient([]))

    session = runner_mod.CodeLoginSession("acct", store, settings)
    await session.step(None)
    await session.close()

    assert account.config_file.read_bytes() == config_before, (
        "config.json 被改动了 —— 用户的转发规则会被静默重置"
    )
    assert account.state_file.is_file(), "state.json 不能删"
    assert not account.session_file.exists(), "旧 session 必须清掉"
    assert not account.session_file.with_name("acct.session-wal").exists(), (
        "session 的 sqlite 附属文件也要清掉，否则残留的 WAL 仍会被 sqlite 认领"
    )


async def test_full_login_flow_and_client_released(
    monkeypatch: pytest.MonkeyPatch, store: Store, paths: Paths, settings: Any
) -> None:
    """走完一遍正常流程：client 必须在结束前被 stop 掉。"""
    account, config_before = _seed_account(store, paths, "acct")
    events: list[str] = []
    fake = _FakeCodeClient(events)
    _wire(monkeypatch, fake)

    session = runner_mod.CodeLoginSession("acct", store, settings)

    assert await session.step(None)  # 初始化，返回提示
    assert await session.step("+8613800000000")  # 发码
    result = await session.step(("12345", ""))

    assert result.user_id == _FakeMe.id
    assert events.index("stop") > events.index("get_me"), (
        "client 必须在读取身份之后、流程结束之前被释放；"
        f"实际顺序：{events}"
    )
    assert session.step_index == 4
    assert account.config_file.read_bytes() == config_before, "登录成功后不该重置已有配置"


async def test_two_factor_does_not_replay_sign_in(
    monkeypatch: pytest.MonkeyPatch, store: Store, paths: Paths, settings: Any
) -> None:
    """回归：验证码只验证一次，2FA 那一步不能再 sign_in。

    部署版每次 ``verify`` 都重新 ``sign_in``，而验证码是一次性的，
    于是开了两步验证的账号在第二步必然拿到 ``PhoneCodeInvalid``。
    """
    _seed_account(store, paths, "acct")
    events: list[str] = []
    fake = _FakeCodeClient(events, sign_in_error=SessionPasswordNeeded())
    _wire(monkeypatch, fake)

    session = runner_mod.CodeLoginSession("acct", store, settings)
    await session.step(None)
    await session.step("+8613800000000")

    # 第一次 verify：验证码通过，但账号开了两步验证
    message = await session.step(("12345", ""))
    assert isinstance(message, str) and "2FA" in message

    # 第二次 verify：只补密码
    result = await session.step(("12345", "my-2fa-password"))

    assert result.user_id == _FakeMe.id
    assert fake.sign_in_calls == [("+8613800000000", "hash-abc", "12345")], (
        f"sign_in 被重放了：{fake.sign_in_calls}"
    )
    assert "check_password" in events


async def test_wrong_two_factor_password_is_reported_friendly(
    monkeypatch: pytest.MonkeyPatch, store: Store, paths: Paths, settings: Any
) -> None:
    """回归：2FA 密码错误要给出可读提示，而不是把 pyrogram 异常原文抛给前端。"""
    _seed_account(store, paths, "acct")
    fake = _FakeCodeClient(
        [],
        sign_in_error=SessionPasswordNeeded(),
        check_password_error=PasswordHashInvalid(),
    )
    _wire(monkeypatch, fake)

    session = runner_mod.CodeLoginSession("acct", store, settings)
    await session.step(None)
    await session.step("+8613800000000")
    await session.step(("12345", ""))

    with pytest.raises(runner_mod.QrLoginError, match="2FA 密码错误"):
        await session.step(("12345", "wrong"))

    await session.close()


async def test_wrong_code_is_reported(
    monkeypatch: pytest.MonkeyPatch, store: Store, paths: Paths, settings: Any
) -> None:
    _seed_account(store, paths, "acct")
    fake = _FakeCodeClient([], sign_in_error=PhoneCodeInvalid())
    _wire(monkeypatch, fake)

    session = runner_mod.CodeLoginSession("acct", store, settings)
    await session.step(None)
    await session.step("+8613800000000")

    with pytest.raises(runner_mod.QrLoginError, match="验证码错误"):
        await session.step(("00000", ""))

    await session.close()


async def test_concurrent_code_login_for_same_account_is_blocked(
    monkeypatch: pytest.MonkeyPatch, store: Store, paths: Paths, settings: Any
) -> None:
    """同一账号不允许并发验证码登录 —— 两个 client 会抢同一个 session 写锁。"""
    _seed_account(store, paths, "acct")
    _wire(monkeypatch, _FakeCodeClient([]))

    first = runner_mod.CodeLoginSession("acct", store, settings)
    second = runner_mod.CodeLoginSession("acct", store, settings)

    await first.step(None)
    try:
        with pytest.raises(runner_mod.QrLoginError, match="正在进行"):
            await second.step(None)

        # 放掉第一个之后，第二个应当可以开始
        await first.close()
        await second.step(None)
    finally:
        await first.close()
        await second.close()
