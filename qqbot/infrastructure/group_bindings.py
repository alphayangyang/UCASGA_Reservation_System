from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path


class GroupBindingStore:
    """群聊与站点配置映射；用 SQLite 替代易损坏的 JSON 文件。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS group_bindings(
                    group_id TEXT PRIMARY KEY,
                    bot_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            conn.commit()

    def get(self, group_id: str) -> str | None:
        with closing(sqlite3.connect(self.path)) as conn:
            row = conn.execute("SELECT bot_id FROM group_bindings WHERE group_id=?", (group_id,)).fetchone()
        return str(row[0]) if row else None

    def groups_for(self, bot_id: str) -> list[str]:
        """该站点绑定的全部群 ID；用于定时任务主动推送。"""
        with closing(sqlite3.connect(self.path)) as conn:
            rows = conn.execute(
                "SELECT group_id FROM group_bindings WHERE bot_id=? ORDER BY group_id", (bot_id,)
            ).fetchall()
        return [str(row[0]) for row in rows]

    def set(self, group_id: str, bot_id: str) -> None:
        with closing(sqlite3.connect(self.path, timeout=10)) as conn:
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute(
                """INSERT INTO group_bindings(group_id, bot_id) VALUES(?, ?)
                ON CONFLICT(group_id) DO UPDATE SET bot_id=excluded.bot_id, updated_at=CURRENT_TIMESTAMP""",
                (group_id, bot_id),
            )
            conn.commit()

    def import_legacy_json(self, path: str | Path) -> int:
        """兼容旧 group_mappings.json，且不覆盖新数据库中已有的绑定。"""
        legacy_path = Path(path)
        if not legacy_path.exists():
            return 0
        try:
            raw = json.loads(legacy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        if not isinstance(raw, dict):
            return 0

        imported = 0
        with closing(sqlite3.connect(self.path, timeout=10)) as conn:
            for group_id, bot_id in raw.items():
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO group_bindings(group_id, bot_id) VALUES(?, ?)",
                    (str(group_id), str(bot_id)),
                )
                imported += max(cursor.rowcount, 0)
            conn.commit()
        return imported
