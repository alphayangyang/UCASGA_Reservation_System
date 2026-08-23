from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

from qqbot.application.service import BookingApplication, Dispatcher
from qqbot.domain.calendar import SHANGHAI_TZ
from qqbot.domain.commands import CreateReservation, QueryFreeSlots, QuerySchedule
from qqbot.domain.models import DateRange, ExternalIdentity, RequestContext, TimeRange
from qqbot.infrastructure.sqlite_repository import SQLiteBookingRepository


def make_context(user_id: str, external_id: str = "qq-user") -> RequestContext:
    return RequestContext(
        request_id=str(uuid4()),
        source="qq",
        site_id="site-yql",
        identity=ExternalIdentity("qq", external_id),
        actor_user_id=user_id,
        received_at=datetime(2026, 8, 7, 12, tzinfo=SHANGHAI_TZ),
    )


def test_advance_permission_uses_direct_offsets(yql_config) -> None:
    repo = SQLiteBookingRepository(yql_config)
    repo.initialize()
    user = repo.bind_user(ExternalIdentity("qq", "qq-user"), "张三", "2024K8009926001")
    dispatcher = Dispatcher(BookingApplication(yql_config, repo))
    ctx = make_context(user.id)

    denied = dispatcher.dispatch(
        ctx,
        CreateReservation("yql-main", date(2026, 8, 8), TimeRange(420, 480), 1),
    )
    assert denied.code == "advance_booking_denied"

    repo.set_role(user.id, "band")
    allowed = dispatcher.dispatch(
        ctx,
        CreateReservation("yql-main", date(2026, 8, 8), TimeRange(420, 480), 1),
    )
    assert allowed.code == "reservation_created"


def test_yql_daily_limit_cannot_be_bypassed_with_multiple_requests(yql_config) -> None:
    repo = SQLiteBookingRepository(yql_config)
    repo.initialize()
    user = repo.bind_user(ExternalIdentity("qq", "qq-user"), "张三", "2024K8009926001")
    dispatcher = Dispatcher(BookingApplication(yql_config, repo))
    ctx = make_context(user.id)
    first = dispatcher.dispatch(
        ctx,
        CreateReservation("yql-main", date(2026, 8, 7), TimeRange(420, 480), 0),
    )
    second = dispatcher.dispatch(
        ctx,
        CreateReservation("yql-main", date(2026, 8, 7), TimeRange(480, 540), 0),
    )
    assert first.code == "reservation_created"
    assert second.code == "daily_limit_exceeded"


def test_schedule_and_free_queries_return_each_requested_day(yql_config) -> None:
    repo = SQLiteBookingRepository(yql_config)
    repo.initialize()
    user = repo.bind_user(ExternalIdentity("qq", "qq-user"), "张三", "2024K8009926001")
    dispatcher = Dispatcher(BookingApplication(yql_config, repo))
    ctx = make_context(user.id)
    dispatcher.dispatch(
        ctx,
        CreateReservation("yql-main", date(2026, 8, 7), TimeRange(420, 480), 0),
    )
    period = DateRange(date(2026, 8, 7), date(2026, 8, 9))

    schedule = dispatcher.dispatch(ctx, QuerySchedule(period, 0))
    free = dispatcher.dispatch(ctx, QueryFreeSlots(period, 0))

    assert schedule.code == "schedule_range"
    assert schedule.data["room_ids"] == ["yql-main"]
    assert [item["date"] for item in schedule.data["days"]] == list(period.dates())
    assert len(schedule.data["days"][0]["occupancies"]) == 1
    assert free.code == "free_slots_range"
    assert len(free.data["days"]) == 3
    assert free.data["days"][0]["slots"]["yql-main"][0] == TimeRange(480, 1380)


def test_cancel_all_reservations(yql_config) -> None:
    """「取消全部预约」：软删本人从业务日起全部未来预约（CancelAllReservations 链路）。"""
    from qqbot.domain.commands import CancelAllReservations

    repo = SQLiteBookingRepository(yql_config)
    repo.initialize()
    user = repo.bind_user(ExternalIdentity("qq", "qq-user"), "张三", "2024K8009926001")
    repo.set_role(user.id, "owner")  # 允许跨天预约
    dispatcher = Dispatcher(BookingApplication(yql_config, repo))
    ctx = make_context(user.id)

    # 预约三天
    for offset in (0, 1, 2):
        from qqbot.domain.calendar import BusinessCalendar

        target = BusinessCalendar(yql_config.business_boundary).resolve_offset(ctx.received_at, offset)
        result = dispatcher.dispatch(
            ctx,
            CreateReservation("yql-main", target, TimeRange(420, 480), offset),
        )
        assert result.code == "reservation_created"

    # 取消全部
    result = dispatcher.dispatch(ctx, CancelAllReservations())
    assert result.code == "all_reservations_cancelled"
    assert len(result.data["slots"]) == 3

    # 再取消 → 无匹配
    result = dispatcher.dispatch(ctx, CancelAllReservations())
    assert result.code == "nothing_to_cancel"

    # 过去/未来均已清理：个人查询为空
    from qqbot.domain.calendar import BusinessCalendar

    assert (
        repo.personal(user.id, BusinessCalendar(yql_config.business_boundary).business_date(ctx.received_at))
        == []
    )
