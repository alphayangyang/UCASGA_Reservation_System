from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
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
from qqbot.interfaces.qq.media_uploader import QQMediaUploader
from qqbot.interfaces.qq.parser import ParsedIntent, QQCommandParser
from qqbot.interfaces.qq.presenter import QQPresenter
from qqbot.nlu import NLU_DATA_DIR, NLUIntentMatcher, mask_sensitive, run_nightly_job, write_pending
from qqbot.presentation.timeline import ScheduleImageRenderer

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

    async def _send(self, message: GroupMessage, content: str) -> None:
        await message._api.post_group_message(
            group_openid=message.group_openid,
            msg_type=0,
            msg_id=message.id,
            content=content,
        )

    async def _send_result(
        self,
        message: GroupMessage,
        bot_id: str,
        result: OperationResult,
        request_id: str,
    ) -> None:
        renderer = self.renderers.get(bot_id)
        if (
            result.code in {"schedule_range", "free_slots_range"}
            and renderer is not None
            and renderer.available
        ):
            try:
                image = await renderer.render(result)
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
