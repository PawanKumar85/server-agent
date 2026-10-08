"use strict";

// ---------- Direct links: channels whose Main server feeds the Final straight, one to one (no transcoder) ----------
// e.g. demo: 94.136.185.222 -> 51.178.16.116. Shown apart from the map, with each end's IP and live state.

const DIRECT_KEY = "directLinksCollapsed";

function directPairs() {
  const chans = {};  // channel -> role -> [{node, url}]
  for (const n of G.nodes || []) {
    for (const l of n.links || []) {
      if (!l.channel) continue;
      ((chans[l.channel] ||= {})[l.role] ||= []).push({node: n, url: l.url});
    }
  }
  const pairs = [];
  for (const [channel, roles] of Object.entries(chans)) {
    const mains = roles.MainInput || [], finals = roles.FinalLink || [];
    if ((roles.Transcoding || []).length || mains.length !== 1 || finals.length !== 1) continue;
    const backup = (roles.BackupLink || [])[0] || null;
    const state = e => {
      const h = e && (e.node.urlHealth || {})[e.url];
      return !e ? null : !h ? "unknown" : h.ignored ? "ignored" : isFailing(h) ? "down" : h.up ? "up" : "unknown";
    };
    const main = mains[0], final = finals[0];
    const s = {main: state(main), backup: state(backup), final: state(final)};
    const overall = s.final === "down" ? {tone: "down", text: "Off air"}
      : s.main === "down" && s.backup === "up" ? {tone: "warn", text: "On backup"}
      : s.main === "down" ? {tone: "down", text: "Main down"}
      : s.main === "up" && s.final === "up" ? {tone: "up", text: "OK"}
      : {tone: "idle", text: "Checking"};
    pairs.push({channel, main, backup, final, s, overall});
  }
  const rank = {down: 0, warn: 1, idle: 2, up: 3};
  return pairs.sort((a, b) => rank[a.overall.tone] - rank[b.overall.tone] || a.channel.localeCompare(b.channel));
}

function directEnd(e, st, role) {
  const name = alertShort(e.node.id).split("/")[0];
  return `<span class="dl-end dl-${st}" title="${esc(role)}: ${esc(e.url)}">
    <span class="dl-ip">${esc(e.node.serverIp || "IP unknown")}</span>
    <span class="dl-host">${esc(name)}${role === "Backup" ? " · backup" : ""}</span></span>`;
}

function renderDirectLinks() {
  const panel = document.getElementById("direct-panel");
  if (!panel || typeof G === "undefined") return;
  const pairs = directPairs();
  const bad = pairs.filter(p => p.overall.tone !== "up").length;
  panel.querySelector(".dl-count").textContent = `${pairs.length}${bad ? ` · ${bad} need attention` : ""}`;
  panel.querySelector(".dl-count").classList.toggle("bad", bad > 0);
  panel.querySelector(".dl-body").innerHTML = pairs.length ? `<ul class="dl-list">${pairs.map(p => `
    <li class="dl-row" data-final="${esc(p.final.node.id)}" tabindex="0" title="Open ${esc(p.channel)}'s final server">
      <span class="dl-chan">${esc(p.channel)}</span>
      <span class="dl-path">${directEnd(p.main, p.s.main, "Main")}<span class="dl-arrow" aria-hidden="true">→</span>${directEnd(p.final, p.s.final, "Final")}</span>
      <span class="dl-state dl-tone-${p.overall.tone}">${p.overall.text}</span>
      ${p.backup ? `<span class="dl-backup">Backup ${directEnd(p.backup, p.s.backup, "Backup")}</span>` : ""}
    </li>`).join("")}</ul>` : `<p class="dl-empty">No channel has its Main feeding its Final directly.</p>`;
}

(function initDirectLinks() {
  const panel = document.getElementById("direct-panel");
  if (!panel) return;
  try { if (localStorage.getItem(DIRECT_KEY) === "1") panel.classList.add("collapsed"); } catch (_) { /* optional */ }
  panel.querySelector("header").addEventListener("click", () => {
    panel.classList.toggle("collapsed");
    try { localStorage.setItem(DIRECT_KEY, panel.classList.contains("collapsed") ? "1" : "0"); } catch (_) { /* optional */ }
  });
  const open = row => {
    const id = row && row.dataset.final;
    if (!id || !byId[id]) return;
    if (typeof fit === "function" && typeof cardIds === "function") fit(cardIds(id));
    if (typeof showDetail === "function") showDetail(id);
  };
  panel.addEventListener("click", e => open(e.target.closest(".dl-row")));
  panel.addEventListener("keydown", e => { if (e.key === "Enter") open(e.target.closest(".dl-row")); });
  setInterval(renderDirectLinks, 5000);  // follows the live health between graph refreshes
})();
