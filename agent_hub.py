"""The other end of the Node Agent (../Node_Agent): server telemetry from inside each streaming server.

Agents connect with socket.io to this app, on the dashboard's own port, at /socket.io:
    AGENT_ID=ingest1  RCA_URL=https://<this app>  AGENT_TOKEN=<one of AGENT_TOKENS>  node agent.js

  connect          the agent's token is checked (AGENT_TOKENS in .env, comma separated; no tokens = no agents);
                   its AGENT_ID is matched to a server in the graph ("ingest1" -> ingest1.ottlive.co.in)
  configure_hls -> sent on connect: the server's own stream URL, so the agent also watches it from the inside
  telemetry        every few seconds: CPU, RAM, disks, network, encoder processes, HLS freshness; kept in SQLite
                   (agent_telemetry, next to the health checks) for TELEMETRY_DAYS
  incident         raised by the agent (CPU/RAM/disk full, an encoder process stopped, stream stale): kept, and
                   written to the alert log as AGENT_<KIND>, so it shows up with every other alert
  health_check  -> asked by GET /api/agents/{agent}/snapshot: the agent answers with a fresh health_response

GET /api/agents lists them (online or not, the latest numbers); GET /api/agents/{agent}/telemetry their history.
Installed by server.py: `agent_hub.install(app)` (outermost, so the dashboard login doesn't apply to agents; they
have their own token).
"""

import asyncio
import hmac
import json
import os
import sqlite3
import threading
import time
from typing import Dict, List, Optional

import socketio
from fastapi import APIRouter, HTTPException, Query

TELEMETRY_DAYS = 7
SNAPSHOT_TIMEOUT_S = 10
SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_telemetry (
    ts REAL NOT NULL, agent TEXT NOT NULL, node TEXT, cpu REAL, memory REAL, disk_max REAL, load1 REAL,
    rx_bps REAL, tx_bps REAL, net_errors INTEGER, encoders INTEGER, hls_status TEXT, hls_age_s REAL, data TEXT);
CREATE INDEX IF NOT EXISTS agent_telemetry_agent_ts ON agent_telemetry (agent, ts);
CREATE TABLE IF NOT EXISTS agent_incidents (
    ts REAL NOT NULL, agent TEXT NOT NULL, node TEXT, reason TEXT, stream TEXT, segment_age_s REAL, data TEXT);
CREATE INDEX IF NOT EXISTS agent_incidents_ts ON agent_incidents (ts);
"""

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins=[], logger=False, engineio_logger=False)
router = APIRouter()
_lock = threading.Lock()
_online: Dict[str, dict] = {}  # agent id -> {"sid", "node", "since", "last": snapshot summary}
_pending: Dict[str, asyncio.Future] = {}  # sid -> waiting health_response
_last_prune = 0.0


def _srv():
    import server  # the app's shared state (driver, metrics path, alert log)
    return server


def _tokens() -> List[str]:
    raw = os.environ.get("AGENT_TOKENS") or os.environ.get("AGENT_TOKEN") or ""
    return [t.strip() for t in raw.split(",") if t.strip()]


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(_srv().metrics.path, timeout=10)
    db.executescript(SCHEMA)
    return db


def match_node(agent_id: str, domains: List[str]) -> Optional[str]:
    """The graph server an agent runs on: the exact domain, else the domain whose first label is the agent id
    ("ingest1" -> "ingest1.ottlive.co.in"). Ambiguous or unknown: None."""
    agent = (agent_id or "").strip().lower()
    if not agent:
        return None
    exact = [d for d in domains if d.lower() == agent]
    if exact:
        return exact[0]
    by_label = [d for d in domains if "/" not in d and d.lower().split(".")[0] == agent]
    return by_label[0] if len(by_label) == 1 else None


def _graph_domains() -> List[str]:
    return [r["d"] for r in _srv().driver.execute_query(
        "MATCH (n:Domain) WHERE NOT n.domain ENDS WITH '.invalid' RETURN n.domain AS d").records]


def _stream_url(node: Optional[str]) -> Optional[str]:
    """The server's own stream to watch from the inside: its Main input link first, else any link on it."""
    if not node:
        return None
    from nodes import load_json_list
    rec = _srv().driver.execute_query("MATCH (n:Domain {domain: $d}) RETURN n.links AS l", d=node).records
    links = load_json_list(rec[0]["l"]) if rec else []
    order = {"MainInput": 0, "Transcoding": 1, "BackupLink": 2, "FinalLink": 3}
    links = sorted((l for l in links if l.get("url")), key=lambda l: order.get(l.get("role"), 9))
    return links[0]["url"] if links else None


def summarize(snap: dict) -> dict:
    """The numbers worth a column (the rest of the snapshot is kept as compact JSON)."""
    disks = [d.get("usage") or 0 for d in snap.get("disk") or []]
    nets = snap.get("network") or []
    hls = snap.get("hls") or {}
    return {
        "cpu": (snap.get("cpu") or {}).get("usage"),
        "memory": (snap.get("memory") or {}).get("usage"),
        "disk_max": max(disks) if disks else None,
        "load1": (snap.get("cpu") or {}).get("load1"),
        "rx_bps": sum((n.get("rxSec") or 0) for n in nets) * 8,
        "tx_bps": sum((n.get("txSec") or 0) for n in nets) * 8,
        "net_errors": sum((n.get("rxErrors") or 0) + (n.get("txErrors") or 0) for n in nets),
        "encoders": len(snap.get("encoders") or []),
        "hls_status": hls.get("status"),
        "hls_age_s": hls.get("segmentAge"),
    }


def _store_telemetry(agent: str, node: Optional[str], snap: dict) -> dict:
    global _last_prune
    s = summarize(snap)
    keep = {k: snap.get(k) for k in ("hostname", "uptime", "encoders", "processes", "system", "hls", "disk")}
    with _lock, _db() as db:
        db.execute("INSERT INTO agent_telemetry (ts, agent, node, cpu, memory, disk_max, load1, rx_bps, tx_bps, "
                   "net_errors, encoders, hls_status, hls_age_s, data) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (time.time(), agent, node, s["cpu"], s["memory"], s["disk_max"], s["load1"], s["rx_bps"],
                    s["tx_bps"], s["net_errors"], s["encoders"], s["hls_status"], s["hls_age_s"],
                    json.dumps(keep, separators=(",", ":"))[:20000]))
        if time.time() - _last_prune > 3600:
            _last_prune = time.time()
            cutoff = time.time() - TELEMETRY_DAYS * 86400
            db.execute("DELETE FROM agent_telemetry WHERE ts < ?", (cutoff,))
            db.execute("DELETE FROM agent_incidents WHERE ts < ?", (cutoff,))
    return s


# --- socket.io events -------------------------------------------------------------------------------------------

@sio.event
async def connect(sid, environ, auth):
    auth = auth or {}
    token, agent = str(auth.get("token") or ""), str(auth.get("agentId") or "").strip()[:100]
    tokens = _tokens()
    if not tokens or not agent or not any(hmac.compare_digest(token, t) for t in tokens):
        print(f"[agents] refused a connection (agent={agent or '?'}): bad or missing token")
        raise socketio.exceptions.ConnectionRefusedError("unauthorized")
    try:
        node = await asyncio.to_thread(lambda: match_node(agent, _graph_domains()))
        hls = await asyncio.to_thread(_stream_url, node)
    except Exception as e:  # the graph being unavailable must not keep an agent out
        print(f"[agents] {agent}: graph lookup failed ({type(e).__name__}: {e})")
        node, hls = None, None
    old = _online.get(agent)
    if old and old["sid"] != sid:
        await sio.disconnect(old["sid"])  # the same agent reconnecting: keep only the newest connection
    _online[agent] = {"sid": sid, "node": node, "since": time.time(), "last": None, "lastAt": None}
    await sio.save_session(sid, {"agent": agent, "node": node})
    print(f"[agents] {agent} connected" + (f" as {node}" if node else " (no matching server in the graph)"))
    if hls:
        await sio.emit("configure_hls", {"hlsUrl": hls}, to=sid)


@sio.event
async def disconnect(sid, *args):
    try:
        session = await sio.get_session(sid)
    except KeyError:
        return
    agent = session.get("agent")
    if agent in _online and _online[agent]["sid"] == sid:
        _online[agent]["sid"] = None
        _online[agent]["offlineSince"] = time.time()
        print(f"[agents] {agent} disconnected")


@sio.on("telemetry")
async def on_telemetry(sid, snap):
    if not isinstance(snap, dict):
        return
    session = await sio.get_session(sid)
    agent, node = session["agent"], session["node"]
    summary = await asyncio.to_thread(_store_telemetry, agent, node, snap)
    if agent in _online:
        _online[agent]["last"], _online[agent]["lastAt"] = summary, time.time()


@sio.on("incident")
async def on_incident(sid, incident):
    if not isinstance(incident, dict):
        return
    session = await sio.get_session(sid)
    agent, node = session["agent"], session["node"]
    reason = str(incident.get("reason") or "agent incident")[:500]
    kind = "AGENT_" + reason.split(":")[0].strip().upper().replace(" ", "_")[:40]

    def store():
        with _lock, _db() as db:
            db.execute("INSERT INTO agent_incidents (ts, agent, node, reason, stream, segment_age_s, data) "
                       "VALUES (?,?,?,?,?,?,?)", (time.time(), agent, node, reason, incident.get("stream"),
                                                  incident.get("segmentAge"),
                                                  json.dumps(summarize(incident.get("health") or {}))))
        try:  # alongside every other alert (history, "Up next" learning)
            _srv().alert_log.add(kind, node or agent, None, reason, "agent")
        except Exception:
            pass
    await asyncio.to_thread(store)
    print(f"[agents] {agent} INCIDENT {reason}")


@sio.on("health_response")
async def on_health_response(sid, snap):
    future = _pending.pop(sid, None)
    if future and not future.done():
        future.set_result(snap)


# --- dashboard API ----------------------------------------------------------------------------------------------

@router.get("/api/agents")
def list_agents():
    """Every agent seen since the app started: online or not, the server it runs on, its latest numbers."""
    now = time.time()
    return {"tokensConfigured": bool(_tokens()), "agents": [
        {"agent": a, "node": v["node"], "online": bool(v["sid"]), "connectedSince": v["since"],
         "offlineSince": v.get("offlineSince"), "lastTelemetryAgo": round(now - v["lastAt"], 1) if v["lastAt"] else None,
         "latest": v["last"]}
        for a, v in sorted(_online.items())]}


@router.get("/api/agents/{agent}/telemetry")
def agent_telemetry(agent: str, since: int = Query(3600, ge=60, le=TELEMETRY_DAYS * 86400), limit: int = Query(2000, le=20000)):
    with _db() as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT ts, cpu, memory, disk_max, load1, rx_bps, tx_bps, net_errors, encoders, hls_status, "
                          "hls_age_s FROM agent_telemetry WHERE agent = ? AND ts >= ? ORDER BY ts DESC LIMIT ?",
                          (agent, time.time() - since, limit)).fetchall()
        incidents = db.execute("SELECT ts, reason, stream, segment_age_s FROM agent_incidents WHERE agent = ? AND ts >= ? "
                               "ORDER BY ts DESC LIMIT 200", (agent, time.time() - since)).fetchall()
    return {"agent": agent, "points": [dict(r) for r in reversed(rows)], "incidents": [dict(r) for r in incidents]}


@router.get("/api/agents/{agent}/snapshot")
async def agent_snapshot(agent: str):
    """Asks a connected agent for a fresh, full snapshot right now."""
    info = _online.get(agent)
    if not info or not info["sid"]:
        raise HTTPException(404, f"Agent {agent} is not connected")
    future = asyncio.get_running_loop().create_future()
    _pending[info["sid"]] = future
    await sio.emit("health_check", {}, to=info["sid"])
    try:
        return await asyncio.wait_for(future, SNAPSHOT_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise HTTPException(504, f"Agent {agent} did not answer in {SNAPSHOT_TIMEOUT_S} s")
    finally:
        _pending.pop(info["sid"], None)


# --- installation -----------------------------------------------------------------------------------------------

class _AgentSocket:
    """ASGI middleware: /socket.io/* goes to the agent hub; everything else to the app unchanged."""

    def __init__(self, app):
        self.app = app
        self.sio_app = socketio.ASGIApp(sio, socketio_path="socket.io")

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and scope.get("path", "").startswith("/socket.io"):
            return await self.sio_app(scope, receive, send)
        return await self.app(scope, receive, send)


def install(app) -> None:
    """Adds the agent endpoint and its API to the FastAPI app (call after every other middleware)."""
    app.include_router(router)
    app.add_middleware(_AgentSocket)
