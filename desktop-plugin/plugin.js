// hermes-audit — Activity feed desktop plugin.
// A page (route /activity) + sidebar nav row + palette command that renders a
// live, filterable feed of the audit journal via the plugin's Python backend
// (/api/plugins/hermes-audit). Read-only.
import { jsx } from "react/jsx-runtime";
import { useEffect, useState, useRef, useCallback } from "react";
import {
  host, Button, Input, Select, SelectTrigger, SelectValue, SelectContent, SelectItem,
  Badge, StatusDot, ScrollArea, EmptyState, Codicon, cn,
} from "@hermes/plugin-sdk";

const ROUTES_AREA = "routes";
const SIDEBAR_NAV_AREA = "sidebar.nav";
const PALETTE_AREA = "palette";

function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  if (isNaN(d)) return ts;
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
function fmtDate(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  if (isNaN(d)) return "";
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}
function titleFor(ev) {
  const t = ev.tool_name || ev.action_type || "event";
  switch (ev.action_type) {
    case "llm_call": return "LLM call" + (ev.tool_name ? " · " + ev.tool_name : "");
    case "tool_call": return "Tool · " + t;
    case "skill_write": return "Learned skill · " + t;
    case "message": return "Assistant message";
    case "session_start": return "Session started";
    case "session_end": return "Session ended";
    default:
      return (ev.action_type || "").indexOf("approval") === 0 ? "Approval · " + t : t;
  }
}
function previewText(ev) {
  try {
    if (!ev.detail_json) return "";
    const d = JSON.parse(ev.detail_json);
    return scrubPath(d.text || d.args_summary || d.result_summary || (d.error ? "Error: " + d.error : "") || (d.model || ""));
  } catch (e) { return ""; }
}
function pretty(s) { try { return scrubPath(JSON.stringify(JSON.parse(s), null, 2)); } catch (e) { return scrubPath(s); } }
function fmtDur(ms) {
  if (ms == null || isNaN(ms)) return "";
  if (ms < 1000) return Math.round(ms) + "ms";
  return (ms / 1000).toFixed(1) + "s";
}
// P3.5: format a token count compactly — 188 -> "188", 381007 -> "381k", 1500000 -> "1.5M".
function fmtTok(n) {
  if (n == null || isNaN(n)) return "";
  n = Number(n);
  if (Math.abs(n) >= 1e6) {
    const m = n / 1e6;
    return (Math.abs(m) >= 100 ? String(Math.round(m)) : m.toFixed(1).replace(/\.0$/, "")) + "M";
  }
  if (Math.abs(n) >= 1e3) return Math.round(n / 1e3) + "k";
  return String(Math.round(n));
}
// P3.5: parse usage_tokens ({prompt, completion, total, ...}) out of an event's
// detail_json. Returns null when absent, malformed, or when detail_json is only
// a raw repr (not valid JSON) — callers then render nothing extra.
function usageTokensOf(ev) {
  if (!ev || !ev.detail_json) return null;
  try {
    const d = JSON.parse(ev.detail_json);
    const u = d && d.usage_tokens;
    if (u && typeof u === "object" && u.total != null) return u;
  } catch (e) { /* raw repr — no structured usage */ }
  return null;
}
// P3.5: sum usage_tokens.total across a group's llm_call events (null if none carry usage).
function sumTokenTotal(events) {
  let s = 0, any = false;
  (events || []).forEach((ev) => {
    const u = usageTokensOf(ev);
    if (u && u.total != null) { s += Number(u.total) || 0; any = true; }
  });
  return any ? s : null;
}
// Sum usage_tokens prompt/completion separately across a list's llm_call
// events. Returns { inp, outp } only when at least one event carries a real
// prompt/completion split; null otherwise (callers fall back to total-only).
function fmtInOut(events) {
  let inp = 0, outp = 0, any = false;
  (events || []).forEach((ev) => {
    if (ev.action_type !== "llm_call") return;
    const u = usageTokensOf(ev);
    if (!u) return;
    if (u.prompt == null && u.completion == null) return;
    any = true;
    inp += Number(u.prompt) || 0;
    outp += Number(u.completion) || 0;
  });
  return any ? { inp, outp } : null;
}
// "161k in / 459 out" when a prompt/completion split exists, else total-only
// "621k tok", else null (no usage data at all).
function fmtInOutLabel(events) {
  const split = fmtInOut(events);
  if (split) return fmtTok(split.inp) + " in / " + fmtTok(split.outp) + " out";
  const tot = sumTokenTotal(events);
  return tot != null ? fmtTok(tot) + " tok" : null;
}
function tsMs(ts) {
  if (!ts) return null;
  const d = new Date(ts);
  return isNaN(d) ? null : d.getTime();
}
// Privacy: scrub absolute home paths (e.g. /Users/<name>/foo) and bare
// usernames from text at RENDER time only — stored journal data is untouched.
// Multiple passes: "~/Users/<name>" collapse, "/Users/<name>" -> "~", bare
// "Users/<name>" -> "~", then any known bare username -> "~". Usernames are
// discovered from the string itself plus the runtime env when available.
// Best-effort: never throws on non-string input.
function scrubPath(s) {
  try {
    if (typeof s !== "string") return s;
    const NAME = "([^/\\s\"':`,)\\]}]+)";
    // Pass 0: discover candidate usernames from any Users/<name> occurrence.
    const names = new Set();
    const re0 = new RegExp("(?:^|[^A-Za-z])Users/" + NAME, "g");
    let m0;
    while ((m0 = re0.exec(s)) !== null) names.add(m0[1]);
    try {
      if (typeof process !== "undefined" && process.env) {
        const u = process.env.USER || (process.env.HOME ? process.env.HOME.split("/").pop() : "");
        if (u) names.add(u);
      }
    } catch (e) { /* browser context — env unavailable */ }
    // Pass 1: "~/Users/<name>" -> "~" (before the generic pass, so a leading
    // "~" is not doubled into "~~").
    let out = s.replace(new RegExp("~\\/Users\\/" + NAME, "g"), "~");
    // Pass 2: "/Users/<name>" -> "~"
    out = out.replace(new RegExp("\\/Users\\/" + NAME, "g"), "~");
    if (names.size) {
      // Pass 3: bare "Users/<name>" (known names only) -> "~"
      out = out.replace(new RegExp("(^|[\\s\"'(\\[=:;])Users\\/" + NAME, "g"), (mm, pre, name) =>
        names.has(name) ? pre + "~" : mm);
      // Pass 4: bare username anywhere (known names only) -> "~"
      names.forEach((name) => {
        if (!name) return;
        const re = new RegExp("(^|[^A-Za-z0-9_])" + name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "(?![A-Za-z0-9_])", "g");
        out = out.replace(re, (mm, pre) => pre + "~");
      });
    }
    return out;
  } catch (e) { return s; }
}

const COLOR = {
  ok: "var(--ui-success, #2ecc71)",
  err: "var(--ui-danger, #e74c3c)",
  warn: "var(--ui-warning, #f1c40f)",
  muted: "var(--ui-text-quaternary)",
  info: "var(--ui-info, #3498db)",
  accent: "var(--ui-accent)",
};
function OutcomeBadge({ outcome }) {
  const o = outcome || "unknown";
  // P4.5: live 'pending' (still running) renders warning-yellow; terminal
  // 'interrupted' (recovered crash) renders muted gray so they read differently.
  const color = o === "success" ? COLOR.ok
    : o === "error" ? COLOR.err
    : o === "pending" ? COLOR.warn
    : o === "interrupted" ? COLOR.muted
    : COLOR.muted;
  return jsx("span", {
    className: "rounded-md border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide",
    style: { borderColor: color, color },
    children: o,
  });
}

function StatChip({ label, val }) {
  return jsx("span", {
    className: "inline-flex items-baseline gap-1.5 rounded-md border px-2.5 py-1 text-xs",
    style: { borderColor: "var(--ui-stroke-secondary)", background: "var(--ui-bg-secondary, transparent)" },
    children: [
      jsx("span", { className: "font-semibold", style: { color: "var(--ui-accent)" }, children: String(val), key: "n" }),
      jsx("span", { style: { color: "var(--ui-text-secondary)" }, children: label, key: "l" }),
    ],
  });
}

function EventDetail({ ctx, eventId }) {
  const [detail, setDetail] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => {
    let live = true;
    ctx.rest("/events/" + encodeURIComponent(eventId))
      .then((r) => { if (live) setDetail(r); })
      .catch((e) => { if (live) setErr(String(e)); });
    return () => { live = false; };
  }, [eventId]);
  if (err) return jsx("div", { className: "text-xs", style: { color: "var(--ui-text-secondary)" }, children: err });
  if (!detail) return jsx("div", { className: "text-xs", style: { color: "var(--ui-text-secondary)" }, children: "Loading…" });
  const ev = detail.event || {};
  const kv = [
    ["action", ev.action_type], ["tool", ev.tool_name], ["actor", ev.actor],
    ["outcome", ev.outcome], ["duration", ev.duration_ms != null ? ev.duration_ms + "ms" : null],
    ["trace", ev.trace_id], ["session", ev.session_id], ["seq", ev.seq],
  ].filter((p) => p[1] != null && p[1] !== "");
  // Privacy: scrub home paths from the human-readable values at render time.
  kv.forEach((p) => { if (typeof p[1] === "string") p[1] = scrubPath(p[1]); });
  // P3.5: token-usage line for llm_call events. Renders nothing when the row
  // carries no structured usage_tokens (e.g. only a raw repr of the detail).
  const usage = ev.action_type === "llm_call" ? usageTokensOf(ev) : null;
  const tokenLine = usage && usage.total != null
    ? "tokens: " + fmtTok(usage.prompt) + " in / " + fmtTok(usage.completion) + " out (" + fmtTok(usage.total) + " total)"
    : null;
  return jsx("div", { className: "mt-2 space-y-2 border-t pt-2", style: { borderColor: "var(--ui-stroke-secondary)" }, children: [
    jsx("div", { className: "grid gap-x-3 gap-y-0.5 text-xs", style: { gridTemplateColumns: "auto 1fr" }, children:
      kv.map((p) => [
        jsx("span", { className: "font-medium", style: { color: "var(--ui-text-secondary)" }, children: p[0], key: p[0] + "k" }),
        jsx("span", { className: "break-all", children: String(p[1]), key: p[0] + "v" }),
      ]).flat(),
    }),
    tokenLine && jsx("div", {
      className: "flex items-center gap-1.5 text-xs",
      style: { color: "var(--ui-text-secondary)", fontVariantNumeric: "tabular-nums" },
      children: [
        jsx(Codicon, { name: "symbol-variable", size: 13, key: "ti", style: { color: "var(--ui-text-secondary)", flexShrink: 0 } }),
        jsx("span", { children: tokenLine, key: "tt" }),
      ],
    }),
    ev.detail_json && jsx("div", { children: [
      jsx("div", { className: "text-[11px] font-semibold uppercase tracking-wide", style: { color: "var(--ui-text-secondary)" }, children: "Detail" }),
      jsx("pre", { className: "mt-1 max-h-64 overflow-auto rounded-md border p-2 text-[11px]", style: { borderColor: "var(--ui-stroke-secondary)", background: "var(--ui-bg-secondary, transparent)" }, children: pretty(ev.detail_json) }),
    ], key: "d" }),
    ev.provenance_json && jsx("div", { children: [
      jsx("div", { className: "text-[11px] font-semibold uppercase tracking-wide", style: { color: "var(--ui-text-secondary)" }, children: "Provenance" }),
      jsx("pre", { className: "mt-1 max-h-64 overflow-auto rounded-md border p-2 text-[11px]", style: { borderColor: "var(--ui-stroke-secondary)", background: "var(--ui-bg-secondary, transparent)" }, children: pretty(ev.provenance_json) }),
    ], key: "p" }),
    detail.chain && detail.chain.length > 1 && jsx("div", { children: [
      jsx("div", { className: "text-[11px] font-semibold uppercase tracking-wide", style: { color: "var(--ui-text-secondary)" }, children: "Trace (" + detail.chain.length + " events)" }),
      jsx("div", { className: "mt-1 space-y-0.5", children: detail.chain.map((c) =>
        jsx("div", { className: cn("flex items-center gap-2 rounded px-1.5 py-0.5 text-xs", c.event_id === ev.event_id && "outline", "outline-1"), style: c.event_id === ev.event_id ? { outlineColor: "var(--ui-accent)" } : {}, children: [
          jsx("span", { style: { color: "var(--ui-text-secondary)", fontVariantNumeric: "tabular-nums" }, children: fmtTime(c.ts_utc), key: "t" }),
          jsx("span", { className: "flex-1", children: scrubPath((c.action_type || "") + (c.tool_name ? " · " + c.tool_name : "")), key: "n" }),
          jsx(OutcomeBadge, { outcome: c.outcome, key: "b" }),
        ], key: c.event_id })
      ) }),
    ], key: "c" }),
  ] });
}

function EventCard({ ctx, ev, expanded, onToggle }) {
  const border =
    ev.action_type === "message" ? COLOR.info :
    ev.action_type === "skill_write" ? "var(--ui-purple, #9b59b6)" :
    (ev.action_type === "session_start" || ev.action_type === "session_end") ? COLOR.muted :
    ev.outcome === "error" ? COLOR.err : COLOR.accent;
  return jsx("div", {
    className: "rounded-lg border transition-colors",
    style: { borderColor: expanded ? "var(--ui-accent)" : "var(--ui-stroke-secondary)", borderLeft: "3px solid " + border, background: "var(--ui-bg-secondary, transparent)" },
    children: [
      // Header: a real <button> — the reliable, keyboard-accessible click target.
      jsx("button", {
        type: "button",
        onClick: onToggle,
        className: "flex w-full cursor-pointer flex-wrap items-center gap-2 rounded-t-lg p-2.5 text-left hover:bg-[var(--ui-bg-tertiary,rgba(255,255,255,0.03))]",
        style: { background: "transparent", border: "none", font: "inherit", color: "inherit" },
        children: [
          jsx(Codicon, { name: expanded ? "chevron-down" : "chevron-right", size: 14, key: "chev", style: { color: "var(--ui-text-secondary)", flexShrink: 0 } }),
          jsx("span", { className: "text-xs", style: { color: "var(--ui-text-secondary)", fontVariantNumeric: "tabular-nums" }, children: fmtTime(ev.ts_utc), key: "t" }),
          jsx("span", { className: "flex-1 text-sm font-medium", children: scrubPath(String(titleFor(ev))), key: "n" }),
          jsx(OutcomeBadge, { outcome: ev.outcome, key: "b" }),
          ev.duration_ms != null && jsx("span", { className: "text-[11px]", style: { color: "var(--ui-text-secondary)" }, children: ev.duration_ms + "ms", key: "d" }),
        ],
      }, "head"),
      previewText(ev) && !expanded && jsx("div", { className: "truncate px-2.5 pb-2 pl-9 text-xs", style: { color: "var(--ui-text-secondary)" }, children: previewText(ev) }, "prev"),
      // Detail: stopPropagation so clicks inside never re-trigger the toggle.
      expanded && jsx("div", {
        onClick: (e) => { e.stopPropagation(); },
        className: "px-2.5 pb-2.5",
        children: jsx(EventDetail, { ctx, eventId: ev.event_id }),
      }, "det"),
    ],
  });
}

// Build per-turn request groups from a flat event list. Prefers the backend
// /groups contract (trace_id/start_ts/end_ts/...); falls back to client-side
// grouping by trace_id (NULL trace_id -> singleton keyed by event_id).
// Order: groups by latest event ts desc, events within a group by seq asc.
function buildGroups(serverGroups, events) {
  let out = null;
  if (serverGroups && serverGroups.length > 0) {
    out = serverGroups.map((g) => ({
      key: g.trace_id || "singleton-" + (g.events && g.events[0] && g.events[0].event_id),
      title: scrubPath(g.title),
      outcome: g.outcome,
      eventCount: g.event_count != null ? g.event_count : (g.events || []).length,
      toolCallCount: g.tool_call_count || (g.events || []).filter((e) => e.action_type === "tool_call").length,
      llmCallCount: g.llm_call_count || (g.events || []).filter((e) => e.action_type === "llm_call").length,
      totalTokens: sumTokenTotal((g.events || []).filter((e) => e.action_type === "llm_call")),
      totalDurationMs: g.total_duration_ms != null ? g.total_duration_ms : null,
      startTs: g.start_ts,
      events: (g.events || []).slice().sort((a, b) => (a.seq || 0) - (b.seq || 0)),
    }));
  } else if (events && events.length > 0) {
    const byTrace = new Map();
    events.forEach((ev) => {
      const key = ev.trace_id || "singleton-" + ev.event_id;
      if (!byTrace.has(key)) byTrace.set(key, []);
      byTrace.get(key).push(ev);
    });
    out = Array.from(byTrace.entries()).map(([key, evs]) => {
      evs = evs.slice().sort((a, b) => (a.seq || 0) - (b.seq || 0));
      const toolCallCount = evs.filter((e) => e.action_type === "tool_call").length;
      const llmCallCount = evs.filter((e) => e.action_type === "llm_call").length;
      const totalTokens = sumTokenTotal(evs.filter((e) => e.action_type === "llm_call"));
      const first = evs[0], last = evs[evs.length - 1];
      const errEv = evs.find((e) => e.outcome === "error");
      const hasPending = evs.some((e) => e.outcome === "pending");
      const t0 = tsMs(first.ts_utc), t1 = tsMs(last.ts_utc);
      let dur = 0, have = true;
      evs.forEach((e) => { if (e.duration_ms == null) have = false; else dur += e.duration_ms; });
      return {
        key,
        title: scrubPath(first.trace_id ? "Turn" : "Ungrouped · " + titleFor(first)),
        outcome: errEv ? "error" : hasPending ? "pending" : "success",
        eventCount: evs.length,
        toolCallCount,
        llmCallCount,
        totalTokens,
        totalDurationMs: have && dur > 0 ? dur : (t0 != null && t1 != null ? t1 - t0 : null),
        startTs: first.ts_utc,
        events: evs,
      };
    });
  }
  if (!out) return [];
  return out.sort((a, b) => {
    const ta = tsMs(b.events.length ? b.events[0].ts_utc : b.startTs) || 0;
    const tb = tsMs(a.events.length ? a.events[0].ts_utc : a.startTs) || 0;
    return ta - tb;
  });
}

// Client-side per-event filter mirroring the live-stream filter + search,
// extended with the day + model facets (model matches llm_call tool_name).
function eventMatches(ev, f, query) {
  if (f.action_type && ev.action_type !== f.action_type) return false;
  if (f.actor && ev.actor !== f.actor) return false;
  if (f.outcome && ev.outcome !== f.outcome) return false;
  if (f.day && fmtDate(ev.ts_utc) !== f.day) return false;
  if (f.model && !(ev.action_type === "llm_call" && ev.tool_name === f.model)) return false;
  if (query) {
    const hay = ((ev.tool_name || "") + " " + (ev.detail_json || "") + " " +
      (ev.human_summary || "") + " " + (ev.display_summary || "")).toLowerCase();
    if (hay.indexOf(query) < 0) return false;
  }
  return true;
}

function LlmSummaryLine({ events }) {
  const n = events.length;
  let dur = 0, have = false;
  events.forEach((e) => { if (e.duration_ms != null) { dur += e.duration_ms; have = true; } });
  const label = n + " model call" + (n === 1 ? "" : "s") + (have ? " · " + fmtDur(dur) + " total" : "");
  return jsx("div", {
    className: "flex items-center gap-2 rounded-lg border border-dashed px-2.5 py-2 text-xs",
    style: { borderColor: "var(--ui-stroke-secondary)", color: "var(--ui-text-secondary)" },
    children: [
      jsx(Codicon, { name: "symbol-variable", size: 14, key: "i", style: { color: "var(--ui-text-secondary)", flexShrink: 0 } }),
      jsx("span", { children: label, key: "l" }),
    ],
  });
}

function RequestGroup({ ctx, group, expanded, onToggle, expandedEvents, onToggleEvent }) {
  const nonLlm = group.events.filter((e) => e.action_type !== "llm_call");
  const llmEvents = group.events.filter((e) => e.action_type === "llm_call");
  return jsx("div", {
    className: "rounded-lg border",
    style: { borderColor: expanded ? "var(--ui-accent)" : "var(--ui-stroke-secondary)", background: "var(--ui-bg-secondary, transparent)" },
    children: [
      // Header: real <button> — reliable, keyboard-accessible click target.
      jsx("button", {
        type: "button",
        onClick: onToggle,
        className: "flex w-full cursor-pointer flex-wrap items-center gap-2 rounded-t-lg p-2.5 text-left hover:bg-[var(--ui-bg-tertiary,rgba(255,255,255,0.03))]",
        style: { background: "transparent", border: "none", font: "inherit", color: "inherit" },
        children: [
          jsx(Codicon, { name: expanded ? "chevron-down" : "chevron-right", size: 14, key: "chev", style: { color: "var(--ui-text-secondary)", flexShrink: 0 } }),
          jsx("span", { className: "text-xs", style: { color: "var(--ui-text-secondary)", fontVariantNumeric: "tabular-nums" }, children: fmtTime(group.startTs), key: "t" }),
          jsx("span", { className: "flex-1 text-sm font-medium", children: scrubPath(String(group.title)), key: "n" }),
          group.toolCallCount > 0 && jsx("span", {
            className: "rounded-md px-1.5 py-0.5 text-[10px]",
            style: { background: "var(--ui-bg-tertiary, rgba(255,255,255,0.06))", color: "var(--ui-text-secondary)" },
            children: group.toolCallCount + (group.toolCallCount === 1 ? " action" : " actions"), key: "c",
          }),
          group.totalDurationMs != null && jsx("span", { className: "text-[11px]", style: { color: "var(--ui-text-secondary)" }, children: fmtDur(group.totalDurationMs), key: "d" }),
          // P3.5b: in/out token split when usage carries prompt/completion;
          // falls back to total-only ("621k tok") when it doesn't.
          jsx("span", {
            className: "text-[11px]",
            style: { color: "var(--ui-text-secondary)", fontVariantNumeric: "tabular-nums" },
            children: fmtInOutLabel(group.events), key: "tok",
          }),
          jsx(OutcomeBadge, { outcome: group.outcome, key: "b" }),
        ],
      }, "head"),
      !expanded && jsx("div", {
        className: "truncate px-2.5 pb-2 pl-9 text-xs",
        style: { color: "var(--ui-text-secondary)" },
        children: group.eventCount + (group.eventCount === 1 ? " event" : " events") +
          (llmEvents.length > 0 ? " · " + llmEvents.length + " model call" + (llmEvents.length === 1 ? "" : "s") : ""),
      }, "prev"),
      // Body: stopPropagation so clicks inside never re-trigger the toggle.
      expanded && jsx("div", {
        onClick: (e) => { e.stopPropagation(); },
        className: "space-y-1.5 px-2.5 pb-2.5",
        children: [
          nonLlm.map((ev) => jsx(EventCard, {
            ctx, ev,
            expanded: !!expandedEvents[ev.event_id],
            onToggle: () => onToggleEvent(ev.event_id),
            key: ev.event_id,
          })),
          llmEvents.length > 0 && jsx(LlmSummaryLine, { events: llmEvents, key: "llm" }),
        ].filter(Boolean),
      }, "det"),
    ],
  });
}

function ActivityPage({ ctx }) {
  const [events, setEvents] = useState([]);
  const [serverGroups, setServerGroups] = useState(null);
  const [viewMode, setViewMode] = useState("grouped"); // "grouped" | "flat"
  const [stats, setStats] = useState(null);
  const [filters, setFilters] = useState({ action_type: "", actor: "", outcome: "", day: "", model: "" });
  const [q, setQ] = useState("");
  const [filterOpts, setFilterOpts] = useState({ action_types: [], actors: [], outcomes: [] });
  const [facets, setFacets] = useState({ days: [], models: [] });
  const [hasMore, setHasMore] = useState(false);
  const [nextBefore, setNextBefore] = useState(null);
  const [expanded, setExpanded] = useState({});        // per-event expand (both views)
  const [expandedGroups, setExpandedGroups] = useState({}); // per-group expand (grouped view)
  const [expandedDays, setExpandedDays] = useState({}); // per-day section expand (grouped view)
  const [expandedDaysInit, setExpandedDaysInit] = useState(false);
  const [live, setLive] = useState(false);
  const [err, setErr] = useState(null);
  const filtersRef = useRef(filters);
  const qRef = useRef(q);
  filtersRef.current = filters;
  qRef.current = q;

  const load = useCallback((append) => {
    const p = new URLSearchParams();
    p.set("limit", "50");
    const f = filtersRef.current;
    if (f.action_type) p.set("action_type", f.action_type);
    if (f.actor) p.set("actor", f.actor);
    if (f.outcome) p.set("outcome", f.outcome);
    // Day/model facets: sent as query params in case the backend supports
    // them server-side; eventMatches filters client-side regardless.
    if (f.day) p.set("day", f.day);
    if (f.model) p.set("model", f.model);
    if (qRef.current) p.set("q", qRef.current);
    if (append && nextBefore) p.set("before_id", nextBefore);
    return ctx.rest("/events?" + p.toString())
      .then((res) => {
        setEvents((prev) => (append ? prev.concat(res.events) : res.events));
        setHasMore(res.has_more);
        setNextBefore(res.next_before_id);
        setErr(null);
      })
      .catch((e) => setErr("Error loading events: " + String(e)));
  }, [ctx, nextBefore]);

  const loadStats = useCallback(() => {
    ctx.rest("/stats").then(setStats).catch(() => {});
  }, [ctx]);
  const loadFilterOpts = useCallback(() => {
    ctx.rest("/meta/filters").then(setFilterOpts).catch(() => {});
  }, [ctx]);
  // Day + model facet options for the filter bar. Endpoint is optional — a
  // 404/other failure just leaves both selects empty (hidden from the bar).
  const loadFacets = useCallback(() => {
    ctx.rest("/facets")
      .then((res) => setFacets({
        days: Array.isArray(res && res.days) ? res.days : [],
        models: Array.isArray(res && res.models) ? res.models : [],
      }))
      .catch(() => {});
  }, [ctx]);
  // Server-side grouping (preferred); fall back to client-side buildGroups on
  // the flat /events list if the endpoint isn't available.
  // Day-scoped paging: fetch the recent D days (server default), and page
  // back through history by passing the oldest returned day as before_day.
  const [hasMoreGroups, setHasMoreGroups] = useState(false);
  const [oldestDay, setOldestDay] = useState(null);
  const [loadingOlderDays, setLoadingOlderDays] = useState(false);
  const loadGroups = useCallback(() => {
    // Generous limit_groups: the day window (days=3) + per-day event cap bound
    // the result, so a small ceiling here would truncate older days' groups
    // (the "past days hidden" bug). Pass a high ceiling and let day-scoping govern.
    ctx.rest("/groups?days=3&limit_groups=2000")
      .then((res) => {
        // Live updates must not wipe days the user paged in: merge — fresh
        // groups (the current/recent window) replace entries with the same
        // group key, older appended pages are kept below.
        setServerGroups((prev) => {
          const fresh = res && res.groups ? res.groups : [];
          if (!prev || prev.length === 0) return fresh.length > 0 ? fresh : null;
          const byKey = new Map(fresh.map((g) => [g.trace_id || "singleton-" + ((g.events || [])[0] || {}).event_id, g]));
          const merged = fresh.slice();
          prev.forEach((g) => {
            const k = g.trace_id || "singleton-" + ((g.events || [])[0] || {}).event_id;
            if (!byKey.has(k)) merged.push(g);
          });
          return merged;
        });
        setHasMoreGroups(!!(res && res.has_more));
        if (res && res.oldest_day) setOldestDay(res.oldest_day);
        setErr(null);
      })
      .catch(() => setServerGroups(null));
  }, [ctx]);
  // Load older days: ask the server for days strictly before oldest_day and
  // APPEND the returned older groups (dedupe by group key). oldest_day /
  // has_more_groups come from the page response so the button pages on.
  const loadOlderDays = useCallback(() => {
    if (!oldestDay || loadingOlderDays) return;
    setLoadingOlderDays(true);
    ctx.rest("/groups?days=3&limit_groups=2000&before_day=" + encodeURIComponent(oldestDay))
      .then((res) => {
        const older = res && res.groups ? res.groups : [];
        if (older.length > 0) {
          setServerGroups((prev) => {
            const seen = new Set((prev || []).map((g) =>
              g.trace_id || "singleton-" + ((g.events || [])[0] || {}).event_id));
            const add = older.filter((g) => {
              const k = g.trace_id || "singleton-" + ((g.events || [])[0] || {}).event_id;
              return !seen.has(k);
            });
            return (prev || []).concat(add);
          });
        }
        // Track the true oldest day across pages so repeated clicks keep
        // paging deeper instead of re-fetching the same window.
        setOldestDay((prev) => (res && res.oldest_day) || prev);
        setHasMoreGroups(!!(res && res.has_more));
      })
      .catch((e) => setErr("Error loading older days: " + String(e)))
      .finally(() => setLoadingOlderDays(false));
  }, [ctx, oldestDay, loadingOlderDays]);

  useEffect(() => { load(false); }, [filters]);
  useEffect(() => { loadFilterOpts(); loadStats(); loadGroups(); loadFacets(); }, []);

  // Live stream via ctx.socket (falls back to a slow poll if it no-ops).
  useEffect(() => {
    let disposed = false;
    let poll = null;
    const onMsg = (msg) => {
      if (disposed) return;
      const ev = msg && (msg.event || (msg.type === "event" && msg.event));
      if (!ev) return;
      setLive(true);
      const f = filtersRef.current, query = qRef.current.toLowerCase();
      if (f.action_type && ev.action_type !== f.action_type) return;
      if (f.actor && ev.actor !== f.actor) return;
      if (f.outcome && ev.outcome !== f.outcome) return;
      if (query) {
        const hay = ((ev.tool_name || "") + " " + (ev.detail_json || "") + " " +
          (ev.human_summary || "") + " " + (ev.display_summary || "")).toLowerCase();
        if (hay.indexOf(query) < 0) return;
      }
      setEvents((prev) => [ev].concat(prev).slice(0, 400));
      loadStats();
      loadGroups();
    };
    let sock = null;
    try {
      if (ctx.socket) sock = ctx.socket("/events/stream?since=0", onMsg);
    } catch (e) { sock = null; }
    // Polling fallback (also covers ctx.socket being a no-op on OAuth remotes).
    poll = setInterval(() => { if (!disposed) loadStats(); }, 5000);
    return () => {
      disposed = true;
      if (poll) clearInterval(poll);
      if (sock && sock.close) try { sock.close(); } catch (e) {}
    };
  }, [ctx, loadStats, loadGroups]);

  const onToggle = (id) => setExpanded((p) => Object.assign({}, p, { [id]: !p[id] }));
  const onToggleGroup = (key) => setExpandedGroups((p) => Object.assign({}, p, { [key]: !p[key] }));

  // Build request groups (server contract preferred, client fallback). Apply
  // active filters/search to the events inside groups; drop empty groups.
  const query = q.toLowerCase();
  const groups = buildGroups(serverGroups, events)
    .map((g) => {
      const evs = g.events.filter((ev) => eventMatches(ev, filters, query));
      return Object.assign({}, g, { events: evs, eventCount: evs.length });
    })
    .filter((g) => g.events.length > 0);

  // Build collapsible DAY SECTIONS for the grouped view. Each distinct day
  // (newest-first) becomes a section with a rollup header; its turn-group
  // cards render inside when expanded. Default: most recent day expanded.
  const daySections = [];
  {
    let cur = null;
    groups.forEach((g) => {
      const d = fmtDate(g.startTs);
      if (!d) return;
      if (!cur || cur.day !== d) {
        cur = { day: d, groups: [] };
        daySections.push(cur);
      }
      cur.groups.push(g);
    });
  }
  daySections.forEach((sec) => {
    const allEvents = [];
    sec.groups.forEach((g) => allEvents.push(...g.events));
    const modelCounts = new Map();
    allEvents.forEach((ev) => {
      if (ev.action_type === "llm_call" && ev.tool_name) {
        modelCounts.set(ev.tool_name, (modelCounts.get(ev.tool_name) || 0) + 1);
      }
    });
    sec.turnCount = sec.groups.length;
    sec.eventCount = allEvents.length;
    sec.modelsLabel = Array.from(modelCounts.entries())
      .sort((a, b) => b[1] - a[1])
      .map(([m, c]) => m + " ×" + c)
      .join(", ");
    sec.tokensLabel = fmtInOutLabel(allEvents);
  });
  // Default expand: most recent day expanded, older collapsed — set once,
  // keyed per day so later days arriving via live-stream stay collapsed.
  useEffect(() => {
    if (expandedDaysInit || daySections.length === 0) return;
    setExpandedDays((p) => Object.assign({ [daySections[0].day]: true }, p));
    setExpandedDaysInit(true);
  }, [daySections.length, expandedDaysInit]);
  const onToggleDay = (day) => setExpandedDays((p) => Object.assign({}, p, { [day]: !p[day] }));

  // Group by date (flat view) — same eventMatches filter so day/model and the
  // existing type/actor/outcome/search filters apply here too.
  const rows = [];
  let lastDate = null;
  events.forEach((ev) => {
    if (!eventMatches(ev, filters, query)) return;
    const d = fmtDate(ev.ts_utc);
    if (d && d !== lastDate) { rows.push({ date: d }); lastDate = d; }
    rows.push(ev);
  });

  return jsx("div", { className: "mx-auto max-w-3xl p-5", children: [
    jsx("div", { className: "mb-3 flex items-center justify-between", children: [
      jsx("h2", { className: "text-xl font-semibold", children: "Activity" }),
      jsx("div", { className: "flex items-center gap-1.5 text-xs", style: { color: "var(--ui-text-secondary)" }, children: [
        jsx(StatusDot, { status: live ? "ok" : "idle", key: "d" }),
        jsx("span", { children: live ? "Live" : "Polling", key: "l" }),
      ] }),
    ], key: "head" }),

    stats && jsx("div", { className: "mb-3 flex flex-wrap gap-1.5", children:
      [jsx(StatChip, { label: "total", val: stats.total || 0, key: "total" })]
        .concat(Object.keys(stats.by_type || {}).map((k) => jsx(StatChip, { label: k, val: stats.by_type[k], key: k })))
        .concat((stats.pending || 0) > 0 ? [jsx(StatChip, { label: "pending", val: stats.pending, key: "pending" })] : []),
    }),

    jsx("div", { className: "mb-3 flex flex-wrap gap-2", children: [
      filterSel("type", "All types", filterOpts.action_types, filters.action_type, (v) => setFilters((f) => ({ ...f, action_type: v })), "type"),
      filterSel("actor", "All actors", filterOpts.actors, filters.actor, (v) => setFilters((f) => ({ ...f, actor: v })), "actor"),
      filterSel("outcome", "All outcomes", filterOpts.outcomes, filters.outcome, (v) => setFilters((f) => ({ ...f, outcome: v })), "outcome"),
      facets.days.length > 0 && filterSel("day", "All days", facets.days, filters.day, (v) => setFilters((f) => ({ ...f, day: v === "__all__" ? "" : v })), "day", "All days"),
      facets.models.length > 0 && filterSel("model", "All models", facets.models.map((m) => m.model), filters.model, (v) => setFilters((f) => ({ ...f, model: v === "__all__" ? "" : v })), "model", "All models"),
      jsx("div", { className: "min-w-[180px] flex-1", children: jsx(Input, {
        placeholder: "Search tool / detail…", value: q,
        onChange: (e) => { setQ(e.target.value); },
        onKeyDown: (e) => { if (e.key === "Enter") load(false); },
      }), key: "q" }),
      jsx(Button, { variant: "outline", onClick: () => load(false), children: "Search", key: "s" }),
      // Grouped / Flat view toggle
      jsx(Button, {
        variant: viewMode === "grouped" ? "default" : "outline",
        onClick: () => setViewMode(viewMode === "grouped" ? "flat" : "grouped"),
        children: viewMode === "grouped" ? "Grouped" : "Flat",
        key: "vm",
      }),
    ], key: "controls" }),

    err && jsx("div", { className: "mb-2 text-xs", style: { color: COLOR.err }, children: err, key: "err" }),

    viewMode === "grouped"
      ? jsx(ScrollArea, { className: "space-y-1.5", children:
          daySections.length === 0
            ? jsx(EmptyState, { title: "No activity yet", description: "Events appear here as the agent acts." })
            : [
                daySections.map((sec) =>
                  jsx(DaySection, {
                    ctx, sec,
                    expanded: !!expandedDays[sec.day],
                    onToggle: () => onToggleDay(sec.day),
                    expandedGroups,
                    onToggleGroup: onToggleGroup,
                    expandedEvents: expanded,
                    onToggleEvent: onToggle,
                    key: "day-" + sec.day,
                  })
                ),
                // Page back through history: requests days strictly before
                // the oldest day currently shown; server appends nothing —
                // we merge the returned older groups in loadOlderDays.
                hasMoreGroups && jsx("div", {
                  className: "mt-3 text-center",
                  children: jsx(Button, {
                    variant: "outline",
                    onClick: loadOlderDays,
                    children: loadingOlderDays ? "Loading…" : "Load older days",
                  }),
                  key: "more-days",
                }),
              ],
        })
      : jsx(ScrollArea, { className: "space-y-1.5", children:
          rows.length === 0
            ? jsx(EmptyState, { title: "No activity yet", description: "Events appear here as the agent acts." })
            : rows.map((r, i) =>
                r.date
                  ? jsx("div", { className: "mb-1 mt-3 text-[11px] font-bold uppercase tracking-wide", style: { color: "var(--ui-text-secondary)" }, children: r.date, key: "date-" + i })
                  : jsx(EventCard, { ctx, ev: r, expanded: !!expanded[r.event_id], onToggle: () => onToggle(r.event_id), key: r.event_id })
              ),
        }),

    hasMore && jsx("div", { className: "mt-3 text-center", children: jsx(Button, { variant: "outline", onClick: () => load(true), children: "Load more" }), key: "more" }),
  ] });
}

function filterSel(key, placeholder, options, value, onChange, uid, allLabel) {
  // Radix-style Select can't deselect, so clearable selects get an explicit
  // "All …" item mapped to the sentinel "__all__" -> "" by the caller wrapper.
  const opts = allLabel != null ? ["__all__"].concat(options || []) : (options || []);
  return jsx(Select, { value: value || undefined, onValueChange: onChange, children: [
    jsx(SelectTrigger, { className: "w-[150px]", children: jsx(SelectValue, { placeholder }), key: "t" }),
    jsx(SelectContent, { children: opts.map((o, i) => jsx(SelectItem, {
      value: o,
      children: allLabel != null && i === 0 ? allLabel : o,
      key: o,
    })), key: "c" }),
  ], key: uid });
}

// A collapsible day section in the grouped view: header row (chevron, day
// label, rollup of turns/events/models/tokens) + the day's RequestGroup
// cards when expanded.
function DaySection({ ctx, sec, expanded, onToggle, expandedGroups, onToggleGroup, expandedEvents, onToggleEvent }) {
  return jsx("div", {
    className: "mt-3",
    children: [
      jsx("button", {
        type: "button",
        onClick: onToggle,
        className: "flex w-full cursor-pointer flex-wrap items-center gap-2 rounded-md px-1 py-1 text-left hover:bg-[var(--ui-bg-tertiary,rgba(255,255,255,0.03))]",
        style: { background: "transparent", border: "none", font: "inherit", color: "inherit" },
        children: [
          jsx(Codicon, { name: expanded ? "chevron-down" : "chevron-right", size: 14, key: "chev", style: { color: "var(--ui-text-secondary)", flexShrink: 0 } }),
          jsx("span", { className: "text-[11px] font-bold uppercase tracking-wide", style: { color: "var(--ui-text-secondary)" }, children: sec.day, key: "day" }),
          jsx("span", {
            className: "text-[11px]",
            style: { color: "var(--ui-text-secondary)", fontVariantNumeric: "tabular-nums" },
            children: sec.turnCount + " turn" + (sec.turnCount === 1 ? "" : "s") +
              " · " + sec.eventCount + " event" + (sec.eventCount === 1 ? "" : "s") +
              (sec.modelsLabel ? " · " + sec.modelsLabel : "") +
              (sec.tokensLabel ? " · " + sec.tokensLabel : ""),
            key: "rollup",
          }),
        ],
      }, "head"),
      expanded && jsx("div", {
        className: "space-y-1.5 pl-4",
        children: sec.groups.map((g) =>
          jsx(RequestGroup, {
            ctx, group: g,
            expanded: !!expandedGroups[g.key],
            onToggle: () => onToggleGroup(g.key),
            expandedEvents,
            onToggleEvent,
            key: g.key,
          })
        ),
      }, "body"),
    ],
  });
}

export default {
  id: "hermes-audit",
  name: "Activity",
  register(ctx) {
    ctx.register({
      id: "activity-page",
      area: ROUTES_AREA,
      data: { path: "/activity" },
      render: () => jsx(ActivityPage, { ctx }),
    });
    ctx.register({
      id: "activity-nav",
      area: SIDEBAR_NAV_AREA,
      data: { path: "/activity", label: "Activity", codicon: "pulse" },
    });
    ctx.register({
      id: "activity-open",
      area: PALETTE_AREA,
      data: { label: "Open Activity (Audit Trail)", onSelect: () => host.navigate("/activity") },
    });
  },
};
