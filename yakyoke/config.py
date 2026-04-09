"""Configuration loading for yakyoke.

Reads from environment variables (with optional .env file support). Keep this
module thin: anything that needs config takes it as a parameter, rather than
importing globals from here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env from cwd if present. Safe to call even when no file exists.
load_dotenv()


@dataclass(frozen=True)
class Config:
    """Runtime configuration for the daemon and worker."""

    # Where the daemon stores its db, workspaces, prompts, etc.
    data_dir: Path

    # Default model when a task doesn't specify one.
    # Format follows LiteLLM conventions:
    #   "claude-opus-4-6"            (Anthropic)
    #   "ollama/gemma3:27b"          (local Ollama)
    #   "openai/gpt-4o-mini"         (OpenAI)
    default_model: str

    # HTTP server bind.
    host: str
    port: int

    # Cap on agent loop iterations to prevent runaway tool-use loops.
    max_agent_steps: int

    @classmethod
    def from_env(cls) -> "Config":
        data_dir = Path(
            os.environ.get("YAKYOKE_DATA_DIR", str(Path.home() / ".yakyoke"))
        ).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "tasks").mkdir(exist_ok=True)
        return cls(
            data_dir=data_dir,
            default_model=os.environ.get("YAKYOKE_DEFAULT_MODEL", "ollama/gemma3:27b"),
            host=os.environ.get("YAKYOKE_HOST", "127.0.0.1"),
            port=int(os.environ.get("YAKYOKE_PORT", "8765")),
            max_agent_steps=int(os.environ.get("YAKYOKE_MAX_STEPS", "12")),
        )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "yakyoke.db"

    @property
    def tasks_dir(self) -> Path:
        return self.data_dir / "tasks"
