from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from reactor_agent.domain.models import ReactorModelSpec


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self, *, check_same_thread: bool = True) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=check_same_thread, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialize(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    current_version INTEGER NOT NULL,
                    user_request TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS specs (
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(task_id, version)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    spec_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS queue (
                    queue_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    claimed_at TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_queue_status_created ON queue(status, created_at);
                CREATE TABLE IF NOT EXISTS chat_messages (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL UNIQUE,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                    content TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    event_key TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(task_id, event_key)
                );
                CREATE INDEX IF NOT EXISTS ix_chat_task_sequence ON chat_messages(task_id,sequence);
                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT,
                    event_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def create_task(self, spec: ReactorModelSpec) -> None:
        now = self._now()
        payload = spec.model_dump_json()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO tasks(task_id,status,created_at,updated_at,current_version,user_request) VALUES(?,?,?,?,?,?)",
                (spec.task_id, "needs_input", now, now, spec.version, spec.user_request),
            )
            conn.execute(
                "INSERT INTO specs(task_id,version,payload,created_at) VALUES(?,?,?,?)",
                (spec.task_id, spec.version, payload, now),
            )
            conn.commit()

    def save_spec(self, spec: ReactorModelSpec, status: str = "needs_input") -> None:
        now = self._now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT 1 FROM tasks WHERE task_id=?", (spec.task_id,)).fetchone()
            if not existing:
                raise KeyError(f"Unknown task {spec.task_id}")
            conn.execute(
                "INSERT INTO specs(task_id,version,payload,created_at) VALUES(?,?,?,?)",
                (spec.task_id, spec.version, spec.model_dump_json(), now),
            )
            conn.execute(
                "UPDATE tasks SET status=?,updated_at=?,current_version=?,user_request=? WHERE task_id=?",
                (status, now, spec.version, spec.user_request, spec.task_id),
            )
            conn.commit()
        self.audit(spec.task_id, "spec_version_created", f"Created specification version {spec.version}")

    def get_spec(self, task_id: str, version: int | None = None) -> ReactorModelSpec:
        with self.connect() as conn:
            if version is None:
                row = conn.execute(
                    "SELECT s.payload FROM specs s JOIN tasks t USING(task_id) WHERE task_id=? AND s.version=t.current_version",
                    (task_id,),
                ).fetchone()
            else:
                row = conn.execute("SELECT payload FROM specs WHERE task_id=? AND version=?", (task_id, version)).fetchone()
        if row is None:
            raise KeyError(f"Specification not found for task {task_id}")
        return ReactorModelSpec.model_validate_json(row["payload"])

    def list_tasks(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM tasks ORDER BY updated_at DESC")]

    def set_status(self, task_id: str, status: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE tasks SET status=?,updated_at=? WHERE task_id=?", (status, self._now(), task_id))

    def audit(self, task_id: str | None, event_type: str, summary: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO audit_events VALUES(?,?,?,?,?)",
                (str(uuid4()), task_id, event_type, summary, self._now()),
            )

    def enqueue(self, task_id: str, payload: dict[str, Any]) -> str:
        queue_id = str(uuid4())
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT queue_id,payload FROM queue WHERE task_id=? AND status IN ('queued','running')", (task_id,)).fetchall()
            for row in existing:
                if json.loads(row["payload"]).get("spec_version") == payload.get("spec_version"):
                    conn.commit()
                    return row["queue_id"]
            conn.execute(
                "INSERT INTO queue(queue_id,task_id,payload,created_at) VALUES(?,?,?,?)",
                (queue_id, task_id, json.dumps(payload), self._now()),
            )
            conn.commit()
        self.audit(task_id, "queued", f"Queued execution request {queue_id}")
        return queue_id

    def claim_next(self, queue_id: str | None = None) -> dict[str, Any] | None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM queue WHERE status='queued' AND (? IS NULL OR queue_id=?) ORDER BY created_at LIMIT 1",
                (queue_id, queue_id),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                "UPDATE queue SET status='running',attempts=attempts+1,claimed_at=? WHERE queue_id=?",
                (self._now(), row["queue_id"]),
            )
            conn.commit()
            return {**dict(row), "payload": json.loads(row["payload"]), "attempts": row["attempts"] + 1}

    def complete_queue_item(self, queue_id: str, *, error: str | None = None) -> None:
        status = "failed" if error else "done"
        with self.connect() as conn:
            conn.execute("UPDATE queue SET status=?,error=? WHERE queue_id=?", (status, error, queue_id))

    def recover_interrupted_queue_items(self, older_than_seconds: int = 0) -> int:
        """Requeue records left running by a process crash; callers invoke at worker startup."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat()
            conn.execute("UPDATE queue SET status='failed',error='Recovery retry budget exhausted' WHERE status='running' AND claimed_at<? AND attempts>=2", (cutoff,))
            cursor = conn.execute("UPDATE queue SET status='queued',claimed_at=NULL WHERE status='running' AND claimed_at<? AND attempts<2", (cutoff,))
            conn.commit()
            return cursor.rowcount

    def save_run(self, run_id: str, task_id: str, spec_version: int, status: str, payload: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,?)",
                (run_id, task_id, spec_version, status, json.dumps(payload), self._now()),
            )
        self.audit(task_id, "run_recorded", f"Run {run_id} recorded as {status}")

    def audit_events(self, task_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT event_type,summary,created_at FROM audit_events WHERE task_id=? ORDER BY created_at",
                (task_id,),
            )]


    def list_runs(self, task_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in conn.execute(
                "SELECT * FROM runs WHERE task_id=? ORDER BY created_at DESC", (task_id,))]

    def get_queue_item(self, queue_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM queue WHERE queue_id=?", (queue_id,)).fetchone()
            return dict(row) if row else None

    def heartbeat_queue_item(self, queue_id: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE queue SET claimed_at=? WHERE queue_id=? AND status='running'", (self._now(), queue_id))

    def add_chat_message(self, task_id: str, role: str, content: str, metadata: dict[str, Any] | None = None,
                         event_key: str | None = None) -> None:
        if role not in {"user", "assistant"}:
            raise ValueError("Only visible user and assistant messages may be stored.")
        with self.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO chat_messages(message_id,task_id,role,content,metadata,event_key,created_at) VALUES(?,?,?,?,?,?,?)",
                         (str(uuid4()), task_id, role, content, json.dumps(metadata or {}), event_key, self._now()))

    def chat_messages(self, task_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [{**dict(row), "metadata": json.loads(row["metadata"])} for row in conn.execute(
                "SELECT * FROM chat_messages WHERE task_id=? ORDER BY sequence", (task_id,))]

    def active_queue_items(self, task_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM queue WHERE task_id=? AND status IN ('queued','running') ORDER BY created_at", (task_id,))]
