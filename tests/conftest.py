from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from qqbot.infrastructure.config import SiteConfig, load_site_config


@pytest.fixture
def yql_config(tmp_path: Path) -> SiteConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_site_config(root / "configs" / "yql.yaml", project_root=root)
    return replace(
        config,
        db_path=tmp_path / "yql.db",
        default_owner_external_id="owner-external",
    )


@pytest.fixture
def yqh_config(tmp_path: Path) -> SiteConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_site_config(root / "configs" / "yqh.yaml", project_root=root)
    return replace(
        config,
        db_path=tmp_path / "yqh.db",
        default_owner_external_id="owner-external",
    )
