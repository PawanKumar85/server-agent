"use strict";

// ---------- Hinglish / English Localization Engine ----------
// Allows NOC & MCR operators to view glitch reasons, ML predictions, and alerts in conversational Hinglish.

const HINGLISH_MAP = {
  // Glitch types (glitch.py)
  "uneven segment length": "uneven segment duration (chote-bade video chunk)",
  "too slow for full quality": "delivery slow hai (full quality buffer hogi)",
  "delivery is too slow for full quality right now": "CDN delivery abhi full quality ke liye bohot slow chal rahi hai (video buffer ho sakti hai)",
  "delivery is getting slower": "Delivery speed lagataar aur slow ho rahi hai",
  "skipped content": "video frame/content jump hua (skip)",
  "timestamps jumped back": "video timestamp peeche ki taraf jump hua",
  "stream restarted or switched": "encoder restart ya source switch hua",
  "segment missing": "video segment download nahi ho paya (missing)",
  "a quality level disappeared": "ek video bitrate quality level gayab ho gaya",
  "Nothing points to glitches right now.": "Abhi koi glitch ka signal nahi hai, sab smoothly chal raha hai.",
  "Checked once a minute; the first results appear after two checks.": "Har minute check hota hai; do checks ke baad results dikhenge.",
  "Working normally (all streams live)": "Sab theek chal raha hai (saari streams live hain)",
  "Not checked yet": "Abhi check nahi hua",

  // Driver keywords
  "its glitches in the last 10 min": "10 min ke andar glitches",
  "its glitches in the last hour": "pichhle ghante ke glitches",
  "slow delivery": "slow network delivery",

  // Risk bands
  "low": "low risk (normal)",
  "medium": "medium risk (alert)",
  "high": "high risk (khatra)",
  "critical": "critical (turant action)",
  "watch": "watch (nazar rakhein)",
  "healthy": "healthy (theek hai)",
};

function getLanguage() {
  return (typeof store !== "undefined" ? store.get("stream_graph_lang") : null) || "en";
}

function trHinglish(text) {
  if (!text) return "";
  if (getLanguage() !== "hi") return text;

  const raw = String(text).trim();
  if (HINGLISH_MAP[raw]) return HINGLISH_MAP[raw];

  // 1. "7 glitches in the last hour (usually about 4)"
  let m = raw.match(/^(\d+)\s+glitches?\s+in\s+the\s+last\s+hour\s+\(usually\s+about\s+([\d.]+)\)$/i);
  if (m) {
    return `Pichhle 1 ghante mein ${m[1]} glitches aaye (aamtaur par lagbhag ${m[2]})`;
  }

  // 2. "7 glitches in the last hour"
  m = raw.match(/^(\d+)\s+glitches?\s+in\s+the\s+last\s+hour$/i);
  if (m) {
    return `Pichhle 1 ghante mein ${m[1]} glitches record hue`;
  }

  // 3. "upstream x is failing"
  m = raw.match(/^upstream\s+(.+?)\s+is\s+failing$/i);
  if (m) {
    return `Peeche ka upstream server ${m[1]} fail ho raha hai (stream ruk sakti hai)`;
  }

  // 4. "x is failing, and its failures came before y% of past glitches here"
  m = raw.match(/^(.+?)\s+is\s+failing,\s+and\s+its\s+failures\s+came\s+before\s+(\d+)%\s+of\s+past\s+glitches\s+here$/i);
  if (m) {
    return `${m[1]} fail ho raha hai; purane ${m[2]}% glitches iske baad hi aaye the`;
  }

  // 5. "glitches are usually higher around 18:00 IST"
  m = raw.match(/^glitches\s+are\s+usually\s+higher\s+around\s+(\d+):00\s+IST$/i);
  if (m) {
    return `Aamtaur par shaam ${m[1]}:00 IST ke aaspaas glitches badh jaate hain`;
  }

  // 6. "learned model: 93% chance in the next 10 min (its glitches in the last hour, slow delivery)"
  m = raw.match(/^learned\s+model:\s+(\d+)%\s+chance\s+in\s+the\s+next\s+10\s+min(?:\s*\((.*?)\))?$/i);
  if (m) {
    let drivers = m[2] || "";
    if (drivers) {
      drivers = drivers.split(",").map(d => {
        const dt = d.trim();
        return HINGLISH_MAP[dt] || dt;
      }).join(", ");
    }
    return `AI Alert: Agale 10 minute mein glitch/outage ka ${m[1]}% high risk hai${drivers ? ` (${drivers} ki wajah se)` : ""}`;
  }

  // 7. "35% of checks failed (120 of 340)"
  m = raw.match(/^(\d+)%\s+of\s+checks\s+failed\s+\((.*?)\)$/i);
  if (m) {
    return `${m[1]}% health checks fail hue (${m[2]})`;
  }

  // 8. "Likely next: STALE_SEGMENTS 5 min (85%)"
  m = raw.match(/^Likely\s+next:\s+(.+?)\s+\(([\d.]+)%\)$/i);
  if (m) {
    return `Agla predicted issue: ${m[1]} (${m[2]}% probability)`;
  }

  // 9. "Notice: ..."
  m = raw.match(/^Notice:\s+(.*)$/i);
  if (m) {
    return `Dhyan dein: ${m[1]}`;
  }

  // 10. "20 short blips"
  m = raw.match(/^(\d+)\s+short\s+blips$/i);
  if (m) {
    return `${m[1]} chote network jhatke (blips)`;
  }

  // 11. "Glitches likely in the next 10 min: ..."
  m = raw.match(/^Glitches\s+likely\s+in\s+the\s+next\s+10\s+min:\s+(.*)$/i);
  if (m) {
    return `Agale 10 min mein glitch ka khatra: ${trHinglish(m[1])}`;
  }

  // Replace sub-phrases if any
  let out = raw;
  for (const [en, hi] of Object.entries(HINGLISH_MAP)) {
    if (out.includes(en)) {
      out = out.replaceAll(en, hi);
    }
  }

  return out;
}

function applyLanguage(lang) {
  if (lang !== "hi" && lang !== "en") lang = "en";
  if (typeof store !== "undefined") store.set("stream_graph_lang", lang);
  document.documentElement.setAttribute("data-lang", lang);

  // Sync segmented buttons
  $$(".lang-seg-btn").forEach(btn => {
    const isMatch = btn.dataset.lang === lang;
    btn.classList.toggle("active", isMatch);
    btn.setAttribute("aria-checked", isMatch ? "true" : "false");
  });

  // Re-render UI components if active
  refreshHinglishUI();
}

function refreshHinglishUI() {
  const isHi = getLanguage() === "hi";

  // 1. Refresh Servers View if active
  if (typeof renderServers === "function" && $("#view-servers")?.classList.contains("active")) {
    renderServers();
  }

  // 2. Refresh Node Detail if currently open
  const ndModal = $("#node-detail-modal") || $("#view-node-detail");
  if (ndModal && !ndModal.hidden && typeof activeNodeId !== "undefined" && activeNodeId) {
    const glitchBox = $(".nd-glitch-container");
    if (glitchBox && typeof glitchSectionHtml === "function") {
      glitchBox.innerHTML = glitchSectionHtml(activeNodeId);
    }
  }

  // 3. Refresh Agent Briefing if active
  if (typeof renderAgentBriefing === "function") {
    renderAgentBriefing();
  }
}

function initLanguage() {
  const current = getLanguage();
  applyLanguage(current);

  // Delegated click handler for language switcher
  document.addEventListener("click", e => {
    const btn = e.target.closest(".lang-seg-btn");
    if (btn && btn.dataset.lang) {
      applyLanguage(btn.dataset.lang);
    }
  });
}

// Auto-initialize on load
if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initLanguage);
  } else {
    initLanguage();
  }
}
