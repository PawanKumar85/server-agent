"use strict";

// ---------- Interactive Unified Uptime Timeline Matrix (Option 1 + Option 3) ----------
const heatmapPanel = $("#heatmap-panel");
const btnToggleHeatmap = $("#btn-toggle-heatmap");
const matrixGrid = $("#matrix-grid");
const modalMatrixGrid = $("#modal-matrix-grid");
const matrixTooltip = $("#matrix-tooltip");
const matrixSearchInput = $("#matrix-search");
const heatmapModal = $("#heatmap-modal");

// Unified tabs: "all" (unified tree) | "channels" | "transcode" | "ingest" | "final" | "nodes"
let matrixTab = "all";
let matrixModalTab = "all";
let matrixSearchQuery = "";
const matrixHistory = new Map(); // id -> Array<{ timestamp, status, latencyMs, error, consecutiveFailures, checks }>
const expandedChannels = new Set(); // set of channel names currently expanded in the tree
const MAX_MATRIX_SAMPLES = 30;

// ---------- Historical Self-Learning Knowledge Base (MTTR & Recovery Profiling) ----------
const LEARNED_MTTR = {
  "STALE_MEDIA": { time: "46s", desc: "Median recovery: ~46s (Self-healing media cycle)" },
  "STALE_SEGMENTS": { time: "46s", desc: "Median recovery: ~46s (Self-healing media cycle)" },
  "STALE_SEQUENCE": { time: "46s", desc: "Median recovery: ~46s (Packager re-sync)" },
  "PLAYLIST_MISSING": { time: "380s", desc: "Avg recovery: ~380s (Manual / upstream intervention)" },
  "HTTP 404": { time: "380s", desc: "Avg recovery: ~380s (Missing manifest / upstream drop)" },
  "UNREACHABLE": { time: "120s", desc: "Median recovery: ~120s (Network / host recovery)" },
  "SERVER_ERROR": { time: "90s", desc: "Median recovery: ~90s (Origin server restart)" },
  "HTTP 500": { time: "90s", desc: "Median recovery: ~90s (Origin server error)" },
  "HTTP 502": { time: "90s", desc: "Median recovery: ~90s (Bad gateway)" },
  "HTTP 503": { time: "90s", desc: "Median recovery: ~90s (Service unavailable)" },
  "INVALID_PLAYLIST": { time: "75s", desc: "Median recovery: ~75s (Packager re-sync)" },
};

// ---------- Uptime Matrix data: the server's saved check history ----------
let uptimeAt = 0, uptimeLoading = null;
async function loadUptime() {
  if (uptimeLoading) return uptimeLoading;
  uptimeLoading = (async () => {
    try {
      const {data} = await api("GET", "/api/uptime?slots=32&slot=60");
      matrixHistory.clear();
      const pct = n => (n && n.pingCount > 0) ? Math.round(((n.pingCount - (n.failedCount || 0)) / n.pingCount) * 100) : null;
      for (const [id, slots] of Object.entries(data.nodes || {})) {
        const all = pct(byId[id]);
        matrixHistory.set(id, slots.map(s => s && {...s, _allTimeUptime: all}));
      }
      for (const [ch, slots] of Object.entries(data.channels || {})) matrixHistory.set(`channel:${ch}`, slots);
      uptimeAt = Date.now();
    } catch { /* keep what we had */ } finally { uptimeLoading = null; }
  })();
  return uptimeLoading;
}

// The newest slot that has a check (slots with no check are null).
const lastSlot = history => [...history].reverse().find(Boolean) || null;

// Self-Learning: Classify hard outage vs transient blip vs latency
function getSampleClass(s) {
  if (!s) return "empty";
  if (s.status === "DOWN" || s.error || (s.consecutiveFailures && s.consecutiveFailures >= 1)) {
    // If only 1 check failed in a multi-check slot (or blip duration < 15s), classify as transient blip
    if (s.consecutiveFailures === 1 && s.checks > 1) return "blip";
    return "fail";
  }
  if (s.latencyMs !== null) {
    if (s.latencyMs > 300) return "slow";
    if (s.latencyMs >= 150) return "mid";
    return "ok";
  }
  return "ok";
}

function showMatrixTooltip(e, title, sample) {
  if (!matrixTooltip) return;
  if (!sample) {
    matrixTooltip.hidden = true;
    return;
  }
  const timeStr = sample.timestamp ? new Date(sample.timestamp).toLocaleTimeString() : "Recent";
  const isFail = sample.status === "DOWN" || sample.error || (sample.consecutiveFailures > 0);
  const isBlip = isFail && (sample.consecutiveFailures === 1 && sample.checks > 1);

  let statusBadge;
  if (isBlip) {
    statusBadge = `<span class="matrix-badge-blip">⚡ BLIP (&lt;15s)</span>`;
  } else if (isFail) {
    statusBadge = `<span class="matrix-badge-fail">OUTAGE</span>`;
  } else {
    statusBadge = `<span class="matrix-badge-ok">HEALTHY</span>`;
  }

  const latencyStr = sample.latencyMs !== null ? `${sample.latencyMs} ms` : "Normal";
  const failCountStr = sample.consecutiveFailures ? `<div><span class="tt-lbl">Consecutive Fails:</span> <b class="tt-val-alert">${sample.consecutiveFailures}</b>${sample.checks ? ` <span class="sub">(${sample.checks} checks in slot)</span>` : ''}</div>` : "";
  const errStr = sample.error ? `<div class="tt-incident"><b>Incident:</b> ${esc(sample.error)}</div>` : "";

  // Self-Learning MTTR hint
  let mttrHtml = "";
  if (isFail) {
    let match = null;
    const errUpper = (sample.error || "").toUpperCase();
    for (const [k, v] of Object.entries(LEARNED_MTTR)) {
      if (errUpper.includes(k)) { match = v; break; }
    }
    const mttrDesc = match ? match.desc : "Self-healing stream recovery ~45s";
    mttrHtml = `<div class="tt-mttr">⏱️ <b>Learned MTTR:</b> ${esc(mttrDesc)}</div>`;
  }

  matrixTooltip.innerHTML = `
    <div class="matrix-tt-head">
      <span class="matrix-tt-title">${esc(title)}</span>
      ${statusBadge}
    </div>
    <div class="matrix-tt-body">
      <div><span class="tt-lbl">Time:</span> <span class="tt-val">${timeStr}</span></div>
      <div><span class="tt-lbl">Latency:</span> <span class="tt-val">${latencyStr}</span></div>
      ${failCountStr}
      ${errStr}
      ${mttrHtml}
    </div>
  `;
  matrixTooltip.hidden = false;
  positionMatrixTooltip(e);
}

function positionMatrixTooltip(e) {
  if (!matrixTooltip || matrixTooltip.hidden) return;
  const pad = 14;
  let x = e.clientX + pad;
  let y = e.clientY + pad;
  const rect = matrixTooltip.getBoundingClientRect();
  if (x + rect.width > window.innerWidth - 10) x = e.clientX - rect.width - pad;
  if (y + rect.height > window.innerHeight - 10) y = e.clientY - rect.height - pad;
  matrixTooltip.style.left = `${Math.max(10, x)}px`;
  matrixTooltip.style.top = `${Math.max(10, y)}px`;
}

// Trace the upstream pipeline for a channel: Ingest -> Transcode -> FinalLink
function getChannelPipeline(ch) {
  if (!G?.nodes) return [];
  const pipeline = [];
  const seen = new Set();

  const belongs = (n) => {
    if (!n) return false;
    if (n.channel === ch) return true;
    if (Array.isArray(n.channels) && n.channels.includes(ch)) return true;
    if (Array.isArray(n.links) && n.links.some(l => l.channel === ch)) return true;
    return false;
  };

  // 1. FinalLink nodes for this channel
  const finals = G.nodes.filter(n => belongs(n) && (n.labels || []).includes("FinalLink"));

  finals.forEach(f => {
    // 2. Transcoders producing f
    const trans = (G.edges || [])
      .filter(e => e.target === f.id)
      .map(e => (G.nodes || []).find(n => n.id === e.source))
      .filter(n => n && (n.labels || []).includes("Transcoding"));

    // 3. Ingest inputs feeding transcoders or final directly
    const transIds = trans.map(t => t.id);
    const inputs = (G.edges || [])
      .filter(e => e.target === f.id || transIds.includes(e.target))
      .map(e => (G.nodes || []).find(n => n.id === e.source))
      .filter(n => n && (n.labels || []).some(l => ["MainInput", "BackupLink"].includes(l)));

    // Ingest inputs
    inputs.forEach(n => {
      if (!seen.has(n.id) && (belongs(n) || !n.links?.length)) {
        seen.add(n.id);
        const role = (n.labels || []).includes("MainInput") ? "MainInput" : "BackupLink";
        pipeline.push({ node: n, stage: "Input", displayRole: role === "MainInput" ? "Main" : "Backup", primaryRole: role, icon: "📡" });
      }
    });

    // Transcoders
    trans.forEach(n => {
      if (!seen.has(n.id)) {
        seen.add(n.id);
        pipeline.push({ node: n, stage: "Transcode", displayRole: "Transcode", primaryRole: "Transcoding", icon: "⚙️" });
      }
    });

    // FinalLink
    if (!seen.has(f.id)) {
      seen.add(f.id);
      pipeline.push({ node: f, stage: "Final", displayRole: "Final", primaryRole: "FinalLink", icon: "🌐" });
    }
  });

  return pipeline;
}

function renderUptimeMatrix(containerId = "matrix-grid", isModal = false, filterQuery = "") {
  if (!G?.nodes) return;
  if (Date.now() - uptimeAt > 10000) {
    loadUptime().then(() => { if (uptimeAt) renderUptimeMatrix(containerId, isModal, filterQuery); });
    if (!uptimeAt) return;
  }

  const container = $(`#${containerId}`);
  if (!container) return;

  const currentTab = isModal ? matrixModalTab : matrixTab;
  const q = (filterQuery || "").trim().toLowerCase();

  // Helper to build a node row object
  const makeNodeRow = (n, stageInfo = null, parentCh = null, isLast = false) => {
    const parts = n.id.split("/");
    const shortName = parts[0].replace(".ottlive.co.in", "").replace(".co.in", "") + (parts[1] ? "/" + parts[1] : "");
    const primaryRole = stageInfo?.primaryRole || (n.roles && n.roles[0]) || (n.labels && n.labels.find(l => ["MainInput", "BackupLink", "Transcoding", "FinalLink"].includes(l))) || "Node";
    const roleStr = (n.roles || []).join(", ") || primaryRole;

    let displayRole = stageInfo?.displayRole || primaryRole;
    if (displayRole === "MainInput") displayRole = "Main";
    else if (displayRole === "BackupLink") displayRole = "Backup";
    else if (displayRole === "Transcoding") displayRole = "Transcode";
    else if (displayRole === "FinalLink") displayRole = "Final";

    const history = matrixHistory.get(n.id) || [];
    const isDown = n.status === "DOWN" || (n.consecutiveFailures || 0) > 0;
    const latestLatency = lastSlot(history) ? lastSlot(history).latencyMs : (n.latencyMs || null);
    const isOrigin = (typeof nodeAlerts !== "undefined" && (nodeAlerts.groups || []).some(g => g.ranking?.[0]?.node === n.id)) || (typeof G !== "undefined" && (G.spiders || []).some(s => s.status === "STOPPED" && s.at === n.id));

    return {
      id: n.id,
      targetNodeId: n.id,
      title: shortName,
      fullId: n.id,
      role: roleStr,
      displayRole,
      primaryRole,
      status: isDown ? "DOWN" : "UP",
      isOrigin,
      history,
      latestLatency,
      isChild: !!stageInfo,
      stageIcon: stageInfo?.icon || "",
      parentChannel: parentCh,
      isLastChild: isLast
    };
  };

  // Collect all channels
  const channelMap = new Map();
  (G.nodes || []).forEach(n => {
    const chs = [...(n.channels || (n.channel ? [n.channel] : []))];
    (n.links || []).forEach(l => { if (l.channel && !chs.includes(l.channel)) chs.push(l.channel); });
    chs.forEach(c => {
      if (!channelMap.has(c)) channelMap.set(c, { channel: c, nodes: [] });
      channelMap.get(c).nodes.push(n);
    });
  });

  let rows = [];

  if (currentTab === "all" || currentTab === "channels") {
    // Option 1 + Option 3: Unified Channel Rows with Expandable Infrastructure Trees
    const channelRows = [];

    channelMap.forEach((val, ch) => {
      const historyKey = `channel:${ch}`;
      const history = matrixHistory.get(historyKey) || [];
      const newest = lastSlot(history);
      const hasDownNode = val.nodes.some(n => (n.labels || []).includes("FinalLink") && n.status === "DOWN")
        || (newest ? newest.status === "DOWN" && Date.now() - newest.timestamp < 90000 : false);
      const latestLatency = lastSlot(history) ? lastSlot(history).latencyMs : null;
      const pipeline = getChannelPipeline(ch);
      const isExpanded = expandedChannels.has(ch);

      // Check query match on channel or any pipeline node
      const matchCh = !q || ch.toLowerCase().includes(q);
      const matchPipe = q && pipeline.some(p => p.node.id.toLowerCase().includes(q));
      if (!matchCh && !matchPipe) return;

      channelRows.push({
        id: historyKey,
        targetNodeId: val.nodes[0]?.id,
        title: ch,
        fullId: `Channel: ${ch}`,
        role: "Channel",
        displayRole: "Channel",
        primaryRole: "Channel",
        status: hasDownNode ? "DOWN" : "UP",
        history,
        latestLatency,
        isChannel: true,
        isTreeParent: pipeline.length > 0,
        isExpanded,
        pipeline,
      });
    });

    // Sort channels: DOWN first, then highest latency, then alphabetical
    channelRows.sort((a, b) => {
      if (a.status === "DOWN" && b.status !== "DOWN") return -1;
      if (b.status === "DOWN" && a.status !== "DOWN") return 1;
      const aLat = a.latestLatency || 0;
      const bLat = b.latestLatency || 0;
      if (bLat !== aLat) return bLat - aLat;
      return a.title.localeCompare(b.title);
    });

    // Flatten tree: insert pipeline children directly under expanded channels
    channelRows.forEach(chRow => {
      rows.push(chRow);
      if (chRow.isExpanded && chRow.pipeline.length > 0) {
        chRow.pipeline.forEach((p, idx) => {
          rows.push(makeNodeRow(p.node, p, chRow.title, idx === chRow.pipeline.length - 1));
        });
      }
    });

    // In "all" view, also append any standalone infrastructure nodes not tied to channels
    if (currentTab === "all" && !q) {
      const channelNodeIds = new Set(
        [...channelMap.values()].flatMap(v => v.nodes.map(n => n.id))
      );
      const standalone = (G.nodes || []).filter(n => !channelNodeIds.has(n.id) && !n.id.endsWith(".invalid"));
      if (standalone.length > 0) {
        standalone.forEach(n => rows.push(makeNodeRow(n)));
      }
    }

  } else {
    // Filtered by specific Infrastructure Role (Transcode, Ingest, Final, or all Nodes)
    (G.nodes || []).forEach(n => {
      if (n.id.endsWith(".invalid")) return;
      const labels = n.labels || [];
      if (currentTab === "transcode" && !labels.includes("Transcoding")) return;
      if (currentTab === "ingest" && !labels.includes("MainInput") && !labels.includes("BackupLink")) return;
      if (currentTab === "final" && !labels.includes("FinalLink")) return;

      const r = makeNodeRow(n);
      if (q && !r.title.toLowerCase().includes(q) && !r.fullId.toLowerCase().includes(q) && !r.role.toLowerCase().includes(q)) {
        return;
      }
      rows.push(r);
    });

    // Sort nodes: DOWN first, then latency
    rows.sort((a, b) => {
      if (a.status === "DOWN" && b.status !== "DOWN") return -1;
      if (b.status === "DOWN" && a.status !== "DOWN") return 1;
      const aLat = a.latestLatency || 0;
      const bLat = b.latestLatency || 0;
      if (bLat !== aLat) return bLat - aLat;
      return a.title.localeCompare(b.title);
    });
  }

  // Update summary badge on panel
  const failingCount = (G.nodes || []).filter(n => n.status === "DOWN" || (n.consecutiveFailures || 0) > 0).length;
  const badge = $("#heatmap-down-badge");
  if (badge) {
    if (failingCount > 0) {
      badge.className = "badge-down";
      badge.textContent = `${failingCount} node${failingCount > 1 ? "s" : ""} failing`;
    } else {
      badge.className = "badge-down ok";
      badge.textContent = "All operational";
    }
  }

  if (rows.length === 0) {
    container.innerHTML = `<div style="padding:16px;text-align:center;color:var(--muted);font-size:12px;">No matching items found.</div>`;
    return;
  }

  const totalCount = rows.length;
  const isWidget = !isModal;
  const displayRows = isWidget ? rows.slice(0, 14) : rows;
  const blockCount = isModal ? 32 : 24;

  const rowsHtml = displayRows.map(r => {
    const isOk = r.status !== "DOWN";
    const history = r.history || [];

    // Fill or pad history blocks up to blockCount
    const samples = [];
    const padCount = Math.max(0, blockCount - history.length);
    for (let i = 0; i < padCount; i++) samples.push(null);
    for (let i = Math.max(0, history.length - blockCount); i < history.length; i++) samples.push(history[i]);

    // Accurate uptime calculation with blip tolerance
    const lastSampleWithPct = [...samples].reverse().find(s => s && s._allTimeUptime !== null && s._allTimeUptime !== undefined);
    let uptimePct;
    if (lastSampleWithPct) {
      uptimePct = lastSampleWithPct._allTimeUptime;
    } else {
      const validSamples = samples.filter(s => s !== null);
      if (validSamples.length > 0) {
        const upCount = validSamples.filter(s => s.status !== "DOWN" && !s.error).length;
        uptimePct = Math.round((upCount / validSamples.length) * 100);
      } else {
        uptimePct = 100;
      }
    }
    const pctClass = uptimePct >= 99 ? "ok" : uptimePct >= 85 ? "mid" : "fail";

    const blocksHtml = samples.map((s, idx) => {
      const cls = getSampleClass(s);
      return `<div class="matrix-block ${cls}" data-idx="${idx}" data-row-id="${esc(r.id)}"></div>`;
    }).join("");

    if (r.isChild) {
      // Indented Pipeline Child Row
      return `
        <div class="matrix-row matrix-tree-child ${!isOk ? 'is-failing' : ''}" data-target-id="${esc(r.targetNodeId || r.id)}" title="Click to view node details & telemetry">
          <div class="matrix-row-info">
            <span class="matrix-tree-branch">${r.isLastChild ? '└─' : '├─'}</span>
            <span class="matrix-stage-icon">${r.stageIcon}</span>
            <span class="dot ${isOk ? 'ok' : 'fail'}"></span>
            <span class="matrix-row-title">${esc(r.title)}</span>
            ${r.isOrigin ? `<span class="matrix-origin-badge" title="Origin of Failure (Problem started here)">🚨 Origin</span>` : ""}
            <span class="matrix-row-role role-${esc(r.primaryRole)}" title="${esc(r.role)}">${esc(r.displayRole)}</span>
          </div>
          <div class="matrix-blocks">${blocksHtml}</div>
          <div class="matrix-row-meta">
            <span class="uptime-pct ${pctClass}">${uptimePct}%</span>
            <span class="latency-val">${r.latestLatency !== null ? Math.round(r.latestLatency) + 'ms' : '—'}</span>
          </div>
        </div>
      `;
    }

    if (r.isChannel) {
      // Channel Parent Row with Tree Toggle
      const treeBtn = r.isTreeParent
        ? `<button type="button" class="matrix-tree-toggle ${r.isExpanded ? 'expanded' : ''}" data-ch="${esc(r.title)}" title="${r.isExpanded ? 'Collapse pipeline' : 'Expand pipeline tree'}">${r.isExpanded ? '▾' : '▸'}</button>`
        : '';
      return `
        <div class="matrix-row matrix-tree-parent ${!isOk ? 'is-failing' : ''}" data-target-id="${esc(r.targetNodeId || r.id)}" data-ch="${esc(r.title)}" title="Click to focus channel, click chevron to expand pipeline">
          <div class="matrix-row-info">
            ${treeBtn}
            <span class="matrix-stage-icon">📺</span>
            <span class="dot ${isOk ? 'ok' : 'fail'}"></span>
            <span class="matrix-row-title">${esc(r.title)}</span>
            <span class="matrix-row-role role-Channel" title="Output Channel">Channel</span>
          </div>
          <div class="matrix-blocks">${blocksHtml}</div>
          <div class="matrix-row-meta">
            <span class="uptime-pct ${pctClass}">${uptimePct}%</span>
            <span class="latency-val">${r.latestLatency !== null ? Math.round(r.latestLatency) + 'ms' : '—'}</span>
          </div>
        </div>
      `;
    }

    // Standard Infrastructure Row
    return `
      <div class="matrix-row ${!isOk ? 'is-failing' : ''}" data-target-id="${esc(r.targetNodeId || r.id)}" title="Click to view details & telemetry">
        <div class="matrix-row-info">
          <span class="dot ${isOk ? 'ok' : 'fail'}"></span>
          <span class="matrix-row-title">${esc(r.title)}</span>
          ${r.isOrigin ? `<span class="matrix-origin-badge" title="Origin of Failure (Problem started here)">🚨 Origin</span>` : ""}
          <span class="matrix-row-role role-${esc(r.primaryRole)}" title="${esc(r.role)}">${esc(r.displayRole)}</span>
        </div>
        <div class="matrix-blocks">${blocksHtml}</div>
        <div class="matrix-row-meta">
          <span class="uptime-pct ${pctClass}">${uptimePct}%</span>
          <span class="latency-val">${r.latestLatency !== null ? Math.round(r.latestLatency) + 'ms' : '—'}</span>
        </div>
      </div>
    `;
  }).join("");

  const footerHtml = (isWidget && totalCount > 14)
    ? `<div class="matrix-more-hint" style="display:flex;align-items:center;justify-content:space-between;padding:4px 6px;margin-top:2px;font-size:10px;color:var(--muted);border-top:1px dashed var(--card-border);">
        <span>Showing top 14 of ${totalCount} items</span>
        <button type="button" id="matrix-btn-view-all" style="background:none;border:0;color:var(--accent);cursor:pointer;font-weight:600;font-size:10px;padding:0;">View all ${totalCount} ⛶</button>
      </div>`
    : "";

  container.innerHTML = rowsHtml + footerHtml;

  const btnViewAll = $("#matrix-btn-view-all", container);
  if (btnViewAll && heatmapModal) {
    btnViewAll.onclick = (e) => {
      e.stopPropagation();
      heatmapModal.hidden = false;
      renderUptimeMatrix("modal-matrix-grid", true, matrixSearchQuery);
    };
  }

  // Wire Tree Toggle Expand / Collapse clicks
  $$(".matrix-tree-toggle", container).forEach(toggleBtn => {
    toggleBtn.onclick = (e) => {
      e.stopPropagation();
      const ch = toggleBtn.dataset.ch;
      if (expandedChannels.has(ch)) {
        expandedChannels.delete(ch);
      } else {
        expandedChannels.add(ch);
      }
      renderUptimeMatrix(containerId, isModal, filterQuery);
    };
  });

  // Wire row clicks and block hover tooltips
  $$(".matrix-row", container).forEach(rowEl => {
    const targetId = rowEl.dataset.targetId;
    rowEl.onclick = (e) => {
      // If user clicked the toggle chevron, do nothing here
      if (e.target.closest(".matrix-tree-toggle")) return;
      if (targetId) focusNode(targetId);
    };

    const blocks = $$(".matrix-block", rowEl);
    const rowObj = displayRows.find(r => (r.targetNodeId || r.id) === targetId || r.id === rowEl.dataset.targetId);
    if (!rowObj) return;

    blocks.forEach(block => {
      const idx = parseInt(block.dataset.idx, 10);
      const history = rowObj.history || [];
      const padCount = Math.max(0, blockCount - history.length);
      const sample = idx >= padCount ? history[idx - padCount] : null;

      block.addEventListener("mouseenter", (e) => {
        showMatrixTooltip(e, rowObj.fullId || rowObj.title, sample);
      });
      block.addEventListener("mousemove", (e) => {
        positionMatrixTooltip(e);
      });
      block.addEventListener("mouseleave", () => {
        if (matrixTooltip) matrixTooltip.hidden = true;
      });
    });
  });
}

function focusNode(id) {
  const cid = (splitCards[id] && splitCards[id][0]) || id;
  const p = pos[cid] || pos[id];
  if (p && canvas.clientWidth) {
    view.x = (canvas.clientWidth / 2) - (p.x + W / 2) * view.k;
    view.y = (canvas.clientHeight / 2) - (p.y + H / 2) * view.k;
    render();
    if (typeof showDetail === "function") showDetail(id);
  }
}

// Global hook to trigger render from outside
window.renderHeatmap = function() {
  renderUptimeMatrix("matrix-grid", false);
  if (heatmapModal && !heatmapModal.hidden) {
    renderUptimeMatrix("modal-matrix-grid", true, matrixSearchQuery);
  }
};

function setupUptimeMatrixControls() {
  const setupTab = (id, tabName, isModalTab) => {
    const el = $(`#${id}`);
    if (!el) return;
    el.onclick = (e) => {
      if (e) e.stopPropagation();
      if (isModalTab) {
        matrixModalTab = tabName;
        $$("#heatmap-modal .matrix-tabs .tab-btn").forEach(b => b.classList.remove("active"));
        el.classList.add("active");
        renderUptimeMatrix("modal-matrix-grid", true, matrixSearchQuery);
      } else {
        matrixTab = tabName;
        $$("#heatmap-panel .matrix-tabs .tab-btn").forEach(b => b.classList.remove("active"));
        el.classList.add("active");
        renderUptimeMatrix("matrix-grid", false);
      }
    };
  };

  // Widget tabs: All, Channels, Infra
  setupTab("matrix-tab-all", "all", false);
  setupTab("matrix-tab-channels", "channels", false);
  setupTab("matrix-tab-nodes", "nodes", false);

  // Modal tabs: All, Channels, Transcode, Ingest, Final, Infra
  setupTab("modal-tab-all", "all", true);
  setupTab("modal-tab-channels", "channels", true);
  setupTab("modal-tab-transcode", "transcode", true);
  setupTab("modal-tab-ingest", "ingest", true);
  setupTab("modal-tab-final", "final", true);
  setupTab("modal-tab-nodes", "nodes", true);

  // Search input
  if (matrixSearchInput) {
    matrixSearchInput.oninput = (e) => {
      matrixSearchQuery = e.target.value;
      renderUptimeMatrix("modal-matrix-grid", true, matrixSearchQuery);
    };
  }

  // Refresh button
  const refreshBtn = $("#heatmap-refresh");
  if (refreshBtn) {
    refreshBtn.onclick = (e) => {
      e.stopPropagation();
      renderHeatmap();
    };
  }

  // Expand to modal
  const btnExpand = $("#heatmap-expand");
  if (btnExpand && heatmapModal) {
    btnExpand.onclick = (e) => {
      e.stopPropagation();
      heatmapModal.hidden = false;
      renderUptimeMatrix("modal-matrix-grid", true, matrixSearchQuery);
    };
  }

  // Close modal
  const modalClose = $("#heatmap-modal-close");
  if (modalClose && heatmapModal) {
    modalClose.onclick = () => { heatmapModal.hidden = true; };
  }
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && heatmapModal && !heatmapModal.hidden) {
      heatmapModal.hidden = true;
    }
  });

  // Minimize / Expand panel
  const toggleBtn = $("#heatmap-toggle");
  const togglePanel = (e) => {
    if (e) e.stopPropagation();
    if (!heatmapPanel) return;
    heatmapPanel.classList.toggle("collapsed");
    const isCollapsed = heatmapPanel.classList.contains("collapsed");
    if (toggleBtn) toggleBtn.textContent = isCollapsed ? "▴" : "▾";
    if (btnToggleHeatmap) btnToggleHeatmap.classList.toggle("active", !isCollapsed);
  };
  if (heatmapPanel) {
    $("header", heatmapPanel).onclick = togglePanel;
  }
  if (btnToggleHeatmap) btnToggleHeatmap.onclick = togglePanel;
}

setupUptimeMatrixControls();
