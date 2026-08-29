from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

import pytest

from qqbot.application.service import BookingApplication, Dispatcher
from qqbot.domain.calendar import SHANGHAI_TZ
from qqbot.domain.commands import AddLock, QuerySchedule, RemoveLock
from qqbot.domain.errors import NotFound
from qqbot.domain.models import (
    CoveredRoutine,
    DateRange,
    ExternalIdentity,
    Occupancy,
    OperationResult,
    RequestContext,
    TimeRange,
)
from qqbot.infrastructure.sqlite_repository import SQLiteBookingRepository
from qqbot.interfaces.qq.parser import QQCommandParser
from qqbot.interfaces.qq.presenter import QQPresenter


def make_repo(config) -> SQLiteBookingRepository:
    repo = SQLiteBookingRepository(config)
    repo.initialize()
    return repo


def admin_context(user_id: str) -> RequestContext:
    return RequestContext(
        request_id=str(uuid4()),
        source="qq",
        site_id="site-yqh",
        identity=ExternalIdentity("qq", "owner-external"),
        actor_user_id=user_id,
        received_at=datetime(2026, 8, 7, 12, tzinfo=SHANGHAI_TZ),
    )


def lock_result(dispatcher, ctx, **kwargs) -> OperationResult:
    return dispatcher.dispatch(ctx, AddLock(**kwargs))


# ---------- Repository 层 ----------


def test_add_lock_writes_and_appears_in_schedule(yqh_config) -> None:
    repo = make_repo(yqh_config)
    target = date(2026, 8, 10)

    slot, covered = repo.add_lock("yqh-303", target, TimeRange(1140, 1260), "临时活动")

    assert slot.room_id == "yqh-303"
    assert slot.label == "临时活动"
    assert covered == []
    occupancies = repo.schedule(target)
    locks = [item for item in occupancies if item.kind == "lock"]
    assert len(locks) == 1
    assert locks[0].time_range == TimeRange(1140, 1260)
    assert locks[0].label == "临时活动"


def test_add_lock_rejects_overlapping_lock(yqh_config) -> None:
    repo = make_repo(yqh_config)
    target = date(2026, 8, 10)
    repo.add_lock("yqh-303", target, TimeRange(1140, 1260), "已有")

    with pytest.raises(NotFound) as exc:
        repo.add_lock("yqh-303", target, TimeRange(1170, 1320), "重叠")
    assert exc.value.details["entity"] == "lock_slot"
    assert exc.value.details.get("reason") == "lock_conflict"


def test_add_lock_rejects_overlapping_reservation(yqh_config) -> None:
    repo = make_repo(yqh_config)
    target = date(2026, 8, 10)
    user = repo.bind_user(ExternalIdentity("qq", "qq-user"), "张三", "2024K8009926001")
    ctx = RequestContext(
        request_id=str(uuid4()),
        source="qq",
        site_id="site-yqh",
        identity=ExternalIdentity("qq", "qq-user"),
        actor_user_id=user.id,
        received_at=datetime(2026, 8, 7, 12, tzinfo=SHANGHAI_TZ),
    )
    repo.book_available(ctx, "yqh-303", target, TimeRange(1140, 1200), 1440)

    with pytest.raises(NotFound) as exc:
        repo.add_lock("yqh-303", target, TimeRange(1170, 1260), "重叠预约")
    assert exc.value.details.get("reason") == "reservation_conflict"


def test_add_lock_allows_overlapping_routine_and_reports_cover(yqh_config) -> None:
    repo = make_repo(yqh_config)
    target = date(2026, 8, 10)  # 周一
    repo.add_routine(target.weekday(), "yqh-303", TimeRange(1140, 1260), "合唱团", date(2026, 1, 1))

    slot, covered = repo.add_lock("yqh-303", target, TimeRange(1140, 1320), "临时活动")

    assert slot.time_range == TimeRange(1140, 1320)
    assert len(covered) == 1
    assert covered[0].purpose == "合唱团"
    assert covered[0].original == TimeRange(1140, 1260)
    assert covered[0].covered == TimeRange(1140, 1260)  # 整条覆盖


def test_add_lock_partial_overlap_crops_routine_in_schedule(yqh_config) -> None:
    repo = make_repo(yqh_config)
    target = date(2026, 8, 10)  # 周一
    repo.add_routine(target.weekday(), "yqh-303", TimeRange(1140, 1260), "合唱团", date(2026, 1, 1))

    # 锁定 20:00-22:00 只覆盖周常 19:00-21:00 的后半段
    _, covered = repo.add_lock("yqh-303", target, TimeRange(1200, 1320), "临时活动")
    assert covered[0].covered == TimeRange(1200, 1260)

    routines = [item for item in repo.schedule(target) if item.kind == "routine"]
    assert routines == [Occupancy("yqh-303", TimeRange(1140, 1200), "routine", "合唱团")]


def test_unlock_restores_routine_visibility(yqh_config) -> None:
    repo = make_repo(yqh_config)
    target = date(2026, 8, 10)  # 周一
    repo.add_routine(target.weekday(), "yqh-303", TimeRange(1140, 1260), "合唱团", date(2026, 1, 1))
    repo.add_lock("yqh-303", target, TimeRange(1140, 1320), "临时活动")
    assert not [item for item in repo.schedule(target) if item.kind == "routine"]

    repo.remove_lock("yqh-303", target, TimeRange(1140, 1320))

    routines = [item for item in repo.schedule(target) if item.kind == "routine"]
    assert len(routines) == 1
    assert routines[0].time_range == TimeRange(1140, 1260)


def test_lock_overrides_routine_in_free_slots(yqh_config) -> None:
    repo = make_repo(yqh_config)
    target = date(2026, 8, 10)  # 周一
    repo.add_routine(target.weekday(), "yqh-303", TimeRange(1140, 1200), "合唱团", date(2026, 1, 1))
    repo.add_lock("yqh-303", target, TimeRange(1140, 1260), "临时活动")

    opening = TimeRange(7 * 60, 23 * 60)
    free = repo.free_slots(target, ["yqh-303"], opening)["yqh-303"]
    # 周常 19:00-20:00 被锁定覆盖：该时段不再是周常占用（但被锁定时段占着，仍不可约）
    assert all(not slot.overlaps(TimeRange(1140, 1260)) for slot in free)


def test_remove_lock_matches_exactly(yqh_config) -> None:
    repo = make_repo(yqh_config)
    target = date(2026, 8, 10)
    repo.add_lock("yqh-303", target, TimeRange(1140, 1260), "临时活动")

    assert repo.remove_lock("yqh-303", target, TimeRange(1140, 1260)) is True
    assert repo.remove_lock("yqh-303", target, TimeRange(1140, 1260)) is False
    assert not [item for item in repo.schedule(target) if item.kind == "lock"]


# ---------- Application 层 ----------


def test_add_lock_requires_admin(yqh_config) -> None:
    repo = make_repo(yqh_config)
    user = repo.bind_user(ExternalIdentity("qq", "normal"), "王五", "2024K8009926003")
    dispatcher = Dispatcher(BookingApplication(yqh_config, repo))
    ctx = RequestContext(
        request_id=str(uuid4()),
        source="qq",
        site_id="site-yqh",
        identity=ExternalIdentity("qq", "normal"),
        actor_user_id=user.id,
        received_at=datetime(2026, 8, 7, 12, tzinfo=SHANGHAI_TZ),
    )

    result = dispatcher.dispatch(ctx, AddLock("yqh-303", date(2026, 8, 10), TimeRange(1140, 1260), "x"))
    assert result.code == "permission_denied"


def test_add_lock_rejects_past_date_and_out_of_hours(yqh_config) -> None:
    repo = make_repo(yqh_config)
    owner = repo.user_by_external(ExternalIdentity("qq", "owner-external"))
    assert owner is not None
    dispatcher = Dispatcher(BookingApplication(yqh_config, repo))
    ctx = admin_context(owner.id)

    past = dispatcher.dispatch(ctx, AddLock("yqh-303", date(2026, 8, 1), TimeRange(1140, 1260), "x"))
    assert past.code == "invalid_time_range"

    out_of_hours = dispatcher.dispatch(ctx, AddLock("yqh-303", date(2026, 8, 10), TimeRange(60, 120), "x"))
    assert out_of_hours.code == "invalid_time_range"


def test_add_and_remove_lock_full_flow(yqh_config) -> None:
    repo = make_repo(yqh_config)
    owner = repo.user_by_external(ExternalIdentity("qq", "owner-external"))
    assert owner is not None
    dispatcher = Dispatcher(BookingApplication(yqh_config, repo))
    ctx = admin_context(owner.id)
    target = date(2026, 8, 10)

    added = dispatcher.dispatch(ctx, AddLock("yqh-303", target, TimeRange(1140, 1260), "临时活动"))
    assert added.code == "lock_added"

    # 普通查询能看到锁定占用
    query = dispatcher.dispatch(ctx, QuerySchedule(DateRange(target, target), None, None))
    assert query.code == "schedule_range"
    kinds = {item.kind for day in query.data["days"] for item in day["occupancies"]}
    assert "lock" in kinds

    removed = dispatcher.dispatch(ctx, RemoveLock("yqh-303", target, TimeRange(1140, 1260)))
    assert removed.code == "lock_removed"

    missing = dispatcher.dispatch(ctx, RemoveLock("yqh-303", target, TimeRange(1140, 1260)))
    assert missing.code == "lock_not_found"


# ---------- Parser / Resolver 层 ----------


def test_parse_lock_command(yqh_config) -> None:
    parser = QQCommandParser()

    intent = parser.parse("#锁定 303 19-21 2026-08-10 临时活动")
    assert intent.operation == "add_lock"
    assert intent.admin is True
    assert intent.arguments == {
        "room_reference": "303",
        "start": "19",
        "end": "21",
        "date": "2026-08-10",
        "purpose": "临时活动",
    }

    defaulted = parser.parse("#锁定 303 19-21")
    assert defaulted.arguments["date"] is None
    assert defaulted.arguments["purpose"] == "临时锁定"

    unlocked = parser.parse("#解锁 303 19-21 2026-08-10")
    assert unlocked.operation == "remove_lock"
    assert unlocked.arguments["purpose"] is None


def test_parse_lock_rejects_bad_format(yqh_config) -> None:
    from qqbot.domain.errors import ParseError

    parser = QQCommandParser()
    with pytest.raises(ParseError) as exc:
        parser.parse("#锁定 303 2026-08-10 活动")
    assert exc.value.details["usage"] == "lock"
    with pytest.raises(ParseError) as exc:
        parser.parse("#解锁 303 19-21 2026-08-10 活动")
    assert exc.value.details["usage"] == "unlock"


def test_lock_resolves_to_absolute_date_without_offset_limit(yqh_config) -> None:
    from qqbot.application.resolver import CommandResolver
    from qqbot.domain.calendar import SHANGHAI_TZ

    parser = QQCommandParser()
    resolver = CommandResolver()
    now = datetime(2026, 8, 7, 12, tzinfo=SHANGHAI_TZ)

    intent = parser.parse("#锁定 303 19-21 2026-08-20 活动")
    command = resolver.resolve(intent, yqh_config, now, actor_role="owner")
    assert isinstance(command, AddLock)
    assert command.reserve_date == date(2026, 8, 20)  # 绝对日期不受 +2 限制
    assert command.room_id == "yqh-303"
    assert command.time_range == TimeRange(1140, 1260)
    assert command.label == "活动"


# ---------- Presenter 层 ----------


def test_presenter_lock_messages(yqh_config) -> None:
    presenter = QQPresenter(yqh_config)
    target = date(2026, 8, 10)
    time_range = TimeRange(1140, 1260)

    added = presenter.render(
        OperationResult.success(
            "lock_added", date=target, room_name="303", time_range=time_range, label="临时活动"
        )
    )
    assert "已锁定" in added and "2026-08-10" in added and "19:00-21:00" in added and "临时活动" in added
    assert "覆盖周常" not in added

    with_cover = presenter.render(
        OperationResult.success(
            "lock_added",
            date=target,
            room_name="303",
            time_range=TimeRange(1140, 1320),
            label="临时活动",
            covered=[CoveredRoutine("合唱团", TimeRange(1140, 1260), TimeRange(1140, 1260))],
        )
    )
    assert "⚠️ 覆盖周常" in with_cover and "合唱团" in with_cover and "19:00-21:00" in with_cover

    removed = presenter.render(
        OperationResult.success("lock_removed", date=target, room_name="303", time_range=time_range)
    )
    assert "已解锁" in removed

    missing = presenter.render(
        OperationResult.success("lock_not_found", date=target, room_name="303", time_range=time_range)
    )
    assert "没有找到匹配的锁定时段" in missing
