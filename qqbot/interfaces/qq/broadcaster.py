"""定时主动播报：一条推送通道 + 三个相互独立的 Job。

分层（彼此解耦，可单独测试/替换）：

- ``ProactiveSender`` —— 主动推送通道。只负责「发给谁、怎么发」：
  从群绑定表查出站点绑定的全部群，逐群调用 QQ 主动消息接口
  （不带 msg_id，即主动消息）。与播报内容完全无关；
- ``RoutineBroadcastJob`` —— 每天按 ``booking.routine_broadcast`` 配置的时刻，
  播报从明天起 n 天（days，默认 1）的周常占用，图片优先；
- ``ClockAnnounceJob`` —— 静默期开始时刻（silent_period.start）文字报时
  （以系统时间为准，解决抢琴房时间争议）；
- ``SilentEndReportJob`` —— 站点静默期结束后播报次日预约情况，图片优先。

三个 Job 之间零共享状态，各自注入依赖（config / application / sender / renderer /
presenter）。日期统一取「自然日次日」：21:00 播报时与业务日次日一致（尚未跨
22:00 业务日边界）；22:15 播报时业务日已是次日，「自然日次日」即今晚 22:00
抢到的琴房所在日。

查询结果统一经 ``BookingApplication.daily_schedule / routine_schedule`` 构造
（手册：Application 是业务规则的唯一入口），Job 不直接触碰 Repository。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from qqbot.application.service import BookingApplication
from qqbot.domain.calendar import SHANGHAI_TZ
from qqbot.domain.models import OperationResult
from qqbot.infrastructure.config import SiteConfig
from qqbot.infrastructure.group_bindings import GroupBindingStore
from qqbot.interfaces.qq.media_uploader import QQMediaUploader
from qqbot.presentation.timeline import ScheduleImageRenderer

logger = logging.getLogger(__name__)

UploaderFactory = Callable[[Any], QQMediaUploader]


class ProactiveSender:
    """系统主动推送通道：把内容推送到站点绑定的所有群。

    单群失败只记录日志，不中断其余群；返回成功发送的群数。
    """

    def __init__(
        self,
        api: Any,
        bindings: GroupBindingStore,
        uploader_factory: UploaderFactory | None = None,
    ) -> None:
        self.api = api
        self.bindings = bindings
        self._uploader_factory = uploader_factory or (lambda api: QQMediaUploader(api))

    def groups_for(self, bot_id: str) -> list[str]:
        return self.bindings.groups_for(bot_id)

    async def send_text(self, bot_id: str, content: str) -> int:
        sent = 0
        for group_id in self.groups_for(bot_id):
            try:
                await self.api.post_group_message(group_openid=group_id, msg_type=0, content=content)
                sent += 1
            except Exception:
                logger.exception("主动文字播报失败 bot_id=%s group_id=%s", bot_id, group_id)
        return sent

    async def send_image(self, bot_id: str, content: bytes, file_name: str = "broadcast.png") -> int:
        sent = 0
        for group_id in self.groups_for(bot_id):
            try:
                uploader = self._uploader_factory(self.api)
                media = await uploader.upload_image(group_id, content, file_name=file_name)
                await self.api.post_group_message(
                    group_openid=group_id,
                    msg_type=7,
                    media=media,
                )
                sent += 1
            except Exception:
                logger.exception("主动图片播报失败 bot_id=%s group_id=%s", bot_id, group_id)
        return sent

    async def send_schedule_image(
        self,
        bot_id: str,
        result: OperationResult,
        renderer: ScheduleImageRenderer | None,
        presenter: Any,
    ) -> int:
        """定时查询播报：图片优先；渲染器不可用或渲染失败时回退同一结果的文字呈示。

        手册要求：图片链路任何一步失败都必须保留文字回退。
        """

        if renderer is not None and renderer.available:
            try:
                content = await renderer.render(result)
                return await self.send_image(bot_id, content)
            except Exception:
                logger.exception("定时播报图片渲染失败 bot_id=%s，回退文字", bot_id)
        return await self.send_text(bot_id, presenter.render(result))


class RoutineBroadcastJob:
    """每天按配置时刻播报未来 n 天（从明天起）的周常占用（图片优先、文字回退）。

    时刻与天数来自 ``booking.routine_broadcast``（time / days），
    开关为 features.broadcast 且 features.weekly_routine。
    """

    def __init__(
        self,
        config: SiteConfig,
        application: BookingApplication,
        sender: ProactiveSender,
        renderer: ScheduleImageRenderer | None,
        presenter: Any,
    ) -> None:
        self.config = config
        self.application = application
        self.sender = sender
        self.renderer = renderer
        self.presenter = presenter

    def target_start(self, now: datetime) -> date:
        # 自然日次日（播报发生在抢琴房高峰之前，次日即首个业务日次日）。
        return (now + timedelta(days=1)).date()

    async def run(self, now: datetime | None = None) -> int:
        now = now or datetime.now(SHANGHAI_TZ)
        start = self.target_start(now)
        days = self.config.routine_broadcast.days
        result = self.application.routine_schedule(start, days)
        return await self.sender.send_schedule_image(
            self.config.bot_id, result, self.renderer, self.presenter
        )


class ClockAnnounceJob:
    """静默期开始时刻（silent_period.start）文字报时。

    以系统时间为准，解决「过没过 10 点」的抢琴房时间争议。
    触发时刻跟随配置的静默开始时间，文案里的时刻同样来自配置。
    """

    def __init__(self, config: SiteConfig, sender: ProactiveSender) -> None:
        self.config = config
        self.sender = sender

    def message(self, now: datetime) -> str:
        start = f"{self.config.silent_start // 60:02d}:{self.config.silent_start % 60:02d}"
        return (
            f"🕙【对时】当前系统时间 {now.strftime('%H:%M:%S')}，"
            f"已到 {start}，可以开始抢明天的琴房啦（以系统时间为准）。"
        )

    async def run(self, now: datetime | None = None) -> int:
        now = now or datetime.now(SHANGHAI_TZ)
        return await self.sender.send_text(self.config.bot_id, self.message(now))


class SilentEndReportJob:
    """静默期结束后播报次日预约情况（图片优先、文字回退）。

    挂载时刻 = 站点配置的 silent_period.end（跨午夜窗口同样支持）。
    """

    def __init__(
        self,
        config: SiteConfig,
        application: BookingApplication,
        sender: ProactiveSender,
        renderer: ScheduleImageRenderer | None,
        presenter: Any,
    ) -> None:
        self.config = config
        self.application = application
        self.sender = sender
        self.renderer = renderer
        self.presenter = presenter

    def target_date(self, now: datetime) -> date:
        # 自然日次日：静默期结束时业务日已是次日，次日即今晚 22:00 抢到的琴房日。
        return (now + timedelta(days=1)).date()

    async def run(self, now: datetime | None = None) -> int:
        now = now or datetime.now(SHANGHAI_TZ)
        result = self.application.daily_schedule(self.target_date(now))
        return await self.sender.send_schedule_image(
            self.config.bot_id, result, self.renderer, self.presenter
        )
