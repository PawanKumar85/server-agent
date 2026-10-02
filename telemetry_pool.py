"""Central Thread-Safe In-Memory Telemetry Data Pool.

Provides ultra-low-latency (< 0.1ms) caching and deduplication for:
- Node states, properties, and parsed links/urlHealth
- Channel-to-node routing index
- Live HLS freshness, media-sequence, and segment metrics
- Topology relationships and spiders

Thread-safe across FastAPI request handlers, background spider threads,
and ChatBot / GraphRAG tool executions.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("telemetry_pool")

TEST_TLD = ".invalid"


def _safe_json(val: Any, default: Any = None) -> Any:
    if not val:
        return default if default is not None else {}
    if isinstance(val, (dict, list)):
        return val
    try:
        return json.loads(val)
    except Exception:
        return default if default is not None else {}


class TelemetryDataPool:
    """Thread-safe in-memory cache for node telemetry, channels, and topology."""

    def __init__(self, ttl_seconds: float = 5.0) -> None:
        self.ttl = ttl_seconds
        self._lock = threading.RLock()
        self._nodes: Dict[str, dict] = {}
        self._channels: Dict[str, Dict[str, List[dict]]] = {}
        self._topology: List[dict] = []
        self._spiders: Dict[str, dict] = []
        self._last_refresh: float = 0.0
        self._hits: int = 0
        self._misses: int = 0
        self._writes: int = 0

    # -------------------------------------------------------------------------
    # Core Cache Access
    # -------------------------------------------------------------------------

    def is_fresh(self) -> bool:
        """Check if cached telemetry is within TTL window."""
        with self._lock:
            return bool(self._nodes) and (time.time() - self._last_refresh) < self.ttl

    def invalidate(self) -> None:
        """Force cache invalidation (e.g., after topology mutation or link update)."""
        with self._lock:
            self._last_refresh = 0.0
            logger.debug("Telemetry pool invalidated.")

    def clear(self) -> None:
        """Clear all in-memory pool data."""
        with self._lock:
            self._nodes.clear()
            self._channels.clear()
            self._topology.clear()
            self._last_refresh = 0.0

    def stats(self) -> dict:
        """Return operational telemetry pool performance metrics."""
        with self._lock:
            total = self._hits + self._misses
            ratio = (self._hits / total * 100.0) if total > 0 else 0.0
            age = round(time.time() - self._last_refresh, 2) if self._last_refresh else None
            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_ratio_pct": round(ratio, 1),
                "writes": self._writes,
                "node_count": len(self._nodes),
                "channel_count": len(self._channels),
                "cached_age_s": age,
                "ttl_s": self.ttl,
                "is_fresh": self.is_fresh(),
            }

    # -------------------------------------------------------------------------
    # Node & Channel Fetching
    # -------------------------------------------------------------------------

    def get_nodes(self, driver: Any = None, force_refresh: bool = False) -> Dict[str, dict]:
        """Return all nodes from pool, refreshing from Neo4j driver if expired."""
        with self._lock:
            if not force_refresh and self.is_fresh():
                self._hits += 1
                return dict(self._nodes)

            self._misses += 1
            if driver is not None:
                self._refresh_from_neo4j(driver)
            return dict(self._nodes)

    def get_node(self, domain: str, driver: Any = None) -> Optional[dict]:
        """Get a single node by domain/node_id from pool."""
        nodes = self.get_nodes(driver=driver)
        return nodes.get(domain)

    def get_channels(self, driver: Any = None, force_refresh: bool = False) -> Dict[str, Dict[str, List[dict]]]:
        """Return indexed channels mapping: channel -> {role: [link_dict]}."""
        with self._lock:
            if not force_refresh and self.is_fresh():
                self._hits += 1
                return dict(self._channels)

            self._misses += 1
            if driver is not None:
                self._refresh_from_neo4j(driver)
            return dict(self._channels)

    def get_channel(self, channel_name: str, driver: Any = None) -> Optional[Dict[str, List[dict]]]:
        """Find channel links by channel name (case-insensitive)."""
        channels = self.get_channels(driver=driver)
        if channel_name in channels:
            return channels[channel_name]
        lower = channel_name.lower()
        for k, v in channels.items():
            if k.lower() == lower:
                return v
        return None

    # -------------------------------------------------------------------------
    # Direct In-Memory Push from Spiders / Probers
    # -------------------------------------------------------------------------

    def update_node_metric(self, domain: str, **metrics: Any) -> None:
        """Push fast telemetry metrics directly into the pool without a full DB query.
        
        Enables spider workers to warm the pool instantly as manifests/ICMP complete.
        """
        with self._lock:
            if domain in self._nodes:
                self._nodes[domain].update(metrics)
                self._writes += 1

    # -------------------------------------------------------------------------
    # Internal Database Sync
    # -------------------------------------------------------------------------

    def _refresh_from_neo4j(self, driver: Any) -> None:
        """Pull full graph state into in-memory structures."""
        try:
            records = driver.execute_query(
                "MATCH (n:Domain) WHERE NOT n.domain ENDS WITH $test "
                "RETURN properties(n) AS p, [l IN labels(n) WHERE l <> 'Domain'] AS labels "
                "ORDER BY n.domain",
                test=TEST_TLD,
            ).records
        except Exception as e:
            logger.warning(f"TelemetryPool: failed to query Neo4j: {e}")
            return

        new_nodes: Dict[str, dict] = {}
        new_channels: Dict[str, Dict[str, List[dict]]] = {}

        for r in records:
            p = dict(r["p"])
            domain = p.get("domain", "")
            raw_links = _safe_json(p.get("links"), default=[])
            raw_health = _safe_json(p.get("urlHealth"), default={})
            labels = list(r["labels"])

            node_entry = {
                "domain": domain,
                "labels": labels,
                "server_ip": p.get("server_ip"),
                "status": p.get("status", "UNKNOWN"),
                "lastPing": p.get("lastPing"),
                "lastLatencyMs": p.get("lastLatencyMs"),
                "lastPacketLoss": p.get("lastPacketLoss"),
                "consecutiveFailures": p.get("consecutiveFailures", 0),
                "pingCount": p.get("pingCount", 0),
                "failedCount": p.get("failedCount", 0),
                "lastError": p.get("lastError"),
                "lastSegmentAgeS": p.get("lastSegmentAgeS"),
                "targetDurationS": p.get("targetDurationS"),
                "streamFreshness": p.get("streamFreshness"),
                "mediaSequence": p.get("mediaSequence"),
                "discontinuities": p.get("discontinuities"),
                "links": raw_links,
                "urlHealth": raw_health,
                "raw_properties": p,
            }
            new_nodes[domain] = node_entry

            # Build inverted channel index
            for l in raw_links:
                ch = l.get("channel")
                role = l.get("role") or l.get("label") or "Stream"
                if ch:
                    url = l.get("url", "")
                    h = raw_health.get(url, {}) if isinstance(raw_health, dict) else {}
                    link_info = {
                        "url": url,
                        "node": domain,
                        "role": role,
                        "channel": ch,
                        "up": h.get("up"),
                        "detail": h.get("detail"),
                        "lastLatencyMs": h.get("latency_ms"),
                        "segment_age_s": h.get("segment_age_s"),
                        "target_duration_s": h.get("target_duration_s"),
                        "freshness": h.get("freshness"),
                        "sequences": h.get("sequences"),
                    }
                    new_channels.setdefault(ch, {}).setdefault(role, []).append(link_info)

        self._nodes = new_nodes
        self._channels = new_channels
        self._last_refresh = time.time()
        self._writes += 1
        logger.debug(f"TelemetryPool: refreshed {len(new_nodes)} nodes, {len(new_channels)} channels.")


# Global singleton instance
telemetry_pool = TelemetryDataPool(ttl_seconds=5.0)
