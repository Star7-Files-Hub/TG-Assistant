"""正则匹配与模板渲染。"""

from __future__ import annotations

import datetime as dt

import pytest

from tg_assistant.config import MatchConfig
from tg_assistant.matching import (
    CompiledMatcher,
    RefSet,
    apply_groups,
    build_variables,
    button_texts,
    chat_identity,
    compile_patterns,
    first_match,
    message_link,
    message_text,
    render_template,
    sender_of,
    truncate,
)

from .conftest import FakeButton, FakeChat, FakeMarkup, FakeUser, make_message


class TestMessageText:
    def test_text(self):
        assert message_text(make_message("你好")) == "你好"

    def test_caption_fallback(self):
        assert message_text(make_message(None, caption="图片说明")) == "图片说明"

    def test_empty(self):
        assert message_text(make_message(None)) == ""


class TestButtonTexts:
    def test_inline(self):
        markup = FakeMarkup([[FakeButton("领取"), FakeButton("查看")]])
        assert button_texts(make_message("x", markup=markup)) == ["领取", "查看"]

    def test_none(self):
        assert button_texts(make_message("x")) == []


class TestCompiledMatcher:
    def test_regex_hit_with_groups(self):
        matcher = CompiledMatcher(
            MatchConfig(mode="regex", patterns=[r"金额[:：]\s*([\d.]+)"])
        )
        result = matcher.match(make_message("订单金额：128.50 元"))
        assert result
        assert result.groups == ("128.50",)
        assert result.keyword == r"金额[:：]\s*([\d.]+)"

    def test_named_groups(self):
        matcher = CompiledMatcher(
            MatchConfig(mode="regex", patterns=[r"(?P<coin>[A-Z]{3,5})/(?P<quote>USDT)"])
        )
        result = matcher.match(make_message("BTC/USDT 拉升"))
        assert result.named == {"coin": "BTC", "quote": "USDT"}

    def test_regex_miss_reports_reason(self):
        matcher = CompiledMatcher(MatchConfig(mode="regex", patterns=[r"^\d+$"]))
        result = matcher.match(make_message("abc"))
        assert not result
        assert "未命中" in result.reason

    def test_exclude_wins(self):
        matcher = CompiledMatcher(
            MatchConfig(mode="regex", patterns=["优惠"], exclude_patterns=["测试"])
        )
        assert matcher.match(make_message("优惠活动"))
        result = matcher.match(make_message("优惠活动（测试）"))
        assert not result
        assert "排除" in result.reason

    def test_ignore_case_toggle(self):
        sensitive = CompiledMatcher(
            MatchConfig(mode="regex", patterns=["hello"], ignore_case=False)
        )
        assert not sensitive.match(make_message("HELLO"))
        insensitive = CompiledMatcher(MatchConfig(mode="regex", patterns=["hello"]))
        assert insensitive.match(make_message("HELLO"))

    def test_contains_mode(self):
        matcher = CompiledMatcher(MatchConfig(mode="contains", patterns=["空投", "红包"]))
        assert matcher.match(make_message("有空投消息")).keyword == "空投"
        assert not matcher.match(make_message("无关内容"))

    def test_exact_mode(self):
        matcher = CompiledMatcher(MatchConfig(mode="exact", patterns=["签到"]))
        assert matcher.match(make_message("签到"))
        assert matcher.match(make_message("  签到  "))  # 两端空白不影响
        assert not matcher.match(make_message("签到成功"))

    def test_all_mode(self):
        matcher = CompiledMatcher(MatchConfig(mode="all"))
        assert matcher.match(make_message("随便什么"))

    def test_all_mode_still_respects_exclude(self):
        matcher = CompiledMatcher(MatchConfig(mode="all", exclude_patterns=["广告"]))
        assert matcher.match(make_message("正常消息"))
        assert not matcher.match(make_message("这是广告"))

    def test_min_length(self):
        matcher = CompiledMatcher(MatchConfig(mode="all", min_length=5))
        assert not matcher.match(make_message("abc"))
        assert matcher.match(make_message("abcdef"))

    def test_fields_caption_only(self):
        matcher = CompiledMatcher(
            MatchConfig(mode="contains", patterns=["优惠"], fields=["caption"])
        )
        assert not matcher.match(make_message("优惠"))
        assert matcher.match(make_message(None, caption="优惠"))

    def test_fields_include_buttons(self):
        matcher = CompiledMatcher(
            MatchConfig(mode="contains", patterns=["领取"], fields=["buttons"])
        )
        markup = FakeMarkup([[FakeButton("点我领取")]])
        assert matcher.match(make_message("无关正文", markup=markup))

    def test_empty_text_with_patterns_misses(self):
        matcher = CompiledMatcher(MatchConfig(mode="regex", patterns=["x"]))
        result = matcher.match(make_message(None))
        assert not result
        assert "无可匹配文本" in result.reason


class TestRefSet:
    def test_ids_and_usernames(self):
        refs = RefSet([-100123, "@Channel", "me"])
        assert refs.matches(-100123, None)
        assert refs.matches(None, "channel")
        assert refs.matches(None, "CHANNEL")  # 大小写无关
        assert refs.matches(999, None, is_self=True)
        assert not refs.matches(999, "other")

    def test_empty_is_falsy(self):
        refs = RefSet([])
        assert not refs
        assert not refs.matches(1, "x")


class TestTemplates:
    def test_missing_placeholder_kept(self):
        """模板里写错变量名不能让整条转发崩掉。"""
        assert render_template("值={nope}", {"a": 1}) == "值={nope}"

    def test_render(self):
        text = render_template("[{chat_title}] {text}", {"chat_title": "群", "text": "内容"})
        assert text == "[群] 内容"

    def test_apply_groups(self):
        matcher = CompiledMatcher(MatchConfig(mode="regex", patterns=[r"(\d+)-(\w+)"]))
        result = matcher.match(make_message("123-abc"))
        variables = apply_groups({"text": "123-abc"}, result)
        assert variables["g1"] == "123"
        assert variables["group2"] == "abc"

    def test_build_variables(self):
        chat = FakeChat(-1001234567890, title="公告群", username="notice")
        sender = FakeUser(777, username="bob", first_name="李", last_name="四")
        message = make_message(
            "正文",
            message_id=42,
            chat=chat,
            sender=sender,
            date=dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc),
        )
        variables = build_variables(message)
        assert variables["text"] == "正文"
        assert variables["chat_title"] == "公告群"
        assert variables["chat_id"] == -1001234567890
        assert variables["sender_name"] == "李 四"
        assert variables["sender_username"] == "bob"
        assert variables["message_id"] == 42
        assert variables["link"] == "https://t.me/notice/42"

    def test_extra_overrides(self):
        variables = build_variables(make_message("x"), {"code": "999"})
        assert variables["code"] == "999"


class TestMessageLink:
    def test_public_username(self):
        chat = FakeChat(-1001234567890, username="mychan")
        assert message_link(make_message("x", message_id=9, chat=chat)) == "https://t.me/mychan/9"

    def test_private_channel(self):
        chat = FakeChat(-1001234567890)
        assert (
            message_link(make_message("x", message_id=9, chat=chat))
            == "https://t.me/c/1234567890/9"
        )

    def test_private_user_chat_has_no_link(self):
        chat = FakeChat(555, chat_type="private")
        assert message_link(make_message("x", chat=chat)) is None


class TestIdentity:
    def test_chat_identity(self):
        chat = FakeChat(-100999, title="群", username="G")
        assert chat_identity(make_message("x", chat=chat)) == (-100999, "g", "群")

    def test_sender_user(self):
        sender = FakeUser(5, username="A", is_bot=True)
        sender_id, username, is_self, is_bot = sender_of(make_message("x", sender=sender))
        assert (sender_id, username, is_self, is_bot) == (5, "a", False, True)

    def test_sender_channel_post(self):
        """频道消息没有 from_user，只有 sender_chat。"""
        message = make_message("x", sender=None, sender_chat=FakeChat(-100777, username="ch"))
        sender_id, username, is_self, is_bot = sender_of(message)
        assert sender_id == -100777
        assert username == "ch"
        assert is_self is False
        assert is_bot is False


class TestHelpers:
    def test_compile_and_first_match(self):
        patterns = compile_patterns(["抢到", "恭喜"])
        assert first_match(patterns, "恭喜你抢到") is not None
        assert first_match(patterns, "无关") is None

    def test_compile_empty(self):
        assert compile_patterns([]) == []
        assert first_match([], "任何内容") is None

    @pytest.mark.parametrize(
        ("text", "limit", "expected"),
        [
            ("abcdef", 10, "abcdef"),
            ("abcdef", 3, "abc"),
            ("", 5, ""),
        ],
    )
    def test_truncate(self, text, limit, expected):
        assert truncate(text, limit, "") == expected

    def test_truncate_with_suffix(self):
        assert truncate("abcdefgh", 5, "…") == "abcd…"

    def test_truncate_default_limit_is_safe_for_telegram(self):
        long_text = "字" * 5000
        assert len(truncate(long_text)) <= 4096
