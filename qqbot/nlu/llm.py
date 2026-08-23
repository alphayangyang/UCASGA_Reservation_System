"""外部能力：DeepSeek API 调用、学号脱敏、一致性投票算法（docs/NLU-DESIGN.md 5.4）。

分层（手册 2 节）：本模块属于 infrastructure —— 外部能力实现，
**不依赖任何项目内部层**（不 import domain/application/interfaces）。

编排层（谁调我、怎么校验、产物落盘）在 qqbot/nlu/annotate.py。
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

STUDENT_ID_PATTERN = re.compile(r"\d{4}[A-Z]\d{10}|\d{15}")
DEFAULT_BASE_URL = "https://api.deepseek.com/chat/completions"
# V4 模型（deepseek-chat/deepseek-reasoner 已于 2026-07-24 停用，官方迁移到 v4-flash/v4-pro）：
# 默认 flash（标注任务封闭式意图分类足够，便宜快）；可用 DEEPSEEK_MODEL 覆盖。
DEFAULT_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEFAULT_VOTES = 3  # x：每轮一致性投票调用次数
DEFAULT_MAX_ROUNDS = 5  # y：最大尝试轮数

LLM_SYSTEM_PROMPT = """你是琴房预约机器人的 NLU 标注器。
把用户自然语言请求解析为 JSON，只输出 JSON、不要任何解释。

JSON 格式：
{"operation": "<意图>", "entities": [{"type": "<实体类型>", "text": "<原文片段>"}]}

意图取值（只能选一个）：
- create_reservation  预约
- cancel_reservation  取消
- query_schedule      查询（查琴房占用/安排/谁在用/情况）
- query_free          空闲（问有没有空/空不空/能约吗）
- query_personal      查自己的预约（我的预约/我约的）
- bind_user           绑定姓名学号
- unsupported         复合指令/涉及他人/多日期多房间/无法可靠解析（必须给 reason）

实体类型：
- date      日期（今天/明天/周X/+N 等原文）
- time      时间（7点到8点半/下午3点 等原文）
- duration  时长（2h/两小时 等原文）
- room      房间指代（原样输出，如 304b、304外面的房间）
- name      姓名（bind 用）
- student_id 学号（若已打码成 *** 就原样输出 ***）

规则：
- room 的 text 原样输出，不做归一化（系统不知道别名）；
- 无法确定的实体不要输出；
- 一句话包含多个动作（如「取消A，预约B」）、多个日期（今天和明天）或多个房间（303和304a）、
  或涉及他人姓名（取消张三的预约）时，**不要强行归到单个意图**——
  输出 {"operation": "unsupported",
  "reason": "中文简述（如：复合指令/涉及他人张三/多日期）", "entities": []}；
- 无法判断意图时输出 {"operation": null, "reason": "简短原因（如：闲聊/非预约指令/信息不足）",
  "entities": []}——必须如实返回 null 或 unsupported，不要硬凑一个意图。"""

# LLM 调用器：注入式，便于测试与 dry-run 模拟
LLMCaller = Callable[[str], Awaitable[dict[str, Any] | None]]


def mask_sensitive(text: str) -> str:
    """学号打码（手册红线：禁记完整学号）。"""
    return STUDENT_ID_PATTERN.sub("***", text)


def _normalize_for_compare(result: dict[str, Any]) -> tuple[Any, ...]:
    """规范化比较键：operation + (type, 值) 排序元组（文档 5.4「一致」的定义）。"""
    entities = []
    for entity in result.get("entities", []):
        key = str(entity.get("type", ""))
        value = str(entity.get("normalized") or entity.get("text") or "")
        entities.append((key, value))
    return (result.get("operation"), tuple(sorted(entities)))


async def annotate_with_consensus(
    caller: LLMCaller,
    text: str,
    votes: int = DEFAULT_VOTES,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> dict[str, Any] | None:
    """一致性投票：一轮内 x 次规范化后结果一致 → 返回；y 轮仍不一致 → None。"""
    for _ in range(max_rounds):
        results: list[dict[str, Any]] = []
        for _ in range(votes):
            result = await caller(text)
            if result is None:
                break
            results.append(result)
        if len(results) < votes:
            continue
        baseline = _normalize_for_compare(results[0])
        if all(_normalize_for_compare(item) == baseline for item in results[1:]):
            return results[0]
    return None


async def deepseek_caller(
    session: aiohttp.ClientSession,
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
) -> LLMCaller:
    """真实 DeepSeek API 调用器（仅离线标注使用；key 永不写入日志）。"""

    async def caller(text: str) -> dict[str, Any] | None:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": LLM_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            # V4 思考模式默认打开且 temperature 不生效（官方文档）——一致性投票需要
            # 稳定输出，必须显式关闭思考（{"thinking": {"type": "disabled"}}）。
            "thinking": {"type": "disabled"},
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "max_tokens": 400,
        }
        try:
            async with session.post(
                base_url,
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status != 200:
                    logger.warning("DeepSeek API 返回 %s", response.status)
                    return None
                data = await response.json()
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except (TimeoutError, KeyError, IndexError, json.JSONDecodeError, aiohttp.ClientError):
            logger.exception("DeepSeek 调用解析失败")
            return None
        return parsed

    return caller
