"""Action tools. Each one only *proposes*: it validates, dry-runs and enqueues for approval."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from sre_copilot.approval.service import ApprovalService
from sre_copilot.models import ActionKind, ActionSpec
from sre_copilot.safety import check_k8s_name
from sre_copilot.tools.base import Tool, ToolResult


class _ProposalArgs(BaseModel):
    namespace: str
    deployment: str
    reason: str = Field(min_length=10, max_length=600, description="Why, citing evidence ids")

    @field_validator("deployment")
    @classmethod
    def _dep(cls, v: str) -> str:
        return check_k8s_name(v, "deployment")


class RestartArgs(_ProposalArgs):
    pass


class RollbackArgs(_ProposalArgs):
    to_revision: int | None = Field(
        default=None, ge=1, description="Target revision; omit for the previous revision"
    )


class ScaleArgs(_ProposalArgs):
    replicas: int = Field(ge=0)


def action_tools(approvals: ApprovalService, investigation_id: str) -> list[Tool]:
    def propose(spec: ActionSpec, reason: str) -> ToolResult:
        proposal = approvals.propose(investigation_id, spec, reason)
        out = {
            "proposal_id": proposal.id,
            "status": proposal.status.value,
            "action": proposal.action.describe(),
            "dry_run": proposal.dry_run.model_dump(mode="json"),
            "expires_at": proposal.expires_at.isoformat(),
            "note": (
                "NOT executed. Queued for human approval; it will be denied automatically if "
                "nobody approves before expires_at."
                if proposal.dry_run.ok
                else "Dry run failed; proposal recorded as failed and cannot be approved."
            ),
        }
        summary = f"proposal {proposal.id}: {proposal.action.describe()} -> {proposal.status.value}"
        return ToolResult(output=out, summary=summary, is_error=not proposal.dry_run.ok)

    def restart(args: RestartArgs) -> ToolResult:
        spec = ActionSpec(
            kind=ActionKind.ROLLOUT_RESTART, namespace=args.namespace, deployment=args.deployment
        )
        return propose(spec, args.reason)

    def rollback(args: RollbackArgs) -> ToolResult:
        spec = ActionSpec(
            kind=ActionKind.ROLLBACK,
            namespace=args.namespace,
            deployment=args.deployment,
            to_revision=args.to_revision,
        )
        return propose(spec, args.reason)

    def scale(args: ScaleArgs) -> ToolResult:
        spec = ActionSpec(
            kind=ActionKind.SCALE,
            namespace=args.namespace,
            deployment=args.deployment,
            replicas=args.replicas,
        )
        return propose(spec, args.reason)

    note = (
        " This does NOT change the cluster: it runs a server-side dry run and queues the action "
        "for explicit human approval."
    )
    return [
        Tool(
            "propose_rollout_restart",
            "Propose `kubectl rollout restart` for a deployment." + note,
            RestartArgs,
            restart,
            kind="action",
        ),
        Tool(
            "propose_rollback",
            "Propose rolling a deployment back to an earlier revision (default: previous)." + note,
            RollbackArgs,
            rollback,
            kind="action",
        ),
        Tool(
            "propose_scale",
            "Propose scaling a deployment to N replicas." + note,
            ScaleArgs,
            scale,
            kind="action",
        ),
    ]
