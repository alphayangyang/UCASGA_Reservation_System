from __future__ import annotations

import asyncio
from datetime import date

from qqbot.domain.models import DateRange
from qqbot.presentation.image_cache import (
    MUTATING_CODES,
    ImageCache,
    default_ranges_from_config,
)


def test_mutating_codes_include_partial_success() -> None:
    """多人抢造成的部分成功必须触发预渲染；无变化的码不触发。"""
    assert "reservation_created" in MUTATING_CODES
    assert "reservation_partially_created" in MUTATING_CODES
    assert "reservation_cancelled" in MUTATING_CODES
    assert "routine_added" in MUTATING_CODES
    assert "lock_added" in MUTATING_CODES
    # 无变化的码不在白名单
    assert "nothing_to_cancel" not in MUTATING_CODES
    assert "routine_not_found" not in MUTATING_CODES
    assert "schedule_range" not in MUTATING_CODES


def test_revision_invalidates_stale_entries() -> None:
    cache = ImageCache()
    dr = DateRange(date(2026, 8, 10), date(2026, 8, 10))
    cache.put("yqh", "schedule", dr, ("303",), "light", b"img-v1")
    assert cache.get("yqh", "schedule", dr, ("303",), "light") is not None

    # 写命令 → invalidate → 旧缓存作废
    cache.invalidate("yqh")
    assert cache.get("yqh", "schedule", dr, ("303",), "light") is None

    # 新 revision 下重新放入可读
    cache.put("yqh", "schedule", dr, ("303",), "light", b"img-v2")
    assert cache.get("yqh", "schedule", dr, ("303",), "light") is not None
    assert cache.get("yqh", "schedule", dr, ("303",), "light").png == b"img-v2"


def test_invalidate_only_affects_that_site() -> None:
    cache = ImageCache()
    dr = DateRange(date(2026, 8, 10), date(2026, 8, 10))
    cache.put("yqh", "schedule", dr, ("303",), "light", b"yqh")
    cache.put("yql", "schedule", dr, ("yql-main",), "light", b"yql")

    cache.invalidate("yqh")

    assert cache.get("yqh", "schedule", dr, ("303",), "light") is None
    assert cache.get("yql", "schedule", dr, ("yql-main",), "light") is not None


def test_lru_evicts_per_site() -> None:
    cache = ImageCache(max_entries_per_site=2)
    for i in range(3):
        dr_i = DateRange(date(2026, 8, 10 + i), date(2026, 8, 10 + i))
        cache.put("yqh", "schedule", dr_i, ("303",), "light", f"img{i}".encode())

    # 只保留最近 2 条
    d0 = DateRange(date(2026, 8, 10), date(2026, 8, 10))
    d1 = DateRange(date(2026, 8, 11), date(2026, 8, 11))
    d2 = DateRange(date(2026, 8, 12), date(2026, 8, 12))
    assert cache.get("yqh", "schedule", d0, ("303",), "light") is None
    assert cache.get("yqh", "schedule", d1, ("303",), "light") is not None
    assert cache.get("yqh", "schedule", d2, ("303",), "light") is not None


def test_mark_rendering_and_wait() -> None:
    cache = ImageCache()
    dr = DateRange(date(2026, 8, 10), date(2026, 8, 10))

    async def fake_render():
        await asyncio.sleep(0.05)
        cache.put("yqh", "schedule", dr, ("303",), "light", b"done")

    async def exercise() -> bytes | None:
        revision = cache.invalidate("yqh")
        task = asyncio.ensure_future(fake_render())
        cache.mark_rendering("yqh", revision, task)
        # 缓存未就绪但正在渲染 → 等待
        rendering = cache.rendering_for("yqh", revision)
        assert rendering is not None
        await rendering.task
        entry = cache.get("yqh", "schedule", dr, ("303",), "light")
        return entry.png if entry else None

    assert asyncio.run(exercise()) == b"done"


def test_rendering_for_returns_none_when_revision_changed() -> None:
    """等待期间又有写命令 → revision 变了 → 原渲染任务不再可信。"""
    cache = ImageCache()

    async def exercise() -> None:
        rev1 = cache.invalidate("yqh")
        task = asyncio.ensure_future(asyncio.sleep(10))
        cache.mark_rendering("yqh", rev1, task)
        rev2 = cache.invalidate("yqh")  # 又来一次写
        assert cache.rendering_for("yqh", rev1) is None  # 旧 revision 不可等
        assert cache.rendering_for("yqh", rev2) is None  # 新 revision 无任务
        task.cancel()

    asyncio.run(exercise())


def test_default_ranges_dynamic_from_config(yql_config) -> None:
    """预渲染范围从 config.query.default_ranges 动态提取并去重。"""
    ranges = default_ranges_from_config(yql_config)
    # yql: user(0,0) band(0,1) admin(0,6) owner(0,6) → 去重后
    assert (0, 0) in ranges
    assert (0, 1) in ranges
    assert (0, 6) in ranges
    assert len(ranges) == 3


def test_hit_stats_recorded_per_site() -> None:
    """命中统计按站点累计，stats() 返回副本。"""
    cache = ImageCache()
    cache.record_hit("yqh", "cache")
    cache.record_hit("yqh", "cache")
    cache.record_hit("yqh", "render")
    cache.record_hit("yql", "wait")

    stats = cache.stats()
    assert stats["yqh"] == {"cache": 2, "render": 1}
    assert stats["yql"] == {"wait": 1}
    # 副本：外部修改不影响内部
    stats["yqh"]["cache"] = 99
    assert cache.stats()["yqh"]["cache"] == 2
