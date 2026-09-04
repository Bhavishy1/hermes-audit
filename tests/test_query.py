"""Tests for the read/query API."""
import os, sys, tempfile, json, sqlite3, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from journal import AuditJournal
from query import AuditQuery
from schema import TOOL_CALL, SUCCESS, FAILED, PENDING


class TestQuery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "audit.db")
        self.j = AuditJournal(self.db)
        e1 = self.j.begin("assistant", TOOL_CALL, tool_name="read_file",
                          context={"trace_id": "trace-1", "session_id": "s1"})
        self.j.complete(e1, SUCCESS, detail={"file": "a.csv"}, provenance={"source": "a.csv"})
        e2 = self.j.begin("automation:weekly", TOOL_CALL, tool_name="terminal",
                          context={"trace_id": "trace-1", "session_id": "s1"})
        self.j.complete(e2, SUCCESS)
        e3 = self.j.begin("assistant", TOOL_CALL, tool_name="web_search",
                          context={"trace_id": "trace-2", "session_id": "s2"})
        self.j.complete(e3, SUCCESS)
        self.j.flush()
        self.q = AuditQuery(self.db)

    def tearDown(self):
        self.j.close(); self.tmp.cleanup()

    def test_recent(self):
        self.assertEqual(len(self.q.recent(limit=2)), 2)
        self.assertEqual(len(self.q.recent(limit=50)), 3)

    def test_by_trace(self):
        self.assertEqual(len(self.q.by_trace("trace-1")), 2)

    def test_by_actor(self):
        self.assertEqual(len(self.q.by_actor("automation:weekly")), 1)

    def test_by_source(self):
        self.assertEqual(len(self.q.by_source("a.csv")), 1)

    def test_pending_empty(self):
        self.assertEqual(self.q.pending(), [])

    def test_export(self):
        out = self.q.export(os.path.join(self.tmp.name, "export.json"))
        data = json.load(open(out))
        self.assertEqual(len(data), 3)

    def test_q_filter_matches_human_summary(self):
        """R2: the plugin_api `q` substring filter also matches human_summary."""
        needle = 'Searched the web for "zebralution"'
        e4 = self.j.begin("assistant", TOOL_CALL, tool_name="web_search",
                          context={"trace_id": "trace-3", "session_id": "s3",
                                   "human_summary": needle})
        self.j.complete(e4, SUCCESS)
        self.j.flush()

        # Replicate the plugin_api list_events `q` WHERE clause:
        # (tool_name LIKE ? OR detail_json LIKE ? OR human_summary LIKE ?).
        # "zebralution" appears only in human_summary, so the third clause
        # must be the one that matches.
        pat = "%zebralution%"
        rows = self.q._select(
            where="(tool_name LIKE ? OR detail_json LIKE ? OR human_summary LIKE ?)",
            params=(pat, pat, pat),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_id"], e4.event_id)
        self.assertEqual(rows[0]["human_summary"], needle)
        # display_summary derivation surfaces human_summary (query.py, no change needed).
        self.assertIn("zebralution", rows[0]["display_summary"])
        # And the row is reachable through the plain recent() read path.
        self.assertTrue(any(r["event_id"] == e4.event_id
                            for r in self.q.recent(limit=50)))

class TestCoverage(unittest.TestCase):
    """P4: coverage() reconciliation heuristic + journal.stats() health."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "audit.db")
        self.j = AuditJournal(self.db)
        self.q = AuditQuery(self.db)

    def tearDown(self):
        self.j.close(); self.tmp.cleanup()

    def test_suspicious_session_tool_calls_no_llm(self):
        """Session with tool_calls but zero llm_calls -> suspicious=True."""
        for i in range(2):
            e = self.j.begin("assistant", TOOL_CALL, tool_name="terminal",
                             context={"trace_id": "t-susp", "session_id": "s-susp"})
            self.j.complete(e, SUCCESS)
        self.j.flush()
        cov = self.q.coverage("s-susp")
        self.assertTrue(cov["suspicious"])
        self.assertEqual(cov["counts"], {"tool_call": 2})
        self.assertEqual(cov["tool_calls"], 2)
        self.assertEqual(cov["llm_calls"], 0)
        self.assertEqual(cov["tool_calls_without_preceding_llm_call"], 2)
        self.assertEqual(cov["session_id"], "s-susp")

    def test_normal_session_not_suspicious(self):
        """llm_call precedes tool_call in the same trace -> suspicious=False."""
        llm = self.j.begin("assistant", "llm_call",
                           context={"trace_id": "t-ok", "session_id": "s-ok"})
        self.j.complete(llm, SUCCESS)
        tool = self.j.begin("assistant", TOOL_CALL, tool_name="read_file",
                            context={"trace_id": "t-ok", "session_id": "s-ok"})
        self.j.complete(tool, SUCCESS)
        msg = self.j.begin("assistant", "message",
                           context={"session_id": "s-ok"})
        self.j.complete(msg, SUCCESS)
        self.j.flush()
        cov = self.q.coverage("s-ok")
        self.assertFalse(cov["suspicious"])
        self.assertEqual(cov["counts"], {"llm_call": 1, "tool_call": 1, "message": 1})
        self.assertEqual(cov["tool_calls_without_preceding_llm_call"], 0)

    def test_tool_call_before_llm_counts_uncovered(self):
        """seq-ordered walk: tool_call BEFORE the trace's first llm_call is uncovered."""
        early = self.j.begin("assistant", TOOL_CALL, tool_name="web_search",
                             context={"trace_id": "t-mix", "session_id": "s-mix"})
        self.j.complete(early, SUCCESS)
        llm = self.j.begin("assistant", "llm_call",
                           context={"trace_id": "t-mix", "session_id": "s-mix"})
        self.j.complete(llm, SUCCESS)
        late = self.j.begin("assistant", TOOL_CALL, tool_name="read_file",
                            context={"trace_id": "t-mix", "session_id": "s-mix"})
        self.j.complete(late, SUCCESS)
        self.j.flush()
        cov = self.q.coverage("s-mix")
        self.assertFalse(cov["suspicious"])  # llm_calls == 1 > 0
        self.assertEqual(cov["tool_calls_without_preceding_llm_call"], 1)
        self.assertEqual(cov["counts"], {"tool_call": 2, "llm_call": 1})

    def test_null_trace_tool_call_is_uncovered(self):
        """A tool_call with no trace can never be tied to a model decision."""
        e = self.j.begin("assistant", TOOL_CALL, tool_name="terminal",
                         context={"session_id": "s-null"})
        self.j.complete(e, SUCCESS)
        self.j.flush()
        cov = self.q.coverage("s-null")
        self.assertTrue(cov["suspicious"])
        self.assertEqual(cov["tool_calls_without_preceding_llm_call"], 1)

    def test_unknown_session_is_empty_and_unsuspicious(self):
        cov = self.q.coverage("no-such-session")
        self.assertEqual(cov["counts"], {})
        self.assertFalse(cov["suspicious"])
        self.assertEqual(cov["tool_calls_without_preceding_llm_call"], 0)

    def test_journal_stats_health(self):
        """journal.stats(): writer alive, drained queue, per-type counts, last ts."""
        llm = self.j.begin("assistant", "llm_call",
                           context={"trace_id": "t-ok", "session_id": "s-ok"})
        self.j.complete(llm, SUCCESS)
        self.j.flush()
        stats = self.j.stats()
        self.assertTrue(stats["writer_alive"])
        self.assertEqual(stats["queue_size"], 0)
        self.assertEqual(stats["events_total"], 1)
        self.assertEqual(stats["by_action_type"], {"llm_call": 1})
        self.assertIsNotNone(stats["last_event_ts"])
        self.assertEqual(
            sum(stats["by_action_type"].values()), stats["events_total"]
        )


class TestGrouped(unittest.TestCase):
    """P3: grouped() request-group rollup over audit_events."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "audit.db")
        self.j = AuditJournal(self.db)

        # trace-1 (older): 1 tool_call + 1 llm_call, all success.
        a1 = self.j.begin("assistant", TOOL_CALL, tool_name="read_file",
                          context={"trace_id": "trace-1", "session_id": "s1",
                                   "human_summary": "Read file a.csv"})
        self.j.complete(a1, SUCCESS, duration_ms=100)
        a2 = self.j.begin("assistant", "llm_call",
                          context={"trace_id": "trace-1", "session_id": "s1",
                                   "human_summary": "LLM turn 1"})
        self.j.complete(a2, SUCCESS, duration_ms=50)

        # Singleton NULL-trace event (legacy/untraced): its own group.
        b1 = self.j.begin("assistant", "message",
                          context={"session_id": "s1"})
        self.j.complete(b1, SUCCESS, duration_ms=None)

        # trace-2 (newest): tool_call error + llm_call pending -> rollup 'error'.
        c1 = self.j.begin("assistant", TOOL_CALL, tool_name="web_search",
                          context={"trace_id": "trace-2", "session_id": "s1",
                                   "human_summary": "Searched the web"})
        self.j.complete(c1, FAILED, duration_ms=10)
        c2 = self.j.begin("assistant", "llm_call",
                          context={"trace_id": "trace-2", "session_id": "s1"})
        self.j.complete(c2, PENDING, duration_ms=20)

        self.j.flush()
        # Force deterministic ts_utc per group (second-granularity timestamps
        # would otherwise tie). Direct UPDATE on a separate connection; the
        # journal is flushed so rows are committed.
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE audit_events SET ts_utc='2026-09-04T10:00:00Z' WHERE trace_id='trace-1'")
        conn.execute("UPDATE audit_events SET ts_utc='2026-09-04T10:00:05Z' WHERE trace_id IS NULL")
        conn.execute("UPDATE audit_events SET ts_utc='2026-09-04T10:00:10Z' WHERE trace_id='trace-2'")
        conn.commit()
        conn.close()
        self.null_event_id = b1.event_id
        self.q = AuditQuery(self.db)

    def tearDown(self):
        self.j.close(); self.tmp.cleanup()

    def test_group_count_and_ordering(self):
        groups = self.q.grouped()
        self.assertEqual(len(groups), 3)
        # Newest group first by latest event ts_utc.
        self.assertEqual([g["trace_id"] for g in groups],
                         ["trace-2", "event:" + self.null_event_id, "trace-1"])

    def test_limit_groups(self):
        self.assertEqual(len(self.q.grouped(limit_groups=2)), 2)
        self.assertEqual(self.q.grouped(limit_groups=2)[0]["trace_id"], "trace-2")

    def test_multi_event_group_counts_and_rollup(self):
        groups = {g["trace_id"]: g for g in self.q.grouped()}
        g1 = groups["trace-1"]
        self.assertEqual(g1["event_count"], 2)
        self.assertEqual(g1["tool_call_count"], 1)
        self.assertEqual(g1["llm_call_count"], 1)
        self.assertEqual(g1["total_duration_ms"], 150)
        self.assertEqual(g1["outcome"], "success")
        self.assertEqual(g1["start_ts"], "2026-09-04T10:00:00Z")
        self.assertEqual(g1["end_ts"], "2026-09-04T10:00:00Z")
        # Title = FIRST tool_call's human_summary (not the later llm_call's).
        self.assertEqual(g1["title"], "Read file a.csv")

        g2 = groups["trace-2"]
        self.assertEqual(g2["event_count"], 2)
        self.assertEqual(g2["tool_call_count"], 1)
        self.assertEqual(g2["llm_call_count"], 1)
        self.assertEqual(g2["total_duration_ms"], 30)
        # error wins over pending in the rollup.
        self.assertEqual(g2["outcome"], "error")
        self.assertEqual(g2["title"], "Searched the web")

    def test_singleton_null_trace_group(self):
        groups = {g["trace_id"]: g for g in self.q.grouped()}
        g = groups["event:" + self.null_event_id]
        self.assertEqual(g["event_count"], 1)
        self.assertEqual(g["tool_call_count"], 0)
        self.assertEqual(g["llm_call_count"], 0)
        self.assertEqual(g["total_duration_ms"], 0)
        self.assertEqual(g["outcome"], "success")
        self.assertEqual(g["title"], "Turn")  # no tool_call, no human_summary

    def test_events_ordered_seq_ascending(self):
        # Interleave a second tool_call in trace-1 out of seq order of arrival.
        a3 = self.j.begin("assistant", TOOL_CALL, tool_name="terminal",
                          context={"trace_id": "trace-1", "session_id": "s1"})
        self.j.complete(a3, SUCCESS, duration_ms=5)
        self.j.flush()
        groups = {g["trace_id"]: g for g in self.q.grouped()}
        seqs = [e["seq"] for e in groups["trace-1"]["events"]]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), 3)

    def test_empty_db_returns_empty_list(self):
        empty = AuditQuery(os.path.join(self.tmp.name, "missing.db"))
        self.assertEqual(empty.grouped(), [])


class TestRetention(unittest.TestCase):
    """P5-B: retention purge — preview (dry_run) vs delete, old vs recent."""

    RECENT_EVENTS = 3   # keep created_at as written (now)
    OLD_EVENTS = 2      # backdated to 400 days ago
    RETENTION_DAYS = 365

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "audit.db")
        self.j = AuditJournal(self.db)
        self.event_ids = []
        for i in range(self.RECENT_EVENTS + self.OLD_EVENTS):
            e = self.j.begin("assistant", TOOL_CALL, tool_name="read_file",
                             context={"trace_id": "trace-ret", "session_id": "s-ret"})
            self.j.complete(e, SUCCESS)
            self.event_ids.append(e.event_id)
        self.j.flush()
        # Backdate the first OLD_EVENTS rows (created_at is a
        # 'YYYY-MM-DD HH:MM:SS' string). Direct writable connection, same
        # pattern TestGrouped.setUp uses for ts_utc backdating.
        conn = sqlite3.connect(self.db, timeout=10.0)
        conn.execute(
            "UPDATE audit_events SET created_at = datetime('now', '-400 days') "
            "WHERE event_id IN ({})".format(
                ",".join("?" * self.OLD_EVENTS)
            ),
            tuple(self.event_ids[: self.OLD_EVENTS]),
        )
        conn.commit()
        conn.close()
        self.q = AuditQuery(self.db)

    def tearDown(self):
        self.j.close(); self.tmp.cleanup()

    def _rows_with_ids(self):
        rows = self.q._select(where="session_id = ?", params=("s-ret",))
        return {r["event_id"] for r in rows}

    def test_purge_dry_run_deletes_nothing(self):
        """dry_run=True previews the count and mutates nothing."""
        res = self.q.purge_older_than(self.RETENTION_DAYS, dry_run=True)
        self.assertEqual(res["would_delete"], self.OLD_EVENTS)
        self.assertIn("cutoff_ts", res)
        # No 'deleted' key on the preview path; rows all still present.
        self.assertNotIn("deleted", res)
        self.assertEqual(len(self._rows_with_ids()),
                         self.RECENT_EVENTS + self.OLD_EVENTS)

    def test_purge_deletes_old(self):
        """dry_run=False removes only rows older than the cutoff."""
        res = self.q.purge_older_than(self.RETENTION_DAYS, dry_run=False)
        self.assertEqual(res["deleted"], self.OLD_EVENTS)
        self.assertIn("cutoff_ts", res)
        remaining = self._rows_with_ids()
        # Old rows gone, recent rows kept.
        for old_id in self.event_ids[: self.OLD_EVENTS]:
            self.assertNotIn(old_id, remaining)
        for recent_id in self.event_ids[self.OLD_EVENTS:]:
            self.assertIn(recent_id, remaining)
        self.assertEqual(len(remaining), self.RECENT_EVENTS)

    def test_purge_keeps_recent(self):
        """Events inside the retention window are never counted or deleted."""
        # Fresh db with only recent events: nothing old, so both paths are no-ops.
        res = self.q.purge_older_than(3650, dry_run=True)
        self.assertEqual(res["would_delete"], 0)
        res = self.q.purge_older_than(3650, dry_run=False)
        self.assertEqual(res["deleted"], 0)
        self.assertEqual(len(self._rows_with_ids()),
                         self.RECENT_EVENTS + self.OLD_EVENTS)


class TestFacets(unittest.TestCase):
    """facets() + day/model filtering on recent() — filter-picker backend."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "audit.db")
        self.j = AuditJournal(self.db)
        # Two llm_calls with model A (day 1), one llm_call with model B
        # (day 2), plus a tool_call (not a model — must not count as one).
        a1 = self.j.begin("assistant", "llm_call", tool_name="model-alpha",
                          context={"trace_id": "trace-1", "session_id": "s1"})
        self.j.complete(a1, SUCCESS)
        a2 = self.j.begin("assistant", "llm_call", tool_name="model-alpha",
                          context={"trace_id": "trace-1", "session_id": "s1"})
        self.j.complete(a2, SUCCESS)
        b1 = self.j.begin("assistant", "llm_call", tool_name="model-beta",
                          context={"trace_id": "trace-2", "session_id": "s2"})
        self.j.complete(b1, SUCCESS)
        t1 = self.j.begin("assistant", TOOL_CALL, tool_name="read_file",
                          context={"trace_id": "trace-2", "session_id": "s2"})
        self.j.complete(t1, SUCCESS)
        self.model_a_ids = {a1.event_id, a2.event_id}
        self.model_b_id = b1.event_id
        self.j.flush()
        # Deterministic days: model-alpha calls on 2026-09-03 (older),
        # model-beta call + tool_call on 2026-09-04. Direct writable
        # connection (journal flushed/committed first), same pattern as
        # TestGrouped.setUp.
        conn = sqlite3.connect(self.db, timeout=10.0)
        conn.execute("UPDATE audit_events SET ts_utc='2026-09-03T10:00:00Z' WHERE event_id IN (?, ?)",
                     (a1.event_id, a2.event_id))
        conn.execute("UPDATE audit_events SET ts_utc='2026-09-04T09:00:00Z' WHERE event_id = ?",
                     (b1.event_id,))
        conn.execute("UPDATE audit_events SET ts_utc='2026-09-04T09:00:01Z' WHERE event_id = ?",
                     (t1.event_id,))
        conn.commit()
        conn.close()
        self.q = AuditQuery(self.db)

    def tearDown(self):
        self.j.close(); self.tmp.cleanup()

    def test_facets_days_newest_first(self):
        facets = self.q.facets()
        self.assertEqual(facets["days"], ["2026-09-04", "2026-09-03"])

    def test_facets_model_counts(self):
        models = self.q.facets()["models"]
        self.assertEqual(models,
                         [{"model": "model-alpha", "count": 2},
                          {"model": "model-beta", "count": 1}])

    def test_facets_missing_db_is_empty(self):
        empty = AuditQuery(os.path.join(self.tmp.name, "missing.db"))
        self.assertEqual(empty.facets(), {"days": [], "models": []})

    def test_day_filter_returns_only_that_day(self):
        day1 = self.q.recent(day="2026-09-03")
        self.assertEqual(len(day1), 2)
        self.assertTrue(all(r["ts_utc"].startswith("2026-09-03") for r in day1))
        day2 = self.q.recent(day="2026-09-04")
        self.assertEqual(len(day2), 2)
        self.assertTrue(all(r["ts_utc"].startswith("2026-09-04") for r in day2))
        self.assertEqual({r["action_type"] for r in day2}, {"llm_call", "tool_call"})

    def test_day_filter_combines_with_model(self):
        rows = self.q.recent(day="2026-09-03", model="model-alpha")
        self.assertEqual(len(rows), 2)
        rows = self.q.recent(day="2026-09-04", model="model-alpha")
        self.assertEqual(rows, [])  # alpha only appears on the older day

    def test_model_filter_returns_only_that_models_llm_calls(self):
        rows = self.q.recent(model="model-alpha")
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["event_id"] for r in rows}, self.model_a_ids)
        for r in rows:
            self.assertEqual(r["action_type"], "llm_call")
            self.assertEqual(r["tool_name"], "model-alpha")
        rows = self.q.recent(model="model-beta")
        self.assertEqual([r["event_id"] for r in rows], [self.model_b_id])
        # The read_file tool_call row is never matched by a model filter.
        self.assertNotIn("read_file", {r["tool_name"] for r in rows})

    def test_model_filter_no_match_returns_empty(self):
        self.assertEqual(self.q.recent(model="model-gamma"), [])

    def test_no_filters_unchanged(self):
        self.assertEqual(len(self.q.recent()), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
