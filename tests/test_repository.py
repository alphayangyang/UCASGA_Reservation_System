from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from threading import Barrier
from uuid import uuid4

import pytest

from qqbot.domain.calendar import SHANGHAI_TZ
from qqbot.domain.errors import DailyLimitExceeded
from qqbot.domain.models import ExternalIdentity, RequestContext, TimeRange
from qqbot.infrastructure.sqlite_repository import SQLiteBookingRepository


def context(user_id: str, external_id: str = "external") -> RequestContext:
    return RequestContext(
        request_id=str(uuid4()),
        source="qq",
        site_id="site-yql",
        identity=ExternalIdentity("qq", external_id),
        actor_user_id=user_id,
        received_at=datetime(2026, 8, 7, 12, tzinfo=SHANGHAI_TZ),
    )


def test_booking_conflict_returns_only_available_fragment(yql_config) -> None:
    repo = SQLiteBookingRepository(yql_config)
    repo.initialize()
    first = repo.bind_user(ExternalIdentity("qq", "u1"), "张三", "2024K8009926001")
    second = repo.bind_user(ExternalIdentity("qq", "u2"), "李四", "2024K8009926002")
    target = date(2026, 8, 8)

    assert repo.book_available(context(first.id, "u1"), "yql-main", target, TimeRange(420, 480), 180) == [
        TimeRange(420, 480)
    ]
    assert repo.book_available(context(second.id, "u2"), "yql-main", target, TimeRange(450, 540), 180) == [
        TimeRange(480, 540)
    ]


def test_daily_limit_is_checked_inside_write_transaction(yql_config) -> None:
    repo = SQLiteBookingRepository(yql_config)
    repo.initialize()
    user = repo.bind_user(ExternalIdentity("qq", "u1"), "张三", "2024K8009926001")
    target = date(2026, 8, 8)
    repo.book_available(context(user.id, "u1"), "yql-main", target, TimeRange(420, 480), 90)
    with pytest.raises(DailyLimitExceeded):
        repo.book_available(context(user.id, "u1"), "yql-main", target, TimeRange(480, 540), 90)


def test_partial_cancel_splits_reservation(yql_config) -> None:
    repo = SQLiteBookingRepository(yql_config)
    repo.initialize()
    user = repo.bind_user(ExternalIdentity("qq", "u1"), "张三", "2024K8009926001")
    target = date(2026, 8, 8)
    repo.book_available(context(user.id, "u1"), "yql-main", target, TimeRange(420, 600), 300)
    cancelled = repo.cancel_user(user.id, target, "yql-main", TimeRange(480, 540))
    assert [item.time_range for item in cancelled] == [TimeRange(480, 540)]
    personal = repo.personal(user.id, target)
    assert [item[2] for item in personal] == [TimeRange(420, 480), TimeRange(540, 600)]


def test_two_simultaneous_writers_cannot_double_book(yql_config) -> None:
    repo = SQLiteBookingRepository(yql_config)
    repo.initialize()
    users = [
        repo.bind_user(ExternalIdentity("qq", f"u{i}"), name, f"2024K800992600{i}")
        for i, name in ((1, "张三"), (2, "李四"))
    ]
    barrier = Barrier(2)

    def reserve(index: int):
        barrier.wait()
        return repo.book_available(
            context(users[index].id, f"u{index + 1}"),
            "yql-main",
            date(2026, 8, 8),
            TimeRange(420, 480),
            90,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, (0, 1)))
    assert sorted(len(item) for item in results) == [0, 1]
    reservations = [item for item in repo.schedule(date(2026, 8, 8)) if item.kind == "reservation"]
    assert len(reservations) == 1


def test_legacy_database_is_imported_without_deleting_old_tables(yql_config) -> None:
    with sqlite3.connect(yql_config.db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE users(user_id TEXT PRIMARY KEY, user_name TEXT, student_id TEXT);
            CREATE TABLE admins(user_id TEXT PRIMARY KEY, role TEXT);
            CREATE TABLE reservations(
                id INTEGER PRIMARY KEY, user_id TEXT, user_name TEXT, room_name TEXT,
                reserve_date TEXT, start_time TEXT, end_time TEXT, duration REAL
            );
            """
        )
        conn.execute("INSERT INTO users VALUES('old-qq', '旧用户', '2024K8009926001')")
        conn.execute("INSERT INTO admins VALUES('old-qq', 'band')")
        conn.execute(
            """INSERT INTO reservations VALUES(
            1, 'old-qq', '旧用户', '玉泉路琴房',
            '2026-08-08', '07:00', '08:00', 1
            )"""
        )
    repo = SQLiteBookingRepository(yql_config)
    repo.initialize()
    user = repo.user_by_external(ExternalIdentity("qq", "old-qq"))
    assert user is not None and user.display_name == "旧用户"
    assert repo.role_of(user.id) == "band"
    assert len(repo.personal(user.id, date(2026, 8, 8))) == 1
    with sqlite3.connect(yql_config.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 1


def test_invalid_legacy_time_is_reported_instead_of_silently_lost(yql_config) -> None:
    with sqlite3.connect(yql_config.db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE users(user_id TEXT PRIMARY KEY, user_name TEXT, student_id TEXT);
            CREATE TABLE reservations(
                id INTEGER PRIMARY KEY, user_id TEXT, user_name TEXT, room_name TEXT,
                reserve_date TEXT, start_time TEXT, end_time TEXT, duration REAL
            );
            """
        )
        conn.execute("INSERT INTO users VALUES('old-qq', '旧用户', '2024K8009926001')")
        conn.execute(
            """INSERT INTO reservations VALUES(
            1, 'old-qq', '旧用户', '玉泉路琴房',
            '2026-08-08', '07:03', '08:00', 0.95
            )"""
        )

    repo = SQLiteBookingRepository(yql_config)
    repo.initialize()
    with sqlite3.connect(yql_config.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM app_reservations").fetchone()[0] == 0
        issue = conn.execute("SELECT source_table, source_id FROM app_migration_issues").fetchone()
    assert issue == ("reservations", "1")
