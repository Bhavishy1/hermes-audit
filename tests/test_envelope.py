"""Tests for the middleware envelopes: single-use next_call, success/error
journaling, skill_manage -> SKILL_WRITE classification, never-break-the-agent."""
import os, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import envelope
from schema import SUCCESS, FAILED, TOOL_CALL, SKILL_WRITE
from journal import AuditJournal


def _rows(db):
    import sqlite3
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute("SELECT * FROM audit_events").fetchall()]
    finally:
        c.close()


class TestToolEnvelope(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "audit.db")
        self.j = AuditJournal(self.db)
        envelope.set_journal(self.j)

    def tearDown(self):
        envelope.set_journal(None)
        self.j.close(); self.tmp.cleanup()

    def test_success_journaled_and_result_returned(self):
        def real_tool(args):
            return {"echo": args["x"]}
        out = envelope.tool_execution_envelope(
            tool_name="read_file", args={"x": 5}, next_call=real_tool,
            session_id="s1", trace_id="t1")
        self.assertEqual(out, {"echo": 5})
        self.j.flush()
        rows = _rows(self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action_type"], TOOL_CALL)
        self.assertEqual(rows[0]["outcome"], SUCCESS)
        self.assertEqual(rows[0]["tool_name"], "read_file")

    def test_error_journaled_and_reraised(self):
        def bad_tool(args):
            raise ValueError("tool exploded")
        with self.assertRaises(ValueError):
            envelope.tool_execution_envelope(
                tool_name="terminal", args={}, next_call=bad_tool)
        self.j.flush()
        rows = _rows(self.db)
        self.assertEqual(rows[0]["outcome"], FAILED)
        self.assertIn("tool exploded", rows[0]["detail_json"])

    def test_skill_manage_is_skill_write(self):
        envelope.tool_execution_envelope(
            tool_name="skill_manage", args={}, next_call=lambda a: "ok")
        self.j.flush()
        self.assertEqual(_rows(self.db)[0]["action_type"], SKILL_WRITE)

    def test_no_journal_runs_bare(self):
        envelope.set_journal(None)
        out = envelope.tool_execution_envelope(
            tool_name="x", args={}, next_call=lambda a: "ran")
        self.assertEqual(out, "ran")
        self.j.flush()
        self.assertEqual(_rows(self.db), [])

    def test_turn_id_maps_to_trace_id(self):
        """Hermes passes turn_id (not trace_id) in the middleware context
        (agent_runtime_helpers -> run_tool_execution_middleware). The envelope
        must map it to trace_id so the event joins its turn's request-group."""
        envelope.tool_execution_envelope(
            tool_name="read_file", args={}, next_call=lambda a: "ok",
            session_id="s1", turn_id="s1:task:abc123")
        self.j.flush()
        rows = _rows(self.db)
        self.assertEqual(rows[0]["trace_id"], "s1:task:abc123")

    def test_explicit_trace_id_wins_over_turn_id(self):
        """An explicit trace_id is not overwritten by turn_id."""
        envelope.tool_execution_envelope(
            tool_name="read_file", args={}, next_call=lambda a: "ok",
            trace_id="explicit", turn_id="s1:task:abc123")
        self.j.flush()
        self.assertEqual(_rows(self.db)[0]["trace_id"], "explicit")


class TestLlmEnvelope(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "audit.db")
        self.j = AuditJournal(self.db)
        envelope.set_journal(self.j)

    def tearDown(self):
        envelope.set_journal(None)
        self.j.close(); self.tmp.cleanup()

    def test_llm_call_journaled_with_usage(self):
        def fake_llm(request):
            return {"text": "hi", "usage": {"total_tokens": 123}}
        out = envelope.llm_execution_envelope(
            request={"model": "kimi-k3"}, next_call=fake_llm)
        self.assertEqual(out["text"], "hi")
        self.j.flush()
        rows = _rows(self.db)
        self.assertEqual(rows[0]["tool_name"], "kimi-k3")
        self.assertIn("total_tokens", rows[0]["detail_json"])

    def test_llm_usage_tokens_structured_object_and_dict(self):
        """P3.5: usage is captured both as raw repr and as structured ints in
        detail.usage_tokens, for attribute-shaped and dict-shaped usage."""
        import json

        class _FakeUsage:
            def __init__(self):
                self.prompt_tokens = 100
                self.completion_tokens = 88
                self.total_tokens = 188
                self.cost_usd = 0.42

        def fake_llm_obj(request):
            class _Resp:
                usage = _FakeUsage()
            return _Resp()

        def fake_llm_dict(request):
            return {"usage": {"prompt_tokens": 381007, "completion_tokens": 188,
                              "total_tokens": 381195, "cost_usd": 1.5}}

        # Object-shaped usage (OpenAI CompletionUsage style)
        envelope.llm_execution_envelope(
            request={"model": "obj-model"}, next_call=fake_llm_obj)
        # Dict-shaped usage
        envelope.llm_execution_envelope(
            request={"model": "dict-model"}, next_call=fake_llm_dict)
        self.j.flush()
        rows = _rows(self.db)
        by_model = {r["tool_name"]: json.loads(r["detail_json"]) for r in rows}

        obj_detail = by_model["obj-model"]
        self.assertEqual(obj_detail["usage_tokens"], {
            "prompt": 100, "completion": 88, "total": 188, "cost_usd": 0.42})
        for v in (obj_detail["usage_tokens"]["prompt"],
                  obj_detail["usage_tokens"]["completion"],
                  obj_detail["usage_tokens"]["total"]):
            self.assertIsInstance(v, int)
        self.assertIsInstance(obj_detail["usage_tokens"]["cost_usd"], float)
        # Raw repr preserved alongside structured capture
        self.assertIn("usage", obj_detail)
        self.assertIsInstance(obj_detail["usage"], str)
        self.assertIn("_FakeUsage", obj_detail["usage"])

        dict_detail = by_model["dict-model"]
        self.assertEqual(dict_detail["usage_tokens"], {
            "prompt": 381007, "completion": 188, "total": 381195,
            "cost_usd": 1.5})
        for v in (dict_detail["usage_tokens"]["prompt"],
                  dict_detail["usage_tokens"]["completion"],
                  dict_detail["usage_tokens"]["total"]):
            self.assertIsInstance(v, int)
        self.assertIn("usage", dict_detail)
        self.assertIn("381007", dict_detail["usage"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
