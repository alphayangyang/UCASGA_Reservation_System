# 多日查询与图片发送

## 指令边界

普通查询支持以下日期表达式：

| 输入 | 含义 |
| --- | --- |
| `/查询` | 按当前角色读取缺省范围 |
| `/查询 +1` | 查询一个业务日 |
| `/查询 +0~+6` | 查询含首尾的 7 个业务日 |
| `/查询 2026-08-10` | 查询一个绝对日期 |
| `/查询 2026-08-10~2026-08-16` | 查询绝对日期范围 |
| `/空闲 ...` | 日期语法完全相同，结果改为空闲时段 |

范围两端必须采用同一种表示法。`+0~2026-08-10`、倒序范围和超过 `query.max_range_days` 的范围都会返回格式错误。本版本将站点上限限制为 7 天。

`+N` 由 `BusinessCalendar` 按 22:00 业务日边界解析；`YYYY-MM-DD` 是绝对日期，不经过边界换算。查询权限与预约权限是两件事，能够查询某天不会赋予该日期的预约权限。

## 角色缺省值

缺省值由各站点 YAML 管理：

```yaml
query:
  max_range_days: 7
  image_enabled: true
  default_ranges:
    user: [0, 0]
    band: [0, 1]
    admin: [0, 6]
    owner: [0, 6]
```

数组为 `[开始偏移, 结束偏移]`，两端均计入。客户端只把数据库确认过的当前角色传给 Resolver；用户不能在消息中声明自己的角色。

## 结果生成链路

1. Parser 提取房间引用以及范围两端，仍不读取时间和数据库；
2. Resolver 生成绝对 `DateRange`，并执行 7 天上限检查；
3. Application 按日期读取 Repository，返回 `schedule_range` 或 `free_slots_range`；
4. `build_timeline_view()` 把结果转换为不依赖 QQ/浏览器的模板数据；
5. `ScheduleImageRenderer` 用 Jinja2 自动转义 HTML，再由 Playwright 截取 PNG；
6. `QQMediaUploader` 上传 PNG 并返回 `media.file_info`；
7. QQ Client 以 `msg_type=7` 回复原消息。

任何图片环节抛出异常时，Client 都用 `QQPresenter` 重新呈示同一份 `OperationResult`，发送文字结果。预约、取消、绑定等写操作不经过图片链路。

## QQ 官方本地文件上传

适配器实现官方推荐的分片上传：

1. `POST /v2/groups/{group_openid}/upload_prepare`，传入图片大小、文件名、MD5、SHA1 和前 10002432 字节的 MD5；
2. 按响应中的 `block_size` 与 `parts`，把每片原始字节 PUT 到对应预签名 URL；
3. 每片成功后调用 `POST /v2/groups/{group_openid}/upload_part_finish`，提交分片序号、实际大小和 MD5；
4. 调用 `POST /v2/groups/{group_openid}/files`，携带 `upload_id` 完成合并；
5. 原样透传响应中的 `file_info` 到群消息 `media`。

当前 `qq-botpy 1.2.1` 只公开 URL 上传方法，所以适配器复用 SDK 的鉴权 HTTP 会话调用新端点。这个私有 SDK 接触面只存在于 `interfaces/qq/media_uploader.py`；未来 SDK 提供正式分片 API 时，只替换这一层。

官方参考：

- [富媒体消息概述](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/rich-media.html)
- [群聊富媒体预上传](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_id_upload_prepare.post.html)
- [群聊分片上传完成](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_id_upload_part_finish.post.html)
- [群聊富媒体上传/合并](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_files.post.html)

## 安装与运行

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
set -a
source .env
set +a
.venv/bin/python -m playwright install chromium
```

`PLAYWRIGHT_BROWSERS_PATH` 必须在安装和启动时保持一致。使用 systemd 时，`EnvironmentFile`、`WorkingDirectory` 和实际安装浏览器的 Linux 用户也必须一致。

如果系统库不全，可执行：

```bash
sudo .venv/bin/python -m playwright install-deps chromium
```

不需要图片时设置：

```yaml
query:
  image_enabled: false
```

## 排错顺序

1. 日志出现“图片渲染器启动失败”：检查 Chromium 安装路径、运行用户和系统动态库；
2. 出现“查询图片发送失败”：在同一条日志中按 `request_id` 定位具体异常；
3. 预上传返回权限错误：在 QQ 开放平台核对群消息与富媒体权限；
4. 分片 PUT 失败：检查服务器到预签名存储域名的出站网络；
5. 合并成功但消息失败：核对 `msg_type=7`、群 OpenID 和被回复消息是否仍有效；
6. 排错期间不应关闭文字回退，用户仍可获得查询结果。

预签名 URL 带临时凭证，不应写入日志或错误回复。`file_info` 是不透明且可能过期的数据，只用于本次紧随其后的消息发送，不写数据库。

## 测试

`tests/test_query_images.py` 不访问 QQ 网络，使用假 HTTP 层验证：

- 三个分片的切割内容；
- prepare → 每片 finish → files 的调用顺序；
- 整文件与分片校验值；
- 当前统一 API 域名；
- 模板数据、HTML 自动转义和文字回退。

真实上线前还应在测试群执行 `/查询 +0~+1` 和 `/空闲 +0~+1`，确认机器人应用实际拥有富媒体接口权限。
