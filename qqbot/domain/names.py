"""姓名规范化与合法性校验（中文 + 拉丁字母）。

背景（2026-09-17 修复）：原校验只接受 1–10 个纯汉字，把外文姓名、
带间隔号的少数民族姓名一律拒之门外——「绑定」对留学生完全不可用。
现放宽为「汉字 + 拉丁字母 + 姓名常见符号」，1–24 个字符。

边界刻意收在**图片字体（Noto Sans SC）实际有字形的范围内**：西里尔、
阿拉伯、韩文、emoji 一律仍拒绝，避免出现「能绑定但查询图片上是豆腐块」
这种更难排查的状态。若将来要支持其他文字系统，须同时扩充字体子集。

纯函数、无外部依赖；parser（归一化）与 application（校验）共用同一套
规则，保证入库名字与后续按名字查人（``#添加管理`` 等）用的是同一规范形式。
"""

from __future__ import annotations

# 姓名常见符号：间隔号、句点、连字符（含各语言异体）、撇号（含异体）、空格
NAME_SYMBOLS = frozenset("·・.-'’ʼʻ‐‑–— ")
MAX_NAME_LENGTH = 24

_CJK_START = 0x4E00
_CJK_END = 0x9FFF


def normalize_display_name(raw: str) -> str:
    """折叠空白，得到入库/查人统一使用的规范形式。

    - 纯汉字姓名去掉空格：「张 三」→「张三」（与旧实现 ``"".join`` 行为一致，
      中文用户不回归）；
    - 含非汉字则保留单词间的单个空格：「John  Smith」→「John Smith」
      （旧实现会拼成 ``JohnSmith``，是本次一并修复的 bug）。
    """
    name = " ".join(raw.split())
    without_spaces = name.replace(" ", "")
    if without_spaces and all(
        _CJK_START <= ord(ch) <= _CJK_END for ch in without_spaces
    ):
        return without_spaces
    return name


def is_valid_display_name(name: str) -> bool:
    """1–24 个字符，且只含汉字、拉丁字母与 ``NAME_SYMBOLS``（至少一个汉字或字母）。

    拒绝：空、纯空白、超长、纯符号（``---``）、emoji（So）、控制字符、
    零宽字符，以及其他文字系统（西里尔/阿拉伯/韩文——字体无字形）。
    """
    if not (1 <= len(name) <= MAX_NAME_LENGTH):
        return False
    if not name.strip():
        return False
    for ch in name:
        if ch in NAME_SYMBOLS:
            continue
        if _CJK_START <= ord(ch) <= _CJK_END:
            continue
        if ch.isascii() and ch.isalpha():
            continue
        return False
    # 走到这里说明每个字符都是符号或合法字母——再要求至少一个字母/汉字，
    # 挡住「...」「-」这类纯符号姓名。
    return any(ch not in NAME_SYMBOLS for ch in name)


LANGUAGE_ZH = "zh"
LANGUAGE_EN = "en"


def preferred_language(name: str | None) -> str:
    """按绑定姓名推断呈示语言：不含汉字即视为英文用户（留学生）。

    用途：回执与查询图片的语言。未绑定（``None``/空）回落中文——此时用户
    还没登记，无法判断，中文是站点默认。
    """
    if not name:
        return LANGUAGE_ZH
    if any(_CJK_START <= ord(ch) <= _CJK_END for ch in name):
        return LANGUAGE_ZH
    return LANGUAGE_EN


def short_display_name(name: str, limit: int = 4) -> str:
    """时间轴块内／列表里的姓名缩写。

    中文姓名沿用既有的「只留末 ``limit`` 字」约定（短名不受影响，
    「阿依古丽·麦麦提」→「丽·麦麦提」）；**英文姓名不按字符数截断**——
    「John Smith」截末 4 字会变成「mith」，是无意义残片，故完整保留。

    与查看者语言无关：图片是全群共用的，缩写只取决于姓名本身。
    """
    if not name:
        return name
    if any(_CJK_START <= ord(ch) <= _CJK_END for ch in name):
        return name[-limit:]
    return name
