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

> ⚠️ **本节有一处方向性调整，以本节为准**：页面最后**不是**「照抄 red_packet.html
> （顶部选账号 + 该账号的任务列表）」，而是**对齐 rules.html 的「任务维度」** ——
> 页面一次渲染**所有**账号，「这条任务跑在哪些账号上」由卡片里的「监听账号」勾选表达。
> 原因：一条抢注任务经常要扇出给多个账号，按账号渲染会让同一条任务在页面上出现 N 次，
> 用户数不清自己有几条。规则页就是这么改的，原话：
> 「展示以及任务是以任务为维度，而不是以账号，同样的规则不做二次展现」。

### API（api.py，仅 reg_grab 段：约 1240–1415 与 1420–1660）

账号维度（原有，保留）：
- `GET/PUT /config/{name}/reg_grab`：`_REG_GRAB_TASK_READONLY = ("ready","problem","in_window")`
  在 `RegGrabConfig.model_validate` 之前从每个 task 里剔除，顶层 `server_now` 同样剔除；
  `GET` 给每条任务附 `ready`/`problem`/`in_window`、顶层附 `server_now`。
- `PUT /config/{name}/reg_grab/enabled`：总开关，开启时要求**至少有一条 ready 的任务**
  （宽松策略，对齐 red_packet；不再看扁平 detect/steps）。
- `POST /config/{name}/reg_grab/test_notify`、`POST /reg_grab/test_extract`：保持不动。

任务维度（新增 4 个，对齐 `/api/rules*`）：
- `GET  /api/reg_grab/overview` → `{accounts: [{name, username, display_name, enabled,
  running, session_exists, reg_grab_enabled, max_concurrency, tasks: [...]}], server_now}`。
  **一次**拿全所有账号，页面不再逐账号 N+1（否则账号 A 落盘、B 还没落盘时会把
  "写了一半的状态"当快照渲染出来）。
- `POST /api/reg_grab/tasks` body `{task, accounts}` → `{saved, conflicts, task}`。
  `accounts` 缺省/空 = 扇出**全部**账号；同 id 已存在的账号**跳过**并计入 `conflicts`
  （全冲突才 409 —— 否则「发给全部账号」在某个号手工加过时就完全用不了了）；
  一个账号都没有 400；任务本身非法 400（"任务校验失败"）。
- `PUT  /api/reg_grab/tasks/{task_id}` body `{task}`（裸 body 也认）→ `{updated, missing}`。
  不带 `accounts` = 改**所有**拥有者里的那一份（发 N 个 per-account PUT 会在第 3 个失败时
  留下"一半账号改了、一半没改"，用户还看到报错）；改成别的账号已占用的 id ⇒ 409；
  哪儿都没有这条任务 ⇒ 404。
- `DELETE /api/reg_grab/tasks/{task_id}?accounts=`（逗号分隔）→ `{removed}`。
  带上 = **只从这些账号**移除（卡片上取消勾选）；不带 = 从**所有**账号删掉（卡片删除键）；
  哪儿都没有 ⇒ 404，不假装成功。
- id 留空由服务端生成（`RegGrabConfig._assign_ids` + `_auto_task_id`）：优先取名称里的
  ASCII 片段，纯中文名回落 `task-xxxxxxxx`，同一次保存内保证不撞号。

### 页面（reg_grab.html）
- 顶部**没有**账号下拉（`switchAccount()` 已删），只有「刷新」+「新建任务」；
  `collectTasks()` 把「账号 → 任务」**转置**成「任务 → 账号」，同一条任务只渲染一张卡片。
- 任务卡片：启停（PUT，改所有拥有者）／编辑／删除／**监听账号勾选**
  （勾 = `POST /api/reg_grab/tasks {accounts:[name]}`；取消 = `DELETE ...?accounts=name`；
  取消**最后一个**账号会二次确认 —— 那等于把这条任务删掉）。卡片直接复用 rules.html
  那套 `.rule-*` 样式，不复制一份 CSS。
- 卡片提示分三类、两种颜色：各账号副本**分叉**（有人手改过单个账号的 config.json）、
  缺提取正则／缺步骤（红 —— 真错了）；当前不在时段（黄 —— 到点就好，不是配错）。
- 弹窗：基本信息（id 可留空，编辑时锁住）／**应用账号**（不勾 = 全部账号；编辑时显示
  勾选**差集**"新增监听 / 取消监听"）／监听会话／识别正则／步骤链／时段／高级设置。
  编辑保存 = `PUT`（所有现有副本）+ `POST`（新勾的账号）+ `DELETE`（取消勾选的账号）——
  只 PUT 的话新勾的账号"勾上了但什么都没发生"（服务端算 missing，不写也不报错）。
- 账号级设置（抢注总开关／并发上限／试发通知）收进「各账号单独设置」折叠节
  `#rg-account-settings`：并发上限走 `PUT /api/config/{name}/reg_grab`，**必须原样带上
  该账号的 tasks**（那个端点不带 tasks 就是"清空任务列表"）；`test_notify` 也从弹窗
  挪到这里（通知走的是账号的机器人，跟哪条任务无关）。
- 卡片/列表上的按钮、开关、勾选一律 `data-*` + 事件委托，**不往内联 handler 里拼
  `task.id`**（id 由用户自由输入，含一个单引号就能截断属性；HTML 转义救不了内联 handler）。
- 标签输入沿用 rules.html 的经验：state 键用下划线字段名、DOM id 用连字符、`tagDomId`
  桥接；并且挡掉中文输入法**选字**那一下的 `keydown(Enter)`（`e.isComposing || keyCode===229`）。
- 步骤链编辑器保留原实现（含「点开深链」`open_link` 的 `link_pattern` / `account` /
  `max_per_minute`）。

测试：`tests/test_web_pages.py` 5 条抢注页用例（52 条静态断言：DOM 契约、**没有**账号下拉、
4 个端点都被调用、`encodeURIComponent`、data-* 委托、只读字段剥离、并发保存带 tasks、
输入法回车）；`tests/test_web_api.py` 9 条任务维度接口用例（overview 覆盖每个账号 /
POST 扇出与 conflicts / 409 / id 生成 / PUT 所有拥有者与裸 body / 404 / DELETE 单账号与全删 /
只读字段往返 / 400 与空注册表）；`tests/test_web_runtime.py` 一条 feature flags 用例。

⚠️ `FEATURE_PAGES` 里的 `"reg_grab.html": "reg_grab"` 已**移除**（rules.html 同样不在表里）：
那条守卫查的是「功能页的账号下拉不能无脑选第一个」，而任务维度页面**根本没有**账号下拉，
这个不变式在它身上已不成立；等价的「每个账号都必须渲染」由 `renderAccountGroup` 的断言接手。

---

## 四、验证（Lead 统一在服务器上跑，勿改生产）
本地无 pytest/pydantic/pyrogram。tar+base64 单流打包源码 → ssh 进 `/tmp/tga-verify` → `.venv/bin/python -m pytest tests/ -q`。
基线：**927 passed / 13 skipped / 0 failed**。改造后须 ≥ 此基线（新增用例只增不减）。

## 五、写作用域（互斥）
- Lead：`tg_assistant/config.py`、`tests/test_config.py`
- engine：`tg_assistant/reg_grab.py`、`tests/test_reg_grab.py`
- web：`tg_assistant/web/routers/api.py`（仅 reg_grab 段）、`tg_assistant/web/templates/reg_grab.html`、
  `tests/test_web_api.py`、`tests/test_web_pages.py`、`tests/test_web_runtime.py`

---

## 六、落地状态（本次改造收尾时）
- **§一 配置层**：已落地并提交。
- **§二 引擎层**：还差「点开深链」`open_link` 的收尾 —— `tests/test_reg_grab.py::TestOpenLinkStep`
  这 4 条仍红：
  - `test_clicks_the_link_found_in_the_message`
  - `test_fourth_click_within_a_minute_gives_up_and_notifies`
  - `test_wait_reply_after_open_link_catches_the_bot_answer`
  - `test_skipped_step_does_not_block_the_rest_of_the_chain`

  断言都是「点完了 `{code}`」发去了 chat id（`-1001234500000`）而不是 `@testbot`，
  即 open_link 那一步的目标会话解析还没对。所以 engine 侧文件（`config.py` /
  `reg_grab.py` / `test_reg_grab.py`）**暂时没有提交**。
- **§三 API + 页面**：已落地。远端 `tests/test_web_api.py tests/test_web_pages.py
  tests/test_web_runtime.py -q` ⇒ **216 passed / 13 skipped / 0 failed**；
  全量 `tests/ -q` ⇒ **1328 passed / 13 skipped / 4 failed**（4 条**全部**是上面那批 `TestOpenLinkStep`，
  与 web 层无关）。
- 抢注页 / 样式 / 页面与接口测试这一批归 teammate「web-ui-dev」，与 engine 侧那批一起提交。
