"use strict";

// ---------- Alerts on the server cards ----------
// A problem is shown where it is: a small toast pinned above the failing server's card, in plain words, instead
// of a pop-up in the corner. The likely root cause says so. Anything that isn't an error on that server itself
// (failing only because of an upstream server, an early warning while still up, a recovery shown briefly) is a
// small pill on the card's bottom edge instead, so it doesn't cover the cards around it. An error can be dismissed (it
// comes back if the problem changes); everything opens the server's panel on click.

const nodeAlerts = {groups: [], warnings: {}, recovered: {}, traces: {}, dismissed: new Set(),
                    glitch: {}, glitchEvents: [], glitchModel: null,
                    scte: {}, upcoming: [], alertScores: null};  // scte: node -> ad breaks; upcoming: alertlog.py  // glitch: node -> forecast (glitch.py)
const RECOVERED_SHOW_MS = 30000;

const PROBLEM_TITLE = {
  STALE_MEDIA: "Video stopped updating", PLAYLIST_MISSING: "Playlist missing", UNREACHABLE: "Server unreachable",
  SERVER_ERROR: "Server error", HTTP_ERROR: "HTTP error", NO_SEGMENTS: "No video pieces", INVALID_PLAYLIST: "Broken playlist",
};
const alertShort = id => String(id || "").replace(".ottlive.co.in", "").replace(/\.co\.in$/, "");

function alertSince(iso) {
  const t = Date.parse(String(iso || "").replace(/(\.\d{3})\d+/, "$1"));
  if (!t) return "";
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  return s < 90 ? `${s} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${Math.round(s / 3600)} h`;
}

// What's wrong with one failing URL: {title, text}.
function urlProblem(h) {
  const behind = String(h.detail || "").match(/FALLING_BEHIND \(advancing, but (\d+)s behind live, (-?\d+)s more/);
  if (behind) {
    return {title: "Falling behind live", text: `Still playing, but ${behind[1]} s behind live and slipping (+${behind[2]} s since the last check)`};
  }
  const title = PROBLEM_TITLE[h.category] || (h.freshness === "STALE" ? PROBLEM_TITLE.STALE_MEDIA : "Stream failing");
  let text;
  if (h.segmentAgeS != null && h.segmentAgeS >= 0 && (h.category === "STALE_MEDIA" || h.freshness === "STALE")) {
    text = `Newest piece is ${Math.round(h.segmentAgeS)} s old` + (h.targetS ? ` (should be under ${Math.round(h.targetS * 1.5)} s)` : "");
  } else {
    text = String(h.detail || "").replace(/^\d+\/\d+ URLs failing: /, "").slice(0, 140);
  }
  return {title, text};
}

// The alert for a card: the whole server, or (url) one channel's Final card. null when there's nothing to say.
function cardAlert(nodeId, url) {
  const n = byId[nodeId];
  if (!n) return null;
  const health = n.urlHealth || {};
  const failing = url ? [health[url] || {}].filter(h => h.up === false)
                      : Object.values(health).filter(h => h.up === false);
  const group = nodeAlerts.groups.find(g => g.nodes.includes(nodeId));
  const top = group && group.ranking[0];

  // If this card is a FinalLink channel card whose spider is STOPPED upstream:
  const stoppedSpider = (G.spiders || []).find(s => s.finalLinkId === nodeId && s.status === "STOPPED");
  if (stoppedSpider) {
    const rootHost = stoppedSpider.at ? alertShort(stoppedSpider.at) : "upstream host";
    const is404 = String(stoppedSpider.stopReason || "").includes("404");
    const title = is404 ? "Ingest stream missing (HTTP 404)" : "Upstream feed failed";
    const text = is404
      ? `Origin ${rootHost} missing manifest. MCR team: restart live encoder push.`
      : (stoppedSpider.stopReason || `Stopped at ${rootHost}.`);
    return {
      tone: "down",
      title,
      text,
      tag: "Upstream fail",
      key: `stopped-spider-${nodeId}`,
      since: stoppedSpider.lastStepAt || ""
    };
  }

  if (failing.length || (!url && n.status === "DOWN")) {
    const since = failing.map(h => h.onsetAt).filter(Boolean).sort()[0];
    if (top && top.node !== nodeId) {
      return {tone: "affected", title: "Affected", key: `${top.node}`, pill: `Affected by ${alertShort(top.node)}`,
              text: `Fails because ${alertShort(top.node)} upstream is down.`, since};
    }
    const p = failing.length ? urlProblem(failing[0]) : {title: "Down", text: n.lastError || ""};
    const extra = failing.length > 1 ? ` ${failing.length} streams failing.` : "";
    return {tone: "down", title: p.title, text: p.text + (p.text && !p.text.endsWith(".") ? "." : "") + extra,
            tag: top && top.node === nodeId && group.nodes.length > 1 ? "Root cause" : "",
            trace: nodeAlerts.traces[nodeId], since, key: failing.map(h => h.category).join(",")};
  }
  const rec = nodeAlerts.recovered[nodeId];
  if (rec && rec.until > Date.now()) {
    return {tone: "ok", title: "Back to normal", key: rec.at, pill: "✓ Back to normal",
            text: rec.durationS != null ? `Was down for ${alertSince(new Date(Date.now() - rec.durationS * 1000).toISOString())}.` : ""};
  }
  const ad = nodeAlerts.scte[nodeId];
  if (ad && ad.open && ad.open.status === "STUCK") {
    return {tone: "warn", title: "Stuck in an ad break", text: ad.issues[0] || "", key: `ad-stuck-${ad.open.start}`,
            pill: `⚠ stuck in ad ${Math.round(ad.open.elapsed_s / 60)} min`};
  }
  const g = nodeAlerts.glitch[nodeId];
  if (g && (g.band === "HIGH" || g.lastHour >= 3)) {  // a Final still playing, but glitching or about to
    return {tone: "warn", title: g.band === "HIGH" ? "Glitches likely" : "Glitching",
            text: g.reasons[0] ? g.reasons[0].replace(/^\w/, c => c.toUpperCase()) + "." : "", key: `glitch-${g.band}`,
            pill: g.band === "HIGH" ? "⚠ glitches likely" : `⚠ ${g.lastHour} glitches / h`};
  }
  const w = nodeAlerts.warnings[nodeId];
  if (w) {
    const parts = typeof agentWarningParts === "function" ? agentWarningParts(w) : {phrase: w, metric: w};
    const how = parts.kind === "high" ? "high" : parts.kind === "rising" ? "rising" : "unusual";
    return {tone: "warn", title: "Early warning", text: parts.phrase.replace(/^\w/, c => c.toUpperCase()) + ". Still up.",
            key: parts.metric, pill: `⚠ ${parts.metric} ${how}`};
  }
  if (ad && ad.open) {
    return {tone: "ad", title: "In an ad break", text: "", key: `ad-${ad.open.start}`,
            pill: `▶ ad break${ad.open.planned_s ? ` · ${Math.round(ad.open.planned_s)} s` : ""}`};
  }
  return null;
}

function renderNodeAlerts() {
  $$(".node[data-node]", world).forEach(card => {
    card.querySelector(".node-alert, .node-alert-pill")?.remove();
    card.classList.remove("has-alert");
    const a = cardAlert(card.dataset.node, card.dataset.url || null);
    if (!a) return;
    const key = `${card.dataset.node}|${card.dataset.url || ""}|${a.tone}|${a.title}|${a.key || ""}|${a.since || ""}`;
    if (nodeAlerts.dismissed.has(key)) return;
    const el = document.createElement("div");
    el.setAttribute("role", a.tone === "down" ? "alert" : "status");
    if (a.pill) {
      // Not an error on this server itself: a small pill on the card's edge; the full text on hover.
      el.className = `node-alert-pill node-alert-${a.tone}`;
      el.tabIndex = 0;
      el.title = `${a.title}: ${a.text}${a.since ? ` (${alertSince(a.since)})` : ""}. Click for details.`;
      el.textContent = a.pill;
      el.addEventListener("mousedown", e => e.stopPropagation());
      el.addEventListener("click", e => { e.stopPropagation(); showDetail(card.dataset.node); });
      card.appendChild(el);
      return;
    }
    el.className = `node-alert node-alert-${a.tone}`;
    el.innerHTML = `
      <div class="node-alert-head">
        <b>${esc(a.title)}</b>
        ${a.since ? `<span class="node-alert-since">${esc(alertSince(a.since))}</span>` : ""}
        <button type="button" class="node-alert-x" aria-label="Dismiss" title="Dismiss">×</button>
      </div>
      ${a.text || a.tag ? `<div class="node-alert-text">${a.tag ? `<span class="node-alert-tag">${esc(a.tag)}</span> ` : ""}${esc(a.text || "")}</div>` : ""}
      ${a.trace ? `<div class="node-alert-trace">Network: ${esc(a.trace)}</div>` : ""}`;
    el.querySelector(".node-alert-x").addEventListener("click", e => {
      e.stopPropagation();
      nodeAlerts.dismissed.add(key);
      el.remove(); card.classList.remove("has-alert");
    });
    el.addEventListener("mousedown", e => e.stopPropagation());  // clicking the note doesn't start a drag
    el.addEventListener("click", e => { e.stopPropagation(); showDetail(card.dataset.node); });
    card.classList.add("has-alert");
    card.appendChild(el);
  });

  if (window.ttsAlerts && typeof window.ttsAlerts.onAlertsRendered === "function") {
    window.ttsAlerts.onAlertsRendered();
  }
}

// --- inputs ---

function setRanking(groups) {
  nodeAlerts.groups = groups || [];
  renderNodeAlerts();
}

function setWarnings(list) {
  nodeAlerts.warnings = Object.fromEntries((list || []).filter(w => (w.warnings || []).length).map(w => [w.node, w.warnings[0]]));
  renderNodeAlerts();
}

function noteIncidents(incidents) {
  for (const i of incidents || []) {
    if (i.type === "RECOVERY") {
      nodeAlerts.recovered[i.node] = {until: Date.now() + RECOVERED_SHOW_MS, durationS: i.durationS, at: i.timestamp};
      delete nodeAlerts.traces[i.node];
      setTimeout(renderNodeAlerts, RECOVERED_SHOW_MS + 100);
    }
  }
}

function noteTraceroute(e) {
  if (e.state !== "done") return;
  if (e.drop_hop != null || e.error) nodeAlerts.traces[e.node] = e.summary || "traceroute found a problem";
  else delete nodeAlerts.traces[e.node];
  renderNodeAlerts();
}

// Times ("3 min") keep counting while nothing else changes.
setInterval(() => { if (document.querySelector(".node-alert, .node-alert-pill")) renderNodeAlerts(); }, 30000);

function setGlitches(data) {
  nodeAlerts.glitch = Object.fromEntries((data.finals || []).map(f => [f.node, f]));
  nodeAlerts.glitchEvents = data.events || [];
  nodeAlerts.glitchModel = data.model || null;
  nodeAlerts.networkSlow = data.monitorNetworkSlow || null;
  renderNodeAlerts();
  if (typeof renderServers === "function") renderServers();
}

function setScte(data) {
  nodeAlerts.scte = Object.fromEntries((data.channels || []).map(c => [c.node, c]));
  renderNodeAlerts();
  if (typeof renderServers === "function") renderServers();
}

// --- What alerts are likely next (alertlog.py): shared by the Agent and Servers pages ---

function setUpcoming(data) {
  nodeAlerts.upcoming = data.upcoming || [];
  nodeAlerts.alertScores = data.scores || null;
  renderUpNext("#agent-upnext");
  renderUpNext("#srv-upnext");
  if (typeof renderServers === "function") renderServers();
}

function upNextWhen(p) {
  const s = Math.round(p.expectedAt - Date.now() / 1000);
  return s <= 15 ? "any moment" : s < 90 ? `in ~${s} s` : `in ~${Math.round(s / 60)} min`;
}

function scoreLine(scores) {
  if (!scores) return "";
  const parts = Object.entries(scores).filter(([, v]) => v.hits + v.misses > 0)
    .map(([src, v]) => `${src === "pipeline" ? "from the pipeline" : "learned"}: right ${Math.round(v.hitRate * 100)}% of ${v.hits + v.misses}`
      + (v.medianWarningS != null ? `, ~${v.medianWarningS < 90 ? v.medianWarningS + " s" : Math.round(v.medianWarningS / 60) + " min"} warning` : ""));
  return parts.length ? `Past predictions (7 days): ${parts.join(" · ")}.` : "Past predictions are scored once they come true or time out.";
}

function renderUpNext(sel) {
  const box = document.querySelector(sel);
  if (!box) return;
  const list = nodeAlerts.upcoming.slice(0, 5);
  box.hidden = !list.length;
  if (!list.length) { box.innerHTML = ""; return; }
  box.innerHTML = `<div class="upnext-head"><b>Up next</b><span>alerts likely to follow what's happening now</span></div>
    <ul>${list.map(p => `<li class="upnext-${p.kind === "OUTAGE" || p.kind === "SPIDER_STOPPED" ? "bad" : "warn"}">
      <button type="button" class="upnext-item" data-focus="${esc(p.node)}">
        <span class="upnext-what"><b>${esc(alertShort(p.node))}</b> ${esc(p.words)}</span>
        <span class="upnext-when">${upNextWhen(p)} · ${Math.round(p.probability * 100)}%</span>
        <span class="upnext-why">${esc(p.reason)}${p.source === "pipeline" ? " (from the pipeline)" : " (learned)"}</span>
      </button></li>`).join("")}</ul>
    <p class="upnext-score">${esc(scoreLine(nodeAlerts.alertScores))}</p>`;
}
