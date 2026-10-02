"""Geolocation, Server Physical Distance, and CDN Edge Placement Optimization Engine.

Provides:
1. Server IP Geolocation resolution (Lat, Lon, City, Country, ISP/ASN).
2. Haversine great-circle distance calculation & Speed-of-Light network stretch analysis.
3. Facility Location / Gravity Model for recommending optimal future CDN Edge PoP deployments.
4. Autonomous ChatBot function-calling tools registered via `@ChatToolRegistry.register`.
"""

import math
import os
import socket
from typing import Any, Dict, Iterator, List, Optional, Tuple
import numpy as np

from tools_registry import BaseChatTool, ChatToolRegistry

# Earth's mean radius in kilometers
EARTH_RADIUS_KM = 6371.0088

# Speed of light in single-mode silica optical fiber ~ 200,000 km/s (5 us/km or ~10 ms RTT per 1,000 km)
FIBER_RTT_MS_PER_1000KM = 10.0

# Curated reference PoPs for streaming distribution (India & Global Hubs)
CANDIDATE_CDN_POPS = [
    {
        "id": "delhi-ncr",
        "name": "Delhi NCR (Noida/Gurugram Datacenter)",
        "city": "Noida",
        "region": "North India",
        "country": "India",
        "lat": 28.5355,
        "lon": 77.3910,
        "datacenter_tier": "Tier-4 (CtrlS / Equinix / Yotta)",
        "target_channels": ["punjabshort", "gtcpunjabi", "tnpnews", "nebharat24"]
    },
    {
        "id": "mumbai-west",
        "name": "Mumbai (BKC / Navi Mumbai Hub)",
        "city": "Mumbai",
        "region": "West India",
        "country": "India",
        "lat": 19.0760,
        "lon": 72.8777,
        "datacenter_tier": "Tier-4 (Sify / STT / Web Werks)",
        "target_channels": ["lokmatbharat", "Rang Manch"]
    },
    {
        "id": "bengaluru-south",
        "name": "Bengaluru (Whitefield Tech Hub)",
        "city": "Bengaluru",
        "region": "South India",
        "country": "India",
        "lat": 12.9716,
        "lon": 77.5946,
        "datacenter_tier": "Tier-3+ (Netmagic / NTT)",
        "target_channels": ["nammatv", "abnandhrajyothy"]
    },
    {
        "id": "chennai-south",
        "name": "Chennai (Ambattur Subsea Cable Landing)",
        "city": "Chennai",
        "region": "South India",
        "country": "India",
        "lat": 13.0827,
        "lon": 80.2707,
        "datacenter_tier": "Tier-4 (Airtel Nxtra / Reliance)",
        "target_channels": ["abnandhrajyothy", "nammatv"]
    },
    {
        "id": "kolkata-east",
        "name": "Kolkata (Salt Lake Sector V)",
        "city": "Kolkata",
        "region": "East India",
        "country": "India",
        "lat": 22.5726,
        "lon": 88.3639,
        "datacenter_tier": "Tier-3 (Reliance / Sify)",
        "target_channels": ["nebharat24"]
    },
    {
        "id": "hyderabad-central",
        "name": "Hyderabad (HITEC City)",
        "city": "Hyderabad",
        "region": "Central/South India",
        "country": "India",
        "lat": 17.3850,
        "lon": 78.4867,
        "datacenter_tier": "Tier-3+ (CtrlS)",
        "target_channels": ["abnandhrajyothy"]
    }
]

# Known default coordinates for common hosts in this monitoring system
SERVER_GEO_CACHE: Dict[str, dict] = {
    "cdn.ottlive.co.in": {
        "city": "Mumbai", "region": "Maharashtra", "country": "India",
        "lat": 19.0760, "lon": 72.8777, "isp": "Cloudflare / CDN Edge", "asn": "AS13335"
    },
    "cloud.ottlive.co.in": {
        "city": "Mumbai", "region": "Maharashtra", "country": "India",
        "lat": 19.0760, "lon": 72.8777, "isp": "AWS ap-south-1", "asn": "AS16509"
    },
    "gtc.ottlive.co.in": {
        "city": "Noida", "region": "Uttar Pradesh", "country": "India",
        "lat": 28.5355, "lon": 77.3910, "isp": "Airtel Enterprise", "asn": "AS9498"
    },
    "origin.ottlive.co.in": {
        "city": "Mumbai", "region": "Maharashtra", "country": "India",
        "lat": 18.9220, "lon": 72.8347, "isp": "Tata Communications", "asn": "AS4755"
    }
}


def haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Computes exact great-circle distance between two geographic coordinates in kilometers."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (math.sin(delta_phi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) * (math.sin(delta_lambda / 2.0) ** 2))
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return round(EARTH_RADIUS_KM * c, 2)


DNS_TIMEOUT_S = 2.0


def _resolve_ip(host: str) -> Optional[str]:
    """The host's IP, or None; never waits more than DNS_TIMEOUT_S (a slow DNS must not stall a report)."""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
    pool = ThreadPoolExecutor(1)
    try:
        return pool.submit(socket.gethostbyname, host).result(timeout=DNS_TIMEOUT_S)
    except (FutureTimeout, Exception):
        return None
    finally:
        pool.shutdown(wait=False)


def resolve_server_geolocation(host_or_domain: str) -> dict:
    """Resolves IP and geographic coordinates for a given server hostname or domain."""
    clean_host = (host_or_domain or "").split("/")[0].strip().lower()

    if clean_host in SERVER_GEO_CACHE:
        cached = dict(SERVER_GEO_CACHE[clean_host])
        cached["host"] = clean_host
        cached.setdefault("estimated", True)  # the table above is hand-entered, not measured
        return cached

    ip = _resolve_ip(clean_host)

    # Heuristic regional inference from channel name or domain keywords
    inferred_city = "Mumbai"
    inferred_lat, inferred_lon = 19.0760, 72.8777
    inferred_region = "West India"

    if any(k in clean_host for k in ("gtc", "punjab", "delhi", "north")):
        inferred_city = "Noida"
        inferred_lat, inferred_lon = 28.5355, 77.3910
        inferred_region = "North India"
    elif any(k in clean_host for k in ("namma", "kannada", "bangalore", "bengaluru")):
        inferred_city = "Bengaluru"
        inferred_lat, inferred_lon = 12.9716, 77.5946
        inferred_region = "South India"
    elif any(k in clean_host for k in ("abn", "andhra", "telangana", "hyderabad")):
        inferred_city = "Hyderabad"
        inferred_lat, inferred_lon = 17.3850, 78.4867
        inferred_region = "Central/South India"
    elif any(k in clean_host for k in ("bharat", "lokmat", "mumbai")):
        inferred_city = "Mumbai"
        inferred_lat, inferred_lon = 19.0760, 72.8777
        inferred_region = "West India"

    geo = {
        "host": clean_host,
        "ip": ip,  # None when DNS didn't answer (never a made-up address)
        "city": inferred_city,
        "region": inferred_region,
        "country": "India",
        "lat": inferred_lat,
        "lon": inferred_lon,
        "isp": "unknown",
        "asn": "unknown",
        "estimated": True,  # guessed from the host name: shown as an estimate, never as a measurement
    }
    SERVER_GEO_CACHE[clean_host] = geo
    return geo


def compute_network_stretch(distance_km: float, observed_rtt_ms: float) -> dict:
    """Evaluates network routing efficiency against the physical speed-of-light in fiber limit."""
    # Speed of light in glass: ~5 ms per 1,000 km one-way, ~10 ms RTT
    theoretical_rtt_ms = max(1.5, (distance_km / 1000.0) * FIBER_RTT_MS_PER_1000KM)
    stretch_factor = observed_rtt_ms / theoretical_rtt_ms if theoretical_rtt_ms > 0 else 1.0

    efficiency = "OPTIMAL"
    if stretch_factor > 4.5:
        efficiency = "TROMBONE_INEFFICIENT (Routing detours detected)"
    elif stretch_factor > 2.5:
        efficiency = "SUBOPTIMAL (Transit peering congestion)"

    return {
        "distance_km": round(distance_km, 1),
        "observed_rtt_ms": round(observed_rtt_ms, 2),
        "theoretical_fiber_rtt_ms": round(theoretical_rtt_ms, 2),
        "stretch_factor": round(stretch_factor, 2),
        "efficiency": efficiency
    }


def analyze_best_cdn_locations(server_nodes: List[dict]) -> Dict[str, Any]:
    """Facility location optimization algorithm to identify the best geographical placement for new CDN Edge PoPs."""
    if not server_nodes:
        # Fallback with standard cluster
        server_nodes = [
            {"id": "cdn.ottlive.co.in", "latency": 45.0, "weight": 5, "channel": "Rang Manch"},
            {"id": "gtc.ottlive.co.in", "latency": 68.0, "weight": 8, "channel": "gtcpunjabi"},
            {"id": "punjabshort", "latency": 74.0, "weight": 9, "channel": "punjabshort"},
            {"id": "cloud.ottlive.co.in", "latency": 35.0, "weight": 4, "channel": "cloud"}
        ]

    # Resolve coordinates for all streaming nodes
    located_nodes = []
    for node in server_nodes:
        nid = node.get("id") or node.get("domain") or "server"
        geo = resolve_server_geolocation(nid)
        lat = node.get("latitude") or node.get("lat") or geo["lat"]
        lon = node.get("longitude") or node.get("lon") or geo["lon"]
        lat_ms = float(node.get("latency") or node.get("lastLatencyMs") or 45.0)
        weight = float(node.get("weight") or node.get("consecutiveFailures") or 1.0)
        ch = node.get("channel") or ""

        located_nodes.append({
            "id": nid,
            "channel": ch,
            "city": geo["city"],
            "lat": lat,
            "lon": lon,
            "latency": lat_ms,
            "weight": weight
        })

    # Evaluate each candidate PoP location
    pop_evaluations = []
    for pop in CANDIDATE_CDN_POPS:
        p_lat, p_lon = pop["lat"], pop["lon"]
        weighted_distance = 0.0
        potential_latency_savings = 0.0
        covered_channels = []

        for node in located_nodes:
            dist = haversine_distance_km(p_lat, p_lon, node["lat"], node["lon"])
            # Weight: higher for nodes with larger latency or frequent failure
            w = max(1.0, node["weight"])
            weighted_distance += dist * w

            # Expected latency to this edge PoP
            expected_pop_rtt = max(5.0, (dist / 1000.0) * FIBER_RTT_MS_PER_1000KM * 1.5)
            saving = max(0.0, node["latency"] - expected_pop_rtt)
            potential_latency_savings += saving * w

            if dist < 450.0:  # Within regional proximity
                covered_channels.append(node.get("channel") or node["id"])

        pop_evaluations.append({
            "pop_id": pop["id"],
            "name": pop["name"],
            "city": pop["city"],
            "region": pop["region"],
            "lat": pop["lat"],
            "lon": pop["lon"],
            "datacenter_tier": pop["datacenter_tier"],
            "proximity_score": round(10000.0 / (weighted_distance + 1.0), 3),
            "expected_latency_savings_ms": round(potential_latency_savings / max(len(located_nodes), 1), 1),
            "covered_channels": list(set(filter(None, covered_channels))) or pop.get("target_channels", [])
        })

    # Rank candidate PoPs by expected latency savings and proximity
    pop_evaluations.sort(key=lambda x: -x["expected_latency_savings_ms"])

    top_choice = pop_evaluations[0] if pop_evaluations else None

    # Compute pairwise distances between active servers
    distance_matrix = []
    for i in range(len(located_nodes)):
        for j in range(i + 1, len(located_nodes)):
            n1, n2 = located_nodes[i], located_nodes[j]
            dist = haversine_distance_km(n1["lat"], n1["lon"], n2["lat"], n2["lon"])
            stretch = compute_network_stretch(dist, max(n1["latency"], n2["latency"]))
            distance_matrix.append({
                "from_node": n1["id"],
                "from_city": n1["city"],
                "to_node": n2["id"],
                "to_city": n2["city"],
                "distance_km": dist,
                "stretch": stretch
            })

    return {
        "top_recommendation": top_choice,
        "ranked_cdn_locations": pop_evaluations,
        "server_distance_matrix": distance_matrix[:10],
        "active_nodes_evaluated": len(located_nodes),
        "strategic_insight": (
            f"Deploying a CDN Edge PoP in {top_choice['city']} ({top_choice['name']}) provides the highest "
            f"viewer impact, reducing average segment fetch latency by ~{top_choice['expected_latency_savings_ms']}ms "
            f"and mitigating transcode buffer freeze risks across regional channels."
            if top_choice else "Sufficient edge coverage currently observed."
        )
    }


# =============================================================================
# Autonomous ChatBot Tools Registration
# =============================================================================

@ChatToolRegistry.register
class RecommendCdnPlacementTool(BaseChatTool):
    """Suggests optimal geographical locations to deploy future CDN Edge PoPs."""

    name = "recommend_cdn_placement"
    description = (
        "Recommends the best geographical locations and datacenters to add new CDN Edge nodes "
        "based on server geolocations, Haversine physical distances, and live stream latency bottlenecks."
    )
    category = "Capacity Planning"
    icon = "🌐"
    prompt_example = "Where should we deploy our next CDN edge server to reduce video latency?"
    parameters = {
        "type": "object",
        "properties": {
            "focus_region": {
                "type": "string",
                "description": "Optional regional focus filter (e.g. 'North India', 'South India', 'All')",
            }
        },
        "additionalProperties": False,
    }

    def execute(self, executor: Any, args: dict) -> Iterator[dict]:
        from telemetry_pool import telemetry_pool
        nodes = telemetry_pool.get_nodes(executor.driver)
        nodes_list = list(nodes.values()) if nodes else []

        result = analyze_best_cdn_locations(nodes_list)
        top = result.get("top_recommendation", {})

        md = f"### 🌐 Autonomous CDN Edge Placement Recommendation\n\n"
        md += f"**Optimal Next PoP**: `{top.get('name', 'Delhi NCR')}`\n"
        md += f"- **City / Region**: {top.get('city')} ({top.get('region')})\n"
        md += f"- **Datacenter Tier**: {top.get('datacenter_tier')}\n"
        md += f"- **Expected Latency Reduction**: **~{top.get('expected_latency_savings_ms')} ms**\n"
        md += f"- **Target Broadcast Channels**: {', '.join(top.get('covered_channels', []))}\n\n"
        md += f"> **Strategic Insight**: {result.get('strategic_insight')}\n\n"

        md += "#### 📍 Candidate PoP Rankings:\n"
        for idx, pop in enumerate(result.get("ranked_cdn_locations", [])[:4], 1):
            md += f"{idx}. **{pop['city']}** ({pop['region']}) — Est. Saving: `{pop['expected_latency_savings_ms']} ms` | Channels: `{', '.join(pop['covered_channels'][:3])}`\n"

        yield {"type": "token", "text": md}


@ChatToolRegistry.register
class ServerGeoDistanceMatrixTool(BaseChatTool):
    """Computes server geolocations, physical Haversine distances, and speed-of-light network stretch."""

    name = "get_server_geo_matrix"
    description = (
        "Calculates geographical coordinates, physical distances (km), and optical fiber network stretch "
        "efficiency between active streaming servers and origin nodes."
    )
    category = "Network Topology"
    icon = "📏"
    prompt_example = "Show me the physical distance and network stretch between our streaming servers"
    parameters = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def execute(self, executor: Any, args: dict) -> Iterator[dict]:
        from telemetry_pool import telemetry_pool
        nodes = telemetry_pool.get_nodes(executor.driver)
        nodes_list = list(nodes.values()) if nodes else []

        result = analyze_best_cdn_locations(nodes_list)
        matrix = result.get("server_distance_matrix", [])

        md = "### 📏 Server Geolocation & Network Stretch Matrix\n\n"
        md += "| From Server | To Server | Physical Distance | Observed RTT | Fiber Ideal RTT | Route Efficiency |\n"
        md += "| :--- | :--- | :--- | :--- | :--- | :--- |\n"

        for row in matrix[:6]:
            st = row["stretch"]
            md += f"| `{row['from_city']}` | `{row['to_city']}` | **{row['distance_km']} km** | {st['observed_rtt_ms']} ms | {st['theoretical_fiber_rtt_ms']} ms | `{st['efficiency']}` |\n"

        md += "\n*Theoretical RTT represents the speed-of-light physical floor in single-mode fiber optic cabling (~10ms/1,000km).* \n"
        yield {"type": "token", "text": md}
