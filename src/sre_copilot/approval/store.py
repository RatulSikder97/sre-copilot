"""Persistence: SQLite for proposals/investigations, hash-chained JSONL for the audit log."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sre_copilot.models import IncidentReport, Proposal, ProposalStatus
from sre_copilot.safety import redact_obj

_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    investigation_id TEXT NOT NULL,
    status TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS investigations (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    body TEXT
);
"""


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    # -- proposals -------------------------------------------------------------------------

    def insert_proposal(self, proposal: Proposal) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO proposals (id, investigation_id, status, expires_at, body) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    proposal.id,
                    proposal.investigation_id,
                    proposal.status.value,
                    proposal.expires_at.isoformat(),
                    proposal.model_dump_json(),
                ),
            )

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        row = self._conn.execute(
            "SELECT body FROM proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        return Proposal.model_validate_json(row[0]) if row else None

    def list_proposals(
        self, status: ProposalStatus | None = None, investigation_id: str | None = None
    ) -> list[Proposal]:
        sql = "SELECT body FROM proposals WHERE 1=1"
        params: list[Any] = []
        if status is not None:
            sql += " AND status = ?"
            params.append(status.value)
        if investigation_id is not None:
            sql += " AND investigation_id = ?"
            params.append(investigation_id)
        rows = self._conn.execute(sql + " ORDER BY rowid", params).fetchall()
        return [Proposal.model_validate_json(r[0]) for r in rows]

    def transition(self, proposal: Proposal, expected: ProposalStatus, new: ProposalStatus) -> bool:
        """Compare-and-set the status; returns False if another actor got there first."""
        proposal = proposal.model_copy(update={"status": new})
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE proposals SET status = ?, body = ? WHERE id = ? AND status = ?",
                (new.value, proposal.model_dump_json(), proposal.id, expected.value),
            )
            return cur.rowcount == 1

    def save_proposal(self, proposal: Proposal) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE proposals SET status = ?, body = ? WHERE id = ?",
                (proposal.status.value, proposal.model_dump_json(), proposal.id),
            )

    # -- investigations ----------------------------------------------------------------------

    def start_investigation(self, investigation_id: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO investigations (id, status, created_at) VALUES (?, ?, ?)",
                (investigation_id, "running", datetime.now(UTC).isoformat()),
            )

    def finish_investigation(self, report: IncidentReport) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO investigations (id, status, created_at, body) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET status = excluded.status, body = excluded.body",
                (
                    report.investigation_id,
                    report.status.value,
                    report.created_at.isoformat(),
                    report.model_dump_json(),
                ),
            )

    def get_investigation(self, investigation_id: str) -> tuple[str, IncidentReport | None] | None:
        row = self._conn.execute(
            "SELECT status, body FROM investigations WHERE id = ?", (investigation_id,)
        ).fetchone()
        if row is None:
            return None
        status, body = row
        return status, IncidentReport.model_validate_json(body) if body else None


class AuditLog:
    """Append-only JSONL where every entry carries the SHA-256 of its predecessor.

    Editing or deleting an earlier line breaks the chain, which ``verify()`` detects.
    """

    GENESIS = "0" * 64

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()

    @staticmethod
    def _digest(entry: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(entry, sort_keys=True, default=str).encode()).hexdigest()

    def _last_hash(self) -> str:
        if not self.path.exists():
            return self.GENESIS
        last = ""
        with self.path.open() as fh:
            for line in fh:
                if line.strip():
                    last = line
        return str(json.loads(last)["hash"]) if last else self.GENESIS

    def append(self, event: str, actor: str, **details: Any) -> dict[str, Any]:
        with self._lock:
            entry: dict[str, Any] = {
                "ts": datetime.now(UTC).isoformat(),
                "event": event,
                "actor": actor,
                "details": redact_obj(details),
                "prev": self._last_hash(),
            }
            entry["hash"] = self._digest(entry)
            with self.path.open("a") as fh:
                fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
            return entry

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(ln) for ln in self.path.read_text().splitlines() if ln.strip()]

    def verify(self) -> tuple[bool, str]:
        prev = self.GENESIS
        for index, entry in enumerate(self.entries()):
            claimed = entry.pop("hash", None)
            if entry.get("prev") != prev:
                return False, f"entry {index}: chain broken (prev mismatch)"
            if self._digest(entry) != claimed:
                return False, f"entry {index}: content hash mismatch"
            prev = str(claimed)
        return True, "audit log intact"
