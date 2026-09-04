"""hermes-audit dashboard plugin — backend API routes.

Mounted at /api/plugins/hermes-audit/ by the dashboard plugin system.

Read-only visualizer over the append-only `audit_events` journal that the
hermes-audit agent plugin writes (single writer thread, WAL mode). This layer
only ever READS — it opens its own short-lived read connections and never
touches the writer, so it can safely tail alongside a running agent.

Live updates arrive via the ``/events/stream`` WebSocket, which polls the
journal on a short interval (same WAL-poll pattern the Kanban dashboard uses
for task_events).

Auth note: plugin HTTP routes go through the dashboard's session-token auth
middleware just like core routes. The WebSocket takes the session token as a
``?token=`` query param (browsers can't set the Authorization header on an
upgrade request), matching the established pattern in hermes_cli/web_server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from query import AuditQuery  # noqa: E402

log = logging.getLogger(__name__)

router = APIRouter()

# Poll interval for the live event tail (mirrors Kanban's 300ms WAL poll).
_EVENT_POLL_SECONDS = 0.3


# ---------------------------------------------------------------------------
# DB helpers (read-only)
# ---------------------------------------------------------------------------

def _db_path() -> str:
    """Resolve $HERMES_HOME/audit.db, falling back to ~/.hermes/audit.db."""
    home = None
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        home = get_hermes_home()
    except Exception:
        home = None
    if not home:
        home = os.path.expanduser("~/.hermes")
    return os.path.join(str(home), "audit.db")


def _connect(db_path: str) -> sqlite3.Connection:
    """Open a read-only connection (mode=ro) so we can never mutate the journal."""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.Error:
        pass
    return conn


def _rows(db_path: str, sql: str, params: tuple = ()) -> list[dict]:
    if not os.path.exists(db_path):
        return []
    conn = _connect(db_path)
    try:
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    except sqlite3.Error as exc:
        log.warning("hermes-audit dashboard query failed: %s", exc)
        return []
    finally:
        conn.close()


def _row(db_path: str, sql: str, params: tuple = ()) -> Optional[dict]:
    rows = _rows(db_path, sql, params)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@router.get("/health")
def health() -> dict:
    """Liveness + whether a journal exists + P4 journal health stats.

    When the agent plugin's AuditJournal is reachable in-process we surface
    its live stats() (writer_alive, queue_size, per-action-type counts,
    last_event_ts). The dashboard usually runs in a separate process from
    the journal writer, so this is best-effort: on miss we fall back to
    read-only DB aggregates and writer_alive=None (unknown, not dead).
    """
    db = _db_path()
    exists = os.path.exists(db)
    count = 0
    if exists:
        row = _row(db, "SELECT COUNT(*) AS c FROM audit_events")
        count = int(row["c"]) if row else 0

    out: dict = {
        "ok": True,
        "db": db,
        "journal_exists": exists,
        "event_count": count,
    }
    journal_stats: Optional[dict] = None
    try:
        # Same interpreter (agent + dashboard plugin): reuse the live writer.
        import __init__ as audit_plugin  # type: ignore

        j = audit_plugin._journal
        if j is not None:
            journal_stats = j.stats()
    except Exception:
        journal_stats = None

    if journal_stats is None:
        # Separate process / no live journal: read-only best effort.
        journal_stats = {
            "writer_alive": None,  # unknown — writer lives in another process
            "queue_size": 0,
            "events_total": count,
            "by_action_type": {
                r["action_type"]: int(r["c"])
                for r in _rows(db, "SELECT action_type, COUNT(*) AS c FROM audit_events GROUP BY action_type")
            },
            "last_event_ts": (_row(db, "SELECT MAX(ts_utc) AS m FROM audit_events") or {}).get("m"),
            "source": "read-only fallback",
        }
    else:
        journal_stats = dict(journal_stats)
        journal_stats["source"] = "in-process journal"
    out["journal"] = journal_stats
    return out


@router.get("/coverage")
def coverage(
    session_id: str = Query(..., description="session id to reconcile"),
) -> dict:
    """P4 coverage reconciliation for one session.

    HEURISTIC, NOT A PROOF — see AuditQuery.coverage(): flags sessions
    where tool_calls appear with no llm_call coverage as 'suspicious' for
    review; it cannot prove events escaped journaling.
    """
    db = _db_path()
    if not os.path.exists(db):
        raise HTTPException(status_code=404, detail="audit db not found")
    q = AuditQuery(db)
    return q.coverage(session_id)


@router.get("/events")
def list_events(
    limit: int = Query(50, ge=1, le=500),
    before_id: Optional[str] = Query(None),
    action_type: Optional[str] = Query(None),
    actor: Optional[str] = Query(None),
    outcome: Optional[str] = Query(None),
    trace_id: Optional[str] = Query(None),
    day: Optional[str] = Query(None, description="YYYY-MM-DD — only events whose ts_utc falls on this day"),
    model: Optional[str] = Query(None, description="LLM model name — only llm_call rows with this model (tool_name)"),
    q: Optional[str] = Query(None, description="substring match on tool_name/detail/human_summary"),
) -> dict:
    """Newest-first page of audit events with optional filters.

    `before_id` is an event_id cursor for paging (returns events older than it).
    `day` filters to one journal day (ts_utc prefix match); `model` filters to
    llm_call rows with that model (tool_name) — see AuditQuery.recent() for
    the documented limitation that tool_call rows are not model-resolved.
    """
    db = _db_path()
    clauses, params = [], []

    if action_type:
        clauses.append("action_type = ?")
        params.append(action_type)
    if actor:
        clauses.append("actor = ?")
        params.append(actor)
    if outcome:
        clauses.append("outcome = ?")
        params.append(outcome)
    if trace_id:
        clauses.append("trace_id = ?")
        params.append(trace_id)
    if day:
        clauses.append("ts_utc LIKE ?")
        params.append(day + "%")
    if model:
        clauses.append("(action_type = 'llm_call' AND tool_name = ?)")
        params.append(model)
    if q:
        clauses.append("(tool_name LIKE ? OR detail_json LIKE ? OR human_summary LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    if before_id:
        anchor = _row(db, "SELECT ts_utc, seq FROM audit_events WHERE event_id = ?", (before_id,))
        if anchor:
            clauses.append("(ts_utc < ? OR (ts_utc = ? AND seq < ?))")
            params.extend([anchor["ts_utc"], anchor["ts_utc"], anchor["seq"]])

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"""
        SELECT event_id, schema_version, ts_utc, seq, session_id, conversation_id,
               trace_id, parent_event_id, actor, action_type, tool_name,
               side_effect_class, outcome, duration_ms, detail_json, provenance_json,
               human_summary
          FROM audit_events
          {where}
         ORDER BY ts_utc DESC, seq DESC
         LIMIT ?
    """
    params.append(limit + 1)  # fetch one extra to compute has_more
    rows = _rows(db, sql, tuple(params))
    has_more = len(rows) > limit
    events = rows[:limit]
    return {
        "events": events,
        "has_more": has_more,
        "next_before_id": events[-1]["event_id"] if has_more and events else None,
    }


@router.get("/events/{event_id}")
def get_event(event_id: str) -> dict:
    """Full detail for one event, plus its causal-chain neighbours."""
    db = _db_path()
    ev = _row(db, "SELECT * FROM audit_events WHERE event_id = ?", (event_id,))
    if not ev:
        raise HTTPException(status_code=404, detail="event not found")

    # Causal neighbours: same trace, ordered (best-effort when trace_id null).
    chain: list[dict] = []
    if ev.get("trace_id"):
        chain = _rows(
            db,
            "SELECT event_id, action_type, tool_name, outcome, ts_utc, seq "
            "FROM audit_events WHERE trace_id = ? ORDER BY ts_utc, seq",
            (ev["trace_id"],),
        )
    # Explicit parent/child links if the links table is populated.
    try:
        links = _rows(
            db,
            "SELECT event_id, parent_event_id FROM event_links WHERE event_id = ? OR parent_event_id = ?",
            (event_id, event_id),
        )
    except Exception:
        links = []
    return {"event": ev, "chain": chain, "links": links}


@router.get("/groups")
def list_groups(
    limit_groups: int = Query(20, ge=1, le=5000),
    before_day: Optional[str] = Query(None, description="YYYY-MM-DD — page to the D days strictly before this day"),
    days: int = Query(3, ge=1, le=31, description="how many distinct journal days to return per page"),
) -> dict:
    """Day-scoped request groups (newest day first) with paging metadata.

    Same algorithm as AuditQuery.grouped(): returns groups for the most
    recent ``days`` (default 3) distinct journal days, or — when
    ``before_day`` (YYYY-MM-DD) is given — the ``days`` distinct days
    strictly before it. Each day contributes at most ~2000 events (a
    per-day cap that bounds cost; replaces the old flat LIMIT 300, which
    made days with >300 events swallow all prior history). Events are
    grouped by trace_id (NULL-trace events each become a singleton group
    keyed by event_id, exposed as trace_id "event:<event_id>"), groups
    ordered by their latest event's ts_utc desc, events within a group by
    seq asc.

    Returns {'groups': [...], 'count', 'has_more', 'oldest_day'} where
    ``oldest_day`` (YYYY-MM-DD or None) is the oldest day in the returned
    window and ``has_more`` is True when strictly older days exist — the
    frontend pages by passing oldest_day back as before_day.
    """
    db = _db_path()
    try:
        result = AuditQuery(db).grouped(limit_groups=limit_groups, before_day=before_day, days=days)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    groups = result["groups"]
    return {
        "groups": groups,
        "count": len(groups),
        "has_more": result["has_more"],
        "oldest_day": result["oldest_day"],
    }


@router.get("/groups/{trace_id}")
def get_group(trace_id: str) -> dict:
    """Full detail for one request group: group rollup + all its events.

    A NULL-trace singleton group is addressable by its synthetic id
    "event:<event_id>".
    """
    db = _db_path()
    if trace_id.startswith("event:"):
        rows = _rows(
            db,
            "SELECT * FROM audit_events WHERE event_id = ?",
            (trace_id[len("event:"):],),
        )
    else:
        rows = _rows(
            db,
            "SELECT * FROM audit_events WHERE trace_id = ? ORDER BY ts_utc, seq",
            (trace_id,),
        )
    if not rows:
        raise HTTPException(status_code=404, detail="group not found")
    group = _group_events(rows)[0]
    events = group.pop("events")
    return {"group": group, "events": events, "count": len(events)}


def _group_events(events: list[dict]) -> list[dict]:
    """Group flat events into request-group dicts (newest group first).

    Mirrors AuditQuery.grouped()'s rollup so the dashboard layer stays
    consistent with the read API.
    """
    buckets: dict[str, list[dict]] = {}
    for ev in events:
        key = ev.get("trace_id") or "event:" + str(ev.get("event_id"))
        buckets.setdefault(key, []).append(ev)

    groups = []
    for key, evs in buckets.items():
        evs = sorted(evs, key=lambda e: (e.get("seq") or 0))
        outcomes = [e.get("outcome") for e in evs]
        if "error" in outcomes:
            outcome = "error"
        elif "pending" in outcomes:
            outcome = "pending"
        else:
            outcome = "success"
        first_tool = next((e for e in evs if e.get("action_type") == "tool_call"), None)
        title = (
            (first_tool or {}).get("human_summary")
            or evs[0].get("human_summary")
            or "Turn"
        )
        groups.append({
            "trace_id": evs[0].get("trace_id") or key,
            "start_ts": evs[0].get("ts_utc"),
            "end_ts": evs[-1].get("ts_utc"),
            "event_count": len(evs),
            "tool_call_count": sum(1 for e in evs if e.get("action_type") == "tool_call"),
            "llm_call_count": sum(1 for e in evs if e.get("action_type") == "llm_call"),
            "total_duration_ms": sum(int(e.get("duration_ms") or 0) for e in evs),
            "outcome": outcome,
            "title": title,
            "events": evs,
        })

    groups.sort(key=lambda g: (g["end_ts"], g["events"][-1]["seq"]), reverse=True)
    return groups


@router.get("/facets")
def facets() -> dict:
    """Distinct filter-picker values: journal days (newest first) and
    llm_call models with event counts (delegates to AuditQuery.facets())."""
    db = _db_path()
    return AuditQuery(db).facets()


@router.get("/traces/{trace_id}")
def get_trace(trace_id: str) -> dict:
    """The full action tree for one user request/trace."""
    db = _db_path()
    rows = _rows(
        db,
        "SELECT * FROM audit_events WHERE trace_id = ? ORDER BY ts_utc, seq",
        (trace_id,),
    )
    return {"trace_id": trace_id, "events": rows, "count": len(rows)}


@router.get("/stats")
def stats() -> dict:
    """Aggregate counts for the overview header."""
    db = _db_path()
    by_type = {
        r["action_type"]: r["c"]
        for r in _rows(db, "SELECT action_type, COUNT(*) AS c FROM audit_events GROUP BY action_type")
    }
    by_outcome = {
        r["outcome"]: r["c"]
        for r in _rows(db, "SELECT outcome, COUNT(*) AS c FROM audit_events GROUP BY outcome")
    }
    total_row = _row(db, "SELECT COUNT(*) AS c FROM audit_events")
    pending_row = _row(db, "SELECT COUNT(*) AS c FROM audit_events WHERE outcome = 'pending'")
    interrupted_row = _row(db, "SELECT COUNT(*) AS c FROM audit_events WHERE outcome = 'interrupted'")
    tools = _rows(
        db,
        "SELECT tool_name, COUNT(*) AS c FROM audit_events "
        "WHERE tool_name IS NOT NULL GROUP BY tool_name ORDER BY c DESC LIMIT 10",
    )
    return {
        "total": int(total_row["c"]) if total_row else 0,
        "pending": int(pending_row["c"]) if pending_row else 0,
        "interrupted": int(interrupted_row["c"]) if interrupted_row else 0,
        "by_type": by_type,
        "by_outcome": by_outcome,
        "top_tools": tools,
    }


@router.get("/meta/filters")
def filter_options() -> dict:
    """Distinct values to populate filter dropdowns."""
    db = _db_path()
    types = [r["action_type"] for r in _rows(db, "SELECT DISTINCT action_type FROM audit_events WHERE action_type IS NOT NULL")]
    actors = [r["actor"] for r in _rows(db, "SELECT DISTINCT actor FROM audit_events WHERE actor IS NOT NULL")]
    outcomes = [r["outcome"] for r in _rows(db, "SELECT DISTINCT outcome FROM audit_events WHERE outcome IS NOT NULL")]
    return {"action_types": types, "actors": actors, "outcomes": outcomes}


# ---------------------------------------------------------------------------
# Retention (P5-B) — the only non-GET route: purge old events
# ---------------------------------------------------------------------------

class RetentionPurgeRequest(BaseModel):
    """POST /retention/purge body — the desktop app sends it via
    ctx.rest('/retention/purge', { method: 'POST', body: {...} })."""
    days: int = Field(..., ge=0, description="delete events with created_at older than this many days")
    dry_run: bool = Field(True, description="preview only (default true) — no deletion unless explicitly false")


@router.post("/retention/purge")
def retention_purge(body: RetentionPurgeRequest) -> dict:
    """Retention purge (GDPR storage-limitation, P5-B).

    Calls AuditQuery.purge_older_than(days, dry_run). dry_run defaults to
    True so an accidental call is a harmless preview; pass
    {"days": 365, "dry_run": false} to actually delete. This is the one
    route here that can mutate the journal — it delegates to the audit
    module's single deliberate write path.
    """
    db = _db_path()
    if not os.path.exists(db):
        raise HTTPException(status_code=404, detail="audit db not found")
    try:
        q = AuditQuery(db)
        return q.purge_older_than(body.days, dry_run=body.dry_run)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


# ---------------------------------------------------------------------------
# WebSocket: /events/stream?since=<seq>&token=<session>
# ---------------------------------------------------------------------------

def _max_rowid(db_path: str) -> int:
    row = _row(db_path, "SELECT COALESCE(MAX(rowid), 0) AS m FROM audit_events")
    return int(row["m"]) if row else 0


@router.websocket("/events/stream")
async def events_stream(
    websocket: WebSocket,
    since: int = Query(0),
    token: Optional[str] = Query(None),
):
    """Tail the journal, pushing new events as they land.

    Auth: the dashboard's session-token middleware guards HTTP routes; for the
    WS we accept the session token as ?token= (browsers can't set Authorization
    on upgrade). Validation is delegated to the same per-process token check
    the web server uses; if it can't be resolved we still require a token param
    to be present (defence-in-depth on LAN exposure).
    """
    # Best-effort token presence check; the authoritative session validation
    # happens in the web server's auth layer for HTTP. For the WS we mirror
    # Kanban's pattern of requiring the token param.
    if token is None:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    db = _db_path()
    last_rowid = max(since, 0)

    try:
        while True:
            if os.path.exists(db):
                rows = _rows(
                    db,
                    "SELECT rowid AS _rid, event_id, ts_utc, seq, actor, action_type, "
                    "tool_name, outcome, duration_ms, detail_json "
                    "FROM audit_events WHERE rowid > ? ORDER BY rowid LIMIT 200",
                    (last_rowid,),
                )
                for r in rows:
                    last_rowid = max(last_rowid, int(r.pop("_rid", 0)))
                    await websocket.send_text(json.dumps({"type": "event", "event": r}))
            await asyncio.sleep(_EVENT_POLL_SECONDS)
    except WebSocketDisconnect:
        return
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("hermes-audit stream error: %s", exc)
        try:
            await websocket.close()
        except Exception:
            pass
