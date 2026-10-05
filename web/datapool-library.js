"use strict";

// ---------- Data Pool Library (UI/UX) ----------
// A curated library-like catalog of all in-memory telemetry, indexed channels, and stream states.

let poolData = { stats: {}, summary: {}, nodes: [], channels: [] };
let poolActiveShelf = "all";
let poolSearchQuery = "";
let poolSelectedEntry = null;

async function loadDataPool(force = false) {
  const container = $("#datapool-shelves-content");
  if (!container) return;

  const refreshBtn = $("#datapool-refresh-btn");
  if (refreshBtn) refreshBtn.classList.add("running");

  try {
    const url = force ? "/api/pool/library?force=true" : "/api/pool/library";
    const res = await api("GET", url);
    if (res && res.data) {
      poolData = res.data;
      renderDataPoolStats();
      renderDataPoolLibrary();
    }
  } catch (err) {
    console.error("Failed to load Data Pool Library", err);
    if (typeof toast === "function") toast("bad", "Data Pool Error", "Failed to fetch in-memory telemetry pool catalog.");
  } finally {
    if (refreshBtn) refreshBtn.classList.remove("running");
  }
}

function renderDataPoolStats() {
  const s = poolData.stats || {};
  const sum = poolData.summary || {};
  const isHi = typeof getLanguage === "function" && getLanguage() === "hi";

  // 1. Catalog Volume
  const elVol = $("#datapool-stat-volume");
  if (elVol) {
    elVol.textContent = `${sum.node_count || 0} Servers · ${sum.channel_count || 0} Channels`;
  }
  const elLinks = $("#datapool-stat-links");
  if (elLinks) {
    elLinks.textContent = `${sum.link_count || 0} ${isHi ? "Live Streams Cataloged" : "Live Stream Records"}`;
  }

  // 2. Cache Hit Ratio
  const elRatio = $("#datapool-stat-ratio");
  if (elRatio) {
    elRatio.textContent = `${s.hit_ratio_pct || 0}%`;
  }
  const elHits = $("#datapool-stat-hits");
  if (elHits) {
    elHits.textContent = `${(s.hits || 0).toLocaleString()} hits / ${(s.misses || 0).toLocaleString()} misses`;
  }

  // 3. Cache Freshness
  const elFresh = $("#datapool-stat-freshness");
  if (elFresh) {
    const fresh = sum.freshness || {};
    elFresh.textContent = `${fresh.FRESH || 0} Fresh · ${fresh.WARM || 0} Warm · ${fresh.STALE || 0} Stale`;
  }
  const elAge = $("#datapool-stat-age");
  if (elAge) {
    const age = s.cached_age_s != null ? `${s.cached_age_s}s` : "live";
    elAge.textContent = isHi ? `Cache aayu: ${age} (TTL: ${s.ttl_s || 5}s)` : `Cache age: ${age} (TTL: ${s.ttl_s || 5}s)`;
  }

  // 4. Memory Speed
  const elSpeed = $("#datapool-stat-speed");
  if (elSpeed) {
    elSpeed.textContent = "< 0.1 ms";
  }
  const elEngine = $("#datapool-stat-engine");
  if (elEngine) {
    elEngine.textContent = isHi ? "Thread-Safe RAM Storage" : "Thread-Safe In-Memory Cache";
  }
}

function renderDataPoolLibrary() {
  const container = $("#datapool-shelves-content");
  if (!container) return;

  const isHi = typeof getLanguage === "function" && getLanguage() === "hi";
  const q = poolSearchQuery.trim().toLowerCase();

  // Filter nodes
  const nodes = (poolData.nodes || []).filter(n => {
    if (poolActiveShelf !== "all" && poolActiveShelf !== "nodes" && poolActiveShelf !== "freshness") return false;
    if (!q) return true;
    const matchDomain = String(n.domain || "").toLowerCase().includes(q);
    const matchIp = String(n.server_ip || "").toLowerCase().includes(q);
    const matchFresh = String(n.streamFreshness || "").toLowerCase().includes(q);
    const matchStatus = String(n.status || "").toLowerCase().includes(q);
    const matchLinks = (n.links || []).some(l => String(l.channel || "").toLowerCase().includes(q) || String(l.role || "").toLowerCase().includes(q));
    return matchDomain || matchIp || matchFresh || matchStatus || matchLinks;
  });

  // Filter channels
  const channels = (poolData.channels || []).filter(c => {
    if (poolActiveShelf !== "all" && poolActiveShelf !== "channels") return false;
    if (!q) return true;
    const matchName = String(c.channel || "").toLowerCase().includes(q);
    const matchRoles = Object.keys(c.roles || {}).some(r => r.toLowerCase().includes(q));
    const matchUrls = Object.values(c.roles || {}).flat().some(l => String(l.node || "").toLowerCase().includes(q) || String(l.url || "").toLowerCase().includes(q));
    return matchName || matchRoles || matchUrls;
  });

  if (poolActiveShelf === "raw") {
    container.innerHTML = `
      <div class="library-shelf-box">
        <div class="library-shelf-header">
          <div class="shelf-title-wrap">
            <span class="shelf-icon">⚙️</span>
            <div>
              <h3 class="shelf-title">Raw Telemetry Pool Dump (JSON)</h3>
              <p class="shelf-desc">${isHi ? "Live In-Memory cache ka raw snapshot" : "Live raw dump of all cached node and channel structures"}</p>
            </div>
          </div>
          <button class="btn small" onclick="copyDataPoolRaw()">${isHi ? "📋 Copy JSON Dump" : "📋 Copy Raw JSON"}</button>
        </div>
        <pre class="library-raw-dump"><code>${esc(JSON.stringify(poolData, null, 2))}</code></pre>
      </div>
    `;
    return;
  }

  let html = "";

  // 1. Server Nodes Collection Shelf
  if (poolActiveShelf === "all" || poolActiveShelf === "nodes" || poolActiveShelf === "freshness") {
    html += `
      <div class="library-shelf-box">
        <div class="library-shelf-header">
          <div class="shelf-title-wrap">
            <span class="shelf-icon">🖥</span>
            <div>
              <h3 class="shelf-title">${isHi ? "Server Nodes Collection" : "Server Nodes Collection"}</h3>
              <p class="shelf-desc">${nodes.length} ${isHi ? "servers in-memory cataloged" : "active servers indexed in data pool"}</p>
            </div>
          </div>
          <span class="shelf-tag">${nodes.length} Books</span>
        </div>
        <div class="library-books-grid">
          ${nodes.length ? nodes.map(n => renderNodeBookCard(n)).join("") : `<p class="library-empty-notice">${isHi ? "Is shelf par koi server nahi mila." : "No server nodes matched your query on this shelf."}</p>`}
        </div>
      </div>
    `;
  }

  // 2. Broadcast Channels Shelf
  if (poolActiveShelf === "all" || poolActiveShelf === "channels") {
    html += `
      <div class="library-shelf-box">
        <div class="library-shelf-header">
          <div class="shelf-title-wrap">
            <span class="shelf-icon">📺</span>
            <div>
              <h3 class="shelf-title">${isHi ? "Broadcast Channels Shelf" : "Broadcast Channels Archives"}</h3>
              <p class="shelf-desc">${channels.length} ${isHi ? "inverted stream channels indexed" : "inverted broadcast channels cataloged with stream roles"}</p>
            </div>
          </div>
          <span class="shelf-tag">${channels.length} Volumes</span>
        </div>
        <div class="library-books-grid">
          ${channels.length ? channels.map(c => renderChannelBookCard(c)).join("") : `<p class="library-empty-notice">${isHi ? "Is shelf par koi channel nahi mila." : "No channel archives matched your query."}</p>`}
        </div>
      </div>
    `;
  }

  container.innerHTML = html;

  // Bind click inspection
  $$(".library-book-card", container).forEach(card => {
    card.addEventListener("click", () => {
      const type = card.dataset.entryType;
      const id = card.dataset.entryId;
      openDataPoolReader(type, id);
    });
  });
}

function renderNodeBookCard(n) {
  const isHi = typeof getLanguage === "function" && getLanguage() === "hi";
  const tr = typeof trHinglish === "function" ? trHinglish : (x => x);

  const status = n.status || "UNKNOWN";
  const spineClass = status === "UP" ? "spine-good" : status === "DOWN" ? "spine-bad" : "spine-warn";
  const freshness = n.streamFreshness || "UNKNOWN";
  const freshBadge = freshness === "FRESH" ? "pill-optimal" : freshness === "WARM" ? "pill-degraded" : "pill-bad";

  const labels = (n.labels || []).filter(l => l !== "Domain").map(l => `<span class="library-chip role-chip">${esc(l)}</span>`).join("");
  const linksCount = (n.links || []).length;
  const latency = n.lastLatencyMs != null ? `${Math.round(n.lastLatencyMs)} ms` : "—";
  const seq = n.mediaSequence != null ? `#${n.mediaSequence}` : "—";
  const segAge = n.lastSegmentAgeS != null ? `${Math.round(n.lastSegmentAgeS)}s` : "—";

  return `
    <div class="library-book-card ${spineClass}" data-entry-type="node" data-entry-id="${esc(n.domain)}">
      <div class="book-spine"></div>
      <div class="book-content">
        <div class="book-header">
          <div class="book-call-number">
            <span class="call-badge">SRV</span>
            <span class="book-status-pill status-${status.toLowerCase()}">${esc(status)}</span>
          </div>
          <span class="library-freshness-pill ${freshBadge}">${esc(freshness)}</span>
        </div>
        <h4 class="book-title" title="${esc(n.domain)}">${esc(n.domain)}</h4>
        <div class="book-chips">${labels}</div>
        <div class="book-biblio-grid">
          <div class="biblio-row"><span class="biblio-label">IP Address</span><span class="biblio-val">${esc(n.server_ip || "—")}</span></div>
          <div class="biblio-row"><span class="biblio-label">Ping Latency</span><span class="biblio-val font-mono">${latency}</span></div>
          <div class="biblio-row"><span class="biblio-label">Media Seq</span><span class="biblio-val font-mono">${seq}</span></div>
          <div class="biblio-row"><span class="biblio-label">Segment Age</span><span class="biblio-val font-mono">${segAge}</span></div>
        </div>
        <div class="book-footer">
          <span class="book-links-stat">🔗 ${linksCount} ${isHi ? "streams" : "links"}</span>
          <button type="button" class="btn-inspect-book">📖 ${isHi ? "Inspect Record" : "Read Entry"}</button>
        </div>
      </div>
    </div>
  `;
}

function renderChannelBookCard(c) {
  const isHi = typeof getLanguage === "function" && getLanguage() === "hi";
  const roles = Object.keys(c.roles || {});
  const roleChips = roles.map(r => `<span class="library-chip channel-role-chip">${esc(r)}</span>`).join("");
  const totalStreams = c.total_streams || 0;

  // Check if all active links are UP
  let hasDown = false;
  let hasWarm = false;
  for (const list of Object.values(c.roles || {})) {
    for (const lnk of list) {
      if (lnk.up === false) hasDown = true;
      if (lnk.freshness === "WARM" || lnk.freshness === "STALE") hasWarm = true;
    }
  }

  const spineClass = hasDown ? "spine-bad" : hasWarm ? "spine-warn" : "spine-good";

  return `
    <div class="library-book-card ${spineClass}" data-entry-type="channel" data-entry-id="${esc(c.channel)}">
      <div class="book-spine"></div>
      <div class="book-content">
        <div class="book-header">
          <div class="book-call-number">
            <span class="call-badge">CHAN</span>
            <span class="book-status-pill ${hasDown ? 'status-down' : 'status-up'}">${hasDown ? 'DEGRADED' : 'HEALTHY'}</span>
          </div>
          <span class="library-chip">${totalStreams} Streams</span>
        </div>
        <h4 class="book-title" title="${esc(c.channel)}">${esc(c.channel)}</h4>
        <div class="book-chips">${roleChips}</div>
        <div class="book-biblio-grid">
          <div class="biblio-row"><span class="biblio-label">Pipeline Roles</span><span class="biblio-val">${roles.join(", ")}</span></div>
          <div class="biblio-row"><span class="biblio-label">Main Stream</span><span class="biblio-val">${c.roles?.MainInput?.[0]?.node || "—"}</span></div>
          <div class="biblio-row"><span class="biblio-label">Final Stream</span><span class="biblio-val">${c.roles?.FinalLink?.[0]?.node || "—"}</span></div>
        </div>
        <div class="book-footer">
          <span class="book-links-stat">📚 ${roles.length} Roles Cataloged</span>
          <button type="button" class="btn-inspect-book">📖 ${isHi ? "Inspect Pipeline" : "Read Archive"}</button>
        </div>
      </div>
    </div>
  `;
}

function formatPingTime(val) {
  if (!val) return "—";
  if (typeof val === "object") {
    if (val.formatted) return val.formatted;
    if (val.year && val.month && val.day) {
      const pad = n => String(n).padStart(2, "0");
      return `${val.year}-${pad(val.month)}-${pad(val.day)} ${pad(val.hour || 0)}:${pad(val.minute || 0)}:${pad(val.second || 0)} IST`;
    }
    try {
      return JSON.stringify(val);
    } catch {
      return String(val);
    }
  }
  return typeof fmtTime === "function" ? fmtTime(val) : String(val);
}

function openDataPoolReader(type, id) {
  const modal = $("#datapool-modal");
  if (!modal) return;

  const isHi = typeof getLanguage === "function" && getLanguage() === "hi";

  if (type === "node") {
    const node = (poolData.nodes || []).find(n => n.domain === id);
    if (!node) return;
    poolSelectedEntry = node;

    const f = node.streamFreshness || "UNKNOWN";
    const fClass = f === "FRESH" ? "pill-optimal" : f === "WARM" ? "pill-degraded" : "pill-bad";

    $("#datapool-modal-title").textContent = `🖥 Library Entry: ${node.domain}`;
    $("#datapool-modal-badge").textContent = `Server Record · Status: ${node.status}`;
    $("#datapool-modal-badge").className = `badge ${node.status === "UP" ? "status-up" : "status-down"}`;

    const linksRows = (node.links || []).map(l => {
      const h = (node.urlHealth && node.urlHealth[l.url]) || {};
      const up = h.up !== false;
      return `
        <tr>
          <td><b>${esc(l.channel || "—")}</b></td>
          <td><span class="library-chip role-chip">${esc(l.role || "Stream")}</span></td>
          <td><span class="dot" style="background:${up ? 'var(--up)' : 'var(--down)'}"></span> ${up ? 'UP' : 'DOWN'}</td>
          <td><code>${esc(h.latency_ms != null ? h.latency_ms + ' ms' : '—')}</code></td>
          <td><code>${esc(h.freshness || '—')}</code></td>
          <td style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(l.url)}"><a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.url)}</a></td>
        </tr>
      `;
    }).join("");

    $("#datapool-modal-body").innerHTML = `
      <div class="reader-tabs">
        <button class="reader-tab active" data-rtab="overview">${isHi ? "Bibliographic Overview" : "Overview & Properties"}</button>
        <button class="reader-tab" data-rtab="links">${isHi ? "Stream Links Table" : "Cataloged Streams"} (${(node.links || []).length})</button>
        <button class="reader-tab" data-rtab="json">${isHi ? "Raw JSON Record" : "Raw JSON Record"}</button>
      </div>

      <div class="reader-panel active" id="rtab-overview">
        <div class="reader-props-grid">
          <div class="rprop"><span class="rprop-k">${isHi ? "Domain / Key" : "Domain / Key"}</span><span class="rprop-v"><code>${esc(node.domain)}</code></span></div>
          <div class="rprop"><span class="rprop-k">${isHi ? "Server IP" : "Server IP"}</span><span class="rprop-v"><code>${esc(node.server_ip || "—")}</code></span></div>
          <div class="rprop"><span class="rprop-k">${isHi ? "Labels" : "Labels"}</span><span class="rprop-v">${(node.labels || []).join(", ")}</span></div>
          <div class="rprop"><span class="rprop-k">${isHi ? "Health Status" : "Health Status"}</span><span class="rprop-v"><b>${esc(node.status)}</b></span></div>
          <div class="rprop"><span class="rprop-k">${isHi ? "Aakhri Ping Check" : "Last Ping Check"}</span><span class="rprop-v"><code>${esc(formatPingTime(node.lastPing))}</code></span></div>
          <div class="rprop"><span class="rprop-k">${isHi ? "ICMP Latency" : "ICMP Latency"}</span><span class="rprop-v"><code>${node.lastLatencyMs != null ? node.lastLatencyMs + ' ms' : '—'}</code></span></div>
          <div class="rprop"><span class="rprop-k">${isHi ? "Stream Taazgi" : "Stream Freshness"}</span><span class="rprop-v"><span class="library-freshness-pill ${fClass}">${esc(f)}</span></span></div>
          <div class="rprop"><span class="rprop-k">${isHi ? "Media Sequence" : "Media Sequence"}</span><span class="rprop-v"><code>${esc(node.mediaSequence ?? "—")}</code></span></div>
          <div class="rprop"><span class="rprop-k">${isHi ? "Segment Age" : "Segment Age"}</span><span class="rprop-v"><code>${node.lastSegmentAgeS != null ? node.lastSegmentAgeS + ' s' : '—'}</code></span></div>
          <div class="rprop"><span class="rprop-k">${isHi ? "Target Duration" : "Target Duration"}</span><span class="rprop-v"><code>${node.targetDurationS != null ? node.targetDurationS + ' s' : '—'}</code></span></div>
          <div class="rprop"><span class="rprop-k">${isHi ? "Consecutive Fails" : "Consecutive Fails"}</span><span class="rprop-v">${node.consecutiveFailures || 0}</span></div>
          <div class="rprop"><span class="rprop-k">${isHi ? "Total Pings" : "Total Pings"}</span><span class="rprop-v">${node.pingCount || 0} (${node.failedCount || 0} failed)</span></div>
        </div>
      </div>

      <div class="reader-panel" id="rtab-links" hidden>
        <table class="reader-table">
          <thead>
            <tr><th>Channel</th><th>Role</th><th>Status</th><th>Latency</th><th>Freshness</th><th>Media URL</th></tr>
          </thead>
          <tbody>${linksRows || `<tr><td colspan="6" style="text-align:center;padding:16px;">No stream links cataloged on this server.</td></tr>`}</tbody>
        </table>
      </div>

      <div class="reader-panel" id="rtab-json" hidden>
        <div style="display:flex;justify-content:flex-end;margin-bottom:8px;">
          <button class="btn small" onclick="copyModalEntryJson()">📋 Copy JSON</button>
        </div>
        <pre class="library-raw-dump"><code>${esc(JSON.stringify(node, null, 2))}</code></pre>
      </div>
    `;
  } else if (type === "channel") {
    const ch = (poolData.channels || []).find(c => c.channel === id);
    if (!ch) return;
    poolSelectedEntry = ch;

    $("#datapool-modal-title").textContent = `📺 Channel Archive: ${ch.channel}`;
    $("#datapool-modal-badge").textContent = `Broadcast Channel · ${ch.total_streams} Streams`;
    $("#datapool-modal-badge").className = "badge status-up";

    let rows = "";
    for (const [role, list] of Object.entries(ch.roles || {})) {
      for (const item of list) {
        const up = item.up !== false;
        rows += `
          <tr>
            <td><span class="library-chip role-chip">${esc(role)}</span></td>
            <td><b>${esc(item.node || "—")}</b></td>
            <td><span class="dot" style="background:${up ? 'var(--up)' : 'var(--down)'}"></span> ${up ? 'UP' : 'DOWN'}</td>
            <td><code>${item.lastLatencyMs != null ? item.lastLatencyMs + ' ms' : '—'}</code></td>
            <td><code>${esc(item.freshness || '—')}</code></td>
            <td style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(item.url)}"><a href="${esc(item.url)}" target="_blank" rel="noopener">${esc(item.url)}</a></td>
          </tr>
        `;
      }
    }

    $("#datapool-modal-body").innerHTML = `
      <div class="reader-tabs">
        <button class="reader-tab active" data-rtab="overview">${isHi ? "Pipeline Routing" : "Pipeline Routing"}</button>
        <button class="reader-tab" data-rtab="json">${isHi ? "Raw JSON Record" : "Raw JSON Record"}</button>
      </div>

      <div class="reader-panel active" id="rtab-overview">
        <table class="reader-table">
          <thead>
            <tr><th>Pipeline Role</th><th>Server Node</th><th>Status</th><th>Latency</th><th>Freshness</th><th>Stream URL</th></tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>

      <div class="reader-panel" id="rtab-json" hidden>
        <div style="display:flex;justify-content:flex-end;margin-bottom:8px;">
          <button class="btn small" onclick="copyModalEntryJson()">📋 Copy JSON</button>
        </div>
        <pre class="library-raw-dump"><code>${esc(JSON.stringify(ch, null, 2))}</code></pre>
      </div>
    `;
  }

  // Bind tab switching inside reader modal
  $$(".reader-tab", modal).forEach(btn => {
    btn.onclick = () => {
      $$(".reader-tab", modal).forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      const target = btn.dataset.rtab;
      $$(".reader-panel", modal).forEach(p => p.hidden = p.id !== `rtab-${target}`);
    };
  });

  modal.hidden = false;
}

function closeDataPoolReader() {
  const modal = $("#datapool-modal");
  if (modal) modal.hidden = true;
}

function copyModalEntryJson() {
  if (!poolSelectedEntry) return;
  navigator.clipboard.writeText(JSON.stringify(poolSelectedEntry, null, 2)).then(() => {
    if (typeof toast === "function") toast("ok", "Copied", "Record JSON copied to clipboard.");
  });
}

function copyDataPoolRaw() {
  navigator.clipboard.writeText(JSON.stringify(poolData, null, 2)).then(() => {
    if (typeof toast === "function") toast("ok", "Copied", "Full Data Pool JSON dump copied to clipboard.");
  });
}

// Bind top controls & search
document.addEventListener("DOMContentLoaded", () => {
  // 1. Search Bar
  const searchInput = $("#datapool-search-input");
  if (searchInput) {
    searchInput.addEventListener("input", e => {
      poolSearchQuery = e.target.value;
      renderDataPoolLibrary();
    });
  }

  // 2. Shelf Tabs
  $$(".shelf-tab-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      $$(".shelf-tab-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      poolActiveShelf = btn.dataset.shelf;
      renderDataPoolLibrary();
    });
  });

  // 3. Refresh Button
  const refreshBtn = $("#datapool-refresh-btn");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", () => {
      loadDataPool(true);
      if (typeof toast === "function") toast("ok", "Restocked", "Telemetry Data Pool re-synchronized from graph.");
    });
  }

  // 4. Modal Close
  const closeBtn = $("#datapool-modal-close");
  if (closeBtn) closeBtn.addEventListener("click", closeDataPoolReader);
  const modal = $("#datapool-modal");
  if (modal) {
    modal.addEventListener("click", e => {
      if (e.target === modal) closeDataPoolReader();
    });
  }

  // 5. ESC Key to close modal
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") {
      const m = $("#datapool-modal");
      if (m && !m.hidden) closeDataPoolReader();
    }
  });
});
