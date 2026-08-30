from __future__ import annotations

import asyncio
import hashlib
from datetime import date, datetime

from qqbot.domain.models import DateRange, Occupancy, OperationResult, TimeRange
from qqbot.interfaces.qq.media_uploader import MD5_10M_BYTES, QQMediaUploader
from qqbot.interfaces.qq.presenter import QQPresenter
from qqbot.presentation.timeline import (
    ScheduleImageRenderer,
    build_timeline_view,
    current_theme,
)


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
    html = ScheduleImageRenderer(yql_config).render_html(result, theme="light")

    assert view["period"] == "2026-08-10 ～ 2026-08-11"
    assert len(view["rows"]) == 2
    assert view["rows"][0]["blocks"][0]["label"] == "小明同学"
    assert "王小明同学" not in html
    assert "<b>合唱</b>" not in html
    assert "&lt;b&gt;合唱&lt;/b&gt;" in html
    assert "21:00-22:30" in html
    assert "暂无占用" in html
    # 类型信息不再以「周常：/锁定：」前缀写进块内（由颜色 + 图例表达）
    assert "周常：" not in html
    assert "锁定：" not in html


def test_timeline_theme_switches_day_and_night(yql_config) -> None:
    renderer = ScheduleImageRenderer(yql_config)
    result = schedule_result()
    light = renderer.render_html(result, theme="light")
    dark = renderer.render_html(result, theme="dark")

    assert 'class="theme-light"' in light
    assert 'class="theme-dark"' in dark
    # 深色主题变量存在（页面背景/卡片颜色不同）
    assert "--page-bg: #0e1524" in dark
    assert "--page-bg: #eef1f7" in light
    # 主题缺失时按当前时刻自动选择
    auto = renderer.render_html(result)
    assert 'class="theme-' in auto


def test_current_theme_boundaries() -> None:
    def at(hour: int) -> datetime:
        return datetime(2026, 8, 10, hour, 0)

    # 19:00 ~ 次日 07:00 深色，其余浅色
    assert current_theme(at(6)) == "dark"
    assert current_theme(at(7)) == "light"
    assert current_theme(at(12)) == "light"
    assert current_theme(at(18)) == "light"
    assert current_theme(at(19)) == "dark"
    assert current_theme(at(23)) == "dark"


def test_timeline_day_rows_alternate_by_date(yql_config) -> None:
    result = schedule_result()
    view = build_timeline_view(yql_config, result)

    # 两天交替：同一天的所有房间行共享同一 day_alt（此处单房间）
    assert [row["day_alt"] for row in view["rows"]] == [False, True]
    # 三天序列验证交替
    period = DateRange(date(2026, 8, 10), date(2026, 8, 12))
    three = OperationResult.success(
        "schedule_range",
        date_range=period,
        room_ids=["yql-main"],
        days=[
            {"date": date(2026, 8, 10), "offset": 0, "occupancies": [], "admin_view": False},
            {"date": date(2026, 8, 11), "offset": 1, "occupancies": [], "admin_view": False},
            {"date": date(2026, 8, 12), "offset": 2, "occupancies": [], "admin_view": False},
        ],
    )
    three_view = build_timeline_view(yql_config, three)
    assert [row["day_alt"] for row in three_view["rows"]] == [False, True, False]


def test_renderer_fonts_loaded_via_virtual_url(yql_config) -> None:
    """字体不内联进 HTML（3MB → 13KB 提速）；模板引用虚拟 URL，渲染时 route 返回。

    HTML 保持小体积；字体由 _load_fonts 读入内存（font_400/font_700），
    无系统字体的服务器也能渲染中文。
    """
    renderer = ScheduleImageRenderer(yql_config)
    html = renderer.render_html(schedule_result(), theme="light")

    # HTML 不再携带 base64 字体（体积保持小）
    assert "data:font/woff2;base64," not in html
    assert "http://fonts.local/" in html
    assert "Noto Sans SC" in html
    # 字体已读入内存，两个字重齐全
    assert "font_400" in renderer._font_bytes
    assert "font_700" in renderer._font_bytes
    assert len(renderer._font_bytes["font_400"]) > 1000
    assert len(renderer._font_bytes["font_700"]) > 1000


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


def test_qq_chunk_upload_server_index_starts_at_one() -> None:
    """手册 25.1：服务端 part_index 从 1 开始也必须正确切片（曾跳过首块、末片越界）。

    协议编号原样回传 part_finish；本地用独立 cursor 切片。
    """

    class OneIndexedFakeHTTP(FakeHTTP):
        async def request(self, route, **kwargs):
            payload = kwargs["json"]
            self.calls.append((route.method, route.path, route.url, payload))
            if route.path.endswith("/upload_prepare"):
                return {
                    "upload_id": "upload-1",
                    "block_size": "4",
                    "parts": [
                        {"index": 1, "presigned_url": "https://upload.invalid/1", "block_size": "4"},
                        {"index": 2, "presigned_url": "https://upload.invalid/2", "block_size": "4"},
                        {"index": 3, "presigned_url": "https://upload.invalid/3", "block_size": "2"},
                    ],
                    "upload_config": {"retry_delay": 0},
                }
            if route.path.endswith("/files"):
                return {"file_info": "opaque-file-info", "ttl": 300}
            return {}

    class OneIndexedAPI:
        def __init__(self) -> None:
            self._http = OneIndexedFakeHTTP()

    content = b"abcdefghij"
    uploaded: list[tuple[str, bytes, int]] = []

    async def put_part(url: str, chunk: bytes, retry_delay: int) -> None:
        uploaded.append((url, chunk, retry_delay))

    api = OneIndexedAPI()
    media = asyncio.run(
        QQMediaUploader(api, put_part=put_part).upload_image("group-1", content, "schedule.png")
    )

    assert media == {"file_info": "opaque-file-info"}
    # 本地切片仍然从头开始、完整覆盖（不因服务端编号偏移而跳块）
    assert [item[1] for item in uploaded] == [b"abcd", b"efgh", b"ij"]
    finishes = [call[3] for call in api._http.calls[1:4]]
    assert [item["part_index"] for item in finishes] == [1, 2, 3]  # 协议编号原样回传
