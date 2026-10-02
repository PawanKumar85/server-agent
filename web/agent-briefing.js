"use strict";

// ---------- Agent: live briefing, channel tally strip, questions from what's happening ----------
// The Agent page opens with the state of the network, not a greeting: one sentence written from live data,
// a tile per channel with its tally lamp (on air from Main, on Backup, off air), and questions about whatever
// currently stands out (off-air channels, the likely root cause, early warnings). Once a conversation starts,
// the briefing folds away and the tally strip stays pinned above it.

const agentView = {anomalies: {}, ranking: [], model: null, lamps: {}, fetchedAt: 0, memory: null, rcAnswered: {}};
const agentShort = id => String(id || "").replace(".ottlive.co.in", "").replace(/\.co\.in$/, "");

function agentChannels() {
  const channels = {};
  for (const n of G.nodes || []) {
    for (const l of n.links || []) {
      const c = channels[l.channel] ||= {MainInput: [], BackupLink: [], Transcoding: [], FinalLink: []};
      (c[l.role] ||= []).push({node: n.id, up: ((n.urlHealth || {})[l.url] || {}).up});
    }
  }
  const up = e => e.up !== false;  // never checked yet counts as up
  return Object.entries(channels).sort(([a], [b]) => a.localeCompare(b)).map(([name, c]) => {
    const main = c.MainInput.find(up), backup = c.BackupLink.find(up);
    const finalUp = c.FinalLink.every(up);
    const state = !finalUp || (!main && !backup) ? "off" : main ? "on" : "backup";
    const source = main || backup;
    const checked = [...c.MainInput, ...c.BackupLink, ...c.FinalLink].some(e => e.up != null);
    return {name, state, checked, source: source ? agentShort(source.node) : null};
  });
}

function agentWarnings() {
  return Object.entries(agentView.anomalies)
    .filter(([node, a]) => !node.endsWith(".invalid") && (a.warnings || []).length && !a.down)
    .map(([node, a]) => ({node, score: a.score, ...agentWarningParts(a.warnings[0])}))
    .sort((a, b) => b.score - a.score);
}

// Early-warning text from the server, as {metric, kind, phrase} in plain words with units:
// "HTTP latency 600 is 12σ above its normal 241.0" -> "HTTP latency is well above its usual 241 ms"
// "segment age is trending up (-0.3 vs normal 3.1)" -> "segment age is rising"
const METRIC_UNITS = {"HTTP latency": " ms", "ICMP RTT": " ms", "jitter": " ms", "segment age": " s", "packet loss": ""};
function agentWarningParts(w) {
  // Learned per-URL segment age: "segment age 45 s is above its usual 4 s (warns above 12 s) on https://…"
  const seg = w.match(/^segment age ([\d.-]+) s is above its usual ([\d.-]+) s \(warns above ([\d.-]+) s\)/);
  if (seg) {
    return {metric: "segment age", kind: "high",
            phrase: `segment age is ${Math.round(Number(seg[1]))} s, above its usual ${Math.round(Number(seg[2]))} s`};
  }
  const sigma = w.match(/^(.+?) [\d.-]+ is [\d.]+σ above its normal ([\d.-]+)$/);
  if (sigma) {
    const usual = Math.round(Number(sigma[2]));
    return {metric: sigma[1], kind: "high", phrase: `${sigma[1]} is well above its usual ${usual}${METRIC_UNITS[sigma[1]] ?? ""}`};
  }
  const trend = w.match(/^(.+?) is trending (up|down)/);
  if (trend) return {metric: trend[1], kind: "rising", phrase: `${trend[1]} is ${trend[2] === "up" ? "rising" : "falling"}`};
  return {metric: w.split(" ")[0], kind: "other", phrase: w.split(" (")[0]};
}

const agentList = names => names.length <= 2 ? names.join(" and ") : `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`;

function agentBriefing(chs, warnings) {
  const off = chs.filter(c => c.state === "off").map(c => c.name);
  const backup = chs.filter(c => c.state === "backup").map(c => c.name);
  const parts = [];
  if (!chs.length) return "No channels yet. Add stream links in Links to start monitoring.";
  if (!off.length && !backup.length) {
    parts.push(`All ${chs.length} channels are on air from their Main inputs.`);
  } else {
    parts.push(`${chs.length - off.length} of ${chs.length} channels are on air.`);
    if (off.length) parts.push(`${agentList(off)} ${off.length === 1 ? "is" : "are"} off air.`);
    if (backup.length) parts.push(`${agentList(backup)} ${backup.length === 1 ? "is" : "are"} running on backup.`);
  }
  const top = (agentView.ranking[0] || {}).ranking?.[0];
  if (top) parts.push(`The likely root cause is ${agentShort(top.node)}.`);
  if (warnings.length && !off.length && !backup.length) {  // during an outage the briefing sticks to it
    const w = warnings[0];
    parts.push(`${agentShort(w.node)}'s ${w.phrase}.`);
  }
  return parts.join(" ");
}

function agentPrompts(chs, warnings) {
  const prompts = [];
  for (const c of chs.filter(c => c.state === "off").slice(0, 2)) {
    prompts.push({q: `Why is ${c.name} off air?`, reason: "Off air", tone: "off"});
  }
  const top = (agentView.ranking[0] || {}).ranking?.[0];
  if (top) prompts.push({q: `Why is ${agentShort(top.node)} the likely root cause?`, reason: "Root cause", tone: "off"});
  for (const c of chs.filter(c => c.state === "backup").slice(0, 1)) {
    prompts.push({q: `Why is ${c.name} running on its backup?`, reason: "On backup", tone: "backup"});
  }
  for (const w of warnings.slice(0, 2)) {
    const how = w.kind === "high" ? "so high" : w.kind === "rising" ? "rising" : "unusual";
    prompts.push({q: `Why is ${agentShort(w.node)}'s ${w.metric} ${how}?`, reason: "Early warning", tone: "warn"});
  }
  const evergreen = [
    "Which channels had outages today?",
    "Is any channel running without a backup?",
    "Make a full report of all nodes",
    "Which servers are the slowest right now?",
  ];
  for (const q of evergreen) {
    if (prompts.length >= 5) break;
    prompts.push({q, reason: "", tone: ""});
  }
  return prompts;
}

function agentAgo(iso) {
  const t = Date.parse(String(iso || "").replace(/(\.\d{3})\d+/, "$1"));
  if (!t) return null;
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  return s < 90 ? `${s} s ago` : `${Math.round(s / 60)} min ago`;
}

const TALLY_TEXT = {on: "Main", backup: "Backup", off: "Off air"};

function renderAgent() {
  if (!$("#view-agent")?.classList.contains("active")) return;
  const chs = agentChannels(), warnings = agentWarnings();
  $("#agent-briefing-text").textContent = agentBriefing(chs, warnings);
  const latest = (G.nodes || []).map(n => n.lastPing).filter(Boolean).sort().at(-1);
  const ago = agentAgo(latest);
  $("#agent-briefing-note").textContent = ago ? `Last checked ${ago}.` : "Waiting for the first check.";

  $("#agent-tally").innerHTML = chs.map(c => {
    const changed = agentView.lamps[c.name] && agentView.lamps[c.name] !== c.state;
    // The lamp's colour says Main / Backup / off air; the text says where the feed comes from.
    const detail = c.state === "off" ? "off air" : c.state === "backup" ? "on backup" : c.source ? `from ${esc(c.source)}` : "on air";
    const tip = `${c.name}: ${c.state === "off" ? "off air" : `${TALLY_TEXT[c.state]} input${c.source ? ` (${c.source})` : ""}`}. Click to ask about it.`;
    return `<button class="tally tally-${c.state}${c.checked ? "" : " tally-unchecked"}${changed ? " tally-changed" : ""}"
      role="listitem" data-channel="${esc(c.name)}" title="${esc(tip)}" aria-label="${esc(tip)}">
      <span class="tally-name">${esc(c.name)}</span><span class="tally-detail">${detail}</span></button>`;
  }).join("");
  agentView.lamps = Object.fromEntries(chs.map(c => [c.name, c.state]));

  renderRootCauseCheck();
  renderMemory();
  if (typeof renderUpNext === "function") renderUpNext("#agent-upnext");

  $("#agent-prompts").innerHTML = agentPrompts(chs, warnings).map(p =>
    `<li><button class="agent-prompt" data-q="${esc(p.q)}"><span class="agent-prompt-q">${esc(p.q)}</span>
      ${p.reason ? `<span class="agent-prompt-reason reason-${p.tone}">${esc(p.reason)}</span>` : ""}</button></li>`).join("");
}

// "Is X the root cause?" under the briefing: the answer is a verdict the ranking learns from (learning.priors).
function renderRootCauseCheck() {
  const box = $("#agent-rc");
  const top = (agentView.ranking[0] || {}).ranking?.[0];
  const key = top ? `${top.node}|${top.onsetAt || ""}` : null;
  if (!top) { box.hidden = true; return; }
  box.hidden = false;
  const said = agentView.rcAnswered[key];
  box.innerHTML = said
    ? `<span>${said === 1 ? "Thanks, confirmed." : "Thanks, I'll weigh it lower next time."} Rankings learn from your answers.</span>`
    : `<span>Is ${esc(agentShort(top.node))} really the root cause?</span>
       <button type="button" class="btn small" data-rc="1" data-node="${esc(top.node)}" data-key="${esc(key)}">Yes</button>
       <button type="button" class="btn small" data-rc="-1" data-node="${esc(top.node)}" data-key="${esc(key)}">No</button>`;
}

function renderMemory() {
  const m = agentView.memory;
  if (!m) return;
  const fb = m.feedback || {answer: {}, root_cause: {}};
  $("#agent-memory-sub").textContent = `From ${m.caseCount} closed outage${m.caseCount === 1 ? "" : "s"} and your feedback `
    + `(answers 👍 ${fb.answer.up || 0} · 👎 ${fb.answer.down || 0}; root causes right ${fb.root_cause.up || 0} · wrong ${fb.root_cause.down || 0}).`;
  const patterns = (m.patterns || []).slice(0, 5);
  $("#agent-memory-patterns").innerHTML = patterns.length
    ? patterns.map(p => `<li>${esc(p.text)}</li>`).join("")
    : `<li class="agent-memory-empty">No patterns yet. They appear once a server has had at least two outages.</li>`;
  $("#agent-memory-facts").innerHTML = (m.facts || []).length
    ? m.facts.map(f => `<li><span>${esc(f.text)}</span><button type="button" class="agent-memory-del" data-fact="${f.id}" aria-label="Forget: ${esc(f.text)}" title="Forget">✕</button></li>`).join("")
    : `<li class="agent-memory-empty">Nothing yet. Tell me things about your network and I'll use them in answers.</li>`;
}

async function refreshMemory() {
  try { agentView.memory = (await api("GET", "/api/learning")).data; } catch { /* keeps the last copy */ }
  renderMemory();
}

async function refreshAgentSignals() {
  if (Date.now() - agentView.fetchedAt < 25000) return;
  agentView.fetchedAt = Date.now();
  const [anomalies, ranking] = await Promise.allSettled([api("GET", "/api/anomalies"), api("GET", "/api/rca/ranking"), refreshMemory(),
    api("GET", "/api/alerts?hours=1").then(r => setUpcoming(r.data)).catch(() => {})]);
  if (anomalies.status === "fulfilled") agentView.anomalies = anomalies.value.data || {};
  if (ranking.status === "fulfilled") agentView.ranking = ranking.value.data.groups || [];
  if (!agentView.model) {
    try {
      const {data} = await api("GET", "/api/chat");
      agentView.model = data.model;
      if (data.model) $("#agent-hint").innerHTML = `Answers use ${esc(data.model)} with your live graph. Type <kbd>/</kbd> for a playbook, or use <kbd>+</kbd> to run a tool directly.`;
    } catch { /* the hint keeps its default text */ }
  }
  renderAgent();
}

// A conversation folds the briefing away and keeps the tally strip as a slim bar above it.
new MutationObserver(() => $("#agent").classList.toggle("is-talking", $("#chat-empty").hidden))
  .observe($("#chat-empty"), {attributes: true, attributeFilter: ["hidden"]});

document.addEventListener("click", e => {
  const tile = e.target.closest("#agent-tally [data-channel]");
  if (tile) { sendChat(`How is the ${tile.dataset.channel} channel right now?`); return; }
  const prompt = e.target.closest("#agent-prompts [data-q]");
  if (prompt) { sendChat(prompt.dataset.q); return; }
  const rc = e.target.closest("#agent-rc [data-rc]");
  if (rc) {
    const rating = Number(rc.dataset.rc);
    agentView.rcAnswered[rc.dataset.key] = rating;
    renderRootCauseCheck();
    api("POST", "/api/learning/feedback", {kind: "root_cause", rating, node: rc.dataset.node})
      .then(() => { agentView.fetchedAt = 0; refreshAgentSignals(); }).catch(err => toast("error", "Feedback not saved", err.message));
    return;
  }
  const del = e.target.closest("#agent-memory [data-fact]");
  if (del) api("DELETE", `/api/learning/facts/${del.dataset.fact}`).then(refreshMemory);
});

$("#agent-memory-add").addEventListener("submit", async e => {
  e.preventDefault();
  const input = $("#agent-memory-input"), text = input.value.trim();
  if (text.length < 3) return;
  await api("POST", "/api/learning/facts", {text});
  input.value = "";
  refreshMemory();
});

// Redraw from the latest graph every few seconds while the page is open (the graph refreshes after every run).
setInterval(() => { if ($("#view-agent")?.classList.contains("active")) { renderAgent(); refreshAgentSignals(); } }, 5000);
