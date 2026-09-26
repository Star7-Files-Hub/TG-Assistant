"""正则匹配与模板渲染。"""

from __future__ import annotations

import datetime as dt
import types

import pytest

from tg_assistant.config import MatchConfig
from tg_assistant.matching import (
    _CHAT_KIND,
    CompiledMatcher,
    RefSet,
    apply_groups,
    build_variables,
    button_texts,
    chat_identity,
    USER_PATTERN_FLAGS,
    chat_kind,
    compile_patterns,
    compile_user_pattern,
    first_match,
    message_link,
    message_text,
    normalize_chat_kind,
    render_template,
    sender_of,
    truncate,
)

from .conftest import FakeButton, FakeChat, FakeMarkup, FakeUser, make_message


class TestChatKind:
    """``chat_kind`` 是「未限定 sources 只监听群组/频道」的依据，必须精确。"""

    def test_covers_every_pyrogram_chat_type(self):
        """归一表要覆盖 pyrogram 的全部 ``ChatType``。

        漏掉的类型会归一成 ``None``，而 ``chat_allowed()`` 对 ``None`` 是放行的 ——
        等于悄悄开了一个后门。这里用集合相等，将来 pyrogram 新增枚举值会直接失败。
        """
        from pyrogram.enums import ChatType

        values = {t.value for t in ChatType}
        assert set(_CHAT_KIND) == values
        assert set(_CHAT_KIND.values()) == {"private", "group", "channel"}

    @pytest.mark.parametrize(
        ("chat_type", "expected"),
        [
            ("private", "private"),
            ("bot", "private"),
            # 频道/商务直聊，同样是 1:1，不能当群组放行。
            ("direct", "private"),
            ("group", "group"),
            ("supergroup", "group"),
            # 论坛型超级群；pyrogram 的 filters.group 也算它。
            ("forum", "group"),
            ("channel", "channel"),
        ],
    )
    def test_normalizes(self, chat_type, expected):
        message = make_message("hi", chat=FakeChat(-1001, chat_type=chat_type))
        assert chat_kind(message) == expected

    def test_matches_pyrogram_filters_classification(self):
        """与 pyrogram 自己的分类对齐：group/channel/private 三个过滤器互斥且完备。"""
        from pyrogram.enums import ChatType

        for chat_type in ChatType:
            kind = _CHAT_KIND[chat_type.value]
            if kind == "group":
                assert chat_type in {
                    ChatType.GROUP,
                    ChatType.SUPERGROUP,
                    ChatType.FORUM,
                }
            elif kind == "private":
                assert chat_type in {ChatType.PRIVATE, ChatType.BOT, ChatType.DIRECT}
            else:
                assert chat_type is ChatType.CHANNEL

    def test_unknown_value_returns_none(self):
        message = make_message("hi", chat=FakeChat(-1001, chat_type="brand_new_type"))
        assert chat_kind(message) is None

    def test_normalize_accepts_enum_instance(self):
        """直接传 pyrogram 的 ``ChatType`` 枚举（而不是 ``.value``）也要认。"""
        from pyrogram.enums import ChatType

        assert normalize_chat_kind(ChatType.PRIVATE) == "private"
        assert normalize_chat_kind(ChatType.BOT) == "private"
        assert normalize_chat_kind(ChatType.DIRECT) == "private"
        assert normalize_chat_kind(ChatType.FORUM) == "group"
        assert normalize_chat_kind(ChatType.CHANNEL) == "channel"

    def test_normalize_case_insensitive_but_strict(self):
        """大小写不敏感，但不做 strip —— 认不出来就返回 None，不去猜。"""
        assert normalize_chat_kind("SUPERGROUP") == "group"
        assert normalize_chat_kind("Forum") == "group"
        assert normalize_chat_kind(" private ") is None

    def test_normalize_rejects_non_string(self):
        assert normalize_chat_kind(None) is None
        assert normalize_chat_kind(123) is None
        assert normalize_chat_kind(object()) is None

    def test_message_without_chat_returns_none(self):
        # 注意不能走 make_message：它内部是 ``chat or FakeChat(...)``，
        # 传 None 会被换成默认群，测不到"没有 chat"这个分支。
        assert chat_kind(object()) is None
        assert chat_kind(types.SimpleNamespace(chat=None)) is None
        assert chat_kind(types.SimpleNamespace(chat=types.SimpleNamespace(type=None))) is None


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


class TestUserPatternsAreMultiline:
    """用户写的正则必须**默认**按行匹配。

    🔴 真实反馈：用户照着「一行一个码」写了
    ``^(?!.*(.)\\1{3})[A-Z0-9]{12}$``，在一个三行的消息上一条都匹配不上 ——
    而这条正则在任何在线正则测试工具里都是好的，因为那些工具**默认就按行匹配**。
    没有 ``re.MULTILINE`` 时 ``^`` / ``$`` 只认整段文本的首尾，用户完全看不出
    哪里错了，只会以为「你们的正则引擎坏了」。
    """

    #: 用户实际会收到的消息形状：码独占一行，上下都是别的字。
    MESSAGE = "🎁 注册码\nCZAMTIRLMX2U\n有效期 30 天"

    def test_patterns_get_the_multiline_flag(self):
        assert compile_patterns(["x"])[0].flags & USER_PATTERN_FLAGS
        assert compile_user_pattern("x").flags & USER_PATTERN_FLAGS

    def test_code_on_its_own_line_matches(self):
        """这一条就是用户报的那个场景。"""
        pattern = compile_patterns([r"^(?!.*(.)\1{3})[A-Z0-9]{12}$"])[0]
        assert pattern.search(self.MESSAGE) is not None

    def test_forward_rule_regex_matches_a_line(self):
        """转发任务走的是 ``CompiledMatcher``，也要按行匹配。"""
        config = MatchConfig(mode="regex", patterns=[r"^MSKY-\d+-[A-Za-z]+_[0-9a-f]{10}$"])
        result = CompiledMatcher(config).match_text("🎥 影库\nMSKY-30-Register_ab12cd34ef\n请尽快注册")
        assert result.matched, result.reason

    def test_exclude_patterns_are_multiline_too(self):
        """排除规则不加 MULTILINE 的话，行内的排除项一条都拦不住。"""
        config = MatchConfig(
            mode="regex",
            patterns=["."],
            exclude_patterns=[r"^广告$"],
        )
        assert CompiledMatcher(config).match_text("正常内容\n广告\n正常内容").matched is False

    def test_ignore_case_still_composes(self):
        config = MatchConfig(mode="regex", patterns=["^abc$"], ignore_case=True)
        assert CompiledMatcher(config).match_text("XYZ\nABC\nXYZ").matched is True

    def test_dot_does_not_cross_lines(self):
        """只加 MULTILINE，**不加** DOTALL。

        ``.`` 跨行会把整条消息吞成一个匹配，``.*`` 这类写法立刻变得不可控 ——
        这是刻意不做的，钉在这里防止以后有人"顺手"补上。
        """
        pattern = compile_patterns([r"^a.*b$"])[0]
        assert pattern.search("a\nb") is None
        assert pattern.search("axxb") is not None

    def test_a_pattern_without_anchors_is_unaffected(self):
        """没写 ``^`` / ``$`` 的老正则行为完全不变（MULTILINE 对它们无影响）。"""
        pattern = compile_patterns(["抢到"])[0]
        assert pattern.search("第一行\n恭喜你抢到\n第三行") is not None


class TestWholeMessageAnchors:
    """「整条消息」语义必须用 ``\\A`` / ``\\Z``，``^`` / ``$`` 表达不了。

    🔴 2026-09-26 真实反馈：用户写了

        ^(?=[\\s\\S]*本期尊贵赞助商)(?![\\s\\S]*(?:幸运抽奖结果公布|开奖已揭晓|抽奖即将开奖提醒))[\\s\\S]*$

    用来排除「抽奖即将开奖提醒」，结果**照样转发**了。

    这不是用户写错，而是多行模式的必然结果：``^`` 在**每一行**行首都成立，
    引擎在第 1 行判定失败后会退到第 2 行**重新开始**。而 ``(?![\\s\\S]*关键词)``
    是向**前**看的 —— 从第 2 行起，第 1 行那个关键词已经"在身后"，看不见，
    守卫于是放行，``[\\s\\S]*$`` 把余下内容整个吃掉 → 命中。

    换成白话：**同一份正则，在多行模式下会从「整条消息」悄悄退化成「某一行往后」**，
    而且不报任何错。要表达"整条消息"，只能用 ``\\A`` / ``\\Z``（不受 MULTILINE 影响）。
    """

    #: 真实原文（2026-09-26 23:06 纳泰云官方交流）：要排除的词在第 1 行。
    MESSAGE = (
        "⏰ 【抽奖即将开奖提醒】 🎁\n"
        "\n"
        "👑 本期尊贵赞助商：@Feria5 (马克斯)\n"
        "🏆 抽奖奖品：#1262 US-弗里蒙特 (PEER17-US1)"
    )

    #: 首行同形状、但**不在**排除词里 —— 这条该转，别一起误杀。
    WANTED = (
        "⏳ 【开奖倒计时：30分钟】 🎁\n"
        "\n"
        "👑 本期尊贵赞助商：@blackmao112\n"
        "⏳ 距离开奖还有 30 分钟"
    )

    EXCLUDE = r"(?:幸运抽奖结果公布|开奖已揭晓|抽奖即将开奖提醒)"
    OLD = rf"^(?=[\s\S]*本期尊贵赞助商)(?![\s\S]*{EXCLUDE})[\s\S]*$"
    NEW = rf"\A(?=[\s\S]*本期尊贵赞助商)(?![\s\S]*{EXCLUDE})[\s\S]*\Z"

    def test_caret_version_really_leaks(self):
        """先把病钉住：``^`` 版本会从第 2 行重新命中，守卫形同虚设。"""
        match = compile_user_pattern(self.OLD).search(self.MESSAGE)
        assert match is not None, "旧写法应当（错误地）命中 —— 病没复现说明测试本身失效了"
        assert not match.group(0).startswith("⏰"), (
            "命中位置应当**不是**第 1 行 —— 正是「退到后面某行重新开始」才让守卫失效"
        )

    def test_absolute_version_excludes(self):
        """``\\A`` 版本从整条消息开头检查，守卫才真正生效。"""
        assert compile_user_pattern(self.NEW).search(self.MESSAGE) is None, (
            "首行就写着排除词，绝不该命中"
        )
        assert compile_user_pattern(self.NEW).search(self.WANTED) is not None, (
            "不在排除词里的「开奖倒计时」必须照常命中，不能连坐"
        )

    def test_engine_path_agrees(self):
        """走 ``CompiledMatcher``（引擎真正用的那条路），结论必须一致。"""
        leaking = CompiledMatcher(MatchConfig(mode="regex", patterns=[self.OLD]))
        fixed = CompiledMatcher(MatchConfig(mode="regex", patterns=[self.NEW]))
        assert leaking.match_text(self.MESSAGE).matched is True
        assert fixed.match_text(self.MESSAGE).matched is False
        assert fixed.match_text(self.WANTED).matched is True


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
