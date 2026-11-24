"""Deterministic replay backend: a scripted stand-in for the model.

A replay script is a YAML file listing the assistant turns of an investigation. Each turn may
carry text and tool calls; each tool call may declare ``expect`` substrings that the *result* of
that call must contain. Expectations are checked when the next turn is requested, so a replay run
against live tools fails loudly (``ReplayDivergenceError``) if the environment does not show what
the script assumes, instead of producing a report that no longer matches reality.

Tool inputs may reference values produced earlier in the run:

* ``{{proposal:N}}``  -> the N-th (0-based) ``proposal_id`` returned by a propose_* tool.

Replay output is always labelled ``mode=replay``; it is not model output.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from sre_copilot.llm.base import LLMError, LLMTurn, Message, ToolCall


class ReplayDivergenceError(LLMError):
    """Observed tool output does not match what the replay script expects."""


class ReplayExhaustedError(LLMError):
    """The agent asked for more turns than the script contains."""


class ScriptedToolCall(BaseModel):
    name: str
    input: dict[str, Any] = Field(default_factory=dict)
    expect: list[str] = Field(default_factory=list)


class ScriptedTurn(BaseModel):
    text: str = ""
    tool_calls: list[ScriptedToolCall] = Field(default_factory=list)


class ReplayScript(BaseModel):
    name: str
    description: str = ""
    symptom: str = ""
    turns: list[ScriptedTurn]

    @classmethod
    def load(cls, path: Path) -> ReplayScript:
        return cls.model_validate(yaml.safe_load(path.read_text()))


_PROPOSAL_REF = re.compile(r"\{\{proposal:(\d+)\}\}")
_PROPOSAL_ID = re.compile(r'"proposal_id":\s*"(prop-[0-9a-f]+)"')


def _iter_tool_results(messages: list[Message]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "user" or not isinstance(message.get("content"), list):
            continue
        results.extend(
            block
            for block in message["content"]
            if isinstance(block, dict) and block.get("type") == "tool_result"
        )
    return results


def _result_text(block: dict[str, Any]) -> str:
    content = block.get("content", "")
    if isinstance(content, str):
        return content
    return "\n".join(part.get("text", "") for part in content if isinstance(part, dict))


class ReplayLLM:
    mode: Literal["live", "replay"] = "replay"

    def __init__(self, script: ReplayScript) -> None:
        self.script = script
        self.model = f"replay:{script.name}"
        self._cursor = 0

    @classmethod
    def from_path(cls, path: Path) -> ReplayLLM:
        return cls(ReplayScript.load(path))

    def _check_expectations(self, messages: list[Message]) -> None:
        if self._cursor == 0:
            return
        previous = self.script.turns[self._cursor - 1]
        by_id = {b.get("tool_use_id"): b for b in _iter_tool_results(messages)}
        for index, call in enumerate(previous.tool_calls):
            if not call.expect:
                continue
            result = by_id.get(self._tool_use_id(self._cursor - 1, index))
            if result is None:
                raise ReplayDivergenceError(f"no result recorded for scripted call {call.name}")
            text = _result_text(result)
            missing = [needle for needle in call.expect if needle not in text]
            if missing:
                raise ReplayDivergenceError(
                    f"replay divergence at turn {self._cursor - 1} ({call.name}): "
                    f"expected {missing!r} in tool output, got: {text[:400]!r}"
                )

    def _resolve(self, value: Any, proposals: list[str]) -> Any:
        if isinstance(value, str):

            def sub(match: re.Match[str]) -> str:
                idx = int(match.group(1))
                if idx >= len(proposals):
                    raise ReplayDivergenceError(f"script references proposal {idx}; none recorded")
                return proposals[idx]

            return _PROPOSAL_REF.sub(sub, value)
        if isinstance(value, dict):
            return {k: self._resolve(v, proposals) for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve(v, proposals) for v in value]
        return value

    @staticmethod
    def _tool_use_id(turn: int, index: int) -> str:
        return f"toolu_replay_{turn:02d}_{index:02d}"

    def complete(
        self, *, system: str, messages: list[Message], tools: list[dict[str, Any]]
    ) -> LLMTurn:
        self._check_expectations(messages)
        if self._cursor >= len(self.script.turns):
            raise ReplayExhaustedError(
                f"replay script {self.script.name!r} has no turn {self._cursor}"
            )
        turn = self.script.turns[self._cursor]
        tool_names = {t["name"] for t in tools}
        proposals = [
            pid
            for b in _iter_tool_results(messages)
            for pid in _PROPOSAL_ID.findall(_result_text(b))
        ]

        content: list[dict[str, Any]] = []
        calls: list[ToolCall] = []
        if turn.text:
            content.append({"type": "text", "text": turn.text})
        for index, scripted in enumerate(turn.tool_calls):
            if scripted.name not in tool_names:
                raise ReplayDivergenceError(
                    f"script calls {scripted.name!r}, which is not offered this turn"
                )
            call = ToolCall(
                id=self._tool_use_id(self._cursor, index),
                name=scripted.name,
                input=self._resolve(scripted.input, proposals),
            )
            calls.append(call)
            content.append(
                {"type": "tool_use", "id": call.id, "name": call.name, "input": call.input}
            )
        self._cursor += 1

        # Synthetic usage (~4 chars/token) so budget accounting behaves like a live run.
        prompt_chars = len(system) + len(json.dumps(messages, default=str))
        return LLMTurn(
            content=content,
            stop_reason="tool_use" if calls else "end_turn",
            text=turn.text,
            tool_calls=calls,
            input_tokens=prompt_chars // 4,
            output_tokens=len(json.dumps(content)) // 4,
        )
