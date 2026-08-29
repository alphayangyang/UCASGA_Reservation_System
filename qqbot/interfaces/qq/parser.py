from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from qqbot.domain.errors import ParseError

if TYPE_CHECKING:
    from qqbot.nlu.matcher import NLUIntentMatcher

TIME_TOKEN = r"[0-9:.：]+"
RANGE_RE = re.compile(rf"^(?P<room>.*?)\s*(?P<start>{TIME_TOKEN})\s*[-~～—－]\s*(?P<end>{TIME_TOKEN})\s*$")
QUERY_DATE_TOKEN = r"(?:\+\d+|\d{4}-\d{2}-\d{2})"
QUERY_RANGE_RE = re.compile(
    rf"(?<!\S)(?P<start>{QUERY_DATE_TOKEN})"
    rf"(?:\s*(?:~|～|至|\.\.)\s*(?P<end>{QUERY_DATE_TOKEN}))?\s*$"
)


@dataclass(frozen=True)
class ParsedIntent:
    """Parser 的稳定输出；未来 NLP 也只需产生这个结构。

    hint：可选呈示提示（如 NLU 降级查询时提醒用户），默认为 None——
    不影响协议兼容，Resolver 与 Command 均不消费它（手册：Command 不含呈示文字）。
    """

    operation: str
    arguments: dict[str, Any] = field(default_factory=dict)
    admin: bool = False
    hint: str | None = None


def _strip_prefix(text: str) -> tuple[str, bool]:
    normalized = text.strip()
    if normalized.startswith("/"):
        normalized = normalized[1:].strip()
    admin = normalized.startswith("#")
    if admin:
        normalized = normalized[1:].strip()
    return normalized, admin


def _take_offset(value: str) -> tuple[str, int]:
    match = re.search(r"\s*\+(\d+)\s*$", value)
    if not match:
        return value.strip(), 0
    return value[: match.start()].strip(), int(match.group(1))


def _take_date_or_offset(value: str) -> tuple[str, str | None, int]:
    body, offset = _take_offset(value)
    date_match = re.search(r"(?:^|\s)(\d{4}-\d{2}-\d{2})\s*$", body)
    if not date_match:
        return body, None, offset
    return body[: date_match.start()].strip(), date_match.group(1), offset


def _take_query_range(value: str) -> tuple[str, str | None, str | None]:
    """取出查询末尾的单日或日期范围；日期语义留给 Resolver。"""
    match = QUERY_RANGE_RE.search(value)
    if not match:
        return value.strip(), None, None
    start = match.group("start")
    return value[: match.start()].strip(), start, match.group("end") or start


class QQCommandParser:
    """只理解文字，不访问数据库、时钟或 QQ SDK。

    nlu：可选注入的 NLU 规则引擎（docs/NLU-DESIGN.md Phase 0）。
    正则路径失败时才尝试 NLU；NLU 也失败则抛回原 ParseError（行为与关闭时一致）。
    """

    def __init__(self, nlu: NLUIntentMatcher | None = None) -> None:
        self._nlu = nlu

    def parse(self, raw_text: str) -> ParsedIntent:
        text, admin = _strip_prefix(raw_text)
        if not text:
            raise ParseError("help")
        # 称呼前缀剥离（「小泉，帮我约…」）：一处剥离，后续意图/房间/他人检测全用干净文本。
        if self._nlu is not None:
            text = self._nlu.strip_names(text)
            if not text:
                raise ParseError("help")

        action, _, remainder = text.partition(" ")
        # 兼容“预约303 7-8”一类没有空格的输入。
        for candidate in (
            "查询个人",
            "添加周常",
            "删除周常",
            "查询周常",
            "绑定配置",
            "添加管理",
            "删除管理",
            "转让群主",
            "清空预约",
            "撤销清空",
            "备份用户",
            "恢复用户",
            "播报周常",
            "锁定",
            "解锁",
            "预约",
            "取消",
            "查询",
            "空闲",
            "绑定",
        ):
            if text.startswith(candidate):
                action = candidate
                remainder = text[len(candidate) :].strip()
                break

        if admin:
            return self._parse_admin(action, remainder)

        try:
            return self._parse_user(action, remainder)
        except ParseError:
            # 文档 2.1：一行 fallback——正则失败才尝试 NLU，NLU 永不触碰 admin。
            if self._nlu is not None:
                intent = self._nlu.match(text)
                if intent is not None:
                    return intent
                # 可爱化 fail-closed（docs/NLU-DESIGN.md 4.7）：
                # match 内护栏（复合/他人/多日期多房间）命中的输入在此给专门文案；
                # 像命令但缺槽位 → 保留原格式指导；纯闲聊 → chitchat；其余 → 听不懂。
                reason = self._nlu.unsupported_reason(text)
                if reason is not None:
                    raise ParseError(reason) from None
                if self._nlu.looks_like_command(text):
                    raise
                if self._nlu.is_chitchat(text):
                    raise ParseError("chitchat") from None
                raise ParseError("nlu_unrecognized") from None
            raise

    def _parse_user(self, action: str, remainder: str) -> ParsedIntent:
        if action == "绑定":
            parts = remainder.split()
            if len(parts) < 2:
                raise ParseError("bind")
            return ParsedIntent(
                "bind_user",
                {"display_name": "".join(parts[:-1]), "student_id": parts[-1]},
            )

        if action == "查询个人":
            if remainder:
                raise ParseError("personal")
            return ParsedIntent("query_personal")

        if action in {"查询", "空闲"}:
            room, range_start, range_end = _take_query_range(remainder)
            # 可解释性约束（docs/NLU-DESIGN.md 4.8）：正则快车道只处理「所有组件都可解释」
            # 的输入——「查询 X」的 X 必须命中房间白名单/数字模式，否则 fallback NLU，
            # 由个人信号/ML 裁决；绝不静默把非房间文本当房间引用返回
            # （曾导致「查询我的预约」→ query_schedule(room='我的预约') 的静默错误）。
            if room and self._nlu is not None and self._nlu.resolve_room_reference(room) is None:
                raise ParseError("help")
            return ParsedIntent(
                "query_schedule" if action == "查询" else "query_free",
                {
                    "room_reference": room or None,
                    "range_start": range_start,
                    "range_end": range_end,
                },
            )

        if action == "取消":
            body, offset = _take_offset(remainder)
            if not body:
                return ParsedIntent("cancel_reservation", {"offset": offset})
            match = RANGE_RE.fullmatch(body)
            if not match:
                raise ParseError("cancel")
            return ParsedIntent(
                "cancel_reservation",
                {
                    "room_reference": match.group("room").strip() or None,
                    "start": match.group("start"),
                    "end": match.group("end"),
                    "offset": offset,
                },
            )

        if action == "预约":
            body, offset = _take_offset(remainder)
            match = RANGE_RE.fullmatch(body)
            if not match:
                raise ParseError("reserve")
            return ParsedIntent(
                "create_reservation",
                {
                    "room_reference": match.group("room").strip() or None,
                    "start": match.group("start"),
                    "end": match.group("end"),
                    "offset": offset,
                },
            )

        # 旧指令不再形成第二套业务分支，只给出迁移提示。
        if action in {"超前查询", "超前取消", "超前预约", "远期预约", "远期取消"}:
            raise ParseError("deprecated_offset")
        raise ParseError("help")

    def _parse_admin(self, action: str, remainder: str) -> ParsedIntent:
        if action == "绑定配置":
            if not remainder or len(remainder.split()) != 1:
                raise ParseError("bind_config")
            return ParsedIntent("bind_config", {"bot_id": remainder}, True)

        if action in {"备份用户", "恢复用户", "播报周常"}:
            if remainder:
                raise ParseError(action)
            operation = {
                "备份用户": "backup_users",
                "恢复用户": "restore_users",
                "播报周常": "broadcast_routines",
            }[action]
            return ParsedIntent(operation, admin=True)

        if action in {"添加管理", "删除管理", "转让群主"}:
            parts = remainder.split()
            if not parts:
                raise ParseError("role")
            if action != "添加管理" and len(parts) != 1:
                raise ParseError("role")
            return ParsedIntent(
                {"添加管理": "assign_role", "删除管理": "remove_role", "转让群主": "transfer_owner"}[action],
                {"target_name": parts[0], "role": parts[1] if len(parts) > 1 else None},
                True,
            )

        if action in {"清空预约", "撤销清空"}:
            body, absolute_date, offset = _take_date_or_offset(remainder)
            if body:
                raise ParseError("clear")
            return ParsedIntent(
                "clear_reservations" if action == "清空预约" else "undo_clear",
                {"date": absolute_date, "offset": offset},
                True,
            )

        if action == "查询":
            parts = remainder.split()
            if not parts:
                raise ParseError("admin_query")
            absolute_date = parts[0]
            room = " ".join(parts[1:]) or None
            return ParsedIntent("admin_query", {"date": absolute_date, "room_reference": room}, True)

        if action == "取消":
            body, absolute_date, offset = _take_date_or_offset(remainder)
            match = RANGE_RE.fullmatch(body)
            if not match:
                raise ParseError("admin_cancel")
            return ParsedIntent(
                "admin_cancel",
                {
                    "room_reference": match.group("room").strip() or None,
                    "start": match.group("start"),
                    "end": match.group("end"),
                    "date": absolute_date,
                    "offset": offset,
                },
                True,
            )

        if action in {"添加周常", "删除周常"}:
            match = re.fullmatch(
                rf"(周[一二三四五六日天]|[1-7])\s+(.+?)\s+({TIME_TOKEN})\s*[-~～—－]\s*({TIME_TOKEN})(?:\s+(.+))?",
                remainder,
            )
            if not match:
                raise ParseError("routine")
            return ParsedIntent(
                "add_routine" if action == "添加周常" else "remove_routine",
                {
                    "weekday": match.group(1),
                    "room_reference": match.group(2).strip(),
                    "start": match.group(3),
                    "end": match.group(4),
                    "purpose": (match.group(5) or "常规占用").strip(),
                },
                True,
            )

        if action == "查询周常":
            if remainder and not re.fullmatch(r"周[一二三四五六日天]|[1-7]", remainder):
                raise ParseError("routine_query")
            return ParsedIntent("list_routines", {"weekday": remainder or None}, True)

        if action in {"锁定", "解锁"}:
            # 语法：#锁定 琴房 21-22.5 [+1 或 YYYY-MM-DD] [用途]
            #       #解锁 琴房 21-22.5 [+1 或 YYYY-MM-DD]（用途不允许）
            match = re.fullmatch(
                rf"(?P<room>.*?)\s*(?P<start>{TIME_TOKEN})\s*[-~～—－]\s*(?P<end>{TIME_TOKEN})"
                rf"(?:\s+(?P<date>\d{{4}}-\d{{2}}-\d{{2}}|\+\d+))?(?:\s+(?P<purpose>.+))?$",
                remainder,
            )
            if not match:
                raise ParseError("lock" if action == "锁定" else "unlock")
            # 防歧义：用户把日期写在时间之前时，MM-DD 会被正则吞成时间范围
            # （如「303 2026-08-10」→ 房间「303 2026-」、时间 08-10）。输入里有完整
            # 日期却没被 date 组捕获，即发生了这种吞并，直接按格式错误拒绝。
            if match.group("date") is None and re.search(r"\d{4}-\d{2}-\d{2}", remainder):
                raise ParseError("lock" if action == "锁定" else "unlock")
            if action == "解锁" and match.group("purpose"):
                raise ParseError("unlock")
            return ParsedIntent(
                "add_lock" if action == "锁定" else "remove_lock",
                {
                    "room_reference": match.group("room").strip() or None,
                    "start": match.group("start"),
                    "end": match.group("end"),
                    "date": match.group("date") or None,
                    "purpose": (
                        (match.group("purpose") or "临时锁定").strip() if action == "锁定" else None
                    ),
                },
                True,
            )

        raise ParseError("admin_help")
