"""Read-only query API over the hermes-audit SQLite database.

audit.db lives at $HERMES_HOME/audit.db in WAL mode with a single writer
(the journal's writer thread). AuditQuery never writes: each public call
opens its own short-lived read-only connection so long-lived readers can
never pin the WAL or block the writer.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from typing import Any, List, Optional

# Day-scoped grouped() window: the default number of distinct journal days
# returned per page, and the per-day event cap that bounds the query so one
# very busy day cannot make it unbounded (replaces the old flat
# 'last 300 events' window that hid entire past days).
_GROUPED_DEFAULT_DAYS = 3
_GROUPED_PER_DAY_CAP = 2000
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_COLUMNS = (
    "event_id, schema_version, ts_utc, seq, session_id, conversation_id, "
    "trace_id, parent_event_id, actor, action_type, tool_name, "
    "side_effect_class, outcome, duration_ms, detail_json, provenance_json, "
    "human_summary, created_at"
)

# Outcome -> display suffix appended to human_summary for the activity feed.
# Deterministic, data-derived, zero LLM: the journal writes the base summary
# at begin() time (outcome unknown); the read layer appends the terminal state.
_OUTCOME_SUFFIX = {
    "error": " (failed)",
    "pending": " (running)",
    "interrupted": " (interrupted)",
}


class AuditQuery:
    """Read API over audit.db. All row values are returned as plain dicts."""

    def __init__(self, db_path: str):
        self.db_path = str(db_path)

    # ------------------------------------------------------------------
    # connection helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open a fresh read-only connection. Returns [] results via callers
        when the file does not exist."""
        uri = "file:{}?mode=ro".format(
            self.db_path.replace("?", "%3f").replace("#", "%23")
        )
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        except sqlite3.OperationalError:
            # Read-only URI open can fail on odd filesystems; fall back to a
            # normal connection (still never writes from this module).
            conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        # Reader-only connection; queries never mutate, but make that a hard
        # guarantee at the SQLite level as well.
        try:
            conn.execute("PRAGMA query_only = ON")
        except sqlite3.Error:
            pass
        return conn

    def _db_exists(self) -> bool:
        return os.path.exists(self.db_path)

    @staticmethod
    def _rows_to_dicts(rows: List[sqlite3.Row]) -> List[dict]:
        out = []
        for r in rows:
            d = dict(r)
            # Derived display field: base human_summary + outcome suffix.
            # UIs render `display_summary`; raw `human_summary` stays untouched.
            base = d.get("human_summary")
            if base:
                d["display_summary"] = base + _OUTCOME_SUFFIX.get(d.get("outcome", ""), "")
            else:
                d["display_summary"] = None
            out.append(d)
        return out

    def _select(
        self,
        where: str = "",
        params: tuple = (),
        order_by: str = "ts_utc DESC, seq DESC",
        limit: Optional[int] = None,
    ) -> List[dict]:
        """Run one read-only SELECT; empty list when db is missing/unreadable."""
        if not self._db_exists():
            return []
        sql = "SELECT {} FROM audit_events".format(_COLUMNS)
        if where:
            sql += " WHERE " + where
        if order_by:
            sql += " ORDER BY " + order_by
        if limit is not None:
            sql += " LIMIT ?"
            params = tuple(params) + (limit,)
        try:
            conn = self._connect()
        except sqlite3.Error:
            return []
        try:
            cur = conn.execute(sql, params)
            return self._rows_to_dicts(cur.fetchall())
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # public read API
    # ------------------------------------------------------------------

    def recent(
        self,
        limit: int = 50,
        before: Optional[str] = None,
        day: Optional[str] = None,
        model: Optional[str] = None,
    ) -> List[dict]:
        """Newest-first page of events.

        ``before`` is a ts_utc cursor for paging (exclusive upper bound).
        ``day`` is an optional YYYY-MM-DD filter (matches ts_utc prefix).
        ``model`` is an optional LLM-model filter (llm_call rows only —
        see LIMITATION below).

        Model-filter LIMITATION: only llm_call rows are matched (their
        tool_name column carries the model name). Tool_call rows are NOT
        resolved to the model that decided them — that would require a
        trace join (tool_calls whose trace_id has an llm_call with that
        model) which is deliberately not done here to keep the read path
        simple; use by_trace()/grouped() to see a call's surrounding tools.
        """
        clauses: List[str] = []
        params: List[str] = []
        if before:
            clauses.append("ts_utc < ?")
            params.append(before)
        if day:
            clauses.append("ts_utc LIKE ?")
            params.append(day + "%")
        if model:
            clauses.append("(action_type = 'llm_call' AND tool_name = ?)")
            params.append(model)
        if clauses:
            return self._select(
                where=" AND ".join(clauses),
                params=tuple(params),
                limit=limit,
            )
        return self._select(limit=limit)

    def facets(self) -> dict:
        """Distinct filter-picker values: days present in the journal and
        llm_call models with counts.

        Returns {'days': ['YYYY-MM-DD', ...] (newest first, derived from
        substr(ts_utc,1,10)), 'models': [{'model': name, 'count': n}, ...]}
        (models = tool_name on action_type='llm_call' rows, most-used
        first). Cheap GROUP BY/DISTINCT aggregates, read-only; empty lists
        when the db is missing or unreadable.
        """
        days: List[str] = []
        models: List[dict] = []
        if not self._db_exists():
            return {"days": days, "models": models}
        conn = self._connect()
        try:
            days = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT substr(ts_utc, 1, 10) AS day "
                    "FROM audit_events "
                    "WHERE ts_utc IS NOT NULL "
                    "ORDER BY day DESC"
                )
            ]
            models = [
                {"model": r[0], "count": int(r[1])}
                for r in conn.execute(
                    "SELECT tool_name, COUNT(*) AS c FROM audit_events "
                    "WHERE action_type = 'llm_call' AND tool_name IS NOT NULL "
                    "GROUP BY tool_name ORDER BY c DESC, tool_name ASC"
                )
            ]
        except sqlite3.Error:
            days, models = [], []
        finally:
            conn.close()
        return {"days": days, "models": models}

    def by_trace(self, trace_id: str) -> List[dict]:
        """Full action tree for one user request, ordered by ts_utc, seq."""
        return self._select(
            where="trace_id = ?",
            params=(trace_id,),
            order_by="ts_utc ASC, seq ASC",
        )

    def by_actor(self, actor: str, limit: int = 100) -> List[dict]:
        """Events issued by an actor (newest first)."""
        return self._select(where="actor = ?", params=(actor,), limit=limit)

    def by_source(self, uri: str) -> List[dict]:
        """Events whose provenance_json mentions *uri* (substring match)."""
        return self._select(
            where="provenance_json LIKE ? ESCAPE '\\'",
            params=("%" + _escape_like(uri) + "%",),
            order_by="ts_utc DESC, seq DESC",
        )

    def pending(self) -> List[dict]:
        """LIVE pending actions only: rows still outcome='pending'.

        P4.5: rows left 'pending' by a prior (crashed) process are marked
        'interrupted' at journal startup, so 'pending' here means genuinely
        still-running. Interrupted rows are terminal — use interrupted().
        """
        return self._select(
            where="outcome = 'pending'",
            order_by="ts_utc DESC, seq DESC",
        )

    def interrupted(self) -> List[dict]:
        """Interrupted actions: rows outcome='interrupted' (terminal).

        These were 'pending' when a prior process ended without completing
        them (crash/SIGKILL); the next journal open marked them interrupted.
        """
        return self._select(
            where="outcome = 'interrupted'",
            order_by="ts_utc DESC, seq DESC",
        )

    def coverage(self, session_id: str) -> dict:
        """P4 coverage reconciliation for one session.

        HEURISTIC, NOT A PROOF: a session with tool_calls but zero llm_calls,
        or tool_calls with no preceding llm_call in the same trace (events
        ordered by seq), looks like actions escaped journaling — every tool
        execution should follow a model decision (llm_call) in the same
        trace. This can false-positive legitimately (llm_call journaling
        itself has gaps, subagent tool_calls traceless from the model, seq
        reuse across journal restarts), so 'suspicious' flags review, not
        proven tampering.

        Returns: {'session_id', 'counts': {action_type: n}, 'tool_calls',
        'llm_calls', 'tool_calls_without_preceding_llm_call', 'suspicious'}.
        """
        rows = self._select(
            where="session_id = ?",
            params=(session_id,),
            order_by="ts_utc ASC, seq ASC",
        )
        counts: dict = {}
        for ev in rows:
            at = ev.get("action_type") or "unknown"
            counts[at] = counts.get(at, 0) + 1

        # Per-trace walk: a tool_call counts as uncovered when no llm_call
        # has been seen earlier in the same trace (trace-NULL tool_calls can
        # never be tied to a model decision, so any of them is uncovered).
        seen_llm: set = set()
        uncovered = 0
        for ev in rows:
            trace = ev.get("trace_id")
            at = ev.get("action_type")
            if at == "llm_call":
                seen_llm.add(trace)
            elif at == "tool_call" and trace not in seen_llm:
                uncovered += 1

        tool_calls = counts.get("tool_call", 0)
        llm_calls = counts.get("llm_call", 0)
        return {
            "session_id": session_id,
            "counts": counts,
            "tool_calls": tool_calls,
            "llm_calls": llm_calls,
            "tool_calls_without_preceding_llm_call": uncovered,
            "suspicious": tool_calls > 0 and llm_calls == 0,
        }

    # ------------------------------------------------------------------
    # retention (P5-B) — the ONE deliberate write path in this module
    # ------------------------------------------------------------------

    def purge_older_than(self, days: int, dry_run: bool = True) -> dict:
        """Retention purge: remove events with created_at older than
        ``days`` days. GDPR storage-limitation: the audit trail should not
        accumulate forever (architecture doc: 12-24 months user-visible).

        dry_run=True (the SAFE default) only counts what *would* be deleted
        and returns {'would_delete': n, 'cutoff_ts': iso} — no mutation.
        dry_run=False deletes and returns {'deleted': n, 'cutoff_ts': iso}.

        WRITE PATH NOTE: every other method here opens mode=ro/query_only
        read-only connections. Purge is the single deliberate exception —
        it must DELETE rows, so it opens its own normal (writable)
        connection below, clearly marked. Everything else stays read-only.
        """
        try:
            days = int(days)
        except (TypeError, ValueError):
            raise ValueError("days must be an integer")
        if days < 0:
            raise ValueError("days must be >= 0")
        # Cutoff in the same 'YYYY-MM-DD HH:MM:SS' UTC string format the
        # created_at column uses (DEFAULT datetime('now')). Lexicographic
        # string comparison against that format is correct ordering.
        cutoff_ts = _utcnow_minus_days(days)

        where = "created_at < ?"
        params = (cutoff_ts,)

        if not self._db_exists():
            cutoff_iso = _iso_utc(cutoff_ts)
            return ({"would_delete": 0, "cutoff_ts": cutoff_iso}
                    if dry_run else {"deleted": 0, "cutoff_ts": cutoff_iso})

        if dry_run:
            # Preview only: reuse the read-only connection.
            rows = self._select(where=where, params=params)
            return {"would_delete": len(rows), "cutoff_ts": _iso_utc(cutoff_ts)}

        # ------------------------------------------------------------------
        # DELIBERATE SINGLE WRITE PATH in this otherwise read-only module.
        # The DELETE needs a writable connection: mode=ro/query_only would
        # raise. Opened fresh, used once, closed — never reused or cached.
        # ------------------------------------------------------------------
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            cur = conn.execute(
                "DELETE FROM audit_events WHERE " + where, params
            )
            deleted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {"deleted": deleted, "cutoff_ts": _iso_utc(cutoff_ts)}

    # ------------------------------------------------------------------
    # export
    # ------------------------------------------------------------------

    def export(self, path: str, since: Optional[str] = None) -> str:
        """Dump matching rows as a JSON list to ``path``; returns path.

        Creates the destination file (and parent dirs) even when the db is
        missing or empty. ``since`` is an optional ts_utc lower bound
        (inclusive).
        """
        if since:
            rows = self._select(
                where="ts_utc >= ?",
                params=(since,),
                order_by="ts_utc ASC, seq ASC",
            )
        else:
            rows = self._select(order_by="ts_utc ASC, seq ASC")

        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2, default=str)
            fh.write("\n")
        return path


    # ------------------------------------------------------------------
    # grouped (request groups)
    # ------------------------------------------------------------------

    def grouped(
        self,
        limit_groups: int = 20,
        before_day: Optional[str] = None,
        days: int = _GROUPED_DEFAULT_DAYS,
    ) -> dict:
        """Day-scoped recent events organized into per-turn request groups.

        Returns groups for the ``days`` most recent distinct journal days
        (default 3). When ``before_day`` (YYYY-MM-DD) is given, returns the
        ``days`` distinct journal days STRICTLY BEFORE that day instead —
        the paging cursor for older history. Each day contributes at most
        ``_GROUPED_PER_DAY_CAP`` events (newest first within the day), so a
        single very busy day cannot make the query unbounded; this replaces
        the old flat 'last 300 events' window that hid entire past days.

        Events are grouped by trace_id (NULL-trace events each become their
        own singleton group keyed by event_id) and groups are ordered
        newest-first by their latest event's ts_utc. Events within a group
        are ordered seq ascending.

        Each group dict: trace_id, start_ts, end_ts, event_count,
        tool_call_count, llm_call_count, total_duration_ms, outcome
        ('error' > 'pending' > 'success' rollup), title (first tool_call's
        human_summary, else first event's human_summary, else 'Turn'),
        and an 'events' list (display_summary derived per _rows_to_dicts).

        The result is a dict (not a bare list) so REST and Python callers
        agree: {'groups': [...], 'has_more': bool, 'oldest_day': str|None}
        where ``oldest_day`` is the oldest journal day present in the
        returned window (the next page's before_day) and ``has_more`` is
        True when journal days strictly older than it still exist.

        Raises ValueError on a malformed before_day (must be YYYY-MM-DD).
        """
        if before_day is not None and not _DAY_RE.match(before_day):
            raise ValueError("before_day must be YYYY-MM-DD")
        try:
            days = max(int(days), 1)
        except (TypeError, ValueError):
            days = _GROUPED_DEFAULT_DAYS

        all_events: List[dict] = []
        oldest_day: Optional[str] = None
        has_more = False
        if self._db_exists():
            conn = self._connect()
            try:
                # 1. Pick the window: the ``days`` most recent distinct
                #    journal days, or the ``days`` most recent ones strictly
                #    BEFORE before_day when paging (bound params only).
                day_rows = conn.execute(
                    "SELECT DISTINCT substr(ts_utc, 1, 10) AS day "
                    "FROM audit_events "
                    "WHERE ts_utc IS NOT NULL "
                    "AND (? IS NULL OR substr(ts_utc, 1, 10) < ?) "
                    "ORDER BY day DESC LIMIT ?",
                    (before_day, before_day, days),
                ).fetchall()
                wanted = [r[0] for r in day_rows]
                if wanted:
                    # 2. Pull events per day, capped per day so one busy day
                    #    bounds cost (newest-first within each day). A plain
                    #    per-day loop — per-component ORDER BY/LIMIT is not
                    #    allowed in a SQLite compound SELECT, so no UNION.
                    for d in wanted:
                        rows = conn.execute(
                            "SELECT event_id, schema_version, ts_utc, seq, session_id, "
                            "conversation_id, trace_id, parent_event_id, actor, "
                            "action_type, tool_name, side_effect_class, outcome, "
                            "duration_ms, detail_json, provenance_json, human_summary "
                            "FROM audit_events "
                            "WHERE ts_utc >= ? AND ts_utc < ? "
                            "ORDER BY ts_utc DESC, seq DESC LIMIT ?",
                            (d + "T00:00:00", d + "T24:00:00", _GROUPED_PER_DAY_CAP),
                        ).fetchall()
                        all_events.extend(self._rows_to_dicts(rows))
                    oldest_day = wanted[-1]
                    # 3. has_more: do strictly-older journal days exist
                    #    beyond the oldest day we just returned?
                    more = conn.execute(
                        "SELECT 1 FROM audit_events "
                        "WHERE ts_utc IS NOT NULL AND substr(ts_utc, 1, 10) < ? LIMIT 1",
                        (oldest_day,),
                    ).fetchone()
                    has_more = more is not None
            except sqlite3.Error:
                all_events, oldest_day, has_more = [], None, False
            finally:
                conn.close()

        groups: dict = {}
        for ev in all_events:
            key = ev.get("trace_id") or "event:" + str(ev.get("event_id"))
            groups.setdefault(key, []).append(ev)

        built = [self._build_group(evs) for evs in groups.values()]

        # Newest group first: by latest event ts_utc desc, then that event's
        # seq desc as a deterministic tie-break.
        built.sort(key=lambda g: (g["end_ts"], g["events"][-1]["seq"]), reverse=True)

        # Day-scoping is the primary window: every group in the selected days
        # is returned (cost is already bounded by the per-day event cap).
        # limit_groups is only a safety ceiling so a pathological multi-day
        # window cannot return an unbounded number of groups; it must NOT cut
        # off older days' groups while newer days fill the budget, which was
        # the original "past days hidden" bug in a new form.
        ceiling = max(int(limit_groups), 0)
        if ceiling and len(built) > ceiling:
            built = built[:ceiling]
        return {
            "groups": built,
            "has_more": has_more,
            "oldest_day": oldest_day,
        }

    @staticmethod
    def _build_group(evs: List[dict]) -> dict:
        evs = sorted(evs, key=lambda e: (e.get("seq") or 0))
        trace_id = evs[0].get("trace_id")
        if trace_id is None:
            # Singleton group keyed by event_id for legacy/untraced events.
            trace_id = "event:" + str(evs[0].get("event_id"))
        outcomes = [e.get("outcome") for e in evs]
        if "error" in outcomes:
            outcome = "error"
        elif "pending" in outcomes:
            outcome = "pending"
        else:
            outcome = "success"

        first_tool = next(
            (e for e in evs if e.get("action_type") == "tool_call"), None
        )
        title = (
            (first_tool or {}).get("human_summary")
            or (first_tool or {}).get("display_summary")
            or evs[0].get("human_summary")
            or evs[0].get("display_summary")
            or "Turn"
        )
        return {
            "trace_id": trace_id,
            "start_ts": evs[0].get("ts_utc"),
            "end_ts": evs[-1].get("ts_utc"),
            "event_count": len(evs),
            "tool_call_count": sum(
                1 for e in evs if e.get("action_type") == "tool_call"
            ),
            "llm_call_count": sum(
                1 for e in evs if e.get("action_type") == "llm_call"
            ),
            "total_duration_ms": sum(int(e.get("duration_ms") or 0) for e in evs),
            "outcome": outcome,
            "title": title,
            "events": evs,
        }


def _utcnow_minus_days(days: int) -> str:
    """Cutoff timestamp as a 'YYYY-MM-DD HH:MM:SS' UTC string — the exact
    format of the created_at column (DEFAULT datetime('now')), so a plain
    lexicographic WHERE comparison orders correctly."""
    import datetime as _dt

    dt = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _iso_utc(cutoff_ts: str) -> str:
    """Render the 'YYYY-MM-DD HH:MM:SS' cutoff as an ISO-8601 UTC instant
    for the API result (cutoff_ts field)."""
    import datetime as _dt

    dt = _dt.datetime.strptime(cutoff_ts, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=_dt.timezone.utc
    )
    return dt.isoformat().replace("+00:00", "Z")


def _escape_like(value: str) -> str:
    """Escape SQL LIKE wildcards so a URI is matched literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


__all__ = ["AuditQuery"]
