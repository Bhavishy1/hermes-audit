"""Audit event schema + SQLite DDL + migrations for hermes-audit.

Own file at $HERMES_HOME/audit.db (separate from Hermes state.db), WAL mode.
Single writer (the journal's writer thread); readers use separate connections.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

SCHEMA_VERSION = "0.3.0"

DDL = """
CREATE TABLE IF NOT EXISTS audit_events (
  event_id TEXT PRIMARY KEY,
  schema_version TEXT NOT NULL,
  ts_utc TEXT NOT NULL,
  seq INTEGER NOT NULL,
  session_id TEXT, conversation_id TEXT, trace_id TEXT, parent_event_id TEXT,
  actor TEXT NOT NULL,
  action_type TEXT NOT NULL,
  tool_name TEXT,
  side_effect_class TEXT,
  outcome TEXT NOT NULL,
  duration_ms INTEGER,
  detail_json TEXT,
  provenance_json TEXT,
  human_summary TEXT,
  -- P5-A hash chain: prev_hash = prior row's event_hash ("" for the first
  -- row); event_hash = sha256 over this row's INSERT-time fields + prev_hash.
  -- Written by the single writer thread at INSERT time; never updated after.
  prev_hash TEXT,
  event_hash TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events(ts_utc);
CREATE INDEX IF NOT EXISTS idx_audit_trace ON audit_events(trace_id);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_events(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_audit_pending ON audit_events(outcome) WHERE outcome='pending';

CREATE TABLE IF NOT EXISTS event_links (
  event_id TEXT NOT NULL, parent_event_id TEXT NOT NULL,
  PRIMARY KEY (event_id, parent_event_id)
);

CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY, applied_at TEXT DEFAULT (datetime('now'))
);
"""

# Action types
TOOL_CALL = "tool_call"
LLM_CALL = "llm_call"
SKILL_WRITE = "skill_write"
APPROVAL_REQUEST = "approval_request"
APPROVAL_GRANTED = "approval_granted"
MESSAGE = "message"
ERROR = "error"
SESSION_START = "session_start"
SESSION_END = "session_end"

# Outcomes
PENDING = "pending"
SUCCESS = "success"
FAILED = "error"
# Terminal recovery state: P4.5 — rows still 'pending' at journal startup were
# written by a process that is no longer running (crash/SIGKILL), so they are
# closed out as 'interrupted' on open. Distinct from a live PENDING row.
INTERRUPTED = "interrupted"


@dataclass
class AuditEvent:
    actor: str
    action_type: str
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    schema_version: str = SCHEMA_VERSION
    ts_utc: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    seq: int = 0
    session_id: Optional[str] = None
    conversation_id: Optional[str] = None
    trace_id: Optional[str] = None
    parent_event_id: Optional[str] = None
    tool_name: Optional[str] = None
    side_effect_class: Optional[str] = None
    outcome: str = PENDING
    duration_ms: Optional[int] = None
    detail_json: Optional[str] = None
    provenance_json: Optional[str] = None
    human_summary: Optional[str] = None
    # P5-A hash chain: set by the journal writer at INSERT time, never changed
    # afterwards. Not part of AuditEvent construction by callers.
    prev_hash: Optional[str] = None
    event_hash: Optional[str] = None

    def to_row(self) -> dict:
        return asdict(self)
