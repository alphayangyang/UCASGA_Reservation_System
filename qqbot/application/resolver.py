from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from qqbot.domain.calendar import BusinessCalendar
from qqbot.domain.commands import (
    AddRoutine,
    AdminCancel,
    AssignRole,
    BackupUsers,
    BindUser,
    BroadcastRoutines,
    CancelAllReservations,
    CancelReservation,
    ClearReservations,
    Command,
    CreateReservation,
    ListRoutines,
    QueryFreeSlots,
    QueryPersonal,
    QuerySchedule,
    RemoveRole,
    RemoveRoutine,
    RestoreUsers,
    TransferOwner,
    UndoClearReservations,
)
from qqbot.domain.errors import InvalidTimeRange, ParseError
from qqbot.domain.models import DateRange, TimeRange
from qqbot.infrastructure.config import SiteConfig
from qqbot.interfaces.qq.parser import ParsedIntent


def _weekday_target(today: date, weekday: int, mode: str) -> date:
    """星期引用 → 绝对日期（周一制：周一是每周第一天，主人 2026-08-23 产品决策）。

    - next：接下来最近的周X（含今天；今天周日说「周三」→ 下周三）
    - this：本周的周X（可能已过 → 由调用方校验拒绝）
    - next_week：下周的周X（今天周日说「下周一」→ 明天——明天即下周第一天）
    - prev_week：上周的周X（恒过去 → 由调用方校验拒绝）
    """
    today_weekday = today.isoweekday()  # 周一=1 … 周日=7
    week_start = today - timedelta(days=today_weekday - 1)  # 本周一
    if mode == "this":
        return week_start + timedelta(days=weekday - 1)
    if mode == "next_week":
        return week_start + timedelta(days=7 + weekday - 1)
    if mode == "prev_week":
        return week_start + timedelta(days=-7 + weekday - 1)
    return today + timedelta(days=(weekday - today_weekday) % 7)  # next

WEEKDAYS = {
    "周一": 0,
    "1": 0,
    "周二": 1,
    "2": 1,
    "周三": 2,
    "3": 2,
    "周四": 3,
    "4": 3,
    "周五": 4,
    "5": 4,
    "周六": 5,
    "6": 5,
    "周日": 6,
    "周天": 6,
    "7": 6,
}


def parse_time(value: str) -> int:
    normalized = value.strip().replace("：", ":")
    if ":" in normalized:
        parts = normalized.split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise InvalidTimeRange("时间格式有误")
        hour, minute = int(parts[0]), int(parts[1])
    elif "." in normalized:
        if not re.fullmatch(r"\d{1,2}\.(?:0|5)", normalized):
            raise InvalidTimeRange("小数时间只能使用 .0 或 .5")
        hour_text, fraction = normalized.split(".")
        hour, minute = int(hour_text), 30 if fraction == "5" else 0
    elif normalized.isdigit():
        hour, minute = int(normalized), 0
    else:
        raise InvalidTimeRange("时间格式有误")

    if hour == 24 and minute == 0:
        return 24 * 60
    if not (0 <= hour <= 23 and minute in (0, 30)):
        raise InvalidTimeRange("时间只能使用整点或半点")
    return hour * 60 + minute


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ParseError("date") from exc


class CommandResolver:
    """将语义 Intent 确定为绝对日期、房间 ID 和半小时 TimeRange。"""

    def resolve(
        self,
        intent: ParsedIntent,
        config: SiteConfig,
        now: datetime,
        actor_role: str = "user",
    ) -> Command:
        calendar = BusinessCalendar(config.business_boundary)
        args = intent.arguments
        operation = intent.operation

        def offset_date() -> tuple[date, int]:
            # 绝对日期（NLU 输出 date=YYYY-MM-DD，自然日语义）：转业务偏移并校验合法性。
            absolute = args.get("date")
            if absolute is not None:
                target = parse_date(str(absolute))
                offset = calendar.offset_of(now, target)
                if offset < 0:
                    raise ParseError("past_date")
                if offset > config.max_query_offset:
                    raise ParseError("offset", maximum=config.max_query_offset)
                return target, offset
            # NLU 自然日词（今天=0/明天=1…）：按用户直觉理解，22:00 后业务日已切到
            # “自然日明天”，故自然日 N 对应业务日 N-1；为负说明“今天”已过 22:00 → 拒绝。
            natural = args.get("natural_date")
            if natural is not None:
                natural = int(natural)
                past_boundary = now.hour * 60 + now.minute >= config.business_boundary
                business = natural - (1 if past_boundary else 0)
                if business < 0:
                    raise ParseError("natural_past")
                if business > config.max_query_offset:
                    raise ParseError("offset", maximum=config.max_query_offset)
                return calendar.resolve_offset(now, business), business
            # 星期引用（周X/下X/这X）：NLU 只给原语，绝对日期在此按周一制换算；
            # 过去（本周已过/上周）→ natural_past 拒绝；超出可预约周期 → offset 拒绝。
            weekday = args.get("weekday")
            if weekday is not None:
                target = _weekday_target(now.date(), int(weekday), str(args.get("week_mode", "next")))
                offset = calendar.offset_of(now, target)
                if offset < 0:
                    raise ParseError("natural_past")
                if offset > config.max_query_offset:
                    raise ParseError("offset", maximum=config.max_query_offset)
                return target, offset
            offset = int(args.get("offset", 0))
            if offset < 0 or offset > config.max_query_offset:
                raise ParseError("offset", maximum=config.max_query_offset)
            return calendar.resolve_offset(now, offset), offset

        def absolute_or_offset_date() -> date:
            if args.get("date"):
                return parse_date(str(args["date"]))
            return offset_date()[0]

        def query_date_range() -> tuple[DateRange, int | None]:
            # NLU 自然日（今天=0/明天=1…）→ 换算业务偏移（22:00 后 -1），再走统一范围逻辑。
            # local_args 为外层 args 的副本（闭包陷阱：函数内赋值即局部，换名避免）。
            local_args = dict(args)
            natural_range = local_args.get("natural_range")
            if natural_range is not None:
                start, end = int(natural_range[0]), int(natural_range[1])
                past_boundary = now.hour * 60 + now.minute >= config.business_boundary
                start = max(start - (1 if past_boundary else 0), 0)
                end = max(end - (1 if past_boundary else 0), 0)
                local_args["range_start"] = f"+{start}"
                local_args["range_end"] = f"+{end}"
            natural = local_args.get("natural_date")
            if natural is not None and "range_start" not in local_args:
                past_boundary = now.hour * 60 + now.minute >= config.business_boundary
                business = int(natural) - (1 if past_boundary else 0)
                if business < 0:
                    raise ParseError("natural_past")
                local_args["range_start"] = f"+{business}"
                local_args["range_end"] = f"+{business}"
            weekday = local_args.get("weekday")
            if weekday is not None and "range_start" not in local_args:
                # 星期引用查询：换算绝对日期（周一制）；过去 → 拒绝；超出可查周期 → 拒绝
                # （与 natural_date 分支的 max_query_offset 校验一致）
                target = _weekday_target(
                    now.date(), int(weekday), str(local_args.get("week_mode", "next"))
                )
                offset = calendar.offset_of(now, target)
                if offset < 0:
                    raise ParseError("natural_past")
                if offset > config.max_query_offset:
                    raise ParseError("offset", maximum=config.max_query_offset)
                local_args["range_start"] = target.isoformat()
                local_args["range_end"] = target.isoformat()

            start_value = local_args.get("range_start")
            end_value = local_args.get("range_end")

            if start_value is None:
                start_offset, end_offset = config.query.default_range(actor_role)
                try:
                    period = DateRange(
                        calendar.resolve_offset(now, start_offset),
                        calendar.resolve_offset(now, end_offset),
                    )
                except OverflowError as exc:
                    raise ParseError(
                        "query_range",
                        maximum=config.query.max_range_days,
                    ) from exc
                first_offset: int | None = start_offset
            else:
                start_text = str(start_value)
                end_text = str(end_value or start_value)
                start_is_offset = start_text.startswith("+")
                end_is_offset = end_text.startswith("+")
                if start_is_offset != end_is_offset:
                    raise ParseError("query_range", maximum=config.query.max_range_days)

                if start_is_offset:
                    start_offset = int(start_text[1:])
                    end_offset = int(end_text[1:])
                    if end_offset < start_offset:
                        raise ParseError("query_range", maximum=config.query.max_range_days)
                    try:
                        period = DateRange(
                            calendar.resolve_offset(now, start_offset),
                            calendar.resolve_offset(now, end_offset),
                        )
                    except OverflowError as exc:
                        raise ParseError("query_range", maximum=config.query.max_range_days) from exc
                    first_offset = start_offset
                else:
                    start_date = parse_date(start_text)
                    end_date = parse_date(end_text)
                    if end_date < start_date:
                        raise ParseError("query_range", maximum=config.query.max_range_days)
                    period = DateRange(start_date, end_date)
                    first_offset = None

            if period.day_count > config.query.max_range_days:
                raise ParseError(
                    "query_range",
                    maximum=config.query.max_range_days,
                    requested=period.day_count,
                )
            return period, first_offset

        def room_id() -> str:
            reference = args.get("room_reference")
            return config.room_by_reference(str(reference) if reference else None).id

        def time_range() -> TimeRange:
            return TimeRange(parse_time(str(args["start"])), parse_time(str(args["end"])))

        if operation == "bind_user":
            return BindUser(str(args["display_name"]), str(args["student_id"]))
        if operation == "create_reservation":
            target, offset = offset_date()
            return CreateReservation(room_id(), target, time_range(), offset)
        if operation == "cancel_reservation":
            if args.get("cancel_all"):
                return CancelAllReservations()
            target, offset = offset_date()
            if "start" not in args:
                return CancelReservation(target, offset)
            return CancelReservation(target, offset, room_id(), time_range())
        if operation == "query_schedule":
            period, first_offset = query_date_range()
            room = (
                config.room_by_reference(args.get("room_reference")).id
                if args.get("room_reference")
                else None
            )
            return QuerySchedule(period, first_offset, room)
        if operation == "query_free":
            period, first_offset = query_date_range()
            room = (
                config.room_by_reference(args.get("room_reference")).id
                if args.get("room_reference")
                else None
            )
            return QueryFreeSlots(period, first_offset, room)
        if operation == "query_personal":
            return QueryPersonal(calendar.business_date(now))
        if operation == "assign_role":
            return AssignRole(str(args["target_name"]), args.get("role"))
        if operation == "remove_role":
            return RemoveRole(str(args["target_name"]))
        if operation == "transfer_owner":
            return TransferOwner(str(args["target_name"]))
        if operation == "admin_cancel":
            return AdminCancel(absolute_or_offset_date(), room_id(), time_range())
        if operation == "clear_reservations":
            return ClearReservations(absolute_or_offset_date())
        if operation == "undo_clear":
            return UndoClearReservations(absolute_or_offset_date())
        if operation == "admin_query":
            room = (
                config.room_by_reference(args.get("room_reference")).id
                if args.get("room_reference")
                else None
            )
            target = parse_date(str(args["date"]))
            return QuerySchedule(DateRange(target, target), None, room, True)
        if operation == "add_routine":
            return AddRoutine(WEEKDAYS[str(args["weekday"])], room_id(), time_range(), str(args["purpose"]))
        if operation == "remove_routine":
            return RemoveRoutine(WEEKDAYS[str(args["weekday"])], room_id(), time_range())
        if operation == "list_routines":
            weekday = WEEKDAYS[str(args["weekday"])] if args.get("weekday") else None
            return ListRoutines(weekday)
        if operation == "broadcast_routines":
            local = calendar.localize(now)
            return BroadcastRoutines(local.date() + timedelta(days=1))
        if operation == "backup_users":
            return BackupUsers()
        if operation == "restore_users":
            return RestoreUsers()
        raise ParseError("help")
