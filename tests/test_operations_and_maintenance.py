from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

from qqbot.application.resolver import CommandResolver
from qqbot.domain.calendar import SHANGHAI_TZ
from qqbot.domain.commands import AdminCancel, ClearReservations
from qqbot.domain.models import ExternalIdentity, RequestContext, TimeRange
from qqbot.infrastructure.group_bindings import GroupBindingStore
from qqbot.infrastructure.sqlite_repository import SQLiteBookingRepository
from qqbot.interfaces.qq.parser import QQCommandParser


def request_context(user_id: str) -> RequestContext:
    return RequestContext(
        request_id=str(uuid4()),
        source="qq",
        site_id="site-yql",
        identity=ExternalIdentity("qq", "u1"),
        actor_user_id=user_id,
        received_at=datetime(2026, 8, 7, 12, tzinfo=SHANGHAI_TZ),
    )


def test_admin_commands_support_absolute_date(yql_config) -> None:
    parser = QQCommandParser()
    resolver = CommandResolver()
    now = datetime(2026, 8, 7, 12, tzinfo=SHANGHAI_TZ)

    cancel = resolver.resolve(
        parser.parse("#取消 玉泉路琴房 21-22.5 2026-08-20"),
        yql_config,
        now,
    )
    clear = resolver.resolve(parser.parse("#清空预约 2026-08-20"), yql_config, now)

    assert isinstance(cancel, AdminCancel)
    assert cancel.reserve_date == date(2026, 8, 20)
    assert isinstance(clear, ClearReservations)
    assert clear.reserve_date == date(2026, 8, 20)


def test_group_binding_import_does_not_overwrite_new_value(tmp_path: Path) -> None:
    store = GroupBindingStore(tmp_path / "control.db")
    store.initialize()
    store.set("group-1", "new-value")
    legacy = tmp_path / "group_mappings.json"
    legacy.write_text(
        json.dumps({"group-1": "old-value", "group-2": "yql"}),
        encoding="utf-8",
    )
    assert store.import_legacy_json(legacy) == 1
    assert store.get("group-1") == "new-value"
    assert store.get("group-2") == "yql"


def test_cleanup_archives_before_deleting(yql_config) -> None:
    repo = SQLiteBookingRepository(yql_config)
    repo.initialize()
    user = repo.bind_user(ExternalIdentity("qq", "u1"), "张三", "2024K8009926001")
    repo.book_available(
        request_context(user.id),
        "yql-main",
        date(2025, 1, 1),
        TimeRange(420, 480),
        90,
    )

    assert repo.cleanup_old(date(2025, 2, 1)) == 1
    assert repo.personal(user.id, date(2025, 1, 1)) == []
    archives = list((yql_config.db_path.parent / "archives").glob("*.csv"))
    assert len(archives) == 1
    assert "张三" in archives[0].read_text(encoding="utf-8-sig")
