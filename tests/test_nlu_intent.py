"""意图层「可解释性约束」验收（docs/NLU-DESIGN.md 4.8）。

原则：规则/正则通道的返回被采纳，当且仅当句子中所有语义组件都可解释——
- 正则「查询 X」的 X 必须命中房间白名单/数字模式，否则 fallback NLU；
- NLU 规则引擎「不采纳」（信号排除清零）≠ 结束，继续到 ML 裁决；
- 绝不静默把非房间文本当房间引用返回（曾导致 query_schedule(room='我的预约')）。

覆盖：5 个历史已知错配（query_personal 被误判 query_schedule）的修复验收、
正则快车道房间验证、ML 兜底裁决、无静默错误的 fail-closed 路径。
"""

from __future__ import annotations

import pytest

from qqbot.domain.errors import ParseError
from qqbot.interfaces.qq.parser import QQCommandParser
from qqbot.nlu import NLU_DATA_DIR, NLUIntentMatcher

MODEL_PATH = NLU_DATA_DIR / "intent_model.json"


def _parser(*, with_ml: bool, aliases: tuple[str, ...] = ()) -> QQCommandParser:
    return QQCommandParser(
        nlu=NLUIntentMatcher(
            model_path=MODEL_PATH if with_ml else None,
            room_aliases=aliases,
        )
    )


def _site_aliases(*configs) -> tuple[str, ...]:
    return tuple(
        alias
        for config in configs
        for room in config.rooms
        for alias in (room.name, *room.aliases)
    )


# —— 5 个历史已知错配修复验收（正则抢跑 → fallback NLU → 个人信号裁决）——
# 注：仅「看看我今天约的」依赖 ML 兜底（信号清零后无关键词分），其余关键词通道直接判对。
MISMATCH_CASES = [
    "查询我的预约",
    "查询一下我的预约",
    "查询我的预约情况",
    "个人预约查询",
    "查询 我的预约",  # 带空格的正则形态同样 fallback
]


@pytest.mark.parametrize("text", MISMATCH_CASES)
def test_personal_query_mismatches_fixed(text: str, yqh_config) -> None:
    parser = _parser(with_ml=False, aliases=_site_aliases(yqh_config))
    intent = parser.parse(text)
    assert intent.operation == "query_personal"


def test_personal_query_needs_ml_fallback(yqh_config) -> None:
    """「看看我今天约的」：个人信号变体清零关键词分 → ML 兜底裁决。"""
    # 有 ML：query_personal
    intent = _parser(with_ml=True, aliases=_site_aliases(yqh_config)).parse("看看我今天约的")
    assert intent.operation == "query_personal"
    # 无 ML：fail-closed（宁可 help，不静默错判 query_schedule）
    with pytest.raises(ParseError):
        _parser(with_ml=False, aliases=_site_aliases(yqh_config)).parse("看看我今天约的")


# —— 正则快车道：房间可解释才采纳 ——


def test_query_fast_path_keeps_valid_rooms(yqh_config) -> None:
    parser = _parser(with_ml=False, aliases=_site_aliases(yqh_config))
    intent = parser.parse("查询 303 +1")
    assert intent.operation == "query_schedule"
    assert intent.arguments["room_reference"] == "303"
    # 中文别名房间同样走快车道
    intent = parser.parse("查询 303琴房")
    assert intent.operation == "query_schedule"
    assert intent.arguments["room_reference"] == "303琴房"


def test_query_unresolvable_room_never_silent(yqh_config) -> None:
    """「查询 X」X 无法解释为房间 → 不静默当房间用（曾把「我的预约」当房间返回）。

    「查询 不存在的房间」→ fallback NLU → 房间组件合法缺省（全查），
    绝不产出 room_reference='不存在的房间' 去撞 NotFound；
    个人信号形态（「查询 我的预约」）→ 表驱动已验 → query_personal。
    """
    parser = _parser(with_ml=False, aliases=_site_aliases(yqh_config))
    intent = parser.parse("查询 不存在的房间")
    assert intent.operation == "query_schedule"
    assert "room_reference" not in intent.arguments  # 房间缺省（全查），非静默假房间


# —— 查询日期 + 房间的既有权重行为（防退化）——


def test_query_with_date_keeps_fast_path(yqh_config) -> None:
    parser = _parser(with_ml=False, aliases=_site_aliases(yqh_config))
    intent = parser.parse("查询 303 2026-08-25")
    assert intent.operation == "query_schedule"
    assert intent.arguments["room_reference"] == "303"
    assert intent.arguments["range_start"] == "2026-08-25"
