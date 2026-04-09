# yakyoke roadmap

The README has the elevator-pitch table. This is the working document.
Read this when you're picking up work on the project to see what's
already in place, what's open, and what interfaces are pre-staged for
future milestones.

## Design philosophy (the part that doesn't change)

Each milestone is **additive**. Earlier interfaces don't get rewritten;
new implementations get plugged in behind them. This is enforced by the
load-bearing rule: the agent loop imports zero concrete implementations.
See `CLAUDE.md` for the details.

If a milestone below ever requires rewriting the agent loop, the queue
interface, the tool interface, or the storage interface, that's a sign
the v0.1 interfaces were wrong. So far they've held up.

## Status legend

- ✅ shipped and validated
- 🟡 partial (some pieces in place, others pending)
- ⬜ not started
- 📌 deferred (intentionally out of scope until later)

---

## v0.1 — Skeleton ✅

**Goal:** the smallest thing that proves the architecture, end-to-end,
against a real LLM.

**Shipped:**

- ✅ FastAPI HTTP daemon (POST/GET/DELETE /tasks, /trace, /result, /health)
- ✅ Typer CLI client (10 commands: daemon, run, submit, status, list,
  trace, result, cancel, health, token)
- ✅ `SQLiteStorage` and `SQLiteQueue` as separate interfaces sharing
  one DB file (the load-bearing separation)
- ✅ `LiteLLM`-backed LLM wrapper for provider-agnostic chat completions
- ✅ Pure-function `AgentLoop` implementing ReAct with tool dispatch and
  JSONL trace
- ✅ Five tools: `web_search`, `fetch_url`, `filesystem_read`,
  `filesystem_write`, `filesystem_list`
- ✅ `NoMemory` stub behind a `Memory` interface (real impl arrives in v0.6)
- ✅ Per-task workspace isolation; filesystem tools cannot escape `..`
- ✅ Reserved schema columns (`parent_id`, `role`, `depends_on`,
  `priority`, `scheduled_for`, `metadata`) for v0.5+ task trees with no
  migration required
- ✅ Single background worker thread inside the daemon process
- ✅ Optional bearer token auth (`YAKYOKE_API_TOKEN`); `/health` stays
  open by design; constant-time comparison
- ✅ Run-wide duplicate-call interception in the agent loop (catches
  small-model loop-on-success failure pattern)
- ✅ Smoke tests covering storage, atomic claim, agent loop, tool
  dispatch, traversal blocking, dedup, auth (16 tests, no LLM required)

**Validated end-to-end against:**
- ✅ `ollama/gemma4:e4b` -- complete agent loop with web_search +
  filesystem_write, terminating cleanly in 4 steps / 16 seconds
- 🟡 `ollama/gemma4:26b` -- worked through first tool call, then crashed
  on a llama.cpp backend assertion (upstream bug, not yakyoke). yakyoke
  handled the failure correctly: task marked failed, error preserved,
  daemon stayed up.
- ⬜ Anthropic Claude (no API key in current dev environment, deferred to
  next session)
- ⬜ OpenAI (same)

**Open questions / known limitations of v0.1:**
- Small models occasionally wrap their terminal text in JSON-shaped
  envelopes. The loop terminates correctly because LiteLLM parses it as
  plain content, but the cosmetic output in the trace is ugly.
- The daemon binds localhost-only by default. Safe for the intended
  scope but means yakyoke is not reachable from the LAN without
  reconfiguration (and ideally a token).
- No graceful shutdown of in-progress tasks. Cancellation marks a row
  but the worker won't notice until its next iteration.

---

## v0.2 — Parallel and persistent ⬜

**Goal:** multiple workers, daemon survives reboots, more robust under
real use.

**Planned:**

- Split the worker into its own command (`yakyoke worker`) so it can be
  run separately from the daemon. The current single-process model
  continues to work via `yakyoke daemon` running both a worker thread
  and the HTTP server.
- `--workers N` flag on the worker command to spin up N concurrent
  workers in one process.
- launchd `.plist` (macOS) and systemd `.service` (Linux) files to run
  yakyoke as a background service that survives reboots. Same daemon
  binary, different init systems.
- Cooperative cancellation: workers check the task status periodically
  inside the agent loop and abort cleanly when cancelled.
- Filesystem read tool for reading larger files in chunks. The current
  `filesystem_read` works but truncates at 100KB.
- Better trace inspection: `yakyoke trace <id> --since N` to tail.

**Pre-staged interfaces:** all of them. The atomic-claim pattern in
`SQLiteQueue.claim_next` already handles multi-worker contention. The
worker is already a pure function with no shared state. Splitting it into
a separate command is just packaging.

**Estimated complexity:** small. Maybe 200 lines of new code total.

---

## v0.3 — Scheduling and observability ⬜

**Goal:** yakyoke runs unattended.

**Planned:**

- Cron-style scheduling: `yakyoke schedule "every day at 7am" "..."`
- `scheduled_for` column starts being used by the queue's claim logic
  (this column already exists from v0.1, so no migration needed).
- File watcher trigger: drop a PDF in a folder, fire a task that
  processes it.
- Webhook trigger: POST to a daemon endpoint creates a task. Combined
  with bearer auth, this is enough for iOS Shortcuts and Slack to
  trigger yakyoke.
- Read-only web UI showing tasks, traces, results. Separate process
  that reads the same SQLite file -- no locking issues thanks to WAL.
- Cost/quality routing config: a default model per task type, with
  optional fallback to a stronger model on failure.

**Pre-staged interfaces:** `scheduled_for` column exists; `priority`
column exists; the daemon's HTTP API is already trigger-friendly.

---

## v0.4 — Sandboxing and richer tools ⬜

**Goal:** more powerful tools without compromising security.

**Planned:**

- Sandboxed Python execution tool. The agent can write code, run it in
  a transient subprocess (or Docker container if available), and read
  the output. Resource limits enforced.
- Shell exec tool with command allowlisting.
- HTTP fetch tool with larger context handling.
- Per-task tool allowlist enforcement at dispatch (currently the
  `tools` field on a task is honored by `ToolRegistry.filtered`, but
  there's no explicit security audit of which tools are dangerous).
- Cost/quality routing implemented: tasks declare a complexity hint,
  the worker picks a model accordingly.

**Pre-staged interfaces:** `tools` allowlist is already in the schema
and honored. Adding new tools is mechanical.

**Open design questions:**
- Docker vs. subprocess vs. nsjail for sandboxing? Probably subprocess
  with seatbelt/sandbox-exec on macOS and bubblewrap on Linux.
- How are sandbox failures distinguished from tool errors?

---

## v0.5 — Task trees / sub-agents ⬜

**Goal:** one task can spawn child tasks. Parent waits for children,
then resumes with their results.

**Planned:**

- A `spawn_task` tool the agent can call. It creates a child task in the
  queue with `parent_id` set, then returns immediately.
- A `wait_for_children` operation that puts the parent task into
  `waiting_for_children` status. When all children are terminal, the
  parent transitions back to `running` and the worker resumes its agent
  loop with the children's results in context.
- Worker logic: when claiming, skip tasks in `waiting_for_children`
  status. When marking a child as done/failed, check if the parent has
  any other non-terminal children; if not, transition the parent.
- Task tree views: `yakyoke tree <id>` shows the parent + all descendants.

**Pre-staged interfaces:** `parent_id` column exists. `WAITING_FOR_CHILDREN`
status exists. The worker is a pure function so children can run on any
worker without coordination.

**Why this matters:** this is the feature that turns yakyoke from a
"single-task agent runner" into a "multi-agent workflow engine." A
"research and synthesize" task spawns N parallel research children,
waits for them, then synthesizes. All using the same primitives.

**Open design questions:**
- Should children inherit the parent's tool allowlist by default, or
  start fresh?
- How are child errors surfaced to the parent? As tool results, or as
  raised exceptions?
- Cancellation cascading: cancelling a parent should cancel all
  in-progress children.

---

## v0.6 — Memory layer ⬜

**Goal:** tasks can recall facts learned in prior tasks.

**Planned:**

- A `Memory` implementation backed by NanoGraph (the local property
  graph DB the project author uses for `lbnz` and `opctx`). Lightweight,
  on-device, schema-as-code, agent-native.
- New tools: `memory_recall(query)`, `memory_store(fact, source)`.
- Optional automatic memory: after each task completes, an "extract
  facts" step writes salient info to memory. Off by default.
- The agent loop calls `memory.recall()` at the start of each task and
  injects relevant facts into the system prompt.

**Pre-staged interfaces:** `Memory` protocol is in `memory.py` from v0.1.
`NoMemory` is the v0.1 stub. Plugging in `NanoGraphMemory` requires zero
agent loop changes.

**Why deferred to v0.6:** without a clear use case for what to remember,
adding a memory layer is premature. By v0.6 we'll have run yakyoke long
enough to know what's worth recalling.

---

## v0.7 — Specialized agents (roles) ⬜

**Goal:** different tasks use different system prompts and tool
allowlists, like assigning a "researcher" or "writer" persona.

**Planned:**

- A `role` field on tasks (column already exists from v0.1) determines
  which system prompt template and tool allowlist to use.
- Roles defined as files in `prompts/roles/<role>.md` with frontmatter
  declaring the tool allowlist.
- A `delegate_to` tool the agent can call to hand off to a different
  role. Combined with v0.5 task trees, this enables editor -> researcher
  -> writer workflows.

**Pre-staged interfaces:** `role` column exists; `prompts/` directory
already exists; `ToolRegistry.filtered` already honors per-task
allowlists.

---

## v1.0 — Personal automation platform ⬜

**Goal:** polished, documented, ready to share publicly as a real tool.

**Planned:**

- iOS Shortcuts integration for voice + text triggers from a phone
- Slack slash command bridge
- GitHub Actions integration (kick off a task from a PR comment)
- Email and calendar integration tools (probably riding on existing MCPs)
- Documented stable HTTP API
- Polished README with screenshots and a 90-second demo video
- pyproject.toml extras: `pip install yakyoke[claude]` to skip LiteLLM bloat

**Optional / situational:**
- Distributed mode: swap `SQLiteQueue` for a Redis or NATS implementation
  to allow workers across multiple machines. Almost certainly never
  needed for a personal tool, but the interface is in place if it ever is.
- PostgreSQL backing for storage. Same answer.

---

## Things explicitly NOT in scope

These come up in conversation but are conscious non-goals:

- 📌 **Multi-tenant / multi-user.** yakyoke is single-user. Authn is a
  single shared bearer token, not per-user accounts. If you want multi-
  tenant, you want a different tool.
- 📌 **Cloud deployment as a hosted service.** Local-first means local-
  first. The HTTP API is designed for localhost or LAN use.
- 📌 **Web UI as the primary interface.** v0.3 ships a read-only
  dashboard, but the primary interfaces are HTTP API and CLI. yakyoke
  is a daemon, not a webapp.
- 📌 **General-purpose framework for building agent libraries.** This is
  a specific opinionated daemon, not a "framework" in the LangChain
  sense. If you find yourself adding plugin abstractions for
  abstraction's sake, you're solving the wrong problem.

## Lessons captured from validation runs

These are observations from real-LLM testing that should inform future
work:

1. **Small models loop on success.** They write a result correctly, then
   keep re-writing it instead of producing a terminal text reply. The
   v0.1 fix was a stronger system prompt + a run-wide duplicate
   interceptor. Real fix is probably cost/quality routing in v0.4 (small
   model decides, larger model terminates).

2. **Small models hallucinate tool names from prompt language.** Words
   like "reply", "respond", "summarize" in instructions can be parsed as
   tool names. The system prompt now uses structural language ("write
   plain text into the message content field with no tool_calls").

3. **LiteLLM's Ollama provider works for tool use** for at least
   gemma4:e4b. gemma4:26b crashed the llama.cpp backend on multi-turn
   tool use, which is upstream and out of yakyoke's hands.

4. **The failure-handling story is genuinely robust.** Two different
   failure modes (LLM exception, max_steps exceeded) both got caught,
   recorded, and reported. The daemon stayed up across both. This is
   what you want.

5. **The architecture survived contact with reality.** Every load-
   bearing decision (separate Storage/Queue interfaces, atomic claim,
   pure-function worker, no concrete imports in the loop) paid off in
   the first real test. Don't undo any of them.
