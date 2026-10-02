"""The weekly (or on-demand) Embeddings & Incident Clearance job.

1. Embeds every :Domain and :SpiderRun with the project's one model (MiniLM via ONNX, see text_embedding) into
   `embedding`: the vectors GraphRAG searches and the report compares. Nodes whose profile didn't change keep
   their vector unless the run is forced.
2. (clear_logs) Folds incident entries older than INCIDENT_RETENTION_DAYS from the incident store (SQLite, see
   metrics) into each node's cumulative `incidentStats` totals, then deletes them.
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

EMBEDDING_DIM = 384


def _log_entries(value) -> List[dict]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []


def _error_kind(error: Optional[str]) -> Optional[str]:
    """'HTTP_CONNECTERROR after 3 tries' / '... index.m3u8 HTTP 404' / '... (STALE_SEGMENTS (36s old))' -> the
    error codes in it ('HTTP_CONNECTERROR', 'HTTP 404', 'STALE_SEGMENTS'), so the same failure counts together."""
    if not error:
        return None
    text = re.sub(r"https?://\S+", "", str(error))
    codes = re.findall(r"HTTP[ _][A-Z0-9_]+|\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b", text)
    if codes:
        return " + ".join(dict.fromkeys(codes))
    return re.sub(r"\s+", " ", text).strip(" :-")[:60] or None


def summarize_incidents(entries: List[dict], previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Folds raw incident log entries into cumulative totals (kept on the node as `incidentStats`), so
    clearing the log keeps the history's shape: how often it failed and recovered, how, and when."""
    stats = {"outages": 0, "recoveries": 0, "firstIncidentAt": None, "lastOutageAt": None,
             "lastRecoveryAt": None, "maxConsecutiveFailures": 0, "errors": {}, "categories": {}, "archivedEntries": 0}
    if previous:
        stats.update({k: v for k, v in previous.items() if k in stats})
        stats["errors"] = dict(previous.get("errors") or {})
        stats["categories"] = dict(previous.get("categories") or {})
    for e in entries:
        ts = e.get("timestamp")
        if e.get("type") == "OUTAGE":
            stats["outages"] += 1
            stats["lastOutageAt"] = max(filter(None, [stats["lastOutageAt"], ts]), default=None)
            kind = _error_kind(e.get("lastError"))
            if kind:
                stats["errors"][kind] = stats["errors"].get(kind, 0) + 1
            if e.get("category"):  # incident type (STALE_MEDIA, PLAYLIST_MISSING, ...)
                stats["categories"][e["category"]] = stats["categories"].get(e["category"], 0) + 1
        elif e.get("type") == "RECOVERY":
            stats["recoveries"] += 1
            stats["lastRecoveryAt"] = max(filter(None, [stats["lastRecoveryAt"], ts]), default=None)
        stats["firstIncidentAt"] = min(filter(None, [stats["firstIncidentAt"], ts]), default=None)
        stats["maxConsecutiveFailures"] = max(stats["maxConsecutiveFailures"], int(e.get("consecutiveFailures") or 0))
        stats["archivedEntries"] += 1
    stats["errors"] = dict(sorted(stats["errors"].items(), key=lambda kv: -kv[1])[:5])  # top 5 kinds
    return stats


def incident_text(stats: Optional[Dict[str, Any]]) -> str:
    if not stats or not (stats.get("outages") or stats.get("recoveries")):
        return "Incidents: none"
    errors = ", ".join(f"{k} x{v}" for k, v in (stats.get("errors") or {}).items())
    return (f"Incidents: {stats['outages']} outages, {stats['recoveries']} recoveries, "
            f"max {stats.get('maxConsecutiveFailures', 0)} consecutive failures, last outage "
            f"{stats.get('lastOutageAt') or '-'}{', errors: ' + errors if errors else ''}")


def sync_all_embeddings(driver, clear_logs: bool = True, only_suffix: Optional[str] = None,
                        incident_store=None, all_logs: bool = False) -> Dict[str, Any]:
    """Embeds every server and spider (forced), then (clear_logs) archives incidents into
    totals. If all_logs=True, archives all current incidents up to now; otherwise respects retention_days."""
    from chatbot import Snapshot
    from graphrag import GraphRAG
    import text_embedding
    from metrics import INCIDENT_RETENTION_DAYS, store

    started = time.monotonic()
    incident_store = incident_store or store()
    snap = Snapshot(driver, only_suffix=only_suffix, incidents=incident_store)
    rag = GraphRAG(driver, text_embedding.encode)
    rag.sync(snap, force=True)
    logs_cleared = entries_archived = 0
    if clear_logs:
        if all_logs:
            cutoff = datetime.now(timezone.utc).isoformat(timespec="seconds")
        else:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=INCIDENT_RETENTION_DAYS)).isoformat(timespec="seconds")
        for node, entries in incident_store.take_incidents_before(cutoff).items():
            if only_suffix and not node.endswith(only_suffix):
                for e in entries:  # not ours to archive: put them back
                    incident_store.add_incident(node, e)
                continue
            archive_incidents(driver, node, entries)
            logs_cleared += 1
            entries_archived += len(entries)
        # If logs were cleared and incidentStats updated, re-sync Domain embeddings with updated stats!
        if entries_archived > 0:
            snap_updated = Snapshot(driver, only_suffix=only_suffix, incidents=incident_store)
            rag.sync(snap_updated, force=True)
    return {"servers_updated": rag.last_sync.get("Domain", 0), "spiders_updated": rag.last_sync.get("SpiderRun", 0),
            "logs_cleared": logs_cleared, "entries_archived": entries_archived, "clear_logs": clear_logs,
            "retention_days": 0 if all_logs else INCIDENT_RETENTION_DAYS, "elapsed_ms": int((time.monotonic() - started) * 1000)}


def archive_incidents(driver, node: str, entries: List[dict]) -> None:
    """Adds incident entries to the node's cumulative `incidentStats` totals (in one transaction)."""
    def update(tx):
        row = tx.run("MATCH (n:Domain {domain: $d}) SET n.incidentStats = n.incidentStats "
                     "RETURN n.incidentStats AS s", d=node).single()
        if row is None:
            return
        try:
            previous = json.loads(row["s"]) if row["s"] else None
        except ValueError:
            previous = None
        tx.run("MATCH (n:Domain {domain: $d}) SET n.incidentStats = $s", d=node,
               s=json.dumps(summarize_incidents(entries, previous)))

    with driver.session() as session:
        session.execute_write(update)


if __name__ == "__main__":
    import os
    import sys
    from dotenv import load_dotenv
    from neo4j import GraphDatabase

    load_dotenv()
    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("NEO4J_USERNAME", "neo4j")
    pwd = os.environ.get("NEO4J_PASSWORD") or os.environ.get("NEO4J_LOCAL_PASSWORD", "")
    print(f"[Weekly Cron] Connecting to Neo4j at {uri}...")
    try:
        driver = GraphDatabase.driver(uri, auth=(user, pwd))
        with driver:
            driver.verify_connectivity()
            counts = sync_all_embeddings(driver, clear_logs="--keep-logs" not in sys.argv)
            print(f"[Weekly Cron] Done: {counts}")
    except Exception as e:
        print(f"[Weekly Cron] Error: {e}", file=sys.stderr)
        sys.exit(1)
