# Stream Graph — System Architecture & Project Organization

Stream Graph is an autonomous telemetry, monitoring, GraphRAG analysis, and root cause diagnosis platform for OTT live video streaming pipelines.

---

## Directory Structure

```plaintext
ping/
├── server.py               # Main FastAPI backend, WebSocket/SSE streams, scheduler
├── Dockerfile              # Container definition (Python 3.12-slim + baked MiniLM ONNX)
├── docker-compose.yml      # Service orchestration (app + local Neo4j 2026.09.0)
├── requirements.txt        # Python production dependencies
├── pytest.ini              # Test runner configuration
├── .env.example            # Environment template
├── .gitignore              # Git ignore rules for state, virtualenvs & artifacts
│
├── routes/                 # Modular FastAPI route controllers
│   ├── pages.py            # Deep-linked HTML5 page routes (/, /spiders, /agent, etc.)
│   ├── chat.py             # Agent SSE stream, tool executions & keypool status
│   ├── monitor.py          # Auto-ping scheduler, spider crawls, live SSE broadcast
│   ├── diagnostics.py      # Hop-by-hop traceroutes, failover audits, early warnings
│   ├── reports.py          # Executive HTML report generator & Excel workbook exports
│   └── skills.py           # Playbooks, HLS specifications & architecture manuals
│
├── web/                    # Static frontend (HTML5 / Vanilla CSS / Modern JS)
│   ├── index.html          # SPA dashboard shell, subnavs, Agent UI & Context Inspector
│   ├── styles.css          # Glassmorphism dark-mode design system & animations
│   ├── core.js             # API helper, routing & UI event handlers
│   ├── chat.js             # Agent streaming client, tool actions & Context Inspector
│   ├── graph.js            # Force-directed interactive Neo4j SVG network topology
│   ├── spiders.js          # Spider crawler run controllers & tally indicators
│   ├── links.js            # Stream link management & 2-phase mutations
│   ├── relationships.js    # FEEDS / PRODUCES pipeline relationship manager
│   ├── runs.js             # Real-time incident logs & audit trails
│   ├── heatmap.js          # Latency & error density grid
│   └── agent-briefing.js   # Live network operational status brief
│
├── tests/                  # Automated test suite (Pytest)
│   ├── conftest.py         # Shared fixtures (mock Neo4j drivers, temporary stores)
│   ├── test_chatbot.py     # Agent tests (KeyPool Strategy B, GraphRAG, failover)
│   ├── test_server.py      # HTTP routes, auth, scheduler & WebSocket tests
│   └── test_spider.py      # HLS crawler, playlist parser & retry logic tests
│
├── scripts/                # Operational and maintenance utilities
│   ├── copy_from_aura.py   # One-off script to migrate graph from Neo4j Aura to local
│   └── crontab.txt         # Weekly cron job definition for embedding sync
│
├── notebooks/              # Research & prototyping notebooks
│   └── ping.ipynb          # Pipeline data model prototyping & Neo4j upsert tests
│
├── backups/                # Backup archives and snapshot dumps
│   └── ping_backup_*.zip   # Legacy snapshot archives
│
├── state/                  # Runtime state directory (isolated volume in Docker)
│   ├── metrics.db          # SQLite telemetry & incident log store (git-ignored)
│   └── scheduler.json      # Auto-ping intervals & scheduler configuration
│
└── .agents/                # Antigravity agent configuration and operational skills
    └── skills/
        └── stream-graph-system/   # Complete system architecture and operations manual
```

---

## Python Domain Modules

### 1. Telemetry & Monitoring
* **[`spider.py`](file:///Users/pawankumar/Desktop/System-eng/ping/spider.py)**: Upstream spider crawling engine. Fetches HLS manifests, validates `.m3u8` playlists, measures hop latency, and detects blips.
* **[`nodes.py`](file:///Users/pawankumar/Desktop/System-eng/ping/nodes.py)**: Graph node schema, channel chain models, and Neo4j Cypher upsert operations (`MainInput`, `BackupLink`, `Transcoding`, `FinalLink`).
* **[`metrics.py`](file:///Users/pawankumar/Desktop/System-eng/ping/metrics.py)**: SQLite metrics store, EWMA baseline drift calculators, z-score anomaly detectors, and log retention pruning.
* **[`health.py`](file:///Users/pawankumar/Desktop/System-eng/ping/health.py)**: HTTP latency, SSL certificate verification, and connectivity probes.
* **[`traceroute.py`](file:///Users/pawankumar/Desktop/System-eng/ping/traceroute.py)**: Hop-by-hop network routing diagnosis and packet loss analyzer.

### 2. Autonomous Agent & GraphRAG
* **[`chatbot.py`](file:///Users/pawankumar/Desktop/System-eng/ping/chatbot.py)**: Agent core with **Strategy B: KeyPool Round-Robin Rotation (Active-Active)** across multiple OpenRouter keys, automated failover on 429/402, and SSE response streaming.
* **[`tools.py`](file:///Users/pawankumar/Desktop/System-eng/ping/tools.py)**: 14 autonomous tools (reporting, diagnostics, risk forecasting, and 2-phase confirmed graph mutations).
* **[`graphrag.py`](file:///Users/pawankumar/Desktop/System-eng/ping/graphrag.py)**: Hybrid GraphRAG combining Neo4j vector cosine search with graph walks (`FEEDS`, `PRODUCES`).
* **[`text_embedding.py`](file:///Users/pawankumar/Desktop/System-eng/ping/text_embedding.py)** & **[`embeddings.py`](file:///Users/pawankumar/Desktop/System-eng/ping/embeddings.py)**: Local MiniLM (384-dim) ONNX embedding generation and Neo4j vector index synchronization.

### 3. Root Cause Analysis (RCA) & Intelligence
* **[`rca_rank.py`](file:///Users/pawankumar/Desktop/System-eng/ping/rca_rank.py)**: Topological RCA engine calculating fault propagation probabilities across stream channels.
* **[`noc_rca.py`](file:///Users/pawankumar/Desktop/System-eng/ping/noc_rca.py)**: LLM-generated NOC operator incident briefings and failure narratives.
* **[`predictor.py`](file:///Users/pawankumar/Desktop/System-eng/ping/predictor.py)**: Failure risk forecaster, MTBF/MTTR estimation, and anomaly correlations.
* **[`heatmap.py`](file:///Users/pawankumar/Desktop/System-eng/ping/heatmap.py)**: Latency and error density matrix calculator.
* **[`report.py`](file:///Users/pawankumar/Desktop/System-eng/ping/report.py)** & **[`report_analysis.py`](file:///Users/pawankumar/Desktop/System-eng/ping/report_analysis.py)**: Executive HTML reporting engine with embedded SVG performance charts and LLM executive summaries.
* **[`export.py`](file:///Users/pawankumar/Desktop/System-eng/ping/export.py)**: Excel workbook exporter (`.xlsx`) for offline audits.
* **[`auth.py`](file:///Users/pawankumar/Desktop/System-eng/ping/auth.py)**: Session authentication and route protection middleware.

---

## System Requirements

Stream Graph runs as two Docker containers (`app` and `neo4j`, see `docker-compose.yml`). The figures below were
measured on 2026-10-02 with 19 servers and 9 channels.

### Measured usage

| Component | RAM | Disk |
|---|---|---|
| App container (FastAPI, spiders, ONNX embeddings) | 300–510 MB; brief peaks while generating reports | 932 MB image |
| Neo4j 2026.09 (capped in `docker-compose.yml`: 512 MB heap, 256 MB page cache) | ~920 MB | 1.13 GB image + ~410 MB data |
| History & learning DB (`state/metrics.db`) and nightly backups (`backups/`, last 7 kept) | — | ~30 MB in total |
| CPU | ~2–5 % idle | short spikes for reports, AI analysis and embedding syncs |

### Minimum

| | Minimum |
|---|---|
| CPU | 2 cores, 64-bit (x86-64 or ARM64 / Apple Silicon) |
| RAM | 4 GB on macOS / Windows (Docker Desktop's VM adds ~1 GB); 3 GB on a Linux host |
| Free disk | 5 GB |
| OS | macOS 12+, Windows 10/11 with WSL2, or 64-bit Linux, with Docker and Docker Compose |
| Network | Outbound HTTP/HTTPS to the monitored stream servers; ICMP allowed (latency, packet loss, traceroute) |

### Recommended

| | Recommended |
|---|---|
| CPU | 4 cores |
| RAM | 8 GB |
| Free disk | 10–20 GB (image updates, 14 days of history, backups) |
| Network | Stable wired or good Wi-Fi link; a fixed LAN address when other devices open the dashboard on port 8080 |

### Optional

* **ChatBot and AI analysis**: outbound access to OpenRouter and an API key (`OPENROUTER_API_KEY(S)`). Monitoring works without them.
* **Test suite**: ~1 GB extra RAM while the throwaway test database runs (`docker compose --profile test up -d neo4j-test`).
* **Dashboard clients**: any current Chrome, Edge, Firefox or Safari, desktop or mobile. Chart.js and hls.js are served locally, so no internet access is needed to view it.

### Scaling

Resource use grows with the number of stream URLs checked, not with the number of dashboard viewers.

* Up to ~50 servers / 25 channels: the minimum specification is enough.
* 100+ servers: use the recommended specification. History takes about 1 MB per stream URL after the 48-hour raw window is compacted into 5-minute rollups.
* Thousands of servers: raise the Neo4j heap and page-cache limits in `docker-compose.yml`.
