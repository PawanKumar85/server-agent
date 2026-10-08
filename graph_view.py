"""n8n-style workflow view of the topology, node health and spider positions.

Layout is computed here (columns by media-flow depth, ordered to reduce edge
crossings); the canvas itself is self-contained HTML/SVG/JS for
`streamlit.components.v1.html`, with pan, zoom, node dragging and a detail panel.
"""

import json
from typing import Dict, List, Optional

from metrics import RETENTION_DAYS, store
from heatmap import generate_3d_heatmap_base64
from nodes import current_topology, load_json_list

CARD_W, CARD_H = 250, 86
COL_GAP, ROW_GAP = 150, 56
ROLE_ORDER = ["FinalLink", "Transcoding", "MainInput", "BackupLink"]  # icon priority


def fetch_graph(driver, incidents=None) -> dict:
    incidents = incidents or store()
    nodes = [
        dict(r) for r in driver.execute_query(
            """
            MATCH (n:Domain) WHERE NOT n.domain ENDS WITH '.invalid'  // hide test fixtures
            RETURN n.domain AS id, [l IN labels(n) WHERE l <> 'Domain'] AS labels, n.status AS status,
                   n.pingCount AS pingCount, n.failedCount AS failedCount, n.lastLatencyMs AS latency,
                   n.consecutiveFailures AS consecutiveFailures, n.lastError AS lastError,
                   toString(n.lastPing) AS lastPing, toString(n.lastRecovery) AS lastRecovery,
                   n.server_ip AS serverIp, n.url AS urls, n.links AS links, n.urlHealth AS urlHealth,
                   n.embedding AS embedding, properties(n).incidentStats AS incidentStats,
                   properties(n).blipCount AS blipCount, properties(n).lastBlip AS lastBlip,
                   toString(properties(n).embeddingSyncedAt) AS embeddingSyncedAt
            ORDER BY id
            """
        ).records
    ]
    # Checks passed from the kept history, without ignored streams or ones no longer on the server (the lifetime
    # counters on the node can't take a stream back out). Servers with no stream history keep the counters.
    import health
    counted = {u for n in nodes for u in (n["urls"] or [])} - health.ignored_urls()
    passed = incidents.checks_passed(counted)
    for n in nodes:
        p = passed.get(n["id"])
        if p and p["checks"]:
            n["pingCount"], n["failedCount"] = p["checks"], p["failed"]
            n["checksWindowDays"] = RETENTION_DAYS
    for n in nodes:
        n["links"] = load_json_list(n["links"])
        try:
            raw_uh = json.loads(n["urlHealth"]) if n["urlHealth"] else {}
        except ValueError:
            raw_uh = {}

        # Compress urlHealth: strip nulls, keep only essential up/detail/lastDown
        comp_uh = {}
        for u, h in raw_uh.items():
            entry = {"up": h.get("up", True)}
            if h.get("detail"):
                entry["detail"] = h["detail"]
            if h.get("lastDown"):
                entry["lastDown"] = h["lastDown"]
            for key in ("ignored", "category", "freshness", "segmentAgeS", "targetS", "bitrates", "resolutions", "cdn_cache", "server_hdr", "discontinuities"):
                val = h.get(key)
                if val is not None and val != [] and val != "":
                    entry[key] = val
            comp_uh[u] = entry
        n["urlHealth"] = comp_uh

        # Enrich links directly with live up/detail health state
        for l in n["links"]:
            u = l.get("url")
            if u in comp_uh:
                l["up"] = comp_uh[u].get("up", True)
                if "detail" in comp_uh[u]:
                    l["detail"] = comp_uh[u]["detail"]

        n["log"] = incidents.incidents(n["id"], limit=50) or None  # the incident log lives in SQLite

        try:
            n["incidentStats"] = json.loads(n["incidentStats"]) if n.get("incidentStats") else None
        except ValueError:
            n["incidentStats"] = None

        if n.get("latency") is not None:
            n["latency"] = round(n["latency"], 1)


    edges = [r.model_dump() for r in current_topology(driver)]
    spiders = [
        dict(r) for r in driver.execute_query(
            """
            MATCH (s:SpiderRun) WHERE NOT s.finalLinkId ENDS WITH '.invalid'
            OPTIONAL MATCH (s)-[:AT]->(n)
            RETURN s.id AS id, s.finalLinkId AS finalLinkId, s.status AS status,
                   s.direction AS direction, n.domain AS at, s.stepCount AS steps,
                   toString(s.lastStepAt) AS lastStepAt, s.stopReason AS stopReason,
                   s.rca AS rca, s.embedding AS embedding
            ORDER BY id
            """
        ).records
    ]
    for s in spiders:
        if s.get("rca"):
            try:
                rca_obj = json.loads(s["rca"])
                if s["status"] == "RUNNING" and not rca_obj.get("failover"):
                    s["rca"] = None
                else:
                    s["rca"] = {k: v for k, v in rca_obj.items() if v not in (None, [], "")}
            except Exception:
                s["rca"] = None
        else:
            s["rca"] = None


    return {"nodes": nodes, "edges": edges, "spiders": spiders}


# One column per role, left to right in media-flow order. A node with several roles goes in the
# column of its processing role (ingest = Backup + Transcoding -> Transcoding); an input host that
# is Main for some channels and Backup for others counts as a Main (cloud -> Main column).
ROLE_COLUMNS = ["MainInput", "BackupLink", "Transcoding", "FinalLink"]


def role_column(labels: List[str]) -> Optional[int]:
    for role in ("FinalLink", "Transcoding", "MainInput", "BackupLink"):
        if role in labels:
            return ROLE_COLUMNS.index(role)
    return None


def layout(nodes: List[dict], edges: List[dict]) -> Dict[str, dict]:
    """Columns by role (Main | Backup | Transcoding | Final); rows ordered by what each node feeds,
    so lines cross as little as possible. Nodes without a role fall back to upstream depth."""
    ids = [n["id"] for n in nodes]
    incoming: Dict[str, List[str]] = {i: [] for i in ids}
    outgoing: Dict[str, List[str]] = {i: [] for i in ids}
    for e in edges:
        if e["source"] in incoming and e["target"] in incoming:
            outgoing[e["source"]].append(e["target"])
            incoming[e["target"]].append(e["source"])

    rank: Dict[str, int] = {}

    def depth(node_id: str, seen: frozenset) -> int:
        if node_id not in rank:
            ups = [u for u in incoming[node_id] if u not in seen]  # tolerate cycles
            rank[node_id] = 1 + max((depth(u, seen | {node_id}) for u in ups), default=-1)
        return rank[node_id]

    for i in ids:
        depth(i, frozenset())
    by_role = {n["id"]: role_column(n.get("labels", [])) for n in nodes}
    for i in ids:
        if by_role[i] is not None:
            rank[i] = by_role[i]
    # drop empty columns (e.g. no BackupLink nodes) so the canvas has no gaps
    used = sorted(set(rank.values()))
    rank = {i: used.index(r) for i, r in rank.items()}

    columns: Dict[int, List[str]] = {}
    for i in ids:
        columns.setdefault(rank[i], []).append(i)
    last = max(columns) if columns else 0
    y_of: Dict[str, float] = {}
    order: Dict[int, List[str]] = {}
    # Right to left: finals alphabetically, then each column by the mean row of what it feeds.
    for col in range(last, -1, -1):
        members = columns.get(col, [])
        if col == last:
            members = sorted(members)
        else:
            def key(i: str):
                targets = [y_of[t] for t in outgoing[i] if t in y_of]
                return (sum(targets) / len(targets) if targets else float("inf"), i)
            members = sorted(members, key=key)
        order[col] = members
        for idx, i in enumerate(members):
            y_of[i] = idx - (len(members) - 1) / 2

    tallest = max((len(m) for m in order.values()), default=1)
    positions = {}
    for col, members in order.items():
        top = (tallest - len(members)) * (CARD_H + ROW_GAP) / 2
        for idx, i in enumerate(members):
            positions[i] = {"x": 40 + col * (CARD_W + COL_GAP), "y": 40 + top + idx * (CARD_H + ROW_GAP)}
    return positions


def render_html(graph: dict, height: int = 640, replay: Optional[List[dict]] = None) -> str:
    """`replay` is a spider cycle's event timeline (CycleResult.events); the canvas animates it."""
    graph = dict(graph, positions=layout(graph["nodes"], graph["edges"]), card={"w": CARD_W, "h": CARD_H},
                 roleOrder=ROLE_ORDER, replay=replay or [])
    data = json.dumps(graph, default=str).replace("</", "<\\/")
    try:
        heatmap_b64 = generate_3d_heatmap_base64(graph.get("nodes", []))
    except Exception:
        heatmap_b64 = ""
    return (
        TEMPLATE.replace("__DATA__", data)
        .replace("__HEIGHT__", str(height))
        .replace("__HEATMAP_B64__", heatmap_b64)
    )


TEMPLATE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<style>
  :root {
    --canvas: #f4f4f6; --dot: #d4d4da; --card: #ffffff; --card-border: #dcdce2; --text: #1f2330;
    --muted: #6b7080; --edge: #9aa0ad; --panel: #ffffff; --shadow: 0 1px 3px rgba(20,20,40,.08), 0 6px 18px rgba(20,20,40,.06);
    --up: #16a34a; --down: #dc2626; --unknown: #9ca3af;
    --MainInput: #2563eb; --BackupLink: #7c3aed; --Transcoding: #d97706; --FinalLink: #0d9488;
    --RUNNING: #16a34a; --STOPPED: #dc2626; --RECOVERED: #2563eb; --ERROR: #ea580c;
  }
  @media (prefers-color-scheme: dark) {
    :root { --canvas: #16171d; --dot: #2c2e38; --card: #22242d; --card-border: #353846; --text: #e8e9ee;
      --muted: #9a9fb0; --edge: #5d6272; --panel: #22242d; --shadow: 0 1px 3px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.35); }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; font: 13px/1.35 -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, sans-serif; color: var(--text); }
  #canvas { position: relative; height: __HEIGHT__px; overflow: hidden; background: var(--canvas);
    background-image: radial-gradient(var(--dot) 1.2px, transparent 1.2px); background-size: 22px 22px;
    border-radius: 10px; cursor: grab; user-select: none; }
  #canvas.panning { cursor: grabbing; }
  #world { position: absolute; left: 0; top: 0; transform-origin: 0 0; }
  svg#edges { position: absolute; left: 0; top: 0; overflow: visible; pointer-events: none; }
  .edge { fill: none; stroke: var(--edge); stroke-width: 2; }
  .edge.flow { stroke-dasharray: 6 6; animation: flow 1.1s linear infinite; }
  .edge.down { stroke: var(--down); stroke-dasharray: 5 5; opacity: .85; }
  @keyframes flow { to { stroke-dashoffset: -12; } }
  .edge-label { font-size: 10px; fill: var(--muted); letter-spacing: .04em; }
  .edge-label-bg { fill: var(--canvas); }

  .node { position: absolute; background: var(--card); border: 1.5px solid var(--card-border); border-radius: 12px;
    box-shadow: var(--shadow); padding: 10px 12px 10px 12px; cursor: pointer; display: flex; gap: 10px; }
  .node:hover { border-color: var(--muted); }
  .node.selected { border-color: var(--text); }
  .node.root { border-color: var(--down); animation: pulse 1.6s ease-out infinite; }
  @keyframes pulse { 0% { box-shadow: 0 0 0 0 rgba(220,38,38,.45);} 100% { box-shadow: 0 0 0 14px rgba(220,38,38,0);} }
  .icon { flex: none; width: 40px; height: 40px; border-radius: 10px; display: grid; place-items: center;
    color: #fff; font-weight: 700; font-size: 15px; }
  .body { min-width: 0; flex: 1; }
  .title { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; display: flex; align-items: center; gap: 6px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; flex: none; }
  .chips { display: flex; gap: 4px; flex-wrap: wrap; margin: 5px 0 6px; }
  .chip { font-size: 10px; padding: 1px 6px; border-radius: 999px; color: #fff; }
  .stats { color: var(--muted); font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .port { position: absolute; top: 50%; width: 10px; height: 10px; margin-top: -5px; border-radius: 50%;
    background: var(--card); border: 2px solid var(--edge); }
  .port.in { left: -6px; } .port.out { right: -6px; }
  .spider { position: absolute; z-index: 4; font-size: 11px; font-weight: 600; padding: 1px 8px 1px 5px; border-radius: 999px;
    background: var(--card); border: 1.5px solid; box-shadow: var(--shadow); white-space: nowrap; display: inline-flex;
    align-items: center; gap: 3px; pointer-events: none; }
  .spider b { font-size: 14px; line-height: 1; }
  .spider.walking b { display: inline-block; animation: bob .3s ease-in-out infinite alternate; }
  @keyframes bob { from { transform: translateY(0) rotate(-8deg); } to { transform: translateY(-3px) rotate(8deg); } }

  /* live check: radar rings while pinging (dashed = checked from downstream, spider not on it) */
  .node::before, .node::after { content: ""; position: absolute; inset: -3px; border-radius: 14px;
    border: 2px solid var(--RECOVERED); opacity: 0; pointer-events: none; }
  .node.checking::before, .node.checking::after { animation: radar 1.1s ease-out infinite; }
  .node.checking::after { animation-delay: .55s; }
  .node.peek::before, .node.peek::after { border-style: dashed; }
  @keyframes radar { 0% { opacity: .9; transform: scale(1); } 100% { opacity: 0; transform: scale(1.12, 1.4); } }
  .node.flash-ok { animation: ok .9s ease-out; }
  .node.flash-fail { animation: fail .9s ease-out; }
  @keyframes ok { 0% { box-shadow: 0 0 0 0 rgba(22,163,74,.6); } 100% { box-shadow: 0 0 0 18px rgba(22,163,74,0); } }
  @keyframes fail { 0% { box-shadow: 0 0 0 0 rgba(220,38,38,.6); } 100% { box-shadow: 0 0 0 18px rgba(220,38,38,0); } }
  .dot.pending { background: var(--unknown) !important; animation: blink .7s ease-in-out infinite alternate; }
  @keyframes blink { to { opacity: .2; } }
  .stats.live { color: var(--RECOVERED); font-weight: 600; }
  .stats.good { color: var(--up); font-weight: 600; }
  .stats.bad { color: var(--down); font-weight: 600; }

  #lists { position: absolute; right: 12px; top: 12px; width: 250px; background: var(--panel); border: 1px solid var(--card-border);
    border-radius: 12px; box-shadow: var(--shadow); z-index: 5; font-size: 11.5px; cursor: auto; padding: 8px 10px 10px; }
  #lists header { display: flex; align-items: center; justify-content: space-between; font-weight: 600; margin-bottom: 2px; }
  #lists header .toggle { background: none; border: 0; color: var(--muted); cursor: pointer; font: inherit; padding: 0 2px; }
  #lists.collapsed .types { display: none; }
  #lists h4 { margin: 8px 0 5px; font-size: 10px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }
  #lists .types { display: flex; flex-direction: column; }
  #lists .chips-row { display: flex; flex-wrap: wrap; gap: 5px; }
  #lists .chip-btn { border: 1.5px solid transparent; cursor: pointer; font: inherit; font-size: 11px; line-height: 1.5; }
  #lists .label-chip { color: #fff; border-radius: 999px; padding: 1px 9px; }
  #lists .type-chip { background: var(--canvas); color: var(--text); border: 1.5px solid var(--card-border); border-radius: 4px;
    padding: 1px 8px; letter-spacing: .03em; }
  #lists .chip-btn.on { border-color: var(--text); box-shadow: 0 0 0 2px var(--panel), 0 0 0 3.5px var(--text); }
  #lists .count { opacity: .8; margin-left: 3px; font-variant-numeric: tabular-nums; }
  #lists .hint { color: var(--muted); font-size: 10.5px; margin-top: 8px; }
  .node.dim, .spider.dim, .edge.dim { opacity: .18; transition: opacity .2s; }
  .edge.hl { stroke: var(--text); stroke-width: 3.5; opacity: 1; }
  .node.hl { border-color: var(--text); }

  #log { position: absolute; right: 12px; bottom: 12px; width: 360px; max-height: 42%; display: none; flex-direction: column;
    background: var(--panel); border: 1px solid var(--card-border); border-radius: 12px; box-shadow: var(--shadow); z-index: 5;
    font-size: 11.5px; cursor: auto; }
  #log header { display: flex; align-items: center; justify-content: space-between; padding: 7px 10px; font-weight: 600;
    border-bottom: 1px solid var(--card-border); }
  #log header .state { font-weight: 500; color: var(--muted); }
  #log ol { list-style: none; margin: 0; padding: 4px 10px 8px; overflow: auto; font-variant-numeric: tabular-nums; }
  #log li { padding: 2px 0; display: grid; grid-template-columns: 44px 1fr; gap: 6px; }
  #log li span { overflow-wrap: anywhere; }
  #log header { cursor: pointer; }
  #log.collapsed ol { display: none; }
  #log.collapsed header { border-bottom: 0; }
  #log li time { color: var(--muted); }
  #log li.new { animation: fadein .4s ease-out; }
  @keyframes fadein { from { opacity: 0; transform: translateY(4px); } }

  #toolbar { position: absolute; left: 12px; bottom: 12px; display: flex; gap: 6px; z-index: 5; }
  #toolbar button { background: var(--panel); color: var(--text); border: 1px solid var(--card-border); border-radius: 8px;
    padding: 5px 10px; font: inherit; cursor: pointer; box-shadow: var(--shadow); }
  #legend { position: absolute; left: 12px; top: 12px; background: var(--panel); border: 1px solid var(--card-border);
    border-radius: 10px; padding: 6px 10px; box-shadow: var(--shadow); z-index: 5; font-size: 11px; color: var(--muted);
    display: flex; flex-wrap: wrap; gap: 3px 12px; max-width: calc(100% - 380px); }
  #legend span { display: inline-flex; align-items: center; gap: 5px; }
  #legend i { width: 9px; height: 9px; border-radius: 3px; display: inline-block; }
  #panel { position: absolute; right: 12px; top: 12px; bottom: 12px; width: 340px; background: var(--panel);
    border: 1px solid var(--card-border); border-radius: 12px; box-shadow: var(--shadow); padding: 14px 16px;
    overflow: auto; z-index: 6; display: none; cursor: auto; user-select: text; }
  #panel h3 { margin: 0 0 4px; font-size: 15px; }
  #panel .close { position: absolute; right: 10px; top: 8px; background: none; border: 0; font-size: 18px; color: var(--muted); cursor: pointer; }
  #panel table { width: 100%; border-collapse: collapse; margin-top: 6px; font-size: 11.5px; }
  #panel td { padding: 4px 0; border-top: 1px solid var(--card-border); vertical-align: top; word-break: break-all; }
  #panel .kv { display: grid; grid-template-columns: auto 1fr; gap: 2px 10px; margin: 8px 0; font-size: 12px; }
  #panel .kv b { color: var(--muted); font-weight: 500; }
  .sub { color: var(--muted); font-size: 11px; }

  #heatmap-panel { position: absolute; left: 12px; bottom: 50px; width: 360px; background: var(--panel); border: 1px solid var(--card-border);
    border-radius: 12px; box-shadow: var(--shadow); z-index: 5; font-size: 11.5px; cursor: auto; overflow: hidden; transition: width .2s ease; }
  #heatmap-panel header { display: flex; align-items: center; justify-content: space-between; padding: 7px 10px; font-weight: 600; cursor: pointer; user-select: none; border-bottom: 1px solid var(--card-border); }
  #heatmap-panel header .title { display: flex; align-items: center; gap: 6px; }
  #heatmap-panel .widget-controls { display: flex; align-items: center; gap: 4px; }
  #heatmap-panel .widget-controls button { background: none; border: 0; color: var(--muted); cursor: pointer; padding: 0 4px; font-size: 13px; line-height: 1; border-radius: 4px; }
  #heatmap-panel .widget-controls button:hover { color: var(--text); background: var(--canvas); }
  #heatmap-panel .heatmap-body { padding: 8px 10px; display: flex; flex-direction: column; gap: 8px; }
  #heatmap-panel.collapsed { width: auto; }
  #heatmap-panel.collapsed .heatmap-body { display: none; }
  #heatmap-panel.collapsed header { border-bottom: 0; }
  #heatmap-panel .heatmap-img-wrap { position: relative; width: 100%; border-radius: 8px; overflow: hidden; background: #16171d; border: 1px solid var(--card-border); min-height: 130px; display: flex; align-items: center; justify-content: center; }
  #heatmap-panel .heatmap-img-wrap img { width: 100%; height: auto; display: block; }
  #heatmap-panel .heatmap-meta { display: flex; align-items: center; justify-content: space-between; font-size: 11px; }
  #heatmap-panel .badge-down { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 999px; background: rgba(220, 38, 38, 0.15); color: var(--down); font-weight: 600; font-size: 11px; }
  #heatmap-panel .badge-down.ok { background: rgba(22, 163, 74, 0.15); color: var(--up); }
  #heatmap-panel .heatmap-nodes-list { display: flex; flex-wrap: wrap; gap: 5px; max-height: 60px; overflow-y: auto; }
  #heatmap-panel .node-pill { padding: 2px 7px; border-radius: 6px; background: var(--canvas); border: 1px solid var(--card-border); font-size: 10.5px; cursor: pointer; display: inline-flex; align-items: center; gap: 5px; }
  #heatmap-panel .node-pill:hover { border-color: var(--text); }
  #heatmap-panel .node-pill .fail-count { color: var(--down); font-weight: 700; }
</style></head>
<body>
<div id="canvas">
  <div id="legend"></div>
  <div id="world"><svg id="edges"></svg></div>
  <div id="toolbar"><button id="fit">Fit</button><button id="zin">+</button><button id="zout">−</button><button id="btn-toggle-heatmap" title="Toggle 3D Heatmap">🔥 Heatmap</button><button id="replay" style="display:none">▶ Replay</button></div>
  <div id="heatmap-panel">
    <header>
      <div class="title"><span class="flame">🔥</span><span>Downtime Heatmap (3D)</span></div>
      <div class="widget-controls">
        <button class="toggle" id="heatmap-toggle" title="Minimize / Expand">▾</button>
      </div>
    </header>
    <div class="heatmap-body">
      <div class="heatmap-meta">
        <span class="badge-down" id="heatmap-down-badge">Node Downtime</span>
        <span class="sub" style="font-size:10.5px;color:var(--muted)">Seaborn 3D</span>
      </div>
      <div class="heatmap-img-wrap">
        <img id="heatmap-img" alt="3D Node Downtime Heatmap" src="__HEATMAP_B64__">
      </div>
      <div class="heatmap-nodes-list" id="heatmap-nodes-list"></div>
    </div>
  </div>
  <div id="lists"><header><span>Database</span><button class="toggle" title="Show / hide">▾</button></header>
    <div class="types"><h4>Node labels</h4><div class="chips-row" id="label-chips"></div>
    <h4>Relationship types</h4><div class="chips-row" id="type-chips"></div>
    <div class="hint">Click a label or type to highlight it</div></div></div>
  <div id="log"><header title="Show / hide"><span>Execution log</span><span class="state"></span></header><ol></ol></div>
  <div id="panel"><button class="close" title="Close">×</button><div id="panel-body"></div></div>
</div>
<script>
const G = __DATA__;
const W = G.card.w, H = G.card.h;
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const short = r => ({MainInput: "Main", BackupLink: "Backup", Transcoding: "Transcoding", FinalLink: "Final"}[r] || r);
const statusColor = s => s === "UP" ? css("--up") : s === "DOWN" ? css("--down") : css("--unknown");
const byId = Object.fromEntries(G.nodes.map(n => [n.id, n]));
const pos = G.positions;
const canvas = document.getElementById("canvas"), world = document.getElementById("world"), svg = document.getElementById("edges");
const REPLAY = (G.replay || []).length > 0;
const rootNodes = new Set(REPLAY ? [] : G.spiders.filter(s => s.status === "STOPPED").map(s => s.at));
const live = Object.fromEntries(G.nodes.map(n => [n.id, n.status]));  // status shown on edges; replay updates it

// Legend
document.getElementById("legend").innerHTML =
  ["MainInput","BackupLink","Transcoding","FinalLink"].map(r => `<span><i style="background:var(--${r})"></i>${short(r)}</span>`).join("") +
  `<span><i style="background:var(--up);border-radius:50%"></i>UP</span><span><i style="background:var(--down);border-radius:50%"></i>DOWN</span>` +
  `<span>🕷 spider</span><span><i style="background:var(--STOPPED)"></i>stopped here</span>`;

// Nodes
const els = {};
for (const n of G.nodes) {
  const p = pos[n.id]; if (!p) continue;
  const primary = G.roleOrder.find(r => n.labels.includes(r)) || n.labels[0] || "";
  const el = document.createElement("div");
  el.className = "node" + (rootNodes.has(n.id) ? " root" : "");
  el.style.cssText = `left:${p.x}px;top:${p.y}px;width:${W}px;height:${H}px`;
  const urls = (n.urls || []).length, down = Object.values(n.urlHealth || {}).filter(h => h.up === false).length;
  el.innerHTML = `
    <div class="port in"></div><div class="port out"></div>
    <div class="icon" style="background:var(--${primary})">${esc(short(primary)[0] || "?")}</div>
    <div class="body">
      <div class="title"><span class="dot" style="background:${statusColor(n.status)}"></span>${esc(n.id)}</div>
      <div class="chips">${n.labels.map(l => `<span class="chip" style="background:var(--${l}, #888)">${esc(short(l))}</span>`).join("")}</div>
      <div class="stats">${n.status || "not checked"} · ${urls} URL${urls === 1 ? "" : "s"}${down ? ` (${down} down)` : ""} · ping ${n.pingCount ?? 0}${n.latency != null ? ` · ${Math.round(n.latency)} ms` : ""}</div>
    </div>`;
  el.dataset.stats = el.querySelector(".stats").textContent;
  el.addEventListener("mousedown", e => startDrag(e, n.id));
  el.addEventListener("click", e => { if (!el.dataset.dragged) showPanel(n.id); delete el.dataset.dragged; });
  world.appendChild(el); els[n.id] = el;
}

// Edges. An edge that skips a column is routed through the gap between that column's cards
// instead of passing behind them.
const GAP = 28;
function lanes(x1, y1, x2, y2, skip) {
  const cols = {};
  for (const [id, p] of Object.entries(pos)) {
    if (skip.includes(id) || p.x <= x1 || p.x + W >= x2) continue;
    (cols[Math.round(p.x)] ||= []).push(p);
  }
  return Object.keys(cols).map(Number).sort((a, b) => a - b).map(cx => {
    let y = y1 + (y2 - y1) * ((cx + W / 2 - x1) / (x2 - x1));
    for (let i = 0; i < 4; i++) {  // step out of any card it would cross
      const hit = cols[cx].find(p => y > p.y - GAP / 2 && y < p.y + H + GAP / 2);
      if (!hit) break;
      y = (y - hit.y < H / 2) ? hit.y - GAP : hit.y + H + GAP;
    }
    return {x: cx, y};
  });
}
function curve(x1, y1, x2, y2) {
  const dx = Math.max(40, (x2 - x1) / 2);
  return `C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`;
}
let focus = null;  // {kind: "label" | "type", value} highlighted from the Database panel
function drawEdges() {
  const parts = [];
  for (const e of G.edges) {
    const a = pos[e.source], b = pos[e.target]; if (!a || !b) continue;
    const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2;
    let d = `M${x1},${y1} `, cx = x1, cy = y1;
    const route = lanes(x1, y1, x2, y2, [e.source, e.target]);
    // Label sits on the first segment (before any routed lane).
    const [fx, fy] = route.length ? [route[0].x - 10, route[0].y] : [x2, y2];
    const mx = (x1 + fx) / 2, my = (y1 + fy) / 2;
    for (const lane of route) {
      d += curve(cx, cy, lane.x - 10, lane.y) + ` L${lane.x + W + 10},${lane.y} `;
      cx = lane.x + W + 10; cy = lane.y;
    }
    d += curve(cx, cy, x2, y2);
    const cls = live[e.source] === "DOWN" ? "edge down" : live[e.source] === "UP" ? "edge flow" : "edge";
    const dim = focus && !(focus.kind === "type" && focus.value === e.type) ? " dim" : "";
    const hl = focus && focus.kind === "type" && focus.value === e.type ? " hl" : "";
    parts.push(`<path class="${cls}${hl}${dim}" d="${d}" data-edge="${esc(e.source)}|${esc(e.target)}"/>`,
      `<rect class="edge-label-bg" x="${mx - 30}" y="${my - 8}" width="60" height="14" rx="4"/>`,
      `<text class="edge-label" x="${mx}" y="${my + 3}" text-anchor="middle">${e.type}</text>`);
  }
  svg.innerHTML = parts.join("");
}
drawEdges();

// Pan / zoom / drag
let view = {x: 0, y: 0, k: 1};
const apply = () => { world.style.transform = `translate(${view.x}px,${view.y}px) scale(${view.k})`; };
function fit() {
  const xs = Object.values(pos);
  if (!xs.length) return;
  const minX = Math.min(...xs.map(p => p.x)), maxX = Math.max(...xs.map(p => p.x + W));
  const minY = Math.min(...xs.map(p => p.y)) - 20, maxY = Math.max(...xs.map(p => p.y + H));
  // Leave room on the right for the Nodes & Relations panel while it's open.
  const listsEl = document.getElementById("lists");
  const reserve = listsEl && !listsEl.classList.contains("collapsed") ? listsEl.offsetWidth + 12 : 0;
  const top = document.getElementById("legend").offsetHeight + 12;  // and at the top for the legend
  const cw = canvas.clientWidth - reserve, ch = canvas.clientHeight - top, pad = 50;
  view.k = Math.min(1.2, (cw - pad * 2) / (maxX - minX), (ch - pad * 2) / (maxY - minY));
  view.x = (cw - (maxX - minX) * view.k) / 2 - minX * view.k;
  view.y = top + (ch - (maxY - minY) * view.k) / 2 - minY * view.k;
  apply();
}
function zoomAt(factor, cx, cy) {
  const k = Math.min(2.5, Math.max(0.25, view.k * factor));
  view.x = cx - (cx - view.x) * (k / view.k); view.y = cy - (cy - view.y) * (k / view.k); view.k = k; apply();
}
canvas.addEventListener("wheel", e => {
  e.preventDefault();
  const r = canvas.getBoundingClientRect();
  zoomAt(e.deltaY < 0 ? 1.1 : 1 / 1.1, e.clientX - r.left, e.clientY - r.top);
}, {passive: false});
let pan = null, drag = null;
canvas.addEventListener("mousedown", e => {
  if (e.target.closest(".node, #panel, #toolbar, #legend, #lists, #log, #heatmap-panel")) return;
  pan = {x: e.clientX - view.x, y: e.clientY - view.y}; canvas.classList.add("panning");
});
function startDrag(e, id) { e.stopPropagation(); drag = {id, sx: e.clientX, sy: e.clientY, x: pos[id].x, y: pos[id].y, moved: false}; }
window.addEventListener("mousemove", e => {
  if (pan) { view.x = e.clientX - pan.x; view.y = e.clientY - pan.y; apply(); }
  if (drag) {
    const dx = (e.clientX - drag.sx) / view.k, dy = (e.clientY - drag.sy) / view.k;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
    pos[drag.id] = {x: drag.x + dx, y: drag.y + dy};
    els[drag.id].style.left = pos[drag.id].x + "px"; els[drag.id].style.top = pos[drag.id].y + "px";
    drawEdges(); placeSpiders();
  }
});
window.addEventListener("mouseup", () => {
  if (drag && drag.moved) els[drag.id].dataset.dragged = "1";
  pan = null; drag = null; canvas.classList.remove("panning");
});
document.getElementById("fit").onclick = fit;
document.getElementById("zin").onclick = () => zoomAt(1.2, canvas.clientWidth / 2, canvas.clientHeight / 2);
document.getElementById("zout").onclick = () => zoomAt(1 / 1.2, canvas.clientWidth / 2, canvas.clientHeight / 2);

// Detail panel
const panel = document.getElementById("panel");
panel.querySelector(".close").onclick = () => { panel.style.display = "none"; document.querySelectorAll(".node.selected").forEach(x => x.classList.remove("selected")); };
function showPanel(id) {
  const n = byId[id];
  document.querySelectorAll(".node.selected").forEach(x => x.classList.remove("selected"));
  els[id].classList.add("selected");
  const role = Object.fromEntries((n.links || []).map(l => [l.url, l]));
  const rows = (n.urls || []).map(u => {
    const h = (n.urlHealth || {})[u] || {}, l = role[u] || {};
    const mark = h.up === true ? "✓" : h.up === false ? "✗" : "·";
    return `<tr><td style="width:18px;color:${h.up === false ? "var(--down)" : h.up ? "var(--up)" : "var(--muted)"}">${mark}</td>
      <td>${esc(u)}<div class="sub">${esc(short(l.role || ""))}${l.channel ? " · " + esc(l.channel) : ""}${h.detail ? " · " + esc(h.detail) : ""}${h.lastDown ? " · last down " + esc(h.lastDown) : ""}</div></td></tr>`;
  }).join("");
  const spiders = G.spiders.filter(s => s.at === id).map(s =>
    `<div>🕷 <b>${esc(s.id)}</b> · <span style="color:var(--${s.status})">${esc(s.status)}</span>${s.stopReason ? `<div class="sub">${esc(s.stopReason)}</div>` : ""}</div>`).join("");
  const ups = G.edges.filter(e => e.target === id).map(e => `${esc(e.source)} (${e.type})`).join("<br>") || "—";
  const downs = G.edges.filter(e => e.source === id).map(e => `${esc(e.target)} (${e.type})`).join("<br>") || "—";
  document.getElementById("panel-body").innerHTML = `
    <h3><span class="dot" style="display:inline-block;background:${statusColor(n.status)}"></span> ${esc(id)}</h3>
    <div class="sub">${n.labels.map(short).join(" · ")}${n.serverIp ? " · " + esc(n.serverIp) : ""}</div>
    ${spiders ? `<div style="margin-top:8px">${spiders}</div>` : ""}
    <div class="kv">
      <b>Status</b><span>${esc(n.status || "not checked")}</span>
      <b>Ping count</b><span>${n.pingCount ?? 0}</span>
      <b>Failed</b><span>${n.failedCount ?? 0} (consecutive ${n.consecutiveFailures ?? 0})</span>
      <b>Latency</b><span>${n.latency != null ? Math.round(n.latency) + " ms" : "—"}</span>
      <b>Last ping</b><span>${esc((n.lastPing || "—").replace("T", " ").slice(0, 19))}</span>
      <b>Last recovery</b><span>${esc((n.lastRecovery || "—").replace("T", " ").slice(0, 19))}</span>
      <b>Upstream</b><span>${ups}</span>
      <b>Downstream</b><span>${downs}</span>
    </div>
    ${n.lastError ? `<div class="sub" style="color:var(--down)">Last error: ${esc(n.lastError)}</div>` : ""}
    <table>${rows}</table>`;
  panel.style.display = "block";
}

// Spiders: badges in world coordinates, parked at the top-right of the card they're AT.
const spiderEls = {}, spiderAt = {}, moving = new Set();
const shortName = s => s.finalLinkId.includes("/") ? s.finalLinkId.split("/").pop() : s.finalLinkId.split(".")[0];
function paintSpider(id, status) {
  const el = spiderEls[id];
  el.style.color = el.style.borderColor = `var(--${status}, #666)`;
  el.title = `${id} · ${status}`;
}
for (const s of G.spiders) {
  const el = document.createElement("span");
  el.className = "spider";
  el.innerHTML = `<b>🕷</b>${esc(shortName(s))}`;
  world.appendChild(el); spiderEls[s.id] = el; spiderAt[s.id] = s.at;
  paintSpider(s.id, s.status);
}
function anchor(id, nodeId) {
  const p = pos[nodeId]; if (!p) return {x: 0, y: 0};
  const here = Object.keys(spiderAt).filter(k => spiderAt[k] === nodeId && !moving.has(k)).sort();
  let x = p.x + W - 10;
  for (const k of here) { x -= spiderEls[k].offsetWidth + 4; if (k === id) break; }
  return {x, y: p.y - 13};
}
function placeSpiders() {
  for (const id of Object.keys(spiderEls)) {
    if (moving.has(id) || !spiderAt[id]) { if (!spiderAt[id]) spiderEls[id].style.display = "none"; continue; }
    const a = anchor(id, spiderAt[id]);
    Object.assign(spiderEls[id].style, {display: "", left: a.x + "px", top: a.y + "px"});
  }
}
placeSpiders();

// Walk a spider from its card to `to`, along the connection line when there is one.
function moveSpider(id, to, duration) {
  const from = spiderAt[id], el = spiderEls[id];
  if (!from || !pos[from] || from === to) { spiderAt[id] = to; placeSpiders(); return; }
  const start = anchor(id, from);
  moving.add(id); el.classList.add("walking");
  const pts = [[start.x, start.y]];
  const path = svg.querySelector(`path[data-edge="${CSS.escape(from + "|" + to)}"]`) ||
               svg.querySelector(`path[data-edge="${CSS.escape(to + "|" + from)}"]`);
  const half = el.offsetWidth / 2;
  if (path) {
    const len = path.getTotalLength(), reverse = path.dataset.edge.startsWith(to + "|");
    for (let i = 0; i <= 24; i++) {
      const pt = path.getPointAtLength(len * (reverse ? 1 - i / 24 : i / 24));
      pts.push([pt.x - half, pt.y - 10]);
    }
  }
  spiderAt[id] = to; moving.delete(id);
  const end = anchor(id, to); moving.add(id);
  pts.push([end.x, end.y]);
  const seg = pts.slice(1).map((p, i) => Math.hypot(p[0] - pts[i][0], p[1] - pts[i][1]));
  const total = seg.reduce((a, b) => a + b, 0) || 1, t0 = performance.now();
  (function frame(now) {
    let d = Math.min(1, (now - t0) / duration);
    d = d < .5 ? 2 * d * d : 1 - Math.pow(-2 * d + 2, 2) / 2;  // ease in-out
    let dist = d * total, i = 0;
    while (i < seg.length - 1 && dist > seg[i]) { dist -= seg[i]; i++; }
    const f = seg[i] ? dist / seg[i] : 1;
    el.style.left = pts[i][0] + (pts[i + 1][0] - pts[i][0]) * f + "px";
    el.style.top = pts[i][1] + (pts[i + 1][1] - pts[i][1]) * f + "px";
    if (d < 1) requestAnimationFrame(frame);
    else { moving.delete(id); el.classList.remove("walking"); placeSpiders(); }
  })(t0);
}

// Replay of the real walk: events carry their real time (ms since the cycle started). Each spider's
// events keep their order and real spacing; animations only stretch gaps too short to see.
const MOVE_MS = 650, CHECK_MIN_MS = 450, logEl = document.getElementById("log"), logList = logEl.querySelector("ol");
const logState = logEl.querySelector(".state");
let timers = [];
function log(t, html) {
  const li = document.createElement("li");
  li.className = "new";
  li.innerHTML = `<time>+${(t / 1000).toFixed(2)}s</time><span>${html}</span>`;
  logList.appendChild(li); logList.scrollTop = logList.scrollHeight;
}
function setStats(id, text, cls) {
  const st = els[id]?.querySelector(".stats"); if (!st) return;
  st.textContent = text; st.className = "stats" + (cls ? " " + cls : "");
}
function setDot(id, status, pending) {
  const dot = els[id]?.querySelector(".dot"); if (!dot) return;
  dot.classList.toggle("pending", !!pending);
  if (!pending) dot.style.background = statusColor(status);
}
function applyEvent(e) {
  const sid = e.spider.replace(/^spider-/, "");
  const who = `🕷 <b>${esc(sid.includes("/") ? sid.split("/").pop() : sid.split(".")[0])}</b>`, node = esc(e.node || "");
  if (e.kind === "move") {
    moveSpider(e.spider, e.node, MOVE_MS);
    log(e.t, `${who} → ${node}`);
  } else if ((e.kind === "check" || e.kind === "result") && e.shared_from) {
    if (e.kind === "check") log(e.t, `↪ ${who} reuses another spider's ping of ${node}`);
  } else if (e.kind === "check") {
    const el = els[e.node]; if (!el) return;
    el.classList.remove("flash-ok", "flash-fail");
    el.classList.add("checking"); el.classList.toggle("peek", !!e.peek);
    setDot(e.node, null, true); setStats(e.node, e.peek ? "checking from downstream…" : "pinging…", "live");
    log(e.t, `📡 ${e.peek ? "checking" : "pinging"} ${node}`);
  } else if (e.kind === "result") {
    const el = els[e.node]; if (!el) return;
    el.classList.remove("checking", "peek"); void el.offsetWidth;
    el.classList.add(e.up ? "flash-ok" : "flash-fail");
    live[e.node] = e.up ? "UP" : "DOWN"; setDot(e.node, live[e.node]); drawEdges();
    const ms = e.latency != null ? `${Math.round(e.latency)} ms` : "";
    setStats(e.node, e.up ? `✓ UP · ${ms}` : `✗ ${e.error || "DOWN"}`, e.up ? "good" : "bad");
    setTimeout(() => setStats(e.node, el.dataset.stats), 2200);
    log(e.t, e.up ? `<span style="color:var(--up)">✓</span> ${node} UP ${ms}`
                  : `<span style="color:var(--down)">✗</span> ${node} DOWN <span class="sub">${esc((e.error || "").slice(0, 90))}</span>`);
  } else if (e.kind === "finish") {
    paintSpider(e.spider, e.status);
    if (e.status === "STOPPED" && els[e.node]) els[e.node].classList.add("root");
    log(e.t, `${who} <b style="color:var(--${e.status})">${e.status}</b> @ ${node}` +
             (e.root ? `<div class="sub">root cause: ${esc(e.root)}</div>` : e.impact ? `<div class="sub">${esc(e.impact)}</div>` : ""));
  }
}
function replay() {
  timers.forEach(clearTimeout); timers = [];
  logList.innerHTML = ""; logEl.style.display = "flex"; logEl.classList.remove("collapsed"); logState.textContent = "running…";
  const checked = new Set(G.replay.filter(e => e.kind === "check").map(e => e.node));
  for (const id of checked) { live[id] = null; setDot(id, null, true); setStats(id, "waiting for spider…"); els[id].classList.remove("root", "checking", "peek"); }
  drawEdges();
  // Spiders start where they were before the walk (first move's source), walking badges.
  for (const s of G.spiders) {
    const first = G.replay.find(e => e.spider === s.id);
    if (first) { spiderAt[s.id] = first.kind === "move" ? (first.source || first.node) : first.node; paintSpider(s.id, "RUNNING"); }
  }
  placeSpiders();
  const busyUntil = {};
  let lastAt = 0;
  for (const e of G.replay) {
    const at = Math.max(e.t, busyUntil[e.spider] || 0);
    busyUntil[e.spider] = at + (e.kind === "move" ? MOVE_MS : e.kind === "check" ? CHECK_MIN_MS : 120);
    lastAt = Math.max(lastAt, busyUntil[e.spider]);
    timers.push(setTimeout(() => applyEvent(e), at + 300));
  }
  timers.push(setTimeout(() => { logState.textContent = `done · real walk ${(Math.max(...G.replay.map(e => e.t)) / 1000).toFixed(1)}s ▾`; }, lastAt + 400));
  timers.push(setTimeout(() => logEl.classList.add("collapsed"), lastAt + 2400));  // get out of the way of the cards
}
logEl.querySelector("header").onclick = () => logEl.classList.toggle("collapsed");
if (REPLAY) {
  const btn = document.getElementById("replay");
  btn.style.display = ""; btn.onclick = replay;
}

// Database panel (top-right): node labels and relationship types with counts, like Neo4j Browser.
const lists = document.getElementById("lists");
const LABELS = ["MainInput", "BackupLink", "Transcoding", "FinalLink"];
const labelCounts = LABELS.map(l => [l, G.nodes.filter(n => n.labels.includes(l)).length]).filter(([, c]) => c);
if (G.spiders.length) labelCounts.push(["SpiderRun", G.spiders.length]);
const typeCounts = {};
for (const e of G.edges) typeCounts[e.type] = (typeCounts[e.type] || 0) + 1;
if (G.spiders.some(s => s.at)) typeCounts.AT = G.spiders.filter(s => s.at).length;
document.getElementById("label-chips").innerHTML = labelCounts.map(([l, c]) =>
  `<button class="chip-btn label-chip" data-kind="label" data-value="${l}" style="background:var(--${l}, #64748b)">${l}<span class="count">${c}</span></button>`).join("");
document.getElementById("type-chips").innerHTML = Object.entries(typeCounts).map(([t, c]) =>
  `<button class="chip-btn type-chip" data-kind="type" data-value="${t}">${t}<span class="count">${c}</span></button>`).join("");

function applyFocus() {
  lists.querySelectorAll(".chip-btn").forEach(b => b.classList.toggle("on", !!focus && b.dataset.kind === focus.kind && b.dataset.value === focus.value));
  const spiderFocus = focus && (focus.value === "SpiderRun" || focus.value === "AT");
  for (const n of G.nodes) {
    let match = !focus;
    if (focus?.kind === "label") match = focus.value === "SpiderRun" ? Object.values(spiderAt).includes(n.id) : n.labels.includes(focus.value);
    if (focus?.kind === "type") match = focus.value === "AT" ? Object.values(spiderAt).includes(n.id)
      : G.edges.some(e => e.type === focus.value && (e.source === n.id || e.target === n.id));
    els[n.id]?.classList.toggle("dim", !match);
  }
  for (const el of Object.values(spiderEls)) el.classList.toggle("dim", !!focus && !spiderFocus);
  drawEdges();
}
lists.addEventListener("click", e => {
  const b = e.target.closest(".chip-btn"); if (!b) return;
  const same = focus && focus.kind === b.dataset.kind && focus.value === b.dataset.value;
  focus = same ? null : {kind: b.dataset.kind, value: b.dataset.value};
  applyFocus();
});
lists.querySelector(".toggle").onclick = () => {
  lists.classList.toggle("collapsed");
  lists.querySelector(".toggle").textContent = lists.classList.contains("collapsed") ? "▸" : "▾";
  fit();
};
canvas.addEventListener("dblclick", e => { if (!e.target.closest(".node, #lists, #panel, #heatmap-panel")) { focus = null; applyFocus(); } });

// Heatmap panel in graph_view
const heatmapPanel = document.getElementById("heatmap-panel");
if (heatmapPanel) {
  const downNodes = G.nodes.filter(n => (n.failedCount || 0) > 0 || (n.consecutiveFailures || 0) > 0 || n.status === "DOWN");
  downNodes.sort((a, b) => (b.failedCount || 0) - (a.failedCount || 0) || (b.consecutiveFailures || 0) - (a.consecutiveFailures || 0));
  const badge = document.getElementById("heatmap-down-badge");
  if (badge) {
    if (downNodes.length > 0) {
      badge.className = "badge-down";
      badge.textContent = `${downNodes.length} node${downNodes.length > 1 ? "s" : ""} down/failing`;
    } else {
      badge.className = "badge-down ok";
      badge.textContent = "All nodes operational";
    }
  }
  const list = document.getElementById("heatmap-nodes-list");
  if (list) {
    if (downNodes.length === 0) {
      list.innerHTML = `<span style="color:var(--muted);font-size:10.5px">No nodes with failure history.</span>`;
    } else {
      list.innerHTML = downNodes.slice(0, 8).map(n => {
        const parts = n.id.split("/");
        const shortName = parts[0].replace(".ottlive.co.in", "").replace(".co.in", "") + (parts[1] ? "/" + parts[1] : "");
        return `<button class="node-pill" data-id="${esc(n.id)}" title="Click to inspect ${esc(n.id)}"><span>${esc(shortName)}</span><span class="fail-count">${n.failedCount || 0} down</span></button>`;
      }).join("");
      list.querySelectorAll(".node-pill").forEach(btn => {
        btn.onclick = (e) => {
          e.stopPropagation();
          const id = btn.dataset.id;
          const p = pos[id];
          if (p && canvas.clientWidth) {
            view.x = (canvas.clientWidth / 2) - (p.x + W / 2) * view.k;
            view.y = (canvas.clientHeight / 2) - (p.y + H / 2) * view.k;
            apply();
          }
          showPanel(id);
        };
      });
    }
  }
  const toggleBtn = document.getElementById("heatmap-toggle");
  const toolbarBtn = document.getElementById("btn-toggle-heatmap");
  const togglePanel = (e) => {
    if (e) e.stopPropagation();
    heatmapPanel.classList.toggle("collapsed");
    const isCollapsed = heatmapPanel.classList.contains("collapsed");
    if (toggleBtn) toggleBtn.textContent = isCollapsed ? "▴" : "▾";
  };
  heatmapPanel.querySelector("header").onclick = togglePanel;
  if (toolbarBtn) toolbarBtn.onclick = togglePanel;
}

fit();
window.addEventListener("resize", fit);
if (REPLAY) replay();
</script>
</body></html>
"""
