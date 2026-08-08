from __future__ import annotations

import asyncio
import hashlib
from datetime import date

from qqbot.domain.models import DateRange, Occupancy, OperationResult, TimeRange
from qqbot.interfaces.qq.media_uploader import MD5_10M_BYTES, QQMediaUploader
from qqbot.interfaces.qq.presenter import QQPresenter
from qqbot.presentation.timeline import ScheduleImageRenderer, build_timeline_view


def schedule_result() -> OperationResult:
    period = DateRange(date(2026, 8, 10), date(2026, 8, 11))
    return OperationResult.success(
        "schedule_range",
        date_range=period,
        room_ids=["yql-main"],
        days=[
            {
                "date": date(2026, 8, 10),
                "offset": 0,
                "occupancies": [
                    Occupancy(
                        "yql-main",
                        TimeRange(21 * 60, 22 * 60 + 30),
                        "reservation",
                        "王小明同学",
                    ),
                    Occupancy(
                        "yql-main",
                        TimeRange(10 * 60, 11 * 60),
                        "routine",
                        "<b>合唱</b>",
                    ),
                ],
                "admin_view": False,
            },
            {
                "date": date(2026, 8, 11),
                "offset": 1,
                "occupancies": [],
                "admin_view": False,
            },
        ],
    )


def test_timeline_view_and_html_are_renderable(yql_config) -> None:
    result = schedule_result()
    view = build_timeline_view(yql_config, result)
    html = ScheduleImageRenderer(yql_config).render_html(result)

    assert view["period"] == "2026-08-10 ～ 2026-08-11"
    assert len(view["rows"]) == 2
    assert view["rows"][0]["blocks"][0]["label"] == "小明同学"
    assert "王小明同学" not in html
    assert "<b>合唱</b>" not in html
    assert "&lt;b&gt;合唱&lt;/b&gt;" in html
    assert "21:00-22:30" in html
    assert "暂无占用" in html


def test_text_fallback_supports_multiple_days(yql_config) -> None:
    message = QQPresenter(yql_config).render(schedule_result())
    assert "2026-08-10（+0）" in message
    assert "2026-08-11（+1）" in message
    assert "暂无预约" in message


def test_free_timeline_uses_free_slots(yql_config) -> None:
    period = DateRange(date(2026, 8, 10), date(2026, 8, 10))
    result = OperationResult.success(
        "free_slots_range",
        date_range=period,
        room_ids=["yql-main"],
        days=[
            {
                "date": period.start,
                "offset": 0,
                "slots": {"yql-main": [TimeRange(420, 510)]},
            }
        ],
    )
    view = build_timeline_view(yql_config, result)
    html = ScheduleImageRenderer(yql_config).render_html(result)

    assert view["mode"] == "free"
    assert view["rows"][0]["blocks"][0]["kind"] == "free"
    assert "07:00-08:30" in html


class FakeHTTP:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, dict]] = []

    async def request(self, route, **kwargs):
        payload = kwargs["json"]
        self.calls.append((route.method, route.path, route.url, payload))
        if route.path.endswith("/upload_prepare"):
            return {
                "upload_id": "upload-1",
                "block_size": "4",
                "parts": [
                    {"index": 0, "presigned_url": "https://upload.invalid/0", "block_size": "4"},
                    {"index": 1, "presigned_url": "https://upload.invalid/1", "block_size": "4"},
                    {"index": 2, "presigned_url": "https://upload.invalid/2", "block_size": "2"},
                ],
                "upload_config": {"retry_delay": 0},
            }
        if route.path.endswith("/files"):
            return {"file_info": "opaque-file-info", "ttl": 300}
        return {}


class FakeAPI:
    def __init__(self) -> None:
        self._http = FakeHTTP()


def test_qq_chunk_upload_follows_official_flow() -> None:
    content = b"abcdefghij"
    uploaded: list[tuple[str, bytes, int]] = []

    async def put_part(url: str, chunk: bytes, retry_delay: int) -> None:
        uploaded.append((url, chunk, retry_delay))

    api = FakeAPI()
    media = asyncio.run(
        QQMediaUploader(api, put_part=put_part).upload_image(
            "group-1",
            content,
            "schedule.png",
        )
    )

    assert media == {"file_info": "opaque-file-info"}
    assert [item[1] for item in uploaded] == [b"abcd", b"efgh", b"ij"]
    assert [call[1].rsplit("/", 1)[-1] for call in api._http.calls] == [
        "upload_prepare",
        "upload_part_finish",
        "upload_part_finish",
        "upload_part_finish",
        "files",
    ]
    assert all(call[2].startswith("https://api.bot.qq.com/") for call in api._http.calls)
    prepare = api._http.calls[0][3]
    assert prepare["file_size"] == "10"
    assert prepare["md5"] == hashlib.md5(content, usedforsecurity=False).hexdigest()
    assert (
        prepare["md5_10m"]
        == hashlib.md5(
            content[:MD5_10M_BYTES],
            usedforsecurity=False,
        ).hexdigest()
    )
    finishes = [call[3] for call in api._http.calls[1:4]]
    assert [item["block_size"] for item in finishes] == ["4", "4", "2"]
    assert [item["part_index"] for item in finishes] == [0, 1, 2]
