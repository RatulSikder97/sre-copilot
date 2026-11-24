"""Shared fixtures: an offline runtime with a fake executor and scripted tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sre_copilot.approval.service import ApprovalService
from sre_copilot.approval.store import AuditLog, Store
from sre_copilot.config import Settings
from sre_copilot.models import ActionSpec, DryRunResult, ExecutionResult

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class FakeExecutor:
    """Records every call so tests can assert nothing is written without approval."""

    dry_run_ok: bool = True
    generation: int = 7
    dry_runs: list[ActionSpec] = field(default_factory=list)
    executions: list[tuple[ActionSpec, int | None]] = field(default_factory=list)

    def dry_run(self, action: ActionSpec) -> DryRunResult:
        self.dry_runs.append(action)
        return DryRunResult(
            ok=self.dry_run_ok,
            summary="server-side dry run OK" if self.dry_run_ok else "dry run failed",
            diff=["env.checkout.DB_POOL_SIZE: '2O' -> '20'"],
            resolved_revision=1 if action.kind.value == "rollback" else None,
            observed_generation=self.generation,
        )

    def execute(self, action: ActionSpec, expected_generation: int | None) -> ExecutionResult:
        self.executions.append((action, expected_generation))
        return ExecutionResult(ok=True, summary="applied", verified=True, verification="ok")


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        state_dir=tmp_path / "state",
        runbooks_dir=ROOT / "runbooks",
        allowed_namespaces=["demo"],
        approval_ttl_seconds=600,
    )


@pytest.fixture
def executor() -> FakeExecutor:
    return FakeExecutor()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def approvals(settings: Settings, executor: FakeExecutor, clock: Clock) -> ApprovalService:
    return ApprovalService(
        Store(settings.db_path),
        AuditLog(settings.audit_log_path),
        lambda: executor,
        allowed_namespaces=settings.allowed_namespaces,
        ttl_seconds=settings.approval_ttl_seconds,
        max_replicas=settings.max_scale_replicas,
        clock=clock,
    )
