"""FastAPI daemon. The HTTP face of yakyoke.

Six endpoints:
  POST   /tasks              create a task
  GET    /tasks              list tasks
  GET    /tasks/{id}         get task state
  DELETE /tasks/{id}         cancel a task
  GET    /tasks/{id}/trace   read the JSONL trace
  GET    /health             liveness

In v0.1 the daemon also starts a background worker thread, so a single
`yakyoke daemon` command runs the whole system. v0.2 splits them.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from yakyoke.config import Config
from yakyoke.models import Task, TaskStatus
from yakyoke.queue import SQLiteQueue
from yakyoke.storage import SQLiteStorage
from yakyoke.worker import create_task_workspace, start_background_worker

log = logging.getLogger("yakyoke.daemon")


# ---------- request/response models ----------


class CreateTaskRequest(BaseModel):
    prompt: str = Field(..., description="The user's task description.")
    model: str | None = Field(None, description="LLM model name (LiteLLM format).")
    tools: list[str] = Field(
        default_factory=list,
        description="Tool allowlist. Empty means all registered tools.",
    )
    max_steps: int | None = Field(None, ge=1, le=100)


class TaskResponse(BaseModel):
    id: str
    status: str
    prompt: str
    model: str
    tools: list[str]
    workspace_path: str
    created_at: str
    started_at: str | None
    completed_at: str | None
    error: str | None
    result_path: str | None

    @classmethod
    def from_task(cls, task: Task) -> "TaskResponse":
        return cls(
            id=task.id,
            status=task.status.value,
            prompt=task.prompt,
            model=task.model,
            tools=task.tools,
            workspace_path=task.workspace_path,
            created_at=task.created_at,
            started_at=task.started_at,
            completed_at=task.completed_at,
            error=task.error,
            result_path=task.result_path,
        )


# ---------- app factory ----------


def create_app(config: Config | None = None) -> FastAPI:
    """Build a FastAPI app with the given config (or one loaded from env)."""
    cfg = config or Config.from_env()

    storage = SQLiteStorage(cfg.db_path)
    queue = SQLiteQueue(cfg.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup
        log.info("yakyoke daemon starting (data_dir=%s)", cfg.data_dir)
        worker, _thread = start_background_worker(cfg, storage, queue)
        app.state.config = cfg
        app.state.storage = storage
        app.state.queue = queue
        app.state.worker = worker
        yield
        # Shutdown
        log.info("yakyoke daemon stopping")
        worker.stop()
        queue.close()
        storage.close()

    app = FastAPI(
        title="yakyoke",
        version="0.1.0",
        description="Local-first agent daemon. Bring your own LLM.",
        lifespan=lifespan,
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": "0.1.0",
            "default_model": cfg.default_model,
            "data_dir": str(cfg.data_dir),
        }

    @app.post("/tasks", response_model=TaskResponse, status_code=201)
    def create_task(req: CreateTaskRequest) -> TaskResponse:
        task = Task(
            prompt=req.prompt,
            model=req.model or cfg.default_model,
            tools=req.tools,
            max_steps=req.max_steps or cfg.max_agent_steps,
        )
        workspace = create_task_workspace(cfg.tasks_dir, task.id)
        task.workspace_path = str(workspace)
        storage.create_task(task)
        return TaskResponse.from_task(task)

    @app.get("/tasks", response_model=list[TaskResponse])
    def list_tasks(
        status: str | None = None,
        limit: int = 50,
    ) -> list[TaskResponse]:
        status_enum = None
        if status:
            try:
                status_enum = TaskStatus(status)
            except ValueError:
                raise HTTPException(400, f"unknown status: {status}")
        tasks = storage.list_tasks(status=status_enum, limit=limit)
        return [TaskResponse.from_task(t) for t in tasks]

    @app.get("/tasks/{task_id}", response_model=TaskResponse)
    def get_task(task_id: str) -> TaskResponse:
        task = storage.get_task(task_id)
        if task is None:
            raise HTTPException(404, "task not found")
        return TaskResponse.from_task(task)

    @app.delete("/tasks/{task_id}")
    def cancel_task(task_id: str) -> dict[str, Any]:
        task = storage.get_task(task_id)
        if task is None:
            raise HTTPException(404, "task not found")
        if task.is_terminal():
            raise HTTPException(
                409, f"task is already {task.status.value}, cannot cancel"
            )
        cancelled = queue.cancel(task_id)
        return {"cancelled": cancelled, "id": task_id}

    @app.get("/tasks/{task_id}/trace", response_class=PlainTextResponse)
    def get_trace(task_id: str) -> str:
        task = storage.get_task(task_id)
        if task is None:
            raise HTTPException(404, "task not found")
        if not task.trace_path.exists():
            return ""
        return task.trace_path.read_text(encoding="utf-8")

    @app.get("/tasks/{task_id}/result", response_class=PlainTextResponse)
    def get_result(task_id: str) -> str:
        task = storage.get_task(task_id)
        if task is None:
            raise HTTPException(404, "task not found")
        if not task.result_path:
            raise HTTPException(404, "no result yet")
        from pathlib import Path

        return Path(task.result_path).read_text(encoding="utf-8")

    return app


# Module-level app for `uvicorn yakyoke.daemon:app`.
app = create_app()
