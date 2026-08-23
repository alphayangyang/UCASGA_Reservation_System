# 架构说明

## 依赖规则

依赖只能由外向内：

```text
interfaces → application → domain
infrastructure → application/domain
```

`domain` 不能导入 QQ SDK、SQLite、YAML 或文件系统。`application` 依赖 Repository Protocol，不依赖 SQLite 实现。

## 请求上下文

每次入口收到请求时，只读取一次当前时间，并建立 `RequestContext`：

- `request_id`：一次请求的追踪 UUID；
- `source`：`qq/web/system`；
- `site_id`：稳定站点 ID；
- `identity`：外部身份；
- `actor_user_id`：由身份系统解析的内部 UUID；
- `received_at`：带时区的接收时间。

用户 ID 不能来自消息文字。管理员代操作以后应增加显式 Command，同时记录操作者和目标用户。

## Parser 与 Resolver

Parser 只回答“用户表达了什么”，输出 `ParsedIntent`。Resolver 才回答：

- `+1` 对应哪个绝对日期；
- 文本房间名对应哪个 `room_id`；
- `21.5` 对应多少分钟；
- 管理员输入的日期是否合法。
- 查询范围对应哪些绝对日期，以及是否超过站点的 7 天上限。

这使规则 Parser、网页表单和 NLP Parser 都可以产生相同 Intent。

普通查询的缺省范围取决于数据库确认的当前角色。QQ Client 只向 Resolver 传入 Repository 返回的角色；Parser 不接受用户自报角色。Resolver 最终产生 `DateRange`，Application 再逐日读取 Repository。

## Application Service

应用服务是业务规则的唯一入口，负责：

- 注册状态；
- 角色和权限；
- 提前预约偏移；
- 开放时间；
- 单次时长；
- 功能开关；
- 调用 Repository 的原子操作。

即使 Command 来自未来网站，应用服务也会再次核对绝对日期与 `business_offset`，避免入口伪造。

## 事务边界

依赖当前数据库状态的校验必须和修改位于同一个事务：

- 时段冲突；
- 当日累计时长；
- 周常与已有预约冲突；
- 部分取消拆分。

静态格式校验可以在事务外完成。

## Result 与 Presenter

核心返回 `OperationResult(code, data)`，不返回 QQ 文案。QQ Presenter 将它转换成中文；未来 Web Presenter 可以转换成 HTTP 状态和 JSON。

新增结果码时应同时补充 Presenter 测试。不要让调用方依靠中文文字判断成功或失败。

多日查询仍只产生一个 `OperationResult`。QQ 入口有两种呈示路径：

- `QQPresenter` 生成文字，也是所有图片故障的兜底；
- `ScheduleImageRenderer` 生成 PNG，再由 `QQMediaUploader` 走平台分片上传。

图片渲染和媒体上传不参与业务决策，也不回写数据库。`QQMediaUploader` 是当前 SDK 私有 HTTP 会话的唯一使用点，SDK 适配不应扩散到 Application 或 Domain。

## 稳定标识

- QQ `member_openid`：外部身份，可被平台规则影响；
- `app_users.id`：系统用户 UUID；
- `site_id`：站点稳定 ID；
- `room.id`：房间稳定 ID；
- `app_reservations.id`：预约 UUID。

展示名称和别名可以修改，稳定 ID 不应修改。

## 扩展边界

接网站时应新增 `interfaces/web`，复用：

- `RequestContext`；
- Command；
- `BookingApplication`；
- Repository；
- OperationResult。

网站不能直接写 SQLite，也不能在 JavaScript 中复制 22:00 和权限规则。
