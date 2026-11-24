"""Report schema: what the model must submit and what the copilot emits."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from sre_copilot.models import (
    ActionKind,
    ActionSpec,
    Alert,
    DryRunResult,
    Evidence,
    IncidentReport,
    Proposal,
    ReportDraft,
    ReportStatus,
    utcnow,
)
from sre_copilot.prompts import STATE_TOOLS, submit_report_tool
from sre_copilot.report import render_markdown
from tests.helpers import MINIMAL_REPORT


def test_minimal_draft_validates() -> None:
    draft = ReportDraft.model_validate(MINIMAL_REPORT)
    assert draft.root_cause.confidence == 0.5


@pytest.mark.parametrize(
    "mutation",
    [
        {"severity": "SEV0"},
        {"root_cause": {**MINIMAL_REPORT["root_cause"], "confidence": 1.5}},
        {"remediation": [{"step": "x", "risk": "yolo"}]},
        {"unexpected": "field"},
    ],
)
def test_invalid_drafts_rejected(mutation: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ReportDraft.model_validate({**MINIMAL_REPORT, **mutation})


def test_missing_required_field_rejected() -> None:
    data = {k: v for k, v in MINIMAL_REPORT.items() if k != "root_cause"}
    with pytest.raises(ValidationError):
        ReportDraft.model_validate(data)


def test_tool_schemas_are_api_shaped() -> None:
    schema = submit_report_tool()["input_schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert {
        "title",
        "severity",
        "summary",
        "impact",
        "timeline",
        "root_cause",
        "remediation",
    } <= set(schema["required"])
    assert "title" not in schema["properties"]["title"]  # pydantic titles stripped
    for tool in STATE_TOOLS:
        assert tool["input_schema"]["additionalProperties"] is False


def _report(mode: str = "replay") -> IncidentReport:
    proposal = Proposal(
        id="prop-0001",
        investigation_id="inv-1",
        action=ActionSpec(
            kind=ActionKind.ROLLBACK, namespace="demo", deployment="checkout", to_revision=1
        ),
        reason="bad config",
        dry_run=DryRunResult(
            ok=True, summary="server-side dry run OK", diff=["env.a: '2O' -> '20'"]
        ),
        expires_at=utcnow(),
    )
    return IncidentReport(
        investigation_id="inv-1",
        mode=mode,
        model="replay:test",
        status=ReportStatus.COMPLETE,
        alert=Alert(labels={"alertname": "KubePodCrashLooping"}),
        draft=ReportDraft.model_validate(
            {**MINIMAL_REPORT, "remediation": [{"step": "roll back", "proposal_id": "prop-0001"}]}
        ),
        evidence=[
            Evidence(id="E1", tool="k8s_list_pods", args={}, summary="1 pod", link="http://x")
        ],
        proposals=[proposal],
    )


def test_report_roundtrip_and_markdown() -> None:
    report = _report()
    again = IncidentReport.model_validate_json(report.model_dump_json())
    assert again == report
    md = render_markdown(report)
    assert "REPLAY MODE" in md
    assert "prop-0001" in md
    assert "rollback deployment/checkout -n demo to revision 1" in md
    assert "**E1** `k8s_list_pods`" in md
    assert "~ env.a: '2O' -> '20'" in md


def test_live_report_is_labelled_with_model() -> None:
    assert "REPLAY MODE" not in render_markdown(_report("live"))


def test_alertmanager_alert_parsing() -> None:
    alert = Alert.model_validate(
        {
            "status": "firing",
            "labels": {"alertname": "KubePodCrashLooping", "namespace": "demo"},
            "annotations": {"summary": "s"},
            "startsAt": "2026-01-01T00:00:00Z",
            "generatorURL": "http://prom/graph",
        }
    )
    assert alert.name == "KubePodCrashLooping"
    assert "label namespace=demo" in alert.describe()
