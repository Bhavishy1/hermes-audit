"""Tests for hooks.on_pre_tool_call (audit finding C1).

The pre_tool_call hook is FAIL-CLOSED in Hermes: a callback that raises or
times out blocks the tool. These tests verify the observer journals tool-call
attempts (phase='pre'), captures block hints, and — critically — never raises
and always returns None even with a broken/absent journal.
"""
import os, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hooks
from schema import TOOL_CALL, SUCCESS
from journal import AuditJournal


def _rows(db):
    import sqlite3
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute("SELECT * FROM audit_events").fetchall()]
    finally:
        c.close()


class TestOnPreToolCall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "audit.db")
        self.j = AuditJournal(self.db)
        hooks.set_journal(self.j)

    def tearDown(self):
        hooks.set_journal(None)
        self.j.close(); self.tmp.cleanup()

    def test_pre_tool_call_journals_attempt(self):
        """C1 spec: tool_name + args journaled as a phase='pre' tool_call row."""
        out = hooks.on_pre_tool_call(
            tool_name="terminal", args={"command": "rm -rf /"}, session_id="s1")
        self.assertIsNone(out)  # always None: must never veto
        self.j.flush()
        rows = _rows(self.db)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["action_type"], TOOL_CALL)
        self.assertEqual(row["tool_name"], "terminal")
        self.assertEqual(row["outcome"], SUCCESS)
        self.assertEqual(row["session_id"], "s1")
        detail = __import__("json").loads(row["detail_json"])
        self.assertEqual(detail["phase"], "pre")
        self.assertEqual(detail["blocked"], False)
        self.assertEqual(detail["args"], {"command": "rm -rf /"})

    def test_pre_tool_call_captures_block_hint(self):
        """A decision/veto-shaped kwarg marks the attempt blocked."""
        hooks.on_pre_tool_call(
            tool_name="terminal", args={"command": "sudo rm"}, session_id="s1",
            block_message="BLOCKED: policy veto")
        self.j.flush()
        detail = __import__("json").loads(_rows(self.db)[0]["detail_json"])
        self.assertEqual(detail["phase"], "pre")
        self.assertEqual(detail["blocked"], True)
        self.assertEqual(detail["block_message"], "BLOCKED: policy veto")

    def test_pre_tool_call_redacts_args(self):
        """Args pass through the journal's redaction path (denylist keys)."""
        hooks.on_pre_tool_call(
            tool_name="terminal", args={"command": "env", "api_key": "sk-secret"},
            session_id="s1")
        self.j.flush()
        detail = __import__("json").loads(_rows(self.db)[0]["detail_json"])
        self.assertEqual(detail["args"]["api_key"], "[REDACTED]")
        self.assertEqual(detail["args"]["command"], "env")

    def test_pre_tool_call_trace_id_from_turn_id(self):
        """turn_id in hook kwargs becomes the row's trace_id (correlation)."""
        hooks.on_pre_tool_call(
            tool_name="read_file", args={"path": "/tmp/x"},
            session_id="s1", turn_id="s1:task:abc123")
        self.j.flush()
        row = _rows(self.db)[0]
        self.assertEqual(row["trace_id"], "s1:task:abc123")
        # _record routes action_type=tool_call through summarize_tool_call;
        # the human summary lands in its own column (not detail_json).
        # read_file with path=/tmp/x -> "Read x" (basename), not the raw name.
        self.assertEqual(row["human_summary"], "Read x")

    def test_no_journal_never_raises(self):
        """Fail-open: without a journal the observer is a silent no-op."""
        hooks.set_journal(None)
        out = hooks.on_pre_tool_call(
            tool_name="terminal", args={"command": "rm -rf /"}, session_id="s1")
        self.assertIsNone(out)

    def test_unhealthy_journal_never_raises(self):
        """Fail-open: a dead writer must not break the agent's tool path."""
        hooks.set_journal(self.j)
        self.j.close()  # writer thread dead; is_healthy() False
        try:
            out = hooks.on_pre_tool_call(
                tool_name="terminal", args={"command": "ls"}, session_id="s1")
            self.assertIsNone(out)
        finally:
            hooks.set_journal(None)

    def test__record_swallows_broken_journal(self):
        """_record with a journal that raises must never propagate."""
        class _Boom:
            def is_healthy(self):
                return True
            def begin(self, *a, **k):
                raise RuntimeError("sqlite is on fire")
        hooks.set_journal(_Boom())
        try:
            # None return; no exception even though journal.begin() raised.
            out = hooks.on_pre_tool_call(
                tool_name="terminal", args={}, session_id="s1")
            self.assertIsNone(out)
        finally:
            hooks.set_journal(None)

    def test__record_none_journal_noop(self):
        """Direct _record with journal=None is a silent no-op (spec assert)."""
        hooks.set_journal(None)
        hooks._record("assistant", TOOL_CALL, detail={"x": 1},
                      tool_name="terminal", session_id="s1")
        # Reaching here without TypeError/AttributeError is the assertion.

    def test_hostile_kwargs_never_raise(self):
        """Catastrophic input must degrade, not veto (fail-closed host)."""
        class Unrepr:
            def __repr__(self):
                raise RuntimeError("no repr")
        out = hooks.on_pre_tool_call(
            tool_name="terminal", args={"obj": Unrepr()},
            session_id="s1", blocked=Unrepr())
        self.assertIsNone(out)
        self.j.flush()
        rows = _rows(self.db)
        # Either journaled degraded or skipped entirely — never a raise.
        self.assertIn(len(rows), (0, 1))


if __name__ == "__main__":
    unittest.main()
