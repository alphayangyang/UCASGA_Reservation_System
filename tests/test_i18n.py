"""多语言呈示（2026-09-17）：英文用户（绑定姓名不含汉字）拿英文回执与英文图片。

覆盖三块：
1. ``preferred_language`` 判定规则（挂在绑定姓名上）；
2. ``QQPresenter(config, lang=EN)`` 的英文文案，以及**中文默认输出不回归**；
3. parser 的英文命令别名（book/cancel/my/...）。
"""

from __future__ import annotations

from datetime import date

import pytest

from qqbot.domain.errors import ParseError
from qqbot.domain.models import OperationResult, TimeRange, User
from qqbot.domain.names import (
    LANGUAGE_EN,
    LANGUAGE_ZH,
    preferred_language,
    short_display_name,
)
from qqbot.interfaces.qq.parser import QQCommandParser
from qqbot.interfaces.qq.presenter import EN, PHRASES, QQPresenter

# —— 语言判定 ——


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("张三", LANGUAGE_ZH),
        ("阿依古丽·麦麦提", LANGUAGE_ZH),
        ("John", LANGUAGE_EN),
        ("John Smith", LANGUAGE_EN),
        ("O'Brien", LANGUAGE_EN),
        ("Aleksandra Ivanova", LANGUAGE_EN),
        ("宋厚聿Ной", LANGUAGE_ZH),  # 含汉字 → 中文
        (None, LANGUAGE_ZH),  # 未绑定回落中文
        ("", LANGUAGE_ZH),
    ],
)
def test_preferred_language(name: str | None, expected: str) -> None:
    assert preferred_language(name) == expected


# —— 文案表结构 ——


def test_phrase_table_covers_both_languages() -> None:
    """每个文案 key 必须同时提供中英两种，避免英文用户拿到 KeyError。"""
    missing = [
        key
        for key, table in PHRASES.items()
        if not table.get("zh", "").strip() or not table.get("en", "").strip()
    ]
    assert missing == []


def test_phrase_table_placeholders_match() -> None:
    """中英模板的占位符集合必须一致，否则 .format() 会因缺参崩溃。"""
    import re

    def fields(text: str) -> set[str]:
        return {m.split(":")[0].split(".")[0] for m in re.findall(r"\{([^}]*)\}", text)}

    mismatched = {
        key: (sorted(fields(t["zh"])), sorted(fields(t["en"])))
        for key, t in PHRASES.items()
        if fields(t["zh"]) != fields(t["en"])
    }
    assert mismatched == {}


# —— Presenter ——


def _bound_result(name: str) -> OperationResult:
    return OperationResult.success("user_bound", user=User("u1", name, "2024K8009926001"))


def test_presenter_defaults_to_chinese(yql_config) -> None:
    assert QQPresenter(yql_config).render(_bound_result("张三")) == ("✅ 绑定成功：张三（2024K8009926001）。")


def test_presenter_english_bound(yql_config) -> None:
    presenter = QQPresenter(yql_config, lang=EN)
    assert presenter.render(_bound_result("John Smith")) == (
        "✅ Bound successfully: John Smith (2024K8009926001)."
    )


def test_presenter_english_reservation_created(yql_config) -> None:
    result = OperationResult.success(
        "reservation_created",
        date=date(2026, 9, 18),
        offset=1,
        room_name="303",
        fragments=[TimeRange(1140, 1260)],
    )
    zh = QQPresenter(yql_config).render(result)
    en = QQPresenter(yql_config, lang=EN).render(result)

    assert "✅ 预约成功！" in zh and "日期：2026-09-18（+1）" in zh and "成功时段：19:00-21:00" in zh
    assert "✅ Booked!" in en
    assert "Date: 2026-09-18 (+1)" in en
    assert "Room: 303" in en
    assert "Booked: 19:00-21:00" in en
    assert "预约" not in en


def test_presenter_english_error_messages(yql_config) -> None:
    presenter = QQPresenter(yql_config, lang=EN)
    cases = [
        (OperationResult.failure("not_registered"), "not registered"),
        (OperationResult.failure("permission_denied"), "Permission denied"),
        (OperationResult.failure("database_busy"), "busy"),
        (OperationResult.failure("invalid_name"), "1–24 characters"),
        (OperationResult.failure("duplicate_identity", field="display_name"), "already taken"),
        (
            OperationResult.failure("advance_booking_denied", maximum_offset=2, requested_offset=3),
            "at most +2",
        ),
        (OperationResult.failure("not_found", entity="room"), "Available rooms"),
    ]
    for result, fragment in cases:
        rendered = presenter.render(result)
        assert fragment in rendered, f"{result.code}: {rendered}"


def test_presenter_english_usage_help(yql_config) -> None:
    text = QQPresenter(yql_config, lang=EN).usage("help")
    assert "Piano room assistant commands" in text
    # 命令本身仍是中文（英文命令别名为附加能力），示例要保留可直接复制的形式
    assert "/预约" in text


def test_presenter_chinese_usage_unchanged(yql_config) -> None:
    text = QQPresenter(yql_config).usage("help")
    assert text.startswith("🤖 琴房助手指令：")
    assert "/查询个人" in text


def test_presenter_english_weekday(yql_config) -> None:
    from qqbot.domain.models import Routine

    routine = Routine("r1", 0, "yql-main", TimeRange(1140, 1260), "合唱团")
    zh = QQPresenter(yql_config).render(OperationResult.success("routines", routines=[routine]))
    en = QQPresenter(yql_config, lang=EN).render(OperationResult.success("routines", routines=[routine]))
    assert "[周一]" in zh
    assert "[Mon]" in en


# —— 时间轴/列表里的姓名缩写 ——


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("张三", "张三"),
        ("欧阳娜娜", "欧阳娜娜"),
        ("阿依古丽·麦麦提", "·麦麦提"),  # 中文沿用「末 4 字」既有约定
        ("John Smith", "John Smith"),  # 英文不再截成 "mith"
        ("Aleksandra Ivanova", "Aleksandra Ivanova"),
        ("O'Brien", "O'Brien"),
    ],
)
def test_short_display_name(name: str, expected: str) -> None:
    assert short_display_name(name) == expected


def test_presenter_schedule_keeps_english_name_intact(yql_config) -> None:
    """回归：英文姓名曾被 item.label[-4:] 截成「mith」。"""
    from qqbot.domain.models import Occupancy

    result = OperationResult.success(
        "schedule",
        date=date(2026, 9, 18),
        offset=0,
        admin_view=False,
        occupancies=[Occupancy("yql-main", TimeRange(840, 960), "reservation", "John Smith")],
    )
    text = QQPresenter(yql_config, lang=EN).render(result)
    assert "John Smith" in text
    assert "mith " not in text.replace("Smith", "")


# —— 英文命令别名 ——


@pytest.mark.parametrize(
    ("text", "operation"),
    [
        ("book 303 21-22", "create_reservation"),
        ("Book 303 7-8 +1", "create_reservation"),
        ("BOOK 303 7-8", "create_reservation"),
        ("reserve 304a 19-21", "create_reservation"),
        ("cancel 303 19-21", "cancel_reservation"),
        ("my", "query_personal"),
        ("my reservations", "query_personal"),
        ("my bookings", "query_personal"),
        ("free 303", "query_free"),
        ("schedule 303", "query_schedule"),
        ("query", "query_schedule"),
    ],
)
def test_english_command_aliases(text: str, operation: str) -> None:
    assert QQCommandParser().parse(text).operation == operation


def test_english_bind_alias_keeps_name_spaces() -> None:
    intent = QQCommandParser().parse("bind John Smith 2023X1234567890")
    assert intent.operation == "bind_user"
    assert intent.arguments["display_name"] == "John Smith"


def test_english_alias_requires_word_boundary() -> None:
    """「booking」不该被读成 book + 房间「ing」。"""
    with pytest.raises(ParseError):
        QQCommandParser().parse("booking 303")


def test_chinese_commands_still_win_their_own_cases() -> None:
    parser = QQCommandParser()
    assert parser.parse("预约 303 21-22").operation == "create_reservation"
    assert parser.parse("查询个人").operation == "query_personal"


@pytest.mark.parametrize(
    "text",
    [
        "Bind John Smith 2023X1234567890",
        "BIND John Smith 2023X1234567890",
        "BiNd John Smith 2023X1234567890",
        "/Bind John Smith 2023X1234567890",  # 带斜杠前缀也认
        "  Bind John Smith 2023X1234567890  ",  # 首尾空白
    ],
)
def test_english_alias_is_case_insensitive(text: str) -> None:
    intent = QQCommandParser().parse(text)
    assert intent.operation == "bind_user"
    assert intent.arguments["display_name"] == "John Smith"


@pytest.mark.parametrize(
    ("text", "operation"),
    [
        ("BOOK 303 7-8", "create_reservation"),
        ("Book 303 7-8", "create_reservation"),
        ("bOoK 303 7-8", "create_reservation"),
        ("My Reservations", "query_personal"),
        ("CANCEL 303 19-21", "cancel_reservation"),
        ("Schedule", "query_schedule"),
    ],
)
def test_english_commands_ignore_case(text: str, operation: str) -> None:
    assert QQCommandParser().parse(text).operation == operation
