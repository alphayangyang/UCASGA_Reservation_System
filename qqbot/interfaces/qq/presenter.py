"""回执文案呈示（中文 / English）。

语言在**构造时绑定**：``QQPresenter(config, lang="en")``。client 按请求用户的
绑定姓名选择实例，因此 ``render(result)`` 签名与既有调用点全部保持不变，
旧的 ``QQPresenter(config)`` 默认中文、输出逐字节不变。

语言判定规则见 ``qqbot.domain.names.preferred_language``：绑定姓名不含 CJK
字符即视为英文用户（留学生）。未绑定的用户回落中文。
"""

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
from qqbot.domain.names import short_display_name
from qqbot.infrastructure.config import SiteConfig

ZH = "zh"
EN = "en"

# 星期：中文按 weekday() 索引取字（周一=0），英文取词
WEEKDAY_CHARS = "一二三四五六日"
WEEKDAY_WORDS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

ROLE_NAMES: dict[str, dict[str, str]] = {
    ZH: {"user": "普通用户", "band": "乐队负责人", "admin": "管理员", "owner": "群主"},
    EN: {"user": "user", "band": "band leader", "admin": "admin", "owner": "owner"},
}

# 文案表：key → {语言: 模板}。两语言必须同时存在（测试覆盖）。
PHRASES: dict[str, dict[str, str]] = {
    # —— 绑定 ——
    "bound": {
        ZH: "✅ 绑定成功：{name}（{student_id}）。",
        EN: "✅ Bound successfully: {name} ({student_id}).",
    },
    # —— 预约 ——
    "reservation_created": {ZH: "✅ 预约成功！", EN: "✅ Booked!"},
    "reservation_partial": {
        ZH: "⚠️ 申请时段部分被占用，已预约可用部分。",
        EN: "⚠️ Part of the requested time was taken; the available part is booked.",
    },
    "label_date": {ZH: "日期：{date}", EN: "Date: {date}"},
    "label_room": {ZH: "琴房：{room}", EN: "Room: {room}"},
    "label_booked": {ZH: "成功时段：{slots}", EN: "Booked: {slots}"},
    "label_requested": {ZH: "申请：{slots}", EN: "Requested: {slots}"},
    "reservation_unavailable": {
        ZH: "❌ 预约失败：申请时段不可用或已被占用。",
        EN: "❌ Booking failed: the requested time is unavailable or already taken.",
    },
    "all_cancelled": {
        ZH: "✅ 已取消未来全部预约（{count} 条）：",
        EN: "✅ Cancelled all upcoming reservations ({count}):",
    },
    "cancelled_on": {ZH: "✅ 已取消 {date} 的预约：", EN: "✅ Cancelled reservations on {date}:"},
    "nothing_to_cancel": {
        ZH: "⚠️ {date} 没有匹配的预约。",
        EN: "⚠️ No matching reservation on {date}.",
    },
    "slot_owner": {ZH: "（原预约人：{name}）", EN: " (originally {name})"},
    # —— 查询 ——
    "no_occupancy": {
        ZH: "📅 {date} 当前暂无预约、周常或临时锁定。",
        EN: "📅 No reservations, routines or locks on {date}.",
    },
    "occupancy_on": {ZH: "📅 {date} 占用情况：", EN: "📅 Occupancy on {date}:"},
    "routine_label": {ZH: "🔒周常：{label}", EN: "🔒Routine: {label}"},
    "no_free": {ZH: "已全天占用", EN: "Fully booked all day"},
    "free_on": {ZH: "🟢 {date} 空闲时段：", EN: "🟢 Free slots on {date}:"},
    "no_routines_match": {
        ZH: "📅 当前没有符合条件的周常占用。",
        EN: "📅 No matching routine.",
    },
    "routines_header": {ZH: "📅 周常占用：", EN: "📅 Routine occupancy:"},
    "no_personal": {
        ZH: "📅 您目前没有即将到来的有效预约。",
        EN: "📅 You have no upcoming reservations.",
    },
    "personal_header": {ZH: "👤 您的有效预约：", EN: "👤 Your reservations:"},
    # —— 角色 / 管理 ——
    "role_assigned": {
        ZH: "✅ 已将【{target}】设为【{role}】。",
        EN: "✅ {target} is now {role}.",
    },
    "role_removed": {
        ZH: "✅ 已撤销【{target}】的管理角色。",
        EN: "✅ Removed the admin role from {target}.",
    },
    "owner_transferred": {
        ZH: "👑 已将群主权限转让给【{target}】。",
        EN: "👑 Ownership transferred to {target}.",
    },
    "admin_cancelled": {
        ZH: "⚔️ 已强制释放以下时段：",
        EN: "⚔️ Force-released the following slots:",
    },
    "date_cleared": {
        ZH: "🗑️ 已清空 {date} 的 {count} 条预约。可发送 #撤销清空 恢复。",
        EN: "🗑️ Cleared {count} reservations on {date}. Send #撤销清空 to undo.",
    },
    "cleared_restored": {
        ZH: "↩️ 已恢复 {date} 的 {count} 条预约。",
        EN: "↩️ Restored {count} reservations on {date}.",
    },
    "nothing_to_undo": {ZH: "⚠️ 没有可撤销的清空批次。", EN: "⚠️ Nothing to undo."},
    # —— 周常 ——
    "routine_added": {
        ZH: "✅ 已添加周常：{weekday} {room} {time}（{purpose}）。",
        EN: "✅ Routine added: {weekday} {room} {time} ({purpose}).",
    },
    "routine_removed": {ZH: "✅ 已删除该周常。", EN: "✅ Routine removed."},
    "routine_not_found": {
        ZH: "⚠️ 没有找到完全匹配的周常。",
        EN: "⚠️ No exactly matching routine found.",
    },
    "broadcast_empty": {ZH: "📢 {date} 暂无周常占用。", EN: "📢 No routines on {date}."},
    "broadcast_header": {ZH: "📢 {date} 周常占用：", EN: "📢 Routines on {date}:"},
    # —— 锁定 ——
    "lock_added": {
        ZH: "✅ 已锁定：{date} [{room}] {time}（{label}）。",
        EN: "✅ Locked: {date} [{room}] {time} ({label}).",
    },
    "lock_removed": {
        ZH: "✅ 已解锁：{date} [{room}] {time}。",
        EN: "✅ Unlocked: {date} [{room}] {time}.",
    },
    "lock_not_found": {
        ZH: "⚠️ 没有找到匹配的锁定时段：{date} [{room}] {time}。",
        EN: "⚠️ No matching lock found: {date} [{room}] {time}.",
    },
    "lock_covers_routine": {
        ZH: "⚠️ 覆盖周常：{room} {detail}（{purpose}），当天该时段按锁定计算。",
        EN: "⚠️ Overrides routine: {room} {detail} ({purpose}); that slot counts as locked.",
    },
    "overlap": {ZH: "{full}，重叠 {part}", EN: "{full}, overlapping {part}"},
    # —— 备份 ——
    "users_backed_up": {
        ZH: "✅ 已备份 {count} 条用户身份记录。",
        EN: "✅ Backed up {count} user records.",
    },
    "users_restored": {
        ZH: "✅ 已恢复 {count} 条用户身份记录。",
        EN: "✅ Restored {count} user records.",
    },
    # —— 错误 ——
    "not_registered": {
        ZH: "⚠️ 您尚未实名登记。请发送：/绑定 姓名 学号",
        EN: "⚠️ You are not registered yet. Reply with: /绑定 姓名 学号",
    },
    "permission_denied": {
        ZH: "⛔ 权限不足，无法执行该操作。",
        EN: "⛔ Permission denied for this operation.",
    },
    "advance_denied": {
        ZH: "⛔ 您最多只能预约到 +{maximum}，不能使用 +{requested}。",
        EN: "⛔ You may book at most +{maximum}; +{requested} is not allowed.",
    },
    "invalid_time_range": {
        ZH: "❌ 时间段无效：{reason}。",
        EN: "❌ Invalid time range: {reason}.",
    },
    "check_input": {ZH: "请检查输入", EN: "please check your input"},
    "daily_limit": {
        ZH: "❌ 超出该日期的预约总时长上限。\n当前已预约：{current}\n上限：{maximum}",
        EN: "❌ Daily booking limit exceeded.\nBooked so far: {current}\nLimit: {maximum}",
    },
    "duplicate_name": {
        ZH: "❌ 绑定失败：该姓名已被其他用户绑定。\n请加上姓氏或首字母区分（例：John → John Smith）。",
        EN: (
            "❌ Binding failed: that name is already taken.\n"
            "Add a surname or initial to distinguish (e.g. John → John Smith)."
        ),
    },
    "duplicate_student_id": {
        ZH: "❌ 绑定失败：该学号已被其他用户绑定。",
        EN: "❌ Binding failed: that student ID is already taken.",
    },
    "invalid_name": {
        ZH: "❌ 姓名格式不正确：1～24 个字符，支持中文或英文姓名（可含空格、·、-、'）。",
        EN: ("❌ Invalid name format: 1–24 characters, Chinese or English (spaces, ·, -, ' allowed)."),
    },
    "invalid_student_id": {
        ZH: "❌ 学号格式不正确。请输入本科生或研究生标准学号。",
        EN: "❌ Invalid student ID. Enter your standard UCAS student number.",
    },
    "invalid_student_year": {
        ZH: "❌ 学号年份 {year} 不合理。",
        EN: "❌ The year {year} in your student ID looks wrong.",
    },
    "database_busy": {
        ZH: "⚠️ 当前预约人数较多，数据库正忙，请稍后重试。",
        EN: "⚠️ The database is busy right now — please retry in a moment.",
    },
    "feature_disabled": {
        ZH: "⚠️ 当前站点未开启 {feature} 功能。",
        EN: "⚠️ This site has the {feature} feature disabled.",
    },
    "room_unknown": {
        ZH: "❌ 琴房不存在或未指定。可用琴房：{rooms}",
        EN: "❌ Unknown or missing room. Available rooms: {rooms}",
    },
    "user_unknown": {
        ZH: "⚠️ 找不到用户【{name}】，请确认对方已经绑定。",
        EN: "⚠️ User {name} not found — make sure they have registered.",
    },
    "cancel_contains_other": {
        ZH: "❌ 该范围包含其他人的预约，不能按此范围取消。",
        EN: "❌ That range contains someone else's reservation and cannot be cancelled as a whole.",
    },
    "routine_conflict": {
        ZH: "❌ 周常时段冲突：{label}。",
        EN: "❌ Conflicts with an existing routine: {label}.",
    },
    "no_role": {
        ZH: "⚠️ 该用户当前没有可撤销的管理角色。",
        EN: "⚠️ That user has no admin role to remove.",
    },
    "not_found_generic": {ZH: "⚠️ 没有找到匹配的数据。", EN: "⚠️ No matching data found."},
    "internal_error": {
        ZH: "❌ 系统处理请求时发生异常，请联系管理员并提供操作时间。",
        EN: "❌ Something went wrong. Please contact an admin with the time of this request.",
    },
    # —— 其他 ——
    "date_tag": {ZH: "（+{offset}）", EN: " (+{offset})"},
    "hours": {ZH: "{value:g} 小时", EN: "{value:g} h"},
}


class QQPresenter:
    """按语言渲染 OperationResult；语言在构造时绑定，实例无请求内可变状态。"""

    def __init__(self, config: SiteConfig, lang: str = ZH) -> None:
        self.config = config
        self.lang = EN if lang == EN else ZH
        self._roles = ROLE_NAMES[self.lang]

    def _p(self, key: str, **kwargs: object) -> str:
        text = PHRASES[key][self.lang]
        return text.format(**kwargs) if kwargs else text

    def _weekday(self, index: int) -> str:
        """周一…周日（中文）/ Mon…Sun（英文）。模板里的星期占位符只填这个词形。"""
        if self.lang == EN:
            return WEEKDAY_WORDS[index]
        return f"周{WEEKDAY_CHARS[index]}"

    def _room_name(self, room_id: str) -> str:
        try:
            return self.config.room_by_id(room_id).name
        except Exception:
            return room_id

    def _date(self, value: date, offset: int | None = None) -> str:
        suffix = self._p("date_tag", offset=offset) if offset is not None else ""
        return f"{value.isoformat()}{suffix}"

    def _hours(self, minutes: int) -> str:
        return self._p("hours", value=minutes / 60)

    def _slots(self, values: Iterable[CancelledSlot], include_user: bool = False) -> str:
        lines: list[str] = []
        for item in values:
            line = f"- [{self._room_name(item.room_id)}] {item.time_range.display()}"
            if include_user and item.user_name:
                line += self._p("slot_owner", name=item.user_name)
            lines.append(line)
        return "\n".join(lines)

    def _schedule(self, result: OperationResult) -> str:
        target = self._date(result.data["date"], result.data.get("offset"))
        values: list[Occupancy] = result.data["occupancies"]
        if not values:
            return self._p("no_occupancy", date=target)
        grouped: dict[str, list[str]] = defaultdict(list)
        admin_view = bool(result.data.get("admin_view"))
        for item in values:
            if item.kind == "routine":
                label = self._p("routine_label", label=item.label)
            elif item.kind == "lock":
                label = f"🔒{item.label}"
            else:
                label = item.label if admin_view else short_display_name(item.label)
            grouped[item.room_id].append(f"{item.time_range.display()} {label}")
        blocks: list[str] = []
        for room in self.config.rooms:
            if grouped.get(room.id):
                blocks.append(f"[{room.name}]\n" + "\n".join(grouped[room.id]))
        for room_id, lines in grouped.items():
            if room_id not in {room.id for room in self.config.rooms}:
                blocks.append(f"[{self._room_name(room_id)}]\n" + "\n".join(lines))
        return self._p("occupancy_on", date=target) + "\n" + "\n\n".join(blocks)

    def _free(self, result: OperationResult) -> str:
        target = self._date(result.data["date"], result.data.get("offset"))
        blocks: list[str] = []
        for room_id, slots in result.data["slots"].items():
            value = "、".join(slot.display() for slot in slots) if slots else self._p("no_free")
            blocks.append(f"[{self._room_name(room_id)}] {value}")
        return self._p("free_on", date=target) + "\n" + "\n\n".join(blocks)

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
            return self._p("no_routines_match")
        return (
            self._p("routines_header")
            + "\n"
            + "\n".join(
                f"[{self._weekday(item.weekday)}] {self._room_name(item.room_id)} "
                f"{item.time_range.display()}（{item.purpose}）"
                for item in routines
            )
        )

    def render(self, result: OperationResult) -> str:
        code, data = result.code, result.data

        if code == "user_bound":
            user = data["user"]
            return self._p("bound", name=user.display_name, student_id=user.student_id)
        if code in {"reservation_created", "reservation_partially_created"}:
            title = self._p("reservation_created" if code == "reservation_created" else "reservation_partial")
            lines = [
                title,
                self._p("label_date", date=self._date(data["date"], data["offset"])),
                self._p("label_room", room=data["room_name"]),
                self._p(
                    "label_booked",
                    slots="、".join(item.display() for item in data["fragments"]),
                ),
            ]
            return "\n".join(lines)
        if code == "reservation_unavailable":
            return (
                self._p("reservation_unavailable")
                + "\n"
                + self._p("label_date", date=self._date(data["date"], data["offset"]))
                + "\n"
                + self._p("label_room", room=data["room_name"])
                + "\n"
                + self._p("label_requested", slots=data["requested"].display())
            )
        if code == "all_reservations_cancelled":
            slots = data.get("slots", [])
            lines = [self._p("all_cancelled", count=len(slots))]
            for slot in slots:
                lines.append(f"- [{self._room_name(slot.room_id)}] {slot.time_range.display()}")
            return "\n".join(lines)

        if code == "reservation_cancelled":
            return (
                self._p("cancelled_on", date=self._date(data["date"], data["offset"]))
                + "\n"
                + self._slots(data["slots"])
            )
        if code == "nothing_to_cancel":
            target = data.get("date")
            return self._p("nothing_to_cancel", date=target.isoformat() if target else "")
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
                return self._p("no_personal")
            return (
                self._p("personal_header")
                + "\n"
                + "\n".join(
                    f"[{target.isoformat()}] {self._room_name(room_id)} {slot.display()}"
                    for target, room_id, slot in values
                )
            )
        if code == "role_assigned":
            return self._p(
                "role_assigned",
                target=data["target"],
                role=self._roles.get(data["role"], data["role"]),
            )
        if code == "role_removed":
            return self._p("role_removed", target=data["target"])
        if code == "owner_transferred":
            return self._p("owner_transferred", target=data["target"])
        if code == "admin_cancelled":
            return self._p("admin_cancelled") + "\n" + self._slots(data["slots"], include_user=True)
        if code == "date_cleared":
            return self._p("date_cleared", date=data["date"].isoformat(), count=data["count"])
        if code == "clear_undone":
            if data["count"]:
                return self._p("cleared_restored", date=data["date"].isoformat(), count=data["count"])
            return self._p("nothing_to_undo")
        if code == "routine_added":
            item: Routine = data["routine"]
            return self._p(
                "routine_added",
                weekday=self._weekday(item.weekday),
                room=self._room_name(item.room_id),
                time=item.time_range.display(),
                purpose=item.purpose,
            )
        if code == "routine_removed":
            return self._p("routine_removed")
        if code == "routine_not_found":
            return self._p("routine_not_found")
        if code == "routines":
            return self._routines(data["routines"])
        if code == "routine_broadcast":
            target: date = data["date"]
            routines: list[Routine] = data["routines"]
            if not routines:
                return self._p("broadcast_empty", date=target.isoformat())
            return (
                self._p("broadcast_header", date=target.isoformat())
                + "\n"
                + "\n".join(
                    f"[{self._room_name(item.room_id)}] {item.time_range.display()}（{item.purpose}）"
                    for item in routines
                )
            )
        if code == "lock_added":
            text = self._p(
                "lock_added",
                date=self._date(data["date"]),
                room=data["room_name"],
                time=data["time_range"].display(),
                label=data["label"],
            )
            covered = data.get("covered") or []
            if covered:
                lines = [text, ""]
                for item in covered:
                    detail = (
                        item.original.display()
                        if item.covered == item.original
                        else self._p(
                            "overlap",
                            full=item.original.display(),
                            part=item.covered.display(),
                        )
                    )
                    lines.append(
                        self._p(
                            "lock_covers_routine",
                            room=self._room_name(data["room_name"]),
                            detail=detail,
                            purpose=item.purpose,
                        )
                    )
                return "\n".join(lines)
            return text
        if code == "lock_removed":
            return self._p(
                "lock_removed",
                date=self._date(data["date"]),
                room=data["room_name"],
                time=data["time_range"].display(),
            )
        if code == "lock_not_found":
            return self._p(
                "lock_not_found",
                date=self._date(data["date"]),
                room=data["room_name"],
                time=data["time_range"].display(),
            )
        if code == "users_backed_up":
            return self._p("users_backed_up", count=data["count"])
        if code == "users_restored":
            return self._p("users_restored", count=data["count"])

        return self._render_error(result)

    def _render_error(self, result: OperationResult) -> str:
        code, data = result.code, result.data
        if code == "not_registered":
            return self._p("not_registered")
        if code == "permission_denied":
            return self._p("permission_denied")
        if code == "advance_booking_denied":
            return self._p(
                "advance_denied",
                maximum=data["maximum_offset"],
                requested=data["requested_offset"],
            )
        if code == "invalid_time_range":
            return self._p("invalid_time_range", reason=data.get("reason") or self._p("check_input"))
        if code == "daily_limit_exceeded":
            return self._p(
                "daily_limit",
                current=self._hours(data["current_minutes"]),
                maximum=self._hours(data["maximum_minutes"]),
            )
        if code == "duplicate_identity":
            if data.get("field") == "display_name":
                return self._p("duplicate_name")
            return self._p("duplicate_student_id")
        if code == "invalid_name":
            return self._p("invalid_name")
        if code == "invalid_student_id":
            return self._p("invalid_student_id")
        if code == "invalid_student_year":
            return self._p("invalid_student_year", year=data["year"])
        if code == "database_busy":
            return self._p("database_busy")
        if code == "feature_disabled":
            return self._p("feature_disabled", feature=data.get("feature", "该"))
        if code == "not_found":
            entity, reason = data.get("entity"), data.get("reason")
            if entity == "room":
                return self._p(
                    "room_unknown",
                    rooms=" / ".join(room.name for room in self.config.rooms),
                )
            if entity == "user":
                return self._p("user_unknown", name=data.get("name", ""))
            if entity == "cancellable_slot" and reason == "contains_other_user":
                return self._p("cancel_contains_other")
            if entity == "routine_slot":
                return self._p("routine_conflict", label=data.get("label", ""))
            if entity == "role":
                return self._p("no_role")
            return self._p("not_found_generic")
        if code == "parse_error":
            return self.usage(str(data.get("usage", "help")), data)
        return self._p("internal_error")

    def usage(self, key: str, details: dict | None = None) -> str:
        room = self.config.rooms[0].name
        max_offset = self.config.max_query_offset
        max_days = self.config.query.max_range_days
        boundary = f"{self.config.business_boundary // 60:02d}:{self.config.business_boundary % 60:02d}"
        # 命令本身是中文的（英文命令别名为可选增强），因此英文文案里原样保留指令示例
        usages: dict[str, dict[str, str]] = {
            "bind": {
                ZH: "❌ 格式：/绑定 姓名 学号",
                EN: "❌ Usage: /绑定 NAME STUDENT_ID",
            },
            "reserve": {
                ZH: f"❌ 格式：/预约 {room} 21-22.5 [+0/+1/+2]",
                EN: f"❌ Usage: /预约 {room} 21-22.5 [+0/+1/+2]",
            },
            "cancel": {
                ZH: f"❌ 格式：/取消 +1，或 /取消 {room} 21-22.5 +1",
                EN: f"❌ Usage: /取消 +1, or /取消 {room} 21-22.5 +1",
            },
            "personal": {ZH: "❌ 格式：/查询个人", EN: "❌ Usage: /查询个人"},
            "offset": {
                ZH: f"❌ 日期偏移量只能为 +0 到 +{max_offset}。",
                EN: f"❌ Date offset must be between +0 and +{max_offset}.",
            },
            "query_range": {
                ZH: (
                    "❌ 查询日期格式：+0、+0~+6、YYYY-MM-DD，或 "
                    "YYYY-MM-DD~YYYY-MM-DD；"
                    f"一次最多 {max_days} 天。"
                ),
                EN: (
                    "❌ Query date format: +0, +0~+6, YYYY-MM-DD, or "
                    "YYYY-MM-DD~YYYY-MM-DD; "
                    f"at most {max_days} days at once."
                ),
            },
            "date": {
                ZH: "❌ 日期格式应为 YYYY-MM-DD。",
                EN: "❌ Date format should be YYYY-MM-DD.",
            },
            "deprecated_offset": {
                ZH: "ℹ️ 旧超前指令已合并，请改用 /预约、/查询、/取消 后接 +1 或 +2。",
                EN: "ℹ️ The old advance commands were merged; use /预约, /查询, /取消 with +1 or +2.",
            },
            "bind_config": {
                ZH: "❌ 格式：#绑定配置 yqh",
                EN: "❌ Usage: #绑定配置 yqh",
            },
            "admin_query": {
                ZH: "❌ 格式：#查询 YYYY-MM-DD [琴房]",
                EN: "❌ Usage: #查询 YYYY-MM-DD [ROOM]",
            },
            "admin_cancel": {
                ZH: f"❌ 格式：#取消 {room} 21-22.5 [+1 或 YYYY-MM-DD]",
                EN: f"❌ Usage: #取消 {room} 21-22.5 [+1 or YYYY-MM-DD]",
            },
            "routine": {
                ZH: f"❌ 格式：#添加周常 周一 {room} 21-22.5 用途",
                EN: f"❌ Usage: #添加周常 周一 {room} 21-22.5 PURPOSE",
            },
            "routine_query": {
                ZH: "❌ 格式：#查询周常 [周一]",
                EN: "❌ Usage: #查询周常 [周一]",
            },
            "lock": {
                ZH: f"❌ 格式：#锁定 {room} 21-22.5 [+1 或 YYYY-MM-DD] 用途",
                EN: f"❌ Usage: #锁定 {room} 21-22.5 [+1 or YYYY-MM-DD] PURPOSE",
            },
            "unlock": {
                ZH: f"❌ 格式：#解锁 {room} 21-22.5 [+1 或 YYYY-MM-DD]",
                EN: f"❌ Usage: #解锁 {room} 21-22.5 [+1 or YYYY-MM-DD]",
            },
            "role": {
                ZH: "❌ 格式：#添加管理 姓名 [角色] / #删除管理 姓名 / #转让群主 姓名",
                EN: "❌ Usage: #添加管理 NAME [ROLE] / #删除管理 NAME / #转让群主 NAME",
            },
            "clear": {
                ZH: "❌ 格式：#清空预约 [+1 或 YYYY-MM-DD]",
                EN: "❌ Usage: #清空预约 [+1 or YYYY-MM-DD]",
            },
            "admin_help": {
                ZH: (
                    "管理员指令：#添加管理、#删除管理、#取消、#查询、#清空预约、"
                    "#添加周常、#查询周常、#锁定、#解锁。"
                ),
                EN: (
                    "Admin commands: #添加管理, #删除管理, #取消, #查询, #清空预约, "
                    "#添加周常, #查询周常, #锁定, #解锁."
                ),
            },
            "chitchat": {
                ZH: "再玩小泉要坏啦QwQ 💦",
                EN: "Careful, you'll wear me out! QwQ 💦",
            },
            "compound": {
                ZH: "❌ 小泉还不支持这样的指令哦 (｡•́︿•̀｡)\n一次只说一件事就好啦～",
                EN: "❌ I can't handle that yet (｡•́︿•̀｡)\nPlease say one thing at a time.",
            },
            "past_date": {
                ZH: "❌ 这个日期已经过去啦，试试「明天」或者 +0/+1 吧～",
                EN: "❌ That date is in the past — try +0 or +1 instead.",
            },
            "natural_past": {
                ZH: (f"⏰ 现在已经过了 {boundary}，「今天」的时段已经结束啦～试试「明天」吧"),
                EN: f"⏰ It is past {boundary}, so today's slots are over — try +1 (tomorrow).",
            },
            "other_person": {
                ZH: ("❌ 小泉不能帮你操作别人的预约哦 (｡•́︿•̀｡)\n只能取消/预约自己的预约～"),
                EN: (
                    "❌ I can only manage your own reservations (｡•́︿•̀｡)\n"
                    "You can book or cancel for yourself only."
                ),
            },
            "nlu_unrecognized": {
                ZH: (
                    "对不起，小泉现在还不能听懂哦 (´･_･`)\n"
                    "试试对我说「预约 303 7-8」或「帮我看看303有没有空」吧～"
                ),
                EN: ("Sorry, I didn't understand that (´･_･`)\nTry the commands below, e.g. /预约 303 7-8"),
            },
            "help": {
                ZH: (
                    "🤖 琴房助手指令：\n"
                    f"/预约 {room} 21-22.5 [+0/+1/+2]\n"
                    f"/取消 [+1] 或 /取消 {room} 21-22.5 [+1]\n"
                    f"/查询 [{room}] [+1 或 +0~+6]\n"
                    f"/空闲 [{room}] [+1 或 +0~+6]\n"
                    "也可使用绝对日期：2026-08-10~2026-08-16\n"
                    "/查询个人\n"
                    "/绑定 姓名 学号"
                ),
                EN: (
                    "🤖 Piano room assistant commands:\n"
                    f"/预约 {room} 21-22.5 [+0/+1/+2]  — book\n"
                    f"/取消 [+1] or /取消 {room} 21-22.5 [+1]  — cancel\n"
                    f"/查询 [{room}] [+1 or +0~+6]  — occupancy\n"
                    f"/空闲 [{room}] [+1 or +0~+6]  — free slots\n"
                    "Absolute dates also work: 2026-08-10~2026-08-16\n"
                    "/查询个人  — my reservations\n"
                    "/绑定 NAME STUDENT_ID  — register"
                ),
            },
        }
        table = usages.get(key) or usages["help"]
        return table[self.lang]
