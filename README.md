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

### Docker 部署（推荐）

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

# 登录
tg-assistant login -a main

# 生成配置
tg-assistant config init -a main --example
# 编辑 data/accounts/main/config.json

# 启动
tg-assistant run
```

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

**Q: 如何多账号？**
A: 换 `--account` 重复登录即可，每个账号数据独立。`run` 不传 `-a` 则并发运行全部账号。

**Q: 单账号监听上限是多少？**
A: Telegram 限制单账号最多加入 **500 个群+频道**。超过就需要多账号分担——每个账号的 `config.json` 里只写自己负责的 sources，框架自动按账号分配监听。详见 [docs/configuration.md](docs/configuration.md)。

## License

MIT
