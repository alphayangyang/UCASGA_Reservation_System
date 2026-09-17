"""姓名规范化与校验（2026-09-17 外文姓名绑定修复）。

背景：原校验只接受 1–10 个纯汉字，导致留学生／带间隔号的少数民族姓名
完全无法绑定。本套件锁住放宽后的边界：

- 中文行为**不回归**（「张 三」仍归一化为「张三」）；
- 拉丁姓名原样保留空格（旧实现会拼成 JohnSmith，是本次修的 bug）；
- 其他文字系统与 emoji 仍拒绝（图片字体无字形）。
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest

from qqbot.application.service import BookingApplication, Dispatcher
from qqbot.domain.calendar import SHANGHAI_TZ
from qqbot.domain.commands import BindUser
from qqbot.domain.models import ExternalIdentity, RequestContext
from qqbot.domain.names import (
    MAX_NAME_LENGTH,
    is_valid_display_name,
    normalize_display_name,
)
from qqbot.interfaces.qq.parser import QQCommandParser

# —— 归一化 ——


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("张三", "张三"),
        ("张 三", "张三"),  # 纯汉字去空格：中文行为不回归
        ("张  三", "张三"),
        ("张三\u3000", "张三"),  # 全角空格
        ("John", "John"),
        ("John Smith", "John Smith"),
        ("John  Smith", "John Smith"),  # 折叠连续空格
        ("  John Smith  ", "John Smith"),  # 去首尾空白
        ("玛丽·居里", "玛丽·居里"),  # 含非汉字 → 不去空格（此处本无空格）
    ],
)
def test_normalize_display_name(raw: str, expected: str) -> None:
    assert normalize_display_name(raw) == expected


def test_normalize_is_idempotent() -> None:
    for raw in ("张 三", "John  Smith", "O'Brien", "玛丽·居里"):
        once = normalize_display_name(raw)
        assert normalize_display_name(once) == once


# —— 校验：放行 ——


@pytest.mark.parametrize(
    "name",
    [
        "张三",
        "张三丰",
        "John",
        "John Smith",
        "Aleksandra Ivanova",
        "O'Brien",
        "Al-Farsi",
        "玛丽·居里",
        "阿依古丽·麦麦提",  # 少数民族姓名（本次被误伤的典型）
        "J. R. Smith",
        "A" * MAX_NAME_LENGTH,
    ],
)
def test_valid_display_names(name: str) -> None:
    assert is_valid_display_name(name) is True


# —— 校验：拒绝 ——


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("", "空"),
        ("   ", "纯空白"),
        ("...", "纯符号"),
        ("---", "纯符号"),
        ("A" * (MAX_NAME_LENGTH + 1), "超长"),
        ("Иван", "西里尔字母（字体无字形）"),
        ("أحمد", "阿拉伯字母（字体无字形）"),
        ("김철수", "韩文（字体无字形）"),
        ("张三😀", "emoji"),
        ("张三\n李四", "换行"),
        ("张三@所有人", "at 符号"),
        ("张三\u200b", "零宽字符"),
        ("张三 123", "数字不在允许集内"),
    ],
)
def test_invalid_display_names(name: str, reason: str) -> None:
    assert is_valid_display_name(name) is False, reason


# —— Parser：空格不再被吃掉 ——


def test_parser_bind_keeps_foreign_name_spaces() -> None:
    intent = QQCommandParser().parse("绑定 John Smith 2023X1234567890")
    assert intent.operation == "bind_user"
    assert intent.arguments["display_name"] == "John Smith"  # 旧实现是 JohnSmith
    assert intent.arguments["student_id"] == "2023X1234567890"


def test_parser_bind_chinese_still_collapses_spaces() -> None:
    intent = QQCommandParser().parse("绑定 张 三 2023X1234567890")
    assert intent.arguments["display_name"] == "张三"


def test_parser_assign_role_with_spaced_name() -> None:
    intent = QQCommandParser().parse("#添加管理 John Smith")
    assert intent.arguments["target_name"] == "John Smith"  # 旧实现是 "John"
    assert intent.arguments["role"] is None


def test_parser_assign_role_explicit_role() -> None:
    intent = QQCommandParser().parse("#添加管理 John Smith admin")
    assert intent.arguments["target_name"] == "John Smith"
    assert intent.arguments["role"] == "admin"


def test_parser_assign_role_conflicting_tail_is_name_not_role() -> None:
    """末段不是已知角色名时整串都当姓名（角色校验留给应用层）。"""
    intent = QQCommandParser().parse("#添加管理 John Smith Manager")
    assert intent.arguments["target_name"] == "John Smith Manager"
    assert intent.arguments["role"] is None


def test_parser_remove_role_rejects_extra_tokens() -> None:
    from qqbot.domain.errors import ParseError

    with pytest.raises(ParseError):
        QQCommandParser().parse("#删除管理 John Smith")


# —— 应用层：端到端绑定 ——


def _context(external_id: str) -> RequestContext:
    return RequestContext(
        request_id=str(uuid4()),
        source="qq",
        site_id="site-yql",
        identity=ExternalIdentity("qq", external_id),
        actor_user_id=None,
        received_at=datetime(2026, 9, 17, 12, tzinfo=SHANGHAI_TZ),
    )


@pytest.fixture
def app_env(yql_config):
    from qqbot.infrastructure.sqlite_repository import SQLiteBookingRepository

    repo = SQLiteBookingRepository(yql_config)
    repo.initialize()
    return Dispatcher(BookingApplication(yql_config, repo)), repo


@pytest.mark.parametrize(
    ("name", "student_id"),
    [
        ("John Smith", "2024K8009926001"),
        ("O'Brien", "2024K8009926002"),
        ("阿依古丽·麦麦提", "2024K8009926003"),
        ("张 三", "2024K8009926005"),
    ],
)
def test_bind_accepts_relaxed_names(app_env, name: str, student_id: str) -> None:
    dispatcher, repo = app_env
    result = dispatcher.dispatch(_context(f"qq-{student_id}"), BindUser(name, student_id))
    assert result.code == "user_bound", result.data
    stored = repo.user_by_name(normalize_display_name(name))
    assert stored is not None
    assert stored.display_name == normalize_display_name(name)


def test_bind_normalized_name_collides_with_existing(app_env) -> None:
    """归一化在查重之前生效：「张三」已存在时「张 三」应判重名而不是新建。"""
    dispatcher, _ = app_env
    assert dispatcher.dispatch(_context("qq-a"), BindUser("张三", "2024K8009926004")).code == "user_bound"
    result = dispatcher.dispatch(_context("qq-b"), BindUser("张 三", "2024K8009926005"))
    assert result.code == "duplicate_identity"
    assert result.data.get("field") == "display_name"


def test_bind_rejects_cyrillic_name(app_env) -> None:
    dispatcher, _ = app_env
    result = dispatcher.dispatch(_context("qq-cy"), BindUser("Иван", "2024K8009926010"))
    assert result.code == "invalid_name"


def test_bind_normalizes_before_persisting(app_env) -> None:
    """入库的一定是规范形式：带多余空格的姓名应能按规范名查到人。"""
    dispatcher, repo = app_env
    result = dispatcher.dispatch(_context("qq-norm"), BindUser("John   Smith ", "2024K8009926011"))
    assert result.code == "user_bound"
    assert repo.user_by_name("John Smith") is not None
    assert repo.user_by_name("John   Smith") is None
