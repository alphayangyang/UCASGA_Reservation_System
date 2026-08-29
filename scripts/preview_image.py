"""渲染时间轴图片预览（开发用）：分别用旧版/新版模板渲染 schedule 与 free 两种模式。

用法：python scripts/preview_image.py [--template DIR]
默认同时渲染两种模板到 scripts/preview/ 目录。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qqbot.domain.models import DateRange, Occupancy, OperationResult, TimeRange
from qqbot.infrastructure.config import load_all_configs
from qqbot.presentation.timeline import ScheduleImageRenderer

ROOT = Path(__file__).resolve().parents[1]


def build_result(config, *, mode: str = "schedule") -> OperationResult:
    main = config.rooms[0]
    if mode == "schedule":
        return OperationResult.success(
            "schedule_range",
            date_range=DateRange(date(2026, 8, 10), date(2026, 8, 12)),
            room_ids=[room.id for room in config.rooms[:3]],
            days=[
                {
                    "date": date(2026, 8, 10),
                    "offset": 0,
                    "occupancies": [
                        Occupancy(main.id, TimeRange(8 * 60, 9 * 60 + 30), "reservation", "王小明同学"),
                        Occupancy(main.id, TimeRange(10 * 60, 11 * 60), "routine", "钢琴与弦乐重奏团排练"),
                        Occupancy(main.id, TimeRange(19 * 60, 21 * 60 + 30), "reservation", "李华"),
                        Occupancy(main.id, TimeRange(21 * 60 + 30, 23 * 60), "lock", "钢琴调律与设备维护"),
                    ],
                    "admin_view": False,
                },
                {
                    "date": date(2026, 8, 11),
                    "offset": 1,
                    "occupancies": [
                        Occupancy(main.id, TimeRange(7 * 60, 8 * 60), "routine", "晨间基本功练习"),
                        Occupancy(main.id, TimeRange(13 * 60, 15 * 60), "reservation", "张三丰"),
                    ],
                    "admin_view": False,
                },
                {
                    "date": date(2026, 8, 12),
                    "offset": 2,
                    "occupancies": [],
                    "admin_view": False,
                },
            ],
        )
    return OperationResult.success(
        "free_slots_range",
        date_range=DateRange(date(2026, 8, 10), date(2026, 8, 10)),
        room_ids=[room.id for room in config.rooms[:3]],
        days=[
            {
                "date": date(2026, 8, 10),
                "offset": 0,
                "slots": {
                    room.id: [TimeRange(7 * 60, 9 * 60), TimeRange(12 * 60, 14 * 60)]
                    for room in config.rooms[:3]
                },
            }
        ],
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--template", type=Path, default=None, help="自定义模板目录（含 schedule.html.jinja）"
    )
    parser.add_argument(
        "--theme",
        choices=["light", "dark", "auto"],
        default="auto",
        help="图片主题（默认按当前时刻）",
    )
    args = parser.parse_args()

    configs = load_all_configs(ROOT / "configs", project_root=ROOT)
    config = next(iter(configs.values()))

    # 默认渲染正式模板；--template 可指定其他模板目录对比。
    templates: list[tuple[str, Path | None]] = [("production", None)]
    if args.template is not None:
        templates = [(args.template.stem, args.template)]

    out = ROOT / "scripts" / "preview"
    out.mkdir(parents=True, exist_ok=True)

    for name, template_dir in templates:
        renderer = ScheduleImageRenderer(config, template_dir=template_dir)
        await renderer.start()
        try:
            for mode in ("schedule", "free"):
                result = build_result(config, mode=mode)
                theme = args.theme if args.theme != "auto" else None
                html = renderer.render_html(result, theme=theme)
                row_count = max(1, len(result.data["days"]) * len(result.data["room_ids"]))
                page = await renderer._browser.new_page(
                    viewport={"width": 1280, "height": min(16000, 260 + row_count * 78)},
                    device_scale_factor=1.5,
                )
                await page.set_content(html, wait_until="load")
                await ScheduleImageRenderer._fit_row_heights(page)
                png = await page.locator("#schedule").screenshot(type="png")
                await page.close()
                suffix = theme or "auto"
                path = out / f"preview_{mode}_{name}_{suffix}.png"
                path.write_bytes(png)
                print(f"written {path.relative_to(ROOT)} ({len(png)} bytes)")
        finally:
            await renderer.close()


if __name__ == "__main__":
    asyncio.run(main())
