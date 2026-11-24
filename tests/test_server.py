"""HTTP surface: webhook ingest, dedup, auth, signed decisions."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from sre_copilot.approval.service import sign
from sre_copilot.config import Settings
from sre_copilot.models import ActionKind, ActionSpec
from sre_copilot.runtime import build_runtime
from sre_copilot.server import create_app
from tests.conftest import FakeExecutor
from tests.helpers import MINIMAL_REPORT, replay

ALERT: dict[str, Any] = {
    "version": "4",
    "status": "firing",
    "receiver": "sre-copilot",
    "groupKey": "{}",
    "alerts": [
        {
            "status": "firing",
            "labels": {"alertname": "KubePodCrashLooping", "namespace": "demo"},
            "annotations": {"summary": "crash looping"},
            "startsAt": "2026-01-01T00:00:00Z",
            "fingerprint": "abc",
        }
    ],
}


@pytest.fixture
def client_and_runtime(settings: Settings, tmp_path: Path) -> Any:
    fixtures = tmp_path / "fixtures.json"
    fixtures.write_text(json.dumps({"tools": [], "dry_runs": []}))
    settings.tools = "fixtures"
    settings.fixtures_path = fixtures
    settings.alertmanager_token = SecretStr("am-token")
    settings.approval_signing_secret = SecretStr("sign-secret")
    executor = FakeExecutor()
    runtime = build_runtime(settings, executor_factory=lambda: executor)

    def llm() -> Any:
        return replay(
            [{"tool_calls": [{"name": "submit_report", "input": MINIMAL_REPORT_NO_EVIDENCE}]}]
        )

    with TestClient(create_app(runtime, llm_factory=llm)) as client:
        yield client, runtime, executor


MINIMAL_REPORT_NO_EVIDENCE = {
    **MINIMAL_REPORT,
    "timeline": [{"time": "T0", "event": "e"}],
    "root_cause": {**MINIMAL_REPORT["root_cause"], "evidence_ids": []},
}


def test_webhook_requires_token(client_and_runtime: Any) -> None:
    client, _, _ = client_and_runtime
    assert client.post("/webhooks/alertmanager", json=ALERT).status_code == 401


def test_webhook_starts_investigation_and_report_is_served(client_and_runtime: Any) -> None:
    client, _, _ = client_and_runtime
    resp = client.post(
        "/webhooks/alertmanager", json=ALERT, headers={"Authorization": "Bearer am-token"}
    )
    assert resp.status_code == 202
    (inv_id,) = resp.json()["investigations"]
    body = client.get(f"/investigations/{inv_id}").json()
    assert body["status"] == "complete"
    assert "REPLAY MODE" in body["markdown"]
    assert body["report"]["alert"]["labels"]["alertname"] == "KubePodCrashLooping"
    assert client.get("/investigations/nope").status_code == 404


def test_resolved_alerts_are_ignored(client_and_runtime: Any) -> None:
    client, _, _ = client_and_runtime
    payload = {**ALERT, "alerts": [{**ALERT["alerts"][0], "status": "resolved"}]}
    resp = client.post(
        "/webhooks/alertmanager", json=payload, headers={"Authorization": "Bearer am-token"}
    )
    assert resp.json()["investigations"] == []


def _signed(body: dict[str, Any], secret: str = "sign-secret") -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    return raw, {
        "x-copilot-request-timestamp": ts,
        "x-copilot-signature": sign(secret, ts, raw),
        "content-type": "application/json",
    }


def test_signed_decision_flow(client_and_runtime: Any) -> None:
    client, runtime, executor = client_and_runtime
    proposal = runtime.approvals.propose(
        "inv-1",
        ActionSpec(kind=ActionKind.ROLLBACK, namespace="demo", deployment="checkout"),
        "bad config in revision 2",
    )
    raw, headers = _signed({"decision": "approve", "actor": "alice"}, secret="wrong")
    resp = client.post(f"/proposals/{proposal.id}/decision", content=raw, headers=headers)
    assert resp.status_code == 401
    assert executor.executions == []

    raw, headers = _signed({"decision": "approve", "actor": "alice"})
    resp = client.post(f"/proposals/{proposal.id}/decision", content=raw, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "executed"
    assert len(executor.executions) == 1

    resp = client.post(f"/proposals/{proposal.id}/decision", content=raw, headers=headers)
    assert resp.status_code == 409
    assert len(executor.executions) == 1

    listed = client.get("/proposals", params={"status": "executed"}).json()
    assert [p["id"] for p in listed] == [proposal.id]
    events = [e["event"] for e in runtime.audit.entries()]
    assert "decision.rejected" in events


def test_decision_endpoint_disabled_without_secret(settings: Settings) -> None:
    runtime = build_runtime(settings, executor_factory=FakeExecutor)
    with TestClient(create_app(runtime)) as client:
        raw, headers = _signed({"decision": "approve", "actor": "a"})
        assert client.post("/proposals/x/decision", content=raw, headers=headers).status_code == 403
