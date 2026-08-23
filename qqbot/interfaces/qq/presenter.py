from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date

from qqbot.domain.models import (
    CancelledSlot,
    Occupancy,
    OperationResult,
    Routine,
)
from qqbot.infrastructure.config import SiteConfig

WEEKDAY_NAMES = "一二三四五六日"
ROLE_NAMES = {"user": "普通用户", "band": "乐队负责人", "admin": "管理员", "owner": "群主"}


def _hours(minutes: int) -> str:
    return f"{minutes / 60:g} 小时"


class QQPresenter:
    def __init__(self, config: SiteConfig) -> None:
        self.config = config

    def _room_name(self, room_id: str) -> str:
        try:
            return self.config.room_by_id(room_id).name
        except Exception:
            return room_id

    @staticmethod
    def _date(value: date, offset: int | None = None) -> str:
        suffix = f"（+{offset}）" if offset is not None else ""
        return f"{value.isoformat()}{suffix}"

    def _slots(self, values: Iterable[CancelledSlot], include_user: bool = False) -> str:
        lines: list[str] = []
        for item in values:
            line = f"- [{self._room_name(item.room_id)}] {item.time_range.display()}"
            if include_user and item.user_name:
                line += f"（原预约人：{item.user_name}）"
            lines.append(line)
        return "\n".join(lines)

    def _schedule(self, result: OperationResult) -> str:
        target = self._date(result.data["date"], result.data.get("offset"))
        values: list[Occupancy] = result.data["occupancies"]
        if not values:
            return f"📅 {target} 当前暂无预约、周常或临时锁定。"
        grouped: dict[str, list[str]] = defaultdict(list)
        admin_view = bool(result.data.get("admin_view"))
        for item in values:
            if item.kind == "routine":
                label = f"🔒周常：{item.label}"
            elif item.kind == "lock":
                label = f"🔒{item.label}"
            else:
                label = item.label if admin_view else item.label[-4:]
            grouped[item.room_id].append(f"{item.time_range.display()} {label}")
        blocks: list[str] = []
        for room in self.config.rooms:
            if grouped.get(room.id):
                blocks.append(f"[{room.name}]\n" + "\n".join(grouped[room.id]))
        for room_id, lines in grouped.items():
            if room_id not in {room.id for room in self.config.rooms}:
                blocks.append(f"[{self._room_name(room_id)}]\n" + "\n".join(lines))
        return f"📅 {target} 占用情况：\n" + "\n\n".join(blocks)

    def _free(self, result: OperationResult) -> str:
        target = self._date(result.data["date"], result.data.get("offset"))
        blocks: list[str] = []
        for room_id, slots in result.data["slots"].items():
            value = "、".join(slot.display() for slot in slots) if slots else "已全天占用"
            blocks.append(f"[{self._room_name(room_id)}] {value}")
        return f"🟢 {target} 空闲时段：\n" + "\n\n".join(blocks)

    def _schedule_range(self, result: OperationResult) -> str:
        return "\n\n".join(
            self._schedule(OperationResult.success("schedule", **day)) for day in result.data["days"]
        )

    def _free_range(self, result: OperationResult) -> str:
        return "\n\n".join(
            self._free(OperationResult.success("free_slots", **day)) for day in result.data["days"]
        )

    def _routines(self, routines: list[Routine]) -> str:
        if not routines:
            return "📅 当前没有符合条件的周常占用。"
        return "📅 周常占用：\n" + "\n".join(
            f"[周{WEEKDAY_NAMES[item.weekday]}] {self._room_name(item.room_id)} "
            f"{item.time_range.display()}（{item.purpose}）"
            for item in routines
        )

    def render(self, result: OperationResult) -> str:
        code, data = result.code, result.data

        if code == "user_bound":
            user = data["user"]
            return f"✅ 绑定成功：{user.display_name}（{user.student_id}）。"
        if code in {"reservation_created", "reservation_partially_created"}:
            title = (
                "✅ 预约成功！" if code == "reservation_created" else "⚠️ 申请时段部分被占用，已预约可用部分。"
            )
            lines = [
                title,
                f"日期：{self._date(data['date'], data['offset'])}",
                f"琴房：{data['room_name']}",
                "成功时段：" + "、".join(item.display() for item in data["fragments"]),
            ]
            return "\n".join(lines)
        if code == "reservation_unavailable":
            return (
                "❌ 预约失败：申请时段不可用或已被占用。\n"
                f"日期：{self._date(data['date'], data['offset'])}\n"
                f"琴房：{data['room_name']}\n申请：{data['requested'].display()}"
            )
        if code == "all_reservations_cancelled":
            slots = data.get("slots", [])
            lines = [f"✅ 已取消未来全部预约（{len(slots)} 条）："]
            for slot in slots:
                lines.append(f"- [{self._room_name(slot.room_id)}] {slot.time_range.display()}")
            return "\n".join(lines)

        if code == "reservation_cancelled":
            return f"✅ 已取消 {self._date(data['date'], data['offset'])} 的预约：\n" + self._slots(
                data["slots"]
            )
        if code == "nothing_to_cancel":
            target = data.get("date")
            return f"⚠️ {target.isoformat() if target else ''} 没有匹配的预约。"
        if code == "schedule":
            return self._schedule(result)
        if code == "schedule_range":
            return self._schedule_range(result)
        if code == "free_slots":
            return self._free(result)
        if code == "free_slots_range":
            return self._free_range(result)
        if code == "personal_schedule":
            values = data["reservations"]
            if not values:
                return "📅 您目前没有即将到来的有效预约。"
            return "👤 您的有效预约：\n" + "\n".join(
                f"[{target.isoformat()}] {self._room_name(room_id)} {slot.display()}"
                for target, room_id, slot in values
            )
        if code == "role_assigned":
            return f"✅ 已将【{data['target']}】设为【{ROLE_NAMES.get(data['role'], data['role'])}】。"
        if code == "role_removed":
            return f"✅ 已撤销【{data['target']}】的管理角色。"
        if code == "owner_transferred":
            return f"👑 已将群主权限转让给【{data['target']}】。"
        if code == "admin_cancelled":
            return "⚔️ 已强制释放以下时段：\n" + self._slots(data["slots"], include_user=True)
        if code == "date_cleared":
            return f"🗑️ 已清空 {data['date'].isoformat()} 的 {data['count']} 条预约。可发送 #撤销清空 恢复。"
        if code == "clear_undone":
            if data["count"]:
                return f"↩️ 已恢复 {data['date'].isoformat()} 的 {data['count']} 条预约。"
            return "⚠️ 没有可撤销的清空批次。"
        if code == "routine_added":
            item: Routine = data["routine"]
            return (
                f"✅ 已添加周常：周{WEEKDAY_NAMES[item.weekday]} "
                f"{self._room_name(item.room_id)} {item.time_range.display()}（{item.purpose}）。"
            )
        if code == "routine_removed":
            return "✅ 已删除该周常。"
        if code == "routine_not_found":
            return "⚠️ 没有找到完全匹配的周常。"
        if code == "routines":
            return self._routines(data["routines"])
        if code == "routine_broadcast":
            target: date = data["date"]
            routines: list[Routine] = data["routines"]
            if not routines:
                return f"📢 {target.isoformat()} 暂无周常占用。"
            return f"📢 {target.isoformat()} 周常占用：\n" + "\n".join(
                f"[{self._room_name(item.room_id)}] {item.time_range.display()}（{item.purpose}）"
                for item in routines
            )
        if code == "users_backed_up":
            return f"✅ 已备份 {data['count']} 条用户身份记录。"
        if code == "users_restored":
            return f"✅ 已恢复 {data['count']} 条用户身份记录。"

        return self._render_error(result)

    def _render_error(self, result: OperationResult) -> str:
        code, data = result.code, result.data
        if code == "not_registered":
            return "⚠️ 您尚未实名登记。请发送：/绑定 姓名 学号"
        if code == "permission_denied":
            return "⛔ 权限不足，无法执行该操作。"
        if code == "advance_booking_denied":
            return f"⛔ 您最多只能预约到 +{data['maximum_offset']}，不能使用 +{data['requested_offset']}。"
        if code == "invalid_time_range":
            return f"❌ 时间段无效：{data.get('reason', '请检查输入')}。"
        if code == "daily_limit_exceeded":
            return (
                "❌ 超出该日期的预约总时长上限。\n"
                f"当前已预约：{_hours(data['current_minutes'])}\n"
                f"上限：{_hours(data['maximum_minutes'])}"
            )
        if code == "duplicate_identity":
            label = "姓名" if data.get("field") == "display_name" else "学号"
            return f"❌ 绑定失败：该{label}已被其他用户绑定。"
        if code == "invalid_name":
            return "❌ 姓名须为 1～10 个汉字。"
        if code == "invalid_student_id":
            return "❌ 学号格式不正确。请输入本科生或研究生标准学号。"
        if code == "invalid_student_year":
            return f"❌ 学号年份 {data['year']} 不合理。"
        if code == "database_busy":
            return "⚠️ 当前预约人数较多，数据库正忙，请稍后重试。"
        if code == "feature_disabled":
            return f"⚠️ 当前站点未开启 {data.get('feature', '该')} 功能。"
        if code == "not_found":
            entity, reason = data.get("entity"), data.get("reason")
            if entity == "room":
                return "❌ 琴房不存在或未指定。可用琴房：" + " / ".join(
                    room.name for room in self.config.rooms
                )
            if entity == "user":
                return f"⚠️ 找不到用户【{data.get('name', '')}】，请确认对方已经绑定。"
            if entity == "cancellable_slot" and reason == "contains_other_user":
                return "❌ 该范围包含其他人的预约，不能按此范围取消。"
            if entity == "routine_slot":
                return f"❌ 周常时段冲突：{data.get('label', '')}。"
            if entity == "role":
                return "⚠️ 该用户当前没有可撤销的管理角色。"
            return "⚠️ 没有找到匹配的数据。"
        if code == "parse_error":
            return self.usage(str(data.get("usage", "help")), data)
        return "❌ 系统处理请求时发生异常，请联系管理员并提供操作时间。"

    def usage(self, key: str, details: dict | None = None) -> str:
        room = self.config.rooms[0].name
        usages = {
            "bind": "❌ 格式：/绑定 姓名 学号",
            "reserve": f"❌ 格式：/预约 {room} 21-22.5 [+0/+1/+2]",
            "cancel": f"❌ 格式：/取消 +1，或 /取消 {room} 21-22.5 +1",
            "personal": "❌ 格式：/查询个人",
            "offset": f"❌ 日期偏移量只能为 +0 到 +{self.config.max_query_offset}。",
            "query_range": (
                "❌ 查询日期格式：+0、+0~+6、YYYY-MM-DD，或 "
                "YYYY-MM-DD~YYYY-MM-DD；"
                f"一次最多 {self.config.query.max_range_days} 天。"
            ),
            "date": "❌ 日期格式应为 YYYY-MM-DD。",
            "deprecated_offset": "ℹ️ 旧超前指令已合并，请改用 /预约、/查询、/取消 后接 +1 或 +2。",
            "bind_config": "❌ 格式：#绑定配置 yqh",
            "admin_query": "❌ 格式：#查询 YYYY-MM-DD [琴房]",
            "admin_cancel": f"❌ 格式：#取消 {room} 21-22.5 [+1 或 YYYY-MM-DD]",
            "routine": f"❌ 格式：#添加周常 周一 {room} 21-22.5 用途",
            "routine_query": "❌ 格式：#查询周常 [周一]",
            "role": "❌ 格式：#添加管理 姓名 [角色] / #删除管理 姓名 / #转让群主 姓名",
            "clear": "❌ 格式：#清空预约 [+1 或 YYYY-MM-DD]",
            "admin_help": "管理员指令：#添加管理、#删除管理、#取消、#查询、#清空预约、#添加周常、#查询周常。",
            "chitchat": "再玩小泉要坏啦QwQ 💦",
            "compound": "❌ 小泉还不支持这样的指令哦 (｡•́︿•̀｡)\n一次只说一件事就好啦～",
            "past_date": "❌ 这个日期已经过去啦，试试「明天」或者 +0/+1 吧～",
            "natural_past": (
                "⏰ 现在已经过了 "
                f"{self.config.business_boundary // 60:02d}:{self.config.business_boundary % 60:02d}，"
                "「今天」的时段已经结束啦～试试「明天」吧"
            ),
            "other_person": "❌ 小泉不能帮你操作别人的预约哦 (｡•́︿•̀｡)\n只能取消/预约自己的预约～",
            "nlu_unrecognized": (
                "对不起，小泉现在还不能听懂哦 (´･_･`)\n"
                "试试对我说「预约 303 7-8」或「帮我看看303有没有空」吧～"
            ),
            "help": (
                "🤖 琴房助手指令：\n"
                f"/预约 {room} 21-22.5 [+0/+1/+2]\n"
                f"/取消 [+1] 或 /取消 {room} 21-22.5 [+1]\n"
                f"/查询 [{room}] [+1 或 +0~+6]\n"
                f"/空闲 [{room}] [+1 或 +0~+6]\n"
                "也可使用绝对日期：2026-08-10~2026-08-16\n"
                "/查询个人\n"
                "/绑定 姓名 学号"
            ),
        }
        return usages.get(key, usages["help"])
