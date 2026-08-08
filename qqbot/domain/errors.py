from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AppError(Exception):
    """可安全交给呈示器处理的结构化错误。"""

    code: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.code


class ParseError(AppError):
    def __init__(self, usage: str = "help", **details: Any) -> None:
        super().__init__("parse_error", {"usage": usage, **details})


class NotRegistered(AppError):
    def __init__(self) -> None:
        super().__init__("not_registered")


class PermissionDenied(AppError):
    def __init__(self, **details: Any) -> None:
        super().__init__("permission_denied", details)


class AdvanceBookingDenied(AppError):
    def __init__(self, requested: int, maximum: int) -> None:
        super().__init__(
            "advance_booking_denied",
            {"requested_offset": requested, "maximum_offset": maximum},
        )


class InvalidTimeRange(AppError):
    def __init__(self, reason: str) -> None:
        super().__init__("invalid_time_range", {"reason": reason})


class DailyLimitExceeded(AppError):
    def __init__(self, current_minutes: int, maximum_minutes: int) -> None:
        super().__init__(
            "daily_limit_exceeded",
            {"current_minutes": current_minutes, "maximum_minutes": maximum_minutes},
        )


class DuplicateIdentity(AppError):
    def __init__(self, field_name: str) -> None:
        super().__init__("duplicate_identity", {"field": field_name})


class NotFound(AppError):
    def __init__(self, entity: str, **details: Any) -> None:
        super().__init__("not_found", {"entity": entity, **details})


class FeatureDisabled(AppError):
    def __init__(self, feature: str) -> None:
        super().__init__("feature_disabled", {"feature": feature})


class DatabaseBusy(AppError):
    def __init__(self) -> None:
        super().__init__("database_busy")
