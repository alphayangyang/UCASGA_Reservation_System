"""夜间 LLM 批处理标注——编排层（docs/NLU-DESIGN.md 5.4）。

依赖方向（手册 2 节）：interfaces → application → domain；interfaces → infrastructure。
- 外部能力（DeepSeek HTTP/脱敏/一致性投票）在 qqbot/nlu/llm.py；
- 本模块负责编排：LLM 结果 → ParsedIntent（qqbot.nlu.matcher）、Resolver 校验（application）、
  批处理主流程与产物落盘。

原则：
- LLM 永不进入实时链路；实时路径保持无状态，学习完全发生在夜间批处理；
- 一致性投票（x 次一致 + y 轮重试）+ Resolver 校验，「x 次一致」+「校验通过」缺一不可；
- 产物只进候选样本库（candidates.jsonl），不直接影响线上行为（安全档）；
- 学号脱敏（qqbot.nlu.llm.mask_sensitive）：手册红线，禁记完整学号。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp

from qqbot.application.resolver import CommandResolver
from qqbot.domain.calendar import SHANGHAI_TZ
from qqbot.domain.errors import AppError
from qqbot.infrastructure.config import SiteConfig
from qqbot.interfaces.qq.parser import ParsedIntent
from qqbot.nlu.llm import LLMCaller, deepseek_caller, mask_sensitive
from qqbot.nlu.matcher import NLUIntentMatcher

logger = logging.getLogger(__name__)

# NLU 插件私有数据目录（插件自包含：代码 + 数据 + 模型一体；不混入 data/ 业务库）
NLU_DATA_DIR = Path(__file__).resolve().parent / "data"

DEFAULT_CONCURRENCY = 3  # 夜间批处理并发上限


def write_pending(data_dir: Path, text: str, bot_id: str) -> None:
    """解析失败的普通输入 → data/nlu/pending/（docs/NLU-DESIGN.md 5.4）。

    NLU 内部语义在此封装（调用方 client 只传文本与站点，不感知细节）：
    - 学号即时打码（mask_sensitive，手册红线）；
    - 复合指令不收集（统一口径不支持，无需夜间标注分析）。
    """
    if NLUIntentMatcher().is_compound(text):
        return
    try:
        pending_dir = data_dir / "pending"
        pending_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "text": mask_sensitive(text),
            "bot_id": bot_id,
            "ts": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
        }
        filename = datetime.now(SHANGHAI_TZ).strftime("%Y%m%d") + ".jsonl"
        with (pending_dir / filename).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("写入 NLU pending 失败（不影响主流程）")


async def run_nightly_job(data_dir: Path, configs: dict[str, SiteConfig], api_key: str) -> None:
    """夜间 LLM 标注任务入口（scheduler 挂载用）。

    失败只记录日志，绝不影响 bot 主链路（与手册 4.4 归档清理同一模式）。
    标注完成后按独立开关（任一站点 features 开启即全局生效）执行自优化：
    - nlu_auto_optimize：白名单自优化（房间别名 + 闲聊词，scripts/optimize_whitelist.py）
    - nlu_auto_retrain：ML 意图模型自动重训（影子验证 + 原子替换，train_intent --auto）
    两个开关互相独立、各自 try 隔离（一个失败不影响另一个与主链路）。
    """
    try:
        async with aiohttp.ClientSession() as session:
            caller = await deepseek_caller(session, api_key)
            await run_nightly_annotate(data_dir, configs, caller)
    except Exception:
        logger.exception("夜间 NLU 标注任务失败")
        return

    if any(config.features.nlu_auto_optimize for config in configs.values()):
        try:
            from qqbot.nlu.optimize import run_auto_optimize

            report = run_auto_optimize(data_dir, configs)
            applied = len(report["manual_applied"]) + len(report["auto_applied"])
            rejected = len(report["manual_rejected"]) + len(report["auto_rejected"])
            logger.info("夜间白名单自优化：应用 %s 条，拒绝 %s 条，闲聊词 %s 条",
                        applied, rejected, len(report["chitchat_added"]))
        except Exception:
            logger.exception("夜间白名单自优化失败（不影响主链路）")

    if any(config.features.nlu_auto_retrain for config in configs.values()):
        try:
            import asyncio

            from scripts.train_intent import run_auto_retrain

            report = await asyncio.to_thread(run_auto_retrain)  # 训练数秒，不阻塞事件循环
            if report.get("applied"):
                logger.info(
                    "夜间 ML 自动重训完成：基线 %s → %s（%s 条用例，样本基线 %s 条）",
                    report["old_hits"], report["new_hits"],
                    report["total_cases"], report["trained_candidates"],
                )
            else:
                logger.info("夜间 ML 自动重训跳过：%s", report.get("skipped"))
        except Exception:
            logger.exception("夜间 ML 自动重训失败（不影响主链路）")


VALID_OPERATIONS = frozenset(
    {
        "create_reservation",
        "cancel_reservation",
        "query_schedule",
        "query_free",
        "query_personal",
        "bind_user",
    }
)


def build_intent_from_llm(operation: str, text: str, room_text: str | None) -> ParsedIntent | None:
    """LLM 意图 + 本地实体解析组装 ParsedIntent（归一化永远在本地）。

    复用规则引擎的实体抽取与槽位验证；预约无时间等 fail-closed 行为一致。
    """
    if operation not in VALID_OPERATIONS:
        return None
    return NLUIntentMatcher().build(operation, text, room_text)


def validate_with_resolver(intent: ParsedIntent, config: SiteConfig, now: datetime) -> bool:
    """文档 5.4 第 4 步：解析结果必须通过现有 CommandResolver 验证。"""
    try:
        CommandResolver().resolve(intent, config, now)
        return True
    except AppError:
        return False


def _load_pending(pending_dir: Path) -> list[dict[str, Any]]:
    if not pending_dir.exists():
        return []
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for path in sorted(pending_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (str(entry.get("text", "")), str(entry.get("bot_id", "")))
            if key in seen:
                continue
            seen.add(key)
            entries.append(entry)
    return entries


@dataclass
class NightlyReport:
    """夜间批处理结果（供日志 / 日报 / 测试断言）。"""

    date: str = ""
    pending: int = 0
    consensus_failed: int = 0
    slot_failed: int = 0
    resolver_failed: int = 0
    accepted: int = 0
    anomalies: list[dict[str, Any]] = field(default_factory=list)
    samples: list[dict[str, Any]] = field(default_factory=list)


def _room_text_from_entities(result: dict[str, Any]) -> str | None:
    for entity in result.get("entities", []):
        if entity.get("type") == "room" and entity.get("text"):
            return str(entity["text"])
    return None


async def run_nightly_annotate(
    data_dir: Path,
    configs: dict[str, SiteConfig],
    caller: LLMCaller,
    *,
    votes: int = 3,
    max_rounds: int = 5,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> NightlyReport:
    """夜间批处理主流程：pending → 去重 → 脱敏 → 一致性投票 → 校验 → 候选库/异常/日报。"""
    from qqbot.nlu.llm import annotate_with_consensus

    pending_dir = data_dir / "pending"
    candidates_path = data_dir / "candidates.jsonl"
    anomalies_path = data_dir / "anomalies.jsonl"
    reports_dir = data_dir / "reports"

    entries = _load_pending(pending_dir)
    report = NightlyReport(date=datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d"), pending=len(entries))
    if not entries:
        return report

    async def process(entry: dict[str, Any]) -> None:
        text = mask_sensitive(str(entry.get("text", "")).strip())
        if not text:
            return
        bot_id = str(entry.get("bot_id", ""))
        result = await annotate_with_consensus(caller, text, votes=votes, max_rounds=max_rounds)
        if result is None:
            report.consensus_failed += 1
            report.anomalies.append({"text": text, "bot_id": bot_id, "reason": "consensus"})
            return
        operation = result.get("operation")
        if operation is None:
            report.consensus_failed += 1
            report.anomalies.append(
                {
                    "text": text,
                    "bot_id": bot_id,
                    "reason": "no_operation",
                    "llm_reason": result.get("reason"),
                }
            )
            return
        if operation == "unsupported":
            # LLM 明确判为无法支持（复合/他人/多日期多房间）→ 直接进候选库（训练 unsupported 类），
            # 不做槽位构建/Resolver 校验（本就无可校验的意图）。
            report.accepted += 1
            report.samples.append(
                {
                    "text": text,
                    "operation": "unsupported",
                    "arguments": {"reason": result.get("reason")},
                    "source": "llm",
                    "bot_id": bot_id,
                }
            )
            return
        intent = build_intent_from_llm(str(operation), text, _room_text_from_entities(result))
        if intent is None:
            report.slot_failed += 1
            report.anomalies.append(
                {"text": text, "bot_id": bot_id, "reason": "slot", "operation": operation}
            )
            return
        config = configs.get(bot_id)
        if config is None:
            report.resolver_failed += 1
            report.anomalies.append({"text": text, "bot_id": bot_id, "reason": "unknown_site"})
            return
        now = datetime.now(SHANGHAI_TZ)
        if not validate_with_resolver(intent, config, now):
            report.resolver_failed += 1
            report.anomalies.append(
                {
                    "text": text,
                    "bot_id": bot_id,
                    "reason": "resolver",
                    "operation": operation,
                    # 房间指代原文（供白名单自优化做相似度建议，见 qqbot/nlu/optimize.py）
                    "room_reference": intent.arguments.get("room_reference"),
                }
            )
            return
        report.accepted += 1
        report.samples.append(
            {
                "text": text,
                "operation": intent.operation,
                "arguments": intent.arguments,
                "source": "llm",
                "bot_id": bot_id,
            }
        )

    semaphore = asyncio.Semaphore(concurrency)

    async def limited(entry: dict[str, Any]) -> None:
        async with semaphore:
            await process(entry)

    await asyncio.gather(*(limited(entry) for entry in entries))

    # 写产物（先写文件，成功后才清空 pending）
    pending_dir.mkdir(parents=True, exist_ok=True)
    candidates_path.parent.mkdir(parents=True, exist_ok=True)
    with candidates_path.open("a", encoding="utf-8") as handle:
        for sample in report.samples:
            handle.write(json.dumps(sample, ensure_ascii=False) + "\n")
    with anomalies_path.open("a", encoding="utf-8") as handle:
        for anomaly in report.anomalies:
            handle.write(json.dumps(anomaly, ensure_ascii=False) + "\n")
    _write_daily_report(report, reports_dir)

    for path in pending_dir.glob("*.jsonl"):
        path.unlink()
    return report


def _write_daily_report(report: NightlyReport, reports_dir: Path) -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
    consensus_rate = (
        f"- 一致性通过率：{(report.pending - report.consensus_failed) / report.pending:.0%}"
        if report.pending
        else "- 一致性通过率：-"
    )
    lines = [
        f"# NLU 夜班标注日报 {report.date}",
        "",
        f"- pending 数量：{report.pending}",
        consensus_rate,
        f"- 槽位失败：{report.slot_failed}",
        f"- Resolver 校验失败：{report.resolver_failed}",
        f"- 异常数据：{len(report.anomalies)}（详见 anomalies.jsonl）",
        f"- 候选样本入库：{report.accepted}",
        "",
        "## 示例",
        "",
    ]
    for sample in report.samples[:5]:
        lines.append(f"- `{sample['text']}` → {sample['operation']} {sample['arguments']}")
    for anomaly in report.anomalies[:5]:
        lines.append(f"- ⚠️ `{anomaly['text']}` → {anomaly.get('reason')} ({anomaly.get('operation', '')})")
    lines.append("")
    (reports_dir / f"{report.date}.md").write_text("\n".join(lines), encoding="utf-8")
