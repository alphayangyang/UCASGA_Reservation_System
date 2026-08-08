from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TypeAlias

from .models import DateRange, TimeRange


@dataclass(frozen=True)
class BindUser:
    display_name: str
    student_id: str


@dataclass(frozen=True)
class CreateReservation:
    room_id: str
    reserve_date: date
    time_range: TimeRange
    business_offset: int


@dataclass(frozen=True)
class CancelReservation:
    reserve_date: date
    business_offset: int
    room_id: str | None = None
    time_range: TimeRange | None = None


@dataclass(frozen=True)
class QuerySchedule:
    date_range: DateRange
    first_business_offset: int | None = None
    room_id: str | None = None
    admin_view: bool = False


@dataclass(frozen=True)
class QueryFreeSlots:
    date_range: DateRange
    first_business_offset: int | None = None
    room_id: str | None = None


@dataclass(frozen=True)
class QueryPersonal:
    from_date: date


@dataclass(frozen=True)
class AssignRole:
    target_name: str
    role: str | None = None


@dataclass(frozen=True)
class RemoveRole:
    target_name: str


@dataclass(frozen=True)
class TransferOwner:
    target_name: str


@dataclass(frozen=True)
class AdminCancel:
    reserve_date: date
    room_id: str
    time_range: TimeRange


@dataclass(frozen=True)
class ClearReservations:
    reserve_date: date


@dataclass(frozen=True)
class UndoClearReservations:
    reserve_date: date


@dataclass(frozen=True)
class AddRoutine:
    weekday: int
    room_id: str
    time_range: TimeRange
    purpose: str


@dataclass(frozen=True)
class RemoveRoutine:
    weekday: int
    room_id: str
    time_range: TimeRange


@dataclass(frozen=True)
class ListRoutines:
    weekday: int | None = None


@dataclass(frozen=True)
class BroadcastRoutines:
    target_date: date


@dataclass(frozen=True)
class BackupUsers:
    pass


@dataclass(frozen=True)
class RestoreUsers:
    pass


Command: TypeAlias = (
    BindUser
    | CreateReservation
    | CancelReservation
    | QuerySchedule
    | QueryFreeSlots
    | QueryPersonal
    | AssignRole
    | RemoveRole
    | TransferOwner
    | AdminCancel
    | ClearReservations
    | UndoClearReservations
    | AddRoutine
    | RemoveRoutine
    | ListRoutines
    | BroadcastRoutines
    | BackupUsers
    | RestoreUsers
)
