from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

from qqbot.application.service import BookingApplication
from qqbot.domain.calendar import SHANGHAI_TZ
from qqbot.domain.models import ExternalIdentity, OperationResult, RequestContext, TimeRange
from qqbot.infrastructure.group_bindings import GroupBindingStore
from qqbot.infrastructure.sqlite_repository import SQLiteBookingRepository
from qqbot.interfaces.qq.broadcaster import (
    ClockAnnounceJob,
    ProactiveSender,
    RoutineBroadcastJob,
    SilentEndReportJob,
)
from qqbot.interfaces.qq.presenter import QQPresenter


class FakeAPI:
    def __init__(self, fail_groups: set[str] | None = None) -> None:
        self.messages: list[dict] = []
        self.fail_groups = fail_groups or set()

    async def post_group_message(self, **kwargs) -> None:
        if kwargs.get("group_openid") in self.fail_groups:
            raise RuntimeError("主动消息发送失败（模拟）")
        self.messages.append(kwargs)


class FakeUploader:
    def __init__(self, api) -> None:
        self.api = api
        self.uploads: list[tuple[str, bytes, str]] = []

    async def upload_image(self, group_openid: str, content: bytes, file_name: str) -> dict:
        self.uploads.append((group_openid, content, file_name))
        return {"file_info": f"file-{group_openid}"}


class FakeRenderer:
    available = True

    def __init__(self) -> None:
        self.rendered: list = []

    async def render(self, result):
        self.rendered.append(result)
        return b"png-bytes"


class BrokenRenderer(FakeRenderer):
    async def render(self, result):
        raise RuntimeError("浏览器不可用")


class FakePresenter:
    def __init__(self) -> None:
        self.rendered: list = []

    def render(self, result) -> str:
        self.rendered.append(result)
        return f"TEXT:{result.code}"


def make_bindings(tmp_path: Path, pairs: dict[str, str]) -> GroupBindingStore:
    store = GroupBindingStore(tmp_path / "control.db")
    store.initialize()
    for group_id, bot_id in pairs.items():
        store.set(group_id, bot_id)
    return store


def make_context(user_id: str) -> RequestContext:
    return RequestContext(
        request_id=str(uuid4()),
        source="test",
        site_id="site-yql",
        identity=ExternalIdentity("qq", "qq-user"),
        actor_user_id=user_id,
        received_at=datetime(2026, 8, 10, 12, tzinfo=SHANGHAI_TZ),
    )


def seed_repository(config) -> SQLiteBookingRepository:
    repo = SQLiteBookingRepository(config)
    repo.initialize()
    user = repo.bind_user(ExternalIdentity("qq", "qq-user"), "张三", "2024K8009926001")
    ctx = make_context(user.id)
    target = date(2026, 8, 11)
    repo.book_available(ctx, "yql-main", target, TimeRange(420, 480), 1440)
    repo.add_routine(target.weekday(), "yql-main", TimeRange(600, 660), "合唱团", date(2026, 1, 1))
    return repo


# ---------- GroupBindingStore.groups_for ----------


def test_groups_for_filters_by_bot_id(tmp_path: Path) -> None:
    store = make_bindings(tmp_path, {"g1": "yql", "g2": "yql", "g3": "other"})
    assert store.groups_for("yql") == ["g1", "g2"]
    assert store.groups_for("other") == ["g3"]
    assert store.groups_for("missing") == []


# ---------- ProactiveSender ----------


def test_send_text_fans_out_to_all_groups(tmp_path: Path) -> None:
    asyncio.run(_run_test_send_text_fans_out_to_all_groups(tmp_path))


async def _run_test_send_text_fans_out_to_all_groups(tmp_path: Path) -> None:
    api = FakeAPI()
    sender = ProactiveSender(api, make_bindings(tmp_path, {"g1": "yql", "g2": "yql"}))

    sent = await sender.send_text("yql", "hello")

    assert sent == 2
    assert [m["group_openid"] for m in api.messages] == ["g1", "g2"]
    assert all(m["msg_type"] == 0 and m["content"] == "hello" and "msg_id" not in m for m in api.messages)


def test_send_text_continues_after_group_failure(tmp_path: Path) -> None:
    asyncio.run(_run_test_send_text_continues_after_group_failure(tmp_path))


async def _run_test_send_text_continues_after_group_failure(tmp_path: Path) -> None:
    api = FakeAPI(fail_groups={"g1"})
    sender = ProactiveSender(api, make_bindings(tmp_path, {"g1": "yql", "g2": "yql", "g3": "yql"}))

    sent = await sender.send_text("yql", "hello")

    assert sent == 2
    assert [m["group_openid"] for m in api.messages] == ["g2", "g3"]


def test_send_image_uploads_and_sends_media_per_group(tmp_path: Path) -> None:
    asyncio.run(_run_test_send_image_uploads_and_sends_media_per_group(tmp_path))


async def _run_test_send_image_uploads_and_sends_media_per_group(tmp_path: Path) -> None:
    api = FakeAPI()
    uploader = FakeUploader(api)
    sender = ProactiveSender(
        api, make_bindings(tmp_path, {"g1": "yql", "g2": "yql"}), uploader_factory=lambda _: uploader
    )

    sent = await sender.send_image("yql", b"png")

    assert sent == 2
    assert uploader.uploads == [("g1", b"png", "broadcast.png"), ("g2", b"png", "broadcast.png")]
    assert all(m["msg_type"] == 7 and m["media"]["file_info"].startswith("file-") for m in api.messages)


def test_send_schedule_image_falls_back_to_text_when_renderer_unavailable(tmp_path: Path) -> None:
    asyncio.run(_run_test_send_schedule_image_falls_back_to_text_when_renderer_unavailable(tmp_path))


async def _run_test_send_schedule_image_falls_back_to_text_when_renderer_unavailable(tmp_path: Path) -> None:
    api = FakeAPI()
    presenter = FakePresenter()
    sender = ProactiveSender(api, make_bindings(tmp_path, {"g1": "yql"}))
    renderer = FakeRenderer()
    renderer.available = False
    result = OperationResult.success("schedule_range")

    sent = await sender.send_schedule_image("yql", result, renderer, presenter)

    assert sent == 1
    assert api.messages[0]["msg_type"] == 0
    assert api.messages[0]["content"] == "TEXT:schedule_range"
    assert presenter.rendered == [result]


def test_send_schedule_image_falls_back_to_text_when_render_fails(tmp_path: Path) -> None:
    asyncio.run(_run_test_send_schedule_image_falls_back_to_text_when_render_fails(tmp_path))


async def _run_test_send_schedule_image_falls_back_to_text_when_render_fails(tmp_path: Path) -> None:
    api = FakeAPI()
    presenter = FakePresenter()
    sender = ProactiveSender(api, make_bindings(tmp_path, {"g1": "yql"}))
    renderer = BrokenRenderer()
    result = OperationResult.success("schedule_range")

    sent = await sender.send_schedule_image("yql", result, renderer, presenter)

    assert sent == 1
    assert api.messages[0]["msg_type"] == 0
    assert api.messages[0]["content"] == "TEXT:schedule_range"


# ---------- RoutineBroadcastJob ----------


def test_routine_broadcast_job_target_start_is_next_calendar_day(yql_config, tmp_path: Path) -> None:
    api = FakeAPI()
    sender = ProactiveSender(api, make_bindings(tmp_path, {"g1": "yql"}))
    job = RoutineBroadcastJob(
        yql_config,
        BookingApplication(yql_config, SQLiteBookingRepository(yql_config)),
        sender,
        None,
        FakePresenter(),
    )

    now = datetime(2026, 8, 10, 21, 0, tzinfo=SHANGHAI_TZ)
    assert job.target_start(now) == date(2026, 8, 11)


def test_routine_broadcast_job_spans_configured_days(yql_config, tmp_path: Path) -> None:
    from dataclasses import replace

    from qqbot.infrastructure.config import RoutineBroadcastConfig

    config = replace(yql_config, routine_broadcast=RoutineBroadcastConfig(time=20 * 60, days=3))
    repo = seed_repository(yql_config)
    app = BookingApplication(config, repo)
    api = FakeAPI()
    presenter = QQPresenter(config)
    sender = ProactiveSender(api, make_bindings(tmp_path, {"g1": "yql"}))

    job = RoutineBroadcastJob(config, app, sender, None, presenter)
    sent = asyncio.run(job.run(datetime(2026, 8, 10, 20, 0, tzinfo=SHANGHAI_TZ)))

    assert sent == 1
    text = api.messages[0]["content"]
    for day in (date(2026, 8, 11), date(2026, 8, 12), date(2026, 8, 13)):
        assert day.isoformat() in text
    assert "合唱团" in text
    assert "张三" not in text


def test_routine_broadcast_job_publishes_only_routines(yql_config, tmp_path: Path) -> None:
    repo = seed_repository(yql_config)
    app = BookingApplication(yql_config, repo)
    api = FakeAPI()
    presenter = QQPresenter(yql_config)
    sender = ProactiveSender(api, make_bindings(tmp_path, {"g1": "yql", "g2": "yql"}))

    job = RoutineBroadcastJob(yql_config, app, sender, None, presenter)
    sent = asyncio.run(job.run(datetime(2026, 8, 10, 21, 0, tzinfo=SHANGHAI_TZ)))

    assert sent == 2
    text = api.messages[0]["content"]
    assert "合唱团" in text
    assert "张三" not in text
    assert "2026-08-11" in text


# ---------- ClockAnnounceJob ----------


def test_clock_announce_message_contains_system_time(yql_config, tmp_path: Path) -> None:
    api = FakeAPI()
    sender = ProactiveSender(api, make_bindings(tmp_path, {"g1": "yql"}))
    job = ClockAnnounceJob(yql_config, sender)

    sent = asyncio.run(job.run(datetime(2026, 8, 10, 22, 0, 5, tzinfo=SHANGHAI_TZ)))

    assert sent == 1
    message = api.messages[0]["content"]
    assert "22:00:05" in message
    assert "已到 22:00" in message


def test_clock_announce_time_follows_silent_start(yql_config, tmp_path: Path) -> None:
    from dataclasses import replace

    config = replace(yql_config, silent_start=23 * 60)
    api = FakeAPI()
    sender = ProactiveSender(api, make_bindings(tmp_path, {"g1": "yql"}))
    job = ClockAnnounceJob(config, sender)

    asyncio.run(job.run(datetime(2026, 8, 10, 23, 0, 0, tzinfo=SHANGHAI_TZ)))

    assert "已到 23:00" in api.messages[0]["content"]


# ---------- SilentEndReportJob ----------


def test_silent_end_report_job_publishes_full_schedule(yql_config, tmp_path: Path) -> None:
    repo = seed_repository(yql_config)
    app = BookingApplication(yql_config, repo)
    api = FakeAPI()
    presenter = QQPresenter(yql_config)
    sender = ProactiveSender(api, make_bindings(tmp_path, {"g1": "yql"}))

    job = SilentEndReportJob(yql_config, app, sender, None, presenter)
    sent = asyncio.run(job.run(datetime(2026, 8, 10, 22, 15, tzinfo=SHANGHAI_TZ)))

    assert sent == 1
    text = api.messages[0]["content"]
    assert "合唱团" in text
    assert "张三" in text


def test_silent_end_report_job_target_date_is_next_calendar_day(yql_config, tmp_path: Path) -> None:
    api = FakeAPI()
    sender = ProactiveSender(api, make_bindings(tmp_path, {"g1": "yql"}))
    job = SilentEndReportJob(
        yql_config,
        BookingApplication(yql_config, SQLiteBookingRepository(yql_config)),
        sender,
        None,
        FakePresenter(),
    )

    now = datetime(2026, 8, 10, 22, 15, tzinfo=SHANGHAI_TZ)
    assert job.target_date(now) == date(2026, 8, 11)


# ---------- client 挂载 ----------


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs: list[tuple] = []

    def add_job(self, fn, trigger, **kwargs) -> None:
        self.jobs.append((fn, trigger, kwargs))


def test_client_registers_broadcast_jobs_per_features(
    yqh_config, yql_config, tmp_path: Path, monkeypatch
) -> None:
    from qqbot.interfaces.qq import client as client_module
    from qqbot.interfaces.qq.client import PianoBotClient

    # 避免测试触碰真实 NLU 数据目录与白名单文件
    monkeypatch.setattr(client_module, "NLU_DATA_DIR", tmp_path / "nlu-data")

    async def exercise() -> FakeScheduler:
        # botpy.Client 构造需要活动事件循环，与 test_qq_client 一致
        client = PianoBotClient({"yqh": yqh_config, "yql": yql_config}, tmp_path / "control.db")
        scheduler = FakeScheduler()
        client.scheduler = scheduler  # type: ignore[assignment]
        client._register_broadcast_jobs()
        return scheduler

    scheduler = asyncio.run(exercise())

    ids = {job[2]["id"]: job for job in scheduler.jobs}
    assert set(ids) == {
        "routine_broadcast_yqh",
        "clock_announce_yqh",
        "silent_end_report_yqh",
        "clock_announce_yql",
        "silent_end_report_yql",
        "image_cache_prewarm_business_day",
        "image_cache_prewarm_theme",
    }
    assert ids["routine_broadcast_yqh"][1] == "cron"
    assert ids["routine_broadcast_yqh"][2]["hour"] == 21
    assert ids["routine_broadcast_yqh"][2]["minute"] == 0
    assert ids["clock_announce_yql"][2]["hour"] == 22
    assert ids["clock_announce_yql"][2]["minute"] == 0
    # 玉泉路静默窗口 22:00–22:03
    assert ids["silent_end_report_yql"][2]["hour"] == 22
    assert ids["silent_end_report_yql"][2]["minute"] == 3


def test_client_broadcast_job_times_follow_config(
    yql_config, tmp_path: Path, monkeypatch
) -> None:
    from dataclasses import replace

    from qqbot.infrastructure.config import RoutineBroadcastConfig
    from qqbot.interfaces.qq import client as client_module
    from qqbot.interfaces.qq.client import PianoBotClient

    monkeypatch.setattr(client_module, "NLU_DATA_DIR", tmp_path / "nlu-data")
    config = replace(
        yql_config,
        features=replace(yql_config.features, broadcast=True, weekly_routine=True),
        silent_start=23 * 60,
        silent_end=23 * 60 + 30,
        routine_broadcast=RoutineBroadcastConfig(time=20 * 60 + 30, days=2),
    )

    async def exercise() -> FakeScheduler:
        client = PianoBotClient({"yql": config}, tmp_path / "control.db")
        scheduler = FakeScheduler()
        client.scheduler = scheduler  # type: ignore[assignment]
        client._register_broadcast_jobs()
        return scheduler

    scheduler = asyncio.run(exercise())
    ids = {job[2]["id"]: job for job in scheduler.jobs}

    assert ids["routine_broadcast_yql"][2]["hour"] == 20
    assert ids["routine_broadcast_yql"][2]["minute"] == 30
    assert ids["clock_announce_yql"][2]["hour"] == 23
    assert ids["clock_announce_yql"][2]["minute"] == 0
    assert ids["silent_end_report_yql"][2]["hour"] == 23
    assert ids["silent_end_report_yql"][2]["minute"] == 30


def test_routine_broadcast_config_parsed_from_yaml() -> None:
    from pathlib import Path

    from qqbot.infrastructure.config import load_site_config

    root = Path(__file__).resolve().parents[1]
    config = load_site_config(root / "configs" / "yqh.yaml", project_root=root)
    assert config.routine_broadcast.time == 21 * 60
    assert config.routine_broadcast.days == 7


def test_routine_broadcast_days_must_be_within_1_and_7(tmp_path: Path) -> None:
    from qqbot.infrastructure.config import load_site_config

    yaml_path = tmp_path / "bad.yaml"
    yaml_path.write_text(
        """bot_id: bad
site_id: site-bad
bot_name: 坏配置
rooms:
  - id: r1
    name: 一号房
roles:
  levels:
    user: 0
booking:
  routine_broadcast:
    days: 8
""",
        encoding="utf-8",
    )
    import pytest

    with pytest.raises(ValueError, match="days"):
        load_site_config(yaml_path, project_root=tmp_path)
