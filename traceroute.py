"""Tiered diagnostics: ping first, traceroute on threshold.

The 30 s spider cycle stays lightweight (HTTP + ICMP ping). When a server keeps failing
(consecutiveFailures reaches TRACEROUTE_THRESHOLD, default = the alert threshold of 10) a traceroute to
that host runs in a background thread, at most once per host every TRACEROUTE_COOLDOWN_S (15 min).
Operators can also start one on demand (short cooldown against double clicks).

The result is stored as JSON on the node (`n.traceroute`, `n.tracerouteAt`) and attached to the RCA of
any spider that stopped at that node.

Uses the system `traceroute` in its default UDP mode, which needs no root on Linux or macOS
(icmplib's traceroute needs raw sockets, i.e. root).
"""

import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

log = logging.getLogger(__name__)

TRACEROUTE_THRESHOLD = int(os.environ.get("TRACEROUTE_THRESHOLD", os.environ.get("ALERT_CONSECUTIVE_THRESHOLD", "10")))
TRACEROUTE_COOLDOWN_S = int(os.environ.get("TRACEROUTE_COOLDOWN_S", "900"))  # automatic: 1 per host / 15 min
MANUAL_COOLDOWN_S = 30  # on demand: just guards against repeated clicks
MAX_HOPS, QUERIES, WAIT_S = 20, 3, 2
TIMEOUT_S = MAX_HOPS * QUERIES * WAIT_S + 10  # worst case: every probe times out
FAST_MS, SLOW_MS = 50, 100

_IP = r"(?:\d{1,3}(?:\.\d{1,3}){3}|[0-9a-fA-F]*:[0-9a-fA-F:]+)"
_HEADER = re.compile(rf"traceroute6? to (\S+) \(({_IP})\)")
_HOP = re.compile(r"^\s*(\d+)\s+(.*)$")
_TOKEN = re.compile(rf"({_IP})|(\d+(?:\.\d+)?)\s*ms|(\*)|(![A-Za-z0-9]*)")


def parse_traceroute(output: str) -> dict:
    """Parses `traceroute -n` output (Linux or macOS) into the target and per-hop probes.

    A hop line may list several routers (load balancing); macOS puts extra routers on
    continuation lines. Every "N ms" belongs to the router named before it.
    """
    target_ip, hops = None, []
    for line in output.splitlines():
        header = _HEADER.search(line)
        if header:
            target_ip = header.group(2)
            continue
        match = _HOP.match(line)
        if match:
            hops.append({"hop": int(match.group(1)), "probes": []})
            rest = match.group(2)
        elif hops and line.startswith((" ", "\t")) and line.strip():
            rest = line
        else:
            continue
        router = hops[-1]["probes"][-1]["ip"] if hops[-1]["probes"] else None
        for ip, rtt, star, _flag in _TOKEN.findall(rest):
            if ip:
                router = ip
            elif rtt:
                hops[-1]["probes"].append({"ip": router, "rtt_ms": float(rtt)})
            elif star:
                hops[-1]["probes"].append({"ip": None, "rtt_ms": None})
    return {"target_ip": target_ip, "hops": [_summarise_hop(h) for h in hops]}


def _summarise_hop(hop: dict) -> dict:
    probes = hop["probes"]
    rtts = [p["rtt_ms"] for p in probes if p["rtt_ms"] is not None]
    ips = list(dict.fromkeys(p["ip"] for p in probes if p["ip"]))
    sent = max(len(probes), 1)
    lost = sent - len(rtts)
    avg = round(sum(rtts) / len(rtts), 1) if rtts else None
    if not rtts:
        status = "timeout"
    elif avg > SLOW_MS:
        status = "slow"
    elif avg >= FAST_MS:
        status = "fair"
    else:
        status = "ok"
    return {"hop": hop["hop"], "ip": ips[0] if ips else None, "ips": ips, "rtt_ms": avg,
            "rtts_ms": rtts, "sent": sent, "lost": lost, "loss_pct": round(100 * lost / sent),
            "status": status}


def analyse(parsed: dict) -> dict:
    """Where do packets start dropping?

    Routers often don't answer traceroute probes (rate limiting) while still forwarding traffic, so a
    single silent hop followed by answering hops is not a drop. The drop point is:
    - destination not reached: the first hop of the final run of silent hops (packets never get past it);
      or, if the last hop answered but hops ran out, none (path is longer than the hop limit);
    - destination reached: none (loss that doesn't persist to the destination is rate limiting).
    If nothing answers beyond a private first hop (the local gateway), the trace is inconclusive, not a drop:
    that is what a network which blocks traceroute replies looks like (e.g. Docker Desktop on macOS).
    """
    hops, target = parsed["hops"], parsed["target_ip"]
    reached = bool(target) and any(target in h["ips"] for h in hops)
    answered = [h for h in hops if h["status"] != "timeout"]
    inconclusive = (not reached and len(hops) > 2 and len(answered) == 1 and answered[0]["hop"] == 1
                    and _is_private(answered[0]["ip"]))
    drop = None
    if not reached and not inconclusive and hops and hops[-1]["status"] == "timeout":
        drop = hops[-1]["hop"]
        for h in reversed(hops):
            if h["status"] != "timeout":
                break
            drop = h["hop"]
    for h in hops:
        h["drop"] = h["hop"] == drop
        # Some probes unanswered at a hop that traffic still passes: ICMP rate limiting, not loss.
        h["rate_limited"] = h["lost"] > 0 and not h["drop"] and (drop is None or h["hop"] < drop)
        h["transit"] = identify_asn_or_cloud(h.get("ip"))
    last_ok = next((h for h in reversed(hops) if h["status"] != "timeout"), None)
    if inconclusive:
        summary = (f"Inconclusive: no replies beyond the local gateway ({answered[0]['ip']}). Traceroute replies "
                   f"are blocked from where the monitor runs, so no drop point can be named.")
    elif reached:
        summary = f"Destination {target} reached in {len(hops)} hops"
        slow = [h for h in hops if h["status"] == "slow"]
        if slow:
            summary += f"; high latency from hop {slow[0]['hop']} ({slow[0]['ip']}, {slow[0]['rtt_ms']} ms)"
    elif drop is not None:
        after = f" after hop {last_ok['hop']} ({last_ok['ip']})" if last_ok else ""
        summary = f"Packet drop detected at hop {drop}{after}: destination {target or '?'} not reached"
    else:
        summary = f"Destination {target or '?'} not reached within {len(hops)} hops"
    return {"reached": reached, "drop_hop": drop, "inconclusive": inconclusive, "hop_count": len(hops),
            "summary": summary, "target_transit": identify_asn_or_cloud(target)}


def identify_asn_or_cloud(ip: Optional[str]) -> Optional[str]:
    """Identifies the transit backbone, ISP, or cloud provider of an IP."""
    if not ip:
        return None
    if _is_private(ip):
        return "Internal / Local Gateway"
    if ip.startswith(("148.113.", "51.", "144.217.", "149.56.", "198.245.")):
        return "OVHcloud (AS16276)"
    if ip.startswith(("182.79.", "122.160.", "125.16.", "125.17.")):
        return "Bharti Airtel (AS9498)"
    if ip.startswith(("115.113.", "180.149.", "203.197.", "14.141.")):
        return "Tata Communications (AS4755)"
    if ip.startswith(("104.", "172.67.", "162.158.")):
        return "Cloudflare (AS13335)"
    if ip.startswith(("3.", "13.", "52.", "54.", "15.", "18.")):
        return "AWS Cloud (AS16509)"
    if ip.startswith(("34.", "35.")):
        return "Google Cloud (AS15169)"
    return "Tier-1 Transit Backbone"


def _is_private(ip: Optional[str]) -> bool:
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


def run_traceroute(host: str) -> dict:
    """Runs one traceroute (blocking; call from a worker thread)."""
    binary = shutil.which("traceroute")
    if not binary:
        raise RuntimeError("traceroute is not installed on the server")
    started = time.monotonic()
    proc = subprocess.run([binary, "-n", "-q", str(QUERIES), "-w", str(WAIT_S), "-m", str(MAX_HOPS), host],
                          capture_output=True, text=True, timeout=TIMEOUT_S)
    output = proc.stdout + "\n" + proc.stderr  # macOS writes the header line to stderr
    parsed = parse_traceroute(output)
    if not parsed["hops"]:
        raise RuntimeError((proc.stderr or proc.stdout).strip()[:300] or f"traceroute exited with {proc.returncode}")
    return {**parsed, **analyse(parsed), "duration_ms": int((time.monotonic() - started) * 1000)}


def host_of(node: dict) -> str:
    """The address to trace: the node's IP, else its domain (channel Finals are "<host>/<channel>")."""
    return node.get("server_ip") or node.get("serverIp") or str(node.get("id") or node.get("domain")).split("/", 1)[0]


SAVE_TRACEROUTE = """
MATCH (n:Domain {domain: $node})
SET n.traceroute = $result, n.tracerouteAt = datetime()
WITH n
OPTIONAL MATCH (s:SpiderRun) WHERE s.stopNodeId = n.domain
RETURN collect({id: s.id, rca: s.rca}) AS spiders
"""


def attach_to_rca(rca_json: Optional[str], result: dict) -> Optional[str]:
    """The traceroute summary inside a spider's RCA JSON (hops stay on the node)."""
    if not rca_json:
        return None
    try:
        rca = json.loads(rca_json)
    except ValueError:
        return None
    rca["traceroute"] = rca_summary(result)
    return json.dumps(rca)


def rca_summary(result: dict) -> dict:
    return {k: result.get(k) for k in ("node", "host", "target_ip", "at", "trigger", "reached", "drop_hop",
                                       "inconclusive", "hop_count", "summary", "error")}


class Cooldown(Exception):
    def __init__(self, retry_in: int):
        super().__init__(f"A traceroute to this host ran recently; try again in {retry_in} s.")
        self.retry_in = retry_in


class Busy(Exception):
    pass


class TracerouteManager:
    """Starts traceroutes in the background, one at a time per host, with per-host cooldowns."""

    def __init__(self, driver, on_update: Optional[Callable[[dict], None]] = None,
                 runner: Callable[[str], dict] = run_traceroute, workers: int = 2):
        self.driver = driver
        self.on_update = on_update or (lambda event: None)
        self.runner = runner
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="traceroute")
        self.lock = threading.Lock()
        self.last_auto: Dict[str, float] = {}  # host -> when the last automatic trace started
        self.last_any: Dict[str, float] = {}
        self.running: Dict[str, str] = {}  # host -> node id

    def on_node_checked(self, node: dict, up: bool, consecutive_failures: int) -> None:
        """Called by the spider cycle after each node's health is written; never blocks it."""
        if up or consecutive_failures < TRACEROUTE_THRESHOLD:
            return
        try:
            self.start(node, trigger="threshold",
                       reason=f"{consecutive_failures} consecutive failures (threshold {TRACEROUTE_THRESHOLD})")
        except (Cooldown, Busy):
            pass

    def start(self, node: dict, trigger: str = "manual", reason: str = "Requested from the dashboard") -> dict:
        node_id, host = node.get("id") or node.get("domain"), host_of(node)
        now = time.time()
        with self.lock:
            if host in self.running:
                raise Busy(f"A traceroute to {host} is already running.")
            if trigger == "manual":
                wait = MANUAL_COOLDOWN_S - (now - self.last_any.get(host, 0))
            else:
                wait = TRACEROUTE_COOLDOWN_S - (now - self.last_auto.get(host, 0))
            if wait > 0:
                raise Cooldown(int(wait) + 1)
            self.running[host] = node_id
            self.last_any[host] = now
            if trigger != "manual":
                self.last_auto[host] = now
        job = {"node": node_id, "host": host, "trigger": trigger, "reason": reason,
               "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self.on_update({**job, "state": "running"})
        self.pool.submit(self._run, job)
        return job

    def status(self, node_id: str) -> Optional[dict]:
        with self.lock:
            host = next((h for h, n in self.running.items() if n == node_id), None)
        return {"running": host is not None, "host": host}

    def _run(self, job: dict) -> None:
        try:
            result = {**job, **self.runner(job["host"])}
        except Exception as e:  # not installed, unresolvable host, timeout, ...
            result = {**job, "error": f"{type(e).__name__}: {e}", "hops": [], "reached": False, "drop_hop": None,
                      "hop_count": 0, "summary": f"Traceroute failed: {e}"}
        try:
            self.save(result)
        except Exception:
            log.exception("could not store traceroute for %s", job["node"])
        finally:
            with self.lock:
                self.running.pop(job["host"], None)
        self.on_update({**rca_summary(result), "state": "done"})

    def save(self, result: dict) -> None:
        record = self.driver.execute_query(SAVE_TRACEROUTE, node=result["node"], result=json.dumps(result)).records
        for spider in (record[0]["spiders"] if record else []):
            if spider.get("id"):
                rca = attach_to_rca(spider.get("rca"), result)
                if rca:
                    self.driver.execute_query("MATCH (s:SpiderRun {id: $id}) SET s.rca = $rca", id=spider["id"], rca=rca)

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)


def stored_traceroute(driver, node_id: str) -> Optional[dict]:
    records = driver.execute_query("MATCH (n:Domain {domain: $id}) RETURN n.traceroute AS t", id=node_id).records
    if not records or not records[0]["t"]:
        return None
    try:
        return json.loads(records[0]["t"])
    except ValueError:
        return None

