# 开发维护指南

## 开发前

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest
```

不要使用生产数据库运行测试。测试通过 `tmp_path` 自动创建隔离数据库。

## 一次改动的推荐流程

1. 先写能描述预期行为的测试；
2. 修改最内层规则；
3. 接入外层 Parser/Presenter；
4. 运行测试、Ruff 和 compileall；
5. 在测试群和复制的数据库上试运行；
6. 再安排生产切换。

## 新增指令

以“锁定日期时段”为例：

1. 新增 `LockSlot` Command；
2. Parser 只提取日期表达式、房间引用和时间文字；
3. Resolver 转换为 `date + room_id + TimeRange`；
4. Application 检查管理员权限；
5. Repository 在事务内检查冲突并插入；
6. 返回 `slot_locked`；
7. Presenter 负责中文回复。

一个 Handler 不应同时包含正则、SQL 和 QQ 发送代码。

## 修改数据库

目前 v3 表由 `SCHEMA` 创建，并用 `app_meta` 记录旧表导入状态。正式增加字段时应：

1. 添加具名 schema 版本；
2. 写幂等迁移；
3. 在数据库副本上验证；
4. 为旧版本与新版本各写测试；
5. 更新 `scripts/migrate.py` 和迁移文档。

不能靠捕获所有 `OperationalError` 来判断“字段大概已经存在”。

## 修改预约规则

规则优先配置化，但不要把算法放进 YAML。适合配置的内容包括：

- 开放时间；
- 业务日边界；
- 角色对应最大偏移；
- 单次与每日时长；
- 功能开关；
- 房间及别名。

冲突算法、权限比较和事务流程应保留在代码中，并有测试。

## 必测边界

- 业务日切换前一秒和切换时刻；
- 月末、年末与闰日；
- `+0` 到最大偏移，以及超过最大偏移；
- 00/30 合法，其他分钟非法；
- 开放时间首尾；
- 完全冲突、部分冲突和相邻不冲突；
- 取消头部、尾部、中间和全部；
- 多写入者同时抢同一时段；
- 每个站点使用自己的 DB 与配置；
- 权限提升、撤销与 owner 转让。

## 日志

日志中的稳定追踪字段是：

- `request_id`；
- `bot_id`；
- 内部 `user_id`；
- operation；
- result code。

不要记录 AppSecret，也不要在普通日志中完整输出学号和 QQ 外部 ID。

## Code Review 检查表

- 是否把业务规则写进了 Parser/Presenter/QQ Client？
- 是否接受客户端传入的用户 ID 或角色？
- 动态冲突校验是否与写入在同一事务？
- 是否新增了无类型的 tuple 或依赖中文字符串判断？
- 是否误用了默认站点数据库？
- 是否读取了多次当前时间，可能跨过 22:00？
- 是否为新的边界情况增加测试？
- 是否提供迁移与回滚方法？
- 查询角色缺省范围是否仍来自可信 Repository？
- 图片失败时是否仍能呈示同一份文字结果？
- 是否把预签名 URL、AppSecret 或 `file_info` 写入了日志？

## 修改查询图片

- 查询数据结构在 `presentation/timeline.py` 中生成，模板只负责布局；
- QQ 上传协议集中在 `interfaces/qq/media_uploader.py`；
- 不要让模板重新计算权限、业务日或空闲时段；
- 模板必须保持 HTML 自动转义；
- 不要记录服务端返回的预签名 URL；
- 新增图片功能时必须保留文字回退，并为上传调用顺序写无网络单元测试。

详细安装和排错见 `QUERY_IMAGES.md`。
