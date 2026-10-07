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

// --- Per-channel "ignore alerts" (channel_mute.py): checks continue, but the channel raises no toast or voice ---
const mutedChannels = new Map();  // channel -> {since, note}

function cardChannels(nodeId, url) {
  const n = byId[nodeId];
  if (!n) return [];
  const links = (n.links || []).filter(l => !url || l.url === url);
  return [...new Set(links.map(l => l.channel).filter(Boolean))];
}

// A card is muted when every channel it carries is muted (a shared server still alerts for its live channels).
function isCardMuted(nodeId, url) {
  const chs = cardChannels(nodeId, url);
  return chs.length > 0 && chs.every(c => mutedChannels.has(c));
}

async function loadChannelMutes() {
  try {
    const res = await fetch("/api/channels/mutes");
    if (!res.ok) return;
    const data = await res.json();
    mutedChannels.clear();
    Object.entries(data.muted || {}).forEach(([c, v]) => mutedChannels.set(c, v));
    if (typeof renderNodeAlerts === "function" && typeof world !== "undefined") renderNodeAlerts();
  } catch (_) { /* the switch is optional: alerts behave normally without it */ }
}

async function setChannelMute(channel, muted, note = null) {
  const res = await fetch(`/api/channels/${encodeURIComponent(channel)}/mute`, {
    method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({muted, note})});
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  await loadChannelMutes();
}
loadChannelMutes();
setInterval(loadChannelMutes, 30000);  // another operator may change it

// --- Confirmation by the Node Agent (agent_hub.py): a server running an agent is asked before an outage is called ---
const agentChecks = {};  // server -> {agent, state: "fresh" | "lost", ago, hlsUrl, hlsStatus, hlsAge, hostProblem}

async function loadAgentChecks() {
  try {
    const res = await fetch("/api/agents/checks");
    if (!res.ok) return;
    const data = await res.json();
    Object.keys(agentChecks).forEach(k => delete agentChecks[k]);
    Object.assign(agentChecks, data.checks || {});
  } catch (_) { /* no agents: alerts behave as before */ }
}
loadAgentChecks();
setInterval(loadAgentChecks, 10000);

// The server's agent says: "confirms" (it sees the problem too, or went silent with it), "contradicts" (it watches
// the failing stream from inside and it plays fine: the fault is on the way, not on the server), or null (no agent,
// or it can't tell). A channel card "<host>/<channel>" asks its host's agent.
function agentVerdict(nodeId, url) {
  const a = agentChecks[nodeId] || agentChecks[String(nodeId).split("/")[0]];
  if (!a) return null;
  const who = `Node agent ${a.agent}`;
  if (a.state === "lost") {
    return {verdict: "confirms", text: `${who} on the server is silent too${a.ago != null ? ` (${alertSince(new Date(Date.now() - a.ago * 1000).toISOString())})` : ""}.`};
  }
  if (a.hostProblem) return {verdict: "confirms", text: `Confirmed from inside the server: ${a.hostProblem}.`};
  const n = byId[nodeId];
  const failingUrls = url ? [url] : Object.entries((n && n.urlHealth) || {}).filter(([, h]) => h.up === false).map(([u]) => u);
  if (!a.hlsUrl || !failingUrls.includes(a.hlsUrl)) return null;  // it watches another stream: no say on this one
  const age = a.hlsAge != null ? ` (newest piece ${Math.round(a.hlsAge)} s old)` : "";
  if (["STALE", "DOWN", "DEGRADED"].includes(a.hlsStatus)) {
    return {verdict: "confirms", text: `${who} sees it from inside too: ${a.hlsStatus.toLowerCase()}${age}.`};
  }
  if (a.hlsStatus === "FRESH") {
    return {verdict: "contradicts", text: `${who} watches this stream on the server and it is playing${age}: ` +
                                          "likely the network or CDN path, not the server."};
  }
  return null;
}
window.agentVerdict = agentVerdict;

// An outage ("down") is only called when the server's agent, if it has one, doesn't see the stream playing.
function confirmWithAgent(nodeId, url, alert) {
  if (!alert || alert.tone !== "down") return alert;
  const v = agentVerdict(nodeId, url);
  if (!v) return alert;
  if (v.verdict === "contradicts") {
    return {tone: "warn", title: "Not confirmed by the server", text: `${alert.title}, seen from outside only. ${v.text}`,
            pill: "⚠ outside only", key: `agent-unconfirmed-${nodeId}`, since: alert.since, agent: v};
  }
  return {...alert, text: `${alert.text} ${v.text}`.trim(), tag: alert.tag || "Confirmed by agent", agent: v};
}

// The alert for a card: the whole server, or (url) one channel's Final card. null when there's nothing to say,
// or when the operator chose to ignore this channel's alerts.
function cardAlert(nodeId, url) {
  if (isCardMuted(nodeId, url)) return null;
  return confirmWithAgent(nodeId, url, cardAlertRaw(nodeId, url));
}

function cardAlertRaw(nodeId, url) {
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
    if (stoppedSpider.at && stoppedSpider.at !== nodeId) {
      return {
        tone: "affected",
        title: "Cascading failure",
        text: `Downstream victim. Ingest stream stopped upstream at ${rootHost}: ${stoppedSpider.stopReason || "Feed stopped"}.`,
        pill: `⚠️ Affected by ${rootHost}`,
        tag: "Cascading Victim",
        key: `stopped-spider-${nodeId}`,
        since: stoppedSpider.lastStepAt || ""
      };
    }
    const title = is404 ? "🚨 Ingest stream missing (HTTP 404)" : "🚨 Upstream feed failed";
    const text = is404
      ? `Origin ${rootHost} missing manifest. MCR team: restart live encoder push.`
      : (stoppedSpider.stopReason || `Stopped at ${rootHost}.`);
    return {
      tone: "down",
      title,
      text,
      tag: "🚨 Problem Origin",
      key: `stopped-spider-${nodeId}`,
      since: stoppedSpider.lastStepAt || ""
    };
  }

  if (failing.length || (!url && n.status === "DOWN")) {
    const since = failing.map(h => h.onsetAt).filter(Boolean).sort()[0];
    const onsetMs = since ? Date.parse(since.replace(/(\.\d{3})\d+/, "$1")) : 0;
    const failingAgeS = onsetMs ? Math.max(0, Math.round((Date.now() - onsetMs) / 1000)) : 0;
    const labels = n.labels || [];
    const isBackupOnly = labels.includes("BackupLink") && !labels.includes("MainInput");
    const hasDownstreamImpact = group && (group.nodes || []).length > 1;

    // Angle 1: If BackupLink and NO downstream victims -> Standby backup link idle, not an outage!
    if (isBackupOnly && !hasDownstreamImpact) {
      return {
        tone: "warn",
        title: "Backup link standby",
        text: `Backup input has no active segments. MainInput is serving broadcast without outage.`,
        pill: "Backup standby",
        key: `backup-idle-${nodeId}`,
        since
      };
    }

    if (top && top.node !== nodeId) {
      return {tone: "affected", title: "Cascading failure", key: `${top.node}`, pill: `⚠️ Affected by ${alertShort(top.node)}`,
              text: `Downstream victim: problem started upstream on ${alertShort(top.node)}${top.onsetAt ? ` (${alertSince(top.onsetAt)})` : ""}.`, since};
    }

    const p = failing.length ? urlProblem(failing[0]) : {title: "Down", text: n.lastError || ""};
    const extra = failing.length > 1 ? ` ${failing.length} streams failing.` : "";

    // Angle 2: Temporal confirmation - monitor for at least 1 minute (60s) before alerting as Outage/Origin
    if (failingAgeS < 60) {
      const stability = (typeof analyzeNodeStability === "function") ? analyzeNodeStability(n) : null;
      const isFlap = stability && stability.isFlapping;
      return {
        tone: "warn",
        title: isFlap ? "Intermittent stream stall (flapping)" : "Monitoring health check",
        text: isFlap
          ? `Stream stall #${stability.dropCount} (${failingAgeS}s / 60s). Frequent 8s-35s stalls due to video packet jitter. Broadcast safe.`
          : `Observed stream blip (${failingAgeS}s / 60s confirmation window). Monitoring across all angles before raising alarm.`,
        pill: isFlap ? `🔄 Flapping (${60 - failingAgeS}s)` : `⏳ Monitoring (${60 - failingAgeS}s)`,
        key: `monitoring-${nodeId}`,
        since
      };
    }

    const isOrigin = top && top.node === nodeId && (hasDownstreamImpact || !isBackupOnly);
    const originTag = isOrigin ? "🚨 Confirmed Origin" : "";
    return {
      tone: "down",
      title: isOrigin ? "🚨 Confirmed Outage Origin" : p.title,
      text: (isOrigin ? `Confirmed after 1 min+ monitoring (${alertSince(since)}): problem started here first. ` : "") + p.text + (p.text && !p.text.endsWith(".") ? "." : "") + extra,
      tag: originTag,
      trace: nodeAlerts.traces[nodeId],
      since,
      key: failing.map(h => h.category).join(",")
    };
  }
  const stability = (typeof analyzeNodeStability === "function") ? analyzeNodeStability(n) : null;
  if (stability && stability.isFlapping) {
    return {
      tone: "flapping",
      title: "Intermittent stream stalls (flapping)",
      key: `flapping-${nodeId}`,
      pill: `🔄 Flapping (${stability.dropCount} drops)`,
      text: `Server is currently UP, but has ${stability.dropCount} transient stalls (~${stability.avgDuration || 18}s). Click for root cause & permanent fix guide.`,
      since: stability.lastOutageAt || ""
    };
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

// Multi-Angle Confirmation Engine:
// 1. Minimum 1-minute (60 seconds) sustained observation window.
// 2. Redundancy role check: BackupLink without downstream victims is normal standby, NOT an outage.
// 3. Downstream impact / spider check: must affect downstream delivery or be confirmed primary ingest.
function getConfirmedOrigins() {
  const origins = [];
  const seenNodes = new Set();
  const now = Date.now();

  for (const g of (nodeAlerts.groups || [])) {
    const ranking = g.ranking || [];
    if (!ranking.length) continue;
    const top = ranking[0];
    const n = byId[top.node];
    const isDown = (n && n.status === "DOWN") || Object.values((n && n.urlHealth) || {}).some(h => h.up === false);
    if (!isDown && (top.score || 0) < 0.3) continue;

    const labels = (n && n.labels) || [];
    const isBackupOnly = labels.includes("BackupLink") && !labels.includes("MainInput");
    const victims = ranking.slice(1).map(r => r.node).filter(vid => {
      const vn = byId[vid];
      return vn && (vn.status === "DOWN" || Object.values(vn.urlHealth || {}).some(h => h.up === false));
    });

    // Angle 1: BackupLink without downstream victims is normal standby, never an outage origin
    if (isBackupOnly && victims.length === 0) continue;

    // Angle 2: Temporal 1-minute observation window
    const onsetMs = top.onsetAt ? Date.parse(top.onsetAt.replace(/(\.\d{3})\d+/, "$1")) : 0;
    const ageS = onsetMs ? Math.max(0, Math.round((now - onsetMs) / 1000)) : 0;
    if (ageS < 60) continue; // Sustained for >= 1 minute

    if ((agentVerdict(top.node) || {}).verdict === "contradicts") continue;  // the server sees its stream fine

    if (!seenNodes.has(top.node)) {
      seenNodes.add(top.node);
      origins.push({
        node: top.node,
        role: (n && labels.find(l => ROLES.includes(l))) || (labels && labels[0]) || "MainInput",
        score: top.score,
        onsetAt: top.onsetAt,
        durationS: ageS,
        reasons: top.reasons || [],
        victims: victims,
        groupNodes: g.nodes || []
      });
    }
  }

  for (const s of (G.spiders || [])) {
    if (s.status === "STOPPED" && s.at && !seenNodes.has(s.at)) {
      const n = byId[s.at];
      const labels = (n && n.labels) || [];
      const isBackupOnly = labels.includes("BackupLink") && !labels.includes("MainInput");

      // Verify duration: spider stop must be monitored for at least 60s
      const stopMs = s.lastStepAt ? Date.parse(s.lastStepAt.replace(/(\.\d{3})\d+/, "$1")) : 0;
      const ageS = stopMs ? Math.max(0, Math.round((now - stopMs) / 1000)) : 0;
      if (ageS < 60) continue;

      const victims = [s.finalLinkId].filter(f => f && f !== s.at);
      if (isBackupOnly && victims.length === 0) continue;
      if ((agentVerdict(s.at) || {}).verdict === "contradicts") continue;

      seenNodes.add(s.at);
      origins.push({
        node: s.at,
        role: (n && labels.find(l => ROLES.includes(l))) || "MainInput",
        score: 1.0,
        onsetAt: s.lastStepAt,
        durationS: ageS,
        reasons: [s.stopReason || "Spider stopped at this upstream dependency"],
        victims: victims,
        groupNodes: [s.at, s.finalLinkId].filter(Boolean)
      });
    }
  }

  // Fallback: ONLY primary nodes (MainInput / FinalLink) that have been DOWN for >= 60s
  // Standby BackupLinks are strictly excluded
  if (!origins.length) {
    for (const n of (G.nodes || [])) {
      if (seenNodes.has(n.id)) continue;
      const labels = n.labels || [];
      const isBackupOnly = labels.includes("BackupLink") && !labels.includes("MainInput");
      if (isBackupOnly) continue;

      const failingUrls = Object.values(n.urlHealth || {}).filter(h => h.up === false);
      if (n.status === "DOWN" || failingUrls.length) {
        const earliestOnset = failingUrls.map(h => h.onsetAt).filter(Boolean).sort()[0];
        const onsetMs = earliestOnset ? Date.parse(earliestOnset.replace(/(\.\d{3})\d+/, "$1")) : 0;
        const ageS = onsetMs ? Math.max(0, Math.round((now - onsetMs) / 1000)) : 0;

        if (ageS < 60) continue;
        if ((agentVerdict(n.id) || {}).verdict === "contradicts") continue;

        seenNodes.add(n.id);
        origins.push({
          node: n.id,
          role: labels.find(l => ROLES.includes(l)) || "Server",
          score: 1.0,
          onsetAt: earliestOnset || null,
          durationS: ageS,
          reasons: [failingUrls[0] ? (failingUrls[0].detail || failingUrls[0].category || "Stream failure") : (n.lastError || "Node DOWN")],
          victims: [],
          groupNodes: [n.id]
        });
      }
    }
  }

  return origins;
}
window.getConfirmedOrigins = getConfirmedOrigins;

function renderNodeAlerts() {
  const confirmedOrigins = getConfirmedOrigins();
  const allOrigins = new Set(confirmedOrigins.map(o => o.node));

  const allVictims = new Set();
  (nodeAlerts.groups || []).forEach(g => {
    (g.ranking || []).slice(1).forEach(r => allVictims.add(r.node));
  });

  $$(".node[data-node]", world).forEach(card => {
    const id = card.dataset.node;
    card.classList.toggle("origin-root", allOrigins.has(id));
    card.classList.toggle("cascading-victim", allVictims.has(id) && !allOrigins.has(id));

    card.querySelector(".node-alert, .node-alert-pill, .node-muted-tag")?.remove();
    card.classList.remove("has-alert");
    const muted = isCardMuted(id, card.dataset.url || null);
    card.classList.toggle("channel-muted", muted);
    if (muted) {  // never silently: the card says its alerts are being ignored
      const tag = document.createElement("div");
      tag.className = "node-muted-tag";
      tag.textContent = "🔕 Alerts ignored";
      tag.title = "This channel is still checked, but raises no alerts. Open the panel to turn alerts back on.";
      card.appendChild(tag);
    }
    const a = cardAlert(id, card.dataset.url || null);
    if (!a) return;
    const key = `${id}|${card.dataset.url || ""}|${a.tone}|${a.title}|${a.key || ""}|${a.since || ""}`;
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
      el.addEventListener("click", e => { e.stopPropagation(); showDetail(id); });
      card.appendChild(el);
      return;
    }
    el.className = `node-alert node-alert-${a.tone}${a.tag?.includes("Origin") ? " alert-origin-pulse" : ""}`;
    el.innerHTML = `
      <div class="node-alert-head">
        <b>${esc(a.title)}</b>
        ${a.since ? `<span class="node-alert-since">${esc(alertSince(a.since))}</span>` : ""}
        <button type="button" class="node-alert-x" aria-label="Dismiss" title="Dismiss">×</button>
      </div>
      ${a.text || a.tag ? `<div class="node-alert-text">${a.tag ? `<span class="node-alert-tag ${a.tag.includes("Origin") ? "tag-origin" : ""}">${esc(a.tag)}</span> ` : ""}${esc(a.text || "")}</div>` : ""}
      ${a.trace ? `<div class="node-alert-trace">Network: ${esc(a.trace)}</div>` : ""}`;
    el.querySelector(".node-alert-x").addEventListener("click", e => {
      e.stopPropagation();
      nodeAlerts.dismissed.add(key);
      el.remove(); card.classList.remove("has-alert");
    });
    el.addEventListener("mousedown", e => e.stopPropagation());
    el.addEventListener("click", e => { e.stopPropagation(); showDetail(id); });
    card.classList.add("has-alert");
    card.appendChild(el);
  });

  renderOriginBanner();

  if (window.ttsAlerts && typeof window.ttsAlerts.onAlertsRendered === "function") {
    window.ttsAlerts.onAlertsRendered();
  }
}

// The banner in words anyone can act on: which channels are off air, what broke where, and what to do.
function channelOf(id) {
  const s = String(id || "");
  return s.includes("/") ? s.split("/").pop() : null;
}

function plainOutage(orig) {
  const n = byId[orig.node] || {};
  const server = alertShort(orig.node);
  const failing = Object.entries(n.urlHealth || {}).filter(([, h]) => h.up === false);
  const linkChannel = url => ((n.links || []).find(l => l.url === url) || {}).channel || channelOf(String(url).replace(/\/[^/]*$/, ""));
  const failingChannels = [...new Set(failing.map(([u]) => linkChannel(u)).filter(Boolean))];
  const offAir = [...new Set((orig.victims || []).map(channelOf).filter(Boolean))];
  const channels = offAir.length ? offAir : failingChannels.length ? failingChannels : [channelOf(orig.node) || server];
  const feed = failingChannels.length ? failingChannels.join(", ") : channels.join(", ");
  const h = (failing[0] || [])[1] || {};
  const detail = `${h.detail || ""} ${h.category || ""} ${h.freshness || ""} ${(orig.reasons || []).join(" ")}`;

  let problem, fix;
  if (/\b404\b|PLAYLIST_MISSING/.test(detail)) {
    problem = `The ${feed} stream is missing on the ${server} server.`;
    fix = `Restart the encoder that sends ${feed} to ${server}.`;
  } else if (/STALE|NO_SEGMENTS|0\/\d+ variants live/.test(detail)) {
    problem = `The ${feed} video is frozen on the ${server} server (no new video coming in).`;
    fix = `Check the encoder sending ${feed} to ${server}: it is connected but stopped sending video.`;
  } else if (/UNREACHABLE|timeout|CONNECT/i.test(detail) || n.status === "DOWN" && !failing.length) {
    problem = `The ${server} server is not reachable.`;
    fix = `Check that ${server} is switched on and its internet is working.`;
  } else if (/\b5\d\d\b|SERVER_ERROR/.test(detail)) {
    problem = `The ${server} server is giving errors for ${feed}.`;
    fix = `Restart the streaming service on ${server}.`;
  } else {
    problem = `The ${feed} stream on the ${server} server is not working.`;
    fix = `Open ${server} (Show server) and check the stream.`;
  }
  const v = typeof agentVerdict === "function" ? agentVerdict(orig.node) : null;
  if (v && v.verdict === "confirms") problem += " " + v.text;
  const since = orig.onsetAt ? alertSince(orig.onsetAt) : `${Math.max(1, Math.round(orig.durationS / 60))} min`;
  return {channels: channels.join(", "), problem, fix, since};
}

function renderOriginBanner() {
  const banner = $("#rca-origin-banner");
  if (!banner) return;

  const origins = getConfirmedOrigins();

  if (!origins.length) {
    banner.hidden = true;
    banner.innerHTML = "";
    return;
  }

  banner.hidden = false;
  banner.innerHTML = origins.map(orig => {
    const p = plainOutage(orig);
    const technical = (orig.reasons || []).join(" · ");
    return `
      <div class="origin-banner-item" data-origin="${esc(orig.node)}">
        <div class="origin-main-col">
          <div class="origin-header-line">
            <span class="origin-alert-pill">🔴 OFF AIR</span>
            <span class="origin-domain-name">${esc(p.channels)}</span>
            <span class="origin-time-badge">down for ${esc(p.since)}</span>
          </div>
          <div class="origin-reason-line">
            <span class="origin-cause-label">Problem:</span>
            <span class="origin-cause-val">${esc(p.problem)}</span>
          </div>
          <div class="origin-victim-line">
            <span class="origin-victim-label">What to do:</span>
            <span class="origin-victim-val">${esc(p.fix)}</span>
          </div>
          ${technical ? `<details class="origin-details"><summary>Details</summary>${esc(technical)}</details>` : ""}
        </div>
        <div class="origin-action-col">
          <button type="button" class="btn small origin-jump-btn" data-locate="${esc(orig.node)}" title="Show this server on the map">
            Show server
          </button>
        </div>
      </div>
    `;
  }).join("");

  banner.querySelectorAll("[data-locate]").forEach(btn => {
    btn.addEventListener("click", e => {
      e.stopPropagation();
      const targetId = btn.dataset.locate;
      if (targetId && byId[targetId]) {
        if (typeof fit === "function") fit(cardIds(targetId));
        if (typeof showDetail === "function") showDetail(targetId);
      }
    });
  });
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
