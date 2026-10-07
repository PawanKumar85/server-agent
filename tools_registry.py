"""Modular, Class-Based Tool Architecture for Stream Graph ChatBot.

Allows developers to create new AI tools by subclassing `BaseChatTool` and
registering them via `@ChatToolRegistry.register`.
Eliminates the need to maintain a single monolithic tools class.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Iterator, List, Optional, Type

logger = logging.getLogger("tools_registry")



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


@ChatToolRegistry.register
class SummarizeIncidentTool(BaseChatTool):
    """Produces an AI-synthesized executive postmortem summary from incident and error logs."""

    name = "summarize_incident"
    description = (
        "Summarizes recent outages, manifest 404s, stale segment warnings, and incident logs into "
        "a crisp executive postmortem report with root cause takeaways using NLP TextRank graph scoring."
    )
    category = "AI Postmortem"
    icon = "🧠"
    prompt_example = "Generate an executive incident postmortem summary of recent outage logs"
    parameters = {
        "type": "object",
        "properties": {
            "channel": {
                "type": "string",
                "description": "Optional channel or server name to filter incident summary",
            },
            "max_sentences": {
                "type": "integer",
                "description": "Number of key findings to extract (default 3)",
            },
        },
        "additionalProperties": False,
    }

    def execute(self, executor: Any, args: dict) -> Iterator[dict]:
        from incident_summarizer import textrank_summarizer
        from telemetry_pool import telemetry_pool

        channel = args.get("channel", "")
        max_sentences = int(args.get("max_sentences", 3))

        # The real alert history (24 h, newest first): outages, recoveries, warnings, glitches, ad-break problems.
        logs = []
        try:
            from alertlog import AlertLog
            from metrics import store as metrics_store
            want = channel.lower().strip()
            for a in AlertLog(metrics_store().path).recent(since_s=86400, limit=200):
                if want and want not in f"{a.get('node')} {a.get('channel') or ''}".lower():
                    continue
                logs.append(f"{a.get('node')}{' (' + a['channel'] + ')' if a.get('channel') else ''}: "
                            f"{str(a.get('kind', '')).lower().replace('_', ' ')}"
                            + (f", {a['detail']}" if a.get("detail") else "") + ".")
        except Exception:
            pass
        if not logs:  # nothing logged: what is down right now
            for n in (telemetry_pool.get_nodes(executor.driver) or {}).values():
                if n.get("status") == "DOWN":
                    logs.append(f"Server {n.get('id') or n.get('domain')} is down: {n.get('lastError') or 'no detail'}.")

        res = textrank_summarizer.summarize_incident(logs, max_sentences=max_sentences, channel=channel)

        md = f"### 🧠 NLP Executive Incident Postmortem\n\n"
        md += f"**{res['headline']}**\n\n"
        md += f"> {res['executive_summary']}\n\n"
        if res.get("key_findings"):
            md += "#### 🔍 Key Takeaways & Root Cause Signals:\n"
            for f in res["key_findings"]:
                md += f"- {f}\n"
        md += f"\n*Analyzed {res['raw_sentence_count']} incident log entries using Graph Centrality TextRank.*"

        yield {"type": "token", "text": md}


# Auto-register modular tools
try:
    import geo_cdn_tool  # Registers recommend_cdn_placement and get_server_geo_matrix
except Exception:
    pass


class MCPChatToolAdapter(BaseChatTool):
    """Bridge adapter allowing the ChatBot Agent to seamlessly call MCP tools."""

    def __init__(self, mcp_tool: Any) -> None:
        self._tool = mcp_tool
        self.name = mcp_tool.name
        self.description = mcp_tool.description
        self.category = f"MCP ({mcp_tool.category})"
        self.icon = mcp_tool.icon
        self.prompt_example = f"Call {mcp_tool.name}"
        self.parameters = mcp_tool.parameters

    def execute(self, executor: Any, args: dict) -> Iterator[dict]:
        res = self._tool.execute(args)
        md = f"### {self._tool.icon} MCP Tool Executed: `{self.name}`\n\n"
        md += f"**Provider:** {self._tool.provider}\n\n"
        md += "```json\n" + json.dumps(res, indent=2) + "\n```"
        yield {"type": "token", "text": md}


try:
    from mcp_server import mcp_server
    for tool_inst in mcp_server._tools.values():
        ChatToolRegistry.register_instance(MCPChatToolAdapter(tool_inst))
except Exception as e:
    logger.warning(f"Failed to auto-register MCP tools into ChatToolRegistry: {e}")


@ChatToolRegistry.register
class NodeAgentRCATool(BaseChatTool):
    """Diagnoses streaming or server issues using live inside-the-box Node Agent telemetry."""

    name = "inspect_node_agent_telemetry"
    description = (
        "Inspects live internal server telemetry (CPU, memory, disk usage, active encoder processes, "
        "NIC packet drops, and local HLS segment freshness) reported by Node Agents on origin streaming servers. "
        "Identifies root causes like OOM crashes, disk exhaustion, encoder failures, or NIC drops."
    )
    category = "RCA & Telemetry"
    icon = "📡"
    prompt_example = "Inspect Node Agent telemetry for server 94.136.185.222 or demo agent to find root cause"
    parameters = {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "Server IP, domain, or agent ID to inspect (e.g. '94.136.185.222', 'demo', 'ingest1'). If empty, checks all connected agents.",
            }
        },
        "additionalProperties": False,
    }

    def execute(self, executor: Any, args: dict) -> Iterator[dict]:
        import sqlite3
        import time
        from metrics import store as metrics_store
        import agent_hub

        target = (args.get("target") or "").strip().lower()
        now = time.time()

        agents_data = agent_hub.list_agents()
        agents_list = agents_data.get("agents", [])

        with sqlite3.connect(metrics_store().path, timeout=5) as db:
            db.row_factory = sqlite3.Row
            if target:
                rows = db.execute(
                    "SELECT ts, agent, node, cpu, memory, disk_max, load1, rx_bps, tx_bps, net_errors, "
                    "encoders, hls_status, hls_age_s, data FROM agent_telemetry "
                    "WHERE lower(agent) LIKE ? OR lower(node) LIKE ? ORDER BY ts DESC LIMIT 20",
                    (f"%{target}%", f"%{target}%")
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT ts, agent, node, cpu, memory, disk_max, load1, rx_bps, tx_bps, net_errors, "
                    "encoders, hls_status, hls_age_s, data FROM agent_telemetry ORDER BY ts DESC LIMIT 50"
                ).fetchall()

        db_by_agent = {}
        for r in rows:
            ag = r["agent"]
            if ag not in db_by_agent:
                db_by_agent[ag] = dict(r)

        md = "### 📡 Node Agent Internal Host Diagnostics (RCA)\n\n"
        if not agents_list and not db_by_agent:
            md += "⚠️ *No Node Agents currently reporting telemetry.*\n"
            yield {"type": "token", "text": md}
            return

        active_map = {a.get("agent"): a for a in agents_list}
        all_agent_ids = sorted(set(list(active_map.keys()) + list(db_by_agent.keys())))

        agents_to_display = []
        for aid in all_agent_ids:
            mem_info = active_map.get(aid)
            db_row = db_by_agent.get(aid)
            node = (mem_info and mem_info.get("node")) or (db_row and db_row.get("node")) or "Unmapped"
            if target and (target not in aid.lower() and target not in node.lower()):
                continue
            is_online = bool(mem_info and mem_info.get("online"))
            if not is_online and db_row and (now - db_row["ts"] < 25):
                is_online = True
            latest = (mem_info and mem_info.get("latest")) or db_row or {}
            agents_to_display.append({
                "agent": aid,
                "node": node,
                "online": is_online,
                "latest": latest,
                "last_seen_s": round(now - db_row["ts"], 1) if db_row else None
            })

        if not agents_to_display:
            md += f"⚠️ *No agents found matching target '{target}'.*\n"
            yield {"type": "token", "text": md}
            return

        md += "| Agent ID | Graph Node | Status | CPU | Memory | Disk Max | Encoders | HLS Status | Issues / Culprits |\n"
        md += "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n"

        for a in agents_to_display:
            agent_id = a.get("agent")
            node = a.get("node") or "Unmapped"
            online = "🟢 Online" if a.get("online") else "🔴 Offline"
            latest = a.get("latest") or {}

            cpu = latest.get("cpu")
            mem = latest.get("memory")
            disk = latest.get("disk_max")
            enc = latest.get("encoders")
            hls = latest.get("hls_status") or "N/A"

            culprits = []
            if enc == 0:
                culprits.append("🚨 0 Encoders running (process stopped)")
            if disk and disk >= 90:
                culprits.append(f"⚠️ Disk full ({disk}%)")
            if mem and mem >= 90:
                culprits.append(f"⚠️ OOM risk ({mem}% RAM)")
            if cpu and cpu >= 95:
                culprits.append(f"⚠️ CPU overload ({cpu}%)")
            if latest.get("net_errors", 0) > 10:
                culprits.append(f"⚠️ NIC drops ({latest['net_errors']})")
            if hls == "STALE":
                culprits.append("⚠️ Stale HLS manifest")

            culprit_str = "<br>".join(culprits) if culprits else "✅ Healthy"

            cpu_str = f"{cpu}%" if cpu is not None else "-"
            mem_str = f"{mem}%" if mem is not None else "-"
            disk_str = f"{disk}%" if disk is not None else "-"
            enc_str = str(enc) if enc is not None else "-"

            md += f"| **{agent_id}** | `{node}` | {online} | {cpu_str} | {mem_str} | {disk_str} | {enc_str} | `{hls}` | {culprit_str} |\n"

        md += "\n"
        if any("🚨" in l or "⚠️" in l for l in md.splitlines()):
            md += "#### 🎯 Root Cause Insights:\n"
            for a in agents_to_display:
                latest = a.get("latest") or {}
                if latest.get("encoders") == 0:
                    md += f"- **Server `{a.get('node') or a.get('agent')}`**: Transcoder process failure. The streaming service/ffmpeg has crashed. Recommended action: restart the encoder process on the host.\n"
                if latest.get("disk_max") and latest["disk_max"] >= 90:
                    md += f"- **Server `{a.get('node') or a.get('agent')}`**: Disk storage critical at {latest['disk_max']}%. New segments cannot be written to disk, causing 404 or stream stalls. Recommended action: purge old TS chunks or clear log directory.\n"
                if latest.get("memory") and latest["memory"] >= 90:
                    md += f"- **Server `{a.get('node') or a.get('agent')}`**: Memory exhaustion ({latest['memory']}% RAM). Recommended action: check for memory leaks or scale host memory.\n"
                if latest.get("cpu") and latest["cpu"] >= 95:
                    md += f"- **Server `{a.get('node') or a.get('agent')}`**: CPU overload ({latest['cpu']}%). Recommended action: throttle background tasks or lower transcoding bitrate.\n"
                if latest.get("hls_status") == "STALE":
                    md += f"- **Server `{a.get('node') or a.get('agent')}`**: Stale origin HLS chunks ({latest.get('hls_age_s')}s old). Recommended action: restart publishing point.\n"
        else:
            md += "> ✅ *Host metrics show no resource exhaustion (CPU, memory, disks, and encoder processes are operating normally).*\n"

        yield {"type": "token", "text": md}


