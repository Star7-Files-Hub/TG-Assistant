# TG-Assistant

多账号 Telegram 助手：扫码登录、秒级正则转发、Bot 通知、自动抢红包。

基于 [kurigram](https://github.com/KurimuzonAkira/kurigram)（仍在维护的 Pyrogram 分支）编写，支持 Python 3.9–3.12。

## 为什么需要多账号？

Telegram 限制单账号最多加入 **500 个群+频道**。当你需要监听/转发超过 500 个会话时，就必须用多个账号分担。TG-Assistant 的设计核心就是让这件事变得简单：

- 每个账号独立会话、独立配置、独立日志
- 单账号挂了不影响其他账号
- 配置里写多少个 sources 都行，框架自动按账号分配监听
- `run` 不传 `-a` 则并发运行全部账号，每个账号各跑各的

## 功能

| 功能 | 说明 |
| --- | --- |
| 扫码登录 | 多账号、数据独立，支持两步验证 |
| 代理 | SOCKS5 / SOCKS4 / HTTP，国内机器必备 |
| 秒级转发 | 正则 / 包含 / 精确 / 全匹配，转发到指定频道 |
| Bot 通知 | 与转发内容完全一致（服务端 copyMessage） |
| 自动抢红包 | 按钮 / 口令双策略，成功识别 + 随机回复 |

## 快速开始

### Web 界面（推荐）

```bash
# 安装依赖
pip install -e ".[speed]"

# 启动 Web 控制台（默认只监听 127.0.0.1，仅本机可访问）
tg-assistant web --port 8080
# 打开 http://localhost:8080

# 要从别的机器访问：必须同时设置访问密钥，
# 否则拒绝启动 —— 没有密钥的控制台等于把账号和配置敞开给所有人。
tg-assistant web --host 0.0.0.0 --port 8080 --secret 'your-strong-secret'
# 密钥也可以走环境变量：
TGA_WEB_SECRET='your-strong-secret' tg-assistant web --host 0.0.0.0
```

首次访问会要求输入访问密钥，验证通过后写入 HttpOnly Cookie（有效期 7 天）。
脚本或命令行调用可以直接用 `Authorization: Bearer <secret>`。

Web 界面包含所有 CLI 功能：
- 📱 **扫码登录**：WebSocket 实时显示二维码
- 📊 **仪表盘**：账号状态、运行控制、实时日志
- 👥 **账号管理**：添加/删除/启用/禁用/设置代理
- ⚙️ **配置编辑**：在线编辑 JSON 配置，实时校验
- 📋 **实时日志**：WebSocket 流式日志，按级别过滤
- 🔍 **代理检查**：一键诊断代理连通性
- 💬 **通知测试**：发送测试通知验证配置

### Docker 部署

```bash
git clone https://github.com/Star7-Files-Hub/TG-Assistant.git
cd TG-Assistant

# 1. 配置环境变量
cp .env.example .env
# 编辑 .env，填入 api_id / api_hash（国内服务器还要填 TGA_PROXY）

# 2. 扫码登录（会显示二维码，用 Telegram 扫）
docker compose run --rm tg-assistant login -a main

# 3. 生成示例配置
docker compose run --rm tg-assistant config init -a main --example

# 4. 编辑转发规则
# 修改 data/accounts/main/config.json

# 5. 启动
docker compose up -d

# 查看日志
docker compose logs -f
```

### 一键脚本部署（裸机 / 虚拟机）

```bash
curl -fsSL https://raw.githubusercontent.com/Star7-Files-Hub/TG-Assistant/main/scripts/deploy.sh | bash
```

### 手动安装

```bash
git clone https://github.com/Star7-Files-Hub/TG-Assistant.git
cd TG-Assistant
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[speed]"

# 配置环境变量（cp 完记得填 api_id / api_hash，国内服务器还要填 TGA_PROXY）
cp .env.example .env

# 登录（会自动读取当前目录下的 .env）
tg-assistant login -a main

# 生成配置
tg-assistant config init -a main --example
# 编辑 data/accounts/main/config.json

# 启动
tg-assistant run
```

> `.env` 是从**当前工作目录**读取的，所以请在项目根目录下执行命令；
> 也可以设置 `TGA_ENV_FILE=/path/to/.env` 指定别的路径。

## 配置参考

详见 [docs/configuration.md](docs/configuration.md)。

核心结构：

```json
{
  "forward": {
    "enabled": true,
    "rules": [{
      "id": "rule-1",
      "name": "示例规则",
      "sources": ["@source_channel"],
      "targets": [-1001234567890],
      "mode": "copy",
      "match": {
        "mode": "regex",
        "patterns": ["金额[:：]\\s*([\\d.]+)"]
      }
    }]
  },
  "notify": {
    "enabled": true,
    "bot_token": "${TGA_BOT_TOKEN}",
    "chat_id": 123456789,
    "mode": "copy"
  },
  "red_packet": {
    "enabled": true,
    "strategy": "auto",
    "reply": {
      "enabled": true,
      "texts": ["谢谢老板", "xxlb"]
    }
  }
}
```

## 命令速查

```bash
tg-assistant web --port 8080         # 启动 Web 控制台
tg-assistant login -a main              # 扫码登录
tg-assistant accounts list              # 查看账号
tg-assistant config init -a main --example   # 生成配置模板
tg-assistant config validate -a main   # 校验配置
tg-assistant chats -a main             # 列出会话（找 chat_id）
tg-assistant proxy-check               # 诊断代理
tg-assistant notify-test -a main       # 测试通知
tg-assistant run -a main               # 启动
```

## 目录结构

```
data/
├── accounts.json              # 账号注册表
├── accounts/
│   └── main/
│       ├── main.session       # 登录会话
│       ├── config.json        # 账号配置
│       └── state.json         # 运行状态
├── logs/
│   ├── tg-assistant.log       # 主日志
│   ├── error.log              # 错误日志
│   ├── events.jsonl           # 结构化事件
│   └── accounts/
│       └── main.log           # 单账号日志
└── qr/                        # 二维码图片
```

## 日志

日志默认写到 `data/logs/`，按账号分文件，敏感信息（token、api_hash、session 等）自动脱敏。

调试时加 `--log-level DEBUG`：

```bash
tg-assistant -l DEBUG run -a main
```

## 常见问题

**Q: 国内服务器连不上 Telegram？**
A: 必须配置 SOCKS5 代理。先用 `tg-assistant proxy-check -p socks5://127.0.0.1:1080` 诊断。

**Q: 转发延迟高？**
A: 检查 `TGA_WORKERS`（建议 >= 4）；确认代理延迟低；查看日志里的 `pipeline_ms` 与 `handler_ms`。

**Q: 通知内容不一致？**
A: 默认 `notify.mode=copy` 用服务端 copyMessage，内容完全一致。如目标会话不支持 copy，会自动回退到纯文本。

**Q: 源群是受保护的，转发报 `CHAT_FORWARDS_RESTRICTED`？**
A: 受保护群/频道不允许「原生转发」，但允许「复制」。`mode="forward"` 的规则遇到这个错误会**自动改用复制**再发一次，不用手动改配置 —— 那条消息的日志里 `mode` 会显示 `copy(降级)`。想省掉「先失败一次」的那次 RPC，可以直接把规则的 `mode` 设成 `copy`。

**Q: 日志里的 `mode=` 和我在规则里配的不一样，怎么看？**
A: 日志打的是**实际生效**的模式，不是配置值 —— 因为一条消息可能走降级/兜底路径：

| 日志里的 `mode` | 含义 | 正文里有原文链接吗 |
|---|---|---|
| `forward` | 原生转发（链接补在**下方**一条独立消息里） | ✅ 下方那条 |
| `copy` | 按 `file_id` 重发新消息 | ✅ 正文末尾 |
| `text` | 按模板重发纯文本 | ✅ 正文末尾 |
| `copy(降级)` | `forward` 撞受保护源会话 → 改用复制 | ✅ 正文末尾 |
| `copy(退化为转发·丢链接)` | 复制失败 → 退成隐藏抬头的转发 | ❌ **丢了** |
| `forward(降级·丢链接)` | 降级去复制、复制也失败 → 又退成转发 | ❌ **丢了** |

⚠️ 带「丢链接」字样的两种情况会**同时**打一条 WARNING（`复制失败，退化为不带抬头的转发（原文链接会丢失）`）。
看到它就说明那条消息只有内容、没有来源链接。

**Q: `copy` 模式到底做了什么？为什么不能直接「转发但去掉来源」？**
A: `copy` 模式**按 `file_id` 重新发送一条全新消息**：文本走 `send_message`（保留粗体/链接等格式），媒体走 `send_cached_media`（服务端按 `file_id` 复用，不下载不上传），相册走 `copy_media_group`。**不能**用「转发 + 隐藏来源」代替 —— 那样发出来的是**转发消息**，而 Telegram **拒绝编辑转发消息**（`400 Bad Request: message can't be edited`），「先发出去、再补上原文链接」这条路根本走不通。重发拿到的是新消息，链接在发送时就写进正文，一次 RPC 完成。

**Q: `include_source_link` 在两种模式下表现一样吗？**
A: 不一样。**`forward`**：那条转发消息**原样不动**，在它**下方单独补发一条**只含 `🔗原文链接：<链接>` 的消息。**`copy` / `text`**：链接写进**当前这条消息**的正文末尾（前面空一行）。补链接失败只记 warning，不影响转发本身。

**Q: 消息很长的时候，链接会不会被截断掉？**
A: **不会** —— 实现上是**先给链接留出位置，再截断原文**（`truncate(base, limit - len(链接块))`）。顺序反过来的话，`truncate` 是从**末尾**砍掉再补「…（已截断）」，链接正好在末尾，就会被吃掉。⚠️ 这一点很容易写错，所以 `tests/test_forwarder.py::TestLinkSurvivesTruncation` 专门守着（覆盖正文 4096 / caption 1024 / 相册 caption / `text` 模式 3800 四条路径）。

**Q: `copy` 模式复制失败了怎么办？**
A: 会退化成「不带来源抬头的转发」（`forward_messages(hide_sender_name=True)`）把内容发出去，代价是**这一条带不上原文链接**，日志里会有 `复制失败，退化为不带抬头的转发（原文链接会丢失）` 的 warning。

**Q: 两个账号都在同一个群里，消息被转发了两次？**
A: 跨账号去重**默认开启**：同一条源消息发往**同一个目标**只允许一个账号发出去，去重窗口取该账号的 `dedupe_window`。去重键里带目标，所以两个账号的目标不同时**两条都会发**，不会互相吃掉。整张表在进程内共享，面板重启 runner 也不会被清空。

**Q: 同一个运营方把同一条推广分别发到频道和它的群组，目标里出现两份？**
A: **「频道 ↔ 群组 同内容」去重默认开启**，只保留**群组**那条（链接也是群组那条）。判定方式是**内容指纹 + 会话类型**：正文（`text` / `caption`）一样、目标相同、且两条分别来自 `channel` 与 `group` 时算同一条；窗口同样是 `dedupe_window`。

⚠️ 三点值得知道：

- **和先后顺序无关**。实测两种顺序都会出现（流光画廊那对是群组先到，秀儿那对是**频道先到**），所以群组那条后到时会**撤回**先前发出去的频道那条（含它下面补的链接消息）。撤回是尽力而为：目标里没有删除权限时只打 WARNING，群组那条照发（结果会多一条重复）。
  还有第三种情况：群组那条在**频道那条还没发完**的时候到达（`claim` 与真正发送之间隔着一个网络往返，线上实测 `pipeline_ms` 到过 4 秒）。这时频道那条发完后会**发现已被顶替并撤回自己刚发的**，最终同样只留群组那条。
- **只认「频道 + 群组」这一种组合**来决定「留群组还是留频道」。两个群组发的同内容帖子不在这里合并（判断不出该留谁），但**会**被下面那层「最近已转发的内容」拦掉。
- 被跳过的频道那条会**同时退还跨账号名额**，不会把那个键占死。

**Q: 还是有很多重复的？**
A: 前面两层都只认**消息 id**，而现实里的重复往往 id 完全不同（同一个运营方把同一段广告发到好几个群、隔几小时再发一遍）。所以还有第三层 —— **「最近已转发的内容」**（默认开启）：

- 命中新消息后，先和目标里**最近已转发的内容**比一比，**一样就跳过**；
- 范围是「最近 `recent_dedupe_limit` 条」（默认 **5**）与「`recent_dedupe_window` 秒内」（默认 **86400** = 一天）**取并集**，哪个更宽算哪个；
- 按**目标**分桶：同内容发到不同目标互不影响，不会互相吃掉；
- 两个都是 0 就关掉这一层。

⚠️ 两个例外不会拦：**相册**（同一组里的多条本来就是一个整体，caption 常常一模一样，按内容比会把整组砍成一条）；以及「群组顶替频道」时那条要发的群组消息（那时频道那条**已被撤回**，目标里其实没内容了）。

**Q: 如何多账号？**
A: 换 `--account` 重复登录即可，每个账号数据独立。`run` 不传 `-a` 则并发运行全部账号。

**Q: 单账号监听上限是多少？**
A: Telegram 限制单账号最多加入 **500 个群+频道**。超过就需要多账号分担——每个账号的 `config.json` 里只写自己负责的 sources，框架自动按账号分配监听。详见 [docs/configuration.md](docs/configuration.md)。

## License

MIT
