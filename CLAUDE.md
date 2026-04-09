# yakyoke -- working notes for AI coding agents

This file is committed on purpose. It exists so that any AI agent working
on this codebase (Claude Code, Cursor, Aider, etc.) starts with the same
load-bearing context that the original author has. Read it before making
non-trivial changes.

## What yakyoke is, in one paragraph

A local-first, LLM-agnostic agent daemon. Tasks arrive over HTTP, get
queued in SQLite, are claimed by a worker, and run through a ReAct-style
agent loop with pluggable tools. The whole system is designed to run
persistently on a single machine (typically a laptop) and accept commands
from anywhere -- CLI, cron, webhooks, other agents. It is NOT a library,
NOT a cloud service, NOT a multi-tenant platform.

## The single most important architectural rule

**The agent loop imports zero concrete implementations.** It depends only
on interfaces (`LLM`, `ToolRegistry`, `Memory`). The Worker assembles
concrete instances and passes them in. This is what allows future versions
to swap providers, queues, storage backends, and memory implementations
without ever rewriting the loop.

If you find yourself wanting to add `from yakyoke.queue import SQLiteQueue`
or `import litellm` directly into `agent.py`, stop. That's the wrong move.
Add it to the worker, the daemon, or a new abstraction. The agent loop
stays clean.

## Other load-bearing decisions

These were chosen carefully and breaking them creates rework that compounds:

1. **Storage and Queue are separate interfaces.** They share a SQLite
   file in v0.1, but `storage.py` and `queue.py` import nothing from each
   other. The day you want a Redis-backed queue, only `queue.py` moves.

2. **Atomic claim from day one.** `claim_next` uses
   `UPDATE ... WHERE status='pending' RETURNING id`. Never write
   "SELECT then UPDATE" -- it works for one worker but not for two, and
   rewriting it later is exactly the kind of subtle bug that bites in
   production. Multi-worker support is "free" as a v0.2 config flag
   precisely because this was right on day one.

3. **Tools take `workspace: Path` as the first argument, always.** Even
   tools that don't touch the filesystem (like `web_search`) follow this
   signature. This is what makes parallel workers safe in v0.2 with zero
   tool changes -- a tool can never collide with another worker's task
   because they have different workspace dirs.

4. **Filesystem tools cannot escape the workspace.** `_resolve_within_workspace`
   in `tools/filesystem.py` rejects any path that resolves outside the task's
   workspace. This is the security boundary. If you add a new filesystem
   tool, use the same helper. Do not bypass it.

5. **Tools return strings; tool errors are also strings.** A tool function
   that raises is a bug in the tool. A tool that catches an error and
   returns `f"failed: {e}"` lets the model see and react to the error.
   Keep the convention.

6. **Trace is append-only JSONL.** One JSON object per line, written
   immediately on each event. Survives crashes mid-run. Don't switch to
   nested JSON or batched writes -- the append-immediately property is
   load-bearing for debuggability.

7. **Reserved schema columns.** `parent_id`, `role`, `depends_on`,
   `priority`, `scheduled_for`, `metadata` exist in the v0.1 schema even
   though they're unused. They're for v0.5+ task trees and v0.7+ role
   specialization. Adding columns to SQLite later requires migrations and
   adds friction. Leaving them in the schema costs nothing.

8. **The `Memory` interface is a stub in v0.1.** `NoMemory` does nothing
   and returns nothing. The agent loop calls `memory.recall()` and
   `memory.remember()` unconditionally so that v0.5 can plug in a real
   NanoGraph-backed implementation without changing a single line in the
   loop. Don't gate the calls behind `if memory is not None` -- that
   defeats the purpose.

## Conventions for the codebase

- **Python 3.10+.** Use modern syntax: `X | Y` unions, `match` statements,
  type hints throughout.
- **No ORM.** Plain `sqlite3` from stdlib. The two tables in v0.1 don't
  justify an ORM and an ORM would obscure the atomic-claim pattern.
- **No async in the agent loop.** The loop is sync. LLM calls block. This
  is intentional: async adds cognitive overhead, and the bottleneck is the
  LLM (multiple seconds per call), not orchestration. v0.2 multi-worker
  uses threads, not asyncio.
- **FastAPI is async at the HTTP layer** because uvicorn is async. The
  worker runs in a background thread of the daemon process in v0.1.
  v0.2 will split it into a separate command.
- **Errors as strings inside tools, exceptions outside.** Tools return
  error strings so the model can react. The Worker catches exceptions
  from the agent loop and converts them into task `failed` rows.
- **One file per concern.** `storage.py` is storage, `queue.py` is queue,
  `agent.py` is the agent loop, `worker.py` is the worker. If a file is
  growing past ~400 lines, that's a smell -- find the seam.

## How to add a new tool

1. Create a Python function in `yakyoke/tools/your_tool.py` with signature
   `def your_tool(workspace: Path, ...) -> str`.
2. Define a corresponding `your_tool_spec()` returning a `ToolSpec` with
   the OpenAI-format JSON schema for the tool's parameters.
3. Register it in `yakyoke/tools/registry.py::build_default_registry()`.
4. Add a smoke test in `tests/test_smoke.py` using a `FakeLLM` that
   scripts a tool call.
5. If the tool reads or writes the filesystem, use
   `_resolve_within_workspace` from `tools/filesystem.py` -- DO NOT
   write to absolute paths or `..`-relative paths.

## How to test

```bash
# Install
python3 -m venv .venv && source .venv/bin/activate
pip install -e . pytest

# Unit tests (no LLM, fast, deterministic)
pytest -v

# End-to-end against Ollama (slow, requires local Ollama running)
ollama serve &
ollama pull gemma4:e4b
YAKYOKE_DEFAULT_MODEL=ollama/gemma4:e4b yakyoke daemon &
yk run "search the web for X and write to result.md" --max-steps 8
```

The unit tests should always pass without any LLM, network, or external
service. They use a `FakeLLM` that scripts deterministic responses. If
you find yourself wanting to mark a test as "skip if no LLM", you're
testing the wrong layer.

## Lessons from real-LLM testing (worth knowing before you change the agent loop)

These were learned by actually running yakyoke against `gemma4:e4b` on
Ollama. They are why the agent loop looks the way it does. Don't undo
them without understanding what they prevent:

1. **Small models hallucinate tool names from prompt language.** The
   first version of the system prompt said "produce a text-only reply"
   and the model interpreted "reply" as a tool name and tried to call
   `text_reply()`. The current prompt avoids any word that could be
   misread as an action verb that might be a tool name. If you reword
   the system prompt, run a real test against a small model afterward.

2. **Small models loop on successful tool calls.** They write the result
   correctly, then keep re-writing the same content because they don't
   know how to terminate. The duplicate-call interceptor in `agent.py`
   catches this. The dedup tracks signatures across the **whole run**,
   not just adjacent steps -- earlier versions tracked only the previous
   step and missed cases where a hallucinated call separated two real
   duplicates.

3. **`max_steps` is your runaway protection.** Don't remove it. Don't
   make it default to "unlimited." Small models can spin forever.

4. **Backend bugs surface as `litellm.APIConnectionError`.** A specific
   llama.cpp assertion (`GGML_ASSERT([rsets->data count] == 0)`) crashes
   the Ollama backend on multi-turn tool use with certain models. yakyoke
   correctly catches and records these as task failures. If you see this
   error, it's not a yakyoke bug -- it's upstream. Try a different model.

## How to NOT break things

A non-exhaustive list of refactors that look helpful but break the
architecture:

- **Don't merge `storage.py` and `queue.py`.** They share a backing file,
  not an interface. Merging them defeats the swap-the-implementation goal.
- **Don't import `litellm` outside `llm.py`.** That file is the seam. If
  you need a feature LiteLLM doesn't expose, add it to the LLM wrapper.
- **Don't add concrete imports to `agent.py`.** Already covered above
  but worth repeating.
- **Don't make tools take a database connection or daemon reference.**
  Tools take a workspace path and their own arguments. If a tool needs
  state, it lives in its own files inside the workspace. v0.5 will add
  a memory parameter; until then, tools are pure.
- **Don't widen the filesystem traversal protection.** If you find
  yourself needing a tool to read outside the workspace, you're solving
  the wrong problem. Either make the relevant data part of the workspace
  setup, or add a properly-authorized escape hatch (with explicit user
  consent at submit time). Don't just relax `_resolve_within_workspace`.
- **Don't add a global state singleton.** No `current_task` global, no
  `daemon_instance` global. Everything is dependency-injected.
- **Don't switch the trace from JSONL to JSON.** Append-immediately is
  the property; nested JSON breaks it.

## Roadmap

Top-level milestones live in `ROADMAP.md`. The README has the elevator
table. If you're picking up work on this project, read ROADMAP.md to see
what's already in flight and what interfaces are pre-staged for future
work.

## Style for code changes

- Match existing style. No black/ruff config in v0.1; the code is
  hand-formatted to ~88 columns with explicit type hints. Don't run a
  formatter unilaterally.
- Comments explain *why*, not *what*. The code shows what; comments are
  for the reasoning that isn't obvious from reading.
- Don't add docstrings to functions you didn't change.
- Don't add error handling for cases that can't happen. Trust internal
  code; validate at boundaries (HTTP requests, file paths from the model,
  external API responses).
- If you're tempted to add a feature flag for backwards compatibility
  with the prior version of an interface, just change the interface.
  yakyoke is pre-1.0 and explicitly does not promise stable internals.

## When in doubt

Read `agent.py`. It's the heart of the system and the cleanest illustration
of how all the pieces are supposed to fit together. If a change you're
considering would require modifying `agent.py` to know about a concrete
type, that's the signal that the change belongs somewhere else.
