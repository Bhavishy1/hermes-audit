# hermes-audit — Usage Guide

Practical guide to installing, reading, querying, verifying, and maintaining the hermes-audit trail. For architecture and design rationale, see [README.md](../README.md) and [AUDIT.md](../AUDIT.md).

## 1. Install and enable

### Agent plugin (the journal — required)

```bash
ln -s "/path/to/hermes-audit" ~/.hermes/plugins/hermes-audit
```

(or copy the directory). On the next Hermes start, `__init__.py`'s `register()` wires everything automatically:

- `tool_execution` and `llm_execution` middleware envelopes (tool calls, LLM calls with token usage, skill writes),
- lifecycle hooks: `on_session_start`, `on_session_end`, `subagent_start`, `subagent_stop`, `pre_approval_request`, `post_approval_response`, `pre_tool_call` (blocked/attempted calls), `post_llm_call` (the assistant's final reply).

The journal writes to `$HERMES_HOME/audit.db` (fallback `~/.hermes/audit.db`), SQLite in WAL mode. There is nothing else to configure; migrations run automatically on open.

### UIs (optional but recommended)

- **Desktop Activity page:** ships as the desktop plugin in `desktop-plugin/plugin.js` — adds an "Activity" entry to the sidebar (route `/activity`) and the palette command "Open Activity (Audit Trail)".
- **Web dashboard feed:** the `dashboard/` directory (`manifest.json` + `plugin_api.py`) provides the tab `/activity` plus the REST API under `/api/plugins/hermes-audit/`.

Verify it's working: check the Hermes log for `hermes-audit: journal created at ...`, or open the Activity page — stat chips (total, per-type counts) should populate.

## 2. Reading the Activity page

The desktop page (route `/activity`) has two view modes:

- **Grouped** (default) — the last 300 events are rolled up into per-turn **request groups** keyed by `trace_id`. Each group card shows the start time, a title (the first tool call's summary), an action count, total duration, summed token usage, and an outcome badge (`success` / `error` / `pending`). Expand a group to see its individual events: non-LLM events as cards, then a dashed line summarizing the model calls ("N model calls · Xs total").
- **Flat** — a plain list grouped by date, one card per event with time, title, outcome badge, duration, and a one-line preview.

Common affordances (both views):

- **Filters** — three dropdowns: type (e.g. `tool_call`, `llm_call`), actor, outcome. Filtering is applied server-side on reload and client-side to the live stream and inside groups; empty groups are dropped.
- **Search** — substring match over tool name, detail JSON, and summaries (press Enter or click Search).
- **Expand-to-detail** — click any event card to load its full record: action/tool/actor/outcome/duration/trace/session/seq, a token-usage line for LLM calls ("tokens: 1.2k in / 380 out (1.6k total)"), the pretty-printed `detail` and `provenance` JSON, and the causal **trace chain** — every event in the same `trace_id`, with the current one outlined.
- **Live updates** — a status dot in the header shows *Live* (WebSocket) or *Polling*; new events appear at the top.
- **Paging** — "Load more" fetches the next 50 via the `before_id` cursor.

Outcome colors: green = success, red = error, yellow = pending (still running), gray = interrupted (recovered from a crash).

## 3. Querying programmatically

```python
import sys; sys.path.insert(0, "/path/to/hermes-audit")
from query import AuditQuery

q = AuditQuery("/Users/you/.hermes/audit.db")
```

All methods return plain dicts and open short-lived **read-only** connections — safe to run while the agent is writing.

```python
# Newest-first page (paging: pass a ts_utc cursor from the previous call)
q.recent(limit=50)
q.recent(limit=50, before="2026-09-04T12:00:00Z")

# Everything that happened in one user request
q.by_trace("sess-abc:task-1:9f2e")            # ordered ts_utc, seq

# Filter by actor (e.g. "assistant", "subagent:<id>")
q.by_actor("assistant", limit=100)

# Events whose provenance mentions a URI
q.by_source("https://example.com/doc")

# Live vs recovered-crash rows
q.pending()        # genuinely still running
q.interrupted()    # pending when a prior process died

# Request groups: per-turn rollups with nested event lists
for g in q.grouped(limit_groups=20):
    print(g["title"], g["outcome"], g["event_count"],
          g["tool_call_count"], g["llm_call_count"], g["total_duration_ms"])
    for ev in g["events"]:
        print("  ", ev["display_summary"])   # human_summary + outcome suffix

# Coverage reconciliation for a session (heuristic, not proof)
q.coverage("sess-abc")
# {'session_id': ..., 'counts': {...}, 'tool_calls': 12, 'llm_calls': 10,
#  'tool_calls_without_preceding_llm_call': 2, 'suspicious': False}

# Portable JSON dump
q.export("audit-export.json", since="2026-09-01T00:00:00Z")
```

Notes:

- `display_summary` is derived at read time (`human_summary` + ` (failed)` / ` (running)` / ` (interrupted)`); the raw `human_summary` is never mutated.
- `grouped()` puts NULL-trace events into singleton groups with synthetic ids `"event:<event_id>"`.
- `coverage()` flags review candidates — it can false-positive, so `suspicious` means "look into it", not "tampering proven".

## 4. Verifying integrity

The hash chain makes the journal tamper-evident: each row's `event_hash` is SHA-256 over its INSERT-time fields plus the previous row's `event_hash`.

```python
import sys; sys.path.insert(0, "/path/to/hermes-audit")
from journal import AuditJournal

journal = AuditJournal("/Users/you/.hermes/audit.db")
print(journal.verify_chain())
```

Intact trail:

```python
{'valid': True, 'length': 1234, 'first_break_seq': None, 'reason': None}
```

Broken trail (row edited, deleted, reordered, or re-inserted):

```python
{'valid': False, 'length': 1234, 'first_break_seq': 87,
 'reason': "row 'abc123' event_hash mismatch: row content was modified after INSERT (tampering?)"}
```

`reason` distinguishes the two failure modes: an `event_hash` mismatch (content edited after INSERT) vs a `prev_hash` mismatch (chain link broken — deletion/reordering). Verification reads on its own read-only connection; it never touches the writer and is safe to run anytime. Caveat: the chain covers INSERT-time state, so the later `complete()` update (outcome/duration/detail) is intentionally not re-hashed — the chain proves *what was recorded when*, not the final outcome.

## 5. Retention — purging old events

By default nothing is ever deleted. To enforce storage-limitation:

```python
q.purge_older_than(days=365)                  # dry run (default): counts only
# {'would_delete': 512, 'cutoff_ts': '2025-09-04T...Z'}
q.purge_older_than(days=365, dry_run=False)   # actually deletes
# {'deleted': 512, 'cutoff_ts': '2025-09-04T...Z'}
```

- Deletion is by `created_at` (row-insert time), not event time.
- `dry_run=True` is the default on purpose — always preview first.
- Through the dashboard REST API it is the only mutating route: `POST /api/plugins/hermes-audit/retention/purge` with body `{"days": 365, "dry_run": false}`.

## 6. Troubleshooting

**Plugin not appearing / hooks not registering**

- Confirm the plugin directory is at `~/.hermes/plugins/hermes-audit/` (or your `$HERMES_HOME`) and contains `__init__.py`, `plugin.yaml`.
- Check the Hermes log: a healthy load logs `hermes-audit: journal created at ...` plus one `registered middleware/hook ...` line per registration. A partial failure logs a warning but does not abort plugin load — e.g. `registering hook X failed` means that hook alone is inactive.
- `register()` bails out early with `failed to create AuditJournal` if the DB can't be opened (permissions, unwritable directory) — fix the path/permissions and restart.

**Activity page missing**

- Desktop: the page registers at route `/activity` with a sidebar entry and palette command ("Open Activity (Audit Trail)"). If the sidebar row is absent, the desktop plugin (`desktop-plugin/`) isn't loaded.
- Dashboard: confirm `dashboard/manifest.json` is discoverable and `dashboard/dist/index.js` is built (`dist/` present); the API routes come from `dashboard/plugin_api.py`.

**Journal not writing / events missing**

- `pre_tool_call` events (tool *attempts*) are marked `phase: "pre"` in their detail and are separate rows from the executed `tool_call` row — filter with the search box (`"phase"`) if they look like duplicates.
- Skill writes appear as `skill_write` (only from `skill_manage`); other skill tools are plain `tool_call`s.
- Cron/automation runs only land when their tools execute through the middleware — check the session the job ran under.
- Verify the writer is alive: the `GET /health` dashboard route reports `journal.writer_alive`, `queue_size`, and `last_event_ts` (in-process journal), or a read-only fallback (`writer_alive: null`) when the dashboard runs in a separate process.

**Suspected data loss after a crash**

- Rows stuck `pending` from a dead process are marked `interrupted` at next startup — check `q.interrupted()` / the gray outcome badge before assuming loss.
- If the trail shows fewer `llm_call` rows than expected for a session, run `q.coverage(session_id)`; `suspicious: true` flags tool calls with no preceding model call for review.

**Chain verification fails**

- `valid: false` with an `event_hash` mismatch means a row's content was modified after INSERT; a `prev_hash` mismatch means rows were deleted/reordered. Inspect `first_break_seq` — everything after the break is also unverifiable, but rows before it remain trustworthy.
- If the DB file itself is unreadable, `verify_chain()` returns `{'valid': False, 'reason': 'cannot open database for chain verification'}` — check file permissions first.
