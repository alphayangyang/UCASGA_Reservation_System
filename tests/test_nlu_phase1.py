"""Phase 1：数据收集与夜间 LLM 批处理标注测试（docs/NLU-DESIGN.md 5.x）。

全部使用注入式假 LLM 调用器，不联网、不碰真实数据。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from qqbot.domain.calendar import SHANGHAI_TZ
from qqbot.infrastructure.config import load_site_config
from qqbot.nlu import (
    NightlyReport,
    build_intent_from_llm,
    mask_sensitive,
    run_nightly_annotate,
    validate_with_resolver,
)
from qqbot.nlu.llm import _normalize_for_compare, annotate_with_consensus


def make_fake_caller(sequence: list[dict[str, Any] | None]) -> Any:
    """按序返回预设结果的假 LLM 调用器（耗尽后返回最后一个）。"""
    calls: list[dict[str, Any] | None] = list(sequence)

    async def caller(_text: str) -> dict[str, Any] | None:
        return calls[0] if len(calls) == 1 else (calls.pop(0) if calls else None)

    return caller


# —— 脱敏（手册红线：禁记完整学号） ——


@pytest.mark.parametrize(
    "text",
    ["我是张三 2023X1234567890", "绑定 张三 123456789012345", "学号2023X1234567890在后面"],
)
def test_mask_sensitive_masks_student_ids(text: str) -> None:
    masked = mask_sensitive(text)
    assert "1234567890" not in masked or "2023X" not in masked
    assert "***" in masked


def test_mask_sensitive_keeps_normal_text() -> None:
    assert mask_sensitive("明天下午帮我看看303有没有空") == "明天下午帮我看看303有没有空"


# —— 一致性投票（文档 5.4） ——


def test_consensus_pass_on_first_round() -> None:
    result = {"operation": "query_free", "entities": [{"type": "room", "text": "303"}]}
    caller = make_fake_caller([result, result, result])
    got = asyncio.run(annotate_with_consensus(caller, "x", votes=3, max_rounds=5))
    assert got == result


def test_consensus_retries_until_consistent() -> None:
    a = {"operation": "query_free", "entities": []}
    b = {"operation": "query_schedule", "entities": []}
    # 第一轮：a,b,a 不一致；第二轮：b,b,b 一致
    caller = make_fake_caller([a, b, a, b, b, b])
    got = asyncio.run(annotate_with_consensus(caller, "x", votes=3, max_rounds=5))
    assert got == b


def test_consensus_fails_after_max_rounds() -> None:
    a = {"operation": "query_free", "entities": []}
    b = {"operation": "query_schedule", "entities": []}
    caller = make_fake_caller([a, b, a] * 5)
    assert asyncio.run(annotate_with_consensus(caller, "x", votes=3, max_rounds=5)) is None


def test_consensus_returns_none_on_call_failure() -> None:
    caller = make_fake_caller([None, None, None])
    assert asyncio.run(annotate_with_consensus(caller, "x", votes=3, max_rounds=2)) is None


def test_normalize_ignores_entity_order() -> None:
    a = {
        "operation": "create_reservation",
        "entities": [{"type": "room", "text": "303"}, {"type": "time", "text": "7-8"}],
    }
    b = {
        "operation": "create_reservation",
        "entities": [{"type": "time", "text": "7-8"}, {"type": "room", "text": "303"}],
    }
    assert _normalize_for_compare(a) == _normalize_for_compare(b)


# —— LLM 结果 → ParsedIntent（归一化在本地） ——


def test_build_intent_from_llm_creates_reservation() -> None:
    intent = build_intent_from_llm("create_reservation", "明天下午约303 3点到5点", "303")
    assert intent is not None
    assert intent.operation == "create_reservation"
    assert intent.arguments["room_reference"] == "303"
    assert intent.arguments["start"] == "15:00"
    assert intent.arguments["end"] == "17:00"


def test_build_intent_from_llm_fails_closed_without_time() -> None:
    # LLM 判预约但本地抽不出时间 → fail-closed
    assert build_intent_from_llm("create_reservation", "预约304b明天下午", "304b") is None


@pytest.mark.parametrize("operation", ["admin_cancel", "hack", "", "clear_reservations"])
def test_build_intent_rejects_unknown_operation(operation: str) -> None:
    assert build_intent_from_llm(operation, "随便什么", None) is None


# —— Resolver 校验（文档 5.4 第 4 步） ——


def test_validate_with_resolver_ok(yqh_config) -> None:
    intent = build_intent_from_llm("create_reservation", "约303明天7-8", "303")
    assert intent is not None
    now = datetime(2026, 8, 7, 21, 0, tzinfo=SHANGHAI_TZ)
    assert validate_with_resolver(intent, yqh_config, now) is True


def test_validate_with_resolver_rejects_unknown_room(yqh_config) -> None:
    # LLM 一致地错（编造房间 306）→ Resolver 拦截
    intent = build_intent_from_llm("create_reservation", "约306明天7-8", "306")
    assert intent is not None
    now = datetime(2026, 8, 7, 21, 0, tzinfo=SHANGHAI_TZ)
    assert validate_with_resolver(intent, yqh_config, now) is False


# —— 夜间批处理端到端（假 LLM） ——


def _write_pending(data_dir: Path, entries: list[dict[str, Any]]) -> None:
    pending_dir = data_dir / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    with (pending_dir / "20260822.jsonl").open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _build_configs(tmp_path: Path) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    yqh = load_site_config(root / "configs" / "yqh.yaml", project_root=root)
    return {"yqh": yqh}


def test_nightly_annotate_end_to_end(tmp_path: Path) -> None:
    configs = _build_configs(tmp_path)
    data_dir = tmp_path / "nlu"
    _write_pending(
        data_dir,
        [
            {"text": "明天下午帮我看看303有没有空", "bot_id": "yqh", "ts": "2026-08-22T12:00:00"},
            {"text": "帮我看看我约了什么", "bot_id": "yqh", "ts": "2026-08-22T12:01:00"},
            {"text": "约306明天7-8", "bot_id": "yqh", "ts": "2026-08-22T12:02:00"},  # 房间不存在
            {"text": "今天天气不错", "bot_id": "yqh", "ts": "2026-08-22T12:03:00"},  # 无法理解
            {"text": "明天下午3点去304外面的房间练2h琴", "bot_id": "yqh", "ts": "2026-08-22T12:04:00"},
            # 房间指代复杂 → 槽位失败
        ],
    )

    from qqbot.nlu import NLUIntentMatcher

    async def fake_caller(text: str) -> dict[str, Any] | None:
        # 模拟 LLM：能圈出房间指代（304外面的房间），意图判断与规则引擎一致
        intent = NLUIntentMatcher().match(text)
        if "304外面的房间" in text:
            return {
                "operation": "create_reservation",
                "entities": [{"type": "room", "text": "304外面的房间"}],
            }
        if intent is None:
            return {"operation": None, "entities": []}
        entities: list[dict[str, Any]] = []
        if intent.arguments.get("room_reference"):
            entities.append({"type": "room", "text": str(intent.arguments["room_reference"])})
        if "start" in intent.arguments and "end" in intent.arguments:
            entities.append(
                {"type": "time", "text": f"{intent.arguments['start']}-{intent.arguments['end']}"}
            )
        if intent.arguments.get("offset"):
            entities.append({"type": "date", "text": f"+{intent.arguments['offset']}"})
        return {"operation": intent.operation, "entities": entities}

    report: NightlyReport = asyncio.run(run_nightly_annotate(data_dir, configs, fake_caller))

    assert report.pending == 5
    assert report.accepted == 2  # query_free + query_personal
    assert report.resolver_failed == 2  # 306 + 304外面的房间（本地归一化拒绝）
    assert report.consensus_failed == 1  # 天气
    assert report.slot_failed == 0

    # 候选库：只有通过校验的样本，且 source=llm
    candidates = [
        json.loads(line)
        for line in (data_dir / "candidates.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(candidates) == 2
    assert {c["operation"] for c in candidates} == {"query_free", "query_personal"}

    # 异常库：全部失败样本都在，不丢（文档 5.4：留着分析）
    anomalies = [
        json.loads(line)
        for line in (data_dir / "anomalies.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(anomalies) == 3

    # 日报生成
    report_file = data_dir / "reports" / f"{report.date}.md"
    assert report_file.exists()
    assert "候选样本入库：2" in report_file.read_text(encoding="utf-8")

    # pending 已清空
    assert not list((data_dir / "pending").glob("*.jsonl"))


def test_write_pending_masks_and_filters_compound(tmp_path: Path) -> None:
    """write_pending：学号打码 + 复合指令不收集（NLU 语义封装在 nlu 模块内）。"""
    from qqbot.nlu import write_pending

    data_dir = tmp_path / "nlu"
    write_pending(data_dir, "我是张三 2023X1234567890", "yqh")
    write_pending(data_dir, "帮我取消今天的预约，预约明天12点到1点的303", "yqh")
    files = list((data_dir / "pending").glob("*.jsonl"))
    assert files, "pending 应已写入"
    content = files[0].read_text(encoding="utf-8")
    assert "2023X1234567890" not in content  # 学号打码
    assert "取消今天的预约" not in content  # 复合指令未收集
    assert content.count("***") == 1  # 只有一条（复合被过滤）


def test_nightly_annotate_dedup_and_sensitive(tmp_path: Path) -> None:
    configs = _build_configs(tmp_path)
    data_dir = tmp_path / "nlu"
    # 重复条目只处理一次；学号在流程中被打码
    _write_pending(
        data_dir,
        [
            {"text": "帮我看看303有没有空", "bot_id": "yqh", "ts": "2026-08-22T10:00:00"},
            {"text": "帮我看看303有没有空", "bot_id": "yqh", "ts": "2026-08-22T11:00:00"},
            {"text": "我是张三 2023X1234567890", "bot_id": "yqh", "ts": "2026-08-22T12:00:00"},
        ],
    )

    async def fake_caller(text: str) -> dict[str, Any] | None:
        if "303" in text and "有空" in text:
            return {"operation": "query_free", "entities": [{"type": "room", "text": "303"}]}
        return {"operation": None, "entities": []}

    report: NightlyReport = asyncio.run(run_nightly_annotate(data_dir, configs, fake_caller))
    assert report.pending == 2  # 重复条目已去重
    assert report.accepted == 1

    # 候选库中不得出现完整学号（手册红线）
    candidates_text = (data_dir / "candidates.jsonl").read_text(encoding="utf-8")
    assert "2023X1234567890" not in candidates_text
    anomalies_text = (data_dir / "anomalies.jsonl").read_text(encoding="utf-8")
    assert "2023X1234567890" not in anomalies_text


def test_nightly_annotate_unsupported_accepted(tmp_path: Path) -> None:
    """LLM 判 unsupported（复合/他人/多日期）→ 直接进候选库（训练 unsupported 类用）。"""
    from qqbot.nlu import run_nightly_annotate

    configs = _build_configs(tmp_path)
    data_dir = tmp_path / "nlu"
    _write_pending(
        data_dir,
        [
            {"text": "取消张三明天的预约", "bot_id": "yqh", "ts": "2026-08-22T12:00:00"},
            {"text": "帮我约今天8点到9点", "bot_id": "yqh", "ts": "2026-08-22T12:01:00"},
        ],
    )

    async def fake_caller(text: str) -> dict[str, Any] | None:
        if "张三" in text:
            return {"operation": "unsupported", "reason": "涉及他人张三", "entities": []}
        return {"operation": "create_reservation", "entities": [{"type": "room", "text": "303"}]}

    report: NightlyReport = asyncio.run(run_nightly_annotate(data_dir, configs, fake_caller))
    assert report.accepted == 2
    candidates = [
        json.loads(line)
        for line in (data_dir / "candidates.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    unsupported = [c for c in candidates if c["operation"] == "unsupported"]
    assert len(unsupported) == 1
    assert unsupported[0]["arguments"]["reason"] == "涉及他人张三"
