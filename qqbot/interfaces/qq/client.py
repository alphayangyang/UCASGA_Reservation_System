from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import botpy
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from botpy.message import GroupMessage

from qqbot.application.resolver import CommandResolver
from qqbot.application.service import BookingApplication, Dispatcher
from qqbot.domain.calendar import SHANGHAI_TZ
from qqbot.domain.errors import AppError
from qqbot.domain.models import ExternalIdentity, OperationResult, RequestContext
from qqbot.infrastructure.config import SiteConfig, load_all_configs
from qqbot.infrastructure.group_bindings import GroupBindingStore
from qqbot.infrastructure.sqlite_repository import SQLiteBookingRepository
from qqbot.interfaces.qq.broadcaster import (
    ClockAnnounceJob,
    ProactiveSender,
    RoutineBroadcastJob,
    SilentEndReportJob,
)
from qqbot.interfaces.qq.media_uploader import QQMediaUploader
from qqbot.interfaces.qq.parser import ParsedIntent, QQCommandParser
from qqbot.interfaces.qq.presenter import QQPresenter
from qqbot.nlu import NLU_DATA_DIR, NLUIntentMatcher, mask_sensitive, run_nightly_job, write_pending
from qqbot.presentation.image_cache import MUTATING_CODES, QUERY_CODES, ImageCache, PreRenderScheduler
from qqbot.presentation.timeline import ScheduleImageRenderer, current_theme

logger = logging.getLogger(__name__)


def _merge_extra_aliases(config: SiteConfig, extra: dict[str, str]) -> SiteConfig:
    """把白名单补充源（{"别名原文": "room_id"}）合并进对应房间的 aliases。

    使 Resolver.room_by_reference 能解析生长别名（RoomConfig 只认 name+aliases）。
    无补充/room_id 不匹配 → 原样返回；dataclasses.replace 保持不可变语义。
    """
    if not extra:
        return config
    from dataclasses import replace

    rooms = []
    for room in config.rooms:
        added = tuple(alias for alias, room_id in extra.items() if room_id == room.id)
        rooms.append(replace(room, aliases=(*room.aliases, *added)) if added else room)
    return replace(config, rooms=tuple(rooms))


class PianoBotClient(botpy.Client):
    def __init__(
        self,
        configs: dict[str, SiteConfig],
        control_db: Path,
        intents: botpy.Intents | None = None,
        renderers: dict[str, ScheduleImageRenderer] | None = None,
    ) -> None:
        super().__init__(intents=intents or botpy.Intents.default())
        # 房间白名单补充源（v2：{"站点": {"别名原文": "room_id"}}）先合并进 configs——
        # 让 Resolver.room_by_reference 能解析生长别名（room_by_reference 只认配置 aliases）。
        whitelist_path = NLU_DATA_DIR / "room_whitelist.json"
        try:
            whitelist = json.loads(whitelist_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("房间白名单文件不可读（%s），仅使用配置权威源", whitelist_path)
            whitelist = {}
        extra_aliases = whitelist.get("extra_aliases", {}) if isinstance(whitelist, dict) else {}
        configs = {
            bot_id: _merge_extra_aliases(config, extra_aliases.get(bot_id, {}))
            for bot_id, config in configs.items()
        }
        self.configs = configs
        self.repositories = {bot_id: SQLiteBookingRepository(config) for bot_id, config in configs.items()}
        for repository in self.repositories.values():
            repository.initialize()
        self.dispatchers = {
            bot_id: Dispatcher(BookingApplication(configs[bot_id], repository))
            for bot_id, repository in self.repositories.items()
        }
        self.presenters = {bot_id: QQPresenter(config) for bot_id, config in configs.items()}
        self.renderers = (
            renderers
            if renderers is not None
            else {
                bot_id: ScheduleImageRenderer(config)
                for bot_id, config in configs.items()
                if config.query.image_enabled
            }
        )
        self.group_bindings = GroupBindingStore(control_db)
        self.group_bindings.initialize()
        imported = self.group_bindings.import_legacy_json(control_db.parent.parent / "group_mappings.json")
        if imported:
            logger.info("已从旧 group_mappings.json 导入 %s 条群绑定", imported)
        # 查询图片预渲染缓存：写命令成功后后台渲染默认范围，查询优先命中缓存。
        # 任一步失败都不影响既有链路（回退实时渲染 → 文字）。
        self.image_cache = ImageCache()
        self.pre_render = PreRenderScheduler(self.image_cache)
        self.pre_render.configure(self._render_for_cache, self._site_context)
        self._render_lock = asyncio.Lock()
        # Phase 1：NLU 数据收集（docs/NLU-DESIGN.md 5.2/5.4）。
        # 插件私有数据目录（qqbot/nlu/data，不混入 data/ 业务库）；api_key 缺失不挂夜间任务。
        # 注意：必须在构造 NLUIntentMatcher 之前初始化（model_path 依赖它）。
        self._nlu_dir = NLU_DATA_DIR
        self._deepseek_api_key = os.getenv("DEEPSEEK_API_KEY", "")
        # NLU 为实验性功能：任一站点配置 features.nlu_enabled 即启用（共享 parser）。
        # Phase 2：intent_model.json 存在时自动挂载 ML 兜底通道（懒加载，损坏自动关闭）。
        # 称呼前缀：bot 昵称（小泉）+ 各站点 bot_name（「玉泉路琴房帮我约…」）。
        # 房间白名单（gazetteer）：权威源 = 全部站点房间 name+aliases 并集，
        # 补充源 = room_whitelist.json 的 extra_aliases 别名原文（keys）——
        # 上述 _merge_extra_aliases 已把 room_id 映射合并进 configs（Resolver 可解析）。
        # 跨站别名由 Resolver.room_by_reference 按站点兜底（本站不存在 → 安全提示）。
        room_aliases = tuple(
            alias
            for config in configs.values()
            for room in config.rooms
            for alias in (room.name, *room.aliases)
        )
        room_aliases = (
            *room_aliases,
            *(alias for site_extra in extra_aliases.values() for alias in site_extra),
        )
        # 闲聊关键词补充（自优化生长产物 chitchat_keywords.json；缺失/损坏不影响启动）
        try:
            chitchat_data = json.loads((NLU_DATA_DIR / "chitchat_keywords.json").read_text(encoding="utf-8"))
            chitchat_keywords = tuple(chitchat_data.get("keywords", []))
        except (OSError, ValueError):
            chitchat_keywords = ()
        nlu = (
            NLUIntentMatcher(
                model_path=self._nlu_dir / "intent_model.json",
                name_prefixes=tuple(config.bot_name for config in configs.values()),
                room_aliases=room_aliases,
                chitchat_keywords=chitchat_keywords,
            )
            if any(config.features.nlu_enabled for config in configs.values())
            else None
        )
        self.parser = QQCommandParser(nlu=nlu)
        self.resolver = CommandResolver()
        self.scheduler: AsyncIOScheduler | None = None

    async def on_ready(self) -> None:
        logger.info("QQ 琴房机器人已连接；已加载配置：%s", ", ".join(self.configs))
        for bot_id, renderer in self.renderers.items():
            try:
                await renderer.start()
            except Exception:
                logger.exception("站点 %s 的查询图片渲染器启动失败，将使用文字结果", bot_id)
        if self.scheduler is None:
            self.scheduler = AsyncIOScheduler(timezone=SHANGHAI_TZ)
            self.scheduler.add_job(
                self._cleanup_all_sites,
                "cron",
                hour=4,
                minute=0,
                id="daily_cleanup",
                replace_existing=True,
            )
            # 每日图片缓存命中率汇总（评估预渲染策略用）
            self.scheduler.add_job(
                self._log_image_cache_stats,
                "cron",
                hour=4,
                minute=5,
                id="image_cache_stats",
                replace_existing=True,
            )
            # 夜间 LLM 批处理标注（文档 5.4）：与归档并列，04:30 错开执行。
            # 任务逻辑在 qqbot/nlu/annotate.py（run_nightly_job），client 只负责挂载。
            if self._deepseek_api_key:
                self.scheduler.add_job(
                    run_nightly_job,
                    "cron",
                    hour=4,
                    minute=30,
                    args=[self._nlu_dir, self.configs, self._deepseek_api_key],
                    id="nlu_nightly_annotate",
                    replace_existing=True,
                )
            self._register_broadcast_jobs()
            self.scheduler.start()

    async def close(self) -> None:
        for renderer in self.renderers.values():
            try:
                await renderer.close()
            except Exception:
                logger.exception("关闭查询图片渲染器失败")
        if self.scheduler is not None and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        await super().close()

    async def _cleanup_all_sites(self) -> None:
        now = datetime.now(SHANGHAI_TZ)
        cutoff = now.date() - timedelta(days=90)
        for bot_id, repository in self.repositories.items():
            try:
                count = repository.cleanup_old(cutoff)
                if count:
                    logger.info("站点 %s 归档并清理 %s 条旧预约", bot_id, count)
            except Exception:
                logger.exception("站点 %s 的历史数据清理失败", bot_id)

    async def _log_image_cache_stats(self) -> None:
        """每日输出图片缓存命中统计（评估预渲染策略）。"""
        for bot_id, counts in self.image_cache.stats().items():
            total = sum(counts.values())
            if not total:
                continue
            hit = counts.get("cache", 0)
            wait = counts.get("wait", 0)
            rate = hit / total * 100
            logger.info(
                "图片缓存统计 bot_id=%s 查询=%d 命中缓存=%d(%.0f%%) 等待预渲染=%d 实时渲染=%d",
                bot_id,
                total,
                hit,
                rate,
                wait,
                counts.get("render", 0),
            )

    def _register_broadcast_jobs(self) -> None:
        """按站点配置挂载定时播报（逻辑在 qqbot/interfaces/qq/broadcaster.py）。

        每个站点独立开关，时刻全部来自 YAML：
        - features.broadcast 且 features.weekly_routine → 按 booking.routine_broadcast.time
          每天播报未来 days 天（从明天起）的周常占用；
        - features.clock_announce → 静默期开始时刻（silent_period.start）文字报时；
        - features.silent_end_report → 静默期结束时刻（silent_period.end）播报次日预约情况。
        """

        if self.scheduler is None:
            return
        sender = ProactiveSender(self.api, self.group_bindings)
        for bot_id, config in self.configs.items():
            application = self.dispatchers[bot_id].application
            renderer = self.renderers.get(bot_id)
            presenter = self.presenters[bot_id]

            if config.features.broadcast and config.features.weekly_routine:
                broadcast_time = config.routine_broadcast.time
                self.scheduler.add_job(
                    RoutineBroadcastJob(config, application, sender, renderer, presenter).run,
                    "cron",
                    hour=broadcast_time // 60,
                    minute=broadcast_time % 60,
                    id=f"routine_broadcast_{bot_id}",
                    replace_existing=True,
                )
            if config.features.clock_announce:
                self.scheduler.add_job(
                    ClockAnnounceJob(config, sender).run,
                    "cron",
                    hour=config.silent_start // 60,
                    minute=config.silent_start % 60,
                    id=f"clock_announce_{bot_id}",
                    replace_existing=True,
                )
            if config.features.silent_end_report:
                self.scheduler.add_job(
                    SilentEndReportJob(config, application, sender, renderer, presenter).run,
                    "cron",
                    hour=config.silent_end // 60,
                    minute=config.silent_end % 60,
                    id=f"silent_end_report_{bot_id}",
                    replace_existing=True,
                )

        # 预渲染预热：业务日边界后（22:05）与主题切换时刻（07:00 / 19:00）
        # 主动作废旧缓存并预渲染，保证切换后第一次查询即命中。
        for hour, minute, label in ((22, 5, "business_day"), (7, 0, "theme"), (19, 0, "theme")):
            self.scheduler.add_job(
                self._prewarm_cache,
                "cron",
                hour=hour,
                minute=minute,
                id=f"image_cache_prewarm_{label}",
                replace_existing=True,
            )

    async def _prewarm_cache(self) -> None:
        """到点后为全部站点作废并预渲染默认范围（业务日切换 / 主题切换）。"""
        for bot_id in self.configs:
            try:
                self.image_cache.invalidate(bot_id)
                self.pre_render.schedule(bot_id)
            except Exception:
                logger.exception("预渲染预热失败 bot_id=%s", bot_id)

    async def _send(self, message: GroupMessage, content: str) -> None:
        await message._api.post_group_message(
            group_openid=message.group_openid,
            msg_type=0,
            msg_id=message.id,
            content=content,
        )

    def _site_context(self, bot_id: str) -> Any:
        """预渲染上下文：站点 config + application（ImageCache 需要）。"""
        return SimpleNamespace(
            config=self.configs[bot_id],
            application=self.dispatchers[bot_id].application,
        )

    async def _render_for_cache(self, bot_id: str, result: OperationResult, theme: str) -> bytes | None:
        """预渲染任务用的渲染函数：串行化（弱 CPU 避免并发抢资源），失败返回 None。"""
        renderer = self.renderers.get(bot_id)
        if renderer is None or not renderer.available:
            return None
        async with self._render_lock:
            try:
                return await renderer.render(result)
            except Exception:
                logger.exception("预渲染渲染失败 bot_id=%s", bot_id)
                return None

    async def _send_result(
        self,
        message: GroupMessage,
        bot_id: str,
        result: OperationResult,
        request_id: str,
    ) -> None:
        renderer = self.renderers.get(bot_id)
        if (
            result.code in QUERY_CODES
            and renderer is not None
            and renderer.available
        ):
            try:
                image = await self._image_for_result(bot_id, result)
                if image is not None:
                    media = await QQMediaUploader(message._api).upload_image(
                        message.group_openid,
                        image,
                        file_name=f"schedule-{request_id}.png",
                    )
                    await message._api.post_group_message(
                        group_openid=message.group_openid,
                        msg_type=7,
                        msg_id=message.id,
                        media=media,
                    )
                    return
            except Exception:
                logger.exception(
                    "查询图片发送失败，回退文字 request_id=%s bot_id=%s",
                    request_id,
                    bot_id,
                )
        await self._send(message, self.presenters[bot_id].render(result))

    async def _image_for_result(self, bot_id: str, result: OperationResult) -> bytes | None:
        """查询图片来源：优先缓存（命中+revision 匹配），其次等待在途渲染，最后实时渲染回填。"""
        mode = "schedule" if result.code == "schedule_range" else "free"
        date_range = result.data["date_range"]
        room_ids = tuple(result.data["room_ids"])
        theme = current_theme()

        # ① 缓存命中且 revision 匹配
        entry = self.image_cache.get(bot_id, mode, date_range, room_ids, theme)
        if entry is not None:
            self.image_cache.record_hit(bot_id, "cache")
            return entry.png

        # ② 缓存未命中但该站点正在渲染当前 revision → 等待（3s 超时）
        revision = self.image_cache.revision(bot_id)
        rendering = self.image_cache.rendering_for(bot_id, revision)
        if rendering is not None:
            try:
                await asyncio.wait_for(asyncio.shield(rendering.task), timeout=self.pre_render.wait_timeout)
            except (TimeoutError, asyncio.CancelledError):
                pass
            entry = self.image_cache.get(bot_id, mode, date_range, room_ids, theme)
            if entry is not None:
                self.image_cache.record_hit(bot_id, "wait")
                return entry.png

        # ③ 实时渲染（现状兜底），成功后回填缓存（下次同范围命中）
        renderer = self.renderers.get(bot_id)
        if renderer is None or not renderer.available:
            return None
        image = await renderer.render(result)
        self.image_cache.put(bot_id, mode, date_range, room_ids, theme, image)
        self.image_cache.record_hit(bot_id, "render")
        return image

    async def _handle_bind_config(
        self,
        message: GroupMessage,
        intent: ParsedIntent,
        identity: ExternalIdentity,
    ) -> None:
        bot_id = str(intent.arguments["bot_id"])
        config = self.configs.get(bot_id)
        if config is None:
            await self._send(message, "❌ 配置不存在。可用配置：" + " / ".join(self.configs))
            return
        repository = self.repositories[bot_id]
        user = repository.user_by_external(identity)
        if user is None or repository.role_of(user.id) != config.highest_role:
            await self._send(message, "⛔ 只有该站点的群主可以绑定配置。")
            return
        self.group_bindings.set(message.group_openid, bot_id)
        await self._send(message, f"✅ 本群已绑定到【{config.bot_name}】（{bot_id}）。")

    async def on_group_at_message_create(self, message: GroupMessage) -> None:
        raw_text = message.content.strip()
        if not raw_text:
            return
        identity = ExternalIdentity("qq", message.author.member_openid)

        try:
            intent = self.parser.parse(raw_text)
        except AppError as exc:
            bot_id = self.group_bindings.get(message.group_openid)
            if bot_id is None:
                return
            config = self.configs[bot_id]
            now = datetime.now(SHANGHAI_TZ)
            minute = now.hour * 60 + now.minute
            normalized = raw_text.lstrip("/").lstrip()
            if not normalized.startswith("#") and config.is_silent(minute):
                return
            # Phase 1：解析失败的普通输入进入 pending（夜间 LLM 标注的数据源）。
            # 收集逻辑（脱敏、复合指令过滤）在 qqbot/nlu/annotate.py 的 write_pending 内封装。
            if not normalized.startswith("#"):
                write_pending(self._nlu_dir, normalized, bot_id)
            result = OperationResult.failure(exc.code, **exc.details)
            await self._send(message, self.presenters[bot_id].render(result))
            return

        if intent.operation == "bind_config":
            await self._handle_bind_config(message, intent, identity)
            return

        bot_id = self.group_bindings.get(message.group_openid)
        if bot_id is None or bot_id not in self.configs:
            return
        config = self.configs[bot_id]
        repository = self.repositories[bot_id]
        now = datetime.now(SHANGHAI_TZ)
        user = repository.user_by_external(identity)

        # 管理员手动刷新图片：作废缓存 + 立即预渲染（应急/纠错用）
        if intent.operation == "refresh_images":
            if user is None or repository.role_of(user.id) < config.role_levels.get("admin", 2):
                await self._send(message, "⛔ 只有管理员可以刷新图片缓存。")
                return
            self.image_cache.invalidate(bot_id)
            self.pre_render.schedule(bot_id)
            await self._send(message, f"🔄 正在刷新【{config.bot_name}】图片缓存…")
            return

        local_minute = now.hour * 60 + now.minute
        if not intent.admin and config.is_silent(local_minute) and intent.operation != "create_reservation":
            return

        context = RequestContext(
            request_id=str(uuid4()),
            source="qq",
            site_id=config.site_id,
            identity=identity,
            actor_user_id=user.id if user else None,
            received_at=now,
        )

        try:
            actor_role = repository.role_of(user.id) if user else "user"
            command = self.resolver.resolve(intent, config, now, actor_role=actor_role)
            result = self.dispatchers[bot_id].dispatch(context, command)
        except AppError as exc:
            result = OperationResult.failure(exc.code, **exc.details)
        except Exception:
            logger.exception(
                "处理指令失败 request_id=%s bot_id=%s operation=%s",
                context.request_id,
                bot_id,
                intent.operation,
            )
            result = OperationResult.failure("internal_error", request_id=context.request_id)

        # 写命令成功（改了数据）→ 作废旧缓存 + 去抖预渲染默认范围。
        # 部分成功（reservation_partially_created）同样触发；无变化码不触发。
        if result.ok and result.code in MUTATING_CODES:
            self.image_cache.invalidate(bot_id)
            self.pre_render.schedule(bot_id)

        logger.info(
            "request_id=%s bot_id=%s user_id=%s operation=%s result=%s",
            context.request_id,
            bot_id,
            context.actor_user_id,
            intent.operation,
            result.code,
        )
        # Phase 1 数据收集：脱敏后的样本行（docs/NLU-DESIGN.md 5.2），供 scripts/collect_samples.py 汇总。
        logger.debug(
            "nlu_sample text=%s operation=%s result=%s",
            mask_sensitive(raw_text),
            intent.operation,
            result.code,
        )

        # 抢单静默期仍执行预约，但不在群内刷屏。
        if not intent.admin and config.is_silent(local_minute) and intent.operation == "create_reservation":
            return
        # NLU 降级提示：查询被降级（多房间/多日期无法理解）时先单独发一条提醒，
        # 再发送正常结果（图片或文字），保证用户知道「没完全听懂」。
        if intent.hint and result.ok:
            await self._send(message, intent.hint)
        await self._send_result(message, bot_id, result, context.request_id)


def run_bot(project_root: str | Path) -> None:
    root = Path(project_root).resolve()
    configs = load_all_configs(root / "configs", project_root=root)
    first = next(iter(configs.values()))
    if not first.appid or not first.secret:
        raise RuntimeError("缺少 QQ Bot 凭证，请设置 QQBOT_APPID 与 QQBOT_SECRET 环境变量")
    credential_pairs = {(item.appid, item.secret) for item in configs.values()}
    if len(credential_pairs) != 1:
        raise RuntimeError("单进程多配置必须使用同一组 QQ Bot 凭证")
    intents = botpy.Intents.default()
    client = PianoBotClient(configs, root / "data" / "control.db", intents=intents)
    client.run(appid=first.appid, secret=first.secret)
