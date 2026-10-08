"use strict";

// ---------- Helpers ----------
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const ROLES = ["MainInput", "BackupLink", "Transcoding", "FinalLink"];
const short = r => ({MainInput: "Main", BackupLink: "Backup", Transcoding: "Transcoding", FinalLink: "Final"}[r] || r);
// A spider watches one channel's FinalLink ("gtc.ottlive.co.in/gtcnews") -> show "gtcnews".
const spiderName = finalId => String(finalId).includes("/") ? String(finalId).split("/").pop() : String(finalId).split(".")[0];
const statusColor = s => s === "UP" ? css("--up") : s === "DOWN" ? css("--down") : css("--unknown");
// Timestamps are stored in UTC; the UI shows them in IST (Asia/Kolkata) as "YYYY-MM-DD HH:MM:SS IST".
const IST = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Kolkata", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
});
function fmtTime(iso) {
  if (!iso) return "—";
  // Neo4j sends nanoseconds ("…05.259000000+00:00"); trim to milliseconds so every browser can parse it.
  const date = new Date(String(iso).replace(/(\.\d{3})\d+/, "$1"));
  if (isNaN(date)) return String(iso).replace("T", " ").slice(0, 19);
  const p = Object.fromEntries(IST.formatToParts(date).map(x => [x.type, x.value]));
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second} IST`;
}
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

// ---------- Theme Management (Light / Dark / System) ----------
const THEMES = ["light", "dark", "system"];

function getEffectiveTheme(theme) {
  if (theme === "system" || !THEMES.includes(theme)) {
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  return theme;
}

function applyTheme(theme) {
  if (!THEMES.includes(theme)) theme = "system";
  store.set("stream_graph_theme", theme);

  if (theme === "system") {
    document.documentElement.removeAttribute("data-theme");
  } else {
    document.documentElement.setAttribute("data-theme", theme);
  }

  // Sync topbar theme segmented control
  $$(".theme-seg-btn").forEach(btn => {
    const isMatch = btn.dataset.themeVal === theme;
    btn.classList.toggle("active", isMatch);
    btn.setAttribute("aria-checked", isMatch ? "true" : "false");
  });
}

function initTheme() {
  const current = store.get("stream_graph_theme") || "system";
  applyTheme(current);

  // Delegated click handler for theme switcher
  document.addEventListener("click", e => {
    const btn = e.target.closest(".theme-seg-btn");
    if (btn && btn.dataset.themeVal) {
      applyTheme(btn.dataset.themeVal);
    }
  });

  // Listen to OS system color-scheme changes if in system mode
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      const mode = store.get("stream_graph_theme") || "system";
      if (mode === "system") {
        applyTheme("system");
      }
    });
  }
}

// Initialize theme on script load
initTheme();

// Every route needs a signed-in session; when it's missing or expired, go to the login page.
function goLogin() { location.href = "/login?next=" + encodeURIComponent(location.pathname); }
const _fetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const res = await _fetch(...args);
  if (res.status === 401) { goLogin(); throw new Error("Signed out"); }
  return res;
};

async function api(method, path, body) {
  const res = await fetch(path, {
    method, headers: body ? {"Content-Type": "application/json"} : {}, body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok && res.status !== 422) throw new Error(data.detail || `${res.status} ${res.statusText}`);
  return {status: res.status, data};
}

function toast(kind, title, text) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `<b>${esc(title)}</b>${esc(text || "")}`;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 9000);
}

// ---------- Navigation & History Routing ----------
const VIEW_ROUTES = {
  workflow: "/",
  servers: "/servers",
  spiders: "/spiders",
  links: "/links",
  relationships: "/relationships",
  agent: "/agent",
  tools: "/agent/tools",
  mcp_tools: "/agent/mcp_tools",
  skills: "/agent/skills",
  learning: "/learning",
  notifications: "/notifications",
  datapool: "/datapool"
};
const ROUTE_VIEWS = {
  "/": "workflow",
  "/servers": "servers",
  "/spiders": "spiders",
  "/links": "links",
  "/relationships": "relationships",
  "/agent": "agent",
  "/agent/": "agent",
  "/agent/tools": "tools",
  "/agent/mcp_tools": "mcp_tools",
  "/agent/skills": "skills",
  "/learning": "learning",
  "/notifications": "notifications",
  "/notifications/email": "notifications",
  "/notifications/whatsapp": "notifications",
  "/notifications/sms": "notifications",
  "/datapool": "datapool",
  "/chat": "agent"
};
const TITLES = {
  workflow: "Workflow",
  servers: "Servers",
  spiders: "Spiders",
  links: "Links",
  relationships: "Relationships",
  agent: "Agent",
  tools: "Agent Tools",
  mcp_tools: "Model Context Protocol (MCP) Tools",
  skills: "Agent Skills",
  learning: "Learning",
  notifications: "Notifications",
  datapool: "Data Pool Library"
};

function showView(name, pushState = true) {
  if (name === "chat") name = "agent";
  $$(".nav-item").forEach(b => b.classList.toggle("active", b.dataset.view === name || ((name === "tools" || name === "skills" || name === "mcp_tools") && b.dataset.view === "agent")));
  $$(".agent-subnav-tab").forEach(b => b.classList.toggle("active", b.dataset.subview === name));
  $$(".view").forEach(v => v.classList.toggle("active", v.id === `view-${name}` || (name === "agent" && v.id === "view-chat")));
  $("#view-title").textContent = TITLES[name] || name;
  if (name === "spiders") loadSpiders();
  if (name === "links") renderNodesTable();
  if (name === "relationships" && !topo.loaded) loadTopology();
  if (name === "workflow") requestAnimationFrame(() => { if (!fitted) fit(); });
  if (name === "agent" && typeof openChat === "function") openChat();
  if (name === "agent" && typeof renderAgent === "function") { renderAgent(); refreshAgentSignals(); }
  if (name === "tools" && typeof loadTools === "function") loadTools();
  if (name === "mcp_tools" && typeof loadMcpTools === "function") loadMcpTools();
  if (name === "skills" && typeof loadSkills === "function") loadSkills();
  if (name === "learning" && typeof loadLearning === "function") loadLearning();
  if (name === "servers" && typeof loadServers === "function") loadServers();
  if (name === "notifications" && typeof loadNotifications === "function") loadNotifications();
  if (name === "datapool" && typeof loadDataPool === "function") loadDataPool();
  const notifSublist = $("#nav-notif-subitems");
  if (notifSublist) notifSublist.hidden = name !== "notifications";
  store.set("view", name);

  if (pushState && VIEW_ROUTES[name] && window.location.pathname !== VIEW_ROUTES[name]) {
    history.pushState({view: name}, "", VIEW_ROUTES[name]);
  }
}

window.addEventListener("popstate", () => {
  const path = window.location.pathname.replace(/\/+$/, "") || "/";
  const viewName = ROUTE_VIEWS[path] || "workflow";
  showView(viewName, false);
});

$$(".nav-item").forEach(b => b.onclick = () => { if (b.dataset.view) showView(b.dataset.view); });

document.addEventListener("click", e => {
  const subBtn = e.target.closest("[data-subview]");
  if (subBtn && subBtn.dataset.subview) {
    e.preventDefault();
    showView(subBtn.dataset.subview);
    return;
  }
  const hubLink = e.target.closest("[data-nav-view]");
  if (hubLink && hubLink.dataset.navView) {
    e.preventDefault();
    showView(hubLink.dataset.navView);
    return;
  }
});

// ---------- Stream Stability & Flapping Analyzer ----------
// A stream counts as failing unless the operator ticked "ignore this stream" (channel_mute.StreamIgnores).
function isFailing(h) {
  return !!h && h.up === false && !h.ignored;
}
window.isFailing = isFailing;

// Flapping = 3 or more failures within the last 30 minutes. Failures hours apart are separate incidents, not
// flapping; backup-feed failures (outage_class.py) don't count. Recent outages and the last sub-incident blip count.
const FLAP_WINDOW_MS = 30 * 60 * 1000, FLAP_MIN = 3;
const parseTs = t => Date.parse(String(t || "").replace(/(\.\d{3})\d+/, "$1")) || 0;

function analyzeNodeStability(n) {
  if (!n) return null;
  const now = Date.now();
  const log = n.log || [];
  const recentOutages = log.filter(l => (l.type === "OUTAGE" || l.type === "ESCALATED") && l.class !== "BACKUP_FAILURE"
                                        && now - parseTs(l.timestamp) <= FLAP_WINDOW_MS);
  const starts = recentOutages.filter(l => l.type === "OUTAGE").map(l => parseTs(l.timestamp));
  let lastBlip = null;
  try { lastBlip = n.lastBlip ? JSON.parse(n.lastBlip) : null; } catch (_) { lastBlip = null; }
  const blipAt = lastBlip && parseTs(lastBlip.at);
  if (blipAt && now - blipAt <= FLAP_WINDOW_MS && !starts.some(t => Math.abs(t - blipAt) < 60000)) starts.push(blipAt);
  if (starts.length < FLAP_MIN) return null;
  starts.sort((a, b) => a - b);

  const recoveries = log.filter(l => l.type === "RECOVERY" && now - parseTs(l.timestamp) <= FLAP_WINDOW_MS);
  const durations = recoveries.map(r => r.durationS).filter(d => typeof d === "number" && d > 0);
  const avgDuration = durations.length ? Math.round(durations.reduce((a, b) => a + b, 0) / durations.length) : 15;
  const categories = Array.from(new Set(recentOutages.map(o => o.category).filter(Boolean)));
  const labels = n.labels || [];
  return {
    isFlapping: true,
    dropCount: starts.length,
    windowMin: FLAP_WINDOW_MS / 60000,
    recoveryCount: recoveries.length,
    avgDuration,
    minDuration: durations.length ? Math.min(...durations) : 8,
    maxDuration: durations.length ? Math.max(...durations) : 35,
    isStaleMedia: categories.includes("STALE_MEDIA") || categories.includes("NO_SEGMENTS")
      || recentOutages.some(o => (o.lastError || "").includes("STALE_SEGMENTS")),
    isBackup: labels.includes("BackupLink") && !labels.includes("MainInput"),
    multiUrlOutage: recentOutages.some(o => /^[2-9]\/\d+ URLs failing/.test(o.lastError || "")),
    categories,
    lastOutageAt: new Date(starts[starts.length - 1]).toISOString(),
  };
}
window.analyzeNodeStability = analyzeNodeStability;

