# v2 → v3 迁移说明

> v3.0 → v3.1 不改变数据库结构，不需要执行本页迁移脚本，也不需要重新绑定用户或群。保留 `data/`、`.env` 和实际使用的 YAML 配置，更新代码与依赖即可。

## 迁移内容

首次初始化会把旧表复制到新 `app_` 表：

| 旧表 | 新表 |
| --- | --- |
| `users` | `app_users` + `app_identities` |
| `admins` | `app_roles` |
| `reservations` | `app_reservations` |
| `weekly_routines` | `app_weekly_routines` |
| `locked_slots` | `app_locked_slots` |

旧 QQ ID 会通过确定性的 UUID 生成规则映射为系统用户 ID。旧房间名称通过配置中的名称和别名映射为稳定房间 ID。

## 上线步骤

1. 在 QQ 群公告维护窗口；
2. 停止旧 Bot，确认没有进程仍在写库；
3. 复制整个旧项目作为离线备份；
4. 把旧 `data/` 和 `group_mappings.json` 放到 v3 根目录；
5. 核对 YAML 数据库路径和房间别名；
6. 执行 `.venv/bin/python -m scripts.migrate`；
7. 执行 `.venv/bin/python -m scripts.doctor`；
8. 执行测试；
9. 在测试群验证绑定、查询、预约、取消和管理员操作；
10. 启动生产服务。

## 验证数据量

迁移脚本会输出新表中的有效预约数量。也可以手动只读检查：

```sql
SELECT COUNT(*) FROM reservations;
SELECT COUNT(*) FROM app_reservations WHERE deleted_at IS NULL;
```

如果旧库中包含已被清理或回收站数据，两者不一定完全相同；应按有效预约逐条抽查日期、房间与时段。

## 回滚

迁移脚本会生成 `.pre_v3_*.bak`。在新版本尚未开放用户写入时，可以：

1. 停止 v3；
2. 保留故障数据库以供分析；
3. 用对应 `.bak` 恢复原数据库文件；
4. 启动旧版本。

一旦 v3 已经接受新预约，不能直接启动旧版本，因为旧版本不会读取 `app_reservations`。此时应先导出 v3 新增记录或修复 v3，不要让两个版本同时运行。

## 重复运行

旧表导入由 `app_meta.legacy_import_v1` 标记，重复启动不会重复导入。`scripts.migrate` 每次运行仍会额外生成数据库备份。

## 已知迁移约束

v3 强制半小时网格。旧表中若存在非 00/30 分钟记录，自动导入会跳过该条、保留旧表原记录，并写入 `app_migration_issues`。`doctor` 会返回失败，阻止把这种状态当作迁移成功。迁移前也可以先在旧库中检查：

```sql
SELECT * FROM reservations
WHERE substr(start_time, 4, 2) NOT IN ('00', '30')
   OR substr(end_time, 4, 2) NOT IN ('00', '30');
```

发现记录时应人工确认后再上线。
