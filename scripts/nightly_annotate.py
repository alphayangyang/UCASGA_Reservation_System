"""夜间 LLM 批处理标注 CLI（docs/NLU-DESIGN.md 5.4）。

用法：
    # 真实 DeepSeek API（需 DEEPSEEK_API_KEY 环境变量）
    .venv/bin/python -m scripts.nightly_annotate

    # 本地模拟（dry-run）：用规则引擎充当“LLM”，验证流程 + 测量本地性能
    .venv/bin/python -m scripts.nightly_annotate --dry-run

流程：data/nlu/pending/ → 去重 → 脱敏 → 一致性投票(x次/y轮) → Resolver 校验
      → candidates.jsonl / anomalies.jsonl / reports/YYYY-MM-DD.md
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import aiohttp

from qqbot.infrastructure.config import load_all_configs
from qqbot.nlu import NLU_DATA_DIR, NLUIntentMatcher, deepseek_caller, run_nightly_annotate

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = NLU_DATA_DIR


async def _fake_caller() -> object:
    """dry-run 假 LLM：规则引擎结果转 LLM 格式（确定性，用于流程验证与本地性能测量）。"""

    async def caller(text: str) -> dict | None:
        intent = NLUIntentMatcher().match(text)
        if intent is None:
            return {"operation": None, "entities": []}
        entities: list[dict] = []
        args = intent.arguments
        if args.get("room_reference"):
            entities.append({"type": "room", "text": str(args["room_reference"])})
        if "start" in args and "end" in args:
            entities.append({"type": "time", "text": f"{args['start']}-{args['end']}"})
        if args.get("offset"):
            entities.append({"type": "date", "text": f"+{args['offset']}"})
        return {"operation": intent.operation, "entities": entities}

    return caller


async def run(dry_run: bool) -> int:
    configs = load_all_configs(ROOT / "configs", project_root=ROOT)
    start = time.perf_counter()
    if dry_run:
        caller = await _fake_caller()
        report = await run_nightly_annotate(DATA_DIR, configs, caller)  # type: ignore[arg-type]
    else:
        import os

        api_key = os.getenv("DEEPSEEK_API_KEY", "")
        if not api_key:
            print("缺少 DEEPSEEK_API_KEY 环境变量（或使用 --dry-run 本地模拟）")
            return 1
        async with aiohttp.ClientSession() as session:
            caller = await deepseek_caller(session, api_key)
            report = await run_nightly_annotate(DATA_DIR, configs, caller)
    elapsed_ms = (time.perf_counter() - start) * 1000

    print(
        f"pending={report.pending} 一致失败={report.consensus_failed} "
        f"槽位失败={report.slot_failed} Resolver失败={report.resolver_failed} "
        f"入库={report.accepted} 异常={len(report.anomalies)}"
    )
    if dry_run:
        print(f"本地处理耗时：{elapsed_ms:.1f} ms（假 LLM，不含网络；真实场景主要耗时在 DeepSeek API 往返）")
    if report.accepted:
        print("日报 →", DATA_DIR / "reports" / f"{report.date}.md")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="夜间 LLM 批处理标注")
    parser.add_argument("--dry-run", action="store_true", help="本地模拟（规则引擎充当 LLM，不联网）")
    args = parser.parse_args()
    return asyncio.run(run(args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
