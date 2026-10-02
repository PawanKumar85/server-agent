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

  // 1. Dedicated Hindi Voices (Google हिन्दी, Lekha, Kalpana, Hemant)
  let v = voices.find(x => x.name === "Google हिन्दी" || /^(Lekha|Kalpana|Hemant)$/i.test(x.name));
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
function playIndianBroadcastChime(severity) {
  try {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return;
    if (!ttsAlerts.audioContext) ttsAlerts.audioContext = new AudioCtx();
    const ctx = ttsAlerts.audioContext;
    if (ctx.state === "suspended") ctx.resume();

    const now = ctx.currentTime;

    // Chime notes:
    // CRITICAL: Descending urgent chime (C#5 -> A4 -> F#4)
    // AGGRESSIVE: Sharp piercing aggressive staccato chord (E5 -> E5 -> A5)
    // WARNING: Ascending pleasant alert chime (F#4 -> A4 -> C#5)
    let notes = [369.99, 440.00, 554.37];
    let noteVol = 0.16;
    let step = 0.11;

    if (severity === "CRITICAL") {
      notes = [554.37, 440.00, 369.99];
      noteVol = 0.22;
      step = 0.11;
    } else if (severity === "AGGRESSIVE") {
      notes = [659.25, 659.25, 880.00];
      noteVol = 0.26;
      step = 0.08;
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
    .replace(/\[\s*dot\s*\]/gi, " dot ")
    .replace(/\s+/g, " ")
    .trim();
}

function speakHinglish(text, severity, skipChime = false) {
  if (!window.speechSynthesis || !ttsAlerts.enabled || !text) return;

  try {
    // 1. Play Indian broadcast chime tone
    if (!skipChime) playIndianBroadcastChime(severity);

    // 2. Cancel any previous speech
    window.speechSynthesis.cancel();

    const spokenText = normalizeTextForSpeech(text);
    const utter = new SpeechSynthesisUtterance(spokenText);
    const voice = getActiveVoice();

    // Target Language: Hindi (hi-IN)
    utter.lang = ttsAlerts.targetLanguage || "hi-IN";
    if (voice) {
      utter.voice = voice;
      if (voice.lang && voice.lang.toLowerCase().startsWith("hi")) {
        utter.lang = voice.lang;
      }
    }

    // Audio Tone Profiles:
    // 1. CRITICAL: Order tone (Commanding, urgent)
    // 2. AGGRESSIVE: Angry tone (Fast, high-pitch, loud for repeated server warnings)
    // 3. WARNING: Request tone (Polite, calm, respectful)
    ttsAlerts.speechStartTime = Date.now();

    if (severity === "CRITICAL") {
      utter.rate = 0.96;
      utter.pitch = 1.05;
      utter.volume = 1.0;
    } else if (severity === "AGGRESSIVE") {
      utter.rate = 1.15;
      utter.pitch = 1.25;
      utter.volume = 1.0;
    } else if (severity === "WARNING") {
      utter.rate = 0.85;
      utter.pitch = 0.92;
      utter.volume = 0.82;
    } else {
      utter.rate = 0.90;
      utter.pitch = 1.0;
      utter.volume = 0.85;
    }

    showTtsBanner(text, severity);
    ttsAlerts.speaking = true;
    ttsAlerts.activeUtterance = utter;

    utter.onend = () => {
      ttsAlerts.speaking = false;
      ttsAlerts.activeUtterance = null;
      hideTtsBanner();
    };

    utter.onerror = (err) => {
      console.debug("[TTS] Utterance error or stopped:", err);
      ttsAlerts.speaking = false;
      ttsAlerts.activeUtterance = null;
      hideTtsBanner();
    };

    window.speechSynthesis.speak(utter);
  } catch (err) {
    console.error("[TTS] Speech execution failed:", err);
    ttsAlerts.speaking = false;
    hideTtsBanner();
  }
}


// --- Human voice (voice.py): a real Indian voice says a freshly written line in the right mood. ---
// The server stitches the line from pre-recorded phrases (calm → urgent → angry → furious as it drags on), picks the
// voice it has learned works best, and keeps every clip for training. The browser voice above is the fallback.
const MOOD_WORDS = {calm: "Calm", urgent: "Urgent", angry: "Angry", furious: "Furious", relieved: "Relieved"};

async function speakAlert(fallbackText, severity, ctx = {}) {
  if (!ttsAlerts.enabled || !fallbackText) return;
  unlockAudioOnInteraction();
  playIndianBroadcastChime(severity);
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 12000);
  ttsAlerts.humanAbort = ctl;
  try {
    const res = await fetch("/api/voice/alert", {
      method: "POST", headers: {"Content-Type": "application/json"}, signal: ctl.signal,
      body: JSON.stringify({
        severity, channels: (ctx.channels || []).filter(Boolean).slice(0, 20), server: ctx.server || null,
        title: ctx.title ? String(ctx.title).slice(0, 500) : null, detail: ctx.detail ? String(ctx.detail).slice(0, 1000) : null,
        minutes: ctx.minutes || null, subject: ctx.subject || null, style: ctx.style || null, text: ctx.text || null,
      }),
    });
    clearTimeout(timer);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const clip = await res.json();
    if (!ttsAlerts.enabled) return;
    if (!clip.audio_url) { speakHinglish(clip.text || fallbackText, severity, true); return; }
    playHumanClip(clip, severity, ctx.nodeIds || []);
  } catch (err) {
    clearTimeout(timer);
    if (!ctl.cancelled && ttsAlerts.enabled) speakHinglish(fallbackText, severity, true);  // timeout or server error
  }
}

function postPlay(playId, data) {
  return fetch(`/api/voice/plays/${playId}`, {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(data)}).catch(() => {});
}

function playHumanClip(clip, severity, nodeIds) {
  if (window.speechSynthesis) window.speechSynthesis.cancel();
  if (ttsAlerts.activeAudio) { try { ttsAlerts.activeAudio.pause(); } catch (e) { } }
  const audio = new Audio(clip.audio_url);
  ttsAlerts.activeAudio = audio;
  ttsAlerts.speaking = true;
  ttsAlerts.lastPlay = {id: clip.play_id, startedAt: Date.now(), nodeIds, acked: false};
  showHumanBanner(clip, severity);
  audio.onplay = () => postPlay(clip.play_id, {heard: true});
  audio.onended = () => {
    ttsAlerts.speaking = false;
    ttsAlerts.activeAudio = null;
    clearTimeout(ttsAlerts.bannerTimer);  // leave a few seconds to rate it
    ttsAlerts.bannerTimer = setTimeout(hideTtsBanner, 9000);
  };
  audio.onerror = () => {
    ttsAlerts.speaking = false;
    ttsAlerts.activeAudio = null;
    speakHinglish(clip.text, severity, true);
  };
  audio.play().catch(() => speakHinglish(clip.text, severity, true));  // autoplay blocked: the browser voice
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
  const speaker = clip.name || "Bot";  // the voice's display name (Sumit, Om Prakash, … or Bot)
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
    postPlay(clip.play_id, {rating: Number(b.dataset.rate)});
    banner.querySelector(".tts-rate").innerHTML = `<span>${b.dataset.rate === "1" ? "Thanks, more like this." : "Got it, I'll change it."}</span>`;
    clearTimeout(ttsAlerts.bannerTimer);
    if (!ttsAlerts.speaking) ttsAlerts.bannerTimer = setTimeout(hideTtsBanner, 2500);
  };
  banner.hidden = false;
}

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
ttsAlerts.evaluateAllFinalNodes = function () {
  if (!ttsAlerts.enabled) return;
  if (typeof G === "undefined" || !Array.isArray(G.nodes)) return;

  const now = Date.now();
  const finalNodes = G.nodes.filter(n =>
    (n.labels || []).includes("FinalLink") || (n.roles || []).includes("FinalLink")
  );

  const pendingCriticals = [];
  const pendingWarnings = [];
  const recoveredList = [];
  const activeWarningsByServer = {};

  for (const n of finalNodes) {
    const finalLinks = (n.links || []).filter(l => l.role === "FinalLink");
    const targets = finalLinks.length > 0 ? finalLinks.map(l => l.url) : [null];

    for (const url of targets) {
      const key = `${n.id}|${url || ""}`;
      const channel = getFinalChannelName(n.id, url);
      const serverName = getNodeServer(n.id, url);
      const alertObj = (typeof cardAlert === "function") ? cardAlert(n.id, url) : null;
      const last = ttsAlerts.state[key] || {
        severity: "NONE",
        spokenAt: 0,
        title: "",
        text: "",
        acknowledged: false,
        warnCount: 0
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

      // Check recovery
      if (currentSeverity === "NONE" && (last.severity === "CRITICAL" || last.severity === "WARNING" || last.severity === "AGGRESSIVE")) {
        recoveredList.push({ nodeId: n.id, channel, key, serverName });
        ttsAlerts.state[key] = { severity: "NONE", spokenAt: 0, title: "", text: "", acknowledged: false, warnCount: 0 };
        continue;
      }

      // If no alert currently active, continue
      if (currentSeverity === "NONE") continue;

      // If user clicked or acknowledged this node, do not speak
      if (last.acknowledged && (currentSeverity === last.severity || last.severity === "AGGRESSIVE")) continue;

      if (currentSeverity === "CRITICAL") {
        // CRITICAL: Speak immediately on new critical alert or escalation from warning.
        const isNew = last.severity !== "CRITICAL";
        const cooldownExpired = (now - last.spokenAt) >= ttsAlerts.criticalCooldownMs;

        if (isNew || cooldownExpired) {
          pendingCriticals.push({ nodeId: n.id, channel, key, alertObj, serverName });
        }
      } else if (currentSeverity === "WARNING") {
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
  const intervalMs = 45 * 1000; // 45s interval for repeated server warnings escalation

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
      if (timeSinceLastSpoken >= intervalMs) {
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
  if (recoveredList.length > 0 && !pendingCriticals.length && !serverAggressiveAlerts.length && !pendingWarnings.length) {
    const recNames = recoveredList.slice(0, 2).map(r => r.channel).join(", ");
    const recMsg = `Anushrav Sir ji, Good news! Final ${recNames} ab wapas normal ho gaya hai. Stream smoothly chal rahi hai.`;
    speakAlert(recMsg, "RECOVERY", {channels: recoveredList.map(r => r.channel), server: recoveredList[0].serverName,
      subject: recoveredList.map(r => r.channel).sort().join(","), nodeIds: recoveredList.map(r => r.nodeId)});
    return;
  }

  // PRIORITY EXECUTION:
  // 1. Critical Outage (Order Tone - Immediate Viewer Outage)
  if (pendingCriticals.length > 0) {
    pendingCriticals.forEach(c => {
      ttsAlerts.state[c.key] = {
        severity: "CRITICAL",
        spokenAt: now,
        title: c.alertObj?.title || "",
        text: c.alertObj?.text || "",
        acknowledged: false,
        warnCount: 0
      };
    });

    const msg = buildConsolidatedHinglishMessage(pendingCriticals, []);
    const first = pendingCriticals[0];
    speakAlert(msg, "CRITICAL", {channels: pendingCriticals.map(c => c.channel), server: first.serverName,
      title: first.alertObj?.title, detail: first.alertObj?.text, subject: pendingCriticals.map(c => c.channel).sort().join(","),
      nodeIds: pendingCriticals.map(c => c.nodeId)});
    return;
  }

  // 2. Repeated Server Warning (Angry Aggressive Tone - "Anushrav tere ko dikhaai nahi de raha hai...")
  if (serverAggressiveAlerts.length > 0) {
    const agg = serverAggressiveAlerts[0];
    agg.items.forEach(w => {
      ttsAlerts.state[w.key] = {
        severity: "AGGRESSIVE",
        spokenAt: now,
        title: w.alertObj?.title || "",
        text: w.alertObj?.text || "",
        acknowledged: false,
        warnCount: (ttsAlerts.state[w.key]?.warnCount || 0) + 1
      };
    });

    const aggState = ttsAlerts.serverWarningState[agg.server] || {};
    speakAlert(agg.message, "AGGRESSIVE", {channels: Array.from(aggState.channels || agg.items.map(w => w.channel)),
      server: agg.server, title: agg.items[0]?.alertObj?.title, detail: agg.items[0]?.alertObj?.text,
      minutes: aggState.firstWarnAt ? (now - aggState.firstWarnAt) / 60000 : null, subject: `server:${agg.server}`,
      nodeIds: agg.items.map(w => w.nodeId)});
    return;
  }

  // 3. New Initial Warning (Polite Request Tone)
  if (pendingWarnings.length > 0) {
    pendingWarnings.forEach(w => {
      ttsAlerts.state[w.key] = {
        severity: "WARNING",
        spokenAt: now,
        title: w.alertObj?.title || "",
        text: w.alertObj?.text || "",
        acknowledged: false,
        warnCount: 1
      };
    });

    const msg = buildConsolidatedHinglishMessage([], pendingWarnings);
    const w0 = pendingWarnings[0];
    speakAlert(msg, "WARNING", {channels: pendingWarnings.map(w => w.channel), server: getNodeServer(w0.nodeId, null),
      title: w0.alertObj?.title, detail: w0.alertObj?.text, subject: pendingWarnings.map(w => w.channel).sort().join(","),
      nodeIds: pendingWarnings.map(w => w.nodeId)});
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
    postPlay(lp.id, {acked_after_s: (Date.now() - lp.startedAt) / 1000});
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
  if (!ttsAlerts.enabled) return;
  unlockAudioOnInteraction();
  loadAvailableVoices();
  const voice = getActiveVoice();
  const vName = voice ? voice.name : "Indian Synthesizer";
  const testMsg = `Anushrav - Voice test: ${vName} active hai. Final alert par order tone aur warning par request tone set hai.`;
  speakAlert(testMsg, "RECOVERY", {style: "relieved", channels: [], subject: "voice-test"});
};

ttsAlerts.cancel = function () {
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
