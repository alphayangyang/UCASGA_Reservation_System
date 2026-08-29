"""P1 白名单化验收：gazetteer 房间提取（docs/NLU-DESIGN.md 3.3/5.4.2）。

覆盖：
- gazetteer 收录规则 / 最长匹配 / 字母数字边界（内部函数单测）；
- 无损规范化（NFKC 全角→半角）与时间朗读防护；
- 泛称缺省（「…的琴房」→ 房间 None → 单房间站点兜底）——「我琴房」类缺陷的修复验收；
- 中文别名 / 语音数字 / 多房间不双计 / 复杂指代不猜（端到端回归）。

设计原则：候选必须命中已知别名才算房间；不命中绝不产出猜测值（fail-closed）。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from qqbot.application.resolver import CommandResolver
from qqbot.domain.calendar import SHANGHAI_TZ
from qqbot.domain.errors import AppError, ParseError
from qqbot.interfaces.qq.parser import QQCommandParser
from qqbot.nlu import NLUIntentMatcher
from qqbot.nlu.matcher import (
    _prepare_gazetteer,
    _resolve_room_reference,
    _room_reference_hits,
    _scan_gazetteer,
)

NOW = datetime(2026, 8, 22, 10, 0, tzinfo=SHANGHAI_TZ)


def _site_aliases(*configs) -> tuple[str, ...]:
    return tuple(alias for config in configs for room in config.rooms for alias in (room.name, *room.aliases))


def _parser(*configs) -> QQCommandParser:
    return QQCommandParser(nlu=NLUIntentMatcher(room_aliases=_site_aliases(*configs)))


# —— 内部函数单测：gazetteer 白名单 ——


def test_prepare_gazetteer_filters_and_normalizes() -> None:
    aliases = _prepare_gazetteer(
        ("玉泉路琴房", "玉泉路", "中关村", "303琴房", "三零三", "304A", "玉泉路排练室")
    )
    # 收录：含琴房/排练室字样或 ASCII 字母数字
    assert "玉泉路琴房" in aliases
    assert "玉泉路排练室" in aliases
    assert "303琴房" in aliases
    assert "304a" in aliases  # NFKC + lower 归一化
    # 不收录：纯地名（无琴房语境会误命中闲聊）；语音数字由 ROOM_TOKEN_CN 兜底
    assert "玉泉路" not in aliases
    assert "中关村" not in aliases
    assert "三零三" not in aliases
    # 去重
    assert aliases.count("303琴房") == 1


def test_prepare_gazetteer_longest_first() -> None:
    aliases = _prepare_gazetteer(("303", "303琴房"))
    assert aliases.index("303琴房") < aliases.index("303")  # 最长优先


def test_scan_gazetteer_longest_match_and_boundary() -> None:
    aliases = _prepare_gazetteer(("303", "303琴房", "304", "304a"))
    # 最长匹配：303琴房 优先于 303
    assert _scan_gazetteer("帮我看看303琴房明天有没有空", aliases) == "303琴房"
    # 字母数字边界：304a 不能被 304 顶替；x304 前缀粘连拒绝
    assert _scan_gazetteer("帮我约304a 7点到8点", aliases) == "304a"
    assert _scan_gazetteer("帮我约304 7点到8点", aliases) == "304"
    assert _scan_gazetteer("x304 7点", aliases) is None
    # 不在白名单的数字 → 不猜（缺省），绝不产出「最像的」
    assert _scan_gazetteer("帮我约305 7点到8点", aliases) is None


def test_resolve_room_general_name_returns_none() -> None:
    """泛称「…的琴房」→ 白名单无命中 → None（不产出「我琴房」类猜测值）。"""
    assert _resolve_room_reference("帮我预约一下今天下午五点半到七点的琴房", ()) is None
    assert _resolve_room_reference("帮俺预约一下琴房明天7点到8点", ()) is None
    assert _resolve_room_reference("劳驾帮我约下琴房下午3点到5点", ()) is None


def test_resolve_room_complex_reference_not_guessed() -> None:
    """数字后跟方位修饰（304外面的房间）→ 复杂指代，不拆解。"""
    assert _resolve_room_reference("明天下午3点去304外面的房间练2h琴", ()) is None
    assert _resolve_room_reference("看看304旁边的303", ()) == "303"  # 跳过 304，命中 303


def test_resolve_room_time_reading_guard() -> None:
    """时间朗读不误伤：三点零四分 / 8点15分 的数字不是房间。"""
    assert _resolve_room_reference("帮我约303明天三点零四分", ()) == "303"
    assert _resolve_room_reference("帮我约8点15分的304", ()) == "304"


def test_resolve_room_fullwidth_normalized() -> None:
    """无损规范化（NFKC）：全角 ３０４b → 304b。"""
    assert _resolve_room_reference("帮我约一下３０４b 7点到8点半", ("304b",)) == "304b"
    assert _resolve_room_reference("帮我看看３０３琴房明天有没有空", ("303琴房",)) == "303琴房"


def test_room_hits_dedupe_and_count() -> None:
    """多房间检测：303琴房 不因数字子串双计；真多房间照常计数。"""
    aliases = _prepare_gazetteer(("303", "303琴房"))
    assert _room_reference_hits("看看303琴房明天有没有空", aliases) == ["303琴房"]
    assert len(_room_reference_hits("303和304a明天有空吗", ())) == 2
    assert len(_room_reference_hits("看看303琴房和304a明天的预约", aliases)) == 2


# —— 端到端验收（parser + Resolver）——

# (输入, 站点, 期望)；期望形态同 test_nlu_regression：
#   "operation"              —— operation 相等
#   ("usage", "xxx")         —— ParseError usage
#   ("resolve", "子串")       —— Resolver 后 Command 字符串包含子串
GAZETTEER_CASES: list[tuple[str, str, object]] = [
    # —— 泛称缺省（P1 核心修复：以前是「我琴房」NotFound，现在单房间站点兜底成功）——
    ("帮我预约一下今天下午五点半到七点的琴房", "yql", ("resolve", "CreateReservation(room_id='yql-main'")),
    ("帮我预约一下琴房明天7点到8点", "yql", ("resolve", "CreateReservation(room_id='yql-main'")),
    ("劳驾帮我约下琴房下午3点到5点", "yql", ("resolve", "CreateReservation(room_id='yql-main'")),
    ("拜托帮我订一下琴房晚上8点到9点", "yql", ("resolve", "CreateReservation(room_id='yql-main'")),
    # —— 中文别名 gazetteer 命中（多房间站点需显式房间）——
    ("帮我看看303琴房明天有没有空", "yqh", ("resolve", "room_id='yqh-303'")),
    ("帮我看看玉泉路琴房明天有没有空", "yql", ("resolve", "room_id='yql-main'")),
    # —— 无损规范化（全角输入）——
    ("帮我约一下３０４b 7点到8点半", "yqh", ("resolve", "CreateReservation(room_id='yqh-304b'")),
    ("帮我看看３０３琴房明天有没有空", "yqh", ("resolve", "room_id='yqh-303'")),
    # —— 语音数字不退化（ROOM_TOKEN_CN 兜底）——
    ("帮我约三零三明天7-8", "yqh", ("resolve", "CreateReservation(room_id='yqh-303'")),
    # —— 复杂指代：房间槽位不拆解（缺省），意图仍为预约（2026-08-23 架构修订）——
    ("明天下午3点去304外面的房间练2h琴", "yqh", "create_reservation"),
    # —— 时间朗读不误伤 ——
    ("帮我约303明天三点零四分", "yqh", ("usage", "help")),
    # —— 多房间：303琴房 不双计，真多房间降级 ——
    ("看看303琴房和304a明天的预约", "yqh", ("degrade", "room")),
]


@pytest.mark.parametrize(
    ("text", "site", "expected"),
    [(case[0], case[1], case[2]) for case in GAZETTEER_CASES],
    ids=[f"{i:02d}-{case[0][:14]}" for i, case in enumerate(GAZETTEER_CASES)],
)
def test_nlu_gazetteer(text: str, site: str, expected: object, yqh_config, yql_config) -> None:
    config = yqh_config if site == "yqh" else yql_config
    parser = _parser(yqh_config, yql_config)

    if isinstance(expected, tuple) and expected[0] == "resolve":
        intent = parser.parse(text)
        command = str(CommandResolver().resolve(intent, config, NOW))
        assert expected[1] in command
        return

    try:
        intent = parser.parse(text)
    except ParseError as exc:
        assert isinstance(expected, tuple) and expected[0] == "usage"
        assert exc.details.get("usage") == expected[1]
        return
    except AppError:
        pytest.fail("不应出现业务错误")

    if isinstance(expected, tuple) and expected[0] == "degrade":
        assert intent.hint is not None, "降级查询应带 hint 提醒"
        assert "房间" in intent.hint
        assert "room_reference" not in intent.arguments  # 房间缺省（查全部）
        return

    assert intent.operation == expected
