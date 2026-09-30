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
    BTN_HIDE,
    BTN_PANEL,
    BTN_TEST,
    RE_PREFIX,
    STATUS_HANDLER_GROUP,
    ProbeResult,
    RuleProbe,
    StatusCommand,
    StatusData,
    bot_id_from_token,
    build_hide_markup,
    build_menu_markup,
    build_probe_text,
    build_status_text,
    build_test_hint_text,
    is_own_bot_dm,
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
        self.sent: list[tuple[int, str, Any]] = []

    async def __call__(self, chat_id: int, text: str, reply_markup: Any = None) -> None:
        self.sent.append((chat_id, text, reply_markup))


def make_command(alog, send=None, gather=None, self_id=ME_ID, probe=None) -> StatusCommand:
    return StatusCommand(
        bot_token=BOT_TOKEN,
        gather=gather or (lambda: StatusData(account_label="小白", running=True)),
        send=send or Recorder(),
        alog=alog,
        # 本账号自己的 user id —— 回话真正要用的 chat_id（见下）。
        self_id=self_id,
        probe=probe,
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
    async def test_replies_to_account_itself_not_to_bot_id(self, alog) -> None:
        """回话必须发给**本账号的 user id**。

        账号会话里 ``chat.id`` 是 **bot 自己的 id**（判定条件就是 chat.id == bot_id），
        拿它去调 Bot API 等于让 bot 发给自己 —— 线上实测被拒：

            403 Forbidden: the bot can't send messages to the bot

        所以目标必须是 self_id（本账号 user id），而不是 BOT_ID。
        """
        rec = Recorder()
        cmd = make_command(alog, send=rec)
        await cmd._on_message(FakeClient(), status_msg())
        assert len(rec.sent) == 1
        chat_id, text, markup = rec.sent[0]
        assert chat_id == ME_ID
        assert chat_id != BOT_ID
        assert "小白" in text and "🟢运行中" in text
        # 面板回话必须**带着按钮菜单**（用户明确要的「回复下面加上按钮菜单」）。
        assert markup is not None
        assert [btn["text"] for row in markup["keyboard"] for btn in row] == [
            BTN_PANEL,
            BTN_TEST,
            BTN_HIDE,
        ]

    @pytest.mark.asyncio
    async def test_falls_back_to_chat_id_without_self_id(self, alog) -> None:
        """拿不到本账号 id 时退回旧行为（会 403，但绝不该静默什么都不发）。"""
        rec = Recorder()
        cmd = make_command(alog, send=rec, self_id=None)
        await cmd._on_message(FakeClient(), status_msg())
        assert [chat_id for chat_id, _, _ in rec.sent] == [BOT_ID]

    @pytest.mark.asyncio
    async def test_ignores_non_status(self, alog) -> None:
        rec = Recorder()
        cmd = make_command(alog, send=rec)
        await cmd._on_message(FakeClient(), status_msg("/help"))
        await cmd._on_message(FakeClient(), status_msg(outgoing=False))
        assert rec.sent == []

    @pytest.mark.asyncio
    async def test_gather_error_replies_with_notice_not_silence(self, alog) -> None:
        """取数炸了也要**有回音** —— 「没回复」正是这次被投诉的症状。"""
        rec = Recorder()

        def boom() -> StatusData:
            raise RuntimeError("取数炸了")

        cmd = make_command(alog, send=rec, gather=boom)
        await cmd._on_message(FakeClient(), status_msg())  # 不应抛
        assert len(rec.sent) == 1
        assert "取面板数据失败" in rec.sent[0][1]

    @pytest.mark.asyncio
    async def test_send_error_is_swallowed(self, alog) -> None:
        async def boom(chat_id: int, text: str, markup=None) -> None:
            raise RuntimeError("发送炸了")

        cmd = make_command(alog, send=boom)
        await cmd._on_message(FakeClient(), status_msg())  # 不应抛


# --------------------------------------------------------------------------- #
# 回话失败**不能静默**（runner._reply_status）
# --------------------------------------------------------------------------- #
class _FakeAlog:
    """只记 warning 的假日志：真实的 account_logger 要落盘，断言起来太重。"""

    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []
        self.infos: list[tuple[str, dict[str, Any]]] = []

    def warning(self, message: str, **kwargs: Any) -> None:
        self.warnings.append((message, kwargs))

    def info(self, message: str, **kwargs: Any) -> None:
        self.infos.append((message, kwargs))


class _FakeNotifier:
    """假 BotNotifier：``_call`` 只返回元组，绝不抛异常（与真实实现一致）。"""

    def __init__(self, ok: bool, description: str | None = None) -> None:
        self.ok = ok
        self.description = description
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def _call(self, method: str, payload: dict[str, Any], **kwargs: Any):
        self.calls.append((method, payload))
        return self.ok, self.description, None


class TestReplyStatusChecksResult:
    """``notify._call`` 不抛异常、只返回 ``(ok, 错误, result)``。

    线上实测过：回话被 403 拒了，因为没人看返回值，用户界面上「没有回复」、
    日志里也一片安静。所以这里必须自己把失败喊出来。
    """

    @pytest.mark.asyncio
    async def test_logs_warning_when_bot_api_rejects(self) -> None:
        from types import SimpleNamespace

        from tg_assistant.runner import AccountRunner

        alog = _FakeAlog()
        notifier = _FakeNotifier(
            False, "Forbidden: the bot can't send messages to the bot"
        )
        stub = SimpleNamespace(notifier=notifier, alog=alog)
        await AccountRunner._reply_status(stub, ME_ID, "面板")
        assert notifier.calls[0][0] == "sendMessage"
        assert notifier.calls[0][1]["chat_id"] == ME_ID
        assert [item[0] for item in alog.warnings] == ["回复 /status 失败"]
        assert "Forbidden" in alog.warnings[0][1]["error"]

    @pytest.mark.asyncio
    async def test_no_warning_when_ok(self) -> None:
        from types import SimpleNamespace

        from tg_assistant.runner import AccountRunner

        alog = _FakeAlog()
        stub = SimpleNamespace(notifier=_FakeNotifier(True), alog=alog)
        await AccountRunner._reply_status(stub, ME_ID, "面板")
        assert alog.warnings == []

    @pytest.mark.asyncio
    async def test_no_notifier_is_a_noop(self) -> None:
        from types import SimpleNamespace

        from tg_assistant.runner import AccountRunner

        alog = _FakeAlog()
        stub = SimpleNamespace(notifier=None, alog=alog)
        await AccountRunner._reply_status(stub, ME_ID, "面板")  # 不应抛
        assert alog.warnings == []


# --------------------------------------------------------------------------- #
# 按钮菜单（回复键盘）
# --------------------------------------------------------------------------- #
class TestMenuMarkup:
    def test_menu_carries_the_three_buttons(self) -> None:
        markup = build_menu_markup()
        assert [btn["text"] for row in markup["keyboard"] for btn in row] == [
            BTN_PANEL,
            BTN_TEST,
            BTN_HIDE,
        ]
        assert markup["resize_keyboard"] is True

    def test_hide_markup_removes_keyboard(self) -> None:
        assert build_hide_markup() == {"remove_keyboard": True}


class TestIsOwnBotDm:
    def test_true_for_own_outgoing_dm_with_this_bot(self) -> None:
        assert is_own_bot_dm(status_msg(), bot_id=BOT_ID) is True

    def test_false_for_other_chat_incoming_group_and_missing_bot_id(self) -> None:
        assert is_own_bot_dm(status_msg(chat=FakeChat(999)), bot_id=BOT_ID) is False
        assert is_own_bot_dm(status_msg(outgoing=False), bot_id=BOT_ID) is False
        assert is_own_bot_dm(status_msg(), bot_id=None) is False
        assert (
            is_own_bot_dm(status_msg(chat=FakeChat(BOT_ID, chat_type="supergroup")), bot_id=BOT_ID)
            is False
        )


class TestMenuRouting:
    """按钮点一下 = 客户端发一条普通文本，所以路由就是按文本分发（没有 callback）。"""

    @pytest.mark.asyncio
    async def test_test_button_arms_test_mode(self, alog) -> None:
        rec = Recorder()
        cmd = make_command(alog, send=rec, probe=lambda text, pattern: ProbeResult(sample=text))
        await cmd._on_message(FakeClient(), status_msg(BTN_TEST))
        assert len(rec.sent) == 1
        _, text, markup = rec.sent[0]
        assert "正则测试" in text and "不会转发" in text
        assert markup is not None  # 菜单继续挂着，方便连着测

    @pytest.mark.asyncio
    async def test_sample_text_is_probed_in_this_session_only(self, alog) -> None:
        rec = Recorder()
        calls: list[tuple[str, str | None]] = []

        def probe(text: str, pattern: str | None) -> ProbeResult:
            calls.append((text, pattern))
            return ProbeResult(
                sample=text,
                rules=[RuleProbe(rule_id="1", matched=True, pattern="预告", groups=["abc"])],
                enabled_rules=2,
            )

        cmd = make_command(alog, send=rec, probe=probe)
        await cmd._on_message(FakeClient(), status_msg(BTN_TEST))
        await cmd._on_message(FakeClient(), status_msg("🎁 预告 abc"))
        assert calls == [("🎁 预告 abc", None)]
        # 回话只回到这个会话（self_id），内容里给出命中结论
        chat_id, text, _ = rec.sent[-1]
        assert chat_id == ME_ID
        assert "会被转发" in text and "预告" in text

    @pytest.mark.asyncio
    async def test_text_outside_test_mode_stays_silent(self, alog) -> None:
        rec = Recorder()
        cmd = make_command(alog, send=rec, probe=lambda text, pattern: ProbeResult(sample=text))
        await cmd._on_message(FakeClient(), status_msg("随便一句话"))
        assert rec.sent == []

    @pytest.mark.asyncio
    async def test_panel_button_exits_test_mode(self, alog) -> None:
        rec = Recorder()
        cmd = make_command(alog, send=rec, probe=lambda text, pattern: ProbeResult(sample=text))
        await cmd._on_message(FakeClient(), status_msg(BTN_TEST))
        await cmd._on_message(FakeClient(), status_msg(BTN_PANEL))
        assert "运行中" in rec.sent[-1][1]
        sent = len(rec.sent)
        await cmd._on_message(FakeClient(), status_msg("随便一句话"))
        assert len(rec.sent) == sent  # 已退出测试模式 → 不再当样本

    @pytest.mark.asyncio
    async def test_hide_button_removes_keyboard(self, alog) -> None:
        rec = Recorder()
        cmd = make_command(alog, send=rec, probe=lambda text, pattern: ProbeResult(sample=text))
        await cmd._on_message(FakeClient(), status_msg(BTN_HIDE))
        assert rec.sent[-1][2] == {"remove_keyboard": True}

    @pytest.mark.asyncio
    async def test_re_sets_and_clears_custom_pattern(self, alog) -> None:
        rec = Recorder()
        seen: list[str | None] = []

        def probe(text: str, pattern: str | None) -> ProbeResult:
            seen.append(pattern)
            return ProbeResult(sample=text, custom_pattern=pattern)

        cmd = make_command(alog, send=rec, probe=probe)
        await cmd._on_message(FakeClient(), status_msg(f"{RE_PREFIX} \\d+"))
        await cmd._on_message(FakeClient(), status_msg("abc123"))
        await cmd._on_message(FakeClient(), status_msg(RE_PREFIX))  # 清空
        await cmd._on_message(FakeClient(), status_msg("abc123"))
        assert seen == ["\\d+", None]

    @pytest.mark.asyncio
    async def test_probe_failure_still_replies(self, alog) -> None:
        rec = Recorder()

        def boom(text: str, pattern: str | None) -> ProbeResult:
            raise RuntimeError("试跑炸了")

        cmd = make_command(alog, send=rec, probe=boom)
        await cmd._on_message(FakeClient(), status_msg(BTN_TEST))
        await cmd._on_message(FakeClient(), status_msg("样本"))  # 不应抛
        assert "试跑失败" in rec.sent[-1][1]

    @pytest.mark.asyncio
    async def test_probe_not_wired_replies_hint(self, alog) -> None:
        rec = Recorder()
        cmd = make_command(alog, send=rec, probe=None)
        await cmd._on_message(FakeClient(), status_msg(BTN_TEST))
        await cmd._on_message(FakeClient(), status_msg("样本"))
        assert "没接线" in rec.sent[-1][1]


# --------------------------------------------------------------------------- #
# 试跑结果的渲染
# --------------------------------------------------------------------------- #
class TestBuildProbeText:
    def test_hit_shows_rule_pattern_and_groups(self) -> None:
        text = build_probe_text(
            ProbeResult(
                sample="🎁 预告 abc",
                rules=[
                    RuleProbe(
                        rule_id="1",
                        rule_name="主规则",
                        matched=True,
                        pattern="预告",
                        groups=["abc"],
                    )
                ],
                enabled_rules=3,
            )
        )
        assert "会被转发" in text and "命中 1 条规则" in text
        assert "主规则" in text and "预告" in text and "abc" in text

    def test_miss_reports_enabled_rule_count(self) -> None:
        text = build_probe_text(ProbeResult(sample="hello", enabled_rules=2))
        assert "不会转发" in text and "2 条启用规则都没命中" in text

    def test_no_enabled_rules_is_distinguished_from_miss(self) -> None:
        text = build_probe_text(ProbeResult(sample="hello", enabled_rules=0))
        assert "没有启用任何转发规则" in text

    def test_custom_regex_hit_groups_and_invalid_pattern(self) -> None:
        ok = build_probe_text(
            ProbeResult(
                sample="a1",
                custom_pattern="\\d",
                custom_matched=True,
                custom_groups=["1"],
                enabled_rules=1,
            )
        )
        assert "✅ 命中" in ok and "捕获组" in ok
        bad = build_probe_text(
            ProbeResult(
                sample="a1",
                custom_pattern="([",
                custom_error="missing ), unterminated subpattern",
                enabled_rules=1,
            )
        )
        assert "⚠️ 无效" in bad

    def test_html_is_escaped(self) -> None:
        text = build_probe_text(
            ProbeResult(
                sample="<b>粗体</b> & 样本",
                rules=[RuleProbe(rule_id="<script>", matched=True, pattern="<i>", groups=["<x>"])],
                enabled_rules=1,
            )
        )
        assert "<b>粗体</b>" not in text
        assert "&lt;b&gt;" in text and "&amp;" in text
        assert "<script>" not in text and "<i>" not in text

    def test_long_sample_is_clipped(self) -> None:
        text = build_probe_text(ProbeResult(sample="x" * 500, enabled_rules=1))
        assert "…" in text


# --------------------------------------------------------------------------- #
# runner._probe_text：必须和引擎用同一套匹配
# --------------------------------------------------------------------------- #
class TestRunnerProbeUsesEngine:
    """试跑要是自己另写一套 ``re``，就会和真转发给出不同答案 —— 用户只会信测试器。"""

    @staticmethod
    def _runner_like(rules: list[Any]):
        from types import SimpleNamespace

        return SimpleNamespace(
            name="小白",
            config=SimpleNamespace(forward=SimpleNamespace(rules=rules)),
            alog=_FakeAlog(),
        )

    @staticmethod
    def _rule(**kwargs: Any):
        from types import SimpleNamespace

        from tg_assistant.config import MatchConfig

        base: dict[str, Any] = {
            "id": "1",
            "name": None,
            "enabled": True,
            "match": MatchConfig(mode="regex", patterns=["预告"]),
        }
        base.update(kwargs)
        return SimpleNamespace(**base)

    def test_reports_hit_rule_and_captured_groups(self) -> None:
        from tg_assistant.config import MatchConfig
        from tg_assistant.runner import AccountRunner

        rule = self._rule(
            name="主规则",
            match=MatchConfig(mode="regex", patterns=["预告[:：]\\s*([A-Za-z0-9]+)"]),
        )
        result = AccountRunner._probe_text(self._runner_like([rule]), "🎁 预告：abc123")
        assert result.enabled_rules == 1
        assert [hit.rule_id for hit in result.hits] == ["1"]
        assert result.hits[0].groups == ["abc123"]
        assert result.hits[0].rule_name == "主规则"

    def test_disabled_rule_is_ignored(self) -> None:
        from tg_assistant.config import MatchConfig
        from tg_assistant.runner import AccountRunner

        rule = self._rule(enabled=False, match=MatchConfig(mode="regex", patterns=["预告"]))
        result = AccountRunner._probe_text(self._runner_like([rule]), "预告")
        assert result.hits == [] and result.enabled_rules == 0

    def test_exclude_pattern_vetoes_like_the_engine(self) -> None:
        from tg_assistant.config import MatchConfig
        from tg_assistant.runner import AccountRunner

        rule = self._rule(
            match=MatchConfig(
                mode="regex", patterns=["预告"], exclude_patterns=["千万别抽"]
            )
        )
        result = AccountRunner._probe_text(
            self._runner_like([rule]), "预告 机器人测试器 正常用户千万别抽"
        )
        assert result.hits == []  # 排除项是整条消息的一票否决，和引擎一致

    def test_custom_pattern_uses_engine_flags(self) -> None:
        """引擎是 ``MULTILINE | IGNORECASE``：``^`` 要能匹配第二行、大小写要忽略。"""
        from tg_assistant.runner import AccountRunner

        result = AccountRunner._probe_text(
            self._runner_like([]), "第一行\nT.ME/SomeBot", "^t\\.me/somebot$"
        )
        assert result.custom_matched is True

    def test_bad_custom_pattern_reports_error_without_raising(self) -> None:
        from tg_assistant.runner import AccountRunner

        result = AccountRunner._probe_text(self._runner_like([]), "abc", "([")
        assert result.custom_error and result.custom_matched is False

    def test_broken_rule_does_not_hide_other_rules(self) -> None:
        """某条规则在**匹配期**炸掉时，其余规则照样要给答案。

        注意：非法正则在配置校验阶段就被 pydantic 挡掉了（进不了 config），
        所以这里用桩对象模拟「匹配期异常」，覆盖的是 _probe_text 里的兜底分支。
        """
        from tg_assistant.config import MatchConfig
        from tg_assistant.runner import AccountRunner

        class _ExplodingMatch:
            def __getattr__(self, item: str) -> Any:
                raise RuntimeError("匹配期炸了")

        broken = self._rule(id="坏", match=_ExplodingMatch())
        good = self._rule(id="好", match=MatchConfig(mode="regex", patterns=["预告"]))
        result = AccountRunner._probe_text(self._runner_like([broken, good]), "预告")
        assert [hit.rule_id for hit in result.hits] == ["好"]


class TestProbeExplainsWhyItDidNotMatch:
    """「明明转发过、现在测却不命中」几乎都是排除项后来才加上的 —— 必须说清楚。"""

    def test_blocked_by_exclude_is_spelled_out(self) -> None:
        text = build_probe_text(
            ProbeResult(
                sample="🎰 机器人测试器，正常用户千万别抽\n\n🎁 奖品内容",
                rules=[
                    RuleProbe(
                        rule_id="1",
                        matched=False,
                        reason="命中排除规则",
                        blocked_by=["机器人测试器|正常用户千万别抽"],
                    )
                ],
                enabled_rules=1,
            )
        )
        assert "不会转发" in text
        assert "命中排除规则" in text
        assert "机器人测试器|正常用户千万别抽" in text

    def test_plain_miss_shows_engine_reason(self) -> None:
        text = build_probe_text(
            ProbeResult(
                sample="无关文本",
                rules=[RuleProbe(rule_id="1", matched=False, reason="所有正则均未命中")],
                enabled_rules=1,
            )
        )
        assert "所有正则均未命中" in text
        assert "排除项" not in text  # 没有排除项就别提，免得误导

    def test_hit_does_not_dump_miss_detail(self) -> None:
        """命中时不必摊开「为什么没命中」，免得回话变噪音。"""
        text = build_probe_text(
            ProbeResult(
                sample="预告",
                rules=[RuleProbe(rule_id="1", matched=True, pattern="预告")],
                enabled_rules=2,
            )
        )
        assert "会被转发" in text


class TestRunnerProbeReportsBlockingExclude:
    @staticmethod
    def _runner_like(rules: list[Any], alog: Any = None):
        from types import SimpleNamespace

        return SimpleNamespace(
            name="小白",
            config=SimpleNamespace(forward=SimpleNamespace(rules=rules)),
            alog=alog or _FakeAlog(),
        )

    def test_exclude_veto_names_the_pattern(self) -> None:
        from types import SimpleNamespace

        from tg_assistant.config import MatchConfig
        from tg_assistant.runner import AccountRunner

        rule = SimpleNamespace(
            id="1",
            name=None,
            enabled=True,
            match=MatchConfig(
                mode="regex", patterns=["预告"], exclude_patterns=["千万别抽"]
            ),
        )
        result = AccountRunner._probe_text(
            self._runner_like([rule]), "预告 机器人测试器 正常用户千万别抽"
        )
        assert result.hits == []
        assert result.rules[0].reason == "命中排除规则"
        assert result.rules[0].blocked_by == ["千万别抽"]

    def test_probe_is_logged_with_sample_and_reasons(self) -> None:
        """试跑必须留日志 —— 否则用户来问「刚才那条为什么没命中」就是无头案。"""
        from tg_assistant.runner import AccountRunner

        alog = _FakeAlog()
        AccountRunner._probe_text(self._runner_like([], alog), "样本文本" * 100)
        assert [item[0] for item in alog.infos] == ["正则试跑"]
        assert alog.infos[0][1]["sample_len"] == 400
        assert len(alog.infos[0][1]["sample"]) <= 121  # 日志里截断，别灌满磁盘

    def test_exception_rule_is_reported_not_hidden(self) -> None:
        """某条规则试跑炸了也要在明细里露出来，不能悄悄少一条。"""
        from types import SimpleNamespace

        from tg_assistant.runner import AccountRunner

        class _ExplodingMatch:
            def __getattr__(self, item: str) -> Any:
                raise RuntimeError("匹配期炸了")

        rule = SimpleNamespace(id="坏", name=None, enabled=True, match=_ExplodingMatch())
        result = AccountRunner._probe_text(self._runner_like([rule]), "预告")
        assert result.hits == []
        assert "试跑异常" in result.rules[0].reason


class TestProbeNamesTheAccount:
    """每个账号的规则是各自独立的，回话必须说清是在哪个账号上试跑的。"""

    def test_account_label_is_in_the_header(self) -> None:
        text = build_probe_text(
            ProbeResult(sample="预告", account_label="SevenStar", enabled_rules=2)
        )
        assert "SevenStar" in text

    def test_account_label_is_filled_from_runner(self) -> None:
        from types import SimpleNamespace

        from tg_assistant.runner import AccountRunner

        runner = SimpleNamespace(
            name="SevenStar",
            config=SimpleNamespace(forward=SimpleNamespace(rules=[])),
            alog=_FakeAlog(),
        )
        result = AccountRunner._probe_text(runner, "预告")
        assert result.account_label == "SevenStar"


class TestCaptionIsReadLikeTheEngine:
    """带图消息的正文在 caption 里，而引擎的 fields=['text','caption'] 两者都看。

    只读 text 的话，用户转发一张带文字的图来测，测的就是**空字符串** —— 线上
    实测踩过这个坑，症状正是「明明转发过，测出来却没命中」。
    """

    @pytest.mark.asyncio
    async def test_caption_text_is_used_as_sample(self, alog) -> None:
        rec = Recorder()
        seen: list[str] = []

        def probe(text: str, pattern: str | None) -> ProbeResult:
            seen.append(text)
            return ProbeResult(sample=text)

        cmd = make_command(alog, send=rec, probe=probe)
        await cmd._on_message(FakeClient(), status_msg(BTN_TEST))
        # text=None + caption=...：正是「转发带说明文字的图片」的样子
        await cmd._on_message(FakeClient(), status_msg(None, caption="🎁 预告 abc"))
        assert seen == ["🎁 预告 abc"]

    @pytest.mark.asyncio
    async def test_media_without_any_text_still_replies(self, alog) -> None:
        rec = Recorder()
        cmd = make_command(alog, send=rec, probe=lambda text, pattern: ProbeResult(sample=text))
        await cmd._on_message(FakeClient(), status_msg(BTN_TEST))
        await cmd._on_message(FakeClient(), status_msg(None))  # 纯图片，什么都没有
        assert "样本是空的" in rec.sent[-1][1]

    def test_empty_sample_notice_in_render(self) -> None:
        text = build_probe_text(ProbeResult(sample="", enabled_rules=2))
        assert "样本是空的" in text
