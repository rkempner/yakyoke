"""LLM provider abstraction.

The agent loop never imports anthropic, openai, or ollama directly. It calls
LLM.complete(), which returns a normalized response. The implementation here
uses LiteLLM, which speaks 100+ providers with a unified interface, including
local Ollama models.

If LiteLLM ever annoys you, this is the one file to replace.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import litellm

# LiteLLM is chatty by default. Quiet it down for our use.
litellm.suppress_debug_info = True


@dataclass
class ToolCall:
    """A tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """Normalized response from any provider.

    `text` is the assistant's textual reply (may be empty if the model
    chose to call tools instead). `tool_calls` is a possibly-empty list of
    tool invocations the model wants to make. The agent loop dispatches
    them, appends results, and calls the LLM again.
    """

    text: str
    tool_calls: list[ToolCall]
    raw: dict[str, Any]  # the original LiteLLM response, for debugging

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class LLM:
    """Provider-agnostic chat completion with tool use.

    Models follow LiteLLM naming:
      - "claude-opus-4-6"            (Anthropic, requires ANTHROPIC_API_KEY)
      - "ollama/gemma3:27b"          (local Ollama at OLLAMA_API_BASE)
      - "openai/gpt-4o-mini"         (OpenAI, requires OPENAI_API_KEY)
    """

    def __init__(self, default_model: str):
        self.default_model = default_model

    def complete(
        self,
        messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """Make one LLM call. Returns a normalized response.

        `messages` follows OpenAI's chat-completions format, which LiteLLM
        normalizes across providers. `tool_schemas` is a list of OpenAI
        function-tool definitions; pass None or empty to disable tools.
        """
        kwargs: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": messages,
            "temperature": temperature,
        }
        if tool_schemas:
            kwargs["tools"] = tool_schemas
            kwargs["tool_choice"] = "auto"

        response = litellm.completion(**kwargs)

        # LiteLLM returns an OpenAI-compatible response object regardless
        # of provider. The first choice is what we want.
        choice = response.choices[0]
        message = choice.message

        text = message.content or ""
        tool_calls: list[ToolCall] = []

        raw_tool_calls = getattr(message, "tool_calls", None) or []
        for tc in raw_tool_calls:
            # Tool call args come as a JSON string per OpenAI spec.
            args_raw = tc.function.arguments
            if isinstance(args_raw, str):
                import json

                try:
                    args = json.loads(args_raw) if args_raw else {}
                except json.JSONDecodeError:
                    # Some models occasionally produce invalid JSON.
                    # Surface it as an empty arg dict and let the tool
                    # error out gracefully.
                    args = {}
            else:
                args = dict(args_raw) if args_raw else {}

            tool_calls.append(
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args,
                )
            )

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            raw=response.model_dump() if hasattr(response, "model_dump") else {},
        )
