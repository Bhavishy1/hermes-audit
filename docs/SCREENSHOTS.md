# Screenshots

Screenshots for the README live in `docs/screenshots/`. Drop the captured PNGs in with the exact filenames below and the README image embeds will work as-is — no edits needed.

| # | Filename | What to show | Where it embeds |
|---|----------|--------------|-----------------|
| 1 | `docs/screenshots/activity-grouped.png` | **Grouped view with day sections expanded.** Desktop Activity page (`/activity`) in *Grouped* view with at least two day sections expanded, showing several turn-cards (per-turn request groups) with their outcome badges, action counts, and durations. | [The Activity UIs](../README.md#the-uis) — right after the desktop Activity page bullet. |
| 2 | `docs/screenshots/turn-group-expanded.png` | **One expanded turn-group.** A single turn-card opened up: its `tool_call` events listed, the LLM call shown collapsed as a one-line summary, and the token chip visible (summed prompt/completion/total usage for the turn). | [The Activity UIs](../README.md#the-uis) — immediately after shot 1. |
| 3 | `docs/screenshots/event-detail.png` | **Expand-to-detail of a single event.** One event clicked open: the key/value grid (tool name, outcome, duration, actor, trace id…) plus the full detail JSON below it. | [The Activity UIs](../README.md#the-uis) — after shot 2. |
| 4 | `docs/screenshots/filters-bar.png` | **Filters bar with pickers open.** The filter row with the day picker and model/action-type dropdown expanded, showing the available filter values. | [The Activity UIs](../README.md#the-uis) — after shot 3. |
| 5 | `docs/screenshots/activity-flat.png` | **Flat view.** The same feed switched to *Flat* view: the date-grouped list of individual events (no turn grouping). | [The Activity UIs](../README.md#the-uis) — after shot 4. |

## Capture tips

- Size for readability: capture the Activity pane at a normal window width (~1200–1400 px). Full-width is fine; the README renders images at up to ~800 px wide.
- Include a realistic mix: several tool calls, at least one LLM call with token usage, and ideally one failed or blocked event so the outcome badges read well.
- Avoid capturing real secrets in detail JSON — the journal redacts `api_key`/`token`/`password`-style keys automatically, but check before saving.
