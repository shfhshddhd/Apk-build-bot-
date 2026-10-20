"""Small SQLite build history for status continuity across bot restarts."""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path


DEFAULT_DB = os.environ.get("BUILD_HISTORY_DB", "data/build_history.sqlite3")
MAX_HISTORY_ROWS = max(20, int(os.environ.get("BUILD_HISTORY_ROWS", "500")))


def _connect(db_path: str | Path = DEFAULT_DB):
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=10)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS builds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            project TEXT NOT NULL,
            engine TEXT NOT NULL,
            status TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT ''
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_builds_user_id_id ON builds(user_id, id DESC)")
    return connection


def record_build(user_id: int, project: str, engine: str, status: str,
                 summary: str = "", db_path: str | Path = DEFAULT_DB) -> int:
    """Insert one lifecycle record and prune oldest records globally."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO builds(user_id, created_at, project, engine, status, summary) VALUES (?, ?, ?, ?, ?, ?)",
            (int(user_id), int(time.time()), str(project)[:120], str(engine)[:40],
             str(status)[:40], str(summary)[:1000]),
        )
        conn.execute(
            "DELETE FROM builds WHERE id NOT IN (SELECT id FROM builds ORDER BY id DESC LIMIT ?)",
            (MAX_HISTORY_ROWS,),
        )
        return int(cur.lastrowid)


def recent_builds(user_id: int, limit: int = 10, db_path: str | Path = DEFAULT_DB) -> list[dict]:
    """Return only the requesting user's recent build history."""
    limit = max(1, min(int(limit), 20))
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, created_at, project, engine, status, summary FROM builds "
            "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (int(user_id), limit),
        ).fetchall()
    return [
        {"id": row[0], "created_at": row[1], "project": row[2], "engine": row[3],
         "status": row[4], "summary": row[5]}
        for row in rows
    ]
