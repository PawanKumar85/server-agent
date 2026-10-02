"use strict";

// ---------- Live spider animation ----------
// Events arrive (live, from /api/run) or are replayed. Each spider's events keep their order and
// real spacing; animations only stretch gaps too short to see.
const MOVE_MS = 650, CHECK_MIN_MS = 450;
let logUserOpened = false;
const logEl = $("#log"), logList = $("ol", logEl), logState = $(".state", logEl);
$("header", logEl).onclick = () => {
  logEl.classList.toggle("collapsed");
  logUserOpened = !logEl.classList.contains("collapsed");
};
let busyUntil = {}, timers = [], lastRun = [];

function log(t, html) {
  const li = document.createElement("li");
  li.className = "new";
  li.innerHTML = `<time>+${(t / 1000).toFixed(2)}s</time><span>${html}</span>`;
  logList.appendChild(li); logList.scrollTop = logList.scrollHeight;
}
function setStats(id, text, cls) {  // text null = restore each card's own stats
  for (const el of cardsOf(id)) {
    const st = el.querySelector(".stats");
    st.textContent = text == null ? el.dataset.stats : text; st.className = "stats" + (cls ? " " + cls : "");
  }
}
function setDot(id, status, pending) {
  for (const el of cardsOf(id)) {
    const dot = el.querySelector(".dot");
    dot.classList.toggle("pending", !!pending);
    if (!pending) dot.style.background = statusColor(status);
  }
}
function applyEvent(e) {
  const who = `🕷 <b>${esc(spiderName(e.spider.replace(/^spider-/, "")))}</b>`, node = esc(e.node || "");
  if (e.kind === "move") {
    moveSpider(e.spider, e.node, MOVE_MS);
    log(e.t, `${who} → ${node}`);
  } else if (e.kind === "check" && e.shared_from) {
    // another spider already pinged this node this cycle: reuse its result, no second ping
    log(e.t, `↪ ${who} reuses <b>${esc(spiderName(e.shared_from.replace(/^spider-/, "")))}</b>'s ping of ${node}`);
  } else if (e.kind === "result" && e.shared_from) {
    // nothing to animate: the owner's result already flashed the card
  } else if (e.kind === "check") {
    const cards = cardsOf(e.node); if (!cards.length) return;
    cards.forEach(el => { el.classList.remove("flash-ok", "flash-fail"); el.classList.add("checking"); el.classList.toggle("peek", !!e.peek); });
    setDot(e.node, null, true); setStats(e.node, e.peek ? "checking from downstream…" : "pinging…", "live");
    log(e.t, `📡 ${e.peek ? "checking" : "pinging"} ${node}`);
  } else if (e.kind === "result") {
    const cards = cardsOf(e.node); if (!cards.length) return;
    cards.forEach(el => { el.classList.remove("checking", "peek"); void el.offsetWidth; el.classList.add(e.up ? "flash-ok" : "flash-fail"); });
    live[e.node] = e.up ? "UP" : "DOWN"; setDot(e.node, live[e.node]); drawEdges();
    const ms = e.latency != null ? `${Math.round(e.latency)} ms` : "";
    setStats(e.node, e.up ? `✓ UP · ${ms}` : `✗ ${e.error || "DOWN"}`, e.up ? "good" : "bad");
    setTimeout(() => setStats(e.node, null), 2200);
    log(e.t, e.up ? `<span style="color:var(--up)">✓</span> ${node} UP ${ms}`
                  : `<span style="color:var(--down)">✗</span> ${node} DOWN <span class="sub">${esc((e.error || "").slice(0, 120))}</span>`);
  } else if (e.kind === "finish") {
    paintSpider(e.spider, e.status);
    if (e.status === "STOPPED") cardsOf(e.node).forEach(el => el.classList.add("root"));
    log(e.t, `${who} <b style="color:var(--${e.status})">${e.status}</b> @ ${node}` +
             (e.root ? `<div class="sub">root cause: ${esc(e.root)}</div>` : e.impact ? `<div class="sub">${esc(e.impact)}</div>` : ""));
  }
}
function schedule(e, at) {
  const start = Math.max(at, busyUntil[e.spider] || 0);
  const moveMs = e.kind === "move" ? MOVE_MS * hopsBetween(e.source, e.node) : 0;  // longer trips take longer
  busyUntil[e.spider] = start + (e.kind === "move" ? moveMs : e.kind === "check" ? CHECK_MIN_MS : 120);
  timers.push(setTimeout(() => applyEvent(e), Math.max(0, start - performance.now())));
}
function drainedAt() { return Math.max(performance.now(), ...Object.values(busyUntil)); }
function prepareWalk(firstEvents, finals = null, quiet = false) {
  // `finals`: a channel test only resets that channel's chain; `quiet`: keep the log closed (auto ping)
  timers.forEach(clearTimeout); timers = []; busyUntil = {};
  logList.innerHTML = ""; logEl.style.display = "flex"; logEl.classList.toggle("collapsed", !logUserOpened); logState.textContent = "running…";
  const scope = finals ? new Set(finals.flatMap(f => [...chainOf(f).nodes])) : null;
  for (const n of G.nodes) {
    if (scope && !scope.has(n.id)) continue;
    live[n.id] = null; setDot(n.id, null, true); setStats(n.id, "waiting for spider…");
    cardsOf(n.id).forEach(el => el.classList.remove("root", "checking", "peek", "flash-ok", "flash-fail"));
  }
  for (const s of G.spiders) {
    if (finals && !finals.includes(s.finalLinkId)) continue;
    const first = firstEvents?.find(e => e.spider === s.id);
    if (first) spiderAt[s.id] = first.kind === "move" ? (first.source || first.node) : first.node;
    paintSpider(s.id, "RUNNING");
  }
  drawEdges(); placeSpiders();
}

// ---------- Runs: live feed from the server (/api/stream) ----------
// Every run — the server's automatic ping, "Run spiders", or a channel test, started from any
// device — is broadcast to every open page and animated here as it happens.
let running = false, runStart = 0, activeRun = null;
const runBtn = $("#run");
const SOURCE_TEXT = {scheduler: "auto ping", "scheduler-adaptive": "auto ping", manual: "manual run", channel: "channel test"};
function setRunning(on, label) {
  running = on;
  if (runBtn) {
    runBtn.disabled = on;
    runBtn.classList.toggle("running", on);
    runBtn.textContent = on ? `${label || "Spiders walking"} ` : "▶ Run spiders";
  }
  const replayBtn = $("#replay");
  if (replayBtn) replayBtn.disabled = on || !lastRun.length;
  $$("[data-test]").forEach(b => { b.disabled = on; });
}
async function startRun(final) {
  if (running) return;
  showView("workflow");
  try {
    const res = await fetch("/api/run", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(final ? {final} : {})});
    if (res.status === 409) toast("error", "Busy", "A spider run is already in progress; try again in a few seconds.");
    else if (!res.ok) toast("error", "Couldn't start the run", (await res.json().catch(() => ({}))).detail || res.statusText);
  } catch (err) { toast("error", "Couldn't start the run", err.message); }
}
function onRunStart(info) {
  activeRun = info.run; lastRun = [];
  const isAuto = info.source === "scheduler" || info.source === "scheduler-adaptive";
  const label = info.source === "channel" ? `Testing ${spiderName(info.finals[0])}` : isAuto ? "Auto ping" : "Spiders walking";
  setRunning(true, label);
  prepareWalk(null, info.finals, isAuto || !logUserOpened);
  logState.textContent = `${SOURCE_TEXT[info.source] || "run"} · running…`;
  // events carry ms since the run started; a page that joined late lines up with the server
  runStart = performance.now() - (info.joined_late ? Math.max(0, Date.now() / 1000 - info.started_at) * 1000 : 0);
}
function onRunStep(e) {
  if (e.run !== activeRun) return;
  lastRun.push(e);
  if (e.kind === "move" && !lastRun.some(x => x !== e && x.spider === e.spider)) {
    spiderAt[e.spider] = e.source || spiderAt[e.spider]; placeSpiders();  // start from where it really was
  }
  schedule(e, runStart + e.t);
}
// Typed incident alerts: what failed (media stale vs playlist missing vs unreachable), where, and the verdict.
const CATEGORY_TEXT = {STALE_MEDIA: "HLS MEDIA STALE", PLAYLIST_MISSING: "PLAYLIST MISSING (404)", UNREACHABLE: "UNREACHABLE",
  SERVER_ERROR: "SERVER ERROR (5xx)", HTTP_ERROR: "HTTP ERROR", NO_SEGMENTS: "NO SEGMENTS", INVALID_PLAYLIST: "INVALID PLAYLIST"};
const categoryText = c => CATEGORY_TEXT[c] || (c ? c.replace(/_/g, " ") : "FAILURE");
// Failures, root causes and early warnings show on the server cards themselves (node-alerts.js).
document.addEventListener("click", e => {
  const a = e.target.closest("[data-focus]");
  if (a) { e.preventDefault(); if (byId[a.dataset.focus]) showDetail(a.dataset.focus); }
});
async function loadRanking() {
  const [ranking, anomalies, glitches] = await Promise.allSettled([api("GET", "/api/rca/ranking"), api("GET", "/api/anomalies"),
                                                                   api("GET", "/api/glitches?hours=24")]);
  if (glitches.status === "fulfilled") setGlitches(glitches.value.data);
  api("GET", "/api/scte").then(r => setScte(r.data)).catch(() => {});
  api("GET", "/api/alerts?hours=1").then(r => setUpcoming(r.data)).catch(() => {});
  if (ranking.status === "fulfilled") setRanking(ranking.value.data.groups || []);
  if (anomalies.status === "fulfilled") {
    setWarnings(Object.entries(anomalies.value.data || {})
      .filter(([node, a]) => !a.down && !node.endsWith(".invalid"))
      .map(([node, a]) => ({node, warnings: a.warnings || []})));
  }
}

async function finishRun(result) {
  if (result.run !== activeRun) return;
  activeRun = null;
  logState.textContent = `${SOURCE_TEXT[result.source] || "run"} · done · real walk ${(result.elapsed_ms / 1000).toFixed(1)}s ▾`;
  // Problems show on the server cards (node-alerts.js), not as pop-ups.
  noteIncidents(result.incidents);
  api("GET", "/api/glitches?hours=24").then(r => setGlitches(r.data)).catch(() => {});
  api("GET", "/api/scte").then(r => setScte(r.data)).catch(() => {});
  api("GET", "/api/alerts?hours=1").then(r => setUpcoming(r.data)).catch(() => {});
  nodeAlerts.groups = result.ranking || [];
  nodeAlerts.warnings = Object.fromEntries((result.warnings || []).filter(w => w.warnings.length).map(w => [w.node, w.warnings[0]]));
  setRunning(false);
  await refreshAll();
  if (typeof refreshHeatmapImage === "function") refreshHeatmapImage();  // gone with the old heatmap image
  if (!logUserOpened) setTimeout(() => logEl.classList.add("collapsed"), 2000);
}
function connectStream() {
  const feed = new EventSource("/api/stream");  // reconnects by itself if the server restarts
  feed.addEventListener("scheduler", m => renderScheduler(JSON.parse(m.data)));
  feed.addEventListener("traceroute", m => onTracerouteEvent(JSON.parse(m.data)));
  // New glitches on a Final: refresh the forecast (debounced: one probe round reports all Finals at once).
  let glitchTimer = null;
  feed.addEventListener("glitch", () => {
    clearTimeout(glitchTimer);
    glitchTimer = setTimeout(() => api("GET", "/api/glitches?hours=24").then(r => setGlitches(r.data)).catch(() => {}), 1500);
  });
  feed.addEventListener("embeddings", m => onEmbeddingsEvent(JSON.parse(m.data)));
  feed.addEventListener("start", m => onRunStart(JSON.parse(m.data)));
  feed.addEventListener("step", m => onRunStep(JSON.parse(m.data)));
  feed.addEventListener("done", m => {
    const result = JSON.parse(m.data);
    setTimeout(() => finishRun(result), drainedAt() - performance.now() + 500);
  });
  feed.addEventListener("error", m => {
    if (!m.data) {  // connection blip (EventSource retries on its own), or the session expired
      fetch("/api/scheduler").catch(() => {});  // a 401 here sends us to the login page
      return;
    }
    toast("error", "Spider run failed", JSON.parse(m.data).message);
    logState.textContent = "failed"; activeRun = null;
    setRunning(false); refreshAll();
  });
}
if (runBtn) runBtn.onclick = () => startRun(null);
document.addEventListener("click", e => {  // "▶ Test ping" buttons (spider cards, node details)
  const b = e.target.closest("[data-test]");
  if (b) startRun(b.dataset.test);
});

$("#replay").onclick = () => {
  if (!lastRun.length || running) return;
  prepareWalk(lastRun);
  const base = performance.now() + 300;
  for (const e of lastRun) schedule(e, base + e.t);
  timers.push(setTimeout(() => {
    logState.textContent = `replayed · real walk ${(Math.max(...lastRun.map(e => e.t)) / 1000).toFixed(1)}s ▾`;
    refreshAll();
  }, drainedAt() - performance.now() + 500));
};

// ---------- Automatic ping (server-side scheduler) ----------
const schedOn = $("#sched-on"), schedInterval = $("#sched-interval"), schedStatus = $("#sched-status");
const OUTDATED_SERVER = "The server is running older code: restart it (uvicorn server:app --host 0.0.0.0 --port 8000)";
async function checkServerVersion() {  // the page is newer than a server that wasn't restarted
  const res = await fetch("/api/scheduler").catch(() => null);
  if (res && res.status === 404) {
    if (schedStatus) { schedStatus.textContent = "restart server"; schedStatus.className = "sched-status off"; }
    if (schedOn) schedOn.disabled = true;
    if (schedInterval) schedInterval.disabled = true;
    toast("error", "Server needs a restart", OUTDATED_SERVER);
    return false;
  }
  return true;
}
let sched = null, clockSkew = 0;  // server time - local time, for the countdown
function renderScheduler(state) {
  sched = state; clockSkew = state.server_time - Date.now() / 1000;
  if (schedOn) schedOn.checked = state.enabled;
  if (schedInterval && document.activeElement !== schedInterval) schedInterval.value = state.interval;
  tickScheduler();
}
function tickScheduler() {
  if (!sched || !schedStatus) return;
  let text, cls;
  if (running || sched.running) { text = "pinging…"; cls = "busy"; }
  else if (!sched.enabled) { text = "stopped"; cls = "off"; }
  else {
    const left = Math.max(0, Math.ceil(sched.next_run_at - (Date.now() / 1000 + clockSkew)));
    text = `next in ${left}s`; cls = "on";
  }
  schedStatus.textContent = text; schedStatus.className = "sched-status " + cls;
}
setInterval(tickScheduler, 1000);
async function saveScheduler(change) {
  try {
    const res = await fetch("/api/scheduler", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(change)});
    if (res.status === 404) throw new Error(OUTDATED_SERVER);
    const data = await res.json();
    if (!res.ok) throw new Error(res.status === 422 ? "Interval must be 10–3600 seconds" : data.detail || res.statusText);
    renderScheduler(data);
    toast("ok", "Auto ping", data.enabled ? `On · every ${data.interval} s` : "Stopped");
  } catch (err) {
    toast("error", "Couldn't change auto ping", err.message);
    if (sched) renderScheduler(sched);
  }
}
if (schedOn) schedOn.onchange = () => saveScheduler({enabled: schedOn.checked});
if (schedInterval) schedInterval.onchange = () => saveScheduler({interval: Number(schedInterval.value)});
