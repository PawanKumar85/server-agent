"""Spider monitoring and root cause analysis over the stream topology.

Three things are kept apart in Neo4j:

* Topology   - resource nodes (MainInput, BackupLink, Transcoding, FinalLink) and
               their FEEDS / PRODUCES relationships. Read-only here.
* Node health - status, pingCount, ... properties on each resource node.
* Spider state - one (:SpiderRun) per FinalLink, located by a single [:AT] edge.

Each cycle, every spider starts at its FinalLink and walks upstream against the
media flow (Final -> Transcoding -> Main/Backup), health-checking and updating
each node it visits. It stops on the root cause: the most upstream failed node
whose own dependencies are healthy. MainInput/BackupLink are alternatives
(OR): one usable input keeps the transcoder fed. Transcoding and BackupLink
are optional in a chain; FinalLink and MainInput are required.
"""

import argparse
import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from neo4j import GraphDatabase
from pydantic import BaseModel

from outage_class import BACKUP_FAILURE, OUTAGE, backup_only, classify
from health import STALE_AFTER_TARGET_DURATIONS, NodeHealth, UrlCheck, check_node, failing, failure_text, http_client
import rca_rank
from metrics import store
from nodes import load_json_list
from traceroute import rca_summary

INPUT_ROLES = ("MainInput", "BackupLink")
SPIDER_STATES = ("RUNNING", "STOPPED", "RECOVERED", "ERROR")
ALERT_CONSECUTIVE_THRESHOLD = int(os.environ.get("ALERT_CONSECUTIVE_THRESHOLD", "10"))

Checker = Callable[[dict], Awaitable[NodeHealth]]
NodeHook = Callable[[dict, bool, int], None]  # (node, up, consecutive failures), called after each recorded check


# --- Topology (read-only) -----------------------------------------------------

# Automatic runs cover every real FinalLink; test fixtures (*.invalid) only run when named explicitly.
Q_FINAL_LINKS = "MATCH (f:Domain:FinalLink) WHERE NOT f.domain ENDS WITH '.invalid' RETURN f.domain AS id ORDER BY id"

# The whole subgraph upstream of the given FinalLinks, with each node's incoming edges.
Q_TOPOLOGY = """
MATCH (f:Domain:FinalLink) WHERE f.domain IN $finals
MATCH (f)<-[:FEEDS|PRODUCES*0..]-(n:Domain)
WITH DISTINCT n
OPTIONAL MATCH (up:Domain)-[r:FEEDS|PRODUCES]->(n)
RETURN n.domain AS id, [l IN labels(n) WHERE l <> 'Domain'] AS labels,
       n.url AS url, n.server_ip AS server_ip, n.links AS links,
       properties(n).checkType AS checkType,  // optional property: map access avoids "unknown key" warnings
       [e IN collect(DISTINCT {type: type(r), source: up.domain}) WHERE e.source IS NOT NULL] AS upstream
"""

# Single-purpose lookups (the spider itself works from Q_TOPOLOGY, loaded once per cycle).
Q_UPSTREAM_TRANSCODERS = """
MATCH (t:Domain:Transcoding)-[:PRODUCES]->(:Domain:FinalLink {domain: $id})
RETURN t.domain AS id ORDER BY id
"""

Q_INPUTS = """
MATCH (i:Domain)-[:FEEDS]->(:Domain {domain: $id})
WHERE i:MainInput OR i:BackupLink
RETURN i.domain AS id ORDER BY id
"""


def get_all_final_links(driver) -> List[str]:
    return [r["id"] for r in driver.execute_query(Q_FINAL_LINKS).records]


def get_upstream_transcoders(driver, final_id: str) -> List[str]:
    return [r["id"] for r in driver.execute_query(Q_UPSTREAM_TRANSCODERS, id=final_id).records]


def get_inputs(driver, node_id: str) -> List[str]:
    return [r["id"] for r in driver.execute_query(Q_INPUTS, id=node_id).records]


class Resource(BaseModel):
    id: str
    labels: List[str]
    url: List[str] = []
    server_ip: Optional[str] = None
    checkType: Optional[str] = None
    links: List[Dict[str, str]] = []  # each URL's {url, role, channel}

    @property
    def role(self) -> str:
        return "/".join(self.labels)

    def has(self, label: str) -> bool:
        return label in self.labels

    def carries(self, channel: str, roles=INPUT_ROLES) -> bool:
        """Does this node serve `channel` in one of `roles` (e.g. as its Main or Backup input)?"""
        return any(l.get("channel") == channel and l.get("role") in roles for l in self.links)

    @property
    def final_channel(self) -> Optional[str]:
        """The channel a per-channel FinalLink node delivers (None for other nodes)."""
        return next((l.get("channel") for l in self.links if l.get("role") == "FinalLink"), None)


class Unit(BaseModel):
    """One required dependency of a node: a single transcoder, or a group of alternative inputs."""

    kind: str  # "TRANSCODER" | "INPUTS"
    ids: List[str]


class Topology:
    """In-memory snapshot of the subgraph upstream of some FinalLinks (one query per cycle)."""

    def __init__(self, driver, final_ids: List[str]):
        self.nodes: Dict[str, Resource] = {}
        self.upstream: Dict[str, List[dict]] = {}
        for record in driver.execute_query(Q_TOPOLOGY, finals=final_ids).records:
            row = dict(record)
            # one entry per real edge, even when the node is reached from several FinalLinks
            upstream = {(e["type"], e["source"]): e for e in row.pop("upstream")}
            self.upstream[row["id"]] = list(upstream.values())
            row["links"] = load_json_list(row.get("links"))
            self.nodes[row["id"]] = Resource(**{k: v for k, v in row.items() if v is not None})

    def node(self, node_id: str) -> Resource:
        return self.nodes[node_id]

    def upstream_transcoders(self, final_id: str) -> List[str]:
        return sorted(
            e["source"] for e in self.upstream[final_id]
            if e["type"] == "PRODUCES" and self.node(e["source"]).has("Transcoding")
        )

    def inputs(self, node_id: str) -> List[str]:
        return sorted(
            e["source"] for e in self.upstream[node_id]
            if e["type"] == "FEEDS" and any(self.node(e["source"]).has(r) for r in INPUT_ROLES)
        )

    def has_main_input(self, final_id: str) -> bool:
        """FinalLink and MainInput are required in every chain (Transcoding and BackupLink are optional)."""
        mains = lambda node_id: any(self.node(i).has("MainInput") for i in self.inputs(node_id))
        return mains(final_id) or any(
            self.node(t).has("MainInput") or mains(t) for t in self.upstream_transcoders(final_id)
        )

    def units(self, node: Resource, channel: Optional[str] = None) -> List[Unit]:
        """Required dependencies, upstream of `node`. Every unit must be healthy (AND);
        within an INPUTS unit one healthy member is enough (OR).

        With `channel`, an input group only keeps the nodes that carry that channel as
        Main or Backup: a transcoder serving several channels is fed by different inputs
        for each (falls back to all inputs when no node has channel data).
        """
        units: List[Unit] = []
        if node.has("FinalLink"):
            units += [Unit(kind="TRANSCODER", ids=[t]) for t in self.upstream_transcoders(node.id)]
        if node.has("FinalLink") or node.has("Transcoding"):
            inputs = self.inputs(node.id)
            # A transcoder that also carries an input role is fed from its own host.
            if node.has("Transcoding") and any(node.has(r) for r in INPUT_ROLES):
                inputs.append(node.id)
            if channel:
                own = [i for i in inputs if self.node(i).carries(channel)]
                inputs = own or inputs
            if inputs:
                units.append(Unit(kind="INPUTS", ids=inputs))
        return units


async def run_query(driver, query: str, **params):
    """Run a (sync-driver) query in a worker thread so concurrent spiders' writes overlap."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: driver.execute_query(query, **params).records)


# --- Node state updater ----------------------------------------------------------

# Every attempt counts; failures and recoveries update counters. Runs in one transaction with
# the per-URL health merge below.
UPDATE_NODE_HEALTH = """
MATCH (n:Domain {domain: $id})
WITH n, n.status AS previous
SET n.pingCount = coalesce(n.pingCount, 0) + 1,
    n.lastPing = datetime(),
    n.status = $status,
    n.failedCount = coalesce(n.failedCount, 0) + CASE WHEN $up THEN 0 ELSE 1 END,
    n.consecutiveFailures = CASE WHEN $up THEN 0 ELSE coalesce(n.consecutiveFailures, 0) + 1 END,
    n.consecutiveSuccesses = CASE WHEN $up THEN coalesce(n.consecutiveSuccesses, 0) + 1 ELSE 0 END,
    n.lastLatencyMs = $latency,
    n.lastError = CASE WHEN $up THEN n.lastError ELSE $error END,
    n.lastRecovery = CASE WHEN $up AND previous = 'DOWN' THEN datetime() ELSE n.lastRecovery END,
    n.lastRttMs = $rtt,
    n.lastJitterMs = $jitter,
    n.lastPacketLoss = $loss,
    n.hopCount = coalesce($hops, n.hopCount),
    n.lastSegmentAgeS = coalesce($lastSegmentAgeS, n.lastSegmentAgeS),
    n.targetDurationS = coalesce($targetDurationS, n.targetDurationS),
    n.streamFreshness = coalesce($streamFreshness, n.streamFreshness),
    n.mediaSequence = coalesce($mediaSequence, n.mediaSequence),
    n.discontinuities = coalesce($discontinuities, n.discontinuities),
    n.urlHealth = coalesce($urlHealth, n.urlHealth)
RETURN previous,
       coalesce(n.consecutiveFailures, 0) AS consecutiveFailures,
       n.pingCount AS pingCount,
       n.failedCount AS failedCount,
       coalesce(n.consecutiveSuccesses, 0) AS consecutiveSuccesses,
       toString(n.lastRecovery) AS lastRecovery
"""

# The same, for a batch of nodes in one transaction (see HealthRecorder: checks that finish together are
# written together): one locked read and one update for the whole batch instead of two queries per node.
LOCK_AND_READ_BATCH = """
UNWIND $ids AS id
MATCH (n:Domain {domain: id})
SET n.urlHealth = n.urlHealth
RETURN id, n.urlHealth AS urlHealth, n.links AS links,
       coalesce(properties(n).incidentOpen, false) AS incidentOpen,
       coalesce(n.consecutiveFailures, 0) AS failuresBefore, n.lastError AS errorBefore
"""

UPDATE_NODES_HEALTH = """
UNWIND $rows AS r
MATCH (n:Domain {domain: r.id})
WITH n, r, n.status AS previous
SET n.pingCount = coalesce(n.pingCount, 0) + 1,
    n.lastPing = datetime(),
    n.status = r.status,
    n.failedCount = coalesce(n.failedCount, 0) + CASE WHEN r.up THEN 0 ELSE 1 END,
    n.consecutiveFailures = CASE WHEN r.up THEN 0 ELSE coalesce(n.consecutiveFailures, 0) + 1 END,
    n.consecutiveSuccesses = CASE WHEN r.up THEN coalesce(n.consecutiveSuccesses, 0) + 1 ELSE 0 END,
    n.lastLatencyMs = r.latency,
    n.lastError = CASE WHEN r.up THEN n.lastError ELSE r.error END,
    n.lastRecovery = CASE WHEN r.up AND previous = 'DOWN' THEN datetime() ELSE n.lastRecovery END,
    n.lastRttMs = r.rtt,
    n.lastJitterMs = r.jitter,
    n.lastPacketLoss = r.loss,
    n.hopCount = coalesce(r.hops, n.hopCount),
    n.lastSegmentAgeS = coalesce(r.lastSegmentAgeS, n.lastSegmentAgeS),
    n.targetDurationS = coalesce(r.targetDurationS, n.targetDurationS),
    n.streamFreshness = coalesce(r.streamFreshness, n.streamFreshness),
    n.mediaSequence = coalesce(r.mediaSequence, n.mediaSequence),
    n.discontinuities = coalesce(r.discontinuities, n.discontinuities),
    n.urlHealth = coalesce(r.urlHealth, n.urlHealth)
RETURN r.id AS id, previous,
       coalesce(n.consecutiveFailures, 0) AS consecutiveFailures,
       n.pingCount AS pingCount,
       n.failedCount AS failedCount,
       coalesce(n.consecutiveSuccesses, 0) AS consecutiveSuccesses,
       toString(n.lastRecovery) AS lastRecovery
"""
BATCH_WINDOW_S = 0.02  # checks finishing within this window share one write transaction

# Takes the node's write lock (the no-op SET) before reading, so the read-merge-write
# of per-URL health and incident logs in one transaction can't lose a concurrent update.
LOCK_AND_READ_URL_HEALTH = """
MATCH (n:Domain {domain: $id})
SET n.urlHealth = n.urlHealth
RETURN n.urlHealth AS urlHealth, n.links AS links,
       coalesce(properties(n).incidentOpen, false) AS incidentOpen,
       coalesce(n.consecutiveFailures, 0) AS failuresBefore, n.lastError AS errorBefore
"""

# Incidents open after this many failed checks in a row and close after this many healthy ones; shorter
# failures are counted as blips (n.blipCount / n.lastBlip) instead of outage/recovery log entries.
HEALTH_SWEEP = os.environ.get("HEALTH_SWEEP", "1") not in ("0", "false", "False")  # record every node each cycle
INCIDENT_AFTER_FAILURES = int(os.environ.get("INCIDENT_AFTER_FAILURES", "2"))
RECOVER_AFTER_SUCCESSES = int(os.environ.get("RECOVER_AFTER_SUCCESSES", "2"))

# Deduplication and adaptive polling settings
HEALTH_CACHE_ENABLED = os.environ.get("HEALTH_CACHE_ENABLED", "1") not in ("0", "false", "False")
# Adaptive checking: a server shared by several channels is reached by each channel's run (cloud carries 6, so
# its 7 streams were re-checked about every 5 s). How long a check is reused depends on how the server is doing:
# all streams fresh -> HEALTH_TTL_FRESH_S; a stream falling behind (WARNING / DEGRADED) -> HEALTH_TTL_WARNING_S;
# failing -> never reused (every run checks it, every 5 s in the outage tier).
HEALTH_TTL_FRESH_S = float(os.environ.get("HEALTH_TTL_FRESH_S", "15"))
HEALTH_TTL_WARNING_S = float(os.environ.get("HEALTH_TTL_WARNING_S", "5"))


def reuse_ttl(health: NodeHealth) -> float:
    """How long a server's last check may stand in for a new one (see HEALTH_TTL_FRESH_S)."""
    if not health.up:
        return 0.0
    if any(u.freshness in ("WARNING", "DEGRADED") for u in health.urls or [] if not u.ignored):
        return min(HEALTH_TTL_WARNING_S, HEALTH_TTL_FRESH_S)
    return HEALTH_TTL_FRESH_S
ADAPTIVE_POLLING = os.environ.get("ADAPTIVE_POLLING", "1") not in ("0", "false", "False")
INTERMEDIATE_CADENCE_S = float(os.environ.get("INTERMEDIATE_CADENCE_S", "60.0"))

_GLOBAL_NODE_CACHE: Dict[str, Tuple[float, NodeHealth]] = {}
_RECORDED: Dict[str, datetime] = {}  # node -> the time of the check last written to the history


def clear_spider_caches() -> None:
    """Clear shared cross-cycle node health caches."""
    _GLOBAL_NODE_CACHE.clear()
    _RECORDED.clear()

# Every node that carries a channel, with its latest stored per-URL health, for incident correlation.
CHANNEL_CHAIN = """
MATCH (n:Domain) WHERE n.links CONTAINS $needle
RETURN n.domain AS node, n.links AS links, n.urlHealth AS urlHealth, toString(n.lastPing) AS lastPing
"""


def _state(previous: Optional[str]) -> dict:
    try:
        state = json.loads(previous) if previous else {}
    except ValueError:
        return {}
    return state if isinstance(state, dict) else {}


def apply_sequence_stall(previous: Optional[str], checks: List[UrlCheck], now: datetime) -> List[UrlCheck]:
    """Marks a URL down when its playlist looks fine but its media sequence hasn't moved for longer than
    STALE_AFTER_TARGET_DURATIONS segments: the producer is stuck (catches it without PROGRAM-DATE-TIME too).
    Returns the URLs it marked."""
    state, stalled = _state(previous), []
    for check in checks:
        seen = (state.get(check.url) or {}).get("seq") or {}
        if not check.up or not check.sequences or not seen:
            continue
        limit = STALE_AFTER_TARGET_DURATIONS * (check.target_duration_s or 6)
        stuck = []
        for variant, number in check.sequences.items():
            old = seen.get(variant) or {}
            since = old.get("since")
            if old.get("n") != number or not since:
                break
            stuck.append((number, (now - datetime.fromisoformat(since)).total_seconds()))
        else:
            if stuck and all(age > limit for _, age in stuck):
                number, age = max(stuck, key=lambda x: x[1])
                check.up, check.category = False, "STALE_MEDIA"
                check.freshness = "STALE"
                check.detail = f"STALE_SEQUENCE (media sequence stuck at {number} for {int(age)}s)"
                stalled.append(check)
    return stalled


def apply_clock_offset(previous: Optional[str], checks: List[UrlCheck]) -> List[UrlCheck]:
    """Marks a URL up again when it only looks stale because its source clock runs behind: the segment
    timestamps say old, but every variant's media sequence moved on since the last check and the age did not
    grow (an encoder stamping its segments ~40 s late, seen on a live source). A frozen stream's sequence stays
    put and its age grows with every check, so it stays down (and apply_sequence_stall catches a stuck
    sequence without timestamps). Returns the URLs it marked up."""
    state, revived = _state(previous), []
    for check in checks:
        old = state.get(check.url) or {}
        seen = old.get("seq") or {}
        if check.up or check.category != "STALE_MEDIA" or "STALE_SEGMENTS" not in check.detail:
            continue
        if not check.sequences or not seen:
            continue
        target = check.target_duration_s or 6.0
        moved = all((seen.get(v) or {}).get("n") is not None and n > seen[v]["n"] for v, n in check.sequences.items())
        age, before = check.segment_age_s, old.get("segmentAgeS")
        steady = age is not None and before is not None and age - before <= 2 * target
        if moved and steady and age <= MAX_CLOCK_SKEW_S:
            check.up, check.category, check.freshness = True, None, "FRESH"
            check.detail = (f"live; media sequence advancing, segment timestamps {int(age)}s behind "
                            f"(the source clock runs late)")
            revived.append(check)
        elif moved and age is not None and before is not None:
            # Still producing, but slower than real time: viewers drift further behind live. Down, but say so.
            check.detail = (f"FALLING_BEHIND (advancing, but {int(age)}s behind live, "
                            f"{int(age - before)}s more than at the last check)")
    return revived


def merge_url_health(previous: Optional[str], checks: List[UrlCheck], now: str) -> Optional[str]:
    """Per-URL health as a JSON object: {up, detail, lastDown, category, freshness, segmentAgeS, targetS,
    seq: {variant: {n, since}}}. lastDown is when the URL last went UP -> DOWN; `since` is when that media
    sequence number was first seen. Only the URLs checked now are kept: a URL removed from the server (a link moved
    or deleted) would otherwise stay in its last state forever, a ghost outage nobody checks any more."""
    if not checks:
        return None
    checked = {c.url for c in checks}
    state = {url: h for url, h in _state(previous).items() if url in checked}
    for check in checks:
        old = state.get(check.url, {})
        went_down = not check.up and old.get("up", True)
        old_seq = old.get("seq") or {}
        seq = {v: {"n": n, "since": old_seq[v]["since"] if (old_seq.get(v) or {}).get("n") == n else now}
               for v, n in check.sequences.items()}
        extra = {"category": check.category, "freshness": check.freshness, "segmentAgeS": check.segment_age_s,
                 "targetS": check.target_duration_s, "seq": seq,
                 "bitrates": check.bitrates, "resolutions": check.resolutions,
                 "discontinuities": check.discontinuities, "cdn_cache": check.cdn_cache,
                 "server_hdr": check.server_hdr,
                 **onset_fields(old, check, now, old_seq)}
        state[check.url] = {
            "up": check.up,
            "detail": check.detail,
            "lastDown": now if went_down else old.get("lastDown"),
            **({"ignored": True} if check.ignored else {}),  # the dashboard shows it, but doesn't count it
            **{k: v for k, v in extra.items() if v not in (None, {}, [])},  # only what this check measured
        }
    return json.dumps(state, sort_keys=True)


AGE_BASELINE_WEIGHT = 0.1  # EWMA weight of each healthy check in a URL's usual segment age
MAX_CLOCK_SKEW_S = 300  # a source clock further ahead than this is a timestamp glitch, not skew


def onset_fields(old: dict, check: UrlCheck, now: str, old_seq: dict) -> dict:
    """When a URL started failing, as precisely as the evidence allows.

    - Stale media with timestamps: the newest segment's age minus this URL's usual age while healthy (which
      absorbs the source clock's skew): the stream stopped `now - (age - usual)`, within about one segment.
    - Stuck media sequence: when the stuck sequence number was first seen.
    - Anything else (404, unreachable, 5xx): between the last good check and this one (the midpoint).
    Kept for the whole outage; cleared on recovery. While healthy, tracks lastOkAt and the usual age."""
    now_dt = datetime.fromisoformat(now)
    if check.up:
        fields = {"lastOkAt": now}
        base = old.get("ageBaseline")
        age, target = check.segment_age_s, check.target_duration_s or 6.0
        # Ignore implausible ages: some encoders' timestamps jump by hours for a moment (seen on a live source),
        # and one such value would drag the usual age, and every later onset estimate, far off.
        if age is not None and -MAX_CLOCK_SKEW_S <= age <= STALE_AFTER_TARGET_DURATIONS * target:
            base = age if base is None else (1 - AGE_BASELINE_WEIGHT) * base + AGE_BASELINE_WEIGHT * age
        if base is not None:
            fields["ageBaseline"] = round(base, 2)
        return fields
    fields = {k: old[k] for k in ("lastOkAt", "ageBaseline") if old.get(k) is not None}
    if old.get("onsetAt") and old.get("up") is False:
        return {**fields, **{k: old[k] for k in ("failingSince", "onsetAt", "onsetPrecisionS", "onsetMethod") if k in old}}
    target = check.target_duration_s or 6.0
    fields["failingSince"] = now
    if check.category == "STALE_MEDIA" and check.detail.startswith("STALE_SEQUENCE"):
        since = min((v.get("since") for v in old_seq.values() if v.get("since")), default=None)
        onset, precision, method = (datetime.fromisoformat(since) if since else now_dt), target, "sequence"
    elif check.category == "STALE_MEDIA" and check.segment_age_s is not None:
        usual = old.get("ageBaseline", target / 2)
        onset = now_dt - timedelta(seconds=max(0.0, check.segment_age_s - usual))
        precision, method = target, "segment timestamps"
    else:
        last_ok = datetime.fromisoformat(old["lastOkAt"]) if old.get("lastOkAt") else now_dt
        window = max(0.0, (now_dt - last_ok).total_seconds())
        onset, precision, method = now_dt - timedelta(seconds=window / 2), window / 2, "between checks"
    fields.update(onsetAt=onset.isoformat(timespec="seconds"), onsetPrecisionS=round(precision, 1), onsetMethod=method)
    return fields


ROLE_WORDS = {"MainInput": "Main", "BackupLink": "Backup", "Transcoding": "Transcoding", "FinalLink": "Final"}


def correlate(chain_rows: List[dict], node_id: str, failing: List[UrlCheck], now: str) -> List[dict]:
    """For each channel a failing URL belongs to: every link in that channel's chain (this node from the check
    just made, the others from their latest stored health) and a verdict on where the fault most likely is."""
    failing_urls = {c.url: c for c in failing}
    results = []
    links_here = [l for r in chain_rows if r["node"] == node_id for l in load_json_list(r["links"])]
    for channel in sorted({l["channel"] for l in links_here if l["url"] in failing_urls}):
        chain = []
        for row in chain_rows:
            health = _state(row["urlHealth"])
            for link in load_json_list(row["links"]):
                if link.get("channel") != channel:
                    continue
                if link["url"] in failing_urls:
                    c = failing_urls[link["url"]]
                    entry = {"up": False, "detail": c.detail, "freshness": c.freshness, "segmentAgeS": c.segment_age_s,
                             "checkedAt": now}
                else:
                    h = health.get(link["url"], {})
                    if h.get("ignored"):
                        continue  # the operator set this stream aside: it says nothing about where the fault is
                    entry = {"up": h.get("up", True) is not False, "detail": h.get("detail"),
                             "freshness": h.get("freshness"), "segmentAgeS": h.get("segmentAgeS"),
                             "checkedAt": row["lastPing"]}
                chain.append({"role": link["role"], "node": row["node"], "url": link["url"], **entry})
        results.append({"channel": channel, **verdict(channel, node_id, chain), "chain": chain})
    return results


def verdict(channel: str, node_id: str, chain: List[dict]) -> dict:
    by_role = {r: [e for e in chain if e["role"] == r] for r in ROLE_WORDS}
    mine = [e for e in chain if e["node"] == node_id and not e["up"]]
    role = mine[0]["role"] if mine else None
    inputs = by_role["MainInput"] + by_role["BackupLink"]
    others = [e for e in inputs if e["node"] != node_id]
    finals_up = all(e["up"] for e in by_role["FinalLink"]) if by_role["FinalLink"] else None
    impact = "viewers affected: the channel's output is down" if finals_up is False else "the output is still up"

    def names(entries):
        return ", ".join(f"{e['node']} ({ROLE_WORDS[e['role']]})" for e in entries) or "none"

    if role in ("MainInput", "BackupLink"):
        if others and not any(e["up"] for e in others):
            return {"verdict": "SHARED_UPSTREAM",
                    "summary": f"Every input of {channel} is failing ({names(inputs)}): look at the common source "
                               f"that feeds them; {impact}."}
        if others:
            return {"verdict": "LOCAL_TO_NODE",
                    "summary": f"Only {node_id}'s input is failing; {channel} is fed from {names([e for e in others if e['up']])}. "
                               f"Look at {node_id}'s HLS producer or the feed into it; {impact}."}
        return {"verdict": "SINGLE_INPUT",
                "summary": f"{node_id} is {channel}'s only input, so nothing to compare with; {impact}."}
    if role == "Transcoding":
        if inputs and not any(e["up"] for e in inputs):
            return {"verdict": "UPSTREAM", "summary": f"The transcoder's inputs are failing too ({names(inputs)}): "
                                                      f"the cause is upstream; {impact}."}
        return {"verdict": "TRANSCODER", "summary": f"Inputs are healthy ({names([e for e in inputs if e['up']])}) but "
                                                    f"the transcoder {node_id} is failing; {impact}."}
    if role == "FinalLink":
        upstream = by_role["Transcoding"] or inputs
        if upstream and not any(e["up"] for e in upstream):
            return {"verdict": "UPSTREAM", "summary": f"Upstream is failing too ({names(upstream)}): the final output "
                                                      f"just follows it."}
        return {"verdict": "FINAL_ORIGIN", "summary": f"Everything upstream is healthy; the final origin/packager "
                                                      f"{node_id} is the problem. Viewers are affected."}
    return {"verdict": "UNKNOWN", "summary": "Not enough chain data to correlate."}


def incident_category(checks: List[UrlCheck]) -> Optional[str]:
    failed = [c.category or "UNKNOWN" for c in failing(checks)]
    return max(set(failed), key=failed.count) if failed else None


class NodeTransition(BaseModel):
    node_id: str
    previous: str
    current: str


class HealthRecorder:
    """Checks each node at most once per cycle (spiders share results).

    Network probes can be started early (`prefetch`) so they run concurrently, but a
    node's health is only written when a spider first reaches it; nodes beyond a
    spider's stop point are never recorded.
    """

    def __init__(self, driver, topology: Topology, checker: Checker, alert_threshold: Optional[int] = None,
                 on_node_checked: Optional["NodeHook"] = None, metrics=None):
        self.driver = driver
        self.topology = topology
        self.checker = checker
        self.alert_threshold = alert_threshold if alert_threshold is not None else ALERT_CONSECUTIVE_THRESHOLD
        self.probes: Dict[str, asyncio.Future] = {}
        self.tasks: Dict[str, asyncio.Future] = {}
        self.owner: Dict[str, Optional[str]] = {}  # node -> spider that pinged it this cycle
        self.transitions: List[NodeTransition] = []
        self.incidents: List[dict] = []  # incident log entries written this cycle (opened, escalated, resolved)
        self.metrics = metrics if metrics is not None else store()  # time series + incident log (SQLite)
        self._pending: List[tuple] = []  # (node_id, health, now, future) waiting for the next batched write
        self._flusher: Optional[asyncio.Future] = None
        self.batches: List[int] = []  # size of each batched write this cycle
        self.on_node_checked = on_node_checked

    def _probe(self, node_id: str, max_age_s: Optional[float] = None) -> "asyncio.Future[NodeHealth]":
        if node_id not in self.probes:
            if HEALTH_CACHE_ENABLED and node_id in _GLOBAL_NODE_CACHE:
                cached_ts, cached_health = _GLOBAL_NODE_CACHE[node_id]
                ttl = max_age_s if max_age_s is not None else reuse_ttl(cached_health)
                # Reuse if within TTL and node was healthy (if failed, always re-probe fresh)
                if time.monotonic() - cached_ts < ttl and cached_health.up:
                    loop = asyncio.get_running_loop()
                    f = loop.create_future()
                    f.set_result(cached_health)
                    self.probes[node_id] = f
                    return f

            self.probes[node_id] = asyncio.ensure_future(self.checker(self.topology.node(node_id).model_dump()))
        return self.probes[node_id]

    def prefetch(self, node_ids: List[str]) -> None:
        for node_id in node_ids:
            self._probe(node_id)

    async def sweep(self) -> None:
        """Record every prefetched node the spiders didn't reach, so each node is checked every cycle and failure
        onsets are comparable across the graph (the spiders' walk and RCA are unchanged)."""
        rest = [n for n in self.probes if n not in self.tasks]
        await asyncio.gather(*(self.check(n) for n in rest), return_exceptions=True)

    def discard_unused(self) -> None:
        """Cancel probes no spider reached, and consume their errors."""
        for node_id, probe in self.probes.items():
            if node_id not in self.tasks:
                probe.cancel()
                probe.add_done_callback(lambda f: f.cancelled() or f.exception())

    def check(self, node_id: str, spider_id: Optional[str] = None, max_age_s: Optional[float] = None) -> "asyncio.Future[NodeHealth]":
        """The first spider to reach a node owns its ping this cycle; later spiders reuse the result."""
        if node_id not in self.tasks:
            self.owner[node_id] = spider_id
            self.tasks[node_id] = asyncio.ensure_future(self._check_and_record(node_id, max_age_s=max_age_s))
        return self.tasks[node_id]

    async def _check_and_record(self, node_id: str, max_age_s: Optional[float] = None) -> NodeHealth:
        health = await self._probe(node_id, max_age_s=max_age_s)
        now_dt = health.checked_at or datetime.now(timezone.utc)  # a reused check counts at the time it was made
        record, status, entry = await self._write(node_id, health, now_dt)
        if entry:  # after the commit: a retried transaction must not log twice
            self.metrics.add_incident(node_id, entry)
        health.consecutive_failures = (
            int(record["consecutiveFailures"])
            if record and record.get("consecutiveFailures") is not None
            else (0 if health.up else 1)
        )
        if record and record.get("previous") is not None and record["previous"] != status:
            self.transitions.append(NodeTransition(node_id=node_id, previous=record["previous"], current=status))
        if entry:
            self.incidents.append({"node": node_id, **entry})
        if health.up:
            _GLOBAL_NODE_CACHE[node_id] = (time.monotonic(), health)
        else:
            _GLOBAL_NODE_CACHE.pop(node_id, None)
        if _RECORDED.get(node_id) != now_dt:  # a reused check is already in the history: not a second check
            _RECORDED[node_id] = now_dt
            try:
                await asyncio.get_running_loop().run_in_executor(None, self.metrics.record, node_id, health, now_dt.timestamp())
            except Exception:
                pass  # history must never break the health cycle
        if self.on_node_checked:  # e.g. the traceroute escalation; it hands work to its own threads
            try:
                self.on_node_checked(self.topology.node(node_id).model_dump(), health.up, health.consecutive_failures)
            except Exception:
                pass  # diagnostics must never break the health cycle
        return health

    async def _write(self, node_id: str, health: NodeHealth, now_dt: datetime):
        """Queues the node's result for the next batched write and waits for it: (record, status, entry)."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending.append((node_id, health, now_dt, future))
        if self._flusher is None or self._flusher.done():
            self._flusher = asyncio.ensure_future(self._flush())
        return await future

    async def _flush(self) -> None:
        """Writes queued results in batches, one transaction at a time (so health writes never deadlock
        each other)."""
        loop = asyncio.get_running_loop()
        while self._pending:
            await asyncio.sleep(BATCH_WINDOW_S)
            batch, self._pending = self._pending, []
            try:
                results = await loop.run_in_executor(None, self._write_batch, batch)
            except Exception as e:
                for *_, future in batch:
                    if not future.done():
                        future.set_exception(e)
                continue
            for (*_, future), result in zip(batch, results):
                if not future.done():
                    future.set_result(result)
            self.batches.append(len(batch))

    def _write_batch(self, batch) -> List[tuple]:
        def work(tx):
            rows = {r["id"]: r for r in tx.run(LOCK_AND_READ_BATCH, ids=[b[0] for b in batch])}
            updates, statuses = [], {}
            for node_id, health, now_dt, _ in batch:
                previous_urls = rows[node_id]["urlHealth"] if node_id in rows else None
                # Timestamps that only look old because the source clock runs late, while the stream moves on.
                if apply_clock_offset(previous_urls, health.urls):
                    health.error = failure_text(health.urls)
                    health.up = health.error is None
                # A playlist that answers but whose media sequence stopped moving is down too (unless ignored).
                if apply_sequence_stall(previous_urls, health.urls, now_dt) and health.up and failing(health.urls):
                    health.up = False
                    health.error = failure_text(health.urls)
                statuses[node_id] = "UP" if health.up else "DOWN"
                ages = [c.segment_age_s for c in health.urls if getattr(c, "segment_age_s", None) is not None]
                min_age = min(ages) if ages else None
                targets = [c.target_duration_s for c in health.urls if getattr(c, "target_duration_s", None) is not None]
                target_dur = targets[0] if targets else None
                freshness = next((c.freshness for c in health.urls if getattr(c, "freshness", None)), None)
                seqs = [s for c in health.urls if getattr(c, "sequences", None) for s in c.sequences.values() if s is not None]
                media_seq = max(seqs) if seqs else None
                discont = sum(c.discontinuities for c in health.urls if getattr(c, "discontinuities", None))

                updates.append({
                    "id": node_id, "status": statuses[node_id], "up": health.up, "latency": health.latency_ms,
                    "error": health.error, "rtt": health.rtt_ms, "jitter": health.jitter_ms,
                    "loss": health.packet_loss, "hops": health.hop_count,
                    "lastSegmentAgeS": min_age, "targetDurationS": target_dur,
                    "streamFreshness": freshness, "mediaSequence": media_seq,
                    "discontinuities": discont,
                    "urlHealth": merge_url_health(previous_urls, health.urls, now_dt.isoformat(timespec="seconds")),
                })
                try:
                    from telemetry_pool import telemetry_pool
                    telemetry_pool.update_node_metric(
                        node_id,
                        status=statuses[node_id],
                        lastLatencyMs=health.latency_ms,
                        lastRttMs=health.rtt_ms,
                        lastPacketLoss=health.packet_loss,
                        lastSegmentAgeS=min_age,
                        streamFreshness=freshness,
                        mediaSequence=media_seq,
                    )
                except Exception:
                    pass
            written = {r["id"]: r for r in tx.run(UPDATE_NODES_HEALTH, rows=updates)}
            out = []
            for node_id, health, now_dt, _ in batch:
                res = written.get(node_id)
                entry = (self._incident_entry(tx, node_id, health, res, rows[node_id], now_dt.isoformat(timespec="seconds"))
                         if res is not None and node_id in rows else None)
                out.append((res, statuses[node_id], entry))
            return out

        with self.driver.session() as session:
            return session.execute_write(work)

    def _incident_entry(self, tx, node_id: str, health: NodeHealth, res, state_row, now: str) -> Optional[dict]:
        """Opens an incident after INCIDENT_AFTER_FAILURES failed checks in a row (with its type and the channel
        chain correlated), escalates it at the alert threshold, and closes it after RECOVER_AFTER_SUCCESSES
        healthy checks. Shorter failures count as blips. Returns the log entry written, if any."""
        fails, oks = int(res["consecutiveFailures"]), int(res["consecutiveSuccesses"])
        is_open = bool(state_row["incidentOpen"])
        base = {
            "timestamp": now, "pingCount": res["pingCount"], "failedCount": res["failedCount"],
            "consecutiveFailures": fails, "consecutiveSuccesses": oks, "lastPing": now,
            "lastRecovery": res["lastRecovery"], "lastError": health.error, "lastLatencyMs": health.latency_ms,
            "lastRttMs": health.rtt_ms, "lastJitterMs": health.jitter_ms, "lastPacketLoss": health.packet_loss,
            "hopCount": health.hop_count,
        }
        entry = None
        if not health.up and fails >= INCIDENT_AFTER_FAILURES and not is_open:
            failed = failing(health.urls)
            roles = {l["url"]: l.get("role") for l in load_json_list(state_row.get("links"))}
            needles = {f'"channel": "{l["channel"]}"' for l in load_json_list(state_row.get("links"))
                       if l["url"] in {c.url for c in failed}}
            chain_rows = [dict(r) for needle in needles for r in tx.run(CHANNEL_CHAIN, needle=needle)]
            unique = list({r["node"]: r for r in chain_rows}.values())
            entry = {"type": "OUTAGE", "from": "UP", "to": "DOWN", **base,
                     "category": incident_category(health.urls),
                     "failedUrls": [{"url": c.url, "role": roles.get(c.url), "category": c.category, "detail": c.detail,
                                     "freshness": c.freshness, "segmentAgeS": c.segment_age_s} for c in failed],
                     # only backup feeds down: a standby problem, not an outage (outage_class.py)
                     "class": BACKUP_FAILURE if backup_only([{"url": c.url, "role": roles.get(c.url)} for c in failed])
                              else OUTAGE,
                     "correlation": correlate(unique, node_id, failed, now)}
            tx.run("MATCH (n:Domain {domain: $id}) SET n.incidentOpen = true", id=node_id)
        elif not health.up and fails == self.alert_threshold and self.alert_threshold > INCIDENT_AFTER_FAILURES:
            roles = {l["url"]: l.get("role") for l in load_json_list(state_row.get("links"))}
            entry = {"type": "ESCALATED", "from": "DOWN", "to": "DOWN", **base, "category": incident_category(health.urls),
                     "class": BACKUP_FAILURE if backup_only([{"url": c.url, "role": roles.get(c.url)}
                                                             for c in failing(health.urls)]) else OUTAGE}
        elif health.up and is_open and oks >= RECOVER_AFTER_SUCCESSES:
            opened = self.metrics.last_incident(node_id, "OUTAGE")
            duration = None
            if opened and opened.get("timestamp"):
                duration = int((datetime.fromisoformat(now) - datetime.fromisoformat(opened["timestamp"])).total_seconds())
            entry = {"type": "RECOVERY", "from": "DOWN", "to": "UP", **base,
                     "category": (opened or {}).get("category"), "durationS": duration,
                     "class": classify(opened or {}, duration)}  # a BLIP when back within a minute
            tx.run("MATCH (n:Domain {domain: $id}) SET n.incidentOpen = false", id=node_id)
        elif health.up and oks == 1 and res["previous"] == "DOWN" and not is_open:
            # Failed fewer times than an incident needs, then recovered: a blip, not an outage.
            tx.run("MATCH (n:Domain {domain: $id}) SET n.blipCount = coalesce(n.blipCount, 0) + 1, n.lastBlip = $blip",
                   id=node_id, blip=json.dumps({"at": now, "failures": int(state_row["failuresBefore"]),
                                                "error": state_row["errorBefore"]}))
        return entry  # the caller stores it in the incident log (SQLite) once the transaction commits


# --- Spider position updater -------------------------------------------------------

# Where a spider ended up after its walk, and how many hops it took: one write per spider per cycle (the page
# animates the walk from the live events, so the hops themselves need no writes). The old AT is replaced only
# when the position changed, so a spider never has two.
SAVE_POSITION = """
MATCH (s:SpiderRun {id: $spider}), (next:Domain {domain: $node})
SET s.lastStepAt = datetime(), s.stepCount = coalesce(s.stepCount, 0) + $steps
WITH s, next
OPTIONAL MATCH (s)-[old:AT]->(prev)
WITH s, next, old, prev
WHERE prev IS NULL OR prev <> next
DELETE old
WITH DISTINCT s, next
CREATE (s)-[:AT]->(next)
SET s.currentNodeId = next.domain
"""

ENSURE_SPIDERS = """
UNWIND $finals AS final
MERGE (s:SpiderRun {finalLinkId: final})
ON CREATE SET s.id = 'spider-' + final, s.status = 'RUNNING', s.startedAt = datetime(), s.stepCount = 0, s.direction = 'UPSTREAM'
WITH s, final
MATCH (f:Domain:FinalLink {domain: final})
OPTIONAL MATCH (s)-[existing:AT]->()
WITH s, f, final, count(existing) AS atCount
// A new (or detached) spider starts AT its FinalLink; one that already has an AT keeps it.
FOREACH (_ IN CASE WHEN atCount = 0 THEN [1] ELSE [] END |
    CREATE (s)-[:AT]->(f)
    SET s.currentNodeId = f.domain, s.lastStepAt = datetime(), s.direction = 'UPSTREAM'
)
RETURN final, s.id AS id, s.status AS status, s.stopNodeId AS stopNodeId, s.currentNodeId AS currentNodeId, s.rca AS rca, s.direction AS direction
"""

SET_SPIDER_STATE = """
MATCH (s:SpiderRun {id: $spider})
SET s.status = $status, s.stopNodeId = $stopNodeId, s.stopReason = $stopReason, s.rca = $rca, s.direction = $direction
"""

REMOVE_ORPHAN_SPIDERS = """
MATCH (s:SpiderRun)
WHERE NOT EXISTS { MATCH (:Domain:FinalLink {domain: s.finalLinkId}) }
DETACH DELETE s
RETURN count(*) AS removed
"""


# --- RCA ---------------------------------------------------------------------------


class Step(BaseModel):
    node_id: str
    role: str
    up: bool
    error: Optional[str] = None
    visited: bool  # False = health-checked from a downstream node without the spider moving


class Rca(BaseModel):
    root_cause: Optional[str] = None  # node id, or "Input dependency failure"
    root_kind: Optional[str] = None  # "NODE" | "INPUTS"
    failed_nodes: List[str] = []
    reason: Optional[str] = None
    impacted: List[str] = []  # downstream chain from the root to the FinalLink
    outage: bool = False
    impact: str = "No faults found."
    failover: List[str] = []
    path: List[Step] = []
    consecutive_failures: int = 0
    alerted: bool = False
    traceroute: Optional[Dict[str, Any]] = None  # latest traceroute summary of the stop node, if any


class Alert(BaseModel):
    kind: str  # SPIDER_STOPPED | SPIDER_RECOVERED | SPIDER_ERROR | FAILOVER_ACTIVE
    spider_id: str
    message: str


class SpiderReport(BaseModel):
    spider_id: str
    final_link_id: str
    status: str
    location: Optional[str]
    rca: Rca
    alerts: List[Alert] = []
    direction: Optional[str] = None
    leg: Optional[str] = None


class Root(Exception):
    """Raised inside the walk to stop the spider at a root cause."""

    def __init__(self, kind: str, node_id: str, failed: List[str], reason: str, chain: List[str]):
        super().__init__(reason)
        self.kind, self.node_id, self.failed, self.reason, self.chain = kind, node_id, failed, reason, chain


class EventLog:
    """Timeline of what the spiders actually did in a cycle, for replaying it in the UI.

    `listener`, if given, is called with each event as it happens (live streaming).
    """

    def __init__(self, listener: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.start = time.monotonic()
        self.events: List[Dict[str, Any]] = []
        self.listener = listener

    def emit(self, spider: str, kind: str, node: Optional[str] = None, **data) -> None:
        t = int((time.monotonic() - self.start) * 1000)
        event = {"t": t, "spider": spider, "kind": kind, "node": node, **data}
        self.events.append(event)
        if self.listener:
            self.listener(event)


class Spider:
    def __init__(self, spider_id: str, final_id: str, driver, topology: Topology, recorder: HealthRecorder,
                 log: Optional[EventLog] = None, alert_threshold: Optional[int] = None):
        self.id = spider_id
        self.log = log or EventLog()
        self.final_id = final_id
        self.driver = driver
        self.topology = topology
        self.recorder = recorder
        self.location: Optional[str] = None
        self.steps = 0  # hops taken this run (saved with the final position)
        self.path: List[Step] = []
        self.failover: List[str] = []
        # The channel this spider follows upstream (per-channel FinalLink); None = whole node.
        self.channel = topology.node(final_id).final_channel if final_id in topology.nodes else None
        self.alert_threshold = alert_threshold if alert_threshold is not None else ALERT_CONSECUTIVE_THRESHOLD

    async def _save_position(self) -> None:
        if self.location:
            await run_query(self.driver, SAVE_POSITION, spider=self.id, node=self.location, steps=self.steps)
        self.steps = 0

    async def _move(self, node_id: str, announce: bool = True) -> None:
        if node_id != self.location:
            if announce:
                self.log.emit(self.id, "move", node_id, source=self.location)
            self.location = node_id
            self.steps += 1  # written once, when the run ends (see _save_position)

    async def _check(self, node_id: str, visited: bool, max_age_s: Optional[float] = None) -> NodeHealth:
        prior = next((s for s in self.path if s.node_id == node_id), None)
        if prior:
            # Already checked by this spider this cycle (e.g. a transcoder that is also its own
            # backup input): reuse the result, no second ping. If it was only checked from
            # downstream and the spider now needs to stand on it, walk there.
            health = await self.recorder.check(node_id, self.id, max_age_s=max_age_s)
            if visited and not prior.visited:
                await self._move(node_id)
                prior.visited = True
            return health
        if visited and node_id != self.location:
            self.log.emit(self.id, "move", node_id, source=self.location)  # the move write itself overlaps the check
        pending = self.recorder.check(node_id, self.id, max_age_s=max_age_s)
        # When several spiders reach the same node, only the first pings it; the rest reuse its result.
        owner = self.recorder.owner.get(node_id)
        shared_from = owner if owner and owner != self.id else None
        self.log.emit(self.id, "check", node_id, peek=not visited, shared_from=shared_from)
        if visited:
            # Moving onto the node and recording its check are independent writes; overlap them.
            _, health = await asyncio.gather(self._move(node_id, announce=False), pending)
        else:
            health = await pending
        self.log.emit(self.id, "result", node_id, peek=not visited, up=health.up,
                      latency=health.latency_ms, error=health.error, shared_from=shared_from)
        if not any(s.node_id == node_id and (s.visited or not visited) for s in self.path):
            node = self.topology.node(node_id)
            self.path = [s for s in self.path if s.node_id != node_id]  # a visit supersedes a peek
            self.path.append(Step(node_id=node_id, role=node.role, up=health.up, error=health.error, visited=visited))
        return health

    async def visit(self, node_id: str, max_age_s: Optional[float] = None) -> NodeHealth:
        """Move onto the node, then check it."""
        return await self._check(node_id, visited=True, max_age_s=max_age_s)

    async def _glance(self, node_id: str) -> NodeHealth:
        """The node's health for a decision only (adaptive polling): not a step of the walk, no event, no move.
        It shares the cycle's probe, so the walk's own check of this node later costs no second ping."""
        return await self.recorder._probe(node_id)

    async def peek(self, node_id: str, max_age_s: Optional[float] = None) -> NodeHealth:
        """Check an upstream dependency of a failed node without moving onto it."""
        return await self._check(node_id, visited=False, max_age_s=max_age_s)

    async def inputs_ok(self, unit: Unit, owner: str, move: bool, max_age_s: Optional[float] = None) -> bool:
        """OR dependency: MainInput first, then BackupLink; one healthy member keeps `owner` fed."""
        members = sorted(unit.ids, key=lambda i: (not self._is_main(i), i == owner, i))
        results = {}
        for member in members:
            already_have_healthy = any(results[m].up for m in results)
            should_move = move and member != owner and not already_have_healthy
            results[member] = await (self.visit(member, max_age_s=max_age_s) if should_move else self.peek(member, max_age_s=max_age_s))
        up = [m for m in members if results[m].up]
        down = [m for m in members if not results[m].up]
        main_down = [m for m in down if self._is_main(m)]
        if up and main_down:
            self.failover.append(
                f"{owner}: MainInput {', '.join(main_down)} DOWN, "
                f"{', '.join(up)} UP - Failover AVAILABLE"
            )
        return bool(up)

    def _is_main(self, node_id: str) -> bool:
        """Main input for this spider's channel (a domain can be Main for one channel, Backup for another)."""
        node = self.topology.node(node_id)
        if self.channel and node.links:
            return node.carries(self.channel, ("MainInput",))
        return node.has("MainInput")

    async def inspect(self, node_id: str, health: NodeHealth, chain: List[str]) -> None:
        """Walk upstream from a checked node; raises Root at the first failed dependency."""
        node = self.topology.node(node_id)
        chain = chain + [node_id]
        units = self.topology.units(node, self.channel)

        if not health.up:
            # The node is down: it's the root cause unless one of its dependencies is down too.
            for unit in units:
                if unit.kind == "TRANSCODER":
                    t = unit.ids[0]
                    t_health = await self.peek(t)
                    if not t_health.up:
                        await self.visit(t)
                        await self.inspect(t, t_health, chain)
                elif not await self.inputs_ok(unit, node_id, move=False):
                    self._raise_input_failure(unit, node_id, chain)
            raise Root("NODE", node_id, [node_id], health.error or "DOWN", chain)

        # Adaptive intermediate cadence:
        # If node is FinalLink and healthy, peek the primary MainInput. If MainInput is ALSO healthy,
        # intermediate transcoders and backups use INTERMEDIATE_CADENCE_S (heartbeat).
        # If either FinalLink or MainInput fails, adaptive cadence is None (immediate fresh probe).
        adaptive_cadence = None
        if ADAPTIVE_POLLING and node_id == self.final_id:
            all_inputs = self.topology.inputs(self.final_id)
            for t_id in self.topology.upstream_transcoders(self.final_id):
                all_inputs.extend(self.topology.inputs(t_id))
            primary_main = next((i for i in all_inputs if self._is_main(i)), all_inputs[0] if all_inputs else None)
            if primary_main:
                main_h = await self._glance(primary_main)
                if main_h.up:
                    adaptive_cadence = INTERMEDIATE_CADENCE_S

        for unit in units:
            if unit.kind == "TRANSCODER":
                t = unit.ids[0]
                t_h = await self.visit(t, max_age_s=adaptive_cadence)
                await self.inspect(t, t_h, chain)
            elif not await self.inputs_ok(unit, node_id, move=True, max_age_s=adaptive_cadence):
                self._raise_input_failure(unit, node_id, chain)

    def _raise_input_failure(self, unit: Unit, owner: str, chain: List[str]) -> None:
        failed = [i for i in unit.ids if i != owner] or unit.ids
        primary = next((i for i in failed if self._is_main(i)), failed[0])
        reasons = "; ".join(f"{i}: {self.recorder.tasks[i].result().error}" for i in failed)
        raise Root("INPUTS", primary, failed, f"All inputs of {owner} DOWN - {reasons}", chain)

    async def walk_upstream(self) -> None:
        """First leg: travel from FinalLink to Main/Backup, pinging each node in between; stops on Main/Backup."""
        await self.inspect(self.final_id, await self.visit(self.final_id), [])

    async def walk_downstream(self) -> None:
        """Second leg: travel from Main/Backup back to FinalLink, pinging each node in between; stops on FinalLink."""
        start_node = self.location
        if not start_node or start_node == self.final_id:
            # Fallback if spider was at final_id
            all_inputs = self.topology.inputs(self.final_id)
            for t in self.topology.upstream_transcoders(self.final_id):
                all_inputs.extend(self.topology.inputs(t))
            start_node = next((i for i in all_inputs if self._is_main(i)), all_inputs[0] if all_inputs else self.final_id)

        # 1. Ping the starting input node
        start_health = await self.visit(start_node)
        if not start_health.up:
            # Check alternative inputs for the same parent unit
            trans = self.topology.upstream_transcoders(self.final_id)
            parent = trans[0] if trans else self.final_id
            alt_node = None
            inputs_unit = None
            for u in self.topology.units(self.topology.node(parent), self.channel):
                if u.kind == "INPUTS":
                    inputs_unit = u
                    for member in u.ids:
                        if member == start_node:
                            continue
                        m_health = await self.visit(member)
                        if m_health.up and not alt_node:
                            alt_node = member
                            self.failover.append(
                                f"{parent}: MainInput {start_node} DOWN, {member} UP - Failover AVAILABLE"
                            )
            if alt_node:
                start_node = alt_node
            else:
                chain = [self.final_id, parent] if parent != self.final_id else [self.final_id]
                if inputs_unit:
                    self._raise_input_failure(inputs_unit, parent, chain)
                else:
                    raise Root("INPUTS", start_node, [start_node], start_health.error or "DOWN", chain)

        # 2. Intermediate transcoders (between start_node and final_id)
        transcoders = [
            t for t in self.topology.upstream_transcoders(self.final_id)
            if any(e["source"] == start_node for e in self.topology.upstream.get(t, []))
        ] or self.topology.upstream_transcoders(self.final_id)

        downstream_cadence = None
        if ADAPTIVE_POLLING and start_health.up:
            final_peek = await self._glance(self.final_id)
            if final_peek.up:
                downstream_cadence = INTERMEDIATE_CADENCE_S

        for t in transcoders:
            t_health = await self.visit(t, max_age_s=downstream_cadence)
            if not t_health.up:
                raise Root("NODE", t, [t], t_health.error or "DOWN", [self.final_id, t])

        # 3. Final link node
        final_health = await self.visit(self.final_id)
        if not final_health.up:
            raise Root("NODE", self.final_id, [self.final_id], final_health.error or "DOWN", [self.final_id])

    async def walk(self, is_downstream: bool = False) -> Optional[Root]:
        try:
            if is_downstream:
                await self.walk_downstream()
            else:
                await self.walk_upstream()
            return None
        except Root as root:
            await self._move(root.node_id)
            return root

    def build_rca(self, root: Optional[Root]) -> Rca:
        final_up = next((s.up for s in self.path if s.node_id == self.final_id), False)
        rca = Rca(path=self.path, failover=self.failover, outage=not final_up)
        if root is None:
            rca.impact = (
                "No current service outage: failover input in use." if self.failover else "No faults found."
            )
            return rca
        rca.root_kind = root.kind
        rca.root_cause = "Input dependency failure" if root.kind == "INPUTS" else root.node_id
        rca.failed_nodes = root.failed
        rca.reason = root.reason
        rca.impacted = list(reversed(root.chain))  # root side first, FinalLink last
        downstream = " -> ".join(rca.impacted) or self.final_id
        rca.impact = (
            f"Service outage on {self.final_id} (via {downstream})."
            if rca.outage
            else f"{self.final_id} is still serving, but {downstream} depends on the failed node."
        )
        return rca

    async def _latest_traceroute(self, node_ids: List[str]) -> Optional[Dict[str, Any]]:
        """The newest stored traceroute among the failed nodes (they are run by the escalation, not here)."""
        records = await run_query(
            self.driver,
            "MATCH (n:Domain) WHERE n.domain IN $ids AND n.traceroute IS NOT NULL "
            "RETURN n.traceroute AS t ORDER BY n.tracerouteAt DESC LIMIT 1",
            ids=node_ids,
        )
        try:
            return rca_summary(json.loads(records[0]["t"])) if records else None
        except (ValueError, TypeError, KeyError):
            return None

    async def run(self, previous: dict) -> SpiderReport:
        prev_status = previous.get("status")
        self.location = previous.get("currentNodeId")
        direction = previous.get("direction")

        # Determine leg:
        # If spider is on Main/Backup (or previous direction was DOWNSTREAM), walk downstream back to FinalLink.
        # If previous status was STOPPED or ERROR, re-evaluate upstream from FinalLink to verify recovery.
        if prev_status in ("STOPPED", "ERROR"):
            is_downstream = False
        else:
            is_downstream = (
                direction == "DOWNSTREAM"
                or (direction is None and self.location and self.location != self.final_id)
            )

        prev_rca = Rca(**json.loads(previous["rca"])) if previous.get("rca") else Rca()
        await run_query(self.driver, SET_SPIDER_STATE, spider=self.id, status="RUNNING",
                        stopNodeId=previous.get("stopNodeId"), stopReason=None, rca=previous.get("rca"),
                        direction=direction)
        alerts: List[Alert] = []
        try:
            root = await self.walk(is_downstream=is_downstream)
        except Exception as e:  # unsupported check, Neo4j error, ...
            reason = f"{type(e).__name__}: {e}"
            await self._save_position()
            await run_query(self.driver, SET_SPIDER_STATE, spider=self.id, status="ERROR",
                            stopNodeId=self.location, stopReason=reason, rca=None, direction=direction)
            if prev_status != "ERROR":
                alerts.append(Alert(kind="SPIDER_ERROR", spider_id=self.id, message=reason))
            self.log.emit(self.id, "finish", self.location, status="ERROR", reason=reason)
            return SpiderReport(spider_id=self.id, final_link_id=self.final_id, status="ERROR",
                                location=self.location, rca=Rca(impact=reason, path=self.path), alerts=alerts,
                                direction=direction, leg="downstream" if is_downstream else "upstream")

        rca = self.build_rca(root)
        if root is not None:
            rca.traceroute = await self._latest_traceroute([root.node_id] + list(root.failed or []))
        if root is not None:
            status, stop_node, stop_reason = "STOPPED", root.node_id, root.reason
            next_direction = direction or ("DOWNSTREAM" if is_downstream else "UPSTREAM")

            failed_nodes = root.failed or [root.node_id]
            consec_counts = []
            for nid in failed_nodes:
                task = self.recorder.tasks.get(nid)
                if task and task.done() and not task.cancelled() and not task.exception():
                    consec_counts.append(task.result().consecutive_failures)
            max_consecutive = max(consec_counts) if consec_counts else 1
            rca.consecutive_failures = max_consecutive

            should_alert = max_consecutive >= self.alert_threshold
            rca.alerted = prev_rca.alerted or should_alert

            if should_alert:
                if (not prev_rca.alerted) or (prev_status != "STOPPED") or (previous.get("stopNodeId") != stop_node) or (prev_rca.root_kind != root.kind):
                    suffix = f" [failed {max_consecutive} consecutive times]" if self.alert_threshold > 1 else ""
                    alerts.append(Alert(kind="SPIDER_STOPPED", spider_id=self.id,
                                        message=f"Root cause: {rca.root_cause} ({stop_reason}){suffix}. {rca.impact}"))
        elif not self.topology.has_main_input(self.final_id):
            status, stop_node = "ERROR", self.location
            stop_reason = f"TOPOLOGY: no MainInput upstream of {self.final_id}"
            next_direction = "UPSTREAM"
            if prev_status != "ERROR":
                alerts.append(Alert(kind="SPIDER_ERROR", spider_id=self.id, message=stop_reason))
        else:
            status = "RECOVERED" if prev_status == "STOPPED" else "RUNNING"
            stop_node, stop_reason = None, None
            # Toggle direction on successful completion
            next_direction = "UPSTREAM" if is_downstream else "DOWNSTREAM"
            if status == "RECOVERED":
                if prev_rca.alerted or self.alert_threshold <= 1:
                    alerts.append(Alert(kind="SPIDER_RECOVERED", spider_id=self.id,
                                        message=f"{self.final_id}: previous root cause {previous.get('stopNodeId')} recovered."))
        for note in self.failover:
            if note not in prev_rca.failover:
                alerts.append(Alert(kind="FAILOVER_ACTIVE", spider_id=self.id, message=note))

        rca_payload = (
            rca.model_dump_json(exclude_defaults=True)
            if (root is None and not self.failover)
            else rca.model_dump_json()
        )
        await self._save_position()
        await run_query(self.driver, SET_SPIDER_STATE, spider=self.id, status=status, stopNodeId=stop_node,
                        stopReason=stop_reason, rca=rca_payload, direction=next_direction)
        self.log.emit(self.id, "finish", self.location, status=status, reason=stop_reason,
                      root=rca.root_cause, impact=rca.impact, direction=next_direction,
                      leg="downstream" if is_downstream else "upstream")
        return SpiderReport(spider_id=self.id, final_link_id=self.final_id, status=status,
                            location=self.location, rca=rca, alerts=alerts, direction=next_direction,
                            leg="downstream" if is_downstream else "upstream")


# --- Spider manager ----------------------------------------------------------------


class CycleResult(BaseModel):
    reports: List[SpiderReport]
    events: List[Dict[str, Any]] = []  # EventLog timeline, ms since cycle start
    node_transitions: List[NodeTransition]
    incidents: List[Dict[str, Any]] = []  # incident log entries written this cycle (opened/escalated/resolved)
    ranking: List[Dict[str, Any]] = []  # root-cause ranking per failure group (rca_rank), when something is down
    write_batches: List[int] = []  # nodes per batched health-write transaction this cycle
    active_spiders: int
    stopped_spiders: int
    elapsed_ms: int


class SpiderManager:
    """One SpiderRun per FinalLink; runs every spider concurrently, sharing node checks."""

    def __init__(self, driver, checker: Optional[Checker] = None, alert_threshold: Optional[int] = None,
                 on_node_checked: Optional["NodeHook"] = None, metrics=None, sweep: Optional[bool] = None,
                 learning=None):
        self.driver = driver
        self.checker = checker
        self.on_node_checked = on_node_checked
        self.metrics = metrics if metrics is not None else store()
        self.sweep = HEALTH_SWEEP if sweep is None else sweep
        self.learning = learning  # learning.Learner: the operator's root-cause verdicts weigh into the ranking
        self.alert_threshold = alert_threshold if alert_threshold is not None else ALERT_CONSECUTIVE_THRESHOLD
        for query in (
            "CREATE CONSTRAINT spider_final_unique IF NOT EXISTS FOR (s:SpiderRun) REQUIRE s.finalLinkId IS UNIQUE",
            "CREATE CONSTRAINT spider_id_unique IF NOT EXISTS FOR (s:SpiderRun) REQUIRE s.id IS UNIQUE",
        ):
            driver.execute_query(query)

    def rank_root_causes(self, node_ids: List[str], incidents: List[dict]) -> List[dict]:
        """Ranks the likely root cause of each group of failing nodes (see rca_rank) and attaches it to the
        incidents opened this cycle."""
        states = node_states(self.driver, node_ids)
        if all(s["up"] for s in states.values()):
            return []
        edges = [dict(r) for r in self.driver.execute_query(
            "MATCH (a:Domain)-[r:FEEDS|PRODUCES]->(b:Domain) WHERE a.domain IN $ids AND b.domain IN $ids "
            "RETURN a.domain AS source, type(r) AS type, b.domain AS target", ids=node_ids).records]
        anomalies = {n: self.metrics.anomalies(n) for n in node_ids}
        groups = rca_rank.rank(states, edges, anomalies, self.learning.priors() if self.learning else None)
        for incident in incidents:
            if incident.get("type") != "OUTAGE":
                continue
            group = next((g for g in groups if incident["node"] in g["nodes"]), None)
            if group:
                incident["rootCauseRanking"] = group["ranking"][:3]
                self.metrics.update_incident(incident["node"], incident["timestamp"], "OUTAGE",
                                             rootCauseRanking=group["ranking"][:3])
        return groups

    def ensure_spiders(self, final_ids: List[str]) -> Dict[str, dict]:
        """Reuse the FinalLink's existing SpiderRun, creating it only if missing (no duplicates)."""
        records = self.driver.execute_query(ENSURE_SPIDERS, finals=final_ids).records
        return {r["final"]: dict(r) for r in records}

    async def run_cycle_async(
        self, final_ids: Optional[List[str]] = None, listener: Optional[Callable[[Dict[str, Any]], None]] = None
    ) -> CycleResult:
        start = time.monotonic()
        log = EventLog(listener)
        if final_ids is None:
            final_ids = get_all_final_links(self.driver)
            self.driver.execute_query(REMOVE_ORPHAN_SPIDERS)
        spiders = self.ensure_spiders(final_ids)
        topology = Topology(self.driver, final_ids)

        async def run_all(checker: Checker):
            recorder = HealthRecorder(self.driver, topology, checker, alert_threshold=self.alert_threshold,
                                      on_node_checked=self.on_node_checked, metrics=self.metrics)
            recorder.prefetch(list(topology.nodes))
            try:
                reports = await asyncio.gather(*(
                    Spider(s["id"], f, self.driver, topology, recorder, log, alert_threshold=self.alert_threshold).run(s)
                    for f, s in spiders.items()
                ))
                if self.sweep:
                    await recorder.sweep()
            finally:
                recorder.discard_unused()
            return reports, recorder.transitions, recorder.incidents, recorder.batches

        if self.checker is not None:
            reports, transitions, incidents, batches = await run_all(self.checker)
        else:
            async with http_client() as client:
                sem = asyncio.Semaphore(32)
                reports, transitions, incidents, batches = await run_all(lambda node: check_node(node, client, sem))

        ranking = await asyncio.get_running_loop().run_in_executor(None, self.rank_root_causes, list(topology.nodes), incidents)
        return CycleResult(
            reports=list(reports), node_transitions=transitions, incidents=incidents, ranking=ranking,
            write_batches=batches, events=log.events,
            active_spiders=count_spiders(self.driver, "RUNNING"),
            stopped_spiders=count_spiders(self.driver, "STOPPED"),
            elapsed_ms=int((time.monotonic() - start) * 1000),
        )

    def run_cycle(
        self, final_ids: Optional[List[str]] = None, listener: Optional[Callable[[Dict[str, Any]], None]] = None
    ) -> CycleResult:
        return asyncio.run(self.run_cycle_async(final_ids, listener))


NODE_STATES = """
MATCH (n:Domain) WHERE n.domain IN $ids
RETURN n.domain AS node, n.status AS status, n.urlHealth AS urlHealth
"""


def node_states(driver, node_ids: List[str]) -> Dict[str, dict]:
    """node -> {up, onsetAt (earliest among its failing URLs), onsetPrecisionS, category}, for rca_rank."""
    out = {}
    for r in driver.execute_query(NODE_STATES, ids=node_ids).records:
        down = [h for h in _state(r["urlHealth"]).values() if h.get("up") is False and not h.get("ignored")]
        onset = min(down, key=lambda h: h.get("onsetAt") or "~", default={})
        categories = [h.get("category") for h in down if h.get("category")]
        out[r["node"]] = {"up": r["status"] != "DOWN", "onsetAt": onset.get("onsetAt"),
                          "onsetPrecisionS": onset.get("onsetPrecisionS"), "onsetMethod": onset.get("onsetMethod"),
                          "category": max(set(categories), key=categories.count) if categories else None}
    return out


def migrate_logs(driver, incident_store) -> int:
    """One-time move of the old per-node `n.log` JSON ring buffers into the incident store; returns entries moved."""
    moved = 0
    for r in driver.execute_query("MATCH (n:Domain) WHERE n.log IS NOT NULL RETURN n.domain AS d, n.log AS log").records:
        for entry in load_json_list(r["log"]):
            if isinstance(entry, dict):
                incident_store.add_incident(r["d"], entry)
                moved += 1
    driver.execute_query("MATCH (n:Domain) WHERE n.log IS NOT NULL REMOVE n.log")
    return moved


# --- Queries for dashboards --------------------------------------------------------


def count_spiders(driver, status: str) -> int:
    return driver.execute_query(
        "MATCH (s:SpiderRun {status: $status}) WHERE NOT s.finalLinkId ENDS WITH '.invalid' RETURN count(s) AS c",
        status=status,
    ).records[0]["c"]


def spider_location(driver, spider_id: str) -> Optional[dict]:
    records = driver.execute_query(
        "MATCH (s:SpiderRun {id: $id})-[:AT]->(n) RETURN properties(s) AS spider, properties(n) AS node, "
        "[l IN labels(n) WHERE l <> 'Domain'] AS labels",
        id=spider_id,
    ).records
    return dict(records[0]) if records else None


def stopped_spiders(driver) -> List[dict]:
    return [dict(r) for r in driver.execute_query(
        "MATCH (s:SpiderRun {status: 'STOPPED'})-[:AT]->(n) RETURN properties(s) AS spider, properties(n) AS node"
    ).records]


def all_spiders(driver) -> List[dict]:
    """Every SpiderRun with its current node and how many AT edges it has (must be 1)."""
    return [dict(r) for r in driver.execute_query(
        """
        MATCH (s:SpiderRun) WHERE NOT s.finalLinkId ENDS WITH '.invalid'
        OPTIONAL MATCH (s)-[:AT]->(n)
        WITH s, collect(n) AS at
        RETURN properties(s) AS spider, size(at) AS atCount,
               CASE WHEN size(at) = 0 THEN null ELSE properties(at[0]) END AS node,
               CASE WHEN size(at) = 0 THEN [] ELSE [l IN labels(at[0]) WHERE l <> 'Domain'] END AS labels
        ORDER BY s.id
        """
    ).records]


# --- CLI ---------------------------------------------------------------------------


def print_cycle(result: CycleResult) -> None:
    for r in result.reports:
        print(f"\n{r.spider_id}\n{'━' * 40}")
        for step in r.rca.path:
            mark = "✓" if step.up else "✗ DOWN"
            how = "" if step.visited else "  (checked, not visited)"
            print(f"  {step.role:24} {step.node_id:26} {mark}{how}")
            if not step.up and step.error:
                print(f"      {step.error[:140]}")
            if r.status == "STOPPED" and step.node_id == r.location:
                print("          ↑ SPIDER STOPPED HERE")
        print(f"  Status: {r.status}   Current location: {r.location}")
        if r.rca.root_cause:
            print(f"  Root cause: {r.rca.root_cause}   Reason: {r.rca.reason[:160]}")
        print(f"  Impact: {r.rca.impact}")
        for note in r.rca.failover:
            print(f"  Failover: {note}")
    print()
    for r in result.reports:
        print(f"{r.spider_id} → {r.final_link_id} → {r.status} @ {r.location}")
    print(f"Active Spiders: {result.active_spiders}   Stopped Spiders: {result.stopped_spiders}   ({result.elapsed_ms}ms)")
    for t in result.node_transitions:
        print(f"node {t.node_id}: {t.previous} → {t.current}")
    for a in (a for r in result.reports for a in r.alerts):
        print(f"ALERT {a.kind} [{a.spider_id}] {a.message}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run one spider per FinalLink and report root causes.")
    parser.add_argument("--interval", type=int, help="keep running, one cycle every N seconds")
    args = parser.parse_args()

    load_dotenv()
    with GraphDatabase.driver(
        os.environ["NEO4J_URI"], auth=(os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"])
    ) as driver:
        manager = SpiderManager(driver)
        if args.interval is None:
            print_cycle(manager.run_cycle())
        else:
            print(f"Spiders patrolling every {args.interval}s; Ctrl+C to stop.")
            try:
                while True:
                    result = manager.run_cycle()
                    # Only state changes are worth a line in a loop, plus a heartbeat.
                    for a in (a for r in result.reports for a in r.alerts):
                        print(f"ALERT {a.kind} [{a.spider_id}] {a.message}")
                    print(f"{time.strftime('%H:%M:%S')} active={result.active_spiders} "
                          f"stopped={result.stopped_spiders} ({result.elapsed_ms}ms)")
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                pass
