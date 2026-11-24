"""HTTP surface: Alertmanager webhook ingest, investigation results, signed approval decisions."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from sre_copilot.agent import new_investigation_id
from sre_copilot.approval.service import (
    ApprovalError,
    ProposalExpiredError,
    ProposalNotFoundError,
    verify_signature,
)
from sre_copilot.llm.base import LLMClient
from sre_copilot.models import AlertmanagerWebhook, ProposalStatus
from sre_copilot.report import render_markdown
from sre_copilot.runtime import Runtime, build_llm

log = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 30.0


class Decision(BaseModel):
    decision: Literal["approve", "deny"]
    actor: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=500)


def create_app(runtime: Runtime, llm_factory: Any = None) -> FastAPI:
    settings = runtime.settings
    make_llm = llm_factory or (lambda: build_llm(settings))
    active: dict[str, str] = {}  # alert fingerprint -> running investigation id
    lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async def sweeper() -> None:
            while True:
                expired = await asyncio.to_thread(runtime.approvals.expire_stale)
                if expired:
                    log.info("expired proposals (default deny): %s", expired)
                await asyncio.sleep(SWEEP_INTERVAL_SECONDS)

        task = asyncio.create_task(sweeper())
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    app = FastAPI(title="sre-copilot", version="0.1.0", lifespan=lifespan)

    def investigate(inv_id: str, payload_alert: Any, fingerprint: str) -> None:
        try:
            llm: LLMClient = make_llm()
            runtime.investigator(llm).run(payload_alert, investigation_id=inv_id)
            runtime.save_recording()
        except Exception:
            log.exception("investigation %s crashed", inv_id)
        finally:
            with lock:
                active.pop(fingerprint, None)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/alertmanager", status_code=status.HTTP_202_ACCEPTED)
    def alertmanager(
        payload: AlertmanagerWebhook, request: Request, background: BackgroundTasks
    ) -> dict[str, Any]:
        token = settings.alertmanager_token
        if token is not None:
            header = request.headers.get("authorization", "")
            if not hmac.compare_digest(header, f"Bearer {token.get_secret_value()}"):
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
        started, skipped = [], []
        for alert in payload.alerts:
            if alert.status != "firing":
                continue
            fingerprint = alert.fingerprint or ",".join(
                f"{k}={v}" for k, v in sorted(alert.labels.items())
            )
            with lock:
                if fingerprint in active:
                    skipped.append(active[fingerprint])
                    continue
                inv_id = new_investigation_id()
                active[fingerprint] = inv_id
            runtime.store.start_investigation(inv_id)
            background.add_task(investigate, inv_id, alert, fingerprint)
            started.append(inv_id)
        return {"investigations": started, "deduplicated": skipped}

    @app.get("/investigations/{investigation_id}")
    def get_investigation(investigation_id: str) -> dict[str, Any]:
        found = runtime.store.get_investigation(investigation_id)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown investigation")
        inv_status, report = found
        body: dict[str, Any] = {"id": investigation_id, "status": inv_status}
        if report is not None:
            body["report"] = report.model_dump(mode="json")
            body["markdown"] = render_markdown(report)
        return body

    @app.get("/proposals")
    def list_proposals(
        status_filter: ProposalStatus | None = Query(default=None, alias="status"),
    ) -> list[dict[str, Any]]:
        runtime.approvals.expire_stale()
        return [p.model_dump(mode="json") for p in runtime.store.list_proposals(status_filter)]

    @app.post("/proposals/{proposal_id}/decision")
    async def decide(proposal_id: str, request: Request) -> dict[str, Any]:
        secret = settings.approval_signing_secret
        if secret is None:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "webhook approvals disabled (no signing secret)"
            )
        raw = await request.body()
        if not verify_signature(
            secret.get_secret_value(),
            request.headers.get("x-copilot-request-timestamp"),
            request.headers.get("x-copilot-signature"),
            raw,
        ):
            runtime.audit.append(
                "decision.rejected", actor="unknown", proposal_id=proposal_id, why="bad signature"
            )
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")
        decision = Decision.model_validate_json(raw)
        try:
            if decision.decision == "approve":
                proposal = await asyncio.to_thread(
                    runtime.approvals.approve, proposal_id, decision.actor
                )
            else:
                proposal = runtime.approvals.deny(proposal_id, decision.actor, decision.note)
        except ProposalNotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except ProposalExpiredError as exc:
            raise HTTPException(status.HTTP_410_GONE, str(exc)) from exc
        except ApprovalError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return proposal.model_dump(mode="json")

    return app
