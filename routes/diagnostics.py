"""Diagnostics routes (split out of server.py). Shared state and helpers are read from the server
module at call time as `srv.<name>`, so there is one copy of each and tests can patch them there."""

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
import json

import server as srv

router = APIRouter()


@router.get("/api/nodes/{node_id:path}/traceroute")
def get_traceroute(node_id: str):
    """The node's latest traceroute (hop by hop) and whether one is running now."""
    srv.node_for_traceroute(node_id)
    return {"result": srv.stored_traceroute(srv.driver, node_id), **srv.tracer.status(node_id),
            "threshold": srv.TRACEROUTE_THRESHOLD, "cooldown_s": srv.TRACEROUTE_COOLDOWN_S}


@router.post("/api/nodes/{node_id:path}/rca")
def node_rca(node_id: str):
    """NOC root-cause analysis of one server from its health checks and latest traceroute. Streams NDJSON:
    {"type": "findings", ...computed zone / drop point / action...}, then the model's text as tokens, then done."""
    analysis = srv.analyse_node(srv.driver, node_id)
    if analysis is None:
        raise HTTPException(404, f"No node {node_id!r}")
    return StreamingResponse((json.dumps(event, default=str) + "\n" for event in srv.stream_rca(srv.chatbot, analysis)),
                             media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/api/nodes/{node_id:path}/traceroute", status_code=202)
def run_traceroute_now(node_id: str):
    """Starts a traceroute now, in the background; the result arrives as a `traceroute` stream event."""
    try:
        return srv.tracer.start(srv.node_for_traceroute(node_id), trigger="manual")
    except srv.Busy as e:
        raise HTTPException(409, str(e))
    except srv.Cooldown as e:
        raise HTTPException(429, str(e), headers={"Retry-After": str(e.retry_in)})
