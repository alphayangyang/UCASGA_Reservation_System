"""架构修订验收：ML 意图主导 + 槽位数据驱动（docs/NLU-DESIGN.md 4.8/6.4，2026-08-23）。

覆盖：
- ML 主通道：高置信意图直接采纳（「我明天的预约」→ query_personal）；
- ML 低置信/无模型 → 规则候选兜底；ML 误报被槽位构建兜底（fail-closed）；
- 规则候选循环：分数最高的候选构建失败 → 自然落到次候选；
- 冲突裁决下沉：写入类冲突拒绝、查询类降级 + hint、他人+个人查询降级全站；
- 复杂指代：房间槽位缺省（不拆解），意图仍判预约。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from qqbot.application.resolver import CommandResolver
from qqbot.domain.calendar import SHANGHAI_TZ
from qqbot.domain.errors import ParseError
from qqbot.interfaces.qq.parser import QQCommandParser
from qqbot.nlu import NLU_DATA_DIR, NLUIntentMatcher

MODEL_PATH = NLU_DATA_DIR / "intent_model.json"
NOW = datetime(2026, 8, 22, 10, 0, tzinfo=SHANGHAI_TZ)


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


# —— ML 主通道 ——


def test_ml_main_channel_resolves_personal_query(yqh_config) -> None:
    """「帮我看看我的预约」：ML 高置信直接判 query_personal
    （规则关键词通道曾被 query_schedule 抢跑的 Bug B 场景）。"""
    intent = _parser(with_ml=True, aliases=_site_aliases(yqh_config)).parse("帮我看看我的预约")
    assert intent.operation == "query_personal"


def test_personal_query_with_future_date_degrades(yqh_config) -> None:
    """「帮我看看我明天的预约」：个人查询只支持今天（QueryPersonal 默认业务日），
    带非今天日期 → 降级全站查询该日期（信息含自己且日期正确），不静默查今天。"""
    intent = _parser(with_ml=True, aliases=_site_aliases(yqh_config)).parse("帮我看看我明天的预约")
    assert intent.operation == "query_schedule"
    assert intent.arguments.get("natural_date") == 1  # 明天，不是静默的今天


def test_ml_misreport_build_fallback(yqh_config) -> None:
    """ML 误报（乱码被高置信判 bind_user 0.961）→ 槽位构建兜底 fail-closed。"""
    with pytest.raises(ParseError):
        _parser(with_ml=True, aliases=_site_aliases(yqh_config)).parse("abcdefg 123")


def test_ml_low_confidence_falls_to_rules(yqh_config) -> None:
    """ML 低置信（0.601）→ 规则候选兜底 → 全失败 → 文案区分（非命令自然语言）。"""
    with pytest.raises(ParseError) as exc:
        _parser(with_ml=True, aliases=_site_aliases(yqh_config)).parse("这个小程序是干嘛的")
    assert exc.value.details.get("usage") == "nlu_unrecognized"


# —— 规则候选循环（无模型退化路径）——


def test_rule_candidate_loop_falls_through(yqh_config) -> None:
    """候选降序：create（缺时间构建失败）→ 自然落到 query_personal。"""
    intent = _parser(with_ml=False, aliases=_site_aliases(yqh_config)).parse("查一下我的预约")
    assert intent.operation == "query_personal"


def test_no_model_pure_rules_behavior(yqh_config) -> None:
    """无模型退化路径：规则引擎完整工作（预约/查询/取消）。"""
    parser = _parser(with_ml=False, aliases=_site_aliases(yqh_config))
    intent = parser.parse("帮我约303 7点到8点半")
    command = str(CommandResolver().resolve(intent, yqh_config, NOW))
    assert "room_id='yqh-303'" in command


# —— 冲突裁决下沉 ——


def test_write_conflict_rejected(yqh_config) -> None:
    """写入类多房间 → fail-closed（不半执行）。"""
    with pytest.raises(ParseError) as exc:
        _parser(with_ml=False, aliases=_site_aliases(yqh_config)).parse("取消303和304a明天的预约")
    assert exc.value.details.get("usage") == "compound"


def test_query_degrade_with_hint(yqh_config) -> None:
    """查询类多房间 → 降级全查 + hint。"""
    intent = _parser(with_ml=False, aliases=_site_aliases(yqh_config)).parse("看看303和304a明天的预约")
    assert intent.operation == "query_schedule"
    assert "room_reference" not in intent.arguments
    assert "房间" in (intent.hint or "")


def test_personal_query_with_other_person_degrades(yqh_config) -> None:
    """「看看张三明天的预约」ML 判 query_personal → 不支持按人过滤 → 降级全站查询 + hint。"""
    intent = _parser(with_ml=True, aliases=_site_aliases(yqh_config)).parse("看看张三明天的预约")
    assert intent.operation == "query_schedule"
    assert "人名" in (intent.hint or "")


# —— 复杂指代（房间槽位缺省）——


def test_complex_reference_room_optional_with_ml(yqh_config) -> None:
    """「304外面的房间」→ 房间槽位不拆解（缺省），意图仍判预约（多房间站点 Resolver 提示）。"""
    parser = _parser(with_ml=True, aliases=_site_aliases(yqh_config))
    intent = parser.parse("明天下午3点去304外面的房间练2h琴")
    assert intent.operation == "create_reservation"
    assert "room_reference" not in intent.arguments
