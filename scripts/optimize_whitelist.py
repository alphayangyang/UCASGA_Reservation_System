"""白名单自优化：手动一键入口（docs/NLU-DESIGN.md 5.4 自动档 + 5.5 护栏）。

用法：
    .venv/bin/python -m scripts.optimize_whitelist                  # 建议模式（默认）
    .venv/bin/python -m scripts.optimize_whitelist --apply          # 应用人工标注 + 高置信建议
    .venv/bin/python -m scripts.optimize_whitelist --dry-run        # 只预览将要应用的
    .venv/bin/python -m scripts.optimize_whitelist --auto           # 自动模式（夜间任务同款）

数据流：
    人工标注池 qqbot/nlu/data/manual_samples.json（手动新标注存这里）
        + anomalies.jsonl 高频新表达（相似度 ≥0.6 且频次 ≥3 才建议）
        → 校验（room_id 存在 / 别名不冲突 / 非纯数字）
        → 写入 qqbot/nlu/data/room_whitelist.json（v2，原子写，幂等）
        → 重启/夜间任务生效（client 合并进 gazetteer 与 Resolver aliases）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from qqbot.infrastructure.config import load_all_configs
from qqbot.nlu import NLU_DATA_DIR
from qqbot.nlu.optimize import (
    MANUAL_SAMPLES_PATH,
    collect_suggestions,
    extract_chitchat_keywords,
    load_chitchat_keywords,
    load_manual_samples,
    run_auto_optimize,
)

ROOT = Path(__file__).resolve().parents[1]


def _load_configs() -> dict[str, object]:
    return load_all_configs(ROOT / "configs", project_root=ROOT)


def render_suggestions(configs) -> str:
    """建议模式：人工标注池待应用 + anomalies 高频新表达 + 闲聊词候选。"""
    lines = ["# NLU 白名单自优化建议", ""]
    manual = load_manual_samples()
    lines.append(f"## 1. 人工标注池待应用（{MANUAL_SAMPLES_PATH.name}）：{len(manual)} 条")
    if manual:
        lines.append("")
        lines.append("| 站点 | 别名 | room_id | 备注 |")
        lines.append("| --- | --- | --- | --- |")
        for entry in manual:
            lines.append(f"| {entry['bot_id']} | {entry['alias']} | {entry['room_id']} | {entry['note']} |")
    suggestions = collect_suggestions(configs, NLU_DATA_DIR / "anomalies.jsonl")
    lines.append("")
    lines.append(f"## 2. 高频新表达建议（频次 ≥3 且相似度 ≥0.6）：{len(suggestions)} 条")
    if suggestions:
        lines.append("")
        lines.append("| 站点 | 用户表达 | 建议 room_id | 相似命中 | 相似度 | 频次 |")
        lines.append("| --- | --- | --- | --- | ---: | ---: |")
        for s in suggestions:
            lines.append(
                f"| {s['bot_id']} | {s['alias']} | {s['room_id']} | {s['matched']} "
                f"| {s['similarity']:.2f} | {s['occurrences']} |"
            )
    chitchat_candidates = extract_chitchat_keywords(NLU_DATA_DIR / "anomalies.jsonl")
    existing = set(load_chitchat_keywords())
    lines.append("")
    lines.append(f"## 3. 闲聊词候选（no_operation 提炼，频次 ≥2）：{len(chitchat_candidates)} 条")
    if chitchat_candidates:
        lines.append("")
        for word in chitchat_candidates:
            mark = "（已存在）" if word in existing else ""
            lines.append(f"- `{word}` {mark}")
    lines.append("")
    lines.append("> 应用：python -m scripts.optimize_whitelist --apply（校验不通过自动拒绝）")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="NLU 白名单自优化（手动入口）")
    parser.add_argument(
        "--apply", action="store_true", help="应用人工标注 + 高置信建议 → room_whitelist.json"
    )
    parser.add_argument("--dry-run", action="store_true", help="只预览将要应用的，不写文件")
    parser.add_argument("--auto", action="store_true", help="自动模式（夜间任务同款：应用 + 报告）")
    args = parser.parse_args()

    configs = _load_configs()

    if args.auto:
        report = run_auto_optimize(NLU_DATA_DIR, configs)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.apply:
        entries = [
            {"bot_id": e["bot_id"], "alias": e["alias"], "room_id": e["room_id"]}
            for e in load_manual_samples()
        ]
        suggestions = collect_suggestions(configs, NLU_DATA_DIR / "anomalies.jsonl")
        entries += [
            {"bot_id": str(s["bot_id"]), "alias": str(s["alias"]), "room_id": str(s["room_id"])}
            for s in suggestions
        ]
        chitchat = extract_chitchat_keywords(NLU_DATA_DIR / "anomalies.jsonl")
        if not entries and not chitchat:
            print("没有可应用的条目（人工标注池为空，anomalies 无高置信建议/闲聊候选）")
            return 0
        if args.dry_run:
            print("== 将要应用 ==")
            for e in entries:
                print(f"  [房间] [{e['bot_id']}] {e['alias']} → {e['room_id']}")
            for word in chitchat:
                print(f"  [闲聊] `{word}`")
            return 0
        from qqbot.nlu.optimize import apply_aliases, apply_chitchat_keywords, clear_manual_samples

        applied, rejected = apply_aliases(configs, entries, source="manual+auto")
        for item in applied:
            print(f"✅ [房间] [{item['bot_id']}] {item['alias']} → {item['room_id']}")
        for item in rejected:
            print(f"❌ [房间] [{item['bot_id']}] {item['alias']} → {item['room_id']}（{item['reason']}）")
        chitchat_added, _ = apply_chitchat_keywords(chitchat)
        for word in chitchat_added:
            print(f"✅ [闲聊] `{word}`")
        if applied:
            clear_manual_samples()  # 人工池已应用 → 清空
        print(
            f"\n已应用房间 {len(applied)} 条（拒绝 {len(rejected)} 条）+ 闲聊词 {len(chitchat_added)} 条；"
            "重启或夜间任务生效"
        )
        return 0

    print(render_suggestions(configs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
