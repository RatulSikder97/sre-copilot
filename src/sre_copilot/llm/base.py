"""Provider-neutral view of one model turn, shaped after the Anthropic Messages API."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Message = dict[str, Any]


class LLMError(RuntimeError):
    """The model call failed in a way the agent loop cannot recover from."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMTurn:
    # Blocks to append verbatim as the assistant message (SDK objects or plain dicts).
    content: list[Any]
    stop_reason: str
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


class LLMClient(Protocol):
    mode: Literal["live", "replay"]
    model: str

    def complete(
        self, *, system: str, messages: list[Message], tools: list[dict[str, Any]]
    ) -> LLMTurn: ...
