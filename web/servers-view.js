"use strict";

// ---------- Servers: every server as a card, riskiest first ----------
// One risk score (0-100) per server from what's happening now and what's predicted, with the reasons spelled out
// in plain words so the order is never a mystery:
//   down now 40 · failing streams up to 15 · likely root cause 15 (affected by it 5) · predicted failure risk up
//   to 20 · error rate up to 20 (a server failing 40% of its checks is unreliable even while up right now) ·
//   early warnings up to 10 · frequent blips up to 10 · on Finals: glitches in the last hour up to 10, glitch risk
//   for the next 10 minutes (glitch.py) 15 when high, 7 when medium
// Bands: Critical 60+, High 35+, Watch 15+, Healthy below. Data: the graph (G), /api/predictions, /api/anomalies
// and the root-cause ranking (node-alerts.js keeps it). Refreshes while the page is open.

const srvView = {band: "all", q: "", predictions: {}, anomalies: {}, fetchedAt: 0};
const SRV_BANDS = [
  {id: "critical", label: "Critical", min: 65}, {id: "high", label: "High", min: 45},
  {id: "watch", label: "Watch", min: 25}, {id: "healthy", label: "Healthy", min: 0},
];
const ROLE_NAMES = {MainInput: "Main", BackupLink: "Backup", Transcoding: "Transcoding", FinalLink: "Final"};
const srvShort = id => String(id || "").replace(".ottlive.co.in", "").replace(/\.co\.in$/, "");

function srvAgo(iso) {
  const t = Date.parse(String(iso || "").replace(/(\.\d{3})\d+/, "$1"));
  if (!t) return "never";
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  return s < 90 ? `${s} s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
}

// The score and the reasons behind it, for one server.
function scoreServer(n) {
  const reasons = [];
  let score = 0;
  const health = Object.values(n.urlHealth || {});
  const failing = health.filter(h => h.up === false);
  const down = n.status === "DOWN";
  if (down) {
    score += 45;
    const p = failing.length && typeof urlProblem === "function" ? urlProblem(failing[0]) : null;
    reasons.push({tone: "bad", text: p ? `Down now: ${p.title.toLowerCase()}` : "Down now"});
  }
  if (failing.length) {
    score += 25 * (failing.length / Math.max(health.length, 1));
    if (!down || failing.length > 1) reasons.push({tone: "bad", text: `${failing.length} of ${health.length} stream${health.length === 1 ? "" : "s"} failing`});
  }
  const groups = (typeof nodeAlerts !== "undefined" ? nodeAlerts.groups : []) || [];
  const group = groups.find(g => g.nodes.includes(n.id));
  if (group) {
    const top = group.ranking[0]?.node;
    if (top === n.id) { score += 20; reasons.push({tone: "bad", text: "Likely root cause of the current outage"}); }
    else if (top) { score += 8; reasons.push({tone: "warn", text: `Affected by ${srvShort(top)} upstream`}); }
  }
  const pred = srvView.predictions[n.id];
  if (pred) {
    const pct = Math.round((pred.blendedScore || 0) * 100);
    // Only add significant score if failure risk is high (> 35%)
    if (pct >= 35) {
      score += 15 * Math.min(1, pred.blendedScore || 0);
      const trend = pred.profile?.failure_trend === "INCREASING" ? ", outages getting more frequent" : "";
      reasons.push({tone: pct >= 50 ? "bad" : "warn", text: `Failure risk ${pct}%${trend}`});
    }
  }
  const rate = n.pingCount ? n.failedCount / n.pingCount : 0;
  // If server is up with all streams live, historical failure rate is strictly informational background context
  const rateWeight = (!down && failing.length === 0) ? 6 : 18;
  score += rateWeight * Math.min(1, rate / 0.4);
  if (rate >= 0.20 && (down || failing.length > 0)) {
    reasons.push({tone: rate >= 0.35 ? "bad" : "warn", text: `${Math.round(rate * 100)}% of checks failed (${(n.failedCount || 0).toLocaleString()} of ${n.pingCount.toLocaleString()})`});
  }
  const warnings = (srvView.anomalies[n.id]?.warnings || []);
  if (warnings.length && !down) {
    score += Math.min(8, 3 * warnings.length);
    const w = typeof agentWarningParts === "function" ? agentWarningParts(warnings[0]).phrase : warnings[0];
    reasons.push({tone: "warn", text: `Notice: ${w}`});
  }
  const gl = (typeof nodeAlerts !== "undefined" && nodeAlerts.glitch[n.id]) || null;  // Finals only (glitch.py)
  if (gl) {
    if (gl.band === "HIGH") { score += 20; reasons.push({tone: "bad", text: `Glitches likely in the next 10 min: ${gl.reasons[0] || ""}`}); }
    else if (gl.band === "MEDIUM") score += 6;
    if (gl.lastHour) {
      score += Math.min(8, 2 * gl.lastHour);
      const kinds = Object.keys(gl.lastHourKinds || {}).slice(0, 2).join(", ");
      if (gl.band !== "HIGH") reasons.push({tone: "warn", text: `${gl.lastHour} glitch${gl.lastHour === 1 ? "" : "es"} in the last hour${kinds ? ` (${kinds})` : ""}`});
    }
  }
  const ad = (typeof nodeAlerts !== "undefined" && nodeAlerts.scte[n.id]) || null;  // ad-break markers (scte.py)
  if (ad && ad.issues.length) {
    score += ad.open && ad.open.status === "STUCK" ? 25 : 8;
    reasons.push({tone: ad.open && ad.open.status === "STUCK" ? "bad" : "warn", text: ad.issues[0]});
  }
  // Ignore self-fulfilling recursive EARLY_WARNING predictions — only display real operational alerts
  const next = (typeof nodeAlerts !== "undefined" ? nodeAlerts.upcoming : []).find(p => p.node === n.id && p.kind !== "EARLY_WARNING");
  if (next) {  // predicted to alert soon (alertlog.py)
    score += Math.round(15 * next.probability);
    reasons.unshift({tone: next.kind === "OUTAGE" ? "bad" : "warn",
                     text: `Likely next: ${next.words} ${upNextWhen(next)} (${Math.round(next.probability * 100)}%)`});
  }
  if ((n.blipCount || 0) >= 20) {
    score += Math.min(6, n.blipCount / 15);
    reasons.push({tone: "warn", text: `${n.blipCount} short blips`});
  }
  score = Math.round(Math.min(100, score));

  let band = SRV_BANDS.find(b => score >= b.min) || SRV_BANDS.at(-1);

  // Guarantee: A live server where all streams are up and no active fault exists cannot be marked Critical or High
  if (!down && failing.length === 0 && !group && (!next || next.kind !== "OUTAGE")) {
    if (band.id === "critical" || band.id === "high") {
      band = SRV_BANDS.find(b => b.id === (score >= 30 ? "watch" : "healthy"));
    }
  }

  if (!reasons.length) reasons.push({tone: "ok", text: n.status === "UP" ? "Working normally (all streams live)" : "Not checked yet"});
  return {score, band, reasons, pred, rate, failing: failing.length, streams: health.length};
}

function serverCard(n, s, rank) {
  const roles = (n.labels || []).filter(l => ROLE_NAMES[l]).map(l => `<span class="srv-role role-${l}">${ROLE_NAMES[l]}</span>`).join("");
  const channels = [...new Set((n.links || []).map(l => l.channel))];
  const passed = n.pingCount ? `${(100 - s.rate * 100).toFixed(1)}%` : "—";
  const mttr = s.pred?.profile?.mttr_s;
  return `<button type="button" class="srv-card band-${s.band.id}" data-server="${esc(n.id)}"
      aria-label="${esc(`#${rank} ${n.id}, ${s.band.label} risk, score ${s.score}. ${s.reasons.map(r => r.text).join(". ")}`)}">
    <div class="srv-card-top">
      <span class="srv-rank">#${rank}</span>
      <span class="srv-band">${s.band.label}</span>
      <span class="srv-score" title="Risk score out of 100">${s.score}</span>
    </div>
    <div class="srv-name"><span class="dot" style="background:${statusColor(n.status)}"></span>${esc(srvShort(n.id))}</div>
    <div class="srv-meta">${roles}${channels.length ? `<span class="srv-channels" title="${esc(channels.join(", "))}">${esc(channels.slice(0, 3).join(", "))}${channels.length > 3 ? ` +${channels.length - 3}` : ""}</span>` : ""}</div>
    <div class="srv-meter" aria-hidden="true"><i style="width:${Math.max(3, s.score)}%"></i></div>
    <ul class="srv-reasons">${s.reasons.slice(0, 3).map(r => `<li class="r-${r.tone}">${esc(r.text)}</li>`).join("")}</ul>
    <dl class="srv-stats">
      <div><dt>Checks passed</dt><dd>${passed}</dd></div>
      <div><dt>Response</dt><dd>${n.latency != null ? `${Math.round(n.latency)} ms` : "—"}</dd></div>
      <div><dt>Streams</dt><dd>${s.streams ? `${s.streams - s.failing}/${s.streams} live` : "—"}</dd></div>
      <div><dt>${mttr ? "Usual recovery" : "Last check"}</dt><dd>${mttr ? (mttr < 90 ? `${Math.round(mttr)} s` : `${Math.round(mttr / 60)} min`) : srvAgo(n.lastPing)}</dd></div>
    </dl>
  </button>`;
}

function renderServers() {
  if (!$("#view-servers")?.classList.contains("active")) return;
  const scored = (G.nodes || []).filter(n => !String(n.id).endsWith(".invalid"))
    .map(n => ({n, s: scoreServer(n)}))
    .sort((a, b) => b.s.score - a.s.score || a.n.id.localeCompare(b.n.id));
  const slowNet = typeof nodeAlerts !== "undefined" && nodeAlerts.networkSlow;
  $("#srv-notice").hidden = !slowNet;
  if (slowNet) {
    $("#srv-notice").textContent = `This monitor's own internet connection looks slow right now (${slowNet.slow} of ${slowNet.of} ` +
      "channels slow at the same moment), so slow-delivery glitches aren't being blamed on the streams until it recovers.";
  }
  const counts = Object.fromEntries(SRV_BANDS.map(b => [b.id, scored.filter(x => x.s.band.id === b.id).length]));
  $("#srv-bands").innerHTML = [{id: "all", label: "All"}, ...SRV_BANDS].map(b =>
    `<button type="button" class="srv-band-btn band-${b.id}${srvView.band === b.id ? " active" : ""}" data-band="${b.id}"
      aria-pressed="${srvView.band === b.id}"><span>${b.label}</span><b>${b.id === "all" ? scored.length : counts[b.id]}</b></button>`).join("");
  const q = srvView.q.trim().toLowerCase();
  const shown = scored.map((x, i) => ({...x, rank: i + 1}))
    .filter(x => srvView.band === "all" || x.s.band.id === srvView.band)
    .filter(x => !q || [x.n.id, ...(x.n.labels || []).map(l => ROLE_NAMES[l] || l), ...(x.n.links || []).map(l => l.channel)]
      .join(" ").toLowerCase().includes(q));
  $("#srv-count").textContent = `${shown.length} of ${scored.length} servers`;
  $("#srv-grid").innerHTML = shown.length ? shown.map(x => serverCard(x.n, x.s, x.rank)).join("")
    : `<p class="srv-empty">${scored.length ? "No servers match." : "No servers yet. Add stream links in Links."}</p>`;
}

async function loadServers(force = false) {
  renderServers();  // straight away from what's loaded, then again with the predictions and warnings
  if (!force && Date.now() - srvView.fetchedAt < 20000) return;
  srvView.fetchedAt = Date.now();
  const [pred, anom] = await Promise.allSettled([api("GET", "/api/predictions"), api("GET", "/api/anomalies")]);
  if (pred.status === "fulfilled") srvView.predictions = Object.fromEntries((pred.value.data.nodes || []).map(p => [p.id, p]));
  if (anom.status === "fulfilled") srvView.anomalies = anom.value.data || {};
  renderServers();
}

$("#srv-bands").addEventListener("click", e => {
  const b = e.target.closest("[data-band]"); if (!b) return;
  srvView.band = srvView.band === b.dataset.band ? "all" : b.dataset.band;
  renderServers();
});
let srvSearchTimer = null;
$("#srv-search").addEventListener("input", e => {
  clearTimeout(srvSearchTimer);
  srvSearchTimer = setTimeout(() => { srvView.q = e.target.value; renderServers(); }, 150);
});
$("#srv-grid").addEventListener("click", e => {  // open the server on the Workflow canvas with its panel
  const card = e.target.closest("[data-server]"); if (!card || !byId[card.dataset.server]) return;
  showView("workflow");
  requestAnimationFrame(() => { fit(cardIds(card.dataset.server)); showDetail(card.dataset.server); });
});
setInterval(() => { if ($("#view-servers")?.classList.contains("active") && !document.hidden) loadServers(); }, 30000);
