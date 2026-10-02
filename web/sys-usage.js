"use strict";

// ---------- Header meter: this server's RAM and CPU (GET /api/system), like Colab's ----------
// Two thin bars beside the theme toggle, refreshed every few seconds while the page is visible; green, amber
// above 70 %, red above 90 %. Hovering shows the exact numbers.

const SYS_POLL_MS = 4000;
const fmtBytes = b => b >= 1073741824 ? `${(b / 1073741824).toFixed(2)} GB` : `${Math.round(b / 1048576)} MB`;

function setMeter(row, pct, text) {
  const bar = $("i", row);
  bar.style.width = `${Math.max(2, Math.min(100, pct || 0))}%`;
  bar.className = pct >= 90 ? "bad" : pct >= 70 ? "warn" : "";
  $(".sys-val", row).textContent = text;
}

async function refreshSystemUsage() {
  if (document.hidden) return;
  try {
    const res = await fetch("/api/system");
    if (!res.ok) return;
    const u = await res.json();
    const box = $("#sys-usage");
    if (u.memPercent == null && u.cpu == null) { box.hidden = true; return; }  // not measurable here
    box.hidden = false;
    setMeter($("#sys-ram"), u.memPercent, u.memPercent == null ? "–" : `${Math.round(u.memPercent)}%`);
    setMeter($("#sys-cpu"), u.cpu, u.cpu == null ? "–" : `${Math.round(u.cpu)}%`);
    box.title = [
      u.memUsed != null ? `RAM: ${fmtBytes(u.memUsed)} of ${u.memTotal ? fmtBytes(u.memTotal) : "?"}` +
        (u.memLimited ? " (container limit)" : " (machine total)") : null,
      u.cpu != null ? `CPU: ${u.cpu}% of ${u.cores} core${u.cores === 1 ? "" : "s"}` : null,
      "Used by this monitoring app's server",
    ].filter(Boolean).join("\n");
    box.setAttribute("aria-label", box.title.replace(/\n/g, ". "));
  } catch { /* the meter is optional */ }
}

refreshSystemUsage();
setInterval(refreshSystemUsage, SYS_POLL_MS);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshSystemUsage(); });
