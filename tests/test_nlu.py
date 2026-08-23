from __future__ import annotations

from datetime import datetime

import pytest

from qqbot.domain.calendar import SHANGHAI_TZ
from qqbot.domain.commands import (
    BindUser,
    CancelReservation,
    CreateReservation,
    QueryFreeSlots,
    QueryPersonal,
)
from qqbot.domain.errors import ParseError
from qqbot.interfaces.qq.parser import ParsedIntent, QQCommandParser
from qqbot.nlu import NLUIntentMatcher


@pytest.fixture
def nlu() -> NLUIntentMatcher:
    return NLUIntentMatcher()


@pytest.fixture
def nlu_parser() -> QQCommandParser:
    return QQCommandParser(nlu=NLUIntentMatcher())


# —— 规则引擎：预约 ——


def test_nlu_reserve_range_with_period(nlu: NLUIntentMatcher) -> None:
    intent = nlu.match("明天下午3点到5点约304a")
    assert intent is not None
    assert intent.operation == "create_reservation"
    assert intent.arguments["room_reference"] == "304a"
    assert intent.arguments["start"] == "15:00"
    assert intent.arguments["end"] == "17:00"
    assert intent.arguments["natural_date"] == 1


def test_nlu_reserve_start_plus_duration(nlu: NLUIntentMatcher) -> None:
    intent = nlu.match("明天下午3点去304b练2h琴")
    assert intent is not None
    assert intent.operation == "create_reservation"
    assert intent.arguments["room_reference"] == "304b"
    assert intent.arguments["start"] == "15:00"
    assert intent.arguments["end"] == "17:00"
    assert intent.arguments["natural_date"] == 1


def test_nlu_reserve_digit_range(nlu: NLUIntentMatcher) -> None:
    intent = nlu.match("约303明天7-8")
    assert intent is not None
    assert intent.operation == "create_reservation"
    assert intent.arguments["room_reference"] == "303"
    assert intent.arguments["start"] == "07:00"
    assert intent.arguments["end"] == "08:00"
    assert intent.arguments["natural_date"] == 1


def test_nlu_reserve_room_after_range_words(nlu: NLUIntentMatcher) -> None:
    intent = nlu.match("帮我约一下303 7点到8点半")
    assert intent is not None
    assert intent.operation == "create_reservation"
    assert intent.arguments["room_reference"] == "303"
    assert intent.arguments["start"] == "07:00"
    assert intent.arguments["end"] == "08:30"


def test_nlu_reserve_chinese_room(nlu: NLUIntentMatcher) -> None:
    intent = nlu.match("约琴房明天晚上")
    assert intent is None  # 无具体时间 → fail-closed


def test_nlu_reserve_tonight(nlu: NLUIntentMatcher) -> None:
    intent = nlu.match("今晚7点约304b")
    assert intent is None  # 有起点无时长/终点 → fail-closed


def test_nlu_reserve_without_time_is_rejected(nlu: NLUIntentMatcher) -> None:
    assert nlu.match("预约304b明天下午") is None  # 文档 4.2：预约必须有时间
    assert nlu.match("预约303") is None


def test_nlu_reserve_complex_room_reference_room_optional(nlu: NLUIntentMatcher) -> None:
    # 「304外面的房间」是复杂指代 → 房间槽位不拆解（None，合法缺省）；
    # 意图仍按 ML/规则判定为预约，房间缺省交给 Resolver（单房间兜底/多房间提示）。
    # （2026-08-23 架构修订：意图与槽位解耦，复杂指代不再导致整句 fail-closed。）
    intent = nlu.match("明天下午3点去304外面的房间练2h琴")
    assert intent is not None
    assert intent.operation == "create_reservation"
    assert "room_reference" not in intent.arguments


# —— 规则引擎：取消 ——


def test_nlu_cancel_whole_day(nlu: NLUIntentMatcher) -> None:
    intent = nlu.match("把我明天的预约退了")
    assert intent is not None
    assert intent.operation == "cancel_reservation"
    assert intent.arguments["natural_date"] == 1
    assert "start" not in intent.arguments


def test_nlu_cancel_range(nlu: NLUIntentMatcher) -> None:
    intent = nlu.match("取消303今天7-8")
    assert intent is not None
    assert intent.operation == "cancel_reservation"
    assert intent.arguments["room_reference"] == "303"
    assert intent.arguments["start"] == "07:00"
    assert intent.arguments["end"] == "08:00"
    assert intent.arguments["natural_date"] == 0


def test_nlu_cancel_all_flag(nlu: NLUIntentMatcher) -> None:
    """「取消全部预约」→ cancel_all 标记（未来整个可预约周期）。"""
    intent = nlu.match("取消我全部的预约")
    assert intent is not None
    assert intent.operation == "cancel_reservation"
    assert intent.arguments["cancel_all"] is True
    intent = nlu.match("删掉我所有预约")
    assert intent is not None
    assert intent.arguments["cancel_all"] is True
    # 无「全部」词 → 不标记
    intent = nlu.match("取消303今天7-8")
    assert intent is not None
    assert "cancel_all" not in intent.arguments


def test_nlu_cancel_colloquial(nlu: NLUIntentMatcher) -> None:
    intent = nlu.match("我明天不去了")
    assert intent is not None
    assert intent.operation == "cancel_reservation"
    assert intent.arguments["natural_date"] == 1


# —— 规则引擎：查询 / 空闲 / 个人 ——


def test_nlu_query_schedule(nlu: NLUIntentMatcher) -> None:
    intent = nlu.match("帮我看看303今天有没有人")
    assert intent is not None
    assert intent.operation == "query_schedule"
    assert intent.arguments["room_reference"] == "303"
    assert intent.arguments["natural_date"] == 0


def test_nlu_query_free(nlu: NLUIntentMatcher) -> None:
    intent = nlu.match("明天下午帮我看看303有没有空")
    assert intent is not None
    assert intent.operation == "query_free"
    assert intent.arguments["room_reference"] == "303"
    assert intent.arguments["natural_date"] == 1


def test_nlu_query_free_room_after_keyword(nlu: NLUIntentMatcher) -> None:
    intent = nlu.match("有空吗304b")
    assert intent is not None
    assert intent.operation == "query_free"
    assert intent.arguments["room_reference"] == "304b"


def test_nlu_query_free_no_room(nlu: NLUIntentMatcher) -> None:
    intent = nlu.match("今晚7点以后哪个房间空着")
    assert intent is not None
    assert intent.operation == "query_free"
    assert "room_reference" not in intent.arguments


def test_nlu_query_personal(nlu: NLUIntentMatcher) -> None:
    intent = nlu.match("帮我看看我约了什么")
    assert intent is not None
    assert intent.operation == "query_personal"
    assert intent.arguments == {}


def test_nlu_defaults_to_query_schedule(nlu: NLUIntentMatcher) -> None:
    """产品语义（docs/NLU-DESIGN.md 3.2）：模糊动作词默认查询，
    因为查询信息最全，空闲/个人只是换一种呈现方式。
    “琴房”是泛称 → 不指定房间，按角色默认范围查询。"""
    intent = nlu.match("帮我看看后天琴房")
    assert intent is not None
    assert intent.operation == "query_schedule"
    assert intent.arguments == {"natural_date": 2}


def test_nlu_look_at_room_defaults_to_query(nlu: NLUIntentMatcher) -> None:
    intent = nlu.match("看看304a后天")
    assert intent is not None
    assert intent.operation == "query_schedule"
    assert intent.arguments["room_reference"] == "304a"


def test_nlu_explicit_free_keeps_free_intent(nlu: NLUIntentMatcher) -> None:
    intent = nlu.match("明天有空吗")
    assert intent is not None
    assert intent.operation == "query_free"
    assert intent.arguments == {"natural_date": 1}


def test_nlu_look_free_means_free(nlu: NLUIntentMatcher) -> None:
    """“看看空闲”是明确空闲指引 → query_free（明确词权重高于模糊动作词）。"""
    intent = nlu.match("看看空闲")
    assert intent is not None
    assert intent.operation == "query_free"
    assert intent.arguments == {}


# —— 规则引擎：绑定 ——


def test_nlu_bind_template(nlu: NLUIntentMatcher) -> None:
    intent = nlu.match("我是张三 2023X1234567890")
    assert intent is not None
    assert intent.operation == "bind_user"
    assert intent.arguments == {"display_name": "张三", "student_id": "2023X1234567890"}


def test_nlu_bind_with_suffix_word(nlu: NLUIntentMatcher) -> None:
    intent = nlu.match("我是王五，学号2023X1234567893")
    assert intent is not None
    assert intent.operation == "bind_user"
    assert intent.arguments == {"display_name": "王五", "student_id": "2023X1234567893"}


# —— fail-closed：无法理解 → None ——


@pytest.mark.parametrize("text", ["今天天气不错", "哈哈哈", "帮我开一下空调", "随机文本abc"])
def test_nlu_unrecognized_returns_none(nlu: NLUIntentMatcher, text: str) -> None:
    assert nlu.match(text) is None


# —— Parser fallback 集成 ——


def test_parser_fallback_returns_parsed_intent(nlu_parser: QQCommandParser) -> None:
    intent = nlu_parser.parse("明天下午帮我看看303有没有空")
    assert isinstance(intent, ParsedIntent)
    assert intent.operation == "query_free"


def test_parser_fallback_still_raises_when_nlu_fails(nlu_parser: QQCommandParser) -> None:
    with pytest.raises(ParseError):
        nlu_parser.parse("今天天气不错")


def test_parser_chitchat_gets_cute_reply(nlu_parser: QQCommandParser) -> None:
    """闲聊输入 → ParseError(chitchat)（Present 渲染俏皮文案，见 presenter.usage）。"""
    with pytest.raises(ParseError) as exc:
        nlu_parser.parse("今天天气不错")
    assert exc.value.details.get("usage") == "chitchat"
    with pytest.raises(ParseError) as exc:
        nlu_parser.parse("在吗在吗")
    assert exc.value.details.get("usage") == "chitchat"


def test_parser_unrecognized_natural_language(nlu_parser: QQCommandParser) -> None:
    """非闲聊、非命令的自然语言 → ParseError(nlu_unrecognized)（听不懂文案）。"""
    with pytest.raises(ParseError) as exc:
        nlu_parser.parse("这个小程序是干嘛的")
    assert exc.value.details.get("usage") == "nlu_unrecognized"


def test_parser_command_like_keeps_usage_guidance(nlu_parser: QQCommandParser) -> None:
    """像命令但槽位缺失 → 保留原格式指导（不换成听不懂文案）。"""
    with pytest.raises(ParseError) as exc:
        nlu_parser.parse("预约 303")
    assert exc.value.details.get("usage") == "reserve"
    with pytest.raises(ParseError) as exc:
        nlu_parser.parse("帮我约一下304a")
    assert exc.value.details.get("usage") in ("help", "reserve")


def test_parser_compound_instruction_rejected(nlu_parser: QQCommandParser) -> None:
    """复合指令（取消…，预约…）统一口径不支持 → ParseError(compound)。"""
    with pytest.raises(ParseError) as exc:
        nlu_parser.parse("帮我取消今天的预约，预约明天中午12点到下午1点的303")
    assert exc.value.details.get("usage") == "compound"
    # 连接词分隔的无标点复合（「再」「然后」）
    with pytest.raises(ParseError) as exc:
        nlu_parser.parse("取消预约再预约明天")
    assert exc.value.details.get("usage") == "compound"
    with pytest.raises(ParseError) as exc:
        nlu_parser.parse("先取消今天的预约然后预约明天中午的303")
    assert exc.value.details.get("usage") == "compound"


def test_parser_single_instruction_not_compound(nlu_parser: QQCommandParser) -> None:
    """含取消+预约词但单段表达（把预约退了）不误判复合。"""
    intent = nlu_parser.parse("把304b今晚的预约取消")
    assert intent is not None
    assert intent.operation == "cancel_reservation"
    assert intent.arguments["room_reference"] == "304b"


def test_nlu_chinese_digit_time(nlu: NLUIntentMatcher) -> None:
    """中文数字时间（上午八点/约1h）+ 后N天日期。"""
    intent = nlu.match("帮我预约一下后两天上午八点的303，约1h")
    assert intent is not None
    assert intent.operation == "create_reservation"
    assert intent.arguments["start"] == "08:00"
    assert intent.arguments["end"] == "09:00"
    assert intent.arguments["natural_date"] == 2
    assert intent.arguments["room_reference"] == "303"


def test_nlu_multi_date_rejected(nlu: NLUIntentMatcher) -> None:
    """多日期（今天和明天）统一不支持 → match None。"""
    assert nlu.match("帮我约今天和明天的琴房，八点到九点") is None
    assert nlu.match("帮我取消明天和后天的预约") is None


def test_nlu_multi_room_query_degrades(nlu: NLUIntentMatcher) -> None:
    """查询多房间（303和304a）→ 降级为全部房间 + hint 提醒（不拒绝）。"""
    intent = nlu.match("看看303和304a明天的预约")
    assert intent is not None
    assert intent.operation == "query_schedule"
    assert "room_reference" not in intent.arguments  # 房间缺省（查全部）
    assert intent.arguments["natural_date"] == 1
    assert intent.hint is not None and "房间" in intent.hint


def test_nlu_multi_date_query_degrades(nlu: NLUIntentMatcher) -> None:
    """查询多日期（今天和明天）→ 转范围查询 + hint 提醒。"""
    intent = nlu.match("看看今天和明天的预约")
    assert intent is not None
    assert intent.operation == "query_schedule"
    assert intent.arguments["natural_range"] == [0, 1]
    assert intent.hint is not None


def test_nlu_multi_write_rejected(nlu: NLUIntentMatcher) -> None:
    """写入类（预约/取消）多日期/多房间 → 拒绝（不能半执行）。"""
    assert nlu.match("帮我约今天和明天的琴房，八点到九点") is None
    assert nlu.match("取消303和304a明天的预约") is None


def test_nlu_single_room_not_rejected(nlu: NLUIntentMatcher) -> None:
    """单房间/单日期不误伤、无 hint。"""
    intent = nlu.match("看看303明天有没有人")
    assert intent is not None
    assert intent.hint is None
    assert nlu.match("帮我约303明天7-8") is not None
    assert nlu.match("帮我约明天的琴房，八点到九点") is not None


def test_nlu_single_date_not_rejected(nlu: NLUIntentMatcher) -> None:
    """单日期不误伤。"""
    intent = nlu.match("帮我约明天的琴房，八点到九点")
    assert intent is not None
    assert intent.operation == "create_reservation"
    assert intent.arguments["start"] == "08:00"
    assert intent.arguments["end"] == "09:00"
    assert intent.arguments["natural_date"] == 1  # 日期词 → 自然日偏移（Resolver 换算业务日）
    intent = nlu.match("帮我看看303今天有没有人")
    assert intent is not None
    assert intent.operation == "query_schedule"


def test_nlu_time_digits_not_room(nlu: NLUIntentMatcher) -> None:
    """时间数字（12点）不能被当作房间号。"""
    intent = nlu.match("帮我取消今天的预约，预约明天中午12点到下午1点的303")
    assert intent is None  # 复合指令拒绝（不解析成 create + room=12）


def test_parser_without_nlu_keeps_original_behavior() -> None:
    parser = QQCommandParser()
    with pytest.raises(ParseError):
        parser.parse("明天下午帮我看看303有没有空")
    intent = parser.parse("预约 303 7-8")
    assert intent.operation == "create_reservation"


def test_admin_never_enters_nlu(nlu_parser: QQCommandParser) -> None:
    # # 前缀：即使 NLU 开启，admin 也走严格正则（文档 4.5 硬隔离）
    with pytest.raises(ParseError):
        nlu_parser.parse("#清空预约 明天下午帮我看看303")
    # 合法 admin 指令不受影响
    intent = nlu_parser.parse("#备份用户")
    assert intent.admin is True
    assert intent.operation == "backup_users"


# —— Resolver 集成：NLU 输出可被现有 Resolver 消费 ——


def test_resolver_accepts_nlu_reservation(yql_config) -> None:
    from qqbot.application.resolver import CommandResolver

    parsed = QQCommandParser(nlu=NLUIntentMatcher()).parse("约琴房明天7-8")
    resolved = CommandResolver().resolve(
        parsed,
        yql_config,
        datetime(2026, 8, 7, 21, 0, tzinfo=SHANGHAI_TZ),
    )
    assert isinstance(resolved, CreateReservation)
    assert resolved.room_id == "yql-main"
    assert resolved.business_offset == 1


def test_resolver_accepts_nlu_cancel(yql_config) -> None:
    from qqbot.application.resolver import CommandResolver

    parsed = QQCommandParser(nlu=NLUIntentMatcher()).parse("把我明天的预约退了")
    resolved = CommandResolver().resolve(
        parsed,
        yql_config,
        datetime(2026, 8, 7, 21, 0, tzinfo=SHANGHAI_TZ),
    )
    assert isinstance(resolved, CancelReservation)
    assert resolved.business_offset == 1
    assert resolved.room_id is None


def test_resolver_accepts_nlu_query_free(yqh_config) -> None:
    from qqbot.application.resolver import CommandResolver

    parsed = QQCommandParser(nlu=NLUIntentMatcher()).parse("明天下午帮我看看303有没有空")
    resolved = CommandResolver().resolve(
        parsed,
        yqh_config,
        datetime(2026, 8, 7, 21, 0, tzinfo=SHANGHAI_TZ),
    )
    assert isinstance(resolved, QueryFreeSlots)
    assert resolved.room_id == "yqh-303"


def test_resolver_accepts_nlu_bind(yql_config) -> None:
    from qqbot.application.resolver import CommandResolver

    parsed = QQCommandParser(nlu=NLUIntentMatcher()).parse("我是张三 2023X1234567890")
    resolved = CommandResolver().resolve(
        parsed,
        yql_config,
        datetime(2026, 8, 7, 21, 0, tzinfo=SHANGHAI_TZ),
    )
    assert isinstance(resolved, BindUser)
    assert resolved.display_name == "张三"
    assert resolved.student_id == "2023X1234567890"


def test_nlu_intent_shapes_match_regular_parser(yqh_config) -> None:
    """NLU 与正则 parser 对同一语义输入产出相同的 arguments 形状（Resolver 契约）。"""
    from qqbot.application.resolver import CommandResolver

    regular = QQCommandParser().parse("预约 304b 19-21 +1")
    nlu = QQCommandParser(nlu=NLUIntentMatcher()).parse("约304b明天19-21")
    assert nlu.operation == regular.operation
    assert nlu.arguments["room_reference"] == regular.arguments["room_reference"]
    # 日期语义：NLU 日期词（natural_date=1）与正则显式（offset=1）在 22:00 前等价
    resolved_regular = CommandResolver().resolve(
        regular, yqh_config, datetime(2026, 8, 7, 21, 0, tzinfo=SHANGHAI_TZ)
    )
    resolved_nlu = CommandResolver().resolve(nlu, yqh_config, datetime(2026, 8, 7, 21, 0, tzinfo=SHANGHAI_TZ))
    assert isinstance(resolved_regular, CreateReservation)
    assert isinstance(resolved_nlu, CreateReservation)
    assert resolved_regular.room_id == resolved_nlu.room_id
    assert resolved_regular.time_range == resolved_nlu.time_range


def test_client_constructs_with_nlu_enabled(tmp_path, yqh_config) -> None:
    """回归：nlu_enabled=true 时 PianoBotClient 能正常构造（曾因 _nlu_dir
    初始化顺序错误在 model_path 处 AttributeError，本地测试未覆盖此路径）。"""
    import asyncio
    from dataclasses import replace

    from qqbot.interfaces.qq.client import PianoBotClient

    config = replace(yqh_config, features=replace(yqh_config.features, nlu_enabled=True))

    async def exercise() -> None:
        client = PianoBotClient({"yqh": config}, tmp_path / "control.db")
        try:
            assert client.parser._nlu is not None  # NLU 已挂载
            assert client._nlu_dir is not None
        finally:
            await client.close()

    asyncio.run(exercise())


def test_nlu_query_personal_shapes(yql_config) -> None:
    from qqbot.application.resolver import CommandResolver

    parsed = QQCommandParser(nlu=NLUIntentMatcher()).parse("帮我看看我约了什么")
    resolved = CommandResolver().resolve(
        parsed,
        yql_config,
        datetime(2026, 8, 7, 21, 0, tzinfo=SHANGHAI_TZ),
    )
    assert isinstance(resolved, QueryPersonal)
