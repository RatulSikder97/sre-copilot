"""Domain models: alerts, investigation state, action proposals and the incident report."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------------------------
# Ingest
# --------------------------------------------------------------------------------------------


class Alert(BaseModel):
    """A single alert, normalised from an Alertmanager webhook or a manual symptom."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    status: Literal["firing", "resolved"] = "firing"
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    starts_at: datetime | None = Field(default=None, alias="startsAt")
    ends_at: datetime | None = Field(default=None, alias="endsAt")
    generator_url: str | None = Field(default=None, alias="generatorURL")
    fingerprint: str | None = None

    @property
    def name(self) -> str:
        return self.labels.get("alertname", "ManualInvestigation")

    def describe(self) -> str:
        lines = [f"Alert: {self.name} ({self.status})"]
        if self.starts_at:
            lines.append(f"Started: {self.starts_at.isoformat()}")
        lines.extend(f"label {k}={v}" for k, v in sorted(self.labels.items()))
        lines.extend(f"annotation {k}: {v}" for k, v in sorted(self.annotations.items()))
        return "\n".join(lines)


class AlertmanagerWebhook(BaseModel):
    """Alertmanager webhook payload (version 4)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    version: str = "4"
    status: str = "firing"
    receiver: str = ""
    group_key: str = Field(default="", alias="groupKey")
    common_labels: dict[str, str] = Field(default_factory=dict, alias="commonLabels")
    alerts: list[Alert] = Field(default_factory=list)


# --------------------------------------------------------------------------------------------
# Investigation state
# --------------------------------------------------------------------------------------------


class HypothesisStatus(StrEnum):
    OPEN = "open"
    SUPPORTED = "supported"
    RULED_OUT = "ruled_out"


class Hypothesis(BaseModel):
    id: str
    statement: str
    status: HypothesisStatus = HypothesisStatus.OPEN
    confidence: float = Field(default=0.3, ge=0.0, le=1.0)
    evidence_ids: list[str] = Field(default_factory=list)
    notes: str = ""


class Evidence(BaseModel):
    id: str
    tool: str
    args: dict[str, Any]
    summary: str
    link: str | None = None
    observed_at: datetime = Field(default_factory=utcnow)
    is_error: bool = False


class InvestigationState(BaseModel):
    """Structured scratchpad the agent maintains through tool calls."""

    hypotheses: list[Hypothesis] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    proposal_ids: list[str] = Field(default_factory=list)
    runbooks_consulted: list[str] = Field(default_factory=list)

    def next_evidence_id(self) -> str:
        return f"E{len(self.evidence) + 1}"

    def next_hypothesis_id(self) -> str:
        return f"H{len(self.hypotheses) + 1}"

    def hypothesis(self, hypothesis_id: str) -> Hypothesis | None:
        return next((h for h in self.hypotheses if h.id == hypothesis_id), None)

    def ruled_out(self) -> list[Hypothesis]:
        return [h for h in self.hypotheses if h.status is HypothesisStatus.RULED_OUT]

    def summary(self) -> dict[str, Any]:
        return {
            "hypotheses": [h.model_dump(mode="json") for h in self.hypotheses],
            "evidence_ids": [e.id for e in self.evidence],
            "proposal_ids": list(self.proposal_ids),
        }


# --------------------------------------------------------------------------------------------
# Actions / approval
# --------------------------------------------------------------------------------------------


class ActionKind(StrEnum):
    ROLLOUT_RESTART = "rollout_restart"
    ROLLBACK = "rollback"
    SCALE = "scale"


class ActionSpec(BaseModel):
    kind: ActionKind
    namespace: str
    deployment: str
    to_revision: int | None = None
    replicas: int | None = None

    def describe(self) -> str:
        target = f"deployment/{self.deployment} -n {self.namespace}"
        match self.kind:
            case ActionKind.ROLLOUT_RESTART:
                return f"rollout restart {target}"
            case ActionKind.ROLLBACK:
                rev = self.to_revision if self.to_revision is not None else "previous"
                return f"rollback {target} to revision {rev}"
            case ActionKind.SCALE:
                return f"scale {target} to {self.replicas} replicas"


class DryRunResult(BaseModel):
    ok: bool
    summary: str
    diff: list[str] = Field(default_factory=list)
    resolved_revision: int | None = None
    observed_generation: int | None = None


class ExecutionResult(BaseModel):
    ok: bool
    summary: str
    verified: bool = False
    verification: str = ""


class ProposalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    EXECUTED = "executed"
    FAILED = "failed"
    DENIED = "denied"
    EXPIRED = "expired"


class Proposal(BaseModel):
    id: str
    investigation_id: str
    action: ActionSpec
    reason: str
    status: ProposalStatus = ProposalStatus.PENDING
    dry_run: DryRunResult
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    execution: ExecutionResult | None = None


# --------------------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------------------


class TimelineEntry(BaseModel):
    time: str = Field(description="ISO-8601 timestamp or relative marker, e.g. '2024-05-01T10:02Z'")
    event: str
    evidence_ids: list[str] = Field(default_factory=list)


class RootCause(BaseModel):
    hypothesis: str
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    evidence_ids: list[str] = Field(default_factory=list)


class RemediationStep(BaseModel):
    step: str
    proposal_id: str | None = Field(
        default=None, description="ID returned by a propose_* tool, if this step is an action"
    )
    risk: Literal["low", "medium", "high"] = "low"


class ReportDraft(BaseModel):
    """What the model submits via the ``submit_report`` tool."""

    model_config = ConfigDict(extra="forbid")

    title: str
    severity: Literal["SEV1", "SEV2", "SEV3", "SEV4"]
    summary: str
    impact: str
    timeline: list[TimelineEntry]
    root_cause: RootCause
    remediation: list[RemediationStep]
    runbook_refs: list[str] = Field(default_factory=list)
    follow_ups: list[str] = Field(default_factory=list)


class BudgetUsage(BaseModel):
    steps: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_seconds: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class ReportStatus(StrEnum):
    COMPLETE = "complete"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ERROR = "error"


class IncidentReport(BaseModel):
    investigation_id: str
    mode: Literal["live", "replay"]
    tools_mode: Literal["live", "fixtures"] = "live"
    model: str
    status: ReportStatus
    alert: Alert
    created_at: datetime = Field(default_factory=utcnow)
    draft: ReportDraft | None = None
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    proposals: list[Proposal] = Field(default_factory=list)
    runbooks: list[str] = Field(default_factory=list)
    usage: BudgetUsage = Field(default_factory=BudgetUsage)
    error: str | None = None
