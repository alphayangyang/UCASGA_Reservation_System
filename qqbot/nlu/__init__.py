"""NLU 可插拔子包（插件形态，docs/NLU-DESIGN.md）。

核心系统通过以下三个挂载点与本包交互，本包缺失/关闭时核心功能完全不受影响：
- parser 构造注入 NLUIntentMatcher（nlu=None 即关闭，见 interfaces/qq/parser.py）；
- configs/*.yaml 的 features.nlu_enabled 开关；
- DEEPSEEK_API_KEY 环境变量（缺失则不挂载夜间 LLM 标注 job，见 client.py）。

模块划分：
- matcher.py    规则引擎（实时路径，Phase 0）+ ML 兜底通道（Phase 2）
- classifier.py 零依赖朴素贝叶斯意图分类器（字符 n-gram，JSON 序列化）
- llm.py        外部能力：DeepSeek API 调用、学号脱敏、一致性投票（零内部依赖）
- annotate.py   夜间批处理编排：pending → 一致性投票 → Resolver 校验 → 候选库/异常/日报
"""

from qqbot.nlu.annotate import (
    NLU_DATA_DIR,
    NightlyReport,
    build_intent_from_llm,
    run_nightly_annotate,
    run_nightly_job,
    validate_with_resolver,
    write_pending,
)
from qqbot.nlu.classifier import NaiveBayesClassifier
from qqbot.nlu.llm import (
    LLMCaller,
    annotate_with_consensus,
    deepseek_caller,
    mask_sensitive,
)
from qqbot.nlu.matcher import NLUIntentMatcher

__all__ = [
    "NLU_DATA_DIR",
    "NLUIntentMatcher",
    "NaiveBayesClassifier",
    "NightlyReport",
    "LLMCaller",
    "annotate_with_consensus",
    "build_intent_from_llm",
    "deepseek_caller",
    "mask_sensitive",
    "run_nightly_annotate",
    "run_nightly_job",
    "validate_with_resolver",
    "write_pending",
]
