"""数据目录布局与路径解析。

每个账号的数据完全隔离在 ``<data_dir>/accounts/<account>/`` 之下：

    data/
    ├── accounts.json                 # 账号注册表（名称/user_id/代理/启用状态）
    ├── accounts/
    │   └── <account>/
    │       ├── <account>.session     # Pyrogram 会话文件（账号独立）
    │       ├── config.json           # 该账号的转发/红包/通知配置（账号独立）
    │       └── state.json            # 运行期状态（去重游标等，可随时删除）
    └── logs/
        ├── tg-assistant.log          # 全量日志
        ├── error.log                 # ERROR 及以上
        ├── events.jsonl              # 结构化事件日志（便于 debug/统计）
        └── accounts/<account>.log    # 单账号日志
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

#: 账号名允许的字符：Unicode 字母/数字/下划线打头，其后可含 ``.`` ``-`` 与空格。
#:
#: 刻意允许非 ASCII（中文 / 日文 / 韩文）：部署版一直支持，而仓库初版只允许
#: ``[A-Za-z0-9]``。两边不一致的后果不只是"少个功能" ——
#: :meth:`Paths.iter_account_names` 对校验不过的目录名是**静默跳过**的，
#: 所以服务器上建的中文账号名在仓库版里会直接"消失"。
#:
#: 安全性：路径分隔符 ``/`` ``\`` 与 NUL 都不在字符集内，且首字符必须是
#: ``\w``，因此 ``.``、``..`` 这类目录穿越名一律不合法（见 tests/test_paths.py）。
ACCOUNT_NAME_RE = re.compile(r"^[\w][\w.\- ]{0,63}$", re.UNICODE)

DEFAULT_DATA_DIR = "./data"


class InvalidAccountName(ValueError):
    """账号名不合法。"""


def validate_account_name(name: str) -> str:
    """校验并归一化账号名。

    账号名会直接作为目录名与会话文件名使用，因此必须严格校验，
    防止 ``../`` 之类的路径穿越写到数据目录之外。
    """
    normalized = (name or "").strip()
    if not ACCOUNT_NAME_RE.match(normalized):
        raise InvalidAccountName(
            f"账号名 {name!r} 不合法：允许字母（含中文等 Unicode 字符）、数字、"
            "'.'、'_'、'-'、空格，首字符不能是符号或空格，长度 1-64。"
        )
    return normalized


@dataclass(frozen=True)
class AccountPaths:
    """单个账号的全部路径。"""

    name: str
    root: Path

    @property
    def session_dir(self) -> Path:
        """Pyrogram 的 workdir（会话文件所在目录）。"""
        return self.root

    @property
    def session_file(self) -> Path:
        return self.root / f"{self.name}.session"

    @property
    def config_file(self) -> Path:
        return self.root / "config.json"

    @property
    def state_file(self) -> Path:
        return self.root / "state.json"

    @property
    def used_codes_file(self) -> Path:
        """【已弃用·仅供迁移读取】账号级「已用注册码」记忆的旧位置。

        「已用码拦截」已改成**全局一份**（见 :meth:`Paths.used_codes_file`）：
        任一账号看到「码已使用」通知，所有账号都不再转发这条码。引擎不再读这里，
        只有一次性迁移脚本会来读旧文件、把历史记忆并进全局那份（不丢数据）。
        """
        return self.root / "used_codes.json"

    @property
    def red_packet_settled_file(self) -> Path:
        """抢红包「已得出定论的消息」记忆（``_settled`` 的落盘）。

        🔴 **为什么必须落盘**：``_settled`` 原本只在内存里，而它的作用是让**同一条红包
        消息不再被点第二次**（红包 bot 会反复编辑同一条消息，见 ``RedPacketHunter._settle``）。
        一次重启就忘光 —— 线上实测（2026-09-29）一条长驻红包 8 小时里被点了 8 次，
        中间夹着 8 次服务重启：11:16 已经判出「你已经领过」，13:50 重启之后就又点了一次。
        与 ``used_codes_file``（原账号级、现已全局化）同一个道理：这是**账号级**的状态，
        每个账号抢的红包各不相同，不能跨账号共享。
        """
        return self.root / "red_packet_settled.json"

    def ensure(self) -> "AccountPaths":
        self.root.mkdir(parents=True, exist_ok=True)
        return self


@dataclass(frozen=True)
class Paths:
    """全局数据目录布局。"""

    data_dir: Path

    @classmethod
    def from_env(cls, data_dir: str | os.PathLike[str] | None = None) -> "Paths":
        raw = str(data_dir or os.environ.get("TGA_DATA_DIR") or DEFAULT_DATA_DIR)
        return cls(data_dir=Path(raw).expanduser().resolve())

    # ---- 目录 ----
    @property
    def accounts_dir(self) -> Path:
        return self.data_dir / "accounts"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def account_log_dir(self) -> Path:
        return self.log_dir / "accounts"

    @property
    def qr_dir(self) -> Path:
        return self.data_dir / "qr"

    # ---- 文件 ----
    @property
    def registry_file(self) -> Path:
        return self.data_dir / "accounts.json"

    @property
    def dedupe_file(self) -> Path:
        """转发去重表（「最近已转发过的内容」）的落盘位置。

        🔴 **为什么必须落盘**：这张表的窗口是 ``recent_dedupe_window``（默认 **24 小时**，
        用户原话「一天内」），但它原来是**纯内存**的 —— 进程一重启就清零，
        「一天内不重复」的承诺被一次重启作废。

        2026-09-26 线上取证：``text:ded5df56…`` 于 09-25 23:50:36 转发，
        00:32:40 服务重启，10:33:16 同一条内容**又被转发了一次** ——
        中间只隔了一次重启。重启越频繁，重复越密。

        放在 ``data/`` 根下而不是按账号分：这张表本来就是**多账号共享**的
        （同一个内容发往同一个目标，只允许一个账号发出去），按账号拆开等于把它拆坏。
        """
        return self.data_dir / "dedupe.json"

    @property
    def metrics_file(self) -> Path:
        """数据大盘（按北京时间自然日累计的「成功」次数）的落盘位置。

        同样放在 ``data/`` 根下、同样**多账号共享** —— 用户要的是「总转发次数」，
        按账号拆开就得在面板上再加一层求和，而且「总计」这个口径也会跟着变形。

        累计值必须落盘：它是**历史**，进程重启（面板点一次「启动」也会重建
        MultiRunner）就清零的话，「总计」永远只等于这次运行以来的数。
        """
        return self.data_dir / "metrics.json"

    @property
    def forward_excludes_file(self) -> Path:
        """转发「全局排除」名单（排除频道 + 发送者黑名单），**所有账号共用一份**。

        🔴 **为什么要跨账号共享**：2026-09-29 线上取证 —— 两个账号（小白 / SevenStar）
        的 ``forward.exclude_chats`` 与 ``forward.exclude_users`` **一模一样**
        （``-1003932130542`` / ``8817602576``）：同一份名单被存了两遍，面板上还得
        一个账号填一次。加一个账号就多填一遍，漏填一个账号 = 那个号照转。
        用户原话：「将转发规则的黑名单及排除的频道也做成全局的」。

        放在 ``data/`` 根下，与 ``dedupe.json`` / ``metrics.json`` 同类 —— 都是
        "跨账号一份事实"。账号目录里那份 ``forward.exclude_*`` 保留为
        **该账号额外排除**，两者在引擎里取**并集**（见 ``ForwardEngine._rebuild_excludes``）。
        """
        return self.data_dir / "forward_excludes.json"

    @property
    def forward_used_codes_file(self) -> Path:
        """转发「已使用注册码拦截」的**配置**，**所有账号共用一份**。

        🔴 **为什么要跨账号共享**：2026-09-30 线上取证 —— 三个账号
        （小白 / SevenStar / 只想睡觉）的 ``forward.used_codes`` **一字不差完全相同**，
        同一份策略存了三遍，面板上还得一个账号填一次。用户原话：
        「将……已使用注册码拦截做成全局，而不是账号级」。

        与 ``forward_excludes_file`` 同类，放 ``data/`` 根下。区别是这里是**替换**
        而非并集：策略只有一份，不再读账号目录里那份 ``forward.used_codes``
        （模型里保留该字段只是为了让旧 ``config.json`` 仍能加载，引擎不再用它）。
        """
        return self.data_dir / "forward_used_codes.json"

    @property
    def used_codes_file(self) -> Path:
        """「已被用掉的注册码」记忆（转发前靠它拦废码），**所有账号共用一份**。

        🔴 **为什么从账号目录移到 ``data/`` 根**：以前它按账号存，理由是
        ``ttl`` / ``min_visible`` 是账号级配置、共用一份会让 A 的策略去裁剪 B 的记忆。
        现在配置已改成**全局一份**（见 :meth:`forward_used_codes_file`），那条顾虑消失，
        而共享记忆恰恰是用户要的：**任一账号**看到「码已使用」的通知，**所有账号**
        都不该再把这条码转出去 —— 一处学习、全账号生效。

        与 ``dedupe.json`` / ``forward_excludes.json`` 同为"跨账号一份事实"。
        （账号目录下的旧 ``used_codes.json`` 由一次性迁移脚本并进这一份，不再使用。）
        """
        return self.data_dir / "used_codes.json"

    def account(self, name: str) -> AccountPaths:
        safe = validate_account_name(name)
        return AccountPaths(name=safe, root=self.accounts_dir / safe)

    def ensure(self) -> "Paths":
        for path in (
            self.data_dir,
            self.accounts_dir,
            self.log_dir,
            self.account_log_dir,
            self.qr_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return self

    def iter_account_names(self) -> list[str]:
        """扫描磁盘上已存在的账号目录（按名称排序）。"""
        if not self.accounts_dir.is_dir():
            return []
        names = []
        for child in self.accounts_dir.iterdir():
            if not child.is_dir():
                continue
            try:
                names.append(validate_account_name(child.name))
            except InvalidAccountName:
                continue
        return sorted(names)


__all__ = [
    "ACCOUNT_NAME_RE",
    "AccountPaths",
    "InvalidAccountName",
    "Paths",
    "validate_account_name",
]
