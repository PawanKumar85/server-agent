"use strict";

let alertedChannelStops = new Set();
function checkChannelIngestAlerts(spiders) {
  for (const s of (spiders || [])) {
    if (s.status === "STOPPED" && String(s.stopReason || "").includes("404")) {
      const chName = typeof spiderName === "function" ? spiderName(s.finalLinkId) : s.finalLinkId;
      const alertKey = `${s.id}|${s.stopReason}`;
      if (!alertedChannelStops.has(alertKey)) {
        alertedChannelStops.add(alertKey);
        const host = s.at ? (typeof alertShort === "function" ? alertShort(s.at) : s.at) : "upstream origin";
        toast(
          "error",
          `🚨 ${chName.toUpperCase()} · Ingest Missing (404)`,
          `Origin ${host} is missing stream /${chName}/index.m3u8. MCR team: please check and restart live encoder push.`
        );
      }
    }
  }
}

// Reloads the graph after every check run. A tab in the background skips it and catches up once when it's
// shown again; reloads asked for while one is still loading are merged into a single follow-up.
const refreshState = {inFlight: null, again: false, stale: false};
async function refreshAll() {
  if (document.hidden) { refreshState.stale = true; return; }
  if (refreshState.inFlight) { refreshState.again = true; return refreshState.inFlight; }
  refreshState.inFlight = (async () => {
    try {
      const [{data: graph}] = await Promise.all([api("GET", "/api/graph"), loadSpiders()]);
      if (!running) renderGraph(graph);
      if (typeof renderServers === "function") renderServers();  // the Servers page, when it's open
      renderNodesTable();
      checkChannelIngestAlerts(graph.spiders || []);
      if (typeof loadNotifications === "function") loadNotifications();
    } catch (err) {
      toast("error", "Couldn't load data", err.message);
    }
  })();
  try { await refreshState.inFlight; } finally { refreshState.inFlight = null; }
  if (refreshState.again) { refreshState.again = false; return refreshAll(); }
}
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && refreshState.stale) { refreshState.stale = false; refreshAll(); }
});
linkRows = [blankLink()]; renderLinkRows();
setupExcelImport();
refreshAll().then(() => {
  const path = window.location.pathname.replace(/\/+$/, "") || "/";
  const initialView = (typeof ROUTE_VIEWS !== "undefined" && ROUTE_VIEWS[path]) || store.get("view") || "workflow";
  showView(initialView, false);
  checkServerVersion().then(ok => { if (ok) connectStream(); });
  loadRanking();  // after the graph exists, so a run in progress can be animated
});
