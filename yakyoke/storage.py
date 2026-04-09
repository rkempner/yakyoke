"""Storage interface and SQLite implementation.

This module is deliberately separated from queue.py even though both are
backed by the same SQLite file in v0.1. The interfaces are the contract;
the shared backing store is an implementation detail. When we want to swap
the queue for Redis or storage for Postgres, only one side moves.

Schema lives here. Queue's atomic claim logic lives in queue.py but operates
on the same `tasks` table.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Protocol

from yakyoke.models import Task, TaskStatus

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    prompt TEXT NOT NULL,
    model TEXT NOT NULL,
    tools TEXT NOT NULL DEFAULT '[]',
    max_steps INTEGER NOT NULL DEFAULT 12,
    workspace_path TEXT NOT NULL,

    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,

    error TEXT,
    result_path TEXT,

    -- Reserved for v0.5+ but live in the schema from day one.
    parent_id TEXT REFERENCES tasks(id),
    role TEXT,
    depends_on TEXT NOT NULL DEFAULT '[]',
    priority INTEGER NOT NULL DEFAULT 0,
    scheduled_for TEXT,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);

CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    step INTEGER NOT NULL,
    tool_name TEXT NOT NULL,
    success INTEGER NOT NULL,
    duration_ms INTEGER,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_task ON tool_calls(task_id);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with the settings we want everywhere."""
    conn = sqlite3.connect(
        db_path,
        isolation_level=None,  # autocommit; we manage transactions explicitly
        timeout=30.0,
        check_same_thread=False,  # daemon and worker share the connection pool
    )
    conn.row_factory = sqlite3.Row
    # WAL gives us concurrent readers + a single writer without locking the
    # whole file. Critical for the daemon-and-worker pattern even with one
    # worker, and free for multi-worker later.
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(db_path: Path) -> None:
    """Create tables and indexes if they don't exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
    finally:
        conn.close()


class Storage(Protocol):
    """Persistent task state. The 'what does task X look like?' interface."""

    def create_task(self, task: Task) -> None: ...
    def get_task(self, task_id: str) -> Task | None: ...
    def update_task(self, task_id: str, **fields: Any) -> None: ...
    def list_tasks(
        self,
        status: TaskStatus | None = None,
        limit: int = 50,
    ) -> list[Task]: ...
    def record_tool_call(
        self,
        task_id: str,
        step: int,
        tool_name: str,
        success: bool,
        duration_ms: int,
    ) -> None: ...


class SQLiteStorage:
    """SQLite-backed Storage. Holds the connection; not thread-local.

    SQLite with check_same_thread=False is safe for our usage pattern: each
    operation is a single statement (or transaction), and WAL mode handles
    concurrency between the daemon and worker threads.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        init_db(db_path)
        self._conn = _connect(db_path)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Explicit transaction. Rolls back on exception."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def create_task(self, task: Task) -> None:
        row = task.to_row()
        cols = ", ".join(row.keys())
        placeholders = ", ".join(f":{k}" for k in row.keys())
        with self._tx() as conn:
            conn.execute(f"INSERT INTO tasks ({cols}) VALUES ({placeholders})", row)

    def get_task(self, task_id: str) -> Task | None:
        cur = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = cur.fetchone()
        return Task.from_row(dict(row)) if row else None

    def update_task(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        # Convert TaskStatus enums to their string value automatically.
        normalized: dict[str, Any] = {}
        for k, v in fields.items():
            if isinstance(v, TaskStatus):
                normalized[k] = v.value
            else:
                normalized[k] = v
        set_clause = ", ".join(f"{k} = :{k}" for k in normalized.keys())
        normalized["__id"] = task_id
        with self._tx() as conn:
            conn.execute(
                f"UPDATE tasks SET {set_clause} WHERE id = :__id",
                normalized,
            )

    def list_tasks(
        self,
        status: TaskStatus | None = None,
        limit: int = 50,
    ) -> list[Task]:
        if status:
            cur = self._conn.execute(
                "SELECT * FROM tasks WHERE status = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (status.value, limit),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        return [Task.from_row(dict(r)) for r in cur.fetchall()]

    def record_tool_call(
        self,
        task_id: str,
        step: int,
        tool_name: str,
        success: bool,
        duration_ms: int,
    ) -> None:
        from datetime import datetime, timezone

        with self._tx() as conn:
            conn.execute(
                "INSERT INTO tool_calls "
                "(task_id, step, tool_name, success, duration_ms, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    step,
                    tool_name,
                    1 if success else 0,
                    duration_ms,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def close(self) -> None:
        self._conn.close()
