"""The approval gate: nothing is written without an explicit, timely, human decision."""

from __future__ import annotations

import json

import pytest

from sre_copilot.approval.service import (
    ApprovalService,
    ProposalExpiredError,
    ProposalStateError,
    sign,
    verify_signature,
)
from sre_copilot.models import ActionKind, ActionSpec, ProposalStatus
from sre_copilot.safety import NamespaceNotAllowedError
from tests.conftest import Clock, FakeExecutor

ROLLBACK = ActionSpec(kind=ActionKind.ROLLBACK, namespace="demo", deployment="checkout")


def test_propose_dry_runs_but_never_executes(
    approvals: ApprovalService, executor: FakeExecutor
) -> None:
    proposal = approvals.propose("inv-1", ROLLBACK, "bad config in revision 2")
    assert proposal.status is ProposalStatus.PENDING
    assert executor.dry_runs == [ROLLBACK]
    assert executor.executions == []
    # Rollback target is pinned to what the dry run resolved.
    assert proposal.action.to_revision == 1


def test_approve_executes_exactly_once_with_generation_guard(
    approvals: ApprovalService, executor: FakeExecutor
) -> None:
    proposal = approvals.propose("inv-1", ROLLBACK, "bad config in revision 2")
    result = approvals.approve(proposal.id, "alice")
    assert result.status is ProposalStatus.EXECUTED
    assert result.decided_by == "alice"
    assert len(executor.executions) == 1
    action, generation = executor.executions[0]
    assert action.to_revision == 1
    assert generation == executor.generation
    # Re-dry-run before executing: propose + pre-execution check.
    assert len(executor.dry_runs) == 2
    with pytest.raises(ProposalStateError):
        approvals.approve(proposal.id, "bob")
    assert len(executor.executions) == 1


def test_deny_never_executes(approvals: ApprovalService, executor: FakeExecutor) -> None:
    proposal = approvals.propose("inv-1", ROLLBACK, "bad config in revision 2")
    denied = approvals.deny(proposal.id, "alice", "prefer a forward fix")
    assert denied.status is ProposalStatus.DENIED
    with pytest.raises(ProposalStateError):
        approvals.approve(proposal.id, "alice")
    assert executor.executions == []


def test_timeout_is_default_deny(
    approvals: ApprovalService, executor: FakeExecutor, clock: Clock
) -> None:
    proposal = approvals.propose("inv-1", ROLLBACK, "bad config in revision 2")
    clock.advance(601)
    with pytest.raises(ProposalExpiredError):
        approvals.approve(proposal.id, "alice")
    assert executor.executions == []
    stored = approvals.store.get_proposal(proposal.id)
    assert stored is not None
    assert stored.status is ProposalStatus.EXPIRED


def test_sweeper_expires_pending(approvals: ApprovalService, clock: Clock) -> None:
    first = approvals.propose("inv-1", ROLLBACK, "bad config in revision 2")
    clock.advance(300)
    second = approvals.propose("inv-1", ROLLBACK, "bad config in revision 2")
    clock.advance(301)
    assert approvals.expire_stale() == [first.id]
    stored = approvals.store.get_proposal(second.id)
    assert stored is not None
    assert stored.status is ProposalStatus.PENDING


def test_failed_dry_run_cannot_be_approved(
    approvals: ApprovalService, executor: FakeExecutor
) -> None:
    executor.dry_run_ok = False
    proposal = approvals.propose("inv-1", ROLLBACK, "bad config in revision 2")
    assert proposal.status is ProposalStatus.FAILED
    with pytest.raises(ProposalStateError):
        approvals.approve(proposal.id, "alice")
    assert executor.executions == []


def test_pre_execution_dry_run_failure_blocks_execution(
    approvals: ApprovalService, executor: FakeExecutor
) -> None:
    proposal = approvals.propose("inv-1", ROLLBACK, "bad config in revision 2")
    executor.dry_run_ok = False
    result = approvals.approve(proposal.id, "alice")
    assert result.status is ProposalStatus.FAILED
    assert executor.executions == []


def test_namespace_allowlist_enforced_before_any_call(
    approvals: ApprovalService, executor: FakeExecutor
) -> None:
    action = ActionSpec(kind=ActionKind.ROLLOUT_RESTART, namespace="kube-system", deployment="x")
    with pytest.raises(NamespaceNotAllowedError):
        approvals.propose("inv-1", action, "restart coredns please")
    assert executor.dry_runs == []


@pytest.mark.parametrize("replicas", [-1, 21, None])
def test_scale_bounds(approvals: ApprovalService, replicas: int | None) -> None:
    action = ActionSpec(
        kind=ActionKind.SCALE, namespace="demo", deployment="checkout", replicas=replicas
    )
    with pytest.raises(ValueError, match="replicas"):
        approvals.validate(action)


def test_audit_log_is_hash_chained_and_tamper_evident(approvals: ApprovalService) -> None:
    proposal = approvals.propose("inv-1", ROLLBACK, "bad config in revision 2")
    approvals.approve(proposal.id, "alice")
    events = [e["event"] for e in approvals.audit.entries()]
    assert events == [
        "proposal.created",
        "proposal.approved",
        "execution.dry_run",
        "execution.result",
    ]
    assert approvals.audit.verify() == (True, "audit log intact")

    lines = approvals.audit.path.read_text().splitlines()
    entry = json.loads(lines[1])
    entry["actor"] = "mallory"
    lines[1] = json.dumps(entry, sort_keys=True)
    approvals.audit.path.write_text("\n".join(lines) + "\n")
    ok, message = approvals.audit.verify()
    assert not ok
    assert "entry 1" in message


def test_signature_verification() -> None:
    body = b'{"decision":"approve","actor":"alice"}'
    sig = sign("s3cret", "1000", body)
    assert verify_signature("s3cret", "1000", sig, body, now=1010)
    assert not verify_signature("other", "1000", sig, body, now=1010)
    assert not verify_signature("s3cret", "1000", sig, body + b" ", now=1010)
    assert not verify_signature("s3cret", "1000", sig, body, now=1000 + 301)  # replayed
    assert not verify_signature("s3cret", None, sig, body, now=1010)
