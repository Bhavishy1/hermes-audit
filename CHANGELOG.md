# Changelog

## 0.3.0

Initial packaged release of the hermes-audit plugin.

- **Append-only audit journal** — every agent action (tool calls, LLM calls, skill writes, subagents, approvals, messages, session lifecycle) is written to a local SQLite database (`$HERMES_HOME/audit.db`, WAL mode) via a write-ahead journal: a `pending` row is inserted before the action runs and completed with outcome, duration, and redacted detail afterward. The agent's hot path never blocks on SQLite (single daemon writer thread, batched commits).
- **Hash-chain tamper evidence** — each row's `event_hash` is a SHA-256 over its INSERT-time fields plus the previous row's hash; `verify_chain()` detects edits, deletions, or reordering and reports the first broken sequence number.
- **Interrupted-action recovery** — `pending` rows left behind by a dead process are marked `interrupted` on next startup, so a crash is distinguishable from a still-running action.
- **Retain-on-failure** — commits that fail go to a bounded retry buffer and are retried before new ops; if the buffer would overflow, the journal marks itself unhealthy and fails closed rather than dropping events.
- **Coverage health** — `journal.stats()` reports writer liveness, queue size, and per-type counts; `AuditQuery.coverage(session_id)` flags tool calls with no preceding LLM call in the same trace as suspicious for review.
- **Grouped / day-section Activity UI** — desktop Activity page (`/activity`) and web dashboard tab roll the last 300 events into per-turn groups with outcome badges, action counts, durations, and token totals; day sections and a flat date-grouped view; type/actor/outcome filters, search, click-to-expand detail (full JSON, provenance, causal trace chain), and live updates over a WebSocket stream with polling fallback.
- **Usage split per turn** — LLM calls record `usage_tokens` (prompt / completion / total) and `cost_usd` when the provider reports it; turn groups sum them into a token chip.
- **Retention** — `purge_older_than(days, dry_run)` for pruning old events (dry-run by default); exposed as the only mutating REST route.
