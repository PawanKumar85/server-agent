"use strict";

// ---------- ChatBot ----------
// Each question is answered on its own: the server retrieves the closest live facts and streams the
// local model's answer back as NDJSON events (sources, token..., done | error).
const chat = {busy: false, checked: false, history: [], conv: null, restored: false};  // conv: the saved conversation
try { chat.conv = localStorage.getItem("chat.conv"); } catch { /* private mode: a new conversation each visit */ }
function setConversation(id) {
  chat.conv = id;
  try { id ? localStorage.setItem("chat.conv", id) : localStorage.removeItem("chat.conv"); } catch { /* fine */ }
}
const chatLog = $("#chat-log"), chatInput = $("#chat-input");

// Tiny, safe Markdown: escape everything, then paragraphs, "-" lists, **bold** and `code`.
function renderMarkdown(text) {
  const inline = s => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  const out = [];
  let para = [], list = null;  // list = {tag: "ul"|"ol", items: []}
  const flush = () => {
    if (para.length) { out.push(`<p>${para.map(inline).join("<br>")}</p>`); para = []; }
    if (list) { out.push(`<${list.tag}>${list.items.map(i => `<li>${inline(i)}</li>`).join("")}</${list.tag}>`); list = null; }
  };
  for (const line of String(text).trim().split("\n")) {
    const heading = line.match(/^\s*#{1,6}\s+(.*?)\s*:?\s*$/);
    const bullet = line.match(/^\s*[-*•]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (!line.trim()) flush();
    else if (heading) { flush(); out.push(`<h5>${inline(heading[1].replace(/\*\*/g, ""))}</h5>`); }
    else if (bullet || numbered) {
      const tag = bullet ? "ul" : "ol";
      if (para.length || (list && list.tag !== tag)) flush();
      list = list || {tag, items: []};
      list.items.push((bullet || numbered)[1]);
    } else {
      if (list) flush();
      para.push(line);
    }
  }
  flush();
  return out.join("");
}

function chatBubble(kind, html) {
  $("#chat-empty").hidden = true;
  const el = document.createElement("div");
  el.className = `msg ${kind}`;
  el.innerHTML = html;
  chatLog.appendChild(el);
  chatLog.scrollTop = chatLog.scrollHeight;
  return el;
}

async function openChat() {
  requestAnimationFrame(() => chatInput.focus());
  if (!chat.restored) { chat.restored = true; if (chat.conv) loadConversation(chat.conv, true); }
  if (chat.checked) return;
  try {
    const {data} = await api("GET", "/api/chat");
    chat.checked = data.configured;
    $("#chat-setup").hidden = data.configured;
    $("#chat-setup-text").textContent = data.problem || "";
    const modelTag = $("#agent-model-badge");
    if (modelTag && data.model) {
      let poolInfo = "";
      if (data.keyPool && data.keyPool.total_keys > 1) {
        poolInfo = ` · Pool (${data.keyPool.active_keys}/${data.keyPool.total_keys})`;
      }
      let langsmithInfo = "";
      if (data.langsmith && data.langsmith.enabled) {
        langsmithInfo = " · 🦜 LangSmith";
      }
      modelTag.textContent = `${data.model} · GraphRAG · 15 Tools · 7 Skills${poolInfo}${langsmithInfo}`;
    }
    const poolBadge = $("#agent-keypool-badge");
    const poolText = $("#agent-keypool-text");
    if (poolBadge && poolText) {
      if (data.keyPool && data.keyPool.total_keys > 1) {
        poolBadge.style.display = "inline-flex";
        poolText.textContent = `Round-Robin (${data.keyPool.active_keys}/${data.keyPool.total_keys} active)`;
        poolBadge.title = `Strategy B: Round-Robin Rotation (Active-Active) across ${data.keyPool.total_keys} OpenRouter keys with auto-failover`;
      } else if (data.keyPool && data.keyPool.total_keys === 1) {
        poolBadge.style.display = "inline-flex";
        poolText.textContent = `1 Key Active`;
        poolBadge.title = `Add multiple keys comma-separated to OPENROUTER_API_KEYS for round-robin rotation`;
      } else {
        poolBadge.style.display = "none";
      }
    }
    const modelLabel = $("#chat-model");  // optional in the page
    if (modelLabel) modelLabel.textContent = `${data.model} · ${data.embedModel.split("/").pop()}`;
  } catch (err) {
    toast("error", "Agent", err.message);
  }
}

// "651 in · 10 out" (+ reasoning when the model used any): OpenRouter bills per token.
const tokenLine = t => t ? `${t.input ?? "?"} in · ${t.output ?? "?"} out tokens${t.reasoning ? ` (${t.reasoning} reasoning)` : ""}` : "";

// Ground truth from the data itself, shown under every answer: the model can be wrong.
function liveLine(live) {
  if (!live) return "";
  const down = live.down.length ? `<b class="bad">${live.down.length} down</b> (${esc(live.down.join(", "))})` : "0 down";
  const backup = live.onBackup.length ? `<b class="warn">${live.onBackup.length} on backup</b> (${esc(live.onBackup.join(", "))})` : "0 on backup";
  return `Live: ${live.channels} channels, ${down}, ${backup}`;
}

async function sendChat(text) {
  text = text.trim();
  if (!text || chat.busy) return;
  chat.busy = true;
  $("#chat-send").disabled = true;
  chatInput.value = ""; autosizeChat();
  chatBubble("user", esc(text));
  const ctxId = "ctx_" + Date.now() + "_" + Math.random().toString(36).substr(2, 6);
  window.CHAT_CONTEXTS = window.CHAT_CONTEXTS || {};
  const bubble = chatBubble("bot typing", "Searching the live data…");
  let answer = "", meta = {sources: [], live: null}, failed = null, done = null, reportCard = null, actionCards = [], steps = [];
  const render = () => {
    window.CHAT_CONTEXTS[ctxId] = { query: text, meta, done, answer };
    const contextBtn = meta && (meta.sources?.length || meta.live) ?
      `<button type="button" class="btn-inspect-context" data-ctx-id="${ctxId}" title="Inspect GraphRAG context, retrieved chunks and prompt">
        <span class="ctx-ico">🧠</span>
        <span>View Context</span>
        <span class="ctx-tag">${meta.sources.length} sources${done?.tokens?.input ? ` · ${done.tokens.input} tok` : ''}</span>
      </button>` : '';

    const footItems = [
      liveLine(meta.live),
      meta.sources.length ? `GraphRAG: ${esc(meta.sources.filter(s => s.via !== "overview").map(s => `${s.title} (${s.via})`).join(", ") || "overview only")}` : "",
      done ? `${done.model ? esc(done.model.split("/").pop()) + " · " : ""}${(done.elapsed_ms / 1000).toFixed(1)} s${done.tokens ? " · " + tokenLine(done.tokens) : ""}${done.truncated ? " · cut at the length limit" : ""}` : ""
    ].filter(Boolean);

    const foot = footItems.length ?
      `<div class="msg-context-bar">${contextBtn}</div><div class="steps-details">${footItems.join("<br>")}</div>` :
      (contextBtn ? `<div class="msg-context-bar">${contextBtn}</div>` : "");

    const card = reportCard ? `<div class="report-card"><div class="report-card-title">${reportCard.icon || "📄"} ${esc(reportCard.title || "Report")}: ${esc(reportCard.label)}</div>
      <div class="report-card-actions"><a class="btn small primary" href="${esc(reportCard.url)}" target="_blank" rel="noopener">${esc(reportCard.openText || "Open report")}</a>
      <a class="btn small" href="${esc(reportCard.download)}" download>⬇ Download</a></div></div>` : "";
    // Every change the assistant asks for gets its own Allow/Deny card (a reply can hold several).
    const actionHtml = actionCards.map(actionCard => {
      const isDanger = actionCard.severity === "danger";
      // The tools send summary + warning (or preview lines); older events used description + preview.
      const preview = actionCard.preview || String(actionCard.warning || "").split("\n").filter(Boolean);
      const previewItems = preview.map(p => `<li>${esc(p).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")}</li>`).join("");
      return `<div class="action-card ${isDanger ? 'danger' : 'warning'}" id="card-${esc(actionCard.action_id)}">
        <div class="action-card-header">
          <span class="action-card-badge">${isDanger ? '⚠️ HIGH IMPACT' : '⚡ MUTATION'}</span>
          <span class="action-card-title">${esc(actionCard.title)}</span>
        </div>
        <div class="action-card-body">
          <p class="action-card-desc">${esc(actionCard.description || actionCard.summary || "")}</p>
          <p class="action-card-ask">The assistant wants to change the graph. Allow it?</p>
          <ul class="action-card-preview">${previewItems}</ul>
        </div>
        <div class="action-card-actions">
          <button class="btn small ${isDanger ? 'danger' : 'primary'} btn-confirm-action" data-action-id="${esc(actionCard.action_id)}">Allow</button>
          <button class="btn small btn-cancel-action" data-action-id="${esc(actionCard.action_id)}">Deny</button>
        </div>
      </div>`;
    }).join("");
    bubble.innerHTML = stepsHtml(steps, !done && !failed) + actionHtml + card + (answer ? renderMarkdown(answer) : "") + (foot ? `<div class="steps">${foot}</div>` : "");
    chatLog.scrollTop = chatLog.scrollHeight;
  };
  try {
    const res = await fetch("/api/chat", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({message: text, history: chat.history.slice(-6), conversation_id: chat.conv}),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(typeof data.detail === "string" ? data.detail : `${res.status} ${res.statusText}`);
    }
    const reader = res.body.getReader(), decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const {value, done: ended} = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), {stream: !ended});
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const line of lines.filter(Boolean)) {
        const event = JSON.parse(line);
        if (event.type === "conversation") { setConversation(event.id); continue; }
        if (event.type === "sources") { meta = event; bubble.className = "msg bot streaming"; }
        else if (event.type === "step") steps.push({index: event.index, tool: event.tool, args: event.args, summary: null});
        else if (event.type === "step_done") {
          const st = steps.find(x => x.index === event.index);
          if (st) st.summary = event.summary || "done";
        }
        else if (event.type === "token") answer += event.text;
        else if (event.type === "report") reportCard = event;
        else if (event.type === "action_confirm") actionCards.push(event);
        else if (event.type === "done") done = event;
        else if (event.type === "error") failed = event.message;
        render();
      }
      if (ended) break;
    }
    if (failed) throw new Error(failed);
    if (!answer && !actionCards.length) { answer = "(no answer)"; render(); }
    bubble.className = "msg bot";
    if (!/^(CONFIRM|CANCEL)\s+act_/i.test(text) && answer && !failed) {
      chat.history.push({role: "user", content: text}, {role: "assistant", content: answer.slice(0, 20000)});
      chat.history = chat.history.slice(-12);
    }
    if (!actionCards.length && !/^(CONFIRM|CANCEL)\s+act_/i.test(text)) addAnswerFeedback(bubble, text, answer, done?.toolsUsed || []);
  } catch (err) {
    if (answer || actionCards.length) { bubble.className = "msg bot"; render(); chatBubble("error", esc(err.message)); }
    else { bubble.className = "msg error"; bubble.textContent = err.message; }
    if (/OPENROUTER_API_KEY|out of credits/.test(err.message)) { chat.checked = false; openChat(); }
  } finally {
    chat.busy = false;
    $("#chat-send").disabled = false;
    chatInput.focus();
  }
}

// 👍/👎 under each answer. A 👎 can carry the right answer: the agent keeps it as a lesson and follows it the
// next time a similar question comes up (learning.py).
function addAnswerFeedback(bubble, question, answer, tools = []) {
  const row = document.createElement("div");
  row.className = "msg-feedback";
  row.innerHTML = `<span class="msg-feedback-q">Was this right?</span>
    <button type="button" class="fb-btn" data-rate="1" aria-label="Good answer" title="Good answer">👍</button>
    <button type="button" class="fb-btn" data-rate="-1" aria-label="Wrong answer" title="Wrong answer">👎</button>`;
  bubble.after(row);
  const send = async (rating, correction) => {
    try {
      await api("POST", "/api/learning/feedback", {kind: "answer", rating, question, answer: answer.slice(0, 8000), correction, tools});
      row.innerHTML = `<span class="msg-feedback-q">${correction ? "Thanks, I'll answer that way next time." : "Thanks for the feedback."}</span>`;
    } catch (err) {
      row.innerHTML = `<span class="msg-feedback-q">Couldn't save the feedback: ${esc(err.message)}</span>`;
    }
  };
  row.addEventListener("click", e => {
    const b = e.target.closest(".fb-btn");
    if (!b) return;
    if (b.dataset.rate === "1") { send(1); return; }
    row.innerHTML = `<form class="fb-fix"><input type="text" maxlength="2000" placeholder="What should the answer have been? (optional)" aria-label="Correct answer">
      <button type="submit" class="btn small primary">Save</button></form>`;
    const form = row.querySelector("form");
    form.querySelector("input").focus();
    form.onsubmit = ev => { ev.preventDefault(); send(-1, form.querySelector("input").value.trim() || null); };
  });
  chatLog.scrollTop = chatLog.scrollHeight;
}

function autosizeChat() {
  chatInput.style.height = "auto";
  chatInput.style.height = Math.min(chatInput.scrollHeight + 2, 160) + "px";
}

chatLog.addEventListener("click", e => {
  const busyNow = e.target.closest(".btn-confirm-action, .btn-cancel-action") && chat.busy;
  if (busyNow) { toast("warn", "One moment", "Wait for the current answer to finish, then click again."); return; }
  const confirmBtn = e.target.closest(".btn-confirm-action");
  if (confirmBtn) {
    const actId = confirmBtn.dataset.actionId;
    const card = confirmBtn.closest(".action-card");
    if (card) {
      card.classList.add("action-card-confirmed");
      confirmBtn.disabled = true;
      const cancelBtn = card.querySelector(".btn-cancel-action");
      if (cancelBtn) cancelBtn.disabled = true;
    }
    sendChat(`CONFIRM ${actId}`);
    return;
  }
  const cancelBtn = e.target.closest(".btn-cancel-action");
  if (cancelBtn) {
    const actId = cancelBtn.dataset.actionId;
    const card = cancelBtn.closest(".action-card");
    if (card) {
      card.classList.add("action-card-cancelled");
      cancelBtn.disabled = true;
      const cBtn = card.querySelector(".btn-confirm-action");
      if (cBtn) cBtn.disabled = true;
    }
    sendChat(`CANCEL ${actId}`);
    return;
  }
});

$("#chat-form").onsubmit = e => {
  e.preventDefault();
  hideSlashMenu();
  sendChat(chatInput.value);
};

// ---------- Slash Commands (/) Skills Autocomplete ----------
let cachedSkills = null;
let slashSelectedIndex = 0;
const slashMenu = $("#chat-skills-slash-menu");
const slashList = $("#chat-slash-list");

async function fetchProjectSkills() {
  if (cachedSkills) return cachedSkills;
  try {
    const {data} = await api("GET", "/api/skills");
    if (data && data.skills) cachedSkills = data.skills;
  } catch (err) {
    console.error("Failed to fetch skills for slash menu", err);
  }
  return cachedSkills || [];
}

function renderSlashMenu(filter = "") {
  if (!slashList) return;
  const q = filter.trim().toLowerCase();
  const matched = (cachedSkills || []).filter(s => {
    if (!q) return true;
    return (s.command && s.command.toLowerCase().includes(q)) ||
           (s.title && s.title.toLowerCase().includes(q)) ||
           (s.category && s.category.toLowerCase().includes(q)) ||
           (s.aliases && s.aliases.some(a => a.toLowerCase().includes(q)));
  });

  if (!matched.length) {
    slashList.innerHTML = `<div class="chat-slash-empty">No skills matching "/${esc(filter)}"</div>`;
    slashSelectedIndex = -1;
    return;
  }

  slashSelectedIndex = Math.max(0, Math.min(slashSelectedIndex, matched.length - 1));

  slashList.innerHTML = matched.map((s, idx) => `
    <button type="button" class="chat-slash-item ${idx === slashSelectedIndex ? 'selected' : ''}" data-idx="${idx}" data-prompt="${esc(s.prompt)}">
      <span class="chat-slash-icon">${s.icon || "🧠"}</span>
      <div class="chat-slash-info">
        <div class="chat-slash-top">
          <span class="chat-slash-cmd">${esc(s.command || '')}</span>
          <span class="chat-slash-name">${esc(s.title || '')}</span>
          <span class="chat-slash-badge">${esc(s.badge || s.category || '')}</span>
        </div>
        <div class="chat-slash-desc">${esc(s.description || '')}</div>
      </div>
    </button>
  `).join("");

  $$(".chat-slash-item", slashList).forEach(b => {
    b.onclick = () => selectSlashItem(b.dataset.prompt);
  });
}

function selectSlashItem(prompt) {
  if (!prompt) return;
  chatInput.value = prompt;
  autosizeChat();
  hideSlashMenu();
  chatInput.focus();
}

function showSlashMenu(filterText) {
  if (!slashMenu) return;
  slashMenu.hidden = false;
  fetchProjectSkills().then(() => renderSlashMenu(filterText));
}

function hideSlashMenu() {
  if (!slashMenu) return;
  slashMenu.hidden = true;
  slashSelectedIndex = 0;
}

chatInput.addEventListener("input", () => {
  autosizeChat();
  const val = chatInput.value;
  if (val.startsWith("/")) {
    showSlashMenu(val.slice(1));
  } else {
    hideSlashMenu();
  }
});

chatInput.addEventListener("keydown", e => {
  if (slashMenu && !slashMenu.hidden) {
    const items = $$(".chat-slash-item", slashList);
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (items.length) {
        slashSelectedIndex = (slashSelectedIndex + 1) % items.length;
        items.forEach((it, i) => it.classList.toggle("selected", i === slashSelectedIndex));
        items[slashSelectedIndex]?.scrollIntoView({block: "nearest"});
      }
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      if (items.length) {
        slashSelectedIndex = (slashSelectedIndex - 1 + items.length) % items.length;
        items.forEach((it, i) => it.classList.toggle("selected", i === slashSelectedIndex));
        items[slashSelectedIndex]?.scrollIntoView({block: "nearest"});
      }
      return;
    }
    if ((e.key === "Enter" || e.key === "Tab") && !e.shiftKey) {
      if (items.length && slashSelectedIndex >= 0 && items[slashSelectedIndex]) {
        e.preventDefault();
        selectSlashItem(items[slashSelectedIndex].dataset.prompt);
        return;
      }
    }
    if (e.key === "Escape") {
      e.preventDefault();
      hideSlashMenu();
      return;
    }
  }

  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    hideSlashMenu();
    sendChat(chatInput.value);
  }
});

document.addEventListener("click", e => {
  if (!slashMenu || slashMenu.hidden) return;
  if (!e.target.closest("#chat-form")) {
    hideSlashMenu();
  }
});
function clearChatLog() {
  $$(".msg, .msg-feedback", chatLog).forEach(m => m.remove());
  chat.history = [];
  $("#chat-empty").hidden = false;
}
$("#chat-new").onclick = () => {
  if (chat.busy) return;
  clearChatLog();
  setConversation(null);  // the server starts a new one with the next question
  chatInput.focus();
};

// What the agent did to answer: one line per tool it ran, live while it works, folded away once it has answered.
const TOOL_WORDS = {
  get_root_cause_ranking: "Ranked the likely causes", get_early_warnings: "Checked early warnings",
  get_incident_history: "Read the outage history", get_risk_forecast: "Checked the risk forecast",
  diagnose_hls_stream: "Tested the stream", check_failover_status: "Checked backups", audit_topology: "Checked the wiring",
  query_graph: "Looked it up in the graph", run_traceroute: "Traced the network path", get_pool_status: "Checked the data pool",
  get_learned_memory: "Read what it learned", trigger_channel_crawl: "Started a health check",
};
function stepsHtml(steps, running) {
  if (!steps.length) return "";
  const rows = steps.map(st => `<li class="${st.summary === null ? "running" : "ok"}">
    <span class="agent-step-ico">${st.summary === null ? "⏳" : "✓"}</span>
    <span><b>${esc(TOOL_WORDS[st.tool] || st.tool.replace(/_/g, " "))}</b>${st.summary ? ` <span class="agent-step-sum">${esc(st.summary.slice(0, 160))}</span>` : ""}</span></li>`).join("");
  return `<details class="agent-steps"${running ? " open" : ""}><summary>${running ? "Working…" : `${steps.length} step${steps.length > 1 ? "s" : ""}`}</summary><ol>${rows}</ol></details>`;
}

// Saved conversations: the same on every device, kept for 90 days.
const historyBtn = $("#chat-history-btn"), historyBox = $("#chat-history");
function closeChatHistory() { historyBox.hidden = true; historyBtn.setAttribute("aria-expanded", "false"); }
async function openChatHistory() {
  historyBox.hidden = false;
  historyBtn.setAttribute("aria-expanded", "true");
  historyBox.innerHTML = `<p class="chat-history-empty">Loading…</p>`;
  try {
    const {data} = await api("GET", "/api/chat/conversations");
    historyBox.innerHTML = data.conversations.length ? data.conversations.map(c => `
      <div class="chat-history-row${c.id === chat.conv ? " current" : ""}" data-id="${esc(c.id)}">
        <button type="button" class="chat-history-open" data-id="${esc(c.id)}">
          <span class="chat-history-title">${esc(c.title)}</span>
          <span class="chat-history-meta">${new Date(c.updated * 1000).toLocaleString()} · ${c.n} messages</span>
        </button>
        <button type="button" class="chat-history-del" data-id="${esc(c.id)}" aria-label="Delete this conversation" title="Delete">✕</button>
      </div>`).join("") : `<p class="chat-history-empty">No saved conversations yet.</p>`;
  } catch (err) {
    historyBox.innerHTML = `<p class="chat-history-empty">Couldn't load: ${esc(err.message)}</p>`;
  }
}
historyBtn.onclick = e => { e.stopPropagation(); historyBox.hidden ? openChatHistory() : closeChatHistory(); };
historyBox.addEventListener("click", async e => {
  e.stopPropagation();
  const del = e.target.closest(".chat-history-del");
  if (del) {
    await api("DELETE", `/api/chat/conversations/${encodeURIComponent(del.dataset.id)}`).catch(() => {});
    if (del.dataset.id === chat.conv) { clearChatLog(); setConversation(null); }
    openChatHistory();
    return;
  }
  const open = e.target.closest(".chat-history-open");
  if (open && !chat.busy) { closeChatHistory(); loadConversation(open.dataset.id); }
});
document.addEventListener("click", e => { if (!historyBox.hidden && !e.target.closest(".chat-history-wrap")) closeChatHistory(); });

async function loadConversation(id, quiet = false) {
  try {
    const {data} = await api("GET", `/api/chat/conversations/${encodeURIComponent(id)}`);
    if (chat.busy) return;
    clearChatLog();
    setConversation(id);
    for (const m of data.messages) {
      if (m.role === "user") { chatBubble("user", esc(m.content)); continue; }
      const steps = (m.meta.steps || []).map((st, i) => ({index: i + 1, tool: st.tool, summary: st.summary || "done"}));
      chatBubble("bot", stepsHtml(steps, false) + renderMarkdown(m.content));
    }
    chat.history = data.messages.filter(m => !/^(CONFIRM|CANCEL)\s+act_/i.test(m.content))
      .map(m => ({role: m.role, content: m.content.slice(0, 20000)})).slice(-12);
  } catch (err) {
    if (quiet) setConversation(null);  // it was deleted or expired: start fresh
    else chatBubble("error", `Couldn't open that conversation: ${esc(err.message)}`);
  }
}
const chatToolsBtn = $("#chat-tools-btn");
if (chatToolsBtn) {
  chatToolsBtn.onclick = () => showView("tools");
}
const chatSkillsBtn = $("#chat-skills-btn");
if (chatSkillsBtn) {
  chatSkillsBtn.onclick = () => showView("skills");
}

// ---------- ChatGPT-style Plus Icon & Tools Dropdown ----------
let cachedChatTools = null;
const btnChatPlus = $("#btn-chat-tools-plus");
const chatToolsDropdown = $("#chat-tools-dropdown");
const chatToolsSearch = $("#chat-tools-search");
const chatToolsList = $("#chat-tools-dropdown-items");
const chatToolsCountPill = $("#chat-tools-count-pill");

async function fetchChatTools() {
  if (cachedChatTools) return cachedChatTools;
  try {
    const {data} = await api("GET", "/api/tools");
    if (data && data.tools) {
      cachedChatTools = data.tools;
      if (chatToolsCountPill) chatToolsCountPill.textContent = data.count || data.tools.length;
    }
  } catch (err) {
    console.error("Failed to fetch tools for dropdown", err);
  }
  return cachedChatTools || [];
}

function renderChatToolsDropdown(filter = "") {
  if (!chatToolsList) return;
  const q = filter.trim().toLowerCase();
  const tools = (cachedChatTools || []).filter(t => {
    if (!q) return true;
    return (t.name || "").toLowerCase().includes(q) ||
           (t.description || "").toLowerCase().includes(q) ||
           (t.category || "").toLowerCase().includes(q);
  });

  if (!tools.length) {
    chatToolsList.innerHTML = `<div class="chat-tools-item-empty">No tools matching "${esc(filter)}"</div>`;
    return;
  }

  chatToolsList.innerHTML = tools.map(t => {
    const isDanger = (t.category || "").includes("Danger");
    const isSafe = (t.category || "").includes("Safe");
    const badgeClass = isDanger ? "danger" : (isSafe ? "safe" : "");
    const title = esc(t.name.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase()));
    return `
      <button type="button" class="chat-tools-item" data-prompt="${esc(t.prompt || '')}" data-name="${esc(t.name)}">
        <span class="chat-tools-item-icon">${t.icon || "🛠"}</span>
        <div class="chat-tools-item-content">
          <div class="chat-tools-item-header">
            <span class="chat-tools-item-name">${title}</span>
            <span class="chat-tools-item-badge ${badgeClass}">${esc(t.category || 'Tool')}</span>
          </div>
          <div class="chat-tools-item-desc">${esc(t.description || '')}</div>
        </div>
      </button>
    `;
  }).join("");

  $$(".chat-tools-item", chatToolsList).forEach(item => {
    item.onclick = () => {
      const prompt = item.dataset.prompt;
      if (prompt) {
        chatInput.value = prompt;
        autosizeChat();
        chatInput.focus();
      }
      closeToolsDropdown();
    };
  });
}

function openToolsDropdown() {
  if (!chatToolsDropdown) return;
  chatToolsDropdown.hidden = false;
  if (btnChatPlus) btnChatPlus.classList.add("active");
  if (chatToolsSearch) {
    chatToolsSearch.value = "";
    requestAnimationFrame(() => chatToolsSearch.focus());
  }
  fetchChatTools().then(() => renderChatToolsDropdown(""));
}

function closeToolsDropdown() {
  if (!chatToolsDropdown) return;
  chatToolsDropdown.hidden = true;
  if (btnChatPlus) btnChatPlus.classList.remove("active");
}

function toggleToolsDropdown() {
  if (!chatToolsDropdown) return;
  if (chatToolsDropdown.hidden) {
    openToolsDropdown();
  } else {
    closeToolsDropdown();
  }
}

if (btnChatPlus) {
  btnChatPlus.onclick = e => {
    e.stopPropagation();
    toggleToolsDropdown();
  };
}

if (chatToolsSearch) {
  chatToolsSearch.oninput = () => renderChatToolsDropdown(chatToolsSearch.value);
  chatToolsSearch.onkeydown = e => {
    if (e.key === "Escape") {
      e.preventDefault();
      closeToolsDropdown();
      chatInput.focus();
    }
  };
}

document.addEventListener("click", e => {
  if (!chatToolsDropdown || chatToolsDropdown.hidden) return;
  if (!e.target.closest(".chat-tools-wrapper")) {
    closeToolsDropdown();
  }
});

document.addEventListener("keydown", e => {
  if (e.key === "Escape" && chatToolsDropdown && !chatToolsDropdown.hidden) {
    closeToolsDropdown();
    chatInput.focus();
  }
});

// ---------- Tools view actions ----------
$$(".btn-chat-tool").forEach(b => {
  b.onclick = () => {
    const prompt = b.dataset.prompt;
    showView("agent");
    if (prompt) {
      chatInput.value = prompt;
      sendChat(prompt);
    }
  };
});
const btnToolsRunSpiders = $("#btn-tools-run-spiders");
if (btnToolsRunSpiders) {
  btnToolsRunSpiders.onclick = () => {
    showView("workflow");
    startRun();
  };
}

// ---------- Context & Retrieval Inspector (Claude-style) ----------
let activeContext = null;
const contextModal = $("#context-modal");
const contextModalClose = $("#context-modal-close");
const ctxModalQuery = $("#context-modal-query");
const ctxModalModel = $("#ctx-modal-model");
const ctxTabCount = $("#ctx-tab-count");
const ctxChunkFilter = $("#ctx-chunk-filter");

function closeContextModal() {
  if (contextModal) contextModal.hidden = true;
}

if (contextModalClose) contextModalClose.onclick = closeContextModal;
if (contextModal) {
  contextModal.onclick = e => {
    if (e.target === contextModal) closeContextModal();
  };
}

document.addEventListener("keydown", e => {
  if (e.key === "Escape" && contextModal && !contextModal.hidden) {
    closeContextModal();
  }
});

// Tab switching inside Context Inspector
$$(".ctx-tabs-bar .tab-btn").forEach(btn => {
  btn.onclick = () => {
    const tab = btn.dataset.ctxTab;
    $$(".ctx-tabs-bar .tab-btn").forEach(b => b.classList.toggle("active", b === btn));
    $$(".ctx-panel").forEach(p => p.hidden = p.id !== `ctx-panel-${tab}`);
  };
});

function renderContextChunks(chunks, filter = "") {
  const container = $("#ctx-chunks-list");
  const statsEl = $("#ctx-chunk-stats");
  if (!container) return;
  const q = filter.trim().toLowerCase();
  const filtered = q ? chunks.filter(c => (c.title || "").toLowerCase().includes(q) || (c.text || "").toLowerCase().includes(q) || (c.via || "").toLowerCase().includes(q)) : chunks;
  
  if (statsEl) statsEl.textContent = `Showing ${filtered.length} of ${chunks.length} chunks`;

  if (!filtered.length) {
    container.innerHTML = `<div style="text-align:center;padding:24px;color:var(--muted);font-size:12px;">No matching context chunks found.</div>`;
    return;
  }

  container.innerHTML = filtered.map((c, i) => {
    const via = c.via || "overview";
    const scoreText = c.score != null ? `<span style="font-size:10px;color:var(--muted);margin-left:4px;">(cos ${Math.round(c.score * 1000) / 1000})</span>` : "";
    return `
      <div class="ctx-chunk-card">
        <div class="ctx-chunk-header" onclick="this.nextElementSibling.hidden = !this.nextElementSibling.hidden">
          <div class="ctx-chunk-title">
            <span class="ctx-badge via-${esc(via)}">${esc(via)}</span>
            <span>${esc(c.title || c.id || "document")}</span>
            ${scoreText}
          </div>
          <div style="display:flex;align-items:center;gap:6px;">
            <button class="btn-copy-ctx" type="button" onclick="event.stopPropagation(); navigator.clipboard.writeText(${JSON.stringify(c.text || '')}); this.textContent='✓ Copied!'; setTimeout(() => this.textContent='📋 Copy', 1500)">📋 Copy</button>
            <span style="font-size:11px;color:var(--muted);">▾</span>
          </div>
        </div>
        <div class="ctx-chunk-body">
          <pre class="ctx-chunk-text">${esc(c.text || "(empty chunk)")}</pre>
        </div>
      </div>
    `;
  }).join("");
}

if (ctxChunkFilter) {
  ctxChunkFilter.oninput = () => {
    if (activeContext) {
      renderContextChunks(activeContext.meta?.sources || [], ctxChunkFilter.value);
    }
  };
}

function openContextInspector(ctxId) {
  const ctx = window.CHAT_CONTEXTS?.[ctxId];
  if (!ctx || !contextModal) return;
  activeContext = ctx;

  if (ctxModalQuery) ctxModalQuery.textContent = `Query: "${ctx.query}"`;
  if (ctxModalModel) ctxModalModel.textContent = ctx.meta?.model || ctx.done?.model || "Stream Agent";
  const chunks = ctx.meta?.sources || [];
  if (ctxTabCount) ctxTabCount.textContent = chunks.length;

  // 1. Chunks
  if (ctxChunkFilter) ctxChunkFilter.value = "";
  renderContextChunks(chunks);

  // 2. Live Telemetry Snapshot
  const live = ctx.meta?.live;
  const liveStatsEl = $("#ctx-live-stats");
  const liveDetailsEl = $("#ctx-live-details");
  if (liveStatsEl) {
    const isNominal = !live?.down?.length;
    liveStatsEl.innerHTML = `
      <div class="ctx-stat-card">
        <span class="ctx-stat-val" style="color:var(--accent)">${live?.channels ?? '—'}</span>
        <span class="ctx-stat-lbl">Monitored Channels</span>
      </div>
      <div class="ctx-stat-card">
        <span class="ctx-stat-val" style="color:${live?.down?.length ? '#ef4444' : '#10b981'}">${live?.down?.length ? live.down.length : '0'}</span>
        <span class="ctx-stat-lbl">Down / Failed</span>
      </div>
      <div class="ctx-stat-card">
        <span class="ctx-stat-val" style="color:${live?.onBackup?.length ? '#f59e0b' : '#10b981'}">${live?.onBackup?.length ? live.onBackup.length : '0'}</span>
        <span class="ctx-stat-lbl">Running on Backup</span>
      </div>
      <div class="ctx-stat-card">
        <span class="ctx-stat-val" style="font-size:14px;color:${isNominal ? '#10b981' : '#f59e0b'}">${isNominal ? '✓ NOMINAL' : '⚠️ OUTAGE'}</span>
        <span class="ctx-stat-lbl">Network Verdict</span>
      </div>
    `;
  }
  if (liveDetailsEl) {
    const downDetails = live?.down?.length ? `<div style="background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.25);border-radius:6px;padding:10px;"><b style="color:#ef4444;font-size:12px;">Down Channels:</b><div style="font-family:var(--font-mono);font-size:11px;margin-top:4px;">${esc(live.down.join(", "))}</div></div>` : `<div style="background:rgba(16,185,129,0.08);border:1px solid rgba(16,185,129,0.25);border-radius:6px;padding:8px 12px;font-size:11px;color:#34d399;">✓ All monitored stream channels were reachable and healthy at query time.</div>`;
    const backupDetails = live?.onBackup?.length ? `<div style="background:rgba(245,158,11,0.08);border:1px solid rgba(245,158,11,0.25);border-radius:6px;padding:10px;"><b style="color:#f59e0b;font-size:12px;">Failover (Running on Backup):</b><div style="font-family:var(--font-mono);font-size:11px;margin-top:4px;">${esc(live.onBackup.join(", "))}</div></div>` : "";
    liveDetailsEl.innerHTML = downDetails + backupDetails;
  }

  // 3. Prompt & System
  const sysEl = $("#ctx-system-text");
  const promptEl = $("#ctx-prompt-text");
  if (sysEl) sysEl.textContent = ctx.meta?.system || "Streaming Network Agent Instructions";
  if (promptEl) promptEl.textContent = ctx.meta?.prompt || ctx.query || "(No compiled prompt available)";

  // 4. Tokens & Audit
  const tokenStatsEl = $("#ctx-token-stats");
  const done = ctx.done;
  const inTok = done?.tokens?.input || 0;
  const outTok = done?.tokens?.output || 0;
  const reasonTok = done?.tokens?.reasoning || 0;
  const elapsedSec = done?.elapsed_ms ? (done.elapsed_ms / 1000).toFixed(2) + "s" : "Live";

  if (tokenStatsEl) {
    tokenStatsEl.innerHTML = `
      <div class="ctx-stat-card">
        <span class="ctx-stat-val" style="color:var(--accent)">${elapsedSec}</span>
        <span class="ctx-stat-lbl">Latency / Elapsed</span>
      </div>
      <div class="ctx-stat-card">
        <span class="ctx-stat-val">${inTok.toLocaleString()}</span>
        <span class="ctx-stat-lbl">Input Tokens</span>
      </div>
      <div class="ctx-stat-card">
        <span class="ctx-stat-val">${outTok.toLocaleString()}</span>
        <span class="ctx-stat-lbl">Output Tokens</span>
      </div>
      <div class="ctx-stat-card">
        <span class="ctx-stat-val" style="color:var(--muted)">${reasonTok.toLocaleString()}</span>
        <span class="ctx-stat-lbl">Reasoning Tokens</span>
      </div>
    `;
  }

  const winPct = Math.min(100, Math.round((inTok / 32768) * 100));
  const winPercentEl = $("#ctx-window-percent");
  const winBarEl = $("#ctx-window-bar");
  const winTokEl = $("#ctx-window-tokens");
  if (winPercentEl) winPercentEl.textContent = `${winPct}%`;
  if (winBarEl) winBarEl.style.width = `${winPct}%`;
  if (winTokEl) winTokEl.textContent = `${inTok.toLocaleString()} / 32,768 tokens`;

  // Default to Chunks tab
  $$(".ctx-tabs-bar .tab-btn").forEach((b, i) => b.classList.toggle("active", i === 0));
  $$(".ctx-panel").forEach((p, i) => p.hidden = i !== 0);

  contextModal.hidden = false;
}

// Delegated click listener for View Context button on messages
document.addEventListener("click", e => {
  const btn = e.target.closest(".btn-inspect-context");
  if (btn && btn.dataset.ctxId) {
    e.preventDefault();
    openContextInspector(btn.dataset.ctxId);
  }
  const copyBtn = e.target.closest(".btn-copy-ctx[data-copy-target]");
  if (copyBtn) {
    const target = $(`#${copyBtn.dataset.copyTarget}`);
    if (target) {
      navigator.clipboard.writeText(target.textContent || "");
      const prev = copyBtn.textContent;
      copyBtn.textContent = "✓ Copied!";
      setTimeout(() => copyBtn.textContent = prev, 1500);
    }
  }
});

