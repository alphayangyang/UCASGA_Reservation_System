from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

from .errors import InvalidTimeRange


def minutes_to_text(minutes: int) -> str:
    hour, minute = divmod(minutes, 60)
    return f"{hour:02d}:{minute:02d}"


@dataclass(frozen=True, order=True)
class TimeRange:
    start: int
    end: int

    def __post_init__(self) -> None:
        if not (0 <= self.start < self.end <= 24 * 60):
            raise InvalidTimeRange("开始时间必须早于结束时间，且处于 00:00-24:00")
        if self.start % 30 or self.end % 30:
            raise InvalidTimeRange("预约时间必须落在整点或半点")

    @property
    def duration_minutes(self) -> int:
        return self.end - self.start

    def overlaps(self, other: TimeRange) -> bool:
        return self.start < other.end and self.end > other.start

    def clipped_to(self, other: TimeRange) -> TimeRange | None:
        start = max(self.start, other.start)
        end = min(self.end, other.end)
        return TimeRange(start, end) if start < end else None

    def display(self) -> str:
        return f"{minutes_to_text(self.start)}-{minutes_to_text(self.end)}"


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError("开始日期不能晚于结束日期")

    @property
    def day_count(self) -> int:
        return (self.end - self.start).days + 1

    def dates(self) -> tuple[date, ...]:
        return tuple(self.start + timedelta(days=index) for index in range(self.day_count))


@dataclass(frozen=True)
class ExternalIdentity:
    provider: str
    external_id: str


@dataclass(frozen=True)
class User:
    id: str
    display_name: str
    student_id: str | None = None


@dataclass(frozen=True)
class Room:
    id: str
    site_id: str
    name: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    source: Literal["qq", "web", "system"]
    site_id: str
    identity: ExternalIdentity
    actor_user_id: str | None
    received_at: datetime


@dataclass(frozen=True)
class Occupancy:
    room_id: str
    time_range: TimeRange
    kind: Literal["reservation", "routine", "lock"]
    label: str
    user_id: str | None = None


@dataclass(frozen=True)
class CancelledSlot:
    room_id: str
    time_range: TimeRange
    user_name: str | None = None


@dataclass(frozen=True)
class Routine:
    id: str
    weekday: int
    room_id: str
    time_range: TimeRange
    purpose: str


@dataclass(frozen=True)
class OperationResult:
    ok: bool
    code: str
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, code: str, **data: Any) -> OperationResult:
        return cls(True, code, data)

    @classmethod
    def failure(cls, code: str, **data: Any) -> OperationResult:
        return cls(False, code, data)
