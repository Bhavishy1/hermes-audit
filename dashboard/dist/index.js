/* hermes-audit dashboard — Activity feed (self-contained, no build step).
 *
 * Mounts into the plugin's tab, fetches /api/plugins/hermes-audit/*, and opens
 * the /events/stream WebSocket for live updates. Read-only.
 *
 * The dashboard injects the per-process session token as
 * window.__HERMES_SESSION_TOKEN__; the WS passes it as ?token=.
 */
(function () {
  "use strict";

  var API = "/api/plugins/hermes-audit";
  var state = {
    events: [],
    filters: { action_type: "", actor: "", outcome: "", q: "" },
    hasMore: false,
    nextBeforeId: null,
    live: true,
    expanded: {},       // event_id -> true
    detailCache: {},    // event_id -> detail response
    ws: null,
    wsRetry: 0,
    lastRowidSeen: 0
  };

  // ---- helpers ------------------------------------------------------------

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }

  function token() {
    return (typeof window !== "undefined" && window.__HERMES_SESSION_TOKEN__) || "";
  }

  function authHeaders() {
    var t = token();
    return t ? { "Authorization": "Bearer " + t } : {};
  }

  function apiGet(path) {
    return fetch(API + path, { headers: authHeaders() }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  function fmtTime(ts) {
    if (!ts) return "";
    try {
      var d = new Date(ts);
      if (isNaN(d)) return ts;
      return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    } catch (e) { return ts; }
  }

  function fmtDate(ts) {
    if (!ts) return "";
    try {
      var d = new Date(ts);
      if (isNaN(d)) return "";
      return d.toLocaleDateString([], { month: "short", day: "numeric" });
    } catch (e) { return ""; }
  }

  function titleFor(ev) {
    var t = ev.tool_name || ev.action_type || "event";
    if (ev.action_type === "llm_call") return "LLM call" + (ev.tool_name ? " · " + ev.tool_name : "");
    if (ev.action_type === "tool_call") return "Tool · " + t;
    if (ev.action_type === "skill_write") return "Learned skill · " + t;
    if (ev.action_type === "message") return "Assistant message";
    if (ev.action_type === "session_start") return "Session started";
    if (ev.action_type === "session_end") return "Session ended";
    if (ev.action_type && ev.action_type.indexOf("approval") === 0) return "Approval · " + t;
    return t;
  }

  function outcomeBadge(ev) {
    var o = ev.outcome || "unknown";
    var b = el("span", "audit-badge audit-badge-" + o, o);
    return b;
  }

  // ---- data loading ---------------------------------------------------------

  function buildQuery(extra) {
    var p = new URLSearchParams();
    p.set("limit", "50");
    var f = state.filters;
    if (f.action_type) p.set("action_type", f.action_type);
    if (f.actor) p.set("actor", f.actor);
    if (f.outcome) p.set("outcome", f.outcome);
    if (f.q) p.set("q", f.q);
    if (extra) Object.keys(extra).forEach(function (k) { p.set(k, extra[k]); });
    return p.toString();
  }

  function loadEvents(append) {
    var extra = {};
    if (append && state.nextBeforeId) extra.before_id = state.nextBeforeId;
    return apiGet("/events?" + buildQuery(extra)).then(function (res) {
      if (append) state.events = state.events.concat(res.events);
      else state.events = res.events;
      state.hasMore = res.has_more;
      state.nextBeforeId = res.next_before_id;
      renderFeed();
      renderLoadMore();
    }).catch(function (e) {
      setStatus("Error loading events: " + e.message, true);
    });
  }

  function loadStats() {
    return apiGet("/stats").then(function (s) {
      var bar = document.getElementById("audit-stats");
      if (!bar) return;
      bar.innerHTML = "";
      bar.appendChild(statChip("total", s.total));
      var byType = s.by_type || {};
      Object.keys(byType).forEach(function (k) {
        bar.appendChild(statChip(k, byType[k]));
      });
      var pend = s.pending || 0;
      if (pend > 0) {
        var c = statChip("pending", pend);
        c.classList.add("audit-chip-warn");
        bar.appendChild(c);
      }
    }).catch(function () {});
  }

  function statChip(label, val) {
    var c = el("span", "audit-chip");
    c.appendChild(el("span", "audit-chip-num", String(val)));
    c.appendChild(el("span", "audit-chip-label", label));
    return c;
  }

  function loadFilters() {
    return apiGet("/meta/filters").then(function (f) {
      fillSelect("audit-filter-type", f.action_types, "All types");
      fillSelect("audit-filter-actor", f.actors, "All actors");
      fillSelect("audit-filter-outcome", f.outcomes, "All outcomes");
    }).catch(function () {});
  }

  function fillSelect(id, values, placeholder) {
    var s = document.getElementById(id);
    if (!s) return;
    var cur = s.value;
    s.innerHTML = "";
    var opt = el("option", "", placeholder); opt.value = ""; s.appendChild(opt);
    (values || []).forEach(function (v) {
      var o = el("option", "", v); o.value = v; s.appendChild(o);
    });
    s.value = cur || "";
  }

  // ---- rendering ------------------------------------------------------------

  function setStatus(msg, isErr) {
    var s = document.getElementById("audit-status");
    if (!s) return;
    s.textContent = msg || "";
    s.className = "audit-status" + (isErr ? " audit-status-err" : "");
  }

  function renderFeed() {
    var feed = document.getElementById("audit-feed");
    if (!feed) return;
    feed.innerHTML = "";
    if (!state.events.length) {
      feed.appendChild(el("div", "audit-empty", "No activity yet. Events appear here as the agent acts."));
      return;
    }
    var lastDate = null;
    state.events.forEach(function (ev) {
      var d = fmtDate(ev.ts_utc);
      if (d && d !== lastDate) {
        feed.appendChild(el("div", "audit-date", d));
        lastDate = d;
      }
      feed.appendChild(renderCard(ev));
    });
  }

  function renderCard(ev) {
    var card = el("div", "audit-card audit-type-" + (ev.action_type || "event"));
    card.dataset.eventId = ev.event_id;

    var head = el("div", "audit-card-head");
    head.appendChild(el("span", "audit-time", fmtTime(ev.ts_utc)));
    head.appendChild(el("span", "audit-title", titleFor(ev)));
    head.appendChild(outcomeBadge(ev));
    if (ev.duration_ms != null) head.appendChild(el("span", "audit-dur", ev.duration_ms + "ms"));
    if (ev.actor && ev.actor !== "assistant") head.appendChild(el("span", "audit-actor", ev.actor));
    card.appendChild(head);

    // preview line from detail
    var preview = previewText(ev);
    if (preview) card.appendChild(el("div", "audit-preview", preview));

    // expanded detail (lazy)
    if (state.expanded[ev.event_id]) {
      var det = el("div", "audit-detail", "Loading…");
      card.appendChild(det);
      fillDetail(ev.event_id, det);
    }

    card.addEventListener("click", function () {
      var id = ev.event_id;
      state.expanded[id] = !state.expanded[id];
      renderFeed();
    });
    return card;
  }

  function previewText(ev) {
    try {
      if (!ev.detail_json) return "";
      var d = JSON.parse(ev.detail_json);
      if (d.text) return d.text;
      if (d.args_summary) return d.args_summary;
      if (d.result_summary) return d.result_summary;
      if (d.error) return "Error: " + d.error;
      if (d.model) return d.model + (d.usage ? " · " + d.usage : "");
      return "";
    } catch (e) { return ""; }
  }

  function fillDetail(eventId, container) {
    if (state.detailCache[eventId]) { renderDetail(container, state.detailCache[eventId]); return; }
    apiGet("/events/" + encodeURIComponent(eventId)).then(function (res) {
      state.detailCache[eventId] = res;
      renderDetail(container, res);
    }).catch(function (e) {
      container.textContent = "Failed to load detail: " + e.message;
    });
  }

  function renderDetail(container, res) {
    container.innerHTML = "";
    var ev = res.event || {};

    var kv = el("div", "audit-kv");
    [["action", ev.action_type], ["tool", ev.tool_name], ["actor", ev.actor],
     ["outcome", ev.outcome], ["duration", ev.duration_ms != null ? ev.duration_ms + "ms" : null],
     ["trace", ev.trace_id], ["session", ev.session_id], ["seq", ev.seq]]
      .forEach(function (pair) {
        if (pair[1] == null || pair[1] === "") return;
        kv.appendChild(el("span", "audit-k", pair[0]));
        kv.appendChild(el("span", "audit-v", String(pair[1])));
      });
    container.appendChild(kv);

    if (ev.detail_json) {
      container.appendChild(el("div", "audit-sec", "Detail"));
      container.appendChild(codeBlock(pretty(ev.detail_json)));
    }
    if (ev.provenance_json) {
      container.appendChild(el("div", "audit-sec", "Provenance"));
      container.appendChild(codeBlock(pretty(ev.provenance_json)));
    }
    if (res.chain && res.chain.length > 1) {
      container.appendChild(el("div", "audit-sec", "Trace (" + res.chain.length + " events)"));
      var list = el("div", "audit-chain");
      res.chain.forEach(function (c) {
        var row = el("div", "audit-chain-row" + (c.event_id === ev.event_id ? " audit-chain-self" : ""));
        row.appendChild(el("span", "audit-time", fmtTime(c.ts_utc)));
        row.appendChild(el("span", "", (c.action_type || "") + (c.tool_name ? " · " + c.tool_name : "")));
        row.appendChild(outcomeBadge(c));
        list.appendChild(row);
      });
      container.appendChild(list);
    }
  }

  function pretty(jsonStr) {
    try { return JSON.stringify(JSON.parse(jsonStr), null, 2); }
    catch (e) { return jsonStr; }
  }

  function codeBlock(text) {
    var pre = el("pre", "audit-code");
    pre.textContent = text;
    return pre;
  }

  function renderLoadMore() {
    var wrap = document.getElementById("audit-loadmore");
    if (!wrap) return;
    wrap.innerHTML = "";
    if (state.hasMore) {
      var btn = el("button", "audit-btn", "Load more");
      btn.addEventListener("click", function () { loadEvents(true); });
      wrap.appendChild(btn);
    }
  }

  // Prepend a live event (from the WebSocket) if it passes current filters.
  function onLiveEvent(ev) {
    if (!state.live) return;
    var f = state.filters;
    if (f.action_type && ev.action_type !== f.action_type) return;
    if (f.actor && ev.actor !== f.actor) return;
    if (f.outcome && ev.outcome !== f.outcome) return;
    if (f.q) {
      var hay = ((ev.tool_name || "") + " " + (ev.detail_json || "")).toLowerCase();
      if (hay.indexOf(f.q.toLowerCase()) < 0) return;
    }
    state.events.unshift(ev);
    if (state.events.length > 400) state.events.length = 400;
    renderFeed();
    loadStats();
  }

  // ---- websocket -------------------------------------------------------------

  function connectWs() {
    var proto = location.protocol === "https:" ? "wss" : "ws";
    var url = proto + "://" + location.host + API + "/events/stream?since=0&token=" + encodeURIComponent(token());
    try { state.ws = new WebSocket(url); } catch (e) { scheduleReconnect(); return; }

    state.ws.onopen = function () {
      state.wsRetry = 0;
      setLiveIndicator(true);
    };
    state.ws.onmessage = function (m) {
      try {
        var data = JSON.parse(m.data);
        if (data.type === "event" && data.event) onLiveEvent(data.event);
      } catch (e) {}
    };
    state.ws.onclose = function () {
      setLiveIndicator(false);
      scheduleReconnect();
    };
    state.ws.onerror = function () {
      try { state.ws.close(); } catch (e) {}
    };
  }

  function scheduleReconnect() {
    state.wsRetry = Math.min(state.wsRetry + 1, 6);
    var delay = Math.min(15000, 500 * Math.pow(2, state.wsRetry));
    setTimeout(connectWs, delay);
  }

  function setLiveIndicator(on) {
    var dot = document.getElementById("audit-live-dot");
    if (dot) dot.className = "audit-live-dot " + (on ? "on" : "off");
    var lbl = document.getElementById("audit-live-label");
    if (lbl) lbl.textContent = on ? "Live" : "Reconnecting…";
  }

  // ---- root render -------------------------------------------------------------

  function render(root) {
    root.innerHTML = "";
    root.classList.add("audit-root");

    var header = el("div", "audit-header");
    var titleRow = el("div", "audit-title-row");
    titleRow.appendChild(el("h2", "audit-h2", "Activity"));
    var liveWrap = el("div", "audit-live");
    liveWrap.appendChild(el("span", "audit-live-dot off", "")).id = "audit-live-dot";
    liveWrap.appendChild(el("span", "", "…")).id = "audit-live-label";
    titleRow.appendChild(liveWrap);
    header.appendChild(titleRow);
    header.appendChild(el("div", "audit-stats", "")).id = "audit-stats";
    root.appendChild(header);

    var controls = el("div", "audit-controls");
    controls.appendChild(sel("audit-filter-type", onFilter));
    controls.appendChild(sel("audit-filter-actor", onFilter));
    controls.appendChild(sel("audit-filter-outcome", onFilter));
    var search = el("input", "audit-search");
    search.placeholder = "Search tool / detail…";
    search.id = "audit-filter-q";
    var deb;
    search.addEventListener("input", function () {
      clearTimeout(deb);
      deb = setTimeout(function () {
        state.filters.q = search.value.trim();
        loadEvents(false);
      }, 300);
    });
    controls.appendChild(search);
    root.appendChild(controls);

    root.appendChild(el("div", "audit-status", "")).id = "audit-status";
    root.appendChild(el("div", "audit-feed")).id = "audit-feed";
    root.appendChild(el("div", "audit-loadmore")).id = "audit-loadmore";

    function onFilter() {
      state.filters.action_type = document.getElementById("audit-filter-type").value;
      state.filters.actor = document.getElementById("audit-filter-actor").value;
      state.filters.outcome = document.getElementById("audit-filter-outcome").value;
      loadEvents(false);
    }

    loadFilters();
    loadStats();
    loadEvents(false);
    connectWs();
  }

  function sel(id) {
    var s = el("select", "audit-sel");
    s.id = id;
    return s;
  }

  // ---- mount (dashboard plugin contract) -----------------------------------
  // The dashboard loads this bundle and calls the global mount with the tab's
  // root element. Support both a named global and a default export shape.
  function mount(rootEl) { render(rootEl); }

  if (typeof window !== "undefined") {
    window.HermesAuditDashboard = { mount: mount };
    // Auto-mount if the dashboard provides a conventional container.
    var auto = document.querySelector('[data-plugin="hermes-audit"]') ||
               document.getElementById("plugin-hermes-audit") ||
               document.getElementById("root");
    if (auto && !auto.__auditMounted) {
      auto.__auditMounted = true;
      mount(auto);
    }
  }
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { mount: mount };
  }
})();
