"""Skills routes: provides project technical skills, architecture manuals, and operational playbooks
for the ChatBot and the UI Skills dashboard."""

from fastapi import APIRouter

router = APIRouter()

PROJECT_SKILLS = [
    {
        "id": "architecture-blueprint",
        "title": "Stream Graph Architecture & Pipeline Blueprint",
        "icon": "🏗️",
        "category": "Architecture",
        "badge": "Core System",
        "description": "Complete breakdown of the OTT video streaming pipeline (Ingest -> Transcoding -> Distribution). Details the asynchronous spider crawler mechanics, HLS manifest parsing (Sequence #, Target Duration, Discontinuities), and Neo4j real-time state propagation.",
        "prompt": "Explain the complete Stream Graph architecture, node roles, and how spiders trace upstream streams step-by-step.",
        "key_topics": [
            "Video Ingest (MainInput vs BackupLink)",
            "Transcoding & HLS Edge Packaging (PRODUCES/FEEDS)",
            "Asynchronous Upstream Spider Crawlers",
            "Manifest Freshness & TTFB Telemetry"
        ]
    },
    {
        "id": "incident-triage-runbook",
        "title": "Incident Triage & Root Cause Analysis Runbook",
        "icon": "🎯",
        "category": "Operations",
        "badge": "Incident Response",
        "description": "NOC diagnostic playbook for stream failures. Explains graph-aware root-cause ranking algorithms that group correlated node failures, distinguish primary root causes from downstream collateral damage, and trigger automated ICMP traceroutes.",
        "prompt": "Walk me through the Incident Triage Runbook to diagnose current stream outages and find the root cause.",
        "key_topics": [
            "Root Cause Ranking (RCA) Algorithms",
            "Upstream Culprit vs Downstream Impact",
            "Automated ICMP Hop Traceroute Diagnostics",
            "Correlated Failure Clustering"
        ]
    },
    {
        "id": "failover-redundancy-playbook",
        "title": "Failover & High-Availability Playbook",
        "icon": "🛡️",
        "category": "Reliability",
        "badge": "High Availability",
        "description": "Guide to monitoring active failovers and eliminating Single Points of Failure (SPOF). Explains how spiders detect silent MainInput drops, audit channels lacking BackupLink redundancy, and verify automated failover recovery.",
        "prompt": "Check our failover readiness and show channels running without redundant backup links.",
        "key_topics": [
            "Automatic Failover State Detection",
            "Single Point of Failure (SPOF) Audits",
            "BackupLink Quality & Latency Thresholds",
            "Post-Recovery Upstream Switchbacks"
        ]
    },
    {
        "id": "predictive-telemetry-ai",
        "title": "Predictive Failure Risk & Telemetry Analytics",
        "icon": "🔮",
        "category": "Intelligence",
        "badge": "ML Analytics",
        "description": "Statistical anomaly detection engine analyzing rolling 60-ping sliding windows for latency jitter, HLS segment drift, and ICMP drops. Calculates MTBF, MTTR, and predictive failure risk percentages before outages happen.",
        "prompt": "Analyze the predictive failure risk scores and explain which servers have high anomaly signals.",
        "key_topics": [
            "Rolling Z-Score Anomaly Detection",
            "MTBF (Mean Time Between Failures) Scoring",
            "MTTR (Mean Time To Repair) Calculations",
            "Proactive Outage Warning Signals"
        ]
    },
    {
        "id": "neo4j-cypher-schema",
        "title": "Neo4j Graph Database & Cypher Schema Reference",
        "icon": "🕸️",
        "category": "Database",
        "badge": "Graph Schema",
        "description": "Comprehensive reference of node labels (`Domain`, `Channel`, `Spider`, `FinalLink`), relationships (`FEEDS`, `PRODUCES`, `AT`, `WATCHES`), vector search index configurations, and standard Cypher query patterns.",
        "prompt": "Explain the Neo4j graph schema, node properties, and common Cypher queries for this topology.",
        "key_topics": [
            "Domain & Link Node Properties",
            "Directed Flow Semantics (FEEDS, PRODUCES)",
            "Local MiniLM-L6 Vector Embeddings",
            "GraphRAG Subgraph Retrieval Queries"
        ]
    },
    {
        "id": "autonomous-crud-safety",
        "title": "Autonomous Graph CRUD & Double-Confirmation Engine",
        "icon": "⚡",
        "category": "Autonomous AI",
        "badge": "Safety Protocol",
        "description": "Operational manual for LLM autonomous graph mutations (`add_stream_link`, `connect_pipeline_relationship`, `update_stream_link`, `delete_node`). Details the 2-phase confirmation workflow, Blast Radius calculations, and safeguard checks.",
        "prompt": "How does the double-confirmation blast-radius engine protect our streaming topology during mutations?",
        "key_topics": [
            "Two-Phase Confirmation Protocol",
            "Blast Radius Impact Analysis",
            "Severed Inbound/Outbound Edge Audits",
            "Safe Action vs Danger Level Mutations"
        ]
    },
    {
        "id": "cypher-cheat-sheet",
        "title": "Neo4j Cypher Cheat Sheet",
        "icon": "🔎",
        "category": "Graph Query",
        "badge": "Reference",
        "description": "The Cypher syntax the agent uses for its read-only query_graph tool: MATCH patterns, WHERE filters, "
                       "aggregation, paths and subqueries, together with this graph's schema (Domain roles, FEEDS / "
                       "PRODUCES, SpiderRun, ActivityLog). Writes are refused.",
        "prompt": "Using a Cypher query, list every channel's Final server with how many servers feed it.",
        "url": "https://neo4j.com/docs/cypher-manual/current/cheat-sheet/",
        "key_topics": [
            "MATCH / OPTIONAL MATCH patterns",
            "WHERE, EXISTS { } and COUNT { } subqueries",
            "Aggregation without GROUP BY",
            "Read-only: CREATE / MERGE / SET / DELETE refused"
        ]
    }
]


@router.get("/api/skills")
def get_skills():
    """Returns all project skills, operational manuals, and playbooks."""
    return {"count": len(PROJECT_SKILLS), "skills": PROJECT_SKILLS}
