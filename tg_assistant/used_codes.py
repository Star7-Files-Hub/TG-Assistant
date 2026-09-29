"""「已被用掉的注册码」的记录与比对。

这些 Emby 码频道会在码被用掉后发一条**使用通知**：

    🎟️ 注册码使用 - jf [7002057019] 使用了 MSKY-30-Register_f1t░░░░░░░

码尾被遮罩（``░``），只露出前几位。所以「防住通知本身」是不够的：真正会
漏掉的是**后面又出现的那条完整码**（猜码谜底、或被谁重发一遍）。
必须把通知里露出的那截前缀记下来，之后再看到同一个码就不转发。

🔴 **为什么单独一个模块**：抢注引擎（``reg_grab``）和转发引擎（``forwarder``）
都要用同一套「从通知里认出码、再按前缀比对」的口径。两边各写一份的话，只要
有一边的切法不一样（比如一边留 ``Register_`` 前缀、一边不留），就**永远比不上**，
而且是静默比不上 —— 线上看不到任何报错，只表现为「拦不住」。所以口径放在这里，
两边共用。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from .matching import compile_user_pattern

__all__ = ["UsedCodeStore", "code_value_of", "visible_code_part", "visible_value_of"]

#: 遮罩字符：使用通知里的码尾部会被 ``░▒▓*•`` 之类盖掉。
#: 只剥**结尾**那一段（遮罩都在尾部），不碰中间 —— 免得把
#: ``..._f1t░░░abc`` 这种奇怪格式硬拼成一个不存在的码。
#:
#: ⚠️ 这个正则从 ``reg_grab`` 原样搬过来共用，语义一个字都不能改：抢注和转发
#: 必须按同一口径切，否则两边记录的前缀永远比不上（而且是静默比不上）。
_MASK_TAIL = re.compile(r"[^A-Za-z0-9]+$")

#: 可见 token 里**最后一段连续字母数字** —— 也就是通知真正露出来的那几位。
_TRAILING_RUN = re.compile(r"([A-Za-z0-9]+)$")


#: 可见 token 里必须出现这个字符之一，否则**不当它是「码」**。
#:
#: 🔴 线上取证（2026-09-29 15:36，云海Emby 交流群）：那个群的「使用通知」写法不同，
#: 结果学到了一个**裸词** ``emby``（原文大概是「…使用了 emby吧」，中文尾巴被遮罩
#: 规则剥掉后只剩 emby）。它只有 4 个字母、没有任何结构，而 :meth:`UsedCodeStore.is_used`
#: 是**子串**比对 —— 存进去之后**任何**提到 emby 的消息都会被拦下来，比如抽奖帖里的
#: 「🎫 加入-云海Emby 交流群」。那是**静默丢消息**，比漏拦一条废码严重得多。
#:
#: 真实码的形状都带分隔符（``ChaPanda-30-Register_Ayqx``、``8643208201-atjx``、
#: ``EMBY-ABC123``），所以「必须能分出码名和码值两部分」是既安全又不误伤的判据。
_CODE_SEPARATORS = "-_"


def visible_code_part(token: str) -> str:
    """从使用通知里那个被遮罩的码中取出**可见部分**。

    ``MSKY-30-Register_f1t░░░░░░░`` → ``MSKY-30-Register_f1t``

    没有遮罩时原样返回（通知偶尔会印完整码）。
    """
    return _MASK_TAIL.sub("", (token or "").strip())


def code_value_of(token: str) -> str:
    """取码的**值**部分 —— 最后一个 ``_`` 之后那一段（统一小写）。

    两边必须按同一口径切，否则永远比不上：

    * 通知里是 ``MSKY-30-Register_f1t``，值是 ``f1t``；
    * 配置里的 ``code_pattern`` 通常只抓 ``f1tAbCdEfGh``，值就是它本身；
      没写捕获组时抓到 ``Register_f1tAbCdEfGh``，切完同样是 ``f1tAbCdEfGh``。

    这个口径假设「码的值里不含 ``_``」（``MSKY-30-Register_<10位字母数字>``
    这种格式成立）。若某天码里真的带下划线，``used_pattern`` 换成能直接圈出
    值部分的正则即可。

    ⚠️ 转发侧**不要**用这个函数去和消息正文比对，用 :func:`visible_value_of` ——
    线上存在没有下划线的码形状，见该函数的说明。
    """
    return (token or "").rsplit("_", 1)[-1].strip().lower()


def visible_value_of(token: str) -> str:
    """可见 token 里**真正有区分度**的那一截：最后一段连续字母数字（小写）。

    线上实际收到的两种通知形状（2026-09-26 取证）都要能对：

    * ``1876596720-G0Ky``          → ``g0ky``  —— ``<用户id>-<可见值>``
    * ``ChaPanda-30-Register_Ayq`` → ``ayq``   —— ``<名字>-<天数>-Register_<可见值>``

    🔴 **为什么不能沿用 ``code_value_of``**：它按最后一个 ``_`` 切。
    第一种形状**没有下划线**，切完会得到整个 token（``1876596720-g0ky``），
    拿去和真码 ``1876596720-G0KyABCDEF`` 比就永远比不上 —— 而这个形状在
    ``茶百道`` / ``茶包影视`` 里是**主力**。换一个思路：不管前面是什么，
    通知露出来的永远是**结尾那几位连续字母数字**。

    这一截同时也是「可见位数够不够」的判据：``ChaPanda-30-Register_`` 这截
    名字对所有码都一样、没有区分度，真正区分码的只有 ``Ayq`` 这几位。
    """
    found = _TRAILING_RUN.search(token or "")
    return found.group(1).lower() if found else ""


class UsedCodeStore:
    """记住「哪些码已经被用掉了」，供转发前比对。

    存的是使用通知里**可见的那个 token**（如 ``chapanda-30-register_ayq``），
    比对方式就是拿它到正文里找子串。这样对线上两种码形状都成立，见
    :func:`visible_value_of`；而且比只存「值」更精确（多带了名字部分），
    误伤好码的概率更低。

    ⚠️ 覆盖不到的形状：如果码在别处是以**另一种写法**出现的（例如
    ``https://xx/register/2cIExxxx`` 这种 URL 形式），通知里的 token 就不会
    出现在正文里，这条拦不住 —— 只会漏拦，不会误拦。真遇上了再加一条
    「按值比对」的兜底规则即可。
    """

    #: 落盘格式版本。以后改结构时可以据此决定要不要读旧文件。
    VERSION = 1

    def __init__(self, *, state_path: Optional[Any] = None) -> None:
        #: 可见 token（小写）-> 记下来的时刻（**墙上时间**）。用 ``time.time()``
        #: 而不是 monotonic 是为了落盘：重启后 monotonic 归零，读回来的时间戳
        #: 会全部「来自未来」。
        self._entries: dict[str, float] = {}
        self.state_path = Path(state_path) if state_path is not None else None
        #: 策略（由 ``configure`` 从账号配置灌进来）
        self.enabled = True
        self.min_visible = 3
        self.ttl = 3600.0
        self.persist = True
        self._keywords: tuple[str, ...] = ()
        self._pattern: Optional[re.Pattern[str]] = None
        self._raw_pattern = ""
        self._ignore: Optional[re.Pattern[str]] = None
        self._raw_ignore = ""
        #: 累计学到多少条（去重后的新增条数），进快照给面板看。
        self.learned = 0
        #: 启动时从磁盘恢复了多少条。
        self.restored = 0

    # ------------------------------------------------------------------ #
    # 策略
    # ------------------------------------------------------------------ #
    def configure(
        self,
        *,
        enabled: bool,
        keywords: Iterable[str],
        pattern: str,
        min_visible: int,
        ttl: float,
        persist: bool,
        state_path: Optional[Any] = None,
        ignore_pattern: str = "",
    ) -> None:
        """把账号配置灌进来（热重载时会再调一次）。

        正则编译失败时**不抛异常**：配置层已经校验过一遍，这里再抛只会让
        「改错一个字 → 整个转发引擎起不来」。留空即等于这个功能不生效。
        """
        self.enabled = bool(enabled)
        self.min_visible = max(1, int(min_visible))
        self.ttl = float(ttl)
        self.persist = bool(persist)
        self._keywords = tuple(str(item) for item in keywords if str(item).strip())
        if state_path is not None:
            self.state_path = Path(state_path)
        # 正则只在**源码变了**时重编译（热重载每秒都可能调一次，不该每次重编译）。
        if str(pattern or "") != self._raw_pattern:
            self._raw_pattern = str(pattern or "")
            self._pattern = self._compile(self._raw_pattern)
        if str(ignore_pattern or "") != self._raw_ignore:
            self._raw_ignore = str(ignore_pattern or "")
            self._ignore = self._compile(self._raw_ignore)
        # 持久化开关关了就不碰磁盘。
        if not (self.persist and self.state_path is not None):
            return
        # 先把磁盘上的记忆读回来（只在第一次），**再**按当前策略清一遍：
        # 🔴 顺序不能反 —— ``_load()`` 在这里才发生，读盘之前记忆是空的，
        # 清扫会直接空跑，形状A条目就留下来了（2026-09-29 单测抓到过这一版）。
        if not self._entries and self.restored == 0:
            self._load()
        self._drop_unwanted()

    @staticmethod
    def _compile(raw: str) -> Optional[re.Pattern[str]]:
        """编译一条**用户写的**正则；编译失败返回 ``None``（等于这条不生效）。

        这里**不抛**异常：配置层已经校验过一遍，再抛只会让「改错一个字 →
        整个转发引擎起不来」。用 ``compile_user_pattern`` 是为了和项目里其它
        用户正则带上同一套 flags（``re.MULTILINE``）—— 用裸 ``re.compile`` 的话，
        用户写了 ``^``/``$`` 的正则在引擎里和面板校验时的行为会不一致，
        而校验只判能不能编译，看不到这种分歧。
        """
        if not raw:
            return None
        try:
            return compile_user_pattern(raw)
        except re.error:
            return None

    def _keep(self, visible: str) -> bool:
        """这条可见 token 该不该记？—— **学、读盘、清扫三处共用同一个判据**。

        两关缺一不可：

        1. **不在「忽略形状」里** —— 线上有两种码形状，但只有一种真会被转发，
           把不会被转发的也学进去只会白占记忆、还可能误伤，见 ``ForwardUsedCodes``；
        2. **本身像个码** —— 见 :meth:`_plausible`。
        """
        if self._ignore is not None and self._ignore.search(visible):
            return False
        return self._plausible(visible)

    def _drop_unwanted(self) -> None:
        """把按**当前**策略不该记的条目清掉，并写回磁盘。"""
        if not self._entries:
            return
        before = len(self._entries)
        self._entries = {key: seen for key, seen in self._entries.items() if self._keep(key)}
        if len(self._entries) != before:
            self._save()

    def _plausible(self, visible: str) -> bool:
        """这截可见 token 像不像一个「码」？（学的时候和读盘的时候都要过这一关）

        两个条件缺一不可：

        1. **能分出码名和码值**（含 ``-`` 或 ``_``）—— 挡掉 ``emby`` 这种裸词，
           理由见 :data:`_CODE_SEPARATORS`；
        2. **可见值够长**（``>= min_visible``）—— 只露一两位时几乎任何码都能碰巧
           对上。判据要用**真正有区分度的那几位**（``visible_value_of``），不能拿
           整个 token 算长度：``ChaPanda-30-Register_`` 这截名字对所有码都一样，
           拿它算等于没判。
        """
        if not any(sep in visible for sep in _CODE_SEPARATORS):
            return False
        return len(visible_value_of(visible)) >= self.min_visible

    # ------------------------------------------------------------------ #
    # 学
    # ------------------------------------------------------------------ #
    def learn(self, text: str) -> list[str]:
        """从一条消息里认「使用通知」，把露出的码前缀记下来。

        返回**这次新记下的**前缀（已经记过的返回空），供调用方打日志 / 计数。
        """
        if not self.enabled or not text or self._pattern is None:
            return []
        if self._keywords:
            low = text.lower()
            if not any(word.lower() in low for word in self._keywords):
                return []

        now = time.time()
        fresh: list[str] = []
        for found in self._pattern.finditer(text):
            token = next((group for group in found.groups() if group), None) or found.group(0)
            visible = visible_code_part(token)
            if not self._keep(visible):
                # 不是「码」的形状（裸词 ``emby``）、或者压根不会被转发的形状
                # （``7017826500-2cIE``）、或者揭示的位数太少 —— 宁可不记。
                continue
            key = visible.lower()
            if key in self._entries:
                self._entries[key] = now  # 续期
                continue
            self._entries[key] = now
            fresh.append(key)
        if fresh:
            self.learned += len(fresh)
            self._save()
        return fresh

    # ------------------------------------------------------------------ #
    # 用
    # ------------------------------------------------------------------ #
    def is_used(self, text: str) -> Optional[str]:
        """这条内容里的码是不是已经被用掉了？是就返回命中的那个 token。"""
        if not self.enabled or not self._entries or not text:
            return None
        self._prune()
        low = text.lower()
        for token in self._entries:
            if token in low:
                return token
        return None

    def _prune(self) -> None:
        """丢掉过期的。ttl<=0 表示**永不过期**（与去重表的口径一致）。"""
        if self.ttl <= 0 or not self._entries:
            return
        deadline = time.time() - self.ttl
        for prefix in [k for k, ts in self._entries.items() if ts < deadline]:
            self._entries.pop(prefix, None)

    # ------------------------------------------------------------------ #
    # 落盘
    # ------------------------------------------------------------------ #
    # 🔴 为什么要落盘：码被用掉是**永久事实**，而这张表原来只在内存里 ——
    #    一次重启就忘光，重启后同一条已经用掉的码又能被转发出去。
    #    这与 ``RecentContentDedupe`` 落盘是同一个理由，写法也保持一致。
    def _load(self) -> None:
        """读回磁盘上的记录。任何异常都只当"没有记录"，绝不阻塞启动。"""
        if self.state_path is None:
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception:
            # 文件被写坏时**不能**让转发起不来：最坏只是"重启后可能漏拦一条"。
            return
        if not isinstance(raw, dict):
            return
        entries = raw.get("entries")
        if not isinstance(entries, dict):
            return
        now = time.time()
        dropped = 0
        for prefix, ts in entries.items():
            try:
                stamp = float(ts)
            except (TypeError, ValueError):
                dropped += 1
                continue
            key = str(prefix).strip().lower()
            if not key:
                continue
            # 🔴 这里也要过一遍「像不像一个码」：老版本存进去的**垃圾条目**（比如裸词
            # ``emby``）必须能在重启时被清掉，否则它会一直留在磁盘上，把任何提到
            # emby 的消息都拦下来 —— 那是静默丢消息。
            # ⚠️ 只能判「像不像码」，「忽略形状」判不了 —— 那是账号配置，本方法跑在
            # ``configure()`` 之前。形状过滤由 ``configure`` 里的 ``_drop_unwanted`` 补。
            if not self._plausible(key):
                dropped += 1
                continue
            if self.ttl > 0 and (now - stamp) > self.ttl:
                dropped += 1
                continue  # 已经过期的别读回来，白占内存
            self._entries[key] = stamp
        self.restored = len(self._entries)
        if self.restored:
            self.learned += self.restored
        # 清掉了东西就顺手写回：否则垃圾会一直躺在磁盘上，每次重启都要重清一遍，
        # 人工去看那份文件也分不清"这条还算不算数"。
        if dropped:
            self._save()

    def _save(self) -> None:
        """原子写回。失败只吞掉 —— 转发永远不能被日志盘拖垮。"""
        if not self.persist or self.state_path is None:
            return
        try:
            payload = {
                "version": self.VERSION,
                "entries": self._entries,
            }
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(self.state_path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except Exception:
            return

    # ------------------------------------------------------------------ #
    @property
    def known(self) -> int:
        """当前记住多少条（过期的不算）。"""
        self._prune()
        return len(self._entries)

    def snapshot(self) -> dict[str, Any]:
        self._prune()
        return {
            "enabled": self.enabled,
            "known": len(self._entries),
            "learned": self.learned,
            "restored": self.restored,
            "persisted": bool(self.persist and self.state_path is not None),
            "min_visible": self.min_visible,
            "ttl": self.ttl,
        }
