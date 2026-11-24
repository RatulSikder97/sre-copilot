"""Budget caps: steps, tool calls, tokens and wall clock all terminate the loop."""

from __future__ import annotations

from sre_copilot.approval.service import ApprovalService
from sre_copilot.config import Settings
from sre_copilot.models import Alert, ReportStatus
from tests.helpers import LoopingLLM, investigator


def _alert() -> Alert:
    return Alert(labels={"alertname": "KubePodCrashLooping", "namespace": "demo"})


def test_step_budget_forces_final_turn_with_only_submit_report(
    settings: Settings, approvals: ApprovalService
) -> None:
    settings.max_steps = 3
    llm = LoopingLLM()
    report = investigator(settings, approvals, llm).run(_alert())
    assert report.status is ReportStatus.BUDGET_EXHAUSTED
    assert "step budget" in (report.error or "")
    # 3 normal steps, then exactly one finalisation turn offering only submit_report.
    assert len(llm.offered) == 4
    assert llm.offered[-1] == ["submit_report"]
    assert "runbook_search" in llm.offered[0]
    assert report.usage.steps == 4


def test_tool_call_budget(settings: Settings, approvals: ApprovalService) -> None:
    settings.max_tool_calls = 2
    report = investigator(settings, approvals, LoopingLLM()).run(_alert())
    assert report.status is ReportStatus.BUDGET_EXHAUSTED
    assert "tool-call budget" in (report.error or "")
    assert report.usage.tool_calls == 2


def test_token_budget(settings: Settings, approvals: ApprovalService) -> None:
    settings.max_total_tokens = 1_000
    report = investigator(settings, approvals, LoopingLLM(tokens_per_turn=300)).run(_alert())
    assert report.status is ReportStatus.BUDGET_EXHAUSTED
    assert "token budget" in (report.error or "")
    assert report.usage.steps == 3  # 600, 1200 -> exhausted -> final turn


def test_wall_clock_budget(settings: Settings, approvals: ApprovalService) -> None:
    settings.max_wall_seconds = 10
    ticks = iter(range(0, 10_000, 4))
    report = investigator(settings, approvals, LoopingLLM(), clock=lambda: float(next(ticks))).run(
        _alert()
    )
    assert report.status is ReportStatus.BUDGET_EXHAUSTED
    assert "time budget" in (report.error or "")
    assert report.usage.steps < settings.max_steps
