"""Centralized, Type-Safe Configuration for Stream Graph.

Allows developers and operators to tweak all database, spider, network probing,
adaptive crawling tiers, AI models, and telemetry settings in one place.
Loads values from environment variables / .env with robust production defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    def load_dotenv(*args, **kwargs):
        pass


@dataclass(frozen=True)
class Neo4jConfig:
    """Neo4j Database connection and pool settings."""
    uri: str = os.getenv("NEO4J_URI", "neo4j://localhost:7687")
    username: str = os.getenv("NEO4J_USERNAME", "neo4j")
    password: str = os.getenv("NEO4J_LOCAL_PASSWORD") or os.getenv("NEO4J_PASSWORD", "test1234")
    max_connection_pool_size: int = int(os.getenv("NEO4J_POOL_SIZE", "50"))
    connection_acquisition_timeout_s: float = float(os.getenv("NEO4J_TIMEOUT_S", "10.0"))


@dataclass(frozen=True)
class ProbingConfig:
    """Network probing, HLS checks, and concurrency limits."""
    timeout_s: float = float(os.getenv("PROBE_TIMEOUT_S", "5.0"))
    retries: int = int(os.getenv("PROBE_RETRIES", "2"))
    concurrency_limit: int = int(os.getenv("PROBE_CONCURRENCY", "32"))
    stale_after_target_durations: int = int(os.getenv("HLS_STALE_DURATIONS", "3"))
    freshness_grades: Tuple[Tuple[float, str], ...] = (
        (1.5, "FRESH"),
        (2.0, "WARNING"),
        (3.0, "DEGRADED"),
    )
    url_cache_ttl_s: float = float(os.getenv("URL_CACHE_TTL_S", "3.0"))
    icmp_cache_ttl_s: float = float(os.getenv("ICMP_CACHE_TTL_S", "5.0"))


@dataclass(frozen=True)
class AdaptiveTiersConfig:
    """Adaptive spider crawl intervals based on stream health."""
    default_interval_s: int = int(os.getenv("DEFAULT_INTERVAL_S", "30"))
    min_interval_s: int = int(os.getenv("MIN_INTERVAL_S", "10"))
    max_interval_s: int = int(os.getenv("MAX_INTERVAL_S", "3600"))
    tier_turbo_s: int = int(os.getenv("TIER_TURBO_S", "5"))     # Active outage
    tier_urgent_s: int = int(os.getenv("TIER_URGENT_S", "10"))   # Running on backup SPOF
    tier_watch_s: int = int(os.getenv("TIER_WATCH_S", "15"))     # Recent incident in 24h
    tier_relaxed_s: int = int(os.getenv("TIER_RELAXED_S", "75")) # 100% uptime for 7+ days


@dataclass(frozen=True)
class AIConfig:
    """ChatBot, GraphRAG, and OpenRouter API configurations."""
    openrouter_api_key: Optional[str] = os.getenv("OPENROUTER_API_KEY")
    openrouter_api_keys: List[str] = field(
        default_factory=lambda: [k.strip() for k in (os.getenv("OPENROUTER_API_KEYS") or "").split(",") if k.strip()]
    )
    chatbot_model: str = os.getenv("CHATBOT_MODEL", "qwen/qwen3.8-27b")
    rca_model: Optional[str] = os.getenv("RCA_MODEL") or os.getenv("CHATBOT_MODEL", "qwen/qwen3.8-27b")
    openrouter_base_url: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    key_cooldown_s: float = float(os.getenv("KEY_COOLDOWN_S", "60.0"))
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")


@dataclass(frozen=True)
class LangSmithConfig:
    """LangSmith AI Tracing and Observability."""
    enabled: bool = os.getenv("LANGSMITH_TRACING", "").lower() in ("true", "1", "yes")
    api_key: Optional[str] = os.getenv("LANGSMITH_API_KEY")
    project: str = os.getenv("LANGSMITH_PROJECT", "stream-graph")
    endpoint: str = os.getenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")


@dataclass(frozen=True)
class TelemetryPoolConfig:
    """In-memory Data Pool caching settings."""
    ttl_s: float = float(os.getenv("TELEMETRY_POOL_TTL_S", "5.0"))


@dataclass(frozen=True)
class StreamGraphConfig:
    """Master Application Configuration."""
    neo4j: Neo4jConfig = field(default_factory=Neo4jConfig)
    probing: ProbingConfig = field(default_factory=ProbingConfig)
    adaptive: AdaptiveTiersConfig = field(default_factory=AdaptiveTiersConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    langsmith: LangSmithConfig = field(default_factory=LangSmithConfig)
    pool: TelemetryPoolConfig = field(default_factory=TelemetryPoolConfig)

    # Server paths
    web_dir: Path = Path(__file__).parent / "web"
    state_dir: Path = Path(os.getenv("STATE_DIR", Path(__file__).parent / "state"))

    @classmethod
    def load(cls) -> StreamGraphConfig:
        """Create a fresh configuration instance synced with environment."""
        load_dotenv()  # .env fills in what isn't set; it never overrides the environment (Docker, tests)
        return cls()


# Global config instance ready for import anywhere in the codebase
config = StreamGraphConfig.load()
