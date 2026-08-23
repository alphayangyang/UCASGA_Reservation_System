from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from qqbot.domain.models import Occupancy, OperationResult, TimeRange, minutes_to_text
from qqbot.infrastructure.config import SiteConfig

WEEKDAY_NAMES = "一二三四五六日"


def _percentage(value: int, opening: int, duration: int) -> float:
    return round((value - opening) / duration * 100, 4)


def _reservation_label(item: Occupancy) -> str:
    if item.kind == "routine":
        return f"周常：{item.label}"
    if item.kind == "lock":
        return f"锁定：{item.label}"
    return item.label[-4:]


def _block(
    slot: TimeRange,
    opening: int,
    closing: int,
    *,
    kind: str,
    label: str,
) -> dict[str, Any] | None:
    clipped = slot.clipped_to(TimeRange(opening, closing))
    if clipped is None:
        return None
    duration = closing - opening
    return {
        "kind": kind,
        "label": label,
        "time": clipped.display(),
        "left": _percentage(clipped.start, opening, duration),
        "width": round((clipped.end - clipped.start) / duration * 100, 4),
    }


def build_timeline_view(config: SiteConfig, result: OperationResult) -> dict[str, Any]:
    """把查询结果转换成模板数据；不接触 QQ SDK 或浏览器。"""
    if result.code not in {"schedule_range", "free_slots_range"}:
        raise ValueError(f"不支持的可视化结果：{result.code}")

    mode = "schedule" if result.code == "schedule_range" else "free"
    opening, closing = config.open_minutes, config.close_minutes
    room_ids = result.data["room_ids"]
    rows: list[dict[str, Any]] = []

    for day in result.data["days"]:
        for room_id in room_ids:
            blocks: list[dict[str, Any]] = []
            if mode == "schedule":
                values = [item for item in day["occupancies"] if item.room_id == room_id]
                for item in values:
                    value = _block(
                        item.time_range,
                        opening,
                        closing,
                        kind=item.kind,
                        label=_reservation_label(item),
                    )
                    if value is not None:
                        blocks.append(value)
            else:
                for slot in day["slots"].get(room_id, []):
                    value = _block(
                        slot,
                        opening,
                        closing,
                        kind="free",
                        label="空闲",
                    )
                    if value is not None:
                        blocks.append(value)

            target = day["date"]
            rows.append(
                {
                    "date": target.isoformat(),
                    "date_label": f"{target.month}月{target.day}日 · 周{WEEKDAY_NAMES[target.weekday()]}",
                    "offset": day.get("offset"),
                    "room": config.room_by_id(room_id).name,
                    "blocks": blocks,
                }
            )

    tick_start = (opening + 59) // 60
    tick_end = closing // 60
    ticks = [
        {
            "label": f"{hour:02d}:00",
            "left": _percentage(hour * 60, opening, closing - opening),
        }
        for hour in range(tick_start, tick_end + 1)
    ]
    period = result.data["date_range"]
    return {
        "site_name": config.bot_name,
        "title": "占用情况" if mode == "schedule" else "空闲时段",
        "mode": mode,
        "period": (
            period.start.isoformat()
            if period.start == period.end
            else f"{period.start.isoformat()} ～ {period.end.isoformat()}"
        ),
        "opening": minutes_to_text(opening),
        "closing": minutes_to_text(closing),
        "ticks": ticks,
        "hour_grid": round(60 / (closing - opening) * 100, 4),
        "half_hour_grid": round(30 / (closing - opening) * 100, 4),
        "rows": rows,
        "row_count": len(rows),
    }


class ScheduleImageRenderer:
    """用本地 HTML 模板生成时间轴 PNG；浏览器不可用时由调用方回退文字。"""

    def __init__(self, config: SiteConfig, template_dir: Path | None = None) -> None:
        self.config = config
        templates = template_dir or Path(__file__).with_name("templates")
        self.environment = Environment(
            loader=FileSystemLoader(templates),
            autoescape=True,
        )
        self.template = self.environment.get_template("schedule.html.jinja")
        self._playwright: Any = None
        self._browser: Any = None

    @property
    def available(self) -> bool:
        return self._browser is not None

    def render_html(self, result: OperationResult) -> str:
        return self.template.render(**build_timeline_view(self.config, result))

    async def start(self) -> None:
        if self.available:
            return
        from playwright.async_api import async_playwright

        runtime = await async_playwright().start()
        try:
            browser = await runtime.chromium.launch(
                headless=True,
                args=["--disable-dev-shm-usage"],
            )
        except Exception:
            await runtime.stop()
            raise
        self._playwright = runtime
        self._browser = browser

    async def render(self, result: OperationResult) -> bytes:
        if not self.available:
            raise RuntimeError("图片渲染器尚未启动")
        html = self.render_html(result)
        row_count = max(1, len(result.data["days"]) * len(result.data["room_ids"]))
        page = await self._browser.new_page(
            viewport={"width": 1280, "height": min(16000, 260 + row_count * 78)},
            device_scale_factor=1.5,
        )
        try:
            await page.set_content(html, wait_until="load")
            return await page.locator("#schedule").screenshot(type="png")
        finally:
            await page.close()

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
