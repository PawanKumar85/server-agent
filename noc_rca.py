"""NOC root-cause analysis for one server: health checks + traceroute -> where the fault is and what to do.

The facts are decided in code, because a small local model can't be trusted to reason them out:
- each traceroute hop is labelled with its network owner (Team Cymru IP-to-ASN whois: local network, local
  ISP, transit carrier, or the destination's network);
- the fault zone (local/ISP, upstream transit, or the target server) follows from where the path breaks
  (traceroute.analyse) and which network that hop belongs to, or from HTTP vs ping when the path is fine;
- the drop point and a recommended action follow from those.

The model (OpenRouter) then writes the operator-facing RCA from the NOC prompt, with those findings
included in its input.
"""

import ipaddress
import json
import os
import re
import socket
import threading
import time
from typing import Dict, Iterator, List, Optional

from chatbot import ROLE_NAMES, Snapshot
from traceroute import host_of

WHOIS_HOST, WHOIS_TIMEOUT_S = "whois.cymru.com", 6
ASN_CACHE_S = 24 * 3600
# The diagnosis benefits from some reasoning, kept cheap: low effort, reasoning text not sent back (it is still
# billed), and a cap that leaves room for the three sections after the reasoning.
GENERATION = {"max_tokens": 1200, "temperature": 0.1, "reasoning": {"effort": "low", "exclude": True}}
ZONES = {
    "local": "(a) Local network/ISP",
    "transit": "(b) Upstream transit carrier",
    "server": "(c) Target streaming server",
    "none": "No fault (server healthy)",
    "unknown": "Undetermined",
}

PROMPT = """You are a Senior Network & Streaming Operations Engineer (NOC). Analyze the following streaming server \
health check, node agent server telemetry, and traceroute diagnostic data and provide a concise, actionable root cause analysis (RCA).

---
### Input Diagnostic Data:
- Target Server: {server_domain} ({server_ip})
- Stream Channel: {channel_name} ({role})
- HTTP Health: {http_status_code} ({last_error})
- Consecutive Ping Failures: {consecutive_failures}
- Ping RTT: {avg_latency_ms} ms (Loss: {packet_loss_percent}%)
{agent_telemetry_section}

### Traceroute Hop Path:
{traceroute_hops}

### Automated Findings (from the monitor; treat as correct):
- Fault zone: {zone}
- Drop point: {drop_point}
- Suggested action: {action}
---

### Output Requirements:
1. Executive Summary: 1–2 sentences explaining whether the fault is in (a) Local network/ISP, (b) Upstream transit \
carrier, or (c) Target streaming server.
2. Exact Drop Point: Identify the specific IP or hop where the route broke.
3. Recommended Action: Specific step for the operator (e.g. "Reroute BGP peering", "Contact carrier NOC", \
"Reboot origin encoder", or "Failover to BackupLink").

Write only these three sections, 1–2 sentences each, and keep them consistent with the Automated Findings."""


# --- IP -> network owner (Team Cymru whois, bulk mode) ---

_asn_cache: Dict[str, tuple] = {}
_asn_lock = threading.Lock()


def lookup_asns(ips: List[str]) -> Dict[str, dict]:
    """{ip: {"asn": "9498", "name": "BHARTI Airtel Ltd., IN"}} for public IPs; best effort, cached for a day."""
    now = time.time()
    public = [ip for ip in dict.fromkeys(ips) if ip and not _is_private(ip)]
    with _asn_lock:
        missing = [ip for ip in public if ip not in _asn_cache or now - _asn_cache[ip][0] > ASN_CACHE_S]
    if missing:
        try:
            with socket.create_connection((WHOIS_HOST, 43), timeout=WHOIS_TIMEOUT_S) as sock:
                sock.sendall(("begin\nverbose\n" + "\n".join(missing) + "\nend\n").encode())
                raw = b""
                while chunk := sock.recv(65536):
                    raw += chunk
            found = parse_cymru(raw.decode(errors="replace"))
        except OSError:
            found = {}
        with _asn_lock:
            for ip in missing:
                _asn_cache[ip] = (now, found.get(ip))
    with _asn_lock:
        return {ip: _asn_cache[ip][1] for ip in public if _asn_cache.get(ip, (0, None))[1]}


def parse_cymru(text: str) -> Dict[str, dict]:
    """Parses verbose bulk output: 'AS | IP | prefix | CC | registry | allocated | AS name'."""
    out = {}
    for line in text.splitlines():
        cols = [c.strip() for c in line.split("|")]
        if len(cols) >= 7 and cols[0].isdigit():
            name = cols[6].split(" - ", 1)[-1]
            out[cols[1]] = {"asn": cols[0], "name": name}
    return out


def _is_private(ip: Optional[str]) -> bool:
    try:
        return ipaddress.ip_address(ip).is_private
    except (ValueError, TypeError):
        return False


def org_key(name: Optional[str]) -> Optional[str]:
    """Same company across its ASNs ("Bharti Airtel Ltd., Telemedia" / "BHARTI Airtel Ltd., IN")."""
    if not name:
        return None
    words = re.findall(r"[a-z0-9]+", name.lower())
    return " ".join(words[:2]) or None


# --- hop labels and the fault zone ---

def label_hops(trace: dict, asns: Dict[str, dict]) -> List[dict]:
    """Each hop with `network` (local/isp/transit/destination/unknown) and the owner's name."""
    hops = trace.get("hops") or []
    target = trace.get("target_ip")
    dest_org = org_key((asns.get(target) or {}).get("name"))
    isp_org, seen_public, labelled = None, False, []
    for h in hops:
        ip, info = h.get("ip"), asns.get(h.get("ip") or "")
        org = org_key(info["name"]) if info else None
        if ip is None:
            network, owner = "unknown", None
        elif _is_private(ip):
            network = "local" if not seen_public else "provider-internal"
            owner = "Local network" if not seen_public else "Private address inside a provider"
        else:
            seen_public = True
            owner = f"AS{info['asn']} {info['name']}" if info else "Unknown network"
            if ip == target or (org and org == dest_org):
                network = "destination"
            elif isp_org is None or org == isp_org:
                isp_org = isp_org or org
                network = "isp"
            else:
                network = "transit"
        labelled.append({**h, "network": network, "owner": owner})
    # Private hops inside a provider belong to whichever network comes next.
    for i, h in enumerate(labelled):
        if h["network"] == "provider-internal":
            nxt = next((x for x in labelled[i + 1:] if x["network"] in ("isp", "transit", "destination")), None)
            h["network"] = nxt["network"] if nxt else "transit"
    return labelled


def classify(node: dict, trace: Optional[dict], hops: List[dict]) -> dict:
    """Fault zone, drop point and action from the node's health and its labelled traceroute. A failing Main
    input whose Backup is healthy is failed over first, whatever the zone."""
    result = _classify(node, trace, hops)
    if result["zone"] != "none" and node["backupUp"]:
        action = result["action"]
        result["action"] = (f"Failover to BackupLink ({', '.join(node['backupUp'])} is up) now; then "
                            f"{action[0].lower()}{action[1:]}")
    return result


def _classify(node: dict, trace: Optional[dict], hops: List[dict]) -> dict:
    failing = node["status"] == "DOWN" or (node["consecutiveFailures"] or 0) > 0
    ping_alive = node["packetLoss"] is not None and node["packetLoss"] < 100
    drop = trace.get("drop_hop") if trace else None
    if not failing:
        return {"zone": "none", "drop_point": "None: the server is answering.", "last_good": None,
                "action": "No action needed; keep monitoring."}
    if drop is not None:
        before = [h for h in hops if h["hop"] < drop and h.get("status") != "timeout"]
        last = before[-1] if before else None
        where = (f"Hop {drop} (no reply after hop {last['hop']} {last['ip']}, {last['owner']})"
                 if last else f"Hop {drop} (nothing answered before it)")
        network = last["network"] if last else "local"
        if network in ("local", "isp"):
            zone = "local"
            action = (f"Check the monitor's own uplink and contact the local ISP NOC"
                      f"{' (' + last['owner'] + ')' if last and last['network'] == 'isp' else ''} with this traceroute.")
        elif network == "transit":
            zone = "transit"
            action = (f"Contact the carrier NOC for {last['owner']} with this traceroute; if it persists, "
                      f"reroute or depeer around that carrier (BGP).")
        else:
            zone = "server"
            action = server_action(node, "the destination network drops traffic before the server")
        return {"zone": zone, "drop_point": where, "last_good": last and last["ip"], "action": action}
    reached = bool(trace and trace.get("reached"))
    if reached or ping_alive:
        why = "the network path reaches the server" if reached else "the server answers ping"
        agent = node.get("agent")
        if agent:
            if agent.get("encoders") == 0 and agent.get("had_encoders"):
                why += " and agent reports 0 active encoder processes (crashed/stopped)"
            elif agent.get("disk_max") and agent["disk_max"] >= 95:
                why += f" and agent reports storage critical ({agent['disk_max']}% full)"
            elif agent.get("memory") and agent["memory"] >= 95:
                why += f" and agent reports severe memory exhaustion ({agent['memory']}%)"
            elif agent.get("hls_age_s") and agent["hls_age_s"] > 20:
                why += f" and agent reports local HLS segments are stale ({round(agent['hls_age_s'], 1)}s old)"
        return {"zone": "server", "drop_point": f"None in the network: {why}, but the stream check fails.",
                "last_good": trace and trace.get("target_ip"), "action": server_action(node, why, agent)}
    note = (" (the traceroute was inconclusive from where the monitor runs)" if trace and trace.get("inconclusive")
            else " (no traceroute yet)" if not trace else "")
    return {"zone": "unknown", "drop_point": f"Unknown{note}.", "last_good": None,
            "action": "Run a traceroute from a host with a real network path (not Docker Desktop), "
                      "and ping the server from another location."}


def server_action(node: dict, why: str, agent: Optional[dict] = None) -> str:
    if agent:  # only passed when the network reaches the server (a drop on the way is not the host's fault)
        if agent.get("encoders") == 0 and agent.get("had_encoders"):
            return "Restart streaming encoder: 0 active encoder processes (ffmpeg) found running on the server."
        if agent.get("disk_max") and agent["disk_max"] >= 95:
            return f"Free disk space on origin host: storage is {agent['disk_max']}% full, blocking segment writes."
        if (agent.get("memory") and agent["memory"] >= 95) or (agent.get("cpu") and agent["cpu"] >= 98):
            return (f"Host resource exhaustion (CPU {agent.get('cpu')}%, RAM {agent.get('memory')}%): "
                    f"terminate hung processes or scale origin server.")
        if agent.get("hls_age_s") and agent["hls_age_s"] > 20:
            return (f"Restart stream pipeline: origin agent reports local HLS segments are stale "
                    f"({round(agent['hls_age_s'], 1)}s old).")
        if agent.get("net_errors_new") and agent["net_errors_new"] > NIC_ERRORS_PER_CHECK:
            return (f"Inspect server network interface: {agent['net_errors_new']} new NIC errors since the "
                    f"agent's previous report.")

    code = node.get("httpCode")
    if code == 404:
        return "Check the publishing point: the stream path is missing on the origin (restart the origin encoder)."
    if code and code >= 500:
        return "Restart the origin server / packager and check its logs."
    return "Reboot the origin encoder and check the server's streaming service."


# --- the node's data, formatted for the prompt ---

NODE_QUERY = """
MATCH (n:Domain {domain: $id})
RETURN properties(n) AS p, [l IN labels(n) WHERE l <> 'Domain'] AS roles
"""


AGENT_FRESH_S = 120  # older telemetry is about a past state (the agent is offline), not this failure
NIC_ERRORS_PER_CHECK = 50  # new interface errors between two reports (the agent sends counters since boot)


def load_agent_telemetry(node_id: str) -> Optional[dict]:
    """The Node Agent's latest report for this server, if it is from the last AGENT_FRESH_S seconds; with
    net_errors_new (errors since its previous report) and had_encoders (the host ran an encoder in the last day,
    so 0 encoders now means one stopped; a host that never runs one, like an edge, has nothing to blame)."""
    try:
        from metrics import store as metrics_store
        import sqlite3
        now = time.time()
        with sqlite3.connect(metrics_store().path, timeout=5) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT ts, agent, node, cpu, memory, disk_max, load1, rx_bps, tx_bps, "
                "net_errors, encoders, hls_status, hls_age_s FROM agent_telemetry "
                "WHERE (node = ? OR agent = ?) AND ts >= ? ORDER BY ts DESC LIMIT 2",
                (node_id, node_id, now - AGENT_FRESH_S)).fetchall()
            if not rows:
                return None
            d = dict(rows[0])
            d["age_s"] = round(now - d["ts"], 1)
            prev = dict(rows[1]) if len(rows) > 1 and rows[1]["agent"] == d["agent"] else None
            d["net_errors_new"] = (d["net_errors"] - prev["net_errors"]
                                   if prev and d["net_errors"] is not None and prev["net_errors"] is not None
                                   and d["net_errors"] >= prev["net_errors"] else None)  # lower = counters reset
            d["had_encoders"] = bool(db.execute(
                "SELECT 1 FROM agent_telemetry WHERE agent = ? AND encoders > 0 AND ts >= ? LIMIT 1",
                (d["agent"], now - 86400)).fetchone())
            return d
    except Exception:
        return None


def load_node(driver, node_id: str) -> Optional[dict]:
    records = driver.execute_query(NODE_QUERY, id=node_id).records
    if not records:
        return None
    p, roles = dict(records[0]["p"]), records[0]["roles"]
    snap = Snapshot(driver)
    channels = snap.nodes.get(node_id, {}).get("channels", [])
    failing_urls = [e for c in channels for entries in snap.channels.get(c, {}).values()
                    for e in entries if e["node"] == node_id and e["up"] is False]
    detail = " ".join(filter(None, [p.get("lastError")] + [e.get("detail") for e in failing_urls]))
    code = re.search(r"HTTP[ _](\d{3})", detail)
    backup_up = sorted({e["node"] for c in channels for e in snap.channels.get(c, {}).get("BackupLink", [])
                        if e["up"] is not False and e["node"] != node_id}) if "MainInput" in roles else []
    try:
        trace = json.loads(p["traceroute"]) if p.get("traceroute") else None
    except ValueError:
        trace = None
    return {
        "id": node_id, "ip": p.get("server_ip") or host_of({"id": node_id}), "roles": roles, "channels": channels,
        "status": p.get("status", "UNKNOWN"), "consecutiveFailures": p.get("consecutiveFailures") or 0,
        "lastError": p.get("lastError") if p.get("status") != "UP" else None,
        "httpCode": int(code.group(1)) if code else None, "rttMs": p.get("lastRttMs"),
        "packetLoss": p.get("lastPacketLoss"), "latencyMs": p.get("lastLatencyMs"),
        "backupUp": backup_up, "traceroute": trace,
        "agent": load_agent_telemetry(node_id),
    }


def format_hops(hops: List[dict], trace: Optional[dict]) -> str:
    if not trace:
        return "No traceroute has been run for this server yet."
    if trace.get("error"):
        return f"Traceroute failed: {trace['error']}"
    lines = []
    for h in hops:
        if h.get("status") == "timeout":
            tag = "TIMEOUT [PACKET LOSS DETECTED - DROP POINT]" if h.get("drop") else "TIMEOUT (no reply)"
            lines.append(f"Hop {h['hop']}: * * * - {tag}")
            continue
        state = "HIGH LATENCY" if h["status"] == "slow" else "OK"
        loss = f", {h['loss_pct']}% loss" if h.get("loss_pct") else ""
        lines.append(f"Hop {h['hop']}: {h['ip']} ({h['rtt_ms']} ms{loss}) - {h['owner']} [{state}]")
    if trace.get("inconclusive"):
        lines.append("(Traceroute replies are blocked beyond the local gateway where the monitor runs.)")
    elif not trace.get("reached"):
        lines.append(f"Destination {trace.get('target_ip')} - DESTINATION UNREACHABLE")
    return "\n".join(lines)


def analyse_node(driver, node_id: str) -> Optional[dict]:
    """Everything the RCA needs: the node, labelled hops, the computed findings and the filled prompt."""
    node = load_node(driver, node_id)
    if node is None:
        return None
    trace = node["traceroute"]
    ips = [h.get("ip") for h in (trace or {}).get("hops", [])] + [(trace or {}).get("target_ip")]
    hops = label_hops(trace, lookup_asns([ip for ip in ips if ip])) if trace else []
    findings = classify(node, trace, hops)
    roles = ", ".join(ROLE_NAMES.get(r, r) for r in node["roles"])
    agent = node.get("agent")
    if agent:
        rx_mbps = round((agent.get("rx_bps") or 0) / 1_000_000, 2)
        tx_mbps = round((agent.get("tx_bps") or 0) / 1_000_000, 2)
        agent_lines = [
            "### Live Server Agent Telemetry (Host Internal Metrics):",
            f"- Reporting Agent: {agent.get('agent')} ({agent.get('age_s', 0)}s ago)",
            f"- CPU Usage: {agent.get('cpu')}% (Load1: {agent.get('load1')})",
            f"- Memory Usage: {agent.get('memory')}%",
            f"- Max Disk Storage: {agent.get('disk_max')}%",
            f"- Network Throughput: RX {rx_mbps} Mbps | TX {tx_mbps} Mbps (new NIC errors: {agent.get('net_errors_new') if agent.get('net_errors_new') is not None else 'n/a'})",
            f"- Active Encoders: {agent.get('encoders') if agent.get('encoders') is not None else 'not reported'}"
            f"{'' if agent.get('had_encoders') else ' (this host has not run an encoder in the last day)'}",
            f"- Origin HLS Freshness: {agent.get('hls_status') or 'n/a'} (Segment Age: {agent.get('hls_age_s')}s)"
        ]
        agent_section = "\n".join(agent_lines)
    else:
        agent_section = "- Node Agent: No agent telemetry connected for this host"

    prompt = PROMPT.format(
        server_domain=node_id, server_ip=node["ip"],
        channel_name=", ".join(node["channels"]) or "-", role=roles or "-",
        http_status_code=node["httpCode"] or ("OK" if node["status"] == "UP" else "no HTTP status"),
        last_error=node["lastError"] or "no error",
        consecutive_failures=node["consecutiveFailures"],
        avg_latency_ms=round(node["rttMs"], 1) if node["rttMs"] is not None else "n/a",
        packet_loss_percent=round(node["packetLoss"]) if node["packetLoss"] is not None else "n/a",
        agent_telemetry_section=agent_section,
        traceroute_hops=format_hops(hops, trace),
        zone=ZONES[findings["zone"]], drop_point=findings["drop_point"], action=findings["action"],
    )
    return {"node": node_id, "status": node["status"], "zone": findings["zone"], "zoneLabel": ZONES[findings["zone"]],
            "dropPoint": findings["drop_point"], "action": findings["action"],
            "tracerouteAt": (trace or {}).get("at"), "agent": agent,
            "hops": [{k: h.get(k) for k in ("hop", "ip", "network", "owner")} for h in hops],
            "prompt": prompt}


def rca_model(bot) -> str:
    """RCA_MODEL (any OpenRouter model id), else the ChatBot's model."""
    return os.environ.get("RCA_MODEL") or bot.model


def stream_rca(bot, analysis: dict) -> Iterator[dict]:
    """The computed findings first, then the model's written RCA, token by token."""
    findings = {k: v for k, v in analysis.items() if k != "prompt"}
    if analysis["zone"] == "none":
        # Nothing to explain, and the prompt's (a)/(b)/(c) framing makes models invent a fault, so no model.
        yield {"type": "findings", "model": "none (server healthy)", **findings}
        yield {"type": "token", "text": HEALTHY_RCA}
        yield {"type": "done", "elapsed_ms": 0, "truncated": False, "model": None}
        return
    model = rca_model(bot)
    yield {"type": "findings", "model": model, **findings}
    yield from bot.generate([{"role": "user", "content": analysis["prompt"]}], GENERATION, model=model)


HEALTHY_RCA = """### Executive Summary
No fault: the server is answering its stream checks, so there is nothing to attribute to the local network, a transit carrier or the server.

### Exact Drop Point
None.

### Recommended Action
No action needed; keep monitoring. Run this analysis again if the server starts failing."""
