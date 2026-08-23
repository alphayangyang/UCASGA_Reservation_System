"""白名单自优化（P2/P3 落地，docs/NLU-DESIGN.md 5.4 自动档 + 5.5 护栏）。

数据流：
- 人工标注池 `qqbot/nlu/data/manual_samples.json`（主人 2026-08-23 要求：手动新标注
  数据存 JSON）——「alias → room_id」映射的权威来源；
- 夜间 LLM 标注的 `anomalies.jsonl`（reason=resolver，带 room_reference）——
  新表达候选；与配置 name/aliases 做字符相似度（difflib，零依赖），
  频次 ≥ MIN_OCCURRENCE 且相似度 ≥ SIMILARITY_THRESHOLD 才进入建议；
- 产出写入 `qqbot/nlu/data/room_whitelist.json`（v2：extra_aliases 按站点存
  {"alias": "room_id"}）——client 启动时合并进 matcher gazetteer 与 Resolver aliases。

护栏（写入前全部校验）：room_id 必须存在于配置；alias 不与配置现有 name/aliases
冲突（属于其他房间 → 拒绝）；非空、非纯数字（数字由 ROOT_TOKEN 兜底）；
写入原子化（tmp + rename）；幂等（已存在则跳过）。

模式：手动 `scripts/optimize_whitelist.py`（建议/--apply/--auto）；
自动：夜间任务（annotate.run_nightly_job）末尾调用 `run_auto_optimize`。
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from qqbot.infrastructure.config import SiteConfig
from qqbot.nlu import NLU_DATA_DIR

logger = logging.getLogger(__name__)

WHITELIST_PATH = NLU_DATA_DIR / "room_whitelist.json"
MANUAL_SAMPLES_PATH = NLU_DATA_DIR / "manual_samples.json"
CHITCHAT_KEYWORDS_PATH = NLU_DATA_DIR / "chitchat_keywords.json"
WHITELIST_VERSION = 2
SIMILARITY_THRESHOLD = 0.6  # 新表达 → 配置别名的最低相似度（字符级，difflib）
MIN_OCCURRENCE = 3  # 自动建议的频次门槛（同一新表达出现 ≥N 次才建议）


# —— 数据加载/保存 ——


def _default_whitelist() -> dict[str, object]:
    return {"version": WHITELIST_VERSION, "extra_aliases": {}}


def load_whitelist() -> dict[str, object]:
    """读取 room_whitelist.json（v2）；缺失/损坏 → 返回默认空结构。"""
    try:
        data = json.loads(WHITELIST_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("extra_aliases"), dict):
            return data
    except (OSError, ValueError):
        logger.warning("白名单文件不可读（%s），按空处理", WHITELIST_PATH)
    return _default_whitelist()


def save_whitelist(data: dict[str, object]) -> None:
    """原子写入 room_whitelist.json（tmp + rename，训练/写入中途断电不留半文件）。"""
    WHITELIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = WHITELIST_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(WHITELIST_PATH)


def load_manual_samples() -> list[dict[str, str]]:
    """读取人工标注池 manual_samples.json → [{bot_id, alias, room_id, note}]。"""
    try:
        data = json.loads(MANUAL_SAMPLES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    samples: list[dict[str, str]] = []
    for bot_id, entries in (data.get("room_aliases") or {}).items():
        for entry in entries or []:
            samples.append(
                {
                    "bot_id": str(bot_id),
                    "alias": str(entry.get("alias", "")).strip(),
                    "room_id": str(entry.get("room_id", "")).strip(),
                    "note": str(entry.get("note", "")).strip(),
                }
            )
    return samples


def clear_manual_samples() -> None:
    """应用成功后清空人工标注池（已进白名单，保留会造成重复应用）。"""
    MANUAL_SAMPLES_PATH.write_text(
        json.dumps({"version": 1, "comment": "人工标注池（已应用后清空）", "room_aliases": {}},
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# —— 校验（写入护栏）——


def validate_alias(config: SiteConfig, alias: str, room_id: str) -> str | None:
    """校验一条新别名；合法返回 None，否则返回拒绝原因。"""
    if not alias:
        return "空别名"
    if not any(ch.isalnum() for ch in alias):
        return "无有效字符"
    if alias.isdigit():
        return "纯数字由数字模式兜底，无需进白名单"
    if len(alias) > 24:
        return "别名过长"
    target = next((room for room in config.rooms if room.id == room_id), None)
    if target is None:
        return f"room_id 不存在：{room_id}"
    # 冲突检测：alias 已是其他房间的 name/aliases → 拒绝（防止跨房间污染）
    for room in config.rooms:
        if room.id == room_id:
            continue
        if alias.casefold() in {item.casefold() for item in room.all_references()}:
            return f"与房间 {room.id} 的现有别名冲突"
    return None


# —— 相似度建议（anomalies → 建议映射）——


def _all_references(config: SiteConfig) -> list[tuple[str, str]]:
    """配置全部房间的 (room_id, name/aliases 原文)。"""
    refs: list[tuple[str, str]] = []
    for room in config.rooms:
        for item in room.all_references():
            refs.append((room.id, item))
    return refs


def suggest_room_mapping(
    config: SiteConfig, reference: str, threshold: float = SIMILARITY_THRESHOLD
) -> tuple[str, str, float] | None:
    """新表达 → 建议映射 (room_id, 命中的配置别名, 相似度)；无高置信候选 → None。

    字符级相似度（difflib，零依赖）——「304外面的房间」≈「304外面」→ 建议 304b。
    最高分必须唯一（防止「303」同时像 303/304 时硬猜）。
    """
    candidates: list[tuple[str, str, float]] = []
    for room_id, item in _all_references(config):
        ratio = SequenceMatcher(None, reference, item).ratio()
        if ratio >= threshold:
            candidates.append((room_id, item, ratio))
    if not candidates:
        return None
    candidates.sort(key=lambda entry: entry[2], reverse=True)
    if len(candidates) >= 2 and candidates[0][2] == candidates[1][2]:
        return None  # 并列最高 → 不硬猜（fail-closed）
    return candidates[0]


def collect_suggestions(
    configs: dict[str, SiteConfig], anomalies_path: Path, min_occurrence: int = MIN_OCCURRENCE
) -> list[dict[str, object]]:
    """扫描 anomalies.jsonl（reason=resolver 且带 room_reference）→ 建议清单。

    频次门槛防偶发表达污染；相似度门槛防硬猜；并列最高不猜。
    """
    counts: dict[tuple[str, str], int] = {}
    try:
        lines = anomalies_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("reason") != "resolver":
            continue
        bot_id = str(entry.get("bot_id", ""))
        reference = str(entry.get("room_reference", "")).strip()
        if not reference:
            continue
        counts[(bot_id, reference)] = counts.get((bot_id, reference), 0) + 1

    suggestions: list[dict[str, object]] = []
    for (bot_id, reference), count in sorted(counts.items(), key=lambda item: item[1], reverse=True):
        if count < min_occurrence:
            continue
        config = configs.get(bot_id)
        if config is None:
            continue
        mapping = suggest_room_mapping(config, reference)
        if mapping is None:
            continue
        room_id, matched, ratio = mapping
        suggestions.append(
            {
                "bot_id": bot_id,
                "alias": reference,
                "room_id": room_id,
                "matched": matched,
                "similarity": round(ratio, 3),
                "occurrences": count,
            }
        )
    return suggestions


# —— 应用（写入白名单）——


def apply_aliases(
    configs: dict[str, SiteConfig],
    entries: list[dict[str, str]],
    *,
    source: str,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """把 (bot_id, alias, room_id) 条目应用进 room_whitelist.json。

    返回 (applied, rejected)；幂等（已存在跳过）；原子写入。
    """
    whitelist = load_whitelist()
    extra = whitelist.setdefault("extra_aliases", {})  # type: ignore[union-attr]
    applied: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    for entry in entries:
        bot_id, alias, room_id = entry["bot_id"], entry["alias"], entry["room_id"]
        config = configs.get(bot_id)
        if config is None:
            rejected.append({**entry, "reason": f"站点不存在：{bot_id}"})
            continue
        reason = validate_alias(config, alias, room_id)
        if reason is not None:
            rejected.append({**entry, "reason": reason})
            continue
        site_extra = extra.setdefault(bot_id, {})  # type: ignore[union-attr]
        if alias in site_extra:
            continue  # 幂等：已存在
        site_extra[alias] = room_id  # type: ignore[index]
        applied.append({**entry, "source": source})
    if applied:
        save_whitelist(whitelist)
    return applied, rejected


def run_auto_optimize(data_dir: Path, configs: dict[str, SiteConfig]) -> dict[str, object]:
    """自动模式（夜间任务调用）：应用人工标注池 + 高置信建议 + 提炼闲聊词。

    护栏：room_id 校验 / 别名冲突检测 / 频次门槛 / 相似度门槛 / 原子写 / 幂等。
    闲聊词提炼只影响 fail-closed 文案区分（is_chitchat），误加词低风险。
    返回报告（供日志/日报）。任何异常不影响调用方（调用方已 try 包裹）。
    """
    report: dict[str, object] = {
        "manual_applied": [],
        "manual_rejected": [],
        "auto_applied": [],
        "chitchat_added": [],
    }
    manual = load_manual_samples()
    if manual:
        applied, rejected = apply_aliases(configs, manual, source="manual")
        report["manual_applied"] = applied
        report["manual_rejected"] = rejected
        if applied:
            clear_manual_samples()
            logger.info("白名单自优化：应用人工标注 %s 条（拒绝 %s 条）", len(applied), len(rejected))
    suggestions = collect_suggestions(configs, data_dir / "anomalies.jsonl")
    if suggestions:
        applied, rejected = apply_aliases(
            configs,
            [{"bot_id": s["bot_id"], "alias": s["alias"], "room_id": s["room_id"]} for s in suggestions],
            source="auto",
        )
        report["auto_applied"] = applied
        report["auto_rejected"] = rejected
        if applied:
            logger.info("白名单自优化：自动应用建议 %s 条", len(applied))
    added, _rejected = apply_chitchat_keywords(extract_chitchat_keywords(data_dir / "anomalies.jsonl"))
    report["chitchat_added"] = added
    if added:
        logger.info("白名单自优化：提炼闲聊关键词 %s 条", len(added))
    return report


# —— 闲聊关键词生长（chitchat，第二类别）——
# 数据源：anomalies.jsonl 的 no_operation 样本（LLM 判 null：闲聊/非指令/信息不足）。
# 提炼：清洗业务成分（时间/星期/房间/业务动词/助词）→ 2-4 字 n-gram 词频 →
#       频次 ≥ 门槛 → 写入 chitchat_keywords.json。误加词低风险：is_chitchat
#       只在 NLU 全部通道失败后影响文案区分（俏皮话 vs 听不懂），不影响解析。


def load_chitchat_keywords() -> tuple[str, ...]:
    """读取 chitchat_keywords.json（生长产物）；缺失/损坏 → 空。"""
    try:
        data = json.loads(CHITCHAT_KEYWORDS_PATH.read_text(encoding="utf-8"))
        keywords = data.get("keywords", [])
        if isinstance(keywords, list):
            return tuple(str(k).strip() for k in keywords if str(k).strip())
    except (OSError, ValueError):
        logger.warning("闲聊词表文件不可读（%s），按空处理", CHITCHAT_KEYWORDS_PATH)
    return ()


def save_chitchat_keywords(keywords: list[str]) -> None:
    CHITCHAT_KEYWORDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CHITCHAT_KEYWORDS_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"version": 1, "keywords": sorted(set(keywords))}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(CHITCHAT_KEYWORDS_PATH)


# 业务噪声成分（清洗 no_operation 样本时剥离）：时间/星期/房间/业务动词/助词
_BUSINESS_NOISE = (
    "今天", "明天", "后天", "大后天", "昨晚", "今晚", "明晚", "早上", "上午", "中午",
    "下午", "晚上", "傍晚", "凌晨", "点", "半", "小时", "分钟", "周", "星期", "礼拜",
    "预约", "预定", "预订", "取消", "退了", "退掉", "查询", "查看", "看看", "查查",
    "查一下", "绑定", "练琴", "练", "弹", "用", "去", "在", "帮我", "麻烦", "拜托",
    "我想", "我要", "可以", "能不能", "能", "吗", "啊", "吧", "呢", "呀", "哦", "嘛",
    "了", "的", "一", "个", "下", "两", "这", "那", "和", "与", "都", "就", "有",
    "什么", "哪些", "几", "房间", "琴房", "排练室", "明天下午", "今天晚上",
    "外面", "旁边", "附近", "对面", "隔壁", "里面", "楼上", "楼下", "琴", "想", "要",
)
_NOISE_RE = re.compile("|".join(re.escape(word) for word in _BUSINESS_NOISE))


def _clean_no_operation_text(text: str) -> str:
    """清洗 no_operation 样本：剥离业务噪声 → 剩余闲聊核心片段。"""
    cleaned = _NOISE_RE.sub("", text)
    # 数字/字母/打码星号/标点残留（房间号、时间数字、学号打码 ***）
    cleaned = re.sub(r"[0-9A-Za-z*，。！？!?、\s]+", "", cleaned)
    return cleaned


def extract_chitchat_keywords(
    anomalies_path: Path, min_occurrence: int = 2, max_keywords: int = 20
) -> list[str]:
    """从 no_operation 样本提炼候选闲聊关键词（2-4 字 n-gram，频次门槛）。

    llm_reason 含「闲聊」的样本优先（LLM 明示闲聊）；无 llm_reason 的历史样本
    也参与（业务噪声剥离后残留的核心片段，如「天气不错」）。
    """
    texts: list[str] = []
    try:
        lines = anomalies_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("reason") != "no_operation":
            continue
        text = str(entry.get("text", "")).strip()
        if not text:
            continue
        if "***" in text:
            # 学号打码残留（如「我是张三 2023X***」）→ 绑定类样本，非闲聊，跳过
            continue
        reason = str(entry.get("llm_reason") or "")
        texts.append(text if "闲聊" in reason else _clean_no_operation_text(text))

    counter: Counter[str] = Counter()
    for text in texts:
        cleaned = _clean_no_operation_text(text)
        if not cleaned:
            continue
        for size in (4, 3, 2):
            for i in range(0, max(0, len(cleaned) - size + 1)):
                gram = cleaned[i : i + size]
                counter[gram] += 1

    existing = set(load_chitchat_keywords())
    candidates: list[str] = []
    for gram, count in counter.most_common(max_keywords * 3):
        if count < min_occurrence:
            break
        if gram in existing or not gram.isprintable():
            continue
        # 阈值：至少 2 次（max_keywords 控制总量）
        candidates.append(gram)
        if len(candidates) >= max_keywords:
            break
    return candidates


def apply_chitchat_keywords(candidates: list[str]) -> tuple[list[str], list[str]]:
    """把候选闲聊词追加进 chitchat_keywords.json（幂等，原子写）。"""
    if not candidates:
        return [], []
    existing = set(load_chitchat_keywords())
    added = [word for word in candidates if word not in existing]
    if added:
        save_chitchat_keywords([*existing, *added])
    return added, []
