"""Smoke tests that exercise the full pipeline without hitting a real LLM.

We use a fake LLM that scripts a deterministic sequence of responses, so
the test exercises:
  - Storage (create/get/update task)
  - Queue (atomic claim, ack)
  - Tool registry (register, dispatch)
  - Agent loop (ReAct iteration, tool dispatch, final reply)
  - Per-task workspace and trace logging

This is the test that proves the architecture holds together end-to-end
even with no API keys or models running.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from yakyoke.agent import AgentLoop
from yakyoke.llm import LLMResponse, ToolCall
from yakyoke.memory import NoMemory
from yakyoke.models import Task, TaskStatus
from yakyoke.queue import SQLiteQueue
from yakyoke.storage import SQLiteStorage
from yakyoke.tools.registry import ToolRegistry, ToolSpec


# ---------- fakes ----------


class FakeLLM:
    """LLM stub that yields scripted responses in order."""

    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.default_model = "fake/test"

    def complete(self, messages, tool_schemas=None, model=None, temperature=0.7):
        self.calls.append(
            {
                "messages": messages,
                "tool_schemas": tool_schemas,
                "model": model,
            }
        )
        if not self._responses:
            raise RuntimeError("FakeLLM ran out of scripted responses")
        return self._responses.pop(0)


def _echo_tool(workspace: Path, message: str = "") -> str:
    """Tool that just echoes its argument back."""
    return f"echo: {message}"


def _echo_tool_spec() -> ToolSpec:
    return ToolSpec(
        name="echo",
        func=_echo_tool,
        schema={
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Echo a message",
                "parameters": {
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                },
            },
        },
    )


# ---------- tests ----------


def test_storage_and_queue_lifecycle(tmp_path):
    """Storage and queue cooperate on the same SQLite file."""
    db = tmp_path / "test.db"
    storage = SQLiteStorage(db)
    queue = SQLiteQueue(db)

    task = Task(
        prompt="hello world",
        model="fake/test",
        workspace_path=str(tmp_path / "ws"),
    )
    storage.create_task(task)

    fetched = storage.get_task(task.id)
    assert fetched is not None
    assert fetched.prompt == "hello world"
    assert fetched.status == TaskStatus.PENDING

    # Atomic claim transitions pending -> running.
    claimed = queue.claim_next("worker-1")
    assert claimed == task.id

    fetched = storage.get_task(task.id)
    assert fetched.status == TaskStatus.RUNNING
    assert fetched.started_at is not None

    # Second claim returns nothing (no more pending).
    assert queue.claim_next("worker-2") is None

    # Ack moves running -> done.
    queue.ack(task.id)
    fetched = storage.get_task(task.id)
    assert fetched.status == TaskStatus.DONE
    assert fetched.completed_at is not None

    storage.close()
    queue.close()


def test_atomic_claim_no_double_assignment(tmp_path):
    """Two workers claiming simultaneously should each get a different task."""
    db = tmp_path / "test.db"
    storage = SQLiteStorage(db)
    queue = SQLiteQueue(db)

    for i in range(3):
        storage.create_task(
            Task(prompt=f"task {i}", model="fake/test", workspace_path=str(tmp_path / f"ws{i}"))
        )

    claimed_a = queue.claim_next("worker-a")
    claimed_b = queue.claim_next("worker-b")
    claimed_c = queue.claim_next("worker-c")
    claimed_d = queue.claim_next("worker-d")

    assert {claimed_a, claimed_b, claimed_c} == {
        t.id for t in storage.list_tasks(status=TaskStatus.RUNNING)
    }
    assert claimed_d is None  # only 3 tasks; the 4th claim is empty

    storage.close()
    queue.close()


def test_agent_loop_with_tool_call(tmp_path):
    """Agent loop dispatches a tool call and ends on a text-only reply."""
    workspace = tmp_path / "ws"
    workspace.mkdir()

    # Script the LLM: first response calls the echo tool, second is final text.
    fake_llm = FakeLLM(
        [
            LLMResponse(
                text="",
                tool_calls=[
                    ToolCall(id="call_1", name="echo", arguments={"message": "hi"})
                ],
                raw={},
            ),
            LLMResponse(
                text="all done",
                tool_calls=[],
                raw={},
            ),
        ]
    )

    registry = ToolRegistry()
    registry.register(_echo_tool_spec())

    loop = AgentLoop(llm=fake_llm, tools=registry, memory=NoMemory())

    task = Task(
        prompt="say hi",
        model="fake/test",
        workspace_path=str(workspace),
    )

    final_text = loop.run(task)
    assert final_text == "all done"
    assert len(fake_llm.calls) == 2

    # Trace should contain start, llm_call, tool_call, llm_call, done.
    trace_lines = task.trace_path.read_text().strip().splitlines()
    events = [json.loads(line) for line in trace_lines]
    types = [e["type"] for e in events]
    assert types == ["start", "llm_call", "tool_call", "llm_call", "done"]

    # The tool call should have run successfully.
    tool_event = next(e for e in events if e["type"] == "tool_call")
    assert tool_event["success"] is True
    assert tool_event["result"] == "echo: hi"


def test_agent_loop_handles_unknown_tool(tmp_path):
    """Unknown tool calls return an error string but don't crash the loop."""
    workspace = tmp_path / "ws"
    workspace.mkdir()

    fake_llm = FakeLLM(
        [
            LLMResponse(
                text="",
                tool_calls=[ToolCall(id="c1", name="nonexistent", arguments={})],
                raw={},
            ),
            LLMResponse(text="recovered", tool_calls=[], raw={}),
        ]
    )

    registry = ToolRegistry()  # empty registry

    loop = AgentLoop(llm=fake_llm, tools=registry, memory=NoMemory())
    task = Task(prompt="x", model="fake/test", workspace_path=str(workspace))

    result = loop.run(task)
    assert result == "recovered"


def test_filesystem_tool_blocks_traversal(tmp_path):
    """Filesystem tool refuses paths that escape the workspace."""
    from yakyoke.tools.filesystem import filesystem_write

    ws = tmp_path / "ws"
    ws.mkdir()

    # Inside the workspace: ok.
    result = filesystem_write(ws, "result.md", "hello")
    assert "wrote" in result
    assert (ws / "result.md").read_text() == "hello"

    # Escape attempt: refused.
    result = filesystem_write(ws, "../escaped.md", "nope")
    assert "refused" in result
    assert not (tmp_path / "escaped.md").exists()


def test_tool_registry_filtering():
    """Filtered registry only exposes the allowed tools."""
    full = ToolRegistry()
    full.register(_echo_tool_spec())

    # Empty allowlist returns the full registry.
    assert full.filtered([]).names() == ["echo"]

    # Specific allowlist returns only listed tools.
    sub = full.filtered(["echo"])
    assert sub.names() == ["echo"]

    # Unknown tool name silently dropped.
    sub2 = full.filtered(["nonexistent"])
    assert sub2.names() == []
