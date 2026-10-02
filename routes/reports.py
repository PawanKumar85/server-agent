"""Reports routes (split out of server.py). Shared state and helpers are read from the server
module at call time as `srv.<name>`, so there is one copy of each and tests can patch them there."""

from datetime import datetime
from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import Response
from fastapi.responses import StreamingResponse
from typing import Optional
from urllib.parse import urlencode
import json
import re

import server as srv

router = APIRouter()


@router.get("/api/report.html")
def report(node: Optional[str] = None, since: int = 7 * 86400, download: bool = False):
    """Complete HTML report (Seaborn/Matplotlib charts, every stored field): all nodes, or `?node=<domain>`."""
    query_params = {}
    if node:
        query_params["node"] = node
    if since and since != 7 * 86400:
        query_params["since"] = since
    query = urlencode(query_params)
    actions = ("<div class='actions noprint'><button onclick='print()'>🖨 Print / PDF</button>"
               f"<a href='/api/report.html?{query}{'&' if query else ''}download=1'>⬇ Download</a></div>")
    try:
        page = srv.build_report(srv.driver, node, srv.scheduler.state(), actions="" if download else actions, live=not download,
                                metrics=srv.metrics, since=since)
    except TypeError:
        page = srv.build_report(srv.driver, node, srv.scheduler.state(), actions="" if download else actions, live=not download,
                                metrics=srv.metrics)
    if page is None:
        raise HTTPException(404, f"No node {node!r}")
    headers = {"Cache-Control": "no-store"}
    if download:
        name = re.sub(r"[^\w.-]+", "_", node or "all-nodes")
        span_str = f"{int(since // 3600)}h" if since < 86400 * 2 else f"{int(round(since / 86400))}d"
        headers["Content-Disposition"] = f'attachment; filename="stream-graph-report-{name}-{span_str}-{datetime.now():%Y%m%d-%H%M}.html"'
    return HTMLResponse(page, headers=headers)


@router.post("/api/report/analysis")
def report_analysis(node: Optional[str] = None):
    """AI deep analysis of the report (all nodes, or `?node=`). Streams NDJSON: findings, tokens, done."""
    prepared = srv.analysis_prompt(srv.driver, node, srv.scheduler.state(), metrics=srv.metrics)
    if prepared is None:
        raise HTTPException(404, f"No node {node!r}")
    return StreamingResponse((json.dumps(event, default=str) + "\n" for event in srv.stream_analysis(srv.chatbot, prepared)),
                             media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/export.xlsx")
def export():
    return Response(
        srv.build_workbook(srv.driver, metrics=srv.metrics),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="stream-graph-{datetime.now():%Y%m%d-%H%M}.xlsx"'},
    )


@router.post("/api/import/excel")
async def import_excel(request: Request, dry_run: bool = False, auto_connect: bool = True):
    """Import channels, links, and relationships from an uploaded Excel workbook."""
    body = await request.body()
    if not body:
        raise HTTPException(400, "Empty file uploaded")
    try:
        parsed = srv.parse_excel_workbook(body)
    except Exception as e:
        raise HTTPException(422, f"Failed to read Excel workbook: {e}")

    links = parsed["links"]
    if not links:
        raise HTTPException(422, "No valid stream links found in the Excel workbook.")

    nodes = srv.group_by_domain(links)
    rels_to_save = parsed["relationships"]
    if not rels_to_save and auto_connect:
        rels_to_save = parsed["auto_relationships"]

    if not dry_run:
        srv.upsert_nodes(srv.driver, nodes)
        if rels_to_save:
            existing = srv.current_topology(srv.driver)
            all_rels = list(existing)
            seen_edges = {(r.source, r.type, r.target) for r in all_rels}
            for r in rels_to_save:
                if (r.source, r.type, r.target) not in seen_edges:
                    all_rels.append(r)
                    seen_edges.add((r.source, r.type, r.target))
            srv.replace_topology(srv.driver, all_rels)
        # Channels the workbook's Relationships sheet doesn't cover (e.g. rows added to an exported workbook)
        # still get their pipeline.
        if auto_connect:
            connected = srv.connect_new_channels(srv.driver, set(parsed["channels"]))
            rels_to_save = list(rels_to_save) + [e for e in connected if e not in set(rels_to_save)]

    return {
        "saved": not dry_run,
        "sheets_found": parsed["sheets_found"],
        "channels": parsed["channels"],
        "link_count": len(links),
        "links": [
            {"channel": l.channel, "label": l.label, "url": str(l.url)}
            for l in links
        ],
        "node_count": len(nodes),
        "nodes": [
            {
                "domain": n.domain,
                "labels": sorted(n.labels),
                "channels": sorted(list(set(l["channel"] for l in n.links))),
            }
            for n in nodes
        ],
        "relationships_saved": len(rels_to_save) if not dry_run else 0,
        "relationships": [r.model_dump() for r in rels_to_save],
        "errors": parsed["errors"],
    }


@router.post("/api/embeddings/sync")
def sync_embeddings(clear_logs: bool = True, all_logs: bool = False):
    """Embeddings & Log Clearance, now: 384-dim embeddings for every Server, Spider, and Activity node,
    then (clear_logs) incident log entries older than INCIDENT_RETENTION_DAYS (90) are archived into
    `incidentStats` on nodes. Recent entries stay: the Learning page, the chatbot's incident history and the
    reports need them. all_logs=true archives everything up to now (only when explicitly asked)."""
    try:
        return srv.jsonable({"success": True, **srv.run_embedding_sync("manual", clear_logs, all_logs=all_logs)})
    except srv.SyncBusy as e:
        raise HTTPException(409, str(e))


@router.get("/api/activities")
def list_activities_endpoint(limit: int = 50, type: Optional[str] = None):
    """List recent system activities, Scrapy crawls, and audit actions stored in Neo4j."""
    from activity import list_activities
    return srv.jsonable({"activities": list_activities(srv.driver, limit=limit, act_type=type)})
