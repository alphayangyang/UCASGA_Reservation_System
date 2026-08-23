from __future__ import annotations

import re

from qqbot.application.ports import BookingRepository
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
from qqbot.domain.errors import (
    AdvanceBookingDenied,
    AppError,
    FeatureDisabled,
    InvalidTimeRange,
    NotFound,
    NotRegistered,
    PermissionDenied,
)
from qqbot.domain.models import OperationResult, RequestContext, TimeRange
from qqbot.infrastructure.config import SiteConfig


class BookingApplication:
    """唯一业务入口；QQ、未来网站和定时任务都调用这里。"""

    def __init__(self, config: SiteConfig, repository: BookingRepository) -> None:
        self.config = config
        self.repository = repository
        self.calendar = BusinessCalendar(config.business_boundary)

    def _actor(self, context: RequestContext) -> str:
        if context.actor_user_id is None:
            raise NotRegistered()
        return context.actor_user_id

    def _role(self, context: RequestContext) -> str:
        return self.repository.role_of(self._actor(context))

    def _require_admin(self, context: RequestContext) -> str:
        role = self._role(context)
        if self.config.role_level(role) < self.config.admin_level:
            raise PermissionDenied(required="admin")
        return role

    def _room_name(self, room_id: str) -> str:
        return self.config.room_by_id(room_id).name

    def execute(self, context: RequestContext, command: Command) -> OperationResult:
        if isinstance(command, BindUser):
            return self._bind(context, command)

        actor_id = self._actor(context)

        if isinstance(command, CreateReservation):
            # 即使未来入口不是本项目的 Resolver，也不能绕过房间和业务日校验。
            self.config.room_by_id(command.room_id)
            actual_offset = self.calendar.offset_of(context.received_at, command.reserve_date)
            if actual_offset < 0:
                raise InvalidTimeRange("不能预约已经过去的业务日")
            if actual_offset != command.business_offset:
                raise InvalidTimeRange("请求日期与业务日偏移量不一致")
            role = self._role(context)
            maximum = self.config.maximum_offset(role)
            if command.business_offset > maximum:
                raise AdvanceBookingDenied(command.business_offset, maximum)
            if (
                command.time_range.start < self.config.open_minutes
                or command.time_range.end > self.config.close_minutes
            ):
                raise InvalidTimeRange("预约时段超出琴房开放时间")
            local = self.calendar.localize(context.received_at)
            received_minute = local.hour * 60 + local.minute
            max_single, max_daily = self.config.limits.active(received_minute)
            if command.time_range.duration_minutes > max_single:
                raise InvalidTimeRange(f"单次预约不能超过 {max_single / 60:g} 小时")
            fragments = self.repository.book_available(
                context,
                command.room_id,
                command.reserve_date,
                command.time_range,
                max_daily,
            )
            if not fragments:
                return OperationResult.failure(
                    "reservation_unavailable",
                    date=command.reserve_date,
                    offset=command.business_offset,
                    room_name=self._room_name(command.room_id),
                    requested=command.time_range,
                )
            code = (
                "reservation_created"
                if fragments == [command.time_range]
                else "reservation_partially_created"
            )
            return OperationResult.success(
                code,
                date=command.reserve_date,
                offset=command.business_offset,
                room_name=self._room_name(command.room_id),
                requested=command.time_range,
                fragments=fragments,
            )

        if isinstance(command, CancelAllReservations):
            slots = self.repository.cancel_all_user(
                actor_id, self.calendar.business_date(context.received_at)
            )
            return OperationResult.success(
                "all_reservations_cancelled" if slots else "nothing_to_cancel",
                slots=slots,
            )

        if isinstance(command, CancelReservation):
            slots = self.repository.cancel_user(
                actor_id,
                command.reserve_date,
                command.room_id,
                command.time_range,
            )
            return OperationResult.success(
                "reservation_cancelled" if slots else "nothing_to_cancel",
                date=command.reserve_date,
                offset=command.business_offset,
                slots=slots,
            )

        if isinstance(command, QuerySchedule):
            if command.admin_view:
                self._require_admin(context)
                target = command.date_range.start
                values = self.repository.schedule(target, command.room_id)
                return OperationResult.success(
                    "schedule",
                    date=target,
                    offset=None,
                    occupancies=values,
                    admin_view=True,
                )

            room_ids = [command.room_id] if command.room_id else [room.id for room in self.config.rooms]
            days = []
            for index, target in enumerate(command.date_range.dates()):
                offset = (
                    command.first_business_offset + index
                    if command.first_business_offset is not None
                    else None
                )
                days.append(
                    {
                        "date": target,
                        "offset": offset,
                        "occupancies": self.repository.schedule(target, command.room_id),
                        "admin_view": False,
                    }
                )
            return OperationResult.success(
                "schedule_range",
                date_range=command.date_range,
                room_ids=room_ids,
                days=days,
            )

        if isinstance(command, QueryFreeSlots):
            room_ids = [command.room_id] if command.room_id else [room.id for room in self.config.rooms]
            opening = TimeRange(self.config.open_minutes, self.config.close_minutes)
            days = []
            for index, target in enumerate(command.date_range.dates()):
                offset = (
                    command.first_business_offset + index
                    if command.first_business_offset is not None
                    else None
                )
                days.append(
                    {
                        "date": target,
                        "offset": offset,
                        "slots": self.repository.free_slots(target, room_ids, opening),
                    }
                )
            return OperationResult.success(
                "free_slots_range",
                date_range=command.date_range,
                room_ids=room_ids,
                days=days,
            )

        if isinstance(command, QueryPersonal):
            values = self.repository.personal(actor_id, command.from_date)
            return OperationResult.success("personal_schedule", reservations=values)

        if isinstance(command, AssignRole):
            actor_role = self._require_admin(context)
            actor_level = self.config.role_level(actor_role)
            target = self.repository.user_by_name(command.target_name)
            if target is None:
                raise NotFound("user", name=command.target_name)
            if command.role:
                assigned = command.role
            else:
                candidates = [
                    role for role, level in self.config.role_levels.items() if 0 < level < actor_level
                ]
                if not candidates:
                    raise PermissionDenied(reason="no_assignable_role")
                assigned = max(candidates, key=self.config.role_level)
            if assigned not in self.config.role_levels or not (
                0 < self.config.role_level(assigned) < actor_level
            ):
                raise PermissionDenied(reason="cannot_assign_role", role=assigned)
            self.repository.set_role(target.id, assigned)
            return OperationResult.success("role_assigned", target=target.display_name, role=assigned)

        if isinstance(command, RemoveRole):
            actor_role = self._require_admin(context)
            target = self.repository.user_by_name(command.target_name)
            if target is None:
                raise NotFound("user", name=command.target_name)
            target_role = self.repository.role_of(target.id)
            if self.config.role_level(target_role) <= 0:
                raise NotFound("role", name=command.target_name)
            if self.config.role_level(target_role) >= self.config.role_level(actor_role):
                raise PermissionDenied(reason="cannot_remove_peer_or_superior")
            self.repository.remove_role(target.id)
            return OperationResult.success("role_removed", target=target.display_name)

        if isinstance(command, TransferOwner):
            actor_role = self._role(context)
            if actor_role != self.config.highest_role:
                raise PermissionDenied(required=self.config.highest_role)
            target = self.repository.user_by_name(command.target_name)
            if target is None:
                raise NotFound("user", name=command.target_name)
            lower_roles = [
                role
                for role, level in self.config.role_levels.items()
                if level < self.config.role_level(self.config.highest_role)
            ]
            fallback = max(lower_roles, key=self.config.role_level)
            self.repository.transfer_role(actor_id, target.id, self.config.highest_role, fallback)
            return OperationResult.success(
                "owner_transferred", target=target.display_name, fallback_role=fallback
            )

        if isinstance(command, AdminCancel):
            self._require_admin(context)
            slots = self.repository.cancel_admin(command.reserve_date, command.room_id, command.time_range)
            return OperationResult.success(
                "admin_cancelled" if slots else "nothing_to_cancel",
                date=command.reserve_date,
                slots=slots,
            )

        if isinstance(command, ClearReservations):
            self._require_admin(context)
            count = self.repository.clear_date(command.reserve_date, actor_id)
            return OperationResult.success("date_cleared", date=command.reserve_date, count=count)

        if isinstance(command, UndoClearReservations):
            self._require_admin(context)
            count = self.repository.undo_clear(command.reserve_date, actor_id)
            return OperationResult.success("clear_undone", date=command.reserve_date, count=count)

        if isinstance(command, AddRoutine):
            self._require_admin(context)
            if not self.config.features.weekly_routine:
                raise FeatureDisabled("weekly_routine")
            routine = self.repository.add_routine(
                command.weekday,
                command.room_id,
                command.time_range,
                command.purpose,
                self.calendar.business_date(context.received_at),
            )
            return OperationResult.success("routine_added", routine=routine)

        if isinstance(command, RemoveRoutine):
            self._require_admin(context)
            if not self.config.features.weekly_routine:
                raise FeatureDisabled("weekly_routine")
            removed = self.repository.remove_routine(command.weekday, command.room_id, command.time_range)
            return OperationResult.success("routine_removed" if removed else "routine_not_found")

        if isinstance(command, ListRoutines):
            self._require_admin(context)
            return OperationResult.success(
                "routines", routines=self.repository.list_routines(command.weekday), weekday=command.weekday
            )

        if isinstance(command, BroadcastRoutines):
            self._require_admin(context)
            if not self.config.features.broadcast:
                raise FeatureDisabled("broadcast")
            routines = [item for item in self.repository.list_routines(command.target_date.weekday())]
            return OperationResult.success("routine_broadcast", date=command.target_date, routines=routines)

        if isinstance(command, BackupUsers):
            self._require_admin(context)
            path, count = self.repository.backup_users()
            return OperationResult.success("users_backed_up", count=count, path=path)

        if isinstance(command, RestoreUsers):
            self._require_admin(context)
            count = self.repository.restore_users()
            return OperationResult.success("users_restored", count=count)

        raise NotFound("command")

    def _bind(self, context: RequestContext, command: BindUser) -> OperationResult:
        if not (1 <= len(command.display_name) <= 10) or not re.fullmatch(
            r"[\u4e00-\u9fff]+", command.display_name
        ):
            raise AppError("invalid_name")
        if not re.fullmatch(r"(?:\d{4}[A-Z]\d{10}|\d{15})", command.student_id):
            raise AppError("invalid_student_id")
        year = int(command.student_id[:4])
        current_year = self.calendar.localize(context.received_at).year
        if not (2018 <= year <= current_year):
            raise AppError("invalid_student_year", {"year": year, "maximum": current_year})
        user = self.repository.bind_user(
            context.identity,
            command.display_name,
            command.student_id,
        )
        return OperationResult.success("user_bound", user=user)


class Dispatcher:
    """统一把异常转换成结构化 Result；呈示器无需理解 Python 异常。"""

    def __init__(self, application: BookingApplication) -> None:
        self.application = application

    def dispatch(self, context: RequestContext, command: Command) -> OperationResult:
        try:
            return self.application.execute(context, command)
        except AppError as exc:
            return OperationResult.failure(exc.code, **exc.details)
