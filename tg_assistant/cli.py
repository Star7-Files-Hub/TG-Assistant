"""命令行入口（click）。

命令一览::

    tg-assistant login            扫码登录（支持多账号）
    tg-assistant accounts list    查看已登录账号
    tg-assistant accounts remove  删除账号（可选清数据）
    tg-assistant accounts enable/disable
    tg-assistant run              启动转发 + 抢红包
    tg-assistant proxy-check      代理连通性诊断
    tg-assistant config init      生成配置模板
    tg-assistant config show      查看/校验配置
    tg-assistant config validate  只校验，适合部署前检查
    tg-assistant chats            列出会话及其 chat_id（配规则时用）
    tg-assistant notify-test      发一条测试通知
    tg-assistant version
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from typing import Any, Optional

import click

from . import __app_name__, __version__
from .config import (
    AccountConfig,
    ProxyConfig,
    Settings,
    utc_now_iso,
)
from .logging_setup import configure_logging, get_logger
from .paths import InvalidAccountName, Paths, validate_account_name
from .proxy import probe_proxy, resolve_proxy, summarize
from .qr_login import DEFAULT_LOGIN_TIMEOUT, QrLoginError, terminal_renderer
from .store import ConfigError, Store

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 110}


class Context:
    """CLI 全局上下文。"""

    def __init__(
        self,
        settings: Settings,
        paths: Paths,
        store: Store,
        log_level: str,
    ) -> None:
        self.settings = settings
        self.paths = paths
        self.store = store
        self.log_level = log_level


pass_ctx = click.make_pass_decorator(Context)


def _fail(message: str, hint: str | None = None) -> None:
    click.secho(f"✗ {message}", fg="red", err=True)
    if hint:
        click.secho(f"  → {hint}", fg="yellow", err=True)
    raise SystemExit(1)


def _ok(message: str) -> None:
    click.secho(f"✓ {message}", fg="green")


def _info(message: str) -> None:
    click.echo(message)


# --------------------------------------------------------------------------- #
@click.group(context_settings=CONTEXT_SETTINGS)
@click.option("--data-dir", "-d", default=None, help="数据目录，默认 ./data 或 $TGA_DATA_DIR")
@click.option("--api-id", type=int, default=None, help="Telegram api_id，默认取 $TGA_API_ID")
@click.option("--api-hash", default=None, help="Telegram api_hash，默认取 $TGA_API_HASH")
@click.option(
    "--proxy",
    "-p",
    "proxy_url",
    default=None,
    help="全局代理，如 socks5://user:pass@127.0.0.1:1080，默认取 $TGA_PROXY",
)
@click.option(
    "--log-level",
    "-l",
    default=None,
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    help="日志级别，排查问题用 DEBUG",
)
@click.option("--no-console-log", is_flag=True, help="只写文件日志，不输出到终端")
@click.version_option(__version__, "-V", "--version", prog_name=__app_name__)
@click.pass_context
def cli(
    ctx: click.Context,
    data_dir: Optional[str],
    api_id: Optional[int],
    api_hash: Optional[str],
    proxy_url: Optional[str],
    log_level: Optional[str],
    no_console_log: bool,
) -> None:
    """TG-Assistant：多账号 Telegram 助手（扫码登录 / 秒级转发 / Bot 通知 / 自动抢红包）。"""
    try:
        settings = Settings.from_env(
            data_dir=data_dir,
            api_id=api_id,
            api_hash=api_hash,
            proxy_url=proxy_url,
            log_level=(log_level or "").upper() or None,
        )
    except ValueError as exc:
        _fail(f"参数有误：{exc}")
        return

    paths = Paths.from_env(settings.data_dir)
    store = Store(paths).bootstrap()
    accounts = paths.iter_account_names()
    configure_logging(
        paths.log_dir,
        settings.log_level,
        accounts=accounts,
        console=not no_console_log,
        pyrogram_level=settings.pyrogram_log_level,
    )
    ctx.obj = Context(settings, paths, store, settings.log_level)
    get_logger().debug(
        "CLI 启动",
        extra={
            "account": "-",
            "extra_fields": {
                "version": __version__,
                "data_dir": str(paths.data_dir),
                "log_level": settings.log_level,
                "proxy": settings.proxy.to_url() if settings.proxy else "无",
                "known_accounts": len(accounts),
            },
        },
    )


# --------------------------------------------------------------------------- #
# login
# --------------------------------------------------------------------------- #
@cli.command("login")
@click.option("--account", "-a", required=True, help="账号名（自定义标识，如 main、alt1）")
@click.option("--proxy", "-p", "proxy_url", default=None, help="该账号专用代理（覆盖全局）")
@click.option("--api-id", type=int, default=None, help="该账号专用 api_id")
@click.option("--api-hash", default=None, help="该账号专用 api_hash")
@click.option("--timeout", default=DEFAULT_LOGIN_TIMEOUT, show_default=True, help="等待扫码的秒数")
@click.option("--force", is_flag=True, help="即使已有有效会话也重新扫码")
@click.option("--qr-invert/--no-qr-invert", default=True, help="二维码配色（深色终端用默认值）")
@click.option("--save-qr", is_flag=True, help="同时把二维码保存为 PNG 图片")
@pass_ctx
def login_cmd(
    ctx: Context,
    account: str,
    proxy_url: Optional[str],
    api_id: Optional[int],
    api_hash: Optional[str],
    timeout: float,
    force: bool,
    qr_invert: bool,
    save_qr: bool,
) -> None:
    """扫码登录一个账号。多账号只需换 --account 重复执行。"""
    from .runner import login_account

    try:
        name = validate_account_name(account)
    except InvalidAccountName as exc:
        _fail(str(exc))
        return

    proxy: Optional[ProxyConfig] = None
    if proxy_url:
        try:
            proxy = ProxyConfig.from_url(proxy_url)
        except ValueError as exc:
            _fail(f"代理地址无法解析：{exc}")
            return

    effective_proxy = proxy or ctx.settings.proxy
    if effective_proxy is None:
        click.secho(
            "提示：未配置代理。国内服务器直连 Telegram 通常不通，"
            "可用 --proxy socks5://127.0.0.1:1080 指定。",
            fg="yellow",
        )

    png_path = ctx.paths.qr_dir / f"{name}.png" if save_qr else None
    renderer = terminal_renderer(invert=qr_invert, png_path=png_path, echo=click.echo)

    async def password_provider(hint: Optional[str]) -> Optional[str]:
        env_password = os.environ.get("TGA_2FA_PASSWORD")
        if env_password:
            click.echo("使用环境变量 TGA_2FA_PASSWORD 中的两步验证密码")
            return env_password
        if not sys.stdin.isatty():
            return None
        prompt = "请输入两步验证密码"
        if hint:
            prompt += f"（提示：{hint}）"
        return click.prompt(prompt, hide_input=True, default="", show_default=False) or None

    async def main() -> None:
        result = await login_account(
            name,
            ctx.store,
            ctx.settings,
            renderer=renderer,
            password_provider=password_provider,
            timeout=timeout,
            proxy_override=proxy,
            api_id=api_id,
            api_hash=api_hash,
            force=force,
        )
        _ok(
            f"账号 {result.account} 登录成功："
            f"{result.display_name or ''} "
            f"{'@' + result.username if result.username else ''} (id={result.user_id})"
        )
        _info(f"  数据目录：{ctx.paths.account(name).root}")
        _info(f"  配置文件：{ctx.paths.account(name).config_file}")
        _info("  下一步：编辑配置文件添加转发规则，然后运行 tg-assistant run")

    try:
        asyncio.run(main())
    except QrLoginError as exc:
        _fail(str(exc))
    except ConfigError as exc:
        _fail(str(exc))
    except KeyboardInterrupt:
        click.secho("\n已取消登录", fg="yellow")
        raise SystemExit(130) from None


# --------------------------------------------------------------------------- #
# accounts
# --------------------------------------------------------------------------- #
@cli.group("accounts")
def accounts_group() -> None:
    """账号管理。"""


@accounts_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="以 JSON 输出")
@pass_ctx
def accounts_list(ctx: Context, as_json: bool) -> None:
    """列出已登录账号及其配置概览。"""
    registry = ctx.store.load_registry()
    if not registry.accounts:
        click.secho("还没有任何账号。先执行：tg-assistant login -a main", fg="yellow")
        return

    rows = []
    for record in registry.accounts:
        try:
            config = ctx.store.load_account_config(record.name, create=False)
        except ConfigError as exc:
            config = AccountConfig.default()
            click.secho(f"! 账号 {record.name} 配置有误：{exc}", fg="red", err=True)
        proxy = resolve_proxy(record, ctx.settings)
        source_count = sum(len(rule.sources) for rule in config.forward.active_rules)
        rows.append(
            {
                "账号": record.name,
                "状态": "启用" if record.enabled else "停用",
                "身份": ("@" + record.username) if record.username else (record.display_name or "-"),
                "user_id": record.user_id or "-",
                "会话": "有" if ctx.store.has_session(record.name) else "缺失",
                "代理": proxy.to_url() if proxy else "直连",
                "转发规则": len(config.forward.active_rules),
                "监听会话": source_count,
                "抢红包": "开" if config.red_packet.enabled else "关",
                "抢注任务": "开" if config.reg_grab.enabled else "关",
                # 时段直接进表格：一眼能看出哪个账号半夜还在动手。
                "抢注时段": config.reg_grab.window.describe(),
                "通知": "开" if config.notify.enabled else "关",
                "最后登录": record.last_login_at or "-",
            }
        )

    if as_json:
        click.echo(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    _print_table(rows)


@accounts_group.command("remove")
@click.argument("account")
@click.option("--purge", is_flag=True, help="同时删除该账号的会话、配置与全部数据（不可恢复）")
@click.option("--yes", "-y", is_flag=True, help="跳过确认")
@pass_ctx
def accounts_remove(ctx: Context, account: str, purge: bool, yes: bool) -> None:
    """从注册表移除账号。加 --purge 才会删除磁盘数据。"""
    name = validate_account_name(account)
    record = ctx.store.get_account(name)
    if record is None:
        _fail(f"账号 {name} 不存在")
        return
    target = ctx.paths.account(name).root
    if purge and not yes:
        click.secho(f"将永久删除目录：{target}", fg="red")
        click.secho("其中包含登录会话与配置，删除后需要重新扫码登录。", fg="yellow")
        click.confirm("确认删除？", abort=True)
    ctx.store.delete_account(name, remove_data=purge)
    _ok(f"账号 {name} 已移除" + ("（数据已删除）" if purge else "（数据保留在磁盘）"))
    if not purge:
        _info(f"  数据仍在：{target}")


@accounts_group.command("enable")
@click.argument("account")
@pass_ctx
def accounts_enable(ctx: Context, account: str) -> None:
    """启用账号（run 时会被拉起）。"""
    _set_enabled(ctx, account, True)


@accounts_group.command("disable")
@click.argument("account")
@pass_ctx
def accounts_disable(ctx: Context, account: str) -> None:
    """停用账号（run 时跳过）。"""
    _set_enabled(ctx, account, False)


def _set_enabled(ctx: Context, account: str, enabled: bool) -> None:
    name = validate_account_name(account)
    record = ctx.store.get_account(name)
    if record is None:
        _fail(f"账号 {name} 不存在")
        return
    ctx.store.upsert_account(record.model_copy(update={"enabled": enabled}))
    _ok(f"账号 {name} 已{'启用' if enabled else '停用'}")


@accounts_group.command("set-proxy")
@click.argument("account")
@click.argument("proxy_url", required=False)
@pass_ctx
def accounts_set_proxy(ctx: Context, account: str, proxy_url: Optional[str]) -> None:
    """设置账号专用代理；不传地址则清除，回退到全局代理。"""
    name = validate_account_name(account)
    record = ctx.store.get_account(name)
    if record is None:
        _fail(f"账号 {name} 不存在")
        return
    proxy = None
    if proxy_url:
        try:
            proxy = ProxyConfig.from_url(proxy_url)
        except ValueError as exc:
            _fail(f"代理地址无法解析：{exc}")
            return
    ctx.store.upsert_account(record.model_copy(update={"proxy": proxy}))
    _ok(f"账号 {name} 的代理已{'设置为 ' + proxy.to_url() if proxy else '清除'}")


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
@cli.command("run")
@click.option(
    "--account",
    "-a",
    "accounts",
    multiple=True,
    help="要运行的账号，可重复；不指定则运行全部启用的账号",
)
@click.option("--heartbeat", default=300.0, show_default=True, help="心跳统计间隔（秒）")
@click.option("--restart-delay", default=15.0, show_default=True, help="异常后重启的基础延迟（秒）")
@click.option("--max-restarts", default=5, show_default=True, help="单账号最大重启次数")
@click.option("--web", is_flag=True, help="同时启动 Web 控制台（零账号时也可用，方便扫码登录）")
@click.option("--web-host", default="0.0.0.0", show_default=True, help="Web 监听地址")
@click.option("--web-port", default=8080, type=int, show_default=True, help="Web 监听端口")
@click.option(
    "--web-password",
    default=None,
    envvar="TGA_WEB_PASSWORD",
    help="Web 访问密码（也可用环境变量 TGA_WEB_PASSWORD）",
)
@pass_ctx
def run_cmd(
    ctx: Context,
    accounts: tuple[str, ...],
    heartbeat: float,
    restart_delay: float,
    max_restarts: int,
    web: bool,
    web_host: str,
    web_port: int,
    web_password: Optional[str],
) -> None:
    """启动转发与抢红包（前台运行，Ctrl-C 优雅退出）。"""
    from .forwarder import ChannelGroupDedupe, CrossAccountDedupe, RecentContentDedupe
    from .runner import MultiRunner

    registry = ctx.store.load_registry()
    if accounts:
        names = []
        for raw in accounts:
            name = validate_account_name(raw)
            if registry.get(name) is None:
                _fail(f"账号 {name} 未登录", f"先执行：tg-assistant login -a {name}")
            names.append(name)
    else:
        names = [record.name for record in registry.enabled_accounts]

    # 启动前先把配置全部校验一遍，避免跑起来才发现规则写错
    problems = 0
    for name in names:
        try:
            config = ctx.store.load_account_config(name)
        except ConfigError as exc:
            click.secho(f"✗ 账号 {name} 配置有误：\n{exc}", fg="red", err=True)
            problems += 1
            continue
        if not config.needs_updates:
            click.secho(
                f"! 账号 {name} 没有启用任何功能（转发规则为空且未开启抢红包）", fg="yellow"
            )
    if problems:
        _fail(f"{problems} 个账号的配置无法加载，已中止启动")
        return

    if names:
        click.secho(f"启动 {len(names)} 个账号：{', '.join(names)}", fg="cyan")
    else:
        # 「零账号 + --web」是合法用法：面板本来就是用来扫码加账号的。
        click.secho("没有可运行的账号", fg="yellow")
        if not web:
            _fail(
                "没有可运行的账号",
                "先执行 tg-assistant login -a <账号名>，或用 accounts enable 启用已停用的账号",
            )
            return
        click.secho("  启动 Web 控制台以便扫码登录…", fg="yellow")

    click.secho(f"日志目录：{ctx.paths.log_dir}", fg="cyan")

    # runner 提前建好：Web 模式下它会被 create_app 交给 RuntimeManager **接管**
    # （见 RuntimeManager._adopt_external_runner），所以必须早于 create_app 存在。
    # MultiRunner 的构造函数不碰网络、不读 session，提前建没有副作用。
    #
    # 🔴 三张去重表必须**显式建好交进去**。原来这里只 `MultiRunner(store, settings)`，
    # pair/recent 两张表默认 None ⇒ ChannelGroupDedupe / RecentContentDedupe
    # 这两层跨账号去重**整个不生效**，每个 ForwardEngine 只能各自 new 一张
    # RecentContentDedupe（见 ForwardEngine.__init__ 的 `if self._recent is None`）。
    # 后果实测：小白 与 SevenStar 都监听「Wakk 研究所 / 鲨鱼影视」，同一条抽奖
    # 先由 SevenStar 发到目标频道、又由小白发一遍 —— 目标里两条一模一样的重复。
    # 只有 Web 面板那条路径（RuntimeManager）建了这三张表，CLI 直跑就漏了。
    runner = MultiRunner(
        ctx.store,
        ctx.settings,
        dedupe=CrossAccountDedupe(),
        pair_dedupe=ChannelGroupDedupe(),
        recent_dedupe=RecentContentDedupe(),
    )
    run_options = {
        "heartbeat": heartbeat,
        "restart_delay": restart_delay,
        "max_restarts": max_restarts,
    }

    app = None
    if web:
        from tg_assistant.web import create_app
        from tg_assistant.web.settings import WebSettings

        # 密钥直接用密码本身，**不要**用 api_hash：api_hash 是 Telegram 的长期凭据，
        # 散落在 .env / 配置备份 / 日志里，泄露即可伪造 session cookie 绕过密码，
        # 而且改密码也救不了（密钥不随密码变）。用密码当密钥则
        # 「改密码 = 所有旧 session 立即失效」，且不必把密钥落盘。
        password = (web_password or "").strip()

        # 对外监听却没有密钥 = 任何人都能删账号、改配置，直接拒绝启动。
        if not password and web_host not in {"127.0.0.1", "localhost", "::1"}:
            _fail(
                f"Web 监听地址 {web_host} 会把控制台暴露到网络上，但未设置访问密码",
                "请加 --web-password <密码>（或设置环境变量 TGA_WEB_PASSWORD）；"
                "若只想本机访问，请用 --web-host 127.0.0.1。",
            )
            return

        click.secho(f"🌐 Web 控制台启动：http://{web_host}:{web_port}", fg="cyan")
        if password:
            click.secho("   访问鉴权：已启用", fg="cyan")
        else:
            click.secho("   访问鉴权：未启用（仅本机可访问）", fg="yellow")

        app = create_app(
            data_dir=ctx.paths.data_dir,
            api_id=ctx.settings.api_id,
            api_hash=ctx.settings.api_hash,
            proxy_url=ctx.settings.proxy.to_url() if ctx.settings.proxy else None,
            log_level=ctx.settings.log_level,
            runner=runner,
            initial_accounts=names,
            run_options=run_options,
        )
        app.state.web_settings = WebSettings(
            host=web_host,
            port=web_port,
            secret_key=password,
        )

    async def _run_all() -> int:
        """跑转发 runner；开了 ``--web`` 就把 Web 控制台一起跑起来。"""
        if app is None:
            return await runner.run(names, **run_options)

        import uvicorn

        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=web_host,
                port=web_port,
                log_level=ctx.settings.log_level.lower(),
            )
        )

        if not names:
            click.secho("等待在 Web 界面扫码登录…（Ctrl-C 退出）", fg="cyan")
        else:
            click.secho("账号由面板托管：启停请用面板上的按钮（Ctrl-C 退出）", fg="cyan")

        # ⚠️ Web 模式下**不要**在这里 await runner.run()。
        # 那个 runner 已经交给 app.state.runtime 接管了，两边都跑的话：
        # 面板既控制不了 CLI 这一套（它的 self._runner 是 None，状态恒为
        # 「未运行」、优选 IP 复用不到已登录的 client），点「启动」还会
        # 再拉起第二套 runner，同一条消息被转发两次。
        # 这里只负责把 Web 服务跑起来；账号的启停归面板。
        await server.serve()
        return 0

    try:
        code = asyncio.run(_run_all())
    except KeyboardInterrupt:
        code = 130
    raise SystemExit(code)


# --------------------------------------------------------------------------- #
# proxy-check
# --------------------------------------------------------------------------- #
@cli.command("proxy-check")
@click.option("--proxy", "-p", "proxy_url", default=None, help="要检测的代理；默认用全局/账号代理")
@click.option("--account", "-a", default=None, help="按该账号的代理配置检测")
@click.option("--timeout", default=8.0, show_default=True, help="单步超时（秒）")
@pass_ctx
def proxy_check_cmd(
    ctx: Context, proxy_url: Optional[str], account: Optional[str], timeout: float
) -> None:
    """诊断代理到 Telegram 的连通性（部署第一步就该跑这个）。"""
    proxy: Optional[ProxyConfig] = None
    if proxy_url:
        try:
            proxy = ProxyConfig.from_url(proxy_url)
        except ValueError as exc:
            _fail(f"代理地址无法解析：{exc}")
            return
    elif account:
        record = ctx.store.get_account(validate_account_name(account))
        if record is None:
            _fail(f"账号 {account} 不存在")
            return
        proxy = resolve_proxy(record, ctx.settings)
    else:
        proxy = ctx.settings.proxy

    click.echo(f"检测目标：{proxy.to_url() if proxy else '直连（未配置代理）'}")
    results = asyncio.run(probe_proxy(proxy, timeout=timeout))
    ok, report = summarize(results)
    for line in report.splitlines():
        color = "green" if line.startswith("[OK]") else "red"
        click.secho(line, fg=color)
    if ok:
        _ok("代理可用，可以继续 login")
    else:
        _fail("代理不可用，请先解决网络问题", "socks5 端口是否正确？代理出口能否访问 Telegram？")


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
@cli.group("config")
def config_group() -> None:
    """配置管理。"""


@config_group.command("init")
@click.option("--account", "-a", required=True, help="账号名")
@click.option("--force", is_flag=True, help="覆盖已有配置")
@click.option("--example", is_flag=True, help="写入带示例规则的配置（而不是空配置）")
@pass_ctx
def config_init(ctx: Context, account: str, force: bool, example: bool) -> None:
    """生成账号配置文件。"""
    name = validate_account_name(account)
    target = ctx.paths.account(name).config_file
    if target.is_file() and not force:
        _fail(f"配置已存在：{target}", "加 --force 覆盖，或直接编辑该文件")
        return
    config = _example_config() if example else AccountConfig.default()
    ctx.store.save_account_config(name, config)
    _ok(f"配置已写入 {target}")
    if example:
        _info("  这是示例配置，请把 sources/targets/chat_id 换成你自己的。")
    _info("  修改后可用 tg-assistant config validate -a " + name + " 校验。")


@config_group.command("show")
@click.option("--account", "-a", required=True, help="账号名")
@pass_ctx
def config_show(ctx: Context, account: str) -> None:
    """打印账号配置（已做敏感信息脱敏）。"""
    name = validate_account_name(account)
    try:
        config = ctx.store.load_account_config(name, create=False)
    except ConfigError as exc:
        _fail(str(exc))
        return
    payload = config.model_dump(mode="json")
    token = payload.get("notify", {}).get("bot_token")
    if token:
        payload["notify"]["bot_token"] = token[:8] + "***"
    click.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@config_group.command("validate")
@click.option("--account", "-a", "accounts", multiple=True, help="要校验的账号，可重复；默认全部")
@pass_ctx
def config_validate(ctx: Context, accounts: tuple[str, ...]) -> None:
    """校验配置是否合法，并汇报将要监听的会话。"""
    if accounts:
        names = [validate_account_name(a) for a in accounts]
    else:
        # 注册表里的账号 + 磁盘上已有配置目录的账号（config init 可能先于 login 执行）
        names = [r.name for r in ctx.store.load_registry().accounts]
        for name in ctx.paths.iter_account_names():
            if name not in names and ctx.paths.account(name).config_file.is_file():
                names.append(name)
    if not names:
        _fail("没有账号可校验", "先执行 tg-assistant config init -a <账号名> 或 login")
        return

    failed = 0
    for name in names:
        try:
            config = ctx.store.load_account_config(name, create=False)
        except ConfigError as exc:
            click.secho(f"✗ {name}: {exc}", fg="red")
            failed += 1
            continue
        watched = config.watched_chats()
        click.secho(f"✓ {name} 配置合法", fg="green")
        _info(f"    转发规则：{len(config.forward.active_rules)} 条（共 {len(config.forward.rules)}）")
        for rule in config.forward.active_rules:
            _info(
                f"      - [{rule.label}] {rule.match.mode} → {', '.join(map(str, rule.targets))}"
                f"（来源：{', '.join(map(str, rule.sources)) or '全部'}）"
            )
        _info(f"    抢红包：{'开启' if config.red_packet.enabled else '关闭'}")
        if config.red_packet.enabled:
            _info(f"      策略：{config.red_packet.strategy}，回复：{'开' if config.red_packet.reply.enabled else '关'}")
        _info(f"    抢注任务：{'开启' if config.reg_grab.enabled else '关闭'}")
        if config.reg_grab.enabled:
            _info(
                f"      提取正则：{config.reg_grab.detect.code_pattern or '（未填）'}"
                f"，步骤：{len(config.reg_grab.steps)} 步"
                f"{'' if config.reg_grab.ready else ' ⚠️ 配置不完整，不会执行'}"
            )
            # 时段单独一行：它是「此刻到底会不会动手」的直接答案，
            # 用户排查「为什么没动静」时第一个要看的就是它。
            window = config.reg_grab.window
            state = "在时段内" if config.reg_grab.in_window else "不在时段内，不会动手"
            _info(
                f"      监听时段：{window.describe()}"
                f"（服务端现在 {datetime.now().strftime('%H:%M')}，{state}）"
            )
        _info(f"    通知：{'开启' if config.notify.enabled else '关闭'}（模式 {config.notify.mode}）")
        if watched:
            _info(f"    监听会话：{len(watched)} 个")
        else:
            _info("    监听会话：全部会话")
        # Telegram 单账号限制 500 群+频道，接近时给出警告
        if 0 < len(watched) >= 450:
            click.secho(
                f"    ⚠ 监听会话已达 {len(watched)} 个，接近 Telegram 单账号 500 上限！"
                "建议拆分到多个账号。",
                fg="yellow",
            )
    if failed:
        raise SystemExit(1)


# --------------------------------------------------------------------------- #
# chats
# --------------------------------------------------------------------------- #
@cli.command("chats")
@click.option("--account", "-a", required=True, help="账号名")
@click.option("--limit", default=50, show_default=True, help="最多列出多少个会话")
@click.option("--keyword", "-k", default=None, help="按标题/用户名过滤")
@pass_ctx
def chats_cmd(ctx: Context, account: str, limit: int, keyword: Optional[str]) -> None:
    """列出会话及其 chat_id —— 配置转发规则时用来找 id。"""
    from .client import build_client

    name = validate_account_name(account)
    record = ctx.store.get_account(name)
    if record is None:
        _fail(f"账号 {name} 不存在", f"先执行：tg-assistant login -a {name}")
        return

    async def main() -> None:
        client = build_client(record, ctx.settings, ctx.paths, no_updates=True)
        await client.start()
        rows: list[dict[str, Any]] = []
        try:
            async for dialog in client.get_dialogs(limit=limit):
                chat = dialog.chat
                title = chat.title or " ".join(
                    filter(None, [chat.first_name, chat.last_name])
                ) or "-"
                username = chat.username or ""
                if keyword:
                    haystack = f"{title} {username}".lower()
                    if keyword.lower() not in haystack:
                        continue
                rows.append(
                    {
                        "chat_id": chat.id,
                        "类型": getattr(chat.type, "value", str(chat.type)),
                        "名称": title[:30],
                        "用户名": ("@" + username) if username else "-",
                    }
                )
        finally:
            await client.stop(block=True)
        if not rows:
            click.secho("没有匹配的会话", fg="yellow")
            return
        _print_table(rows)
        _info("\n把上面的 chat_id 填进配置的 sources / targets / chats 即可。")

    try:
        asyncio.run(main())
    except Exception as exc:
        _fail(f"读取会话列表失败：{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# notify-test
# --------------------------------------------------------------------------- #
@cli.command("notify-test")
@click.option("--account", "-a", required=True, help="账号名")
@pass_ctx
def notify_test_cmd(ctx: Context, account: str) -> None:
    """发一条测试通知，验证 bot token / chat_id / 代理是否都对。"""
    from .logging_setup import account_logger
    from .notify import BotNotifier, NotifyTask

    name = validate_account_name(account)
    record = ctx.store.get_account(name)
    try:
        config = ctx.store.load_account_config(name, create=False)
    except ConfigError as exc:
        _fail(str(exc))
        return
    if not config.notify.enabled:
        _fail("该账号未启用通知", "在配置里把 notify.enabled 设为 true，并填 bot_token 与 chat_id")
        return

    proxy = resolve_proxy(record, ctx.settings)

    async def main() -> None:
        notifier = BotNotifier(config.notify, account_logger(name), proxy)
        await notifier.start()
        ok, detail = await notifier.verify()
        if not ok:
            await notifier.stop()
            _fail(f"bot 验证失败：{detail}")
            return
        notifier.submit(
            NotifyTask(
                event=config.notify.events[0] if config.notify.events else "forward",
                text=(
                    f"✅ <b>TG-Assistant 通知测试</b>\n"
                    f"账号：{name}\n"
                    f"时间：{utc_now_iso()}\n"
                    f"如果你看到这条消息，说明通知渠道已经打通。"
                ),
            )
        )
        await notifier.stop(drain_timeout=20)
        if notifier.stats["sent"]:
            _ok(f"测试通知已发送（bot: {detail}）")
        else:
            _fail("通知未能发出，请看上面的错误日志")

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
@cli.command("status")
@pass_ctx
def status_cmd(ctx: Context) -> None:
    """展示数据目录概况：账号、会话、日志大小。"""
    registry = ctx.store.load_registry()
    click.secho(f"{__app_name__} {__version__}", fg="cyan", bold=True)
    _info(f"数据目录：{ctx.paths.data_dir}")
    _info(f"日志目录：{ctx.paths.log_dir}")
    _info(f"全局代理：{ctx.settings.proxy.to_url() if ctx.settings.proxy else '未配置'}")
    _info(
        f"api 凭据：{'已配置' if ctx.settings.api_id and ctx.settings.api_hash else '未配置（登录前必须配置）'}"
    )
    _info(f"账号数量：{len(registry.accounts)}（启用 {len(registry.enabled_accounts)}）")

    log_files = []
    if ctx.paths.log_dir.is_dir():
        for path in sorted(ctx.paths.log_dir.rglob("*.log*")):
            log_files.append((path.relative_to(ctx.paths.log_dir), path.stat().st_size))
    if log_files:
        _info("\n日志文件：")
        for rel, size in log_files[:12]:
            _info(f"  {str(rel):<32} {_human_size(size)}")


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}GB"


def _display_width(text: str) -> int:
    """中文按 2 列宽计算，保证表格对齐。"""
    width = 0
    for char in text:
        width += 2 if ord(char) > 0x2E80 else 1
    return width


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _print_table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    headers = list(rows[0].keys())
    widths = {
        header: max(_display_width(str(header)), *(_display_width(str(row[header])) for row in rows))
        for header in headers
    }
    click.secho("  ".join(_pad(str(h), widths[h]) for h in headers), bold=True)
    click.echo("  ".join("-" * widths[h] for h in headers))
    for row in rows:
        click.echo("  ".join(_pad(str(row[header]), widths[header]) for header in headers))


def _example_config() -> AccountConfig:
    """带注释意味的示例配置，用户改 id 即可用。"""
    return AccountConfig.model_validate(
        {
            "forward": {
                "enabled": True,
                "rules": [
                    {
                        "id": "demo-regex",
                        "name": "示例：抓取带金额的消息",
                        "enabled": False,
                        "sources": ["@source_channel_username"],
                        "targets": [-1001234567890],
                        "mode": "copy",
                        "match": {
                            "mode": "regex",
                            "patterns": [r"(?:金额|价格)[:：]\s*([\d.]+)"],
                            "exclude_patterns": ["测试"],
                            "fields": ["text", "caption"],
                        },
                        "notify": True,
                    }
                ],
            },
            "notify": {
                "enabled": False,
                "bot_token": "${TGA_BOT_TOKEN}",
                "chat_id": 0,
                "mode": "copy",
                "events": ["forward", "red_packet", "reg_grab"],
            },
            "red_packet": {
                "enabled": False,
                "chats": [],
                "strategy": "auto",
                "detect": {
                    "button_keywords": ["领取", "抢", "红包", "🧧"],
                    "code_pattern": None,
                    "keyword_template": None,
                },
                "reply": {
                    "enabled": True,
                    "texts": ["谢谢老板", "xxlb", "感谢大哥"],
                    "only_on_success": True,
                    "delay_range": [0.8, 2.5],
                },
            },
            "reg_grab": {
                "enabled": False,
                "chats": [],
                "detect": {
                    "code_pattern": r"(?:Register|Renew|Whitelist)_([A-Za-z0-9]{10})",
                    "text_patterns": [],
                },
                "steps": [
                    {"type": "send", "name": "把码发给机器人", "chat": "@example_bot", "text": "/bind {code}"},
                    {"type": "wait", "name": "等它处理", "seconds": 1.5},
                    {"type": "wait_reply", "name": "看回执", "pattern": "成功|失败|已使用", "timeout": 15},
                ],
            },
        }
    )


# --------------------------------------------------------------------------- #
# web
# --------------------------------------------------------------------------- #
@cli.command("web")
@click.option("--host", default="127.0.0.1", help="监听地址（默认仅本机可访问）")
@click.option("--port", "-p", default=8080, type=int, help="监听端口")
@click.option("--log-level", "-l", default=None, help="日志级别")
@click.option(
    "--secret",
    "-s",
    default=None,
    envvar="TGA_WEB_SECRET",
    help="Web 访问密钥（也可用环境变量 TGA_WEB_SECRET）；留空则不校验",
)
@pass_ctx
def web_cmd(
    ctx: Context,
    host: str,
    port: int,
    log_level: str | None,
    secret: str | None,
) -> None:
    """启动 Web 控制台（FastAPI + WebSocket）。"""
    import uvicorn

    from tg_assistant.web import create_app

    # 对外监听却没有密钥 = 任何人都能删账号、改配置，直接拒绝启动。
    if not secret and host not in {"127.0.0.1", "localhost", "::1"}:
        raise click.ClickException(
            f"监听地址 {host} 会把控制台暴露到网络上，但未设置访问密钥。"
            "请加 --secret <密钥>（或设置环境变量 TGA_WEB_SECRET）；"
            "若只想本机访问，请用 --host 127.0.0.1。"
        )

    app = create_app(
        data_dir=ctx.paths.data_dir,
        api_id=ctx.settings.api_id,
        api_hash=ctx.settings.api_hash,
        proxy_url=ctx.settings.proxy.to_url() if ctx.settings.proxy else None,
        log_level=log_level or ctx.settings.log_level,
    )
    if secret:
        app.state.web_settings.secret_key = secret

    click.secho(f"🌐 Web 控制台启动：http://{host}:{port}", fg="cyan")
    click.secho(f"   数据目录：{ctx.paths.data_dir}", fg="cyan")
    if secret:
        click.secho("   访问鉴权：已启用（首次访问需输入密钥）", fg="green")
    else:
        click.secho("   访问鉴权：未启用（仅本机可访问）", fg="yellow")
    uvicorn.run(app, host=host, port=port, log_level=(log_level or ctx.settings.log_level).lower())


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
