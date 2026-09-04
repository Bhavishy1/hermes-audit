"""Tests for deterministic human-summary generation and the human_summary
schema migration."""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from journal import AuditJournal
from schema import SUCCESS, TOOL_CALL
from summarize import summarize_event, summarize_tool_call


class TestSummarizeToolCall(unittest.TestCase):
    def test_web_search(self):
        s = summarize_tool_call("web_search", {"query": "hermes audit"})
        self.assertEqual(s, 'Searched the web for "hermes audit"')

    def test_read_file_basename(self):
        s = summarize_tool_call("read_file", {"path": "~/x/y/envelope.py"})
        self.assertEqual(s, "Read envelope.py")

    def test_terminal_command(self):
        s = summarize_tool_call("terminal", {"command": "git status"})
        self.assertEqual(s, "Ran `git status`")

    def test_patch_file(self):
        s = summarize_tool_call("patch", {"path": "hooks.py"})
        self.assertEqual(s, "Edited hooks.py")

    def test_write_file(self):
        s = summarize_tool_call("write_file", {"path": "/a/b/c.md"})
        self.assertEqual(s, "Wrote c.md")

    def test_unknown_tool_fallback(self):
        s = summarize_tool_call("some_new_tool", {"foo": "bar"})
        self.assertEqual(s, "Used some_new_tool")

    def test_none_tool(self):
        s = summarize_tool_call(None, {})
        self.assertEqual(s, "Used tool")

    def test_malformed_args_never_raises(self):
        # args not a dict
        s = summarize_tool_call("web_search", "not-a-dict")
        self.assertEqual(s, "Searched the web")
        # None args
        s = summarize_tool_call("terminal", None)
        self.assertEqual(s, "Ran a shell command")

    def test_long_query_truncated(self):
        s = summarize_tool_call("web_search", {"query": "x" * 200})
        self.assertTrue(s.endswith('…"'))
        self.assertLess(len(s), 120)

    def test_memory_verbs(self):
        self.assertEqual(summarize_tool_call("memory", {"action": "add"}), "Saved a memory")
        self.assertEqual(summarize_tool_call("memory", {"action": "remove"}), "Removed a memory")

    def test_delegate_task_count(self):
        s = summarize_tool_call("delegate_task", {"tasks": [{}, {}, {}]})
        self.assertEqual(s, "Delegated 3 subagent tasks")


class TestSummarizeEvent(unittest.TestCase):
    def test_llm_call(self):
        s = summarize_event("llm_call", None, {"model": "kimi-k3"})
        self.assertEqual(s, "Thought with kimi-k3")

    def test_message(self):
        self.assertEqual(summarize_event("message", None, {}), "Replied to you")

    def test_session_events(self):
        self.assertEqual(summarize_event("session_start", None, {}), "Session started")
        self.assertEqual(summarize_event("session_end", None, {}), "Session ended")


class TestHumanSummaryMigration(unittest.TestCase):
    def test_human_summary_column_added_to_old_db(self):
        """A DB created with the old (no human_summary) schema gets migrated."""
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "audit.db")
            # Create an old-schema DB by hand
            conn = sqlite3.connect(db)
            conn.execute("""
                CREATE TABLE audit_events (
                  event_id TEXT PRIMARY KEY, schema_version TEXT NOT NULL,
                  ts_utc TEXT NOT NULL, seq INTEGER NOT NULL,
                  session_id TEXT, conversation_id TEXT, trace_id TEXT,
                  parent_event_id TEXT, actor TEXT NOT NULL, action_type TEXT NOT NULL,
                  tool_name TEXT, side_effect_class TEXT, outcome TEXT NOT NULL,
                  duration_ms INTEGER, detail_json TEXT, provenance_json TEXT,
                  created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT DEFAULT (datetime('now')))")
            conn.commit()
            conn.close()

            # Opening with the journal should add human_summary
            j = AuditJournal(db)
            cols = {r[1] for r in j._conn.execute("PRAGMA table_info(audit_events)")}
            self.assertIn("human_summary", cols)

            # And a begin() with human_summary in context persists it
            ev = j.begin("assistant", TOOL_CALL, tool_name="web_search",
                         context={"human_summary": 'Searched the web for "x"'})
            j.complete(ev, SUCCESS)
            j.flush()
            row = j._conn.execute("SELECT human_summary FROM audit_events WHERE event_id=?",
                                  (ev.event_id,)).fetchone()
            self.assertEqual(row[0], 'Searched the web for "x"')
            j.close()


if __name__ == "__main__":
    unittest.main()
