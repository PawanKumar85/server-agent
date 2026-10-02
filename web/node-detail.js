"use strict";

// ---------- Node detail ----------
const detail = $("#detail");
$(".close", detail).onclick = () => { detail.style.display = "none"; $$(".node.selected").forEach(x => x.classList.remove("selected")); };
// The panel reads top to bottom as: what is this server, is it OK (in plain words), what can I do, the numbers,
// each stream it serves, where it sits in the pipeline, and then the history and diagnostics.
const ndShortHost = id => String(id || "").replace(".ottlive.co.in", "").replace(/\.co\.in$/, "");

function ndAgo(iso) {
  const t = Date.parse(String(iso || "").replace(/(\.\d{3})\d+/, "$1"));
  if (!t) return null;
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  return s < 90 ? `${s} s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 172800 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} days ago`;
}

// One sentence on what's wrong with a failing URL, in everyday words.
function ndProblem(h) {
  const behind = String(h.detail || "").match(/FALLING_BEHIND \(advancing, but (\d+)s behind live, (-?\d+)s more/);
  if (behind) return `Still playing, but slower than real time: ${behind[1]} s behind live and slipping (+${behind[2]} s since the last check). Viewers will start buffering.`;
  if (h.freshness && h.freshness !== "FRESH" && h.segmentAgeS != null && h.segmentAgeS >= 0) {
    return `The video stopped updating: the newest piece is ${Math.round(h.segmentAgeS)} s old` +
      (h.targetS ? ` (each piece is ${h.targetS} s long, so it should be under ${Math.round(h.targetS * 1.5)} s).` : ".");
  }
  return h.category ? `${categoryText(h.category).toLowerCase().replace(/^\w/, c => c.toUpperCase())}${h.detail ? `: ${h.detail}` : ""}.` : (h.detail || "");
}

function ndState(n) {
  const failing = Object.values(n.urlHealth || {}).filter(h => h.up === false);
  if (n.status === "DOWN") {
    const what = failing.length ? ndProblem(failing[0]) : (n.lastError || "");
    const since = failing.map(h => h.onsetAt).filter(Boolean).sort()[0];
    return {tone: "down", head: `Down${since ? ` since ${ndAgo(since)}` : ""}`,
            text: what + (failing.length > 1 ? ` ${failing.length} of its streams are failing.` : "")};
  }
  if (n.status === "UP" && failing.length) {
    return {tone: "warn", head: "Up, with problems", text: ndProblem(failing[0])};
  }
  if (n.status === "UP") {
    const count = (n.urls || []).length;
    return {tone: "up", head: "Working normally",
            text: (count === 1 ? "Its stream is live" : `All ${count} streams are live`) +
                  (n.latency != null ? `, answering in ${Math.round(n.latency)} ms.` : ".")};
  }
  return {tone: "idle", head: "Not checked yet", text: "Run a check to see its health."};
}

function ndStream(u, h, l) {
  const tone = h.up === false ? "down" : h.up ? "up" : "idle";
  let path = u, host = "";
  try { const x = new URL(u); path = x.pathname + x.search; host = x.host; } catch { /* not a URL */ }
  const target = h.targetS || null, age = h.segmentAgeS;
  const meter = age != null && age >= 0 && target
    ? `<div class="nd-meter" title="Newest piece ${Math.round(age)} s old; pieces are ${target} s long">
         <span class="nd-meter-fill fresh-${esc(h.freshness || "")}" style="width:${Math.min(100, age / (target * 3) * 100).toFixed(0)}%"></span>
         <span class="nd-meter-mark" style="left:${(1.5 / 3 * 100).toFixed(0)}%"></span></div>`
    : "";
  const ageText = age == null ? "" : age < -2 ? `newest piece just now (the source clock runs ${Math.round(-age)} s ahead)`
    : age < 1 ? `newest piece just now${target ? ` · pieces ${target} s` : ""}`
    : `newest piece ${Math.round(age)} s old${target ? ` · pieces ${target} s` : ""}`;
  return `<li class="nd-stream nd-${tone}">
    <div class="nd-stream-top">
      <span class="nd-badge nd-badge-${tone}">${h.up === false ? "Failing" : h.up ? "Live" : "Unchecked"}</span>
      ${h.freshness ? `<span class="fresh fresh-${esc(h.freshness)}">${esc(h.freshness)}</span>` : ""}
      <span class="nd-stream-role">${esc(short(l.role || ""))}${l.channel ? ` · ${esc(l.channel)}` : ""}</span>
    </div>
    <a class="nd-url" href="${esc(u)}" target="_blank" rel="noopener" title="${esc(u)}"><span>${esc(host)}</span>${esc(path)}</a>
    ${(() => {
      const badges = [];
      if (h.resolutions && h.resolutions.length) {
        const bitText = h.bitrates && h.bitrates[0] ? ` @ ${(h.bitrates[0] / 1000000).toFixed(1)} Mbps` : "";
        badges.push(`<span class="nd-badge-pill" title="Resolution & Bitrate">📺 ${esc(h.resolutions[0])}${bitText}</span>`);
      }
      if (h.cdn_cache) {
        const isHit = String(h.cdn_cache).toUpperCase().includes("HIT");
        badges.push(`<span class="nd-badge-pill ${isHit ? 'cdn-hit' : 'cdn-miss'}" title="CDN Cache Status">⚡ CDN: ${esc(h.cdn_cache)}</span>`);
      }
      if (h.server_hdr) {
        badges.push(`<span class="nd-badge-pill" title="Origin Web Server">🖥️ ${esc(h.server_hdr)}</span>`);
      }
      if (h.discontinuities > 0) {
        badges.push(`<span class="nd-badge-pill discont" title="Media Discontinuities">⚠️ ${h.discontinuities} Discont</span>`);
      }
      return badges.length ? `<div class="nd-stream-badges">${badges.join("")}</div>` : "";
    })()}
    ${meter}
    ${ageText ? `<div class="sub">${esc(ageText)}</div>` : ""}
    ${h.up === false && h.detail ? `<div class="nd-stream-err">${esc(h.detail)}</div>` : ""}
    ${h.up === false && h.onsetAt ? `<div class="sub">Stopped ${esc(fmtTime(h.onsetAt))} (±${Math.round(h.onsetPrecisionS || 0)} s, from ${esc(h.onsetMethod || "checks")})</div>` : ""}
    ${h.lastDown && h.up !== false ? `<div class="sub">Last failed ${esc(ndAgo(h.lastDown) || fmtTime(h.lastDown))}</div>` : ""}
  </li>`;
}

function showDetail(id) {
  const n = byId[id];
  $$(".node.selected").forEach(x => x.classList.remove("selected"));
  cardsOf(id).forEach(el => el.classList.add("selected"));
  const role = Object.fromEntries((n.links || []).map(l => [l.url, l]));
  const streams = (n.urls || []).map(u => ndStream(u, (n.urlHealth || {})[u] || {}, role[u] || {})).join("");
  const state = ndState(n);
  const pings = n.pingCount ?? 0, failed = n.failedCount ?? 0;
  const uptime = pings ? ((pings - failed) / pings * 100) : null;
  const stat = (label, value, sub, tone = "") =>
    `<div class="nd-stat ${tone}"><span class="nd-stat-l">${label}</span><span class="nd-stat-v">${value}</span>${sub ? `<span class="nd-stat-s">${sub}</span>` : ""}</div>`;
  const chip = (other, type) => `<button type="button" class="nd-chip" data-goto="${esc(other)}" title="${esc(other)} (${type})">
    <span class="dot" style="background:${statusColor((byId[other] || {}).status)}"></span>${esc(ndShortHost(other))}</button>`;
  const ups = G.edges.filter(e => e.target === id).map(e => chip(e.source, e.type)).join("");
  const downs = G.edges.filter(e => e.source === id).map(e => chip(e.target, e.type)).join("");
  const spiders = G.spiders.filter(s => spiderAt[s.id] === id).map(s =>
    `<li>🕷 <b>${esc(s.id)}</b> <span style="color:var(--${s.status})">${esc(s.status)}</span>${s.stopReason ? `<div class="sub">${esc(s.stopReason)}</div>` : ""}</li>`).join("");
  const recent = (n.log || []).slice(-10).reverse().map(l => {
    const bad = l.type !== "RECOVERY", corr = (l.correlation || [])[0];
    return `<li class="nd-event ${bad ? "nd-down" : "nd-up"}">
      <div><b>${esc(l.type === "RECOVERY" ? "Recovered" : l.type === "ESCALATED" ? "Still down (escalated)" : "Went down")}</b>
        ${l.category ? `<span class="cat">${esc(categoryText(l.category))}</span>` : ""}
        <span class="sub">· ${esc(fmtTime(l.timestamp))}${l.durationS != null ? ` · lasted ${Math.round(l.durationS)} s` : ""}</span></div>
      ${corr ? `<div class="sub corr"><b>${esc(corr.verdict.replace(/_/g, " ").toLowerCase())}</b>: ${esc(corr.summary)}</div>` : ""}
      ${l.lastError ? `<div class="nd-stream-err">${esc(l.lastError)}</div>` : ""}</li>`;
  }).join("");

  $("#detail-body").innerHTML = `
    <header class="nd-head">
      <h3><span class="dot" style="background:${statusColor(n.status)}"></span><span class="nd-name">${esc(id)}</span></h3>
      <div class="nd-meta">${n.labels.map(r => `<span class="nd-role">${esc(short(r))}</span>`).join("")}
        ${n.serverIp ? `<span class="nd-ip" title="Server IP">${esc(n.serverIp)}</span>` : ""}</div>
    </header>
    <div class="nd-state nd-state-${state.tone}" role="status">
      <b>${esc(state.head)}</b>${state.text ? `<p>${esc(state.text)}</p>` : ""}
    </div>
    <div class="nd-actions">
      ${n.labels.includes("FinalLink") ? `<button class="btn small" data-test="${esc(id)}" ${running ? "disabled" : ""}>▶ Test this channel</button>` : ""}
      <button class="btn small" data-rca="${esc(id)}" title="NOC root-cause analysis from the health checks and the latest traceroute">🧠 Find the cause</button>
      <button class="btn small" data-trace="${esc(id)}" title="Trace the network path to this server now">↯ Traceroute</button>
      <a class="btn small" href="/api/report.html?node=${encodeURIComponent(id)}" target="_blank" rel="noopener" title="Complete report on this node: charts and every stored field">📄 Report</a>
    </div>
    <div class="nd-stats">
      ${stat("Checks passed", uptime == null ? "—" : `${uptime.toFixed(1)}%`, pings ? `${failed.toLocaleString()} of ${pings.toLocaleString()} failed` : "", uptime != null && uptime < 95 ? "warn" : "")}
      ${stat("Response time", n.latency != null ? `${Math.round(n.latency)} ms` : "—", "HTTP latency")}
      ${stat("Last check", ndAgo(n.lastPing) || "—", n.consecutiveFailures ? `${n.consecutiveFailures} failed in a row` : "", n.consecutiveFailures ? "bad" : "")}
      ${stat("Last recovery", ndAgo(n.lastRecovery) || "never", "")}
      ${stat("Blips", (n.blipCount ?? 0).toLocaleString(), "too short to count as an outage", (n.blipCount || 0) > 10 ? "warn" : "")}
    </div>

    <h4 class="trace-title">Streams <span class="nd-count">${(n.urls || []).length}</span></h4>
    ${streams ? `<ul class="nd-streams">${streams}</ul>` : `<p class="sub">No stream URLs on this server.</p>`}

    <h4 class="trace-title">Pipeline</h4>
    <div class="nd-flow">
      <div class="nd-flow-col"><span class="nd-flow-l">Gets video from</span>${ups || `<span class="sub">nothing (it's a source)</span>`}</div>
      <span class="nd-flow-arrow" aria-hidden="true">→</span>
      <div class="nd-flow-col"><span class="nd-flow-l">Sends video to</span>${downs || `<span class="sub">nothing (end of the chain)</span>`}</div>
    </div>
    ${spiders ? `<h4 class="trace-title">Spiders here</h4><ul class="nd-plain">${spiders}</ul>` : ""}
    ${recent ? `<h4 class="trace-title">Recent incidents</h4><ul class="nd-events">${recent}</ul>` : ""}
    ${archivedIncidents(n.incidentStats)}
    ${n.labels.includes("FinalLink") ? glitchSectionHtml(id) + adBreakSectionHtml(id) : ""}
    <div id="history-section" data-node="${esc(id)}"></div>
    <div id="trend-section" data-node="${esc(id)}"></div>
    <div id="rca-section" data-node="${esc(id)}"></div>
    <div id="trace-section" data-node="${esc(id)}"></div>
    <footer class="nd-foot">Search index: ${n.embeddingDims ? `${n.embeddingDims}-dim vector${n.embeddingSyncedAt ? `, synced ${esc(ndAgo(n.embeddingSyncedAt) || fmtTime(n.embeddingSyncedAt))}` : ""}` : "not indexed yet"}</footer>
  `;
  detail.style.display = "block";
  detail.scrollTop = 0;
  loadTraceroute(id);
  loadTrend(id);
  if (typeof loadCompactHistory === "function") loadCompactHistory(id);
}

// Glitches on a Final (glitch.py): the last hour against its learned normal, the 10-minute risk and why, the
// upstream failures that usually come first, and the latest events.
function glitchSectionHtml(id) {
  const isHi = typeof getLanguage === "function" && getLanguage() === "hi";
  const tr = typeof trHinglish === "function" ? trHinglish : (x => x);
  const g = (typeof nodeAlerts !== "undefined" && nodeAlerts.glitch[id]) || null;
  if (!g) return `<h4 class="trace-title">${isHi ? "Stream Glitches" : "Glitches"}</h4><p class="sub">${isHi ? "Har minute check hota hai; do checks ke baad results aate hain." : "Checked once a minute; the first results appear after two checks."}</p>`;
  const events = nodeAlerts.glitchEvents.filter(e => e.node === id).slice(0, 5);
  const tone = g.band === "HIGH" ? "bad" : g.band === "MEDIUM" ? "warn" : "";
  const kinds = Object.entries(g.lastHourKinds || {}).map(([k, n]) => `${tr(k)} ×${n}`).join(", ");
  const bandLabel = isHi ? tr(g.band.toLowerCase()) : g.band.toLowerCase();
  const summaryLine = isHi
    ? `<b>${g.lastHour}</b> glitches pichhle 1 ghante mein${g.normalPerHour != null ? `, aamtaur par lagbhag ${Math.round(g.normalPerHour)}` : " (normal rate analyze ho raha hai)"}${kinds ? ` · ${esc(kinds)}` : ""}`
    : `<b>${g.lastHour}</b> in the last hour${g.normalPerHour != null ? `, usually about ${Math.round(g.normalPerHour)}` : " (still learning the normal rate)"}${kinds ? ` · ${esc(kinds)}` : ""}`;

  return `<h4 class="trace-title">${isHi ? "Stream Glitches" : "Glitches"} <span class="nd-count ${tone}" title="${isHi ? 'Agale 10 minute mein glitch ka khatra' : 'Risk of glitches in the next 10 minutes'}">${isHi ? `khatra ${g.risk}/100 · ${bandLabel}` : `risk ${g.risk}/100 · ${bandLabel}`}</span></h4>
    <div class="nd-glitch">
      <div>${summaryLine}</div>
      ${g.reasons.length ? `<ul class="nd-glitch-why">${g.reasons.map(r => `<li>${esc(tr(r))}</li>`).join("")}</ul>` : `<div class="sub">${isHi ? "Abhi koi glitch ka signal nahi hai, sab theek chal raha hai." : "Nothing points to glitches right now."}</div>`}
      ${g.leads.length ? `<div class="sub">${isHi ? "AI History: Yahan ke glitches aamtaur par in servers ke down hone ke baad aate hain: " : "Learned: glitches here usually follow failures on "}${g.leads.map(l => `${esc(srvShortName(l.node))} (${Math.round(l.hit * 100)}% ${isHi ? "baar" : "of the time"}, ${l.lift}× ${isHi ? "zyada asar" : "the usual"})`).join(", ")}.</div>` : ""}
      ${g.deliveryRatio != null ? `<div class="sub">${isHi ? `CDN Delivery: Full-quality segment aane mein apne duration ka ${Math.round(g.deliveryRatio * 100)}% time le raha hai (80% se upar buffer karega).` : `Delivery: a full-quality segment arrives in ${Math.round(g.deliveryRatio * 100)}% of its length (buffering above 80%).`}</div>` : ""}
      ${events.length ? `<ul class="nd-glitch-events">${events.map(e => `<li><span>${esc(tr(e.words))}${e.count > 1 ? ` ×${e.count}` : ""}</span><span class="sub">${esc(ndAgo(new Date(e.ts * 1000).toISOString()) || "")}</span></li>`).join("")}</ul>` : ""}
    </div>`;
}
// SCTE-35 ad breaks on a Final (scte.py): now, the learned weekly pattern, the next expected one, problems, recent.
function adBreakSectionHtml(id) {
  const a = (typeof nodeAlerts !== "undefined" && nodeAlerts.scte[id]) || null;
  if (!a) return "";
  const mins = s => s == null ? "—" : s < 600 ? `${Math.round(s)} s` : `${Math.round(s / 60)} min`;  // ads are measured in seconds
  const when = t => new Date(t * 1000).toLocaleString("en-IN", {timeZone: "Asia/Kolkata", day: "2-digit", month: "short",
                                                                 hour: "2-digit", minute: "2-digit", hour12: false});
  const now = a.open ? `<div><b>In an ad break</b> for ${mins(a.open.elapsed_s)}${a.open.planned_s ? ` of a planned ${mins(a.open.planned_s)}` : ""}.</div>`
    : a.recent.length ? `<div>Last break ${esc(ndAgo(new Date(a.recent[0].start * 1000).toISOString()) || "")}.</div>` : "";
  const next = a.nextExpected ? `<div>Next break expected around <b>${esc(when(a.nextExpected).split(", ").pop())} IST</b>.</div>` : "";
  const rows = a.recent.slice(0, 5).map(b => `<li><span>${esc(when(b.start))}</span>
      <span>${mins(b.actual_s)}${b.planned_s ? ` / ${mins(b.planned_s)} planned` : ""}</span>
      <span class="ad-status ad-${b.status.toLowerCase()}">${b.status === "CLOSED" ? "ok" : b.status.toLowerCase()}</span></li>`).join("");
  return `<h4 class="trace-title">Ad breaks (SCTE-35) <span class="nd-count">${a.breaks7d} in 7 days</span></h4>
    <div class="nd-glitch">
      ${now}${next}
      ${a.issues.length ? `<ul class="nd-glitch-why ad-issues">${a.issues.map(i => `<li>${esc(i)}</li>`).join("")}</ul>` : ""}
      ${a.lines.map(l => `<div class="sub">${esc(l)}</div>`).join("")}
      ${a.mainBreaks24h ? `<div class="sub">Main input: ${a.mainBreaks24h} breaks in 24 h · this Final: ${a.breaks24h}.</div>` : ""}
      ${rows ? `<ul class="nd-glitch-events ad-breaks">${rows}</ul>` : ""}
    </div>`;
}
const srvShortName = id => String(id || "").replace(".ottlive.co.in", "").replace(/\.co\.in$/, "");

// Pipeline chips open that server's panel.
$("#detail-body").addEventListener("click", e => {
  const go = e.target.closest("[data-goto]");
  if (go && byId[go.dataset.goto]) showDetail(go.dataset.goto);
});

// Totals of incident logs that an Embeddings & Log Clearance run archived (the raw entries are gone).
function archivedIncidents(st) {
  if (!st || !st.archivedEntries) return "";
  const errors = Object.entries(st.errors || {}).sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `<span class="nd-err-chip">${esc(k.replace(/_/g, " ").toLowerCase())} <b>×${v}</b></span>`).join("");
  return `<h4 class="trace-title">Older incidents</h4>
    <div class="nd-history">
      <div><span class="nd-history-n">${st.outages}</span><span class="sub">outages</span></div>
      <div><span class="nd-history-n">${st.recoveries}</span><span class="sub">recoveries</span></div>
      <div><span class="nd-history-n">${st.maxConsecutiveFailures}</span><span class="sub">worst run of failures</span></div>
    </div>
    ${errors ? `<div class="nd-errs">${errors}</div>` : ""}
    <div class="sub">${esc(fmtTime(st.firstIncidentAt))} to ${esc(fmtTime(st.lastOutageAt))} · ${st.archivedEntries} log entries summarised by the weekly sync</div>`;
}

// ---------- Embeddings & Log Clearance ----------
// Same run as the weekly cron: embed every node, archive + clear incident logs. Cron runs arrive as events.
const syncBtn = $("#btn-sync-embeddings");
function setSyncing(on) {
  syncBtn.disabled = on;
  syncBtn.classList.toggle("running", on);
  syncBtn.textContent = on ? "Syncing…" : "⚡ Sync Embeddings";
}
function syncedMessage(r) {
  return `✓ Synced embeddings${r.clear_logs ? " & cleared logs" : ""}: ${r.servers_updated} servers, ${r.spiders_updated} spiders updated` +
    (r.clear_logs ? ` (${r.entries_archived} log entries archived from ${r.logs_cleared} servers).` : ".");
}
async function refreshAfterSync() {
  await refreshAll();
  const open = $("#trace-section")?.dataset.node;
  if (open && detail.style.display !== "none" && byId[open]) showDetail(open);
}
syncBtn.onclick = async () => {
  setSyncing(true);
  try {
    const res = await fetch("/api/embeddings/sync?clear_logs=true", {method: "POST"});
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || res.statusText);
    toast("ok", "Embeddings synced", syncedMessage(data));
    await refreshAfterSync();
  } catch (err) {
    toast("error", "Embeddings sync failed", err.message);
  } finally {
    setSyncing(false);
  }
};
function onEmbeddingsEvent(e) {
  if (e.source !== "cron") return;  // this page's own button reports its result itself
  if (e.state === "running") setSyncing(true);
  else {
    setSyncing(false);
    if (e.state === "done") { toast("ok", "Weekly embeddings sync", syncedMessage(e)); refreshAfterSync(); }
    else toast("error", "Weekly embeddings sync failed", e.message || "");
  }
}

// ---------- Health trend and anomalies (per-check history) ----------
function sparkline(points, color, label, unit) {
  const vals = points.filter(p => p.v != null);
  if (vals.length < 2) return "";
  const w = 300, h = 44, xs = vals.map(p => p.t), ys = vals.map(p => p.v);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys, 0), y1 = Math.max(...ys) || 1;
  const d = vals.map((p, i) => `${i ? "L" : "M"}${((p.t - x0) / (x1 - x0 || 1) * w).toFixed(1)},${(h - (p.v - y0) / (y1 - y0 || 1) * h).toFixed(1)}`).join("");
  const downs = points.filter(p => p.down).map(p => `<rect x="${((p.t - x0) / (x1 - x0 || 1) * w - 1).toFixed(1)}" y="0" width="2" height="${h}" fill="var(--down)" opacity=".35"/>`).join("");
  return `<div class="spark"><div class="spark-label">${esc(label)} <b>${Math.round(ys[ys.length - 1])}${unit}</b> <span class="sub">min ${Math.round(Math.min(...ys))} · max ${Math.round(Math.max(...ys))}</span></div>
    <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">${downs}<path d="${d}" fill="none" stroke="${color}" stroke-width="1.5"/></svg></div>`;
}

async function loadTrend(id) {
  const box = $("#trend-section");
  try {
    const res = await fetch(`/api/nodes/${encodeURIComponent(id).replace(/%2F/g, "/")}/metrics?since=21600`);
    if (!res.ok || box.dataset.node !== id) return;
    const {history, anomalies: a} = await res.json();
    if (!history.length) { box.innerHTML = ""; return; }
    // Segment ages far below zero are timestamp glitches of the source (not real ages): leave them out of the chart.
    const pts = key => history.map(r => ({t: r.ts, v: key === "segment_age_s" && r[key] != null && r[key] < -300 ? null : r[key], down: !r.up}));
    const metricNames = {latency_ms: "HTTP latency", rtt_ms: "ICMP RTT", jitter_ms: "jitter", segment_age_s: "segment age", loss: "packet loss"};
    const rows = Object.entries(a.metrics || {}).map(([k, m]) => `<b>${metricNames[k] || k}</b><span>${m.value} (normal ${m.normal}${m.z >= 4 ? `, <span style="color:var(--down)">${m.z}σ above</span>` : ""}${m.trend ? `, ${m.trend}` : ""})</span>`).join("");
    const warn = (a.metrics && Object.values(a.metrics).some(m => m.z >= 4 || m.trend === "rising")) && !a.down;
    box.innerHTML = `<h4 class="trace-title">Health, last 6 hours <span class="nd-count ${a.score >= 7 ? "bad" : a.score >= 4 ? "warn" : ""}" title="Anomaly score: how far its metrics are from its own normal">anomaly ${a.score}/10</span></h4>
      ${warn ? `<div class="trace-summary warn">Early warning: drifting from its normal while still up</div>` : ""}
      ${sparkline(pts("latency_ms"), "var(--accent)", "HTTP latency", " ms")}
      ${sparkline(pts("segment_age_s"), "#0d9488", "Newest segment age", " s")}
      ${rows ? `<div class="kv">${rows}</div>` : `<p class="sub">Collecting a baseline (${a.samples} checks so far; anomalies need 20).</p>`}
      ${segmentAlertsHtml(id, a.segments || [])}`;
  } catch { /* the panel works without it */ }
}

// Segment age is judged per stream against its own learned normal (metrics.segment_alerts): show each line,
// what it was learned from, how its warnings ended, and let the operator say it's too sensitive.
function segmentAlertsHtml(id, segments) {
  if (!segments.length) return "";
  const channelOf = url => ((byId[id]?.links || []).find(l => l.url === url) || {}).channel;
  const items = segments.map(a => {
    const b = a.baseline, name = channelOf(a.url) || a.url.replace(/^https?:\/\/[^/]+/, "");
    const ended = a.raised ? `${a.raised} warning${a.raised === 1 ? "" : "s"}: ${a.clearedAlone} settled by themselves, ${a.beforeOutage} came before an outage${a.tooSensitive ? `, ${a.tooSensitive} marked too sensitive` : ""}` : "no warnings yet";
    return `<li class="nd-seg ${a.active ? "nd-seg-on" : ""}">
      <div class="nd-seg-top"><b>${esc(name)}</b>${a.active ? `<span class="nd-badge nd-badge-down">warning now</span>` : ""}
        <button type="button" class="nd-seg-btn" data-loosen="${esc(a.url)}" title="Raise this stream's warning line one step">Too sensitive</button></div>
      <div>Normal ${b.median} s${b.spread ? ` (±${b.spread})` : ""} · warns above <b>${a.warn} s</b> · now ${a.age == null ? "—" : Math.round(a.age) + " s"}</div>
      <div class="sub">Learned from ${b.samples.toLocaleString()} checks over ${b.days < 1 ? `${Math.max(1, Math.round(b.days * 24))} h` : `${b.days} days`}${b.byHour ? ", for this time of day" : ""} · sensitivity ${a.k} · ${esc(ended)}</div>
    </li>`;
  }).join("");
  return `<h4 class="trace-title">Segment-age alert <span class="nd-count">learned per stream</span></h4><ul class="nd-segs">${items}</ul>`;
}

document.addEventListener("click", async e => {
  const b = e.target.closest("[data-loosen]");
  if (!b) return;
  b.disabled = true;
  try {
    const {data} = await api("POST", "/api/learning/segment-sensitivity", {url: b.dataset.loosen});
    toast("ok", "Warning line raised", `Now warns above ${data.warn} s for this stream.`);
    const open = $("#trend-section")?.dataset.node;
    if (open) loadTrend(open);
    if (typeof loadRanking === "function") loadRanking();
  } catch (err) { toast("error", "Couldn't change it", err.message); b.disabled = false; }
});

// ---------- Traceroute (Ping-First, Traceroute-on-Threshold) ----------
// The server traces a server's path automatically once it fails `threshold` times in a row (at most once per
// host per cooldown); operators can also run one now. Results come back as "traceroute" stream events.
const traceUrl = id => `/api/nodes/${encodeURIComponent(id).replace(/%2F/g, "/")}/traceroute`;
const traceRunning = new Set();

function hopBadge(h) {
  if (h.drop) return `<span class="hop-badge drop">🔴 drop</span>`;
  if (h.status === "timeout") return `<span class="hop-badge silent">⚪ no reply</span>`;
  if (h.status === "slow") return `<span class="hop-badge slow">🟡 high latency</span>`;
  if (h.status === "fair") return `<span class="hop-badge fair">🔵 moderate</span>`;
  return `<span class="hop-badge ok">🟢 ok</span>`;
}

function renderTraceroute(id, data) {
  const box = $("#trace-section");
  if (!box || box.dataset.node !== id) return;  // the panel moved on to another node
  const r = data.result, node = byId[id] || {};
  const runningNow = data.running || traceRunning.has(id);
  $$(`[data-trace="${CSS.escape(id)}"]`).forEach(b => { b.disabled = runningNow; b.classList.toggle("running", runningNow); });
  if (!r && !runningNow) {
    box.innerHTML = `<p class="sub trace-note">No traceroute yet. One runs automatically after ${data.threshold} consecutive failures (at most every ${Math.round(data.cooldown_s / 60)} min per host).</p>`;
    return;
  }
  const failing = node.status === "DOWN" || (node.consecutiveFailures || 0) > 0;
  const lastReply = r ? [...(r.hops || [])].reverse().find(h => h.status !== "timeout") : null;
  const hops = (r?.hops || []).map(h => {
    const after = r.drop_hop != null && h.hop > r.drop_hop;
    const note = h.drop ? `<div class="hop-warn">⚠ Packet drop detected at this hop</div>`
      : after ? `<div class="sub">no reply beyond the drop point</div>`
      : h.status === "timeout" && r.inconclusive ? `<div class="sub">no reply</div>`
      : h.status === "timeout" ? `<div class="sub">router silent; traffic still passes further</div>`
      : h.rate_limited ? `<div class="sub">${h.lost}/${h.sent} probes unanswered (rate limiting, not loss)</div>` : "";
    return `<tr class="hop ${h.drop ? "is-drop" : ""} ${after ? "is-after" : ""}">
      <td class="hop-n">${h.hop}</td>
      <td><code>${esc(h.ip || "*")}</code>${h.ips && h.ips.length > 1 ? `<div class="sub">also ${esc(h.ips.slice(1).join(", "))}</div>` : ""}${note}</td>
      <td class="hop-rtt">${h.rtt_ms != null ? `${h.rtt_ms} ms` : "—"}<div class="sub">${h.loss_pct}% loss</div></td>
      <td>${hopBadge(h)}</td></tr>`;
  }).join("");
  box.innerHTML = `
    <h4 class="trace-title">Network path (traceroute)</h4>
    ${runningNow ? `<div class="trace-running">Tracing the path to this server…</div>` : ""}
    ${r ? `
      <div class="trace-summary ${r.error || r.drop_hop != null ? "bad" : r.reached ? "good" : "warn"}">${esc(r.summary || "")}</div>
      <div class="sub">${esc(r.host)}${r.target_ip && r.target_ip !== r.host ? " (" + esc(r.target_ip) + ")" : ""} ·
        ${r.trigger === "manual" ? "run manually" : "auto: " + esc(r.reason || "threshold")} · ${esc(fmtTime(r.at))}
        ${r.duration_ms != null ? " · " + (r.duration_ms / 1000).toFixed(1) + " s" : ""}
        ${!failing ? " · node is up now" : ""}</div>
      ${r.drop_hop == null && !r.reached && lastReply ? `<div class="sub">Last reply from hop ${lastReply.hop} (${esc(lastReply.ip)}).</div>` : ""}
      ${hops ? `<table class="hops"><thead><tr><th>#</th><th>Hop</th><th>RTT</th><th></th></tr></thead><tbody>${hops}</tbody></table>` : ""}` : ""}`;
}

async function loadTraceroute(id) {
  try {
    const res = await fetch(traceUrl(id));
    if (res.ok) renderTraceroute(id, await res.json());
  } catch { /* the panel still works without it */ }
}

async function runTraceroute(id) {
  const res = await fetch(traceUrl(id), {method: "POST"});
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { toast("error", "Traceroute", data.detail || res.statusText); return; }
  traceRunning.add(id);
  toast("ok", "Traceroute started", `Tracing ${data.host}; results appear here when it finishes.`);
  loadTraceroute(id);
}

// ---------- AI root-cause analysis (NOC) ----------
// Zone, drop point and action are computed by the server; the local model then writes the RCA around them.
const ZONE_CLASS = {local: "warn", transit: "bad", server: "bad", none: "good", unknown: "warn"};

async function runRca(id) {
  const box = $("#rca-section");
  if (!box || box.dataset.node !== id) return;
  const btn = $(`[data-rca="${CSS.escape(id)}"]`);
  if (btn) { btn.disabled = true; btn.classList.add("running"); }
  let findings = null, text = "", done = null, failed = null;
  const render = () => {
    if (box.dataset.node !== id) return;
    box.innerHTML = `<h4 class="trace-title">Root cause analysis</h4>
      ${findings ? `<div class="rca-findings">
        <div class="trace-summary ${ZONE_CLASS[findings.zone] || "warn"}">${esc(findings.zoneLabel)}</div>
        <div class="kv"><b>Drop point</b><span>${esc(findings.dropPoint)}</span><b>Action</b><span>${esc(findings.action)}</span></div>
      </div>` : `<div class="trace-running">Gathering health and traceroute data…</div>`}
      ${findings ? `<div class="rca-text ${done || failed ? "" : "streaming"}">${text ? renderMarkdown(text) : (failed ? "" : `<div class="trace-running">Writing the RCA with ${esc(findings.model)}…</div>`)}</div>` : ""}
      ${failed ? `<div class="sub" style="color:var(--down)">${esc(failed)}</div>` : ""}
      ${findings ? `<div class="sub">${findings.tracerouteAt ? `Traceroute from ${esc(fmtTime(findings.tracerouteAt))}` : "No traceroute yet: run one for a network-level diagnosis"} · ${findings.zone === "none" ? "written without a model (nothing to diagnose)" : `${esc(findings.model)}${done ? ` · ${(done.elapsed_ms / 1000).toFixed(1)} s${done.tokens ? " · " + tokenLine(done.tokens) : ""}` : ""} · the model's text can be wrong; the findings above are computed`}</div>` : ""}`;
  };
  render();
  try {
    const res = await fetch(traceUrl(id).replace(/\/traceroute$/, "/rca"), {method: "POST"});
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    const reader = res.body.getReader(), decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const {value, done: ended} = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), {stream: !ended});
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const line of lines.filter(Boolean)) {
        const e = JSON.parse(line);
        if (e.type === "findings") findings = e;
        else if (e.type === "token") text += e.text;
        else if (e.type === "done") done = e;
        else if (e.type === "error") failed = e.message;
      }
      render();
      if (ended) break;
    }
  } catch (err) {
    failed = err.message; render();
  } finally {
    const b = $(`[data-rca="${CSS.escape(id)}"]`);
    if (b) { b.disabled = false; b.classList.remove("running"); }
  }
}

document.addEventListener("click", e => {
  const b = e.target.closest("[data-rca]");
  if (b && !b.disabled) runRca(b.dataset.rca);
});

function onTracerouteEvent(e) {
  if (e.state === "running") traceRunning.add(e.node); else traceRunning.delete(e.node);
  if (typeof noteTraceroute === "function") noteTraceroute(e);  // a drop shows on the server's card
  if ($("#trace-section")?.dataset.node === e.node) loadTraceroute(e.node);
}

document.addEventListener("click", e => {
  const b = e.target.closest("[data-trace]");
  if (b && !b.disabled) runTraceroute(b.dataset.trace);
});
