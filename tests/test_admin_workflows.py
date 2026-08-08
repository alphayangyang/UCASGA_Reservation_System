from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

from qqbot.application.service import BookingApplication, Dispatcher
from qqbot.domain.calendar import SHANGHAI_TZ
from qqbot.domain.commands import (
    AddRoutine,
    AssignRole,
    BackupUsers,
    ClearReservations,
    CreateReservation,
    ListRoutines,
    QuerySchedule,
    RemoveRole,
    RestoreUsers,
    TransferOwner,
    UndoClearReservations,
)
from qqbot.domain.models import DateRange, ExternalIdentity, RequestContext, TimeRange
from qqbot.infrastructure.sqlite_repository import SQLiteBookingRepository


def admin_context(user_id: str) -> RequestContext:
    return RequestContext(
        request_id=str(uuid4()),
        source="qq",
        site_id="site-yqh",
        identity=ExternalIdentity("qq", "owner-external"),
        actor_user_id=user_id,
        received_at=datetime(2026, 8, 7, 12, tzinfo=SHANGHAI_TZ),
    )


def test_role_assignment_removal_and_transfer(yqh_config) -> None:
    repo = SQLiteBookingRepository(yqh_config)
    repo.initialize()
    owner = repo.user_by_external(ExternalIdentity("qq", "owner-external"))
    target = repo.bind_user(ExternalIdentity("qq", "target"), "李四", "2024K8009926002")
    assert owner is not None
    dispatcher = Dispatcher(BookingApplication(yqh_config, repo))
    ctx = admin_context(owner.id)

    assigned = dispatcher.dispatch(ctx, AssignRole("李四", "admin"))
    assert assigned.code == "role_assigned"
    assert repo.role_of(target.id) == "admin"

    removed = dispatcher.dispatch(ctx, RemoveRole("李四"))
    assert removed.code == "role_removed"
    assert repo.role_of(target.id) == "user"

    transferred = dispatcher.dispatch(ctx, TransferOwner("李四"))
    assert transferred.code == "owner_transferred"
    assert repo.role_of(target.id) == "owner"
    assert repo.role_of(owner.id) == "admin"


def test_routine_clear_undo_and_backup_workflow(yqh_config) -> None:
    repo = SQLiteBookingRepository(yqh_config)
    repo.initialize()
    owner = repo.user_by_external(ExternalIdentity("qq", "owner-external"))
    assert owner is not None
    dispatcher = Dispatcher(BookingApplication(yqh_config, repo))
    ctx = admin_context(owner.id)

    routine_result = dispatcher.dispatch(
        ctx,
        AddRoutine(5, "yqh-303", TimeRange(1260, 1320), "合唱团"),
    )
    assert routine_result.code == "routine_added"
    listed = dispatcher.dispatch(ctx, ListRoutines(5))
    assert len(listed.data["routines"]) == 1

    schedule = dispatcher.dispatch(
        ctx,
        QuerySchedule(DateRange(date(2026, 8, 8), date(2026, 8, 8)), None, "yqh-303", True),
    )
    assert any(item.kind == "routine" for item in schedule.data["occupancies"])

    created = dispatcher.dispatch(
        ctx,
        CreateReservation("yqh-303", date(2026, 8, 7), TimeRange(420, 480), 0),
    )
    assert created.code == "reservation_created"
    cleared = dispatcher.dispatch(ctx, ClearReservations(date(2026, 8, 7)))
    assert cleared.data["count"] == 1
    undone = dispatcher.dispatch(ctx, UndoClearReservations(date(2026, 8, 7)))
    assert undone.data["count"] == 1

    backup = dispatcher.dispatch(ctx, BackupUsers())
    assert backup.code == "users_backed_up"
    assert backup.data["path"].exists()
    restored = dispatcher.dispatch(ctx, RestoreUsers())
    assert restored.code == "users_restored"


def test_weekly_routine_feature_switch_is_enforced(yql_config) -> None:
    repo = SQLiteBookingRepository(yql_config)
    repo.initialize()
    owner = repo.user_by_external(ExternalIdentity("qq", "owner-external"))
    assert owner is not None
    ctx = RequestContext(
        request_id=str(uuid4()),
        source="qq",
        site_id=yql_config.site_id,
        identity=ExternalIdentity("qq", "owner-external"),
        actor_user_id=owner.id,
        received_at=datetime(2026, 8, 7, 12, tzinfo=SHANGHAI_TZ),
    )
    result = Dispatcher(BookingApplication(yql_config, repo)).dispatch(
        ctx,
        AddRoutine(5, "yql-main", TimeRange(1260, 1320), "测试"),
    )
    assert result.code == "feature_disabled"
