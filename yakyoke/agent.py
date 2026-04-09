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
Your job is to complete the user's task in as few steps as possible.

== CRITICAL RULES ==

1. NEVER call the same tool with the same arguments twice. If a tool call
   succeeded once, the work is DONE. Calling it again is forbidden and
   wastes effort.

2. To END the task, you must produce an assistant message containing only
   plain-text content in the message content field, with NO function calls
   and NO tool_calls. Just write your summary as the message content. This
   is how the task terminates.

3. There is NO tool named "text_reply", "respond", "answer", "summarize",
   or anything similar. Do not invent tool names. The ONLY way to finish
   the task is to write plain text into your message content with the
   tool_calls field empty.

4. When filesystem_write returns a message like "wrote N chars to result.md",
   the file is SAVED. You do NOT need to call filesystem_write again.

== PROCESS ==

1. Think about what information you need.
2. Call tools to gather it (web_search, fetch_url, filesystem_read, etc.).
3. Synthesize a final result.
4. Save the result with filesystem_write (ONCE).
5. Produce one final assistant message: plain text content, no tool_calls,
   summarizing what you did. This ends the task.

== OTHER GUIDELINES ==

- Be concise. The execution trace records every step; do not narrate.
- If a tool returns an error, read it carefully and try a DIFFERENT approach.
  Do not repeat a failing call with the same arguments.
- If you genuinely cannot complete the task, say so plainly in your final
  message rather than fabricating an answer.
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

        # Track every successful tool call signature across the whole run.
        # If the model emits a call whose (name, args) exactly matches any
        # earlier successful call, we intercept it instead of re-dispatching
        # and feed back a "you already did this, stop" message. This catches
        # a common small-model failure pattern where the model writes the
        # result successfully but then keeps re-writing it (or wandering
        # through other already-done calls) instead of producing a terminal
        # plain-text message.
        seen_call_signatures: set[tuple[str, str]] = set()

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
                sig = self._call_signature(tc)

                if sig in seen_call_signatures:
                    # Duplicate of an earlier successful call anywhere in
                    # this run. Don't re-run the tool; tell the model to
                    # produce a plain-text terminal message instead.
                    result_text = (
                        f"STOP: you already called {tc.name} with these "
                        f"exact arguments earlier in this task and it "
                        f"succeeded. The work is done. To finish the task, "
                        f"write a plain-text message in the content field "
                        f"with NO tool_calls and NO function calls. Do not "
                        f"invent tool names. Just write your summary as "
                        f"message content."
                    )
                    trace.log(
                        "tool_call",
                        step=step,
                        name=tc.name,
                        arguments=tc.arguments,
                        result=result_text,
                        success=False,
                        intercepted_duplicate=True,
                        elapsed_ms=0,
                    )
                else:
                    result_text = self._dispatch_tool(tc, workspace, trace, step)
                    seen_call_signatures.add(sig)

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result_text,
                    }
                )

        trace.log("error", reason="max_steps_exceeded")
        raise RuntimeError(f"agent exceeded max_steps={task.max_steps}")

    @staticmethod
    def _call_signature(tc: ToolCall) -> tuple[str, str]:
        """Stable signature for duplicate detection.

        JSON-encodes the arguments with sorted keys so two calls with the
        same args in different dict orders compare equal.
        """
        return (tc.name, json.dumps(tc.arguments, sort_keys=True, default=str))

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
