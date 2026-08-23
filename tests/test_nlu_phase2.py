"""Phase 2：ML 意图分类器测试（docs/NLU-DESIGN.md 6 节）。

分类器为纯 Python 零依赖实现，测试不联网；训练数据用内存内小样本。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qqbot.nlu import NaiveBayesClassifier, NLUIntentMatcher

# 训练用小样本：每类 3-4 条，足够区分意图
TRAIN_SAMPLES = [
    ("帮我约303明天7-8", "create_reservation"),
    ("约304b今晚8点到9点", "create_reservation"),
    ("帮我订明天下午的304a", "create_reservation"),
    ("取消303今天7-8", "cancel_reservation"),
    ("把我明天的预约退了", "cancel_reservation"),
    ("我明天不去了", "cancel_reservation"),
    ("帮我看看303今天有没有人", "query_schedule"),
    ("查一下304b明天的安排", "query_schedule"),
    ("看看琴房使用情况", "query_schedule"),
    ("304b下午有空吗", "query_free"),
    ("帮我看看304a有没有空", "query_free"),
    ("明天琴房有空吗", "query_free"),
    ("我的预约", "query_personal"),
    ("查一下我的预约记录", "query_personal"),
    ("我约了哪些", "query_personal"),
    ("我是张三 2023X1234567890", "bind_user"),
    ("绑定 李四 2023X1234567891", "bind_user"),
    ("我叫王五 2023X1234567892", "bind_user"),
]


@pytest.fixture
def trained() -> NaiveBayesClassifier:
    classifier = NaiveBayesClassifier(threshold=0.0)
    classifier.fit([(text, operation, 1.0) for text, operation in TRAIN_SAMPLES])
    return classifier


def test_classifier_predicts_correct_intent(trained: NaiveBayesClassifier) -> None:
    assert trained.predict("帮我约304b明天晚上7点到9点")[0] == "create_reservation"
    assert trained.predict("取消我明天的预约")[0] == "cancel_reservation"
    assert trained.predict("帮我查查304a有没有空")[0] == "query_free"
    assert trained.predict("看看我的预约")[0] == "query_personal"
    assert trained.predict("我是赵六 2023X1234567893")[0] == "bind_user"


def test_classifier_threshold_fail_closed() -> None:
    # 阈值 0.99：几乎所有预测都低于阈值 → None
    classifier = NaiveBayesClassifier(threshold=0.99)
    classifier.fit([(text, operation, 1.0) for text, operation in TRAIN_SAMPLES])
    assert classifier.predict("帮我约303明天7-8") is None  # 低置信被拒（fail-closed）


def test_classifier_json_round_trip(trained: NaiveBayesClassifier, tmp_path: Path) -> None:
    path = tmp_path / "model.json"
    trained.save(path)
    loaded = NaiveBayesClassifier.load(path)
    assert loaded.classes == trained.classes
    for text, _operation in TRAIN_SAMPLES:
        # 保存前后行为一致（含短文本被 0.9 门槛拒绝的情况）
        assert loaded.predict(text) == trained.predict(text)
    # 长文本（>6 字符）正常分类
    long_sample = [t for t, _ in TRAIN_SAMPLES if len(t) > 6]
    for text in long_sample:
        assert loaded.predict(text) is not None


def test_classifier_json_not_pickle(tmp_path: Path) -> None:
    trained = NaiveBayesClassifier()
    trained.fit([(text, operation, 1.0) for text, operation in TRAIN_SAMPLES])
    path = tmp_path / "model.json"
    trained.save(path)
    content = path.read_text(encoding="utf-8")
    assert content.lstrip().startswith("{")  # 纯 JSON，非 pickle 二进制
    assert "feat_log_prob" in content


def test_char_ngrams_no_whitespace() -> None:
    from qqbot.nlu.classifier import char_ngrams

    grams = char_ngrams("预约 303")
    assert "预约" in grams
    assert "预约303" not in grams  # 空白不产生跨词 n-gram


def test_matcher_ml_channel_handles_rule_failure() -> None:
    """规则引擎失败的句子，ML 通道能兜底（ML 只出意图，槽位走本地规则）。"""
    classifier = NaiveBayesClassifier(threshold=0.0)
    classifier.fit([(text, operation, 1.0) for text, operation in TRAIN_SAMPLES])

    # 规则引擎（无 ML）：识别不了
    plain = NLUIntentMatcher()
    assert plain.match("我预约的") is None
    # 带 ML：分类为 query_personal（置信 0.92 ≥ 短文本门槛，ML 只出意图）
    with_ml = NLUIntentMatcher(classifier=classifier)
    intent = with_ml.match("我预约的")
    assert intent is not None
    assert intent.operation == "query_personal"
    assert intent.arguments == {}


def test_matcher_ml_lazy_loads_model(tmp_path: Path) -> None:
    """model_path 懒加载：首次 match 才读文件；模型缺失/损坏时静默关闭。"""
    classifier = NaiveBayesClassifier(threshold=0.0)
    classifier.fit([(text, operation, 1.0) for text, operation in TRAIN_SAMPLES])
    model_path = tmp_path / "intent_model.json"
    classifier.save(model_path)

    matcher = NLUIntentMatcher(model_path=model_path)
    assert matcher._lazy_classifier is None  # 未触发加载
    matcher._ensure_classifier()
    assert matcher._lazy_classifier is not None  # 首次调用才加载
    loaded = matcher._ensure_classifier()
    assert loaded is matcher._lazy_classifier  # 重复调用复用

    # 损坏的模型文件 → ML 通道静默关闭，不抛异常
    broken = tmp_path / "broken.json"
    broken.write_text("not json{{{", encoding="utf-8")
    matcher_broken = NLUIntentMatcher(model_path=broken)
    assert matcher_broken._ensure_classifier() is None
    assert matcher_broken._lazy_classifier is None

    # 模型不存在 → 同样静默
    matcher_missing = NLUIntentMatcher(model_path=tmp_path / "missing.json")
    assert matcher_missing._ensure_classifier() is None


def test_matcher_ml_channel_respects_slots() -> None:
    """ML 分类为预约但本地抽不出时间 → fail-closed（槽位永远不猜）。"""
    classifier = NaiveBayesClassifier(threshold=0.0)
    classifier.fit([(text, operation, 1.0) for text, operation in TRAIN_SAMPLES])

    matcher = NLUIntentMatcher(classifier=classifier)
    # 无时间 → 拒绝（文档 4.2）
    assert matcher.match("帮我约一下304a") is None


def test_matcher_ml_never_touches_admin() -> None:
    from qqbot.interfaces.qq.parser import QQCommandParser

    classifier = NaiveBayesClassifier(threshold=0.0)
    classifier.fit([(text, operation, 1.0) for text, operation in TRAIN_SAMPLES])
    parser = QQCommandParser(nlu=NLUIntentMatcher(classifier=classifier))
    # admin 走严格正则，NLU（含 ML）永不介入
    intent = parser.parse("#备份用户")
    assert intent.admin is True
    assert intent.operation == "backup_users"


def test_matcher_ml_unsupported_class_rejects() -> None:
    """unsupported 类闭环：ML 学会「复合/他人」→ 实时预测 unsupported → 拒绝（fail-closed）。"""
    classifier = NaiveBayesClassifier(threshold=0.0)
    samples = TRAIN_SAMPLES + [
        ("取消张三明天的预约", "unsupported"),
        ("帮我取消今天的预约，预约明天中午的303", "unsupported"),
        ("帮我约今天和明天的琴房", "unsupported"),
    ]
    classifier.fit([(text, operation, 1.0) for text, operation in samples])
    # 训练后能识别 unsupported 样本
    assert classifier.predict("取消张三明天的预约")[0] == "unsupported"

    matcher = NLUIntentMatcher(classifier=classifier)
    # 规则先拦截他人/复合（护栏仍在），ML 的 unsupported 覆盖规则不拦的变体：
    # 构造规则护栏检测不到的 unsupported 变体（如无分隔符复合）
    assert matcher.match("帮我取消今天预约明天约303") is None


def test_classifier_learns_unsupported_class() -> None:
    classifier = NaiveBayesClassifier(threshold=0.0)
    classifier.fit(
        [(t, o, 1.0) for t, o in TRAIN_SAMPLES]
        + [
            ("取消李四的预约", "unsupported", 1.0),
            ("取消王五明天的预约", "unsupported", 1.0),
            ("退掉赵六的预约", "unsupported", 1.0),
        ]
    )
    assert "unsupported" in classifier.classes
    pred = classifier.predict("取消李四的预约")
    assert pred is not None and pred[0] == "unsupported"
