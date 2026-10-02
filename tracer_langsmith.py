"""LangSmith Tracing Integration for Stream Graph AI & GraphRAG.

Provides zero-overhead, production-safe tracing for:
- GraphRAG subgraph expansion and vector searches
- ChatBot LLM prompts, latency, and streaming responses
- Autonomous tool execution (diagnose_hls_stream, query_graph, etc.)

When LANGSMITH_API_KEY is not set or langsmith is not installed, all tracing
decorators cleanly degrade into zero-overhead pass-throughs.
"""

from __future__ import annotations

import functools
import logging
import os
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("langsmith_tracer")

_LANGSMITH_AVAILABLE = False
_traceable_internal = None

# Check if tracing is enabled via environment variables
LANGSMITH_TRACING = os.getenv("LANGSMITH_TRACING", "").lower() in ("true", "1", "yes") or \
                    os.getenv("LANGCHAIN_TRACING_V2", "").lower() in ("true", "1", "yes")
LANGSMITH_API_KEY = os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY")
LANGSMITH_PROJECT = os.getenv("LANGSMITH_PROJECT") or os.getenv("LANGCHAIN_PROJECT") or "stream-graph"
LANGSMITH_ENDPOINT = os.getenv("LANGSMITH_ENDPOINT") or os.getenv("LANGCHAIN_ENDPOINT") or "https://api.smith.langchain.com"

# Sync environment variables for langsmith SDK internal detection
if LANGSMITH_API_KEY:
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = LANGSMITH_API_KEY
    os.environ["LANGSMITH_PROJECT"] = LANGSMITH_PROJECT
    os.environ["LANGSMITH_ENDPOINT"] = LANGSMITH_ENDPOINT
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_API_KEY"] = LANGSMITH_API_KEY
    os.environ["LANGCHAIN_PROJECT"] = LANGSMITH_PROJECT

try:
    if LANGSMITH_API_KEY:
        from langsmith import traceable as ls_traceable  # type: ignore
        _traceable_internal = ls_traceable
        _LANGSMITH_AVAILABLE = True
        logger.info(f"LangSmith Tracing active: project='{LANGSMITH_PROJECT}'")
except ImportError:
    logger.debug("langsmith package not installed; tracing will run in pass-through mode.")
except Exception as e:
    logger.warning(f"Failed to initialize langsmith: {e}")


def is_langsmith_enabled() -> bool:
    """Check if LangSmith tracing is currently configured and active."""
    return bool(_LANGSMITH_AVAILABLE and LANGSMITH_API_KEY)


def get_langsmith_status() -> dict:
    """Operational status of the LangSmith telemetry tracer."""
    return {
        "enabled": is_langsmith_enabled(),
        "project": LANGSMITH_PROJECT,
        "endpoint": LANGSMITH_ENDPOINT,
        "has_api_key": bool(LANGSMITH_API_KEY),
    }


def traceable(name: Optional[str] = None, run_type: str = "chain", **trace_kwargs: Any) -> Callable:
    """Safe decorator that traces functions in LangSmith if active, or passes through cleanly."""
    def decorator(fn: Callable) -> Callable:
        if _LANGSMITH_AVAILABLE and _traceable_internal is not None:
            wrapped = _traceable_internal(name=name or fn.__name__, run_type=run_type, **trace_kwargs)(fn)
            return wrapped

        # Pass-through when langsmith is not configured
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return fn(*args, **kwargs)
        return wrapper
    return decorator
