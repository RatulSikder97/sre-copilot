"""The investigation loop: bounded tool use over the Messages API with structured state."""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from sre_copilot.approval.service import ApprovalService
from sre_copilot.approval.store import Store
from sre_copilot.config import Settings
from sre_copilot.llm.base import LLMClient, LLMError, Message, ToolCall
from sre_copilot.models import (
    Alert,
    BudgetUsage,
    Evidence,
    Hypothesis,
    HypothesisStatus,
    IncidentReport,
    InvestigationState,
    ReportDraft,
    ReportStatus,
)
from sre_copilot.prompts import STATE_TOOLS, SYSTEM_PROMPT, submit_report_tool
from sre_copilot.safety import fence_untrusted, redact
from sre_copilot.tools.base import ToolRegistry
from sre_copilot.tools.toolbox import Toolbox

log = logging.getLogger(__name__)

MAX_NUDGES = 2


def new_investigation_id() -> str:
    return f"inv-{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


@dataclass
class Budget:
    max_steps: int
    max_tool_calls: int
    max_total_tokens: int
    max_wall_seconds: float
    clock: Callable[[], float] = time.monotonic
    usage: BudgetUsage = field(default_factory=BudgetUsage)
    _started: float = 0.0

    @classmethod
    def from_settings(cls, s: Settings, clock: Callable[[], float] = time.monotonic) -> Budget:
        return cls(s.max_steps, s.max_tool_calls, s.max_total_tokens, s.max_wall_seconds, clock)

    def start(self) -> None:
        self._started = self.clock()

    def tick(self) -> None:
        self.usage.elapsed_seconds = round(self.clock() - self._started, 2)

    def exhausted(self) -> str | None:
        self.tick()
        u = self.usage
        if u.steps >= self.max_steps:
            return f"step budget ({self.max_steps}) exhausted"
        if u.tool_calls >= self.max_tool_calls:
            return f"tool-call budget ({self.max_tool_calls}) exhausted"
        if u.total_tokens >= self.max_total_tokens:
            return f"token budget ({self.max_total_tokens}) exhausted"
        if u.elapsed_seconds >= self.max_wall_seconds:
            return f"time budget ({self.max_wall_seconds:.0f}s) exhausted"
        return None

    def status_line(self) -> str:
        u = self.usage
        return (
            f"[budget] steps {u.steps}/{self.max_steps}, tool calls {u.tool_calls}/"
            f"{self.max_tool_calls}, tokens {u.total_tokens}/{self.max_total_tokens}, "
            f"elapsed {u.elapsed_seconds:.0f}s/{self.max_wall_seconds:.0f}s"
        )


class Investigator:
    def __init__(
        self,
        settings: Settings,
        llm: LLMClient,
        toolbox: Toolbox,
        approvals: ApprovalService,
        store: Store,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.toolbox = toolbox
        self.approvals = approvals
        self.store = store
        self.clock = clock

    # -- public ---------------------------------------------------------------------------

    def run(
        self, alert: Alert, symptom: str | None = None, investigation_id: str | None = None
    ) -> IncidentReport:
        inv_id = investigation_id or new_investigation_id()
        self.store.start_investigation(inv_id)
        run = _Run(self, inv_id, alert, symptom)
        report = run.execute()
        self.store.finish_investigation(report)
        return report


class _Run:
    """State for one investigation."""

    def __init__(self, owner: Investigator, inv_id: str, alert: Alert, symptom: str | None) -> None:
        self.o = owner
        self.settings = owner.settings
        self.inv_id = inv_id
        self.alert = alert
        self.symptom = symptom
        self.state = InvestigationState()
        self.budget = Budget.from_settings(owner.settings, owner.clock)
        self.registry: ToolRegistry = owner.toolbox.registry(inv_id)
        self.messages: list[Message] = []
        self.draft: ReportDraft | None = None
        self.system = SYSTEM_PROMPT.format(namespaces=", ".join(self.settings.allowed_namespaces))

    # -- loop -----------------------------------------------------------------------------

    def tools(self, final_only: bool = False) -> list[dict[str, Any]]:
        if final_only:
            return [submit_report_tool()]
        return [*self.registry.schemas(), *STATE_TOOLS, submit_report_tool()]

    def opening_message(self) -> Message:
        parts = [
            f"Investigation {self.inv_id} started at {datetime.now(UTC).isoformat()}.",
            "<alert>\n" + redact(self.alert.describe()) + "\n</alert>",
        ]
        if self.symptom:
            parts.append("<alert>\nReported symptom: " + redact(self.symptom) + "\n</alert>")
        parts.append(
            f"Allowed namespaces: {', '.join(self.settings.allowed_namespaces)}. "
            "Investigate, propose remediation if warranted, then call submit_report."
        )
        return {"role": "user", "content": "\n\n".join(parts)}

    def execute(self) -> IncidentReport:
        self.budget.start()
        self.messages.append(self.opening_message())
        status = ReportStatus.COMPLETE
        error: str | None = None
        nudges = 0
        final_only = False
        try:
            while self.draft is None:
                reason = self.budget.exhausted()
                if reason and final_only:
                    status = ReportStatus.BUDGET_EXHAUSTED
                    error = reason
                    break
                if reason:
                    # One last turn with only submit_report available.
                    final_only = True
                    self._append_user_text(
                        f"{reason}. Stop investigating and call submit_report now with the "
                        "evidence gathered so far; state remaining uncertainty explicitly."
                    )
                turn = self.o.llm.complete(
                    system=self.system, messages=self.messages, tools=self.tools(final_only)
                )
                self.budget.usage.steps += 1
                self.budget.usage.input_tokens += turn.input_tokens
                self.budget.usage.output_tokens += turn.output_tokens
                self.messages.append({"role": "assistant", "content": turn.content})

                if turn.stop_reason == "refusal":
                    status, error = ReportStatus.ERROR, "model declined the request (refusal)"
                    break
                if not turn.tool_calls:
                    if final_only or nudges >= MAX_NUDGES:
                        status = ReportStatus.BUDGET_EXHAUSTED if final_only else ReportStatus.ERROR
                        error = "model ended without submitting a report"
                        break
                    nudges += 1
                    self._append_user_text("Continue the investigation; finish with submit_report.")
                    continue
                self._handle_tool_calls(turn.tool_calls)
                if final_only and self.draft is None:
                    status = ReportStatus.BUDGET_EXHAUSTED
                    error = self.budget.exhausted() or "budget exhausted before a valid report"
                    break
        except LLMError as exc:
            status, error = ReportStatus.ERROR, str(exc)
            log.error("investigation %s failed: %s", self.inv_id, exc)

        if self.draft is not None and status is ReportStatus.COMPLETE and final_only:
            status = ReportStatus.BUDGET_EXHAUSTED
        self.budget.tick()
        return self._report(status, error)

    def _append_user_text(self, text: str) -> None:
        self.messages.append({"role": "user", "content": [{"type": "text", "text": text}]})

    # -- tool dispatch ----------------------------------------------------------------------

    def _handle_tool_calls(self, calls: list[ToolCall]) -> None:
        results: list[dict[str, Any]] = []
        for call in calls:
            if self.budget.usage.tool_calls >= self.budget.max_tool_calls and (
                call.name != "submit_report"
            ):
                results.append(self._result(call, "tool-call budget exhausted", is_error=True))
                continue
            self.budget.usage.tool_calls += 1
            if call.name == "submit_report":
                results.append(self._submit(call))
            elif call.name == "record_hypothesis":
                results.append(self._record_hypothesis(call))
            elif call.name == "update_hypothesis":
                results.append(self._update_hypothesis(call))
            else:
                results.append(self._invoke(call))
        self.budget.tick()
        content: list[dict[str, Any]] = [*results]
        if self.draft is None:
            content.append({"type": "text", "text": self.budget.status_line()})
        self.messages.append({"role": "user", "content": content})

    @staticmethod
    def _result(call: ToolCall, text: str, *, is_error: bool = False) -> dict[str, Any]:
        block: dict[str, Any] = {"type": "tool_result", "tool_use_id": call.id, "content": text}
        if is_error:
            block["is_error"] = True
        return block

    def _invoke(self, call: ToolCall) -> dict[str, Any]:
        if call.name not in self.registry:
            return self._result(call, f"unknown tool {call.name!r}", is_error=True)
        tool = self.registry.get(call.name)
        result = self.registry.invoke(call.name, call.input)
        if tool.kind == "action":
            pid = result.output.get("proposal_id") if isinstance(result.output, dict) else None
            if pid:
                self.state.proposal_ids.append(pid)
            return self._result(call, result.render(), is_error=result.is_error)

        evidence = Evidence(
            id=self.state.next_evidence_id(),
            tool=call.name,
            args=call.input,
            summary=redact(result.summary),
            link=result.link,
            is_error=result.is_error,
        )
        self.state.evidence.append(evidence)
        if call.name == "runbook_search" and isinstance(result.output, dict):
            for hit in result.output.get("results", []):
                if hit["path"] not in self.state.runbooks_consulted:
                    self.state.runbooks_consulted.append(hit["path"])
        body = fence_untrusted(
            call.name,
            evidence.id,
            redact(result.render()),
            max_chars=self.settings.max_tool_output_chars,
        )
        return self._result(call, body, is_error=result.is_error)

    def _record_hypothesis(self, call: ToolCall) -> dict[str, Any]:
        try:
            hyp = Hypothesis(
                id=self.state.next_hypothesis_id(),
                statement=str(call.input["statement"]),
                confidence=float(call.input.get("confidence", 0.3)),
            )
        except (KeyError, ValueError, ValidationError) as exc:
            return self._result(call, f"invalid hypothesis: {exc}", is_error=True)
        self.state.hypotheses.append(hyp)
        return self._result(call, json.dumps({"id": hyp.id, "status": hyp.status.value}))

    def _update_hypothesis(self, call: ToolCall) -> dict[str, Any]:
        hyp = self.state.hypothesis(str(call.input.get("id", "")))
        if hyp is None:
            return self._result(call, "unknown hypothesis id", is_error=True)
        evidence_ids = [str(e) for e in call.input.get("evidence_ids", [])]
        unknown = self._unknown_evidence(evidence_ids)
        if unknown:
            return self._result(call, f"unknown evidence ids: {unknown}", is_error=True)
        try:
            hyp.status = HypothesisStatus(call.input.get("status", "open"))
            hyp.confidence = max(0.0, min(1.0, float(call.input.get("confidence", hyp.confidence))))
        except ValueError as exc:
            return self._result(call, f"invalid update: {exc}", is_error=True)
        hyp.evidence_ids = sorted(set(hyp.evidence_ids) | set(evidence_ids), key=_id_order)
        if call.input.get("notes"):
            hyp.notes = str(call.input["notes"])
        return self._result(call, json.dumps(self.state.summary()))

    def _unknown_evidence(self, ids: list[str]) -> list[str]:
        known = {e.id for e in self.state.evidence}
        return [i for i in ids if i not in known]

    def _submit(self, call: ToolCall) -> dict[str, Any]:
        try:
            draft = ReportDraft.model_validate(call.input)
        except ValidationError as exc:
            return self._result(
                call, f"report rejected: {exc.errors(include_url=False)}", is_error=True
            )
        cited = list(draft.root_cause.evidence_ids)
        for entry in draft.timeline:
            cited.extend(entry.evidence_ids)
        unknown = self._unknown_evidence(cited)
        bad_proposals = [
            s.proposal_id
            for s in draft.remediation
            if s.proposal_id and s.proposal_id not in self.state.proposal_ids
        ]
        problems = []
        if unknown:
            problems.append(f"unknown evidence ids {sorted(set(unknown))}")
        if bad_proposals:
            problems.append(f"unknown proposal ids {bad_proposals}")
        if problems:
            return self._result(
                call,
                "report rejected: " + "; ".join(problems) + ". Fix and resubmit.",
                is_error=True,
            )
        self.draft = draft
        return self._result(call, "report accepted")

    # -- report -----------------------------------------------------------------------------

    def _report(self, status: ReportStatus, error: str | None) -> IncidentReport:
        proposals = self.o.store.list_proposals(investigation_id=self.inv_id)
        return IncidentReport(
            investigation_id=self.inv_id,
            mode=self.o.llm.mode,
            tools_mode=self.settings.tools,
            model=self.o.llm.model,
            status=status,
            alert=self.alert,
            draft=self.draft,
            hypotheses=self.state.hypotheses,
            evidence=self.state.evidence,
            proposals=proposals,
            runbooks=self.state.runbooks_consulted,
            usage=self.budget.usage,
            error=error,
        )


def _id_order(evidence_id: str) -> tuple[int, str]:
    digits = "".join(ch for ch in evidence_id if ch.isdigit())
    return (int(digits) if digits else 0, evidence_id)
