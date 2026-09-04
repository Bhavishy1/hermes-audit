"""AuditJournal — the write path for the hermes-audit plugin.

Pattern: write-ahead, async journal. Public methods (`begin`/`complete`) build
AuditEvent objects and enqueue lightweight DB ops, returning immediately so the
agent's hot path never blocks on SQLite. A single daemon writer thread drains
the queue and applies ops on one dedicated connection (WAL: one writer, many
readers), batching commits every BATCH_MAX_OPS or FLUSH_INTERVAL, whichever
comes first.

Fail-closed: if the writer thread dies, `is_healthy()` returns False and
enqueue methods raise RuntimeError — the middleware must not proceed unaudited.

Redaction: sensitive keys in `detail` payloads are masked and oversized strings
truncated before they ever hit disk.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import queue
import re
import sqlite3
import threading
import time

from schema import DDL, SCHEMA_VERSION, AuditEvent, INTERRUPTED, PENDING
logger = logging.getLogger("hermes-audit.journal")

# Batching knobs: commit after this many ops, or this many seconds, whichever first.
BATCH_MAX_OPS = 50
FLUSH_INTERVAL = 0.5  # seconds

# Retain-on-failure (P4 part 2): ops whose commit fails are held in a retry
# buffer and retried before any new ops, instead of being dropped. The buffer
# is bounded: if it would exceed this cap, the journal marks itself unhealthy
# (fail-closed) rather than grow unbounded.
RETRY_BUFFER_CAP = 500

# Queue sentinel telling the writer to finish up and exit.
_SENTINEL = object()

# Grace period when joining the writer thread on close().
JOIN_TIMEOUT = 10.0  # seconds

# Redaction denylist: any dict key containing one of these substrings
# (case-insensitive) has its value replaced with "[REDACTED]".
REDACT_KEY_DENYLIST = (
    "api_key",
    "token",
    "secret",
    "password",
    "authorization",
    "credential",
)
REDACTED = "[REDACTED]"

# Exact-name allowlist: keys that substring-match the denylist but hold only
# benign numeric metadata (e.g. LLM token COUNTS, not auth tokens). The redact
# walk checks this before the denylist so these keys keep their values.
REDACT_EXACT_ALLOWLIST = frozenset({
    "usage_tokens",  # P3.5: {prompt, completion, total, cost_usd} token counts
})

# URL query parameters whose values are always secrets (case-insensitive).
# Applied to string VALUES too (not just dict keys): authorization redirects
# like "https://...?code=SECRET&scope=..." previously stored the code verbatim.
REDACT_URL_PARAM_PATTERN = re.compile(
    r"(?i)([?&])(code|access_token|token|api_key|apikey|key|secret|password)"
    r"=([^&\s]+)"
)

# Any string value longer than this is truncated (keeps tool dumps bounded).
MAX_STRING_LEN = 2000


class AuditJournal:
    """Async, fail-closed audit event writer backed by SQLite (WAL mode).

    Usage:
        journal = AuditJournal(db_path)
        event = journal.begin("agent", TOOL_CALL, tool_name="search",
                              context={"session_id": sid, "trace_id": tid})
        try:
            ...
            journal.complete(event, SUCCESS, detail={"hits": 5})
        except Exception as exc:
            journal.complete(event, FAILED, error=str(exc))
        journal.close()
    """

    def __init__(self, db_path: str | os.PathLike):
        self._db_path = str(db_path)
        pathlib.Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        # Single writer connection; all DB writes happen on it, in the writer thread.
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(DDL)
        self._migrate()
        self._conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version) VALUES (?)",
            (SCHEMA_VERSION,),
        )
        self._conn.commit()

        self._recover_interrupted()

        # P5-A: seed the hash chain head from the last durable row BEFORE the
        # writer thread starts (single-threaded here), so a reopened journal
        # resumes the chain instead of resetting it.
        last = self._conn.execute(
            "SELECT event_hash FROM audit_events ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        self._last_hash: str = last[0] if last and last[0] else ""

        self._queue: queue.Queue = queue.Queue()
        self._seq_lock = threading.Lock()
        self._seq_counters: dict[str | None, int] = {}
        # P4 part 2: ops whose commit failed are retained here and retried
        # (before any new ops) on the next _apply_batch, instead of dropped.
        # Writer-thread-only; bounded by RETRY_BUFFER_CAP.
        self._retry_buffer: list[tuple] = []
        self._retry_lock = threading.Lock()
        self._alive = False
        self._closed = False

        self._writer = threading.Thread(
            target=self._writer_loop, name="audit-journal-writer", daemon=True
        )
        self._writer.start()

    def _recover_interrupted(self) -> None:
        """P4.5 crash recovery: close out 'pending' rows from prior processes.

        Invariant: any row still outcome='pending' at journal startup was
        written by a process that is no longer running — this process just
        started, so none of its OWN events are pending yet (begin() enqueues
        only after the writer thread exists, and it never marks rows pending
        retroactively). A pending row at open is therefore definitionally
        evidence of a crash/SIGKILL, not a live run, so it is marked
        'interrupted' — a terminal state distinct from live 'pending'.

        Runs on the writer connection BEFORE the writer thread starts, so
        there is no concurrency with the queue drain.
        """
        cur = self._conn.execute(
            "UPDATE audit_events SET outcome = ? WHERE outcome = ?",
            (INTERRUPTED, PENDING),
        )
        self._conn.commit()
        if cur.rowcount:
            logger.info(
                "interrupted-turn recovery: marked %d stale pending event(s) "
                "as '%s' (prior process did not shut down cleanly)",
                cur.rowcount,
                INTERRUPTED,
            )

    def _migrate(self) -> None:
        """Idempotent column migrations for DBs created by older schema versions.

        CREATE TABLE IF NOT EXISTS won't add columns to an existing table, so
        inspect the live schema and ALTER only what's missing. Runs before the
        writer thread starts, on the writer connection.
        """
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(audit_events)")}
        if "human_summary" not in cols:
            self._conn.execute("ALTER TABLE audit_events ADD COLUMN human_summary TEXT")
        # P5-A: add hash-chain columns to DBs created by older schema versions
        # (small loop keeps future additive migrations one-line each).
        for name, decl in (("prev_hash", "TEXT"), ("event_hash", "TEXT")):
            if name not in cols:
                self._conn.execute(
                    "ALTER TABLE audit_events ADD COLUMN %s %s" % (name, decl)
                )

    # ------------------------------------------------------------------ #
    # Public API (thread-safe; may be called from many worker threads)
    # ------------------------------------------------------------------ #

    def begin(
        self,
        actor: str,
        action_type: str,
        tool_name: str | None = None,
        context: dict | None = None,
    ) -> AuditEvent:
        """Start an audit event: enqueue the INSERT and return the event immediately.

        `context` may carry session_id / conversation_id / trace_id /
        parent_event_id / side_effect_class.
        """
        context = context or {}
        event = AuditEvent(
            actor=actor,
            action_type=action_type,
            tool_name=tool_name,
            outcome=PENDING,
            session_id=context.get("session_id"),
            conversation_id=context.get("conversation_id"),
            trace_id=context.get("trace_id"),
            parent_event_id=context.get("parent_event_id"),
            side_effect_class=context.get("side_effect_class"),
            human_summary=context.get("human_summary"),
        )
        event.seq = self._next_seq(event.session_id)

        op = ("insert", event.to_row())
        self._enqueue(op)
        return event

    def complete(
        self,
        event: AuditEvent,
        outcome: str,
        result: object = None,
        error: object = None,
        duration_ms: int | None = None,
        detail: dict | None = None,
        provenance: dict | None = None,
    ) -> None:
        """Finish an event: enqueue an UPDATE with outcome, timing and detail.

        `result` and `error` are convenience shorthands folded into the detail
        payload as "result" / "error" keys when no explicit `detail` is given.
        Redaction is applied before serialization.
        """
        payload = detail if detail is not None else {}
        if error is not None:
            payload = {**payload, "error": error}
        elif result is not None:
            payload = {**payload, "result": result}

        detail_json = self._dump_json(payload)
        provenance_json = self._dump_json(provenance)

        op = (
            "update",
            {
                "event_id": event.event_id,
                "outcome": outcome,
                "duration_ms": duration_ms,
                "detail_json": detail_json,
                "provenance_json": provenance_json,
            },
        )
        self._enqueue(op)

    def is_healthy(self) -> bool:
        """True while the writer thread is running normally."""
        return self._alive

    def stats(self) -> dict:
        """P4 coverage health snapshot. Cheap: aggregate SELECTs on a fresh
        read connection (WAL: never blocks or blocks-by the writer), so the
        writer's failure/retry path is untouched.

        Returns: writer_alive, queue_size, events_total, by_action_type,
        last_event_ts (max ts_utc, None when empty). Best-effort — a read
        failure degrades to queue/alive info rather than raising.
        """
        info: dict = {
            "writer_alive": self._alive,
            "queue_size": self._queue.qsize(),
            "events_total": 0,
            "by_action_type": {},
            "last_event_ts": None,
        }
        try:
            conn = sqlite3.connect(
                "file:{}?mode=ro".format(self._db_path.replace("?", "%3f").replace("#", "%23")),
                uri=True,
                timeout=5.0,
            )
            try:
                for row in conn.execute(
                    "SELECT action_type, COUNT(*) FROM audit_events GROUP BY action_type"
                ):
                    info["by_action_type"][row[0]] = int(row[1])
                row = conn.execute("SELECT COUNT(*), MAX(ts_utc) FROM audit_events").fetchone()
                info["events_total"] = int(row[0]) if row else 0
                info["last_event_ts"] = row[1] if row else None
            finally:
                conn.close()
        except sqlite3.Error:
            logger.warning("audit journal stats(): read-side query failed", exc_info=True)
        return info

    def flush(self) -> None:
        """Block until everything enqueued so far has been applied.

        Implemented with a queue-join barrier: the writer calls task_done()
        after each op is committed, so join() returns once the queue is empty.
        """
        self._queue.join()

    def close(self) -> None:
        """Drain the queue, stop the writer, close the connection. Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put(_SENTINEL)
            self._writer.join(timeout=JOIN_TIMEOUT)
            if self._writer.is_alive():
                logger.warning("audit writer thread did not exit within %.1fs", JOIN_TIMEOUT)
        finally:
            try:
                self._conn.commit()
                self._conn.close()
            except sqlite3.Error:
                logger.exception("error closing audit database")
            self._alive = False

    # ------------------------------------------------------------------ #
    # Hash chain (P5-A)
    # ------------------------------------------------------------------ #

    # Fields covered by the chain, in fixed order. The chain covers INSERT-time
    # state ONLY: outcome is always 'pending' here, and the later complete()
    # UPDATE (outcome/duration/detail) deliberately does NOT re-hash. The chain
    # therefore proves an event was recorded with this content at this point —
    # it is not a commitment to the event's final outcome. Ordering is
    # writer-thread insertion order (the single writer is the single source of
    # ordering), not seq.
    _HASH_FIELDS = (
        "event_id", "ts_utc", "seq", "session_id", "trace_id",
        "actor", "action_type", "tool_name", "outcome", "human_summary",
    )

    @classmethod
    def _compute_hash(cls, row: dict, prev_hash: str) -> str:
        """sha256 over the canonical serialization of row + prev_hash.

        prev_hash is empty string (""), not None, for the genesis row.
        """
        parts = ["" if row.get(f) is None else str(row.get(f)) for f in cls._HASH_FIELDS]
        parts.append(prev_hash)
        payload = "\x1f".join(parts)  # unit-separator joins; no field may contain it
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def verify_chain(self) -> dict:
        """Recompute the hash chain over every row (rowid order) and report.

        Returns {'valid', 'length', 'first_break_seq', 'reason'}. A break is a
        row whose recomputed event_hash != stored event_hash (tampered/edited
        row) or whose prev_hash != the previous row's event_hash (deletion,
        reordering, or re-inserted rows). Reads on a fresh read-only
        connection; never touches the writer.
        """
        result = {"valid": True, "length": 0, "first_break_seq": None, "reason": None}
        try:
            conn = sqlite3.connect(
                "file:{}?mode=ro".format(self._db_path.replace("?", "%3f").replace("#", "%23")),
                uri=True,
                timeout=5.0,
            )
        except sqlite3.Error:
            result["valid"] = False
            result["reason"] = "cannot open database for chain verification"
            return result
        try:
            rows = conn.execute(
                """
                SELECT event_id, ts_utc, seq, session_id, trace_id,
                       actor, action_type, tool_name, outcome,
                       human_summary, prev_hash, event_hash
                  FROM audit_events
                 ORDER BY rowid
                """
            ).fetchall()
        except sqlite3.Error:
            result["valid"] = False
            result["reason"] = "audit_events table unreadable"
            return result
        finally:
            conn.close()

        result["length"] = len(rows)
        expected_prev = ""
        for row in rows:
            (event_id, ts_utc, seq, session_id, trace_id,
             actor, action_type, tool_name, outcome,
             human_summary, prev_hash, event_hash) = row
            row_dict = {
                "event_id": event_id, "ts_utc": ts_utc, "seq": seq,
                "session_id": session_id, "trace_id": trace_id,
                "actor": actor, "action_type": action_type,
                "tool_name": tool_name, "outcome": outcome,
                "human_summary": human_summary,
            }
            if prev_hash != expected_prev:
                result["valid"] = False
                result["first_break_seq"] = seq
                result["reason"] = (
                    "row %r prev_hash does not link to the previous row's "
                    "event_hash (deleted/reordered/inserted rows?)" % event_id
                )
                return result
            recomputed = self._compute_hash(
                {**row_dict, "outcome": PENDING},  # chain covers INSERT-time outcome
                prev_hash or "",
            )
            if recomputed != event_hash:
                result["valid"] = False
                result["first_break_seq"] = seq
                result["reason"] = (
                    "row %r event_hash mismatch: row content was modified "
                    "after INSERT (tampering?)" % event_id
                )
                return result
            expected_prev = event_hash
        return result

    def _next_seq(self, session_id: str | None) -> int:
        """Per-session monotonic sequence number (starts at 1).

        Restart invariant: `_seq_counters` is process-local, but audit.db is
        durable. If a restarted process assigned seq=1 to a session that
        already has rows, two different events would share (session_id, seq)
        and the feed's `ORDER BY ts_utc, seq` (and any seq-based cursor)
        would silently interleave/misorder them. So the FIRST use of a
        session in this process seeds the counter from MAX(seq) on the
        writer connection before incrementing. `session_id IS ?` is
        NULL-safe in SQLite, so one query covers session_id=None too.
        Cost: one SELECT per NEW session per process, cached thereafter.
        """
        with self._seq_lock:
            current = self._seq_counters.get(session_id)
            if current is None:
                row = self._conn.execute(
                    "SELECT MAX(seq) FROM audit_events WHERE session_id IS ?",
                    (session_id,),
                ).fetchone()
                current = row[0] if row and row[0] is not None else 0
            next_val = current + 1
            self._seq_counters[session_id] = next_val
            return next_val

    def _enqueue(self, op: tuple) -> None:
        """Queue an op, failing closed if the writer thread is dead."""
        if not self._alive:
            raise RuntimeError("audit writer dead")
        self._queue.put(op)

    @staticmethod
    def _dump_json(obj: object) -> str | None:
        """Redact + serialize to JSON. Best-effort: never raises."""
        try:
            redacted = AuditJournal._redact(obj)
            if redacted is None:
                return None
            return json.dumps(redacted, default=str)
        except Exception:
            logger.exception("failed to serialize audit detail payload")
            return json.dumps({"redaction_error": "payload could not be serialized"})

    @staticmethod
    def _redact(obj: object) -> object:
        """Recursively redact sensitive keys and truncate oversized strings.

        Denylist keys (case-insensitive substring match) get "[REDACTED]".
        Any string value longer than MAX_STRING_LEN is truncated. Best-effort:
        wraps the whole walk so an unexpected structure never breaks logging.
        """
        try:
            return AuditJournal._redact_walk(obj)
        except Exception:
            logger.exception("redaction walk failed; dropping payload")
            return None

    @staticmethod
    def _redact_walk(obj: object) -> object:
        if isinstance(obj, dict):
            out = {}
            for key, value in obj.items():
                key_str = str(key)
                if key_str in REDACT_EXACT_ALLOWLIST:
                    out[key_str] = AuditJournal._redact_walk(value)
                elif any(marker in key_str.lower() for marker in REDACT_KEY_DENYLIST):
                    out[key_str] = REDACTED
                else:
                    out[key_str] = AuditJournal._redact_walk(value)
            return out
        if isinstance(obj, (list, tuple)):
            return [AuditJournal._redact_walk(item) for item in obj]
        if isinstance(obj, str):
            if len(obj) > MAX_STRING_LEN:
                obj = obj[:MAX_STRING_LEN] + f"...[truncated {len(obj) - MAX_STRING_LEN} chars]"
            return AuditJournal._redact_url_params(obj)
        return obj

    @staticmethod
    def _redact_url_params(s: str) -> str:
        """Redact secret values embedded in URL query strings.

        Catches e.g. "...?code=SECRET&scope=..." -> "...?code=[REDACTED]&scope=...".
        Best-effort: pattern substitution can't raise on plain str input.
        """
        return REDACT_URL_PARAM_PATTERN.sub(r"\1\2=[REDACTED]", s)

    # ------------------------------------------------------------------ #
    # Writer thread
    # ------------------------------------------------------------------ #

    def _writer_loop(self) -> None:
        """Drain the queue, batch ops into transactions, keep the connection.

        Commit policy: every BATCH_MAX_OPS ops, or after FLUSH_INTERVAL of no
        new ops, whichever comes first. A bad op is logged and skipped; the
        writer keeps running. Only sets _alive=True once initialization is
        complete and the loop is draining.
        """
        pending_ops: list[tuple] = []
        batch_start = time.monotonic()

        try:
            self._alive = True
            while True:
                try:
                    op = self._queue.get(timeout=FLUSH_INTERVAL)
                except queue.Empty:
                    op = None

                if op is _SENTINEL:
                    break

                if op is not None:
                    pending_ops.append(op)

                should_commit = (
                    len(pending_ops) >= BATCH_MAX_OPS
                    or (pending_ops and time.monotonic() - batch_start >= FLUSH_INTERVAL)
                    or (op is _SENTINEL)
                )
                if op is None and not pending_ops:
                    # Idle: still give the retry backlog a chance each interval so a
                    # retained batch isn't stranded when no new ops arrive (flush()
                    # depends on this — retained ops are NOT task_done until committed).
                    if self._has_retry_backlog():
                        self._apply_batch([])
                    continue
                if should_commit and pending_ops:
                    self._apply_batch(pending_ops)
                    pending_ops = []
                    batch_start = time.monotonic()
        except Exception:
            # The writer itself hit something unrecoverable — mark unhealthy.
            # Fail-closed: subsequent enqueue calls raise RuntimeError.
            logger.exception("audit writer thread crashed; journal is now unhealthy")
            self._alive = False
        finally:
            # Drain anything left so close() loses no events.
            if pending_ops:
                self._apply_batch(pending_ops)
                pending_ops = []
            try:
                while True:
                    op = self._queue.get_nowait()
                    if op is _SENTINEL:
                        break
                    self._apply_batch([op])
            except queue.Empty:
                pass
            self._alive = False

    def _has_retry_backlog(self) -> bool:
        """True when retained ops are waiting to be retried."""
        with self._retry_lock:
            return bool(self._retry_buffer)

    def _drain_retry_buffer(self) -> list[tuple]:
        """Pop and return all retained ops (writer-thread only). Return [] when
        empty. Uses _retry_lock; readers see a consistent snapshot."""
        with self._retry_lock:
            ops = list(self._retry_buffer)
            self._retry_buffer.clear()
        return ops

    def _retain_for_retry(self, ops: list[tuple]) -> None:
        """Move failed ops into the retry buffer, oldest first. If the buffer
        would exceed RETRY_BUFFER_CAP, mark the journal unhealthy (fail-closed)
        instead of growing unbounded. task_done() is deliberately NOT called —
        an op is done only when it commits, keeping flush() fail-closed."""
        with self._retry_lock:
            if len(self._retry_buffer) + len(ops) > RETRY_BUFFER_CAP:
                logger.error(
                    "audit retry buffer exceeded cap (%d); journal unhealthy",
                    RETRY_BUFFER_CAP,
                )
                self._alive = False
                return
            self._retry_buffer.extend(ops)

    def _apply_batch(self, ops: list[tuple]) -> None:
        """Apply a batch of ops in one transaction on the writer connection.

        Retain-on-failure (P4 part 2): on sqlite3.Error the batch's ops are
        moved into `_retry_buffer` (not dropped) and retried before any new
        ops on the next call. task_done() is called for an op ONLY when it
        commits, so flush() (queue.join) stays fail-closed on real failure
        instead of falsely reporting success. The buffer is bounded:
        exceeding RETRY_BUFFER_CAP marks the journal unhealthy (fail-closed)
        rather than grow unbounded.
        """
        # try the retained backlog first, then this batch's ops — one attempt per op
        backlog = self._drain_retry_buffer()
        if backlog:
            ops = backlog + ops
        try:
            for kind, payload in ops:
                if kind == "insert":
                    self._insert_event(payload)
                elif kind == "update":
                    self._update_event(payload)
                else:
                    logger.error("unknown audit op kind %r; skipping", kind)
            self._conn.commit()
            for _ in ops:
                self._queue.task_done()
        except sqlite3.Error:
            logger.exception("audit batch failed after %d ops; retaining for retry", len(ops))
            try:
                self._conn.rollback()
            except sqlite3.Error:
                pass
            self._retain_for_retry(ops)

    def _insert_event(self, row: dict) -> None:
        # P5-A: chain the row BEFORE the INSERT, in the writer thread (the
        # single source of ordering). prev_hash = last inserted row's hash;
        # outcome here is always the INSERT-time value 'pending'. The later
        # complete() UPDATE must NOT re-hash: the chain proves the event was
        # recorded, not its final outcome.
        prev_hash = self._last_hash
        row["prev_hash"] = prev_hash
        row["event_hash"] = self._compute_hash(row, prev_hash)
        self._last_hash = row["event_hash"]

        self._conn.execute(
            """
            INSERT INTO audit_events (
                event_id, schema_version, ts_utc, seq,
                session_id, conversation_id, trace_id, parent_event_id,
                actor, action_type, tool_name, side_effect_class,
                outcome, duration_ms, detail_json, provenance_json, human_summary,
                prev_hash, event_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["event_id"],
                row["schema_version"],
                row["ts_utc"],
                row["seq"],
                row["session_id"],
                row["conversation_id"],
                row["trace_id"],
                row["parent_event_id"],
                row["actor"],
                row["action_type"],
                row["tool_name"],
                row["side_effect_class"],
                row["outcome"],
                row["duration_ms"],
                row["detail_json"],
                row["provenance_json"],
                row.get("human_summary"),
                row["prev_hash"],
                row["event_hash"],
            ),
        )

    def _update_event(self, payload: dict) -> None:
        self._conn.execute(
            """
            UPDATE audit_events
               SET outcome = ?,
                   duration_ms = ?,
                   detail_json = ?,
                   provenance_json = ?
             WHERE event_id = ?
            """,
            (
                payload["outcome"],
                payload["duration_ms"],
                payload["detail_json"],
                payload["provenance_json"],
                payload["event_id"],
            ),
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("journal.py — import AuditJournal from here; no CLI entry point.")
