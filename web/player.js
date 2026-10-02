"use strict";

// ---------- HLS preview player ----------
const modal = $("#player-modal"), video = $("#player-video"), overlay = $("#player-overlay");
const qualitySel = $("#player-quality");
let hls = null, statsTimer = null, playerNode = null;

function playerMessage(text) { overlay.hidden = !text; overlay.textContent = text || ""; }
video.addEventListener("playing", () => playerMessage(""));  // whatever the message was, frames are flowing now
overlay.addEventListener("click", () => video.play().catch(() => {}));
function stopPlayback() {
  clearInterval(statsTimer); statsTimer = null;
  if (hls) { hls.destroy(); hls = null; }
  video.removeAttribute("src"); video.load();
}
function closePlayer() { stopPlayback(); modal.hidden = true; playerNode = null; }
$(".close", modal).onclick = closePlayer;
modal.addEventListener("click", e => { if (e.target === modal) closePlayer(); });
document.addEventListener("keydown", e => { if (e.key === "Escape" && !modal.hidden) closePlayer(); });

function openPlayer(finalId, onlyUrl) {
  const n = byId[finalId]; if (!n) return;
  playerNode = n;
  const role = Object.fromEntries((n.links || []).map(l => [l.url, l]));
  const urls = onlyUrl ? [onlyUrl] : n.urls || [];
  const channel = onlyUrl && role[onlyUrl] ? role[onlyUrl].channel : null;
  $("#player-title").textContent = channel ? `Video preview · ${channel}` : `Video preview · ${finalId}`;
  $("#player-tabs").innerHTML = urls.map((u, i) => {
    const h = (n.urlHealth || {})[u] || {};
    const color = h.up === false ? "var(--down)" : h.up ? "var(--up)" : "var(--unknown)";
    return `<button data-url="${esc(u)}" class="${i === 0 ? "on" : ""}"><span class="dot" style="background:${color}"></span>${esc(role[u]?.channel || u.split("/").slice(-2, -1)[0] || u)}</button>`;
  }).join("");
  $("#player-tabs").hidden = urls.length < 2;  // one channel card = one stream, no tabs needed
  modal.hidden = false;
  if (urls.length) playUrl(urls[0]); else playerMessage("This FinalLink has no URLs.");
}
$("#player-tabs").addEventListener("click", e => {
  const b = e.target.closest("button[data-url]"); if (!b) return;
  $$("#player-tabs button").forEach(x => x.classList.toggle("on", x === b));
  playUrl(b.dataset.url);
});
qualitySel.onchange = () => { if (hls) hls.currentLevel = +qualitySel.value; };

function playUrl(url) {
  stopPlayback();
  const h = (playerNode.urlHealth || {})[url] || {};
  $("#player-sub").textContent = url + (h.detail ? ` · last check: ${h.detail}` : "") + (h.lastDown ? ` · last down ${fmtTime(h.lastDown)}` : "");
  $("#player-open").href = url;
  qualitySel.innerHTML = `<option value="-1">Auto</option>`;
  playerMessage("Loading stream…");

  if (window.Hls && Hls.isSupported()) {
    hls = new Hls({liveSyncDurationCount: 3});
    hls.on(Hls.Events.MANIFEST_PARSED, (_e, data) => {
      qualitySel.innerHTML = `<option value="-1">Auto</option>` + data.levels.map((l, i) =>
        `<option value="${i}">${l.height ? l.height + "p" : "Level " + (i + 1)}${l.bitrate ? " · " + Math.round(l.bitrate / 1000) + " kbps" : ""}</option>`).join("");
      video.play().catch(() => playerMessage("Press play to start"));
    });
    hls.on(Hls.Events.FRAG_BUFFERED, () => { if (!overlay.hidden && overlay.textContent === "Loading stream…") playerMessage(""); });
    hls.on(Hls.Events.ERROR, (_e, data) => {
      if (!data.fatal) return;
      if (data.type === Hls.ErrorTypes.MEDIA_ERROR) { hls.recoverMediaError(); return; }
      const status = data.response && data.response.code ? ` (HTTP ${data.response.code})` : "";
      playerMessage(`Can't play this stream: ${data.details}${status}`);
    });
    hls.loadSource(url);
    hls.attachMedia(video);
  } else if (video.canPlayType("application/vnd.apple.mpegurl")) {  // Safari plays HLS natively
    video.src = url;
    video.addEventListener("loadeddata", () => playerMessage(""), {once: true});
    video.play().catch(() => playerMessage("Press play to start"));
  } else {
    playerMessage("This browser can't play HLS, and the hls.js player didn't load (check the internet connection).");
    return;
  }
  video.onerror = () => playerMessage("The video element reported an error while playing this stream.");
  statsTimer = setInterval(renderPlayerStats, 1000);
}

function renderPlayerStats() {
  const parts = [];
  if (video.videoWidth) parts.push(`<b>${video.videoWidth}×${video.videoHeight}</b>`);
  if (hls && hls.levels && hls.levels.length) {
    const lvl = hls.levels[hls.currentLevel >= 0 ? hls.currentLevel : hls.loadLevel] || {};
    if (lvl.bitrate) parts.push(`<b>${Math.round(lvl.bitrate / 1000)}</b> kbps${hls.autoLevelEnabled ? " (auto)" : ""}`);
    if (hls.latency) parts.push(`latency <b>${hls.latency.toFixed(1)}</b> s`);
  }
  const b = video.buffered;
  if (b.length) parts.push(`buffer <b>${Math.max(0, b.end(b.length - 1) - video.currentTime).toFixed(1)}</b> s`);
  const q = video.getVideoPlaybackQuality ? video.getVideoPlaybackQuality() : null;
  if (q) parts.push(`dropped <b>${q.droppedVideoFrames}</b>/${q.totalVideoFrames} frames`);
  parts.push(video.paused ? "paused" : "▶ playing");
  $("#player-stats").innerHTML = parts.join(" · ");
}
