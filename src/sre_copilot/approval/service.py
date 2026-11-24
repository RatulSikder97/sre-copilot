"""The approval gate: proposals are stored, dry-run, and only executed after a human decision."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx

from sre_copilot.approval.executor import Executor
from sre_copilot.approval.store import AuditLog, Store
from sre_copilot.models import (
    ActionKind,
    ActionSpec,
    ExecutionResult,
    Proposal,
    ProposalStatus,
)
from sre_copilot.safety import check_k8s_name, check_namespace

log = logging.getLogger(__name__)

SIGNATURE_VERSION = "v1"
MAX_SIGNATURE_AGE_SECONDS = 300


class ApprovalError(RuntimeError):
    pass


class ProposalNotFoundError(ApprovalError):
    pass


class ProposalExpiredError(ApprovalError):
    pass


class ProposalStateError(ApprovalError):
    pass


def new_proposal_id() -> str:
    return f"prop-{secrets.token_hex(4)}"


class ApprovalService:
    def __init__(
        self,
        store: Store,
        audit: AuditLog,
        executor_factory: Callable[[], Executor],
        *,
        allowed_namespaces: list[str],
        ttl_seconds: int,
        max_replicas: int,
        notify_webhook_url: str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self.audit = audit
        self._executor_factory = executor_factory
        self._executor: Executor | None = None
        self.allowed_namespaces = allowed_namespaces
        self.ttl = timedelta(seconds=ttl_seconds)
        self.max_replicas = max_replicas
        self.notify_webhook_url = notify_webhook_url
        self.clock = clock

    @property
    def executor(self) -> Executor:
        if self._executor is None:
            self._executor = self._executor_factory()
        return self._executor

    def validate(self, action: ActionSpec) -> None:
        check_namespace(action.namespace, self.allowed_namespaces)
        check_k8s_name(action.deployment, "deployment")
        if action.kind is ActionKind.SCALE:
            if action.replicas is None or not 0 <= action.replicas <= self.max_replicas:
                raise ValueError(f"replicas must be between 0 and {self.max_replicas}")
        elif action.replicas is not None:
            raise ValueError("replicas is only valid for scale")
        if action.kind is not ActionKind.ROLLBACK and action.to_revision is not None:
            raise ValueError("to_revision is only valid for rollback")

    # -- propose ------------------------------------------------------------------------

    def propose(self, investigation_id: str, action: ActionSpec, reason: str) -> Proposal:
        """Validate, dry-run and enqueue an action. Never mutates the cluster."""
        self.validate(action)
        dry_run = self.executor.dry_run(action)
        if action.kind is ActionKind.ROLLBACK and dry_run.resolved_revision is not None:
            # Pin the target so approval later executes exactly what was reviewed.
            action = action.model_copy(update={"to_revision": dry_run.resolved_revision})
        now = self.clock()
        proposal = Proposal(
            id=new_proposal_id(),
            investigation_id=investigation_id,
            action=action,
            reason=reason,
            dry_run=dry_run,
            created_at=now,
            expires_at=now + self.ttl,
            status=ProposalStatus.PENDING if dry_run.ok else ProposalStatus.FAILED,
        )
        self.store.insert_proposal(proposal)
        self.audit.append(
            "proposal.created",
            actor="sre-copilot",
            proposal_id=proposal.id,
            investigation_id=investigation_id,
            action=action.model_dump(mode="json"),
            reason=reason,
            dry_run_ok=dry_run.ok,
            dry_run=dry_run.summary,
        )
        if dry_run.ok:
            self._notify(proposal)
        return proposal

    def _notify(self, proposal: Proposal) -> None:
        if not self.notify_webhook_url:
            return
        text = (
            f":rotating_light: sre-copilot proposes `{proposal.action.describe()}`\n"
            f"Reason: {proposal.reason}\nDry run: {proposal.dry_run.summary}\n"
            + "\n".join(f"  {d}" for d in proposal.dry_run.diff[:10])
            + f"\nApprove: `sre-copilot approve {proposal.id}` "
            f"(expires {proposal.expires_at:%H:%M:%SZ}; default deny)"
        )
        try:
            httpx.post(
                self.notify_webhook_url,
                json={"text": text, "proposal_id": proposal.id},
                timeout=5.0,
            )
        except httpx.HTTPError as exc:
            log.warning("approval notification failed: %s", exc)

    # -- decide -------------------------------------------------------------------------

    def _load_pending(self, proposal_id: str) -> Proposal:
        proposal = self.store.get_proposal(proposal_id)
        if proposal is None:
            raise ProposalNotFoundError(proposal_id)
        if proposal.status is ProposalStatus.PENDING and self.clock() >= proposal.expires_at:
            self._expire(proposal)
            raise ProposalExpiredError(f"{proposal_id} expired at {proposal.expires_at}")
        if proposal.status is not ProposalStatus.PENDING:
            raise ProposalStateError(f"{proposal_id} is {proposal.status.value}, not pending")
        return proposal

    def _expire(self, proposal: Proposal) -> None:
        if self.store.transition(proposal, ProposalStatus.PENDING, ProposalStatus.EXPIRED):
            self.audit.append(
                "proposal.expired",
                actor="sre-copilot",
                proposal_id=proposal.id,
                note="no decision before TTL; default deny",
            )

    def expire_stale(self) -> list[str]:
        expired = []
        for proposal in self.store.list_proposals(ProposalStatus.PENDING):
            if self.clock() >= proposal.expires_at:
                self._expire(proposal)
                expired.append(proposal.id)
        return expired

    def deny(self, proposal_id: str, actor: str, note: str = "") -> Proposal:
        proposal = self._load_pending(proposal_id)
        decided = proposal.model_copy(update={"decided_by": actor, "decided_at": self.clock()})
        if not self.store.transition(decided, ProposalStatus.PENDING, ProposalStatus.DENIED):
            raise ProposalStateError(f"{proposal_id} was decided concurrently")
        self.audit.append("proposal.denied", actor=actor, proposal_id=proposal_id, note=note)
        return decided.model_copy(update={"status": ProposalStatus.DENIED})

    def approve(self, proposal_id: str, actor: str) -> Proposal:
        """Approve and execute: re-dry-run, staleness check, apply, verify, audit every step."""
        proposal = self._load_pending(proposal_id)
        decided = proposal.model_copy(update={"decided_by": actor, "decided_at": self.clock()})
        if not self.store.transition(decided, ProposalStatus.PENDING, ProposalStatus.APPROVED):
            raise ProposalStateError(f"{proposal_id} was decided concurrently")
        decided = decided.model_copy(update={"status": ProposalStatus.APPROVED})
        self.audit.append(
            "proposal.approved",
            actor=actor,
            proposal_id=proposal_id,
            action=decided.action.model_dump(mode="json"),
        )

        recheck = self.executor.dry_run(decided.action)
        self.audit.append(
            "execution.dry_run",
            actor=actor,
            proposal_id=proposal_id,
            ok=recheck.ok,
            summary=recheck.summary,
            diff=recheck.diff,
        )
        if not recheck.ok:
            return self._finish(
                decided,
                ok=False,
                summary=f"pre-execution dry run failed: {recheck.summary}",
                actor=actor,
            )

        result = self.executor.execute(decided.action, decided.dry_run.observed_generation)
        final = decided.model_copy(
            update={
                "execution": result,
                "status": ProposalStatus.EXECUTED if result.ok else ProposalStatus.FAILED,
            }
        )
        self.store.save_proposal(final)
        self.audit.append(
            "execution.result",
            actor=actor,
            proposal_id=proposal_id,
            ok=result.ok,
            summary=result.summary,
            verified=result.verified,
            verification=result.verification,
        )
        return final

    def _finish(self, proposal: Proposal, *, ok: bool, summary: str, actor: str) -> Proposal:
        final = proposal.model_copy(
            update={
                "execution": ExecutionResult(ok=ok, summary=summary),
                "status": ProposalStatus.EXECUTED if ok else ProposalStatus.FAILED,
            }
        )
        self.store.save_proposal(final)
        self.audit.append(
            "execution.result", actor=actor, proposal_id=proposal.id, ok=ok, summary=summary
        )
        return final


# -- Slack-style request signing ------------------------------------------------------------


def sign(secret: str, timestamp: str, body: bytes) -> str:
    base = f"{SIGNATURE_VERSION}:{timestamp}:".encode() + body
    digest = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_VERSION}={digest}"


def verify_signature(
    secret: str, timestamp: str | None, signature: str | None, body: bytes, now: float | None = None
) -> bool:
    if not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - ts) > MAX_SIGNATURE_AGE_SECONDS:
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), signature)
