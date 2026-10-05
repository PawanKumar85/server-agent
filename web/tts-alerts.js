"use strict";

// ==============================================================================
// Final Node Voice Alerts (Indian Hinglish Text-To-Speech & Auto-Adjust Engine)
// ==============================================================================
// Features:
// 1. Strictly monitors Final Nodes (FinalLink) - the last stage before viewers.
// 2. Anti-Looping Engine: Warnings are announced strictly ONCE. No repeated audio loops.
// 3. Smart Auto-Adjust (Aggregated NOC Mode):
//    - If multiple channels have alerts at the same time, auto-adjusts into ONE
//      consolidated summary (e.g. "3 Final nodes par alerts hain...") instead of
//      spamming individual announcements one after another.
//    - Auto-adjusts pitch, rate, and volume based on alert severity.
// 4. Genuine Indian Tone & Voice:
//    - Dynamically catches 'onvoiceschanged' to load native Indian voices.
//    - Priority: Google हिन्दी, Google English (India), Lekha, Rishi, Veena, Aman, Tara,
//      Microsoft Kalpana, Microsoft Heera, or any hi-IN / en-IN voice.
//    - Explicitly sets utter.lang = 'hi-IN' or 'en-IN' for natural Indian phonetics.
//    - Indian broadcast 3-tone chime via Web Audio API.
// 5. Auto-Acknowledge:
//    - Clicking a card or opening node details marks the alert as acknowledged.
// ==============================================================================

const ttsAlerts = {
  enabled: true,
  targetLanguage: "hi-IN",    // Target Language: Hindi
  autoAdjust: true,           // Smart auto-adjustment enabled
  criticalCooldownMs: 300000, // 5 min cooldown for ongoing critical down alerts
  warnCooldownMs: Infinity,   // Warnings NEVER loop! Spoken strictly once.
  state: {},                  // key -> { severity, title, text, spokenAt, acknowledged }
  speaking: false,
  audioContext: null,
  audioUnlocked: false,
  voices: [],
  selectedVoiceName: "",
  evaluationTimer: null,
  activeUtterance: null,
  speechStartTime: 0
};

// --- Storage & Initialization ---
(function initTtsSettings() {
  try {
    const saved = localStorage.getItem("stream_graph_tts_hinglish");
    if (saved !== null) {
      ttsAlerts.enabled = saved === "true";
    } else {
      ttsAlerts.enabled = true;
    }
  } catch (e) {
    ttsAlerts.enabled = true;
  }
  // Immediately load dynamic learned hold-down and cascading policies
  if (typeof ttsAlerts.fetchDynamicPolicy === "function") {
    ttsAlerts.fetchDynamicPolicy();
  }
  setInterval(() => {
    if (typeof ttsAlerts.fetchDynamicPolicy === "function") {
      ttsAlerts.fetchDynamicPolicy();
    }
  }, 60000);
})();

// --- Voice Discovery & Hindi Voice Selection ---
function loadAvailableVoices() {
  if (!window.speechSynthesis) return;
  const list = window.speechSynthesis.getVoices() || [];
  if (!list.length) return;
  ttsAlerts.voices = list;

  // Find preferred Hindi voice (Target Language: Hindi)
  const hindiVoice = findHindiVoice(list);
  if (hindiVoice) {
    ttsAlerts.selectedVoiceName = hindiVoice.name;
  }
}

function findHindiVoice(voices) {
  if (!voices || !voices.length) return null;

  // 0. The most natural Hindi voices first: neural / enhanced / premium / online ones sound far less robotic.
  const hindi = voices.filter(x => (x.lang || "").toLowerCase().replace("_", "-").startsWith("hi"));
  let v = hindi.find(x => /natural|neural|enhanced|premium|online/i.test(x.name)) || hindi.find(x => x.name === "Google हिन्दी");
  if (v) return v;
  // 1. Dedicated Hindi Voices (Lekha, Kalpana, Hemant)
  v = voices.find(x => /^(Lekha|Kalpana|Hemant)/i.test(x.name));
  if (v) return v;

  // 2. Language tag matching Hindi (hi-IN, hi_IN, or starting with hi)
  v = voices.find(x => x.lang && (x.lang === "hi-IN" || x.lang === "hi_IN" || x.lang.toLowerCase().startsWith("hi")));
  if (v) return v;

  // 3. Name contains Hindi / हिन्दी
  v = voices.find(x => /hindi|हिन्दी/i.test(x.name));
  if (v) return v;

  // 4. Secondary Indian voices with Hindi support (Rishi, Veena, Aman, Tara, Google English India)
  v = voices.find(x => /^(Rishi|Veena|Aman|Tara)$/i.test(x.name) || x.name === "Google English (India)");
  if (v) return v;

  v = voices.find(x => x.lang && (x.lang === "en-IN" || x.lang === "en_IN"));
  if (v) return v;

  v = voices.find(x => /india/i.test(x.name));
  if (v) return v;

  // Fallback to default
  return voices.find(x => x.default) || voices[0];
}

function getActiveVoice() {
  if (!ttsAlerts.voices.length && window.speechSynthesis) {
    ttsAlerts.voices = window.speechSynthesis.getVoices() || [];
  }
  if (ttsAlerts.selectedVoiceName) {
    const match = ttsAlerts.voices.find(v => v.name === ttsAlerts.selectedVoiceName);
    if (match) return match;
  }
  return findHindiVoice(ttsAlerts.voices);
}

if (window.speechSynthesis) {
  loadAvailableVoices();
  window.speechSynthesis.onvoiceschanged = loadAvailableVoices;
}

// Unlock Web Audio & Speech on first user interaction
function unlockAudioOnInteraction() {
  if (ttsAlerts.audioUnlocked) return;
  ttsAlerts.audioUnlocked = true;
  try {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (AudioCtx && !ttsAlerts.audioContext) {
      ttsAlerts.audioContext = new AudioCtx();
    }
    if (ttsAlerts.audioContext && ttsAlerts.audioContext.state === "suspended") {
      ttsAlerts.audioContext.resume();
    }
  } catch (err) {
    console.debug("[TTS] Audio unlock notice:", err);
  }
}
["click", "keydown", "touchstart"].forEach(evt => {
  document.addEventListener(evt, unlockAudioOnInteraction, { once: true, passive: true });
});

// Auto-acknowledge when clicking any node or card
document.addEventListener("click", (e) => {
  const nodeEl = e.target.closest("[data-node]");
  if (nodeEl && nodeEl.dataset.node) {
    ttsAlerts.acknowledge(nodeEl.dataset.node);
  }
}, true);

// --- Helpers ---
function isFinalNode(nodeId, url) {
  if (!nodeId) return false;
  const n = (typeof byId !== "undefined" && byId[nodeId])
    || ((typeof G !== "undefined" && Array.isArray(G.nodes)) ? G.nodes.find(x => x.id === nodeId) : null);
  if (!n) return false;

  if (Array.isArray(n.labels) && n.labels.includes("FinalLink")) return true;
  if (Array.isArray(n.roles) && n.roles.includes("FinalLink")) return true;
  if (Array.isArray(n.links)) {
    if (url && n.links.some(l => l.url === url && l.role === "FinalLink")) return true;
    if (!url && n.links.some(l => l.role === "FinalLink")) return true;
  }
  if (typeof G !== "undefined" && Array.isArray(G.spiders)) {
    if (G.spiders.some(s => s.finalLinkId === nodeId)) return true;
  }
  return false;
}

function getFinalChannelName(nodeId, url) {
  const n = (typeof byId !== "undefined" && byId[nodeId])
    || ((typeof G !== "undefined" && Array.isArray(G.nodes)) ? G.nodes.find(x => x.id === nodeId) : null);
  if (n && Array.isArray(n.links)) {
    if (url) {
      const match = n.links.find(l => l.url === url && l.channel);
      if (match) return match.channel;
    }
    const finalLink = n.links.find(l => l.role === "FinalLink" && l.channel);
    if (finalLink) return finalLink.channel;
  }
  if (typeof spiderName === "function") return spiderName(nodeId);
  return String(nodeId).split(".")[0];
}

function getNodeServer(nodeId, url) {
  if (url) {
    try {
      const u = new URL(url);
      if (u.hostname) return u.hostname;
    } catch (e) { }
  }
  if (nodeId && typeof nodeId === "string") {
    if (nodeId.includes("/")) return nodeId.split("/")[0];
    return nodeId;
  }
  return "server";
}

// --- Indian Melodic Broadcast Chime via Web Audio API ---
function playIndianBroadcastChime(severity, serverName = null) {
  try {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return;
    if (!ttsAlerts.audioContext) ttsAlerts.audioContext = new AudioCtx();
    const ctx = ttsAlerts.audioContext;
    if (ctx.state === "suspended") ctx.resume();

    const now = ctx.currentTime;
    const profile = serverName ? ttsAlerts.getNodeProfile(serverName) : null;
    const tuning = profile?.audio_tuning;
    const tod = ttsAlerts.dynamicPolicy?.time_of_day || {};

    // Dynamic Chime Notes learned from incident MTTR & failure profile:
    let notes = [369.99, 440.00, 554.37];
    let noteVol = 0.16;
    let step = 0.11;

    if (tuning?.siren_notes && Array.isArray(tuning.siren_notes) && tuning.siren_notes.length > 0) {
      notes = tuning.siren_notes;
      noteVol = tuning.chime_vol || 0.18;
      step = (tuning.speech_rate && tuning.speech_rate > 1.15) ? 0.08 : 0.11;
    } else if (severity === "CRITICAL") {
      notes = [554.37, 440.00, 369.99];
      noteVol = 0.22;
      step = 0.11;
    } else if (severity === "AGGRESSIVE") {
      notes = [659.25, 659.25, 880.00];
      noteVol = 0.26;
      step = 0.08;
    }

    // Apply Time-of-Day Attention Boost (e.g. night shift 02:00-06:00 IST)
    if (tod.attention_chime_boost) {
      noteVol = Math.min(0.35, noteVol + tod.attention_chime_boost);
    }

    notes.forEach((freq, idx) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.setValueAtTime(freq, now + idx * step);

      gain.gain.setValueAtTime(0.001, now + idx * step);
      gain.gain.exponentialRampToValueAtTime(noteVol, now + idx * step + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.001, now + idx * step + (step * 2.2));

      osc.connect(gain);
      gain.connect(ctx.destination);

      osc.start(now + idx * step);
      osc.stop(now + idx * step + (step * 2.4));
    });
  } catch (err) {
    console.debug("[TTS] Chime bypass:", err);
  }
}

// --- Rotation Memory for Dynamic Variety & Anti-Repetition ---
ttsAlerts.rotationCounter = ttsAlerts.rotationCounter || {};

function getNextRotation(key, max) {
  ttsAlerts.rotationCounter = ttsAlerts.rotationCounter || {};
  const cur = ttsAlerts.rotationCounter[key] || 0;
  ttsAlerts.rotationCounter[key] = (cur + 1) % max;
  return cur;
}

// --- Hinglish Phrasing Generator with Dynamic AI Switching & Past Data Variety ---
function buildConsolidatedHinglishMessage(criticals, warnings) {
  // CRITICAL ALERT: Order dene wale tone me with "Anushrav Sir - " prefix
  if (criticals.length > 1) {
    const names = criticals.slice(0, 3).map(c => c.channel).join(", ");
    const more = criticals.length > 3 ? ` aur ${criticals.length - 3} anya channels` : "";
    const idx = getNextRotation("crit:multi", 3);
    const multiCritVariants = [
      `Anushrav Sir - ${criticals.length} Final completely down hain! ${names}${more} band ho chuke hain! MCR team, turant action lo aur streaming abhi restore karo! Delay bilkul mat karo!`,
      `Anushrav Sir - Major multi-channel outage! ${criticals.length} channels offline hain: ${names}${more}. Turant encoders check karke restore kijiye!`,
      `Anushrav Sir - High priority emergency! ${names}${more} par zero video chunks aa rahe hain. MCR team immediate failover initiate karo!`
    ];
    return multiCritVariants[idx];
  }

  if (criticals.length === 1) {
    const c = criticals[0];
    const ch = c.channel;
    const title = String(c.alertObj?.title || "");
    const text = String(c.alertObj?.text || "");

    let detail = "stream down ho chuki hai";
    if (title.includes("404") || text.includes("404") || title.includes("missing")) {
      detail = "ingest manifest missing hai aur 404 error aa raha hai. Live encoder push abhi restart karo";
    } else if (title.includes("stopped updating") || text.includes("old") || text.includes("STALE")) {
      detail = "video pieces update hona band ho gaye hain aur stream freeze ho chuki hai";
    } else if (title.includes("unreachable")) {
      detail = "server unreachable hai aur network connection fail ho gaya hai";
    } else if (title.includes("Upstream")) {
      detail = "upstream feed fail ho chuka hai";
    }

    const idx = getNextRotation(`crit:${ch}`, 4);
    const critVariants = [
      `Anushrav Sir - Final ${ch} down ho chuka hai! ${detail}! Turant encoder check karo aur stream restart karo! Delay bilkul mat karo!`,
      `Anushrav Sir - Emergency alert! Final node ${ch} completely blackout ho gaya hai. ${detail}. MCR team, stream abhi restore karo!`,
      `Anushrav Sir - Final ${ch} offline chala gaya hai! ${detail}. Encoders check karke broadcast abhi live lijiye!`,
      `Anushrav Sir - Attention! Final ${ch} feed abruptly band ho chuki hai. Upstream encoder restart karein bina delay ke!`
    ];
    return critVariants[idx];
  }

  // WARNING: Request tone me (Polite, courteous request with rotational variety)
  if (warnings.length > 1) {
    const names = warnings.slice(0, 3).map(w => w.channel).join(", ");
    const idx = getNextRotation("warn:multi", 3);
    const multiWarnVariants = [
      `Anushrav Sir ji Kripya dhyan dijiye. ${warnings.length} Final channels par stream performance degrade ho rahi hai: ${names}. Aapse request hai ki please ek baar verify kar lijiye taaki viewers ko buffering na dekhni pade.`,
      `Anushrav Sir ji, Cluster alert: ${warnings.length} channels par playback jitter observe hua hai (${names}). Kripya upstream encoders inspect kar lijiye.`,
      `Anushrav Sir ji, Multiple channels delay notice: ${names} live playback se slow chal rahe hain. Aapse request hai ki please check kar lijiye.`
    ];
    return multiWarnVariants[idx];
  }

  if (warnings.length === 1) {
    const w = warnings[0];
    const ch = w.channel;
    const title = String(w.alertObj?.title || "");
    const text = String(w.alertObj?.text || "");

    let detail = "stream mein thodi dikkat aa rahi hai";
    if (title.includes("Falling behind") || text.includes("behind live")) {
      detail = "stream live broadcast se peeche chal rahi hai aur delay badh raha hai";
    } else if (title.includes("Glitch") || text.includes("glitch")) {
      detail = "kuch frame drops aur video glitches detect hue hain";
    } else if (title.includes("ad break")) {
      detail = "stream ad break mein stuck ho gayi hai";
    } else if (title.includes("updating") || text.includes("STALE")) {
      detail = "video segments thode delay se aa rahe hain";
    }

    const idx = getNextRotation(`warn:${ch}`, 4);
    const warnVariants = [
      `Anushrav Sir ji Kripya dhyan dijiye. Final channel ${ch} par ${detail}. Aapse request hai ki please ek baar check kar lijiye taaki viewers ko problem na ho.`,
      `Anushrav Sir ji, Final ${ch} par playback thoda slow observe hua hai: ${detail}. Aapse request hai ki please stream buffer verify kar lijiye.`,
      `Anushrav Sir ji, Final node ${ch} stream broadcast se halki peeche chal rahi hai. Request hai ki stream drop hone se pehle check kar lijiye.`,
      `Anushrav Sir ji, Monitoring update: Final channel ${ch} par minor performance drop notice hua hai. Please pipeline verify karwa lijiye.`
    ];
    return warnVariants[idx];
  }

  return "";
}

// --- Live Banner UI ---
function showTtsBanner(text, severity) {
  let banner = document.getElementById("tts-live-banner");
  if (!banner) {
    banner = document.createElement("div");
    banner.id = "tts-live-banner";
    banner.className = "tts-live-banner";
    document.body.appendChild(banner);
  }

  const voice = getActiveVoice();
  const voiceLabel = voice ? (voice.name.replace(/Google|Microsoft|\(.*?\)/g, "").trim() || "Indian Voice") : "Indian Voice";
  const isAgg = severity === "AGGRESSIVE";
  const icon = isAgg ? "🔥" : (severity === "CRITICAL" ? "🚨" : "🎙️");
  const prefix = isAgg ? "Angry Voice Alert" : "Hinglish Voice Alert";

  banner.className = `tts-live-banner tts-${severity.toLowerCase()}`;
  banner.innerHTML = `
    <span class="tts-live-icon">${icon}</span>
    <div class="tts-live-content">
      <b>[${voiceLabel}] ${prefix}:</b> <span>${esc(text)}</span>
    </div>
    <button type="button" class="tts-live-stop" title="Mute/Stop Speech">Silence ✕</button>
  `;

  const stopBtn = banner.querySelector(".tts-live-stop");
  if (stopBtn) {
    stopBtn.onclick = () => {
      const activeDur = Date.now() - (ttsAlerts.speechStartTime || 0);
      const targetSrv = ttsAlerts.activeServerSpeaking;
      if (activeDur < 4000 && targetSrv) {
        ttsAlerts.recordOutcome(targetSrv, "silenced_fast");
      }
      ttsAlerts.cancel();
    };
  }
  banner.hidden = false;
}

function hideTtsBanner() {
  const banner = document.getElementById("tts-live-banner");
  if (banner) banner.hidden = true;
}

// --- Speech Synthesis Core with Pure Indian Pitch, Rate & Phonetics ---
function normalizeTextForSpeech(text) {
  if (!text) return "";
  return String(text)
    // Phonetize Devanagari words into clean Latin Hinglish for seamless voice synthesis
    .replace(/कर/g, "kar")
    .replace(/सही/g, "Sahi")
    // Expand bracket dot markers e.g. [dot] or [dot ] into natural spoken 'dot'
    // Servers by their short name, like people say them: "xcode4.ottlive.co.in" -> "xcode4"
    .replace(/\b([a-z0-9-]+)(?:\s*\[\s*dot\s*\]\s*[a-z0-9-]+)+/gi, "$1")
    .replace(/\b([a-z][a-z0-9-]*)(?:\.[a-z0-9-]+)+\.(?:in|com|net|org|io|co)\b/gi, "$1")
    .replace(/\b[a-z0-9-]+\/(?=[A-Z])/g, "")  // "cdn/Rang Manch" -> "Rang Manch" (the channel, not the path)
    .replace(/(\d+)\.(\d+)/g, "$1 point $2")  // 98.5 -> "98 point 5", never "98 dot 5"
    .replace(/(\d)\s*ms\b/g, "$1 millisecond")
    .replace(/(\d)\s*s\b/g, "$1 second")
    .replace(/(\d)\s*%/g, "$1 percent")
    .replace(/\[\s*dot\s*\]/gi, " ")
    .replace(/\s+/g, " ")
    .trim();
}

const MOOD_WORDS = { calm: "Calm", urgent: "Urgent", angry: "Angry", furious: "Furious", relieved: "Relieved" };

// --- One voice at a time, like a real control-room announcer ---
// Alerts wait in a queue: nothing talks over anything else. A more urgent alert may cut in on a calmer one; a newer
// alert about the same thing replaces the queued one; anything older than 90 s is dropped (no longer news). The chime
// finishes before the voice starts, and preview videos are turned down while the voice speaks.
const PRIORITY = { CRITICAL: 3, AGGRESSIVE: 3, WARNING: 2, RECOVERY: 1 };
const QUEUE_MAX_AGE_MS = 90000;
const GAP_BETWEEN_MS = 700;
ttsAlerts.queue = [];
ttsAlerts.current = null;  // {job, stop()}

function jobSubject(job) {
  return job.ctx.subject || (job.ctx.channels || []).slice().sort().join(",") || job.ctx.server || job.text;
}

function enqueueSpeech(job) {
  const subject = jobSubject(job);
  // A recovery makes any queued alert about the same thing pointless, and vice versa: keep only the newest.
  ttsAlerts.queue = ttsAlerts.queue.filter(j => jobSubject(j) !== subject);
  ttsAlerts.queue.push(job);
  ttsAlerts.queue.sort((a, b) => (PRIORITY[b.severity] || 0) - (PRIORITY[a.severity] || 0) || a.at - b.at);
  const cur = ttsAlerts.current;
  if (cur && (PRIORITY[job.severity] || 0) > (PRIORITY[cur.job.severity] || 0)) cur.stop();  // urgent cuts in
  drainSpeechQueue();
}

async function drainSpeechQueue() {
  if (ttsAlerts.draining) return;  // one runner only, even during the pause between alerts
  ttsAlerts.draining = true;
  try {
    await drainLoop();
  } finally {
    ttsAlerts.draining = false;
  }
}

async function drainLoop() {
  while (ttsAlerts.queue.length && ttsAlerts.enabled) {
    const job = ttsAlerts.queue.shift();
    if (Date.now() - job.at > QUEUE_MAX_AGE_MS) continue;  // stale: don't announce old news
    let stopFn = () => { };
    const current = { job, stopped: false, stop: () => { current.stopped = true; stopFn(); } };
    ttsAlerts.current = current;
    duckPageAudio(true);
    try {
      await runSpeechJob(job, current, fn => { stopFn = fn; });
    } catch (err) {
      console.debug("[TTS] job failed:", err);
    } finally {
      // Whatever happened, nothing from this job may keep sounding under the next one.
      try { current.stop(); } catch (_) { }
      if (ttsAlerts.activeAudio) { try { ttsAlerts.activeAudio.pause(); } catch (_) { } ttsAlerts.activeAudio = null; }
      duckPageAudio(false);
      ttsAlerts.current = null;
    }
    await sleep(GAP_BETWEEN_MS);
  }
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

// Preview videos (and any other media on the page) drop to 15% while the voice speaks, then come back.
function duckPageAudio(on) {
  document.querySelectorAll("video, audio").forEach(m => {
    if (m === ttsAlerts.activeAudio) return;
    if (on) {
      if (m.dataset.duckedFrom === undefined) { m.dataset.duckedFrom = String(m.volume); m.volume = Math.min(m.volume, 0.15); }
    } else if (m.dataset.duckedFrom !== undefined) {
      m.volume = Number(m.dataset.duckedFrom); delete m.dataset.duckedFrom;
    }
  });
}

async function runSpeechJob(job, current, onStop) {
  const { severity, ctx } = job;
  const serverName = ctx.server || null;
  await playChimeAndWait(severity, serverName);
  if (current.stopped || !ttsAlerts.enabled) return;
  if (ctx.test) {  // the voice check: just the browser voice, nothing recorded
    await speakBrowserAndWait(job.text, severity, null, onStop);
    return;
  }
  const ctl = new AbortController();
  ttsAlerts.humanAbort = ctl;
  onStop(() => ctl.abort());
  const timer = setTimeout(() => ctl.abort(), 8000);
  let clip = null;
  try {
    const res = await fetch("/api/voice/alert", {
      method: "POST", headers: { "Content-Type": "application/json" }, signal: ctl.signal,
      body: JSON.stringify({
        severity, channels: (ctx.channels || []).filter(Boolean).slice(0, 20), server: serverName,
        title: ctx.title ? String(ctx.title).slice(0, 500) : null, detail: ctx.detail ? String(ctx.detail).slice(0, 1000) : null,
        minutes: ctx.minutes || null, subject: ctx.subject || null, style: ctx.style || null,
      }),
    });
    if (res.ok) clip = await res.json();
  } catch (_) { /* timeout or server down: the browser voice reads it */ }
  clearTimeout(timer);
  if (current.stopped || !ttsAlerts.enabled) return;
  if (clip && clip.muted) return;  // every channel in it is set to "ignore alerts"
  if (clip && clip.audio_url) {
    const ok = await playClipAndWait(clip, severity, ctx.nodeIds || [], onStop);
    if (ok || current.stopped) return;
  }
  await speakBrowserAndWait((clip && clip.text) || job.text, severity, serverName, onStop);
}

function playChimeAndWait(severity, serverName) {
  playIndianBroadcastChime(severity, serverName);
  return sleep(severity === "RECOVERY" ? 350 : 550);  // the three notes ring out before anyone speaks
}

function playClipAndWait(clip, severity, nodeIds, onStop) {
  return new Promise(resolve => {
    if (window.speechSynthesis) window.speechSynthesis.cancel();
    const audio = new Audio(clip.audio_url);
    ttsAlerts.activeAudio = audio;
    ttsAlerts.speaking = true;
    ttsAlerts.speechStartTime = Date.now();
    ttsAlerts.lastPlay = { id: clip.play_id, startedAt: Date.now(), nodeIds, acked: false };
    let settled = false;
    const finish = ok => {
      if (settled) return;
      settled = true;
      clearTimeout(guard);
      ttsAlerts.speaking = false;
      if (ttsAlerts.activeAudio === audio) ttsAlerts.activeAudio = null;
      clearTimeout(ttsAlerts.bannerTimer);  // leave a few seconds to rate it
      ttsAlerts.bannerTimer = setTimeout(hideTtsBanner, ok ? 9000 : 0);
      resolve(ok);
    };
    const guard = setTimeout(() => { try { audio.pause(); } catch (_) { } finish(true); }, 60000);
    onStop(() => { try { audio.pause(); } catch (_) { } finish(true); });
    showHumanBanner(clip, severity);
    audio.onplay = () => { try { postPlay(clip.play_id, { heard: true }); } catch (_) { } };
    audio.onended = () => finish(true);
    audio.onerror = () => finish(false);
    audio.play().catch(() => finish(false));  // autoplay blocked or bad file: the browser voice instead
  });
}

// The browser's own voice: natural pace and pitch only (extreme settings are what make it sound robotic).
function speakBrowserAndWait(text, severity, serverName, onStop) {
  return new Promise(resolve => {
    if (!window.speechSynthesis || !text) return resolve(false);
    window.speechSynthesis.cancel();
    const utter = new SpeechSynthesisUtterance(normalizeTextForSpeech(text));
    const voice = getActiveVoice();
    utter.lang = ttsAlerts.targetLanguage || "hi-IN";
    if (voice) {
      utter.voice = voice;
      if (voice.lang) utter.lang = voice.lang;
    }
    const style = {
      CRITICAL: [1.06, 1.0, 1.0], AGGRESSIVE: [1.1, 0.97, 1.0], WARNING: [0.98, 1.0, 0.9],
      RECOVERY: [1.0, 1.03, 0.9]
    }[severity] || [1.0, 1.0, 0.9];
    [utter.rate, utter.pitch, utter.volume] = style;
    const tuning = serverName ? ttsAlerts.getNodeProfile(serverName)?.audio_tuning : null;
    if (tuning?.speech_rate) utter.rate = Math.min(1.15, Math.max(0.9, tuning.speech_rate));
    if (tuning?.speech_pitch) utter.pitch = Math.min(1.08, Math.max(0.92, tuning.speech_pitch));
    const tod = ttsAlerts.dynamicPolicy?.time_of_day || {};
    if (tod.volume_boost) utter.volume = Math.min(1.0, utter.volume + tod.volume_boost);
    ttsAlerts.speechStartTime = Date.now();
    ttsAlerts.activeServerSpeaking = serverName || null;
    ttsAlerts.speaking = true;
    ttsAlerts.activeUtterance = utter;
    showTtsBanner(text, severity, serverName);
    let settled = false;
    const finish = ok => {
      if (settled) return;
      settled = true;
      clearTimeout(guard);
      ttsAlerts.speaking = false;
      ttsAlerts.activeUtterance = null;
      hideTtsBanner();
      resolve(ok);
    };
    const guard = setTimeout(() => { window.speechSynthesis.cancel(); finish(true); }, 60000);
    onStop(() => { window.speechSynthesis.cancel(); finish(true); });
    utter.onend = () => finish(true);
    utter.onerror = () => finish(false);
    window.speechSynthesis.speak(utter);
  });
}

// Kept for older callers: queue it like everything else.
function speakHinglish(text, severity, skipChime = false, serverName = null) {
  speakAlert(text, severity, { server: serverName });
}

async function speakAlert(fallbackText, severity, ctx = {}) {
  if (!ttsAlerts.enabled || !fallbackText) return;
  unlockAudioOnInteraction();
  let text = fallbackText;
  const profile = ctx.server ? ttsAlerts.getNodeProfile(ctx.server) : null;
  const tuning = profile?.audio_tuning;
  if (tuning?.verbosity === "brief" && severity === "WARNING") {  // never for a real outage
    const srvShort = (ctx.server || "Server").split(".")[0];
    const chs = (ctx.channels || []).filter(Boolean).slice(0, 3).join(", ");
    text = `Anushrav Sir, ${srvShort}${chs ? ` (${chs})` : ""} par chhota sa stall dikha hai. Ek baar check kar lijiye.`;
  } else if (tuning?.learned_fix && severity !== "RECOVERY") {
    text += ` Pichli baar iska fix tha: ${tuning.learned_fix}.`;
  }
  enqueueSpeech({ text, severity, ctx, at: Date.now() });
}

function postPlay(playId, data) {
  return fetch(`/api/voice/plays/${playId}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data)
  }).catch(() => { });
}

function showHumanBanner(clip, severity) {
  clearTimeout(ttsAlerts.bannerTimer);
  let banner = document.getElementById("tts-live-banner");
  if (!banner) {
    banner = document.createElement("div");
    banner.id = "tts-live-banner";
    document.body.appendChild(banner);
  }
  const mood = clip.style || "urgent";
  banner.className = `tts-live-banner tts-${severity.toLowerCase()} tts-mood-${mood}`;
  const speaker = clip.name || "Bot";  // the voice's display name (Himanshu, Manish, … or Bot)
  banner.innerHTML = `
    <span class="tts-live-icon tts-wave" aria-hidden="true"><i></i><i></i><i></i><i></i></span>
    <div class="tts-live-content">
      <div class="tts-live-meta"><b>${esc(speaker)}</b><span class="tts-mood">${esc(MOOD_WORDS[mood] || mood)}</span>${clip.repeats ? `<span class="tts-repeat">said ${clip.repeats + 1}×</span>` : ""}</div>
      <span class="tts-live-text">${esc(clip.text)}</span>
    </div>
    <div class="tts-rate" role="group" aria-label="Did it sound like a real person?">
      <span>Sounded human?</span>
      <button type="button" data-rate="1" aria-label="Yes, sounded human" title="Yes: use more like this">👍</button>
      <button type="button" data-rate="-1" aria-label="No, sounded off" title="No: say it differently next time">👎</button>
    </div>
    <button type="button" class="tts-live-stop" title="Stop">✕</button>`;
  banner.querySelector(".tts-live-stop").onclick = () => ttsAlerts.cancel();
  banner.querySelector(".tts-rate").onclick = e => {
    const b = e.target.closest("[data-rate]");
    if (!b) return;
    postPlay(clip.play_id, { rating: Number(b.dataset.rate) });
    banner.querySelector(".tts-rate").innerHTML = `<span>${b.dataset.rate === "1" ? "Thanks, more like this." : "Got it, I'll change it."}</span>`;
    clearTimeout(ttsAlerts.bannerTimer);
    if (!ttsAlerts.speaking) ttsAlerts.bannerTimer = setTimeout(hideTtsBanner, 2500);
  };
  banner.hidden = false;
}

// --- Dynamic Learned Policy & Auto-Tuning Engine ---
ttsAlerts.dynamicPolicy = {
  nodeProfiles: {},
  cascadeGraph: {},
  lastFetchedAt: 0
};

ttsAlerts.fetchDynamicPolicy = async function () {
  try {
    const res = await fetch("/api/learning/dynamic-alert-policy");
    if (res.ok) {
      const data = await res.json();
      ttsAlerts.dynamicPolicy.nodeProfiles = data.node_profiles || {};
      ttsAlerts.dynamicPolicy.cascadeGraph = data.cascade_graph || {};
      ttsAlerts.dynamicPolicy.time_of_day = data.time_of_day || {};
      ttsAlerts.dynamicPolicy.lastFetchedAt = Date.now();
      console.debug("[TTS] Dynamic alert policy updated:", Object.keys(ttsAlerts.dynamicPolicy.nodeProfiles).length, "nodes");
    }
  } catch (err) {
    console.debug("[TTS] Failed to fetch dynamic alert policy:", err);
  }
};

ttsAlerts.getNodeProfile = function (serverName, nodeId) {
  const profiles = ttsAlerts.dynamicPolicy.nodeProfiles || {};
  const srvKey = (serverName || "").toLowerCase().replace(/https?:\/\/|\/.*$/g, "").split(".")[0];
  const nodeKey = (nodeId || "").toLowerCase();
  return profiles[srvKey] || profiles[nodeKey] || profiles[serverName] || null;
};

ttsAlerts.isCascadeSuppressed = function (serverName, activeDownServers) {
  if (!activeDownServers || activeDownServers.size === 0) return false;
  const graph = ttsAlerts.dynamicPolicy.cascadeGraph || {};
  const srvClean = (serverName || "").toLowerCase().replace(/https?:\/\/|\/.*$/g, "").split(".")[0];

  for (const parentSrv of activeDownServers) {
    const pClean = (parentSrv || "").toLowerCase().replace(/https?:\/\/|\/.*$/g, "").split(".")[0];
    if (pClean === srvClean) continue;
    const followers = graph[pClean] || [];
    for (const f of followers) {
      const fClean = (f.follower || "").toLowerCase();
      const fullClean = (f.full_follower || "").toLowerCase();
      if (fClean === srvClean || fullClean.includes(srvClean)) {
        return { parent: parentSrv, lag: f.lag_s || 60 };
      }
    }
  }
  return false;
};

ttsAlerts.recordOutcome = function (node, outcome) {
  try {
    fetch("/api/learning/record-alert-outcome", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node: node || "unknown", outcome })
    }).catch(() => { });
  } catch (_) { }
};

// Server Warning Tracking Map: serverName -> { count, firstWarnAt, lastSpokenAt, escalated, channels }
ttsAlerts.serverWarningState = {};

// --- Dynamic Aggressive Dialogue Generator ---
function buildDynamicAggressiveVariants(srv, srvPhonetic, srvState, warnItems, now) {
  const chList = Array.from(srvState.channels || []).slice(0, 3).join(", ") || "channels";
  const minsDown = Math.max(1, Math.round((now - (srvState.firstWarnAt || now)) / 60000));
  const count = srvState.count || 2;

  // 1. Dynamic Telemetry Symptom Extraction from live alert payload
  const rawSymptoms = (warnItems || []).map(it => {
    const t = (it.alertObj?.title || "") + " " + (it.alertObj?.text || "");
    return t.toLowerCase();
  }).join(" ");

  let symptomClause = "down hai";
  if (rawSymptoms.includes("404") || rawSymptoms.includes("manifest")) {
    symptomClause = "404 manifest error de raha hai";
  } else if (rawSymptoms.includes("stale") || rawSymptoms.includes("freeze") || rawSymptoms.includes("stopped")) {
    symptomClause = "par media segments freeze ho chuke hain";
  } else if (rawSymptoms.includes("delay") || rawSymptoms.includes("behind") || rawSymptoms.includes("latency")) {
    symptomClause = "par latency spike ho rahi hai aur playback buffer delay throw kar raha hai";
  } else if (rawSymptoms.includes("refused") || rawSymptoms.includes("unreachable")) {
    symptomClause = "connection refuse kar raha hai aur unreachable hai";
  }

  // 2. Incident Frequency & Escalation Context
  let pastClause = "";
  if (count >= 5) {
    pastClause = `Aaj is server par ${count} baar continuous breakdown ho chuka hai, extreme negligence hai!`;
  } else if (count >= 3) {
    pastClause = `Aaj is server par ${count} baar breakdown repeat ho chuka hai! Pichhle ${minsDown} minute se alerts aa rahe hain!`;
  } else if (count === 2) {
    pastClause = `Doosri baar ye same server issue throw kar raha hai!`;
  }

  // 3. Multi-dimensional Dynamic Variant Pool (Combines exact telemetry, real elapsed time, and human dialogue)
  const variants = [
    // Core base variant with past context
    `Anushrav tere ko dikhaai nahi de raha hai ${srvPhonetic} down hai, Sahi kar!${pastClause ? ' ' + pastClause : ''}`,
    // Visual inspection variant with symptom & channel impact
    `Anushrav! Dekh ${srvPhonetic} abhi tak down pada hai! Tere ko dikhai nahi deta kya? Sahi kar jaldi, channels ${chList} par buffer badh raha hai!`,
    // Command urgent variant with impacted channels
    `Anushrav tere ko dikhaai nahi de raha hai ${srvPhonetic} down hai, Sahi kar! Channels ${chList} par baar baar warning aa rahi hai, turant live restart maro!`,
    // Root cause investigation variant
    `Anushrav tere ko dikhaai nahi de raha hai ${srvPhonetic} down hai, Sahi kar! ${pastClause || 'Pehle bhi yahi crash hua tha'}, encoder logs check karke fix karo!`,
    // High-urgency warning variant
    `Anushrav! Baar-baar ${srvPhonetic} warning throw kar raha hai, sahi kar isko pehle! Blackout hone ka wait kar rahe ho kya?`,
    // Live telemetry diagnostics variant
    `Anushrav tere ko dikhaai nahi de raha hai ${srvPhonetic} ${symptomClause}! Sahi kar jaldi, broadcast freeze hone se pehle bypass route switch karo!`,
    // Duration escalation variant
    `Anushrav bhai, abhi tak so rahe ho kya? ${srvPhonetic} lagbhag ${minsDown} minute se alert throw kar raha hai! Sahi kar turant!`,
    // CTO executive direct callout variant
    `Anushrav sir! Team ko turant bolo ${srvPhonetic} par failover initiate kare! Channels ${chList} down hone wale hain, Sahi kar!`
  ];

  return variants;
}

// --- Auto-Adjust Evaluation Engine (Evaluates All Final Nodes Concurrently) ---
// --- Announce like a real NOC: only what matters, once, and escalate only for what really persists ---
const WARNING_HOLD_MS = 90 * 1000;       // a warning must last 90 s before it's spoken (most clear on their own)
const SAME_ALERT_REPEAT_MS = 10 * 60000; // the same channel at the same level is not announced again for 10 min
const ESCALATE_AFTER_MS = 5 * 60000;     // "angry" only after a server's warnings have lasted 5 min straight...
const ESCALATE_REPEAT_MS = 5 * 60000;    // ...and at most once every 5 min
const RECOVERY_MIN_MS = 2 * 60000;       // "good news" only after a spoken outage that lasted 2+ min
ttsAlerts.spokenMemory = {};             // "channel|LEVEL" -> when it was last announced

function spokenRecently(channel, level, now) {
  return now - (ttsAlerts.spokenMemory[`${channel}|${level}`] || 0) < SAME_ALERT_REPEAT_MS;
}
function markSpoken(channels, level, now) {
  channels.forEach(ch => { ttsAlerts.spokenMemory[`${ch}|${level}`] = now; });
}

ttsAlerts.evaluateAllFinalNodes = function () {
  if (!ttsAlerts.enabled) return;
  if (typeof G === "undefined" || !Array.isArray(G.nodes)) return;

  const now = Date.now();
  if (now - (ttsAlerts.dynamicPolicy?.lastFetchedAt || 0) > 60000) {
    ttsAlerts.fetchDynamicPolicy();
  }

  const finalNodes = G.nodes.filter(n =>
    (n.labels || []).includes("FinalLink") || (n.roles || []).includes("FinalLink")
  );

  const pendingCriticals = [];
  const pendingWarnings = [];
  const recoveredList = [];
  const activeWarningsByServer = {};

  // First pass: identify all servers currently experiencing confirmed critical failure
  const confirmedDownServers = new Set();
  for (const n of finalNodes) {
    const finalLinks = (n.links || []).filter(l => l.role === "FinalLink");
    const targets = finalLinks.length > 0 ? finalLinks.map(l => l.url) : [null];
    for (const url of targets) {
      const alertObj = (typeof cardAlert === "function") ? cardAlert(n.id, url) : null;
      if (alertObj && alertObj.tone === "down") {
        const srv = getNodeServer(n.id, url);
        if (srv) confirmedDownServers.add(srv);
      }
    }
  }

  for (const n of finalNodes) {
    const finalLinks = (n.links || []).filter(l => l.role === "FinalLink");
    const targets = finalLinks.length > 0 ? finalLinks.map(l => l.url) : [null];

    for (const url of targets) {
      const key = `${n.id}|${url || ""}`;
      const channel = getFinalChannelName(n.id, url);
      const serverName = getNodeServer(n.id, url);
      if (typeof isCardMuted === "function" && isCardMuted(n.id, url)) {
        delete ttsAlerts.state[key];  // ignored channel: no alert now, and no "wapas aa gaya" when it is unmuted
        continue;
      }
      const alertObj = (typeof cardAlert === "function") ? cardAlert(n.id, url) : null;
      const last = ttsAlerts.state[key] || {
        severity: "NONE",
        spokenAt: 0,
        title: "",
        text: "",
        acknowledged: false,
        warnCount: 0,
        episodeStartAt: 0,
        hasVoiced: false
      };

      // Determine current severity
      let currentSeverity = "NONE";
      if (alertObj) {
        if (alertObj.tone === "down") {
          currentSeverity = "CRITICAL";
        } else if (alertObj.tone === "warn") {
          currentSeverity = "WARNING";
        }
      }

      // Track ongoing episode start time
      if (currentSeverity !== "NONE") {
        if (!last.episodeStartAt) {
          last.episodeStartAt = now;
          last.hasVoiced = false;
        }
        ttsAlerts.state[key] = last;  // keep the episode start between checks, or the hold-down never ends
      }

      // Check recovery
      // An episode ends: it was spoken (real outage) or it cleared during the hold-down (settled alone).
      if (currentSeverity === "NONE" && (last.episodeStartAt || last.severity === "CRITICAL" || last.severity === "WARNING" || last.severity === "AGGRESSIVE")) {
        const episodeDurationMs = now - (last.episodeStartAt || now);
        if (!last.hasVoiced) {
          // Self-healed inside dynamic hold-down grace window! Suppressed false alert.
          ttsAlerts.recordOutcome(serverName || channel, "settled_alone");
          console.info(`[TTS Auto-Tune] Stream ${channel} self-healed in ${Math.round(episodeDurationMs / 1000)}s! Suppressed false alert.`);
        } else {
          // Real confirmed outage that was voiced. Announce the recovery only if it was worth hearing about.
          ttsAlerts.recordOutcome(serverName || channel, "real_outage");
          // "Wapas aa gaya" only after a real outage: a warning clearing (slow, glitches) isn't a comeback.
          if (last.severity === "CRITICAL" && episodeDurationMs >= RECOVERY_MIN_MS) {
            recoveredList.push({ nodeId: n.id, channel, key, serverName });
          }
        }
        ttsAlerts.state[key] = { severity: "NONE", spokenAt: 0, title: "", text: "", acknowledged: false, warnCount: 0, episodeStartAt: 0, hasVoiced: false };
        continue;
      }

      // If no alert currently active, continue
      if (currentSeverity === "NONE") continue;

      // If user clicked or acknowledged this node, do not speak
      if (last.acknowledged && (currentSeverity === last.severity || last.severity === "AGGRESSIVE")) continue;

      if (currentSeverity === "CRITICAL") {
        const durationActiveMs = now - (last.episodeStartAt || now);
        const profile = ttsAlerts.getNodeProfile(serverName, n.id);
        const holdDownMs = (profile?.hold_down_s || 5) * 1000;

        // Dynamic Hold-Down Check (Silences transient micro-flappers)
        if (durationActiveMs < holdDownMs && !last.hasVoiced) {
          console.debug(`[TTS Hold-Down] Holding alert for ${channel} (${Math.round(durationActiveMs / 1000)}s / ${profile?.hold_down_s || 5}s)`);
          continue;
        }

        // Dynamic Cascading Suppression Check
        const cascadeParent = ttsAlerts.isCascadeSuppressed(serverName, confirmedDownServers);
        if (cascadeParent) {
          console.info(`[TTS Cascade-Guard] Suppressing downstream alert for ${serverName} (Caused by parent ${cascadeParent.parent})`);
          continue;
        }

        // CRITICAL: Speak on new critical alert or when cooldown expired
        const isNew = last.severity !== "CRITICAL";
        const cooldownExpired = (now - last.spokenAt) >= ttsAlerts.criticalCooldownMs;

        if (isNew || cooldownExpired) {
          last.hasVoiced = true;
          pendingCriticals.push({ nodeId: n.id, channel, key, alertObj, serverName });
        }
      } else if (currentSeverity === "WARNING") {
        const durationActiveMs = now - (last.episodeStartAt || now);
        const profile = ttsAlerts.getNodeProfile(serverName, n.id);
        const holdDownMs = Math.max(WARNING_HOLD_MS, (profile?.hold_down_s || 10) * 1000);

        // Don't queue warnings if still within transient hold-down
        if (durationActiveMs < holdDownMs && !last.hasVoiced) {
          continue;
        }

        // Cascade suppression for warnings
        const cascadeParent = ttsAlerts.isCascadeSuppressed(serverName, confirmedDownServers);
        if (cascadeParent) {
          continue;
        }

        // Group active warnings by server to detect repeated warnings across channels
        if (!activeWarningsByServer[serverName]) {
          activeWarningsByServer[serverName] = [];
        }
        activeWarningsByServer[serverName].push({
          nodeId: n.id,
          channel,
          key,
          alertObj,
          isNew: last.severity !== "WARNING" && last.severity !== "AGGRESSIVE"
        });
      }
    }
  }

  // Evaluate Server Repeated Warnings & Interval Escalation
  const serverAggressiveAlerts = [];

  for (const [srv, warnItems] of Object.entries(activeWarningsByServer)) {
    let srvState = ttsAlerts.serverWarningState[srv];
    const hasNew = warnItems.some(it => it.isNew);

    if (!srvState) {
      // First warning ever for this server
      srvState = {
        count: 1,
        firstWarnAt: now,
        lastSpokenAt: now,
        escalated: false,
        channels: new Set(warnItems.map(it => it.channel))
      };
      ttsAlerts.serverWarningState[srv] = srvState;
      // Queue for polite Request Tone
      pendingWarnings.push(...warnItems.filter(it => it.isNew));
    } else {
      // Warning is coming again and again from same server to their channels
      srvState.count++;
      warnItems.forEach(it => srvState.channels.add(it.channel));

      const timeSinceLastSpoken = now - srvState.lastSpokenAt;
      const lastingMs = now - srvState.firstWarnAt;
      if (lastingMs >= ESCALATE_AFTER_MS && timeSinceLastSpoken >= ESCALATE_REPEAT_MS) {
        // ESCALATE TO ANGRY AGGRESSIVE TONE!
        // "Anushrav tere ko dikhaai nahi de raha hai {server} down hai, Sahi kar!"
        const chList = Array.from(srvState.channels).slice(0, 3).join(", ");
        const srvPhonetic = (srv && srv.includes(".") && !srv.includes("[dot"))
          ? srv.replace(/\./g, " [dot] ")
          : (srv || "server");

        // DYNAMIC CONTEXTUAL AGGRESSIVE DIALOGUE SYNTHESIS
        const aggVariants = buildDynamicAggressiveVariants(srv, srvPhonetic, srvState, warnItems, now);
        const aggIdx = getNextRotation(`agg:${srv}`, aggVariants.length);
        let aggMsg = aggVariants[aggIdx];

        serverAggressiveAlerts.push({
          server: srv,
          message: aggMsg,
          items: warnItems
        });

        srvState.lastSpokenAt = now;
        srvState.escalated = true;
      } else if (hasNew && !srvState.escalated) {
        // New channel warning on this server before interval
        pendingWarnings.push(...warnItems.filter(it => it.isNew));
      }
    }
  }

  // Clear server warning state for servers where all warnings recovered
  for (const srv of Object.keys(ttsAlerts.serverWarningState)) {
    if (!activeWarningsByServer[srv]) {
      delete ttsAlerts.serverWarningState[srv];
    }
  }

  // Handle Recoveries first if all clear
  // Nothing already said in the last 10 min is said again (the same 8-channel warning 3 minutes later is noise).
  pendingWarnings.splice(0, pendingWarnings.length, ...pendingWarnings.filter(w => !spokenRecently(w.channel, "WARNING", now)));
  for (let i = recoveredList.length - 1; i >= 0; i--) {
    if (spokenRecently(recoveredList[i].channel, "RECOVERY", now)) recoveredList.splice(i, 1);
  }

  if (recoveredList.length > 0 && !pendingCriticals.length && !serverAggressiveAlerts.length && !pendingWarnings.length) {
    markSpoken(recoveredList.map(r => r.channel), "RECOVERY", now);
    const recNames = recoveredList.slice(0, 2).map(r => r.channel).join(", ");
    const recMsg = `Anushrav Sir ji, Good news! Final ${recNames} ab wapas normal ho gaya hai. Stream smoothly chal rahi hai.`;
    speakAlert(recMsg, "RECOVERY", {
      channels: recoveredList.map(r => r.channel), server: recoveredList[0].serverName,
      subject: recoveredList.map(r => r.channel).sort().join(","), nodeIds: recoveredList.map(r => r.nodeId)
    });
    return;
  }

  // PRIORITY EXECUTION:
  // 1. Critical Outage (Order Tone - Immediate Viewer Outage)
  if (pendingCriticals.length > 0) {
    pendingCriticals.forEach(c => {
      const prev = ttsAlerts.state[c.key] || {};
      ttsAlerts.state[c.key] = {
        severity: "CRITICAL",
        spokenAt: now,
        title: c.alertObj?.title || "",
        text: c.alertObj?.text || "",
        acknowledged: false,
        warnCount: 0,
        episodeStartAt: prev.episodeStartAt || now,
        hasVoiced: true
      };
    });

    markSpoken(pendingCriticals.map(c => c.channel), "CRITICAL", now);
    const msg = buildConsolidatedHinglishMessage(pendingCriticals, []);
    const first = pendingCriticals[0];
    speakAlert(msg, "CRITICAL", {
      channels: pendingCriticals.map(c => c.channel), server: first.serverName,
      title: first.alertObj?.title, detail: first.alertObj?.text, subject: pendingCriticals.map(c => c.channel).sort().join(","),
      nodeIds: pendingCriticals.map(c => c.nodeId)
    });
    return;
  }

  // 2. Repeated Server Warning (Angry Aggressive Tone - "Anushrav tere ko dikhaai nahi de raha hai...")
  if (serverAggressiveAlerts.length > 0) {
    const agg = serverAggressiveAlerts[0];
    agg.items.forEach(w => {
      const prev = ttsAlerts.state[w.key] || {};
      ttsAlerts.state[w.key] = {
        severity: "AGGRESSIVE",
        spokenAt: now,
        title: w.alertObj?.title || "",
        text: w.alertObj?.text || "",
        acknowledged: false,
        warnCount: (prev.warnCount || 0) + 1,
        episodeStartAt: prev.episodeStartAt || now,
        hasVoiced: true
      };
    });

    markSpoken([`server:${agg.server}`], "AGGRESSIVE", now);
    const aggState = ttsAlerts.serverWarningState[agg.server] || {};
    speakAlert(agg.message, "AGGRESSIVE", {
      channels: Array.from(aggState.channels || agg.items.map(w => w.channel)),
      server: agg.server, title: agg.items[0]?.alertObj?.title, detail: agg.items[0]?.alertObj?.text,
      minutes: aggState.firstWarnAt ? (now - aggState.firstWarnAt) / 60000 : null, subject: `server:${agg.server}`,
      nodeIds: agg.items.map(w => w.nodeId)
    });
    return;
  }

  // 3. New Initial Warning (Polite Request Tone)
  if (pendingWarnings.length > 0) {
    pendingWarnings.forEach(w => {
      const prev = ttsAlerts.state[w.key] || {};
      ttsAlerts.state[w.key] = {
        severity: "WARNING",
        spokenAt: now,
        title: w.alertObj?.title || "",
        text: w.alertObj?.text || "",
        acknowledged: false,
        warnCount: (prev.warnCount || 0) + 1,
        episodeStartAt: prev.episodeStartAt || now,
        hasVoiced: true
      };
    });

    markSpoken(pendingWarnings.map(w => w.channel), "WARNING", now);
    const msg = buildConsolidatedHinglishMessage([], pendingWarnings);
    const w0 = pendingWarnings[0];
    speakAlert(msg, "WARNING", {
      channels: pendingWarnings.map(w => w.channel), server: getNodeServer(w0.nodeId, null),
      title: w0.alertObj?.title, detail: w0.alertObj?.text, subject: pendingWarnings.map(w => w.channel).sort().join(","),
      nodeIds: pendingWarnings.map(w => w.nodeId)
    });
  }
};

// Hook called by renderNodeAlerts() in web/node-alerts.js
ttsAlerts.onAlertsRendered = function () {
  if (!ttsAlerts.enabled) return;

  // Debounce by 800ms to allow all DOM updates & spider steps to settle
  clearTimeout(ttsAlerts.evaluationTimer);
  ttsAlerts.evaluationTimer = setTimeout(() => {
    ttsAlerts.evaluateAllFinalNodes();
  }, 800);
};

// Acknowledge an alert to silence it for this session
ttsAlerts.acknowledge = function (nodeId) {
  const lp = ttsAlerts.lastPlay;
  if (lp && !lp.acked && lp.nodeIds.includes(nodeId)) {  // how fast the operator reacted: the voice worked
    lp.acked = true;
    postPlay(lp.id, { acked_after_s: (Date.now() - lp.startedAt) / 1000 });
  }
  Object.keys(ttsAlerts.state).forEach(k => {
    if (k.startsWith(`${nodeId}|`)) {
      ttsAlerts.state[k].acknowledged = true;
    }
  });
  if (ttsAlerts.speaking) {
    ttsAlerts.cancel();
  }
};

// --- User Controls & UI Menu ---
ttsAlerts.toggle = function () {
  unlockAudioOnInteraction();
  ttsAlerts.enabled = !ttsAlerts.enabled;
  try {
    localStorage.setItem("stream_graph_tts_hinglish", String(ttsAlerts.enabled));
  } catch (e) { }

  ttsAlerts.updateUiButton();

  if (ttsAlerts.enabled) {
    if (typeof toast === "function") {
      toast("ok", "Hinglish Voice Alerts ON", "Auto-adjusted Indian broadcast voice active");
    }
    ttsAlerts.testVoice();
  } else {
    ttsAlerts.cancel();
    if (typeof toast === "function") {
      toast("warn", "Voice Alerts Muted", "Voice: OFF");
    }
  }
};

ttsAlerts.activeAudio = null;

// Stops whatever is playing and any alert still being fetched.
ttsAlerts.stopAudio = function () {
  if (ttsAlerts.activeAudio) {
    try {
      ttsAlerts.activeAudio.pause();
      ttsAlerts.activeAudio.removeAttribute("src");
      ttsAlerts.activeAudio.load();
    } catch (e) { }
    ttsAlerts.activeAudio = null;
  }
};

ttsAlerts.testVoice = function () {
  // A voice check that sounds like a check, never like a real "channel is back" alert; not recorded as a play.
  if (!ttsAlerts.enabled) return;
  unlockAudioOnInteraction();
  loadAvailableVoices();
  enqueueSpeech({
    text: "Voice check. Alerts ab isi awaaz mein aayenge.", severity: "RECOVERY",
    ctx: { test: true, subject: "voice-test" }, at: Date.now()
  });
};

ttsAlerts.cancel = function () {
  ttsAlerts.queue = [];
  if (ttsAlerts.current) ttsAlerts.current.stop();
  if (ttsAlerts.humanAbort) { ttsAlerts.humanAbort.cancelled = true; ttsAlerts.humanAbort.abort(); }
  ttsAlerts.stopAudio();
  ttsAlerts.speaking = false;
  ttsAlerts.speechStartTime = 0;
  if (window.speechSynthesis) window.speechSynthesis.cancel();
  hideTtsBanner();
};

ttsAlerts.updateUiButton = function () {
  const btn = document.getElementById("btn-tts-toggle");
  if (!btn) return;
  const icon = document.getElementById("tts-icon");
  const label = document.getElementById("tts-label");

  if (ttsAlerts.enabled) {
    btn.classList.add("active");
    if (icon) icon.textContent = "🔊";
    if (label) label.textContent = "Voice: ON";
    btn.title = "Final Hinglish Voice Alerts: ON (Click to Mute, Shift+Click to Test Indian Voice)";
  } else {
    btn.classList.remove("active");
    if (icon) icon.textContent = "🔇";
    if (label) label.textContent = "Voice: OFF";
    btn.title = "Final Hinglish Voice Alerts: OFF (Click to Enable)";
  }
};

// Keyboard Hotkey: Escape silences active voice
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (ttsAlerts.speaking) {
      ttsAlerts.cancel();
    }
  }
});

// Setup UI once DOM is ready
document.addEventListener("DOMContentLoaded", () => {
  const btn = document.getElementById("btn-tts-toggle");
  if (btn) {
    btn.addEventListener("click", (e) => {
      if (e.shiftKey) {
        ttsAlerts.testVoice();
      } else {
        ttsAlerts.toggle();
      }
    });
    ttsAlerts.updateUiButton();
  }
  loadAvailableVoices();
});

// Also load on script execution if DOM already parsed
if (document.readyState === "complete" || document.readyState === "interactive") {
  loadAvailableVoices();
}

window.ttsAlerts = ttsAlerts;
