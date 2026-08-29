# 果壳琴房预约 QQ Bot v3.1

面向果壳吉协琴房预约场景的可维护版本。目前唯一外部接口是 QQ Bot，但核心业务不依赖 QQ SDK，可在后续直接增加网站、管理后台或 NLP Parser。

v3.1 新增：

- `/查询` 与 `/空闲` 支持单日、偏移范围和绝对日期范围，一次最多 7 天；
- 查询缺省范围可按角色配置，例如 user 当天、band 两天、admin/owner 七天；
- 查询结果优先生成时间轴图片，并通过 QQ 官方本地文件分片接口上传；
- 图片渲染、浏览器或上传失败时自动发送原有文字结果；
- 数据库结构不变，从 v3.0 升级无需迁移数据或重新绑定用户/群。

## 这次重构解决了什么

- 指令解析、日期/房间解析、权限规则、数据库事务和中文回复完全分层；
- 系统用户、房间和预约使用稳定内部 ID，不再把 QQ `member_openid` 当业务主键；
- `+0/+1/+2` 在 22:00 业务日边界上统一解析为绝对日期；
- 时间只允许整点或半点，支持 `21`、`21.5`、`21:00`、`21:30`；
- 空闲检查、每日总量检查和写入处于同一个 `BEGIN IMMEDIATE` 事务；
- 玉泉路 1.5 小时限制同时约束单次时长与当日累计时长；
- 三个站点不再因为“默认数据库”而串库；
- 旧 SQLite 表会被导入新表，但旧表不删除；
- 旧 `group_mappings.json` 会导入 `data/control.db`；
- 旧“超前预约/超前查询/超前取消”不再维护第二套逻辑，统一使用 `+N`。

## 请求流水线

```text
QQ SDK
  ↓ 可信 QQ 身份、群 ID、接收时间
QQ Adapter
  ↓
QQCommandParser        文本 → ParsedIntent
  ↓
CommandResolver        日期表达式/房间名 → 绝对日期/room_id/TimeRange
  ↓
Dispatcher
  ↓
BookingApplication     权限、业务日、时长和功能开关
  ↓
SQLiteRepository       事务内冲突检查与数据修改
  ↓
OperationResult
  ├─ QQPresenter              结构化结果 → QQ 中文文字（也是兜底）
  └─ ScheduleImageRenderer    结构化结果 → HTML → PNG
       ↓
     QQMediaUploader          upload_prepare → PUT → part_finish → files
```

Parser 不读取数据库，不取得用户身份，也不能直接写预约。用户 ID 来自 QQ 身份映射；动态冲突校验在数据库写事务内完成。

## 目录

```text
.
├── main.py
├── configs/
│   ├── yqh.yaml
│   ├── yql.yaml
│   └── zgc.yaml
├── qqbot/
│   ├── domain/                 # 纯领域类型、命令和错误
│   ├── application/            # Resolver、应用服务、Repository 端口
│   ├── infrastructure/         # YAML、SQLite、群绑定
│   ├── interfaces/qq/          # QQ Parser、Presenter、Client、媒体上传
│   └── presentation/           # 时间轴数据、HTML 模板和 PNG 渲染
├── scripts/
│   ├── migrate.py              # 备份并迁移旧数据库
│   └── doctor.py               # 只读健康检查
├── tests/
├── deploy/qqbot.service.example
└── docs/
```

## 环境要求

- Python 3.11 或更新版本；
- Linux 推荐使用 systemd；
- QQ 开放平台机器人凭证；
- SQLite 数据库必须位于本机磁盘，不要放在 NFS/SMB 网络盘上。

## 全新安装

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

设置凭证：

```bash
cp .env.example .env
```

`.env`：

```dotenv
QQBOT_APPID=你的AppID
QQBOT_SECRET=你的AppSecret
QQBOT_OWNER_EXTERNAL_ID=群主QQ的member_openid（初始 owner；本地填写，不要提交到仓库）
PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers
```

首次启用图片查询时安装 Chromium。先导入 `.env`，可确保安装位置与运行时一致：

```bash
set -a
source .env
set +a
.venv/bin/python -m playwright install chromium
```

Debian/Ubuntu 如果提示缺少系统库，再由有 sudo 权限的维护者执行：

```bash
sudo .venv/bin/python -m playwright install-deps chromium
```

直接在 Shell 启动：

```bash
.venv/bin/python main.py
```

首次在群里绑定站点：

```text
#绑定配置 yqh
#绑定配置 yql
#绑定配置 zgc
```

只有对应站点的 `owner` 可以执行绑定。

## 从 v3.0 升级到 v3.1

数据库 schema 没有变化，用户身份、角色、预约数据和 `data/control.db` 中的群绑定都可直接沿用；不需要运行迁移脚本，也不需要重新执行 `/绑定` 或 `#绑定配置`。

1. 停止旧 Bot；
2. 备份原项目，至少保留 `data/`、`.env` 和改过的 `configs/`；
3. 用 v3.1 代码替换程序文件，保留上述数据和配置；
4. 在 `.env` 增加可选的 `PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers`；
5. 更新依赖并安装浏览器；
6. 运行健康检查和测试后启动。

```bash
set -a
source .env
set +a
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m playwright install chromium
.venv/bin/python -m scripts.doctor
.venv/bin/python -m pytest
.venv/bin/python main.py
```

若暂时不想安装 Chromium，可在每个站点 YAML 中设置 `query.image_enabled: false`；查询会始终使用文字输出。

## 从 v2/旧表升级到 v3

不要在旧 Bot 仍运行时迁移。

1. 停止旧 Bot；
2. 把原来的 `data/` 和 `group_mappings.json` 放在新项目根目录；
3. 检查三个 YAML 中的数据库路径与旧路径一致；
4. 不要随意修改已经投入使用的 `site_id` 和 `rooms[].id`；
5. 运行迁移；
6. 执行健康检查和测试；
7. 再启动新 Bot。

```bash
.venv/bin/python -m scripts.migrate
.venv/bin/python -m scripts.doctor
.venv/bin/python -m pytest
```

迁移脚本会在每个旧数据库旁创建：

```text
piano_room_yql.db.pre_v3_YYYYMMDD_HHMMSS.bak
```

新表统一使用 `app_` 前缀。旧的 `users`、`admins`、`reservations`、`weekly_routines` 和 `locked_slots` 不会被删除。

注意：新版本开始产生预约后，这些数据只写入 `app_reservations`。因此旧程序不会看到新版本运行期间产生的预约。回滚演练应在开放用户使用前完成。

更完整的迁移与回滚说明见 `docs/MIGRATION.md`。

## 普通指令

所有普通指令都可带 `/`，也兼容不带 `/`。

| 功能 | 指令 |
| --- | --- |
| 绑定 | `/绑定 张三 2024K8009926001` |
| 预约 | `/预约 303 21-22.5` |
| 提前预约 | `/预约 玉泉路琴房 21-22.5 +1` |
| 取消 +0 全部预约 | `/取消` |
| 取消 +1 全部预约 | `/取消 +1` |
| 取消指定时段 | `/取消 玉泉路琴房 21-22.5 +1` |
| 查询单日占用 | `/查询 [琴房] [+0/+1/...]` |
| 查询范围占用 | `/查询 [琴房] +0~+6` |
| 绝对日期范围 | `/查询 [琴房] 2026-08-10~2026-08-16` |
| 查询单日空闲 | `/空闲 [琴房] +1` |
| 查询范围空闲 | `/空闲 [琴房] +0~+6` |
| 查询个人后续预约 | `/查询个人` |

只有一个房间的站点可以省略房间名：

```text
/预约 7-8.5 +1
```

多个房间的雁栖湖必须指定房间。

### 日期规则

- 22:00 以前，`+0` 是当前自然日；
- 22:00 以后，`+0` 是下一个自然日；
- `+1/+2` 始终相对于当前业务日；
- 查询可写单个日期，也可写 `开始~结束`，首尾都包含在结果中；
- 范围两端必须同为 `+N` 或同为 `YYYY-MM-DD`，不能混写；
- 一次查询最多包含 7 个日期；
- 绝对日期不受 22:00 边界影响；
- 数据库保存绝对日期，不进行每日“后天搬到明天”的数据移动。

示例：

```text
/查询
/查询 303 +1
/查询 303 +0~+6
/查询 2026-08-10
/查询 玉泉路琴房 2026-08-10~2026-08-16
/空闲 +0~+2
```

没有显式写日期时，按当前用户角色读取站点配置。默认配置为：

| 角色 | `/查询`、`/空闲`缺省范围 |
| --- | --- |
| user | `+0~+0` |
| band | `+0~+1` |
| admin | `+0~+6` |
| owner | `+0~+6` |

显式输入最多 7 天的范围不改变预约权限：能看到某天，不代表能预约某天。

### 查询图片

普通 `/查询` 与 `/空闲`优先返回时间轴图片。图片在本机通过 Jinja2 + Playwright 生成，不需要公网图床；随后按 QQ 官方协议进行 `upload_prepare`、分片 PUT、`upload_part_finish`、`/files` 合并，并用返回的 `file_info` 发消息。

以下任一环节失败都会记录日志并回退到文字，不会影响预约和取消：

- Chromium 未安装或无法启动；
- HTML 截图失败；
- QQ 预上传、分片上传、确认或合并失败；
- 富媒体消息发送失败。

详细实现和排错见 `docs/QUERY_IMAGES.md`。

### 权限规则

玉泉路和中关村默认配置：

| 角色 | 可预约偏移 |
| --- | --- |
| user | `+0` |
| band | `+0/+1` |
| admin | `+0/+1/+2` |
| owner | `+0/+1/+2` |

雁栖湖 `advance_booking: false`，所有角色只能预约 `+0`。查询和取消仍可指定已经存在的 `+1/+2` 数据。

## 管理指令

| 功能 | 指令 |
| --- | --- |
| 任命角色 | `#添加管理 李四 band` |
| 撤销角色 | `#删除管理 李四` |
| 转让群主 | `#转让群主 王五` |
| 强制取消 | `#取消 303 10-11 +1` |
| 按绝对日期强制取消 | `#取消 303 10-11 2026-08-10` |
| 清空日期 | `#清空预约 +1` 或 `#清空预约 2026-08-10` |
| 撤销最近一次清空 | `#撤销清空 +1` |
| 查询任意日期 | `#查询 2026-08-10 [琴房]` |
| 添加周常 | `#添加周常 周一 303 21-23 合唱团` |
| 删除周常 | `#删除周常 周一 303 21-23` |
| 查询周常 | `#查询周常 [周一]` |
| 手动播报次日周常 | `#播报周常` |
| 单日临时锁定 | `#锁定 303 19-21 2026-09-10 活动` |
| 解除锁定 | `#解锁 303 19-21 2026-09-10` |
| 备份用户 | `#备份用户` |
| 恢复用户 | `#恢复用户` |

周常和播报受站点功能开关控制。

## 定时主动播报

按站点功能开关在 `on_ready` 挂载（各站可独立启停，详见开发手册 5.2）：

| 时间 | 内容 | 开关 |
| --- | --- | --- |
| 每天 `booking.routine_broadcast.time`（默认 21:00） | 从明天起 `days` 天（默认 1，可配 1～7）的周常占用（图片，失败回退文字） | `features.broadcast` 且 `features.weekly_routine` |
| 每天 `silent_period.start`（默认 22:00） | 系统时间文字报时（抢琴房对时） | `features.clock_announce` |
| 各站静默期结束 `silent_period.end`（如 22:15） | 次日预约情况（图片，失败回退文字） | `features.silent_end_report` |

播报推送到 `data/control.db` 中该站点绑定的全部群；图片链路失败自动回退文字。播报时刻全部由 YAML 配置驱动，改配置即生效。

## 配置

一个站点对应一个 YAML。关键字段示例：

```yaml
bot_id: yql
site_id: site-yql

rooms:
  - id: yql-main
    name: 玉泉路琴房
    aliases: [玉泉路]

booking:
  business_day_boundary: "22:00"
  advance_offsets:
    user: 0
    band: 1
    admin: 2
    owner: 2
  limits:
    regular:
      max_single_hours: 1.5
      max_daily_hours: 1.5

query:
  max_range_days: 7
  image_enabled: true
  default_ranges:
    user: [0, 0]
    band: [0, 1]
    admin: [0, 6]
    owner: [0, 6]
```

`query.default_ranges` 的两个数字是含首尾的业务日偏移；每个范围长度不得超过 `max_range_days`，而 `max_range_days` 本版本限制在 1～7。`rooms[].name` 可以调整展示名称，`aliases` 可以继续增加；已经写入数据库后不要修改 `rooms[].id`。`site_id`、房间 ID、内部用户 UUID 和预约 UUID 是系统稳定标识。

## 数据库与并发

数据库启用：

```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=10000;
BEGIN IMMEDIATE;
```

预约事务依次完成：

1. 获取 SQLite 写锁；
2. 读取普通预约、临时锁定和周常；
3. 计算可预约片段；
4. 检查该用户当天累计时长；
5. 写入预约片段；
6. 提交。

两个用户同时抢同一时段时，不会同时读到同一份空闲状态。

主要新表：

| 表 | 用途 |
| --- | --- |
| `app_users` | 内部用户 UUID 与资料 |
| `app_identities` | QQ 等外部身份到内部用户的映射 |
| `app_roles` | 用户在站点内的角色 |
| `app_rooms` | 稳定房间 ID |
| `app_reservations` | 预约与软删除信息 |
| `app_weekly_routines` | 周常占用 |
| `app_locked_slots` | 日期锁定 |
| `app_audit_log` | 为后续审计保留的结构化日志表 |

群到站点的绑定放在 `data/control.db`，不再直接覆盖 JSON 文件。

## 测试与质量检查

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check . --exclude .venv
.venv/bin/ruff format --check . --exclude .venv
.venv/bin/python -m compileall -q qqbot scripts main.py
```

当前测试覆盖：

- 21:59/22:00 业务日边界；
- `21.5` 与非法分钟输入；
- 指令到绝对日期、room ID 的解析；
- `取消 +1` 的全天取消语义；
- 查询单日、7 天范围、绝对日期范围和角色缺省范围；
- 时间轴模板数据与文字回退；
- QQ 本地图片预上传、逐片确认和合并协议；
- 权限提前天数；
- 玉泉路累计时长；
- 冲突后的可用片段；
- 部分取消拆分；
- 两线程同时抢同一时段；
- 旧数据库导入且旧表保留。

## 部署

推荐使用 `deploy/qqbot.service.example` 作为 systemd 模板：

```bash
sudo cp deploy/qqbot.service.example /etc/systemd/system/qqbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now qqbot
sudo systemctl status qqbot
```

需要按实际路径与 Linux 用户修改模板。日志默认写入 `logs/qqbot.log`，保留 14 天。

每天 04:00 会将 90 天前预约先导出到各站点 `archives/`，再从活动数据库中删除。

## 如何新增功能

增加普通业务功能时，按这个顺序修改：

1. 在 `domain/commands.py` 增加 Command；
2. 在 QQ Parser 中增加文本到 `ParsedIntent` 的映射；
3. 在 Resolver 中把引用解析成绝对 ID 和日期；
4. 在 `BookingApplication.execute()` 增加业务用例；
5. 如需数据操作，先扩展 `BookingRepository` 端口，再实现 SQLite；
6. Presenter 增加结果文案；
7. 增加 Parser、Service 和 Repository 测试。

不要在 QQ Client、Presenter 或 Parser 中直接执行 SQL，也不要在 Presenter 中重新判断权限。

## 自然语言支持（NLU，v3.1 已落地）

> 2026-08-23 起，bot 支持用自然语言预约/查询/取消/绑定，不限于 `/指令` 格式。
> 详细设计见 `docs/NLU-DESIGN.md`，部署说明见 `docs/NLU-DEPLOY.md`。

**支持的表达（示例）**：

```
帮我约一下303 7点到8点半            → 预约
明天下午3点去304外面的房间练2h琴      → 预约（复杂指代 → 房间缺省提示）
查一下我的预约 / 看看我今天约的        → 查询个人预约
周三303有没有空 / 下周二能约吗        → 空闲查询（周一制星期）
把304b今晚的预约取消                → 取消
我是张三 2023X1234567890          → 绑定
明天下午约303（缺具体时间）          → 安全拒绝并给出格式指导
```

**基本实现方式**（三层分工，全部本地可运行）：

1. **意图识别**：ML 意图分类为主通道（字符 n-gram 朴素贝叶斯，模型约 61KB，`qqbot/nlu/data/intent_model.json`），置信度低于阈值时规则引擎（句式模板 + 关键词评分）兜底；复合指令（一次说多件事）在最前拦截防半执行；
2. **槽位抓取**（全部本地规则，`qqbot/nlu/matcher.py`）：房间走 **gazetteer 白名单**（配置 name+aliases + `room_whitelist.json` 生长别名，最长匹配 + 边界检查）；时间/日期/星期走确定性正则（星期为周一制，NLU 只提原语、Resolver 换算绝对日期）；「琴房」等泛称安全缺省；
3. **自优化闭环**：解析失败的输入夜间经 LLM 一致性投票标注（可选 `DEEPSEEK_API_KEY`），产物回流——房间别名/闲聊词自动生长进白名单（`scripts/optimize_whitelist.py`），意图模型自动重训（影子验证零回归才原子替换，`scripts/train_intent.py --auto`）。两个开关独立：`features.nlu_auto_optimize` / `nlu_auto_retrain`。

关键原则：**槽位永远本地规则、fail-closed**——模型只判断意图，绝不猜测槽位（约错房间是事故）；规则引擎「组件可解释才采纳」，解释不了就交给 ML 或安全拒绝（help/格式指导），不做静默猜测。`ParsedIntent` 协议与 `/指令` 完全一致，后续均走 Resolver → 应用校验 → 数据库事务。

> 边界与扩展规则见 `docs/ARCHITECTURE.md` 和 `docs/DEVELOPMENT.md`。
