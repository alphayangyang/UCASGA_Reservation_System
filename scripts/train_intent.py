"""NLU Phase 2 训练脚本（docs/NLU-DESIGN.md 6.2/6.3）。

数据源（自动合并去重）：
- data/nlu/seed_samples.jsonl     冷启动种子（计数权重 SEED_WEIGHT，文档 5.0 衰减）
- data/nlu/samples.jsonl          真实日志样本（source=real）
- data/nlu/candidates.jsonl       夜间 LLM 标注候选（source=llm）

流程：合并 → 分层 5 折交叉验证（报告每类 precision/recall）→ 阈值扫描（宏平均 F1）
      → 全量重训 → 导出 data/nlu/intent_model.json（JSON，非 pickle）

用法：
    .venv/bin/python -m scripts.train_intent              # 训练 + 交叉验证 + 导出
    .venv/bin/python -m scripts.train_intent --threshold 0.8
    .venv/bin/python -m scripts.train_intent --no-save    # 只评估不导出
    .venv/bin/python -m scripts.train_intent --verify     # 影子验证：训练到 tmp，回归集对比，不替换
    .venv/bin/python -m scripts.train_intent --auto       # 自动重训：样本量门槛 → 训练 → 影子验证 → 原子替换
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from qqbot.nlu import NLU_DATA_DIR
from qqbot.nlu.classifier import NaiveBayesClassifier

ROOT = Path(__file__).resolve().parents[1]
NLU_DIR = NLU_DATA_DIR
MODEL_PATH = NLU_DIR / "intent_model.json"

SOURCES = ("seed_samples.jsonl", "samples.jsonl", "candidates.jsonl")
SOURCE_WEIGHTS = {"seed": 0.3, "real": 1.0, "llm": 1.0}
THRESHOLD_SCAN = (0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9)  # 下限 0.6：低于此的置信度多为闲聊幻觉


def load_samples() -> list[tuple[str, str, float]]:
    """合并多源样本（去重），返回 [(text, operation, weight)]。"""
    samples: list[tuple[str, str, float]] = []
    seen: set[tuple[str, str]] = set()
    for filename in SOURCES:
        path = NLU_DIR / filename
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = str(entry.get("text", "")).strip()
            operation = str(entry.get("operation", "")).strip()
            if not text or not operation:
                continue
            key = (text, operation)
            if key in seen:
                continue
            seen.add(key)
            weight = SOURCE_WEIGHTS.get(str(entry.get("source", "real")), 1.0)
            samples.append((text, operation, weight))
    return samples


def cross_validate(
    samples: list[tuple[str, str, float]],
    threshold: float = 0.0,
    folds: int = 5,
) -> dict[str, object]:
    """分层 K 折交叉验证：报告每类 precision/recall/F1/支持数与总体准确率。

    注意：同类样本在数据文件中相邻（种子按意图分组），必须**先分层打乱再分折**，
    否则相邻样本泄漏会让分数虚高（曾实测 90% → 真实 ~20%）。
    threshold 用于评估（fail-closed 影响）：预测低于阈值计为未命中。
    """
    import random

    rng = random.Random(20260822)  # 固定 seed，可复现
    by_class: dict[str, list[tuple[str, str, float]]] = {}
    for sample in samples:
        by_class.setdefault(sample[1], []).append(sample)
    for items in by_class.values():
        rng.shuffle(items)

    fold_size = {cls: max(1, len(items) // folds) for cls, items in by_class.items()}
    correct = 0
    total = 0
    per_class: dict[str, dict[str, int]] = {}
    for cls in by_class:
        per_class[cls] = {"tp": 0, "fp": 0, "fn": 0}

    for fold in range(folds):
        train: list[tuple[str, str, float]] = []
        test: list[tuple[str, str, float]] = []
        for cls, items in by_class.items():
            start = fold * fold_size[cls]
            end = start + fold_size[cls] if fold < folds - 1 else len(items)
            test.extend(items[start:end])
            train.extend(items[:start] + items[end:])
        if not train or not test:
            continue
        classifier = NaiveBayesClassifier(threshold=threshold)
        classifier.fit(train)
        for text, operation, _weight in test:
            predicted = classifier.predict(text)
            total += 1
            if predicted is not None and predicted[0] == operation:
                correct += 1
                per_class[operation]["tp"] += 1
            else:
                per_class[operation]["fn"] += 1
                if predicted is not None:
                    per_class[predicted[0]]["fp"] += 1

    report: dict[str, object] = {
        "accuracy": correct / total if total else 0.0,
        "macro_f1": 0.0,
        "classes": {},
    }
    f1s: list[float] = []
    for cls in sorted(per_class):
        tp, fp, fn = per_class[cls]["tp"], per_class[cls]["fp"], per_class[cls]["fn"]
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        f1s.append(f1)
        report["classes"][cls] = {  # type: ignore[union-attr]
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": len(by_class[cls]),
        }
    report["macro_f1"] = sum(f1s) / len(f1s) if f1s else 0.0
    return report


def scan_threshold(samples: list[tuple[str, str, float]]) -> float:
    """扫描置信度阈值（maximize 宏平均 F1），返回推荐阈值。"""
    best_threshold, best_f1 = THRESHOLD_SCAN[0], -1.0
    for threshold in THRESHOLD_SCAN:
        report = cross_validate(samples, threshold=threshold)
        macro_f1 = float(report["macro_f1"])
        if macro_f1 > best_f1:
            best_f1 = macro_f1
            best_threshold = threshold
    return best_threshold


def render_report(report: dict[str, object]) -> str:
    lines = [
        "## 交叉验证报告",
        "",
        f"总体准确率：{report['accuracy']:.1%}　宏平均 F1：{report['macro_f1']:.1%}",
        "",
        "| 意图 | precision | recall | F1 | 支持数 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for cls in sorted(report["classes"]):  # type: ignore[union-attr]
        info = report["classes"][cls]  # type: ignore[union-attr]
        lines.append(
            "| {} | {:.1%} | {:.1%} | {:.1%} | {} |".format(
                cls, info["precision"], info["recall"], info["f1"], info["support"]
            )
        )
    return "\n".join(lines)


# —— 影子验证 + 自动重训（docs/NLU-DESIGN.md 5.5.3：转正门槛 + 原子替换）——
# 回归集复用 bench_nlu（315 条：种子 + 口语化/边界）；「误伤任何一条历史可解析输入
# → 拒绝转正」；训练产物先写 tmp，验证通过后原子 rename。
META_PATH = NLU_DATA_DIR / "intent_model.meta.json"
TMP_MODEL_PATH = NLU_DATA_DIR / "intent_model.json.tmp"
MIN_NEW_SAMPLES = 20  # 自动重训门槛：candidates 较上次训练新增 ≥20 条才触发


def _candidates_count() -> int:
    path = NLU_DIR / "candidates.jsonl"
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _load_meta() -> dict[str, int]:
    try:
        return json.loads(META_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def should_auto_retrain(min_new: int = MIN_NEW_SAMPLES) -> bool:
    """自动重训门槛：candidates 新增样本量（较上次训练）≥ min_new。"""
    trained = _load_meta().get("trained_candidates", 0)
    return _candidates_count() - trained >= min_new


def shadow_verify(
    new_model_path: Path, cases: list[tuple[str, str | None]], aliases: tuple[str, ...]
) -> dict[str, object]:
    """影子验证：新模型 vs 旧模型（无旧模型 → 纯规则基线）。

    返回 {old_hits, new_hits, regressions}；regressions 非空 → 拒绝转正。
    """
    from qqbot.domain.errors import ParseError
    from qqbot.interfaces.qq.parser import QQCommandParser
    from qqbot.nlu import NLUIntentMatcher

    def evaluate(model_path: Path | None) -> tuple[int, set[str]]:
        parser = QQCommandParser(nlu=NLUIntentMatcher(model_path=model_path, room_aliases=aliases))
        hits: set[str] = set()
        for text, expected in cases:
            try:
                got = parser.parse(text).operation
            except ParseError:
                got = None
            if got == expected:
                hits.add(text)
        return len(hits), hits

    baseline_path = MODEL_PATH if MODEL_PATH.exists() else None
    old_hits, old_ok = evaluate(baseline_path)
    new_hits, new_ok = evaluate(new_model_path)
    return {
        "old_hits": old_hits,
        "new_hits": new_hits,
        "regressions": sorted(old_ok - new_ok),
        "baseline": str(baseline_path) if baseline_path else "pure-rules",
    }


def load_regression_cases() -> list[tuple[str, str | None]]:
    """回归集 = bench_nlu 用例（种子 + 附加 + 边界），影子验证用。"""
    from scripts.bench_nlu import BOUNDARY_CASES, EXTRA_CASES, load_seed

    return load_seed() + EXTRA_CASES + BOUNDARY_CASES


def main() -> int:
    parser = argparse.ArgumentParser(description="NLU Phase 2 训练")
    parser.add_argument("--threshold", type=float, default=None, help="置信度阈值（默认扫描最优）")
    parser.add_argument("--no-save", action="store_true", help="只评估，不导出模型")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="影子验证模式：训练到临时文件，跑回归集对比新旧，报告但不替换",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="自动重训模式（夜间任务同款）：样本量门槛 → 训练 → 影子验证 → 原子替换",
    )
    args = parser.parse_args()

    samples = load_samples()
    if len(samples) < 10:
        print(f"样本不足（{len(samples)} 条），暂不训练。数据源：{', '.join(SOURCES)}")
        return 1

    counts = Counter(operation for _text, operation, _weight in samples)
    print(f"样本总数：{len(samples)}（去重后，seed 权重 {SOURCE_WEIGHTS['seed']}）")
    print(f"意图分布：{dict(counts)}")
    print()

    report = cross_validate(samples)
    print(render_report(report))

    if args.threshold is None:
        threshold = scan_threshold(samples)
        print(f"\n阈值扫描推荐：{threshold}")
    else:
        threshold = args.threshold
    print(f"阈值 {threshold} 下交叉验证：")
    print(render_report(cross_validate(samples, threshold=threshold)))

    classifier = NaiveBayesClassifier(threshold=threshold)
    classifier.fit(samples)
    print(f"\n模型：类数={len(classifier.classes)} 特征数={classifier.feature_count} 阈值={threshold}")

    if args.auto:
        report = run_auto_retrain()
        if report.get("applied"):
            print(
                f"✅ 自动重训完成：基线 {report['old_hits']}/{report['total_cases']} → "
                f"{report['new_hits']}/{report['total_cases']}，样本基线 {report['trained_candidates']} 条"
            )
        else:
            print(f"⏭️ 跳过自动重训：{report.get('skipped')}")
        return 0

    if args.verify:
        # 影子验证模式：训练到 tmp，不碰线上模型（baseline 始终是旧 MODEL_PATH）
        target = TMP_MODEL_PATH
        source = "verify"
    else:
        target = MODEL_PATH
        source = "save"

    if args.no_save:
        print("未导出（--no-save）")
        return 0

    NLU_DIR.mkdir(parents=True, exist_ok=True)
    classifier.save(target)
    size_kb = target.stat().st_size / 1024
    print(f"已训练到 → {target}（{size_kb:.0f} KB）")

    # 影子验证（auto / verify 模式）：误伤任何历史可解析输入 → 拒绝转正
    if source in ("auto", "verify"):
        from scripts.bench_nlu import load_room_aliases

        cases = load_regression_cases()
        aliases = load_room_aliases()
        shadow = shadow_verify(target, cases, aliases)
        print(
            f"\n影子验证：基线命中 {shadow['old_hits']}/{len(cases)}"
            f"（{shadow['baseline']}）→ 新模型 {shadow['new_hits']}/{len(cases)}"
        )
        if shadow["regressions"]:
            print(f"❌ 回归 {len(shadow['regressions'])} 条，拒绝转正：")
            for text in shadow["regressions"][:10]:
                print(f"   - {text}")
            if source == "auto":
                TMP_MODEL_PATH.unlink(missing_ok=True)
                print("保留旧模型（intent_model.json），未替换")
                return 1
            return 1
        print("✅ 影子验证通过（零回归）")
        if source == "auto":
            TMP_MODEL_PATH.replace(MODEL_PATH)
            _save_meta(_candidates_count())
            print(f"已原子替换 → {MODEL_PATH}，并记录训练样本基线 {_candidates_count()} 条")
        else:
            TMP_MODEL_PATH.unlink(missing_ok=True)
            print("verify 模式不替换线上模型（tmp 已清理）")
        return 0

    print(f"已导出 → {MODEL_PATH}（{size_kb:.0f} KB，JSON 非 pickle）")
    return 0


def _save_meta(trained_candidates: int) -> None:
    META_PATH.write_text(
        json.dumps({"trained_candidates": trained_candidates}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def run_auto_retrain() -> dict[str, object]:
    """自动重训核心（--auto 与夜间任务共用）：门槛 → 训练 → 影子验证 → 原子替换。

    返回报告；任何回归 → 拒绝转正（保留旧模型）。调用方自行 try 包裹。
    """
    samples = load_samples()
    if len(samples) < 10:
        return {"skipped": f"样本不足（{len(samples)} 条）"}
    if not should_auto_retrain():
        trained = _load_meta().get("trained_candidates", 0)
        return {
            "skipped": (
                f"candidates 新增样本不足（上次训练 {trained} 条，当前 {_candidates_count()} 条，"
                f"需新增 ≥{MIN_NEW_SAMPLES} 条）"
            )
        }
    classifier = NaiveBayesClassifier(threshold=scan_threshold(samples))
    classifier.fit(samples)
    NLU_DIR.mkdir(parents=True, exist_ok=True)
    classifier.save(TMP_MODEL_PATH)

    from scripts.bench_nlu import load_room_aliases

    shadow = shadow_verify(TMP_MODEL_PATH, load_regression_cases(), load_room_aliases())
    if shadow["regressions"]:
        TMP_MODEL_PATH.unlink(missing_ok=True)
        return {
            "skipped": f"影子验证回归 {len(shadow['regressions'])} 条，拒绝转正",
            "regressions": shadow["regressions"][:10],
            "old_hits": shadow["old_hits"],
            "new_hits": shadow["new_hits"],
        }
    TMP_MODEL_PATH.replace(MODEL_PATH)
    _save_meta(_candidates_count())
    return {
        "applied": True,
        "old_hits": shadow["old_hits"],
        "new_hits": shadow["new_hits"],
        "total_cases": len(load_regression_cases()),
        "trained_candidates": _candidates_count(),
    }


if __name__ == "__main__":
    sys.exit(main())
