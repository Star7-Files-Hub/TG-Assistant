"""配置热重载：面板改完即生效，**不用重启账号**。

用户的原话是「抢注，转发，抢红包都需要有热重载功能」。在此之前只有转发引擎能重载，
而且它靠「下一条消息顺带检查」—— 零流量的群永远不重载，一旦 handler 过滤器把新群
挡在外面，新群的消息永远进不来 ⇒ 检查永远不触发 ⇒ 过滤器永远不更新，**死锁**。

现在改成：:class:`AccountRunner` 每秒看一次 ``config.json`` 的 mtime，
变了就**读一次盘**、把同一份 :class:`AccountConfig` 分发给三个引擎。

本文件覆盖三层：
1. 三个引擎各自的 ``apply_config``（装/拆 handler、换过滤、按任务 id 继承计数）；
2. 引擎级「改完当场生效」的行为验收（不只看返回值，看真的抢不抢）；
3. runner 的 mtime 监视器（含"配置写坏了要沿用旧配置继续跑"）。
"""

from __future__ import annotations

import asyncio
import os

import pytest

from tg_assistant.config import AccountConfig, AccountRecord, ForwardExcludes
from tg_assistant.forward_excludes import ForwardExcludeStore
from tg_assistant.forwarder import ForwardEngine
from tg_assistant.red_packet import RedPacketHunter
from tg_assistant.reg_grab import RegGrabHunter
from tg_assistant.runner import AccountRunner

from .conftest import FakeButton, FakeChat, FakeClient, FakeMarkup, make_message
from .test_forwarder import SRC, build_config as fwd_config, drain, src_message
from .test_red_packet import (
    button_markup as rp_button_markup,
    rp_config,
    rp_message,
    rp_multi,
)
from .test_reg_grab import (
    CHAT as RG_CHAT,
    CODE,
    bot_client as rg_client,
    rg_message,
    rg_multi,
)

DST = -1002626018568
NEW_CHAT = -1002543096170


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


# --------------------------------------------------------------------------- #
# 抢红包
# --------------------------------------------------------------------------- #
class TestRedPacketHotReload:
    @pytest.mark.asyncio
    async def test_registers_lazily_when_turned_on(self, alog):
        """先关着配好、再打开 —— 打开的瞬间才装 handler。

        多任务之后这是**常态**流程，不是边角情况。
        """
        client = FakeClient()
        hunter = RedPacketHunter(client, rp_config(enabled=False), alog)
        assert client.handlers == []

        assert await hunter.apply_config(rp_config(enabled=True)) is True
        assert len(client.handlers) == 2, "消息 + 编辑事件两个 handler"
        assert hunter.enabled is True

    @pytest.mark.asyncio
    async def test_turning_off_removes_handlers(self, alog):
        client = FakeClient()
        hunter = RedPacketHunter(client, rp_config(), alog)
        await hunter.register()
        assert client.handlers

        assert await hunter.apply_config(rp_config(enabled=False)) is True
        assert client.handlers == [], "关掉之后不该再收到任何消息"

    @pytest.mark.asyncio
    async def test_unchanged_config_is_skipped(self, alog):
        """面板原样保存一次不该触发重建（否则在途抢包会被无谓打扰）。"""
        client = FakeClient()
        hunter = RedPacketHunter(client, rp_config(), alog)
        await hunter.register()
        before = list(client.handlers)

        assert await hunter.apply_config(rp_config()) is False
        assert client.handlers == before, "内容没变就不该重建 handler"

    @pytest.mark.asyncio
    async def test_added_task_appears_and_old_counts_survive(self, alog):
        hunter = RedPacketHunter(FakeClient(), rp_multi({"id": "a"}), alog)
        await hunter.register()
        hunter.stats["success"] = 3
        hunter.task_stats["a"]["success"] = 3

        assert await hunter.apply_config(rp_multi({"id": "a"}, {"id": "b"})) is True
        assert [task.id for task in hunter.prepared] == ["a", "b"]
        assert set(hunter.task_stats) == {"a", "b"}
        assert hunter.task_stats["a"]["success"] == 3, "老任务的计数不能被清掉"
        assert hunter.task_stats["b"]["success"] == 0

    @pytest.mark.asyncio
    async def test_max_concurrency_is_rebuilt(self, alog):
        """并发上限是账号级资源限制，改了必须当场生效。"""
        hunter = RedPacketHunter(FakeClient(), rp_multi({"id": "a"}), alog)
        await hunter.register()
        assert hunter._semaphore._value == 3

        await hunter.apply_config(rp_multi({"id": "a"}, max_concurrency=1))
        assert hunter._semaphore._value == 1

    @pytest.mark.asyncio
    async def test_watched_chats_follow_config(self, alog):
        """把监听从某个群改到另一个群：过滤器必须跟着换。

        🔴 这是热重载最容易漏、也最难自己发现的一处：只换 ``self.config`` 而不换
        handler 的过滤器，**新群的消息根本进不来** —— 配置改得再对也没用，
        日志里一条错都不报。
        """
        client = FakeClient()
        hunter = RedPacketHunter(client, rp_multi({"id": "a", "chats": [DST]}), alog)
        await hunter.register()
        old_handlers = [handler for handler, _ in client.handlers]
        assert hunter.watched_chats() == [DST]

        await hunter.apply_config(rp_multi({"id": "a", "chats": [NEW_CHAT]}))
        assert hunter.watched_chats() == [NEW_CHAT]
        new_handlers = [handler for handler, _ in client.handlers]
        assert len(new_handlers) == len(old_handlers)
        assert all(new is not old for new, old in zip(new_handlers, old_handlers)), (
            "过滤器变了就必须重建 handler"
        )

    @pytest.mark.asyncio
    async def test_new_keywords_take_effect_without_restart(self, alog):
        """行为验收：改完关键词，**同一个红包**从抢不到变成抢得到。"""
        client = FakeClient(callback_answer="🧧 抢到 50 积分！")
        hunter = RedPacketHunter(
            client, rp_config(detect={"button_keywords": ["旧词"]}), alog
        )
        await hunter.register()

        msg = rp_message("🧧 积分红包", markup=rp_button_markup(("🧧 抢积分", b"grab")))
        hunter._dispatch(msg, edited=True)
        await asyncio.sleep(0)
        assert client.callbacks == [], "旧关键词不该命中"

        assert await hunter.apply_config(
            rp_config(detect={"button_keywords": ["抢积分"]})
        ) is True
        hunter._dispatch(msg, edited=True)
        await asyncio.gather(*list(hunter._tasks))
        assert len(client.callbacks) == 1, "新关键词必须当场生效"

    @pytest.mark.asyncio
    async def test_in_flight_state_is_preserved(self, alog):
        """``_bus`` / ``_seen`` / ``_settled`` 不能被热重载清空。

        正在抢的那个包、以及「这条消息已经定论」的记忆，都不能因为用户随手
        点了保存就丢掉 —— 丢掉的后果是又点一次已经抢过的红包。
        """
        hunter = RedPacketHunter(FakeClient(), rp_multi({"id": "a"}), alog)
        await hunter.register()
        bus = hunter._bus
        hunter._seen[(1, 2)] = 123.0
        hunter._settled[(1, 2)] = (123.0, "success")

        await hunter.apply_config(rp_multi({"id": "a"}, {"id": "b"}))
        assert hunter._bus is bus
        assert hunter._seen[(1, 2)] == 123.0
        assert hunter._settled[(1, 2)] == (123.0, "success")

    @pytest.mark.asyncio
    async def test_window_change_is_applied(self, alog):
        """全局动手时段也在热重载范围内。"""
        hunter = RedPacketHunter(FakeClient(), rp_multi({"id": "a"}), alog)
        await hunter.register()
        assert hunter.config.in_window is True

        start, end = "08:00", "09:00"
        await hunter.apply_config(
            rp_multi({"id": "a"}, window={"enabled": True, "start": start, "end": end})
        )
        assert hunter.config.window.enabled is True
        assert hunter.config.window.start == start
        assert hunter.config.in_window == hunter.config.window.contains()


# --------------------------------------------------------------------------- #
# 抢注
# --------------------------------------------------------------------------- #
class TestRegGrabHotReload:
    @pytest.mark.asyncio
    async def test_registers_lazily_when_turned_on(self, alog):
        client = rg_client()
        hunter = RegGrabHunter(client, rg_multi(enabled=False), alog)
        assert client.handlers == []

        config = rg_multi(
            {"id": "a", "detect": {"code_pattern": r"Register_([A-Za-z0-9]{10})"}}
        )
        assert await hunter.apply_config(config) is True
        assert len(client.handlers) == 2
        assert hunter.enabled is True

    @pytest.mark.asyncio
    async def test_turning_off_removes_handlers(self, alog):
        client = rg_client()
        config = rg_multi(
            {"id": "a", "detect": {"code_pattern": r"Register_([A-Za-z0-9]{10})"}}
        )
        hunter = RegGrabHunter(client, config, alog)
        await hunter.register()
        assert client.handlers

        off = rg_multi(
            {"id": "a", "detect": {"code_pattern": r"Register_([A-Za-z0-9]{10})"}},
            enabled=False,
        )
        assert await hunter.apply_config(off) is True
        assert client.handlers == []

    @pytest.mark.asyncio
    async def test_unchanged_config_is_skipped(self, alog):
        client = rg_client()
        config = rg_multi(
            {"id": "a", "detect": {"code_pattern": r"Register_([A-Za-z0-9]{10})"}}
        )
        hunter = RegGrabHunter(client, config, alog)
        await hunter.register()
        before = list(client.handlers)
        assert await hunter.apply_config(config) is False
        assert client.handlers == before

    @pytest.mark.asyncio
    async def test_added_task_appears_and_old_counts_survive(self, alog):
        client = rg_client()
        hunter = RegGrabHunter(
            client,
            rg_multi({"id": "a", "detect": {"code_pattern": r"Register_([A-Za-z0-9]{10})"}}),
            alog,
        )
        await hunter.register()
        hunter.stats["success"] = 2
        hunter.task_stats["a"]["success"] = 2

        doubled = rg_multi(
            {"id": "a", "detect": {"code_pattern": r"Register_([A-Za-z0-9]{10})"}},
            {"id": "b", "detect": {"code_pattern": r"Register_([A-Za-z0-9]{10})"}},
        )
        assert await hunter.apply_config(doubled) is True
        assert [task.id for task in hunter.prepared] == ["a", "b"]
        assert hunter.task_stats["a"]["success"] == 2, "老任务的计数不能被清掉"
        assert hunter.task_stats["b"]["success"] == 0

    @pytest.mark.asyncio
    async def test_watched_chats_follow_config(self, alog):
        client = rg_client()
        base = {"detect": {"code_pattern": r"Register_([A-Za-z0-9]{10})"}}
        hunter = RegGrabHunter(
            client, rg_multi({"id": "a", "chats": [DST], **base}), alog
        )
        await hunter.register()
        old_handlers = [handler for handler, _ in client.handlers]

        await hunter.apply_config(rg_multi({"id": "a", "chats": [NEW_CHAT], **base}))
        assert hunter.watched_chats() == [NEW_CHAT]
        new_handlers = [handler for handler, _ in client.handlers]
        assert all(new is not old for new, old in zip(new_handlers, old_handlers))

    @pytest.mark.asyncio
    async def test_new_pattern_takes_effect_without_restart(self, alog):
        """行为验收：换一条识别正则，原来抓不到的码当场就能抓到。"""
        client = rg_client()
        hunter = RegGrabHunter(
            client,
            rg_multi({"id": "a", "detect": {"code_pattern": r"NOPE_([A-Za-z0-9]{10})"}}),
            alog,
        )
        await hunter.register()
        assert hunter._match(rg_message(CODE), RG_CHAT) is None

        assert await hunter.apply_config(
            rg_multi({"id": "a", "detect": {"code_pattern": r"Register_([A-Za-z0-9]{10})"}})
        ) is True
        assert hunter._match(rg_message(CODE), RG_CHAT) is not None, (
            "新正则会后必须当场生效"
        )

    @pytest.mark.asyncio
    async def test_dedupe_state_is_preserved(self, alog):
        """``_seen_codes`` / ``_used`` 不能被热重载清空（否则会重复执行步骤链）。"""
        client = rg_client()
        hunter = RegGrabHunter(
            client,
            rg_multi({"id": "a", "detect": {"code_pattern": r"Register_([A-Za-z0-9]{10})"}}),
            alog,
        )
        await hunter.register()
        hunter._seen_codes["MSKY-30-Register_Ex5I0Fx5Bg"] = 1.0
        hunter._used["MSKY-30-Register_Ex5I0Fx5Bg"] = 1.0
        bus = hunter._bus

        await hunter.apply_config(
            rg_multi(
                {"id": "a", "detect": {"code_pattern": r"Register_([A-Za-z0-9]{10})"}},
                {"id": "b", "detect": {"code_pattern": r"Register_([A-Za-z0-9]{10})"}},
            )
        )
        assert hunter._bus is bus
        assert hunter._seen_codes["MSKY-30-Register_Ex5I0Fx5Bg"] == 1.0
        assert "MSKY-30-Register_Ex5I0Fx5Bg" in hunter._used


# --------------------------------------------------------------------------- #
# 转发
# --------------------------------------------------------------------------- #
class TestForwardHotReload:
    @pytest.mark.asyncio
    async def test_reload_rules_swaps_rules(self, alog):
        client = FakeClient()
        engine = ForwardEngine(client, fwd_config(), alog)
        engine.register()
        assert len(engine.rules) == 1

        new_config = fwd_config()
        new_config.forward.rules[0].name = "改过的规则"
        assert engine.apply_config(new_config) is True
        assert engine.rules[0].rule.name == "改过的规则"

    @pytest.mark.asyncio
    async def test_unchanged_config_is_skipped(self, alog):
        client = FakeClient()
        engine = ForwardEngine(client, fwd_config(), alog)
        engine.register()
        before = list(client.handlers)
        assert engine.apply_config(fwd_config()) is False
        assert client.handlers == before

    @pytest.mark.asyncio
    async def test_new_source_is_picked_up(self, alog):
        """行为验收：加一个监听来源，同一个引擎对象必须立刻开始收它。"""
        client = FakeClient()
        engine = ForwardEngine(client, fwd_config(sources=[NEW_CHAT]), alog)
        engine.register()
        assert engine.watched_chats() == [NEW_CHAT]
        old_handlers = [handler for handler, _ in client.handlers]

        config = fwd_config(sources=[NEW_CHAT, DST])
        assert engine.apply_config(config) is True
        assert set(engine.watched_chats()) == {NEW_CHAT, DST}
        # sources 变了 ⇒ 过滤器必须重建（否则新来源的消息根本进不来）。
        new_handlers = [handler for handler, _ in client.handlers]
        assert len(new_handlers) == len(old_handlers)
        assert all(new is not old for new, old in zip(new_handlers, old_handlers))

    @pytest.mark.asyncio
    async def test_disabling_removes_handlers(self, alog):
        client = FakeClient()
        engine = ForwardEngine(client, fwd_config(), alog)
        engine.register()
        assert client.handlers

        config = fwd_config()
        config.forward.enabled = False
        assert engine.apply_config(config) is True
        assert client.handlers == [], "关掉转发之后不该再收到任何消息"


# --------------------------------------------------------------------------- #
# runner 的 mtime 监视器
# --------------------------------------------------------------------------- #
class StubForward:
    """转发引擎的替身：``apply_config`` 是**同步**的。"""

    def __init__(self, changed: bool = True) -> None:
        self.changed = changed
        self.seen: list[AccountConfig] = []
        self.rules: list[object] = []

    def apply_config(self, config: AccountConfig) -> bool:
        self.seen.append(config)
        return self.changed


class StubHunter:
    """抢红包 / 抢注引擎的替身：``apply_config`` 是**异步**的。"""

    def __init__(self, changed: bool = True) -> None:
        self.changed = changed
        self.seen: list[AccountConfig] = []
        self.prepared: list[object] = []

    async def apply_config(self, config: AccountConfig) -> bool:
        self.seen.append(config)
        return self.changed


def make_runner(store, config: AccountConfig) -> AccountRunner:
    """造一个只差引擎的 runner：store 是真的（要读写磁盘上的 config.json）。"""
    runner = AccountRunner(
        AccountRecord(name="acc-a"), config, None, store.paths, store=store
    )
    runner.forwarder = StubForward()
    runner.hunter = StubHunter()
    runner.reg_grab = StubHunter()
    runner.alog = CapturingLog()
    runner._config_file = store.paths.account("acc-a").config_file
    runner._config_mtime = None
    # 「全局排除」名单的路径也要接上 —— 生产里 `AccountRunner.start()` 一起记这两个，
    # 少了它，下面的用例就测不到"只改全局名单也会触发重载"这条路径（而它正是
    # 用户最容易以为坏掉的地方：面板改完、群里照样转）。
    runner._global_excludes_file = store.paths.forward_excludes_file
    runner._global_excludes_mtime = None
    return runner


def bump_mtime(path, seconds: float = 10.0) -> None:
    """把 mtime 明确往前推 —— 不靠 sleep 去赌文件系统的时间精度。"""
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + seconds))


class TestRunnerConfigWatcher:
    @pytest.mark.asyncio
    async def test_mtime_change_reloads_and_dispatches(self, store, alog):
        """核心行为：config.json 变了 ⇒ 读**一次**盘 ⇒ 三个引擎都收到同一份。"""
        config = rp_config()
        store.save_account_config("acc-a", config)
        runner = make_runner(store, config)

        # 第一次必定读盘（_config_mtime 初始为 None）—— 这样"启动过程中配置刚好
        # 被改过"也不会漏掉。
        assert await runner._reload_config_if_changed() is True
        assert len(runner.forwarder.seen) == 1
        assert len(runner.hunter.seen) == 1
        assert len(runner.reg_grab.seen) == 1
        # 三个引擎拿到的是**同一份对象**，不会各自读盘读到不同版本。
        assert runner.forwarder.seen[0] is runner.hunter.seen[0]
        assert runner.hunter.seen[0] is runner.reg_grab.seen[0]

        # 没再动过 ⇒ 不再读盘、不再分发。
        assert await runner._reload_config_if_changed() is False
        assert len(runner.hunter.seen) == 1

    @pytest.mark.asyncio
    async def test_real_content_change_is_applied(self, store, alog):
        config = AccountConfig.default()
        store.save_account_config("acc-a", config)
        runner = make_runner(store, config)
        await runner._reload_config_if_changed()

        changed = AccountConfig.default()
        changed.red_packet.enabled = True
        store.save_account_config("acc-a", changed)
        bump_mtime(store.paths.account("acc-a").config_file)

        assert await runner._reload_config_if_changed() is True
        assert runner.config.red_packet.enabled is True
        assert runner.hunter.seen[-1].red_packet.enabled is True
        assert "配置已热重载（无需重启账号）" in runner.alog.text

    @pytest.mark.asyncio
    async def test_mtime_bump_without_content_change_is_noop(self, store, alog):
        """面板原样保存一次：mtime 变了，但引擎都说"没变" ⇒ 不报"已热重载"。"""
        config = AccountConfig.default()
        store.save_account_config("acc-a", config)
        runner = make_runner(store, config)
        await runner._reload_config_if_changed()
        for engine in (runner.forwarder, runner.hunter, runner.reg_grab):
            engine.changed = False
        # 第一轮必然"变了"（替身默认返回 True），会留下一条「已热重载」日志。
        # 这里换一个新的日志收集器，否则断言会被那一条历史记录污染。
        runner.alog = CapturingLog()

        bump_mtime(store.paths.account("acc-a").config_file)
        assert await runner._reload_config_if_changed() is False
        assert "配置已热重载" not in runner.alog.text

    @pytest.mark.asyncio
    async def test_broken_config_keeps_old_one(self, store, alog):
        """面板写坏了配置：**沿用旧配置继续跑**，不能把整个账号带停。

        一次坏写不该让转发 / 抢红包 / 抢注一起停摆。
        """
        config = AccountConfig.default()
        config.forward.enabled = True
        store.save_account_config("acc-a", config)
        runner = make_runner(store, config)
        await runner._reload_config_if_changed()
        applied = len(runner.hunter.seen)

        path = store.paths.account("acc-a").config_file
        path.write_text("{ 这不是合法 JSON", encoding="utf-8")
        bump_mtime(path)

        assert await runner._reload_config_if_changed() is False
        assert runner.config.forward.enabled is True, "旧配置必须还在"
        assert len(runner.hunter.seen) == applied, "坏配置不该分发给引擎"
        assert "配置热重载失败，继续沿用旧配置" in runner.alog.text

    @pytest.mark.asyncio
    async def test_missing_config_file_is_tolerated(self, store, alog):
        """config.json 被删掉时不能抛异常（stat 失败只是这一轮跳过）。"""
        runner = make_runner(store, AccountConfig.default())
        runner._config_file = store.paths.account("acc-a").config_file
        assert await runner._reload_config_if_changed() is False

    @pytest.mark.asyncio
    async def test_without_store_it_never_fires(self, paths, alog):
        """没有 store（纯本地构造）时监视器必须安静地什么都不做。"""
        runner = AccountRunner(
            AccountRecord(name="acc-a"), AccountConfig.default(), None, paths
        )
        runner.hunter = StubHunter()
        runner._config_file = None
        assert await runner._reload_config_if_changed() is False
        assert runner.hunter.seen == []

    @pytest.mark.asyncio
    async def test_run_forever_reloads_without_restart(self, store, alog):
        """端到端：``run_forever`` 跑着的时候，改配置就会被自动应用。"""
        config = AccountConfig.default()
        store.save_account_config("acc-a", config)
        runner = make_runner(store, config)
        runner.CONFIG_RELOAD_INTERVAL = 0.01  # type: ignore[misc]

        changed = AccountConfig.default()
        changed.red_packet.enabled = True
        store.save_account_config("acc-a", changed)
        bump_mtime(store.paths.account("acc-a").config_file)

        task = asyncio.create_task(runner.run_forever(heartbeat=3600))
        try:
            for _ in range(200):
                if runner.hunter.seen:
                    break
                await asyncio.sleep(0.01)
        finally:
            # 不调 ``runner.stop()``：那会去 close 三个引擎/客户端，而这里它们只是
            # 替身。直接拍停止信号，和 ``stop()`` 最后一步做的事一样。
            runner._stopped.set()
            await asyncio.wait_for(task, timeout=5)

        assert runner.hunter.seen, "run_forever 期间改配置必须被应用"
        assert runner.hunter.seen[-1].red_packet.enabled is True
        assert runner._stopped.is_set()

    @pytest.mark.asyncio
    async def test_reload_error_does_not_kill_the_account(self, store, alog):
        """引擎的 ``apply_config`` 抛错时，``run_forever`` 必须活下来。

        🔴 热重载出错若冒到 ``_supervise``，会被当成账号崩溃然后重启 ——
        用户会莫名其妙掉线。
        """

        class Exploding(StubHunter):
            async def apply_config(self, config: AccountConfig) -> bool:
                raise RuntimeError("假装引擎炸了")

        config = AccountConfig.default()
        store.save_account_config("acc-a", config)
        runner = make_runner(store, config)
        runner.hunter = Exploding()
        runner.CONFIG_RELOAD_INTERVAL = 0.01  # type: ignore[misc]
        bump_mtime(store.paths.account("acc-a").config_file)

        task = asyncio.create_task(runner.run_forever(heartbeat=3600))
        await asyncio.sleep(0.2)
        alive = not task.done()
        runner._stopped.set()
        await asyncio.wait_for(task, timeout=5)

        assert alive, "热重载抛错不能把 run_forever 带崩"
        assert "配置热重载出错，已跳过这一轮" in runner.alog.text


# --------------------------------------------------------------------------- #
# 转发：「全局排除」名单也要热重载
# --------------------------------------------------------------------------- #
class TestForwardGlobalExcludesHotReload:
    """改全局排除名单（``data/forward_excludes.json``）当场生效，**不用重启账号**。

    那份名单**不在**任何账号的 ``config.json`` 里（所有账号共用一份）——
    只盯 ``config.json`` 的 mtime 的话，面板改完全局名单不会触发任何重载。
    用户改完的下一步动作就是去群里看有没有被拦住，看到照样转出去只会以为功能坏了，
    而日志里**一条错都不会有**（名单文件是合法的，只是没人去读）。

    所以这里用**真引擎**（不是 ``StubForward`` 替身）验收：
    ``_reload_config_if_changed()`` 返回 True 只说明"有人报告变了"，
    消息真的被拦住才是用户要的结果。
    """

    ACCOUNT = "acc-a"

    def _runner_with_real_engine(self, store, alog, client):
        """runner + 真转发引擎：配置落盘、引擎接真实 store（全局名单由它定位）。"""
        config = fwd_config()
        store.save_account_config(self.ACCOUNT, config)
        runner = make_runner(store, config)
        engine = ForwardEngine(client, config, alog, store=store, account=self.ACCOUNT)
        engine.register()
        runner.forwarder = engine
        return runner, engine

    @staticmethod
    def _write_global(store, *, chats=()) -> None:
        ForwardExcludeStore(store.paths.forward_excludes_file).save(
            ForwardExcludes.model_validate({"exclude_chats": list(chats)})
        )

    @pytest.mark.asyncio
    async def test_global_file_change_reloads_without_a_restart(self, store, alog):
        client = FakeClient()
        runner, engine = self._runner_with_real_engine(store, alog, client)

        # 第一次必定读一次盘（两个 mtime 初始都是 None）：启动过程中名单刚好被改过
        # 也不会漏掉；之后没再动过就不该反复读。
        assert await runner._reload_config_if_changed() is True
        assert await runner._reload_config_if_changed() is False

        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)
        assert len(client.forwarded) == 1, "名单还是空的，这条该正常转出去"

        # 只动全局名单文件：config.json 一个字节都没改
        self._write_global(store, chats=[SRC])

        assert await runner._reload_config_if_changed() is True, (
            "全局名单变了没触发重载 —— 用户改完要等重启才生效"
        )
        assert runner.forwarder is engine, "重载把引擎实例换掉了，那就是重启而不是热重载"
        assert runner._stopped.is_set() is False, "改一份名单不该把账号停掉"

        engine._handle(src_message("关键词123", message_id=101), edited=False)
        await drain(engine)
        assert len(client.forwarded) == 1, "重载后全局名单里的群必须当场被拦住"
        assert engine.stats["excluded"] == 1
        # 用户能看到的证据：面板 / 日志里必须写出来"真的重载了"
        assert "配置已热重载（无需重启账号）" in runner.alog.text

        # mtime 没再动 ⇒ 不该每一轮轮询都白重建一次 handler
        assert await runner._reload_config_if_changed() is False

    @pytest.mark.asyncio
    async def test_clearing_the_global_file_is_picked_up(self, store, alog):
        """把名单清空也要当场生效 —— 并集是"重算"而不是"只增不减"。

        只说"加进去能生效"是不够的：如果重载时把新名单**追加**到旧集合上，
        用户删掉一项后消息照样被拦，而且界面上看不出任何异常。
        """
        self._write_global(store, chats=[SRC])
        client = FakeClient()
        runner, engine = self._runner_with_real_engine(store, alog, client)
        await runner._reload_config_if_changed()

        engine._handle(src_message("关键词123"), edited=False)
        await drain(engine)
        assert client.forwarded == [], "名单里的群一开始就该被拦住"

        self._write_global(store)  # 清空
        # 明确把 mtime 往前推：不靠 sleep 赌文件系统的时间精度
        bump_mtime(store.paths.forward_excludes_file)

        assert await runner._reload_config_if_changed() is True
        engine._handle(src_message("关键词123", message_id=101), edited=False)
        await drain(engine)
        assert len(client.forwarded) == 1, "名单清空后消息该恢复转发"
        assert engine.snapshot()["global_exclude_chats"] == 0

    @pytest.mark.asyncio
    async def test_missing_global_file_is_tolerated(self, store, alog):
        """从没配过全局名单（文件不存在）时监视器要安静地正常工作。

        ``stat`` 一个不存在的文件会抛 ``OSError``；这里必须吞掉并当成"没变化"，
        否则轮询一上来就异常，``config.json`` 的重载也会一起被带停。
        """
        client = FakeClient()
        runner, _engine = self._runner_with_real_engine(store, alog, client)
        assert not store.paths.forward_excludes_file.exists()

        assert await runner._reload_config_if_changed() is True, "config.json 那次必须读到"
        assert await runner._reload_config_if_changed() is False
        assert "配置热重载出错" not in runner.alog.text
