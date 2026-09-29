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

仪表盘上有一块**数据大盘**，只统计**成功**的次数（转发已发出 / 红包已抢到 / 注册已成功），
可以按 天 / 月 / 总 三个口径查看：

- **天** 是**北京时间的自然日**（00:00 换日），不是服务器本地时区 —— 服务跑在 UTC 上时
  两者差 8 小时，界面上写明了这一点；
- 计数按天累计并落盘在 `data/metrics.json`，重启不会清零，「总计」就是历史累计；
- 大盘从部署之后开始记录，**部署之前的历史无法回填**。

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
    "recent_dedupe_limit": 5,
    "recent_dedupe_window": 86400,
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
| `dedupe_window` | number | 去重窗口（秒）。**三层生效**：账号内同一条消息不重复触发；**跨账号**同一条消息发往**同一个目标**也只发一次（多个账号共享一张表，键里带目标，所以目标不同时互不影响）；**「频道 ↔ 群组 同内容」**（同一个运营方把同一条推广分别发到频道和它的群组）也只留**群组**那条 —— 正文一致 + 目标一致 + 两条分别来自 `channel` 与 `group` 才算同一条，群组那条后到时会把先前发出去的频道那条（含链接消息）撤回；只认「频道 + 群组」这一种组合，两个群组发的同内容帖子不合并、类型判不出来时也不合并 |
| `exclude_chats` | array | **账号级**排除的会话，写一次全部规则都不监听；格式同 sources。与全局那份（见下文「全局排除名单」）取**并集** |
| `rules[].id` | string | 唯一标识 |
| `rules[].sources` | array | 来源会话：`@username`、`chat_id`、`t.me/...` 链接。**空数组 = 监听全部** |
| `rules[].exclude_sources` | array | 只对**这一条规则**生效的排除来源 |
| `rules[].targets` | array | 目标会话，格式同 sources |
| `rules[].mode` | string | `forward`（原生转发，带「转发自」抬头）/ `copy`（**按 `file_id` 重新发送一条新消息**，不带来源标记，默认值）/ `text`（按模板重发纯文本）。⚠️ **`forward` 遇到受保护源会话时（Telegram 回 `CHAT_FORWARDS_RESTRICTED`）会自动改用复制**再发一次，不用手动改成 `copy`；那条消息的日志会显示 `mode=copy(降级)`。日志里的 `mode` 是**实际生效**值，还可能出 `copy(退化为转发·丢链接)` / `forward(降级·丢链接)`（复制也失败、退成转发 ⇒ **正文里没有原文链接**，同时打一条 WARNING） |
| `rules[].match.mode` | string | `regex` / `contains` / `exact` / `all` |
| `rules[].match.patterns` | array | 匹配模式（正则或关键词） |
| `rules[].match.exclude_patterns` | array | 排除模式 |
| `rules[].match.fields` | array | 匹配范围：`text` / `caption` / `buttons` |
| `rules[].template` | string | `text` 模式下的模板，可用变量见下文 |
| `rules[].include_source_link` | bool | 是否附带来源链接（默认 `true`）。**两种模式的加法不同**：`forward` 模式**不动**那条转发消息（Telegram 本来就拒绝编辑转发消息），在它**下方**单独补发一条只含 `🔗原文链接：<t.me 链接>` 的消息；`copy` / `text` 模式写进**当前消息**的正文/caption 末尾（前面空一行）。`copy` 的链接是**发送时就写进正文**的（按 `file_id` 重发：文本走 `send_message`、媒体走 `send_cached_media`、相册走 `copy_media_group`），**不是**事后编辑。⚠️ 正文/caption 超长时是**先给链接留出位置、再截断原文**，所以链接**不会被截断吃掉**。补链接失败只 warning，不影响转发本身 |
| `rules[].media_group` | bool | 是否聚合相册（攒齐后一次转发） |
| `rules[].delay` | number | 命中后延迟多少秒再发 |
| `rules[].min_interval` | number | 同一规则两次触发的最小间隔 |
| `recent_dedupe_limit` | int | 「最近已转发的内容」按**条数**保留多少条（默认 5）。命中新消息时先跟这些比，一致就跳过 |
| `recent_dedupe_window` | number | 同一层按**时间**保留多久（秒，默认 86400 = 一天）。与上面那条**取并集**，哪个更宽算哪个；两个都设 0 = 关掉这一层 |
| `rules[].notify` | bool | 转发后是否推 bot 通知 |

> 💡 「最近已转发的内容」判重**不只看哈希**：**同一条内容后面又加了些文字**也算重复
> （线上取证：`茶包影视-30-Register_UDMlfejslD` 发过一次，两分钟后
> `…UDMlfejslD yanpeihao816` 又发了一次，指纹不同就漏过去了）。
> 判据是「短的那条被长的那条整个包含 + 短的那条 ≥ 12 字 + 占长的那条 ≥ 60%」，
> 所以长帖里**引用**一小段（比如开奖公告里带上的那个码）不会被误判成同一条 ——
> 误判成重复 = 静默丢消息，比多转一条重得多。命中时日志里 `via=` 会写明是
> `完全相同` 还是 `加了些文字`。

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
> （面板：转发规则页 → 底部「各账号单独设置」→ 对应账号的「排除频道」）。
> 如果这个会话**所有账号**都该排除，写进下面那份全局名单，不用一个账号填一遍。

### 全局排除名单（`data/forward_excludes.json`）

账号级那两份名单只作用于**一个账号**。如果"这些群不要转、这些人不要转"对**所有**
账号都成立，就把它写进**全局**这一份 —— 它不在任何账号的 `config.json` 里，
所有账号共用一份：

```json
{
  "version": 1,
  "exclude_chats": [-1003932130542, "noisy_channel"],
  "exclude_users": [8817602576, "spammer"]
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `exclude_chats` | array | 排除的会话：这些群 / 频道来的消息，**任何账号、任何规则**都不转发 |
| `exclude_users` | array | 发送者黑名单：这些人发的、命中规则的消息不转发 |

面板：**转发规则页 → 顶部「全局排除（所有账号共用）」**。两个名单各一行，
回车添加、点 × 移除，写一次所有账号同时生效。

> 💡 **与账号级是并集，不是替换。** 全局那份回答"所有账号都要排除的"（那个刷屏频道、
> 那个转发机器人），账号级那份留给"只有这个号要排除的"（各账号的来源本来就不同）。
> 任一边写了都生效、两边都写都不漏；删掉全局里的一项，不影响任何账号自己加的那些。

> 🔴 **群 id 写进去之后，既不转发、也不会推给你。** 通知是转发成功之后才发的，
> 没有转发就没有通知 —— 这正是「我设置的群id就不要转给我了」要的效果。

> 💡 **改完立刻生效，不用重启账号。** 热重载同时盯着 `config.json` 和这份文件的
> mtime，任一变化就重读一次盘；改名单只动这一个文件，账号配置一个字节都不用碰。

> ⚠️ **文件写坏 = 按空名单继续跑，不会让转发停摆。** 代价不对称：少排除几条只是多转
> 了几条消息，而一份写坏的 JSON 让**所有**账号的转发都起不来是灾难性的。
> 读失败会通过 `GET /api/rules` 的 `global_excludes.load_errors` 报到面板上
> （状态里显示"名单文件读不到，当前按空名单运行"）——「没配过」和「读坏了」是两件事，
> 前者显示"未设置"，不会报警。
> **写入相反**：保存失败会直接报错，不会让你以为存上了（否则重启后名单消失，
> 表现就是"排除又失效了"，而且没有任何线索）。

> 💡 归一化与账号级完全一致（同一套规则）：`"-1003932130542"` 会存成数字
> `-1003932130542` —— 字符串不转成数字的话，引擎按数字 id 比对时永远匹配不上；
> `"@Foo"` 会去掉 `@` 并转小写存成 `"foo"`（比对时大小写不敏感）。

接口层：`GET /api/rules` 在**顶层**返回 `global_excludes`（含 `load_errors`，
不是每个账号各带一份）；`PUT /api/forward-excludes` 按 body 里出现的键**部分更新**
（只改 `exclude_chats` 不会把 `exclude_users` 覆盖掉），不认识的字段 / 空 body /
归一化不了的值都返回 `400`。

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

### used_codes —— 已经用掉的注册码不再转发

有些频道会把「注册码使用通知」也发到同一个来源里（内容形如
`ChaPanda-30-Register_AyqXyZ1234`）。这类通知本身命不中转发规则，但**同一个码**
过一会儿又会被完整发一遍 —— 不拦的话目标里就会出现一条已经用掉的码。
这一节让转发引擎**从通知里学会这些码**，之后凡是正文里带着这个码的消息一律不转发。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `enabled` | bool | 开关，默认 `true` |
| `notice_keywords` | array | 认定「这是使用通知」的关键词，默认 `["码使用"]`；一个都不出现就不学 |
| `notice_pattern` | string | 从通知里**取出码**的正则，默认取「使用」后面那串非空白字符 |
| `ignore_token_pattern` | string | **不需要学的形状**。默认 `^\d+-\w+$`：线上还有一种 `<数字>-<4位>` 形状的码（如 `7017826500-2cIEq8ZKmN`），它**从来命不中任何转发规则**，学它只会白占记忆、还可能误伤 |
| `min_visible` | int | 可见部分至少要有多长才认（默认 3）。通知里码的尾部常被打码，露一两位时几乎任何码都能碰巧对上 |
| `ttl` | number | 记住多久（秒，默认 3600） |
| `persist` | bool | 是否落盘（每个账号 `data/accounts/<账号>/used_codes.json`），默认 `true` —— 重启不该把「已用掉」这件事忘掉 |

> ⚠️ 判据是「学到的可见片段**出现在**新消息正文里」，所以它只会**少转**、不会误拦
> 正常内容 —— 但**太短的片段**（比如 `emby` 这种裸词）会误伤任何提到这个词的帖子，
> 所以记忆里既要求「能分出码名和码值」（含 `-` 或 `_`）也要求可见部分够长；
> 从磁盘读回来时会按当前策略清掉不合规的旧条目。
>
> 面板：转发规则页 → 底部「各账号单独设置」→ 对应账号的「已使用注册码」，可改上面这些字段，
> 并显示 `已知 / 已拦截 / 累计学到` 三个计数。

---

## notify — Bot 通知

```json
{
  "notify": {
    "enabled": true,
    "bot_token": "${TGA_BOT_TOKEN}",
    "chat_id": 123456789,
    "message_thread_id": null,
    "mode": "forward",
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
| `mode` | string | **`forward`**（默认：用 `forwardMessage` 转发目标频道里那条消息 —— **通知和频道里那条一模一样**，带「转发自」抬头；源会话禁止转发 / 内容受保护时自动降级 `copyMessage`）/ `copy`（只用 `copyMessage`，不带抬头）/ `text`（纯文本，媒体退化为文字说明） |
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
    "edit_max_age": 1800,
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
| `include_edited` | bool | 也处理消息**编辑**事件（有些 bot 先发消息、随后才编辑出按钮） |
| `edit_max_age` | number | `include_edited` 的**年龄闸门**：只处理"原消息发布时间"在这个秒数以内的编辑，默认 `1800`（30 分钟），`0` = 不限 |

### 为什么不点"老红包"

红包 bot 常常**反复编辑同一条消息**（把「已领 19/200 份」改成 20/200），而
`include_edited` 会把每次编辑都当成一个新事件。线上实测（2026-09-29，白嫖分享社）：

- 一条**长驻红包**（`message_id=352733`）8 小时里被处理了 **8 次**，全部是编辑事件；
- 我们 10:18 已经抢到 50 积分，之后 7 次点下去只换回「你已经领过这个红包啦」；
- 每次都要白等一个 `wait_timeout`，而且反复点同一条按钮本身就像在刷风控。

两道防线都在引擎里，不用额外配置：

1. **年龄闸门**（`edit_max_age`，默认 30 分钟）：几十分钟前的消息再被编辑，绝不可能是
   新红包 → 直接跳过，计入心跳的 `rp_stale_edit_skip`。想恢复老行为设 `0`。
   "*消息太老*"和"编辑"是两件事：闸门只管前者，因为「先发消息、几秒后编辑出按钮」这种
   bot 仍然要靠 `include_edited` 抓。
2. **定论记忆**：某条消息一旦判出「抢到 / 明确没抢到」就**再也不点**，并落盘到
   `data/accounts/<账号>/red_packet_settled.json`（心跳里的 `rp_settled` /
   `rp_settled_restored`）。

> 🔴 第 2 条**必须落盘**：它原本只在内存里，而改配置、部署都会重启账号。线上就是
> 11:16 判出「你已经领过」→ 13:50 一次重启（忘光）→ 14:23 又点一次；16:38~17:46
> 又重启 6 次 → 18:07 再点一次。重启是常态，所以「已经点过了」得活过重启。
>
> `unknown`（机器人没回显 / 回显看不懂）刻意**不算定论、也不落盘** —— 那可能只是网络
> 抖动，重试是有意义的；短时间内靠 60 秒的 `_seen` 窗口挡。

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
