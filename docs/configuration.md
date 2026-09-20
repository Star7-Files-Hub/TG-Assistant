# 配置参考

TG-Assistant 的配置分两层：

1. **环境变量**（`.env` / `TGA_*`）：全局设置，如 API 凭据、代理、日志级别
2. **账号配置**（`data/accounts/<name>/config.json`）：每个账号独立的转发规则、通知、抢红包设置

## Web 界面

所有配置都可以通过 Web 界面在线编辑：

```bash
tg-assistant web --port 8080
```

Web 界面包含：仪表盘、扫码登录、账号管理、配置编辑、实时日志、代理检查、通知测试。

## 环境变量

| 变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `TGA_API_ID` | ✅ | — | 从 https://my.telegram.org 获取 |
| `TGA_API_HASH` | ✅ | — | 从 https://my.telegram.org 获取 |
| `TGA_PROXY` | 国内必填 | — | `socks5://user:pass@host:1080` |
| `TGA_BOT_TOKEN` | — | — | 通知 bot 的 token |
| `TGA_DATA_DIR` | — | `./data` | 数据目录（会话、配置、日志） |
| `TGA_LOG_LEVEL` | — | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `TGA_PYROGRAM_LOG_LEVEL` | — | `WARNING` | pyrogram 自身日志级别 |
| `TGA_WORKERS` | — | `8` | pyrogram worker 数，秒级转发建议 >= 4 |
| `TGA_SLEEP_THRESHOLD` | — | `30` | FloodWait 低于此值直接 sleep |
| `TGA_IPV6` | — | `0` | 是否使用 IPv6 |
| `TGA_WEB_SECRET` | 远程访问必填 | — | Web 控制台访问密钥（等价于 `web --secret`） |
| `TGA_ENV_FILE` | — | `./.env` | 指定要加载的环境变量文件路径 |

### `.env` 是怎么加载的

程序启动时会自动读取**当前工作目录下的 `.env`**（也可以用 `TGA_ENV_FILE` 指定别的路径），
查找顺序和优先级如下：

```
真实环境变量  >  .env  >  代码默认值
```

也就是说 `.env` **不会覆盖**已经存在的环境变量。这跟 docker compose 的 `env_file`
和 systemd 的 `EnvironmentFile` 语义一致，所以同一份 `.env` 在容器、systemd
服务和手工执行的 CLI 命令下行为都一样。

这点对裸机部署尤其重要：`deploy.sh` 只把 `.env` 挂给了 systemd 服务，
但脚本提示你手工执行的 `tg-assistant login` / `config init` 不经过 systemd ——
自动加载 `.env` 才能让它们也拿到 `api_id` / `api_hash` / 代理。

## 账号配置结构

```json
{
  "version": 1,
  "forward": { ... },
  "notify": { ... },
  "red_packet": { ... }
}
```

---

## forward — 秒级转发

```json
{
  "forward": {
    "enabled": true,
    "dedupe_window": 300,
    "exclude_chats": [],
    "rules": [
      {
        "id": "rule-1",
        "name": "规则名（可选，默认用 id）",
        "enabled": true,
        "sources": ["@src_channel", -1001234567890],
        "exclude_sources": [],
        "targets": [-1009876543210],
        "target_thread_id": null,
        "match": {
          "mode": "regex",
          "patterns": ["金额[:：]\\s*([\\d.]+)"],
          "exclude_patterns": ["测试"],
          "ignore_case": true,
          "fields": ["text", "caption"],
          "min_length": 0
        },
        "from_users": [],
        "exclude_users": [],
        "ignore_self": true,
        "include_edited": false,
        "include_service": false,
        "mode": "copy",
        "template": "{text}",
        "include_source_link": true,
        "media_group": true,
        "media_group_window": 1.2,
        "delay": 0,
        "min_interval": 0,
        "notify": true,
        "silent": false
      }
    ]
  }
}
```

### 字段说明

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `enabled` | bool | 总开关 |
| `dedupe_window` | number | 去重窗口（秒）。**两层生效**：账号内同一条消息不重复触发；**跨账号**同一条消息发往**同一个目标**也只发一次（多个账号共享一张表，键里带目标，所以目标不同时互不影响） |
| `exclude_chats` | array | **账号级**排除的会话，写一次全部规则都不监听；格式同 sources |
| `rules[].id` | string | 唯一标识 |
| `rules[].sources` | array | 来源会话：`@username`、`chat_id`、`t.me/...` 链接。**空数组 = 监听全部** |
| `rules[].exclude_sources` | array | 只对**这一条规则**生效的排除来源 |
| `rules[].targets` | array | 目标会话，格式同 sources |
| `rules[].mode` | string | `forward`（带转发抬头）/ `copy`（无抬头）/ `text`（按模板重发）。⚠️ **`forward` 遇到受保护源会话时（Telegram 回 `CHAT_FORWARDS_RESTRICTED`）会自动改用复制**再发一次，不用手动改成 `copy`；那条消息的日志会显示 `mode=copy(降级)` |
| `rules[].match.mode` | string | `regex` / `contains` / `exact` / `all` |
| `rules[].match.patterns` | array | 匹配模式（正则或关键词） |
| `rules[].match.exclude_patterns` | array | 排除模式 |
| `rules[].match.fields` | array | 匹配范围：`text` / `caption` / `buttons` |
| `rules[].template` | string | `text` 模式下的模板，可用变量见下文 |
| `rules[].include_source_link` | bool | 是否附带来源链接（默认 `true`）。**`forward` 模式不需要它** —— Telegram 的「转发自」抬头本身就是回溯入口；**`copy` 模式**（含 `forward` 撞受保护源会话后的自动降级）没有抬头，会在正文/caption 末尾追加一行 `🔗原文链接：<t.me 链接>`（前面空一行）。实现上是发出后 `edit` 一次消息，**编辑失败不影响转发本身** |
| `rules[].media_group` | bool | 是否聚合相册（攒齐后一次转发） |
| `rules[].delay` | number | 命中后延迟多少秒再发 |
| `rules[].min_interval` | number | 同一规则两次触发的最小间隔 |
| `rules[].notify` | bool | 转发后是否推 bot 通知 |

> ⚠️ **转发目标不能同时是监听来源。**
> `sources` 为空数组时表示"监听全部群组/频道"，此时如果目标频道也在账号可见范围内，
> 转发出去的新消息会被本账号重新监听到（新消息 = 新 id，去重窗口拦不住），
> 再次命中同一条规则 —— 每转发一次就多产生一次命中，几秒内就能刷爆目标频道。
>
> 代码里已经**自动把每条规则自己的 `targets` 排除在来源之外**（`PreparedRule.chat_allowed`），
> 所以正常情况下不需要额外配置。但**反向也要注意**：想让 A 转发到 B 时，
> 必须把 A 显式写进 `sources`，不要用 `[]` 全监听。
>
> 想额外排除一些"目标之外、但同样不想监听"的会话，用账号级的 `exclude_chats`
> （面板：转发规则页 → 每个账号分组标题下的「排除频道」）。

### 模板变量

| 变量 | 说明 |
| --- | --- |
| `{text}` | 消息正文 |
| `{chat_title}` | 来源会话名称 |
| `{chat_id}` | 来源会话 id |
| `{chat_username}` | 来源会话用户名 |
| `{sender}` | 发送者显示名 |
| `{sender_name}` | 发送者纯姓名 |
| `{sender_id}` | 发送者 id |
| `{sender_username}` | 发送者用户名 |
| `{message_id}` | 消息 id |
| `{link}` | 消息链接 |
| `{time}` | 时间戳 |
| `{date}` | 日期 |
| `{media}` | 媒体类型 |
| `{g1}` `{group1}` ... | 正则捕获组 |
| `{code}` | 红包口令（抢红包专用） |

---

## notify — Bot 通知

```json
{
  "notify": {
    "enabled": true,
    "bot_token": "${TGA_BOT_TOKEN}",
    "chat_id": 123456789,
    "message_thread_id": null,
    "mode": "copy",
    "include_source_link": true,
    "template": "<b>{chat_title}</b>\\n{text}",
    "rate_limit_per_minute": 18,
    "queue_size": 500,
    "silent": false,
    "events": ["forward", "red_packet", "error"],
    "api_base": "https://api.telegram.org",
    "use_proxy": true,
    "timeout": 15
  }
}
```

### 字段说明

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `bot_token` | string | Bot token，支持 `${ENV_VAR}` 引用环境变量 |
| `chat_id` | number/string | 通知目标：你的私聊 id、频道 id、或话题 id |
| `mode` | string | `copy`（服务端复制，内容一致）/ `text`（纯文本） |
| `events` | array | 订阅事件：`forward` / `red_packet` / `error` |
| `rate_limit_per_minute` | number | 限流（默认 18，留余量给 Telegram 30/min 限制） |
| `use_proxy` | bool | 是否通过代理访问 Bot API |

> **为什么用 copy？** 频道消息无法收到通知。用 Bot API 的 `copyMessage` 把转发后的消息原样复制到你的私聊，内容与频道完全一致，且 Telegram 会正常推送通知。

---

## red_packet — 自动抢红包

```json
{
  "red_packet": {
    "enabled": true,
    "chats": [-1009999999999],
    "exclude_chats": [],
    "strategy": "auto",
    "delay": 0,
    "jitter": 0.5,
    "max_attempts": 2,
    "max_concurrency": 3,
    "include_edited": false,
    "notify": true,
    "detect": {
      "button_keywords": ["领取", "抢", "红包", "开", "拆", "grab", "claim", "open", "🧧"],
      "text_patterns": [],
      "code_pattern": null,
      "keyword_template": null,
      "only_from_bots": false,
      "ignore_self": true
    },
    "success": {
      "success_patterns": ["抢到", "领取成功", "恭喜"],
      "failure_patterns": ["已被抢完", "手慢", "已领完"],
      "wait_timeout": 8.0,
      "require_self_mention": false
    },
    "reply": {
      "enabled": true,
      "texts": ["谢谢老板", "xxlb", "感谢大哥"],
      "only_on_success": true,
      "delay_range": [0.8, 2.5],
      "reply_to_message": false,
      "cooldown": 0
    }
  }
}
```

### 字段说明

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `strategy` | string | `auto`（优先按钮，否则口令）/ `button` / `keyword` |
| `detect.button_keywords` | array | 按钮文字包含任一关键词即视为红包按钮 |
| `detect.code_pattern` | string | 从正文提取口令的正则，第一个捕获组作为 `{code}` |
| `detect.keyword_template` | string | 口令策略下发送的内容，如 `"/grab {code}"` |
| `success.success_patterns` | array | 判定成功的关键词 |
| `success.failure_patterns` | array | 判定失败的关键词 |
| `success.wait_timeout` | number | 等待后续消息判定结果的时间窗（秒） |
| `reply.texts` | array | 抢到后随机抽一条回复 |
| `reply.delay_range` | array | 回复前的随机延迟范围 `[min, max]`（秒） |
| `reply.cooldown` | number | 同一会话两次回复的最小间隔（秒） |

### 判定逻辑

1. 点击按钮后，Telegram 会**同步返回** callback answer 文本 → 最快判定
2. 若 callback 无结论，在 `wait_timeout` 内监听该会话的新消息 → 事件驱动，非轮询
3. 失败优先："已被抢完" 里也含 "抢"，先判失败可避免误报成功
4. 都没命中 → 记为 `unknown`（不会误报成功）

---

## 多账号

每个账号有独立的会话、配置、状态、日志：

```bash
tg-assistant login -a main       # 主账号
tg-assistant login -a alt1       # 小号 1
tg-assistant login -a alt2       # 小号 2

tg-assistant accounts list        # 查看全部
tg-assistant run                  # 并发运行全部启用账号
tg-assistant run -a main -a alt1 # 只运行指定账号
```

账号数据在 `data/accounts/<name>/` 下，互不影响。某个账号会话失效不会拖垮其他账号。

### 为什么要多账号？

Telegram 限制单账号最多加入 **500 个群+频道**。当你需要监听/转发的会话超过 500 个时，就必须用多个账号分担。

**分配策略示例：**

| 账号 | 监听范围 | sources 数量 |
| --- | --- | --- |
| main | 群 1–400 | 400 |
| alt1 | 群 401–800 | 400 |
| alt2 | 群 801–1200 | 400 |

每个账号的 `config.json` 里只写自己负责的那些 sources，框架会自动按账号分配监听。`run` 不传 `-a` 则并发运行全部账号，每个账号各跑各的。

> **注意：** 每个账号都需要单独扫码登录、单独配置转发规则。建议把规则按账号分文件管理，避免混淆。
