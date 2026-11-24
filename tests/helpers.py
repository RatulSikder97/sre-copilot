"""Test helpers: scripted LLMs and an offline investigator factory."""

from __future__ import annotations

from typing import Any, Literal

from sre_copilot.agent import Investigator
from sre_copilot.approval.service import ApprovalService
from sre_copilot.config import Settings
from sre_copilot.llm.base import LLMTurn, Message, ToolCall
from sre_copilot.llm.replay import ReplayLLM, ReplayScript
from sre_copilot.tools.base import FixtureStore
from sre_copilot.tools.toolbox import Toolbox


def investigator(
    settings: Settings,
    approvals: ApprovalService,
    llm: Any,
    fixtures: FixtureStore | None = None,
    clock: Any = None,
) -> Investigator:
    toolbox = Toolbox(settings, approvals, fixtures=fixtures or FixtureStore())
    kwargs = {"clock": clock} if clock is not None else {}
    return Investigator(settings, llm, toolbox, approvals, approvals.store, **kwargs)


def replay(turns: list[dict[str, Any]], name: str = "test") -> ReplayLLM:
    return ReplayLLM(ReplayScript.model_validate({"name": name, "turns": turns}))


class LoopingLLM:
    """Never submits a report; keeps calling a read tool. Records the tools offered."""

    mode: Literal["live", "replay"] = "replay"
    model = "looping"

    def __init__(self, tokens_per_turn: int = 10) -> None:
        self.offered: list[list[str]] = []
        self.tokens = tokens_per_turn
        self.n = 0

    def complete(
        self, *, system: str, messages: list[Message], tools: list[dict[str, Any]]
    ) -> LLMTurn:
        names = [t["name"] for t in tools]
        self.offered.append(names)
        self.n += 1
        call = ToolCall(id=f"t{self.n}", name="runbook_search", input={"query": "crashloop"})
        return LLMTurn(
            content=[{"type": "tool_use", "id": call.id, "name": call.name, "input": call.input}],
            stop_reason="tool_use",
            tool_calls=[call],
            input_tokens=self.tokens,
            output_tokens=self.tokens,
        )


MINIMAL_REPORT: dict[str, Any] = {
    "title": "t",
    "severity": "SEV3",
    "summary": "s",
    "impact": "i",
    "timeline": [{"time": "T0", "event": "e", "evidence_ids": ["E1"]}],
    "root_cause": {"hypothesis": "h", "confidence": 0.5, "reasoning": "r", "evidence_ids": ["E1"]},
    "remediation": [{"step": "do nothing", "risk": "low"}],
}
