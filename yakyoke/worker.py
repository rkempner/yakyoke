"""Worker process. Pulls tasks from the queue and runs them.

The Worker is the only place that knows about all the concrete pieces:
storage, queue, llm, tools, memory. It assembles them and hands them to
the agent loop.

In v0.1 the worker runs as a thread inside the daemon process. In v0.2 it
will become a separate command (`yakyoke worker`) so you can run multiple.
The agent loop, queue, and storage do not change.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from pathlib import Path

from yakyoke.agent import AgentLoop
from yakyoke.config import Config
from yakyoke.llm import LLM
from yakyoke.memory import NoMemory
from yakyoke.models import Task, TaskStatus
from yakyoke.queue import SQLiteQueue
from yakyoke.storage import SQLiteStorage
from yakyoke.tools.registry import ToolRegistry, build_default_registry

log = logging.getLogger("yakyoke.worker")


class Worker:
    """Single-task worker. v0.1 runs one of these in a background thread."""

    def __init__(
        self,
        config: Config,
        storage: SQLiteStorage,
        queue: SQLiteQueue,
        tools: ToolRegistry | None = None,
    ):
        self.config = config
        self.storage = storage
        self.queue = queue
        self.tools = tools or build_default_registry()
        self.llm = LLM(default_model=config.default_model)
        self.memory = NoMemory()
        self.agent = AgentLoop(
            llm=self.llm,
            tools=self.tools,
            memory=self.memory,
        )
        self.worker_id = f"{socket.gethostname()}-{threading.get_ident()}"
        self._stop_event = threading.Event()

    def run_forever(self, poll_interval: float = 0.5) -> None:
        """Main loop. Polls the queue and runs tasks until stopped."""
        log.info("worker started: %s", self.worker_id)
        while not self._stop_event.is_set():
            task_id = self.queue.claim_next(self.worker_id)
            if task_id is None:
                # Nothing to do. Sleep a bit, but allow stop to interrupt.
                self._stop_event.wait(poll_interval)
                continue
            self._run_one(task_id)
        log.info("worker stopped: %s", self.worker_id)

    def stop(self) -> None:
        self._stop_event.set()

    def _run_one(self, task_id: str) -> None:
        """Run a single task. Catches all exceptions and nacks on failure."""
        task = self.storage.get_task(task_id)
        if task is None:
            log.error("claimed task vanished: %s", task_id)
            return

        log.info("running task %s (%s)", task.id, task.model or "default-model")

        # Refresh model from config if the task didn't specify one.
        if not task.model:
            task = self._patch_model(task)

        try:
            final_text = self.agent.run(task)
        except Exception as e:
            log.exception("task %s failed", task.id)
            self.queue.nack(task.id, str(e))
            return

        # Persist the final text as result.md if the agent didn't write one.
        result_path = task.workspace / "result.md"
        if not result_path.exists():
            result_path.write_text(final_text, encoding="utf-8")

        self.storage.update_task(
            task.id,
            result_path=str(result_path),
        )
        self.queue.ack(task.id)
        log.info("task %s done", task.id)

    def _patch_model(self, task: Task) -> Task:
        """Fill in the default model if the task left it blank."""
        self.storage.update_task(task.id, model=self.config.default_model)
        task.model = self.config.default_model
        return task


def create_task_workspace(tasks_dir: Path, task_id: str) -> Path:
    """Create the per-task scratch directory and return its path."""
    workspace = tasks_dir / task_id
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def start_background_worker(
    config: Config,
    storage: SQLiteStorage,
    queue: SQLiteQueue,
) -> tuple[Worker, threading.Thread]:
    """Spin up a Worker in a daemon thread. Returns (worker, thread).

    The daemon (FastAPI app) calls this on startup so v0.1 has one process
    to manage. v0.2 splits the worker into its own command.
    """
    worker = Worker(config, storage, queue)
    thread = threading.Thread(target=worker.run_forever, daemon=True, name="yakyoke-worker")
    thread.start()
    return worker, thread
