"use strict";

// ---------- Server history charts (GET /api/nodes/<id>/timeline) ----------
// A compact 7-day chart in the server panel, and a full view with four stacked charts:
//   1. Availability: checks passed per time bucket (bars, green → red) with incidents as markers
//   2. Response time: HTTP latency, ICMP RTT and jitter (lines)
//   3. Segment age: average (line) and worst (dots), with each stream's learned normal and warning line
//   4. Failures: failed checks (bars) and packet loss (line)
// Times are IST; hovering shows every value at that moment; legend items hide/show their series.

const historyView = {node: null, since: 7 * 86400, charts: [], compact: null};
const HISTORY_RANGES = [["24 h", 86400], ["3 days", 3 * 86400], ["7 days", 7 * 86400]];

const cssVar = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
function historyColors() {
  return {
    up: cssVar("--up") || "#16a34a", down: cssVar("--down") || "#dc2626", warn: cssVar("--backup-feed") || "#f59e0b",
    text: cssVar("--text") || "#111", muted: cssVar("--muted") || "#6b7280", grid: cssVar("--card-border") || "#e5e7eb",
    latency: "#2563eb", rtt: "#7c3aed", ad: "#a855f7", jitter: "#0d9488", age: "#0891b2", ageMax: "#ea580c", loss: "#f97316",
  };
}

const istTime = t => new Date(t * 1000).toLocaleString("en-IN", {timeZone: "Asia/Kolkata", day: "2-digit", month: "short",
  hour: "2-digit", minute: "2-digit", hour12: false});
const availabilityColor = (up, c) => up >= 99.5 ? c.up : up >= 90 ? c.warn : c.down;

// Options shared by every chart: a linear time axis (seconds) in IST, one tooltip for all series at a moment.
function baseOptions(data, c, {yTitle, yMax, compact = false, extraScales = {}} = {}) {
  return {
    responsive: true, maintainAspectRatio: false, animation: false, parsing: false, normalized: true,
    interaction: {mode: "nearest", axis: "x", intersect: false},
    plugins: {
      legend: {display: !compact, position: "top", align: "start",
               labels: {color: c.text, boxWidth: 10, boxHeight: 10, usePointStyle: true, font: {size: 11}}},
      tooltip: {callbacks: {title: items => items.length ? istTime(items[0].parsed.x) + " IST" : ""}},
    },
    scales: {
      x: {type: "linear", min: data.from, max: data.to, grid: {color: c.grid},
          ticks: {color: c.muted, maxTicksLimit: compact ? 4 : 8, font: {size: 10}, callback: v => istTime(v)}},
      y: {beginAtZero: true, ...(yMax ? {max: yMax} : {}), grid: {color: c.grid},
          title: {display: !!yTitle && !compact, text: yTitle, color: c.muted, font: {size: 11}},
          ticks: {color: c.muted, font: {size: 10}, maxTicksLimit: compact ? 3 : 6}},
      ...extraScales,
    },
  };
}

// A series, with a break (null) wherever no checks were made for a while, so lines don't bridge the gap.
let gapS = Infinity;
function series(points, key) {
  const out = [];
  let prev = null;
  for (const p of points) {
    if (p[key] == null) continue;
    if (prev && p.t - prev.t > gapS) out.push({x: (prev.t + p.t) / 2, y: null});
    out.push({x: p.t, y: p[key]}); prev = p;
  }
  return out;
}
const INCIDENT_STYLE = {OUTAGE: ["triangle", "down", "Went down"], RECOVERY: ["circle", "up", "Recovered"],
                        BLIP: ["crossRot", "muted", "Blip (too short for an outage)"]};

function availabilityChart(canvas, data, c) {
  const bars = data.points.map(p => ({x: p.t, y: p.up}));
  const incidents = Object.entries(INCIDENT_STYLE).map(([type, [shape, tone, label]]) => ({
    type: "scatter", label, data: data.incidents.filter(i => i.type === type).map(i => ({x: i.t, y: 104, i})),
    pointStyle: shape, pointRadius: type === "BLIP" ? 4 : 6, pointHoverRadius: 8, backgroundColor: c[tone], borderColor: c[tone],
    borderWidth: type === "BLIP" ? 1.5 : 1,
  })).filter(d => d.data.length);
  const ads = (data.adBreaks || []).filter(b => b.start >= data.from);
  if (ads.length) {
    incidents.push({type: "scatter", label: "Ad break (SCTE-35)", data: ads.map(b => ({x: b.start, y: 101, b})),
      pointStyle: "rect", pointRadius: 4, pointHoverRadius: 6, backgroundColor: c.ad, borderColor: c.ad});
  }
  const glitches = (data.glitches || []).filter(g => g.ts >= data.from);
  if (glitches.length) {
    incidents.push({type: "scatter", label: "Glitch (stream kept playing)", data: glitches.map(g => ({x: g.ts, y: 97, g})),
      pointStyle: "rectRot", pointRadius: 5, pointHoverRadius: 7, backgroundColor: c.warn, borderColor: c.warn});
  }
  const opts = baseOptions(data, c, {yTitle: "checks passed %", yMax: 108});
  opts.plugins.tooltip.callbacks.label = ctx => {
    if (ctx.raw.b) {
      const b = ctx.raw.b, m = v => v == null ? "?" : v < 600 ? `${Math.round(v)} s` : `${Math.round(v / 60)} min`;
      return `Ad break: ${m(b.actual_s)}${b.planned_s ? ` of ${m(b.planned_s)} planned` : ""} · ${b.status.toLowerCase()}`;
    }
    if (ctx.raw.g) {
      const g = ctx.raw.g;
      return `Glitch: ${g.words}${g.count > 1 ? ` ×${g.count}` : ""}${g.detail ? ` · ${g.detail}` : ""}`;
    }
    if (ctx.raw.i) {
      const i = ctx.raw.i;
      const lasted = i.durationS == null ? "" : i.durationS < 90 ? ` · lasted ${Math.round(i.durationS)} s` : ` · lasted ${Math.round(i.durationS / 60)} min`;
      return `${ctx.dataset.label}${i.category ? ` · ${categoryText(i.category)}` : ""}${lasted}${i.checks ? ` · ${i.checks} failed check${i.checks === 1 ? "" : "s"}` : ""}`;
    }
    const p = data.points[ctx.dataIndex];
    return `Checks passed: ${p.up}% (${p.checks - p.fails} of ${p.checks})`;
  };
  return new Chart(canvas, {data: {datasets: [
    {type: "bar", label: "Checks passed %", data: bars, backgroundColor: bars.map(b => availabilityColor(b.y, c)),
     barPercentage: 1, categoryPercentage: 1, grouped: false},
    ...incidents]}, options: opts});
}

function responseChart(canvas, data, c) {
  const line = (label, key, color, extra = {}) => ({type: "line", label, data: series(data.points, key), borderColor: color,
    backgroundColor: color, borderWidth: 1.6, pointRadius: 0, pointHoverRadius: 3, tension: .25, spanGaps: false, ...extra});
  return new Chart(canvas, {data: {datasets: [
    line("HTTP latency (ms)", "latency", c.latency),
    line("ICMP RTT (ms)", "rtt", c.rtt),
    line("Jitter (ms)", "jitter", c.jitter, {borderDash: [4, 3]}),
  ]}, options: baseOptions(data, c, {yTitle: "milliseconds"})});
}

function segmentChart(canvas, data, c, compact = false) {
  const flat = (label, y, color, dash) => ({type: "line", label, data: [{x: data.from, y}, {x: data.to, y}],
    borderColor: color, borderWidth: 1.2, borderDash: dash, pointRadius: 0, pointHoverRadius: 0});
  const channelOf = url => ((byId[data.node]?.links || []).find(l => l.url === url) || {}).channel || "stream";
  const lines = compact ? [] : data.segments.flatMap(a => [
    flat(`Normal · ${channelOf(a.url)} (${a.baseline.median} s)`, a.baseline.median, c.muted, [2, 3]),
    flat(`Warns above · ${channelOf(a.url)} (${a.warn} s)`, a.warn, c.down, [6, 4]),
  ]);
  return new Chart(canvas, {data: {datasets: [
    {type: "line", label: "Segment age, average (s)", data: series(data.points, "age"), borderColor: c.age,
     backgroundColor: c.age + "22", fill: true, borderWidth: 1.6, pointRadius: 0, pointHoverRadius: 3, tension: .25},
    {type: "scatter", label: "Segment age, worst (s)", data: series(data.points, "ageMax"), backgroundColor: c.ageMax,
     pointRadius: compact ? 1.2 : 2, pointHoverRadius: 4},
    ...lines]}, options: baseOptions(data, c, {yTitle: "seconds", compact})});
}

function failureChart(canvas, data, c) {
  const opts = baseOptions(data, c, {yTitle: "failed checks", extraScales: {
    y2: {position: "right", beginAtZero: true, max: 100, grid: {drawOnChartArea: false},
         title: {display: true, text: "packet loss %", color: c.muted, font: {size: 11}}, ticks: {color: c.muted, font: {size: 10}}}}});
  return new Chart(canvas, {data: {datasets: [
    {type: "bar", label: "Failed checks", data: series(data.points, "fails"), backgroundColor: c.down,
     barPercentage: 1, categoryPercentage: 1},
    {type: "line", label: "Packet loss (%)", yAxisID: "y2", data: series(data.points, "loss"), borderColor: c.loss,
     backgroundColor: c.loss, borderWidth: 1.4, pointRadius: 0, pointHoverRadius: 3, stepped: true},
  ]}, options: opts});
}

// A combined small chart for the panel: availability as a coloured strip, latency and segment age as lines.
function compactChart(canvas, data, c) {
  const opts = baseOptions(data, c, {compact: true, extraScales: {
    y2: {position: "right", beginAtZero: true, grid: {drawOnChartArea: false}, ticks: {color: c.muted, font: {size: 9}, maxTicksLimit: 3}},
    yA: {display: false, min: 0, max: 100}}});
  opts.plugins.legend = {display: true, position: "bottom", labels: {color: c.muted, boxWidth: 8, boxHeight: 8, usePointStyle: true, font: {size: 10}}};
  return new Chart(canvas, {data: {datasets: [
    {type: "bar", label: "Checks passed", yAxisID: "yA", data: data.points.map(p => ({x: p.t, y: 100})),
     backgroundColor: data.points.map(p => availabilityColor(p.up, c) + "40"), barPercentage: 1, categoryPercentage: 1},
    {type: "line", label: "Latency ms", data: series(data.points, "latency"), borderColor: c.latency, borderWidth: 1.4,
     pointRadius: 0, tension: .25},
    {type: "line", label: "Segment age s", yAxisID: "y2", data: series(data.points, "age"), borderColor: c.age,
     borderWidth: 1.4, pointRadius: 0, tension: .25},
    {type: "scatter", label: "Incidents", yAxisID: "yA",
     data: data.incidents.filter(i => i.type === "OUTAGE").map(i => ({x: i.t, y: 92})), backgroundColor: c.down,
     pointStyle: "triangle", pointRadius: 4},
    ...((data.glitches || []).length ? [{type: "scatter", label: "Glitches", yAxisID: "yA",
      data: data.glitches.filter(g => g.ts >= data.from).map(g => ({x: g.ts, y: 84})), backgroundColor: c.warn,
      pointStyle: "rectRot", pointRadius: 3}] : []),
  ]}, options: opts});
}

async function fetchTimeline(id, since) {
  const res = await fetch(`/api/nodes/${encodeURIComponent(id).replace(/%2F/g, "/")}/timeline?since=${since}`);
  if (!res.ok) throw new Error(res.statusText);
  return res.json();
}

function historySummary(data) {
  const up = data.checks ? ((data.checks - data.fails) / data.checks * 100).toFixed(1) : null;
  const outages = data.incidents.filter(i => i.type === "OUTAGE").length;
  const glitchCount = (data.glitches || []).reduce((n, g) => n + (g.count || 1), 0);
  if (!data.checks) return "No checks recorded in this period yet.";
  const span = (data.to - data.from) / 3600;
  const only = data.clipped ? `Only ${span < 48 ? `${Math.max(1, Math.round(span))} h` : `${Math.round(span / 24)} days`} of history so far · ` : "";
  return `${only}${up}% of ${data.checks.toLocaleString()} checks passed · ${outages} outage${outages === 1 ? "" : "s"}` +
    (data.glitches ? ` · ${glitchCount} glitch${glitchCount === 1 ? "" : "es"}` : "");
}

// The panel's compact chart (called by node-detail.js when a panel opens).
async function loadCompactHistory(id) {
  const box = $("#history-section");
  if (!box || typeof Chart === "undefined") return;
  try {
    const data = await fetchTimeline(id, 7 * 86400);
    if (box.dataset.node !== id) return;
    historyView.compact?.destroy();
    box.innerHTML = `<h4 class="trace-title">${data.clipped ? "History" : "Last 7 days"} <button type="button" class="nd-seg-btn" data-history="${esc(id)}">Open full history</button></h4>
      <div class="sub">${esc(historySummary(data))}</div>
      ${data.points.length ? `<div class="hist-compact"><canvas aria-label="7-day history chart"></canvas></div>` : ""}`;
    gapS = data.bucketS * 3;
    if (data.points.length) historyView.compact = compactChart(box.querySelector("canvas"), data, historyColors());
  } catch { box.innerHTML = ""; }
}

function updateExportButtons(id, since) {
  const activeRange = HISTORY_RANGES.find(([, s]) => s === since);
  const rangeLabel = activeRange ? activeRange[0] : "";
  const dlBtn = $("#hist-btn-download");
  const dlText = $("#hist-dl-text");
  const csvBtn = $("#hist-btn-csv");
  const csvText = $("#hist-csv-text");
  if (dlText) dlText.textContent = rangeLabel ? `Download Report (${rangeLabel})` : "Download Report";
  if (dlBtn) dlBtn.title = `Download comprehensive HTML report for ${id} (${rangeLabel})`;
  if (csvText) csvText.textContent = rangeLabel ? `CSV (${rangeLabel})` : "CSV";
  if (csvBtn) csvBtn.title = `Export CSV timeline metrics for ${id} (${rangeLabel})`;
}

function triggerDownload(url) {
  const a = document.createElement("a");
  a.href = url;
  a.setAttribute("download", "");
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function downloadNodeReport(node, since) {
  if (!node) return;
  triggerDownload(`/api/report.html?node=${encodeURIComponent(node)}&since=${since}&download=1`);
}

function downloadNodeCsv(node, since) {
  if (!node) return;
  triggerDownload(`/api/nodes/${encodeURIComponent(node).replace(/%2F/g, "/")}/timeline.csv?since=${since}`);
}

// AI summary of the period (history_ai.py): facts computed on the server, the model only explains them.
function resetHistoryAi() {
  historyView.aiAbort?.abort();
  historyView.aiAbort = null;
  const body = $("#hist-ai-body");
  body.hidden = true;
  body.innerHTML = "";
  $("#hist-ai-run").disabled = false;
  $("#hist-ai-run").textContent = "Explain with AI";
}

async function runHistoryAi() {
  const id = historyView.node, since = historyView.since;
  if (!id) return;
  resetHistoryAi();
  const body = $("#hist-ai-body"), btn = $("#hist-ai-run");
  const ctl = new AbortController();
  historyView.aiAbort = ctl;
  btn.disabled = true;
  btn.textContent = "Thinking…";
  body.hidden = false;
  body.innerHTML = `<p class="sub">Reading ${esc(id)}'s history…</p>`;
  let text = "", done = null, failed = null;
  const md = t => (typeof renderMarkdown === "function" ? renderMarkdown(t) : `<p>${esc(t)}</p>`);
  const draw = () => {
    const foot = done ? `<div class="hist-ai-foot">${done.cached ? "from a moment ago · " : ""}${done.model ? esc(done.model.split("/").pop()) + " · " : ""}${done.elapsed_ms ? (done.elapsed_ms / 1000).toFixed(1) + " s" : ""}</div>` : "";
    body.innerHTML = (failed ? `<p class="learn-down">${esc(failed)}</p>` : "") + (text ? md(text) : "") + foot;
  };
  try {
    const res = await fetch(`/api/nodes/${encodeURIComponent(id).replace(/%2F/g, "/")}/history-ai?since=${since}`,
                            {method: "POST", signal: ctl.signal});
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const reader = res.body.getReader(), decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const {value, done: ended} = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), {stream: !ended});
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const line of lines.filter(Boolean)) {
        const ev = JSON.parse(line);
        if (ev.type === "token") text += ev.text;
        else if (ev.type === "done") done = ev;
        else if (ev.type === "error") failed = ev.message;
      }
      if (historyView.node !== id || historyView.since !== since) return;  // switched away meanwhile
      draw();
      if (ended) break;
    }
    if (!text && !failed) { failed = "No answer came back."; draw(); }
  } catch (err) {
    if (err.name !== "AbortError") { failed = `Couldn't explain the history: ${err.message}`; draw(); }
  } finally {
    if (historyView.aiAbort === ctl) {
      btn.disabled = false;
      btn.textContent = text ? "Explain again" : "Explain with AI";
    }
  }
}

function askAgentAboutHistory() {
  const id = historyView.node;
  if (!id) return;
  const range = (HISTORY_RANGES.find(([, s]) => s === historyView.since) || ["7 days"])[0];
  closeHistory();
  showView("agent");
  const input = document.getElementById("chat-input");
  if (input) {
    input.value = `About ${id} over the last ${range}: why did it fail, and what should I fix first?`;
    input.focus();
    input.dispatchEvent(new Event("input"));
  }
}

async function openHistory(id, since = historyView.since) {
  if (historyView.node !== id || historyView.since !== since) resetHistoryAi();
  historyView.node = id; historyView.since = since;
  const modal = $("#history-modal");
  modal.hidden = false;
  $("#history-title").textContent = id;
  $("#history-ranges").innerHTML = HISTORY_RANGES.map(([label, s]) =>
    `<button type="button" class="hist-range${s === since ? " active" : ""}" data-range="${s}">${label}</button>`).join("");
  updateExportButtons(id, since);
  $("#history-summary").textContent = "Loading…";
  try {
    const data = await fetchTimeline(id, since);
    if (historyView.node !== id) return;
    historyView.charts.forEach(ch => ch.destroy());
    const c = historyColors();
    gapS = data.bucketS * 3;
    $("#history-summary").textContent = historySummary(data) + ` · each bar is ${Math.round(data.bucketS / 60)} min`;
    historyView.charts = [
      availabilityChart($("#hist-availability"), data, c),
      responseChart($("#hist-response"), data, c),
      segmentChart($("#hist-segment"), data, c),
      failureChart($("#hist-failures"), data, c),
    ];
  } catch (err) {
    $("#history-summary").textContent = `Couldn't load the history: ${err.message}`;
  }
}

function closeHistory() {
  resetHistoryAi();
  $("#history-modal").hidden = true;
  historyView.charts.forEach(ch => ch.destroy());
  historyView.charts = []; historyView.node = null;
}

document.addEventListener("click", e => {
  const open = e.target.closest("[data-history]");
  if (open) { openHistory(open.dataset.history); return; }
  const range = e.target.closest("#history-ranges [data-range]");
  if (range && historyView.node) { openHistory(historyView.node, Number(range.dataset.range)); return; }
  const dl = e.target.closest("#hist-btn-download");
  if (dl && historyView.node) { downloadNodeReport(historyView.node, historyView.since); return; }
  if (e.target.closest("#hist-ai-run")) { runHistoryAi(); return; }
  if (e.target.closest("#hist-ai-ask")) { askAgentAboutHistory(); return; }
  const csv = e.target.closest("#hist-btn-csv");
  if (csv && historyView.node) { downloadNodeCsv(historyView.node, historyView.since); return; }
  if (e.target.id === "history-modal" || e.target.closest("#history-close")) closeHistory();
});
document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("#history-modal").hidden) closeHistory(); });
