from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from qqbot.infrastructure.config import SiteConfig, load_site_config

# 测试基准站点配置 = 仓库里的示例模板（生产 configs/*.yaml 不入库，各服务器不同）。
# 断言写在这些模板的取值上；改模板即改测试期望，属预期行为。
YQL_EXAMPLE = "yql.yaml.example"
YQH_EXAMPLE = "yqh.yaml.example"


@pytest.fixture
def yql_config(tmp_path: Path) -> SiteConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_site_config(root / "configs" / YQL_EXAMPLE, project_root=root)
    return replace(
        config,
        db_path=tmp_path / "yql.db",
        default_owner_external_id="owner-external",
    )


@pytest.fixture
def yqh_config(tmp_path: Path) -> SiteConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_site_config(root / "configs" / YQH_EXAMPLE, project_root=root)
    return replace(
        config,
        db_path=tmp_path / "yqh.db",
        default_owner_external_id="owner-external",
    )
