"""零依赖朴素贝叶斯意图分类器（Phase 2，docs/NLU-DESIGN.md 6 节）。

选型（文档 6.1）：
- 特征：字符级 n-gram(1-2) + 文档频率(DF)筛选，中文短文本无需分词（不用 jieba）；
- 模型：多项式朴素贝叶斯（拉普拉斯平滑），训练时预计算 log 概率，预测仅查表求和；
- 序列化：JSON（不用 pickle，规避反序列化风险）；模型量级数百 KB，加载毫秒级；
- 边界：**只分类意图**，槽位抽取永远走本地规则（matcher._build）。

置信度：softmax 归一化的最高类概率；低于阈值返回 None（fail-closed，文档 4.1）。
种子权重（文档 5.0 三防设计）：source=seed 的样本计数按 SEED_WEIGHT 衰减，
真实数据（real/llm）积累后种子不会永久主导模型。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

NGRAM_MIN = 1
NGRAM_MAX = 2
LAPLACE_ALPHA = 1.0
MIN_DF = 2  # 文档频率筛选：至少出现在 N 条样本中的特征才保留
SEED_WEIGHT = 0.3  # 种子样本计数权重（真实数据积累后衰减）
DEFAULT_THRESHOLD = 0.75  # 置信度门槛（fail-closed，文档 4.1）
SHORT_TEXT_MAX_CHARS = 6  # 短文本判定长度
SHORT_TEXT_THRESHOLD = 0.9  # 短文本更高置信门槛（防闲聊幻觉）


def char_ngrams(text: str) -> list[str]:
    """字符级 n-gram(1-2)。空白不产生特征。"""
    compact = "".join(text.split())
    grams: list[str] = []
    for n in range(NGRAM_MIN, NGRAM_MAX + 1):
        for i in range(len(compact) - n + 1):
            grams.append(compact[i : i + n])
    return grams


class NaiveBayesClassifier:
    """多项式朴素贝叶斯意图分类器（纯 Python，零依赖）。"""

    def __init__(self, threshold: float = DEFAULT_THRESHOLD) -> None:
        self.threshold = threshold
        self._classes: list[str] = []
        self._class_log_prior: dict[str, float] = {}
        self._feat_log_prob: dict[str, dict[str, float]] = {}
        self._vocab: set[str] = set()

    # —— 训练 ——

    def fit(self, samples: list[tuple[str, str, float]]) -> None:
        """训练：samples = [(text, operation, weight)]，weight 为计数权重（种子衰减）。"""
        class_counts: dict[str, float] = {}
        feature_counts: dict[str, dict[str, float]] = {}
        df: dict[str, int] = {}
        total_weight = 0.0

        for text, operation, weight in samples:
            class_counts[operation] = class_counts.get(operation, 0.0) + weight
            total_weight += weight
            grams = char_ngrams(text)
            seen_in_doc: set[str] = set()
            for gram in grams:
                feature_counts.setdefault(operation, {}).setdefault(gram, 0.0)
                feature_counts[operation][gram] += weight
                seen_in_doc.add(gram)
            for gram in seen_in_doc:
                df[gram] = df.get(gram, 0) + 1

        self._classes = sorted(class_counts)
        vocab = {gram for gram, count in df.items() if count >= MIN_DF}
        self._vocab = vocab

        self._class_log_prior = {cls: math.log(class_counts[cls] / total_weight) for cls in self._classes}
        self._feat_log_prob = {}
        for cls in self._classes:
            counts = feature_counts.get(cls, {})
            denominator = sum(count for gram, count in counts.items() if gram in vocab) + LAPLACE_ALPHA * len(
                vocab
            )
            probs: dict[str, float] = {}
            for gram in vocab:
                count = counts.get(gram, 0.0)
                probs[gram] = math.log((count + LAPLACE_ALPHA) / denominator)
            self._feat_log_prob[cls] = probs

    # —— 预测 ——

    def predict(self, text: str) -> tuple[str, float] | None:
        """返回 (operation, confidence)；置信度低于阈值返回 None（fail-closed）。

        短文本（≤ SHORT_TEXT_MAX_CHARS）用更高的置信门槛：
        闲聊/无关输入多为短句（“在吗在吗”“帮我开一下空调”），而真实短意图
        （“我约了”“看看琴房”等）已被规则引擎前置覆盖，不会走到 ML 通道——
        因此短文本高门槛几乎不影响真实意图，却能拦下高置信闲聊幻觉。
        """
        if not self._classes:
            return None
        grams = char_ngrams(text)
        scores: dict[str, float] = {}
        for cls in self._classes:
            score = self._class_log_prior[cls]
            probs = self._feat_log_prob[cls]
            for gram in grams:
                prob = probs.get(gram)
                if prob is not None:
                    score += prob
            scores[cls] = score

        # softmax 归一化 → 置信度
        max_score = max(scores.values())
        exp_sum = sum(math.exp(score - max_score) for score in scores.values())
        best = max(scores, key=scores.get)
        confidence = math.exp(scores[best] - max_score) / exp_sum
        compact_len = len("".join(text.split()))
        if compact_len <= SHORT_TEXT_MAX_CHARS:
            effective = max(self.threshold, SHORT_TEXT_THRESHOLD)
        else:
            effective = self.threshold
        if confidence < effective:
            return None
        return best, confidence

    # —— 序列化（JSON，不用 pickle） ——

    def save(self, path: Path) -> None:
        payload = {
            "classes": self._classes,
            "class_log_prior": self._class_log_prior,
            "feat_log_prob": self._feat_log_prob,
            "threshold": self.threshold,
            "vocab_size": len(self._vocab),
            "meta": {
                "feature": f"char-ngram({NGRAM_MIN}-{NGRAM_MAX})",
                "min_df": MIN_DF,
                "seed_weight": SEED_WEIGHT,
            },
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path, threshold: float | None = None) -> NaiveBayesClassifier:
        payload = json.loads(path.read_text(encoding="utf-8"))
        classifier = cls(threshold=threshold if threshold is not None else float(payload["threshold"]))
        classifier._classes = list(payload["classes"])
        classifier._class_log_prior = payload["class_log_prior"]
        classifier._feat_log_prob = payload["feat_log_prob"]
        classifier._vocab = set()
        return classifier

    # —— 供报告/测试使用 ——

    @property
    def classes(self) -> list[str]:
        return list(self._classes)

    @property
    def feature_count(self) -> int:
        return len(self._feat_log_prob.get(self._classes[0], {})) if self._classes else 0

    def summary(self) -> dict[str, Any]:
        return {
            "classes": self._classes,
            "feature_count": self.feature_count,
            "threshold": self.threshold,
        }
