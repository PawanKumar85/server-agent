"""Monitor routes (split out of server.py). Shared state and helpers are read from the server
module at call time as `srv.<name>`, so there is one copy of each and tests can patch them there."""

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.responses import Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pydantic import Field
from pydantic import ValidationError
from typing import Any, Dict, List, Optional
import asyncio
import json

import server as srv

router = APIRouter()


@router.get("/api/graph")
def graph():
    data = srv.fetch_graph(srv.driver)
    data["nodes"] = [srv.slim(n) for n in data["nodes"]]
    data["spiders"] = [srv.slim(s) for s in data.get("spiders", [])]
    return srv.jsonable({
        **data,
        "positions": srv.layout(data["nodes"], data["edges"]),
        "card": {"w": srv.CARD_W, "h": srv.CARD_H},
        "roleOrder": srv.ROLE_ORDER,
    })


@router.get("/api/heatmap.png")
def heatmap_png(theme: str = "dark", dpi: int = 140):
    data = srv.fetch_graph(srv.driver)
    safe_dpi = min(300, max(72, dpi))
    png_bytes = srv.generate_3d_heatmap_png(data["nodes"], theme=theme, dpi=safe_dpi)
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@router.get("/api/spiders")
def spiders():
    out = []
    for s in srv.all_spiders(srv.driver):
        spider = srv.slim(dict(s["spider"]))
        rca = srv.Rca(**json.loads(spider.pop("rca"))) if spider.get("rca") else srv.Rca()
        out.append({"spider": spider, "node": srv.slim(s["node"]), "labels": s["labels"], "atCount": s["atCount"],
                    "rca": rca.model_dump()})
    return srv.jsonable({
        "spiders": out,
        "counts": {status: srv.count_spiders(srv.driver, status) for status in ("RUNNING", "STOPPED", "RECOVERED", "ERROR")},
    })


class LinkRow(BaseModel):
    channel: str = ""
    label: str = ""
    url: str = ""


class LinksRequest(BaseModel):
    links: List[LinkRow]
    dry_run: bool = False


@router.post("/api/links")
def save_links(body: LinksRequest):
    """Validate every row; with no errors, group by domain and (unless dry_run) upsert."""
    links, errors = [], []
    for i, row in enumerate(body.links):
        values = {k: v.strip() for k, v in row.model_dump().items()}
        if not any(values.values()):
            continue
        try:
            links.append(srv.StreamLink(**values))
        except ValidationError as e:
            errors += [{"row": i, "field": str(err["loc"][0]), "message": err["msg"]} for err in e.errors()]
    if errors:
        return JSONResponse({"errors": errors}, status_code=422)
    nodes = srv.group_by_domain(links)
    connected = []
    if not body.dry_run:
        srv.upsert_nodes(srv.driver, nodes)
        # A new channel gets its pipeline (Main/Backup → Transcoding → Final) right away.
        connected = srv.connect_new_channels(srv.driver, {l.channel for l in links})
    return {
        "saved": not body.dry_run,
        "connected": [e.model_dump() for e in connected],
        "nodes": [
            {"domain": n.domain, "labels": sorted(n.labels), "url": sorted(str(u) for u in n.url),
             "channels": sorted({l["channel"] for l in n.links}), "server_ip": n.server_ip}
            for n in nodes
        ],
    }


@router.get("/api/topology")
def get_topology():
    domains = [r["d"] for r in srv.driver.execute_query("MATCH (n:Domain) RETURN n.domain AS d ORDER BY d").records]
    return {"edges": [r.model_dump() for r in srv.current_topology(srv.driver)], "domains": domains}


class TopologyRequest(BaseModel):
    edges: List[srv.Relationship]


@router.put("/api/topology")
def put_topology(body: TopologyRequest):
    rejected = srv.replace_topology(srv.driver, body.edges)
    return {
        "saved": len(body.edges) - len(rejected),
        "rejected": [{**rel.model_dump(), "reason": reason} for rel, reason in rejected],
    }


class RunRequest(BaseModel):
    final: Optional[str] = None  # one channel's FinalLink id, or all spiders when omitted


@router.post("/api/run", status_code=202)
def run_spiders(body: RunRequest = RunRequest()):
    """Start a spider cycle now: all channels, or a single channel's spider ("test ping")."""
    finals = None
    if body.final:
        found = srv.driver.execute_query("MATCH (f:Domain:FinalLink {domain: $f}) RETURN count(f) AS c", f=body.final).records[0]["c"]
        if not found:
            raise HTTPException(404, f"No FinalLink {body.final!r}")
        finals = [body.final]
    run_id = srv.start_run(finals, source="channel" if finals else "manual")
    if run_id is None:
        raise HTTPException(409, "A spider cycle is already running")
    return {"run": run_id, "finals": finals}


@router.get("/api/stream")
async def stream_events():
    """Every run's events (scheduled, manual or channel test) for every open page, as they happen."""
    queue = srv.hub.subscribe()

    async def events():
        try:
            yield f"event: scheduler\ndata: {json.dumps(srv.scheduler.state())}\n\n"
            if srv.current_run:  # joined mid-run: tell the page a run is in progress
                yield f"event: start\ndata: {json.dumps({**srv.current_run, 'joined_late': True})}\n\n"
            while True:
                try:
                    kind, payload = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"event: {kind}\ndata: {payload}\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            srv.hub.unsubscribe(queue)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class SchedulerRequest(BaseModel):
    enabled: Optional[bool] = None
    interval: Optional[int] = Field(None, ge=srv.MIN_INTERVAL_S, le=srv.MAX_INTERVAL_S)


@router.get("/api/scheduler")
def get_scheduler():
    return srv.scheduler.state()


@router.post("/api/scheduler")
def set_scheduler(body: SchedulerRequest):
    """Start/stop the automatic ping and/or change its interval (seconds)."""
    srv.scheduler.update(body.enabled, body.interval)
    return srv.scheduler.state()


@router.get("/api/adaptive")
def get_adaptive():
    """Live adaptive crawl tiers for every spider/channel.

    Returns a list of entries, one per FinalLink, showing its current tier
    (TURBO / URGENT / WATCH / NORMAL / RELAXED), the effective crawl interval,
    the human-readable reason, and when it is next scheduled to run.
    """
    return {
        "base_interval_s": srv.scheduler.interval,
        "tiers": {
            "TURBO":   {"interval_s": srv.ADAPTIVE_TIER_TURBO_S,   "description": "Active outage – rapid recovery"},
            "URGENT":  {"interval_s": srv.ADAPTIVE_TIER_URGENT_S,  "description": "On backup link – SPOF"},
            "WATCH":   {"interval_s": srv.ADAPTIVE_TIER_WATCH_S,   "description": "Flapping / recent incident"},
            "NORMAL":  {"interval_s": srv.scheduler.interval,      "description": "Standard polling"},
            "RELAXED": {"interval_s": srv.ADAPTIVE_TIER_RELAXED_S, "description": "Rock-solid 7+ days stable"},
        },
        "spiders": sorted(srv._adaptive.values(), key=lambda x: x.get("interval_s", 30)),
    }


@router.get("/api/predictions")
def get_predictions():
    """Latest predictive failure risk scores for every node.

    Scores are recomputed in a background thread after each completed spider cycle.
    band: NOMINAL | LOW | MEDIUM | HIGH | CRITICAL
    """
    sorted_preds = sorted(srv._last_predictions,
                          key=lambda p: p.get("blendedScore", 0), reverse=True)
    high_risk = [p for p in sorted_preds if p["band"] in ("HIGH", "CRITICAL")]
    return {
        "count":    len(sorted_preds),
        "highRisk": len(high_risk),
        "nodes":    sorted_preds,
    }


@router.get("/api/anomalies")
def anomalies():
    """Per server: anomaly score (0-10), each metric vs its normal, trend, and readable early warnings."""
    return {node: {**a, "warnings": srv.anomaly_warnings(a)} for node, a in srv.metrics.all_anomalies().items()
            if not node.endswith(".invalid")}


@router.get("/api/nodes/{node_id:path}/metrics")
def node_metrics(node_id: str, since: int = Query(21600, ge=60, le=14 * 86400)):
    """The node's per-check history (latency, RTT, jitter, loss, segment age, up) and its anomalies."""
    return {"history": srv.metrics.history(node_id, since), "anomalies": srv.metrics.anomalies(node_id)}


@router.get("/api/nodes/{node_id:path}/timeline")
def node_timeline(node_id: str, since: int = Query(7 * 86400, ge=3600, le=14 * 86400),
                  buckets: int = Query(336, ge=24, le=1000)):
    """The node's history for the charts: `since` seconds in `buckets` time buckets, plus its incidents and
    learned segment-age lines."""
    tl = srv.metrics.timeline(node_id, since, buckets)
    tl["glitches"] = srv.glitch_probe.events(node_id, since_s=since, limit=1000)  # Final streams only
    tl["adBreaks"] = srv.scte_store.breaks(node=node_id, since_s=since)
    return tl


def history_learned(node_id: str) -> dict:
    """What was learned about this server, for the AI history summary: patterns, recorded fixes, root causes, now."""
    from collections import Counter
    out = {"patterns": [], "fixes": [], "root_causes": None, "status": None}
    try:
        out["patterns"] = [p["text"] for p in srv.learner.patterns() if node_id in (p.get("nodes") or [])]
        cases = srv.learner.cases(node=node_id, limit=100)
        out["fixes"] = [c["resolution"] for c in cases if c.get("resolution")]
        roots = Counter(c["root_cause"] for c in cases if c.get("root_cause") and c["root_cause"] != node_id)
        if roots:
            out["root_causes"] = ", ".join(f"{r} ({n}x)" for r, n in roots.most_common(3))
    except Exception:
        pass
    try:
        rec = srv.driver.execute_query("MATCH (n:Domain {domain: $d}) RETURN n.status AS s, n.lastError AS e",
                                       d=node_id).records
        if rec:
            out["status"] = f"{rec[0]['s'] or 'unknown'}" + (f" (last error: {rec[0]['e']})" if rec[0]["s"] != "UP" and rec[0]["e"] else "")
    except Exception:
        pass
    return out


@router.post("/api/nodes/{node_id:path}/history-ai")
def node_history_ai(node_id: str, since: int = Query(7 * 86400, ge=3600, le=14 * 86400)):
    """The AI's plain-words reading of the server's history (NDJSON): facts, then the explanation token by token,
    then done (or error). Facts come from the same timeline as the charts; a repeat within 10 min is cached."""
    tl = node_timeline(node_id, since, 336)
    events = srv.history_ai.stream(node_id, since, tl, history_learned(node_id))
    return StreamingResponse((json.dumps(e) + "\n" for e in events), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/nodes/{node_id:path}/timeline.csv")
def node_timeline_csv(node_id: str, since: int = Query(7 * 86400, ge=3600, le=14 * 86400)):
    """CSV export of the node's timeline metrics for the selected time range."""
    import csv
    import io
    import re
    from datetime import datetime, timezone, timedelta
    if srv.metrics is None:
        raise HTTPException(503, "Metrics database not enabled")
    data = srv.metrics.timeline(node_id, since)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Timestamp (IST)", "Unix Epoch (s)", "Availability (%)", "Checks Passed",
        "Checks Failed", "Total Checks", "HTTP Latency (ms)", "ICMP RTT (ms)",
        "Jitter (ms)", "Packet Loss (%)", "Segment Age Avg (s)", "Segment Age Max (s)"
    ])
    ist = timezone(timedelta(hours=5, minutes=30))
    for p in data.get("points", []):
        dt_ist = datetime.fromtimestamp(p["t"], ist).strftime("%Y-%m-%d %H:%M:%S")
        checks = p.get("checks", 0)
        fails = p.get("fails", 0)
        passed = checks - fails
        writer.writerow([
            dt_ist, p["t"], p.get("up", ""), passed, fails, checks,
            p.get("latency", ""), p.get("rtt", ""), p.get("jitter", ""),
            p.get("loss", ""), p.get("age", ""), p.get("ageMax", "")
        ])

    clean_name = re.sub(r"[^\w.-]+", "_", node_id)
    span_str = f"{int(since // 3600)}h" if since < 86400 * 2 else f"{int(round(since / 86400))}d"
    filename = f"stream-graph-metrics-{clean_name}-{span_str}-{datetime.now():%Y%m%d-%H%M}.csv"

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store"
        }
    )


def _model_summary():
    m = srv.glitch_model.load(srv.metrics.path)
    if not m:
        return None
    weights = sorted(zip(m.get("weights") or [], m.get("features") or []), key=lambda x: -abs(x[0]))[:5]
    return {k: m.get(k) for k in ("trained_at", "active", "note", "samples", "positives", "auc", "best_single",
                                  "best_single_auc", "precision", "recall", "history_days", "base_rate")} | {
        "topFactors": [{"factor": srv.glitch_model.WORDS.get(f, f), "weight": w} for w, f in weights],
        "best_single": srv.glitch_model.WORDS.get(m.get("best_single"), m.get("best_single"))}


@router.post("/api/glitches/train")
def train_glitches():
    """Retrain the glitch model now (it also retrains hourly)."""
    srv.train_glitch_model()
    return _model_summary()


@router.get("/api/glitches")
def glitches(hours: int = Query(24, ge=1, le=14 * 24)):
    """Glitches on the Final streams: each Final's last hour against its learned normal and its risk of glitches in
    the next 10 minutes (with reasons and the upstream failures that usually come first), the recent events, and
    whether there's enough history yet to train a model."""
    return {"finals": srv.glitch_forecast(), "forecastEnabled": srv.GLITCH_FORECAST,
            "events": srv.glitch_probe.events(since_s=hours * 3600, limit=300),
            "model": srv.shared("glitch:readiness", 300, lambda: srv.glitch.model_readiness(srv.metrics.path)),
            "trainedModel": _model_summary(),
            "probeEveryS": srv.glitch.PROBE_S,
            "lastProbeAt": srv.glitch_monitor.last_run, "monitorNetworkSlow": srv.glitch_monitor.network_slow}


@router.get("/api/alerts")
def alerts(hours: int = Query(24, ge=1, le=14 * 24)):
    """The one alert log (newest first), what's likely to alert next (with why and when), the learned "A is
    followed by B" rules, and how past predictions scored."""
    # Reading only: the scheduler records predictions after each run; a page poll must not record a new batch.
    return {"alerts": srv.alert_log.recent(hours * 3600),
            "upcoming": srv.shared("alerts:upcoming", 15, lambda: srv.alert_log.predict(srv.alert_topology, record=False)),
            "rules": srv.alert_log.learn()[:50], "scores": srv.shared("alerts:scores", 60, srv.alert_log.scores)}


@router.get("/api/scte")
def scte_breaks():
    """SCTE-35 ad breaks per channel (last 7 days): the learned pattern, when the next one is expected, problems
    (stuck, overrun, missing, dropped between Main and Final) and the recent breaks."""
    return {"channels": srv.ad_breaks(), "retentionDays": srv.scte.RETENTION_DAYS}


@router.get("/api/backups")
def backups():
    """The backup files kept (newest first)."""
    return {"dir": str(srv.backup.BACKUP_DIR), "keep": srv.backup.BACKUP_KEEP, "files": srv.backup.list_backups()}


@router.post("/api/backup")
def backup_now():
    """Back up the history/learning database and the graph now."""
    if srv.scheduler.backup_running:
        raise HTTPException(409, "A backup is already running")
    srv.scheduler.backup_running = True
    return srv.run_backup()


@router.get("/api/system")
def system_usage():
    """RAM and CPU use of this app's container (header meter)."""
    import sysinfo
    return sysinfo.usage()


@router.get("/api/uptime")
def uptime(slots: int = Query(32, ge=4, le=240), slot: int = Query(60, ge=10, le=3600)):
    """The Uptime Matrix from the saved check history: per server and per channel (its Final URLs), `slots`
    slots of `slot` seconds, oldest first."""
    finals: Dict[str, List[str]] = {}
    for r in srv.driver.execute_query("MATCH (n:Domain:FinalLink) WHERE NOT n.domain ENDS WITH '.invalid' "
                                  "RETURN n.links AS links").records:
        for link in srv.load_json_list(r["links"]):
            if link.get("role") == "FinalLink":
                finals.setdefault(link["channel"], []).append(link["url"])
    data = srv.metrics.uptime(slots, slot, finals)
    data["nodes"] = {k: v for k, v in data["nodes"].items() if not k.endswith(".invalid")}
    return data


@router.get("/api/rca/ranking")
def rca_ranking():
    """The likely root cause of each current failure group, ranked (computed now from the live state)."""
    return srv.current_ranking()


@router.get("/api/cdn/recommendations")
def cdn_recommendations():
    """Facility location algorithm: suggests optimal future CDN Edge PoP deployments based on server distances and latency."""
    from geo_cdn_tool import analyze_best_cdn_locations
    from telemetry_pool import telemetry_pool
    nodes = telemetry_pool.get_nodes(srv.driver)
    nodes_list = list(nodes.values()) if nodes else []
    return analyze_best_cdn_locations(nodes_list)


@router.get("/api/cdn/geo-matrix")
def cdn_geo_matrix():
    """Returns server geolocations, Haversine physical distances, and speed-of-light optical fiber stretch."""
    from geo_cdn_tool import analyze_best_cdn_locations
    from telemetry_pool import telemetry_pool
    nodes = telemetry_pool.get_nodes(srv.driver)
    nodes_list = list(nodes.values()) if nodes else []
    analysis = analyze_best_cdn_locations(nodes_list)
    return {
        "server_distance_matrix": analysis.get("server_distance_matrix", []),
        "top_recommendation": analysis.get("top_recommendation", {}),
        "active_nodes_evaluated": analysis.get("active_nodes_evaluated", 0)
    }


@router.get("/api/pool/library")
def get_datapool_library(force: bool = False):
    """Returns the complete in-memory Telemetry Data Pool catalog for the Library view."""
    from telemetry_pool import telemetry_pool
    if force:
        telemetry_pool.invalidate()
    nodes = telemetry_pool.get_nodes(srv.driver)
    channels = telemetry_pool.get_channels(srv.driver)
    stats = telemetry_pool.stats()

    freshness_summary = {"FRESH": 0, "WARM": 0, "STALE": 0, "UNKNOWN": 0}
    total_links = 0
    for n in nodes.values():
        f = n.get("streamFreshness") or "UNKNOWN"
        freshness_summary[f] = freshness_summary.get(f, 0) + 1
        total_links += len(n.get("links", []))

    return {
        "stats": stats,
        "summary": {
            "node_count": len(nodes),
            "channel_count": len(channels),
            "link_count": total_links,
            "freshness": freshness_summary,
            "ttl_s": telemetry_pool.ttl,
            "is_fresh": telemetry_pool.is_fresh(),
        },
        "nodes": list(nodes.values()),
        "channels": [
            {"channel": ch, "roles": roles, "total_streams": sum(len(v) for v in roles.values())}
            for ch, roles in sorted(channels.items())
        ],
    }


@router.post("/api/pool/refresh")
def refresh_datapool():
    """Forces cache invalidation and immediate re-indexing of the Telemetry Data Pool."""
    from telemetry_pool import telemetry_pool
    telemetry_pool.invalidate()
    nodes = telemetry_pool.get_nodes(srv.driver, force_refresh=True)
    channels = telemetry_pool.get_channels(srv.driver, force_refresh=True)
    return {
        "status": "ok",
        "message": "Data Pool re-indexed successfully",
        "stats": telemetry_pool.stats(),
        "node_count": len(nodes),
        "channel_count": len(channels),
    }


@router.get("/api/agent/mcp_tools")
def get_mcp_tools():
    """List all Model Context Protocol (MCP) tools and schemas."""
    from mcp_server import mcp_server
    return {
        "status": "ok",
        "protocol": "Model Context Protocol (MCP) v1.0",
        "tool_count": len(mcp_server.get_tools_metadata()),
        "tools": mcp_server.get_tools_metadata(),
        "mcp_schema": mcp_server.list_tools(),
    }


@router.post("/api/agent/mcp_tools/{tool_name}/execute")
def execute_mcp_tool(tool_name: str, payload: Dict[str, Any] = Body(default={})):
    """Execute a Model Context Protocol (MCP) tool."""
    from mcp_server import mcp_server
    return mcp_server.call_tool(tool_name, payload)



