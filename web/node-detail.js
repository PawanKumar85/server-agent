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
  const tone = h.ignored ? "ignored" : h.up === false ? "down" : h.up ? "up" : "idle";
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
      <span class="nd-badge nd-badge-${tone}">${h.ignored ? (h.up === false ? "Ignored · failing" : "Ignored") : h.up === false ? "Failing" : h.up ? "Live" : "Unchecked"}</span>
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
    ${h.up === false || h.ignored ? `<label class="nd-ignore" title="Still checked and kept in history, but it no longer makes this server DOWN or raises alerts">
      <input type="checkbox" data-ignore-url="${esc(u)}" ${h.ignored ? "checked" : ""}>
      <span>Ignore this stream${h.ignored ? "" : " (known broken or not in use)"}</span></label>` : ""}
  </li>`;
}

function ndStabilityCardHtml(n, st) {
  if (!st || !st.isFlapping) return "";
  const isHi = typeof getLanguage === "function" && getLanguage() === "hi";

  const fixTextToCopy = `[NOC Action for ${n.id} Flapping Issue]
1. Protocol: Switch live encoder push to SRT with 1000ms latency buffer (absorbs internet packet drops).
2. Keyframes: Set strict fixed GOP = 2.0s (e.g. 50 frames @ 25fps) and CBR bitrate on encoder.
3. HLS Storage: Mount HLS fragment path to RAM disk (/dev/shm/hls) to eliminate disk write stalls.
4. Redundancy: Primary feed is active; no broadcast outage occurring.`;

  return `
    <div class="nd-stability-card" id="nd-stability-${esc(n.id)}">
      <div class="nd-stability-head">
        <span class="nd-stability-badge">🔄 ${isHi ? "FLAPPING STREAM / BAR-BAR STALL DETECTED" : "INTERMITTENT STALL / FLAPPING DETECTED"}</span>
        <span class="sub">${st.dropCount} drops · quick recoveries (~${st.avgDuration || 18}s)</span>
      </div>

      <div class="nd-stability-metrics">
        <div class="nd-stability-metric">
          <span class="nd-sm-label">Recent Drops</span>
          <span class="nd-sm-val warn">${st.dropCount} times</span>
        </div>
        <div class="nd-stability-metric">
          <span class="nd-sm-label">Avg Stall</span>
          <span class="nd-sm-val">~${st.avgDuration || 18} s</span>
        </div>
        <div class="nd-stability-metric">
          <span class="nd-sm-label">Broadcast Impact</span>
          <span class="nd-sm-val good">${st.isBackup ? "🛡️ 0% (Safe)" : "Downstream OK"}</span>
        </div>
        <div class="nd-stability-metric">
          <span class="nd-sm-label">Pattern</span>
          <span class="nd-sm-val">${st.isStaleMedia ? "Video Freeze" : "Transient"}</span>
        </div>
      </div>

      <div class="nd-stability-section">
        <div class="nd-stability-title">
          <span>🔍</span> ${isHi ? "Asal Wajah (Root Cause Analysis)" : "Plain-Language Root Cause"}
        </div>
        <div class="nd-stability-text">
          ${isHi
            ? `Server ka OS aur network port <b>bilkul UP hai</b>. Video chunks naye aane me time lag raha hai (<b>STALE_SEGMENTS</b>). ${st.multiUrlOutage ? "Multiple channels ek sath stall hue, jisse saaf hai ki camera ya TV channel ka issue nahi hai, balki <b>upstream encoder network push ya packager buffer</b> ka temporary drop hai." : "Encoder push me packet drop hone par chunks rukte hain aur reconnect hote hi 8s-30s me recover ho jate hain."}`
            : `Server host and network are <b>online and healthy</b>. Incoming video chunks are freezing intermittently (<b>STALE_SEGMENTS</b>). ${st.multiUrlOutage ? "Multiple stream URLs stalled simultaneously, proving the fault is <b>incoming network push jitter or encoder buffer underrun</b>, not a broken channel." : "When encoder packets drop, segments stall for 10-35s and recover immediately upon buffer flush."}`}
        </div>
      </div>

      <div class="nd-stability-fix-box">
        <div class="nd-stability-title">
          <span>🛠️</span> ${isHi ? "Permanent Fix (Fixed Solution)" : "Actionable Fix Checklist for NOC / DevOps"}
        </div>
        <div class="nd-fix-step">
          <span class="nd-fix-num">1</span>
          <span><b>Switch Push to SRT:</b> Use SRT caller with <code>latency=1000</code> ms instead of RTMP to absorb public internet packet drops without freezing.</span>
        </div>
        <div class="nd-fix-step">
          <span class="nd-fix-num">2</span>
          <span><b>Strict 2.0s GOP & CBR:</b> Ensure live encoder has fixed 2.0s keyframe interval (e.g. 50 frames @ 25fps) with Constant Bitrate.</span>
        </div>
        <div class="nd-fix-step">
          <span class="nd-fix-num">3</span>
          <span><b>Mount HLS in RAM (/dev/shm):</b> Point packager output to <code>/dev/shm/hls</code> to eliminate disk I/O write latency.</span>
        </div>
        <div class="nd-fix-copy-row">
          <button type="button" class="btn small nd-copy-btn" onclick="navigator.clipboard.writeText(${esc(JSON.stringify(fixTextToCopy))}).then(() => alert('Fix guide copied to clipboard!'))">📋 Copy Fix Instructions</button>
        </div>
      </div>
    </div>
  `;
}

// "Ignore alerts" switch for each channel this server carries (default off).
function muteSwitchHtml(id) {
  const chs = typeof cardChannels === "function" ? cardChannels(id, null) : [];
  if (!chs.length) return "";
  return `<div class="nd-mutes">${chs.map(c => {
    const m = mutedChannels.get(c);
    return `<label class="nd-mute" title="Checks and history continue; only alerts (voice, toasts) stop">
      <input type="checkbox" data-mute-channel="${esc(c)}" ${m ? "checked" : ""}>
      <span>Ignore alerts for <b>${esc(c)}</b>${m && m.since ? ` <span class="sub">since ${esc(new Date(m.since * 1000).toLocaleString())}</span>` : ""}</span>
    </label>`;
  }).join("")}</div>`;
}

// "Ignore this stream": the server stops counting it from the next check; the panel and cards update now.
document.addEventListener("change", async e => {
  const box = e.target.closest("[data-ignore-url]");
  if (!box) return;
  const url = box.dataset.ignoreUrl, ignored = box.checked;
  box.disabled = true;
  try {
    const res = await fetch("/api/streams/ignore", {method: "PUT", headers: {"Content-Type": "application/json"},
                                                    body: JSON.stringify({url, ignored})});
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    for (const n of G.nodes || []) {
      const h = (n.urlHealth || {})[url];
      if (h) { if (ignored) h.ignored = true; else delete h.ignored; }
    }
    if (typeof toast === "function") toast(ignored ? "warn" : "ok", ignored ? "Stream ignored" : "Stream counted again",
      ignored ? "Still checked, but it no longer makes its server DOWN or raises alerts." : "It counts towards its server's status again.");
    if (typeof renderNodeAlerts === "function") renderNodeAlerts();
    const open = document.querySelector(".node.selected");
    if (open && open.dataset.node && typeof showDetail === "function") showDetail(open.dataset.node);
  } catch (err) {
    box.checked = !ignored;
    if (typeof toast === "function") toast("bad", "Couldn't change it", err.message);
  } finally {
    box.disabled = false;
  }
});

document.addEventListener("change", async e => {
  const box = e.target.closest("[data-mute-channel]");
  if (!box) return;
  box.disabled = true;
  try {
    await setChannelMute(box.dataset.muteChannel, box.checked);
    if (typeof toast === "function") toast(box.checked ? "warn" : "ok", box.checked ? "Alerts ignored" : "Alerts on",
      `${box.dataset.muteChannel}: ${box.checked ? "still checked, but no voice or toasts" : "alerts are back on"}`);
  } catch (err) {
    box.checked = !box.checked;
    if (typeof toast === "function") toast("bad", "Couldn't change it", err.message);
  } finally {
    box.disabled = false;
  }
});

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

  const stability = (typeof analyzeNodeStability === "function") ? analyzeNodeStability(n) : null;
  const stabilityHtml = ndStabilityCardHtml(n, stability);

  const group = (typeof nodeAlerts !== "undefined" && nodeAlerts.groups || []).find(g => (g.nodes || []).includes(id));
  const top = group && group.ranking && group.ranking[0];
  const confirmedList = (typeof getConfirmedOrigins === "function") ? getConfirmedOrigins() : [];
  const confirmedObj = confirmedList.find(o => o.node === id);
  const isConfirmedOrigin = Boolean(confirmedObj);
  const isCandidateOrigin = top && top.node === id;
  const isVictim = top && top.node !== id;

  let originCalloutHtml = "";
  if (isConfirmedOrigin) {
    const victims = confirmedObj.victims || [];
    const sustainedAgo = confirmedObj.onsetAt ? ndAgo(confirmedObj.onsetAt) : `${Math.round(confirmedObj.durationS / 60)}m ago`;
    originCalloutHtml = `
      <div class="nd-origin-callout origin">
        <div class="nd-callout-badge">🚨 CONFIRMED ROOT CAUSE ORIGIN</div>
        <div class="nd-callout-msg">
          <strong>The pipeline problem started on this server</strong> (sustained & monitored for ${sustainedAgo}).
          ${victims.length ? `<div class="nd-callout-sub">⚠️ ${victims.length} downstream server(s) are failing as confirmed victims because of this node.</div>` : ""}
        </div>
        ${typeof rateCauseHtml === "function" ? rateCauseHtml(id, confirmedObj.onsetAt, [...(confirmedObj.groupNodes || []), ...victims]) : ""}
      </div>
    `;
  } else if (isCandidateOrigin) {
    const labels = n.labels || [];
    const isBackupOnly = labels.includes("BackupLink") && !labels.includes("MainInput");
    if (isBackupOnly) {
      originCalloutHtml = `
        <div class="nd-origin-callout" style="border-left-color: #3b82f6; background: rgba(59, 130, 246, 0.08);">
          <div class="nd-callout-badge" style="color: #60a5fa;">ℹ️ BACKUP LINK (STANDBY)</div>
          <div class="nd-callout-msg">
            This redundant backup link has no active segments while primary broadcast is operating normally. Not an outage origin.
          </div>
        </div>
      `;
    } else {
      originCalloutHtml = `
        <div class="nd-origin-callout" style="border-left-color: #f59e0b; background: rgba(245, 158, 11, 0.08);">
          <div class="nd-callout-badge" style="color: #fbbf24;">⏳ MONITORING & CONFIRMATION WINDOW</div>
          <div class="nd-callout-msg">
            Stream blip observed. System is monitoring for 60 seconds across all angles before declaring an origin alert.
          </div>
        </div>
      `;
    }
  } else if (isVictim) {
    originCalloutHtml = `
      <div class="nd-origin-callout victim">
        <div class="nd-callout-badge">⚠️ CASCADING FAILURE (VICTIM)</div>
        <div class="nd-callout-msg">
          This server is not the origin. The problem started upstream on
          <button type="button" class="nd-chip" data-goto="${esc(top.node)}"><span class="dot" style="background:${statusColor((byId[top.node] || {}).status)}"></span>${esc(ndShortHost(top.node))}</button>${top.onsetAt ? ` (${ndAgo(top.onsetAt)})` : ""}.
        </div>
        ${typeof rateCauseHtml === "function" ? rateCauseHtml(top.node, top.onsetAt, [...(group.nodes || [])]) : ""}
      </div>
    `;
  }

  $("#detail-body").innerHTML = `
    <header class="nd-head">
      <h3><span class="dot" style="background:${statusColor(n.status)}"></span><span class="nd-name">${esc(id)}</span></h3>
      <div class="nd-meta">${n.labels.map(r => `<span class="nd-role">${esc(short(r))}</span>`).join("")}
        ${n.serverIp ? `<span class="nd-ip" title="Server IP">${esc(n.serverIp)}</span>` : ""}</div>
    </header>
    ${originCalloutHtml}
    <div class="nd-state nd-state-${state.tone}" role="status">
      <b>${esc(state.head)}</b>${state.text ? `<p>${esc(state.text)}</p>` : ""}
    </div>
    ${muteSwitchHtml(id)}
    <div class="nd-actions">
      ${n.labels.includes("FinalLink") ? `<button class="btn small" data-test="${esc(id)}" ${running ? "disabled" : ""}>▶ Test this channel</button>` : ""}
      <button class="btn small" data-rca="${esc(id)}" title="NOC root-cause analysis from the health checks and the latest traceroute">🧠 Find the cause</button>
      <button class="btn small" data-trace="${esc(id)}" title="Trace the network path to this server now">↯ Traceroute</button>
      <a class="btn small" href="/api/report.html?node=${encodeURIComponent(id)}" target="_blank" rel="noopener" title="Complete report on this node: charts and every stored field">📄 Report</a>
    </div>
    <div class="nd-stats">
      ${stat("Checks passed", uptime == null ? "—" : `${uptime.toFixed(1)}%`, pings ? `${failed.toLocaleString()} of ${pings.toLocaleString()} failed${n.checksWindowDays ? `, last ${n.checksWindowDays} days (ignored streams not counted)` : ""}` : "", uptime != null && uptime < 95 ? "warn" : "")}
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
    ${stabilityHtml}
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
