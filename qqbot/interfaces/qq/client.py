from __future__ import annotations

import logging
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
from qqbot.presentation.timeline import ScheduleImageRenderer

logger = logging.getLogger(__name__)


class PianoBotClient(botpy.Client):
    def __init__(
        self,
        configs: dict[str, SiteConfig],
        control_db: Path,
        intents: botpy.Intents | None = None,
        renderers: dict[str, ScheduleImageRenderer] | None = None,
    ) -> None:
        super().__init__(intents=intents or botpy.Intents.default())
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
        self.parser = QQCommandParser()
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

        # 抢单静默期仍执行预约，但不在群内刷屏。
        if not intent.admin and config.is_silent(local_minute) and intent.operation == "create_reservation":
            return
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
