"use strict";

// ---------- Spiders view ----------
async function loadSpiders() {
  const {data} = await api("GET", "/api/spiders");
  $("#count-running").textContent = data.counts.RUNNING;
  $("#count-stopped").textContent = data.counts.STOPPED;
  $("#count-error").textContent = data.counts.ERROR;
  $("#spider-cards").innerHTML = data.spiders.map(({spider: s, node, atCount, rca}) => {
    const n = node || {};
    const steps = (rca.path || []).map(step => {
      const here = s.status === "STOPPED" && step.node_id === s.currentNodeId;
      return `<li class="${step.up ? "" : "down"}${here ? " stop" : ""}">${esc(step.node_id)} ${step.up ? "✓" : "✗ DOWN"}
        ${here ? `<span class="here">SPIDER STOPPED HERE</span>` : ""}
        <div class="role">${esc(step.role)}${step.visited ? "" : " · checked from downstream"}</div>
        ${!step.up && step.error ? `<div class="err">${esc(step.error)}</div>` : ""}</li>`;
    }).join("");
    return `<article class="spider-card">
      <h4>🕷 ${esc(spiderName(s.finalLinkId))} <span class="status-tag" style="background:var(--${s.status})">${esc(s.status)}</span>
        <button class="btn small" style="margin-left:auto" data-test="${esc(s.finalLinkId)}" ${running ? "disabled" : ""}
          title="Ping only this channel now">▶ Test ping</button></h4>
      <div class="sub" style="margin:-6px 0 8px">${esc(s.id)}</div>
      <ol class="path">${steps || `<li class="sub">Not run yet</li>`}</ol>
      ${atCount !== 1 ? `<div class="msg err">This spider has ${atCount} AT relationships (expected exactly 1).</div>` : ""}
      <div class="metrics">
        <div class="metric"><b>Current location</b><span>${esc(n.domain || "—")}</span></div>
        <div class="metric"><b>Ping count</b><span>${n.pingCount ?? 0}</span></div>
        <div class="metric"><b>Failed count</b><span>${n.failedCount ?? 0}</span></div>
        <div class="metric"><b>Last latency</b><span>${n.lastLatencyMs != null ? Math.round(n.lastLatencyMs) + " ms" : "—"}</span></div>
      </div>
      <div class="kv"><b>Last ping</b> ${esc(fmtTime(n.lastPing))} · <b>Node</b> ${esc(n.status || "—")} ·
        <b>RTT</b> ${n.lastRttMs ?? "—"} ms · <b>Jitter</b> ${n.lastJitterMs ?? "—"} ms · <b>Loss</b> ${n.lastPacketLoss ?? "—"} · <b>Steps</b> ${s.stepCount ?? 0} · <b>Embedding</b> ${s.embeddingDims ? `${s.embeddingDims}d ✓` : "✓"}</div>
      ${s.stopReason ? `<div class="kv"><b>Reason</b> ${esc(s.stopReason)}</div>` : ""}
      ${rca.root_cause ? `<div class="kv"><b>Root cause</b> ${esc(rca.root_cause)}</div>` : ""}
      <div class="kv"><b>Impact</b> ${esc(rca.impact)}</div>
      ${(rca.failover || []).map(f => `<div class="failover">${esc(f)}</div>`).join("")}
    </article>`;
  }).join("") || `<p class="lead">No spiders yet — press <b>Run spiders</b> to create one per FinalLink.</p>`;
}

// ---------- Links view ----------
let linkRows = [];
const blankLink = () => ({channel: "", label: "MainInput", url: ""});
function renderLinkRows(invalid = new Set()) {
  $("#links-table tbody").innerHTML = linkRows.map((r, i) => `
    <tr data-i="${i}" class="${invalid.has(i) ? "invalid" : ""}">
      <td><input data-f="channel" value="${esc(r.channel)}" placeholder="e.g. gtcnews"></td>
      <td><select data-f="label">${ROLES.map(l => `<option ${l === r.label ? "selected" : ""} value="${l}">${l}</option>`).join("")}</select></td>
      <td><input data-f="url" value="${esc(r.url)}" placeholder="https://…/index.m3u8"></td>
      <td><button class="icon-btn" data-del="${i}" title="Remove">×</button></td>
    </tr>`).join("");
}
$("#links-table").addEventListener("input", e => {
  const tr = e.target.closest("tr[data-i]"); if (!tr) return;
  linkRows[+tr.dataset.i][e.target.dataset.f] = e.target.value;
});
$("#links-table").addEventListener("click", e => {
  const del = e.target.closest("[data-del]"); if (!del) return;
  linkRows.splice(+del.dataset.del, 1); if (!linkRows.length) linkRows.push(blankLink()); renderLinkRows();
});
$("#links-add").onclick = () => { linkRows.push(blankLink()); renderLinkRows(); };
async function submitLinks(dryRun) {
  const msg = $("#links-msg"); msg.innerHTML = ""; $("#links-preview-out").innerHTML = "";
  const {status, data} = await api("POST", "/api/links", {links: linkRows, dry_run: dryRun});
  if (status === 422) {
    renderLinkRows(new Set(data.errors.map(e => e.row)));
    msg.innerHTML = `<div class="msg err">Fix these first:<ul>${data.errors.map(e => `<li>Row ${e.row + 1} · ${esc(e.field)}: ${esc(e.message)}</li>`).join("")}</ul></div>`;
    return;
  }
  renderLinkRows();
  if (!data.nodes.length) { msg.innerHTML = `<div class="msg warn">Add at least one link.</div>`; return; }
  $("#links-preview-out").innerHTML = `<h3>${dryRun ? "Preview" : "Saved"} · ${data.nodes.length} domain node(s)</h3>
    <table class="grid"><thead><tr><th>Domain</th><th>Roles</th><th>Channels</th><th>URLs</th><th>Server IP</th></tr></thead><tbody>
    ${data.nodes.map(n => `<tr><td>${esc(n.domain)}</td><td>${n.labels.map(l => `<span class="chip" style="background:var(--${l})">${short(l)}</span>`).join("")}</td>
      <td>${esc(n.channels.join(", "))}</td><td class="urls">${n.url.map(esc).join("<br>")}</td><td>${esc(n.server_ip || "unresolved")}</td></tr>`).join("")}
    </tbody></table>`;
  if (!dryRun) {
    const wired = data.connected || [];
    msg.innerHTML = `<div class="msg ok">Saved ${data.nodes.length} domain node(s).${wired.length
      ? ` Connected into the channel's pipeline:<ul>${wired.map(e => `<li>${esc(e.source)} ${e.type} ${esc(e.target)}</li>`).join("")}</ul>`
      : " Check their connections in Relationships."}</div>`;
    linkRows = [blankLink()]; renderLinkRows();
    topo.loaded = false;
    await refreshAll();
  }
}
$("#links-preview").onclick = () => submitLinks(true).catch(err => toast("error", "Preview failed", err.message));
$("#links-save").onclick = () => submitLinks(false).catch(err => toast("error", "Save failed", err.message));
function renderNodesTable() {
  $("#nodes-table tbody").innerHTML = G.nodes.map(n => `<tr>
    <td>${esc(n.id)}</td>
    <td>${n.labels.map(l => `<span class="chip" style="background:var(--${l}, #888)">${short(l)}</span>`).join("")}</td>
    <td><span class="dot" style="background:${statusColor(n.status)}"></span> ${esc(n.status || "—")}</td>
    <td>${esc([...new Set((n.links || []).map(l => l.channel))].sort().join(", "))}</td>
    <td class="urls">${(n.urls || []).map(esc).join("<br>")}</td>
    <td>${n.pingCount ?? 0}</td></tr>`).join("") || `<tr><td colspan="6" class="sub">No nodes yet.</td></tr>`;
}

// ---------- Relationships view ----------
const topo = {loaded: false, rows: [], domains: []};
async function loadTopology() {
  const {data} = await api("GET", "/api/topology");
  topo.rows = data.edges; topo.domains = data.domains; topo.loaded = true;
  renderTopology();
}
function renderTopology(rejected = []) {
  const bad = new Set(rejected.map(r => `${r.source}|${r.type}|${r.target}`));
  const opts = sel => topo.domains.map(d => `<option ${d === sel ? "selected" : ""}>${esc(d)}</option>`).join("");
  $("#rels-table tbody").innerHTML = topo.rows.map((r, i) => `
    <tr data-i="${i}" class="${bad.has(`${r.source}|${r.type}|${r.target}`) ? "invalid" : ""}">
      <td><select data-f="source">${opts(r.source)}</select></td>
      <td><select data-f="type">${["FEEDS", "PRODUCES"].map(t => `<option ${t === r.type ? "selected" : ""}>${t}</option>`).join("")}</select></td>
      <td><select data-f="target">${opts(r.target)}</select></td>
      <td><button class="icon-btn" data-del="${i}" title="Remove">×</button></td>
    </tr>`).join("") || `<tr><td colspan="4" class="sub">No relationships.</td></tr>`;
}
$("#rels-table").addEventListener("change", e => {
  const tr = e.target.closest("tr[data-i]"); if (!tr) return;
  topo.rows[+tr.dataset.i][e.target.dataset.f] = e.target.value;
});
$("#rels-table").addEventListener("click", e => {
  const del = e.target.closest("[data-del]"); if (!del) return;
  topo.rows.splice(+del.dataset.del, 1); renderTopology();
});
$("#rels-add").onclick = () => {
  topo.rows.push({source: topo.domains[0] || "", type: "FEEDS", target: topo.domains[1] || topo.domains[0] || ""});
  renderTopology();
};
$("#rels-reload").onclick = () => { $("#rels-msg").innerHTML = ""; loadTopology(); };
$("#rels-save").onclick = async () => {
  const msg = $("#rels-msg");
  try {
    const {data} = await api("PUT", "/api/topology", {edges: topo.rows});
    msg.innerHTML = `<div class="msg ${data.rejected.length ? "warn" : "ok"}">Saved ${data.saved} relationship(s).` +
      (data.rejected.length ? `<ul>${data.rejected.map(r => `<li>Skipped ${esc(r.source)} -[:${r.type}]-> ${esc(r.target)}: ${esc(r.reason)}</li>`).join("")}</ul>` : "") + `</div>`;
    renderTopology(data.rejected);
    await refreshAll();
  } catch (err) { msg.innerHTML = `<div class="msg err">${esc(err.message)}</div>`; }
};

// ---------- Excel Import ----------
let currentExcelFile = null;
let currentExcelData = null;

function setupExcelImport() {
  const fileInput = $("#file-import-excel");
  const btnTop = $("#btn-import-excel");
  const btnLinks = $("#links-import-excel");
  const modal = $("#excel-modal");
  const btnClose = $("#excel-modal-close");
  const btnCancel = $("#excel-btn-cancel");
  const btnSave = $("#excel-btn-save");
  const btnTable = $("#excel-btn-table");

  if (!fileInput) return;

  const triggerPicker = () => { fileInput.value = ""; fileInput.click(); };
  if (btnTop) btnTop.onclick = triggerPicker;
  if (btnLinks) btnLinks.onclick = triggerPicker;

  const closeModal = () => { modal.hidden = true; };
  if (btnClose) btnClose.onclick = closeModal;
  if (btnCancel) btnCancel.onclick = closeModal;

  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && !modal.hidden) closeModal();
  });

  fileInput.onchange = async () => {
    const file = fileInput.files[0];
    if (!file) return;
    currentExcelFile = file;
    $("#excel-modal-filename").textContent = `${file.name} (${(file.size / 1024).toFixed(1)} KB)`;
    $("#excel-preview-stats").innerHTML = `<div class="sub">Analyzing workbook…</div>`;
    $("#excel-preview-details").innerHTML = "";
    btnSave.disabled = true;
    btnTable.disabled = true;
    modal.hidden = false;

    try {
      const res = await fetch("/api/import/excel?dry_run=true", {
        method: "POST",
        headers: {"Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
        body: file,
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || data.message || "Failed to parse Excel file.");
      }
      currentExcelData = data;
      btnSave.disabled = false;
      btnTable.disabled = false;

      // Render stats cards
      $("#excel-preview-stats").innerHTML = `
        <div class="excel-stats-grid">
          <div class="excel-stat-card"><div class="val">${data.channels.length}</div><div class="lbl">Channels</div></div>
          <div class="excel-stat-card"><div class="val">${data.link_count}</div><div class="lbl">Stream Links</div></div>
          <div class="excel-stat-card"><div class="val">${data.node_count}</div><div class="lbl">Domain Nodes</div></div>
          <div class="excel-stat-card"><div class="val">${data.relationships.length}</div><div class="lbl">Relationships</div></div>
        </div>
      `;

      // Render sample table
      const sampleLinks = data.links.slice(0, 10);
      $("#excel-preview-details").innerHTML = `
        <div style="font-size:11px;color:var(--muted);margin-bottom:6px">
          Sheets found: <b>${esc(data.sheets_found.join(", "))}</b> · Showing ${sampleLinks.length} of ${data.link_count} link(s)
        </div>
        <table class="grid" style="font-size:11px">
          <thead><tr><th>Channel</th><th>Role</th><th>URL</th></tr></thead>
          <tbody>
            ${sampleLinks.map(l => `<tr><td><b>${esc(l.channel)}</b></td><td><span class="chip" style="background:var(--${l.label}, #64748b)">${short(l.label)}</span></td><td class="urls">${esc(l.url)}</td></tr>`).join("")}
          </tbody>
        </table>
        ${data.errors && data.errors.length ? `<div class="msg warn" style="margin-top:8px"><b>Warnings (${data.errors.length}):</b><ul>${data.errors.slice(0, 5).map(e => `<li>${esc(e)}</li>`).join("")}</ul></div>` : ""}
      `;
    } catch (err) {
      $("#excel-preview-stats").innerHTML = `<div class="msg err">${esc(err.message)}</div>`;
      btnSave.disabled = true;
      btnTable.disabled = true;
    }
  };

  btnSave.onclick = async () => {
    if (!currentExcelFile) return;
    const autoConn = $("#excel-auto-connect").checked;
    btnSave.disabled = true;
    btnSave.textContent = "Saving…";
    try {
      const res = await fetch(`/api/import/excel?dry_run=false&auto_connect=${autoConn}`, {
        method: "POST",
        headers: {"Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
        body: currentExcelFile,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || data.message || "Failed to save Excel contents.");

      closeModal();
      toast("ok", "Excel Import Complete", `Saved ${data.channels.length} channels (${data.link_count} links) and ${data.relationships_saved} relationship(s).`);
      topo.loaded = false;
      await refreshAll();
    } catch (err) {
      toast("error", "Import failed", err.message);
    } finally {
      btnSave.disabled = false;
      btnSave.textContent = "Save to Database";
    }
  };

  btnTable.onclick = () => {
    if (!currentExcelData || !currentExcelData.links) return;
    closeModal();
    showView("links");
    linkRows = currentExcelData.links.map(l => ({channel: l.channel, label: l.label, url: l.url}));
    renderLinkRows();
    toast("ok", "Links Loaded", `Loaded ${linkRows.length} links into editor. Review and click "Save links" when ready.`);
  };
}

// ---------- Tools view ----------
async function loadTools() {
  const container = $("#tools-grid-dynamic");
  if (!container) return;
  try {
    const {data} = await api("GET", "/api/tools");
    if (!data || !data.tools) return;
    container.innerHTML = data.tools.map(t => {
      const isDanger = t.category.includes("Danger");
      const isSafe = t.category.includes("Safe");
      const tagStyle = isDanger
        ? "color:var(--down);border-color:color-mix(in srgb, var(--down) 40%, transparent)"
        : isSafe
        ? "color:#f59e0b;border-color:color-mix(in srgb, #f59e0b 40%, transparent)"
        : "";
      const cardStyle = isDanger ? "border-color:color-mix(in srgb, var(--down) 40%, var(--card-border))" : "";
      const btnClass = isDanger ? "btn danger small btn-chat-tool" : "btn primary small btn-chat-tool";

      let customAction = "";
      if (t.name === "generate_report") {
        customAction = `<a class="btn primary small" href="/api/report.html" target="_blank" rel="noopener">📄 Open Full Report</a>`;
      } else if (t.name === "generate_excel") {
        customAction = `<a class="btn primary small" href="/api/export.xlsx" download>⬇ Download .xlsx</a>`;
      } else if (t.name === "trigger_channel_crawl") {
        customAction = `<button class="btn primary small btn-crawl-all">▶ Run All Spiders</button>`;
      }

      return `
        <div class="tool-card" style="${cardStyle}">
          <div>
            <div class="tool-header">
              <div class="tool-title-wrap">
                <span class="tool-icon">${t.icon || "🛠"}</span>
                <div>
                  <h3 class="tool-title">${esc(t.name.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase()))}</h3>
                  <div style="font-size:11px;color:var(--muted)"><code>${esc(t.name)}</code></div>
                </div>
              </div>
              <span class="tool-tag" style="${tagStyle}">${esc(t.category)}</span>
            </div>
            <p class="tool-desc">${esc(t.description)}</p>
          </div>
          <div class="tool-actions">
            ${customAction}
            <button class="${btnClass}" data-prompt="${esc(t.prompt)}">${t.icon || "💬"} Run via ChatBot</button>
          </div>
        </div>
      `;
    }).join("");

    $$(".btn-chat-tool", container).forEach(b => {
      b.onclick = () => {
        const prompt = b.dataset.prompt;
        showView("agent");
        if (prompt) {
          const chatInput = $("#chat-input");
          if (chatInput) {
            chatInput.value = prompt;
            if (typeof sendChat === "function") sendChat(prompt);
          }
        }
      };
    });

    $$(".btn-crawl-all", container).forEach(b => {
      b.onclick = () => {
        showView("workflow");
        if (typeof startRun === "function") startRun();
      };
    });
  } catch (err) {
    console.error("Failed to load tools dynamically", err);
  }
}

// ---------- Skills view ----------
async function loadSkills() {
  const container = $("#skills-grid-dynamic");
  if (!container) return;
  try {
    const {data} = await api("GET", "/api/skills");
    if (!data || !data.skills) return;
    container.innerHTML = data.skills.map(s => {
      const topics = (s.key_topics || []).map(t => `<span class="skill-topic-chip">${esc(t)}</span>`).join("");
      return `
        <div class="skill-card">
          <div>
            <div class="skill-card-header">
              <div class="skill-title-wrap">
                <span class="skill-icon">${s.icon || "🧠"}</span>
                <div>
                  <h3 class="skill-title">${esc(s.title)}</h3>
                  <div style="font-size:11px;color:var(--muted)"><code>${esc(s.id)}</code></div>
                </div>
              </div>
              <span class="skill-badge">${esc(s.badge || s.category)}</span>
            </div>
            <p class="skill-desc">${esc(s.description)}</p>
            <div class="skill-topics">${topics}</div>
          </div>
          <div class="skill-actions">
            <button class="btn primary small btn-skill-chat" data-prompt="${esc(s.prompt)}">
              🤖 Ask Agent with this Skill
            </button>
            ${s.url ? `<a class="btn small" href="${esc(s.url)}" target="_blank" rel="noopener">Open reference ↗</a>` : ""}
          </div>
        </div>
      `;
    }).join("");

    $$(".btn-skill-chat", container).forEach(b => {
      b.onclick = () => {
        const prompt = b.dataset.prompt;
        showView("agent");
        if (prompt) {
          const chatInput = $("#chat-input");
          if (chatInput) {
            chatInput.value = prompt;
            if (typeof sendChat === "function") sendChat(prompt);
          }
        }
      };
    });
  } catch (err) {
    console.error("Failed to load skills dynamically", err);
  }
}
