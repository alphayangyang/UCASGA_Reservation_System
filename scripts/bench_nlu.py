"""NLU 本地模拟与性能基准（不联网、不写库、不读真实数据）。

用法：
    .venv/bin/python -m scripts.bench_nlu                 # 完整报告（文本）
    .venv/bin/python -m scripts.bench_nlu --json          # 逐条结果（JSONL，供程序消费）
    .venv/bin/python -m scripts.bench_nlu --force-ml      # 极端测试：全部强制 ML 通道（Phase 2 独立能力）
    .venv/bin/python -m scripts.bench_nlu --compare       # 规则引擎 vs ML 单独 vs 现状 三方对比

输入源：
    1. data/nlu/seed_samples.jsonl（288 条种子样本，真实房间名）
    2. 内置口语化用例（主人验收用例 + 群聊常见表达）
    3. 边界用例（闲聊 / 乱码 / 空输入 / admin 隔离）

报告指标：
    覆盖率（命中/错配/fail-closed）、逐意图分布、耗时分布（min/avg/P50/P95/P99/max）、
    吞吐量（条/秒）、进程峰值内存（RSS）。
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
SEED_PATH = NLU_DATA_DIR / "seed_samples.jsonl"
MODEL_PATH = NLU_DATA_DIR / "intent_model.json"


def load_room_aliases() -> tuple[str, ...]:
    """全部站点房间 name+aliases 并集（与 client.py 注入一致）。

    bench 需要真实别名表才能测中文别名房间（303琴房/玉泉路琴房）的
    gazetteer 白名单命中；只读 configs/ 配置文件，不碰业务数据。
    """
    from qqbot.infrastructure.config import load_all_configs

    configs = load_all_configs(ROOT / "configs", project_root=ROOT)
    return tuple(
        alias
        for config in configs.values()
        for room in config.rooms
        for alias in (room.name, *room.aliases)
    )

# (输入, 期望 operation；None 表示应 fail-closed)
EXTRA_CASES: list[tuple[str, str | None]] = [
    # 主人的验收用例
    ("帮我看看明天琴房情况", "query_schedule"),
    ("看看空闲", "query_free"),
    ("帮我看看后天琴房", "query_schedule"),
    ("查一下我的预约", "query_personal"),
    ("明天下午帮我看看303有没有空", "query_free"),
    ("帮我看看我约了什么", "query_personal"),
    # 群聊常见口语化表达
    # 复杂指代（「304外面的房间」）：房间槽位不拆解（缺省），意图仍判预约——
    # 多房间站点由 Resolver 提示选房（2026-08-23 架构修订：意图与槽位解耦）。
    ("明天下午3点去304外面的房间练2h琴", "create_reservation"),
    ("帮我约一下303 7点到8点半", "create_reservation"),
    ("今晚7点约304b", None),  # 有起点无终点 → fail-closed
    ("把我明天的预约退了", "cancel_reservation"),
    ("取消303今天7-8", "cancel_reservation"),
    ("看看304a明天谁在用", "query_schedule"),
    ("304b下午有空吗", "query_free"),
    ("我是张三 2023X1234567890", "bind_user"),
    # —— P1 白名单化验收（房间泛称 / 口语变体 / 全角输入；意图级）——
    ("帮我预约一下今天下午五点半到七点的琴房", "create_reservation"),
    # 注：「帮俺…」类口语会触发他人检测误判（_OTHER_CLEAN_WORDS 黑名单缺陷，
    # 与房间清洗词同理，留待数据驱动方案），此处用「帮我」聚焦房间泛称。
    ("帮我预约一下琴房明天7点到8点", "create_reservation"),
    ("劳驾帮我约下琴房下午3点到5点", "create_reservation"),
    ("拜托帮我订一下琴房晚上8点到9点", "create_reservation"),
    ("帮我看看303琴房明天有没有空", "query_free"),
    ("帮我约一下３０４b 7点到8点半", "create_reservation"),
    ("帮我看看玉泉路琴房明天有没有空", "query_free"),
    # 边界：应 fail-closed
    ("今天天气不错", None),
    ("哈哈哈", None),
    ("abcdefg 123", None),
    ("帮我开一下空调", None),
]

BOUNDARY_CASES: list[tuple[str, str | None]] = [
    ("#备份用户", "backup_users"),  # admin：走严格正则，NLU 不介入
    ("#清空预约 明天下午帮我看看303", None),  # admin 语法错误：不 fallback 到 NLU
]


def load_seed() -> list[tuple[str, str | None]]:
    if not SEED_PATH.exists():
        return []
    cases: list[tuple[str, str | None]] = []
    for line in SEED_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        sample = json.loads(line)
        cases.append((sample["text"], sample["operation"]))
    return cases


def run_single(parser: QQCommandParser, text: str) -> tuple[str | None, float]:
    start = time.perf_counter_ns()
    try:
        intent = parser.parse(text)
        result: str | None = intent.operation
    except ParseError:
        result = None
    elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000
    return result, elapsed_ms


def bench(parser: QQCommandParser, cases: list[tuple[str, str | None]]) -> dict[str, object]:
    hits = 0
    mismatches: list[tuple[str, str | None, str | None]] = []
    closed = 0
    latencies: list[float] = []
    details: list[dict[str, object]] = []

    for text, expected in cases:
        got, elapsed = run_single(parser, text)
        latencies.append(elapsed)
        details.append({"text": text, "expected": expected, "got": got, "ms": round(elapsed, 4)})
        if got == expected:
            hits += 1
        elif got is None:
            closed += 1
        else:
            mismatches.append((text, expected, got))

    total = len(cases)
    latencies_sorted = sorted(latencies)

    def percentile(p: float) -> float:
        if not latencies_sorted:
            return 0.0
        index = min(len(latencies_sorted) - 1, int(len(latencies_sorted) * p))
        return latencies_sorted[index]

    return {
        "total": total,
        "hits": hits,
        "fail_closed": closed,
        "mismatches": mismatches,
        "hit_rate": hits / total if total else 0.0,
        "latency_ms": {
            "min": min(latencies) if latencies else 0.0,
            "avg": statistics.fmean(latencies) if latencies else 0.0,
            "p50": percentile(0.5),
            "p95": percentile(0.95),
            "p99": percentile(0.99),
            "max": max(latencies) if latencies else 0.0,
        },
        "throughput_per_sec": (total / sum(latencies) * 1000) if latencies else 0.0,
        "details": details,
    }


def peak_rss_kb() -> int:
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmHWM"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def render_report(parser: QQCommandParser) -> str:
    seed = load_seed()
    extra = EXTRA_CASES + BOUNDARY_CASES

    seed_result = bench(parser, seed)
    extra_result = bench(parser, extra)
    combined = bench(parser, seed + extra)

    lines: list[str] = []
    lines.append("# NLU Phase 0 本地模拟与性能报告")
    lines.append("")
    lines.append(f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("- 模型：**零训练规则引擎**（Phase 0，无 ML）")
    lines.append(f"- 峰值 RSS：{peak_rss_kb():,} KB")
    lines.append("")

    lines.append("## 1. 覆盖率")
    lines.append("")
    lines.append("| 数据集 | 条数 | 命中 | fail-closed | 错配 | 命中率 |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
    for name, result in (
        ("种子样本", seed_result),
        ("口语化+边界", extra_result),
        ("合计", combined),
    ):
        lines.append(
            f"| {name} | {result['total']} | {result['hits']} | {result['fail_closed']} "
            f"| {len(result['mismatches'])} | {result['hit_rate']:.1%} |"
        )
    if combined["mismatches"]:
        lines.append("")
        lines.append("### 错配明细（应修 bug）")
        for text, expected, got in combined["mismatches"]:  # type: ignore[union-attr]
            lines.append(f"- `{text}`：期望 {expected}，实际 {got}")
    lines.append("")

    lines.append("## 2. 性能")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("| --- | ---: |")
    latency = combined["latency_ms"]  # type: ignore[union-attr]
    lines.append(f"| 平均耗时 | {latency['avg']:.4f} ms |")
    lines.append(f"| 最小耗时 | {latency['min']:.4f} ms |")
    lines.append(f"| P50 | {latency['p50']:.4f} ms |")
    lines.append(f"| P95 | {latency['p95']:.4f} ms |")
    lines.append(f"| P99 | {latency['p99']:.4f} ms |")
    lines.append(f"| 最大耗时 | {latency['max']:.4f} ms |")
    lines.append(f"| 吞吐量 | {combined['throughput_per_sec']:.0f} 条/秒 |")
    lines.append(f"| 峰值 RSS | {peak_rss_kb():,} KB |")
    lines.append("")

    lines.append("## 3. 逐条输出（种子样本）")
    lines.append("")
    lines.append("| # | 输入 | 期望 | 实际 | 耗时(ms) |")
    lines.append("| --- | --- | --- | --- | ---: |")
    for index, detail in enumerate(seed_result["details"], 1):  # type: ignore[union-attr]
        text = detail["text"]
        display = text if len(text) <= 40 else text[:39] + "…"
        lines.append(
            f"| {index} | {display} | {detail['expected']} | {detail['got'] or 'help'} | {detail['ms']:.3f} |"
        )
    lines.append("")

    lines.append("## 4. 逐条输出（口语化 + 边界用例）")
    lines.append("")
    lines.append("| 输入 | 期望 | 实际 | 耗时(ms) |")
    lines.append("| --- | --- | --- | ---: |")
    for detail in extra_result["details"]:  # type: ignore[union-attr]
        text = detail["text"]
        display = text if len(text) <= 40 else text[:39] + "…"
        lines.append(f"| {display} | {detail['expected']} | {detail['got'] or 'help'} | {detail['ms']:.3f} |")
    lines.append("")
    lines.append("> ✅ 命中　🛡️ 设计内 fail-closed（安全拒绝）　❌ 错配（需修复）")
    lines.append("")
    lines.append(
        f"（逐条统计含 {len(seed_result['details'])} 条种子 + {len(extra_result['details'])} 条附加用例，"
        f"mark 仅示意，未计入 mark 列）"
    )
    return "\n".join(lines)


def bench_ml_only(cases: list[tuple[str, str | None]], model_path: Path) -> dict[str, object]:
    """极端测试：全部输入强制走 ML 通道（意图分类 + 本地槽位构建），跳过规则引擎。

    回答「ML 单独能扛多少」：分类准确率 / 全链路成功率 / 每类 recall。
    """
    if not model_path.exists():
        return {"error": f"模型不存在：{model_path}（先运行 scripts.train_intent）"}
    classifier = NaiveBayesClassifier.load(model_path)
    matcher = NLUIntentMatcher()

    hits = 0
    intent_ok = 0
    slot_failed = 0
    closed = 0
    total = len(cases)
    per_class: dict[str, dict[str, int]] = {}
    details: list[dict[str, object]] = []

    for text, expected in cases:
        predicted = classifier.predict(text)
        if predicted is None:
            got: str | None = None
            closed += 1
            details.append({"text": text, "expected": expected, "got": None, "via": "threshold"})
            continue
        operation, confidence = predicted
        if operation == expected:
            intent_ok += 1
        intent = matcher.build(operation, text, None)
        if intent is None:
            slot_failed += 1
            got = None
            details.append(
                {"text": text, "expected": expected, "got": None, "via": "slot", "class": operation}
            )
        else:
            got = intent.operation
            if got == expected:
                hits += 1
        per_class.setdefault(str(expected), {"total": 0, "hit": 0, "slot_fail": 0})
        info = per_class[str(expected)]
        info["total"] += 1
        if got == expected:
            info["hit"] += 1
        if got is None and predicted is not None:
            info["slot_fail"] += 1
        details.append(
            {
                "text": text,
                "expected": expected,
                "got": got,
                "via": "ml",
                "class": operation,
                "confidence": round(confidence, 3),
            }
        )

    return {
        "total": total,
        "intent_ok": intent_ok,
        "hits": hits,
        "slot_failed": slot_failed,
        "closed": closed,
        "intent_accuracy": intent_ok / total if total else 0.0,
        "full_accuracy": hits / total if total else 0.0,
        "per_class": per_class,
        "details": details,
    }


def render_ml_only_report(seed: list[tuple[str, str | None]], extra: list[tuple[str, str | None]]) -> str:
    result = bench_ml_only(seed + extra, MODEL_PATH)
    if "error" in result:
        return str(result["error"])
    lines = [
        "# NLU 极端测试：全部强制 ML 通道（Phase 2 独立能力）",
        "",
        f"- 模型：{MODEL_PATH.name}",
        f"- 样本：{result['total']} 条（种子 + 口语化/边界）",
        "",
        "## 1. 总体",
        "",
        "| 指标 | 值 |",
        "| --- | ---: |",
        f"| 意图分类准确率 | {result['intent_accuracy']:.1%} |",
        f"| 全链路成功率（分类对 + 槽位构建成功） | {result['full_accuracy']:.1%} |",
        f"| 分类对但槽位失败 | {result['slot_failed']} 条 |",
        f"| 置信度阈值拒绝（fail-closed） | {result['closed']} 条 |",
        "",
        "## 2. 每类",
        "",
        "| 意图 | 支持 | 全链路命中 | 槽位失败 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for cls in sorted(result["per_class"]):  # type: ignore[union-attr]
        info = result["per_class"][cls]  # type: ignore[union-attr]
        lines.append(f"| {cls} | {info['total']} | {info['hit']} | {info['slot_fail']} |")
    lines.append("")
    lines.append(
        "> 注：槽位失败多为「分类为预约但本地抽不出时间」——"
        "> ML 只出意图，槽位规则 fail-closed（文档 6.3 边界）。"
    )
    return "\n".join(lines)


def render_compare_report(
    seed: list[tuple[str, str | None]], extra: list[tuple[str, str | None]], aliases: tuple[str, ...]
) -> str:
    """三方对比：规则引擎单独 / ML 单独 / 现状（规则+ML）。"""
    cases = seed + extra
    # 规则引擎单独（无 ML 模型）：QQCommandParser + NLUIntentMatcher（不含 model_path）
    parser_plain = QQCommandParser(nlu=NLUIntentMatcher(room_aliases=aliases))
    parser_full = QQCommandParser(nlu=NLUIntentMatcher(model_path=MODEL_PATH, room_aliases=aliases))
    ml_result = bench_ml_only(cases, MODEL_PATH)
    if "error" in ml_result:
        return str(ml_result["error"])

    both = rule_only = ml_only = neither = 0
    for text, expected in cases:
        try:
            rule_got = parser_plain.parse(text).operation
        except ParseError:
            rule_got = None
        try:
            full_got = parser_full.parse(text).operation
        except ParseError:
            full_got = None
        rule_ok = rule_got == expected
        full_ok = full_got == expected
        if rule_ok and full_ok:
            both += 1
        elif rule_ok:
            rule_only += 1
        elif full_ok:
            ml_only += 1
        else:
            neither += 1

    lines = [
        "# NLU 三方对比：规则引擎 / ML 单独 / 现状",
        "",
        "| 通道 | 命中 | 比例 |",
        "| --- | ---: | ---: |",
        f"| 规则引擎单独（现状无 ML） | {rule_only + both} | {(rule_only + both) / len(cases):.1%} |",
        f"| ML 单独（强制 Phase 2） | {ml_only + both} | — |",
        f"| 现状（规则 + ML 兜底） | {both + rule_only + ml_only} | "
        f"{(both + rule_only + ml_only) / len(cases):.1%} |",
        "",
        "## 互补性（以期望意图为真值）",
        "",
        "| 组合 | 条数 | 含义 |",
        "| --- | ---: | --- |",
        f"| 规则对 + ML 对 | {both} | 双保险 |",
        f"| 仅规则对 | {rule_only} | ML 兜不住（规则独有能力） |",
        f"| 仅 ML 对 | {ml_only} | ML 兜住了规则失败（**ML 的增量价值**） |",
        f"| 都错/都拒 | {neither} | 超出当前能力（留给夜间标注积累） |",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="NLU 本地模拟与性能基准")
    parser.add_argument("--json", action="store_true", help="输出逐条结果 JSONL")
    parser.add_argument("--force-ml", action="store_true", help="极端测试：全部强制走 ML 通道")
    parser.add_argument("--compare", action="store_true", help="规则引擎 vs ML 单独 vs 现状 三方对比")
    args = parser.parse_args()

    cases = load_seed() + EXTRA_CASES + BOUNDARY_CASES
    aliases = load_room_aliases()
    if args.json:
        for detail in bench(QQCommandParser(nlu=NLUIntentMatcher(room_aliases=aliases)), cases)["details"]:
            print(json.dumps(detail, ensure_ascii=False))
        return 0
    if args.force_ml:
        print(render_ml_only_report(load_seed(), EXTRA_CASES + BOUNDARY_CASES))
        return 0
    if args.compare:
        print(render_compare_report(load_seed(), EXTRA_CASES + BOUNDARY_CASES, aliases))
        return 0
    print(render_report(QQCommandParser(nlu=NLUIntentMatcher(room_aliases=aliases))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
