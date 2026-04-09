"""Core data models for yakyoke.

The Task is the central object. Workers pull tasks from the Queue, run them
through the AgentLoop, and write results back to Storage. Reserved columns
(parent_id, role, depends_on, priority, scheduled_for, metadata) are present
from v0.1 even though most are unused, so the schema can grow without
migrations.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class TaskStatus(str, Enum):
    """Task lifecycle. The Queue uses these to decide what's claimable."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # Reserved for v0.5 (task trees). A parent task waiting on children.
    WAITING_FOR_CHILDREN = "waiting_for_children"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_task_id() -> str:
    # ULID-ish but simpler: just a uuid4 with a tsk_ prefix.
    return f"tsk_{uuid.uuid4().hex[:24]}"


@dataclass
class Task:
    """A unit of work for the agent.

    Field groups:
      - identity & lifecycle: id, status, timestamps, error
      - request: prompt, model, tools, max_steps
      - filesystem: workspace_path (per-task scratch)
      - reserved (v0.5+): parent_id, role, depends_on, priority,
        scheduled_for, metadata
    """

    id: str = field(default_factory=_new_task_id)
    status: TaskStatus = TaskStatus.PENDING

    # The user's request.
    prompt: str = ""
    model: str = ""

    # Allowlist of tool names this task can call. Empty means "all registered".
    tools: list[str] = field(default_factory=list)

    # Cap on agent loop iterations.
    max_steps: int = 12

    # Per-task scratch directory. Tools that touch the filesystem are scoped here.
    workspace_path: str = ""

    # Lifecycle timestamps (ISO 8601 UTC strings).
    created_at: str = field(default_factory=_now_iso)
    started_at: str | None = None
    completed_at: str | None = None

    # Populated on terminal states.
    error: str | None = None
    result_path: str | None = None  # Filesystem path to result.md

    # ----- Reserved columns: present in v0.1, used in later versions -----

    # v0.5: task trees. Parent task that spawned this one.
    parent_id: str | None = None

    # v0.7: specialized agents. Determines system prompt template.
    role: str | None = None

    # v0.5: task dependencies. List of task IDs that must complete first.
    depends_on: list[str] = field(default_factory=list)

    # Future: priority queue. Higher = sooner.
    priority: int = 0

    # Future: scheduled tasks. ISO timestamp; only claim when now >= this.
    scheduled_for: str | None = None

    # Universal escape hatch for forward-compat.
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        """Serialize for SQLite insert/update.

        List/dict fields are JSON-encoded since SQLite has no native support.
        """
        return {
            "id": self.id,
            "status": self.status.value,
            "prompt": self.prompt,
            "model": self.model,
            "tools": json.dumps(self.tools),
            "max_steps": self.max_steps,
            "workspace_path": self.workspace_path,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "result_path": self.result_path,
            "parent_id": self.parent_id,
            "role": self.role,
            "depends_on": json.dumps(self.depends_on),
            "priority": self.priority,
            "scheduled_for": self.scheduled_for,
            "metadata": json.dumps(self.metadata),
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Task":
        """Deserialize from a SQLite row (dict-like)."""
        return cls(
            id=row["id"],
            status=TaskStatus(row["status"]),
            prompt=row["prompt"],
            model=row["model"],
            tools=json.loads(row["tools"] or "[]"),
            max_steps=row["max_steps"],
            workspace_path=row["workspace_path"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            error=row["error"],
            result_path=row["result_path"],
            parent_id=row["parent_id"],
            role=row["role"],
            depends_on=json.loads(row["depends_on"] or "[]"),
            priority=row["priority"],
            scheduled_for=row["scheduled_for"],
            metadata=json.loads(row["metadata"] or "{}"),
        )

    @property
    def workspace(self) -> Path:
        return Path(self.workspace_path)

    @property
    def trace_path(self) -> Path:
        return self.workspace / "trace.jsonl"

    def is_terminal(self) -> bool:
        return self.status in (
            TaskStatus.DONE,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        )
