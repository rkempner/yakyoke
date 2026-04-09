"""Filesystem tools.

All operations are scoped to the per-task workspace. The agent cannot read,
write, or list anything outside of it. This is the security boundary that
makes parallel workers safe and prevents an agent from clobbering things
elsewhere on the user's machine.

The workspace path is passed in as the first argument to every tool, and
each tool resolves user-provided paths relative to it (with `..` traversal
blocked).
"""

from __future__ import annotations

from pathlib import Path

from yakyoke.tools.registry import ToolSpec

MAX_READ_BYTES = 100_000  # cap on a single file read returned to the model


def _resolve_within_workspace(workspace: Path, user_path: str) -> Path | None:
    """Resolve a user-supplied path inside the workspace.

    Returns None if the resolved path escapes the workspace (..-traversal,
    absolute path, symlink chase). The model never gets to touch anything
    outside its scratch directory.
    """
    workspace = workspace.resolve()
    candidate = (workspace / user_path).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError:
        return None
    return candidate


def filesystem_write(workspace: Path, path: str, content: str) -> str:
    """Write text content to a file inside the task's workspace."""
    target = _resolve_within_workspace(workspace, path)
    if target is None:
        return f"refused: {path} resolves outside the workspace"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    rel = target.relative_to(workspace.resolve())
    return f"wrote {len(content)} chars to {rel}"


def filesystem_read(workspace: Path, path: str) -> str:
    """Read a text file from the task's workspace."""
    target = _resolve_within_workspace(workspace, path)
    if target is None:
        return f"refused: {path} resolves outside the workspace"
    if not target.exists():
        return f"not found: {path}"
    if not target.is_file():
        return f"not a file: {path}"
    data = target.read_bytes()
    if len(data) > MAX_READ_BYTES:
        truncated = data[:MAX_READ_BYTES].decode("utf-8", errors="replace")
        omitted = len(data) - MAX_READ_BYTES
        return f"{truncated}\n\n[...truncated, {omitted} more bytes...]"
    return data.decode("utf-8", errors="replace")


def filesystem_list(workspace: Path, path: str = ".") -> str:
    """List entries in a directory inside the workspace."""
    target = _resolve_within_workspace(workspace, path)
    if target is None:
        return f"refused: {path} resolves outside the workspace"
    if not target.exists():
        return f"not found: {path}"
    if not target.is_dir():
        return f"not a directory: {path}"
    entries = []
    for entry in sorted(target.iterdir()):
        kind = "dir " if entry.is_dir() else "file"
        size = entry.stat().st_size if entry.is_file() else 0
        entries.append(f"{kind}  {size:>9}  {entry.name}")
    if not entries:
        return f"(empty: {path})"
    return "\n".join(entries)


def filesystem_write_spec() -> ToolSpec:
    return ToolSpec(
        name="filesystem_write",
        func=filesystem_write,
        schema={
            "type": "function",
            "function": {
                "name": "filesystem_write",
                "description": (
                    "Write text content to a file inside the task's workspace. "
                    "Use this to save results, drafts, notes, or any output the "
                    "user should be able to read. Path is relative to the "
                    "workspace root; .. is not allowed."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path within the workspace (e.g. 'result.md').",
                        },
                        "content": {
                            "type": "string",
                            "description": "The text content to write.",
                        },
                    },
                    "required": ["path", "content"],
                },
            },
        },
    )


def filesystem_read_spec() -> ToolSpec:
    return ToolSpec(
        name="filesystem_read",
        func=filesystem_read,
        schema={
            "type": "function",
            "function": {
                "name": "filesystem_read",
                "description": (
                    "Read a text file from the task's workspace. Returns the "
                    "file contents (truncated if very large)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path within the workspace.",
                        },
                    },
                    "required": ["path"],
                },
            },
        },
    )


def filesystem_list_spec() -> ToolSpec:
    return ToolSpec(
        name="filesystem_list",
        func=filesystem_list,
        schema={
            "type": "function",
            "function": {
                "name": "filesystem_list",
                "description": (
                    "List entries in a directory inside the workspace. Use to "
                    "discover what files exist before reading them."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path within the workspace (default '.').",
                        },
                    },
                    "required": [],
                },
            },
        },
    )
