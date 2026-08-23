from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from qqbot.infrastructure.config import load_all_configs


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    configs = load_all_configs(root / "configs", project_root=root)
    errors = 0
    for bot_id, config in configs.items():
        credentials = "已设置" if config.appid and config.secret else "未设置（检查环境变量）"
        print(f"[{bot_id}] site_id={config.site_id} rooms={len(config.rooms)} credentials={credentials}")
        if not config.db_path.exists():
            print(f"  数据库尚未创建：{config.db_path}")
            continue
        try:
            with closing(sqlite3.connect(f"file:{config.db_path}?mode=ro", uri=True)) as conn:
                check = conn.execute("PRAGMA quick_check").fetchone()[0]
                new_count = (
                    conn.execute("SELECT COUNT(*) FROM app_reservations").fetchone()[0]
                    if table_exists(conn, "app_reservations")
                    else 0
                )
                legacy_count = (
                    conn.execute("SELECT COUNT(*) FROM reservations").fetchone()[0]
                    if table_exists(conn, "reservations")
                    else 0
                )
                issue_count = (
                    conn.execute("SELECT COUNT(*) FROM app_migration_issues").fetchone()[0]
                    if table_exists(conn, "app_migration_issues")
                    else 0
                )
            print(f"  quick_check={check} v3预约={new_count} 旧预约={legacy_count} 迁移问题={issue_count}")
            if check != "ok":
                errors += 1
            if issue_count:
                errors += 1
                print("  请检查 app_migration_issues，处理后再上线")
        except sqlite3.Error as exc:
            errors += 1
            print(f"  数据库读取失败：{exc}")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
