"""抢注任务：配置校验、注册码提取、步骤链执行、去重、通知。

抢注已从「单任务扁平」改成**多任务**（对齐抢红包）：监听会话 / 识别正则 / 步骤链 /
时段全都是**任务级**的，账号级只剩 ``enabled`` / ``tasks`` / ``max_concurrency``，
而去重、并发与通知仍是账号级共享。

本文件的单任务用例分两种写法：

* :func:`rg_config` 造的是**旧扁平**结构 —— 靠配置层的迁移 shim 变成一条
  ``id="default"`` 的任务。这条路径故意保留：服务器现网的 config.json 就是扁平
  结构，迁移坏了会让**整份账号配置**加载失败（不只是抢注）。
* :func:`rg_multi` 造的是**显式多任务**结构，多任务契约的用例全走它。
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from datetime import datetime

import pytest
from pydantic import ValidationError

from tg_assistant.config import (
    AccountConfig,
    RegGrabConfig,
    RegGrabStep,
    RegGrabTask,
    RegGrabWindow,
)
from tg_assistant.metrics import MetricsStore
from tg_assistant.reg_grab import (
    ChainOutcome,
    ChainResult,
    PreparedTask,
    RegGrabHunter,
    StepResult,
    code_value_of,
    extract_code,
    extract_code_detail,
    iter_inline_buttons,
    step_label,
    visible_code_part,
)

from .conftest import (
    FakeButton,
    FakeChat,
    FakeClient,
    FakeMarkup,
    FakeUser,
    make_message,
)

CHAT = -1009999999999
#: 第二个群 —— 用来验证「任务各监听各的群」这类逐任务行为。
OTHER_CHAT = -1007777777777
BOT_CHAT = -1001234500000
CODE = "MSKY-30-Register_Ex5I0Fx5Bg"
CODE_VALUE = "Ex5I0Fx5Bg"
CODE_B = "MSKY-30-Register_JbrMDOxx38"
CODE_VALUE_B = "JbrMDOxx38"
PATTERN = r"Register_([A-Za-z0-9]{10})"


class FakeNotifier:
    """只记录 submit 的内容，不真的发通知。"""

    def __init__(self) -> None:
        self.tasks: list[object] = []

    def submit(self, task: object) -> bool:
        self.tasks.append(task)
        return True


class CapturingLog:
    """把日志收进列表 —— 用来断言「该说的说出来了」。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def bind(self, *args, **kwargs):
        return self

    def _add(self, msg, **kw):
        self.lines.append(msg + " " + " ".join(f"{k}={v}" for k, v in kw.items()))

    def info(self, msg, **kw):
        self._add(msg, **kw)

    def warning(self, msg, **kw):
        self._add(msg, **kw)

    def error(self, msg, **kw):
        self._add(msg, **kw)

    def debug(self, msg, **kw):
        pass

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def rg_config(**overrides) -> AccountConfig:
    """旧版**扁平**抢注配置（单个任务）。

    🔴 这一层**故意保留**：服务器上跑着的 config.json 就是扁平结构，靠
    :func:`_migrate_legacy_reg_grab` 变成一条 ``id="default"`` 的任务。迁移一旦
    坏掉，整份账号配置（转发 / 通知 / 抢红包）都会加载失败，所以单任务行为用例
    继续走这条路径 —— 等于顺手把兼容路径钉死。多任务契约见 :func:`rg_multi`。
    """
    base: dict = {
        "reg_grab": {
            "enabled": True,
            "detect": {"code_pattern": PATTERN},
            "steps": [{"type": "send", "text": "/bind {code}", "chat": "@testbot"}],
            "delay": 0,
            "jitter": 0,
        }
    }
    base["reg_grab"].update(overrides)
    return AccountConfig.model_validate(base)


def rg_task(task_id: str = "t1", **overrides) -> dict:
    """一条任务的原始配置：默认就绪（有提取正则 + 一条发送步骤）、无延迟。"""
    base: dict = {
        "id": task_id,
        "detect": {"code_pattern": PATTERN},
        "steps": [{"type": "send", "text": "/bind {code}", "chat": "@testbot"}],
        "delay": 0,
        "jitter": 0,
    }
    base.update(overrides)
    return base


def rg_multi(*tasks: dict, **overrides) -> AccountConfig:
    """显式多任务配置。``tasks`` 的顺序**就是优先级**（首命中）。"""
    base: dict = {"reg_grab": {"enabled": True, "tasks": list(tasks)}}
    base["reg_grab"].update(overrides)
    return AccountConfig.model_validate(base)


def rg_message(text: str = CODE, *, chat=None, markup=None, sender=None, **kwargs):
    kwargs.setdefault("chat", chat or FakeChat(CHAT, title="码群"))
    kwargs.setdefault("markup", markup)
    if sender is not None:
        kwargs["sender"] = sender
    return make_message(text, **kwargs)


def bot_client(**kwargs) -> FakeClient:
    """登记好 ``@testbot → BOT_CHAT`` 的客户端。"""
    client = FakeClient(**kwargs)
    client.chat_map["testbot"] = FakeChat(BOT_CHAT, title="TestBot")
    return client


def button_markup(*buttons: tuple[str, bytes | None]):
    return FakeMarkup([[FakeButton(text, callback_data=data) for text, data in buttons]])


def build(config: AccountConfig, client=None, alog=None, notifier=None) -> RegGrabHunter:
    return RegGrabHunter(client or bot_client(), config, alog, notifier)


def task_of(hunter: RegGrabHunter, task_id: str | None = None) -> PreparedTask:
    """取引擎里预编译好的任务（默认第一条）。

    引擎里所有动作现在都挂在某条具体任务上（``_run`` / ``_in_window`` /
    ``_is_used`` 都要它），所以单任务用例也要把「哪条任务」显式带出来。
    """
    if task_id is None:
        return hunter.prepared[0]
    return next(task for task in hunter.prepared if task.id == task_id)


# --------------------------------------------------------------------------- #

class TestMetrics:
    """大盘只记成功的抢注（PARTIAL 不算 —— 没注册成就是没成）。"""

    def test_success_only(self, alog):
        metrics = MetricsStore()
        hunter = RegGrabHunter(bot_client(), rg_config(), alog, metrics=metrics)
        task = task_of(hunter)

        hunter._record(task, ChainOutcome(result=ChainResult.SUCCESS, code=CODE, chat_id=CHAT))
        assert metrics.totals()["total"]["reg_grab"] == 1
        hunter._record(task, ChainOutcome(result=ChainResult.PARTIAL, code=CODE, chat_id=CHAT))
        hunter._record(task, ChainOutcome(result=ChainResult.FAILED, code=CODE, chat_id=CHAT))
        hunter._record(task, ChainOutcome(result=ChainResult.SKIPPED, code=CODE, chat_id=CHAT))
        assert metrics.totals()["total"]["reg_grab"] == 1, "只有 SUCCESS 才进大盘"

class TestConfig:
    """配置模型：该拦的拦住，不该拦的别拦。"""

    def test_default_is_disabled_and_not_ready(self) -> None:
        config = AccountConfig()
        assert config.reg_grab.enabled is False
        assert config.reg_grab.tasks == [], "空配置不凭空造任务"
        assert RegGrabTask(id="default").ready is False

    def test_ready_requires_pattern_and_steps(self) -> None:
        assert RegGrabTask(id="a", detect={"code_pattern": PATTERN}).ready is False
        assert (
            RegGrabTask(
                id="a",
                detect={"code_pattern": PATTERN},
                steps=[{"type": "wait", "seconds": 1}],
            ).ready
            is True
        )
        assert RegGrabTask(id="a", enabled=False, detect={"code_pattern": PATTERN}).ready is False

    def test_flat_config_becomes_one_default_task(self) -> None:
        """🔴 兼容路径：旧扁平结构 ⇒ 一条 ``default`` 任务（服务器现网就是它）。"""
        config = rg_config()
        assert config.reg_grab.enabled is True
        assert [task.id for task in config.reg_grab.tasks] == ["default"]
        assert config.reg_grab.tasks[0].label == "默认任务"
        assert config.reg_grab.tasks[0].detect.code_pattern == PATTERN
        assert config.reg_grab.tasks[0].ready is True

    def test_send_step_requires_text(self) -> None:
        with pytest.raises(ValidationError, match="发送内容"):
            RegGrabStep(type="send", chat="@bot")

    def test_click_step_requires_button(self) -> None:
        with pytest.raises(ValidationError, match="按钮文字"):
            RegGrabStep(type="click")

    def test_invalid_code_pattern_rejected(self) -> None:
        with pytest.raises(ValidationError, match="正则无效"):
            RegGrabTask(id="a", detect={"code_pattern": "([unclosed"})
        with pytest.raises(ValidationError, match="正则无效"):
            # 旧扁平结构经迁移 shim 进来同样要拦得住
            RegGrabConfig(detect={"code_pattern": "([unclosed"})

    def test_invalid_step_button_regex_rejected(self) -> None:
        with pytest.raises(ValidationError, match="正则无效"):
            RegGrabStep(type="click", button="[unclosed")

    def test_used_pattern_must_be_valid_regex(self) -> None:
        with pytest.raises(ValidationError, match="正则无效"):
            RegGrabTask(id="a", detect={"code_pattern": PATTERN, "used_pattern": "([unclosed"})

    def test_used_pattern_has_a_default(self) -> None:
        """开箱即用：不填也能认出「使用了 XXX」这种通知。"""
        assert RegGrabTask(id="a").detect.used_pattern == r"使用[了]?\s*([A-Za-z0-9][^\s，。、]*)"

    def test_default_used_pattern_skips_the_header_word(self) -> None:
        """🔴 回归：通知里 ``使用`` 出现两次，标题那个「注册码使用 - jf」不能被抓成码。

        默认正则若写成 ``使用[了]?\\s*(\\S+)``，第一个匹配就是标题里的 ``-``，
        可见部分为空 —— 虽然会被 ``used_min_len`` 挡掉，但那是撞运气，不是设计。
        """
        pattern = re.compile(RegGrabTask(id="a").detect.used_pattern)
        tokens = [next((g for g in m.groups() if g), m.group(0)) for m in pattern.finditer(USAGE_NOTICE)]
        assert tokens == ["MSKY-30-Register_f1t░░░░░░░"], tokens

    def test_blank_used_pattern_disables_the_check(self) -> None:
        assert RegGrabTask(id="a", detect={"used_pattern": "   "}).detect.used_pattern is None

    def test_used_min_len_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            RegGrabTask(id="a", detect={"used_min_len": 0})

    def test_blank_strings_become_none(self) -> None:
        step = RegGrabStep(type="wait", text="   ", button="", pattern="")
        assert step.text is None and step.button is None and step.pattern is None

    def test_step_chat_is_normalized(self) -> None:
        assert RegGrabStep(type="send", text="x", chat="@MyBot").chat == "mybot"
        assert RegGrabStep(type="send", text="x", chat="-1001234567890").chat == -1001234567890
        assert RegGrabStep(type="send", text="x", chat="https://t.me/c/123/5").chat == -100123

    def test_chats_are_normalized(self) -> None:
        task = RegGrabTask(id="a", chats=["@MyGroup", -1001234567890])
        assert task.chats == ["mygroup", -1001234567890]
        # 旧扁平结构经迁移进来也要归一化（服务器上是这条路径）
        legacy = RegGrabConfig.model_validate({"chats": ["@MyGroup", -1001234567890]})
        assert legacy.tasks[0].chats == ["mygroup", -1001234567890]

    def test_enabled_toggle_does_not_raise_on_incomplete_config(self) -> None:
        """面板总开关就是一次赋值 —— 模型校验不能把它拦死（否则 500）。"""
        config = AccountConfig.model_validate({"reg_grab": {"tasks": [{"id": "a"}]}})
        config.reg_grab.enabled = True
        assert config.reg_grab.enabled is True
        assert config.reg_grab.tasks[0].ready is False

    def test_needs_updates_and_watched_chats_include_reg_grab(self) -> None:
        config = AccountConfig.model_validate(
            {"reg_grab": {"enabled": True, "chats": [-1001234567890]}}
        )
        assert config.needs_updates is True
        assert -1001234567890 in config.watched_chats()

    def test_empty_chats_means_watch_everything(self) -> None:
        config = AccountConfig.model_validate({"reg_grab": {"enabled": True, "chats": []}})
        assert config.watched_chats() == []


# --------------------------------------------------------------------------- #
class TestAutoTaskId:
    """任务 ID 不必填：留空/缺省自动生成、落盘稳定、同名不撞车。

    用户原话：「抢注的任务ID不要做成必填，名称填了就行，ID自动生成一个」。
    """

    def test_blank_id_is_generated_from_the_name(self) -> None:
        config = RegGrabConfig(tasks=[{"name": "主群 Register #1"}])
        assert config.tasks[0].id == "register-1", "名称里的 ASCII 片段要拿来做 id"

    def test_missing_id_key_also_generates(self) -> None:
        """「缺省」也得能用：面板新增一条任务时发上来的就是不带 id 的对象。"""
        config = RegGrabConfig(tasks=[{}])
        assert config.tasks[0].id

    def test_chinese_name_falls_back_to_a_short_random_id(self) -> None:
        config = RegGrabConfig(tasks=[{"name": "主群注册码"}])
        assert re.fullmatch(r"task-[0-9a-f]{8}", config.tasks[0].id), config.tasks[0].id

    def test_explicit_id_wins(self) -> None:
        config = RegGrabConfig(tasks=[{"id": "mine", "name": "别的名字"}])
        assert config.tasks[0].id == "mine"

    def test_same_name_twice_does_not_collide(self) -> None:
        """🔴 复制一条任务改改是常规操作：两条同名任务不能生成同一个 id。

        生成若放在 ``RegGrabTask`` 自己的校验器里（看不到兄弟任务），这里就会撞上
        唯一性校验，用户只看到「保存失败」——所以生成必须在父模型里做。
        """
        first, second = RegGrabConfig(tasks=[{"name": "Main"}, {"name": "Main"}]).tasks
        assert first.id == "main"
        assert second.id != first.id and second.id.startswith("main")

    def test_generated_id_avoids_an_explicit_one(self) -> None:
        config = RegGrabConfig(tasks=[{"id": "main"}, {"name": "Main"}])
        assert config.tasks[1].id != "main"

    def test_two_chinese_names_get_two_ids(self) -> None:
        config = RegGrabConfig(tasks=[{"name": "群一"}, {"name": "群二"}])
        ids = [task.id for task in config.tasks]
        assert len(set(ids)) == 2, ids

    def test_survives_a_dump_validate_round_trip(self) -> None:
        """🔴 重新加载后必须**不变**：生成只在「这条任务还没有 id」时发生。"""
        config = RegGrabConfig(tasks=[{"name": "主群"}, {"name": "备用群"}])
        ids = [task.id for task in config.tasks]
        again = RegGrabConfig.model_validate(config.model_dump(mode="json"))
        assert [task.id for task in again.tasks] == ids

    def test_duplicate_explicit_ids_are_still_rejected(self) -> None:
        """自动生成不能顺手把「用户手打的重复 id」也放过去。"""
        with pytest.raises(ValidationError, match="重复"):
            RegGrabConfig(tasks=[{"id": "same"}, {"id": "same"}])

    def test_id_is_trimmed_and_must_be_a_string(self) -> None:
        assert RegGrabConfig(tasks=[{"id": "  padded  "}]).tasks[0].id == "padded"
        with pytest.raises(ValidationError, match="字符串"):
            RegGrabConfig(tasks=[{"id": 123}])

    def test_flat_legacy_config_still_gets_default_id(self) -> None:
        """兼容路径别被带坏：旧扁平结构迁移出来的那条仍然是 ``default``。"""
        assert [t.id for t in rg_config().reg_grab.tasks] == ["default"]


# --------------------------------------------------------------------------- #
class TestDetection:
    def test_extracts_capture_group(self, alog) -> None:
        hunter = build(rg_config(), alog=alog)
        assert hunter._should_grab(rg_message(), CHAT) == CODE_VALUE

    def test_multi_line_code_list_is_detected(self, alog) -> None:
        """回归：多行码列表里的第一条也要能提取到。

        这正是线上漏掉 ``MSKY-30-Register_Ex5I0Fx5Bg`` 的那种消息形态。
        """
        hunter = build(rg_config(), alog=alog)
        text = "MSKY-30-Register_Ex5I0Fx5Bg\nMSKY-30-Register_JbrMDOxx38"
        assert hunter._should_grab(rg_message(text), CHAT) == CODE_VALUE

    def test_whole_match_when_no_capture_group(self, alog) -> None:
        hunter = build(rg_config(detect={"code_pattern": r"[A-Z]+-\d+-\w+"}), alog=alog)
        assert hunter._should_grab(rg_message(CODE), CHAT) == CODE

    def test_no_code_returns_none(self, alog) -> None:
        hunter = build(rg_config(), alog=alog)
        assert hunter._should_grab(rg_message("今天天气不错"), CHAT) is None

    def test_exclude_chats_blocks(self, alog) -> None:
        hunter = build(rg_config(exclude_chats=[CHAT]), alog=alog)
        assert hunter._should_grab(rg_message(), CHAT) is None

    def test_chats_whitelist_blocks_other_chats(self, alog) -> None:
        hunter = build(rg_config(chats=[OTHER_CHAT]), alog=alog)
        assert hunter._should_grab(rg_message(), CHAT) is None

    def test_ignore_self_blocks_own_message(self, alog) -> None:
        hunter = build(rg_config(), alog=alog)
        msg = rg_message(sender=FakeUser(1, username="me", is_self=True))
        assert hunter._should_grab(msg, CHAT) is None

    def test_only_from_bots(self, alog) -> None:
        hunter = build(rg_config(detect={"code_pattern": PATTERN, "only_from_bots": True}), alog=alog)
        assert hunter._should_grab(rg_message(), CHAT) is None
        bot_msg = rg_message(sender=FakeUser(9, is_bot=True))
        assert hunter._should_grab(bot_msg, CHAT) == CODE_VALUE

    def test_text_patterns_prefilter(self, alog) -> None:
        hunter = build(
            rg_config(detect={"code_pattern": PATTERN, "text_patterns": ["注册码"]}), alog=alog
        )
        assert hunter._should_grab(rg_message(), CHAT) is None
        assert hunter._should_grab(rg_message("新的注册码 " + CODE), CHAT) == CODE_VALUE

    def test_service_message_ignored(self, alog) -> None:
        hunter = build(rg_config(), alog=alog)
        msg = rg_message(service=object())
        assert hunter._should_grab(msg, CHAT) is None

    def test_watch_chats_includes_step_targets(self, alog) -> None:
        """步骤链里点名的机器人私聊也要收得到消息，否则 wait_reply 永远等不到回执。"""
        hunter = build(rg_config(chats=[CHAT]), alog=alog)
        assert hunter._watch_chats() == [CHAT, "testbot"]

    def test_watch_chats_empty_means_no_filter(self, alog) -> None:
        hunter = build(rg_config(chats=[]), alog=alog)
        assert hunter._watch_chats() == []


# --------------------------------------------------------------------------- #
#: 用户报的那条正则与他给的消息正文（原话：「我想要 XYING-Whitelist_4rLuucEgs5，
#: 但只匹配到 4rLuucEgs5，我的正则是 (?:Whitelist)_([A-Za-z0-9]{10})」）。
#: 捕获组只括住后缀 ⇒ 引擎按契约只交出后缀 —— 这不是 bug，但面板必须讲清楚。
USER_PATTERN = r"(?:Whitelist)_([A-Za-z0-9]{10})"
USER_TEXT = "🎟️ 注册码使用 - jf [7002057019] 使用了 XYING-Whitelist_4rLuucEgs5 已绑定"
USER_FULL = "Whitelist_4rLuucEgs5"
USER_CODE = "4rLuucEgs5"
#: 面板给出的改法：把整串括起来，就拿到整串。
USER_FIXED_PATTERN = r"(XYING-Whitelist_[A-Za-z0-9]{10})"
USER_WHOLE = "XYING-Whitelist_4rLuucEgs5"


class TestExtractCode:
    """``extract_code``：第一个非空捕获组；没有捕获组才用整段（语义**保持不变**）。"""

    def test_user_pattern_only_yields_the_suffix(self) -> None:
        """用户的原始正则拿到后缀 —— 这正是他反馈的现象，契约如此。"""
        assert extract_code(USER_PATTERN, USER_TEXT) == USER_CODE

    def test_wrapping_the_whole_thing_yields_the_whole_code(self) -> None:
        """按面板提示把整串括起来 ⇒ 拿到整串（不用改引擎）。"""
        assert extract_code(USER_FIXED_PATTERN, USER_TEXT) == USER_WHOLE

    def test_no_capture_group_uses_the_whole_match(self) -> None:
        assert extract_code(r"MSKY-\d+-Register_\w+", CODE) == CODE

    def test_multiple_groups_take_the_first_non_empty(self) -> None:
        """第 1 组用 ``(x)?`` 写成可选、这次没参与匹配 ⇒ 落到第 2 组。"""
        assert extract_code(r"(?:pre_)(x)?([A-Za-z0-9]{5})", "pre_abcde") == "abcde"

    def test_no_match_returns_none(self) -> None:
        assert extract_code(USER_PATTERN, "今天天气不错") is None

    def test_none_pattern_returns_none(self) -> None:
        """任务没填提取正则时引擎走的就是这条路（``code_pattern is None``）。"""
        assert extract_code(None, USER_TEXT) is None

    def test_compiled_pattern_works_too(self) -> None:
        """引擎传的是预编译好的正则；字符串与正则对象两种入参都要收。"""
        assert extract_code(re.compile(USER_PATTERN), USER_TEXT) == USER_CODE

    def test_engine_still_agrees_with_the_module_function(self, alog) -> None:
        """🔴 引擎的 ``_extract_code`` 已改成转调本函数，行为必须一模一样。"""
        hunter = build(rg_config(detect={"code_pattern": USER_PATTERN}), alog=alog)
        assert hunter._should_grab(rg_message(USER_TEXT), CHAT) == USER_CODE

    def test_detail_reports_full_match_groups_and_group_number(self) -> None:
        assert extract_code_detail(USER_PATTERN, USER_TEXT) == {
            "matched": True,
            "full": USER_FULL,
            "groups": [USER_CODE],
            "code": USER_CODE,
            "from_group": 1,
        }

    def test_detail_says_whole_match_when_there_is_no_group(self) -> None:
        detail = extract_code_detail(r"MSKY-\d+-Register_\w+", CODE)
        assert detail["code"] == detail["full"] == CODE
        assert detail["groups"] == []
        assert detail["from_group"] == 0, "0 表示整段匹配"

    def test_detail_without_any_match(self) -> None:
        detail = extract_code_detail(USER_PATTERN, "今天天气不错")
        assert detail["matched"] is False
        assert detail["code"] is None and detail["full"] is None
        assert detail["groups"] == [] and detail["from_group"] is None

    def test_detail_keeps_empty_optional_groups_visible(self) -> None:
        """``(x)?`` 没参与匹配时留 ``None`` —— 面板要能显示「第 1 组：（空）」。"""
        detail = extract_code_detail(r"(?:pre_)(x)?([A-Za-z0-9]{5})", "pre_abcde")
        assert detail["groups"] == [None, "abcde"]
        assert detail["from_group"] == 2


# --------------------------------------------------------------------------- #
class TestSteps:
    """步骤链的每一步。"""

    @pytest.mark.asyncio
    async def test_send_renders_code_and_targets_chat(self, alog) -> None:
        client = bot_client()
        hunter = build(rg_config(), client=client, alog=alog)
        outcome = await hunter._run(task_of(hunter), rg_message(), CODE_VALUE, time.perf_counter())

        assert outcome.result is ChainResult.SUCCESS
        assert len(client.sent) == 1
        assert client.sent[0]["text"] == f"/bind {CODE_VALUE}"
        assert client.sent[0]["chat_id"] == BOT_CHAT

    @pytest.mark.asyncio
    async def test_send_without_chat_goes_to_source_chat(self, alog) -> None:
        client = FakeClient()
        hunter = build(rg_config(steps=[{"type": "send", "text": "收到 {code}"}]), client=client, alog=alog)
        await hunter._run(task_of(hunter), rg_message(), CODE_VALUE, time.perf_counter())
        assert client.sent[0]["chat_id"] == CHAT

    @pytest.mark.asyncio
    async def test_send_falls_back_when_chat_cannot_be_resolved(self, alog) -> None:
        """解析不到会话也要把消息发出去 —— 只发不等的链照样能用。"""
        client = FakeClient()
        hunter = build(rg_config(), client=client, alog=alog)
        outcome = await hunter._run(task_of(hunter), rg_message(), CODE_VALUE, time.perf_counter())
        assert outcome.result is ChainResult.SUCCESS
        assert client.sent[0]["chat_id"] == "testbot"

    @pytest.mark.asyncio
    async def test_wait_step_sleeps(self, alog) -> None:
        client = FakeClient()
        hunter = build(rg_config(steps=[{"type": "wait", "seconds": 0.05}]), client=client, alog=alog)
        started = time.perf_counter()
        outcome = await hunter._run(task_of(hunter), rg_message(), CODE_VALUE, started)
        assert outcome.result is ChainResult.SUCCESS
        assert outcome.cost_ms >= 50

    @pytest.mark.asyncio
    async def test_wait_reply_matches_bot_reply(self, alog) -> None:
        client = bot_client()
        hunter = build(
            rg_config(
                steps=[
                    {"type": "send", "text": "/bind {code}", "chat": "@testbot"},
                    {"type": "wait_reply", "pattern": "成功", "timeout": 1.0},
                ]
            ),
            client=client,
            alog=alog,
        )

        async def feed() -> None:
            await asyncio.sleep(0.05)
            hunter._bus.feed(
                BOT_CHAT,
                make_message(
                    "✅ 绑定成功", chat=FakeChat(BOT_CHAT, title="TestBot"), sender=FakeUser(9, is_bot=True)
                ),
            )

        feeder = asyncio.create_task(feed())
        outcome = await hunter._run(task_of(hunter), rg_message(), CODE_VALUE, time.perf_counter())
        await feeder

        assert outcome.result is ChainResult.SUCCESS
        assert [step.type for step in outcome.steps] == ["send", "wait_reply"]

    @pytest.mark.asyncio
    async def test_wait_reply_ignores_own_messages(self, alog) -> None:
        """自己发出去的 ``/bind MSKY-...`` 不能被当成机器人的回执。"""
        client = bot_client()
        hunter = build(
            rg_config(
                steps=[
                    {"type": "send", "text": "/bind {code}", "chat": "@testbot"},
                    {"type": "wait_reply", "pattern": "Ex5I0Fx5Bg", "timeout": 0.25},
                ]
            ),
            client=client,
            alog=alog,
        )

        async def feed() -> None:
            await asyncio.sleep(0.05)
            hunter._bus.feed(
                BOT_CHAT,
                make_message(
                    f"/bind {CODE}",
                    chat=FakeChat(BOT_CHAT, title="TestBot"),
                    sender=FakeUser(1, username="me", is_self=True),
                ),
            )

        feeder = asyncio.create_task(feed())
        outcome = await hunter._run(task_of(hunter), rg_message(), CODE_VALUE, time.perf_counter())
        await feeder

        assert outcome.result is ChainResult.FAILED
        assert "没有等到匹配" in outcome.steps[-1].detail

    @pytest.mark.asyncio
    async def test_wait_reply_timeout_fails_chain(self, alog) -> None:
        client = bot_client()
        hunter = build(
            rg_config(
                steps=[
                    {"type": "send", "text": "/bind {code}", "chat": "@testbot"},
                    {"type": "wait_reply", "pattern": "成功", "timeout": 0.1},
                ]
            ),
            client=client,
            alog=alog,
        )
        outcome = await hunter._run(task_of(hunter), rg_message(), CODE_VALUE, time.perf_counter())
        assert outcome.result is ChainResult.FAILED
        assert outcome.steps[-1].result is StepResult.FAILED

    @pytest.mark.asyncio
    async def test_click_uses_current_message(self, alog) -> None:
        client = bot_client(callback_answer="领取成功")
        hunter = build(
            rg_config(steps=[{"type": "click", "button": "领取", "chat": "@testbot"}]),
            client=client,
            alog=alog,
        )
        msg = rg_message(markup=button_markup(("🎁 领取", b"claim")))
        outcome = await hunter._run(task_of(hunter), msg, CODE_VALUE, time.perf_counter())

        assert outcome.result is ChainResult.SUCCESS
        assert client.callbacks
        assert client.callbacks[0]["callback_data"] == b"claim"

    @pytest.mark.asyncio
    async def test_click_falls_back_to_recent_message_in_target_chat(self, alog) -> None:
        """当前消息没按钮时，退回目标会话最近一条带按钮的消息。"""
        client = bot_client(callback_answer="ok")
        hunter = build(
            rg_config(
                steps=[
                    {"type": "send", "text": "/bind {code}", "chat": "@testbot"},
                    {"type": "click", "button": "确认"},
                ]
            ),
            client=client,
            alog=alog,
        )
        # 机器人先回了一条带按钮的消息（经过 handler 就会进 _recent）
        hunter._dispatch(
            make_message(
                "请确认",
                chat=FakeChat(BOT_CHAT, title="TestBot"),
                sender=FakeUser(9, is_bot=True),
                markup=button_markup(("✅ 确认绑定", b"confirm")),
            ),
            edited=False,
        )
        outcome = await hunter._run(task_of(hunter), rg_message(), CODE_VALUE, time.perf_counter())

        assert outcome.result is ChainResult.SUCCESS
        assert client.callbacks[0]["callback_data"] == b"confirm"

    @pytest.mark.asyncio
    async def test_click_without_any_button_fails(self, alog) -> None:
        client = bot_client()
        hunter = build(
            rg_config(steps=[{"type": "click", "button": "领取", "chat": "@testbot"}]),
            client=client,
            alog=alog,
        )
        outcome = await hunter._run(task_of(hunter), rg_message(), CODE_VALUE, time.perf_counter())
        assert outcome.result is ChainResult.FAILED
        assert "按钮" in outcome.steps[0].detail

    @pytest.mark.asyncio
    async def test_optional_step_failure_is_partial(self, alog) -> None:
        client = bot_client()
        hunter = build(
            rg_config(
                steps=[
                    {"type": "click", "button": "领取", "chat": "@testbot", "optional": True},
                    {"type": "send", "text": "兜底 {code}", "chat": "@testbot"},
                ]
            ),
            client=client,
            alog=alog,
        )
        outcome = await hunter._run(task_of(hunter), rg_message(), CODE_VALUE, time.perf_counter())

        assert outcome.result is ChainResult.PARTIAL
        assert len(outcome.steps) == 2
        assert client.sent[0]["text"] == f"兜底 {CODE_VALUE}"

    @pytest.mark.asyncio
    async def test_required_step_failure_aborts_chain(self, alog) -> None:
        client = bot_client()
        hunter = build(
            rg_config(
                steps=[
                    {"type": "click", "button": "领取", "chat": "@testbot"},
                    {"type": "send", "text": "兜底 {code}", "chat": "@testbot"},
                ]
            ),
            client=client,
            alog=alog,
        )
        outcome = await hunter._run(task_of(hunter), rg_message(), CODE_VALUE, time.perf_counter())

        assert outcome.result is ChainResult.FAILED
        assert len(outcome.steps) == 1
        assert client.sent == []

    @pytest.mark.asyncio
    async def test_step_delay_is_honoured(self, alog) -> None:
        client = bot_client()
        hunter = build(
            rg_config(steps=[{"type": "send", "text": "{code}", "chat": "@testbot", "delay": 0.05}]),
            client=client,
            alog=alog,
        )
        started = time.perf_counter()
        await hunter._run(task_of(hunter), rg_message(), CODE_VALUE, started)
        assert time.perf_counter() - started >= 0.05


# --------------------------------------------------------------------------- #
class TestDedupe:
    """同一条码只抢一次。"""

    @pytest.mark.asyncio
    async def test_same_code_twice_only_runs_once(self, alog) -> None:
        client = bot_client()
        hunter = build(rg_config(), client=client, alog=alog)
        msg = rg_message()
        hunter._dispatch(msg, edited=False)
        hunter._dispatch(msg, edited=False)
        await asyncio.gather(*list(hunter._tasks))

        assert hunter.stats["detected"] == 1
        assert hunter.stats["duplicate_code"] == 1
        assert len(client.sent) == 1

    @pytest.mark.asyncio
    async def test_different_codes_both_run(self, alog) -> None:
        client = bot_client()
        hunter = build(rg_config(), client=client, alog=alog)
        hunter._dispatch(rg_message("MSKY-30-Register_Ex5I0Fx5Bg"), edited=False)
        hunter._dispatch(rg_message("MSKY-30-Register_JbrMDOxx38"), edited=False)
        await asyncio.gather(*list(hunter._tasks))

        assert hunter.stats["detected"] == 2
        assert len(client.sent) == 2

    @pytest.mark.asyncio
    async def test_code_ttl_zero_disables_dedupe(self, alog) -> None:
        client = bot_client()
        hunter = build(rg_config(code_ttl=0), client=client, alog=alog)
        msg = rg_message()
        hunter._dispatch(msg, edited=False)
        hunter._dispatch(msg, edited=False)
        await asyncio.gather(*list(hunter._tasks))

        assert hunter.stats["detected"] == 2

    @pytest.mark.asyncio
    async def test_same_code_in_two_chats_only_runs_once(self, alog) -> None:
        """同一条码常被多个群同时转发出来。"""
        client = bot_client()
        hunter = build(rg_config(), client=client, alog=alog)
        hunter._dispatch(rg_message(chat=FakeChat(CHAT, title="码群A")), edited=False)
        hunter._dispatch(rg_message(chat=FakeChat(-1005555555555, title="码群B")), edited=False)
        await asyncio.gather(*list(hunter._tasks))

        assert hunter.stats["duplicate_code"] == 1
        assert len(client.sent) == 1


# --------------------------------------------------------------------------- #
class TestNotify:
    @pytest.mark.asyncio
    async def test_notify_submitted_with_reg_grab_event(self, alog) -> None:
        notifier = FakeNotifier()
        hunter = build(rg_config(), alog=alog, notifier=notifier)
        await hunter._run(task_of(hunter), rg_message(), CODE_VALUE, time.perf_counter())

        assert len(notifier.tasks) == 1
        task = notifier.tasks[0]
        assert task.event == "reg_grab"
        assert CODE_VALUE in task.text

    @pytest.mark.asyncio
    async def test_notify_disabled_submits_nothing(self, alog) -> None:
        notifier = FakeNotifier()
        hunter = build(rg_config(notify=False), alog=alog, notifier=notifier)
        await hunter._run(task_of(hunter), rg_message(), CODE_VALUE, time.perf_counter())
        assert notifier.tasks == []


# --------------------------------------------------------------------------- #
#: 线上真实形态的使用通知：尾部被遮罩，只露出 3 位。
USAGE_NOTICE = "🎟️ 注册码使用 - jf [7002057019] 使用了 MSKY-30-Register_f1t░░░░░░░"


class TestUsageNotice:
    """「已被使用」通知判定：对得上的码直接剔除。"""

    def test_visible_code_part_strips_trailing_mask(self) -> None:
        assert visible_code_part("MSKY-30-Register_f1t░░░░░░░") == "MSKY-30-Register_f1t"
        assert visible_code_part("MSKY-30-Register_f1t。") == "MSKY-30-Register_f1t"
        # 没有遮罩时原样返回
        assert visible_code_part("MSKY-30-Register_f1tAbCdEfGh") == "MSKY-30-Register_f1tAbCdEfGh"

    def test_code_value_of_takes_last_segment(self) -> None:
        """两边必须按同一口径切：通知带前缀、捕获组不带，切完都要落到码值上。"""
        assert code_value_of("MSKY-30-Register_f1t") == "f1t"
        assert code_value_of("f1tAbCdEfGh") == "f1tabcdefgh"
        assert code_value_of("Register_f1tAbCdEfGh") == "f1tabcdefgh"

    def test_notice_is_recorded(self, alog) -> None:
        hunter = build(rg_config(), alog=alog)
        hunter._dispatch(rg_message(USAGE_NOTICE), edited=False)

        assert hunter.stats["usage_notices"] == 1
        assert hunter._is_used("f1tAbCdEfGh", task_of(hunter)) == "f1t"
        # 通知本身提不出码（尾部被遮罩），不该被当成一条要抢的码
        assert hunter.stats["detected"] == 0

    def test_matching_code_is_dropped(self, alog) -> None:
        client = bot_client()
        hunter = build(rg_config(), client=client, alog=alog)
        hunter._dispatch(rg_message(USAGE_NOTICE), edited=False)
        hunter._dispatch(rg_message("MSKY-30-Register_f1tAbCdEfGh"), edited=False)

        assert hunter.stats["used_skipped"] == 1
        assert hunter.stats["detected"] == 0
        assert client.sent == [], "已经被用掉的码不该再跑步骤链"

    @pytest.mark.asyncio
    async def test_other_code_still_runs(self, alog) -> None:
        client = bot_client()
        hunter = build(rg_config(), client=client, alog=alog)
        hunter._dispatch(rg_message(USAGE_NOTICE), edited=False)
        hunter._dispatch(rg_message("MSKY-30-Register_Ex5I0Fx5Bg"), edited=False)
        await asyncio.gather(*list(hunter._tasks))

        assert hunter.stats["used_skipped"] == 0
        assert hunter.stats["success"] == 1

    def test_short_visible_part_is_ignored(self, alog) -> None:
        """可见位数不够就不判定 —— 只露 1~2 位时几乎任何码都能「对得上」。"""
        hunter = build(
            rg_config(detect={"code_pattern": PATTERN, "used_min_len": 5}), alog=alog
        )
        hunter._dispatch(rg_message(USAGE_NOTICE), edited=False)

        assert hunter._is_used("f1tAbCdEfGh", task_of(hunter)) is None

    def test_disabled_when_pattern_blank(self, alog) -> None:
        hunter = build(rg_config(detect={"code_pattern": PATTERN, "used_pattern": None}), alog=alog)
        hunter._dispatch(rg_message(USAGE_NOTICE), edited=False)

        assert hunter.stats["usage_notices"] == 0
        assert hunter._is_used("f1tAbCdEfGh", task_of(hunter)) is None

    @pytest.mark.asyncio
    async def test_notice_during_delay_aborts_chain(self, alog) -> None:
        """反脚本延迟正好是检测窗口：延迟期间冒出通知，链在第一步之前就刹车。"""
        client = bot_client()
        hunter = build(rg_config(delay=0.2, jitter=0), client=client, alog=alog)

        async def feed_notice() -> None:
            await asyncio.sleep(0.05)
            hunter._dispatch(rg_message(USAGE_NOTICE), edited=False)

        feeder = asyncio.create_task(feed_notice())
        outcome = await hunter._run(
            task_of(hunter),
            rg_message("MSKY-30-Register_f1tAbCdEfGh"),
            "f1tAbCdEfGh",
            time.perf_counter(),
        )
        await feeder

        assert outcome.result is ChainResult.SKIPPED
        assert "使用通知" in outcome.detail
        assert client.sent == []
        assert hunter.stats["used_skipped"] == 1

    @pytest.mark.asyncio
    async def test_skip_does_not_notify(self, alog) -> None:
        """码被别人用掉是预期内的事，推给用户纯属噪音。"""
        notifier = FakeNotifier()
        client = bot_client()
        hunter = build(
            rg_config(delay=0.2, jitter=0), client=client, alog=alog, notifier=notifier
        )

        async def feed_notice() -> None:
            await asyncio.sleep(0.05)
            hunter._dispatch(rg_message(USAGE_NOTICE), edited=False)

        feeder = asyncio.create_task(feed_notice())
        await hunter._run(
            task_of(hunter),
            rg_message("MSKY-30-Register_f1tAbCdEfGh"),
            "f1tAbCdEfGh",
            time.perf_counter(),
        )
        await feeder

        assert notifier.tasks == []


# --------------------------------------------------------------------------- #
def at(hour: int, minute: int = 0) -> datetime:
    """构造一个「本地时间」用于时段判定 —— 只看时分，日期无关紧要。"""
    return datetime(2026, 1, 1, hour, minute)


class TestWindow:
    """监听时段：只在人类活动时段动手，避免半夜秒抢暴露脚本。"""

    def test_disabled_by_default_means_all_day(self) -> None:
        window = RegGrabWindow()
        assert window.enabled is False
        assert window.describe() == "全天"
        for hour in (0, 3, 12, 23):
            assert window.contains(at(hour)) is True, "开关关着 ⇒ 任何时刻都允许"

    def test_normal_range_is_left_closed_right_open(self) -> None:
        window = RegGrabWindow(enabled=True, start="08:00", end="23:00")
        assert window.contains(at(7, 59)) is False
        assert window.contains(at(8, 0)) is True, "起点含在内"
        assert window.contains(at(22, 59)) is True
        assert window.contains(at(23, 0)) is False, "终点不含 —— 否则相邻两段会重叠"

    def test_adjacent_ranges_do_not_overlap(self) -> None:
        morning = RegGrabWindow(enabled=True, start="08:00", end="09:00")
        noon = RegGrabWindow(enabled=True, start="09:00", end="10:00")
        assert morning.contains(at(9, 0)) is False
        assert noon.contains(at(9, 0)) is True

    def test_crossing_midnight(self) -> None:
        """``start > end`` ⇒ 跨零点（如 22:00 ~ 06:00）。"""
        window = RegGrabWindow(enabled=True, start="22:00", end="06:00")
        assert window.contains(at(23, 30)) is True
        assert window.contains(at(0, 0)) is True
        assert window.contains(at(5, 59)) is True
        assert window.contains(at(6, 0)) is False
        assert window.contains(at(12, 0)) is False
        assert "跨零点" in window.describe()

    def test_start_equal_end_is_rejected(self) -> None:
        """🔴 不许用「相等」猜语义 —— 想全天就关开关，别让人以为配上了。"""
        with pytest.raises(ValidationError, match="不能相同"):
            RegGrabWindow(enabled=True, start="08:00", end="08:00")

    def test_bad_format_is_rejected_loudly(self) -> None:
        """🔴 不静默兜底：解析失败就当 0 点的话，配错了会「看起来一切正常」。"""
        for bad in ("8点", "晚上8点", "", "25:00", "08:70", "0800"):
            with pytest.raises(ValidationError):
                RegGrabWindow(start=bad)

    def test_clock_is_normalized(self) -> None:
        assert RegGrabWindow(start="8:5").start == "08:05"
        assert RegGrabWindow(start="8：00", end="23:00").start == "08:00", "中文冒号也认"
        assert RegGrabWindow(start= 8 * 60, end=23 * 60).start == "08:00", "纯分钟数也认"

    def test_window_default_in_task_is_off(self) -> None:
        """时段现在是**每任务**的 —— 任务默认就是全天可抢。"""
        task = RegGrabTask(id="a")
        assert task.window.enabled is False
        assert task.in_window is True

    def test_ready_ignores_the_window(self) -> None:
        """``ready`` 是「配好了没有」，不是「现在能不能动手」—— 两者混在一起，
        面板会出现「明明配好了却显示未就绪」，而且随着时间跳变。"""
        task = RegGrabTask(
            id="a",
            detect={"code_pattern": PATTERN},
            steps=[{"type": "wait", "seconds": 1}],
            window={"enabled": True, "start": "08:00", "end": "09:00"},
        )
        assert task.ready is True
        assert task.window.contains(at(3)) is False, "凌晨不在时段内"


# --------------------------------------------------------------------------- #
class TestWindowInEngine:
    """引擎层：时段外只看着不动，而且**不占去重名额**。"""

    @staticmethod
    def _at(hunter, hour: int, minute: int = 0):
        hunter._now = lambda: at(hour, minute)
        return hunter

    def test_outside_window_does_not_grab(self, alog) -> None:
        client = bot_client()
        hunter = self._at(
            build(
                rg_multi(rg_task("a", window={"enabled": True, "start": "08:00", "end": "23:00"})),
                client=client,
                alog=alog,
            ),
            3,
        )
        hunter._dispatch(rg_message(), edited=False)

        assert hunter.stats["outside_window"] == 1
        assert hunter.stats["detected"] == 0, "时段外连「发现」都不该记"
        assert client.sent == [], "半夜不该动手"
        assert list(hunter._tasks) == []
        assert hunter.task_stats["a"]["outside_window"] == 1

    @pytest.mark.asyncio
    async def test_inside_window_still_grabs(self, alog) -> None:
        client = bot_client()
        hunter = self._at(
            build(
                rg_multi(rg_task("a", window={"enabled": True, "start": "08:00", "end": "23:00"})),
                client=client,
                alog=alog,
            ),
            12,
        )
        hunter._dispatch(rg_message(), edited=False)
        await asyncio.gather(*list(hunter._tasks))

        assert hunter.stats["outside_window"] == 0
        assert hunter.stats["detected"] == 1
        assert hunter.stats["success"] == 1, "白天照常抢"

    @pytest.mark.asyncio
    async def test_disabled_window_grabs_around_the_clock(self, alog) -> None:
        """开关关着 ⇒ 任何时刻都照抢（不改变老行为）。"""
        client = bot_client()
        hunter = self._at(build(rg_multi(rg_task("a")), client=client, alog=alog), 3)
        hunter._dispatch(rg_message(), edited=False)
        await asyncio.gather(*list(hunter._tasks))
        assert hunter.stats["detected"] == 1

    @pytest.mark.asyncio
    async def test_outside_window_does_not_consume_the_dedupe_slot(self, alog) -> None:
        """🔴 时段外**不能**把「同码去重」名额占掉。

        占了的话，时段内同一条码再来时会被当成重复而跳过 —— 白等一整天。
        """
        config = rg_multi(
            rg_task("a", window={"enabled": True, "start": "08:00", "end": "23:00"})
        )
        client = bot_client()
        hunter = self._at(build(config, client=client, alog=alog), 3)
        hunter._dispatch(rg_message(), edited=False)
        assert hunter.stats["outside_window"] == 1
        assert hunter._seen_codes == {}, "时段外不许记去重名额"

        self._at(hunter, 12)
        hunter._dispatch(rg_message(), edited=False)
        await asyncio.gather(*list(hunter._tasks))
        assert hunter.stats["detected"] == 1, "时段内同一条码必须还能抢"

    @pytest.mark.asyncio
    async def test_window_closing_during_delay_aborts_chain(self, alog) -> None:
        """延迟把动手时刻推到了时段之外（22:59:59 派发、23:00:01 才跑）⇒ 放弃。

        抢注的价值全在「准点」，多等 2 秒也抢不到；但半夜动手会留下脚本痕迹。
        """
        client = bot_client()
        hunter = build(
            rg_multi(
                rg_task(
                    "a",
                    delay=0.2,
                    jitter=0,
                    window={"enabled": True, "start": "08:00", "end": "23:00"},
                )
            ),
            client=client,
            alog=alog,
        )
        hunter._now = lambda: at(22, 59)

        async def close_window() -> None:
            await asyncio.sleep(0.05)
            hunter._now = lambda: at(23, 0)

        closer = asyncio.create_task(close_window())
        outcome = await hunter._run(task_of(hunter), rg_message(), CODE_VALUE, time.perf_counter())
        await closer

        assert outcome.result is ChainResult.SKIPPED
        assert "监听时段" in outcome.detail
        assert client.sent == [], "时段外不许动手"
        assert hunter.stats["outside_window"] == 1

    def test_snapshot_exposes_the_window_state(self, alog) -> None:
        """面板/CLI 靠 ``windows``（每任务）与 ``in_window``（any）解释「为什么现在没动静」。"""
        hunter = self._at(
            build(
                rg_multi(
                    rg_task("night", window={"enabled": True, "start": "22:00", "end": "06:00"}),
                    rg_task("day", window={"enabled": True, "start": "08:00", "end": "23:00"}),
                ),
                alog=alog,
            ),
            3,
        )
        snap = hunter.snapshot()
        assert snap["windows"] == {"night": True, "day": False}
        assert snap["in_window"] is True, "有任意一条在时段内 ⇒ any 语义为真"
        assert snap["outside_window"] == 0

    def test_snapshot_in_window_is_false_when_no_task_may_act(self, alog) -> None:
        hunter = self._at(
            build(
                rg_multi(
                    rg_task("night", window={"enabled": True, "start": "22:00", "end": "06:00"}),
                    rg_task("day", window={"enabled": True, "start": "08:00", "end": "23:00"}),
                ),
                alog=alog,
            ),
            12,
        )
        snap = hunter.snapshot()
        assert snap["windows"] == {"night": False, "day": True}
        assert snap["in_window"] is True

        hunter2 = self._at(
            build(
                rg_multi(rg_task("night", window={"enabled": True, "start": "22:00", "end": "06:00"})),
                alog=alog,
            ),
            12,
        )
        assert hunter2.snapshot()["in_window"] is False, "没有任何一条在时段内 ⇒ False"

    @pytest.mark.asyncio
    async def test_register_logs_the_window(self) -> None:
        """注册那行必须带上时段与「此刻在不在时段内」—— 排查「为什么没动静」全看它。"""
        log = CapturingLog()
        hunter = self._at(
            build(
                rg_multi(
                    rg_task("night", window={"enabled": True, "start": "22:00", "end": "06:00"})
                ),
                alog=log,
            ),
            3,
        )
        await hunter.register()

        assert "22:00~06:00（跨零点）" in log.text
        assert "in_window=True" in log.text, "凌晨 3 点落在 22:00~06:00 内 ⇒ 应当是 True"
        # 🔴 日志必须和**判定**同源。原来这行读的是 ``self.config.in_window``
        # （真实时钟），而放行与否读的是 ``self._in_window()``（可注入时钟）——
        # 两者在线上一致、在测试里不一致，于是这个用例会**随一天中的时刻时好时坏**：
        # 只有真实时间恰好落在 22:00~06:00 内才通过。把同源性钉死，别再靠运气。
        assert f"in_window={hunter._in_window(task_of(hunter))}" in log.text, (
            "日志里的 in_window 必须来自 _in_window(task)，不能用另一个时钟"
        )


# --------------------------------------------------------------------------- #
class TestDelay:
    """反脚本延迟：默认就该有一点，别秒回。"""

    def test_default_delay_is_not_instant(self) -> None:
        task = RegGrabTask(id="a")
        assert task.delay >= 0.5
        assert task.jitter >= 1.0
        assert task.delay + task.jitter >= 1.5

    def test_jitter_makes_delay_vary(self) -> None:
        """两次的延迟不能一模一样 —— 整齐一致本身就是机器人特征。"""
        task = RegGrabTask(id="a", delay=0.0, jitter=5.0)
        seen = {round(task.delay + random.uniform(0, task.jitter), 6) for _ in range(20)}
        assert len(seen) > 1

    @pytest.mark.asyncio
    async def test_delay_is_honoured(self, alog) -> None:
        client = bot_client()
        hunter = build(rg_config(delay=0.15, jitter=0), client=client, alog=alog)
        started = time.perf_counter()
        await hunter._run(task_of(hunter), rg_message(), CODE_VALUE, started)
        assert time.perf_counter() - started >= 0.15


# --------------------------------------------------------------------------- #
class TestRegistration:
    @pytest.mark.asyncio
    async def test_register_adds_handlers(self, alog) -> None:
        client = bot_client()
        hunter = build(rg_config(), client=client, alog=alog)
        await hunter.register()
        assert len(client.handlers) == 2  # 消息 + 编辑
        await hunter.close()
        assert client.handlers == []

    @pytest.mark.asyncio
    async def test_incomplete_task_still_registers_but_warns(self, alog) -> None:
        """开关开着但任务没配好：照样注册（用户可能正在填），但必须留一条警告。

        🔴 不能静默地什么都不注册 —— 用户只会看到「开了却没动静」，日志里
        也只有一条很容易被忽略的 warning。
        """
        log = CapturingLog()
        client = bot_client()
        hunter = build(rg_multi(rg_task("good"), rg_task("bad", detect={})), client=client, alog=log)
        await hunter.register()

        assert len(client.handlers) == 2
        assert "tasks=2" in log.text
        assert "配置不完整" in log.text
        assert "bad" in log.text

    @pytest.mark.asyncio
    async def test_disabled_registers_nothing(self, alog) -> None:
        client = bot_client()
        hunter = build(rg_config(enabled=False), client=client, alog=alog)
        await hunter.register()
        assert client.handlers == []

    @pytest.mark.asyncio
    async def test_include_edited_false_skips_edited_handler(self, alog) -> None:
        client = bot_client()
        hunter = build(rg_config(include_edited=False), client=client, alog=alog)
        await hunter.register()
        assert len(client.handlers) == 1

    @pytest.mark.asyncio
    async def test_include_edited_is_any_across_tasks(self, alog) -> None:
        """只要有一条任务要处理编辑事件，就整体注册那个 handler（any 语义）。"""
        client = bot_client()
        hunter = build(
            rg_multi(rg_task("a", include_edited=False), rg_task("b", include_edited=True)),
            client=client,
            alog=alog,
        )
        await hunter.register()
        assert len(client.handlers) == 2

    @pytest.mark.asyncio
    async def test_all_tasks_ignoring_edits_register_message_only(self, alog) -> None:
        client = bot_client()
        hunter = build(
            rg_multi(rg_task("a", include_edited=False), rg_task("b", include_edited=False)),
            client=client,
            alog=alog,
        )
        await hunter.register()
        assert len(client.handlers) == 1


# --------------------------------------------------------------------------- #
class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_happy_path(self, alog) -> None:
        client = bot_client()
        notifier = FakeNotifier()
        hunter = build(rg_config(), client=client, alog=alog, notifier=notifier)
        await hunter.register()

        hunter._dispatch(rg_message(), edited=False)
        await asyncio.gather(*list(hunter._tasks))

        assert hunter.stats["detected"] == 1
        assert hunter.stats["success"] == 1
        assert hunter.stats["steps_ok"] == 1
        assert hunter.task_stats["default"]["success"] == 1, "迁移过来的任务也要按任务记账"
        assert notifier.tasks

    @pytest.mark.asyncio
    async def test_snapshot_has_counters(self, alog) -> None:
        hunter = build(rg_config(), alog=alog)
        snapshot = hunter.snapshot()
        for key in ("detected", "success", "failed", "duplicate_code", "pending_tasks"):
            assert key in snapshot
        # 多任务之后面板还得能「按任务看」：每任务计数 + 各自此刻在不在时段内。
        assert snapshot["tasks"] == 1
        assert snapshot["active_tasks"] == 1
        assert set(snapshot["per_task"]) == {"default"}
        assert snapshot["windows"] == {"default": True}


# --------------------------------------------------------------------------- #
class TestMultipleTasks:
    """多任务：各条一套监听 / 正则 / 步骤 / 时段，但去重、并发、通知是账号级的。"""

    def test_first_matching_task_wins(self, alog) -> None:
        """🔴 两条任务都能接住同一条码时，只取排在前面的那条。

        两条都动手就是对同一个码跑两遍 ``/bind``，机器人只会回「已注册」，
        还平白多留一次脚本痕迹。列表顺序就是优先级。
        """
        hunter = build(rg_multi(rg_task("a"), rg_task("b")), alog=alog)
        task, code = hunter._match(rg_message(), CHAT)
        assert task.id == "a"
        assert code == CODE_VALUE

    def test_chat_filter_is_per_task(self, alog) -> None:
        """A 只监听这个群、B 只监听另一个群 —— 消息各归各的任务。"""
        hunter = build(
            rg_multi(rg_task("a", chats=[CHAT]), rg_task("b", chats=[OTHER_CHAT])), alog=alog
        )
        assert hunter._match(rg_message(), CHAT)[0].id == "a"
        other = rg_message(chat=FakeChat(OTHER_CHAT, title="别的码群"))
        assert hunter._match(other, OTHER_CHAT)[0].id == "b"

    def test_exclude_chats_is_per_task(self, alog) -> None:
        """A 排除了这个群，消息要落到没排除的 B 上（不是整条码作废）。"""
        hunter = build(rg_multi(rg_task("a", exclude_chats=[CHAT]), rg_task("b")), alog=alog)
        assert hunter._match(rg_message(), CHAT)[0].id == "b"

    def test_watched_chats_unions_tasks(self, alog) -> None:
        hunter = build(
            rg_multi(rg_task("a", chats=[CHAT]), rg_task("b", chats=[OTHER_CHAT, CHAT])),
            alog=alog,
        )
        assert hunter.watched_chats() == [CHAT, OTHER_CHAT]

    def test_any_task_watching_all_means_watch_everything(self, alog) -> None:
        """🔴 一条留空 ⇒ 整体全监听，否则那条留空的会被无声忽略。"""
        hunter = build(rg_multi(rg_task("a", chats=[CHAT]), rg_task("b", chats=[])), alog=alog)
        assert hunter.watched_chats() == []
        assert hunter._watch_chats() == []

    def test_disabled_task_is_not_prepared(self, alog) -> None:
        """停用的任务不进 ``prepared`` ⇒ 连正则都不编译。"""
        hunter = build(rg_multi(rg_task("on"), rg_task("off", enabled=False)), alog=alog)
        assert [task.id for task in hunter.prepared] == ["on"]
        assert "off" not in hunter.task_stats
        assert "off" not in hunter.snapshot()["windows"]

    @pytest.mark.asyncio
    async def test_disabled_task_does_not_fire(self, alog) -> None:
        client = bot_client()
        hunter = build(
            rg_multi(
                rg_task("on"),
                rg_task(
                    "off",
                    enabled=False,
                    steps=[{"type": "send", "text": "不该发 {code}", "chat": "@testbot"}],
                ),
            ),
            client=client,
            alog=alog,
        )
        hunter._dispatch(rg_message(), edited=False)
        await asyncio.gather(*list(hunter._tasks))

        assert [call["text"] for call in client.sent] == [f"/bind {CODE_VALUE}"]
        assert "off" not in hunter.snapshot()["per_task"]

    @pytest.mark.asyncio
    async def test_dispatch_runs_only_the_first_matching_task(self, alog) -> None:
        """端到端：两条任务都命中，实际只用第一条跑了一遍链。"""
        client = bot_client()
        hunter = build(
            rg_multi(
                rg_task("a", steps=[{"type": "send", "text": "A {code}", "chat": "@testbot"}]),
                rg_task("b", steps=[{"type": "send", "text": "B {code}", "chat": "@testbot"}]),
            ),
            client=client,
            alog=alog,
        )
        hunter._dispatch(rg_message(), edited=False)
        await asyncio.gather(*list(hunter._tasks))

        assert [call["text"] for call in client.sent] == [f"A {CODE_VALUE}"]
        assert hunter.task_stats["a"]["success"] == 1
        assert hunter.task_stats["b"]["success"] == 0

    @pytest.mark.asyncio
    async def test_steps_are_per_task(self, alog) -> None:
        """同一个引擎里两套步骤链：A 单发一条，B 连发两条。"""
        client = bot_client()
        hunter = build(
            rg_multi(
                rg_task(
                    "a",
                    chats=[CHAT],
                    steps=[{"type": "send", "text": "A {code}", "chat": "@testbot"}],
                ),
                rg_task(
                    "b",
                    chats=[OTHER_CHAT],
                    steps=[
                        {"type": "send", "text": "B1 {code}", "chat": "@testbot"},
                        {"type": "send", "text": "B2 {code}", "chat": "@testbot"},
                    ],
                ),
            ),
            client=client,
            alog=alog,
        )
        hunter._dispatch(rg_message(chat=FakeChat(CHAT, title="码群A")), edited=False)
        hunter._dispatch(rg_message(CODE_B, chat=FakeChat(OTHER_CHAT, title="码群B")), edited=False)
        await asyncio.gather(*list(hunter._tasks))

        assert sorted(call["text"] for call in client.sent) == sorted(
            [f"A {CODE_VALUE}", f"B1 {CODE_VALUE_B}", f"B2 {CODE_VALUE_B}"]
        )
        assert hunter.task_stats["a"]["steps_ok"] == 1
        assert hunter.task_stats["b"]["steps_ok"] == 2

    @pytest.mark.asyncio
    async def test_window_is_per_task(self, alog) -> None:
        """时段是**每任务**的：夜班那条现在不该动手，白班那条照抢。"""
        client = bot_client()
        hunter = build(
            rg_multi(
                rg_task(
                    "night",
                    chats=[CHAT],
                    window={"enabled": True, "start": "22:00", "end": "06:00"},
                    steps=[{"type": "send", "text": "夜班 {code}", "chat": "@testbot"}],
                ),
                rg_task(
                    "day",
                    chats=[OTHER_CHAT],
                    window={"enabled": True, "start": "08:00", "end": "23:00"},
                    steps=[{"type": "send", "text": "白班 {code}", "chat": "@testbot"}],
                ),
            ),
            client=client,
            alog=alog,
        )
        hunter._now = lambda: at(12, 0)
        hunter._dispatch(rg_message(chat=FakeChat(CHAT, title="码群A")), edited=False)
        hunter._dispatch(rg_message(chat=FakeChat(OTHER_CHAT, title="码群B")), edited=False)
        await asyncio.gather(*list(hunter._tasks))

        assert [call["text"] for call in client.sent] == [f"白班 {CODE_VALUE}"]
        snap = hunter.snapshot()
        assert snap["windows"] == {"night": False, "day": True}
        assert snap["in_window"] is True, "任意一条在时段内 ⇒ any 语义为真"
        assert hunter.task_stats["night"]["outside_window"] == 1
        assert hunter.task_stats["night"]["detected"] == 0
        assert hunter.task_stats["day"]["success"] == 1
        # 🔴 时段外不许占掉账号级的同码名额：占了这个码就被夜班那条白白废掉。
        assert hunter.stats["duplicate_code"] == 0

    @pytest.mark.asyncio
    async def test_out_of_window_task_does_not_shadow_a_later_task(self, alog) -> None:
        """🔴 回归：时段外的任务**不算命中**，不能把码吃掉让后面的任务轮不到。

        两条任务监听**同一个群**，``a``（夜班）排在前面但此刻不在自己时段内，
        ``b``（白班）此刻正在时段内。若按「先取首命中、再判时段」，``a`` 会把这条码
        吃掉然后什么都不做 —— 用户专门为白天建的那条任务会永远静默失效。
        """
        client = bot_client()
        hunter = build(
            rg_multi(
                rg_task(
                    "a",
                    chats=[CHAT],
                    window={"enabled": True, "start": "22:00", "end": "06:00"},
                    steps=[{"type": "send", "text": "夜班 {code}", "chat": "@testbot"}],
                ),
                rg_task(
                    "b",
                    chats=[CHAT],
                    window={"enabled": True, "start": "08:00", "end": "23:00"},
                    steps=[{"type": "send", "text": "白班 {code}", "chat": "@testbot"}],
                ),
            ),
            client=client,
            alog=alog,
        )
        hunter._now = lambda: at(12, 0)
        hunter._dispatch(rg_message(chat=FakeChat(CHAT, title="码群")), edited=False)
        await asyncio.gather(*list(hunter._tasks))

        assert [call["text"] for call in client.sent] == [f"白班 {CODE_VALUE}"], "顺延给时段内的 b"
        assert hunter.task_stats["a"]["detected"] == 0, "时段外的 a 一个动作都不该有"
        assert hunter.task_stats["b"]["success"] == 1
        # 有任务真的动手了 ⇒ 不该记 outside_window
        assert hunter.stats["outside_window"] == 0
        assert hunter.snapshot()["windows"] == {"a": False, "b": True}

    @pytest.mark.asyncio
    async def test_all_tasks_out_of_window_are_still_reported(self, alog) -> None:
        """全都不在时段内 ⇒ 谁都别动手，但要**说清楚**是「命中却在时段外」。"""
        client = bot_client()
        hunter = build(
            rg_multi(
                rg_task("a", chats=[CHAT], window={"enabled": True, "start": "08:00", "end": "23:00"}),
                rg_task("b", chats=[CHAT], window={"enabled": True, "start": "08:00", "end": "23:00"}),
            ),
            client=client,
            alog=alog,
        )
        hunter._now = lambda: at(2, 0)
        hunter._dispatch(rg_message(chat=FakeChat(CHAT, title="码群")), edited=False)

        assert client.sent == [], "凌晨不该动手"
        assert list(hunter._tasks) == []
        assert hunter.stats["outside_window"] == 1
        # 提示/计数落在**第一条**命中（忽略时段）的任务上。
        assert hunter.task_stats["a"]["outside_window"] == 1
        assert hunter.task_stats["b"]["outside_window"] == 0
        # 🔴 时段外不许占掉账号级的同码名额：占了这个码就被白白废掉。
        assert hunter.stats["duplicate_code"] == 0
        assert hunter._seen_codes == {}

    def test_should_grab_ignores_the_window(self, alog) -> None:
        """``_should_grab`` 只回答「正则提不提得出码」，时段判定是 ``_match`` 的事。"""
        hunter = build(
            rg_multi(rg_task("a", window={"enabled": True, "start": "08:00", "end": "23:00"})),
            alog=alog,
        )
        hunter._now = lambda: at(2, 0)
        assert hunter._should_grab(rg_message(), CHAT) == CODE_VALUE
        assert hunter._match(rg_message(), CHAT) is None, "时段外不算命中"

    @pytest.mark.asyncio
    async def test_same_code_seen_by_two_tasks_is_processed_once(self, alog) -> None:
        """同一条码被两条不同任务（不同群）看到 ⇒ 账号级去重只放行一次。"""
        client = bot_client()
        hunter = build(
            rg_multi(
                rg_task(
                    "a",
                    chats=[CHAT],
                    steps=[{"type": "send", "text": "A {code}", "chat": "@testbot"}],
                ),
                rg_task(
                    "b",
                    chats=[OTHER_CHAT],
                    steps=[{"type": "send", "text": "B {code}", "chat": "@testbot"}],
                ),
            ),
            client=client,
            alog=alog,
        )
        hunter._dispatch(rg_message(chat=FakeChat(CHAT, title="码群A")), edited=False)
        hunter._dispatch(rg_message(chat=FakeChat(OTHER_CHAT, title="码群B")), edited=False)
        await asyncio.gather(*list(hunter._tasks))

        assert [call["text"] for call in client.sent] == [f"A {CODE_VALUE}"]
        assert hunter.task_stats["b"]["duplicate_code"] == 1
        assert hunter.task_stats["b"]["detected"] == 0

    @pytest.mark.asyncio
    async def test_dedupe_ttl_comes_from_the_matched_task(self, alog) -> None:
        """去重表是账号级的，但时间窗取**命中任务**的 ``code_ttl``。"""
        client = bot_client()
        hunter = build(
            rg_multi(
                rg_task("a", chats=[CHAT]),
                rg_task("b", chats=[OTHER_CHAT], code_ttl=0),
            ),
            client=client,
            alog=alog,
        )
        hunter._dispatch(rg_message(chat=FakeChat(CHAT, title="码群A")), edited=False)
        hunter._dispatch(rg_message(chat=FakeChat(OTHER_CHAT, title="码群B")), edited=False)
        await asyncio.gather(*list(hunter._tasks))

        assert len(client.sent) == 2, "B 的 code_ttl=0 ⇒ 不吃账号级去重的时间窗"
        assert hunter.stats["duplicate_code"] == 0

    def test_snapshot_reports_tasks_and_per_task_counters(self, alog) -> None:
        """多任务之后「一共抢到 3 个」说明不了是谁干的 —— 必须分任务记。"""
        hunter = build(rg_multi(rg_task("a"), rg_task("b", enabled=False)), alog=alog)
        snap = hunter.snapshot()
        assert snap["tasks"] == 2, "停用的任务也算在总数里"
        assert snap["active_tasks"] == 1
        assert set(snap["per_task"]) == {"a"}
        assert snap["windows"] == {"a": True}

    @pytest.mark.asyncio
    async def test_per_task_counters_are_separate(self, alog) -> None:
        client = bot_client()
        hunter = build(
            rg_multi(rg_task("a", chats=[CHAT]), rg_task("b", chats=[OTHER_CHAT])),
            client=client,
            alog=alog,
        )
        hunter._dispatch(rg_message(chat=FakeChat(CHAT, title="码群A")), edited=False)
        await asyncio.gather(*list(hunter._tasks))

        snap = hunter.snapshot()
        assert snap["detected"] == 1, "账号级总数"
        assert snap["per_task"]["a"]["detected"] == 1
        assert snap["per_task"]["a"]["success"] == 1
        assert snap["per_task"]["b"]["detected"] == 0
        assert snap["per_task"]["b"]["started"] == 0


# --------------------------------------------------------------------------- #
class TestTestNotifyEndpoint:
    """「试发通知」接口，以及它旁边那两个配置端点（GET / PUT 多任务契约）。"""

    @staticmethod
    def _app_with_account(tmp_path, *, reg_grab: dict | None = None, **notify_overrides):
        from tg_assistant.config import AccountRecord, utc_now_iso
        from tg_assistant.web import create_app

        app = create_app(tmp_path / "data")
        store = app.state.store
        store.upsert_account(AccountRecord(name="acct", created_at=utc_now_iso()))
        config = store.load_account_config("acct", create=True)
        # ⚠️ 顺序不能反：模型开了 validate_assignment，enabled=True 会立刻校验
        # 「必须有 bot_token」，所以依赖项要先赋值。
        config.notify.bot_token = "123456:TEST-TOKEN"
        config.notify.chat_id = 5608153118
        config.notify.events = ["forward", "red_packet", "reg_grab"]
        config.notify.enabled = True
        if reg_grab is not None:
            config.reg_grab = RegGrabConfig.model_validate(reg_grab)
        for key, value in notify_overrides.items():
            setattr(config.notify, key, value)
        store.save_account_config("acct", config)
        return app, store

    def test_disabled_notify_is_rejected(self, tmp_path) -> None:
        from fastapi.testclient import TestClient

        app, _ = self._app_with_account(tmp_path, enabled=False)
        with TestClient(app) as client:
            res = client.post("/api/config/acct/reg_grab/test_notify")
        assert res.status_code == 400
        assert "通知没启用" in res.json()["detail"]

    def test_missing_event_is_rejected(self, tmp_path) -> None:
        """最常见的坑：机器人配好了，但「抢注通知」那个勾没打。"""
        from fastapi.testclient import TestClient

        app, _ = self._app_with_account(tmp_path, events=["forward"])
        with TestClient(app) as client:
            res = client.post("/api/config/acct/reg_grab/test_notify")
        assert res.status_code == 400
        assert "抢注通知" in res.json()["detail"]

    def test_not_running_is_rejected(self, tmp_path) -> None:
        from fastapi.testclient import TestClient

        app, _ = self._app_with_account(tmp_path)
        with TestClient(app) as client:
            res = client.post("/api/config/acct/reg_grab/test_notify")
        assert res.status_code == 400
        assert "没在运行" in res.json()["detail"]

    def test_get_exposes_per_task_state_and_the_server_clock(self, tmp_path) -> None:
        """面板要靠 ``server_now`` 对照时区，并逐条任务拿到 ``ready`` / ``in_window``。

        只给两个时间框，用户本地时区不一致时是发现不了的（填 08:00 以为是本地时间，
        实际按北京时间算）；而多任务之后「配好了没有」也必须是**每任务**的回答。
        """
        from fastapi.testclient import TestClient

        app, _ = self._app_with_account(
            tmp_path,
            reg_grab={
                "enabled": True,
                "tasks": [
                    {
                        "id": "a",
                        "name": "码群",
                        "detect": {"code_pattern": PATTERN},
                        "steps": [{"type": "send", "text": "/bind {code}"}],
                    },
                    {
                        "id": "b",
                        "window": {"enabled": True, "start": "22:00", "end": "06:00"},
                    },
                ],
            },
        )
        with TestClient(app) as client:
            res = client.get("/api/config/acct/reg_grab")

        assert res.status_code == 200
        data = res.json()
        assert data["enabled"] is True
        first, second = data["tasks"]
        assert first["ready"] is True
        assert first["problem"] is None
        assert first["in_window"] is True, "时段开关没开 ⇒ 恒为「可抢」"
        assert second["ready"] is False
        assert second["problem"], "配不全的任务必须有一句中文说明"
        # 🔴 带时段的那条不钉死具体值：那会随一天中的时刻时好时坏（见 register 用例）。
        assert isinstance(second["in_window"], bool)
        assert re.fullmatch(r"\d{2}:\d{2}", data["server_now"]), data["server_now"]

    def test_get_then_put_round_trips(self, tmp_path) -> None:
        """🔴 回归：GET 的结果原样 PUT 回去必须成功。

        GET 额外给每条任务塞了 ``ready`` / ``problem`` / ``in_window``、顶层塞了
        ``server_now``，而 ``RegGrabConfig`` 是 ``extra="forbid"`` 的 —— 不把它们
        丢掉就是 400，用户只看到「保存失败」，根本猜不到是这几个字段惹的。
        """
        from fastapi.testclient import TestClient

        app, store = self._app_with_account(
            tmp_path, reg_grab={"enabled": True, "tasks": [rg_task("a")]}
        )
        with TestClient(app) as client:
            data = client.get("/api/config/acct/reg_grab").json()
            data["tasks"][0]["name"] = "改个名字"
            data["tasks"][0]["window"] = {"enabled": True, "start": "22:00", "end": "06:00"}
            res = client.put("/api/config/acct/reg_grab", json=data)

        assert res.status_code == 200, res.text
        assert res.json() == {"ok": True, "tasks": 1}
        saved = store.load_account_config("acct", create=False).reg_grab
        assert saved.tasks[0].name == "改个名字"
        assert saved.tasks[0].window.enabled is True
        assert (saved.tasks[0].window.start, saved.tasks[0].window.end) == ("22:00", "06:00")

    def test_put_rejects_an_impossible_window(self, tmp_path) -> None:
        """开始 == 结束 ⇒ 400，且提示里要说清「想全天就关开关」。"""
        from fastapi.testclient import TestClient

        app, _ = self._app_with_account(
            tmp_path, reg_grab={"enabled": True, "tasks": [rg_task("a")]}
        )
        with TestClient(app) as client:
            data = client.get("/api/config/acct/reg_grab").json()
            data["tasks"][0]["window"] = {"enabled": True, "start": "08:00", "end": "08:00"}
            res = client.put("/api/config/acct/reg_grab", json=data)

        assert res.status_code == 400
        assert "不能相同" in res.json()["detail"]

    def test_submits_through_the_running_notifier(self, tmp_path) -> None:
        """有实例在跑时走的是同一个 notifier、同一个 reg_grab 事件。"""
        import types

        from fastapi.testclient import TestClient

        app, _ = self._app_with_account(tmp_path)
        submitted: list[object] = []

        class _Notifier:
            def submit(self, task: object) -> bool:
                submitted.append(task)
                return True

        app.state.runtime.running_runner = lambda name: types.SimpleNamespace(
            notifier=_Notifier()
        )
        with TestClient(app) as client:
            res = client.post("/api/config/acct/reg_grab/test_notify")

        assert res.status_code == 200, res.text
        assert len(submitted) == 1
        assert submitted[0].event == "reg_grab"
        assert "测试" in submitted[0].text


# --------------------------------------------------------------------------- #
def _reg_grab_template() -> str:
    from pathlib import Path

    web_dir = Path(__file__).resolve().parents[1] / "tg_assistant" / "web"
    return (web_dir / "templates" / "reg_grab.html").read_text(encoding="utf-8")


def _js_function_body(html: str, name: str) -> str:
    """截出某个 JS 函数的函数体。

    ⚠️ 断言必须限定在函数体内：模板别处也可能出现同样的字符串，全文搜的话
    把接线删掉测试照样过（test_web_pages.py 里有一份同名的同款工具）。
    """
    start = html.index(f"async function {name}(")
    end = html.index("\n    }\n", start)
    return html[start:end]


class TestTestExtractEndpoint:
    """「测试提取」的后端、面板文案，以及**走真实入口**的 id 自动生成。

    为什么这一组用例要打 API 而不是只构造模型：需求是「用户能这么用」——
    只证明 ``RegGrabConfig`` 会生成 id，证明不了 PUT/GET 这条用户真正走的路上
    它也生成、也落盘、刷新后还是同一个。
    """

    @staticmethod
    def _app(tmp_path):
        from tg_assistant.config import AccountRecord, utc_now_iso
        from tg_assistant.web import create_app

        app = create_app(tmp_path / "data")
        store = app.state.store
        store.upsert_account(AccountRecord(name="acct", created_at=utc_now_iso()))
        store.load_account_config("acct", create=True)
        return app, store

    def test_reports_what_the_engine_would_grab(self, tmp_path) -> None:
        """用户的原始正则：``code`` 只有后缀，``from_group`` 说清它是第 1 组。"""
        from fastapi.testclient import TestClient

        app, _ = self._app(tmp_path)
        with TestClient(app) as client:
            res = client.post(
                "/api/reg_grab/test_extract",
                json={"pattern": USER_PATTERN, "text": USER_TEXT},
            )

        assert res.status_code == 200, res.text
        assert res.json() == {
            "matched": True,
            "full": USER_FULL,
            "groups": [USER_CODE],
            "code": USER_CODE,
            "from_group": 1,
        }

    def test_needs_no_account_context(self, tmp_path) -> None:
        """页面去掉了「先选账号」：一个账号都没注册时这条路径也要能用。"""
        from fastapi.testclient import TestClient
        from tg_assistant.web import create_app

        app = create_app(tmp_path / "data")
        with TestClient(app) as client:
            res = client.post(
                "/api/reg_grab/test_extract",
                json={"pattern": USER_FIXED_PATTERN, "text": USER_TEXT},
            )
        assert res.status_code == 200, res.text
        assert res.json()["code"] == USER_WHOLE

    def test_no_match_is_reported_as_such(self, tmp_path) -> None:
        from fastapi.testclient import TestClient

        app, _ = self._app(tmp_path)
        with TestClient(app) as client:
            res = client.post(
                "/api/reg_grab/test_extract",
                json={"pattern": USER_PATTERN, "text": "今天天气不错"},
            )
        assert res.status_code == 200, res.text
        assert res.json()["matched"] is False
        assert res.json()["code"] is None

    def test_invalid_regex_gets_a_readable_error(self, tmp_path) -> None:
        """正则写错是输入错误：给一句人话，不要 500。"""
        from fastapi.testclient import TestClient

        app, _ = self._app(tmp_path)
        with TestClient(app) as client:
            res = client.post(
                "/api/reg_grab/test_extract",
                json={"pattern": "([unclosed", "text": USER_TEXT},
            )
        assert res.status_code == 200, res.text
        assert "正则表达式无效" in res.json()["error"]

    def test_blank_pattern_or_text_is_an_error(self, tmp_path) -> None:
        from fastapi.testclient import TestClient

        app, _ = self._app(tmp_path)
        with TestClient(app) as client:
            empty_pattern = client.post(
                "/api/reg_grab/test_extract", json={"pattern": "", "text": USER_TEXT}
            )
            empty_text = client.post(
                "/api/reg_grab/test_extract", json={"pattern": USER_PATTERN, "text": ""}
            )
        assert "正则表达式为空" in empty_pattern.json()["error"]
        assert "测试文本为空" in empty_text.json()["error"]

    def test_put_without_id_generates_and_persists_one(self, tmp_path) -> None:
        """🔴 需求 A 走真实入口：两条同名任务都不带 id ⇒ 各自拿到 id 并落盘。"""
        from fastapi.testclient import TestClient

        app, store = self._app(tmp_path)
        ready = {
            "name": "Main",
            "detect": {"code_pattern": PATTERN},
            "steps": [{"type": "send", "chat": "@bot", "text": "/bind {code}"}],
        }
        with TestClient(app) as client:
            res = client.put(
                "/api/config/acct/reg_grab",
                json={"enabled": False, "tasks": [dict(ready), dict(ready)]},
            )
            assert res.status_code == 200, res.text
            data = client.get("/api/config/acct/reg_grab").json()
            again = client.get("/api/config/acct/reg_grab").json()

        ids = [task["id"] for task in data["tasks"]]
        assert ids == ["main", "main-2"], f"生成/避重不对：{ids}"
        assert [task["id"] for task in again["tasks"]] == ids, "两次 GET 必须是同一个 id"
        # 面板刷新 / 服务重启走的就是「重新加载配置文件」这条路，id 必须不变
        saved = store.load_account_config("acct", create=False)
        assert [task.id for task in saved.reg_grab.tasks] == ids

    def test_panel_says_what_the_regex_will_grab(self) -> None:
        html = _reg_grab_template()

        # 弹窗里那一项要说明「抓的是第一个非空捕获组」，并给出想抓整串时的写法
        assert "第一个非空捕获组" in html
        assert "(XYING-Whitelist_[A-Za-z0-9]{10})" in html
        # 试提取结果里的三块内容：抓到的注册码 / 整段匹配 / 提示怎么改成整串
        assert "抓到的注册码" in html
        assert "整段匹配" in html
        assert "想抓整串就把整串括起来" in html
        assert "(XYING-Whitelist_xxxxxxxxxx)" in html
        assert 'id="rg-test-result"' in html
        # 「可见位数至少」那个阈值说明不能被顺手删掉（同一个表单区块）
        assert "可见位数至少" in html

    def test_panel_uses_the_new_endpoint_and_the_engine_semantics(self) -> None:
        body = _js_function_body(_reg_grab_template(), "testPattern")

        assert "/api/reg_grab/test_extract" in body
        assert "/api/rules/test" not in body, "别再走转发页那个只回答「匹配到了没有」的端点"
        assert "from_group" in body, "面板要显示抓到的是第几个捕获组"
        assert "escapeHtml" in body, "注进 innerHTML 的值必须先转义（正则/正文都是用户输入）"

    def test_panel_does_not_block_a_blank_task_id(self) -> None:
        html = _reg_grab_template()

        assert "任务 ID（可留空，自动生成）" in html
        assert "任务 ID 不能为空" not in html, "前端不该再拦 id 必填"
        save = _js_function_body(html, "saveTask")
        assert "if (!task.id)" not in save, "id 留空时保存路径不能提前 return"


# --------------------------------------------------------------------------- #
class TestHelpers:
    def test_iter_inline_buttons_flattens_rows(self) -> None:
        msg = rg_message(markup=FakeMarkup([[FakeButton("a"), FakeButton("b")], [FakeButton("c")]]))
        assert [button.text for button in iter_inline_buttons(msg)] == ["a", "b", "c"]

    def test_iter_inline_buttons_ignores_reply_keyboard(self) -> None:
        msg = rg_message(markup=FakeMarkup([[FakeButton("a")]], inline=False))
        assert iter_inline_buttons(msg) == []

    def test_step_label_prefers_name(self) -> None:
        assert step_label(RegGrabStep(type="wait", seconds=1)) == "等待"
        assert step_label(RegGrabStep(type="wait", seconds=1, name="等它处理")) == "等它处理"
