"""Unit tests for the hermes-audit journal write path."""
import os, sqlite3, sys, tempfile, threading, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from schema import TOOL_CALL, PENDING, SUCCESS, FAILED, INTERRUPTED
from journal import AuditJournal
from query import AuditQuery


def _rows(db, sql="SELECT * FROM audit_events"):
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql).fetchall()]
    finally:
        c.close()


class TestWriteAhead(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "audit.db")
        self.j = AuditJournal(self.db)

    def tearDown(self):
        self.j.close(); self.tmp.cleanup()

    def test_begin_pending_then_complete(self):
        ev = self.j.begin("assistant", TOOL_CALL, tool_name="read_file",
                          context={"session_id": "s1", "trace_id": "t1"})
        self.j.flush()
        rows = _rows(self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], PENDING)
        self.assertEqual(rows[0]["trace_id"], "t1")
        self.j.complete(ev, SUCCESS, detail={"ok": True}, duration_ms=42)
        self.j.flush()
        rows = _rows(self.db)
        self.assertEqual(rows[0]["outcome"], SUCCESS)
        self.assertEqual(rows[0]["duration_ms"], 42)
        self.assertIn("ok", rows[0]["detail_json"])

    def test_failed_outcome(self):
        ev = self.j.begin("assistant", TOOL_CALL, tool_name="terminal")
        self.j.complete(ev, FAILED, error="boom")
        self.j.flush()
        self.assertEqual(_rows(self.db)[0]["outcome"], FAILED)

    def test_seq_monotonic(self):
        e1 = self.j.begin("a", TOOL_CALL, context={"session_id": "s1"})
        e2 = self.j.begin("a", TOOL_CALL, context={"session_id": "s1"})
        e3 = self.j.begin("a", TOOL_CALL, context={"session_id": "s2"})
        self.assertEqual((e1.seq, e2.seq, e3.seq), (1, 2, 1))

    def test_redaction(self):
        ev = self.j.begin("a", TOOL_CALL)
        self.j.complete(ev, SUCCESS, detail={
            "api_key": "sk-secret-123",
            "nested": {"auth_token": "tok"},
            "long": "x" * 5000})
        self.j.flush()
        d = _rows(self.db)[0]["detail_json"]
        self.assertNotIn("sk-secret-123", d)
        self.assertNotIn('"tok"', d)  # the VALUE is redacted (key name 'auth_token' may remain)
        self.assertIn("[REDACTED]", d)
        self.assertIn("truncated", d)

    def test_redaction_url_query_params(self):
        """Secrets embedded in URL query strings get redacted; benign params stay."""
        ev = self.j.begin("a", TOOL_CALL)
        self.j.complete(ev, SUCCESS, detail={
            "url": "https://accounts.google.com/o/oauth2/auth?code=SECRET123&x=1",
            "msg": "redirect to /callback?access_token=TOK99&state=ok",
        })
        self.j.flush()
        d = _rows(self.db)[0]["detail_json"]
        self.assertNotIn("SECRET123", d)
        self.assertIn("code=[REDACTED]", d)
        self.assertIn("x=1", d)  # benign param preserved
        self.assertNotIn("TOK99", d)
        self.assertIn("access_token=[REDACTED]", d)
        self.assertIn("state=ok", d)

    def test_concurrent(self):
        errors = []
        def worker(n):
            try:
                for i in range(20):
                    ev = self.j.begin("a", TOOL_CALL, tool_name="t%d" % n,
                                      context={"session_id": "shared"})
                    self.j.complete(ev, SUCCESS, detail={"i": i})
            except Exception as exc:
                errors.append(exc)
        ts = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.j.flush()
        self.assertEqual(errors, [])
        rows = _rows(self.db)
        self.assertEqual(len(rows), 160)
        self.assertTrue(all(r["outcome"] == SUCCESS for r in rows))

    def test_fail_closed(self):
        self.assertTrue(self.j.is_healthy())
        self.j._alive = False
        with self.assertRaises(RuntimeError):
            self.j.begin("a", TOOL_CALL)


class TestClose(unittest.TestCase):
    def test_close_drains_idempotent(self):
        tmp = tempfile.TemporaryDirectory()
        db = os.path.join(tmp.name, "audit.db")
        j = AuditJournal(db)
        for i in range(100):
            ev = j.begin("a", TOOL_CALL, tool_name="t%d" % i)
            j.complete(ev, SUCCESS)
        j.close(); j.close()
        self.assertEqual(len(_rows(db)), 100)
        tmp.cleanup()


class TestSeqDurability(unittest.TestCase):
    """W1: per-session seq must survive a journal/process restart."""

    def test_seq_survives_restart(self):
        tmp = tempfile.TemporaryDirectory()
        db = os.path.join(tmp.name, "audit.db")
        j1 = AuditJournal(db)
        ev1 = j1.begin("a", TOOL_CALL, context={"session_id": "s1"})
        j1.complete(ev1, SUCCESS)
        j1.flush()
        j1.close()

        # Second journal on the SAME db file simulates a process restart:
        # its in-memory counter starts fresh, but seeding from MAX(seq)
        # must continue the sequence instead of resetting to 1.
        j2 = AuditJournal(db)
        try:
            ev2 = j2.begin("a", TOOL_CALL, context={"session_id": "s1"})
            j2.flush()
            self.assertGreater(ev2.seq, ev1.seq)
            rows = _rows(db, "SELECT seq FROM audit_events ORDER BY rowid")
            self.assertEqual([r["seq"] for r in rows], [ev1.seq, ev2.seq])
            self.assertEqual(len(set(r["seq"] for r in rows)), 2)
        finally:
            j2.close()
        tmp.cleanup()


class TestShutdownFlush(unittest.TestCase):
    """T1: close() must drain everything enqueued — zero pending rows lost."""

    def test_shutdown_flush(self):
        tmp = tempfile.TemporaryDirectory()
        db = os.path.join(tmp.name, "audit.db")
        j = AuditJournal(db)
        try:
            for i in range(25):
                ev = j.begin("a", TOOL_CALL, tool_name="t%d" % i,
                             context={"session_id": "s1"})
                j.complete(ev, SUCCESS)
        finally:
            j.close()
        # After the shutdown-equivalent (close), the queue is empty and
        # every enqueued event landed durably. (unfinished_tasks stays 1
        # because close()'s _SENTINEL never gets task_done() — expected.)
        self.assertEqual(j._queue.qsize(), 0)
        rows = _rows(db)
        self.assertEqual(len(rows), 25)
        self.assertTrue(all(r["outcome"] == SUCCESS for r in rows))


class TestInterruptedClosers(unittest.TestCase):
    """P4.5 (W2): pending rows left by a prior process become 'interrupted'."""

    def test_interrupted_closers(self):
        tmp = tempfile.TemporaryDirectory()
        db = os.path.join(tmp.name, "audit.db")
        j1 = AuditJournal(db)
        # Begin 2 events, leave them pending, flush, close WITHOUT completing.
        e1 = j1.begin("a", TOOL_CALL, tool_name="t1", context={"session_id": "s1"})
        e2 = j1.begin("a", TOOL_CALL, tool_name="t2", context={"session_id": "s1"})
        j1.flush()
        j1.close()

        # Sanity: both rows are durably 'pending' after the unclean close.
        rows = _rows(db, "SELECT event_id, outcome FROM audit_events ORDER BY rowid")
        self.assertEqual([r["outcome"] for r in rows], [PENDING, PENDING])

        # Second journal on the SAME db file simulates a process restart:
        # startup recovery must mark the stale pending rows 'interrupted'.
        j2 = AuditJournal(db)
        try:
            rows = _rows(db, "SELECT event_id, outcome FROM audit_events ORDER BY rowid")
            self.assertEqual(
                [r["outcome"] for r in rows], [INTERRUPTED, INTERRUPTED],
                "prior-process pending rows must be closed as interrupted",
            )
            self.assertEqual(
                {r["event_id"] for r in rows}, {e1.event_id, e2.event_id},
                "the interrupted rows are exactly the two begun-but-never-completed events",
            )

            # A NEW begin() on the restarted journal is LIVE pending, and the
            # recovery pass must not have touched it (runs before the writer
            # thread starts, i.e. before any of this process's own events).
            e3 = j2.begin("a", TOOL_CALL, tool_name="t3", context={"session_id": "s1"})
            j2.flush()
            row3 = _rows(db, "SELECT outcome FROM audit_events WHERE event_id = '%s'" % e3.event_id)
            self.assertEqual(len(row3), 1)
            self.assertEqual(row3[0]["outcome"], PENDING)
            self.assertNotEqual(row3[0]["outcome"], INTERRUPTED)

            # Live pending vs terminal interrupted must be distinguishable
            # through the read API.
            q = AuditQuery(db)
            live = q.pending()
            self.assertEqual(len(live), 1, "pending() must return only the LIVE pending row")
            self.assertEqual(live[0]["event_id"], e3.event_id)
            gone = q.interrupted()
            self.assertEqual(
                {r["event_id"] for r in gone}, {e1.event_id, e2.event_id},
                "interrupted() must return exactly the two recovered rows",
            )
        finally:
            j2.close()
        tmp.cleanup()


class TestRetainOnWriteFailure(unittest.TestCase):
    """P4 part 2: a failed batch is retained and retried, not dropped."""

    def test_retain_on_write_failure(self):
        tmp = tempfile.TemporaryDirectory()
        db = os.path.join(tmp.name, "audit.db")
        j = AuditJournal(db)
        try:
            # Fail the FIRST _apply_batch attempt by patching the write methods.
            holder = {"fail": True}

            def flaky_insert(payload):
                if holder["fail"]:
                    raise sqlite3.Error("simulated disk error")
                return j.__class__._insert_event(j, payload)

            j._insert_event = flaky_insert
            ev1 = j.begin("a", TOOL_CALL, tool_name="t1", context={"session_id": "s1"})
            j.complete(ev1, SUCCESS)
            # writer attempt (and fail) the first batch.
            import time
            time.sleep(1.0)
            # Restore; the retained batch should retry+commit.
            holder["fail"] = False
            j.flush()
            rows = _rows(db)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["outcome"], SUCCESS,
                             "the failed batch must be RETRIED and committed, not dropped")
        finally:
            j.close()
        tmp.cleanup()

    def test_retry_buffer_bounds_unhealthy(self):
        """Exceeding RETRY_BUFFER_CAP marks the journal unhealthy (fail-closed)."""
        tmp = tempfile.TemporaryDirectory()
        db = os.path.join(tmp.name, "audit.db")
        j = AuditJournal(db)
        try:
            def always_fail(payload):
                raise sqlite3.Error("permanent failure")
            j._insert_event = always_fail
            j._update_event = always_fail
            import journal as J
            # Enqueue past the cap; begin() starts raising RuntimeError once the
            # buffer overflows and the journal marks itself unhealthy — tolerate it.
            for i in range(J.RETRY_BUFFER_CAP + 10):
                try:
                    ev = j.begin("a", TOOL_CALL, context={"session_id": "s1"})
                    j.complete(ev, SUCCESS)
                except RuntimeError:
                    break  # expected once unhealthy
            import time
            time.sleep(2.0)
            self.assertFalse(j.is_healthy(),
                             "overflow of the retry buffer must mark the journal unhealthy")
        finally:
            j.close()
        tmp.cleanup()


class TestHashChain(unittest.TestCase):
    """P5-A: tamper-evidence via a sha256 hash chain over insert-time rows.

    The chain covers INSERT-time state only: complete()'s later UPDATE of
    outcome/duration must NOT break verification.
    """

    def _journal3(self, j):
        """Begin+complete 3 events; returns after flush."""
        for i in range(3):
            ev = j.begin("a", TOOL_CALL, tool_name="t%d" % i,
                         context={"session_id": "s1", "human_summary": "event %d" % i})
            j.complete(ev, SUCCESS, duration_ms=i)
        j.flush()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "audit.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_chain_links(self):
        j = AuditJournal(self.db)
        try:
            self._journal3(j)
            result = j.verify_chain()
            self.assertTrue(result["valid"], "chain must verify: %r" % result)
            self.assertEqual(result["length"], 3)
            self.assertIsNone(result["first_break_seq"])
            self.assertIsNone(result["reason"])
            rows = _rows(self.db,
                         "SELECT prev_hash, event_hash FROM audit_events ORDER BY rowid")
            self.assertEqual(rows[0]["prev_hash"], "",
                             "genesis row's prev_hash must be the empty string")
            for i in range(1, len(rows)):
                self.assertEqual(rows[i]["prev_hash"], rows[i - 1]["event_hash"],
                                 "each row's prev_hash must link to the prior event_hash")
            # The complete() UPDATE must not have broken anything.
            self.assertTrue(all(r["event_hash"] for r in rows))
        finally:
            j.close()

    def test_complete_does_not_break_chain(self):
        j = AuditJournal(self.db)
        try:
            self._journal3(j)  # completes UPDATE outcome/duration AFTER insert
            result = j.verify_chain()
            self.assertTrue(result["valid"],
                            "complete() changes outcome/duration and must NOT re-hash or break the chain")
        finally:
            j.close()

    def test_tamper_detected(self):
        j = AuditJournal(self.db)
        try:
            self._journal3(j)
            # Tamper a MIDDLE row via a SEPARATE raw sqlite connection.
            c = sqlite3.connect(self.db, timeout=5.0)
            try:
                mid_seq = c.execute(
                    "SELECT seq FROM audit_events ORDER BY rowid LIMIT 1 OFFSET 1"
                ).fetchone()[0]
                c.execute(
                    "UPDATE audit_events SET human_summary = 'TAMPERED' WHERE seq = ?",
                    (mid_seq,),
                )
                c.commit()
            finally:
                c.close()
            result = j.verify_chain()
            self.assertFalse(result["valid"], "tampering must invalidate the chain")
            self.assertEqual(result["first_break_seq"], mid_seq,
                             "first break must point at the tampered row")
            self.assertTrue(result["reason"])
        finally:
            j.close()

    def test_chain_survives_restart(self):
        j1 = AuditJournal(self.db)
        self._journal3(j1)
        j1.close()

        # Reopen the same db: the chain must resume from the stored last row.
        j2 = AuditJournal(self.db)
        try:
            ev = j2.begin("a", TOOL_CALL, context={"session_id": "s1"})
            j2.complete(ev, SUCCESS)
            j2.flush()
            result = j2.verify_chain()
            self.assertTrue(result["valid"], "chain must survive restart: %r" % result)
            self.assertEqual(result["length"], 4)
        finally:
            j2.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
