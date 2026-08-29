"""轻量 NLU 规则引擎（Phase 0，docs/NLU-DESIGN.md）。

属于可插拔的 qqbot.nlu 子包（插件形态）：

只做「文本 → ParsedIntent」的纯词法转换，约束与 QQCommandParser 相同：
- 不读数据库、不读当前时间、不判断权限；
- 输出与 QQCommandParser 完全一致的 arguments 键，CommandResolver 零改动；
- 任何无法可靠理解的情况返回 None（fail-closed），由调用方回退 help。

设计文档：docs/NLU-DESIGN.md（第 3 节 Phase 0、第 4 节防幻觉）。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from qqbot.interfaces.qq.parser import ParsedIntent
from qqbot.nlu.classifier import NaiveBayesClassifier

# —— 分数与阈值（文档 4.1：fail-closed）——
SCORE_TEMPLATE = 1.0  # 句式模板精确命中
MIN_KEYWORD_SCORE = 3  # 关键词评分至少 3 分（一个强关键词或组合）
# ML 意图主通道的置信度门槛（docs/NLU-DESIGN.md 4.8/6.4）：
# ML 预测 ≥ 此值才直接采纳为意图（实测 ML 意图准确率 95.6%，阈值 0.7 离线模拟最优）；
# 低于此值 / unsupported / 无模型 → 规则引擎意图兜底（模板 + 关键词），
# 意图判定与槽位抓取解耦：ML 只出意图，槽位永远由本地规则多值提取 + 冲突裁决。
ML_FIRST_THRESHOLD = 0.7

# —— 意图关键词表：命中加分，用于模板未命中时的兜底 ——
INTENT_KEYWORDS: dict[str, tuple[tuple[str, int], ...]] = {
    "create_reservation": (
        ("预约", 3),
        ("约了", 3),
        ("约一下", 3),
        ("预定", 3),
        ("订", 3),
        ("开一下", 3),
        ("约", 2),
        ("练琴", 2),
    ),
    "cancel_reservation": (
        ("取消", 3),
        ("退了", 3),
        ("退掉", 3),
        ("撤销", 3),
        ("不去了", 3),
        ("删掉", 3),
        ("删除", 3),
        ("清掉", 3),
        ("去掉", 3),
    ),
    "query_schedule": (
        ("查询", 3),
        ("查查", 3),
        ("查一下", 3),
        ("有没有人", 3),
        ("有人吗", 3),
        ("谁在用", 3),
        ("谁预约", 3),
        ("使用情况", 3),
        ("都有谁", 3),
        ("有什么安排", 3),
        # 产品语义（文档 3.2）：模糊动作词默认查询——查询信息最全，
        # 空闲/个人只是换一种呈现方式；query_free/query_personal 只在明确词触发。
        ("看看", 3),
        ("看下", 3),
        ("情况", 2),
        ("安排", 2),
        ("查", 1),
    ),
    "query_free": (
        # 明确空闲词权重 4 > 模糊动作词（看看/查查，权重 3）：
        # “看看空闲”应指向空闲，而“帮我看看琴房情况”默认查询
        ("有没有空", 4),
        ("有空吗", 4),
        ("有空没有", 4),
        ("有空么", 4),
        ("空不空", 4),
        ("空闲", 4),
        ("能约吗", 4),
        ("空着", 4),
        ("有空房", 4),
        ("有空房间", 4),
        ("还有位置", 4),
        ("哪个房间", 4),
        ("有位置", 4),
        ("有空", 4),
    ),
    "query_personal": (
        ("我的预约", 3),
        ("预约记录", 3),
        ("查询个人", 3),
        ("约了哪些", 3),
        ("约过的", 3),
        ("有哪些预约", 3),
        ("有什么预约", 3),
        ("几天的预约", 3),
        ("我约了", 3),
        ("我订的", 3),
        ("我约的", 3),
        ("个人预约", 3),
    ),
    "bind_user": (
        ("绑定", 3),
        ("我是", 3),
        ("我叫", 3),
    ),
}

# —— 句式模板：只负责意图判定；房间除「去X练琴」句式外统一整句提取 ——
PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # 预约：…(去|在) <房间> (练|弹|用)…琴（房间夹在动词之间，需模板圈定）
    (
        re.compile(
            r"(?:今天|明天|后天|大后天|今晚|明晚)?\s*"
            r"(?:下午|晚上|上午|早上|中午|傍晚)?\s*\d{1,2}(?:点半?|[:：]\d{2})?\s*"
            r"(?:想|要)?\s*(?:去|在)\s*(?P<room>[^\s，。！？]{1,16}?)\s*(?:练|弹|用)\s*.{0,6}琴"
        ),
        "create_reservation",
    ),
    # 预约：…(约|订|预约|预定|开一下)[一下|一个|了]?
    (
        re.compile(r"(?:帮我|麻烦|我想|帮忙|把)?\s*(?:约|订|预约|预定|开一下)\s*(?:一下|一个|了)?"),
        "create_reservation",
    ),
    # 空闲：…(看看|查查)? …(有没有空|有空吗|空不空|能约吗|有位置吗)
    (
        re.compile(
            r"(?:帮我|麻烦)?\s*(?:看看|看下|查查|查一下)?\s*"
            r".{0,16}?(?:有没有空|有空吗|有空么|有空没有|空不空|能约吗|有位置吗)"
        ),
        "query_free",
    ),
    # 空闲：…哪个房间 (空|空着|有位置)（明确找空房，优先于默认查询）
    (
        re.compile(
            r"(?:帮我|麻烦)?\s*(?:看看|看下|查查|查一下)?\s*(?:今晚|今天|明天|后天)?\s*"
            r"哪个房间\s*(?:空|空着|有位置|没人用)"
        ),
        "query_free",
    ),
    # 绑定：我是/我叫/绑定 + 姓名 + 学号
    (
        re.compile(
            r"(?:我是|我叫|绑定)\s*(?P<name>[\u4e00-\u9fff]{1,10})[\s，,]*"
            r"(?P<student_id>\d{4}[A-Z]\d{10}|\d{15})"
        ),
        "bind_user",
    ),
)

# —— 实体正则 ——
CN_HOUR = r"\d{1,2}|[一二两三四五六七八九十]"
# 语音转文字“数字朗读”的房间引用（三零三/三百零三/三零四B）：
# NLU 只负责提取原文，归一化（→ 303）由配置 aliases + Resolver 完成（文档 5.4.2）。
ROOM_TOKEN_CN = re.compile(
    r"(?<![零一二两三四五六七八九十百])([零一二两三四五六七八九十百]{2,5}[a-zA-Z]?)"
    r"(?![零一二两三四五六七八九十百])(?!\s*点)"
)
ROOM_TOKEN = re.compile(r"(?<![0-9A-Za-z])(\d{2,4}[a-zA-Z]?)(?![0-9A-Za-z])(?!\s*[:：点])")
STUDENT_ID_RE = re.compile(r"(\d{4}[A-Z]\d{10}|\d{15})")
DATE_WORDS: tuple[tuple[str, int], ...] = (
    ("大后天", 3),
    ("后天", 2),
    ("明晚", 1),
    ("明天", 1),
    ("今晚", 0),
    ("今天", 0),
    # 过去日期词（负偏移 → Resolver natural_past 拒绝，绝不静默按今天执行）
    ("昨天", -1),
    ("前天", -2),
)
CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
PERIOD_OFFSET = {"早上": 0, "上午": 0, "中午": 0, "下午": 12, "傍晚": 12, "晚上": 12}

# —— 星期表达（周X/星期X/礼拜X；前缀：无=接下来最近、这/本=本周、下=下周、上=上周）——
# 周一制产品决策（主人 2026-08-23）：周一是每周第一天——今天周日说「下周一」= 明天。
# NLU 只提取星期原语（weekday + week_mode），不读当前时间（手册 10.2）——
# 绝对日期换算由 Resolver 按当前日期完成（与自然日词同架构）。
# (?<!一)：防「查一下周一」的「下」被当作前缀（「一下周一」是动词短语，不是下周）。
WEEKDAY_RE = re.compile(r"(?<!一)(?P<mode>下|这|本|上)?(?P<unit>周|星期|礼拜)(?P<wd>[一二三四五六日天])")
WEEKDAY_MAP = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
_WEEK_MODE_MAP = {"下": "next_week", "这": "this", "本": "this", "上": "prev_week"}


def _extract_weekdays(text: str) -> list[tuple[int, str]]:
    """星期引用 → [(weekday 1-7, mode)]；无前缀 mode="next"（接下来最近的周X）。

    mode 取值：next（最近，含今天）/ this（本周）/ next_week（下周）/ prev_week（上周）。
    """
    hits: list[tuple[int, str]] = []
    for match in WEEKDAY_RE.finditer(text):
        mode = _WEEK_MODE_MAP.get(match.group("mode") or "", "next")
        hits.append((WEEKDAY_MAP[match.group("wd")], mode))
    return hits


TIME_RANGE_RE = re.compile(
    rf"(?P<p1>下午|晚上|上午|早上|中午|傍晚)?\s*(?P<h1>{CN_HOUR})\s*(?:点\s*(?P<hm1>半)?|[:：](?P<m1>\d{{2}}))?\s*"
    rf"(?:到|至|[-~～—－])\s*"
    rf"(?P<p2>下午|晚上|上午|早上|中午|傍晚)?\s*(?P<h2>{CN_HOUR})\s*(?:点\s*(?P<hm2>半)?|[:：](?P<m2>\d{{2}}))?"
)
START_TIME_RE = re.compile(rf"(?P<p>下午|晚上|上午|早上|中午|傍晚)?\s*(?P<h>{CN_HOUR})\s*点\s*(?P<hm>半)?")
DIGIT_RANGE_RE = re.compile(
    r"(?<!\d)(\d{1,2}(?:\.5|[:：]\d{2})?)\s*[-~～—－]\s*(\d{1,2}(?:\.5|[:：]\d{2})?)(?!\d)"
)
DURATION_RE = re.compile(
    r"(?:(?P<num>\d+(?:\.\d+)?)\s*(?:h|H|小时|个小时))|(?:(?P<cn>一|两|二|三|四|五|六|七|八|九|十)\s*个?小时)"
)
HALF_HOUR_CN = re.compile(r"一个半小时")

# —— 房间白名单（gazetteer，docs/NLU-DESIGN.md 3.3/5.4.2）——
# 设计：房间是封闭集合（配置 name + aliases）。槽位提取不做「删词猜实体」，
# 而是「候选必须命中已知别名才算房间」；未命中 → 缺省 None（fail-closed）。
# 别名表由 client 按全部站点注入并集（与 name_prefixes 同一模式），
# 夜间 LLM 标注（P2/P3）将数据驱动地生长该表。

# 收录规则：含「琴房/排练室/房间」字样或 ASCII 字母数字的别名才进 gazetteer。
# 纯地名别名（如玉泉路/中关村）不收录——无琴房语境时可能误命中闲聊；
# 语音数字朗读（三零三）由 ROOM_TOKEN_CN 兜底，无需收录。
_GAZETTEER_HINTS = ("琴房", "排练室", "房间")
_GAZETTEER_ALNUM_RE = re.compile(r"[A-Za-z0-9]")
# 数字房间引用后紧跟方位修饰 → 复杂指代（「304外面的房间」）→ 不猜（文档 5.4.2）
_ROOM_MODIFIER_SUFFIX = (
    "外面",
    "旁边",
    "附近",
    "隔壁",
    "对面",
    "里面",
    "楼下",
    "楼上",
    "门口",
    "外",
    "里",
    "旁",
)


def _normalize_text(text: str) -> str:
    """无损规范化：NFKC（全角→半角）、小写、空白规整。

    只改格式、不删词、不改语义——「我」「俺」等助词残留由白名单判定兜住，
    不再靠清洗词表删词（清洗词表是打地鼠，删不完且会误伤）。
    """
    normalized = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"\s+", " ", normalized).strip()


def _is_gazetteer_alias(alias: str) -> bool:
    """别名是否满足 gazetteer 收录规则（见上）。"""
    return any(hint in alias for hint in _GAZETTEER_HINTS) or bool(_GAZETTEER_ALNUM_RE.search(alias))


def _prepare_gazetteer(aliases: Iterable[str]) -> tuple[str, ...]:
    """别名表预处理：无损规范化 + 收录过滤 + 去重 + 长度降序（最长匹配优先）。"""
    seen: set[str] = set()
    prepared: list[str] = []
    for alias in aliases:
        normalized = _normalize_text(alias)
        if not normalized or normalized in seen or not _is_gazetteer_alias(normalized):
            continue
        seen.add(normalized)
        prepared.append(normalized)
    return tuple(sorted(prepared, key=len, reverse=True))


def _gazetteer_boundary_ok(text: str, match: re.Match[str]) -> bool:
    """字母数字边界保护：304 不能从 304a / x304 里抠出来（汉字不算粘连）。"""
    before = text[match.start() - 1] if match.start() > 0 else ""
    after = text[match.end()] if match.end() < len(text) else ""
    return not (_GAZETTEER_ALNUM_RE.search(before) or _GAZETTEER_ALNUM_RE.search(after))


def _followed_by_modifier(text: str, position: int) -> bool:
    """数字房间引用后紧跟方位修饰（外面/旁边…）→ 复杂指代，拒绝而非拆解。"""
    tail = text[position : position + 2]
    return any(tail.startswith(word) for word in _ROOM_MODIFIER_SUFFIX)


def _time_reading_guard(text: str, match: re.Match[str]) -> bool:
    """时间朗读防护：
    - 命中紧跟在「点」后（8点15分→15）→ 时间读数；
    - 命中后紧跟「分」（「三点零四分」剥掉「三点」后剩「零四分」→「零四」）→ 时间读数。
    两种形态都不是房间引用，拒绝而非误提取。
    """
    if match.start() > 0 and text[match.start() - 1] == "点":
        return True
    return text[match.end() : match.end() + 1] == "分"


def _scan_gazetteer(text: str, aliases: tuple[str, ...]) -> str | None:
    """gazetteer 最大匹配扫描：长度降序 + 边界检查，返回首个命中的别名原文。"""
    for alias in aliases:
        for match in re.finditer(re.escape(alias), text):
            if _gazetteer_boundary_ok(text, match):
                return alias
    return None


def _strip_time_parts(text: str) -> str:
    """剥离时间片段（“12点到下午1点的303”中“12”不是房间号）。

    顺序沿用旧实现：TIME_RANGE 先吃「8点到9点」，DIGIT_RANGE 再吃日期残骸
    （「2026-08-22 8点到9点」→ 剥「26-08」「8点到9点」→ 剩「20-22」→
    DIGIT_RANGE 剥「20-22」→ 只剩「303」）。
    """
    cleaned = TIME_RANGE_RE.sub("", text)
    cleaned = START_TIME_RE.sub("", cleaned)
    return DIGIT_RANGE_RE.sub("", cleaned)


def _resolve_room_reference(text: str, aliases: tuple[str, ...]) -> str | None:
    """白名单式房间引用解析（唯一入口）：
    ① gazetteer 别名扫描（配置已知名）→ ② 数字房间 → ③ 语音数字朗读 → ④ 缺省 None。
    数字/语音数字扫描前先剥时间片段（防日期数字被当成房间号）；
    命中后跟方位修饰词视为复杂指代 → 不猜（fail-closed）。不命中绝不产出猜测值。
    """
    normalized = _normalize_text(text)
    hit = _scan_gazetteer(normalized, aliases)
    if hit is not None:
        return hit
    cleaned = _strip_time_parts(normalized)
    for pattern in (ROOM_TOKEN, ROOM_TOKEN_CN):
        for match in pattern.finditer(cleaned):
            if _time_reading_guard(cleaned, match) or _followed_by_modifier(cleaned, match.end()):
                continue
            return match.group(1)
    return None


def _overlaps(covered: list[tuple[int, int]], span: tuple[int, int]) -> bool:
    return any(start < span[1] and span[0] < end for start, end in covered)


def _room_reference_hits(text: str, aliases: tuple[str, ...]) -> list[str]:
    """收集全部房间引用命中（区间去重，供多房间检测）。

    先剥时间片段（「303和304a 17-19」的 17/19 不是房间），
    gazetteer 命中区间优先，数字命中与已覆盖区间重叠则跳过（「303琴房」不再双计）。
    """
    normalized = _normalize_text(text)
    cleaned = _strip_time_parts(normalized)
    hits: list[str] = []
    covered: list[tuple[int, int]] = []
    for alias in aliases:
        for match in re.finditer(re.escape(alias), cleaned):
            if not _gazetteer_boundary_ok(cleaned, match) or _overlaps(covered, match.span()):
                continue
            hits.append(alias)
            covered.append(match.span())
    for pattern in (ROOM_TOKEN, ROOM_TOKEN_CN):
        for match in pattern.finditer(cleaned):
            if _time_reading_guard(cleaned, match) or _followed_by_modifier(cleaned, match.end()):
                continue
            if _overlaps(covered, match.span()):
                continue
            hits.append(match.group(1))
            covered.append(match.span())
    return hits


def _to_hour(value: str) -> int:
    """小时文本 → 整数（支持阿拉伯数字与中文数字，如“八”→“8”）。"""
    value = value.strip()
    if value.isdigit():
        return int(value)
    return CN_NUM.get(value, 0)


def _norm_clock(period: str | None, hour: int, half: bool) -> str | None:
    """中文钟点 → 'HH:MM' 文本（纯词法，不读当前时间）。

    非法小时（>24，如「25点」）返回 None——调用方 fail-closed，绝不回绕成凌晨。
    """
    minute = 30 if half else 0
    if period in ("下午", "晚上", "傍晚") and hour < 12:
        hour += 12
    if period == "晚上" and hour == 12 and not half:
        return "24:00"
    if hour == 24:
        return "24:00" if not half else None  # 24点=日末边界；24点半非法
    if hour > 24:
        return None  # 打错的时间（25点）→ 拒绝，不静默回绕
    return f"{hour:02d}:{minute:02d}"


def _norm_clock_from_match(match: re.Match[str], prefix: str) -> str | None:
    period = match.group(f"p{prefix}")
    hour = _to_hour(match.group(f"h{prefix}"))
    half = bool(match.group(f"hm{prefix}"))
    if f"m{prefix}" in match.groupdict() and match.group(f"m{prefix}") is not None:
        return f"{hour:02d}:{match.group(f'm{prefix}')}" if hour <= 24 else None
    return _norm_clock(period, hour, half)


ABS_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _extract_absolute_date(text: str) -> str | None:
    """绝对日期（YYYY-MM-DD）→ 原样返回；其余 → None。"""
    match = ABS_DATE_RE.search(text)
    if match:
        return match.group(0)
    return None


def _extract_natural_date(text: str) -> int | None:
    """日期词 → **自然日**偏移（今天=0、明天=1、后天=2、大后天=3、后N天=N）。

    注意：自然日 ≠ 业务日（手册 6.1）。22:00 后业务日已切到“自然日明天”，
    「明天」在自然日语义下仍是 +1；业务偏移换算由 Resolver 按当前时刻完成
    （Parser/NLU 不读当前时间，手册 10.2）。显式「+N」不走此函数（保持业务日语义）。
    """
    for word, natural_offset in DATE_WORDS:
        if word in text:
            return natural_offset
    match = re.search(r"后\s*([一二两三四五六七八九十\d]+)\s*天", text)
    if match:
        return _to_hour(match.group(1))
    match = re.search(r"([一二两三四五六七八九十\d]+)\s*天\s*后", text)
    if match:
        return _to_hour(match.group(1))
    return None


def _extract_offset(text: str) -> int | None:
    """显式业务偏移（+N）→ 原样返回；其余 → None。"""
    match = re.search(r"\+\d+", text)
    if match:
        return int(match.group(0)[1:])
    return None


def _time_view(text: str) -> str:
    """把‘今晚/明晚/今天晚上…’归一为‘晚上…’，便于钟点提取。"""
    view = text.replace("今晚", "晚上").replace("明晚", "晚上")
    view = re.sub(r"今天\s*晚上", "晚上", view)
    view = re.sub(r"明天\s*晚上", "晚上", view)
    return view


def _extract_time_span(text: str) -> tuple[str, str] | None:
    """时间跨度 → (start_text, end_text)，交给 Resolver.parse_time 验证。

    支持：中文范围（7点到8点半）、数字范围（7-8、7:00-8:30）、
    起点+时长（下午3点练2h → 15:00-17:00）。无法可靠理解 → None。
    """
    # 先剥绝对日期（“2026-08-25 8点到9点”的“25”不能污染时间解析）
    view = ABS_DATE_RE.sub("", _time_view(text))

    match = TIME_RANGE_RE.search(view)
    if match:
        start = _norm_clock_from_match(match, "1")
        end = _norm_clock_from_match(match, "2")
        if start is None or end is None:
            return None  # 非法小时（25点）→ fail-closed，绝不回绕
        h1 = _to_hour(match.group("h1"))
        h2 = _to_hour(match.group("h2"))
        p1 = match.group("p1")
        p2 = match.group("p2")
        if p1 is None and h1 < 12:
            # 时段词在范围之前（“明天下午约304b 3点到5点”）：从匹配点前的文本推断
            prefix = view[: match.start()]
            for period in ("下午", "晚上", "傍晚"):
                if period in prefix:
                    start = _norm_clock(period, h1, bool(match.group("hm1")))
                    if p2 is None and h2 < 12:
                        end = _norm_clock(period, h2, bool(match.group("hm2")))
                    break
        elif p2 is None and p1 in ("下午", "晚上", "傍晚") and h2 < 12:
            end = _norm_clock(p1, h2, bool(match.group("hm2")))
        if start is None or end is None:
            return None
        return start, end

    digit = DIGIT_RANGE_RE.search(view)
    if digit:
        return digit.group(1), digit.group(2)

    start_match = START_TIME_RE.search(view)
    duration = _duration_minutes(view)
    if start_match and duration is not None:
        start_text = _norm_clock(
            start_match.group("p"), _to_hour(start_match.group("h")), bool(start_match.group("hm"))
        )
        if start_text is None:
            return None
        start_min = int(start_text.split(":")[0]) * 60 + int(start_text.split(":")[1])
        end_min = start_min + duration
        if end_min % 30 != 0 or end_min > 24 * 60:
            return None  # 跨日或超出日界 → fail-closed（24点+时长非法）
        return start_text, f"{end_min // 60:02d}:{end_min % 60:02d}"

    return None


def _duration_minutes(text: str) -> int | None:
    """时长（2h/2小时/两个小时/一个半小时）→ 分钟；不支持 → None。"""
    if HALF_HOUR_CN.search(text):
        return 90
    match = DURATION_RE.search(text)
    if not match:
        return None
    if match.group("num") is not None:
        hours = float(match.group("num"))
    else:
        hours = float(CN_NUM[match.group("cn")])
    minutes = int(round(hours * 60))
    return minutes if minutes % 30 == 0 else None


# 闲聊关键词（仅用于 NLU 全部通道失败后的文案区分，不参与意图识别）
CHITCHAT_KEYWORDS = (
    "在吗",
    "在不在",
    "哈哈",
    "嘿嘿",
    "hhh",
    "HHH",
    "早安",
    "早上好",
    "午安",
    "晚安",
    "天气",
    "吃饭",
    "吃了没",
    "饿",
    "困",
    "无聊",
    "QwQ",
    "QAQ",
    "呜呜",
    "好耶",
    "笑死",
    "离谱",
    "666",
    "好家伙",
    "卧槽",
    "拜拜",
    "再见",
    "886",
)

# 取消语义信号：出现时“预约/约/订”不应再算作预约意图（如“把预约退了”）
CANCEL_SIGNALS = ("取消", "退了", "退掉", "撤销", "不去了", "删掉", "删除", "清掉", "去掉")
# 空闲语境信号（query_free 关键词表）：出现时「约」是问句动词（「能约吗」），不是预约意图
QUERY_FREE_SIGNALS = tuple(keyword for keyword, _weight in INTENT_KEYWORDS["query_free"])
# 个人查询信号（封闭集合、可生长——夜间标注提炼的新变体加在这里，白名单非黑名单）：
# 出现时“查询/查/看看”不应再算作普通查询意图（如“查询我的预约”“看看我今天约的”）。
# 注意子串必须连续匹配（“我今天约的”不含连续子串“我约的”，故有“今天约的”变体）。
PERSONAL_SIGNALS = (
    "我的预约",
    "预约记录",
    "查询个人",
    "我约的",
    "我约了",
    "约了哪些",
    "约过的",
    "我订的",
    "有哪些预约",
    "有什么预约",
    "几天的预约",
    "今天约的",
    "个人预约",
)


def _score_keywords(text: str) -> list[tuple[str, int]] | None:
    scores: dict[str, int] = {}
    for operation, keywords in INTENT_KEYWORDS.items():
        total = sum(weight for keyword, weight in keywords if keyword in text)
        if total:
            scores[operation] = total
    if any(signal in text for signal in CANCEL_SIGNALS):
        scores.pop("create_reservation", None)
    if any(signal in text for signal in PERSONAL_SIGNALS):
        # 个人语境（我的预约/我约的…）下「查询/看看」是个人查询前缀、
        # 「预约/约了」是名词宾语——query_schedule 与 create 都不计
        # （与 _intents_with_exclusions 的 discard 同源对齐）。
        scores.pop("query_schedule", None)
        scores.pop("create_reservation", None)
    if any(signal in text for signal in QUERY_FREE_SIGNALS):
        # 空闲语境（能约吗/有空吗…）下「约」是问句动词（「能约吗」= 能预约吗）
        # 不是预约意图信号——create 不计（防「明天304b能约吗」误判复合/误判预约）。
        scores.pop("create_reservation", None)
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    if ranked[0][1] < MIN_KEYWORD_SCORE:
        return None
    return ranked


# 复合指令连接词（无标点时也视为动作分隔，如「取消预约再预约明天」）
COMPOUND_CONNECTORS = ("再", "然后", "接着", "顺便", "并且")
COMPOUND_SEGMENT_RE = re.compile(r"[，,；;。]|" + "|".join(COMPOUND_CONNECTORS))


def _intents_with_exclusions(text: str) -> set[str]:
    """整句意图集合（排除「宾语型」信号，防单意图句子误判复合）。

    排除规则（与关键词评分的 CANCEL/PERSONAL 清零同源）：
    - 取消信号（取消/退了…）存在 → 「预约/约/订」多为取消宾语（「取消预约」）→ create 不计；
    - 个人信号（我的预约/我约的…）存在 → 「查询/查」为个人查询前缀（「查询我的预约」）→
      query_schedule 不计；「预约」为名词（「我的预约」）→ create 不计；
    - 空闲信号（有空/空闲…）存在 → 「看看/查」是弱查询词（「看看303有没有空」）→
      query_schedule 不计；
    - 查询信号（看看/查查/安排…）存在 → 「预约」多为名词宾语（「看看X的预约」）→
      create 不计。
    """
    intents: set[str] = set()
    for operation, keywords in INTENT_KEYWORDS.items():
        if any(keyword in text for keyword, _weight in keywords):
            intents.add(operation)
    if any(signal in text for signal in CANCEL_SIGNALS):
        intents.discard("create_reservation")
        intents.discard("query_personal")  # 「取消我的预约」中「我的预约」是取消宾语
    if any(signal in text for signal in PERSONAL_SIGNALS):
        intents.discard("query_schedule")
        intents.discard("create_reservation")
    if any(keyword in text for keyword, _weight in INTENT_KEYWORDS["query_free"]):
        intents.discard("query_schedule")
        intents.discard("create_reservation")  # 「能约吗」的「约」是问句动词（见 QUERY_FREE_SIGNALS）
    if any(keyword in text for keyword, _weight in INTENT_KEYWORDS["query_schedule"]):
        intents.discard("create_reservation")
    return intents


def _compound_instruction(text: str) -> bool:
    """复合指令检测：同一句话表达多个动作 → True（统一口径不支持）。

    ① 标点或连接词（再/然后/接着/顺便/并且）分段：不同段命中不同意图 → 复合
       （「取消今天的预约，预约明天…」「取消预约再预约明天」）；
    ② 无标记：整句排除宾语型信号后意图 ≥2 → 复合（「预约明天取消」类罕见输入）。
    单意图句子（「取消预约」「查看我的预约」「看看303有没有空」）不误判。
    """
    segments = COMPOUND_SEGMENT_RE.split(text)
    if len(segments) >= 2:
        seen: set[str] = set()
        for segment in segments:
            segment = segment.strip()
            if not segment:
                continue
            seen.update(_intents_with_exclusions(segment))
        if len(seen) >= 2:
            return True
    return len(_intents_with_exclusions(text)) >= 2


def _multi_date_offsets(text: str) -> list[int]:
    """收集文本中出现的全部日期偏移（去重排序）。

    例：「今天和明天」→ [0, 1]；「明天」→ [1]；「后两天」→ [2]。
    """
    offsets: set[int] = set()
    for word, offset in DATE_WORDS:
        if word in text:
            offsets.add(offset)
    match = re.search(r"后\s*([一二两三四五六七八九十\d]+)\s*天", text)
    if match:
        offsets.add(_to_hour(match.group(1)))
    match = re.search(r"([一二两三四五六七八九十\d]+)\s*天\s*后", text)
    if match:
        offsets.add(_to_hour(match.group(1)))
    match = re.search(r"\+\d+", text)
    if match:
        offsets.add(int(match.group(0)[1:]))
    return sorted(offsets)


def _multi_date_instruction(text: str) -> bool:
    """多日期检测：同一动作指定多个日期（「今天和明天」）→ True。"""
    return len(_multi_date_offsets(text)) >= 2


def _query_like(text: str) -> bool:
    """查询倾向检测：命中 query_schedule / query_free 关键词 → True。"""
    return any(
        keyword in text
        for operation in ("query_schedule", "query_free")
        for keyword, _weight in INTENT_KEYWORDS[operation]
    )


# 默认称呼词（bot 昵称）；client 可注入各站点 bot_name
DEFAULT_NAME_PREFIXES = ("小泉",)


def strip_name_prefix(text: str, names: tuple[str, ...]) -> str:
    """剥离开头的称呼词（「小泉，帮我约…」「小泉帮我约…」）。

    称呼词不是意图/房间/人名的组成部分——显式剥离避免它污染
    意图提取、房间清洗与「他人检测」（曾误判「小泉退了我的预约」）。
    """
    for name in sorted(names, key=len, reverse=True):
        if text.startswith(name):
            rest = text[len(name) :]
            return rest.lstrip(" ，,！!。、:：")
    return text


def _multi_room_instruction(text: str, aliases: tuple[str, ...] = ()) -> bool:
    """多房间检测：房间引用命中 ≥2 个（「303和304a」）→ True。

    系统按单房间表达（Resolver 只接受一个 room_id），多房间无法表达——
    拒绝而不是静默只查/只约其中一个（fail-closed，与复合/多日期同一口径）。
    命中统计走 `_room_reference_hits`（gazetteer + 数字 + 语音数字，区间去重）。
    """
    return len(_room_reference_hits(text, aliases)) >= 2


def _degrade_hint(degrade_room: bool, degrade_date: bool, other_person: bool = False) -> str | None:
    """降级查询的呈示提示（用户需要知道「没完全听懂」）。"""
    if not (degrade_room or degrade_date):
        return None
    if other_person:
        return "⚠️ 小泉没完全听懂（好像提到了人名），已按全部房间查询～"
    if degrade_room and degrade_date:
        return "⚠️ 小泉没完全听懂（房间和日期），已按缺省查询～"
    if degrade_room:
        return "⚠️ 小泉没完全听懂房间，已按全部房间查询～"
    return "⚠️ 小泉没完全听懂日期，已按查询范围显示～"


def _attach_hint(
    intent: ParsedIntent, degrade_room: bool, degrade_date: bool, other_person: bool = False
) -> ParsedIntent:
    hint = _degrade_hint(degrade_room, degrade_date, other_person)
    if hint is None or intent.operation not in ("query_schedule", "query_free"):
        return intent
    return replace(intent, hint=hint)


# 涉及他人的“X的预约”结构（X 为 2-10 汉字，如「取消张三明天的预约」）
OTHER_PERSON_RE = re.compile(r"([\u4e00-\u9fff]{2,10}?)(?:的?预约|的\d{2,4}[a-zA-Z]?)")
# 清洗「X的预约」中 X 的非人名成分（本人代词/动词/时间/疑问词/量词——
# 都是「X的预约」里不可能人名的封闭成分；清洗按长度降序，长词必须先替换）。
_OTHER_CLEAN_WORDS = (
    "帮我",
    "麻烦",
    "把",
    "将",
    "给",
    "取消",
    "退了",
    "退掉",
    "退",
    "撤销",
    "删掉",
    "删除",
    "清掉",
    "去掉",
    "一下",
    "看看",
    "看下",
    "查看",
    "查查",
    "查一下",
    "查询",
    "查",
    "预约",
    "预订",
    "订",
    "约",
    "有",
    "什么",
    "哪些",
    "啥",
    "几天",
    "这",
    "那",
    "个",
    "今天",
    "明天",
    "后天",
    "大后天",
    "明晚",
    "今晚",
    "下午",
    "晚上",
    "上午",
    "早上",
    "中午",
    "傍晚",
    "和",
    "与",
    "的",
    "了",
    "我",
    "俺",
    "咱",
    "本人",
    "个人",
    "所有",
    "全部",
    "都",
)


def _other_person_instruction(text: str) -> str | None:
    """他人操作检测：「X的预约」中 X 是人名（非本人、非日期、非房间）→ 返回人名。

    例：「帮我取消一下张三明天的预约」→ "张三"；「取消我明天的预约」→ None。
    """
    for match in OTHER_PERSON_RE.finditer(text):
        who = match.group(1)
        who = re.sub(r"[一二两三四五六七八九十\d]+点(半)?", "", who)  # 中文数字时间（上午八点）
        who = re.sub(r"后?[一二两三四五六七八九十\d]+天(后)?", "", who)  # 中文数字日期（后两天/两天后）
        who = re.sub(r"(?:下|这|本|上)?(?:周|星期|礼拜)[一二三四五六日天]", "", who)  # 星期表达（下周二）
        # 按长度降序清洗：长词必须先替换（「查一下」在「一下」之前），
        # 否则「查一下」被拆成「查」+「一下」→ 残留「查」→ 误判他人。
        for word in sorted(_OTHER_CLEAN_WORDS, key=len, reverse=True):
            who = who.replace(word, "")
        who = who.strip(" ，,。")
        if not who:
            continue  # 本人/日期/房间 → 放行
        if who.endswith(("琴房", "排练室")):
            continue  # 房间名
        remaining = who
        for keyword in CHITCHAT_KEYWORDS:
            remaining = remaining.replace(keyword, "")
        remaining = re.sub(r"[哈嘿呜嘤]+", "", remaining)  # 拟声残留（哈哈哈→哈）
        if not remaining.strip(" ，,。"):
            continue  # 纯闲聊词（哈哈哈）→ 非人名；还有剩余（哈哈张三）→ 仍判他人
        return who
    return None


class NLUIntentMatcher:
    """NLU 规则引擎（Phase 0）+ ML 意图分类兜底（Phase 2，可选）。

    通道顺序：句式模板 → 关键词评分 → ML 分类器（懒加载，模型文件存在才生效）。
    ML 只输出意图，槽位永远由本地规则抽取（docs/NLU-DESIGN.md 6.3）。
    """

    def __init__(
        self,
        classifier: NaiveBayesClassifier | None = None,
        model_path: Path | None = None,
        name_prefixes: tuple[str, ...] = DEFAULT_NAME_PREFIXES,
        room_aliases: Iterable[str] = (),
        chitchat_keywords: Iterable[str] = (),
    ) -> None:
        self._classifier = classifier  # 注入（测试用）
        self._model_path = model_path  # 懒加载路径（生产用）
        self._lazy_classifier: NaiveBayesClassifier | None = None
        self._name_prefixes = tuple(dict.fromkeys((*name_prefixes, *DEFAULT_NAME_PREFIXES)))
        # 房间白名单（gazetteer）：client 注入全部站点房间 name+aliases 并集；
        # 预处理（规范化/收录过滤/最长优先）后供槽位提取与多房间检测使用。
        self._room_aliases = _prepare_gazetteer(room_aliases)
        # 闲聊关键词（可生长）：默认表 + 夜间自优化提炼的补充词（chitchat_keywords.json）
        # is_chitchat 只在 NLU 全部通道失败后影响文案区分，误加词低风险。
        self._chitchat_keywords = tuple(dict.fromkeys((*chitchat_keywords, *CHITCHAT_KEYWORDS)))

    def strip_names(self, text: str) -> str:
        """公开剥离称呼前缀（parser 在 parse 入口调用一次，后续全链路用剥离后文本）。"""
        return strip_name_prefix(text, self._name_prefixes)

    def resolve_room_reference(self, text: str) -> str | None:
        """房间引用白名单解析（公开封装，供 parser 正则分支验证 remainder）。

        可解释性约束（docs/NLU-DESIGN.md）：正则快车道只处理「所有组件都能解释」
        的输入——「查询 X」的 X 必须能命中房间白名单/数字模式，否则返回 None，
        由 parser fallback 到 NLU（个人信号/ML 裁决），绝不静默把非房间当房间。
        """
        return _resolve_room_reference(text, self._room_aliases)

    def _ensure_classifier(self) -> NaiveBayesClassifier | None:
        if self._classifier is not None:
            return self._classifier
        if self._lazy_classifier is None and self._model_path is not None:
            if self._model_path.exists():
                try:
                    self._lazy_classifier = NaiveBayesClassifier.load(self._model_path)
                except Exception:
                    # 模型损坏/加载失败 → ML 通道静默关闭，规则引擎照常工作
                    self._lazy_classifier = None
        return self._lazy_classifier

    def build(self, operation: str, text: str, room: str | None = None) -> ParsedIntent | None:
        """按给定意图 + 本地实体解析组装 ParsedIntent（归一化永远在本地）。

        供夜间 LLM 标注（qqbot/nlu/llm.py）复用：LLM 只给意图与房间原文，
        槽位值由本地规则计算并验证，行为与 match() 完全一致（fail-closed）。
        """
        return self._build(operation, text, room, score=0.0)

    def _rule_intents(self, text: str) -> list[tuple[str, float]]:
        """规则引擎意图候选（分数降序，供 match 逐个构建尝试）。

        只判意图，不抓槽位（槽位由 match 统一多值提取 + 冲突裁决）。
        模板阶段应用信号排除（与关键词评分同源）：「把…预约退了」的「预约」
        是取消宾语、「查一下我的预约」的「预约」是名词宾语——都不触发 create 模板。
        """
        candidates: list[tuple[str, float]] = []
        if not any(signal in text for signal in CANCEL_SIGNALS) and not any(
            signal in text for signal in PERSONAL_SIGNALS
        ):
            for pattern, operation in PATTERNS:
                if operation == "create_reservation" and any(signal in text for signal in QUERY_FREE_SIGNALS):
                    continue  # 「能约吗」的「约」是问句动词，不触发 create 模板
                if pattern.search(text):
                    candidates.append((operation, SCORE_TEMPLATE))
                    break  # 模板只取第一个命中的句式
        ranked = _score_keywords(text)
        if ranked is not None:
            candidates.extend((operation, float(score)) for operation, score in ranked)
        return candidates

    def _intent_candidates(self, text: str) -> list[tuple[str, float]]:
        """意图候选（分数降序）：ML 主通道排第一，规则引擎候选兜底跟随。

        「不害怕 fallback，害怕误报」（主人决策 2026-08-23）：ML 高置信才置顶
        （实测意图准确率 95.6%）；但 ML 意图可能构建不可行/槽位冲突
        （如「下周二的预约」被 ML 判 query_personal 却带日期槽位）——
        规则候选随后逐个尝试，ML 的误报从架构上被「构建 + 冲突裁决」拦截。
        """
        candidates: list[tuple[str, float]] = []
        classifier = self._ensure_classifier()
        if classifier is not None:
            predicted = classifier.predict(text)
            if predicted is not None:
                operation, confidence = predicted
                if operation != "unsupported" and confidence >= ML_FIRST_THRESHOLD:
                    # 取消信号护栏（与规则引擎 CANCEL_SIGNALS 排除同源）：
                    # ML 判 create 但文本含取消信号（「取消303明天7点到8点半」被 ML
                    # 带偏成 create）→ ML 候选不可信，交给规则候选裁决。
                    if not (
                        operation == "create_reservation" and any(signal in text for signal in CANCEL_SIGNALS)
                    ):
                        candidates.append((operation, confidence))
        candidates.extend(self._rule_intents(text))
        # 去重（保留首位 = ML 高置信版本）
        seen: set[str] = set()
        unique: list[tuple[str, float]] = []
        for operation, score in candidates:
            if operation in seen:
                continue
            seen.add(operation)
            unique.append((operation, score))
        return unique

    def match(self, text: str) -> ParsedIntent | None:
        stripped = text.strip()
        if not stripped:
            return None
        # ① 复合指令（“取消A，预约B”）：防半执行，唯一的前置检测——其余冲突
        # （多房间/多日期/他人）全部由槽位抓取结果暴露，不在意图前猜（4.8）。
        if _compound_instruction(stripped):
            return None

        # ② 意图候选：ML 主通道（高置信单意图）→ 规则引擎候选列表（无模型时纯规则）。
        candidates = self._intent_candidates(stripped)
        if not candidates:
            return None

        # ③ 槽位多值提取（数据驱动，抓到了什么就是什么，不猜）：
        # 房间 hits（gazetteer+数字+语音数字，区间去重）、日期 offsets、星期引用、他人结构检查。
        room_hits = _room_reference_hits(stripped, self._room_aliases)
        date_offsets = _multi_date_offsets(stripped)
        weekday_hits = _extract_weekdays(stripped)
        other_person = _other_person_instruction(stripped)

        # ④ 冲突裁决 + 构建（按候选降序逐个尝试）：
        # 写入类冲突（多房间/多日期/他人）→ 该候选不可行，继续下一个候选；
        # 查询类冲突 → 降级（房间缺省/日期转范围）+ hint；构建失败 → 下一候选。
        for operation, score in candidates:
            if other_person is not None and operation == "query_personal":
                # 「看看张三明天的预约」被 ML 判为个人查询：系统不支持按人过滤，
                # 降级为全站查询 + 「提到人名」hint（与查询类他人降级同口径）。
                operation = "query_schedule"
            if operation == "query_personal":
                # 个人查询只支持「今天」（QueryPersonal 默认业务日）；「我明天的预约」
                # 被 ML 泛化成 query_personal 时带日期槽位 → 候选不可行，落到规则候选
                # （全站查询该日期，信息含自己且日期正确），拦截槽位-意图不一致误报。
                # 「我今天约的」（自然日 0=今天）恰好是默认日期 → 放行。
                personal_today = bool(date_offsets) and all(offset == 0 for offset in date_offsets)
                if room_hits or weekday_hits or (date_offsets and not personal_today):
                    continue
            query_like = operation in ("query_schedule", "query_free")
            multi_room = len(room_hits) >= 2
            # 多日期 = 自然日词 + 星期引用合计 ≥2（「今天和周三」「周三和周四」）
            multi_date = len(date_offsets) + len(weekday_hits) >= 2
            if (multi_room or multi_date or other_person is not None) and not query_like:
                continue  # 写入类冲突 → fail-closed，绝不半执行
            degrade_room = (multi_room or other_person is not None) and query_like
            degrade_date = multi_date and query_like
            # 降级范围只对自然日偏移可用（「周三和周四」无偏移 → 缺省默认范围 + hint）
            date_range: tuple[int, int] | None = (
                (date_offsets[0], date_offsets[-1]) if degrade_date and date_offsets else None
            )
            room = room_hits[0] if room_hits else None
            if degrade_room:
                room = None  # 查询多房间/涉及他人 → 房间缺省（查全部）
            if operation == "bind_user":
                intent = self._build_bind_from_text(stripped)
            else:
                intent = self._build(operation, stripped, room, score=score, date_range=date_range)
            if intent is not None:
                return _attach_hint(intent, degrade_room, degrade_date, other_person is not None)
        return None

    def _build(
        self,
        operation: str,
        text: str,
        room: str | None,
        score: float,
        date_range: tuple[int, int] | None = None,
    ) -> ParsedIntent | None:
        absolute_date = _extract_absolute_date(text)  # 绝对日期（YYYY-MM-DD，自然日语义）
        natural_date = _extract_natural_date(text)  # 日期词 → 自然日偏移（Resolver 换算业务日）
        weekdays = _extract_weekdays(text)  # 星期引用 → (weekday, mode)；换算在 Resolver
        offset = _extract_offset(text)  # 显式 +N → 业务日偏移（原语义）
        weekday_ref = weekdays[0] if len(weekdays) == 1 else None  # 多星期 → 冲突裁决处理

        if operation == "bind_user":
            return self._build_bind_from_text(text)

        if operation == "create_reservation":
            span = _extract_time_span(text)
            if span is None:
                return None  # 文档 4.2：预约必须有时间，绝不猜测
            arguments: dict[str, object] = {
                "start": span[0],
                "end": span[1],
                "offset": offset if offset is not None else 0,
            }
            if absolute_date is not None:
                arguments["date"] = absolute_date
            elif natural_date is not None:
                arguments["natural_date"] = natural_date
            elif weekday_ref is not None:
                arguments["weekday"], arguments["week_mode"] = weekday_ref
            if room:
                arguments["room_reference"] = room
            return ParsedIntent(operation, arguments)

        if operation == "cancel_reservation":
            arguments: dict[str, object] = {"offset": offset if offset is not None else 0}
            if re.search(r"全部|所有|一切", text):
                # 「取消全部预约」→ 未来整个可预约周期（业务日起点，见 CancelAllReservations）
                arguments["cancel_all"] = True
            elif absolute_date is not None:
                arguments["date"] = absolute_date
            elif natural_date is not None:
                arguments["natural_date"] = natural_date
            elif weekday_ref is not None:
                arguments["weekday"], arguments["week_mode"] = weekday_ref
            span = _extract_time_span(text)
            if span is not None:
                arguments["start"] = span[0]
                arguments["end"] = span[1]
            if room:
                arguments["room_reference"] = room
            return ParsedIntent(operation, arguments)

        if operation in ("query_schedule", "query_free"):
            arguments: dict[str, object] = {}
            if room:
                arguments["room_reference"] = room
            if date_range is not None:
                # 多日期查询降级：转范围查询（「今天和明天」→ 自然日 0~1）
                arguments["natural_range"] = list(date_range)
            elif absolute_date is not None:
                arguments["range_start"] = absolute_date
                arguments["range_end"] = absolute_date
            elif natural_date is not None:
                arguments["natural_date"] = natural_date
            elif weekday_ref is not None:
                arguments["weekday"], arguments["week_mode"] = weekday_ref
            elif offset is not None:
                arguments["range_start"] = f"+{offset}"
                arguments["range_end"] = f"+{offset}"
            return ParsedIntent(operation, arguments)

        if operation == "query_personal":
            return ParsedIntent(operation)

        return None

    @staticmethod
    def _build_bind_from_text(text: str) -> ParsedIntent | None:
        match = STUDENT_ID_RE.search(text)
        if not match:
            return None
        student_id = match.group(1)
        before = text[: match.start()]
        for word in ("我是", "我叫", "绑定", "学号", "，", ",", "："):
            before = before.replace(word, "")
        name = before.strip()
        if not re.fullmatch(r"[\u4e00-\u9fff]{1,10}", name):
            return None
        return ParsedIntent("bind_user", {"display_name": name, "student_id": student_id})

    def is_chitchat(self, text: str) -> bool:
        """闲聊检测：命中闲聊关键词（默认表 + 自优化补充词）→ True。

        仅在 NLU 全部通道失败后调用；命中时上层回复俏皮文案而不是 help。
        """
        return any(keyword in text for keyword in self._chitchat_keywords)

    def is_compound(self, text: str) -> bool:
        """复合指令检测（模块级 `_compound_instruction` 的公开封装，供 parser 用）。"""
        return _compound_instruction(text)

    def is_multi_date(self, text: str) -> bool:
        """多日期检测（模块级 `_multi_date_instruction` 的公开封装，供 parser 用）。"""
        return _multi_date_instruction(text)

    def is_multi_room(self, text: str) -> bool:
        """多房间检测（模块级 `_multi_room_instruction` 的公开封装，供 parser 用）。"""
        return _multi_room_instruction(text, self._room_aliases)

    def unsupported_reason(self, text: str) -> str | None:
        """统一口径「不支持」的原因判定（parse 正则成功路径前拦截用，docs/NLU-DESIGN.md 4.7）。

        返回 None（放行）或原因：
        - "compound"：复合指令；或写入类的多日期/多房间（统一不支持文案）；
        - "other_person"：涉及他人（专门文案，区别于“一次只说一件事”）。
        查询类多日期/多房间/他人**放行**——由 match() 降级为缺省查询并附加 hint 提醒。
        """
        if _compound_instruction(text):
            return "compound"
        if _query_like(text):
            return None
        if len(_multi_date_offsets(text)) >= 2 or _multi_room_instruction(text, self._room_aliases):
            return "compound"
        if _other_person_instruction(text) is not None:
            return "other_person"
        return None

    def looks_like_command(self, text: str) -> bool:
        """命令倾向检测：模板/关键词/ML 任一通道「觉得像命令但构建失败」。

        返回 True 时上层应保留原格式指导（如「预约 303」缺时间 → 格式提示），
        而不是当作无法理解的自然语言。关键词**弱命中**（低于评分阈值）也算
        命令倾向——「明天上午去304b练琴」意图明显但缺时间，应给格式指导。
        """
        for pattern, _operation in PATTERNS:
            if pattern.search(text):
                return True
        if _score_keywords(text) is not None:
            return True
        for _operation, keywords in INTENT_KEYWORDS.items():
            if any(keyword in text for keyword, _weight in keywords):
                return True
        classifier = self._ensure_classifier()
        # ML 预测只有达到主通道阈值才算命令倾向（与 match 的 ML 采纳门槛一致）——
        # 低置信预测（如「这个小程序是干嘛的」0.60）不构成命令倾向 → 走非命令文案。
        if classifier is not None:
            predicted = classifier.predict(text)
            if predicted is not None and predicted[1] >= ML_FIRST_THRESHOLD:
                return True
        return False
