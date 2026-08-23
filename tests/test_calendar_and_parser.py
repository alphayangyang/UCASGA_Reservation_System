from __future__ import annotations

from datetime import date, datetime

import pytest

from qqbot.application.resolver import CommandResolver, parse_time
from qqbot.domain.calendar import SHANGHAI_TZ, BusinessCalendar
from qqbot.domain.commands import (
    AddRoutine,
    AdminCancel,
    AssignRole,
    BackupUsers,
    CancelReservation,
    ClearReservations,
    CreateReservation,
    ListRoutines,
    QueryFreeSlots,
    QuerySchedule,
    RemoveRole,
    RemoveRoutine,
    RestoreUsers,
    TransferOwner,
    UndoClearReservations,
)
from qqbot.domain.errors import InvalidTimeRange, ParseError
from qqbot.interfaces.qq.parser import QQCommandParser


def test_business_day_changes_exactly_at_2200() -> None:
    calendar = BusinessCalendar(22 * 60)
    assert calendar.resolve_offset(datetime(2026, 8, 7, 21, 59, tzinfo=SHANGHAI_TZ), 0) == date(2026, 8, 7)
    assert calendar.resolve_offset(datetime(2026, 8, 7, 22, 0, tzinfo=SHANGHAI_TZ), 0) == date(2026, 8, 8)
    assert calendar.resolve_offset(datetime(2026, 8, 7, 22, 0, tzinfo=SHANGHAI_TZ), 2) == date(2026, 8, 10)


@pytest.mark.parametrize(
    ("text", "minutes"),
    [("7", 420), ("7.0", 420), ("7.5", 450), ("07:30", 450), ("24:00", 1440)],
)
def test_supported_time_formats(text: str, minutes: int) -> None:
    assert parse_time(text) == minutes


@pytest.mark.parametrize("text", ["7.25", "21:03", "21:60", "24:30", "hello"])
def test_rejects_non_half_hour_time(text: str) -> None:
    with pytest.raises(InvalidTimeRange):
        parse_time(text)


def test_parser_and_resolver_create_absolute_command(yql_config) -> None:
    intent = QQCommandParser().parse("/预约 玉泉路琴房 21-22.5 +1")
    command = CommandResolver().resolve(
        intent,
        yql_config,
        datetime(2026, 8, 7, 21, 0, tzinfo=SHANGHAI_TZ),
    )
    assert isinstance(command, CreateReservation)
    assert command.room_id == "yql-main"
    assert command.reserve_date == date(2026, 8, 8)
    assert command.time_range.start == 21 * 60
    assert command.time_range.end == 22 * 60 + 30


def test_single_room_may_be_omitted(yql_config) -> None:
    intent = QQCommandParser().parse("预约7-8.5")
    command = CommandResolver().resolve(
        intent,
        yql_config,
        datetime(2026, 8, 7, 12, 0, tzinfo=SHANGHAI_TZ),
    )
    assert isinstance(command, CreateReservation)
    assert command.room_id == "yql-main"


def test_cancel_offset_only_means_cancel_whole_day(yql_config) -> None:
    intent = QQCommandParser().parse("/取消 +1")
    command = CommandResolver().resolve(
        intent,
        yql_config,
        datetime(2026, 8, 7, 12, 0, tzinfo=SHANGHAI_TZ),
    )
    assert isinstance(command, CancelReservation)
    assert command.room_id is None
    assert command.time_range is None
    assert command.reserve_date == date(2026, 8, 8)


def test_query_defaults_to_offset_zero(yql_config) -> None:
    command = CommandResolver().resolve(
        QQCommandParser().parse("/查询"),
        yql_config,
        datetime(2026, 8, 7, 22, 1, tzinfo=SHANGHAI_TZ),
    )
    assert isinstance(command, QuerySchedule)
    assert command.date_range.start == date(2026, 8, 8)
    assert command.date_range.end == date(2026, 8, 8)


def test_query_default_range_depends_on_role(yql_config) -> None:
    now = datetime(2026, 8, 7, 22, 1, tzinfo=SHANGHAI_TZ)
    resolver = CommandResolver()
    intent = QQCommandParser().parse("/查询")

    band = resolver.resolve(intent, yql_config, now, actor_role="band")
    owner = resolver.resolve(intent, yql_config, now, actor_role="owner")

    assert isinstance(band, QuerySchedule)
    assert band.date_range.start == date(2026, 8, 8)
    assert band.date_range.end == date(2026, 8, 9)
    assert isinstance(owner, QuerySchedule)
    assert owner.date_range.start == date(2026, 8, 8)
    assert owner.date_range.end == date(2026, 8, 14)


@pytest.mark.parametrize(
    ("text", "expected_type", "start", "end"),
    [
        ("/查询 +0~+6", QuerySchedule, date(2026, 8, 7), date(2026, 8, 13)),
        (
            "/查询 玉泉路 2026-08-10~2026-08-16",
            QuerySchedule,
            date(2026, 8, 10),
            date(2026, 8, 16),
        ),
        ("/空闲 +1", QueryFreeSlots, date(2026, 8, 8), date(2026, 8, 8)),
    ],
)
def test_query_accepts_single_day_and_seven_day_ranges(
    yql_config,
    text: str,
    expected_type: type,
    start: date,
    end: date,
) -> None:
    command = CommandResolver().resolve(
        QQCommandParser().parse(text),
        yql_config,
        datetime(2026, 8, 7, 12, tzinfo=SHANGHAI_TZ),
    )
    assert isinstance(command, expected_type)
    assert command.date_range.start == start
    assert command.date_range.end == end


@pytest.mark.parametrize(
    "text",
    [
        "/查询 +0~+7",
        "/空闲 2026-08-10~2026-08-17",
        "/查询 +2~+1",
        "/查询 +0~2026-08-10",
    ],
)
def test_query_rejects_invalid_or_long_ranges(yql_config, text: str) -> None:
    with pytest.raises(ParseError) as caught:
        CommandResolver().resolve(
            QQCommandParser().parse(text),
            yql_config,
            datetime(2026, 8, 7, 12, tzinfo=SHANGHAI_TZ),
        )
    assert caught.value.details["usage"] == "query_range"


def test_deprecated_command_is_explicitly_rejected() -> None:
    with pytest.raises(ParseError) as caught:
        QQCommandParser().parse("超前查询")
    assert caught.value.details["usage"] == "deprecated_offset"


@pytest.mark.parametrize(
    ("text", "expected_type"),
    [
        ("#添加管理 李四 band", AssignRole),
        ("#删除管理 李四", RemoveRole),
        ("#转让群主 李四", TransferOwner),
        ("#取消 玉泉路琴房 21-22.5 +1", AdminCancel),
        ("#清空预约 +1", ClearReservations),
        ("#撤销清空 +1", UndoClearReservations),
        ("#查询 2026-08-20", QuerySchedule),
        ("#添加周常 周一 玉泉路琴房 21-22.5 合唱团", AddRoutine),
        ("#删除周常 周一 玉泉路琴房 21-22.5", RemoveRoutine),
        ("#查询周常 周一", ListRoutines),
        ("#备份用户", BackupUsers),
        ("#恢复用户", RestoreUsers),
    ],
)
def test_admin_command_matrix(yql_config, text: str, expected_type: type) -> None:
    intent = QQCommandParser().parse(text)
    command = CommandResolver().resolve(
        intent,
        yql_config,
        datetime(2026, 8, 7, 12, tzinfo=SHANGHAI_TZ),
    )
    assert isinstance(command, expected_type)
