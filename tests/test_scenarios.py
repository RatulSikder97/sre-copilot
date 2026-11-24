"""Offline replay of the recorded demo incidents (real tool outputs captured from kind)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from sre_copilot.cli import app
from sre_copilot.config import Settings
from sre_copilot.llm.replay import ReplayLLM
from sre_copilot.models import AlertmanagerWebhook, ProposalStatus, ReportStatus
from sre_copilot.runtime import build_runtime

SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios"


@pytest.mark.parametrize("name", ["checkout-crashloop", "checkout-oom"])
def test_recorded_scenario_replays_to_a_complete_report(settings: Settings, name: str) -> None:
    root = SCENARIOS / name
    settings.tools = "fixtures"
    settings.fixtures_path = root / "fixtures.json"
    runtime = build_runtime(settings)
    alert = AlertmanagerWebhook.model_validate_json((root / "alert.json").read_text()).alerts[0]
    report = runtime.investigator(ReplayLLM.from_path(root / "script.yaml")).run(alert)

    assert report.status is ReportStatus.COMPLETE, report.error
    assert report.mode == "replay"
    assert report.draft is not None
    assert len(report.proposals) == 1
    proposal = report.proposals[0]
    assert proposal.status is ProposalStatus.PENDING
    assert proposal.dry_run.ok
    assert report.draft.remediation[0].proposal_id == proposal.id
    assert all(not e.is_error for e in report.evidence)
    assert report.usage.steps <= settings.max_steps

    # Fixtures mode can never execute: approval runs the gate but the executor refuses.
    result = runtime.approvals.approve(proposal.id, "tester")
    assert result.status is ProposalStatus.FAILED
    ok, _ = runtime.audit.verify()
    assert ok


def test_cli_replay_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SRE_COPILOT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SRE_COPILOT_RUNBOOKS_DIR", str(SCENARIOS.parent / "runbooks"))
    root = SCENARIOS / "checkout-crashloop"
    out = tmp_path / "report.md"
    result = CliRunner().invoke(
        app,
        [
            "investigate",
            "--replay",
            str(root / "script.yaml"),
            "--fixtures",
            str(root / "fixtures.json"),
            "--alert-file",
            str(root / "alert.json"),
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "REPLAY MODE" in out.read_text()
    listed = CliRunner().invoke(app, ["proposals", "--json"])
    assert listed.exit_code == 0
    assert '"status": "pending"' in listed.output
