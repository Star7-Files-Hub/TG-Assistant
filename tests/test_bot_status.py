"""``/status`` 指令：匹配判定、面板渲染、注册/回话。

这些用例**完全离线**跑 —— 不起 pyrogram、不联网。核心就是要证明：账号自己
发出的那条 ``/status`` 会被认出来并回一份面板，而**别的一切**（别人的私信、
bot 发来的通知回执、群里的 /status、/help）都被默默放过，从而不会干扰共用
这个 bot 的其它项目。
"""

from __future__ import annotations

from typing import Any

import pytest

from tg_assistant.bot_commands import (
    STATUS_HANDLER_GROUP,
    StatusCommand,
    StatusData,
    bot_id_from_token,
    build_status_text,
    is_status_command,
)

from .conftest import FakeChat, FakeClient, FakeUser, make_message

#: 通知 bot 的 token 与其 id（冒号前那段）。小白本人的账号 id 另算。
BOT_TOKEN = "5566778899:AAExampleTokenValue"
BOT_ID = 5566778899
ME_ID = 5608153118


def status_msg(text: str = "/status", **overrides: Any):
    """一条「账号在和 bot 的私聊里、自己发出的」消息。"""
    kwargs: dict[str, Any] = {
        "chat": FakeChat(BOT_ID, username="notify_bot", chat_type="bot"),
        "sender": FakeUser(ME_ID, is_self=True),
        "outgoing": True,
    }
    kwargs.update(overrides)
    return make_message(text, **kwargs)


# --------------------------------------------------------------------------- #
# token → bot id
# --------------------------------------------------------------------------- #
class TestBotIdFromToken:
    def test_parses_leading_segment(self) -> None:
        assert bot_id_from_token(BOT_TOKEN) == BOT_ID

    @pytest.mark.parametrize("bad", [None, "", "no-colon", "abc:def", ":123"])
    def test_bad_tokens_return_none(self, bad: Any) -> None:
        assert bot_id_from_token(bad) is None


# --------------------------------------------------------------------------- #
# 匹配判定
# --------------------------------------------------------------------------- #
class TestIsStatusCommand:
    def test_matches_own_status_in_bot_chat(self) -> None:
        assert is_status_command(status_msg(), bot_id=BOT_ID) is True

    def test_matches_status_with_bot_username_suffix(self) -> None:
        msg = status_msg("/status@notify_bot")
        assert is_status_command(msg, bot_id=BOT_ID) is True

    def test_matches_with_surrounding_whitespace(self) -> None:
        assert is_status_command(status_msg("  /status \n"), bot_id=BOT_ID) is True

    def test_rejects_incoming_message(self) -> None:
        # bot 发给账号的通知回执是 incoming —— 必须排除，否则会自己回自己。
        msg = status_msg(outgoing=False)
        assert is_status_command(msg, bot_id=BOT_ID) is False

    def test_rejects_other_chat(self) -> None:
        # 和别的 bot / 别人的私聊里打 /status 不关本项目的事。
        msg = status_msg(chat=FakeChat(99999, chat_type="bot"))
        assert is_status_command(msg, bot_id=BOT_ID) is False

    def test_rejects_group_chat(self) -> None:
        msg = status_msg(chat=FakeChat(BOT_ID, chat_type="supergroup"))
        assert is_status_command(msg, bot_id=BOT_ID) is False

    @pytest.mark.parametrize("text", ["/start", "/help", "status", "/status extra", "/statuses"])
    def test_rejects_other_commands(self, text: str) -> None:
        assert is_status_command(status_msg(text), bot_id=BOT_ID) is False

    def test_rejects_wrong_bot_username_suffix(self) -> None:
        msg = status_msg("/status@other_bot")
        assert is_status_command(msg, bot_id=BOT_ID) is False

    def test_none_bot_id_never_matches(self) -> None:
        assert is_status_command(status_msg(), bot_id=None) is False

    def test_empty_text_never_matches(self) -> None:
        assert is_status_command(status_msg(text=None), bot_id=BOT_ID) is False


# --------------------------------------------------------------------------- #
# 面板渲染
# --------------------------------------------------------------------------- #
class TestBuildStatusText:
    def test_renders_all_sections(self) -> None:
        data = StatusData(
            account_label="小白",
            username="xiaobai",
            running=True,
            forward_on=True,
            forward_rules=3,
            red_packet_on=False,
            red_packet_tasks=0,
            reg_grab_on=True,
            reg_grab_tasks=2,
            exclude_chats=4,
            exclude_users=5,
            metrics={
                "day": {"forward": 1, "red_packet": 2, "reg_grab": 3},
                "month": {"forward": 10, "red_packet": 20, "reg_grab": 30},
                "total": {"forward": 100, "red_packet": 200, "reg_grab": 300},
            },
        )
        text = build_status_text(data)
        assert "小白 (@xiaobai)" in text
        assert "🟢运行中" in text
        assert "转发 ✅开 · 3 条规则" in text
        assert "抢红包 ⛔关 · 0 个任务" in text
        assert "抢注 ✅开 · 2 个任务" in text
        assert "频道 4 · 发送者 5" in text
        assert "今日 转发 1 · 抢包 2 · 抢注 3" in text
        assert "本月 转发 10 · 抢包 20 · 抢注 30" in text
        assert "累计 转发 100 · 抢包 200 · 抢注 300" in text

    def test_defaults_show_zeros_not_crash(self) -> None:
        # 什么都没取到时也要能渲出一份「全 0/全关」的面板，绝不抛异常。
        text = build_status_text(StatusData())
        assert "🔴未运行" in text
        assert "今日 转发 0 · 抢包 0 · 抢注 0" in text
        assert "累计 转发 0 · 抢包 0 · 抢注 0" in text

    def test_escapes_html_in_account_name(self) -> None:
        # 账号名里的裸标签必须被转义，否则会破坏我们自己拼的 HTML 结构。
        text = build_status_text(StatusData(account_label="<i>x</i>"))
        assert "&lt;i&gt;x&lt;/i&gt;" in text

    def test_partial_metrics_fill_zero(self) -> None:
        data = StatusData(metrics={"day": {"forward": 7}})
        text = build_status_text(data)
        assert "今日 转发 7 · 抢包 0 · 抢注 0" in text
        assert "本月 转发 0 · 抢包 0 · 抢注 0" in text


# --------------------------------------------------------------------------- #
# 注册 / 回话
# --------------------------------------------------------------------------- #
class Recorder:
    """记录 send 被调用的参数（替代真实 BotNotifier._call）。"""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def __call__(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


def make_command(alog, send=None, gather=None) -> StatusCommand:
    return StatusCommand(
        bot_token=BOT_TOKEN,
        gather=gather or (lambda: StatusData(account_label="小白", running=True)),
        send=send or Recorder(),
        alog=alog,
    )


class TestStatusCommandRegistration:
    def test_register_uses_group_three(self, alog) -> None:
        client = FakeClient()
        cmd = make_command(alog)
        cmd.register(client)
        assert len(client.handlers) == 1
        assert client.handlers[0][1] == STATUS_HANDLER_GROUP

    def test_unregister_removes_handler(self, alog) -> None:
        client = FakeClient()
        cmd = make_command(alog)
        cmd.register(client)
        cmd.unregister(client)
        assert client.handlers == []

    def test_bad_token_skips_registration(self, alog) -> None:
        client = FakeClient()
        cmd = StatusCommand(
            bot_token="no-colon",
            gather=lambda: StatusData(),
            send=Recorder(),
            alog=alog,
        )
        cmd.register(client)
        assert client.handlers == []


class TestStatusCommandDispatch:
    @pytest.mark.asyncio
    async def test_replies_only_to_originating_chat(self, alog) -> None:
        rec = Recorder()
        cmd = make_command(alog, send=rec)
        await cmd._on_message(FakeClient(), status_msg())
        assert len(rec.sent) == 1
        chat_id, text = rec.sent[0]
        assert chat_id == BOT_ID
        assert "小白" in text and "🟢运行中" in text

    @pytest.mark.asyncio
    async def test_ignores_non_status(self, alog) -> None:
        rec = Recorder()
        cmd = make_command(alog, send=rec)
        await cmd._on_message(FakeClient(), status_msg("/help"))
        await cmd._on_message(FakeClient(), status_msg(outgoing=False))
        assert rec.sent == []

    @pytest.mark.asyncio
    async def test_gather_error_does_not_send_or_raise(self, alog) -> None:
        rec = Recorder()

        def boom() -> StatusData:
            raise RuntimeError("取数炸了")

        cmd = make_command(alog, send=rec, gather=boom)
        await cmd._on_message(FakeClient(), status_msg())  # 不应抛
        assert rec.sent == []

    @pytest.mark.asyncio
    async def test_send_error_is_swallowed(self, alog) -> None:
        async def boom(chat_id: int, text: str) -> None:
            raise RuntimeError("发送炸了")

        cmd = make_command(alog, send=boom)
        await cmd._on_message(FakeClient(), status_msg())  # 不应抛
