"""NLU 样本收集（docs/NLU-DESIGN.md 5.2）：日志 → data/nlu/samples.jsonl。

数据源：
- logs/qqbot.log（及轮转文件）中的 `nlu_sample` DEBUG 行（client 写入，已脱敏）；
- 输出按 (text, operation) 去重，追加到 data/nlu/samples.jsonl（source=real）。

用法：
    .venv/bin/python -m scripts.collect_samples
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from qqbot.nlu import NLU_DATA_DIR

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"
OUTPUT = NLU_DATA_DIR / "samples.jsonl"

SAMPLE_RE = re.compile(r"nlu_sample text=(?P<text>.+?) operation=(?P<operation>\S+) result=\S+")


def iter_log_lines() -> list[str]:
    lines: list[str] = []
    if not LOG_DIR.exists():
        return lines
    for path in sorted(LOG_DIR.glob("qqbot.log*")):
        try:
            lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            continue
    return lines


def main() -> int:
    output_path = OUTPUT
    output_path.parent.mkdir(parents=True, exist_ok=True)

    seen: set[tuple[str, str]] = set()
    if output_path.exists():
        for line in output_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                sample = json.loads(line)
            except json.JSONDecodeError:
                continue
            seen.add((sample.get("text", ""), sample.get("operation", "")))

    collected = 0
    for line in iter_log_lines():
        match = SAMPLE_RE.search(line)
        if not match:
            continue
        text = match.group("text").strip()
        operation = match.group("operation").strip()
        if not text or (text, operation) in seen:
            continue
        seen.add((text, operation))
        with output_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"text": text, "operation": operation, "source": "real"}, ensure_ascii=False)
                + "\n"
            )
        collected += 1

    print(f"已从日志收集 {collected} 条新样本 → {output_path}（累计 {len(seen)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
