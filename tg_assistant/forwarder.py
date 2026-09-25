"""秒级正则转发引擎。

延迟从哪里来、怎么压下去：

1. **不在 handler 里做慢活**：pyrogram 的 handler worker 是串行执行同一 group 的 handler，
   在里面 ``await`` 网络请求会阻塞后续消息。这里 handler 只做纯内存匹配（微秒级），
   命中后立刻 ``create_task`` 派发，真正的发送在独立任务里跑。
2. **零 ``get_chat``**：所有会话/用户判断都用预构建的集合，不发网络请求。
3. **正则预编译**：配置加载时编译一次。
4. **相册聚合**：同一 ``media_group_id`` 的多条消息在 ``media_group_window`` 内攒齐后
   一次性 ``forward_messages(message_ids=[...])``，避免拆成多条丢失排版。
5. **去重**：四层 —— 账号内 ``(规则 id, chat_id, message_id)`` + TTL，防止编辑事件或
   重复更新造成二次转发；**跨账号** ``(chat_id, message_id, 目标)`` 共享表，防止多个账号
   都在同一个源群里时把同一条消息各发一遍；**「频道 ↔ 群组 同内容」**（见
   :class:`ChannelGroupDedupe`）共享表，同一个运营方把同一条推广分别发到频道和它的群组时
   **先到的那条留下、后到的直接拦掉**；**「最近已转发的内容」**（见 :class:`RecentContentDedupe`）共享表，
   按目标记下最近 N 条 / 时间窗内已转发的内容指纹，同内容再来一遍直接跳过 ——
   这三张共享表都跨面板重建复用，否则窗口会被清空。
6. **forward 失败自动降级 copy**：源会话受保护时 Telegram 会拒 forward，此时自动改用
   复制再发一次，而不是直接失败。
7. **日志里的 ``mode=`` 是「实际生效」的模式**，不是配置值。取值见
   :meth:`ForwardEngine._send_to_target` 的文档 —— 带「丢链接」字样的那两种说明
   消息是**退成转发发出去的、正文里没有原文链接**（同时会打一条 WARNING）。
   ⚠️ 别把这里改回「一律写 ``rule.mode``」：那样日志会和同时打出的 WARNING 自相矛盾，
   而用户正是靠这一行判断链接有没有加上。

⚠️ **``copy`` 模式 = 按 ``file_id`` 重新发送一条新消息**，不是
``forward_messages(hide_sender_name=True)``。后者发出来的是**转发消息**，而 Telegram
**拒绝编辑转发消息**（实测 ``400 Bad Request: message can't be edited``）⇒
「先转发、再 edit 补上原文链接」这条路根本走不通。重发拿到的是全新消息，链接在**发送时**
就写进正文，一次 RPC 搞定、零编辑。

⚠️ **``forward`` 模式不动那条转发消息**：原文链接在它**下方**单独补发一条
（``🔗原文链接：<link>``）。``copy`` 才是写进当前消息正文。

每条转发都会记录 ``pipeline_ms``（消息在 Telegram 的时间戳到发送完成的总耗时）和
``handler_ms``（本进程内耗时），日志里直接能看出慢在哪一段。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import random
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, NamedTuple, Optional

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import ChatForwardsRestricted
from pyrogram.handlers import EditedMessageHandler, MessageHandler
from pyrogram.types import LinkPreviewOptions

from .client import SessionInvalid, with_flood_retry
from .config import AccountConfig, ChatRef, ForwardRule
from .logging_setup import AccountLogger
from .matching import (
    MAX_TEXT_LENGTH,
    SAFE_TEXT_LENGTH,
    CompiledMatcher,
    MatchResult,
    RefSet,
    apply_groups,
    build_variables,
    chat_identity,
    chat_kind,
    message_text,
    normalize_chat_kind,
    render_template,
    sender_of,
    truncate,
)
from .notify import BotNotifier, NotifyTask, build_notify_text


# --------------------------------------------------------------------------- #
# 去重
# --------------------------------------------------------------------------- #
class DedupeCache:
    """带 TTL 的一次性键集合。

    用惰性清理（每 N 次插入扫一遍）而不是后台定时任务，省掉一个 task。
    """

    __slots__ = ("_seen", "_ttl", "_ops")

    def __init__(self, ttl: float = 300.0) -> None:
        self._seen: dict[tuple[Any, ...], float] = {}
        self._ttl = ttl
        self._ops = 0

    def check_and_add(self, key: tuple[Any, ...]) -> bool:
        """首次出现返回 True；TTL 内重复出现返回 False。"""
        if self._ttl <= 0:
            return True
        now = time.monotonic()
        self._ops += 1
        if self._ops % 256 == 0:
            self._purge(now)
        seen_at = self._seen.get(key)
        if seen_at is not None and now - seen_at < self._ttl:
            return False
        self._seen[key] = now
        return True

    def _purge(self, now: float) -> None:
        expired = [k for k, t in self._seen.items() if now - t >= self._ttl]
        for key in expired:
            self._seen.pop(key, None)

    def __len__(self) -> int:
        return len(self._seen)


class CrossAccountDedupe:
    """跨账号去重：同一条源消息发往**同一个目标**，只允许一个账号发出去。

    为什么需要它：多个账号可能都在同一个源群里（规则也一样），于是同一条消息
    会被每个账号各转发一次，目标频道里就出现重复。账号内的 :class:`DedupeCache`
    是 **per-engine** 的，挡不住这种重复。

    键是 ``(源会话, 消息 id, 目标会话)`` 三元组，**必须带目标**：只按
    ``(源会话, 消息 id)`` 去重的话，「账号 A 发到 T1、账号 B 发到 T2」会被误判成
    重复，后一个账号**丢消息**（项目里早先那版 Postgres 去重就是两元组键）。

    为什么用进程内共享而不是数据库：单进程里所有账号的 handler 都跑在**同一个
    事件循环**上，而 :meth:`claim` 是**纯同步、无 await** 的 —— 在 handler 里天然
    原子，不可能出现两个账号同时抢到同一个名额。这比「每条消息连一次库」既快又准
    （那版实现是同步阻塞的，会把转发延迟从毫秒级推到几百毫秒）。

    TTL 按次传入、不在构造时固定：``dedupe_window`` 是**账号级**配置，多个账号
    可以配不同的值，共享表不能只有一个 TTL。
    """

    __slots__ = ("_seen", "_ops", "claimed", "rejected", "released")

    def __init__(self) -> None:
        #: 键 -> 过期时刻（``time.monotonic()`` 基准）。
        self._seen: dict[tuple[Any, ...], float] = {}
        self._ops = 0
        #: 占用成功（= 由本账号发出）的次数。
        self.claimed = 0
        #: 因「别的账号已经发过」而跳过的次数。
        self.rejected = 0
        #: 占用之后又**退还**的次数（发送失败）。用来把「真去重」和「占位后失败」分开。
        self.released = 0

    def claim(self, source_chat_id: Any, message_id: Any, target: Any, ttl: float) -> bool:
        """尝试占用「这条源消息发往这个目标」的名额。

        首次（或 TTL 过后）返回 ``True``，表示由本账号发送；TTL 内已被占用返回
        ``False``，调用方应当跳过。``ttl <= 0`` 表示关闭去重，恒返回 ``True``。
        """
        if ttl <= 0:
            self.claimed += 1
            return True
        now = time.monotonic()
        self._ops += 1
        if self._ops % 256 == 0:
            self._purge(now)
        key = (source_chat_id, message_id, str(target))
        deadline = self._seen.get(key)
        if deadline is not None and deadline > now:
            self.rejected += 1
            return False
        self._seen[key] = now + ttl
        self.claimed += 1
        return True

    def release(self, source_chat_id: Any, message_id: Any, target: Any) -> None:
        """退还名额 —— **发送失败时必须调用**。

        ``claim`` 是「**先占位、后发送**」（为了在事件循环里保持同步原子）。一旦发送失败
        而名额不退，这个键会在整个 TTL（默认 300s）内保持被占 ⇒ **另一个账号也补发不了**
        —— 这条消息对该目标就**彻底丢了**。退还之后别的账号还能正常补上。

        不存在的键退还也无害（幂等），所以调用方不必先判断。
        """
        self._seen.pop((source_chat_id, message_id, str(target)), None)
        self.released += 1

    def _purge(self, now: float) -> None:
        for key in [k for k, deadline in self._seen.items() if deadline <= now]:
            self._seen.pop(key, None)

    def __len__(self) -> int:
        return len(self._seen)

    def snapshot(self) -> dict[str, Any]:
        return {
            "size": len(self._seen),
            "claimed": self.claimed,
            "rejected": self.rejected,
            "released": self.released,
        }


# --------------------------------------------------------------------------- #
# 频道 ↔ 群组 双份消息去重
# --------------------------------------------------------------------------- #
#: 媒体字段（按优先级）—— 用来给「没有正文」的消息取指纹。
_MEDIA_ATTRS = (
    "photo",
    "video",
    "audio",
    "document",
    "animation",
    "voice",
    "video_note",
    "sticker",
)


def _media_signature(message: Any) -> Optional[str]:
    """媒体指纹：优先 ``file_unique_id``（同一个文件在不同会话里都一样），退回 ``file_id``。"""
    for attr in _MEDIA_ATTRS:
        obj = getattr(message, attr, None)
        if obj is None:
            continue
        for key in ("file_unique_id", "file_id"):
            value = getattr(obj, key, None)
            if value:
                return f"{attr}:{value}"
        return attr
    return None


def content_fingerprint(message: Any) -> Optional[str]:
    """跨会话比对「是不是同一条内容」用的指纹；无从比对时返回 ``None``。

    为什么需要它：同一个运营方会把**同一条推广**分别发到频道和它的关联群里，两条正文
    一模一样、只有「原文链接」不同（链接是按**来源会话**生成的），用户只想留群组那条。

    🔴 2026-09-21 线上取证**推翻**了两个想当然的假设，所以这里才必须是「内容指纹 + 会话
    类型」，而不是「看转发关系」或「先到先得」：

    1. 群里那条**不是**频道的转发（``forward_origin`` / ``forward_from_chat`` 都是空）
       ⇒ 拿不到「同一条原始消息」这个强标识，只能比内容；
    2. 两条的**先后顺序是随机的** —— 流光画廊那对是群组先到（47.505 / 48.354），
       秀儿那对却是**频道先到**（37.651 / 39.144，而群组 39.144 反而在后）
       ⇒ 「先到先得」会留下一半的错那条。

    取正文（``text`` / ``caption``）优先：这类推广帖一定有正文，规则本身也是按正文匹配的。
    没有正文时才退回媒体自身的 id。两者都没有 ⇒ ``None``，调用方直接放行。

    ⚠️ 用 ``surrogatepass`` 编码：消息里可能含非法代理对，直接 ``encode`` 会抛
    ``UnicodeEncodeError``，而这里只是算指纹，不该因为一条脏消息把转发链路带崩。
    """
    text = message_text(message).strip()
    if text:
        digest = hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()
        return f"text:{digest}"
    media = _media_signature(message)
    return f"media:{media}" if media else None


def _is_channel_group_pair(kind_a: Optional[str], kind_b: Optional[str]) -> bool:
    """是不是「一边频道、一边群组」这一对。

    只认 ``{"channel", "group"}`` 这一种组合（``supergroup`` / ``forum`` 已被
    :func:`~tg_assistant.matching.normalize_chat_kind` 归一成 ``group``）。
    类型判不出来（``None``，例如 pyrogram 的 ``community``）时**一律不算** ——
    宁可漏去重（目标里多一条重复），也不能误杀（消息彻底丢掉）。
    """
    return {kind_a, kind_b} == {"channel", "group"}


class _PairEntry:
    """``ChannelGroupDedupe`` 里的一条记录：先到的那条是谁、什么时候过期。"""

    __slots__ = ("deadline", "kind")

    def __init__(self, deadline: float, kind: Optional[str]) -> None:
        self.deadline = deadline
        self.kind = kind


class ChannelGroupDedupe:
    """频道与它的关联群组各发一遍同一条内容时，只留**先到**的那条。

    为什么需要它：同一个运营方会把同一条推广分别发到频道和群里（两条正文完全一样、
    只有来源链接不同 —— 链接是按**来源会话**生成的）。两条都命中同一条规则，
    于是目标里出现两份，用户只要其中一份。

    键是 ``(内容指纹, 目标会话)``，**必须带目标**：同一条内容发往不同目标时互不影响。

    **先到先得，后到的直接不发**：

    - 群组那条先到 ⇒ 频道那条判成重复，直接不发；
    - 频道那条先到 ⇒ 群组那条判成重复，**同样直接不发**。

    🔴 **2026-09-25 起不再撤回已发出的那条。** 早先的版本是「群组优先，与先后顺序无关」：
    频道先到就先把它发出去，等群组那条到了再把频道那条**撤回**、重发群组那条。
    用户的原话是「我要的是重复的直接拦截，而不是一直更新再自动删除上一条消息」——
    目标里先冒出一条、过一两秒又消失、再补一条新的，看起来就是消息被吞了又重发，
    比多一条重复还难受；而且撤回本身是**尽力而为**的（没删除权限、消息太旧都会失败），
    失败就在目标里留下两条，等于白折腾。

    改成「先到先得」之后，这一层是**纯判断、零副作用**：拦下就是不发，不会先发再删。
    代价是频道先到的那一对会留下频道那条（链接指向频道帖而不是群组帖）——
    这是用户明确选的取舍。

    为什么用进程内共享表：所有账号的 handler 跑在**同一个事件循环**上，而 :meth:`claim`
    是**纯同步、无 await** 的 ⇒ 天然原子，不可能两条同时抢到名额。
    """

    __slots__ = ("_seen", "_ops", "claimed", "channel_dropped", "group_dropped", "released")

    def __init__(self) -> None:
        #: 键 -> 记录（``deadline`` 用 ``time.monotonic()`` 基准）。
        self._seen: dict[tuple[str, str], _PairEntry] = {}
        self._ops = 0
        #: 建立记录（= 本条内容在这个目标上第一次出现）的次数。
        self.claimed = 0
        #: 频道那条后到、被群组拦掉的次数。
        self.channel_dropped = 0
        #: 群组那条后到、被频道拦掉的次数。
        self.group_dropped = 0
        #: 发送失败后**退还**名额的次数。
        self.released = 0

    def claim(
        self,
        fingerprint: str,
        target: Any,
        kind: Optional[str],
        ttl: float,
    ) -> bool:
        """登记「这条内容要发往这个目标」，返回**该不该发**。

        ``ttl <= 0`` 表示关闭本层去重，恒放行。

        🔴 返回 ``False`` 就是「本条不发」——**不会**要求调用方去撤回任何东西。
        """
        if ttl <= 0:
            self.claimed += 1
            return True
        now = time.monotonic()
        self._ops += 1
        if self._ops % 256 == 0:
            self._purge(now)
        key = (fingerprint, str(target))
        entry = self._seen.get(key)
        if entry is not None and entry.deadline <= now:
            self._seen.pop(key, None)
            entry = None

        if entry is None:
            self._seen[key] = _PairEntry(now + ttl, kind)
            self.claimed += 1
            return True

        if not _is_channel_group_pair(entry.kind, kind):
            # 不是「频道 ↔ 群组」那一对（同类型，或类型判不出来）⇒ 不去重，各自发。
            # ⚠️ 这里**不能**顺手记 claimed/覆盖记录：否则两个群组发的同内容帖子会被误杀。
            return True

        # 已经是「频道 ↔ 群组」那一对 ⇒ 先到的那条留着，后到的这条直接拦掉。
        # ⚠️ **不覆盖记录、不撤回**：记录里的 ``kind`` 保持「先到的那条」，
        #    这样万一还有第三条同内容的消息到达，判据不会漂移。
        if kind == "channel":
            self.channel_dropped += 1
        else:
            self.group_dropped += 1
        return False

    def release(self, fingerprint: str, target: Any) -> None:
        """退还名额 —— **发送失败时必须调用**。

        不退的话，这个「内容 + 目标」键会在整个 TTL 里保持被占：同内容的另一条会被
        判成重复而跳过 ⇒ 这条消息对该目标**彻底丢了**（与
        :meth:`CrossAccountDedupe.release` 同一个坑）。不存在的键退还是无害空操作。
        """
        self._seen.pop((fingerprint, str(target)), None)
        self.released += 1

    def _purge(self, now: float) -> None:
        for key in [k for k, entry in self._seen.items() if entry.deadline <= now]:
            self._seen.pop(key, None)

    def __len__(self) -> int:
        return len(self._seen)

    def snapshot(self) -> dict[str, Any]:
        return {
            "size": len(self._seen),
            "claimed": self.claimed,
            "channel_dropped": self.channel_dropped,
            "group_dropped": self.group_dropped,
            "released": self.released,
        }


class RecentContentDedupe:
    """「目标里**最近已经转发过**的内容」—— 命中新消息后先跟它比一比。

    为什么需要它：前面两层去重都只认**消息 id**（``(chat_id, message_id)``），
    以及「频道↔群组」这一特定组合。现实里的重复远不止这两种 —— 同一个运营方把
    同一段广告发到好几个群、隔几小时又发一遍、或者转发关系压根不存在，
    这些情况下 ``message_id`` 全都不同 ⇒ 目标里就会出现一堆一模一样的消息。

    小白原话：「命中新消息要跟前 5 条对比，不一致才进行转发，或者一天内而不是前 x 条」。

    ⇒ 两个条件取**并集**：既看最近 ``limit`` 条，也看 ``ttl`` 秒内的全部
    （哪个更宽算哪个）。``limit=0`` ⇒ 只看时间窗；``ttl=0`` ⇒ 只看条数；
    两个都是 0 ⇒ 关掉这一层。

    ⚠️ 分桶键是**目标**：同一个内容发到不同目标互不影响，不会互相吃掉。
    """

    def __init__(self, limit: int = 5, ttl: float = 86400.0) -> None:
        self.limit = max(0, int(limit))
        self.ttl = float(ttl)
        #: 目标 -> deque[(时间戳, 指纹)]，左旧右新。
        self._by_target: dict[str, deque] = {}
        self.hits = 0
        #: 串行闸门：``(指纹, 目标)`` -> ``[asyncio.Lock, 待用计数]``。见 :meth:`gate`。
        self._gates: dict[tuple[str, str], list] = {}
        #: 因为闸门串行而**省下来**的重复发送次数（诊断用）。
        self.gated = 0

    @property
    def enabled(self) -> bool:
        return self.limit > 0 or self.ttl > 0

    @contextlib.asynccontextmanager
    async def gate(self, fingerprint: Optional[str], target: Any):
        """「同一条内容发往同一个目标」的串行闸门。

        🔴 **为什么必须有它** —— 这是线上实测出来的真 bug，不是理论情况：

        三层去重全是「**先判断、后发送**」，而发送要 ``await``（线上 ``pipeline_ms``
        实测到过 4.2 秒）。判断和落表之间隔着一个 await，于是两个并发的 handler
        完全可能**都在对方落表之前通过了判断**：

        - 同一个账号的频道那条与群组那条（两个聊天各起一个 handler，并发跑）；
        - 两个账号的同内容消息（源消息 id 不同 ⇒ 跨账号去重那颗键不同 ⇒ 都放行）。

        2026-09-23 线上取证（26 小时）实测到 6 个指纹被重复转发，其中
        ``text:b6fe2d70…`` 是 小白 11:19:37.045、SevenStar 11:19:37.358，
        两条相距 **0.3 秒** —— 就是这种竞态。

        闸门让「同一内容 + 同一目标」的 handler **排队**执行：先到的发完并落表，
        后到的再判断时就一定能看到，于是被正常跳过。竞态从根上消失，
        而不是靠「把窗口调小」。

        ⚠️ 闸门只串行**同内容同目标**：不同指纹、不同目标互不阻塞。
        没有指纹（既无正文也无媒体）时无从比对，直接放行、不加锁。
        本表在多账号之间是**共享实例**，所以闸门天然也是跨账号的。
        """
        if fingerprint is None:
            yield
            return
        key = (fingerprint, str(target))
        entry = self._gates.get(key)
        if entry is None:
            entry = [asyncio.Lock(), 0]
            self._gates[key] = entry
        entry[1] += 1
        #: 到达时锁已被别人持有 ⇒ 本条曾排队等待，极可能就是「本来会重复」的那条。
        waited = entry[0].locked()
        try:
            async with entry[0]:
                if waited:
                    self.gated += 1
                yield
        finally:
            entry[1] -= 1
            # 没人持有、也没人排队 ⇒ 收掉，别让字典无限长。
            if entry[1] <= 0 and not entry[0].locked():
                self._gates.pop(key, None)

    def _bucket(self, target: Any) -> deque:
        return self._by_target.setdefault(str(target), deque())

    def _prune(self, bucket: deque) -> None:
        """丢弃**同时**超出条数上限**且**已过期的项 —— 这就是「取并集」的实现。"""
        now = time.time()
        while bucket and len(bucket) > self.limit and (now - bucket[0][0]) > self.ttl:
            bucket.popleft()

    def contains(self, fingerprint: Optional[str], target: Any) -> bool:
        """这条内容是不是**刚刚**就往这个目标发过。"""
        if fingerprint is None or not self.enabled:
            return False
        bucket = self._by_target.get(str(target))
        if not bucket:
            return False
        self._prune(bucket)
        hit = any(fp == fingerprint for _, fp in bucket)
        if hit:
            self.hits += 1
        return hit

    def add(self, fingerprint: Optional[str], target: Any) -> None:
        """记下「这条内容刚发到这个目标」。同一个指纹只留最新一条。"""
        if fingerprint is None or not self.enabled:
            return
        bucket = self._bucket(target)
        for item in list(bucket):
            if item[1] == fingerprint:
                bucket.remove(item)
        bucket.append((time.time(), fingerprint))
        self._prune(bucket)

    def __len__(self) -> int:
        return sum(len(b) for b in self._by_target.values())

    def snapshot(self) -> dict[str, Any]:
        return {
            "size": len(self),
            "targets": len(self._by_target),
            "hits": self.hits,
            "limit": self.limit,
            "ttl": self.ttl,
            #: 曾因闸门排队等待的条数（≈ 本来会重复发送的条数）。
            "gated": self.gated,
        }


# --------------------------------------------------------------------------- #
# 预编译规则
# --------------------------------------------------------------------------- #
@dataclass
class PreparedRule:
    """把 :class:`ForwardRule` 预处理成运行期高效结构。"""

    rule: ForwardRule
    matcher: CompiledMatcher
    sources: RefSet
    exclude_sources: RefSet
    from_users: RefSet
    exclude_users: RefSet
    #: 转发目标集合。**只用于"目标不能当来源"的防循环判断**，不参与其它筛选。
    targets: RefSet
    last_fired: float = 0.0
    stats: dict[str, int] = field(
        default_factory=lambda: {
            "matched": 0,
            "sent": 0,
            "failed": 0,
            "skipped": 0,
            #: 会话层就被拒的次数（来源不匹配 / 被排除 / 私聊 / 命中「目标不能当来源」）。
            #: 规则配了却不生效时，这个数字是线上**唯一**能一眼看出问题的信号。
            "chat_rejected": 0,
        }
    )

    @classmethod
    def build(cls, rule: ForwardRule) -> "PreparedRule":
        return cls(
            rule=rule,
            matcher=CompiledMatcher(rule.match),
            sources=RefSet(rule.sources),
            exclude_sources=RefSet(rule.exclude_sources),
            from_users=RefSet(rule.from_users),
            exclude_users=RefSet(rule.exclude_users),
            targets=RefSet(rule.targets),
        )

    @property
    def id(self) -> str:
        return self.rule.id

    @property
    def label(self) -> str:
        return self.rule.label

    def chat_allowed(
        self,
        chat_id: Optional[int],
        chat_username: Optional[str],
        kind: Optional[str] = None,
    ) -> tuple[bool, str]:
        """``kind`` 可以是 :func:`~tg_assistant.matching.chat_kind` 的返回值，
        也可以是 pyrogram 的原始 ``ChatType`` / 其 ``.value``。

        ``sources`` 为空表示"监听全部"，但**只限群组与频道**：
        私聊一律不参与转发。否则任何陌生人给账号发一条含关键词的私信，
        都会被原样转发到目标频道里去。

        这里走 :func:`~tg_assistant.matching.normalize_chat_kind` 而不是直接比较
        字符串：``private`` / ``bot`` / ``direct`` 都是 1:1 会话，
        逐个枚举容易漏（``direct`` 就漏过一次）。

        **本规则的目标会话永远不能当来源**，即使它被显式写进 ``sources``。
        原因：``sources=[]``（监听全部）时，转发到目标的那些新消息会被本账号
        重新监听到（新消息 = 新 id，去重缓存拦不住），再次命中同一条规则，
        于是每转发一次就多产生一次命中 —— 正反馈死循环，几秒内就能刷爆目标频道。
        详见 ``tests/test_forwarder.py::TestPreparedRule::test_target_never_acts_as_source``。
        """
        if self.targets and self.targets.matches(chat_id, chat_username):
            return False, "该会话是本规则的目标，不能作为来源（防转发循环）"
        if self.exclude_sources and self.exclude_sources.matches(chat_id, chat_username):
            return False, "来源在 exclude_sources 中"
        if self.sources:
            if not self.sources.matches(chat_id, chat_username):
                return False, "来源不在 sources 中"
            return True, ""
        if normalize_chat_kind(kind) == "private":
            return False, "未限定 sources 时只监听群组与频道，私聊不转发"
        return True, ""

    def sender_allowed(
        self, sender_id: Optional[int], username: Optional[str], is_self: bool
    ) -> tuple[bool, str]:
        if self.rule.ignore_self and is_self:
            return False, "忽略自己发送的消息（ignore_self）"
        if self.exclude_users and self.exclude_users.matches(sender_id, username, is_self=is_self):
            return False, "发送者在 exclude_users 中"
        if self.from_users and not self.from_users.matches(sender_id, username, is_self=is_self):
            return False, "发送者不在 from_users 中"
        return True, ""

    def interval_ok(self, now: float) -> tuple[bool, str]:
        if self.rule.min_interval <= 0:
            return True, ""
        elapsed = now - self.last_fired
        if elapsed < self.rule.min_interval:
            return False, f"触发过于频繁（{elapsed:.1f}s < min_interval {self.rule.min_interval}s）"
        return True, ""


# --------------------------------------------------------------------------- #
# 相册缓冲
# --------------------------------------------------------------------------- #
@dataclass
class MediaGroupBuffer:
    """同一相册的消息缓冲。"""

    chat_id: int
    group_id: str
    rule_id: str
    message_ids: list[int]
    first_message: Any
    match: MatchResult
    created_at: float = field(default_factory=time.monotonic)
    task: Optional[asyncio.Task[None]] = None


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #
class ForwardEngine:
    """把匹配到的消息转发到目标会话，并同步推送 bot 通知。"""

    #: handler group：转发用 0，抢红包用 1，互不干扰。
    HANDLER_GROUP = 0

    #: 热重载检查间隔（秒）。只决定「多久发现一次变更」，不影响正确性 ——
    #: 真正决定要不要重载的是 ``config.json`` 的 mtime。
    RELOAD_CHECK_INTERVAL = 1.0

    def __init__(
        self,
        client: Client,
        config: AccountConfig,
        alog: AccountLogger,
        notifier: Optional[BotNotifier] = None,
        *,
        store: Optional[Any] = None,
        account: Optional[str] = None,
        shared_dedupe: Optional[CrossAccountDedupe] = None,
        pair_dedupe: Optional[ChannelGroupDedupe] = None,
        recent_dedupe: Optional[RecentContentDedupe] = None,
    ) -> None:
        self.client = client
        self.config = config
        self.alog = alog.bind("forward")
        self.notifier = notifier
        self.rules: list[PreparedRule] = [
            PreparedRule.build(rule) for rule in config.forward.active_rules
        ]
        #: 账号级全局排除：这些会话任何规则都不监听（与每条规则的 exclude_sources 互补）。
        self._exclude_chats = RefSet(config.forward.exclude_chats)
        self.dedupe = DedupeCache(config.forward.dedupe_window)
        #: 跨账号去重表（同一条消息发往同一个目标只允许一个账号发）。
        #: 由 ``MultiRunner`` 创建、多个账号**共享同一个实例**；不传（单测 / 离线场景）
        #: 即关闭跨账号去重，行为与从前一致。
        self._shared_dedupe = shared_dedupe
        #: 「频道 ↔ 群组 同内容」去重表（同样是多账号共享同一个实例）。
        #: 不传（单测 / 离线场景）即关闭这一层，行为与从前一致。
        self._pair_dedupe = pair_dedupe
        #: 「最近已转发的内容」表（同样是多账号共享同一个实例）。
        #: 不传时按本账号配置自建一张（单账号场景下效果一样）。
        self._recent = recent_dedupe
        if self._recent is None:
            fwd = config.forward
            self._recent = RecentContentDedupe(
                limit=getattr(fwd, "recent_dedupe_limit", 5),
                ttl=getattr(fwd, "recent_dedupe_window", 86400.0),
            )

        # ---- 规则热重载 ----
        #: 只有同时给了 ``store`` 与 ``account`` 才开启；单测 / 离线场景不传，
        #: 行为与从前完全一致（永不读盘）。
        self._store = store
        self._account = account
        self._config_file = self._resolve_config_file()
        #: 上次读到的 config.json mtime。``None`` = 还没读到过。
        self._rules_mtime: Optional[float] = self._config_mtime()
        self._last_reload_check = 0.0
        self._handlers: list[tuple[Any, int]] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._media_groups: dict[tuple[int, str], MediaGroupBuffer] = {}
        self._lock = asyncio.Lock()
        self.stats = {
            "received": 0,
            "excluded": 0,
            "matched": 0,
            "forwarded": 0,
            "failed": 0,
            "deduped": 0,
            #: 因**别的账号**已经把这条消息发往同一目标而跳过的次数。
            "cross_deduped": 0,
            #: 同一条内容已由**群组**发往同一目标，因而跳过**频道**这条的次数。
            "pair_deduped": 0,
            #: 同一条内容已由**频道**发往同一目标，因而跳过**群组**这条的次数。
            #: （与 ``pair_deduped`` 只差「谁先到」；两个方向都只是**不发**，不撤回。）
            "pair_blocked": 0,
            #: 目标里**最近已经转发过相同内容**，因而直接跳过的次数。
            "recent_deduped": 0,
            #: 源会话禁止转发、自动改用复制的次数。
            "downgraded": 0,
        }

    # ------------------------------------------------------------------ #
    # 规则热重载（改完配置不用重启账号）
    # ------------------------------------------------------------------ #
    def _resolve_config_file(self) -> Optional[Any]:
        """账号 ``config.json`` 的路径；没给 store/account 时返回 ``None``（关闭热重载）。"""
        if self._store is None or self._account is None:
            return None
        try:
            return self._store.paths.account(self._account).config_file
        except Exception:  # pragma: no cover - 账号名异常时静默关闭热重载
            return None

    def _config_mtime(self) -> Optional[float]:
        if self._config_file is None:
            return None
        try:
            return self._config_file.stat().st_mtime
        except OSError:
            return None

    def _rules_signature(self) -> tuple[Any, ...]:
        """规则指纹。用来识别「mtime 变了但内容其实没变」（例如面板原样保存一次）。"""
        return (
            self.config.forward.enabled,
            tuple(rule.model_dump(mode="json") for rule in self.config.forward.active_rules),
            tuple(str(chat) for chat in self.config.forward.exclude_chats),
        )

    def reload_rules(self) -> bool:
        """重新从磁盘读账号配置，重建规则与 handler 过滤器。返回 True 表示确实变了。

        **为什么 handler 必须一起重建**：``sources`` 决定 handler 的过滤器
        （限定了来源用 ``filters.chat(chats)``，未限定用 ``filters.group | filters.channel``）。
        只换 ``self.rules`` 而不换过滤器的话，把 ``sources`` 从「指定群」改成 ``[]``
        （= 监听全部）之后，**新群的消息根本进不来** —— 规则配得再对也没用。
        """
        if self._store is None or self._account is None:
            return False
        try:
            config = self._store.load_account_config(self._account, create=False)
        except Exception as exc:
            # 配置被写坏（例如面板提交了非法正则）时**保留旧规则继续跑**，
            # 不能因为一次坏写就让转发整个停摆。
            self.alog.warning("规则热重载失败，继续沿用旧规则", error=str(exc))
            return False

        before = self._rules_signature()
        self.config = config
        self.rules = [PreparedRule.build(rule) for rule in config.forward.active_rules]
        self._exclude_chats = RefSet(config.forward.exclude_chats)
        if self._rules_signature() == before:
            return False

        self.alog.info(
            "转发规则已热重载（无需重启）",
            rules=len(self.rules),
            enabled=config.forward.enabled,
            exclude_chats=len(self._exclude_chats.ids) + len(self._exclude_chats.usernames),
            rule_ids=",".join(prepared.id for prepared in self.rules),
        )
        self._refresh_handlers()
        return True

    def _maybe_reload(self) -> None:
        """按 :attr:`RELOAD_CHECK_INTERVAL` 节流检查 mtime，变了才真去读盘。

        全程**没有 await**，所以在事件循环里是原子的 —— 不会出现
        「检查到一半规则被换掉」的中间态。
        """
        if self._config_file is None:
            return
        now = time.monotonic()
        if now - self._last_reload_check < self.RELOAD_CHECK_INTERVAL:
            return
        self._last_reload_check = now
        mtime = self._config_mtime()
        if mtime is None or mtime == self._rules_mtime:
            return
        self._rules_mtime = mtime
        self.reload_rules()

    def _refresh_handlers(self) -> None:
        """注销旧 handler，再按新规则重新注册。

        规则被全部禁用时 ``register()`` 会直接返回 —— 此时 handler 保持已注销状态，
        正是想要的（不监听）。之后重新启用会再次触发重载并重新注册。
        """
        for handler, group in self._handlers:
            with contextlib.suppress(Exception):
                self.client.remove_handler(handler, group)
        self._handlers.clear()
        self.register()

    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        return self.config.forward.enabled and bool(self.rules)

    def watched_chats(self) -> list[ChatRef]:
        """所有规则来源的并集；任一规则未限定来源则返回空（监听全部）。"""
        chats: list[ChatRef] = []
        for prepared in self.rules:
            if not prepared.rule.sources:
                return []
            for source in prepared.rule.sources:
                if source not in chats:
                    chats.append(source)
        return chats

    def register(self) -> None:
        """注册消息处理器。必须在 client 启动前或启动后立即调用。"""
        if not self.enabled:
            self.alog.info("转发功能未启用（无启用的规则）")
            return

        chats = self.watched_chats()
        if chats:
            message_filter: Any = filters.chat(chats)
        else:
            # 未限定来源 = 监听全部群组与频道。
            # 这里刻意用 filters.group | filters.channel 而不是 None：
            # None 会把私聊也收进来，陌生人发条含关键词的私信就会被转发出去。
            message_filter = filters.group | filters.channel
        handler = MessageHandler(self._on_message, message_filter)
        self._handlers.append(self.client.add_handler(handler, group=self.HANDLER_GROUP))

        if any(prepared.rule.include_edited for prepared in self.rules):
            edited = EditedMessageHandler(self._on_edited, message_filter)
            self._handlers.append(self.client.add_handler(edited, group=self.HANDLER_GROUP))

        self.alog.info(
            "转发引擎已注册",
            rules=len(self.rules),
            watched_chats=len(chats) or "全部群组/频道",
            exclude_chats=len(self._exclude_chats.ids) + len(self._exclude_chats.usernames),
            dedupe_window_s=self.config.forward.dedupe_window,
            rule_ids=",".join(prepared.id for prepared in self.rules),
        )
        for prepared in self.rules:
            self.alog.info(
                f"  规则 [{prepared.label}]",
                mode=prepared.rule.mode,
                match_mode=prepared.rule.match.mode,
                patterns=len(prepared.rule.match.patterns),
                sources=len(prepared.rule.sources) or "全部",
                targets=",".join(str(t) for t in prepared.rule.targets),
                notify=prepared.rule.notify,
                delay_s=prepared.rule.delay,
            )

    async def close(self) -> None:
        """注销 handler 并等待在途任务结束。"""
        for handler, group in self._handlers:
            with contextlib.suppress(Exception):
                self.client.remove_handler(handler, group)
        self._handlers.clear()

        for buffer in list(self._media_groups.values()):
            if buffer.task is not None:
                buffer.task.cancel()
        self._media_groups.clear()

        if self._tasks:
            self.alog.info("等待在途转发任务结束", pending=len(self._tasks))
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        self.alog.info("转发引擎已停止", **self.stats)

    # ------------------------------------------------------------------ #
    # handler：只做内存判断，命中后立刻派发
    # ------------------------------------------------------------------ #
    async def _on_message(self, client: Client, message: Any) -> None:
        del client
        self._handle(message, edited=False)

    async def _on_edited(self, client: Client, message: Any) -> None:
        del client
        self._handle(message, edited=True)

    def _handle(self, message: Any, *, edited: bool) -> None:
        started = time.perf_counter()
        # 先看磁盘上的规则有没有变（有节流，不是每条都 stat）。放在最前面，
        # 保证这一条消息就已经用上新规则。
        self._maybe_reload()
        self.stats["received"] += 1
        chat_id, chat_username, chat_title = chat_identity(message)
        message_id = getattr(message, "id", None)
        if chat_id is None or message_id is None:
            return

        is_service = bool(getattr(message, "service", None))
        kind = chat_kind(message)
        sender_id, sender_username, is_self, _is_bot = sender_of(message)
        now = time.monotonic()

        # 账号级全局排除：命中就直接丢弃，连规则都不看。
        # 放在规则循环**外面**是刻意的 —— 它是"所有规则都不监听"的语义，
        # 每条规则各判一次既浪费又容易漏（漏一条就等于没排除）。
        if self._exclude_chats and self._exclude_chats.matches(chat_id, chat_username):
            self.stats["excluded"] += 1
            self.alog.debug(
                "跳过消息",
                reason="会话在 forward.exclude_chats 中",
                chat=chat_title or chat_id,
                message_id=message_id,
            )
            return

        for prepared in self.rules:
            rule = prepared.rule
            if edited and not rule.include_edited:
                continue
            if is_service and not rule.include_service:
                continue

            allowed, reason = prepared.chat_allowed(chat_id, chat_username, kind)
            if not allowed:
                # ⚠️ 这里以前是**裸 continue、零日志零计数** —— 规则配了却不生效时，
                # 线上完全查不出是「群 ID 写错 / 配置没生效 / 消息根本没进来」。
                # 计数会进 snapshot（`status` / 面板），能直接看到「这条规则收到了消息，
                # 但全被会话层拒了」；日志给 debug，因为这条路径在 sources 限定下很常见，
                # INFO 级会刷屏。
                prepared.stats["chat_rejected"] += 1
                self.alog.debug(
                    "跳过消息",
                    rule=prepared.label,
                    reason=reason,
                    chat=chat_title or chat_id,
                    chat_id=chat_id,
                    message_id=message_id,
                )
                continue
            allowed, reason = prepared.sender_allowed(sender_id, sender_username, is_self)
            if not allowed:
                prepared.stats["skipped"] += 1
                self.alog.debug(
                    "跳过消息", rule=prepared.label, reason=reason, chat=chat_title, message_id=message_id
                )
                continue

            result = prepared.matcher.match(message)
            if not result:
                # 安全取预览：消息里可能含非法代理对，直接 str() 会抛 UnicodeEncodeError，
                # 而这里只是打日志，不该因为一条脏消息把整条转发链路带崩。
                try:
                    preview = truncate(str(message_text(message)), 60, "…")
                except Exception:
                    preview = "[无法解析的消息]"

                self.alog.debug(
                    "未命中规则",
                    rule=prepared.label,
                    reason=result.reason,
                    chat=chat_title,
                    message_id=message_id,
                    preview=preview,
                )
                continue

            allowed, reason = prepared.interval_ok(now)
            if not allowed:
                prepared.stats["skipped"] += 1
                self.alog.info("命中但被最小间隔限制", rule=prepared.label, reason=reason)
                continue

            # 账号内去重键必须带上规则 id：同一条消息可以合法地命中多条规则、
            # 转发到不同频道；只用 (chat_id, message_id) 会让第二条规则被误判为重复。
            # ⚠️ **跨账号去重不在这里** —— 它的键要带目标，而一条规则可以有多个目标，
            #    放在这里会「一条规则的多个目标共用一个名额」。见 ``_forward_one``。
            dedupe_key = (prepared.id, chat_id, message_id)
            if not self.dedupe.check_and_add(dedupe_key):
                self.stats["deduped"] += 1
                self.alog.debug(
                    "重复消息已忽略", rule=prepared.label, chat_id=chat_id, message_id=message_id
                )
                continue

            prepared.last_fired = now
            prepared.stats["matched"] += 1
            self.stats["matched"] += 1
            match_ms = (time.perf_counter() - started) * 1000
            try:
                preview = truncate(str(message_text(message)), 80, "…")
            except Exception:
                preview = "[无法解析的消息]"

            self.alog.info(
                "命中转发规则",
                rule=prepared.label,
                chat=chat_title or chat_id,
                message_id=message_id,
                keyword=result.keyword,
                groups=",".join(result.groups) if result.groups else "-",
                match_ms=round(match_ms, 3),
                edited=edited,
                preview=preview,
            )

            group_id = getattr(message, "media_group_id", None)
            if group_id and prepared.rule.media_group:
                self._buffer_media_group(prepared, message, result, str(group_id), chat_id, message_id)
            else:
                self._spawn(self._forward_one(prepared, message, result, started))
            # 一条消息可以命中多条规则，继续循环（不 break）

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------ #
    # 相册聚合
    # ------------------------------------------------------------------ #
    def _buffer_media_group(
        self,
        prepared: PreparedRule,
        message: Any,
        result: MatchResult,
        group_id: str,
        chat_id: int,
        message_id: int,
    ) -> None:
        key = (chat_id, f"{prepared.id}:{group_id}")
        buffer = self._media_groups.get(key)
        if buffer is None:
            buffer = MediaGroupBuffer(
                chat_id=chat_id,
                group_id=group_id,
                rule_id=prepared.id,
                message_ids=[message_id],
                first_message=message,
                match=result,
            )
            self._media_groups[key] = buffer
            buffer.task = asyncio.create_task(self._flush_media_group(prepared, key))
            self._tasks.add(buffer.task)
            buffer.task.add_done_callback(self._tasks.discard)
            self.alog.debug("开始聚合相册", rule=prepared.label, media_group_id=group_id)
        else:
            if message_id not in buffer.message_ids:
                buffer.message_ids.append(message_id)
            self.alog.debug(
                "相册新增一条",
                rule=prepared.label,
                media_group_id=group_id,
                count=len(buffer.message_ids),
            )

    async def _flush_media_group(self, prepared: PreparedRule, key: tuple[int, str]) -> None:
        window = prepared.rule.media_group_window
        # 等待窗口内的后续分片；相册的各条消息几乎同时到达，窗口 1.2s 足够
        await asyncio.sleep(window)
        buffer = self._media_groups.pop(key, None)
        if buffer is None:
            return
        buffer.message_ids.sort()
        self.alog.info(
            "相册聚合完成，开始转发",
            rule=prepared.label,
            media_group_id=buffer.group_id,
            count=len(buffer.message_ids),
            window_s=window,
        )
        await self._forward_one(
            prepared,
            buffer.first_message,
            buffer.match,
            time.perf_counter(),
            message_ids=buffer.message_ids,
        )

    # ------------------------------------------------------------------ #
    # 实际发送
    # ------------------------------------------------------------------ #
    async def _forward_one(
        self,
        prepared: PreparedRule,
        message: Any,
        result: MatchResult,
        started: float,
        message_ids: Optional[Sequence[int]] = None,
    ) -> None:
        rule = prepared.rule
        chat_id, _, chat_title = chat_identity(message)
        ids = list(message_ids) if message_ids else [message.id]
        #: 来源会话类型（``channel`` / ``group`` / ...）与内容指纹 —— 供「频道 ↔ 群组 同内容」
        #: 去重判断用。指纹为 ``None``（既没正文也没媒体）时这一层直接放行。
        kind = chat_kind(message)
        fingerprint = content_fingerprint(message)
        #: 相册 id。非空表示这条属于某个相册（一组消息是一个整体）。
        album_id = getattr(message, "media_group_id", None)

        if rule.delay > 0:
            await asyncio.sleep(rule.delay)

        variables = apply_groups(build_variables(message), result)
        variables["rule"] = rule.label
        variables["rule_id"] = rule.id

        delivered: list[tuple[ChatRef, int]] = []
        #: 去重窗口。账号内 / 跨账号 / 频道↔群组 三层同源（账号级 ``dedupe_window``）。
        dedupe_ttl = float(self.config.forward.dedupe_window)
        for target in rule.targets:
            # 🔴 串行闸门：同一条内容发往同一个目标，同一时刻只允许一个 handler 在跑。
            # 三层去重都是「先判断、后发送」，而发送要 await；判断与落表之间的那个
            # 窗口足以让并发的两条（同账号的频道+群组、或两个账号的同内容消息）
            # **都通过判断**。线上实测 6 个指纹被重复转发，最近的一对只差 0.3 秒。
            # 进闸门后先到的发完并落表，后到的再判断就一定看得见 ⇒ 竞态从根上消失。
            async with self._recent.gate(fingerprint, target):
                # 跨账号去重：同一条源消息发往**同一个目标**，只允许一个账号发出去。
                # 键里带目标 ⇒ 必须放在 target 循环里（放 ``_handle`` 会让多个目标共用名额）。
                # ``claim`` 是同步的、无 await，在事件循环里天然原子 —— 两个账号的 handler
                # 不可能同时抢到同一个名额。不传共享表时整段跳过（单测 / 离线场景）。
                if self._shared_dedupe is not None and not self._shared_dedupe.claim(
                    chat_id, ids[0], target, dedupe_ttl
                ):
                    self.stats["cross_deduped"] += 1
                    self.alog.debug(
                        "跨账号去重：该消息已由其它账号发往同一目标",
                        rule=rule.label,
                        source_chat=chat_title or chat_id,
                        message_id=ids[0],
                        target=target,
                    )
                    continue

                # 频道 ↔ 群组 同内容去重。放在跨账号去重**之后**是刻意的：
                # 顺序反过来的话，一条「因别的账号已发而被跳过」的消息也会在本表留下记录，
                # 于是随后到达的另一条会被当成「已经发过」而拦掉 —— 消息白丢。
                #
                # 🔴 这一层现在**只判断、不撤回**：先到的那条留下，后到的直接不发。
                # 早先的版本是「群组优先」，频道先到时会把已发的频道那条撤回再重发群组那条，
                # 用户明确否掉了那种「先发一条、过一两秒删掉、再补一条」的观感。
                if self._pair_dedupe is not None and fingerprint is not None and not self._pair_dedupe.claim(
                    fingerprint, target, kind, dedupe_ttl
                ):
                    # ⚠️ 名额要退：本账号不发这条，别把跨账号名额也一起占死。
                    self._release_claim(chat_id, ids[0], target)
                    if kind == "channel":
                        self.stats["pair_deduped"] += 1
                        reason = "同内容去重：该内容已由群组发往同一目标，跳过频道这条"
                    else:
                        self.stats["pair_blocked"] += 1
                        reason = "同内容去重：该内容已由频道发往同一目标，跳过群组这条"
                    self.alog.info(
                        reason,
                        rule=rule.label,
                        source_chat=chat_title or chat_id,
                        source_kind=kind,
                        message_id=ids[0],
                        target=target,
                        fingerprint=fingerprint,
                    )
                    continue

                # 「最近已转发过的内容」。
                # ⚠️ 例外：**相册**。同一组里的多条本来就是一个整体，caption 常常一模一样，
                #    按内容比会把整组砍成一条（剩下的图全丢）。整组只在发送后记一次。
                if (
                    album_id is None
                    and self._recent.contains(fingerprint, target)
                ):
                    self.stats["recent_deduped"] += 1
                    # ⚠️ 两层名额都要退，否则这条在该目标上会被占死一整个 TTL。
                    self._release_pair(fingerprint, target)
                    self._release_claim(chat_id, ids[0], target)
                    self.alog.info(
                        "最近已转发过相同内容，跳过",
                        rule=rule.label,
                        source_chat=chat_title or chat_id,
                        message_id=ids[0],
                        target=target,
                        fingerprint=fingerprint,
                    )
                    continue
                try:
                    sent_ids, actual_mode = await self._send_to_target(
                        prepared, message, ids, target, variables
                    )
                except SessionInvalid:
                    # 会话已失效 ⇒ 本账号发不出去了，把名额让给别的账号再试。
                    self._release_pair(fingerprint, target)
                    self._release_claim(chat_id, ids[0], target)
                    raise
                except Exception as exc:
                    prepared.stats["failed"] += 1
                    self.stats["failed"] += 1
                    self.alog.error(
                        "转发失败",
                        rule=rule.label,
                        target=target,
                        source_chat=chat_title or chat_id,
                        message_ids=",".join(map(str, ids)),
                        # 同上：失败也要留指纹，否则「这条内容后来补发成功了吗」串不起来。
                        fingerprint=fingerprint or "-",
                        error=f"{type(exc).__name__}: {exc}",
                        hint=_forward_hint(exc),
                    )
                    # ⚠️ **必须退还名额**：`claim` 是先占位后发送，不退的话这个键会在整个 TTL
                    # （默认 300s）里保持被占 ⇒ 另一个账号也补发不了，这条消息对该目标彻底丢失。
                    self._release_pair(fingerprint, target)
                    self._release_claim(chat_id, ids[0], target)
                    continue

                prepared.stats["sent"] += 1
                self.stats["forwarded"] += 1
                # 记进「最近已转发的内容」—— 之后同样内容的消息直接跳过。
                self._recent.add(fingerprint, target)
                for sent_id in sent_ids:
                    delivered.append((target, sent_id))

                pipeline_ms = _pipeline_ms(message)
                self.alog.info(
                    "转发成功",
                    rule=rule.label,
                    # **实际生效**的模式（不是配置里的 `rule.mode`）：源会话受保护时 forward 会
                    # 降级成复制；复制又失败时还会退成 drop_author 转发（那种情况下**链接是丢的**）。
                    # 小白靠这一行判断「链接有没有加上」，所以这里必须说真话，不能一律写 copy。
                    mode=actual_mode,
                    source_chat=chat_title or chat_id,
                    target=target,
                    message_ids=",".join(map(str, ids)),
                    # 🔴 必须打指纹：否则「同一内容到底有没有被发过两次」从日志里**查不了** ——
                    # 2026-09-22 实测，被去重拦下的那 7 个指纹在日志里只出现在「跳过」行里，
                    # 第一次真正转发的那条完全没有痕迹，等于没法自证去重有没有漏。
                    # 有了它，`grep fingerprint=xxx` 数出 >1 次就是漏了。
                    fingerprint=fingerprint or "-",
                    sent_ids=",".join(map(str, sent_ids)) or "-",
                    handler_ms=round((time.perf_counter() - started) * 1000, 1),
                    pipeline_ms=round(pipeline_ms, 1) if pipeline_ms is not None else "-",
                )

        if rule.notify and self.notifier is not None and delivered:
            self._submit_notify(prepared, variables, delivered)

    def _release_claim(self, source_chat_id: Any, message_id: Any, target: ChatRef) -> None:
        """把跨账号去重名额退回去（发送失败时用）。没配共享表时是空操作。"""
        if self._shared_dedupe is not None:
            self._shared_dedupe.release(source_chat_id, message_id, target)

    def _release_pair(self, fingerprint: Optional[str], target: ChatRef) -> None:
        """把「频道 ↔ 群组 同内容」名额退回去（发送失败时用）。没配表 / 没指纹时空操作。"""
        if self._pair_dedupe is not None and fingerprint is not None:
            self._pair_dedupe.release(fingerprint, target)

    async def _send_to_target(
        self,
        prepared: PreparedRule,
        message: Any,
        ids: list[int],
        target: ChatRef,
        variables: dict[str, Any],
    ) -> tuple[list[int], str]:
        """把消息发到 ``target``。

        返回 ``(已发送的消息 id, **实际生效的模式**)``。

        模式取值（就是日志里 ``转发成功 ... mode=`` 打出来的那个）：
        ``forward`` / ``copy`` / ``text`` —— 与配置一致；
        ``copy(降级)`` —— ``forward`` 撞上受保护源会话，已改用复制（**链接在正文里**）；
        ``copy(退化为转发·丢链接)`` —— ``copy`` 失败，退成 ``drop_author`` 转发；
        ``forward(降级·丢链接)`` —— 降级去复制，复制也失败，又退成 ``drop_author`` 转发。

        🔴 后两种带「丢链接」字样的，说明**发出去的是转发消息、正文里没有原文链接**
        （唯一的线索是同时打了一条 WARNING）。日志必须如实反映，否则会误导排查
        —— 2026-09-20 之前这里一律写 ``copy``，属于「日志撒谎」。
        """
        rule = prepared.rule
        chat_id, _, chat_title = chat_identity(message)
        kwargs: dict[str, Any] = {}
        if rule.target_thread_id is not None:
            kwargs["message_thread_id"] = rule.target_thread_id
        if rule.silent:
            kwargs["disable_notification"] = True

        #: 原文链接（``build_variables`` 给的 t.me 永久链接）。
        link = str(variables.get("link") or "")
        #: 要不要附带原文链接。两种模式的**加法不同**：
        #: ``copy`` 写进**这条消息**的正文末尾（它是重发出来的新消息，正文可写）；
        #: ``forward`` 在**下方**单独补一条 —— 转发消息本身不可编辑，也不该动。
        want_link = bool(rule.include_source_link and link)

        def _with_link(base: str, limit: int) -> tuple[str, bool]:
            """把「🔗原文链接：…」接到正文 / caption 末尾，并保证总长不超 ``limit``。

            返回 ``(拼好的文本, 原文是否被**完整**保留)`` —— 第二项决定能不能沿用原来的
            ``entities``（截断会切断实体边界 ⇒ Telegram 回 ``ENTITY_BOUNDS_INVALID``）。

            🔴 **必须先给链接留位置、再截断原文**。顺序反了（先拼再截断）的话：
            :func:`~tg_assistant.matching.truncate` 是**从末尾砍掉**再补「…（已截断）」，
            而链接正好拼在末尾 ⇒ 消息一长，用户最想要的那行反而被吃掉，
            而且**日志一切正常**（静默丢）。2026-09-20 审出来的边界 bug。

            ⚠️ ``CAPTION_LIMIT`` 只有 **1024**，比正文更容易撞到 —— 资源帖的长 caption
            正是这条规则匹配的东西，所以这不是理论问题。
            """
            if not want_link or link in base:
                return truncate(base, limit), len(base) <= limit

            # 正文为空时不加前导空行（否则 caption 会以两个换行开头，很难看）
            block = f"\n\n{SOURCE_LINK_PREFIX}{link}" if base else f"{SOURCE_LINK_PREFIX}{link}"
            room = limit - len(block)
            if room <= 0:
                # 链接本身就长到放不下（t.me 链接不会这么长，纯属兜底）：
                # 保链接、牺牲原文 —— 链接是用户明确要的那部分。
                return truncate(block.lstrip("\n"), limit), False
            return truncate(base, room) + block, len(base) <= room

        async def _server_forward(hide_sender: bool) -> list[int]:
            """服务端转发。``hide_sender=True``（= raw ``drop_author``）去掉「转发自」抬头。

            单次 RPC 完成：天然保留相册分组与媒体，不需要下载再上传。
            """
            sent = await self.client.forward_messages(
                chat_id=target,
                from_chat_id=chat_id,
                message_ids=ids if len(ids) > 1 else ids[0],
                hide_sender_name=True if hide_sender else None,
                **kwargs,
            )
            return _sent_ids(sent)

        async def _server_copy() -> list[int]:
            """**真正的复制**：按 ``file_id`` 重新发送一条**新消息**。

            ⚠️ 不能拿 ``forward_messages(hide_sender_name=True)`` 当复制用。那样发出来的是
            **转发消息**，而 Telegram **拒绝编辑转发消息**（2026-09-20 实测
            ``400 Bad Request: message can't be edited``）⇒ 事后没法把原文链接追加进去。
            「重新发送」拿到的是全新消息，链接在**发送时**就写进正文 / caption ——
            一次 RPC 搞定，零编辑。

            - 文本：``send_message``（带上原 entities，保留粗体 / 链接等格式）
            - 媒体：``Message.copy``（内部 ``send_cached_media``，服务端按 file_id 复用，
              不下载不上传）
            - 相册：``copy_media_group``（服务端 ``SendMultiMedia``）。``captions`` 传 list 时
              **按下标取值、越界回落原 caption** ⇒ 只覆盖第一项就能给相册首图加链接
            """
            if len(ids) > 1:
                captions = None
                if want_link:
                    # 只覆盖第一项（pyrogram 按下标取值、越界回落原 caption）
                    captions = [_with_link(getattr(message, "caption", None) or "", CAPTION_LIMIT)[0]]
                sent = await self.client.copy_media_group(
                    chat_id=target,
                    from_chat_id=chat_id,
                    message_id=ids[0],
                    captions=captions,
                    **kwargs,
                )
                return _sent_ids(sent)

            text = getattr(message, "text", None)
            if text is not None:
                body, intact = _with_link(text, MAX_TEXT_LENGTH)
                if not body.strip():
                    body = "（原消息无文本内容）"
                    intact = False
                sent = await self.client.send_message(
                    chat_id=target,
                    text=body,
                    # 截断（或退化成占位文案）会切断实体边界 ⇒ Telegram 回
                    # ENTITY_BOUNDS_INVALID，这种情况就丢掉 entities
                    # （宁可少点格式，也不能发不出去）。
                    # ⚠️ 只在**原文被完整保留**时才沿用：单纯在末尾追加链接不影响前面
                    # 实体的 offsets，所以「加了链接但没截断」仍然可以带 entities。
                    entities=getattr(message, "entities", None) if intact else None,
                    parse_mode=ParseMode.DISABLED,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    **kwargs,
                )
                return _sent_ids(sent)

            sent = await message.copy(
                chat_id=target,
                caption=_with_link(getattr(message, "caption", None) or "", CAPTION_LIMIT)[0],
                **kwargs,
            )
            return _sent_ids(sent)

        async def _forward_link_note() -> list[int]:
            """``forward`` 模式：**不动**那条转发消息，在它**下方**补发一条独立消息。

            为什么不能把链接写进转发消息本身：Telegram **拒绝编辑转发消息**
            （实测 ``400 Bad Request: message can't be edited``）。所以 forward 模式
            只能单独发一条 —— 这也正是小白要的格式：
            「转发的消息不用编辑，直接在下方加一条原文链接」。

            刻意**不抛出**：内容已经发出去了，链接没补上不该让整条转发记成失败。
            """
            try:
                sent = await with_flood_retry(
                    lambda: self.client.send_message(
                        chat_id=target,
                        text=f"{SOURCE_LINK_PREFIX}{link}",
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                        **kwargs,
                    ),
                    alog=self.alog,
                    action=f"补发原文链接到 {target}",
                    retries=2,
                    max_flood_wait=60.0,
                )
            except SessionInvalid:
                raise
            except Exception as exc:  # noqa: BLE001
                self.alog.warning(
                    "补发原文链接失败",
                    rule=rule.label,
                    target=target,
                    error=f"{type(exc).__name__}: {exc}",
                )
                return []
            return _sent_ids(sent)

        async def _copy_with_fallback() -> tuple[list[int], bool]:
            """复制；复制失败就退化成 ``drop_author`` 转发（内容能到，但**丢原文链接**）。

            返回 ``(已发送的消息 id, 是否**真的复制成功**)``。

            ⚠️ 第二项**必须**往上传：退化成 drop_author 转发时，发出来的是**转发消息**、
            正文里没有链接。调用方若一律按 ``copy`` 记日志，就会出现
            「日志说 copy，实际链接丢了」—— 而小白正是靠这条日志判断链接有没有加上。
            这里返回真实结果，由调用方如实报告（2026-09-20 修）。
            """
            try:
                sent = await with_flood_retry(
                    _server_copy,
                    alog=self.alog,
                    action=f"复制到 {target}",
                    retries=2,
                    max_flood_wait=60.0,
                )
            except SessionInvalid:
                raise
            except Exception as exc:  # noqa: BLE001
                self.alog.warning(
                    "复制失败，退化为不带抬头的转发（原文链接会丢失）",
                    rule=rule.label,
                    target=target,
                    source_chat=chat_title or chat_id,
                    message_ids=",".join(map(str, ids)),
                    error=f"{type(exc).__name__}: {exc}",
                )
                sent = await with_flood_retry(
                    lambda: _server_forward(True),
                    alog=self.alog,
                    action=f"转发到 {target}",
                    retries=2,
                    max_flood_wait=60.0,
                )
                # ⚠️ **不要**再套一层 ``_sent_ids``：``_server_copy`` 与 ``_server_forward``
                # **本身就已经返回 ``list[int]``**（它们内部各自提取过一次）。
                # 对整数列表再取一次 ``.id`` 会得到**空列表** —— 2026-09-24 实测：
                # ``sent_ids`` 恒为空 ⇒ ``delivered`` 也空 ⇒ 通知被**静默跳过**
                # （2026-09-24 就是这么发现并修掉的）。
                return sent, False
            return sent, True

        try:
            if rule.mode == "forward":
                sent = await with_flood_retry(
                    lambda: _server_forward(False),
                    alog=self.alog,
                    action=f"转发到 {target}",
                    retries=2,
                    max_flood_wait=60.0,
                )
                # 转发消息本身**不编辑**（Telegram 也拒绝编辑转发消息）——
                # 链接在它下方单独补一条。
                if want_link:
                    sent = [*sent, *await _forward_link_note()]
                return sent, "forward"

            if rule.mode == "copy":
                sent, copied = await _copy_with_fallback()
                # ⚠️ 不能一律写 "copy" —— 退化那条是 drop_author 转发，正文里**没有链接**。
                return sent, "copy" if copied else "copy(退化为转发·丢链接)"

            # text 模式：按模板重发纯文本
            text = render_template(rule.template or "{text}", variables)
            if want_link and link not in text:
                # ⚠️ 同样**先给链接留位置再截断**，否则长消息会把链接砍掉
                # （truncate 的默认上限是 SAFE_TEXT_LENGTH=3800）。
                block = f"\n\n{SOURCE_LINK_PREFIX}{link}"
                room = SAFE_TEXT_LENGTH - len(block)
                if room <= 0:
                    text = truncate(block.lstrip("\n"))
                else:
                    text = truncate(text, room) + block
            else:
                text = truncate(text)
            if not text.strip():
                text = "（原消息无文本内容）"
            sent = await with_flood_retry(
                lambda: self.client.send_message(
                    chat_id=target,
                    text=text,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    **kwargs,
                ),
                alog=self.alog,
                action=f"发送到 {target}",
                retries=2,
                max_flood_wait=60.0,
            )
            return _sent_ids(sent), "text"
        except SessionInvalid:
            raise
        except Exception as exc:
            # 源会话禁止转发（受保护群/频道）⇒ 自动降级为复制。
            # Telegram 对受保护内容回 400 CHAT_FORWARDS_RESTRICTED，而「复制」是允许的
            # —— 这是唯一能靠换模式绕过去的错误，所以单独识别。
            # 代价：该源会话的每条消息都要先撞一次失败。受保护与否是**源会话的属性**，
            # 按 (规则, 源会话) 记忆能省掉这次 RPC，但源会话可能取消保护，
            # 所以这里选择每次都真试一遍 —— 正确性优先。
            if rule.mode != "forward" or not _is_forwards_restricted(exc):
                raise
            self.alog.warning(
                "源会话禁止转发，自动降级为复制",
                rule=rule.label,
                target=target,
                source_chat=chat_title or chat_id,
                message_ids=",".join(map(str, ids)),
                error=f"{type(exc).__name__}: {exc}",
            )
            sent, copied = await _copy_with_fallback()
            # 计数口径：走到降级分支就 +1（哪怕复制本身又退了）。真实结果看日志的 mode。
            self.stats["downgraded"] += 1
            return sent, "copy(降级)" if copied else "forward(降级·丢链接)"

    def _submit_notify(
        self,
        prepared: PreparedRule,
        variables: dict[str, Any],
        delivered: list[tuple[ChatRef, int]],
        key: Optional[str] = None,
    ) -> None:
        assert self.notifier is not None
        config = self.notifier.config
        text = build_notify_text(config, variables)
        task = NotifyTask(
            event="forward",
            text=text,
            copy_from=delivered[0],
            copy_from_ids=delivered if len(delivered) > 1 else [],
            silent=config.silent,
            context={
                "rule": prepared.label,
                "source_chat": variables.get("chat_title"),
            },
            key=key,
        )
        if self.notifier.submit(task):
            self.alog.debug("已提交 bot 通知", rule=prepared.label, copy_from=str(delivered[0]))

    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict[str, Any]:
        """运行状态快照，供 ``status`` 命令与周期性日志使用。"""
        data: dict[str, Any] = {
            **self.stats,
            "dedupe_size": len(self.dedupe),
            "pending_tasks": len(self._tasks),
            "media_group_buffers": len(self._media_groups),
            "rules": {prepared.id: dict(prepared.stats) for prepared in self.rules},
        }
        if self._shared_dedupe is not None:
            # ⚠️ 这是**跨账号共享表**的数字（所有账号合计），不是本账号的。
            data["cross_dedupe"] = self._shared_dedupe.snapshot()
        if self._pair_dedupe is not None:
            #: 同样是跨账号共享表（「频道 ↔ 群组 同内容」那一层）。
            data["pair_dedupe"] = self._pair_dedupe.snapshot()
        if self._recent is not None:
            #: 同样是跨账号共享表（「最近已转发的内容」那一层）。
            data["recent_dedupe"] = self._recent.snapshot()
        return data


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
#: 「原文链接」那一行的前缀。``copy`` 模式发出来的是**新消息**（不带「转发自」抬头），
#: Telegram 不会附带任何回溯入口，所以只能自己把链接写进正文。
SOURCE_LINK_PREFIX = "🔗原文链接："

#: 媒体 caption 的硬上限（Telegram 限制），比正文的 4096 短得多。
CAPTION_LIMIT = 1024


def _sent_ids(sent: Any) -> list[int]:
    """从 pyrogram 的返回值里取出「发出去生成了哪些消息 id」。

    对**已经是 id 列表**的输入**幂等透传**：上游 helper（``_server_copy`` /
    ``_server_forward`` / ``_forward_link_note``）返回的就是 ``list[int]``，
    再取一次 ``.id`` 会得到**空列表** —— 2026-09-24 那个 bug 正是这么发生的
    （``sent_ids`` 静默变空 ⇒ 通知不推送 + 频道那条撤不掉）。幂等化之后，
    这类「二次提取」不会再悄悄把 id 吞掉。
    """
    if sent is None:
        return []
    if isinstance(sent, (list, tuple)):
        # 空列表 ``all(...)`` 为真 ⇒ 返回 ``[]``，语义一致。
        # 排除 ``bool``：``isinstance(True, int)`` 为真，但布尔不是消息 id。
        if all(isinstance(item, int) and not isinstance(item, bool) for item in sent):
            return list(sent)
        return [item.id for item in sent if getattr(item, "id", None) is not None]
    message_id = getattr(sent, "id", None)
    return [message_id] if message_id is not None else []


def _pipeline_ms(message: Any) -> Optional[float]:
    """消息在 Telegram 服务端的时间戳到现在的耗时（含网络传输）。"""
    import datetime as _dt

    date = getattr(message, "date", None)
    if not isinstance(date, _dt.datetime):
        return None
    if date.tzinfo is None:
        # ⚠️ **不能当 UTC**。pyrogram 的 ``utils.timestamp_to_datetime`` 是
        # ``datetime.fromtimestamp(ts)`` ⇒ 拿到的是 **naive 本地时间**（服务器 TZ = CST）。
        # 这里原先写的是 ``date.replace(tzinfo=timezone.utc)``，于是 pipeline_ms 恒为
        # **-8 小时**（2026-09-20 线上实测 -28799461.9ms，189 条）。
        # ``astimezone()`` 会把 naive 时间**按本地时区**解释并补上 tzinfo，这才是对的。
        date = date.astimezone()
    # 时钟偏差（消息时间戳略超前于本机）仍可能算出一丁点负值；「负耗时」没有意义，兜底 0。
    return max(0.0, (_dt.datetime.now(_dt.timezone.utc) - date).total_seconds() * 1000)


def _is_forwards_restricted(exc: Exception) -> bool:
    """是不是「源会话禁止转发」（受保护群 / 频道）。

    这是**唯一**能靠「改用复制」绕过去的错误：它说明**源**会话受保护，而复制是允许的，
    所以 ``mode="forward"`` 的规则遇到它要自动降级。

    以异常类型为主、错误文本兜底 —— pyrogram 各版本类名一致，但代理层或包装层
    可能把它换成别的类型，而文本里一定带 ``CHAT_FORWARDS_RESTRICTED``。
    """
    if isinstance(exc, ChatForwardsRestricted):
        return True
    return "forwards_restricted" in f"{type(exc).__name__}: {exc}".lower()


def _forward_hint(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    if "peer_id_invalid" in text or "peer id invalid" in text:
        return "目标会话未在本账号的 peer 缓存中：先让该账号加入/打开一次目标频道，或改用数字 chat_id"
    if "chat_write_forbidden" in text:
        return "本账号在目标会话没有发言权限"
    if "chat_admin_required" in text:
        return "需要管理员权限才能发送"
    if "channel_private" in text:
        return "目标频道私有且本账号未加入"
    if "forwards_restricted" in text or ("forbidden" in text and "forward" in text):
        return (
            "源会话是受保护内容，Telegram 不允许转发；已自动改用复制仍失败 ⇒ "
            "把规则改成 mode=\"copy\" 再试，或换一个源会话"
        )
    if "media_empty" in text:
        return "媒体已失效，可能源消息被删除"
    if "slowmode" in text:
        return "目标会话开启了慢速模式，适当增大 min_interval"
    return "查看上面的错误类型定位原因"


def random_jitter(base: float, jitter: float) -> float:
    if jitter <= 0:
        return base
    return base + random.uniform(0, jitter)


__all__ = [
    "CAPTION_LIMIT",
    "SOURCE_LINK_PREFIX",
    "ChannelGroupDedupe",
    "CrossAccountDedupe",
    "DedupeCache",
    "ForwardEngine",
    "MediaGroupBuffer",
    "PreparedRule",
    "RecentContentDedupe",
    "content_fingerprint",
    "random_jitter",
]
