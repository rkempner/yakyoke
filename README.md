# yakyoke

A local-first agent daemon. Bring your own LLM. Yokes the yak.

`yakyoke` is a small autonomous agent harness designed to run as a persistent
background process on your laptop. You submit tasks over HTTP, the daemon
queues them, a worker pulls them, and an agent loop runs them through the
LLM of your choice with a set of pluggable tools.

Unlike most agent frameworks, yakyoke is:

- **Local-first.** Runs on your machine. Stores state in SQLite. No cloud
  required.
- **LLM-agnostic.** Uses [LiteLLM](https://github.com/BerriAI/litellm) under
  the hood, so the same daemon can route tasks to Claude, GPT-4, local
  Ollama models, or anything else LiteLLM speaks.
- **Persistent.** State survives daemon restarts. Tasks survive crashes.
- **Commandable.** A simple HTTP API plus a `yk` CLI client. You can also
  drive it from cron, webhooks, iOS shortcuts, or other agent harnesses.
- **Designed to grow.** v0.1 is single-worker, single-machine, and minimal.
  The interfaces (queue, storage, llm, tools, memory) are clean enough that
  multi-worker, task trees, and graph-backed memory are additive in later
  versions, not rewrites.

## Why "yakyoke"?

A **yak** is what you shave when you wander off-task to do tangential
work. A **yoke** is what you put on a draft animal to make it pull weight.
Yakyoke is the harness that puts the wandering, talkative thing to actual
work.

## Status

v0.1. Single worker. Local SQLite. Five built-in tools (web search, URL
fetch, filesystem read/write/list). Works against any LiteLLM-supported
provider.

## Quickstart

```bash
git clone https://github.com/yourname/yakyoke
cd yakyoke
python -m venv .venv && source .venv/bin/activate
pip install -e .

# Configure your LLM. For local Ollama:
export YAKYOKE_DEFAULT_MODEL="ollama/gemma3:27b"

# Or for Claude:
export ANTHROPIC_API_KEY="sk-..."
export YAKYOKE_DEFAULT_MODEL="claude-opus-4-6"

# Start the daemon (runs the worker thread too in v0.1):
yakyoke daemon

# In another terminal, run a task:
yk run "Search for recent FDA approvals and write a brief summary to result.md"
```

## CLI

```
yakyoke daemon              start the daemon (HTTP server + worker)
yakyoke run "..."           submit a task and wait for it to complete
yakyoke submit "..."        submit a task and return immediately
yakyoke status <id>         show task state
yakyoke list                list recent tasks
yakyoke trace <id>          print the JSONL execution trace
yakyoke result <id>         print the task's result file
yakyoke cancel <id>         cancel a task
yakyoke health              check daemon liveness
```

`yk` is a short alias for `yakyoke`.

## HTTP API

```
POST   /tasks               create a task
GET    /tasks               list tasks (?status=...&limit=...)
GET    /tasks/{id}          get task state
DELETE /tasks/{id}          cancel a task
GET    /tasks/{id}/trace    JSONL execution trace
GET    /tasks/{id}/result   the task's result file
GET    /health              liveness check
```

Default bind: `127.0.0.1:8765`.

## Configuration

Environment variables (a `.env` file in the working directory is also
loaded):

| Variable                | Default                | Notes |
|---|---|---|
| `YAKYOKE_DATA_DIR`      | `~/.yakyoke`          | Where the SQLite db and per-task workspaces live |
| `YAKYOKE_DEFAULT_MODEL` | `ollama/gemma3:27b`   | LiteLLM model name |
| `YAKYOKE_HOST`          | `127.0.0.1`           | HTTP bind host |
| `YAKYOKE_PORT`          | `8765`                | HTTP bind port |
| `YAKYOKE_MAX_STEPS`     | `12`                  | Cap on agent loop iterations per task |
| `ANTHROPIC_API_KEY`     | (none)                | Required for Claude models |
| `OPENAI_API_KEY`        | (none)                | Required for OpenAI models |
| `OLLAMA_API_BASE`       | `http://localhost:11434` | Where LiteLLM finds Ollama |

## Architecture

```
   ┌──────────┐
   │  client  │  (CLI, webhook, cron, Claude Code, ...)
   └────┬─────┘
        │ HTTP
   ┌────▼─────────────────────────────┐
   │  daemon  (FastAPI)               │
   │   ├── POST /tasks  ──┐           │
   │   ├── GET  /tasks/.. │           │
   │   └── ...            │           │
   │                      ▼           │
   │  ┌──────────┐  ┌───────────┐    │
   │  │ Storage  │  │  Queue    │    │
   │  │ (SQLite) │  │ (SQLite)  │    │
   │  └────┬─────┘  └────┬──────┘    │
   │       │             │ claim     │
   │       │      ┌──────▼────────┐  │
   │       │      │   Worker      │  │
   │       │      │   ┌────────┐  │  │
   │       └─────▶│   │ Agent  │  │  │
   │              │   │ Loop   │  │  │
   │              │   └───┬────┘  │  │
   │              │       │       │  │
   │              │  ┌────▼───┐   │  │
   │              │  │  LLM   │   │  │ ── LiteLLM ──▶ Anthropic / Ollama / OpenAI / ...
   │              │  └────────┘   │  │
   │              │  ┌────────┐   │  │
   │              │  │ Tools  │   │  │
   │              │  └────────┘   │  │
   │              │  ┌────────┐   │  │
   │              │  │ Memory │   │  │ (NoMemory in v0.1)
   │              │  └────────┘   │  │
   │              └───────────────┘  │
   └──────────────────────────────────┘
```

The agent loop never imports a concrete provider, queue, storage, or memory.
It takes them as parameters. This is what lets later versions swap
implementations without rewriting the loop.

### Per-task workspaces

Each task gets its own scratch directory under `~/.yakyoke/tasks/<task_id>/`.
Filesystem tools are scoped to it. The task's `result.md` and `trace.jsonl`
live there. This is what makes parallel workers safe in v0.2 with no
changes to tools.

## Roadmap

| Version | Adds |
|---|---|
| **v0.1** | Skeleton: 1 worker, SQLite, LiteLLM, 5 tools, HTTP daemon, CLI |
| **v0.2** | Worker pool (`yakyoke worker --workers N`), launchd service |
| **v0.3** | Scheduling, recurring tasks, read-only web UI |
| **v0.4** | Sandboxed Python execution tool, cost/quality routing |
| **v0.5** | Task trees: `spawn_task` tool, parent/child coordination |
| **v0.6** | Memory layer (NanoGraph) for cross-task semantic recall |
| **v0.7** | Role-based specialized agents (researcher, writer, critic) |
| **v1.0** | Webhooks, calendar/email triggers, polished docs |

## License

MIT.
