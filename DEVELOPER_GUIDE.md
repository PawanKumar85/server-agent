# Stream Graph Developer & Extensibility Guide

Welcome to **Stream Graph**! This guide is written so that anyone can pull this repository, customize ("tweak") any subsystem, add new protocols or AI tools, and adapt it to their infrastructure using clean, class-based Object-Oriented design patterns.

---

## 1. Architecture & Design Patterns

The codebase is structured around **SOLID design principles**:
- **Type-Safe Configuration**: Encapsulated in typed `@dataclass` classes ([`config.py`](config.py)), eliminating hardcoded strings and scattered `os.getenv` calls.
- **Open-Closed Principle (OCP)**: Subsystems are open for extension without modifying core dispatchers.
- **Abstract Base Classes & Registries**:
  - **Network & Stream Probing**: [`BaseStreamProbe`](probes.py) + [`ProbeRegistry`](probes.py).
  - **AI Diagnostic Tooling**: [`BaseChatTool`](tools_registry.py) + [`ChatToolRegistry`](tools_registry.py).

---

## 2. Centralized Configuration Hierarchy (`config.py`)

All configuration parameters are organized into modular, typed dataclasses. In your custom code or scripts, import `config`:

```python
from config import config

# Access typed configurations with full IDE autocomplete:
print(config.neo4j.uri)
print(config.probing.hls_timeout_seconds)
print(config.tiers.critical_interval_seconds)
print(config.ai.primary_model)
```

### Configuration Dataclasses:
| Config Class | Responsibility | Key Attributes / Defaults |
| :--- | :--- | :--- |
| `Neo4jConfig` | Database connection & pooling | `uri`, `username`, `password`, `max_connection_pool_size` (50) |
| `ProbingConfig` | Network & stream polling limits | `hls_timeout_seconds` (5.0), `http_timeout_seconds` (5.0), `icmp_packet_count` (3) |
| `AdaptiveTiersConfig`| Dynamic spider crawl intervals | `critical_interval_seconds` (10), `degraded_interval_seconds` (30), `healthy_interval_seconds` (120) |
| `AIConfig` | LLM & GraphRAG parameters | `primary_model` ("gpt-4o-mini"), `max_tool_iterations` (10), `history_window` (12) |
| `LangSmithConfig` | LangSmith LLM tracing | `enabled`, `api_key`, `project`, `endpoint` |
| `TelemetryPoolConfig` | Concurrent worker bounds | `max_workers` (8), `task_timeout_seconds` (15.0) |

To tweak settings in production or local development, set the corresponding variable in your `.env` file (e.g. `NEO4J_POOL_SIZE=100`, `ADAPTIVE_CRITICAL_INTERVAL_SEC=5`).

---

## 3. Adding New Stream & Network Probes (`probes.py`)

Stream Graph uses an extensible probe registry. Adding support for a new protocol (e.g., MPEG-DASH, SRT, WebRTC, RTMP) requires **zero changes** to existing probing logic.

### Example: Adding an MPEG-DASH Probe
Create your probe class inheriting from `BaseStreamProbe` and decorate it with `@ProbeRegistry.register`:

```python
from probes import BaseStreamProbe, ProbeRegistry, ProbeResult

@ProbeRegistry.register("DASH", "MPD")
class DashStreamProbe(BaseStreamProbe):
    """Probes MPEG-DASH MPD manifests and checks stream health."""

    async def check(self, target_url: str, timeout_seconds: float = 5.0) -> ProbeResult:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                resp = await client.get(target_url)
                healthy = resp.status_code == 200 and "<MPD" in resp.text
                return ProbeResult(
                    protocol="DASH",
                    target_url=target_url,
                    healthy=healthy,
                    latency_ms=resp.elapsed.total_seconds() * 1000,
                    status_code=resp.status_code,
                    details={"is_dynamic": 'type="dynamic"' in resp.text}
                )
        except Exception as exc:
            return ProbeResult(
                protocol="DASH",
                target_url=target_url,
                healthy=False,
                latency_ms=0.0,
                error=str(exc)
            )
```

Now, any spider or telemetry worker can dynamically probe DASH streams:
```python
probe = ProbeRegistry.get("DASH")
result = await probe.check("https://live.example.com/manifest.mpd")
```

---

## 4. Adding Custom AI Diagnostic Tools (`tools_registry.py`)

The ChatBot AI Agent automatically dispatches tools registered in `ChatToolRegistry`. When you add a new tool:
1. It automatically appears in the LLM tool definition schema sent to OpenAI/Anthropic/Gemini.
2. It is automatically registered in the UI frontend chips (`/api/tools`).
3. It is automatically routed when the LLM triggers a tool call.

### Example: Adding a Custom Tool
Inherit from `BaseChatTool` and decorate with `@ChatToolRegistry.register`:

```python
from typing import Any, Dict
from tools_registry import BaseChatTool, ChatToolRegistry

@ChatToolRegistry.register
class TranscoderStatsTool(BaseChatTool):
    """Fetches real-time GPU/CPU load on video transcoders."""

    @property
    def name(self) -> str:
        return "get_transcoder_stats"

    @property
    def description(self) -> str:
        return "Retrieve CPU and GPU encoder load across streaming cluster nodes."

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "cluster_region": {
                    "type": "string",
                    "description": "Region code, e.g. us-east, eu-west."
                }
            },
            "required": ["cluster_region"]
        }

    @property
    def category(self) -> str:
        return "Infrastructure"

    @property
    def display_name(self) -> str:
        return "Transcoder Stats"

    async def execute(self, arguments: Dict[str, Any], driver=None, context=None) -> Dict[str, Any]:
        region = arguments.get("cluster_region", "default")
        # Query your infrastructure or Neo4j driver here:
        return {
            "region": region,
            "gpu_utilization_pct": 42.5,
            "active_encoding_pipelines": 18,
            "status": "HEALTHY"
        }
```

That's it! The LLM will now understand when to use `get_transcoder_stats` during live debugging sessions.

---

## 5. Running Tests

Unit tests for class extensibility and regression testing are located under `tests/`:

```bash
# Run all unit tests:
python3 -m unittest discover tests

# Run class extensibility test suite:
python3 -m unittest tests/test_classes_extensibility.py
```

---

## 6. Docker Deployment

When tweaking Python code locally or on a production host, rebuild and start the container:

```bash
docker compose up -d --build app
```
The app will bind to `http://localhost:8080` with Neo4j running on port `7687` and browser on `7474`.
