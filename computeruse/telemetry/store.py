"""SQLite-backed persistence for sessions, events and frames.

Frames (PNG) live on disk under <data_dir>/sessions/<id>/frames/<seq>.png and
are referenced by URL from `frame` events, so the UI can replay any session.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from computeruse.telemetry.events import (
    Event,
    EventType,
    Outcome,
    SessionRecord,
    SessionStatus,
    Usage,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    started_at REAL,
    ended_at REAL,
    task TEXT NOT NULL,
    backend TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    outcome TEXT,
    outcome_reason TEXT,
    steps INTEGER NOT NULL DEFAULT 0,
    turns INTEGER NOT NULL DEFAULT 0,
    duration_ms REAL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL,
    display_width INTEGER,
    display_height INTEGER,
    live_view_url TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    eval_run_id TEXT,
    eval_task_id TEXT,
    eval_passed INTEGER,
    eval_detail TEXT,
    metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_eval ON sessions(eval_run_id);
CREATE TABLE IF NOT EXISTS events (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
CREATE TABLE IF NOT EXISTS eval_runs (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    suite TEXT NOT NULL,
    model TEXT NOT NULL,
    backend TEXT,
    status TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '{}'
);
"""


class Store:
    def __init__(self, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "computeruse.sqlite3"
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- sessions ----------------------------------------------------------

    def create_session(self, rec: SessionRecord) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO sessions (id, created_at, started_at, ended_at, task, backend, model, status,
                   outcome, outcome_reason, steps, turns, duration_ms, input_tokens, output_tokens,
                   cache_read_input_tokens, cache_creation_input_tokens, cost_usd, display_width,
                   display_height, live_view_url, tags, eval_run_id, eval_task_id, eval_passed, eval_detail, metadata)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                self._row(rec),
            )

    def update_session(self, rec: SessionRecord) -> None:
        with self._lock:
            row = self._row(rec)
            self._conn.execute(
                """UPDATE sessions SET created_at=?, started_at=?, ended_at=?, task=?, backend=?, model=?,
                   status=?, outcome=?, outcome_reason=?, steps=?, turns=?, duration_ms=?, input_tokens=?,
                   output_tokens=?, cache_read_input_tokens=?, cache_creation_input_tokens=?, cost_usd=?,
                   display_width=?, display_height=?, live_view_url=?, tags=?, eval_run_id=?, eval_task_id=?,
                   eval_passed=?, eval_detail=?, metadata=? WHERE id=?""",
                row[1:] + (rec.id,),
            )

    @staticmethod
    def _row(r: SessionRecord) -> tuple:
        return (
            r.id, r.created_at, r.started_at, r.ended_at, r.task, r.backend, r.model, r.status.value,
            r.outcome.value if r.outcome else None, r.outcome_reason, r.steps, r.turns, r.duration_ms,
            r.usage.input_tokens, r.usage.output_tokens, r.usage.cache_read_input_tokens,
            r.usage.cache_creation_input_tokens, r.cost_usd, r.display_width, r.display_height,
            r.live_view_url, json.dumps(r.tags), r.eval_run_id, r.eval_task_id,
            None if r.eval_passed is None else int(r.eval_passed), r.eval_detail, json.dumps(r.metadata),
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            id=row["id"], task=row["task"], backend=row["backend"], model=row["model"],
            status=SessionStatus(row["status"]),
            outcome=Outcome(row["outcome"]) if row["outcome"] else None,
            outcome_reason=row["outcome_reason"], created_at=row["created_at"],
            started_at=row["started_at"], ended_at=row["ended_at"], steps=row["steps"], turns=row["turns"],
            duration_ms=row["duration_ms"],
            usage=Usage(
                input_tokens=row["input_tokens"], output_tokens=row["output_tokens"],
                cache_read_input_tokens=row["cache_read_input_tokens"],
                cache_creation_input_tokens=row["cache_creation_input_tokens"],
            ),
            cost_usd=row["cost_usd"], display_width=row["display_width"], display_height=row["display_height"],
            live_view_url=row["live_view_url"], tags=json.loads(row["tags"] or "[]"),
            eval_run_id=row["eval_run_id"], eval_task_id=row["eval_task_id"],
            eval_passed=None if row["eval_passed"] is None else bool(row["eval_passed"]),
            eval_detail=row["eval_detail"], metadata=json.loads(row["metadata"] or "{}"),
        )

    def get_session(self, session_id: str) -> SessionRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return self._from_row(row) if row else None

    def list_sessions(self, limit: int = 100, offset: int = 0, backend: str | None = None,
                      eval_run_id: str | None = None, status: str | None = None,
                      since: float | None = None) -> list[SessionRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        for clause, value in (("backend=?", backend), ("eval_run_id=?", eval_run_id),
                              ("status=?", status), ("created_at>=?", since)):
            if value:
                clauses.append(clause)
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM sessions {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [self._from_row(r) for r in rows]

    def mark_interrupted(self) -> int:
        """On startup, sessions left non-terminal by a previous process are failed."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE sessions SET status=?, outcome=?, outcome_reason=?, ended_at=? WHERE status IN (?,?,?,?,?)",
                (SessionStatus.FAILED.value, Outcome.INTERNAL_ERROR.value, "server restarted mid-session",
                 time.time(), SessionStatus.CREATED.value, SessionStatus.STARTING.value,
                 SessionStatus.RUNNING.value, SessionStatus.PAUSED.value, SessionStatus.AWAITING_APPROVAL.value),
            )
            return cur.rowcount

    # -- events ------------------------------------------------------------

    def append_event(self, event: Event) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO events (session_id, seq, ts, type, data) VALUES (?,?,?,?,?)",
                (event.session_id, event.seq, event.ts, event.type.value, json.dumps(event.data)),
            )

    def get_events(self, session_id: str, after_seq: int = -1, types: list[str] | None = None,
                   limit: int = 100000) -> list[Event]:
        q = "SELECT * FROM events WHERE session_id=? AND seq>?"
        params: list[Any] = [session_id, after_seq]
        if types:
            q += f" AND type IN ({','.join('?' * len(types))})"
            params.extend(types)
        q += " ORDER BY seq LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [Event(session_id=r["session_id"], seq=r["seq"], ts=r["ts"], type=EventType(r["type"]),
                      data=json.loads(r["data"])) for r in rows]

    def events_by_type(self, event_type: EventType, since: float | None = None,
                       limit: int = 200000) -> list[Event]:
        q = "SELECT * FROM events WHERE type=?"
        params: list[Any] = [event_type.value]
        if since:
            q += " AND ts>=?"
            params.append(since)
        q += " ORDER BY ts LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [Event(session_id=r["session_id"], seq=r["seq"], ts=r["ts"], type=EventType(r["type"]),
                      data=json.loads(r["data"])) for r in rows]

    # -- frames ------------------------------------------------------------

    def session_dir(self, session_id: str) -> Path:
        d = self.data_dir / "sessions" / session_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_frame(self, session_id: str, seq: int, png: bytes) -> Path:
        frames = self.session_dir(session_id) / "frames"
        frames.mkdir(exist_ok=True)
        path = frames / f"{seq}.png"
        path.write_bytes(png)
        return path

    def frame_path(self, session_id: str, seq: int) -> Path | None:
        path = self.data_dir / "sessions" / session_id / "frames" / f"{seq}.png"
        return path if path.exists() else None

    # -- eval runs ---------------------------------------------------------

    def create_eval_run(self, run_id: str, suite: str, model: str, backend: str | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO eval_runs (id, created_at, suite, model, backend, status, summary) VALUES (?,?,?,?,?,?,?)",
                (run_id, time.time(), suite, model, backend, "running", "{}"),
            )

    def finish_eval_run(self, run_id: str, summary: dict[str, Any], status: str = "finished") -> None:
        with self._lock:
            self._conn.execute("UPDATE eval_runs SET status=?, summary=? WHERE id=?",
                               (status, json.dumps(summary), run_id))

    def list_eval_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM eval_runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) | {"summary": json.loads(r["summary"] or "{}")} for r in rows]

    def get_eval_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM eval_runs WHERE id=?", (run_id,)).fetchone()
        return (dict(row) | {"summary": json.loads(row["summary"] or "{}")}) if row else None
