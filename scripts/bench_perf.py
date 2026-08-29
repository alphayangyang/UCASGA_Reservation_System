"""NLU 完整链路性能压测（真实环境形态：ML 意图主通道 + 规则槽位 + 冲突裁决）。

与 bench_nlu（纯规则退化路径评测）互补：
- 默认模式：完整链路（挂 ML 模型 + 白名单注入），315 条用例每条迭代 N 次；
- --breakdown：分解对比 纯规则 / ML 单独 / 完整链路 的延迟；
- 冷启动：首次命中时模型懒加载耗时（真实极端场景：重启后第一个请求）；
- 极端用例：复杂指代/多房间/他人/星期等「最重」输入单独报告。

用法：
    .venv/bin/python -m scripts.bench_perf               # 完整链路压测
    .venv/bin/python -m scripts.bench_perf --breakdown    # 分解对比
    .venv/bin/python -m scripts.bench_perf --iter 500     # 每条迭代 500 次
    .venv/bin/python -m scripts.bench_perf --json         # JSON 输出
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

from qqbot.domain.errors import ParseError
from qqbot.interfaces.qq.parser import QQCommandParser
from qqbot.nlu import NLU_DATA_DIR, NaiveBayesClassifier, NLUIntentMatcher

ROOT = Path(__file__).resolve().parents[1]
MODEL_PATH = NLU_DATA_DIR / "intent_model.json"

# 极端用例：复杂指代 / 多房间 / 他人 / 星期 / 长句（最坏情况）
EXTREME_CASES: list[tuple[str, str | None]] = [
    ("帮我约一下明天下午3点到5点的303，然后周四晚上8点到9点的304a也帮我约了", None),
    ("明天下午3点去304外面的房间练2h琴", None),
    ("帮我查一下下周二的预约情况和303的占用", None),
    ("取消303和304a明天晚上的预约", None),
    ("帮我看看张三明天的预约，还有李四后天的303", None),
    ("预约304b明天下午", None),
    ("今晚7点约304b", None),
    ("明天上午去304b练琴", None),
    ("帮我约今天和周三303 7-8", None),
    ("看看303琴房和304a明天的预约", None),
]


def _build_cases() -> list[tuple[str, str | None]]:
    from scripts.bench_nlu import BOUNDARY_CASES, EXTRA_CASES, load_seed

    return load_seed() + EXTRA_CASES + BOUNDARY_CASES


def _aliases() -> tuple[str, ...]:
    from scripts.bench_nlu import load_room_aliases

    return load_room_aliases()


def _build_parsers(aliases: tuple[str, ...]) -> tuple[QQCommandParser, QQCommandParser]:
    rule_parser = QQCommandParser(nlu=NLUIntentMatcher(room_aliases=aliases))
    full_parser = QQCommandParser(
        nlu=NLUIntentMatcher(model_path=MODEL_PATH, room_aliases=aliases)
    )
    return rule_parser, full_parser


def _run_once(parser: QQCommandParser, text: str) -> None:
    try:
        parser.parse(text)
    except ParseError:
        pass


def _stats(latencies: list[float]) -> dict[str, float]:
    ordered = sorted(latencies)

    def percentile(p: float) -> float:
        index = min(len(ordered) - 1, int(len(ordered) * p))
        return ordered[index]

    return {
        "avg_ms": statistics.fmean(latencies),
        "p50_ms": percentile(0.5),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
        "max_ms": max(latencies),
        "throughput_per_sec": len(latencies) / sum(latencies) * 1000,
    }


def _bench(parser: QQCommandParser, cases: list[tuple[str, str | None]], iterations: int) -> dict[str, float]:
    # 预热（首次 ML 懒加载不计入）
    for text, _expected in cases[:3]:
        _run_once(parser, text)
    latencies: list[float] = []
    for _ in range(iterations):
        for text, _expected in cases:
            start = time.perf_counter_ns()
            _run_once(parser, text)
            latencies.append((time.perf_counter_ns() - start) / 1_000_000)
    return _stats(latencies)


def _cold_start() -> float:
    """冷启动：全新 matcher 首次命中（模型懒加载 + 规则初始化）。"""
    aliases = _aliases()
    matcher = NLUIntentMatcher(model_path=MODEL_PATH, room_aliases=aliases)
    start = time.perf_counter_ns()
    matcher.match("帮我约303 7点到8点半")
    return (time.perf_counter_ns() - start) / 1_000_000


def _ml_predict_latency() -> dict[str, float]:
    """ML 分类器单独 predict 延迟（意图判定核心开销）。"""
    classifier = NaiveBayesClassifier.load(MODEL_PATH)
    latencies: list[float] = []
    for _ in range(2000):
        start = time.perf_counter_ns()
        classifier.predict("帮我约303 7点到8点半")
        latencies.append((time.perf_counter_ns() - start) / 1_000_000)
    return _stats(latencies)


def render_report(iterations: int) -> str:
    aliases = _aliases()
    rule_parser, full_parser = _build_parsers(aliases)
    cases = _build_cases()
    extreme = EXTREME_CASES

    lines = ["# NLU 完整链路性能报告（真实环境：ML 意图主通道 + 规则槽位）", ""]
    lines.append(f"- 用例：{len(cases)} 条（种子+附加+边界）× 迭代 {iterations} 次")
    lines.append(f"- 模型：{MODEL_PATH.name}（{'存在' if MODEL_PATH.exists() else '缺失→纯规则'}）")
    lines.append("")

    full = _bench(full_parser, cases, iterations)
    lines.append("## 1. 完整链路（规则 + ML）")
    lines.append("")
    lines.append(_format_stats(full))
    lines.append("")

    extreme_stats = _bench(full_parser, extreme, max(iterations, 50))
    lines.append("## 2. 极端用例（复杂指代/多房间/他人/星期，最坏情况）")
    lines.append("")
    lines.append(_format_stats(extreme_stats))
    lines.append("")

    cold = _cold_start()
    lines.append("## 3. 冷启动（重启后首个请求：模型懒加载 + 规则初始化）")
    lines.append("")
    lines.append(f"- {cold:.2f} ms（一次性；之后全部走热路径）")
    lines.append("")

    return "\n".join(lines)


def _format_stats(stats: dict[str, float]) -> str:
    return (
        "| 指标 | 值 |\n"
        "| --- | ---: |\n"
        f"| 平均耗时 | {stats['avg_ms']:.4f} ms |\n"
        f"| P50 | {stats['p50_ms']:.4f} ms |\n"
        f"| P95 | {stats['p95_ms']:.4f} ms |\n"
        f"| P99 | {stats['p99_ms']:.4f} ms |\n"
        f"| 最大耗时 | {stats['max_ms']:.4f} ms |\n"
        f"| 吞吐量 | {stats['throughput_per_sec']:.0f} 条/秒 |\n"
    )


def render_breakdown(iterations: int) -> str:
    aliases = _aliases()
    rule_parser, full_parser = _build_parsers(aliases)
    cases = _build_cases()

    lines = ["# NLU 性能分解（纯规则 / ML 单独 / 完整链路）", ""]
    lines.append(f"- 用例：{len(cases)} 条 × 迭代 {iterations} 次")
    lines.append("")
    lines.append("| 通道 | 平均 | P95 | P99 | 吞吐 |")
    lines.append("| --- | ---: | ---: | ---: | ---: |")

    rule_stats = _bench(rule_parser, cases, iterations)
    full_stats = _bench(full_parser, cases, iterations)
    lines.append(
        f"| 纯规则（退化路径） | {rule_stats['avg_ms']:.4f} | {rule_stats['p95_ms']:.4f} "
        f"| {rule_stats['p99_ms']:.4f} | {rule_stats['throughput_per_sec']:.0f} |"
    )
    lines.append(
        f"| 完整链路（规则+ML） | {full_stats['avg_ms']:.4f} | {full_stats['p95_ms']:.4f} "
        f"| {full_stats['p99_ms']:.4f} | {full_stats['throughput_per_sec']:.0f} |"
    )
    if MODEL_PATH.exists():
        ml_stats = _ml_predict_latency()
        lines.append(
            f"| ML 单独（predict） | {ml_stats['avg_ms']:.4f} | {ml_stats['p95_ms']:.4f} "
            f"| {ml_stats['p99_ms']:.4f} | {ml_stats['throughput_per_sec']:.0f} |"
        )
        overhead = full_stats["avg_ms"] - rule_stats["avg_ms"]
        lines.append("")
        lines.append(
            f"> ML 增量开销：完整链路 - 纯规则 ≈ {overhead:.4f} ms"
            f"  （占完整链路 {overhead / full_stats['avg_ms'] * 100:.0f}%）"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="NLU 完整链路性能压测")
    parser.add_argument("--iter", type=int, default=100, help="每条用例迭代次数（默认 100）")
    parser.add_argument("--breakdown", action="store_true", help="分解对比：纯规则/ML/完整链路")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    if args.breakdown:
        text = render_breakdown(args.iter)
    else:
        text = render_report(args.iter)
    if args.json:
        print(json.dumps({"report": text}, ensure_ascii=False, indent=2))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
