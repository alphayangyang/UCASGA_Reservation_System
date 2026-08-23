"""NLU 全量极端样例回归套件（docs/NLU-DESIGN.md）。

覆盖所有历史讨论过的边界：基础口语化、中文数字时间、后N天日期、
复合/多日期/多房间统一口径、查询降级 + hint、可爱化 fail-closed、
正则路径零回归、管理员硬隔离。任何新改动不得让以下用例失败。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from qqbot.application.resolver import CommandResolver
from qqbot.domain.calendar import SHANGHAI_TZ
from qqbot.domain.errors import AppError, ParseError
from qqbot.interfaces.qq.parser import QQCommandParser
from qqbot.nlu import NLUIntentMatcher

NOW = datetime(2026, 8, 22, 10, 0, tzinfo=SHANGHAI_TZ)

# (输入, 站点, 期望)
# 期望形态：
#   "operation"            —— ParsedIntent.operation 相等
#   ("usage", "xxx")       —— ParseError usage
#   ("resolve", "子串")     —— Resolver 后 Command 字符串包含子串
#   ("degrade", "room"/"date") —— 降级查询（hint 标记）
REGRESSION_CASES: list[tuple[str, str, object]] = [
    # —— 绝对日期（YYYY-MM-DD，自然日语义）——
    # 注：past_date（过去日期拒绝）是 Resolver 层错误，见 test_nlu_regression_absolute_date
    ("帮我看看2026-08-25 303", "yqh", ("resolve", "QuerySchedule(")),
    # —— 语音转文字：数字朗读房间（NLU 提取原文，Resolver 别名归一化）——
    ("帮我约三零三明天7-8", "yqh", ("resolve", "CreateReservation(room_id='yqh-303'")),
    ("帮我约三百零三明天7-8", "yqh", ("resolve", "CreateReservation(room_id='yqh-303'")),
    ("帮我约三零四B明天7-8", "yqh", ("resolve", "CreateReservation(room_id='yqh-304b'")),
    ("看看三零三明天有没有人", "yqh", ("resolve", "QuerySchedule(")),
    ("帮我约303明天三点零四分", "yqh", ("usage", "help")),  # 时间朗读不误伤房间
    # —— 称呼前缀（小泉/站点名）剥离 ——
    ("小泉帮我约一下明天早上7-9的琴房嘛qwq", "yql", ("resolve", "CreateReservation(room_id='yql-main'")),
    ("小泉，帮我看看303明天有没有空", "yqh", "query_free"),
    ("小泉退了我明天的预约", "yqh", "cancel_reservation"),
    ("小泉取消我的预约", "yqh", "cancel_reservation"),
    ("小泉，今天天气不错", "yqh", ("usage", "chitchat")),
    ("小泉，取消张三的预约", "yqh", ("usage", "other_person")),
    ("帮我约一下明天8点的303嘛qwq", "yqh", ("usage", "help")),  # 语气词不掩盖缺槽位
    ("哈哈哈预约303明天", "yqh", ("usage", "help")),  # 闲聊词+命令词 → 格式指导
    ("哈哈取消张三的预约", "yqh", ("usage", "other_person")),  # 语气+人名 → 仍判他人
    # —— 基础口语化（Phase 0 验收） ——
    ("帮我看看明天琴房情况", "yqh", "query_schedule"),
    ("看看空闲", "yqh", "query_free"),
    ("帮我看看后天琴房", "yqh", "query_schedule"),
    ("查一下我的预约", "yqh", "query_personal"),
    ("明天下午帮我看看303有没有空", "yqh", "query_free"),
    ("帮我看看我约了什么", "yqh", "query_personal"),
    ("帮我约一下303 7点到8点半", "yqh", "create_reservation"),
    ("明天下午3点去304外面的房间练2h琴", "yqh", "create_reservation"),  # 复杂指代：房间缺省（Resolver 提示）
    ("约303明天7-8", "yqh", "create_reservation"),
    ("今晚7点约304b", "yqh", ("usage", "help")),
    ("把304b今晚的预约取消", "yqh", "cancel_reservation"),
    ("我是张三 2023X1234567890", "yqh", "bind_user"),
    ("有空吗304b", "yqh", "query_free"),
    ("看看304a后天", "yqh", "query_schedule"),
    ("帮我查查304a有空没有", "yqh", "query_free"),
    # —— 中文数字时间 / 后N天日期 ——
    ("帮我预约一下后两天上午八点的303，约1h", "yqh", ("resolve", "CreateReservation(room_id='yqh-303'")),
    ("帮我约一下后天上午八点的琴房，约1h", "yql", ("resolve", "CreateReservation(room_id='yql-main'")),
    ("明天下午3点到5点约304a", "yqh", ("resolve", "CreateReservation(room_id='yqh-304a'")),
    ("明天下午约304b 3点到5点", "yqh", ("resolve", "CreateReservation(room_id='yqh-304b'")),
    ("明天上午去304b练琴", "yqh", ("usage", "help")),  # 意图明显缺时间 → 格式指导
    # —— 复合 / 多日期 / 多房间：统一口径不支持 ——
    ("帮我取消今天的预约，预约明天中午12点到下午1点的303", "yqh", ("usage", "compound")),
    # 注：parser 前置拦截已删（2026-08-23 移交 ML 策略）；此输入走正则路径恰好匹配，
    # room 提取为「303 7-8，取消304b」→ Resolver 以「房间不存在」兜底（安全）。
    ("预约303 7-8，取消304b 9-10", "yqh", "create_reservation"),
    ("取消预约再预约明天", "yqh", ("usage", "compound")),
    ("先取消今天的预约然后预约明天中午的303", "yqh", ("usage", "compound")),
    ("取消预约", "yqh", "cancel_reservation"),  # 宾语型不误判
    ("查看我的预约", "yqh", "query_personal"),
    ("看看303有没有空", "yqh", "query_free"),
    ("帮我约今天和明天的琴房，八点到九点", "yql", ("usage", "compound")),  # 写入多日期
    ("取消303和304a明天的预约", "yqh", ("usage", "compound")),  # 写入多房间
    # —— 涉及他人（写入拒绝 / 查询降级 + hint）——
    ("帮我取消一下张三明天的预约", "yqh", ("usage", "other_person")),
    ("取消所有预约", "yqh", "cancel_reservation"),  # 量词不误判他人
    ("删掉我明天的全部预约", "yqh", "cancel_reservation"),  # 口语删除词（曾误判他人/复合）
    ("取消我全部的预约", "yqh", ("resolve", "CancelAllReservations")),  # 未来全部取消
    ("删除明天的预约", "yqh", "cancel_reservation"),
    ("把明天的预约清掉", "yqh", "cancel_reservation"),
    ("删掉张三明天的预约", "yqh", ("usage", "other_person")),  # 删除词+他人 → 仍判他人
    ("把我所有的预约都取消了", "yqh", "cancel_reservation"),
    ("取消全部预约", "yqh", "cancel_reservation"),
    ("帮我约李四明天的303", "yqh", ("usage", "other_person")),
    ("看看张三明天的预约", "yqh", ("degrade", "room")),
    ("把我明天的预约退了", "yqh", "cancel_reservation"),  # 本人不误伤
    ("取消我明天的预约", "yqh", "cancel_reservation"),
    # —— 查询降级（多房间 → 全部房间；多日期 → 范围）+ hint ——
    ("看看303和304a明天的预约", "yqh", ("degrade", "room")),
    ("看看今天和明天的预约", "yqh", ("degrade", "date")),
    ("看看303今天和明天的预约", "yqh", ("degrade", "date")),
    # —— 可爱化 fail-closed ——
    ("今天天气不错", "yqh", ("usage", "chitchat")),
    ("在吗在吗", "yqh", ("usage", "chitchat")),
    ("这个小程序是干嘛的", "yqh", ("usage", "nlu_unrecognized")),
    ("预约 303", "yqh", ("usage", "reserve")),
    ("帮我开一下空调", "yqh", ("usage", "help")),
    ("哈哈哈", "yqh", ("usage", "chitchat")),
    # —— 正则路径零回归 ——
    ("预约 303 7-8", "yqh", "create_reservation"),
    ("/预约 玉泉路琴房 21-22.5 +1", "yql", ("resolve", "CreateReservation(room_id='yql-main'")),
    ("查询 303 +1", "yqh", "query_schedule"),
    ("/取消 +1", "yqh", "cancel_reservation"),
    ("/查询个人", "yqh", "query_personal"),
    ("绑定 张三 2023X1234567890", "yqh", "bind_user"),
    ("预约304b明天下午", "yqh", ("usage", "reserve")),
    # —— 管理员硬隔离（# 永不进 NLU）——
    ("#备份用户", "yqh", "backup_users"),
    ("#添加周常 周一 303 21-22.5 排练", "yqh", "add_routine"),
    ("#取消 303 21-22.5 +1", "yqh", "admin_cancel"),
    ("#查询 2026-08-23 303", "yqh", "admin_query"),
]


def _resolve_with(parser: QQCommandParser, config, text: str) -> str:
    intent = parser.parse(text)
    return str(CommandResolver().resolve(intent, config, NOW))


@pytest.mark.parametrize(
    ("text", "site", "expected"),
    [(case[0], case[1], case[2]) for case in REGRESSION_CASES],
    ids=[f"{i:02d}-{case[0][:16]}" for i, case in enumerate(REGRESSION_CASES)],
)
def test_nlu_regression(text: str, site: str, expected: object, yqh_config, yql_config) -> None:
    config = yqh_config if site == "yqh" else yql_config
    parser = QQCommandParser(nlu=NLUIntentMatcher())

    if isinstance(expected, tuple) and expected[0] == "resolve":
        assert expected[1] in _resolve_with(parser, config, text)
        return

    try:
        intent = parser.parse(text)
    except ParseError as exc:
        assert isinstance(expected, tuple) and expected[0] == "usage"
        assert exc.details.get("usage") == expected[1]
        return
    except AppError:
        pytest.fail("不应出现业务错误")

    if isinstance(expected, tuple) and expected[0] == "degrade":
        assert intent.hint is not None, "降级查询应带 hint 提醒"
        hint = "房间" if expected[1] == "room" else "日期"
        assert hint in intent.hint
        return

    if isinstance(expected, tuple) and expected[0] == "usage":
        pytest.fail(f"期望 usage={expected[1]}，实际解析成功 {intent.operation}")
    assert intent.operation == expected


def test_nlu_regression_natural_day_vs_business_day(yqh_config) -> None:
    """自然日语义（主人 2026-08-22 决策）：日期词按自然日理解，
    22:00 后「明天」仍是自然日明天（业务日 +0），「今天」已过边界应拒绝。
    显式 +N 保持业务日语义。边界时间来自 config.business_boundary。"""
    from qqbot.domain.errors import ParseError

    parser = QQCommandParser(nlu=NLUIntentMatcher())
    resolver = CommandResolver()
    night = datetime(2026, 8, 22, 23, 0, tzinfo=SHANGHAI_TZ)  # 22:00 后

    # 22:00 后「明天」→ 自然日 8/23（修复前会错到 8/24）
    command = resolver.resolve(parser.parse("帮我约明天8点到9点的303"), yqh_config, night)
    assert command.reserve_date.isoformat() == "2026-08-23"
    assert command.business_offset == 0

    # 22:00 后「今天」→ natural_past 拒绝
    with pytest.raises(ParseError) as exc:
        resolver.resolve(parser.parse("帮我约今天8点到9点的303"), yqh_config, night)
    assert exc.value.details.get("usage") == "natural_past"

    # 显式 +1 保持业务日语义（22:00 后 = 自然日后天 8/24）
    command = resolver.resolve(parser.parse("帮我约303 8点到9点+1"), yqh_config, night)
    assert command.reserve_date.isoformat() == "2026-08-24"

    # 22:00 前「明天」→ 自然日 8/23（业务日 +1）
    day = datetime(2026, 8, 22, 10, 0, tzinfo=SHANGHAI_TZ)
    command = resolver.resolve(parser.parse("帮我约明天8点到9点的303"), yqh_config, day)
    assert command.reserve_date.isoformat() == "2026-08-23"
    assert command.business_offset == 1

    # 22:00 后查询「明天」→ 自然日 8/23
    command = resolver.resolve(parser.parse("帮我看看303明天有没有人"), yqh_config, night)
    assert command.date_range.start.isoformat() == "2026-08-23"


def test_nlu_regression_absolute_date(yqh_config) -> None:
    """绝对日期：预约/取消走 date 字段（自然日语义，经 offset_of 校验）；
    查询走 range_start 绝对日期；时间解析不被日期数字污染。"""
    from qqbot.domain.errors import ParseError

    parser = QQCommandParser(nlu=NLUIntentMatcher())
    resolver = CommandResolver()
    now = datetime(2026, 8, 22, 10, 0, tzinfo=SHANGHAI_TZ)

    # 预约今天（绝对日期 = +0，合法）
    command = resolver.resolve(parser.parse("帮我约2026-08-22 8点到9点的303"), yqh_config, now)
    assert command.reserve_date.isoformat() == "2026-08-22"
    assert command.time_range.start == 480  # 08:00（日期数字未污染时间解析）
    assert command.time_range.end == 540

    # 查询绝对日期
    command = resolver.resolve(parser.parse("帮我看看2026-08-25 303"), yqh_config, now)
    assert command.date_range.start.isoformat() == "2026-08-25"

    # 过去日期拒绝
    with pytest.raises(ParseError) as exc:
        resolver.resolve(parser.parse("帮我约2026-08-20 8点到9点的303"), yqh_config, now)
    assert exc.value.details.get("usage") == "past_date"


def test_nlu_regression_query_degrade_arguments(yqh_config) -> None:
    """降级查询的槽位细节：多房间 → 无 room；多日期 → 范围。"""
    parser = QQCommandParser(nlu=NLUIntentMatcher())

    intent = parser.parse("看看303和304a明天的预约")
    assert intent.operation == "query_schedule"
    assert "room_reference" not in intent.arguments  # 房间缺省（查全部）
    assert intent.arguments["natural_date"] == 1
    assert "房间" in (intent.hint or "")

    intent = parser.parse("看看今天和明天的预约")
    assert intent.operation == "query_schedule"
    assert intent.arguments["natural_range"] == [0, 1]  # 自然日范围（Resolver 换算业务日）
    assert "日期" in (intent.hint or "")


def test_nlu_regression_weekday_expressions(yqh_config) -> None:
    """星期表达（周一制产品决策 2026-08-23：周一是每周第一天）：
    - 今天（2026-08-23 周日）说「下周一」→ 明天 8/24（下周第一天）；
    - 无前缀「周X」→ 接下来最近的周X（含今天）；
    - 「这X」本周已过 → natural_past 拒绝；「上X」→ 恒拒绝；
    - 查询/预约/取消 三操作都支持；多星期 → 查询降级；「查一下周一」的「下」
      不被误当前缀（(?<!一) 边界）。"""
    from qqbot.domain.errors import ParseError

    parser = QQCommandParser(nlu=NLUIntentMatcher())
    resolver = CommandResolver()
    sunday = datetime(2026, 8, 23, 10, 0, tzinfo=SHANGHAI_TZ)  # 周日

    # 周一制：下周一 = 下周第一天；今天周日 → 明天 8/24
    command = resolver.resolve(parser.parse("帮我约下周一303 7-8"), yqh_config, sunday)
    assert command.reserve_date.isoformat() == "2026-08-24"

    # 无前缀周X = 接下来最近（今天周日说「周二」→ 8/25，+2 在可查周期内）
    command = resolver.resolve(parser.parse("周二303有没有空"), yqh_config, sunday)
    assert command.date_range.start.isoformat() == "2026-08-25"

    # 超出可查/可约周期（下周三 = +3 > max_query_offset=2）→ 拒绝（与自然日一致）
    with pytest.raises(ParseError) as exc:
        resolver.resolve(parser.parse("周三303有没有空"), yqh_config, sunday)
    assert exc.value.details.get("usage") == "offset"

    # 本周已过（这周五 8/21）→ natural_past 拒绝；上周 → 拒绝
    with pytest.raises(ParseError) as exc:
        resolver.resolve(parser.parse("帮我约这周五303 7-8"), yqh_config, sunday)
    assert exc.value.details.get("usage") == "natural_past"
    with pytest.raises(ParseError) as exc:
        resolver.resolve(parser.parse("帮我约上周三303 7-8"), yqh_config, sunday)
    assert exc.value.details.get("usage") == "natural_past"

    # 「查一下周一」→ 周一（next），不是「下周一」（一下的「下」是动词短语）
    intent = parser.parse("查一下周一的预约情况")
    assert intent.arguments.get("weekday") == 1
    assert intent.arguments.get("week_mode") == "next"

    # 取消也支持星期（下周二 = +2 在可取消周期内）
    command = resolver.resolve(parser.parse("取消我下周二的303"), yqh_config, sunday)
    assert command.reserve_date.isoformat() == "2026-08-25"

    # 多星期查询 → 降级（默认范围 + hint）
    intent = parser.parse("看看周三和周四的预约")
    assert intent.operation == "query_schedule"
    assert "日期" in (intent.hint or "")

    # 写入类多日期（自然日+星期混合）→ 拒绝
    with pytest.raises(ParseError):
        parser.parse("帮我约今天和周三303 7-8")
