from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from qqbot.infrastructure.config import load_all_configs
from qqbot.infrastructure.group_bindings import GroupBindingStore
from qqbot.infrastructure.sqlite_repository import SQLiteBookingRepository


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    configs = load_all_configs(root / "configs", project_root=root)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    for bot_id, config in configs.items():
        if config.db_path.exists():
            backup = config.db_path.with_name(f"{config.db_path.name}.pre_v3_{stamp}.bak")
            shutil.copy2(config.db_path, backup)
            print(f"[{bot_id}] 已备份：{backup}")
        repository = SQLiteBookingRepository(config)
        repository.initialize()
        with closing(sqlite3.connect(config.db_path)) as conn:
            check = conn.execute("PRAGMA quick_check").fetchone()[0]
            count = conn.execute("SELECT COUNT(*) FROM app_reservations WHERE deleted_at IS NULL").fetchone()[
                0
            ]
            issues = conn.execute("SELECT COUNT(*) FROM app_migration_issues").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"[{bot_id}] 数据库检查失败：{check}")
        print(f"[{bot_id}] 升级完成；当前有效预约 {count} 条；迁移问题 {issues} 条")
        if issues:
            print(f"[{bot_id}] 请先运行 doctor 并处理迁移问题，不要启动 Bot")

    bindings = GroupBindingStore(root / "data" / "control.db")
    bindings.initialize()
    imported = bindings.import_legacy_json(root / "group_mappings.json")
    print(f"群绑定升级完成；从旧 JSON 新导入 {imported} 条")


if __name__ == "__main__":
    main()
