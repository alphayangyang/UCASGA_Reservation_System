from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class BusinessCalendar:
    boundary_minutes: int = 22 * 60

    def localize(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=SHANGHAI_TZ)
        return value.astimezone(SHANGHAI_TZ)

    def business_date(self, now: datetime) -> date:
        local = self.localize(now)
        minute = local.hour * 60 + local.minute
        return local.date() + timedelta(days=1 if minute >= self.boundary_minutes else 0)

    def resolve_offset(self, now: datetime, offset: int) -> date:
        return self.business_date(now) + timedelta(days=offset)

    def offset_of(self, now: datetime, target: date) -> int:
        return (target - self.business_date(now)).days
