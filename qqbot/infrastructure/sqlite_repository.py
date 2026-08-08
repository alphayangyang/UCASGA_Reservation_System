from __future__ import annotations

import csv
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from qqbot.domain.errors import (
    DailyLimitExceeded,
    DatabaseBusy,
    DuplicateIdentity,
    NotFound,
)
from qqbot.domain.models import (
    CancelledSlot,
    ExternalIdentity,
    Occupancy,
    RequestContext,
    Routine,
    TimeRange,
    User,
)
from qqbot.infrastructure.config import SiteConfig

LEGACY_NAMESPACE = UUID("269acae4-dc0c-4b38-a386-f2cf81af1502")


SCHEMA = """
CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_users (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    student_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_app_users_name ON app_users(display_name);
CREATE INDEX IF NOT EXISTS idx_app_users_student ON app_users(student_id);

CREATE TABLE IF NOT EXISTS app_identities (
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES app_users(id),
    PRIMARY KEY(provider, external_id)
);

CREATE TABLE IF NOT EXISTS app_roles (
    site_id TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES app_users(id),
    role TEXT NOT NULL,
    PRIMARY KEY(site_id, user_id)
);

CREATE TABLE IF NOT EXISTS app_rooms (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS app_reservations (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES app_users(id),
    room_id TEXT NOT NULL REFERENCES app_rooms(id),
    reserve_date TEXT NOT NULL,
    start_min INTEGER NOT NULL,
    end_min INTEGER NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    deleted_at TEXT,
    delete_batch_id TEXT,
    CHECK(start_min >= 0 AND end_min <= 1440 AND start_min < end_min),
    CHECK(start_min % 30 = 0 AND end_min % 30 = 0)
);
CREATE INDEX IF NOT EXISTS idx_app_reservations_slot
    ON app_reservations(site_id, reserve_date, room_id, start_min, end_min)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_app_reservations_user
    ON app_reservations(site_id, user_id, reserve_date)
    WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS app_weekly_routines (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    weekday INTEGER NOT NULL,
    room_id TEXT NOT NULL REFERENCES app_rooms(id),
    start_min INTEGER NOT NULL,
    end_min INTEGER NOT NULL,
    purpose TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK(weekday BETWEEN 0 AND 6),
    CHECK(start_min % 30 = 0 AND end_min % 30 = 0 AND start_min < end_min)
);
CREATE INDEX IF NOT EXISTS idx_app_routines_slot
    ON app_weekly_routines(site_id, weekday, room_id, start_min);

CREATE TABLE IF NOT EXISTS app_locked_slots (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    room_id TEXT NOT NULL REFERENCES app_rooms(id),
    locked_date TEXT NOT NULL,
    start_min INTEGER NOT NULL,
    end_min INTEGER NOT NULL,
    label TEXT NOT NULL DEFAULT '临时锁定',
    CHECK(start_min % 30 = 0 AND end_min % 30 = 0 AND start_min < end_min)
);

CREATE TABLE IF NOT EXISTS app_audit_log (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    actor_user_id TEXT,
    action TEXT NOT NULL,
    entity_id TEXT,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_migration_issues (
    id TEXT PRIMARY KEY,
    source_table TEXT NOT NULL,
    source_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _uuid_for(value: str) -> str:
    return str(uuid5(LEGACY_NAMESPACE, value))


def _merge(ranges: Sequence[TimeRange]) -> list[TimeRange]:
    merged: list[TimeRange] = []
    for item in sorted(ranges):
        if not merged or item.start > merged[-1].end:
            merged.append(item)
        else:
            merged[-1] = TimeRange(merged[-1].start, max(merged[-1].end, item.end))
    return merged


def _available_parts(requested: TimeRange, occupied: Sequence[TimeRange]) -> list[TimeRange]:
    result: list[TimeRange] = []
    cursor = requested.start
    for block in _merge(occupied):
        if block.end <= requested.start or block.start >= requested.end:
            continue
        if cursor < block.start:
            result.append(TimeRange(cursor, min(block.start, requested.end)))
        cursor = max(cursor, block.end)
        if cursor >= requested.end:
            break
    if cursor < requested.end:
        result.append(TimeRange(cursor, requested.end))
    return result


class SQLiteBookingRepository:
    """一个站点数据库的完整持久化实现。"""

    def __init__(self, config: SiteConfig) -> None:
        self.config = config
        self.site_id = config.site_id
        self.db_path = config.db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except sqlite3.OperationalError as exc:
            conn.rollback()
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise DatabaseBusy() from exc
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._read() as conn:
            conn.executescript(SCHEMA)
            for room in self.config.rooms:
                conn.execute(
                    """INSERT INTO app_rooms(id, site_id, name, active) VALUES(?, ?, ?, 1)
                    ON CONFLICT(id) DO UPDATE SET site_id=excluded.site_id, name=excluded.name, active=1""",
                    (room.id, self.site_id, room.name),
                )
        self._migrate_legacy_once()
        if self.config.default_owner_external_id:
            self._ensure_bootstrap_owner(self.config.default_owner_external_id)

    def _table_exists(self, conn: sqlite3.Connection, name: str) -> bool:
        row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        return row is not None

    def _legacy_room_id(self, conn: sqlite3.Connection, room_name: str) -> str:
        try:
            return self.config.room_by_reference(room_name).id
        except NotFound:
            room_id = f"legacy-{_uuid_for(self.site_id + ':room:' + room_name)[:8]}"
            conn.execute(
                "INSERT OR IGNORE INTO app_rooms(id, site_id, name, active) VALUES(?, ?, ?, 0)",
                (room_id, self.site_id, room_name),
            )
            return room_id

    def _legacy_user(
        self,
        conn: sqlite3.Connection,
        external_id: str,
        name: str | None = None,
        student_id: str | None = None,
    ) -> str:
        row = conn.execute(
            "SELECT user_id FROM app_identities WHERE provider='qq' AND external_id=?",
            (external_id,),
        ).fetchone()
        if row:
            return str(row[0])
        user_id = _uuid_for(f"{self.site_id}:qq:{external_id}")
        now = _now_text()
        conn.execute(
            """INSERT OR IGNORE INTO app_users
            (id, display_name, student_id, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?)""",
            (user_id, name or "未完善资料", student_id, now, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO app_identities(provider, external_id, user_id) VALUES('qq', ?, ?)",
            (external_id, user_id),
        )
        return user_id

    def _migration_issue(
        self,
        conn: sqlite3.Connection,
        source_table: str,
        source_id: object,
        reason: str,
    ) -> None:
        conn.execute(
            """INSERT OR REPLACE INTO app_migration_issues
            (id, source_table, source_id, reason, created_at)
            VALUES(?, ?, ?, ?, ?)""",
            (
                _uuid_for(f"{self.site_id}:migration:{source_table}:{source_id}"),
                source_table,
                str(source_id),
                reason,
                _now_text(),
            ),
        )

    @staticmethod
    def _legacy_time(value: str) -> int:
        hour, minute = (int(part) for part in value.split(":", 1))
        result = hour * 60 + minute
        if result % 30:
            raise ValueError(f"旧数据时间不在半小时网格：{value}")
        return result

    def _migrate_legacy_once(self) -> None:
        with self._write() as conn:
            done = conn.execute("SELECT value FROM app_meta WHERE key='legacy_import_v1'").fetchone()
            if done:
                return

            if self._table_exists(conn, "users"):
                columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
                student_expr = "student_id" if "student_id" in columns else "NULL"
                for row in conn.execute(
                    f"SELECT user_id, user_name, {student_expr} AS student_id FROM users"
                ):
                    self._legacy_user(conn, row["user_id"], row["user_name"], row["student_id"])

            if self._table_exists(conn, "admins"):
                for row in conn.execute("SELECT user_id, role FROM admins"):
                    if row["role"] not in self.config.role_levels:
                        self._migration_issue(
                            conn,
                            "admins",
                            row["user_id"],
                            f"未知角色：{row['role']}",
                        )
                        continue
                    user_id = self._legacy_user(conn, row["user_id"])
                    conn.execute(
                        "INSERT OR REPLACE INTO app_roles(site_id, user_id, role) VALUES(?, ?, ?)",
                        (self.site_id, user_id, row["role"]),
                    )

            if self._table_exists(conn, "reservations"):
                for row in conn.execute(
                    """SELECT id, user_id, user_name, room_name, reserve_date,
                    start_time, end_time FROM reservations"""
                ):
                    try:
                        start, end = self._legacy_time(row["start_time"]), self._legacy_time(row["end_time"])
                        TimeRange(start, end)
                        date.fromisoformat(row["reserve_date"])
                    except Exception as exc:
                        self._migration_issue(conn, "reservations", row["id"], str(exc))
                        continue
                    user_id = self._legacy_user(conn, row["user_id"], row["user_name"])
                    room_id = self._legacy_room_id(conn, row["room_name"])
                    reservation_id = _uuid_for(f"{self.site_id}:legacy-reservation:{row['id']}")
                    conn.execute(
                        """INSERT OR IGNORE INTO app_reservations
                        (id, site_id, user_id, room_id, reserve_date, start_min, end_min, source, created_at)
                        VALUES(?, ?, ?, ?, ?, ?, ?, 'legacy', ?)""",
                        (
                            reservation_id,
                            self.site_id,
                            user_id,
                            room_id,
                            row["reserve_date"],
                            start,
                            end,
                            _now_text(),
                        ),
                    )

            if self._table_exists(conn, "weekly_routines"):
                for row in conn.execute(
                    "SELECT id, weekday, room_name, start_time, end_time, purpose FROM weekly_routines"
                ):
                    try:
                        if not 0 <= int(row["weekday"]) <= 6:
                            raise ValueError(f"星期越界：{row['weekday']}")
                        interval = TimeRange(
                            self._legacy_time(row["start_time"]), self._legacy_time(row["end_time"])
                        )
                    except Exception as exc:
                        self._migration_issue(conn, "weekly_routines", row["id"], str(exc))
                        continue
                    room_id = self._legacy_room_id(conn, row["room_name"])
                    conn.execute(
                        """INSERT OR IGNORE INTO app_weekly_routines
                        (id, site_id, weekday, room_id, start_min, end_min, purpose, created_at)
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            _uuid_for(f"{self.site_id}:legacy-routine:{row['id']}"),
                            self.site_id,
                            row["weekday"],
                            room_id,
                            interval.start,
                            interval.end,
                            row["purpose"],
                            _now_text(),
                        ),
                    )

            if self._table_exists(conn, "locked_slots"):
                for row in conn.execute(
                    "SELECT id, room_name, locked_date, start_time, end_time FROM locked_slots"
                ):
                    try:
                        interval = TimeRange(
                            self._legacy_time(row["start_time"]), self._legacy_time(row["end_time"])
                        )
                        date.fromisoformat(row["locked_date"])
                    except Exception as exc:
                        self._migration_issue(conn, "locked_slots", row["id"], str(exc))
                        continue
                    room_id = self._legacy_room_id(conn, row["room_name"])
                    conn.execute(
                        """INSERT OR IGNORE INTO app_locked_slots
                        (id, site_id, room_id, locked_date, start_min, end_min, label)
                        VALUES(?, ?, ?, ?, ?, ?, '旧系统锁定')""",
                        (
                            _uuid_for(f"{self.site_id}:legacy-lock:{row['id']}"),
                            self.site_id,
                            room_id,
                            row["locked_date"],
                            interval.start,
                            interval.end,
                        ),
                    )

            conn.execute(
                "INSERT INTO app_meta(key, value) VALUES('legacy_import_v1', ?)",
                (_now_text(),),
            )

    def _ensure_bootstrap_owner(self, external_id: str) -> None:
        with self._write() as conn:
            user_id = self._legacy_user(conn, external_id, "系统管理员")
            existing = conn.execute(
                "SELECT 1 FROM app_roles WHERE site_id=? AND role=?",
                (self.site_id, self.config.highest_role),
            ).fetchone()
            if not existing:
                conn.execute(
                    "INSERT OR REPLACE INTO app_roles(site_id, user_id, role) VALUES(?, ?, ?)",
                    (self.site_id, user_id, self.config.highest_role),
                )

    def user_by_external(self, identity: ExternalIdentity) -> User | None:
        with self._read() as conn:
            row = conn.execute(
                """SELECT u.id, u.display_name, u.student_id
                FROM app_identities i JOIN app_users u ON u.id=i.user_id
                WHERE i.provider=? AND i.external_id=?""",
                (identity.provider, identity.external_id),
            ).fetchone()
        return User(row["id"], row["display_name"], row["student_id"]) if row else None

    def bind_user(self, identity: ExternalIdentity, name: str, student_id: str) -> User:
        with self._write() as conn:
            identity_row = conn.execute(
                "SELECT user_id FROM app_identities WHERE provider=? AND external_id=?",
                (identity.provider, identity.external_id),
            ).fetchone()
            user_id = str(identity_row[0]) if identity_row else str(uuid4())

            duplicate_name = conn.execute(
                "SELECT id FROM app_users WHERE display_name=? AND id<>?", (name, user_id)
            ).fetchone()
            if duplicate_name:
                raise DuplicateIdentity("display_name")
            duplicate_student = conn.execute(
                "SELECT id FROM app_users WHERE student_id=? AND id<>?", (student_id, user_id)
            ).fetchone()
            if duplicate_student:
                raise DuplicateIdentity("student_id")

            now = _now_text()
            if identity_row:
                conn.execute(
                    "UPDATE app_users SET display_name=?, student_id=?, updated_at=? WHERE id=?",
                    (name, student_id, now, user_id),
                )
            else:
                conn.execute(
                    """INSERT INTO app_users
                    (id, display_name, student_id, created_at, updated_at)
                    VALUES(?, ?, ?, ?, ?)""",
                    (user_id, name, student_id, now, now),
                )
                conn.execute(
                    "INSERT INTO app_identities(provider, external_id, user_id) VALUES(?, ?, ?)",
                    (identity.provider, identity.external_id, user_id),
                )
        return User(user_id, name, student_id)

    def user_by_name(self, name: str) -> User | None:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT id, display_name, student_id FROM app_users WHERE display_name=?", (name,)
            ).fetchall()
        if len(rows) > 1:
            raise NotFound("user", name=name, reason="ambiguous")
        if not rows:
            return None
        row = rows[0]
        return User(row["id"], row["display_name"], row["student_id"])

    def role_of(self, user_id: str) -> str:
        with self._read() as conn:
            row = conn.execute(
                "SELECT role FROM app_roles WHERE site_id=? AND user_id=?",
                (self.site_id, user_id),
            ).fetchone()
        return str(row[0]) if row else "user"

    def set_role(self, user_id: str, role: str) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO app_roles(site_id, user_id, role) VALUES(?, ?, ?)",
                (self.site_id, user_id, role),
            )

    def remove_role(self, user_id: str) -> None:
        with self._write() as conn:
            conn.execute(
                "DELETE FROM app_roles WHERE site_id=? AND user_id=?",
                (self.site_id, user_id),
            )

    def transfer_role(self, old_user_id: str, new_user_id: str, owner_role: str, fallback_role: str) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO app_roles(site_id, user_id, role) VALUES(?, ?, ?)",
                (self.site_id, old_user_id, fallback_role),
            )
            conn.execute(
                "INSERT OR REPLACE INTO app_roles(site_id, user_id, role) VALUES(?, ?, ?)",
                (self.site_id, new_user_id, owner_role),
            )

    def _occupied(self, conn: sqlite3.Connection, target_date: date, room_id: str) -> list[TimeRange]:
        values: list[TimeRange] = []
        for row in conn.execute(
            """SELECT start_min, end_min FROM app_reservations
            WHERE site_id=? AND reserve_date=? AND room_id=? AND deleted_at IS NULL""",
            (self.site_id, target_date.isoformat(), room_id),
        ):
            values.append(TimeRange(row[0], row[1]))
        for row in conn.execute(
            """SELECT start_min, end_min FROM app_locked_slots
            WHERE site_id=? AND locked_date=? AND room_id=?""",
            (self.site_id, target_date.isoformat(), room_id),
        ):
            values.append(TimeRange(row[0], row[1]))
        for row in conn.execute(
            """SELECT start_min, end_min FROM app_weekly_routines
            WHERE site_id=? AND weekday=? AND room_id=?""",
            (self.site_id, target_date.weekday(), room_id),
        ):
            values.append(TimeRange(row[0], row[1]))
        return values

    def book_available(
        self,
        context: RequestContext,
        room_id: str,
        target_date: date,
        requested: TimeRange,
        max_daily_minutes: int,
    ) -> list[TimeRange]:
        if context.actor_user_id is None:
            raise NotFound("actor")
        with self._write() as conn:
            free = _available_parts(requested, self._occupied(conn, target_date, room_id))
            if not free:
                return []
            row = conn.execute(
                """SELECT COALESCE(SUM(end_min-start_min), 0) FROM app_reservations
                WHERE site_id=? AND user_id=? AND reserve_date=? AND deleted_at IS NULL""",
                (self.site_id, context.actor_user_id, target_date.isoformat()),
            ).fetchone()
            current = int(row[0])
            if current + sum(item.duration_minutes for item in free) > max_daily_minutes:
                raise DailyLimitExceeded(current, max_daily_minutes)
            now = _now_text()
            for item in free:
                conn.execute(
                    """INSERT INTO app_reservations
                    (id, site_id, user_id, room_id, reserve_date, start_min, end_min, source, created_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(uuid4()),
                        self.site_id,
                        context.actor_user_id,
                        room_id,
                        target_date.isoformat(),
                        item.start,
                        item.end,
                        context.source,
                        now,
                    ),
                )
        return free

    def cancel_user(
        self,
        user_id: str,
        target_date: date,
        room_id: str | None,
        requested: TimeRange | None,
    ) -> list[CancelledSlot]:
        with self._write() as conn:
            if room_id is None and requested is None:
                rows = conn.execute(
                    """SELECT id, room_id, start_min, end_min FROM app_reservations
                    WHERE site_id=? AND user_id=? AND reserve_date=? AND deleted_at IS NULL
                    ORDER BY room_id, start_min""",
                    (self.site_id, user_id, target_date.isoformat()),
                ).fetchall()
                batch = f"cancel:{uuid4()}"
                now = _now_text()
                conn.execute(
                    """UPDATE app_reservations SET deleted_at=?, delete_batch_id=?
                    WHERE site_id=? AND user_id=? AND reserve_date=? AND deleted_at IS NULL""",
                    (now, batch, self.site_id, user_id, target_date.isoformat()),
                )
                return [
                    CancelledSlot(row["room_id"], TimeRange(row["start_min"], row["end_min"])) for row in rows
                ]

            if room_id is None or requested is None:
                raise ValueError("room_id 与 requested 必须同时提供")

            other_rows = conn.execute(
                """SELECT start_min, end_min FROM app_reservations
                WHERE site_id=? AND reserve_date=? AND room_id=? AND user_id<>? AND deleted_at IS NULL""",
                (self.site_id, target_date.isoformat(), room_id, user_id),
            ).fetchall()
            if any(requested.overlaps(TimeRange(row[0], row[1])) for row in other_rows):
                raise NotFound("cancellable_slot", reason="contains_other_user")

            rows = conn.execute(
                """SELECT * FROM app_reservations
                WHERE site_id=? AND reserve_date=? AND room_id=? AND user_id=? AND deleted_at IS NULL""",
                (self.site_id, target_date.isoformat(), room_id, user_id),
            ).fetchall()
            return self._cancel_rows(conn, rows, requested, include_user=False)

    def _cancel_rows(
        self,
        conn: sqlite3.Connection,
        rows: Sequence[sqlite3.Row],
        requested: TimeRange,
        include_user: bool,
    ) -> list[CancelledSlot]:
        cancelled: list[CancelledSlot] = []
        now = _now_text()
        batch = f"cancel:{uuid4()}"
        for row in rows:
            original = TimeRange(row["start_min"], row["end_min"])
            removed = original.clipped_to(requested)
            if removed is None:
                continue
            user_name = None
            if include_user:
                name_row = conn.execute(
                    "SELECT display_name FROM app_users WHERE id=?", (row["user_id"],)
                ).fetchone()
                user_name = str(name_row[0]) if name_row else "未知用户"
            cancelled.append(CancelledSlot(row["room_id"], removed, user_name))
            conn.execute(
                "UPDATE app_reservations SET deleted_at=?, delete_batch_id=? WHERE id=?",
                (now, batch, row["id"]),
            )
            remaining: list[TimeRange] = []
            if original.start < removed.start:
                remaining.append(TimeRange(original.start, removed.start))
            if removed.end < original.end:
                remaining.append(TimeRange(removed.end, original.end))
            for fragment in remaining:
                conn.execute(
                    """INSERT INTO app_reservations
                    (id, site_id, user_id, room_id, reserve_date, start_min, end_min, source, created_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(uuid4()),
                        row["site_id"],
                        row["user_id"],
                        row["room_id"],
                        row["reserve_date"],
                        fragment.start,
                        fragment.end,
                        row["source"],
                        now,
                    ),
                )
        return cancelled

    def cancel_admin(self, target_date: date, room_id: str, requested: TimeRange) -> list[CancelledSlot]:
        with self._write() as conn:
            rows = conn.execute(
                """SELECT * FROM app_reservations
                WHERE site_id=? AND reserve_date=? AND room_id=? AND deleted_at IS NULL""",
                (self.site_id, target_date.isoformat(), room_id),
            ).fetchall()
            return self._cancel_rows(conn, rows, requested, include_user=True)

    def schedule(self, target_date: date, room_id: str | None = None) -> list[Occupancy]:
        room_clause = " AND r.room_id=?" if room_id else ""
        params: list[object] = [self.site_id, target_date.isoformat()]
        if room_id:
            params.append(room_id)
        with self._read() as conn:
            reservations = conn.execute(
                f"""SELECT r.room_id, r.start_min, r.end_min, r.user_id, u.display_name
                FROM app_reservations r JOIN app_users u ON u.id=r.user_id
                WHERE r.site_id=? AND r.reserve_date=? AND r.deleted_at IS NULL{room_clause}""",
                params,
            ).fetchall()
            routine_params: list[object] = [self.site_id, target_date.weekday()]
            lock_params: list[object] = [self.site_id, target_date.isoformat()]
            extra = " AND room_id=?" if room_id else ""
            if room_id:
                routine_params.append(room_id)
                lock_params.append(room_id)
            routines = conn.execute(
                f"""SELECT room_id, start_min, end_min, purpose
                FROM app_weekly_routines WHERE site_id=? AND weekday=?{extra}""",
                routine_params,
            ).fetchall()
            locks = conn.execute(
                f"""SELECT room_id, start_min, end_min, label
                FROM app_locked_slots WHERE site_id=? AND locked_date=?{extra}""",
                lock_params,
            ).fetchall()
        result = [
            Occupancy(
                row["room_id"],
                TimeRange(row["start_min"], row["end_min"]),
                "reservation",
                row["display_name"],
                row["user_id"],
            )
            for row in reservations
        ]
        result += [
            Occupancy(row["room_id"], TimeRange(row["start_min"], row["end_min"]), "routine", row["purpose"])
            for row in routines
        ]
        result += [
            Occupancy(row["room_id"], TimeRange(row["start_min"], row["end_min"]), "lock", row["label"])
            for row in locks
        ]
        return sorted(result, key=lambda item: (item.room_id, item.time_range.start, item.kind))

    def free_slots(
        self, target_date: date, room_ids: list[str], opening: TimeRange
    ) -> dict[str, list[TimeRange]]:
        with self._read() as conn:
            return {
                room_id: _available_parts(opening, self._occupied(conn, target_date, room_id))
                for room_id in room_ids
            }

    def personal(self, user_id: str, from_date: date) -> list[tuple[date, str, TimeRange]]:
        with self._read() as conn:
            rows = conn.execute(
                """SELECT reserve_date, room_id, start_min, end_min FROM app_reservations
                WHERE site_id=? AND user_id=? AND reserve_date>=? AND deleted_at IS NULL
                ORDER BY reserve_date, start_min""",
                (self.site_id, user_id, from_date.isoformat()),
            ).fetchall()
        return [(date.fromisoformat(row[0]), row[1], TimeRange(row[2], row[3])) for row in rows]

    def clear_date(self, target_date: date, actor_id: str) -> int:
        with self._write() as conn:
            batch = f"clear:{uuid4()}"
            cursor = conn.execute(
                """UPDATE app_reservations SET deleted_at=?, delete_batch_id=?
                WHERE site_id=? AND reserve_date=? AND deleted_at IS NULL""",
                (_now_text(), batch, self.site_id, target_date.isoformat()),
            )
            return cursor.rowcount

    def undo_clear(self, target_date: date, actor_id: str) -> int:
        with self._write() as conn:
            row = conn.execute(
                """SELECT delete_batch_id FROM app_reservations
                WHERE site_id=? AND reserve_date=? AND delete_batch_id LIKE 'clear:%'
                ORDER BY deleted_at DESC LIMIT 1""",
                (self.site_id, target_date.isoformat()),
            ).fetchone()
            if not row:
                return 0
            cursor = conn.execute(
                """UPDATE app_reservations SET deleted_at=NULL, delete_batch_id=NULL
                WHERE site_id=? AND reserve_date=? AND delete_batch_id=?""",
                (self.site_id, target_date.isoformat(), row[0]),
            )
            return cursor.rowcount

    def add_routine(
        self,
        weekday: int,
        room_id: str,
        time_range: TimeRange,
        purpose: str,
        from_date: date,
    ) -> Routine:
        with self._write() as conn:
            for row in conn.execute(
                """SELECT start_min, end_min, purpose FROM app_weekly_routines
                WHERE site_id=? AND weekday=? AND room_id=?""",
                (self.site_id, weekday, room_id),
            ):
                if time_range.overlaps(TimeRange(row[0], row[1])):
                    raise NotFound("routine_slot", reason="routine_conflict", label=row[2])
            for row in conn.execute(
                """SELECT reserve_date, start_min, end_min, u.display_name
                FROM app_reservations r JOIN app_users u ON u.id=r.user_id
                WHERE r.site_id=? AND r.room_id=? AND r.reserve_date>=? AND r.deleted_at IS NULL""",
                (self.site_id, room_id, from_date.isoformat()),
            ):
                if date.fromisoformat(row[0]).weekday() == weekday and time_range.overlaps(
                    TimeRange(row[1], row[2])
                ):
                    raise NotFound("routine_slot", reason="reservation_conflict", date=row[0], label=row[3])
            routine = Routine(str(uuid4()), weekday, room_id, time_range, purpose)
            conn.execute(
                """INSERT INTO app_weekly_routines
                (id, site_id, weekday, room_id, start_min, end_min, purpose, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    routine.id,
                    self.site_id,
                    weekday,
                    room_id,
                    time_range.start,
                    time_range.end,
                    purpose,
                    _now_text(),
                ),
            )
            return routine

    def remove_routine(self, weekday: int, room_id: str, time_range: TimeRange) -> bool:
        with self._write() as conn:
            cursor = conn.execute(
                """DELETE FROM app_weekly_routines
                WHERE site_id=? AND weekday=? AND room_id=? AND start_min=? AND end_min=?""",
                (self.site_id, weekday, room_id, time_range.start, time_range.end),
            )
            return cursor.rowcount > 0

    def list_routines(self, weekday: int | None = None) -> list[Routine]:
        sql = (
            "SELECT id, weekday, room_id, start_min, end_min, purpose "
            "FROM app_weekly_routines WHERE site_id=?"
        )
        params: list[object] = [self.site_id]
        if weekday is not None:
            sql += " AND weekday=?"
            params.append(weekday)
        sql += " ORDER BY weekday, room_id, start_min"
        with self._read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Routine(row[0], row[1], row[2], TimeRange(row[3], row[4]), row[5]) for row in rows]

    @property
    def _backup_path(self) -> Path:
        return self.db_path.parent / "backups" / "users_backup_v3.csv"

    def backup_users(self) -> tuple[Path, int]:
        with self._read() as conn:
            rows = conn.execute(
                """SELECT u.id, u.display_name, u.student_id, i.provider, i.external_id,
                COALESCE(r.role, 'user') AS role
                FROM app_users u
                JOIN app_identities i ON i.user_id=u.id
                LEFT JOIN app_roles r ON r.user_id=u.id AND r.site_id=?
                ORDER BY u.display_name""",
                (self.site_id,),
            ).fetchall()
        self._backup_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._backup_path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["system_user_id", "display_name", "student_id", "provider", "external_id", "role"]
            )
            writer.writerows(rows)
        temp.replace(self._backup_path)
        return self._backup_path, len(rows)

    def restore_users(self) -> int:
        if not self._backup_path.exists():
            return 0
        restored = 0
        with self._backup_path.open("r", encoding="utf-8-sig", newline="") as handle, self._write() as conn:
            for row in csv.DictReader(handle):
                user_id = row["system_user_id"]
                now = _now_text()
                conn.execute(
                    """INSERT OR IGNORE INTO app_users
                    (id, display_name, student_id, created_at, updated_at)
                    VALUES(?, ?, ?, ?, ?)""",
                    (user_id, row["display_name"], row["student_id"] or None, now, now),
                )
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO app_identities(provider, external_id, user_id) VALUES(?, ?, ?)",
                    (row["provider"], row["external_id"], user_id),
                )
                if row["role"] != "user":
                    conn.execute(
                        "INSERT OR IGNORE INTO app_roles(site_id, user_id, role) VALUES(?, ?, ?)",
                        (self.site_id, user_id, row["role"]),
                    )
                restored += max(cursor.rowcount, 0)
        return restored

    def cleanup_old(self, cutoff: date) -> int:
        with self._write() as conn:
            rows = conn.execute(
                """SELECT r.id, r.user_id, u.display_name, r.room_id, r.reserve_date,
                r.start_min, r.end_min, r.source, r.created_at, r.deleted_at
                FROM app_reservations r JOIN app_users u ON u.id=r.user_id
                WHERE r.site_id=? AND r.reserve_date<?
                ORDER BY r.reserve_date, r.room_id, r.start_min""",
                (self.site_id, cutoff.isoformat()),
            ).fetchall()
            if not rows:
                return 0

            archive_dir = self.db_path.parent / "archives"
            archive_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            archive_path = archive_dir / f"reservations_before_{cutoff.isoformat()}_{stamp}.csv"
            temporary = archive_path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(
                    [
                        "reservation_id",
                        "system_user_id",
                        "display_name",
                        "room_id",
                        "reserve_date",
                        "start_min",
                        "end_min",
                        "source",
                        "created_at",
                        "deleted_at",
                    ]
                )
                writer.writerows(rows)
            temporary.replace(archive_path)
            conn.execute(
                "DELETE FROM app_reservations WHERE site_id=? AND reserve_date<?",
                (self.site_id, cutoff.isoformat()),
            )
            return len(rows)
