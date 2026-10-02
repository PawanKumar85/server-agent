"use strict";

// ---------- Canvas state ----------
const canvas = $("#canvas"), world = $("#world"), svg = $("#edges");
let G = {nodes: [], edges: [], spiders: [], positions: {}, card: {w: 250, h: 86}, roleOrder: ROLES};
let W = 250, H = 86, pos = {}, byId = {}, live = {}, els = {};
let spiderEls = {}, spiderAt = {}, moving = new Set();
let view = {x: 0, y: 0, k: 1}, fitted = false, focus = null;
const PREVIEW = "preview:", PW = 214, PH = 62, PREVIEW_GAP = 110;
// Final domains are drawn as one card per channel (canvas only; Neo4j keeps one FinalLink per domain).
let splitCards = {}, cardInfo = {}, chanIndex = {};
const cardIds = id => splitCards[id] || [id];               // card ids drawn for a Neo4j node
const cardsOf = id => cardIds(id).map(c => els[c]).filter(Boolean);
const anchorId = id => (splitCards[id] || [id])[0];          // where a spider AT that node is drawn

function renderGraph(graph) {
  G = graph; W = G.card.w; H = G.card.h;
  // keep positions of cards the user dragged
  const dragged = pos; pos = {};
  for (const [id, p] of Object.entries(G.positions)) pos[id] = dragged[id] && dragged[id].moved ? dragged[id] : {...p};
  byId = Object.fromEntries(G.nodes.map(n => [n.id, n]));
  buildChanIndex();
  computeActiveFeeds();
  // One Final card per channel, stacked in the Final column, ordered by where each channel comes from.
  splitCards = {}; cardInfo = {};
  const ys = Object.values(G.positions).map(p => p.y);
  const center = ys.length ? (Math.min(...ys) + Math.max(...ys) + H) / 2 : 0;
  const columns = {};
  for (const n of G.nodes) {
    const base = pos[n.id];
    const finals = (n.links || []).filter(l => l.role === "FinalLink");
    if (!base || !n.labels.includes("FinalLink") || !finals.length) continue;
    for (const l of finals) {
      const cid = `final:${n.id}#${l.channel}`;
      (splitCards[n.id] ||= []).push(cid);
      cardInfo[cid] = {domain: n.id, channel: l.channel, url: l.url};
      (columns[Math.round(base.x)] ||= []).push({cid, x: base.x, srcY: channelSourceY(l.channel, base.y)});
    }
    delete pos[n.id];
  }
  for (const list of Object.values(columns)) {
    list.sort((a, b) => a.srcY - b.srcY || a.cid.localeCompare(b.cid));
    const step = H + 22, top = center - (list.length * step - 22) / 2;
    list.forEach((c, i) => { pos[c.cid] = dragged[c.cid] && dragged[c.cid].moved ? dragged[c.cid] : {x: c.x, y: top + i * step}; });
  }
  // A Video Preview Player card to the right of every Final card (UI only, not stored in Neo4j).
  for (const n of G.nodes) {
    if (!n.labels.includes("FinalLink")) continue;
    for (const cid of cardIds(n.id)) {
      const p = pos[cid]; if (!p) continue;
      const id = PREVIEW + cid;
      pos[id] = dragged[id] && dragged[id].moved ? dragged[id] : {x: p.x + W + PREVIEW_GAP, y: p.y + (H - PH) / 2};
    }
  }
  live = Object.fromEntries(G.nodes.map(n => [n.id, n.status]));
  $$(".node, .spider", world).forEach(el => el.remove());
  els = {}; spiderEls = {}; spiderAt = {}; moving = new Set();
  $("#canvas-empty").hidden = G.nodes.length > 0;

  const roots = new Set(G.spiders.filter(s => s.status === "STOPPED").map(s => s.at));
  for (const n of G.nodes) {
    for (const cid of splitCards[n.id] || []) {  // per-channel Final cards
      const info = cardInfo[cid], p = pos[cid], h = (n.urlHealth || {})[info.url] || {};
      const st = h.up === false ? "DOWN" : h.up ? "UP" : n.status;
      const el = document.createElement("div");
      el.className = "node" + (roots.has(n.id) ? " root" : "");
      el.style.cssText = `left:${p.x}px;top:${p.y}px;width:${W}px;height:${H}px`;
      el.dataset.node = n.id; el.dataset.url = info.url;  // for its alert (node-alerts.js)
      el.innerHTML = `
        <div class="port in"></div><div class="port out"></div>
        <div class="icon" style="background:var(--FinalLink)">F</div>
        <div class="body">
          <div class="title"><span class="dot" style="background:${statusColor(st)}"></span>${esc(info.channel)}</div>
          <div class="chips one-line"><span class="chip" style="background:var(--FinalLink)">Final</span>${feedBadge(info.channel)}<span class="sub" title="${esc(n.id)}">${esc(n.id)}</span></div>
          <div class="stats">${st || "not checked"} · ping ${n.pingCount ?? 0}${h.detail ? " · " + esc(h.detail) : ""}</div>
        </div>`;
      el.dataset.stats = el.querySelector(".stats").textContent;
      el.addEventListener("mousedown", e => startDrag(e, cid));
      el.addEventListener("click", () => { if (!el.dataset.dragged) showDetail(n.id); delete el.dataset.dragged; });
      el.addEventListener("mouseenter", () => setHover(n.id));
      el.addEventListener("mouseleave", () => setHover(null));
      world.appendChild(el); els[cid] = el;
    }
    const p = pos[n.id]; if (!p) continue;
    const primary = G.roleOrder.find(r => n.labels.includes(r)) || n.labels[0] || "";
    const el = document.createElement("div");
    el.className = "node" + (roots.has(n.id) ? " root" : "");
    el.style.cssText = `left:${p.x}px;top:${p.y}px;width:${W}px;height:${H}px`;
    el.dataset.node = n.id;
    const urls = (n.urls || []).length, down = Object.values(n.urlHealth || {}).filter(h => h.up === false).length;
    el.innerHTML = `
      <div class="port in"></div><div class="port out"></div>
      <div class="icon" style="background:var(--${primary})">${esc(short(primary)[0] || "?")}</div>
      <div class="body">
        <div class="title"><span class="dot" style="background:${statusColor(n.status)}"></span>${esc(n.id)}</div>
        <div class="chips">${n.labels.map(l => `<span class="chip" style="background:var(--${l}, #888)">${esc(short(l))}</span>`).join("")}</div>
        <div class="stats">${n.status || "not checked"} · ${urls} URL${urls === 1 ? "" : "s"}${down ? ` (${down} down)` : ""} · ping ${n.pingCount ?? 0}${n.latency != null ? ` · ${Math.round(n.latency)} ms` : ""}</div>
      </div>`;
    el.dataset.stats = el.querySelector(".stats").textContent;
    el.addEventListener("mousedown", e => startDrag(e, n.id));
    el.addEventListener("click", () => { if (!el.dataset.dragged) showDetail(n.id); delete el.dataset.dragged; });
    el.addEventListener("mouseenter", () => setHover(n.id));
    el.addEventListener("mouseleave", () => setHover(null));
    world.appendChild(el); els[n.id] = el;
  }
  for (const n of G.nodes) for (const cid of cardIds(n.id)) {
    const id = PREVIEW + cid, p = pos[id]; if (!p) continue;
    const info = cardInfo[cid];
    const el = document.createElement("div");
    el.className = "node preview-node";
    el.style.cssText = `left:${p.x}px;top:${p.y}px;width:${PW}px;height:${PH}px`;
    el.innerHTML = `<div class="port in"></div><div class="play">▶</div>
      <div class="body"><div class="title">${esc(info ? info.channel : n.id)}</div>
      <div class="stats">${info ? feedBadge(info.channel) + " " : ""}Video preview · ${esc(n.id)}</div></div>`;
    el.addEventListener("mousedown", e => startDrag(e, id));
    el.addEventListener("click", () => { if (!el.dataset.dragged) openPlayer(n.id, info && info.url); delete el.dataset.dragged; });
    el.addEventListener("mouseenter", () => setHover(n.id));
    el.addEventListener("mouseleave", () => setHover(null));
    world.appendChild(el); els[id] = el;
  }
  for (const s of G.spiders) {
    const el = document.createElement("span");
    el.className = "spider";
    el.innerHTML = `<b>🕷</b>${esc(spiderName(s.finalLinkId))}`;
    world.appendChild(el); spiderEls[s.id] = el; spiderAt[s.id] = s.at;
    paintSpider(s.id, s.status);
  }
  renderLegend(); renderDbPanel(); drawEdges(); placeSpiders(); renderHeatmap();
  if (search.active) runSearch(search.q); else applyFocus();  // a search re-runs on the new graph
  if (typeof renderNodeAlerts === "function") renderNodeAlerts();
  if (!fitted && canvas.clientWidth) fit();
}

function renderLegend() {
  const line = (cls, text) => `<span><svg width="26" height="8"><path class="edge ${cls}" d="M1,4 L25,4"/></svg>${text}</span>`;
  $("#legend").innerHTML =
    ROLES.map(r => `<span><i style="background:var(--${r})"></i>${short(r)}</span>`).join("") +
    `<span><i style="background:var(--up);border-radius:50%"></i>UP</span><span><i style="background:var(--down);border-radius:50%"></i>DOWN</span>` +
    line("active", "feed from Main") + line("active backup", "feed from Backup") + line("standby", "standby") + line("down", "down / no feed") +
    `<span>🕷 spider</span><span><i style="background:var(--STOPPED)"></i>stopped here</span>`;
}

// Where each channel's feed is actually coming from, from per-URL health: the MainInput if its URL
// is up, otherwise the BackupLink (the spider's failover order). An edge is "active" when it carries
// that live feed for at least one channel; activeFeeds maps "source|TYPE|target" -> [channels].
let activeFeeds = new Map(), urlUp = {}, urlLive = {};  // urlLive: checked and up
let channelFeed = {};  // channel -> "MAIN" | "BACKUP" | "NONE": which input its live feed comes from
const FEED_TEXT = {MAIN: "MAIN", BACKUP: "BACKUP", DEGRADED: "MAIN ⚠", NONE: "NO FEED"};
function buildChanIndex() {  // channel -> role -> [{url, domain}], plus each URL's last health
  chanIndex = {}; urlUp = {}; urlLive = {};
  for (const n of G.nodes) {
    for (const [url, h] of Object.entries(n.urlHealth || {})) { urlUp[url] = h.up !== false; urlLive[url] = h.up === true; }
    for (const l of n.links || []) {
      const ch = (chanIndex[l.channel] ||= {MainInput: [], BackupLink: [], Transcoding: [], FinalLink: []});
      if (ch[l.role]) ch[l.role].push({url: l.url, domain: n.id});
    }
  }
}
function channelSourceY(channel, fallback) {  // y of the node a channel's Final is fed from
  const ch = chanIndex[channel]; if (!ch) return fallback;
  const src = (ch.Transcoding[0] || ch.MainInput[0] || ch.BackupLink[0] || {}).domain;
  return src && pos[src] ? pos[src].y : fallback;
}
function computeActiveFeeds() {
  activeFeeds = new Map();
  const channels = chanIndex;
  const up = link => urlUp[link.url] !== false;  // never checked counts as up
  const add = (source, type, target, channel) => {
    if (source === target) return;  // same host: no edge to colour
    const key = `${source}|${type}|${target}`;
    if (!activeFeeds.has(key)) activeFeeds.set(key, []);
    if (!activeFeeds.get(key).includes(channel)) activeFeeds.get(key).push(channel);
  };
  channelFeed = {};
  for (const [channel, ch] of Object.entries(channels)) {
    const main = ch.MainInput.find(up), input = main || ch.BackupLink.find(up);
    // No healthy input but the Final still answers live: the feed is degraded (e.g. Main falling behind), not gone.
    const finalUp = ch.FinalLink.length && ch.FinalLink.every(f => urlLive[f.url]);  // checked and live
    channelFeed[channel] = input ? (main ? "MAIN" : "BACKUP") : finalUp ? "DEGRADED" : "NONE";
    const consumers = ch.Transcoding.length ? ch.Transcoding : ch.FinalLink;
    if (input) for (const c of consumers) add(input.domain, "FEEDS", c.domain, channel);
    for (const t of ch.Transcoding.filter(up)) for (const f of ch.FinalLink) add(t.domain, "PRODUCES", f.domain, channel);
  }
}
function feedBadge(channel) {
  const feed = channelFeed[channel]; if (!feed) return "";
  const title = feed === "MAIN" ? "Running on its Main input" : feed === "BACKUP" ? "Main is down: running on its Backup"
    : feed === "DEGRADED" ? "Its input is failing checks, but the Final is still on air" : "Main and Backup are both down";
  return `<span class="feed-badge ${feed.toLowerCase()}" title="${title}">${FEED_TEXT[feed]}</span>`;
}
function carriedBy(e) {  // channels whose live feed uses this edge (a channel edge: only its own)
  const all = activeFeeds.get(`${e.source}|${e.type}|${e.domainTarget || e.target}`) || [];
  return e.channel ? all.filter(c => c === e.channel) : all;
}
function edgeState(e) {
  if (live[e.source] === "DOWN") return "down";
  if (live[e.source] == null) return "";  // not checked yet (e.g. mid-walk)
  const carried = carriedBy(e);
  if (!carried.length) return "standby";
  // amber if any channel on this edge has failed over to its backup
  return carried.some(c => channelFeed[c] === "BACKUP") ? "active backup" : "active";
}
// Edges into a split Final domain go to the channel cards that edge actually serves.
function drawnEdges() {
  const out = [];
  for (const e of G.edges) {
    const cards = splitCards[e.target];
    if (!cards) { out.push(e); continue; }
    const serves = cards.filter(cid => {
      const ch = chanIndex[cardInfo[cid].channel]; if (!ch) return false;
      if (e.type === "PRODUCES") return ch.Transcoding.some(t => t.domain === e.source);
      return !ch.Transcoding.length && [...ch.MainInput, ...ch.BackupLink].some(i => i.domain === e.source);
    });
    for (const cid of serves.length ? serves : cards) out.push({...e, target: cid, domainTarget: e.target, channel: cardInfo[cid].channel});
  }
  return out;
}

// ---------- Edges (skip-column edges routed through the gaps between cards) ----------
const GAP = 28;
function lanes(x1, y1, x2, y2, skip) {
  const cols = {};
  for (const [id, p] of Object.entries(pos)) {
    if (skip.includes(id) || id.startsWith(PREVIEW) || p.x <= x1 || p.x + W >= x2) continue;
    (cols[Math.round(p.x)] ||= []).push(p);
  }
  return Object.keys(cols).map(Number).sort((a, b) => a - b).map(cx => {
    let y = y1 + (y2 - y1) * ((cx + W / 2 - x1) / (x2 - x1));
    for (let i = 0; i < 4; i++) {
      const hit = cols[cx].find(p => y > p.y - GAP / 2 && y < p.y + H + GAP / 2);
      if (!hit) break;
      y = (y - hit.y < H / 2) ? hit.y - GAP : hit.y + H + GAP;
    }
    return {x: cx, y};
  });
}
const curve = (x1, y1, x2, y2) => { const dx = Math.max(40, (x2 - x1) / 2); return `C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`; };
function drawEdges() {
  const parts = [];
  for (const e of drawnEdges()) {
    const a = pos[e.source], b = pos[e.target]; if (!a || !b) continue;
    const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2;
    const route = lanes(x1, y1, x2, y2, [e.source, e.target]);
    const [fx, fy] = route.length ? [route[0].x - 10, route[0].y] : [x2, y2];
    const mx = (x1 + fx) / 2, my = (y1 + fy) / 2;
    let d = `M${x1},${y1} `, cx = x1, cy = y1;
    for (const lane of route) { d += curve(cx, cy, lane.x - 10, lane.y) + ` L${lane.x + W + 10},${lane.y} `; cx = lane.x + W + 10; cy = lane.y; }
    d += curve(cx, cy, x2, y2);
    const state = edgeState(e);
    const cls = "edge" + (state ? " " + state : "");
    // Hovering a node wins over a search, which wins over the Database-panel focus: the highlighted chain's
    // links stand out, the rest fade.
    const hl = highlight();
    const on = hl ? hl.edges.has(edgeKey(e.source, e.type, e.domainTarget || e.target))
      : focus && focus.kind === "type" && focus.value === e.type;
    const dim = (hl || focus) && !on ? " dim" : "";
    const active = state.startsWith("active");
    const carried = active ? carriedBy(e) : [];
    const label = carried.length ? `${e.type} · ${carried.length > 2 ? carried.length + " channels" : carried.join(", ")}` : e.type;
    const lw = label.length * 5.8 + 12;
    const tip = carried.length ? `Feed in use: ${carried.map(c => `${c} (from ${FEED_TEXT[channelFeed[c]]})`).join(", ")}`
      : state === "standby" ? "Standby: not carrying a live feed right now" : state === "down" ? "Source is DOWN" : "";
    parts.push(`<path class="${cls}${on ? " hl" : ""}${dim}" d="${d}" data-edge="${esc(e.source)}|${esc(e.domainTarget || e.target)}"><title>${esc(tip)}</title></path>`,
      `<rect class="edge-label-bg${dim}" x="${mx - lw / 2}" y="${my - 8}" width="${lw}" height="14" rx="4"/>`,
      `<text class="edge-label${active ? " " + state : ""}${dim}" x="${mx}" y="${my + 3}" text-anchor="middle">${esc(label)}</text>`);
  }
  for (const n of G.nodes) for (const cid of cardIds(n.id)) {  // Final card -> its preview player
    const a = pos[cid], b = pos[PREVIEW + cid]; if (!a || !b) continue;
    const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + PH / 2, mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
    const hlp = highlight();
    const dim = hlp ? (hlp.nodes.has(n.id) ? "" : " dim")
      : focus && !(focus.kind === "label" && focus.value === "FinalLink") ? " dim" : "";
    const url = cardInfo[cid] && cardInfo[cid].url;
    const down = live[n.id] === "DOWN" || (url && live[n.id] != null && urlUp[url] === false);
    const feed = cardInfo[cid] ? channelFeed[cardInfo[cid].channel] : null;
    const pstate = down || feed === "NONE" ? " down" : live[n.id] === "UP" ? (feed === "BACKUP" ? " active backup" : " active") : "";
    parts.push(`<path class="edge preview${pstate}${dim}" d="M${x1},${y1} ${curve(x1, y1, x2, y2)}"/>`,
      `<rect class="edge-label-bg" x="${mx - 30}" y="${my - 8}" width="60" height="14" rx="4"/>`,
      `<text class="edge-label" x="${mx}" y="${my + 3}" text-anchor="middle">PREVIEW</text>`);
  }
  svg.innerHTML = parts.join("");
}

// ---------- Pan / zoom / drag ----------
const applyView = () => { world.style.transform = `translate(${view.x}px,${view.y}px) scale(${view.k})`; };
function fit(only) {  // only: card ids to frame (default: everything)
  const ps = only ? only.map(id => pos[id]).filter(Boolean) : Object.values(pos);
  if (!ps.length || !canvas.clientWidth) return;
  const minX = Math.min(...ps.map(p => p.x)), maxX = Math.max(...ps.map(p => p.x + W));
  const minY = Math.min(...ps.map(p => p.y)) - 20, maxY = Math.max(...ps.map(p => p.y + H));
  const panel = $("#db-panel");
  const reserve = panel.classList.contains("collapsed") ? 0 : panel.offsetWidth + 12;
  const top = $("#canvas-top").offsetHeight + 12;
  const cw = canvas.clientWidth - reserve, ch = canvas.clientHeight - top, pad = 50;
  view.k = Math.min(1.2, (cw - pad * 2) / Math.max(maxX - minX, W), (ch - pad * 2) / Math.max(maxY - minY, H));
  view.x = (cw - (maxX - minX) * view.k) / 2 - minX * view.k;
  view.y = top + (ch - (maxY - minY) * view.k) / 2 - minY * view.k;
  applyView(); fitted = true;
}
function zoomAt(factor, cx, cy) {
  const k = Math.min(2.5, Math.max(0.25, view.k * factor));
  view.x = cx - (cx - view.x) * (k / view.k); view.y = cy - (cy - view.y) * (k / view.k); view.k = k; applyView();
}
canvas.addEventListener("wheel", e => {
  if (e.target.closest("#heatmap-panel, #heatmap-modal, #excel-modal, #player-modal, #detail, #log")) return;
  e.preventDefault();
  const r = canvas.getBoundingClientRect();
  zoomAt(e.deltaY < 0 ? 1.1 : 1 / 1.1, e.clientX - r.left, e.clientY - r.top);
}, {passive: false});
let pan = null, drag = null;
canvas.addEventListener("mousedown", e => {
  if (e.target.closest(".node, #detail, #toolbar, #canvas-top, #canvas-search-box, #db-panel, #log, #heatmap-panel")) return;
  pan = {x: e.clientX - view.x, y: e.clientY - view.y}; canvas.classList.add("panning");
});
function startDrag(e, id) { e.stopPropagation(); drag = {id, sx: e.clientX, sy: e.clientY, x: pos[id].x, y: pos[id].y, moved: false}; }
window.addEventListener("mousemove", e => {
  if (pan) { view.x = e.clientX - pan.x; view.y = e.clientY - pan.y; applyView(); }
  if (drag) {
    const dx = (e.clientX - drag.sx) / view.k, dy = (e.clientY - drag.sy) / view.k;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
    pos[drag.id] = {x: drag.x + dx, y: drag.y + dy, moved: true};
    els[drag.id].style.left = pos[drag.id].x + "px"; els[drag.id].style.top = pos[drag.id].y + "px";
    drawEdges(); placeSpiders();
  }
});
window.addEventListener("mouseup", () => {
  if (drag && drag.moved) els[drag.id].dataset.dragged = "1";
  pan = null; drag = null; canvas.classList.remove("panning");
});
$("#fit").onclick = fit;
$("#zin").onclick = () => zoomAt(1.2, canvas.clientWidth / 2, canvas.clientHeight / 2);
$("#zout").onclick = () => zoomAt(1 / 1.2, canvas.clientWidth / 2, canvas.clientHeight / 2);
window.addEventListener("resize", () => { if ($("#view-workflow").classList.contains("active")) fit(); });

// ---------- Spiders on the canvas ----------
function paintSpider(id, status) {
  const el = spiderEls[id]; if (!el) return;
  el.style.color = el.style.borderColor = `var(--${status}, #666)`;
  el.title = `${id} · ${status}`;
}
function anchor(id, nodeId) {
  const p = pos[anchorId(nodeId)]; if (!p) return {x: 0, y: 0};
  const here = Object.keys(spiderAt).filter(k => spiderAt[k] === nodeId && !moving.has(k)).sort();
  let x = p.x + W - 10;
  for (const k of here) { x -= spiderEls[k].offsetWidth + 4; if (k === id) break; }
  return {x, y: p.y - 13};
}
function placeSpiders() {
  for (const id of Object.keys(spiderEls)) {
    if (moving.has(id)) continue;
    if (!spiderAt[id] || !pos[anchorId(spiderAt[id])]) { spiderEls[id].style.display = "none"; continue; }
    const a = anchor(id, spiderAt[id]);
    Object.assign(spiderEls[id].style, {display: "", left: a.x + "px", top: a.y + "px"});
  }
}
// A spider always walks along the connection lines, never straight across the canvas: going home
// it retraces its path (Backup → Transcoding → Final), and between two inputs of one transcoder it
// goes through that transcoder. routeBetween gives the shortest such path of node ids.
function routeBetween(a, b) {
  if (!a || !b || a === b) return [b];
  const adj = {};
  for (const e of G.edges) { (adj[e.source] ||= []).push(e.target); (adj[e.target] ||= []).push(e.source); }
  const prev = {[a]: null}, queue = [a];
  while (queue.length) {
    const u = queue.shift();
    if (u === b) break;
    for (const v of adj[u] || []) if (!(v in prev)) { prev[v] = u; queue.push(v); }
  }
  if (!(b in prev)) return [a, b];  // not connected: fall back to a direct hop
  const route = [];
  for (let u = b; u !== null; u = prev[u]) route.unshift(u);
  return route;
}
const hopsBetween = (a, b) => Math.max(1, routeBetween(a, b).length - 1);
function linePoints(u, v, half) {  // points along the drawn line between two nodes, in walking order
  const path = svg.querySelector(`path[data-edge="${CSS.escape(u + "|" + v)}"]`) ||
               svg.querySelector(`path[data-edge="${CSS.escape(v + "|" + u)}"]`);
  if (!path) return [];
  const len = path.getTotalLength(), reverse = path.dataset.edge.startsWith(v + "|");
  const pts = [];
  for (let i = 0; i <= 24; i++) {
    const pt = path.getPointAtLength(len * (reverse ? 1 - i / 24 : i / 24));
    pts.push([pt.x - half, pt.y - 10]);
  }
  return pts;
}
function moveSpider(id, to, hopMs) {
  const from = spiderAt[id], el = spiderEls[id];
  if (!el) return;
  if (!from || !pos[anchorId(from)] || from === to) { spiderAt[id] = to; placeSpiders(); return; }
  const start = anchor(id, from);
  moving.add(id); el.classList.add("walking");
  const pts = [[start.x, start.y]];
  const half = el.offsetWidth / 2;
  const route = routeBetween(from, to);
  for (let i = 0; i < route.length - 1; i++) pts.push(...linePoints(route[i], route[i + 1], half));
  const duration = hopMs * Math.max(1, route.length - 1);
  spiderAt[id] = to; moving.delete(id);
  const end = anchor(id, to); moving.add(id);
  pts.push([end.x, end.y]);
  const seg = pts.slice(1).map((p, i) => Math.hypot(p[0] - pts[i][0], p[1] - pts[i][1]));
  const total = seg.reduce((a, b) => a + b, 0) || 1, t0 = performance.now();
  (function frame(now) {
    let d = Math.min(1, (now - t0) / duration);
    d = d < .5 ? 2 * d * d : 1 - Math.pow(-2 * d + 2, 2) / 2;
    let dist = d * total, i = 0;
    while (i < seg.length - 1 && dist > seg[i]) { dist -= seg[i]; i++; }
    const f = seg[i] ? dist / seg[i] : 1;
    el.style.left = pts[i][0] + (pts[i + 1][0] - pts[i][0]) * f + "px";
    el.style.top = pts[i][1] + (pts[i + 1][1] - pts[i][1]) * f + "px";
    if (d < 1) requestAnimationFrame(frame);
    else { moving.delete(id); el.classList.remove("walking"); placeSpiders(); }
  })(t0);
}

// ---------- Database panel: labels and relationship types ----------
const dbPanel = $("#db-panel");
function renderDbPanel() {
  const labels = ROLES.map(l => [l, G.nodes.filter(n => n.labels.includes(l)).length]).filter(([, c]) => c);
  if (G.spiders.length) labels.push(["SpiderRun", G.spiders.length]);
  const types = {};
  for (const e of G.edges) types[e.type] = (types[e.type] || 0) + 1;
  const at = G.spiders.filter(s => s.at).length;
  if (at) types.AT = at;
  $("#label-chips").innerHTML = labels.map(([l, c]) =>
    `<button class="chip-btn label-chip" data-kind="label" data-value="${l}" style="background:var(--${l}, #64748b)">${l}<span class="count">${c}</span></button>`).join("");
  $("#type-chips").innerHTML = Object.entries(types).map(([t, c]) =>
    `<button class="chip-btn type-chip" data-kind="type" data-value="${t}">${t}<span class="count">${c}</span></button>`).join("");
}
function applyFocus() {
  if (!hover && search.active) { paintHighlight(search); return; }  // a search outranks the panel focus
  $$(".node.search-match", world).forEach(el => el.classList.remove("search-match"));
  $$(".chip-btn", dbPanel).forEach(b => b.classList.toggle("on", !!focus && b.dataset.kind === focus.kind && b.dataset.value === focus.value));
  const spiderFocus = focus && (focus.value === "SpiderRun" || focus.value === "AT");
  const withSpider = new Set(Object.values(spiderAt));
  for (const n of G.nodes) {
    let match = !focus;
    if (focus?.kind === "label") match = focus.value === "SpiderRun" ? withSpider.has(n.id) : n.labels.includes(focus.value);
    if (focus?.kind === "type") match = focus.value === "AT" ? withSpider.has(n.id)
      : G.edges.some(e => e.type === focus.value && (e.source === n.id || e.target === n.id));
    cardsOf(n.id).forEach(el => el.classList.toggle("dim", !match));
    cardIds(n.id).forEach(c => els[PREVIEW + c]?.classList.toggle("dim", !!focus && !(focus.kind === "label" && focus.value === "FinalLink")));
  }
  Object.values(spiderEls).forEach(el => el.classList.toggle("dim", !!focus && !spiderFocus));
  drawEdges();
}
dbPanel.addEventListener("click", e => {
  const b = e.target.closest(".chip-btn"); if (!b) return;
  const same = focus && focus.kind === b.dataset.kind && focus.value === b.dataset.value;
  focus = same ? null : {kind: b.dataset.kind, value: b.dataset.value};
  applyFocus();
});
const toggleDbPanel = () => {
  dbPanel.classList.toggle("collapsed");
  $(".toggle", dbPanel).textContent = dbPanel.classList.contains("collapsed") ? "▸" : "▾";
  fit();
};
$(".toggle", dbPanel).onclick = toggleDbPanel;
$("header", dbPanel).onclick = toggleDbPanel;
canvas.addEventListener("dblclick", e => { if (!e.target.closest(".node, #db-panel, #detail, #heatmap-panel")) { focus = null; applyFocus(); } });

// ---------- Hover: highlight a node's own links (no pop-up) ----------
let hover = null, hoverNodes = new Set(), hoverEdges = new Set();
const edgeKey = (source, type, target) => `${source}|${type}|${target}`;
const nodeChannels = id => new Set((byId[id]?.links || []).map(l => l.channel));

// The hovered node's complete chain: upstream all the way to its Main/Backup inputs and
// downstream all the way to its Finals, following only the channels this node carries
// (a shared transcoder is fed by different inputs for different channels).
function chainOf(id) {
  const channels = nodeChannels(id);
  const serves = e => {
    if (!channels.size) return true;  // no channel data: follow every link
    const a = nodeChannels(e.source), b = nodeChannels(e.target);
    if (!a.size || !b.size) return true;
    return [...channels].some(c => a.has(c) && b.has(c));
  };
  const nodes = new Set([id]), edges = new Set();
  for (const upstream of [true, false]) {
    let frontier = [id];
    const seen = new Set([id]);
    while (frontier.length) {
      const next = [];
      for (const e of G.edges) {
        const [from, to] = upstream ? [e.target, e.source] : [e.source, e.target];
        if (!frontier.includes(from) || !serves(e)) continue;
        edges.add(edgeKey(e.source, e.type, e.target));
        nodes.add(to);
        if (!seen.has(to)) { seen.add(to); next.push(to); }
      }
      frontier = next;
    }
  }
  return {nodes, edges};
}

function setHover(id) {
  if (drag || pan || hover === id) return;
  hover = id;
  if (!id) { hoverNodes = new Set(); hoverEdges = new Set(); applyFocus(); return; }
  ({nodes: hoverNodes, edges: hoverEdges} = chainOf(id));
  paintHighlight({nodes: hoverNodes, edges: hoverEdges, matches: search.active ? search.matches : new Set()});
}

// The chain being highlighted right now: the hovered node's, else the search's, else none.
function highlight() {
  if (hover) return {nodes: hoverNodes, edges: hoverEdges};
  return search.active ? search : null;
}

function paintHighlight(h) {
  for (const n of G.nodes) {
    cardsOf(n.id).forEach(el => {
      el.classList.toggle("dim", !h.nodes.has(n.id));
      el.classList.toggle("search-match", !!h.matches && h.matches.has(n.id));
    });
    // a Final in the chain keeps its preview player lit
    cardIds(n.id).forEach(c => els[PREVIEW + c]?.classList.toggle("dim", !(h.nodes.has(n.id) && n.labels.includes("FinalLink"))));
  }
  for (const [sid, el] of Object.entries(spiderEls)) el.classList.toggle("dim", !h.nodes.has(spiderAt[sid]));
  drawEdges();
}

// ---------- Search: find servers by name, channel, URL, role or IP, and light up everything they connect to ----------
const search = {q: "", active: false, matches: new Set(), nodes: new Set(), edges: new Set()};
const SEARCH_DEBOUNCE_MS = 220;
const ROLE_WORDS = {MainInput: "main input", BackupLink: "backup link", Transcoding: "transcoding transcoder xcode", FinalLink: "final output"};

function searchText(n) {
  return [n.id, n.serverIp, ...(n.labels || []).map(l => `${l} ${ROLE_WORDS[l] || ""}`),
          ...(n.links || []).flatMap(l => [l.channel, l.url])].filter(Boolean).join(" ").toLowerCase();
}

function runSearch(q) {
  search.q = q.trim().toLowerCase();
  const words = search.q.split(/\s+/).filter(Boolean);
  search.active = words.length > 0;
  search.matches = new Set(); search.nodes = new Set(); search.edges = new Set();
  if (search.active) {
    // A query that names a channel (or one of its URLs) follows just that channel's pipeline, so a shared
    // server doesn't light up every other channel it carries; otherwise a match brings its whole chain.
    const linkHit = l => words.every(w => `${l.channel} ${l.url}`.toLowerCase().includes(w));
    const channels = new Set(G.nodes.flatMap(n => (n.links || []).filter(linkHit).map(l => l.channel)));
    for (const n of G.nodes) {
      const text = searchText(n);
      if (words.every(w => text.includes(w))) search.matches.add(n.id);
    }
    if (channels.size) {
      for (const n of G.nodes) if ((n.links || []).some(l => channels.has(l.channel))) search.nodes.add(n.id);
      // A link belongs to the channel when it fits that channel's own pipeline: Main/Backup into its
      // Transcoding (or Final), Transcoding into its Final. Two shared servers can also be linked for another channel.
      const rolesIn = (id, c) => new Set((byId[id]?.links || []).filter(l => l.channel === c).map(l => l.role));
      const fits = (e, c) => {
        const from = rolesIn(e.source, c), to = rolesIn(e.target, c);
        const input = from.has("MainInput") || from.has("BackupLink");
        return (input && (to.has("Transcoding") || to.has("FinalLink"))) || (from.has("Transcoding") && to.has("FinalLink"));
      };
      for (const e of G.edges) {
        if ([...channels].some(c => fits(e, c))) search.edges.add(edgeKey(e.source, e.type, e.target));
      }
      search.matches.forEach(id => search.nodes.add(id));
    } else {
      for (const id of search.matches) {  // each match with its whole chain, upstream and downstream
        const c = chainOf(id);
        c.nodes.forEach(x => search.nodes.add(x)); c.edges.forEach(x => search.edges.add(x));
      }
    }
  }
  const info = $("#canvas-search-info"), clear = $("#canvas-search-clear");
  clear.hidden = !search.active;
  info.hidden = !search.active;
  info.textContent = !search.active ? "" : search.matches.size
    ? `${search.matches.size} match${search.matches.size === 1 ? "" : "es"} · ${search.nodes.size - search.matches.size} connected`
    : "No matches";
  info.classList.toggle("none", search.active && !search.matches.size);
  applyFocus();
}

let searchTimer = null;
$("#canvas-search").addEventListener("input", e => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => runSearch(e.target.value), SEARCH_DEBOUNCE_MS);  // debounced: one pass per pause
});
$("#canvas-search").addEventListener("keydown", e => {
  if (e.key === "Enter") {  // run now and frame the matches
    clearTimeout(searchTimer); runSearch(e.target.value);
    const ids = [...search.matches].flatMap(id => cardIds(id).length ? cardIds(id) : [id]);
    if (ids.length) fit(ids);
  } else if (e.key === "Escape") { e.target.value = ""; runSearch(""); e.target.blur(); }
});
$("#canvas-search-clear").addEventListener("click", () => { $("#canvas-search").value = ""; runSearch(""); fit(); });
document.addEventListener("keydown", e => {  // "/" or Ctrl/Cmd+F on the canvas focuses the search
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName);
  if (!$("#view-workflow")?.classList.contains("active") || typing) return;
  if (e.key === "/" || ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "f")) { e.preventDefault(); $("#canvas-search").focus(); }
});
