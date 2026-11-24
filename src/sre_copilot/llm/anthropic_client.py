"""Live model backend: Anthropic Messages API with tool use."""

from __future__ import annotations

import logging
from typing import Any, Literal

import anthropic

from sre_copilot.config import Settings
from sre_copilot.llm.base import LLMError, LLMTurn, Message, ToolCall

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicLLM:
    """Thin wrapper over ``client.beta.messages.create``.

    * Prompt caching: the (frozen) system prompt carries a cache breakpoint, which also covers
      the deterministic tool list rendered before it; top-level automatic caching covers the
      growing message history between agent steps.
    * Thinking: left at the model default (adaptive); depth is steered with ``effort``.
    * Refusals: server-side fallback (``fallbacks="default"``) is enabled unless disabled in
      settings; a refusal that survives the fallback chain surfaces as ``stop_reason="refusal"``.
    * Retries: the SDK retries 408/409/429/5xx and connection errors with backoff.
    """

    mode: Literal["live", "replay"] = "live"

    def __init__(self, settings: Settings, client: anthropic.Anthropic | None = None) -> None:
        self.settings = settings
        self.model = settings.model
        self._client = client or anthropic.Anthropic(
            timeout=settings.llm_timeout_seconds, max_retries=settings.llm_max_retries
        )

    def complete(
        self, *, system: str, messages: list[Message], tools: list[dict[str, Any]]
    ) -> LLMTurn:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.settings.max_tokens_per_turn,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
            "tools": tools,
            "tool_choice": {"type": "auto"},
            "cache_control": {"type": "ephemeral"},
            "output_config": {"effort": self.settings.effort},
        }
        if self.settings.refusal_fallback:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        try:
            response = self._client.beta.messages.create(**kwargs)
        except anthropic.AuthenticationError as exc:
            raise LLMError("Anthropic authentication failed; set ANTHROPIC_API_KEY") from exc
        except anthropic.BadRequestError as exc:
            raise LLMError(f"Anthropic rejected the request: {exc.message}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("Anthropic rate limit persisted after retries") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"Could not reach the Anthropic API: {exc}") from exc

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, input=dict(block.input)))
        usage = response.usage
        log.debug("llm turn: stop=%s request_id=%s", response.stop_reason, response._request_id)
        return LLMTurn(
            content=list(response.content),
            stop_reason=str(response.stop_reason),
            text="\n".join(text_parts),
            tool_calls=calls,
            input_tokens=(usage.input_tokens or 0)
            + (usage.cache_read_input_tokens or 0)
            + (usage.cache_creation_input_tokens or 0),
            output_tokens=usage.output_tokens or 0,
        )
