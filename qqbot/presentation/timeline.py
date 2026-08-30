from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from qqbot.domain.calendar import SHANGHAI_TZ
from qqbot.domain.models import Occupancy, OperationResult, TimeRange, minutes_to_text
from qqbot.infrastructure.config import SiteConfig

WEEKDAY_NAMES = "一二三四五六日"

# 昼夜主题：19:00 ~ 次日 07:00 深色，其余浅色。
DARK_START_HOUR = 19
DARK_END_HOUR = 7


def current_theme(now: datetime | None = None) -> str:
    """按时刻返回图片主题：深色（night）或浅色（day）。"""
    now = now or datetime.now(SHANGHAI_TZ)
    hour = now.hour
    return "dark" if hour >= DARK_START_HOUR or hour < DARK_END_HOUR else "light"


def _percentage(value: int, opening: int, duration: int) -> float:
    return round((value - opening) / duration * 100, 4)


def _reservation_label(item: Occupancy) -> str:
    # 类型信息由块颜色 + 图例表达（预约=蓝、周常=橙、锁定=紫），
    # 块内只保留实际内容；预约按脱敏规则只显示名字末 4 字。
    if item.kind in {"routine", "lock"}:
        return item.label
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


def _collapsed_row(days: list[dict[str, Any]], day_alt: bool) -> dict[str, Any]:
    """连续空日合并成一行「无占用」摘要（schedule 模式折叠空行）。"""
    start, end = days[0]["date"], days[-1]["date"]
    if start == end:
        date_label = f"{start.month}月{start.day}日 · 周{WEEKDAY_NAMES[start.weekday()]}"
    else:
        date_label = f"{start.month}月{start.day}日 ～ {end.month}月{end.day}日"
    return {
        "collapsed": True,
        "date": start.isoformat(),
        "date_label": date_label,
        "room": "",
        "blocks": [],
        "day_alt": day_alt,
    }


def build_timeline_view(config: SiteConfig, result: OperationResult) -> dict[str, Any]:
    """把查询结果转换成模板数据；不接触 QQ SDK 或浏览器。"""
    if result.code not in {"schedule_range", "free_slots_range"}:
        raise ValueError(f"不支持的可视化结果：{result.code}")

    mode = "schedule" if result.code == "schedule_range" else "free"
    opening, closing = config.open_minutes, config.close_minutes
    room_ids = result.data["room_ids"]
    rows: list[dict[str, Any]] = []

    def room_blocks(day: dict[str, Any], room_id: str) -> list[dict[str, Any]]:
        """把某天某房间的占用/空闲片段转成时间轴块。"""
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
        return blocks

    def day_empty(day: dict[str, Any]) -> bool:
        """schedule 模式：该天所有查询房间都无占用视为空日（可折叠）。"""
        return mode == "schedule" and all(
            not room_blocks(day, room_id) for room_id in room_ids
        )

    # 空日连续段合并成一行「无占用」摘要；非空日正常逐房间展开。
    empty_run: list[dict[str, Any]] = []
    empty_run_alt = False  # 折叠段取首日的交替色，保证与相邻日期错开
    for day_index, day in enumerate(result.data["days"]):
        day_alt = day_index % 2 == 1
        if day_empty(day):
            if not empty_run:
                empty_run_alt = day_alt
            empty_run.append(day)
            continue
        if empty_run:
            rows.append(_collapsed_row(empty_run, empty_run_alt))
            empty_run = []
        for room_id in room_ids:
            target = day["date"]
            rows.append(
                {
                    "date": target.isoformat(),
                    "date_label": f"{target.month}月{target.day}日 · 周{WEEKDAY_NAMES[target.weekday()]}",
                    "offset": day.get("offset"),
                    "room": config.room_by_id(room_id).name,
                    "blocks": room_blocks(day, room_id),
                    "day_alt": day_alt,
                }
            )
    if empty_run:
        rows.append(_collapsed_row(empty_run, empty_run_alt))

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
    """用本地 HTML 模板生成时间轴 PNG；浏览器不可用时由调用方回退文字。

    字体加载（性能优化，2026-08-30）：模板里的 ``@font-face`` 引用虚拟域名
    ``http://fonts.local/*.woff2``，渲染时由 Playwright ``page.route`` 拦截该
    请求、直接从本地字体文件返回（启动时读入内存缓存）。相比把 woff2 base64
    内联进 HTML（~3MB，每次渲染解析开销 ~400ms），HTML 保持 ~13KB，渲染从
    ~630ms 降到 ~170ms。字体缺失时 route 直接 abort，浏览器回退系统字体。
    """

    FONT_URL_PREFIX = "http://fonts.local/"

    def __init__(self, config: SiteConfig, template_dir: Path | None = None) -> None:
        self.config = config
        templates = template_dir or Path(__file__).with_name("templates")
        self.environment = Environment(
            loader=FileSystemLoader(templates),
            autoescape=True,
        )
        self.template = self.environment.get_template("schedule.html.jinja")
        self._font_bytes: dict[str, bytes] = self._load_fonts(templates / "fonts")
        self._playwright: Any = None
        self._browser: Any = None

    @staticmethod
    def _load_fonts(font_dir: Path) -> dict[str, bytes]:
        """把 fonts/ 下的 woff2 读入内存（启动一次），供 route 按字重返回。

        键为模板字重名（font_400 / font_700），启动时读取后不再访问磁盘。
        """
        if not font_dir.is_dir():
            return {}
        data: dict[str, bytes] = {}
        for path in sorted(font_dir.glob("*.woff2")):
            try:
                content = path.read_bytes()
            except OSError:
                continue
            name = path.stem
            if "400" in name or "regular" in name:
                data["font_400"] = content
            elif "700" in name or "bold" in name:
                data["font_700"] = content
        return data

    @property
    def available(self) -> bool:
        return self._browser is not None

    def render_html(self, result: OperationResult, theme: str | None = None) -> str:
        view = build_timeline_view(self.config, result)
        view["theme"] = theme or current_theme()
        view["generated_at"] = datetime.now(SHANGHAI_TZ).strftime("%m-%d %H:%M")
        return self.template.render(**view)

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
            viewport={"width": 1820, "height": min(16000, 260 + row_count * 62)},
            device_scale_factor=1.2,
        )
        try:
            await page.route(f"{self.FONT_URL_PREFIX}**", self._serve_font)
            await page.set_content(html, wait_until="load")
            # 字体经 route 异步加载，wait_until="load" 不等待字体；
            # 必须等 document.fonts.ready 后再测量/截图，否则会回退系统字体（豆腐块）。
            await page.evaluate("document.fonts.ready")
            await self._fit_row_heights(page)
            raw = await page.locator("#schedule").screenshot(type="png")
            return self._optimize_png(raw)
        finally:
            await page.close()

    @staticmethod
    def _optimize_png(raw: bytes) -> bytes:
        """PNG 调色板量化：时间轴图以大块纯色 + 少量文字为主，
        256 色调色板足够（量化误差 ~0.03，文字抗锯齿灰阶保留）。

        FASTOCTREE（快速八叉树）比 MEDIANCUT 更适合纯色图：
        颜色聚类收敛快（48ms vs 98ms），且体积更小（50KB vs 96KB）。
        实测（2.5M 像素）：MEDIANCUT 98ms/96KB，FASTOCTREE 48ms/50KB。
        Pillow 不可用时原样返回。
        """
        try:
            from PIL import Image
        except ImportError:
            return raw
        try:
            import io

            image = Image.open(io.BytesIO(raw)).convert("RGB")
            quantized = image.quantize(
                colors=256,
                method=Image.FASTOCTREE,
                dither=Image.NONE,
            )
            out = io.BytesIO()
            quantized.save(out, format="PNG", optimize=True)
            result = out.getvalue()
            return result if len(result) < len(raw) else raw
        except Exception:
            return raw

    async def _serve_font(self, route: Any) -> None:
        """拦截模板里的字体请求，从内存返回 woff2；字体缺失时 abort 回退系统字体。"""
        url = str(route.request.url)
        key = "font_700" if "-700-" in url or "700" in url else "font_400"
        content = self._font_bytes.get(key)
        if content is None:
            await route.abort()
            return
        await route.fulfill(status=200, content_type="font/woff2", body=content)

    @staticmethod
    async def _fit_row_heights(page: Any) -> None:
        """行高自适应：块内名字换行后块变高，同行所有块等高、整行撑到容纳最高块。

        字体大小固定（13px 名字 / 11px 时间），不压缩字号；同行块统一取最高块
        的高度（底部对齐，避免参差不齐），行高 = 块高 + 上下留白。
        空行（无块）与折叠行保持 CSS 默认矮行高（40px）。
        """
        await page.evaluate(
            """() => {
                const TOP = 7, BOTTOM = 7;
                for (const row of document.querySelectorAll('.row')) {
                    const track = row.querySelector('.track');
                    if (!track) continue;
                    const blocks = [...track.querySelectorAll('.block')];
                    if (!blocks.length) {
                        row.style.minHeight = '';
                        track.style.minHeight = '';
                        continue;
                    }
                    let maxH = 54;
                    for (const block of blocks) {
                        maxH = Math.max(maxH, block.offsetHeight);
                    }
                    for (const block of blocks) {
                        block.style.height = maxH + 'px';
                    }
                    const rowH = maxH + TOP + BOTTOM;
                    row.style.minHeight = rowH + 'px';
                    track.style.minHeight = rowH + 'px';
                }
            }"""
        )

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
