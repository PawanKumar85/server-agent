# 📡 Stream Graph

Autonomous Live Video Streaming Monitoring, GraphRAG Intelligence & Failure Diagnostics Platform.

Built for OTT broadcast operations with Neo4j graph topology, automated spider crawlers, real-time HLS telemetry, and an autonomous AI Agent powered by GraphRAG and multi-key OpenRouter rotation.

---

## Quick Start

### 1. Configure Environment
Copy the example environment configuration and add your credentials:
```bash
cp .env.example .env
```
Key settings in `.env`:
* `NEO4J_LOCAL_PASSWORD`: Local Neo4j container database password.
* `APP_LOGIN_EMAIL` / `APP_LOGIN_PASSWORD`: Credentials for the web dashboard (set your own; use a strong password).
* `OPENROUTER_API_KEYS`: Comma-separated OpenRouter keys for **Strategy B (Round-Robin Active-Active)** rotation with automatic failover.

### 2. Launch Services
Run both the Neo4j Community instance and the FastAPI web service:
```bash
docker compose up -d --build
```

### 3. Open the Dashboard
Navigate to [http://localhost:8080](http://localhost:8080) and sign in.

Direct deep-links:
* **Topology Graph**: [http://localhost:8080/](http://localhost:8080/)
* **Spiders (Channel Crawlers)**: [http://localhost:8080/spiders](http://localhost:8080/spiders)
* **Stream Links**: [http://localhost:8080/links](http://localhost:8080/links)
* **Pipeline Relationships**: [http://localhost:8080/relationships](http://localhost:8080/relationships)
* **Autonomous Agent**: [http://localhost:8080/agent](http://localhost:8080/agent)
* **Agent Tools**: [http://localhost:8080/agent/tools](http://localhost:8080/agent/tools)
* **Agent Skills**: [http://localhost:8080/agent/skills](http://localhost:8080/agent/skills)

---

## Testing & Quality
Run the automated test suite locally:
```bash
.venv/bin/pytest tests/test_chatbot.py
```

---

## Architecture & Codebase Map
See [ARCHITECTURE.md](ARCHITECTURE.md) for a detailed breakdown of all directories, API routes, telemetry engines, and AI models.
