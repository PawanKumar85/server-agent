---
name: stream-graph-system
description: >-
  Complete operational manual, architecture blueprint, telemetry pipeline, Neo4j schema,
  and autonomous tool specifications for the Stream Graph (OTT Live video streaming monitoring platform).
  Use this skill whenever asked about the system design, network hops, HLS metrics, spider walkers,
  RCA ranking, failover mechanics, deduplication caching, adaptive polling, or graph database mutations.
---

# Stream Graph: Architecture & Pipeline Blueprint

## 1. High-Level System Architecture & Delivery Pipeline
Stream Graph is an autonomous telemetry, graph topology mapping, and root cause analysis (RCA) platform purpose-built for live OTT video streaming pipelines (HLS/HTTP/ICMP).

```
   [Video Ingest / Source]  -------->  [Transcoder Cluster]  -------->  [Edge / CDN / Origin]  -------->  [Client / Player]
        (MainInput)                           |                              (FinalLink)                  (Spider Crawlers)
        (BackupLink)                          v                                   ^
              \-----------------------> [Transcoding] ----------------------------/
```

### Core Architecture Layers:
1. **Spider Walkers (`spider.py`)**:
   - Independent crawler instances tied to each channel's `FinalLink`.
   - Walks upstream against the media flow (`FinalLink` -> `Transcoding` -> `MainInput`/`BackupLink`) or downstream (`MainInput` -> `Transcoding` -> `FinalLink`).
   - Pinpoints root cause failures at the exact point in the topological chain where dependencies broke.
2. **Graph Database Engine (Neo4j)**:
   - Stores topology, dynamic health properties, URL states, and spider locations in real time.
3. **Telemetry & L7 Media Inspector (`health.py`)**:
   - Multi-tier inspector verifying HTTP status, TLS latency, ABR bitrate ladders, video resolutions, discontinuity counts, and CDN cache headers.
4. **Shared Health & Deduplication Cache**:
   - Eliminates redundant probes across spiders, cycles, and shared servers using TTL caches and in-flight request coalescing.
5. **Adaptive Polling Engine**:
   - Evaluates end-to-end health (FinalLink + MainInput); relaxes intermediate node checks to heartbeat cadence when delivery is healthy, with instant fallback to deep walks on fault.
6. **Self-Learning & Anomaly Intelligence (`metrics.py`, `learning.py`)**:
   - Historical MTTR (Mean Time to Recovery) profiling by failure bucket, anomaly scoring (Z-scores across 6-hour windows), and channel alert tuning.
7. **Network Transit Diagnostics (`traceroute.py`)**:
   - UDP/ICMP multi-hop traceroute with autonomous ISP/ASN backbone attribution (Airtel, Tata, OVHcloud, Cloudflare, AWS, Google Cloud).
8. **NOC RCA & Incident Correlator (`noc_rca.py`, `rca_rank.py`)**:
   - Differentiates root causes from downstream cascade failures (`LOCAL_TO_NODE`, `TRANSCODER`, `SHARED_UPSTREAM`, `FINAL_ORIGIN`).
9. **GraphRAG & Operator Chatbot (`graphrag.py`, `chatbot.py`, `tools.py`)**:
   - Local FastEmbed vectors in Neo4j; 14 autonomous tools supporting zero-shot reporting, diagnostic execution, and 2-phase double-confirmed mutations.

---

## 2. Graph Schema & Relationship Semantics (Neo4j)

### Node Labels
* `Domain`: Primary infrastructure unit (e.g. `cloud1.ottlive.co.in`, `transcoder-01`).
* `FinalLink`: Client/player-facing endpoint delivering a specific channel.
* `Transcoding`: Transcoder or packager instance encoding/repackaging streams.
* `MainInput`: Primary ingest feed or contribution stream.
* `BackupLink`: Secondary/redundant failover input feed.
* `SpiderRun`: Crawler state marker storing current node position, direction, and RCA report.

### Relationships
* `(:Domain)-[:FEEDS]->(:Domain)`: Media flow from input (Main/Backup) to transcoding/relay.
* `(:Domain)-[:PRODUCES]->(:Domain)`: Media flow from transcoder to final distribution.
* `(:SpiderRun)-[:AT]->(:Domain)`: Current physical location of the crawler spider.
* `(:SpiderRun)-[:WATCHES]->(:Domain)`: Association between spider and its assigned `FinalLink`.

### Node Properties
```cypher
n.status               // UP | DOWN | DEGRADED
n.lastPing             // Datetime of latest health evaluation
n.lastLatencyMs        // Round-trip HTTP/TCP latency (ms)
n.lastRttMs            // ICMP average RTT (ms)
n.lastJitterMs         // ICMP jitter (ms)
n.lastPacketLoss       // ICMP packet loss percentage (0.0 - 1.0)
n.consecutiveFailures  // Successive failure count (triggers incident when >= INCIDENT_AFTER_FAILURES)
n.consecutiveSuccesses // Successive pass count (recovers when >= RECOVER_AFTER_SUCCESSES)
n.lastRecovery         // Datetime of last DOWN -> UP recovery transition
n.urlHealth            // Serialized JSON map containing deep URL health details
```

---

## 3. Spider Walkers & Traversal Algorithm

### Spider Legs & Navigation
Each monitoring cycle, spiders evaluate their leg and direction:
1. **Upstream Leg (`walk_upstream`)**:
   - Starts at `FinalLink` and visits upstream towards `MainInput`/`BackupLink`.
   - Halts at the most upstream failed node whose dependencies are healthy (Root Cause).
   - If failover is detected (MainInput DOWN, BackupLink UP), logs `Failover AVAILABLE` and remains `RUNNING`.
2. **Downstream Leg (`walk_downstream`)**:
   - Walks from the ingest/transcoder back to `FinalLink`, verifying downstream pipeline continuity.
3. **Recovery Verification**:
   - If previously `STOPPED` or `ERROR`, re-evaluates upstream from `FinalLink` to verify end-to-end recovery.

---

## 4. Deep Telemetry & L7 Media Extraction Pipeline

Every URL check on a node extracts deep L7 metrics:

### 1. HLS Manifest & Stream Analysis (`check_hls_url`)
* **ABR Bitrate Ladder**: Extracts variant `bandwidth` attributes (e.g. `[3210000, 1800000]`).
* **Variant Resolutions**: Extracts dimensions (e.g. `['1280x720', '640x360']`).
* **Discontinuities**: Counts `#EXT-X-DISCONTINUITY` tags across media playlists; warns of stream resets, encoder re-inits, or ad insertions.
* **Target Duration Drift**: Compares segment duration against `#EXT-X-TARGETDURATION`.
* **Sequence Stall Detection (`apply_sequence_stall`)**: Flags streams where sequence number is stuck across multiple checks.
* **Late Clock Tolerance (`apply_clock_offset`)**: Tolerates encoders whose clock is offset from real-time without false-alarm flagging.

### 2. CDN & Server Intelligence
* **CDN Cache Status**: Captures `X-Cache`, `CF-Cache-Status`, and `X-Proxy-Cache` headers (`HIT`, `MISS`, `EXPIRED`).
* **Origin Server Software**: Captures the `Server` response header (e.g., `nginx/1.24.0`, `cloudflare`).

---

## 5. Multi-Level Deduplication & Shared Health Cache

Prevents redundant network probes, DDoS self-effects, and CDN rate-limits (HTTP 429):

```
[Request Node / URL]
        |
        v
[Is in Cache & Fresh (< TTL) & Healthy (UP)?]
       /                                   \
   (YES)                                   (NO)
     |                                       |
Return Cached Result                  [In-Flight Probe Pending?]
(0 Network Calls)                            /                \
                                         (YES)                (NO)
                                           |                    |
                                     Await In-Flight      Execute Physical Probe
                                     Shared Future        Update Cache & Return
```

1. **Cross-Spider Node Cache (`_GLOBAL_NODE_CACHE`)**:
   - When any spider checks a node and it is UP, caches result for `HEALTH_CACHE_TTL_S` (Default: 15s).
   - Concurrent or subsequent spiders visiting the same node reuse the cached health.
2. **L7 URL Deduplication (`_URL_CACHE`, `_URL_IN_FLIGHT`)**:
   - Caches parsed HLS playlist results; coalesces concurrent requests to the same URL.
3. **Host-Level ICMP Coalescing (`_ICMP_CACHE`, `_ICMP_IN_FLIGHT`)**:
   - When multiple channels share the same host IP, only 1 ICMP ping runs at a time; results are shared.
4. **Zero-Latency Failure Bypass**:
   - If a node or URL is DOWN, cache is immediately bypassed or evicted so outages and recoveries are confirmed fresh.

---

## 6. Adaptive Polling & Smart Intermediate Relaxation

Eliminates 70–85% of redundant network load while preserving 100% RCA accuracy:

1. **End-to-End Operational Verification**:
   - When a spider visits `FinalLink` and finds it UP, it peeks the primary `MainInput`.
   - If `FinalLink` is UP AND `MainInput` is UP, the end-to-end media pipeline is actively functioning.
2. **Intermediate Node Relaxation**:
   - Intermediate transcoders and backup links shift to `INTERMEDIATE_CADENCE_S` (Default: 60s) instead of aggressive rapid polling.
   - If checked within 60s, the spider records the step from cache without new network traffic.
3. **Instant Fault Disengagement**:
   - If `FinalLink` drops OR `MainInput` drops OR telemetry drifts, adaptive relaxation **immediately disengages**.
   - The spider executes a complete deep walk through every intermediate node to pinpoint the root cause.
4. **Full Path Integrity**:
   - `report.rca.path` retains every step in the pipeline, ensuring complete visibility in the Web UI and RCA reports.

---

## 7. Transit ISP / ASN Network Attribution

Per-hop traceroute diagnostics map network bottlenecks to autonomous backbones:

| Provider | AS Number | Identification Subnets / Signatures |
| :--- | :--- | :--- |
| **OVHcloud** | AS16276 | `51.0.0.0/8`, `141.94.0.0/16`, `147.135.0.0/16`, `198.27.64.0/18` |
| **Bharti Airtel** | AS9498 | `182.64.0.0/12`, `125.16.0.0/12`, `122.160.0.0/13` |
| **Tata Communications** | AS4755 | `180.144.0.0/13`, `115.112.0.0/14`, `203.197.0.0/16` |
| **Cloudflare** | AS13335 | `104.16.0.0/13`, `104.24.0.0/14`, `172.64.0.0/13`, `1.1.1.0/24` |
| **Amazon Web Services (AWS)** | AS16509 | `3.0.0.0/8`, `13.32.0.0/15`, `52.0.0.0/8`, `54.0.0.0/8`, `99.84.0.0/16` |
| **Google Cloud (GCP)** | AS15169 | `34.0.0.0/8`, `35.184.0.0/13`, `8.8.8.0/24` |

* **Root Cause Attribution**: Allows the NOC to immediately declare whether an outage is an Origin server failure or an upstream ISP routing drop.

---

## 8. Self-Learning, Anomaly Intelligence & MTTR Profiling

* **MTTR by Failure Category (`mttr_by_category`)**:
  - Queries historical incidents in SQLite `metrics.db`.
  - Computes `median_s`, `avg_s`, `min_s`, and `max_s` per failure type (`STALE_MEDIA`, `PLAYLIST_MISSING`, `UNREACHABLE`, `SERVER_ERROR`).
  - Differentiates self-healing encoder blips (median ~46s) from manual intervention outages (>300s).
* **Statistical Anomaly Baseline**:
  - Maintains 6-hour rolling window per node for latency, segment age, and packet loss.
  - Scores anomalies (0–10) and flags warnings when metrics drift `z >= 4` standard deviations while still UP.
* **Channel History & Auto-Tuning**:
  - Channels with frequent segment timestamp jumps (e.g. clock drift) learn baseline offsets (`ageBaseline`) to eliminate false alerts.

---

## 9. Graph Mutation Rules & Strict 2-Phase Safety Verification

All pipeline modifications through autonomous AI tools require a two-phase preview and explicit confirmation:

| Tool | Action | Safety Pre-Check |
| :--- | :--- | :--- |
| `add_stream_link` | Register URL & Channel on Domain | Validates URL scheme, domain syntax, and pipeline role. |
| `connect_pipeline_relationship` | Connect `FEEDS` / `PRODUCES` edge | Validates topology role pairs against `EDGE_SHAPES`. |
| `update_stream_link` | Modify existing URL, Channel, Role | Generates field-by-field diff preview before committing. |
| `delete_node` | Permanently remove node & edges | Computes Blast Radius: severed inbound/outbound links, impacted channels, downstream broken paths. |

---

## 10. Tunable Operational Configurations & Environment Matrix

Configure runtime behaviors via environment variables in `.env`:

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `HEALTH_CACHE_ENABLED` | `1` | Master switch for multi-level deduplication and caching. |
| `HEALTH_CACHE_TTL_S` | `15.0` | Cross-spider and cross-cycle node health cache TTL (seconds). |
| `ADAPTIVE_POLLING` | `1` | Enables end-to-end adaptive intermediate node relaxation. |
| `INTERMEDIATE_CADENCE_S`| `60.0` | Heartbeat polling interval for intermediate transcoders when stream is healthy. |
| `URL_CACHE_TTL_S` | `15.0` | HTTP/HLS manifest check deduplication window (seconds). |
| `ICMP_CACHE_TTL_S` | `15.0` | Host-level ICMP ping deduplication window (seconds). |
| `HEALTH_SWEEP` | `1` | Enables full-graph sweep of unvisited prefetched nodes. |
| `INCIDENT_AFTER_FAILURES`| `2` | Number of consecutive failed checks before opening an incident. |
| `RECOVER_AFTER_SUCCESSES`| `2` | Number of consecutive healthy checks before resolving an incident. |
| `ALERT_CONSECUTIVE_THRESHOLD` | `10` | Threshold of consecutive failures before escalating critical alerts. |
| `CONCURRENCY` | `32` | Maximum concurrent HTTP connections for the health inspector. |
