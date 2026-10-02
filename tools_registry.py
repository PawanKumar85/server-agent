"""Modular, Class-Based Tool Architecture for Stream Graph ChatBot.

Allows developers to create new AI tools by subclassing `BaseChatTool` and
registering them via `@ChatToolRegistry.register`.
Eliminates the need to maintain a single monolithic tools class.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Iterator, List, Optional, Type


class BaseChatTool(ABC):
    """Abstract Base Class for ChatBot Function-Calling Tools."""

    name: str = ""
    description: str = ""
    category: str = "General"
    icon: str = "🛠"
    prompt_example: str = ""
    parameters: Dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

    @abstractmethod
    def execute(self, executor: Any, args: dict) -> Iterator[dict]:
        """Execute the tool logic and stream response events (tokens, reports, actions)."""
        raise NotImplementedError

    def schema(self) -> dict:
        """Generate OpenAI/OpenRouter compatible function calling schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def metadata(self) -> dict:
        """UI presentation metadata (category, icon, suggested prompt)."""
        return {
            "category": self.category,
            "icon": self.icon,
            "prompt": self.prompt_example or f"Run {self.name}",
        }


class ChatToolRegistry:
    """Central registry for ChatBot function-calling tools."""

    _tools: Dict[str, BaseChatTool] = {}

    @classmethod
    def register(cls, tool_class: Type[BaseChatTool]):
        """Decorator to register a custom tool class."""
        instance = tool_class()
        if not instance.name:
            instance.name = tool_class.__name__.lower()
        cls._tools[instance.name] = instance
        return tool_class

    @classmethod
    def register_instance(cls, tool_instance: BaseChatTool) -> None:
        """Register an instantiated tool."""
        cls._tools[tool_instance.name] = tool_instance

    @classmethod
    def get(cls, name: str) -> Optional[BaseChatTool]:
        """Retrieve tool by name."""
        return cls._tools.get(name)

    @classmethod
    def all_tools(cls) -> Dict[str, BaseChatTool]:
        """Return mapping of all registered tools."""
        return dict(cls._tools)

    @classmethod
    def definitions(cls) -> List[dict]:
        """Export all tool schemas for OpenRouter function calling."""
        return [tool.schema() for tool in cls._tools.values()]

    @classmethod
    def metadata_map(cls) -> Dict[str, dict]:
        """Export UI metadata dictionary for all tools."""
        return {name: tool.metadata() for name, tool in cls._tools.items()}


# -----------------------------------------------------------------------------
# Example Pluggable Tool Demonstration
# -----------------------------------------------------------------------------

@ChatToolRegistry.register
class StreamFreshnessSummaryTool(BaseChatTool):
    """Quick summary tool showing stream freshness breakdown across all nodes."""

    name = "get_stream_freshness_summary"
    description = "Get a quick high-level summary of stream freshness tiers (FRESH, WARM, STALE) across all channels."
    category = "Diagnostics"
    icon = "⏱"
    prompt_example = "Show me the stream freshness breakdown across all channels"
    parameters = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def execute(self, executor: Any, args: dict) -> Iterator[dict]:
        from telemetry_pool import telemetry_pool
        nodes = telemetry_pool.get_nodes(executor.driver)
        tiers: Dict[str, int] = {}
        for n in nodes.values():
            freshness = n.get("streamFreshness") or "UNKNOWN"
            tiers[freshness] = tiers.get(freshness, 0) + 1

        summary = " | ".join(f"`{k}`: {v}" for k, v in sorted(tiers.items()))
        yield {
            "type": "token",
            "text": f"⏱ **Live Stream Freshness Breakdown**\n\n{summary}\n\n*Cached via In-Memory Telemetry Pool (< 0.1ms)*\n"
        }
