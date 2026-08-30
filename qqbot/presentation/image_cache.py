"""查询图片预渲染缓存（方案 B）。

设计要点（详见对话决策）：

- 每个站点一个内存 revision 计数器；任何写命令成功（按结果码白名单）都 +1，
  使全部旧缓存条目即刻作废——**绝不发旧图**；
- 缓存条目携带生成时的 revision，查询时校验：revision 匹配才可用；
- 写命令后投递**去抖**预渲染任务（debounce_ms 窗口合并连续写），任务醒来时
  渲染「默认查询范围」（按 config.query.default_ranges 动态提取，去重 × 双主题
  × schedule/free）；
- 查询时缓存未命中但目标正在渲染 → 等待该任务完成（wait_timeout），拿最新结果；
- **无 TTL**：只靠 revision 作废 + LRU 容量上限淘汰（查询分布稀疏时避免
  「过期导致的下一次查询反而慢」）；
- 实时渲染的查询结果也回填缓存（带 revision），下次同范围查询直接命中。

模块无 QQ SDK / 浏览器依赖，可独立单测。
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from qqbot.domain.calendar import SHANGHAI_TZ
from qqbot.domain.models import DateRange, OperationResult
from qqbot.infrastructure.config import SiteConfig

logger = logging.getLogger(__name__)

# 写命令结果码白名单：只有真的改了数据的才触发缓存作废/预渲染。
# （部分成功 reservation_partially_created 也触发；nothing_to_cancel 等无变化不触发）
MUTATING_CODES = frozenset(
    {
        "reservation_created",
        "reservation_partially_created",
        "reservation_cancelled",
        "all_reservations_cancelled",
        "routine_added",
        "routine_removed",
        "lock_added",
        "lock_removed",
        "clear_undone",
        "date_cleared",
    }
)

QUERY_CODES = frozenset({"schedule_range", "free_slots_range"})


@dataclass
class CacheEntry:
    """一条已渲染图片。revision 为生成它时的数据版本。"""

    key: tuple[Any, ...]
    png: bytes
    revision: int
    rendered_at: datetime


@dataclass
class RenderingTask:
    """正在进行的预渲染任务（供查询等待）。"""

    revision: int
    task: asyncio.Task[None]
    started_at: datetime = field(default_factory=lambda: datetime.now(SHANGHAI_TZ))


def _range_key(
    bot_id: str, mode: str, date_range: DateRange, room_ids: tuple[str, ...], theme: str
) -> tuple[Any, ...]:
    return (bot_id, mode, date_range.start, date_range.end, tuple(sorted(room_ids)), theme)


class ImageCache:
    """按站点管理 revision 与 LRU 图片缓存。非线程安全，单事件循环内使用。"""

    def __init__(self, max_entries_per_site: int = 100) -> None:
        self.max_entries_per_site = max_entries_per_site
        self._revisions: dict[str, int] = {}  # bot_id -> revision
        self._entries: OrderedDict[tuple[Any, ...], CacheEntry] = OrderedDict()
        self._rendering: dict[str, RenderingTask] = {}  # bot_id -> 正在渲染的任务
        # 命中率观测（上线后评估预渲染策略）：
        # hits[cache|wait|render] 按站点累计，stats() 汇总日志。
        self._hits: dict[str, dict[str, int]] = {}

    def record_hit(self, bot_id: str, source: str) -> None:
        """记录查询图片来源：cache（缓存命中）/ wait（等待预渲染）/ render（实时渲染）。"""
        self._hits.setdefault(bot_id, {}).setdefault(source, 0)
        self._hits[bot_id][source] += 1

    def stats(self) -> dict[str, dict[str, int]]:
        """返回每站点的命中统计副本（供日志/健康检查）。"""
        return {bot_id: dict(counts) for bot_id, counts in self._hits.items()}

    # ── revision ──────────────────────────────────────────────

    def revision(self, bot_id: str) -> int:
        return self._revisions.get(bot_id, 0)

    def invalidate(self, bot_id: str) -> int:
        """写命令成功后调用：revision +1，全部旧缓存即刻作废。返回新 revision。"""
        revision = self._revisions.get(bot_id, 0) + 1
        self._revisions[bot_id] = revision
        # 按站点批量淘汰旧条目（保留其他站点）
        stale = [key for key in self._entries if key[0] == bot_id]
        for key in stale:
            self._entries.pop(key, None)
        # 正在渲染的旧 revision 任务一并作废（新写命令后其产物不再可信）
        self._rendering.pop(bot_id, None)
        return revision

    # ── get / put ─────────────────────────────────────────────

    def get(
        self, bot_id: str, mode: str, date_range: DateRange, room_ids: tuple[str, ...], theme: str
    ) -> CacheEntry | None:
        key = _range_key(bot_id, mode, date_range, room_ids, theme)
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.revision != self.revision(bot_id):
            # 版本过期（期间有写命令）→ 丢弃
            self._entries.pop(key, None)
            return None
        self._entries.move_to_end(key)
        return entry

    def put(
        self, bot_id: str, mode: str, date_range: DateRange, room_ids: tuple[str, ...], theme: str, png: bytes
    ) -> None:
        revision = self.revision(bot_id)
        key = _range_key(bot_id, mode, date_range, room_ids, theme)
        self._entries[key] = CacheEntry(
            key=key, png=png, revision=revision, rendered_at=datetime.now(SHANGHAI_TZ)
        )
        self._entries.move_to_end(key)
        # LRU 淘汰：只数本站点条目
        site_count = sum(1 for k in self._entries if k[0] == bot_id)
        while site_count > self.max_entries_per_site:
            oldest_key = next(k for k in self._entries if k[0] == bot_id)
            self._entries.pop(oldest_key, None)
            site_count -= 1

    def clear(self) -> None:
        self._entries.clear()
        self._revisions.clear()
        self._rendering.clear()

    # ── 渲染中任务（供查询等待）───────────────────────────────

    def mark_rendering(self, bot_id: str, revision: int, task: asyncio.Task[None]) -> None:
        self._rendering[bot_id] = RenderingTask(revision=revision, task=task)

    def rendering_for(self, bot_id: str, revision: int) -> RenderingTask | None:
        task = self._rendering.get(bot_id)
        if task is not None and task.revision == revision and not task.task.done():
            return task
        return None


class PreRenderScheduler:
    """去抖预渲染调度器：写命令后合并渲染请求，后台渲染默认范围。

    渲染函数签名：``async def render_default(bot_id, mode, date_range, theme) -> bytes | None``
    """

    def __init__(
        self,
        cache: ImageCache,
        debounce_ms: int = 500,
        wait_timeout: float = 3.0,
    ) -> None:
        self.cache = cache
        self.debounce_ms = debounce_ms
        self.wait_timeout = wait_timeout
        self._timers: dict[str, asyncio.Task[None]] = {}  # bot_id -> 去抖定时器
        self._render_fn: Callable[..., Any] | None = None
        self._context_provider: Callable[[str], Any] | None = None

    def configure(
        self,
        render_fn: Callable[..., Any],
        context_provider: Callable[[str], Any],
    ) -> None:
        """注入渲染函数（async）与站点上下文提供者（返回含 config/application 的对象）。"""
        self._render_fn = render_fn
        self._context_provider = context_provider

    def schedule(self, bot_id: str) -> None:
        """写命令成功后调用：去抖投递预渲染。"""
        timer = self._timers.get(bot_id)
        if timer is not None and not timer.done():
            timer.cancel()
        self._timers[bot_id] = asyncio.ensure_future(self._debounced(bot_id))

    async def _debounced(self, bot_id: str) -> None:
        try:
            await asyncio.sleep(self.debounce_ms / 1000)
        except asyncio.CancelledError:
            return  # 窗口内又来写命令，被新定时器取代
        await self.run_once(bot_id)

    async def run_once(self, bot_id: str) -> None:
        """渲染该站点全部默认范围（双主题 × schedule/free）。"""
        if self._render_fn is None or self._context_provider is None:
            return
        revision = self.cache.revision(bot_id)
        context = self._context_provider(bot_id)
        config: SiteConfig = context.config
        application = context.application

        # 从 config 动态提取默认范围（全部角色 default_ranges 去重）
        ranges = default_ranges_from_config(config)
        max_days = config.query.max_range_days
        now = datetime.now(SHANGHAI_TZ)
        calendar = application.calendar

        # 主题优先级：先渲染当前主题（用户马上可能查询），再渲染另一主题（静默补）
        from qqbot.presentation.timeline import current_theme

        themes = [current_theme(now)]
        themes.append("dark" if themes[0] == "light" else "light")

        for start_offset, end_offset in ranges:
            day_count = end_offset - start_offset + 1
            if day_count > max_days:
                end_offset = start_offset + max_days - 1
            start_date = calendar.resolve_offset(now, start_offset)
            end_date = start_date + timedelta(days=end_offset - start_offset)
            date_range = DateRange(start_date, end_date)
            for theme in themes:
                for mode in ("schedule", "free"):
                    await self._render_and_cache(bot_id, context, mode, date_range, theme, revision)
        logger.info("预渲染完成 bot_id=%s revision=%d ranges=%s", bot_id, revision, ranges)

    async def _render_and_cache(
        self,
        bot_id: str,
        context: Any,
        mode: str,
        date_range: DateRange,
        theme: str,
        revision: int,
    ) -> None:
        """渲染单个范围并写入缓存；渲染期间记录任务供查询等待。"""
        room_ids = tuple(room.id for room in context.config.rooms)
        task = asyncio.current_task()
        if task is not None:
            self.cache.mark_rendering(bot_id, revision, task)

        try:
            result = self._build_result(context, mode, date_range)
            if result is None:
                return
            png = await self._render_fn(bot_id, result, theme)
            if png is None:
                return
            self.cache.put(bot_id, mode, date_range, room_ids, theme, png)
            logger.info(
                "预渲染缓存 bot_id=%s mode=%s %s theme=%s (%d bytes)",
                bot_id, mode, date_range, theme, len(png),
            )
        except Exception:
            logger.exception("预渲染失败 bot_id=%s mode=%s theme=%s", bot_id, mode, theme)
        finally:
            if task is not None and self.cache._rendering.get(bot_id) is not None:
                self.cache._rendering.pop(bot_id, None)

    @staticmethod
    def _build_result(context: Any, mode: str, date_range: DateRange) -> OperationResult | None:
        """用 Application 构造预渲染用的查询结果（无身份系统读）。"""
        application = context.application
        if mode == "schedule":
            return application.schedule_range_system(date_range)
        return application.free_slots_range_system(date_range)


def default_ranges_from_config(config: SiteConfig) -> list[tuple[int, int]]:
    """从 config.query.default_ranges 提取全部角色默认范围并去重（供测试/调试）。"""
    raw = config.query.default_ranges or {"user": (0, 0)}
    return sorted(set(raw.values()))
