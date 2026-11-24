"""Command-line interface."""

from __future__ import annotations

import getpass
import json
import logging
import time
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer

from sre_copilot.config import Settings, load_settings
from sre_copilot.models import Alert, AlertmanagerWebhook, Proposal, ProposalStatus
from sre_copilot.report import render_markdown
from sre_copilot.runtime import ConfigurationError, Runtime, build_runtime

app = typer.Typer(
    help="Agentic incident-response copilot with a human approval gate.",
    no_args_is_help=True,
    add_completion=False,
)
audit_app = typer.Typer(help="Inspect the tamper-evident audit log.", no_args_is_help=True)
runbooks_app = typer.Typer(help="Search local runbooks.", no_args_is_help=True)
app.add_typer(audit_app, name="audit")
app.add_typer(runbooks_app, name="runbooks")

ReplayOpt = Annotated[
    Path | None, typer.Option("--replay", help="Replay script (scripted model, no API key)")
]
FixturesOpt = Annotated[
    Path | None, typer.Option("--fixtures", help="Recorded tool fixtures (no cluster needed)")
]
RecordOpt = Annotated[
    Path | None, typer.Option("--record", help="Record live read-tool outputs to this file")
]
StateOpt = Annotated[Path | None, typer.Option("--state-dir", help="Approval queue + audit dir")]


def _settings(
    replay: Path | None = None,
    fixtures: Path | None = None,
    record: Path | None = None,
    state_dir: Path | None = None,
) -> Settings:
    overrides: dict[str, object] = {"state_dir": state_dir, "record_path": record}
    if replay is not None:
        overrides.update(llm="replay", replay_script=replay)
    if fixtures is not None:
        overrides.update(tools="fixtures", fixtures_path=fixtures)
    return load_settings(**overrides)


def _runtime(settings: Settings) -> Runtime:
    try:
        return build_runtime(settings)
    except (ConfigurationError, FileNotFoundError) as exc:
        typer.secho(f"configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@app.command()
def investigate(
    symptom: Annotated[str, typer.Argument(help="What is wrong, in plain words")] = "",
    alert_file: Annotated[
        Path | None, typer.Option("--alert-file", help="Alertmanager webhook JSON payload")
    ] = None,
    replay: ReplayOpt = None,
    fixtures: FixturesOpt = None,
    record: RecordOpt = None,
    state_dir: StateOpt = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the report as JSON")] = False,
    out: Annotated[Path | None, typer.Option("--out", help="Also write Markdown here")] = None,
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """Investigate a symptom (or the first firing alert of a payload) and print a report."""
    _setup_logging(verbose)
    if not symptom and alert_file is None:
        raise typer.BadParameter("give a SYMPTOM or --alert-file")
    settings = _settings(replay, fixtures, record, state_dir)
    runtime = _runtime(settings)
    if alert_file is not None:
        payload = AlertmanagerWebhook.model_validate_json(alert_file.read_text())
        firing = [a for a in payload.alerts if a.status == "firing"]
        if not firing:
            raise typer.BadParameter("payload has no firing alerts")
        alert = firing[0]
    else:
        alert = Alert(labels={"alertname": "ManualInvestigation"})
    report = runtime.investigator().run(alert, symptom=symptom or None)
    runtime.save_recording()
    markdown = render_markdown(report)
    if out is not None:
        out.write_text(markdown)
    typer.echo(report.model_dump_json(indent=2) if as_json else markdown)
    if report.status.value == "error":
        raise typer.Exit(1)


@app.command()
def serve(
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option()] = 8080,
    replay: ReplayOpt = None,
    fixtures: FixturesOpt = None,
    record: RecordOpt = None,
    state_dir: StateOpt = None,
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """Run the HTTP server (Alertmanager webhook + signed approval endpoint)."""
    import uvicorn

    from sre_copilot.server import create_app

    _setup_logging(verbose)
    runtime = _runtime(_settings(replay, fixtures, record, state_dir))
    uvicorn.run(create_app(runtime), host=host, port=port, log_level="info")


def _print_proposal(p: Proposal) -> None:
    typer.echo(f"{p.id}  [{p.status.value}]  {p.action.describe()}")
    typer.echo(f"  investigation: {p.investigation_id}")
    typer.echo(f"  reason:        {p.reason}")
    typer.echo(f"  dry run:       {p.dry_run.summary}")
    for line in p.dry_run.diff:
        typer.echo(f"    ~ {line}")
    typer.echo(f"  expires:       {p.expires_at.isoformat()}")
    if p.decided_by:
        typer.echo(f"  decided by:    {p.decided_by} at {p.decided_at}")
    if p.execution:
        typer.echo(f"  execution:     {p.execution.summary}")
        if p.execution.verification:
            typer.echo(f"  verification:  {p.execution.verification}")


@app.command("proposals")
def proposals_cmd(
    status: Annotated[ProposalStatus | None, typer.Option(help="Filter by status")] = None,
    state_dir: StateOpt = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print proposals as JSON")] = False,
) -> None:
    """List proposals in the approval queue (expired ones are marked denied-by-default)."""
    runtime = _runtime(_settings(state_dir=state_dir))
    runtime.approvals.expire_stale()
    items = runtime.store.list_proposals(status)
    if as_json:
        typer.echo(json.dumps([p.model_dump(mode="json") for p in items], indent=2))
        return
    if not items:
        typer.echo("no proposals")
    for p in items:
        _print_proposal(p)


@app.command()
def show(proposal_id: str, state_dir: StateOpt = None) -> None:
    """Show a proposal including its server-side dry-run diff."""
    runtime = _runtime(_settings(state_dir=state_dir))
    runtime.approvals.expire_stale()
    proposal = runtime.store.get_proposal(proposal_id)
    if proposal is None:
        typer.secho(f"unknown proposal {proposal_id}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    _print_proposal(proposal)


@app.command()
def approve(
    proposal_id: str,
    actor: Annotated[str | None, typer.Option(help="Approver identity (default: $USER)")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt")] = False,
    state_dir: StateOpt = None,
) -> None:
    """Approve a proposal: re-run the dry run, execute, verify the rollout, audit everything."""
    from sre_copilot.approval.service import ApprovalError

    runtime = _runtime(_settings(state_dir=state_dir))
    proposal = runtime.store.get_proposal(proposal_id)
    if proposal is None:
        typer.secho(f"unknown proposal {proposal_id}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    _print_proposal(proposal)
    if not yes and not typer.confirm("Execute this action?", default=False):
        typer.echo("aborted; proposal left pending")
        raise typer.Exit(1)
    try:
        result = runtime.approvals.approve(proposal_id, actor or getpass.getuser())
    except ApprovalError as exc:
        typer.secho(f"not executed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    _print_proposal(result)
    if result.status is not ProposalStatus.EXECUTED or not (
        result.execution and result.execution.verified
    ):
        raise typer.Exit(1)


@app.command()
def deny(
    proposal_id: str,
    actor: Annotated[str | None, typer.Option()] = None,
    note: Annotated[str, typer.Option()] = "",
    state_dir: StateOpt = None,
) -> None:
    """Deny a pending proposal."""
    from sre_copilot.approval.service import ApprovalError

    runtime = _runtime(_settings(state_dir=state_dir))
    try:
        result = runtime.approvals.deny(proposal_id, actor or getpass.getuser(), note)
    except ApprovalError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    _print_proposal(result)


@audit_app.command("verify")
def audit_verify(state_dir: StateOpt = None) -> None:
    """Verify the audit log hash chain."""
    runtime = _runtime(_settings(state_dir=state_dir))
    ok, message = runtime.audit.verify()
    typer.secho(message, fg=typer.colors.GREEN if ok else typer.colors.RED)
    if not ok:
        raise typer.Exit(1)


@audit_app.command("show")
def audit_show(state_dir: StateOpt = None) -> None:
    """Print audit log entries."""
    runtime = _runtime(_settings(state_dir=state_dir))
    for entry in runtime.audit.entries():
        details = json.dumps(entry["details"], sort_keys=True)
        typer.echo(f"{entry['ts']}  {entry['event']:<20} {entry['actor']:<14} {details}")


@runbooks_app.command("search")
def runbooks_search(query: str, k: Annotated[int, typer.Option()] = 3) -> None:
    """BM25 search over the runbooks directory."""
    from sre_copilot.tools.runbooks import BM25Index

    index = BM25Index.from_dir(load_settings().runbooks_dir)
    for doc, score in index.search(query, k):
        typer.echo(f"{score:6.3f}  {doc.title}  ({doc.path})")


def _alerts_from_prometheus(prometheus_url: str, alertname: str) -> list[dict[str, Any]]:
    resp = httpx.get(f"{prometheus_url.rstrip('/')}/api/v1/alerts", timeout=10.0)
    resp.raise_for_status()
    alerts = []
    for a in resp.json()["data"]["alerts"]:
        if a.get("state") != "firing" or a["labels"].get("alertname") != alertname:
            continue
        alerts.append(
            {
                "status": "firing",
                "labels": a["labels"],
                "annotations": a.get("annotations", {}),
                "startsAt": a.get("activeAt"),
                "generatorURL": f"{prometheus_url.rstrip('/')}/alerts",
                "fingerprint": ",".join(f"{k}={v}" for k, v in sorted(a["labels"].items())),
            }
        )
    return alerts


@app.command("fire-alert")
def fire_alert(
    alertname: Annotated[str, typer.Option(help="Prometheus alert to forward")],
    prometheus_url: Annotated[str, typer.Option()] = "http://localhost:39090",
    webhook_url: Annotated[str, typer.Option()] = "http://127.0.0.1:8080",
    wait_firing: Annotated[float, typer.Option(help="Seconds to wait for the alert")] = 300.0,
    wait_report: Annotated[float, typer.Option(help="Seconds to wait for the report")] = 300.0,
    out: Annotated[Path | None, typer.Option(help="Write the Markdown report here")] = None,
) -> None:
    """Forward a firing Prometheus alert to the copilot in Alertmanager webhook format.

    Stands in for Alertmanager routing in the demo: polls Prometheus until ALERTNAME fires,
    posts an Alertmanager v4 payload to /webhooks/alertmanager, then waits for the report.
    """
    deadline = time.monotonic() + wait_firing
    alerts: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        alerts = _alerts_from_prometheus(prometheus_url, alertname)
        if alerts:
            break
        typer.echo(f"waiting for {alertname} to fire...")
        time.sleep(5)
    if not alerts:
        typer.secho(f"{alertname} did not fire within {wait_firing:.0f}s", fg="red", err=True)
        raise typer.Exit(1)
    payload = {
        "version": "4",
        "status": "firing",
        "receiver": "sre-copilot",
        "groupKey": f'{{}}:{{alertname="{alertname}"}}',
        "commonLabels": {"alertname": alertname},
        "alerts": alerts[:1],
    }
    resp = httpx.post(f"{webhook_url}/webhooks/alertmanager", json=payload, timeout=10.0)
    resp.raise_for_status()
    inv_ids = resp.json()["investigations"]
    if not inv_ids:
        typer.secho("no investigation started (deduplicated?)", fg="red", err=True)
        raise typer.Exit(1)
    inv_id = inv_ids[0]
    typer.echo(f"alert forwarded; investigation {inv_id}")
    deadline = time.monotonic() + wait_report
    while time.monotonic() < deadline:
        body = httpx.get(f"{webhook_url}/investigations/{inv_id}", timeout=10.0).json()
        if body.get("markdown"):
            if out is not None:
                out.write_text(body["markdown"])
            typer.echo(body["markdown"])
            if body["status"] == "error":
                raise typer.Exit(1)
            return
        time.sleep(2)
    typer.secho("timed out waiting for the report", fg="red", err=True)
    raise typer.Exit(1)


if __name__ == "__main__":
    app()
