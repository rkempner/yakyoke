"""The agent loop.

This is the core of yakyoke: a ReAct-style loop that calls an LLM, dispatches
any tool calls, appends the results, and repeats until the model produces a
text-only reply or hits the step cap.

CRITICAL: this module imports no concrete implementation. It depends only on
the interfaces (LLM, ToolRegistry, Memory). The Worker assembles concrete
instances and passes them in. This is what lets us swap providers, queues,
and storage backends in later versions without ever rewriting the loop.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from yakyoke.llm import LLM, LLMResponse, ToolCall
from yakyoke.memory import Memory
from yakyoke.models import Task
from yakyoke.tools.registry import ToolRegistry

DEFAULT_SYSTEM_PROMPT = """\
You are an autonomous task-execution agent running inside the yakyoke daemon.
You have been given a task to complete. You have access to a set of tools to
help you do it.

Guidelines:
- Think step by step. Decide what you need, call tools to get it, then use
  the results to make progress.
- When you have completed the task, write your final result to a file inside
  the workspace using the filesystem_write tool. Use a sensible filename like
  result.md unless the task specifies otherwise.
- After writing the final result, produce a brief textual summary of what you
  did. That textual reply (with no tool calls) ends the task.
- Be concise. Don't narrate every step in your text; the trace already
  records everything.
- If a tool returns an error, read it carefully and try a different approach.
  Don't repeat the same failing call.
- If you genuinely cannot complete the task, say so plainly in your final
  reply rather than fabricating an answer.
"""


class TraceLogger:
    """Append-only JSONL logger for an agent run.

    One JSON object per line, written immediately on each event so a crash
    mid-run still leaves a usable trace on disk.
    """

    def __init__(self, trace_path: Path):
        self.trace_path = trace_path
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        # Truncate any prior trace at start. Tasks own their workspace, so
        # this is safe; nothing else writes here.
        trace_path.write_text("")

    def log(self, event_type: str, **fields: Any) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            **fields,
        }
        with self.trace_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")


class AgentLoop:
    """Pure agent loop. No HTTP, no DB, no concrete implementations.

    Construct with the dependencies it needs, then call run(task). Returns
    the final text reply from the model. Tool dispatch, trace logging, and
    workspace handling all happen internally.
    """

    def __init__(
        self,
        llm: LLM,
        tools: ToolRegistry,
        memory: Memory,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    ):
        self.llm = llm
        self.tools = tools
        self.memory = memory
        self.system_prompt = system_prompt

    def run(self, task: Task) -> str:
        """Execute the task. Returns the final assistant text.

        Raises on hard failures (LLM error, step cap exceeded with no
        terminal reply). The Worker catches and marks the task failed.
        """
        workspace = task.workspace
        workspace.mkdir(parents=True, exist_ok=True)

        trace = TraceLogger(task.trace_path)
        trace.log("start", task_id=task.id, model=task.model, prompt=task.prompt)

        # Filter the registry by the task's tool allowlist.
        allowed_tools = self.tools.filtered(task.tools)

        # Per-task system prompt: tack on the workspace path so the model
        # knows where it can write.
        system = (
            f"{self.system_prompt}\n\n"
            f"Your workspace directory is: {workspace}\n"
            f"Available tools: {', '.join(allowed_tools.names()) or '(none)'}"
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": task.prompt},
        ]

        for step in range(1, task.max_steps + 1):
            t0 = time.monotonic()
            response = self.llm.complete(
                messages=messages,
                tool_schemas=allowed_tools.schemas() if allowed_tools.names() else None,
                model=task.model,
            )
            elapsed_ms = int((time.monotonic() - t0) * 1000)

            trace.log(
                "llm_call",
                step=step,
                elapsed_ms=elapsed_ms,
                text=response.text,
                tool_calls=[
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                    for tc in response.tool_calls
                ],
            )

            # Always record the assistant message in history (with tool_calls
            # if present), so the model sees a coherent thread.
            messages.append(self._assistant_message(response))

            if not response.has_tool_calls:
                # Text-only reply: we're done. Persist the final text as the
                # task result and return.
                trace.log("done", step=step, final_text=response.text)
                return response.text

            # Dispatch tool calls and append their results.
            for tc in response.tool_calls:
                result_text = self._dispatch_tool(tc, workspace, trace, step)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result_text,
                    }
                )

        trace.log("error", reason="max_steps_exceeded")
        raise RuntimeError(f"agent exceeded max_steps={task.max_steps}")

    def _assistant_message(self, response: LLMResponse) -> dict[str, Any]:
        """Build the assistant message to append to history.

        Includes tool_calls in the OpenAI-format expected by LiteLLM on the
        next call. We round-trip the model's tool call IDs so the matching
        tool result messages can reference them.
        """
        msg: dict[str, Any] = {
            "role": "assistant",
            "content": response.text or "",
        }
        if response.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                for tc in response.tool_calls
            ]
        return msg

    def _dispatch_tool(
        self,
        call: ToolCall,
        workspace: Path,
        trace: TraceLogger,
        step: int,
    ) -> str:
        """Look up and run a tool. Returns its string result.

        Errors are caught and returned as strings so the model can see
        them and react, rather than crashing the agent loop.
        """
        spec = self.tools.get(call.name)
        if spec is None:
            err = f"unknown tool: {call.name}"
            trace.log("tool_call", step=step, name=call.name, error=err)
            return err

        t0 = time.monotonic()
        try:
            result = spec.func(workspace, **call.arguments)
            success = True
        except TypeError as e:
            # Bad argument shape from the model.
            result = f"tool error ({call.name}): {e}"
            success = False
        except Exception as e:
            result = f"tool error ({call.name}): {e}"
            success = False
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        trace.log(
            "tool_call",
            step=step,
            name=call.name,
            arguments=call.arguments,
            result=result[:2000],  # cap so trace doesn't bloat
            success=success,
            elapsed_ms=elapsed_ms,
        )
        return result
