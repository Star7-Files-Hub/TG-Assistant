"""账号名校验。

账号名会**直接当目录名**用（``data/accounts/<name>/``），所以这个正则是一条
安全边界：既要挡住 ``../`` 之类的目录穿越，又不能把合法的名字误伤。

仓库初版只允许 ASCII（``[A-Za-z0-9]``），而部署版允许 Unicode。这不是
"少个功能"那么轻 —— :meth:`Paths.iter_account_names` 对校验不过的目录名是
**静默跳过**的，于是服务器上建的中文账号名在仓库版里会凭空"消失"。
"""

from __future__ import annotations

import pytest

from tg_assistant.paths import ACCOUNT_NAME_RE, InvalidAccountName, Paths, validate_account_name


# --------------------------------------------------------------------------- #
# 合法名字
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name",
    [
        "a",
        "acct",
        "acct1",
        "_private",
        "my.account",
        "my-account",
        "my account",
        "A" * 64,  # 上限正好 64
        # 部署版支持的 Unicode 名字
        "我的账号",
        "主号",
        "テスト",
        "테스트",
        "张 三",
        "账号-1",
        "账号.2",
    ],
)
def test_valid_names_pass(name: str) -> None:
    assert validate_account_name(name) == name


def test_surrounding_whitespace_is_stripped() -> None:
    assert validate_account_name("  acct  ") == "acct"
    assert validate_account_name("\tacct\n") == "acct"


def test_name_of_exactly_64_chars_is_allowed() -> None:
    name = "a" * 64
    assert validate_account_name(name) == name


# --------------------------------------------------------------------------- #
# 目录穿越 / 非法名字
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        ".",
        "..",
        "...",
        "a/b",
        "a\\b",
        "/etc/passwd",
        "..\\..\\windows",
        "../escape",
        "a/../b",
        "sub/dir",
        # 首字符不能是符号
        "-leading-dash",
        ".leading-dot",
        # 注意：首尾空白是被 strip 掉的（见 test_surrounding_whitespace_is_stripped），
        # 所以 "  x" 是合法名而不是非法名，别往这里加。
        # 不合法字符
        "a\x00b",
        "a\nb",
        "a*b",
        "a?b",
        "a:b",
        "a|b",
        "a<b",
        "a>b",
        "a\"b",
        "a'b",
        "a;b",
        "a$b",
        "a%b",
        "a@b",
        "a+b",
        "a=b",
        "a,b",
        "a(b)",
        "a[b]",
        "a{b}",
        "a~b",
        "a`b",
        "a!b",
        "a#b",
        "a^b",
        "a&b",
    ],
)
def test_invalid_names_are_rejected(name: str) -> None:
    with pytest.raises(InvalidAccountName):
        validate_account_name(name)


def test_name_longer_than_64_chars_is_rejected() -> None:
    with pytest.raises(InvalidAccountName):
        validate_account_name("a" * 65)


def test_none_is_rejected() -> None:
    with pytest.raises(InvalidAccountName):
        validate_account_name(None)  # type: ignore[arg-type]


def test_traversal_never_escapes_the_accounts_dir(paths: Paths) -> None:
    """就算校验被绕过，路径也必须落在 accounts 目录内。

    这是上面那堆用例的"总闸"：正则哪天被放宽，这里会先红。
    """
    accounts_dir = paths.accounts_dir.resolve()
    for evil in ["../escape", "..", "a/../../b", "a/b", "..\\..\\x"]:
        with pytest.raises(InvalidAccountName):
            paths.account(evil)

    # 合法的 Unicode 名字必须真的落在 accounts 目录下面
    assert paths.account("我的账号").root.resolve().parent == accounts_dir


def test_regex_does_not_match_path_separators() -> None:
    """正则本身就不该接受分隔符（不依赖上面的黑名单）。"""
    for evil in ["a/b", "a\\b", "..", "."]:
        assert ACCOUNT_NAME_RE.match(evil) is None, f"{evil!r} 被正则放行了"


# --------------------------------------------------------------------------- #
# 回归：Unicode 账号目录不能被 iter_account_names 静默跳过
# --------------------------------------------------------------------------- #
def test_iter_account_names_keeps_unicode_accounts(paths: Paths) -> None:
    """这是移植 Unicode 支持要修的真正问题。

    ``iter_account_names()`` 遍历 ``accounts/`` 下每个目录并逐个
    ``validate_account_name()``，**校验不过的直接 continue**。所以只要正则
    不认中文，中文账号就在账号列表 / 日志初始化 / CLI 里全部消失，
    而且没有任何报错 —— 用户只会觉得"我的账号不见了"。
    """
    paths.ensure()
    for name in ["acct", "我的账号", "テスト"]:
        paths.account(name).ensure()

    # 混进一个真正非法的目录名，确认它仍被跳过（不是把校验整个关掉）
    (paths.accounts_dir / "bad name!").mkdir(parents=True, exist_ok=True)

    assert sorted(paths.iter_account_names()) == ["acct", "テスト", "我的账号"]


def test_unicode_account_files_land_in_its_own_dir(paths: Paths) -> None:
    acct = paths.account("我的账号").ensure()
    acct.session_file.write_bytes(b"x")
    assert acct.session_file.parent == acct.root
    assert acct.root.name == "我的账号"
    assert (paths.accounts_dir / "我的账号").is_dir()
