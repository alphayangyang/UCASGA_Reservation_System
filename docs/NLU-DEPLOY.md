# NLU 增量部署指南

> 适用：QQBot v3.1.0 + NLU（Phase 0/1/2，见 `docs/NLU-DESIGN.md` 与手册第 33 节）
> 总原则：**增量部署——只更新 NLU 相关文件，生产数据零触碰**（手册 21 节红线）。

---

## 1. ✅ 需要更新的

### 1.1 代码（随版本走，git 管理）

| 路径 | 说明 |
| --- | --- |
| `qqbot/nlu/`（整个目录） | **新增**：规则引擎 + ML 分类器 + LLM 标注 |
| `qqbot/interfaces/qq/parser.py` | 修改：NLU fallback + 称呼剥离 + 可爱化 fail-closed + 统一不支持拦截 |
| `qqbot/interfaces/qq/presenter.py` | 修改：chitchat / nlu_unrecognized / compound / other_person / natural_past 文案 |
| `qqbot/interfaces/qq/client.py` | 修改：pending 收集 + 04:30 夜间任务 + 模型懒加载挂载 + 称呼词注入 |
| **`qqbot/application/resolver.py`** | **修改：自然日语义换算（`natural_date`/`natural_range` → 业务偏移，22:00 边界来自 config）——漏了它「明天」在夜间会约错一天** |
| `qqbot/infrastructure/config.py` | 修改：`FeatureConfig.nlu_enabled` |
| `pyproject.toml` | 修改：ruff exclude docs |
| `scripts/bench_nlu.py` | 新增：性能基准 |
| `scripts/train_intent.py` | 新增：训练 + 交叉验证 + **影子验证（--verify）/ 自动重训（--auto）** |
| `scripts/collect_samples.py` | 新增：日志样本收集 |
| `scripts/nightly_annotate.py` | 新增：夜间标注 CLI |
| `scripts/optimize_whitelist.py` | **新增（2026-08-23）：白名单自优化手动入口**（建议/--apply/--auto） |
| `qqbot/nlu/optimize.py` | **新增：自优化核心**（房间别名 + 闲聊词提炼，见 NLU-DESIGN.md 5.4） |
| `tests/test_nlu*.py` | 新增：测试（开发机用，生产可不装 pytest） |
| `docs/` | 可选：手册 33 节 + NLU-DESIGN.md + NLU-DEPLOY.md |

### 1.2 配置

| 文件 | 操作 |
| --- | --- |
| `configs/*.yaml` | 加 `features.nlu_enabled: false`（**先保持 false，测试群再开 true**） |
| `configs/*.yaml` | **可选：语音数字朗读别名**——房间 `aliases` 加中文数字变体（yqh 已加示例：`三零三`/`三百零三`/`三零四B`）；不加则语音用户说「三零三」约不了房 |
| `.env` | **追加** `DEEPSEEK_API_KEY=sk-...`（可选，不配则夜间标注不启动） |
| `.env` | **可选** `DEEPSEEK_MODEL=deepseek-v4-flash`（默认即此；V4 系列，旧名 deepseek-chat 已停用；思考模式已在代码中显式关闭，勿自行开启以免一致性投票发散） |

> ⚠️ `configs/*.yaml` 生产上可能被本地改过（owner、房间别名）——**不要用 git 覆盖**，手动合并只加 nlu_enabled（及需要的语音别名）。
> 称呼前缀无需配置：client 自动注入「小泉」+ 各站点 bot_name。

### 1.3 数据文件（gitignore，必须手动拷贝）

`data/` 不在 git 里，git pull 不会带过来。首次部署需从开发机拷贝：

```bash
scp -r qqbot/nlu/data/ user@server:/opt/qqbot/qqbot/nlu/data/
# 或最小集：seed_samples.jsonl（288 条种子）+ intent_model.json（61KB 模型）
```

| 文件 | 缺失的影响 |
| --- | --- |
| `intent_model.json` | ML 兜底通道静默关闭，**规则引擎照常工作**（非硬依赖） |
| `seed_samples.jsonl` | 不能训练/评估（非硬依赖） |
| `room_whitelist.json` / `chitchat_keywords.json` / `manual_samples.json` | 自优化产物；缺失时 client 按空处理，启动不受影响 |
| `candidates.jsonl` 等 | 无影响（夜间标注产物，服务器会自己生成） |

### 1.5 自优化开关（2026-08-23 新增，独立开关）

`configs/*.yaml` 的 `features` 下（任一站点开启即全局生效，与 `nlu_enabled` 同模式）：

```yaml
features:
  nlu_enabled: true          # 启用 NLU（先 false，测试群再开）
  nlu_auto_optimize: false   # 白名单自优化（房间别名 + 闲聊词，夜间自动应用）
  nlu_auto_retrain: false    # ML 自动重训（样本增量门槛 + 影子验证 + 原子替换）
```

手动操作（不开自动开关也能用）：
- `python -m scripts.optimize_whitelist`（建议报告）→ `--apply`（应用）
- 人工标注写入 `qqbot/nlu/data/manual_samples.json`（`room_aliases` 按站点填
  `{"alias": "room_id"}` 列表），夜间/`--apply` 自动校验并应用
- `python -m scripts.train_intent --verify`（影子验证，不替换线上模型）

### 1.4 部署后（服务器上执行一次）

```bash
cd /opt/qqbot
.venv/bin/python -m scripts.train_intent --no-save   # 确认数据在、模型可复现
.venv/bin/python -m scripts.bench_nlu --compare       # 冒烟：三方对比
.venv/bin/python -m scripts.optimize_whitelist       # 冒烟：自优化建议模式
```

---

## 2. ❌ 不要更新的（生产红线，手册 21 节）

| 路径 | 为什么不能动 |
| --- | --- |
| `data/control.db` | 群绑定关系，动了 bot 全群失联 |
| `data/yqh/*.db`、`data/yql/*.db`、`data/zgc/*.db` | 业务数据库（预约/用户/角色），NLU 完全不涉及 |
| 生产 `.env` 的 `QQBOT_APPID/SECRET` 等既有行 | 只追加新变量，不整文件替换 |
| `group_mappings.json` | 旧群映射，迁移后不应再改 |
| `logs/` | 运行日志，部署时清空会丢失排查线索 |
| `main.py`、`domain/`、`presentation/timeline.py`、`sqlite_repository.py`、`media_uploader.py` 等 | **NLU 未修改这些文件**——不要用开发机版本覆盖（行尾符/版本混杂风险） |
| `qqbot/application/` 下**除 `resolver.py` 外**的文件 | `resolver.py` 是 NLU 的自然日换算修改点（必须更新）；其余 application 文件未动，不要覆盖 |
| 生产 venv（若已存在） | 不要重建；只缺依赖时用 `uv pip install` 增量补 |

---

## 3. 部署步骤（推荐顺序）

```bash
# ① 备份（手册 21.1：停服后复制或 SQLite .backup）
sudo systemctl stop qqbot
cp -r /opt/qqbot/data /tmp/data-backup-$(date +%F)
cp /opt/qqbot/.env /tmp/env-backup-$(date +%F)

# ② 更新代码（git 或拷贝；configs 手动合并，勿 git 覆盖）
cd /opt/qqbot && git pull          # 或按 1.1 清单拷贝新文件

# ③ 拷贝 NLU 数据
scp -r qqbot/nlu/data/ user@server:/opt/qqbot/qqbot/nlu/data/

# ④ .env 追加 DEEPSEEK_API_KEY（可选）
echo 'DEEPSEEK_API_KEY=sk-...' >> /opt/qqbot/.env

# ⑤ 启动并验证
sudo systemctl start qqbot
journalctl -u qqbot -f | head -50     # 确认无异常，NLU 相关日志出现

# ⑥ 测试群开启 NLU（单独一步，可随时关）
#    改 configs/yqh.yaml（测试群对应站点）→ features.nlu_enabled: true
sudo systemctl restart qqbot
```

---

## 4. 验证清单

- [ ] `scripts.doctor` 通过（只读健康检查）
- [ ] 测试群：「帮我看看303有没有空」→ 空闲查询（NLU 命中）
- [ ] 测试群：「今天天气不错」→ 「再玩小泉要坏啦QwQ 💦」（闲聊）
- [ ] 测试群：「这个小程序是干嘛的」→ 「对不起，小泉现在还不能听懂哦」（听不懂）
- [ ] 测试群：「预约 303」→ 格式指导（不丢失）
- [ ] **自然日**：22:00 后发「帮我约明天8点到9点的303」→ 约到**自然日明天**（不是后天）；发「今天」→ 「已经过了 22:00」提示
- [ ] **统一不支持**：「取消今天的预约，预约明天…」→ 「❌ 小泉还不支持这样的指令哦」；「取消张三的预约」→ 「不能帮你操作别人的预约哦」
- [ ] **称呼**：「小泉，帮我看看303明天有没有空」→ 正常解析
- [ ] `qqbot/nlu/data/pending/` 出现新文件（失败输入在收集）
- [ ] 配了 key 的：次日 04:30 后 `qqbot/nlu/data/reports/` 有日报
- [ ] `free -h`：available 无明显下降（NLU <10MB）
- [ ] 业务回归：标准格式指令（/预约、/查询、#备份用户）全部正常

---

## 5. 回滚（分层，任一级即可）

| 层级 | 操作 | 影响 |
| --- | --- | --- |
| ① 关 NLU | yaml `nlu_enabled: false` → 重启 | 与旧版逐字节一致（parser fallback 不触发） |
| ② 删模型 | 删 `qqbot/nlu/data/intent_model.json` | ML 通道关闭，规则引擎照常 |
| ③ 删数据 | 删 `qqbot/nlu/data/`（先备份） | 数据收集停止 |
| ④ 全回滚 | `git revert` + 删 `qqbot/nlu/` | 回到无 NLU 版本（生产数据不受影响） |
