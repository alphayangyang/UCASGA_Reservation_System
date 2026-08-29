"""白名单自优化测试（docs/NLU-DESIGN.md 5.4 自动档 + 5.5 护栏）。

覆盖：校验护栏（room_id/冲突/纯数字）、相似度建议（304外面的房间→304b）、
频次门槛、写入幂等与原子性、人工标注池应用与清空、闲聊词提炼质量、
自动重训门槛（should_auto_retrain）。数据文件路径经 monkeypatch 隔离。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qqbot.nlu import optimize as opt


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 optimize 的数据文件路径全部隔离到 tmp（不碰真实数据）。"""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(opt, "WHITELIST_PATH", data / "room_whitelist.json")
    monkeypatch.setattr(opt, "MANUAL_SAMPLES_PATH", data / "manual_samples.json")
    monkeypatch.setattr(opt, "CHITCHAT_KEYWORDS_PATH", data / "chitchat_keywords.json")
    return data


def _yqh_config():
    from qqbot.infrastructure.config import load_site_config

    root = Path(__file__).resolve().parents[1]
    return load_site_config(root / "configs" / "yqh.yaml", project_root=root)


# —— 校验护栏 ——


def test_validate_alias(yqh_config) -> None:
    assert opt.validate_alias(yqh_config, "303练琴房", "yqh-303") is None
    assert "不存在" in opt.validate_alias(yqh_config, "x琴房", "yqh-999")  # room_id 不存在
    assert "冲突" in opt.validate_alias(yqh_config, "304A", "yqh-303")  # 属于其他房间
    assert "纯数字" in opt.validate_alias(yqh_config, "123", "yqh-303")
    assert "空" in opt.validate_alias(yqh_config, "", "yqh-303")


# —— 相似度建议 ——


def test_suggest_room_mapping(yqh_config) -> None:
    # 完全匹配（配置自身别名）
    mapping = opt.suggest_room_mapping(yqh_config, "303琴房")
    assert mapping is not None
    assert mapping[0] == "yqh-303"
    # 相似表达（字符级 ≥0.6）：「三百零三琴房」≈「三零三」
    mapping = opt.suggest_room_mapping(yqh_config, "三百零三琴房")
    assert mapping is not None
    assert mapping[0] == "yqh-303"
    # 无高置信候选 → None（不硬猜）
    assert opt.suggest_room_mapping(yqh_config, "完全不相干的表达") is None


def test_collect_suggestions_frequency_gate(yqh_config, tmp_path) -> None:
    anomalies = tmp_path / "anomalies.jsonl"
    # 只出现 1 次 → 低于频次门槛 → 无建议
    anomalies.write_text(
        json.dumps({"text": "x", "bot_id": "yqh", "reason": "resolver", "room_reference": "303琴房"}) + "\n",
        encoding="utf-8",
    )
    assert opt.collect_suggestions({"yqh": yqh_config}, anomalies) == []
    # 出现 3 次（≥门槛）且相似 → 建议
    with anomalies.open("a", encoding="utf-8") as handle:
        for _ in range(2):
            handle.write(
                json.dumps({"text": "x", "bot_id": "yqh", "reason": "resolver", "room_reference": "303琴房"})
                + "\n"
            )
    suggestions = opt.collect_suggestions({"yqh": yqh_config}, anomalies)
    assert len(suggestions) == 1
    assert suggestions[0]["room_id"] == "yqh-303"


# —— 写入：幂等 + 原子性 ——


def test_apply_aliases_idempotent(yqh_config, isolated) -> None:
    entries = [{"bot_id": "yqh", "alias": "303练琴房", "room_id": "yqh-303"}]
    applied, rejected = opt.apply_aliases({"yqh": yqh_config}, entries, source="manual")
    assert len(applied) == 1 and rejected == []
    # 幂等：再次应用不重复
    applied2, _ = opt.apply_aliases({"yqh": yqh_config}, entries, source="manual")
    assert applied2 == []
    # 拒绝：room_id 不存在
    bad = [{"bot_id": "yqh", "alias": "坏别名", "room_id": "yqh-999"}]
    _, rejected2 = opt.apply_aliases({"yqh": yqh_config}, bad, source="manual")
    assert len(rejected2) == 1


def test_whitelist_atomic_write(yqh_config, isolated, monkeypatch) -> None:
    entries = [{"bot_id": "yqh", "alias": "303练琴房", "room_id": "yqh-303"}]
    opt.apply_aliases({"yqh": yqh_config}, entries, source="manual")
    # 无 .tmp 残留
    assert not list(isolated.glob("*.tmp"))


# —— 人工标注池：应用 + 清空 ——


def test_run_auto_optimize_manual_flow(yqh_config, isolated) -> None:
    opt.MANUAL_SAMPLES_PATH.write_text(
        json.dumps(
            {
                "version": 1,
                "room_aliases": {"yqh": [{"alias": "304外面的房间", "room_id": "yqh-304b", "note": "测试"}]},
            }
        ),
        encoding="utf-8",
    )
    report = opt.run_auto_optimize(isolated, {"yqh": yqh_config})
    assert len(report["manual_applied"]) == 1
    # 人工池已清空（room_aliases 空 dict）
    cleared = json.loads(opt.MANUAL_SAMPLES_PATH.read_text(encoding="utf-8"))
    assert cleared["room_aliases"].get("yqh", []) == []
    # 白名单已写入
    assert opt.load_whitelist()["extra_aliases"]["yqh"]["304外面的房间"] == "yqh-304b"


# —— 闲聊词提炼 ——


def test_extract_chitchat_keywords_quality(yqh_config, isolated) -> None:
    anomalies = isolated / "anomalies.jsonl"
    lines = [
        # 真闲聊（重复 2 次 → 提炼）
        {"text": "今天天气不错", "bot_id": "yqh", "reason": "no_operation"},
        {"text": "今天天气不错", "bot_id": "yqh", "reason": "no_operation"},
        {"text": "哈哈哈哈哈", "bot_id": "yqh", "reason": "no_operation"},
        # 业务噪声（练琴预约）→ 清洗后无残留
        {"text": "明天下午3点去304外面的房间练2h琴", "bot_id": "yqh", "reason": "no_operation"},
        # 学号打码 → 跳过（绑定类样本）
        {"text": "我是张三 2023X1234567890", "bot_id": "yqh", "reason": "no_operation"},
    ]
    anomalies.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n", encoding="utf-8"
    )
    candidates = opt.extract_chitchat_keywords(anomalies)
    assert "天气" in candidates  # 高频闲聊核心
    assert "哈哈哈" in candidates
    # 业务残留不混入
    assert not any("琴" in word or "房间" in word for word in candidates)
    assert "我是" not in candidates


def test_apply_chitchat_idempotent(isolated) -> None:
    added, _ = opt.apply_chitchat_keywords(["天气不错", "好无聊"])
    assert added == ["天气不错", "好无聊"]
    added2, _ = opt.apply_chitchat_keywords(["天气不错", "新词"])
    assert added2 == ["新词"]  # 幂等


# —— 自动重训门槛 ——


def test_should_auto_retrain_gate(isolated, monkeypatch) -> None:
    candidates = isolated / "candidates.jsonl"
    candidates.write_text("\n".join(["{}"] * 30), encoding="utf-8")
    monkeypatch.setattr(opt, "WHITELIST_PATH", isolated / "w.json")  # 防误写
    monkeypatch.setattr(opt, "CHITCHAT_KEYWORDS_PATH", isolated / "c.json")
    # 无 meta → 基线 0 → 30 条 ≥ 20 → 触发
    monkeypatch.setattr(opt, "MANUAL_SAMPLES_PATH", isolated / "m.json")
    # 直接测 train_intent 的 should_auto_retrain（meta 与 candidates 路径全局——用 monkeypatch）
    import scripts.train_intent as train

    monkeypatch.setattr(train, "NLU_DIR", isolated)
    monkeypatch.setattr(train, "META_PATH", isolated / "intent_model.meta.json")
    monkeypatch.setattr(train, "MODEL_PATH", isolated / "intent_model.json")
    monkeypatch.setattr(train, "TMP_MODEL_PATH", isolated / "intent_model.json.tmp")
    assert train.should_auto_retrain() is True
    # 记录基线后：30-30=0 < 20 → 跳过
    train._save_meta(train._candidates_count())
    assert train.should_auto_retrain() is False
