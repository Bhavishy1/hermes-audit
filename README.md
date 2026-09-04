# hermes-audit

Append-only audit trail for [Hermes Agent](https://hermes-agent.nousresearch.com/docs). Every action the agent takes — tool calls, LLM calls with token usage, skill writes, automations, subagents, approvals, messages, and blocked tool attempts — is journaled to a local, tamper-evident, queryable SQLite database.

**What you get:**

- **Live Activity feed** — a real-time, filterable feed of everything the agent does, in the Hermes desktop app (`/activity`) and the web dashboard, with per-turn grouping and click-to-expand detail. The read API covers scripts and programmatic audits.
- **Tamper-evident integrity** — every row is hash-chained (SHA-256) so edits, deletions, or reordering are detectable; crash recovery marks actions interrupted mid-flight; a failed commit is retained and retried rather than silently dropped.
- **Blocked-attempt visibility** — tool attempts are journaled *before* execution, so calls the agent tried and was denied are on the trail alongside the ones that ran.

## What it captures

| What | How | Action type |
|---|---|---|
| Tool calls (core tools, MCP calls, automations/cron, subagent tools) | `tool_execution` middleware envelope | `tool_call` |
| LLM API calls (model, duration, `usage_tokens`: prompt/completion/total, `cost_usd` when the provider reports it) | `llm_execution` middleware envelope | `llm_call` |
| Skill writes (self-learning via `skill_manage`) | tool envelope, typed `skill_write` | `skill_write` |
| Subagent spawns/stops | `subagent_start` / `subagent_stop` hooks, actor `subagent:<id>` | `tool_call` |
| Approval requests + user decisions | `pre_approval_request` / `post_approval_response` hooks | `approval_request` / `approval_granted` |
| The assistant's final reply per turn (first 500 chars) | `post_llm_call` hook | `message` |
| Blocked / attempted tool calls before execution (marked `phase: "pre"`, `blocked` flag) | `pre_tool_call` observer hook (fail-open — it can never veto a tool) | `tool_call` |
| Session lifecycle | `on_session_start` / `on_session_end` hooks | `session_start` / `session_end` |

Automations and cron jobs are captured as the tool calls they execute — there is no separate automation action type.

## Traceability features

- **Write-ahead journal** — `begin()` inserts a `pending` row before the action runs; `complete()` updates it with outcome, duration, and redacted detail. The agent's hot path never blocks on SQLite (single daemon writer thread, WAL mode, batched commits every 50 ops or 0.5 s).
- **Hash-chain tamper evidence** — each row's `event_hash` is a SHA-256 over its INSERT-time fields plus the previous row's `event_hash`. Editing, deleting, or reordering rows breaks the chain; `verify_chain()` detects it and reports the first broken `seq`. (The chain covers INSERT-time state; the later outcome update is not re-hashed.)
- **Crash recovery** — a `pending` row left by a dead process is marked `interrupted` (terminal) at next journal startup, so a crash is distinguishable from a still-running action (`query.pending()` = live only, `query.interrupted()` = recovered crashes).
- **Durable seq** — per-session sequence numbers survive restarts: the counter is seeded from `MAX(seq)` in the DB on first use of each session, so ordering by `(ts_utc, seq)` is stable across processes.
- **Retain-on-failure** — ops whose commit fails go to a bounded retry buffer (500) and are retried before new ops; if the buffer would overflow, the journal marks itself unhealthy and fails closed rather than dropping events.
- **Fail-closed** — if the writer thread dies, `is_healthy()` returns False and journaling calls raise, so the middleware does not proceed unaudited. Conversely, every hook/envelope wraps journaling in try/except so a broken journal never breaks the agent.
- **Coverage health** — `journal.stats()` (writer alive, queue size, per-action-type counts, last event ts) and `AuditQuery.coverage(session_id)` (heuristic reconciliation: tool calls with no preceding `llm_call` in the same trace are flagged `suspicious` for review — not proof of tampering).
- **Redaction by default** — detail payloads are scrubbed before they hit disk: keys matching a denylist (`api_key`, `token`, `secret`, `password`, `authorization`, `credential`) become `[REDACTED]` (numeric token *counts* under `usage_tokens` are allowlisted), and any string over 2000 chars is truncated.

## Human-readable summaries

Every event gets a deterministic one-line `human_summary` generated at log time from `(action_type, tool_name, args)` — zero LLM cost, zero hallucination risk. Unknown tools fall back to `Used <tool_name>`; the read layer derives `display_summary` by appending the outcome (`(failed)`, `(running)`, `(interrupted)`).

## How it works

```
 Hermes agent process
 ┌────────────────────────────────────────────────────────────┐
 │  middleware envelopes            lifecycle hooks           │
 │  ┌───────────────────┐   ┌──────────────────────────────┐  │
 │  │ tool_execution    │   │ pre_tool_call (attempts,     │  │
 │  │ llm_execution     │   │   blocked/vetoed calls)      │  │
 │  │ (skill_write via  │   │ session/subagent/approval/   │  │
 │  │  tool envelope)   │   │ message (post_llm_call)      │  │
 │  └────────┬──────────┘   └───────────────┬──────────────┘  │
 │           │  begin() / complete()  (async, fail-closed)   │
 │           ▼                               ▼                │
 │        ┌─────────────────────────────────────┐            │
 │        │ AuditJournal — queue → writer thread│            │
 │        │ redact → hash-chain → batch commit  │            │
 │        └──────────────────┬──────────────────┘            │
 └───────────────────────────┼───────────────────────────────┘
                             ▼
              $HERMES_HOME/audit.db  (SQLite, WAL)

 Read side (read-only, short-lived connections; never blocks the writer)
 ┌─────────────────────┐   ┌────────────────────────────────────┐
 │ AuditQuery (query.py)│   │ dashboard/plugin_api.py (FastAPI)  │
 │ recent/by_trace/     │◄──┤ /api/plugins/hermes-audit/...      │
 │ grouped/coverage/    │   │ + WS /events/stream (0.3 s poll)   │
 │ purge_older_than     │   └──────────────┬─────────────────────┘
 └─────────────────────┘                  ▼
                            ┌──────────────────────────────┐
                            │ UIs: desktop Activity page    │
                            │ (/activity, grouped feed) +   │
                            │ web dashboard Activity tab    │
                            └──────────────────────────────┘
```

## The UIs

- **Desktop Activity page** (Hermes desktop app, route `/activity`): a live, filterable feed. *Grouped* view rolls the last 300 events into per-turn request groups (by `trace_id`) with outcome badges, action counts, total duration, and summed token usage; *Flat* view is a date-grouped list. Type/actor/outcome filters, substring search over tool name/detail/summary, click-to-expand per event (full detail JSON, provenance, token line, causal trace chain), "Load more" paging. Live updates arrive over the `/events/stream` WebSocket with a polling fallback; also reachable via the palette command "Open Activity (Audit Trail)".

![Grouped view of the desktop Activity page with day sections expanded, showing per-turn cards with outcome badges and durations](docs/screenshots/activity-grouped.png)

![An expanded turn-group listing its tool calls, the collapsed LLM summary line, and the token-usage chip](docs/screenshots/turn-group-expanded.png)

![A single event expanded to its key/value grid and full detail JSON](docs/screenshots/event-detail.png)

![The filters bar with the day picker and action-type dropdown open](docs/screenshots/filters-bar.png)

![Flat view: the date-grouped list of individual events](docs/screenshots/activity-flat.png)

- **Web dashboard feed** (dashboard plugin, tab `/activity`): same data through the plugin's REST API, grouped or flat.

## Reading the journal programmatically

```python
from query import AuditQuery

q = AuditQuery("/Users/you/.hermes/audit.db")   # $HERMES_HOME/audit.db

q.recent(limit=50, before=ts_cursor)   # newest-first page
q.by_trace(trace_id)                   # full action tree for one request
q.by_actor("assistant")                # per-actor feed
q.by_source("https://example.com")     # provenance substring match
q.pending() / q.interrupted()          # live vs recovered-crash rows
q.grouped(limit_groups=20)             # per-turn request groups (rollups + events)
q.coverage(session_id)                 # journaling-coverage heuristic for a session
q.export("audit.json", since=ts)       # JSON dump for portability
q.purge_older_than(days=365)           # retention: dry_run=True (default) previews only
```

The journal itself exposes `verify_chain()`, `stats()`, `flush()`, and `close()`.

## REST API (dashboard plugin, mounted at `/api/plugins/hermes-audit/`)

| Route | Purpose |
|---|---|
| `GET /health` | Liveness, journal existence, live `stats()` (or read-only fallback) |
| `GET /coverage?session_id=` | Coverage reconciliation for one session |
| `GET /events` | Newest-first page; filters `action_type`, `actor`, `outcome`, `trace_id`, `q`; `before_id` cursor paging |
| `GET /events/{event_id}` | Full event detail + causal chain neighbours |
| `GET /groups` | Per-turn request groups (last 300 events rolled up) |
| `GET /groups/{trace_id}` | One group's rollup + all events (singleton groups addressable as `event:<event_id>`) |
| `GET /traces/{trace_id}` | Full action tree for one trace |
| `GET /stats` | Totals by type/outcome, pending/interrupted counts, top tools |
| `GET /meta/filters` | Distinct values for filter dropdowns |
| `POST /retention/purge` | `{"days": N, "dry_run": true|false}` — the only mutating route |
| `WS /events/stream?since=<rowid>&token=` | Live event tail (0.3 s WAL poll) |

## Verifying the trail

```python
from journal import AuditJournal

journal = AuditJournal("/Users/you/.hermes/audit.db")
print(journal.verify_chain())
```

On an intact trail:

```python
{'valid': True, 'length': 1234, 'first_break_seq': None, 'reason': None}
```

After any edit, deletion, reordering, or re-insertion, `valid` is `False`, `first_break_seq` points at the first broken row, and `reason` says whether the row's *content* was modified after INSERT (`event_hash` mismatch) or the *chain link* is broken (`prev_hash` does not match the previous row's `event_hash`). Verification runs on its own read-only connection and never touches the writer.

## Install

hermes-audit is a **standalone plugin** — you drop it into your Hermes plugins directory and enable it. No core changes, no pip install.

> **Fast path:** if you just want to use it (not modify it), `hermes plugins install Bhavishy1/hermes-audit` does the clone + enable in one step. The manual steps below are for hacking on the source.

**Quick start (default profile):**

```bash
# 1. Clone into the Hermes plugins directory
git clone https://github.com/Bhavishy1/hermes-audit ~/.hermes/plugins/hermes-audit

# 2. Enable it (adds hermes-audit to plugins.enabled in config.yaml)
hermes config set plugins.enabled "$(hermes config get plugins.enabled),hermes-audit"

# 3. Restart the backend (desktop app or `hermes` gateway) — the middleware
#    registers when a session starts
```

The journal is created at `$HERMES_HOME/audit.db` (falls back to `~/.hermes/audit.db`) on the first agent action after restart. Open the **Activity** page in the desktop app (⌘K → "Reload desktop plugins" if it doesn't appear) or the web dashboard's `/activity` tab to watch the feed live.

**Using a named profile** (e.g. `hermes -p myprofile`): the *middleware* (the journal writer) is discovered from the profile's own plugins dir, while the *desktop/web UI* is discovered from the root `~/.hermes/plugins/`. So link it into both:

```bash
# The clone from step 1 already covers the root plugins dir (drives the UI).
# Also link it into your profile's plugins dir (drives the middleware/journal):
mkdir -p ~/.hermes/profiles/myprofile/plugins
ln -s ~/.hermes/plugins/hermes-audit ~/.hermes/profiles/myprofile/plugins/hermes-audit
```

Then enable and restart as above. (If you only ever use the default profile, the quick-start clone is all you need.)

**What gets loaded:**

1. **Agent plugin (the journal):** `plugin.yaml` + `__init__.py` at the repo root. Hermes calls `register(ctx)`, which wires the middleware envelopes (tool + LLM execution) and lifecycle hooks.
2. **Desktop Activity page:** `desktop-plugin/plugin.js` (id `hermes-audit`).
3. **Web dashboard feed:** `dashboard/` (`manifest.json` + `plugin_api.py`, tab `/activity`).

**Schema migrations** are automatic and idempotent — existing `audit.db` files from older versions are upgraded in place on open.

**Requirements:** a running Hermes Agent (no extra Python dependencies — the journal uses only the standard library).

## Tests

`tests/` — 70 tests across 5 files (journal, query, envelope, hooks, summarize).

## License

MIT — see [LICENSE](LICENSE).
