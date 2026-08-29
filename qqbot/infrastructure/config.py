from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from qqbot.domain.errors import NotFound
from qqbot.domain.models import Room


def parse_clock(value: str) -> int:
    try:
        hour_text, minute_text = str(value).split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"时间必须为 HH:MM：{value}") from exc
    if hour == 24 and minute == 0:
        return 24 * 60
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"时间越界：{value}")
    return hour * 60 + minute


@dataclass(frozen=True)
class RoomConfig:
    id: str
    name: str
    aliases: tuple[str, ...] = ()

    def as_domain(self, site_id: str) -> Room:
        return Room(self.id, site_id, self.name, self.aliases)

    def all_references(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.id, self.name, *self.aliases)))


@dataclass(frozen=True)
class BookingLimits:
    regular_start: int = 7 * 60
    rush_start: int = 22 * 60
    regular_single: int = 24 * 60
    regular_daily: int = 24 * 60
    rush_single: int = 90
    rush_daily: int = 90

    def active(self, received_minute: int) -> tuple[int, int]:
        if self.regular_start <= received_minute < self.rush_start:
            return self.regular_single, self.regular_daily
        return self.rush_single, self.rush_daily


@dataclass(frozen=True)
class FeatureConfig:
    advance_booking: bool = False
    weekly_routine: bool = False
    broadcast: bool = False
    nlu_enabled: bool = False
    # NLU 自优化独立开关（任一站点开启即全局生效，与 nlu_enabled 同模式）：
    # nlu_auto_optimize —— 白名单自优化（房间别名 + 闲聊词，scripts/optimize_whitelist.py）
    # nlu_auto_retrain  —— ML 意图模型自动重训（影子验证 + 原子替换，scripts/train_intent.py --auto）
    nlu_auto_optimize: bool = False
    nlu_auto_retrain: bool = False
    # 定时播报开关（qqbot/interfaces/qq/broadcaster.py）：
    # clock_announce   —— 22:00 整点文字报时（对时，解决抢琴房时间争议）
    # silent_end_report —— 静默期结束后自动播报次日预约情况（图片）
    clock_announce: bool = False
    silent_end_report: bool = False


@dataclass(frozen=True)
class RoutineBroadcastConfig:
    """周常定时播报参数（booking.routine_broadcast）。

    time —— 每天播报时刻（分钟制，默认 21:00）；
    days —— 播报从明天起连续 n 天（默认 1，保持只播次日；1～7）。
    """

    time: int = 21 * 60
    days: int = 1


@dataclass(frozen=True)
class QueryConfig:
    max_range_days: int = 7
    default_ranges: dict[str, tuple[int, int]] | None = None
    image_enabled: bool = True

    def default_range(self, role: str) -> tuple[int, int]:
        ranges = self.default_ranges or {"user": (0, 0)}
        return ranges.get(role, ranges.get("user", (0, 0)))


@dataclass(frozen=True)
class SiteConfig:
    bot_id: str
    site_id: str
    bot_name: str
    db_path: Path
    rooms: tuple[RoomConfig, ...]
    open_minutes: int
    close_minutes: int
    business_boundary: int
    silent_start: int
    silent_end: int
    role_levels: dict[str, int]
    advance_offsets: dict[str, int]
    limits: BookingLimits
    features: FeatureConfig
    query: QueryConfig
    default_owner_external_id: str
    routine_broadcast: RoutineBroadcastConfig = RoutineBroadcastConfig()
    appid: str = ""
    secret: str = ""
    max_query_offset: int = 2

    def room_by_reference(self, reference: str | None) -> RoomConfig:
        if reference is None:
            if len(self.rooms) == 1:
                return self.rooms[0]
            raise NotFound("room", reason="room_required")
        wanted = reference.strip().casefold()
        for room in self.rooms:
            if wanted in {item.casefold() for item in room.all_references()}:
                return room
        raise NotFound("room", reference=reference)

    def room_by_id(self, room_id: str) -> RoomConfig:
        for room in self.rooms:
            if room.id == room_id:
                return room
        raise NotFound("room", room_id=room_id)

    def role_level(self, role: str) -> int:
        return self.role_levels.get(role, -1)

    @property
    def highest_role(self) -> str:
        return max(self.role_levels, key=self.role_levels.get)

    @property
    def admin_level(self) -> int:
        return self.role_levels.get("admin", 2)

    def maximum_offset(self, role: str) -> int:
        if not self.features.advance_booking:
            return 0
        return self.advance_offsets.get(role, self.advance_offsets.get("user", 0))

    def is_silent(self, minute_of_day: int) -> bool:
        if self.silent_start <= self.silent_end:
            return self.silent_start <= minute_of_day < self.silent_end
        return minute_of_day >= self.silent_start or minute_of_day < self.silent_end


def _hours_to_minutes(value: Any, default: float) -> int:
    return int(round(float(default if value is None else value) * 60))


def load_site_config(path: str | Path, project_root: str | Path | None = None) -> SiteConfig:
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    root = Path(project_root) if project_root else config_path.parent.parent

    rooms = tuple(
        RoomConfig(
            id=str(item["id"]),
            name=str(item["name"]),
            aliases=tuple(str(alias) for alias in item.get("aliases", [])),
        )
        for item in raw.get("rooms", [])
    )
    if not rooms:
        raise ValueError(f"{config_path}: 至少需要一个房间")

    booking = raw.get("booking", {})
    limit_raw = booking.get("limits", {})
    regular = limit_raw.get("regular", {})
    rush = limit_raw.get("rush", {})
    features_raw = raw.get("features", {})
    query_raw = raw.get("query", {})
    max_range_days = int(query_raw.get("max_range_days", 7))
    default_ranges_raw = query_raw.get("default_ranges", {})

    default_ranges: dict[str, tuple[int, int]] = {}
    for role in raw.get("roles", {}).get("levels", {}):
        fallback_end = int(booking.get("advance_offsets", {}).get(role, 0))
        raw_range = default_ranges_raw.get(role, [0, min(fallback_end, max_range_days - 1)])
        if not isinstance(raw_range, (list, tuple)) or len(raw_range) != 2:
            raise ValueError(f"{config_path}: query.default_ranges.{role} 必须是 [开始偏移, 结束偏移]")
        default_ranges[str(role)] = (int(raw_range[0]), int(raw_range[1]))

    db_path = Path(raw.get("database", {}).get("path", f"data/{raw['bot_id']}/piano_room.db"))
    if not db_path.is_absolute():
        db_path = root / db_path

    credentials = raw.get("credentials", {})
    appid_env = credentials.get("appid_env", "QQBOT_APPID")
    secret_env = credentials.get("secret_env", "QQBOT_SECRET")

    config = SiteConfig(
        bot_id=str(raw["bot_id"]),
        site_id=str(raw["site_id"]),
        bot_name=str(raw.get("bot_name", raw["bot_id"])),
        db_path=db_path,
        rooms=rooms,
        open_minutes=parse_clock(booking.get("open_time", "07:00")),
        close_minutes=parse_clock(booking.get("close_time", "23:00")),
        business_boundary=parse_clock(booking.get("business_day_boundary", "22:00")),
        silent_start=parse_clock(booking.get("silent_period", {}).get("start", "22:00")),
        silent_end=parse_clock(booking.get("silent_period", {}).get("end", "22:15")),
        role_levels={str(k): int(v) for k, v in raw.get("roles", {}).get("levels", {}).items()},
        advance_offsets={str(k): int(v) for k, v in booking.get("advance_offsets", {}).items()},
        limits=BookingLimits(
            regular_start=parse_clock(limit_raw.get("regular_start", "07:00")),
            rush_start=parse_clock(limit_raw.get("rush_start", "22:00")),
            regular_single=_hours_to_minutes(regular.get("max_single_hours"), 24),
            regular_daily=_hours_to_minutes(regular.get("max_daily_hours"), 24),
            rush_single=_hours_to_minutes(rush.get("max_single_hours"), 1.5),
            rush_daily=_hours_to_minutes(rush.get("max_daily_hours"), 1.5),
        ),
        features=FeatureConfig(
            advance_booking=bool(features_raw.get("advance_booking", False)),
            weekly_routine=bool(features_raw.get("weekly_routine", False)),
            broadcast=bool(features_raw.get("broadcast", False)),
            nlu_enabled=bool(features_raw.get("nlu_enabled", False)),
            nlu_auto_optimize=bool(features_raw.get("nlu_auto_optimize", False)),
            nlu_auto_retrain=bool(features_raw.get("nlu_auto_retrain", False)),
            clock_announce=bool(features_raw.get("clock_announce", False)),
            silent_end_report=bool(features_raw.get("silent_end_report", False)),
        ),
        query=QueryConfig(
            max_range_days=max_range_days,
            default_ranges=default_ranges,
            image_enabled=bool(query_raw.get("image_enabled", True)),
        ),
        routine_broadcast=RoutineBroadcastConfig(
            time=parse_clock(booking.get("routine_broadcast", {}).get("time", "21:00")),
            days=int(booking.get("routine_broadcast", {}).get("days", 1)),
        ),
        default_owner_external_id=os.getenv(
            "QQBOT_OWNER_EXTERNAL_ID", str(raw.get("default_owner_external_id", ""))
        ),
        appid=os.getenv(appid_env, credentials.get("appid", "")),
        secret=os.getenv(secret_env, credentials.get("secret", "")),
        max_query_offset=int(booking.get("max_query_offset", 2)),
    )
    if config.open_minutes >= config.close_minutes:
        raise ValueError(f"{config_path}: open_time 必须早于 close_time")
    if not config.role_levels or "user" not in config.role_levels:
        raise ValueError(f"{config_path}: roles.levels 必须包含 user")
    if not 1 <= config.query.max_range_days <= 7:
        raise ValueError(f"{config_path}: query.max_range_days 必须在 1～7 之间")
    for role, (start, end) in (config.query.default_ranges or {}).items():
        if start < 0 or end < start:
            raise ValueError(f"{config_path}: query.default_ranges.{role} 范围无效")
        if end - start + 1 > config.query.max_range_days:
            raise ValueError(
                f"{config_path}: query.default_ranges.{role} 不能超过 {config.query.max_range_days} 天"
            )
    room_ids = [room.id for room in config.rooms]
    if len(room_ids) != len(set(room_ids)):
        raise ValueError(f"{config_path}: 房间 ID 重复")
    if not 1 <= config.routine_broadcast.days <= 7:
        raise ValueError(f"{config_path}: booking.routine_broadcast.days 必须在 1～7 之间")
    return config


def load_all_configs(config_dir: str | Path, project_root: str | Path | None = None) -> dict[str, SiteConfig]:
    configs: dict[str, SiteConfig] = {}
    for path in sorted(Path(config_dir).glob("*.yaml")):
        config = load_site_config(path, project_root=project_root)
        if config.bot_id in configs:
            raise ValueError(f"重复的 bot_id：{config.bot_id}")
        configs[config.bot_id] = config
    if not configs:
        raise RuntimeError("configs/ 中没有可用的 YAML 配置")
    return configs
