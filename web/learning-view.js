"use strict";

// ---------- Learning: everything the agent learned (learning.py, GET /api/learning?full=true) ----------
// Patterns counted from past outages, facts the operator taught it, root-cause weights from Yes/No verdicts,
// corrections it follows, every closed outage it remembers (with the fix, which can be added here), and the
// latest feedback.

const learnView = {data: null, fixing: null};

const PROBLEM_WORDS = {
  STALE_MEDIA: "Video stopped updating", PLAYLIST_MISSING: "Playlist missing", UNREACHABLE: "Server unreachable",
  SERVER_ERROR: "Server error", HTTP_ERROR: "HTTP error", NO_SEGMENTS: "No video pieces",
  INVALID_PLAYLIST: "Broken playlist",
};
const FAULT_WORDS = {
  SHARED_UPSTREAM: "Every input failing: a shared source", LOCAL_TO_NODE: "Only this server",
  SINGLE_INPUT: "The channel's only input", TRANSCODER: "The transcoder", UPSTREAM: "Further upstream",
  FINAL_ORIGIN: "The final output server",
};
const PATTERN_KIND = {history: "History", time_of_day: "Time of day", recurrence: "Repeats", together: "Fail together"};

const learnShort = n => String(n || "").replace(".ottlive.co.in", "");

function learnWhen(iso) {
  const d = new Date(String(iso || "").replace(/(\.\d{3})\d+/, "$1"));
  if (isNaN(d)) return "—";
  return d.toLocaleString("en-IN", {timeZone: "Asia/Kolkata", day: "2-digit", month: "short", hour: "2-digit",
                                    minute: "2-digit", hour12: false}) + " IST";
}

function learnLasted(s) {
  if (s == null) return "—";
  return s < 90 ? `${s} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`;
}

const learnEmpty = (cols, text) => `<tr><td colspan="${cols}" class="learn-empty">${esc(text)}</td></tr>`;

function renderLearning() {
  const d = learnView.data;
  if (!d) return;
  const fb = d.feedback;
  const verdicts = fb.root_cause.up + fb.root_cause.down;
  const stats = [
    [d.caseCount, "past outages remembered"],
    [d.patterns.length, "patterns found"],
    [d.facts.length, "facts you taught it"],
    [d.lessons.length, "corrections it follows"],
    [verdicts, "root-cause verdicts"],
    [`👍 ${fb.answer.up} · 👎 ${fb.answer.down}`, "answer ratings"],
    [fb.answer.rephrased || 0, "questions you had to ask again"],
  ];
  $("#learn-stats").innerHTML = stats.map(([n, label]) =>
    `<div class="learn-stat"><span class="learn-stat-n">${esc(n)}</span><span class="learn-stat-l">${esc(label)}</span></div>`).join("");
  $$("[data-setting]").forEach(el => { el.textContent = d.settings[el.dataset.setting]; });

  $("#learn-patterns").innerHTML = d.patterns.length
    ? d.patterns.map(p => `<li><span class="learn-tag">${esc(PATTERN_KIND[p.kind] || p.kind)}</span><span>${esc(p.text)}</span></li>`).join("")
    : `<li class="learn-empty">None yet. Patterns appear once a server has had at least ${d.settings.minPattern} outages.</li>`;

  $("#learn-facts").innerHTML = d.facts.length
    ? d.facts.map(f => `<li><span>${esc(f.text)}${f.nodes.length ? ` <span class="learn-sub">about ${esc(f.nodes.map(learnShort).join(", "))}</span>` : ""}
        <span class="learn-sub">· ${esc(learnWhen(f.ts))}</span></span>
        <button type="button" class="learn-del" data-fact="${f.id}" title="Forget" aria-label="Forget: ${esc(f.text)}">✕</button></li>`).join("")
    : `<li class="learn-empty">Nothing yet.</li>`;

  $("#learn-weights tbody").innerHTML = d.weights.length
    ? d.weights.map(w => `<tr><td>${esc(w.node)}</td><td>${w.right}</td><td>${w.wrong}</td>
        <td><b class="${w.factor > 1 ? "learn-up" : w.factor < 1 ? "learn-down" : ""}">×${w.factor}</b>
        <span class="learn-sub">${w.factor > 1 ? "ranked higher" : w.factor < 1 ? "ranked lower" : "unchanged"}</span></td></tr>`).join("")
    : learnEmpty(4, "No verdicts yet. Answer “Is this really the root cause?” on the Agent page during an outage.");

  const segs = (d.segmentAlerts || []).filter(a => a.baseline);
  const chanOf = (node, url) => ((byId?.[node]?.links || []).find(l => l.url === url) || {}).channel;
  $("#learn-segments tbody").innerHTML = segs.length
    ? segs.map(a => `<tr class="${a.active ? "learn-warn-row" : ""}">
        <td>${esc(learnShort(a.node))}<div class="learn-sub">${esc(chanOf(a.node, a.url) || a.url.replace(/^https?:\/\/[^/]+/, ""))}</div></td>
        <td class="learn-nowrap">${a.baseline.median} s${a.baseline.spread ? ` <span class="learn-sub">±${a.baseline.spread}</span>` : ""}</td>
        <td class="learn-nowrap"><b>${a.warn} s</b></td>
        <td class="learn-nowrap">${a.age == null ? "—" : `${Math.round(a.age)} s`}${a.active ? ` <span class="learn-tag-warn">warning</span>` : ""}</td>
        <td>${a.k}</td>
        <td>${a.baseline.samples.toLocaleString()} checks · ${a.baseline.days < 1 ? `${Math.max(1, Math.round(a.baseline.days * 24))} h` : `${a.baseline.days} days`}${a.baseline.byHour ? " · this hour" : ""}</td>
        <td>${a.raised ? `${a.raised}: ${a.clearedAlone} settled, ${a.beforeOutage} before outage${a.tooSensitive ? `, ${a.tooSensitive} too sensitive` : ""}` : "none"}</td>
        <td><button type="button" class="learn-link" data-loosen="${esc(a.url)}">Too sensitive</button></td></tr>`).join("")
    : learnEmpty(8, "Still learning: each stream needs about 20 healthy checks first.");

  $("#learn-lessons tbody").innerHTML = d.lessons.length
    ? d.lessons.map(l => `<tr><td>${esc(learnWhen(l.ts))}</td><td>${esc(l.question || "")}</td><td>${esc(l.correction)}</td></tr>`).join("")
    : learnEmpty(3, "None yet. Mark an answer 👎 and type the right answer.");

  $("#learn-cases tbody").innerHTML = d.cases.length
    ? d.cases.map((c, i) => {
      const root = c.root_cause && c.root_cause !== c.node ? esc(c.root_cause) : c.root_cause ? "Itself" : "—";
      const fault = (c.verdict || "").split(", ").filter(Boolean).map(v => FAULT_WORDS[v] || v).join("; ") || "—";
      const fix = learnView.fixing === i
        ? `<form class="learn-fix" data-case="${i}"><input type="text" maxlength="1000" aria-label="What fixed it"
             placeholder="What fixed it?" value="${esc(c.resolution || "")}"><button class="btn small primary">Save</button></form>`
        : c.resolution
          ? `${esc(c.resolution)} <button type="button" class="learn-link" data-fix="${i}">Edit</button>`
          : `<button type="button" class="learn-link" data-fix="${i}">＋ Add fix</button>`;
      return `<tr><td class="learn-nowrap">${esc(learnWhen(c.onset || c.opened))}</td><td>${esc(learnShort(c.node))}</td>
        <td>${esc((c.channels || []).join(", ") || "—")}</td><td>${esc(PROBLEM_WORDS[c.category] || c.category || "Down")}</td>
        <td>${esc(fault)}</td><td>${root}</td><td class="learn-nowrap">${esc(learnLasted(c.duration_s))}</td><td>${fix}</td></tr>`;
    }).join("")
    : learnEmpty(8, "No outage has ended yet.");

  $("#learn-feedback tbody").innerHTML = d.recentFeedback.length
    ? d.recentFeedback.map(f => `<tr><td>${esc(learnWhen(f.ts))}</td>
        <td>${f.kind === "answer" ? (f.source === "implicit" ? "Asked again" : "Answer") : "Root cause"}</td><td>${f.rating > 0 ? "👍" : "👎"}</td>
        <td>${f.kind === "answer"
          ? `${esc(f.question || "")}${f.correction ? ` <span class="learn-sub">→ ${esc(f.correction)}</span>` : ""}${(f.tools || []).length ? ` <span class="learn-sub">· used ${esc(f.tools.join(", "))}</span>` : ""}`
          : `${esc(f.node)} ${f.rating > 0 ? "was" : "was not"} the root cause`}</td></tr>`).join("")
    : learnEmpty(4, "No feedback yet. Use 👍/👎 under the agent's answers.");

  const form = $("#learn-cases form.learn-fix input");
  if (form) form.focus();
}

function renderAdPatterns(channels) {
  const withData = channels.filter(c => c.breaks7d || c.issues.length);
  $("#learn-ads").innerHTML = withData.length ? withData.map(c => `<li><span class="learn-tag">${esc(c.channel || c.node)}</span>
      <span>${esc(c.lines.join(" "))}${c.issues.length ? ` <b class="learn-down">${esc(c.issues.join(". "))}.</b>` : ""}</span></li>`).join("")
    : `<li class="learn-empty">No SCTE-35 ad-break markers seen on any channel in the last 7 days. Patterns appear once
        a channel has had a few breaks.</li>`;
}

function renderModel(m) {
  const box = $("#learn-model");
  if (!m) { box.innerHTML = `<p class="learn-empty">Not trained yet: the first training runs within an hour of starting.</p>`; return; }
  const pct = v => v == null ? "—" : `${Math.round(v * 100)}%`;
  box.innerHTML = `
    <div class="learn-model-state ${m.active ? "on" : "off"}">${m.active ? "In use" : "Learning, not in use yet"}</div>
    <p>${esc(m.note || "")}</p>
    <dl class="learn-model-stats">
      <div><dt>History</dt><dd>${m.history_days ?? "—"} days</dd></div>
      <div><dt>Examples</dt><dd>${(m.samples || 0).toLocaleString()} (${(m.positives || 0).toLocaleString()} glitches/failures)</dd></div>
      <div><dt>Accuracy on unseen data (AUC)</dt><dd>${m.auc ?? "—"} <span class="learn-sub">0.5 = guessing</span></dd></div>
      <div><dt>Best single sign</dt><dd>${esc(m.best_single || "—")} ${m.best_single_auc ? `(${m.best_single_auc})` : ""}</dd></div>
      <div><dt>When it says "likely"</dt><dd>right ${pct(m.precision)} of the time, catches ${pct(m.recall)}</dd></div>
      <div><dt>Trained</dt><dd>${esc(learnWhen(new Date(m.trained_at * 1000).toISOString()))}</dd></div>
    </dl>
    ${(m.topFactors || []).length ? `<p class="learn-sub">What it weighs most: ${m.topFactors.map(f => `${esc(f.factor)} (${f.weight > 0 ? "raises" : "lowers"} the risk)`).join(", ")}.</p>` : ""}`;
}

// The chatbot's exam (chat_eval.py): start a run, follow its progress, and show the latest results.
let evalTimer = null;
function renderEval(st) {
  const box = $("#learn-eval");
  const [last, prev] = st.runs;
  const running = st.running;
  const delta = last && prev ? last.passed / last.total - prev.passed / prev.total : null;
  const head = running
    ? `<p><b>Running…</b> ${running.done} of ${running.total} questions asked, ${running.passed} passed so far.</p>`
    : last ? `<p><b>${last.passed} of ${last.total} passed</b> (${Math.round(100 * last.passed / last.total)}%)
        ${delta != null ? `<span class="${delta < 0 ? "learn-down" : "learn-sub"}">${delta >= 0 ? "▲" : "▼"} ${Math.abs(Math.round(delta * 100))} points since the run before</span>` : ""}
        <span class="learn-sub">· ${esc(learnWhen(new Date(last.ts * 1000).toISOString()))} · ${esc(last.model || "")} · ${last.seconds} s · ${(last.tokens || 0).toLocaleString()} tokens</span></p>`
    : `<p class="learn-empty">Never run. ${st.cases} questions are ready${st.corrections ? `, plus ${st.corrections} from your corrections` : ""}.</p>`;
  const failed = (last?.results || []).filter(r => !r.passed);
  box.innerHTML = `${head}
    <button type="button" class="btn small primary" id="learn-eval-run"${running ? " disabled" : ""}>${running ? "Running…" : "Run exam"}</button>
    ${!running && failed.length ? `<table class="grid learn-eval-fails"><thead><tr><th>Question</th><th>What went wrong</th><th>Tools used</th></tr></thead><tbody>
      ${failed.map(r => `<tr><td>${esc(r.question)}${r.source === "correction" ? ` <span class="learn-sub">(your correction)</span>` : ""}</td>
        <td>${esc(r.problems.join("; "))}</td><td>${esc(r.tools.join(", ") || "—")}</td></tr>`).join("")}</tbody></table>` : ""}`;
  clearTimeout(evalTimer);
  if (running) evalTimer = setTimeout(loadEval, 3000);
}
function loadEval() {
  api("GET", "/api/chat/eval").then(r => renderEval(r.data)).catch(err => {
    $("#learn-eval").innerHTML = `<p class="learn-empty">Couldn't load the exam: ${esc(err.message)}</p>`;
  });
}
$("#learn-eval").addEventListener("click", async e => {
  if (!e.target.closest("#learn-eval-run")) return;
  e.target.disabled = true;
  await api("POST", "/api/chat/eval", {}).catch(() => {});
  loadEval();
});

// Voice alerts (voice.py): what's stored, which voice wins each mood, recent lines to replay and rate.
const MOOD_LABEL = {calm: "Calm heads-up", urgent: "Urgent", angry: "Angry (still down)", furious: "Furious (keeps failing)", relieved: "Relieved"};
function renderVoice(v) {
  const box = $("#learn-voice");
  const moods = Object.entries(v.styles).map(([mood, st]) => {
    const ranked = [...st.voices].sort((a, b) => b.score - a.score);
    const moodTo = v.people.moods[mood];
    return `<tr><td><b>${esc(MOOD_LABEL[mood] || mood)}</b>
        <button type="button" class="voice-to" data-mood-person="${esc(mood)}" title="Shown as this person whatever the voice (click to change)">${moodTo ? `shown as ${esc(moodTo)}` : "shown by voice"}</button>
</td>
      <td>${ranked.map((x, i) => `<button type="button" class="voice-chip${i === 0 && x.score > 0.5 ? " lead" : ""}" data-voice-person="${esc(x.speaker)}"
          title="Recorded voice '${esc(x.speaker)}' · ${x.plays} plays · ${x.up} 👍 · ${x.down} 👎 · click to rename">${esc(moodTo || v.people.voices[x.speaker] || "Bot")} <small class="voice-real">${esc(x.speaker)}</small> <small>${Math.round(x.score * 100)}%</small></button>`).join("")}</td></tr>`;
  }).join("");
  const recent = v.recent.map(r => `<tr>
      <td class="learn-nowrap">${esc(learnWhen(new Date(r.ts * 1000).toISOString()))}</td>
      <td><span class="voice-mood voice-${esc(r.style)}">${esc(r.style)}</span> ${esc(r.name || r.speaker || "")}</td>
      <td>${r.clip_id ? `<button type="button" class="btn small" data-voice-play="${r.clip_id}" aria-label="Play">▶</button> ` : ""}${esc(r.text || "(browser voice)")}</td>
      <td class="learn-nowrap">${r.rating === 1 ? "👍" : r.rating === -1 ? "👎" : `<button type="button" class="fb-btn" data-voice-rate="1" data-play="${r.id}" aria-label="Sounded human">👍</button><button type="button" class="fb-btn" data-voice-rate="-1" data-play="${r.id}" aria-label="Sounded off">👎</button>`}
        ${r.acked_after_s != null ? `<div class="learn-sub">reacted in ${Math.round(r.acked_after_s)} s</div>` : ""}</td></tr>`).join("");
  const lib = v.library;
  const libHtml = `
    <div class="voice-lib">
      <div class="voice-lib-head"><b>Voice library</b>
        <span class="learn-sub">${lib.phrases} recorded phrases · ${lib.minutes} min · ${lib.megabytes} MB. Alerts are stitched from these: no API calls. Anything not recorded is read by the browser voice.</span></div>
      <div class="voice-lib-grid">${lib.voices.map(p => `<span class="voice-chip${p.phrases ? " lead" : ""}" title="${p.phrases} phrases">${esc(MOOD_LABEL[p.mood] || p.mood)} · ${esc(v.people.moods[p.mood] || v.people.voices[p.voice] || "Bot")} <small class="voice-real">${esc(p.voice)}</small> <small>${p.phrases}</small></span>`).join("")}</div>
    </div>`;
  box.innerHTML = libHtml + `
    <div class="voice-top">
      <span><b>${v.clips}</b> clips · <b>${v.audioMinutes}</b> min of audio · ${v.megabytes} MB</span>
      <span>${v.plays} played · 👍 ${v.up} · 👎 ${v.down}${v.avgAckS != null ? ` · you react in ~${Math.round(v.avgAckS)} s` : ""}</span>
      <label class="voice-swear">Swearing on high alerts
        <select id="voice-profanity" aria-label="Swearing on high alerts">
          ${[["off", "Off"], ["mild", "Mild (bakwaas, dhakkan…)"], ["strong", "Strong (saala, ghanta…)"]].map(([k, l]) =>
            `<option value="${k}"${v.profanity === k ? " selected" : ""}>${l}</option>`).join("")}
        </select></label>
      <span class="learn-sub">calls you <b>${esc(v.operator)}</b></span>
      <span class="voice-actions">
        <button type="button" class="btn small" data-voice-test="urgent">▶ Hear urgent</button>
        <button type="button" class="btn small" data-voice-test="furious">▶ Hear furious</button>
        <a class="btn small primary" href="/api/voice/dataset.zip" download>⬇ Dataset (all)</a>
        <a class="btn small" href="/api/voice/dataset.zip?min_rating=1" download>⬇ Only 👍</a>
      </span>
    </div>
    <table class="grid"><thead><tr><th style="width:190px">Mood</th><th>Voices, best first (chance it works)</th></tr></thead><tbody>${moods}</tbody></table>
    ${recent ? `<h4 class="voice-h">Recent lines</h4><div class="learn-scroll"><table class="grid"><thead><tr><th style="width:140px">When</th><th style="width:140px">Mood · voice</th><th>Line</th><th style="width:110px">Human?</th></tr></thead><tbody>${recent}</tbody></table></div>` : `<p class="learn-empty">Nothing spoken yet.</p>`}`;
}
let voiceTimer = null;
function loadVoice() {
  api("GET", "/api/voice/stats").then(r => {
    renderVoice(r.data);
    clearTimeout(voiceTimer);
  }).catch(err => {
    $("#learn-voice").innerHTML = `<p class="learn-empty">Couldn't load the voice data: ${esc(err.message)}</p>`;
  });
}
let voicePreview = null;
$("#learn-voice").addEventListener("change", async e => {
  if (e.target.id !== "voice-profanity") return;
  await api("POST", "/api/voice/settings", {profanity: e.target.value}).catch(() => {});
  loadVoice();
});
$("#learn-voice").addEventListener("click", async e => {
  const who = e.target.closest("[data-voice-person], [data-mood-person]");
  if (who) {
    const isMood = !!who.dataset.moodPerson;
    const key = isMood ? who.dataset.moodPerson : who.dataset.voicePerson;
    const name = window.prompt(isMood ? `Show every "${MOOD_LABEL[key] || key}" alert as spoken by: (empty: the voice's own name)`
      : `Show the voice "${key}" as: (empty: Bot)`, "");
    if (name === null) return;
    await api("POST", "/api/voice/settings", isMood ? {moods: {[key]: name}} : {voices: {[key]: name}}).catch(() => {});
    loadVoice();
    return;
  }
  const play = e.target.closest("[data-voice-play]");
  if (play) { if (voicePreview) voicePreview.pause(); voicePreview = new Audio(`/api/voice/clips/${play.dataset.voicePlay}`); voicePreview.play().catch(() => {}); return; }
  const rate = e.target.closest("[data-voice-rate]");
  if (rate) { await api("POST", `/api/voice/plays/${rate.dataset.play}`, {rating: Number(rate.dataset.voiceRate)}).catch(() => {}); loadVoice(); return; }
  const test = e.target.closest("[data-voice-test]");
  if (test) {
    test.disabled = true;
    const sev = test.dataset.voiceTest === "furious" ? "AGGRESSIVE" : "CRITICAL";
    try {
      const {data} = await api("POST", "/api/voice/alert", {severity: sev, style: test.dataset.voiceTest, channels: ["Rang Manch"],
        server: "xcode4.ottlive.co.in", title: "Stream stopped updating", minutes: test.dataset.voiceTest === "furious" ? 25 : 1,
        subject: `preview:${test.dataset.voiceTest}`});
      if (data.audio_url) { if (voicePreview) voicePreview.pause(); voicePreview = new Audio(data.audio_url); voicePreview.play().catch(() => {}); }
      loadVoice();
    } finally { test.disabled = false; }
  }
});

async function loadLearning() {
  loadEval();
  loadVoice();
  api("GET", "/api/scte").then(r => renderAdPatterns(r.data.channels || [])).catch(() => {});
  api("GET", "/api/glitches?hours=1").then(r => renderModel(r.data.trainedModel)).catch(() => {});
  try {
    learnView.data = (await api("GET", "/api/learning?full=true")).data;
    renderLearning();
  } catch (err) {
    $("#learn-stats").innerHTML = `<p class="learn-empty">Couldn't load what the agent learned: ${esc(err.message)}</p>`;
  }
}

$("#learn-refresh").onclick = loadLearning;

$("#learn-fact-form").addEventListener("submit", async e => {
  e.preventDefault();
  const input = $("#learn-fact-input"), text = input.value.trim();
  if (text.length < 3) return;
  await api("POST", "/api/learning/facts", {text});
  input.value = "";
  loadLearning();
});

$("#view-learning").addEventListener("click", async e => {
  const del = e.target.closest("[data-fact]");
  if (del) { await api("DELETE", `/api/learning/facts/${del.dataset.fact}`); loadLearning(); return; }
  if (e.target.closest("[data-loosen]")) { setTimeout(loadLearning, 400); return; }  // handled in node-detail.js
  const fix = e.target.closest("[data-fix]");
  if (fix) { learnView.fixing = Number(fix.dataset.fix); renderLearning(); }
});

$("#view-learning").addEventListener("submit", async e => {
  const form = e.target.closest("form.learn-fix");
  if (!form) return;
  e.preventDefault();
  const c = learnView.data.cases[Number(form.dataset.case)];
  const resolution = form.querySelector("input").value.trim();
  if (resolution.length < 3) return;
  await api("POST", "/api/learning/resolution", {node: c.node, opened: c.opened, resolution});
  learnView.fixing = null;
  loadLearning();
});
