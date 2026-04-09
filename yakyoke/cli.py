"""yakyoke CLI client.

Talks to the daemon over HTTP. Subcommands:
  yakyoke daemon                  start the daemon (and a worker thread)
  yakyoke run "..."               submit a task and wait for it to complete
  yakyoke submit "..."            submit a task and return immediately
  yakyoke status <id>             show task state
  yakyoke list [--status pending] list recent tasks
  yakyoke trace <id>              print the JSONL trace
  yakyoke result <id>             print the task's result file
  yakyoke cancel <id>             cancel a task
  yakyoke health                  check daemon liveness
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Optional

import httpx
import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

app = typer.Typer(
    name="yakyoke",
    help="A local-first agent daemon. Bring your own LLM. Yokes the yak.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()


def _base_url() -> str:
    host = os.environ.get("YAKYOKE_HOST", "127.0.0.1")
    port = os.environ.get("YAKYOKE_PORT", "8765")
    return f"http://{host}:{port}"


def _client() -> httpx.Client:
    return httpx.Client(base_url=_base_url(), timeout=30.0)


def _die(msg: str, code: int = 1) -> None:
    console.print(f"[red]error:[/red] {msg}")
    raise typer.Exit(code)


# ---------- daemon ----------


@app.command()
def daemon(
    host: str = typer.Option("127.0.0.1", help="Bind host"),
    port: int = typer.Option(8765, help="Bind port"),
    reload: bool = typer.Option(False, help="Auto-reload on code changes (dev)"),
) -> None:
    """Start the yakyoke daemon (HTTP server + background worker)."""
    import uvicorn

    # Stash bind values in env so create_app() and the worker pick them up.
    os.environ["YAKYOKE_HOST"] = host
    os.environ["YAKYOKE_PORT"] = str(port)

    console.print(f"[bold green]yakyoke[/bold green] daemon starting on {host}:{port}")
    uvicorn.run(
        "yakyoke.daemon:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


# ---------- task lifecycle ----------


@app.command()
def submit(
    prompt: str = typer.Argument(..., help="The task prompt"),
    model: Optional[str] = typer.Option(None, help="LLM model (LiteLLM format)"),
    tools: Optional[str] = typer.Option(
        None, help="Comma-separated tool allowlist (default: all)"
    ),
    max_steps: Optional[int] = typer.Option(None, help="Max agent loop iterations"),
) -> None:
    """Submit a task and return immediately with its ID."""
    payload = {
        "prompt": prompt,
        "model": model,
        "tools": [t.strip() for t in tools.split(",")] if tools else [],
    }
    if max_steps is not None:
        payload["max_steps"] = max_steps

    try:
        with _client() as c:
            r = c.post("/tasks", json=payload)
            r.raise_for_status()
    except httpx.HTTPError as e:
        _die(f"daemon request failed: {e}")

    task = r.json()
    console.print(f"[green]submitted[/green] {task['id']}")


@app.command()
def run(
    prompt: str = typer.Argument(..., help="The task prompt"),
    model: Optional[str] = typer.Option(None, help="LLM model (LiteLLM format)"),
    tools: Optional[str] = typer.Option(None, help="Comma-separated tool allowlist"),
    max_steps: Optional[int] = typer.Option(None, help="Max agent loop iterations"),
    poll: float = typer.Option(1.0, help="Poll interval in seconds"),
    timeout: float = typer.Option(600.0, help="Total wait timeout in seconds"),
    show_trace: bool = typer.Option(False, "--trace", help="Print the trace at end"),
) -> None:
    """Submit a task, wait for it to complete, and print the result."""
    payload = {
        "prompt": prompt,
        "model": model,
        "tools": [t.strip() for t in tools.split(",")] if tools else [],
    }
    if max_steps is not None:
        payload["max_steps"] = max_steps

    try:
        with _client() as c:
            r = c.post("/tasks", json=payload)
            r.raise_for_status()
            task = r.json()
            task_id = task["id"]
            console.print(f"[green]submitted[/green] {task_id}")

            deadline = time.monotonic() + timeout
            with console.status("[bold cyan]running...[/bold cyan]") as status:
                while True:
                    if time.monotonic() > deadline:
                        _die(f"timed out after {timeout}s waiting for {task_id}")
                    r = c.get(f"/tasks/{task_id}")
                    r.raise_for_status()
                    task = r.json()
                    status.update(f"[bold cyan]{task['status']}[/bold cyan]")
                    if task["status"] in ("done", "failed", "cancelled"):
                        break
                    time.sleep(poll)
    except httpx.HTTPError as e:
        _die(f"daemon request failed: {e}")

    console.print()
    if task["status"] == "done":
        console.print(f"[green]done[/green]: {task_id}")
        if task.get("result_path"):
            try:
                with _client() as c:
                    r = c.get(f"/tasks/{task_id}/result")
                    r.raise_for_status()
                    console.print(Markdown(r.text))
            except httpx.HTTPError:
                pass
    elif task["status"] == "failed":
        console.print(f"[red]failed[/red]: {task_id}")
        if task.get("error"):
            console.print(f"[red]{task['error']}[/red]")
    else:
        console.print(f"[yellow]{task['status']}[/yellow]: {task_id}")

    if show_trace:
        console.rule("trace")
        try:
            with _client() as c:
                r = c.get(f"/tasks/{task_id}/trace")
                r.raise_for_status()
                console.print(r.text)
        except httpx.HTTPError:
            pass


@app.command()
def status(task_id: str = typer.Argument(...)) -> None:
    """Show the state of a task."""
    try:
        with _client() as c:
            r = c.get(f"/tasks/{task_id}")
            r.raise_for_status()
    except httpx.HTTPError as e:
        _die(f"daemon request failed: {e}")
    console.print_json(data=r.json())


@app.command(name="list")
def list_tasks(
    status: Optional[str] = typer.Option(None, help="Filter by status"),
    limit: int = typer.Option(20, help="Max rows to show"),
) -> None:
    """List recent tasks."""
    params = {"limit": limit}
    if status:
        params["status"] = status
    try:
        with _client() as c:
            r = c.get("/tasks", params=params)
            r.raise_for_status()
    except httpx.HTTPError as e:
        _die(f"daemon request failed: {e}")
    tasks = r.json()
    if not tasks:
        console.print("(no tasks)")
        return

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("id", style="dim")
    table.add_column("status")
    table.add_column("model")
    table.add_column("created", style="dim")
    table.add_column("prompt")
    for t in tasks:
        prompt_preview = (t["prompt"][:60] + "...") if len(t["prompt"]) > 60 else t["prompt"]
        status_color = {
            "pending": "yellow",
            "running": "cyan",
            "done": "green",
            "failed": "red",
            "cancelled": "dim",
        }.get(t["status"], "white")
        table.add_row(
            t["id"],
            f"[{status_color}]{t['status']}[/{status_color}]",
            t["model"],
            t["created_at"][:19],
            prompt_preview,
        )
    console.print(table)


@app.command()
def trace(task_id: str = typer.Argument(...)) -> None:
    """Print the JSONL trace for a task."""
    try:
        with _client() as c:
            r = c.get(f"/tasks/{task_id}/trace")
            r.raise_for_status()
    except httpx.HTTPError as e:
        _die(f"daemon request failed: {e}")
    if not r.text:
        console.print("(no trace yet)")
        return
    # Pretty-print each line as a JSON object.
    for line in r.text.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            console.print_json(data=obj)
        except json.JSONDecodeError:
            console.print(line)


@app.command()
def result(task_id: str = typer.Argument(...)) -> None:
    """Print the result file for a completed task."""
    try:
        with _client() as c:
            r = c.get(f"/tasks/{task_id}/result")
            r.raise_for_status()
    except httpx.HTTPError as e:
        _die(f"daemon request failed: {e}")
    console.print(Markdown(r.text))


@app.command()
def cancel(task_id: str = typer.Argument(...)) -> None:
    """Cancel a pending or running task."""
    try:
        with _client() as c:
            r = c.delete(f"/tasks/{task_id}")
            r.raise_for_status()
    except httpx.HTTPError as e:
        _die(f"daemon request failed: {e}")
    console.print(r.json())


@app.command()
def health() -> None:
    """Check daemon liveness."""
    try:
        with _client() as c:
            r = c.get("/health")
            r.raise_for_status()
    except httpx.HTTPError as e:
        _die(f"daemon not reachable at {_base_url()}: {e}")
    console.print_json(data=r.json())


def main() -> None:
    app()


if __name__ == "__main__":
    main()
