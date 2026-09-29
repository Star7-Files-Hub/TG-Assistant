# 抢注（reg_grab）多任务化改造设计（对齐抢红包 red_packet）

> 目标：把「抢注」从**单任务扁平配置**改成**多任务**结构 —— 像转发/抢红包那样，
> 单个任务单个任务地添加，每个任务各自监听多个群组、各自一套识别正则 + 步骤链 + 时段。
> **抢红包（red_packet）已经是多任务，本次改造以它为逐层参照。** 不要改动 red_packet。

参照实现（逐层照抄改名）：
- 配置：`RedPacketConfig` / `RedPacketTask` + `_migrate_legacy_red_packet`（`tg_assistant/config.py`）
- 引擎：`tg_assistant/red_packet.py`（`PreparedTask` / `RedPacketHunter._match` 首命中）
- API：`tg_assistant/web/routers/api.py` 里 `/config/{name}/red_packet*`
- 页面：`tg_assistant/web/templates/red_packet.html`（任务卡片列表 + 弹窗编辑）
- 测试：`tests/test_config.py::TestRedPacket*`、`tests/test_red_packet.py`、
  `tests/test_web_api.py`、`tests/test_web_pages.py`、`tests/test_web_runtime.py`

---

## 一、配置层（config.py）— Lead 负责，最先落地（其余层依赖它）

### 1. 新增 `RegGrabTask(StrictModel)` —— 把下列「每任务」字段从 `RegGrabConfig` 挪进来
每个任务字段（对齐 `RedPacketTask` 的写法：`id`/`name`/`enabled` + 归一化校验 + `label`/`ready`/`problem`）：
- `id: str`（非空，去空白；校验器同 RedPacketTask）
- `name: Optional[str] = None`
- `enabled: bool = True`
- `chats: list[ChatRef] = []`（监听会话，可多群；空=全部）
- `exclude_chats: list[ChatRef] = []`
- `detect: RegGrabDetect`（保持原类不动）
- `steps: list[RegGrabStep] = []`
- `delay: float = 0.5 (0~60)`
- `jitter: float = 1.5 (0~10)`
- `code_ttl: float = 3600 (0~86400)`
- `notify: bool = True`
- `include_edited: bool = True`
- `window: RegGrabWindow`（保持原类不动；**时段变成每任务**）
- 归一化：`chats`/`exclude_chats` 用 `_normalize`（同 RedPacketTask）

`RegGrabTask.ready`（**不抛异常**，对齐 RedPacketTask.ready 的理由注释）：
```
enabled 不看；返回「配好了没有」= 有 detect.code_pattern 且至少一条 step
```
`RegGrabTask.problem`：一句话说清缺什么（没填提取正则 / 没有步骤）。
`RegGrabTask.in_window` 属性 = `self.window.contains()`（供 API 每任务现算）。

### 2. `RegGrabConfig` 瘦身成账号级容器（对齐 RedPacketConfig）
保留/新增：
- `enabled: bool = False`
- `tasks: list[RegGrabTask] = []`
- `max_concurrency: int = 1 (1~20)`  ← **账号级资源上限，留在外层**
- `@model_validator(mode="before")` → `_migrate_legacy_reg_grab`
- `@model_validator(mode="after") _unique_ids`（重复 id 报「抢注任务 id 重复: ...」）
- `active_tasks` 属性（跳过 disabled）
- `watched_chats` 属性（并集；**任一任务留空 ⇒ 返回空=全监听**，逻辑照抄 RedPacketConfig.watched_chats）
- `include_edited` 属性（any 语义）
- **删除**原来的 `ready`/`in_window` 属性（迁到任务级）。

### 3. 迁移函数 `_migrate_legacy_reg_grab(data)`（照抄 `_migrate_legacy_red_packet` 思路）
- 旧字段清单 `_REG_GRAB_TASK_FIELDS = ("chats","exclude_chats","detect","steps","delay","jitter","code_ttl","notify","include_edited","window")`
- `mode="before"`：dict 里出现任一旧字段就收进 `{"id":"default","name":"默认任务", ...}` 一条任务。
- 已有 `tasks` 键或空 `{}` 不动。**服务器现网 config.json 是旧扁平结构，必须无缝迁移，否则整份账号配置加载失败。**

### 4. config 测试（tests/test_config.py，Lead 负责）
照抄 `TestRedPacketConfig` / `TestRedPacketLegacyMigration` 改成 reg_grab：
- defaults / duplicate ids / active_tasks skips disabled / watched_chats union / watched_chats empty-when-any-all / include_edited any
- flat→one task / new shape left alone / empty 不造任务 / AccountConfig 装旧文件 / round-trip 稳定（旧字段不再写出）

---

## 二、引擎层（reg_grab.py）— teammate「engine」

照抄 red_packet.py 的多任务骨架：
- 新增 `PreparedTask`（含 `config: RegGrabTask` + 预编译的 text_patterns/code_pattern/used_pattern/steps 按钮与回执正则/RefSet(chats)/RefSet(exclude_chats)）。`build(cls, task)` 工厂。
- `RegGrabHunter.__init__`：`self.prepared = [PreparedTask.build(t) for t in cfg.active_tasks]`；
  账号级资源留在 hunter：`self._semaphore = Semaphore(cfg.max_concurrency)`、账号级 code 去重 `self._seen_codes`（键=code；窗口用命中任务的 `code_ttl`）、`self._used`（used 通知前缀表，账号级共享）。
- `_watch_chats()`：所有任务 `chats` 的并集 + 各任务 step.chat（照旧逻辑但跨任务聚合）；任一任务全监听 ⇒ 全监听。
- `_match(message, chat_id) -> Optional[(PreparedTask, code)]`：**按任务顺序取第一个命中**（对齐 red_packet `_match` 的「只取第一个」注释与理由：同一条码被多任务命中只处理一次）。命中判断走每任务的 chats/exclude/detect。
  - 🔴 **命中判断含 `self._in_window(task)`：时段外的任务不算命中，直接顺延给后面的任务。**
    **不要**写成「先取首命中、再判时段」—— 那样排在前面的任务只要 `chats` 覆盖到该消息，
    就能在自己时段外把码**吃掉**，后面本处于自己时段内的任务永远轮不到（用户为夜班单独
    建的那条任务会静默失效）。这是「时段外的目击不该占用机会」的跨任务版本。
  - 另设 `_match_ignoring_window(message, chat_id)`（第一条不管时段的命中）与
    `_report_out_of_window(...)`：仅当**没有任何任务可动手**、且原因是「命中但都不在时段内」时，
    记一次 `outside_window`（全局 + 第一条命中的任务）并打原日志；**不占** `_seen_codes` 同码名额。
  - `_should_grab()` 兼容接口走 ignoring-window 版本（它只回答「正则提不提得出码」，不看时段）。
- 时段/延迟/jitter/notify/include_edited/used 判定全部改成**用命中任务的** `task.config.*`；`_in_window` 接收 task（保留可注入 `self._now()`）。`outside_window` 等 stats 建议按任务累计：`self.task_stats[task.id][...]`（对齐 red_packet 的 `task_stats`）。
- `register()`：`include_edited` 用 `cfg.include_edited`（any）决定是否注册 edited handler；日志按任务打印（tasks 数、labels、各自 window/code_pattern）。对未 ready 的任务打 warning（照抄 red_packet register 的逐任务 warning）。
- `enabled` = `cfg.enabled`；`watched_chats()` = `cfg.watched_chats`。

引擎测试（tests/test_reg_grab.py，同一 teammate）：把现有 67 处单任务用例迁移到「任务」维度；新增：多任务首命中、不同任务不同时段/步骤、watched_chats 聚合、disabled 任务不触发、同码跨任务只处理一次。保持既有 used_pattern / visible_code / 步骤链行为用例（挪到 task 内）。

---

## 三、API + 页面 — teammate「web」

### API（api.py，仅 reg_grab 段落 ~892–1000）
对齐 red_packet 三个端点：
- `GET /config/{name}/reg_grab`：返回 `config.model_dump()`；给每个 task 附 `ready`/`problem`/`in_window`；顶层附 `server_now`（`datetime.now().strftime("%H:%M")`）。
- `PUT /config/{name}/reg_grab`：`_REG_GRAB_TASK_READONLY = ("ready","problem","in_window")` 从每个 task 里剔除后再 `RegGrabConfig.model_validate`。顶层 `server_now` 也要剔除。
- `PUT /config/{name}/reg_grab/enabled`：总开关。开启时的校验改为**至少有一条 ready 的任务**（对齐 red_packet enabled 的宽松策略；不要再看扁平 detect/steps）。
- `POST .../reg_grab/test_notify` 保持不动（账号级）。

### 页面（reg_grab.html）
照抄 red_packet.html 的任务卡片列表 + 弹窗编辑交互：
- 顶部：账号选择 + 总开关 + 并发上限（账号级）+「新增任务」按钮 + 任务卡片列表 `#rg-task-list`。
- 弹窗：把现有的「监听会话 / 识别正则 / 步骤链 / 时段 / 延迟&jitter&code_ttl&notify&include_edited」表单搬进**单任务弹窗**，多出 `id`/`name`/`enabled`。
- `loadTasks/renderTasks/renderTaskCard/openTaskModal/collectTask/saveTask/toggleTask/deleteTask/saveConcurrency/toggleRegGrab` 逐个对齐 red_packet.html 的同名函数。
- 标签输入沿用 rules.html 的经验：**state 键用下划线字段名，DOM id 用连字符**，用 `tagDomId` 桥接（避免回车加不进）。
- 步骤链编辑器保留现有实现，作为弹窗内的一个区块。

页面/运行时测试：`tests/test_web_pages.py`、`tests/test_web_runtime.py`、`tests/test_web_api.py` 里 reg_grab 相关用例改成多任务契约（GET 返回 tasks[].ready、PUT 往返、enabled 校验、页面含任务列表 DOM）。

---

## 四、验证（Lead 统一在服务器上跑，勿改生产）
本地无 pytest/pydantic/pyrogram。tar+base64 单流打包源码 → ssh 进 `/tmp/tga-verify` → `.venv/bin/python -m pytest tests/ -q`。
基线：**927 passed / 13 skipped / 0 failed**。改造后须 ≥ 此基线（新增用例只增不减）。

## 五、写作用域（互斥）
- Lead：`tg_assistant/config.py`、`tests/test_config.py`
- engine：`tg_assistant/reg_grab.py`、`tests/test_reg_grab.py`
- web：`tg_assistant/web/routers/api.py`（仅 reg_grab 段）、`tg_assistant/web/templates/reg_grab.html`、
  `tests/test_web_api.py`、`tests/test_web_pages.py`、`tests/test_web_runtime.py`
