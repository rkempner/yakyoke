"""Queue interface and SQLite implementation.

Separated from storage.py on purpose, even though both share a SQLite file.
The queue's job is *ordering*: who gets the next pending task. This is
distinct from the storage's job of "what does task X look like".

The atomic claim pattern matters even though v0.1 has only one worker. The
moment you spin up a second worker (a v0.2 config flag), `claim_next` must
not race. Doing it right from day one means no rewrite later.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from yakyoke.models import TaskStatus


class Queue(Protocol):
    """Task ordering. The 'who gets the next pending task?' interface."""

    def claim_next(self, worker_id: str) -> str | None:
        """Atomically claim the next pending task. Returns task_id or None."""
        ...

    def ack(self, task_id: str) -> None:
        """Mark a claimed task as successfully completed."""
        ...

    def nack(self, task_id: str, reason: str) -> None:
        """Mark a claimed task as failed."""
        ...

    def cancel(self, task_id: str) -> bool:
        """Cancel a pending or running task. Returns True if cancelled."""
        ...


class SQLiteQueue:
    """SQLite-backed queue. Shares the tasks table with SQLiteStorage.

    The atomic claim uses UPDATE...RETURNING (SQLite 3.35+, available
    everywhere modern). The WHERE clause checks status='pending' so two
    workers can never both claim the same task: whichever runs the UPDATE
    first wins, the other gets zero rows back.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        # Reuse the same connection model as storage. Separate connection
        # is fine because SQLite WAL mode lets multiple connections safely
        # read/write the same file.
        self._conn = sqlite3.connect(
            db_path,
            isolation_level=None,
            timeout=30.0,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def claim_next(self, worker_id: str) -> str | None:
        """Atomically transition a pending task to running and return its id.

        We pick the highest-priority, oldest pending task that's eligible
        to run now (scheduled_for is null or in the past). The UPDATE is
        guarded by status='pending' so it cannot race.
        """
        now = self._now()
        cur = self._conn.execute(
            """
            UPDATE tasks
               SET status = 'running',
                   started_at = ?
             WHERE id = (
                 SELECT id FROM tasks
                  WHERE status = 'pending'
                    AND (scheduled_for IS NULL OR scheduled_for <= ?)
                  ORDER BY priority DESC, created_at ASC
                  LIMIT 1
             )
         RETURNING id
            """,
            (now, now),
        )
        row = cur.fetchone()
        return row["id"] if row else None

    def ack(self, task_id: str) -> None:
        """Mark a running task as done. Worker calls this on success."""
        self._conn.execute(
            "UPDATE tasks SET status = ?, completed_at = ? "
            "WHERE id = ? AND status = 'running'",
            (TaskStatus.DONE.value, self._now(), task_id),
        )

    def nack(self, task_id: str, reason: str) -> None:
        """Mark a running task as failed. Worker calls this on exception."""
        self._conn.execute(
            "UPDATE tasks SET status = ?, completed_at = ?, error = ? "
            "WHERE id = ? AND status = 'running'",
            (TaskStatus.FAILED.value, self._now(), reason, task_id),
        )

    def cancel(self, task_id: str) -> bool:
        """Cancel a pending or running task.

        Pending tasks are simply transitioned to cancelled. Running tasks
        are also marked cancelled, but in v0.1 the worker won't notice
        until it finishes its current iteration. Cooperative cancellation
        comes in v0.2.
        """
        cur = self._conn.execute(
            "UPDATE tasks SET status = ?, completed_at = ? "
            "WHERE id = ? AND status IN ('pending', 'running')",
            (TaskStatus.CANCELLED.value, self._now(), task_id),
        )
        return cur.rowcount > 0

    def close(self) -> None:
        self._conn.close()
