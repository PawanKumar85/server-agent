"""Tools registry and executor for ChatBot function calling."""

import json
import re
import time
import uuid
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional, Set, Tuple
from urllib.parse import quote

from cypher_reference import REFERENCE as CYPHER_REFERENCE
from nodes import load_json_list
from telemetry_pool import telemetry_pool
from tracer_langsmith import traceable

if TYPE_CHECKING:
    from chatbot import ChatBot

# Thread-safe in-memory cache for pending actions requiring confirmation.
# Key: action_id -> dict with action details, params, created_at, and blast_radius.
PENDING_ACTIONS: Dict[str, dict] = {}
ACTION_TIMEOUT_S = 600  # 10 minutes to confirm


def _clean_expired_actions() -> None:
    now = time.time()
    expired = [k for k, v in PENDING_ACTIONS.items() if now - v.get("created_at", 0) > ACTION_TIMEOUT_S]
    for k in expired:
        PENDING_ACTIONS.pop(k, None)


QUERY_MAX_ROWS = 200
QUERY_SHOW_ROWS = 50
QUERY_TIMEOUT_S = 10
CHEAT_SHEET_URL = "https://neo4j.com/docs/cypher-manual/current/cheat-sheet/"
# Clauses that write, or call procedures that can: refused before the query reaches Neo4j (which also runs it
# in a READ session, so anything that slips past is rejected there).
WRITE_CLAUSES = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|FOREACH|LOAD\s+CSV|IN\s+TRANSACTIONS|"
    r"CALL\s+(dbms|db\.(create|drop|index\.vector\.create)|apoc\.(create|merge|refactor|periodic|load|do|cypher)))\b",
    re.IGNORECASE)


def cypher_problem(cypher: str) -> Optional[str]:
    """Why this query isn't allowed, or None."""
    if not cypher:
        return "it's empty"
    if len(cypher) > 4000:
        return "it's too long"
    bare = re.sub(r"'[^']*'|\"[^\"]*\"|//[^\n]*", "", cypher)  # string literals and comments can say anything
    if ";" in bare:
        return "only one query at a time"
    hit = WRITE_CLAUSES.search(bare)
    if hit:
        return f"it would change the graph ({hit.group(0).upper()}); only read queries are allowed here"
    return None


def _plain(value):
    """A Neo4j value as something small and JSON-friendly: nodes/relationships as their properties (without
    vectors), datetimes as text, long strings and lists cut short."""
    if hasattr(value, "labels") and hasattr(value, "items"):  # Node
        return {"labels": sorted(value.labels), **{k: _plain(v) for k, v in value.items() if not k.startswith("embedding")}}
    if hasattr(value, "type") and hasattr(value, "items") and hasattr(value, "start_node"):  # Relationship
        return {"type": value.type, **{k: _plain(v) for k, v in value.items()}}
    if hasattr(value, "nodes") and hasattr(value, "relationships"):  # Path
        return [n.get("domain") or n.get("id") for n in value.nodes]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items() if not str(k).startswith("embedding")}
    if isinstance(value, (list, tuple)):
        if len(value) > 20 and all(isinstance(x, float) for x in value):
            return f"[vector of {len(value)}]"
        return [_plain(v) for v in value[:50]]
    if isinstance(value, str):
        return value if len(value) <= 200 else value[:200] + "…"
    if hasattr(value, "iso_format"):
        return value.iso_format()
    return value


def format_rows(rows: List[dict], more: bool) -> str:
    if not rows:
        return "No rows.\n"
    cols = list(rows[0].keys())
    cell = lambda v: "—" if v is None else str(json.dumps(v, default=str) if isinstance(v, (dict, list)) else v).replace("|", "\\|").replace("\n", " ")[:120]
    shown = rows[:QUERY_SHOW_ROWS]
    table = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n" + "".join(
        "| " + " | ".join(cell(r.get(c)) for c in cols) + " |\n" for r in shown)
    note = ""
    if more or len(rows) > len(shown):
        note = f"\nShowing {len(shown)} of {'more than ' + str(QUERY_MAX_ROWS) if more else len(rows)} rows.\n"
    return f"{len(rows) if not more else str(QUERY_MAX_ROWS) + '+'} row(s):\n\n" + table + note


# Which tools a question gets. Sending all ~25 tool schemas costs ~4k tokens per question; the core read-only
# tools always go, the rest only when the question (or the one before it, for "yes, do it") points at them.
CORE_TOOLS = {"generate_report", "get_root_cause_ranking", "get_early_warnings", "get_incident_history",
              "check_failover_status", "get_risk_forecast", "get_learned_memory", "get_stream_freshness_summary",
              "get_pool_status", "audit_topology"}
TOOL_GROUPS = [
    (re.compile(r"\b(add|connect|link|links|update|change|edit|rename|delete|remove|create|new|main|backup|"
                r"final|transcod\w*|xcode|role|relationship|feeds?|produces)\b|https?://", re.I),
     {"add_channel", "add_stream_link", "connect_pipeline_relationship", "update_stream_link", "delete_node"}),
    (re.compile(r"\b(cypher|query|count|how many|list|which|each|every|more than|less than|fewer|most|least|"
                r"top|filter|group|per|graph|path|between|all)\b", re.I), {"query_graph"}),
    (re.compile(r"\b(traceroute|network|hops?|ping|route|crawl|spider|check|run|diagnos\w*|hls|segments?|"
                r"m3u8|playlist|stream|test)\b", re.I),
     {"run_traceroute", "trigger_channel_crawl", "diagnose_hls_stream"}),
    (re.compile(r"\b(remember|forget|note|fact|fix|fixed|learn\w*|feedback|wrong|right|was not|wasn'?t|"
                r"root cause was)\b", re.I),
     {"remember_fact", "forget_fact", "record_root_cause_feedback", "record_incident_fix"}),
    (re.compile(r"\b(scrap\w*|crawl\w*|website|web ?page|extract)\b|https?://", re.I), {"scrapy"}),
    (re.compile(r"\b(excel|xlsx|spreadsheet|workbook|export|download)\b", re.I), {"generate_excel"}),
    # Notifications: only when the question is about sending or notifying (they are ~1,000 tokens of schemas).
    (re.compile(r"\b(send|notify|notification|message|msg|email|e-mail|mail|sms|text|whatsapp|slack|telegram|"
                r"webhook|pagerduty|page|on-?call|bhejo|bhej|inform)\b", re.I),
     {"mcp_send_whatsapp", "mcp_send_sms", "mcp_send_email", "mcp_send_slack", "mcp_send_telegram",
      "mcp_send_webhook", "mcp_trigger_pagerduty"}),
    (re.compile(r"\b(cdn|geo\w*|locations?|city|cities|distance|placement|pop|region|where)\b", re.I),
     {"recommend_cdn_placement", "get_server_geo_matrix"}),
    (re.compile(r"\b(summar\w*|postmortem|post-mortem|incident report|what happened|recap)\b", re.I),
     {"summarize_incident"}),
]


SEMANTIC_TOP, SEMANTIC_MIN = 3, 0.5  # tools picked by meaning: at most this many, at least this similar
# How people actually ask for each tool: a question is compared with these (closest one counts), which matches far
# better than the one-line descriptions with a small embedding model.
TOOL_EXAMPLES = {
    "remember_fact": ["remember that", "save this note", "note that this server is a test feed",
                      "keep in mind that xcode2 restarts every night", "make a note"],
    "forget_fact": ["forget that", "delete that note", "you can forget what I said about"],
    "record_root_cause_feedback": ["the root cause was actually", "that was not the root cause", "you blamed the wrong server"],
    "record_incident_fix": ["it was fixed by restarting", "we fixed it by", "the outage was solved by"],
    "query_graph": ["how many servers", "list all channels that", "which servers have the most", "count the",
                    "show me every server with"],
    "diagnose_hls_stream": ["is the stream broken", "why is the video not playing", "is the playlist stale",
                            "check the hls stream", "is the stream dropping"],
    "run_traceroute": ["trace the network path", "is it a network problem", "where are packets lost", "run a traceroute"],
    "trigger_channel_crawl": ["check the channels now", "run a crawl", "test the channel right now", "refresh the checks"],
    "scrapy": ["crawl this website", "extract the links from this page", "scrape this url"],
    "generate_excel": ["export to excel", "download a spreadsheet", "give me a workbook"],
    "add_channel": ["add a new channel with these links", "add channel main backup final", "set up this new channel"],
    "add_stream_link": ["add this link", "add a backup url to the channel"],
    "connect_pipeline_relationship": ["connect these servers", "this server feeds that one", "link the pipeline"],
    "update_stream_link": ["change the url", "update the stream link", "rename the channel"],
    "delete_node": ["delete this server", "remove the node"],
}


def tools_for(text: str) -> set:
    names = set(CORE_TOOLS)
    for pattern, group in TOOL_GROUPS:
        if pattern.search(text or ""):
            names |= group
    return names


class Tools:
    """Registry and executor for all ChatBot tools (Analysis, Telemetry, and Two-Phase CRUD)."""

    def __init__(self, chatbot: "ChatBot"):
        self.chatbot = chatbot
        self.driver = chatbot.driver

    @classmethod
    def definitions(cls) -> List[dict]:
        """Tool schemas for OpenAI-compatible function calling (OpenRouter)."""
        defs = [
            # 1. Report
            {
                "type": "function",
                "function": {
                    "name": "generate_report",
                    "description": "Create the complete monitoring report (charts, every stored field including hidden ones) "
                                   "for one server, one channel, or all nodes. Use whenever the user asks for a report, "
                                   "summary document, full details, or charts.",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["node"],
                        "properties": {
                            "node": {
                                "type": "string",
                                "description": "A server domain (e.g. jio.ottlive.co.in or just jio), a channel name (e.g. gtcnews), or 'all' for every node"
                            }
                        }
                    }
                }
            },
            # 2. Excel
            {
                "type": "function",
                "function": {
                    "name": "generate_excel",
                    "description": "Generate and download the complete Excel workbook (.xlsx) containing all stream channels, "
                                   "links, server nodes, spiders, incidents, logs, IPs, health telemetry, and flow relationships. Use whenever the user asks "
                                   "for complete data of this project, full project data, complete info in excel, an Excel file, spreadsheet, sheet, workbook, or export of the full data.",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "scope": {
                                "type": "string",
                                "description": "Optional export scope (e.g. 'all', 'channels', 'nodes')"
                            }
                        }
                    }
                }
            },
            # 3. Diagnose HLS Stream
            {
                "type": "function",
                "function": {
                    "name": "diagnose_hls_stream",
                    "description": "Deeply diagnose HLS live stream failure modes: STALE_SEGMENTS, NO_SEGMENTS, "
                                   "STALE_SEQUENCE, FALLING_BEHIND, HTTP 404, or segment age freshness issues. "
                                   "Use whenever asked to diagnose, troubleshoot, or analyze stream stalls, freshness, or HLS errors.",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "node_or_channel": {
                                "type": "string",
                                "description": "Optional server domain, channel name, or 'all'"
                            }
                        }
                    }
                }
            },
            # 4. Risk Forecast
            {
                "type": "function",
                "function": {
                    "name": "get_risk_forecast",
                    "description": "Get predictive failure risk scores, warning probability, MTBF, MTTR, and anomaly causes "
                                   "(e.g. latency jitter, freshness drift, packet loss) for servers or channels. "
                                   "Use when asked about failure risk, predictions, which nodes might fail, or risk forecasts.",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "node_or_channel": {
                                "type": "string",
                                "description": "Optional domain of a server, name of a channel, or 'all'"
                            }
                        }
                    }
                }
            },
            # 4. Trigger Spider Crawl
            {
                "type": "function",
                "function": {
                    "name": "trigger_channel_crawl",
                    "description": "Trigger an immediate upstream spider crawl for a specific stream channel or all channels "
                                   "right now. Use when asked to test, crawl, re-check, or verify a channel or stream.",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["channel"],
                        "properties": {
                            "channel": {
                                "type": "string",
                                "description": "Name of the channel to crawl (e.g. 'gtcnews', 'bharat24news'), or 'all'"
                            }
                        }
                    }
                }
            },
            # 5. Root Cause Ranking
            {
                "type": "function",
                "function": {
                    "name": "get_root_cause_ranking",
                    "description": "Identify and rank the root cause of current stream failures across the topology graph. "
                                   "Use when asked for the root cause of outages, which server caused a failure, or ranked failure culprits.",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {}
                    }
                }
            },
            # 6. Early Warnings
            {
                "type": "function",
                "function": {
                    "name": "get_early_warnings",
                    "description": "Fetch real-time early warning anomaly signals on active servers (abnormal latency, high jitter, "
                                   "segment drift, packet drops) before failures turn into outages. "
                                   "Use when asked for early warnings, anomalies, or abnormal server metrics.",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {}
                    }
                }
            },
            # 7. Traceroute
            {
                "type": "function",
                "function": {
                    "name": "run_traceroute",
                    "description": "Start an immediate background ICMP / hop-by-hop traceroute to a target server node to diagnose "
                                   "network transit hops, latency spikes, and packet drop points.",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["node"],
                        "properties": {
                            "node": {
                                "type": "string",
                                "description": "The domain name or IP of the target server to traceroute (e.g. 'stream.ottlive.co.in')"
                            }
                        }
                    }
                }
            },
            # 8. Failover Status
            {
                "type": "function",
                "function": {
                    "name": "check_failover_status",
                    "description": "Inspect failover redundancy state across channels. Reports which channels are currently "
                                   "running on BackupLink, which channels have unhealthy Main inputs, and which lack backup redundancy. "
                                   "Use when asked about failover, backup links, redundancy, or streams running on backup.",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "channel": {
                                "type": "string",
                                "description": "Optional channel name to inspect (or 'all' for full report)"
                            }
                        }
                    }
                }
            },
            # 9. Incident History
            {
                "type": "function",
                "function": {
                    "name": "get_incident_history",
                    "description": "Retrieve chronological outage and recovery timeline from server incident logs. "
                                   "Reports OUTAGE and RECOVERY events, outage durations, error codes, and impacted URLs. "
                                   "Use when asked for outage history, incident logs, past downtime, or error history.",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "node_or_channel": {
                                "type": "string",
                                "description": "Optional server domain, channel name, or 'all'"
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Maximum number of incidents to return (default 10)"
                            }
                        }
                    }
                }
            },
            # 10. Audit Topology
            {
                "type": "function",
                "function": {
                    "name": "audit_topology",
                    "description": "Audit the media stream graph topology for architectural weaknesses: channels lacking backup redundancy "
                                   "(Single Points of Failure), orphan servers with no connections, or broken stream flows. "
                                   "Use when asked to audit topology, check configuration health, find orphan links, or check redundancy gaps.",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {}
                    }
                }
            },
            # 10a. Read-only Cypher over the graph (schema + the Neo4j cheat sheet in the description)
            {
                "type": "function",
                "function": {
                    "name": "query_graph",
                    "description": "Run a READ-ONLY Cypher query on the Neo4j graph and show the result as a table. Use it for "
                                   "questions the other tools don't answer directly (counts, filters, which servers/channels "
                                   "match a condition, paths between servers). Writes are refused.\n\n" + CYPHER_REFERENCE,
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["cypher", "purpose"],
                        "properties": {
                            "cypher": {"type": "string", "description": "One read-only Cypher query (MATCH ... RETURN ...), with a LIMIT"},
                            "purpose": {"type": "string", "description": "In a few plain words, what the query finds"}
                        }
                    }
                }
            },
            # 11a. CRUD: Add a whole channel (several links at once, one confirmation)
            {
                "type": "function",
                "function": {
                    "name": "add_channel",
                    "description": "Add a channel's stream links in one go (Main, Backup, Transcoding, Final: any of them) and "
                                   "wire its pipeline Main/Backup -> Transcoding -> Final. Asks the user to Allow or Deny once "
                                   "for all of them. Use this whenever the user gives two or more links, or a whole channel.",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["channel", "links"],
                        "properties": {
                            "channel": {"type": "string", "description": "Channel name (e.g. 'sakshitv')"},
                            "links": {
                                "type": "array",
                                "minItems": 1,
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["role", "url"],
                                    "properties": {
                                        "role": {"type": "string",
                                                 "enum": ["MainInput", "BackupLink", "Transcoding", "FinalLink"],
                                                 "description": "Main -> MainInput, Backup -> BackupLink, Transcoding/xcode -> Transcoding, Final/output -> FinalLink"},
                                        "url": {"type": "string", "description": "The full stream URL"}
                                    }
                                }
                            }
                        }
                    }
                }
            },
            # 11. CRUD: Add Stream Link (Double Confirmation)
            {
                "type": "function",
                "function": {
                    "name": "add_stream_link",
                    "description": "Add a new stream URL entry to the graph (creating or expanding a server domain node). "
                                   "Always asks the user to Allow or Deny before applying. Use when asked to add a new link, stream URL, "
                                   "or channel entry.",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["channel", "role", "url"],
                        "properties": {
                            "channel": {
                                "type": "string",
                                "description": "Channel name (e.g. 'gtcnews', 'sports1')"
                            },
                            "role": {
                                "type": "string",
                                "enum": ["MainInput", "BackupLink", "Transcoding", "FinalLink"],
                                "description": "Pipeline role of this link"
                            },
                            "url": {
                                "type": "string",
                                "description": "Complete HTTP, RTMP or HLS stream URL (e.g. 'http://ingest1.live.com/live/ch1.m3u8')"
                            }
                        }
                    }
                }
            },
            # 12. CRUD: Connect Pipeline Relationship (Double Confirmation)
            {
                "type": "function",
                "function": {
                    "name": "connect_pipeline_relationship",
                    "description": "Create a directed media pipeline relationship between two servers in the graph (FEEDS or PRODUCES). "
                                   "Always asks the user to Allow or Deny before applying. Use when asked to connect servers, link pipelines, "
                                   "or make relationships.",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["source", "type", "target"],
                        "properties": {
                            "source": {
                                "type": "string",
                                "description": "Source domain or node ID (e.g. 'ingest1.ottlive.co.in')"
                            },
                            "type": {
                                "type": "string",
                                "enum": ["FEEDS", "PRODUCES"],
                                "description": "Relationship type (Main/Backup FEEDS Transcoder/Final, Transcoder PRODUCES Final)"
                            },
                            "target": {
                                "type": "string",
                                "description": "Target domain or node ID (e.g. 'xcode2.ottlive.co.in')"
                            }
                        }
                    }
                }
            },
            # 13. CRUD: Update Stream Link (Double Confirmation)
            {
                "type": "function",
                "function": {
                    "name": "update_stream_link",
                    "description": "Modify an existing stream link's URL, channel, or role. Always asks the user to Allow or Deny before applying. "
                                   "Use when asked to update, edit, rename, or change a stream link.",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["old_url"],
                        "properties": {
                            "old_url": {
                                "type": "string",
                                "description": "The exact current URL of the link to update"
                            },
                            "new_url": {
                                "type": "string",
                                "description": "Optional replacement stream URL"
                            },
                            "new_channel": {
                                "type": "string",
                                "description": "Optional replacement channel name"
                            },
                            "new_role": {
                                "type": "string",
                                "enum": ["MainInput", "BackupLink", "Transcoding", "FinalLink"],
                                "description": "Optional replacement pipeline role"
                            }
                        }
                    }
                }
            },
            # 14. CRUD: Delete Node with Blast Radius (Double Confirmation)
            {
                "type": "function",
                "function": {
                    "name": "delete_node",
                    "description": "Permanently delete a server node and its attached pipeline relationships from the Neo4j graph. "
                                   "Calculates and previews the Blast Radius (impacted channels, lost backup redundancy) and requires "
                                   "strict double confirmation before execution. Use when asked to delete or remove a node/server.",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["node"],
                        "properties": {
                            "node": {
                                "type": "string",
                                "description": "The domain or node ID of the server to delete (e.g. 'ingest3.ottlive.co.in')"
                            }
                        }
                    }
                }
            },
            # 15. Scrapy: URL Crawler & Link Extractor (Internal & External Links)
            {
                "type": "function",
                "function": {
                    "name": "scrapy",
                    "description": "Crawl and scrape any URL provided by the user to extract, audit, and categorize all hyperlinks: "
                                   "internal links, external outbound links, and streaming media links (.m3u8, .ts, .mpd, .mp4). "
                                   "Use whenever the user asks to scrape a URL, crawl links, inspect a website or stream endpoint, or run Scrapy.",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["url"],
                        "properties": {
                            "url": {
                                "type": "string",
                                "description": "The webpage or streaming endpoint URL to crawl and scrape (e.g. 'https://example.com' or 'http://jio.ottlive.co.in/gtcnews')"
                            },
                            "max_links": {
                                "type": "integer",
                                "description": "Maximum number of links to extract and return (default: 100)"
                            },
                            "filter_type": {
                                "type": "string",
                                "enum": ["all", "internal", "external", "media"],
                                "description": "Optional filter: 'all' (default), 'internal' only, 'external' only, or 'media' only"
                            }
                        }
                    }
                }
            },
            # 16-20. Learning: operator facts, root-cause verdicts, what fixed an outage, and what was learned
            {
                "type": "function",
                "function": {
                    "name": "remember_fact",
                    "description": "Remember a fact the operator states about the network for later answers (e.g. "
                                   "'xcode2 restarts every night at 03:00', 'ingest1 is a test feed'). Use only when the "
                                   "user asks you to remember or note something.",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["fact"],
                        "properties": {"fact": {"type": "string", "description": "The fact, as one plain sentence"}}
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "forget_fact",
                    "description": "Forget remembered facts containing the given words. Use when the user asks you to forget something.",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["about"],
                        "properties": {"about": {"type": "string", "description": "Words from the fact to forget"}}
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "record_root_cause_feedback",
                    "description": "Record the operator's verdict on a root cause: that a server was, or was not, the real "
                                   "root cause of an outage. Future root-cause rankings learn from it.",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["node", "was_root_cause"],
                        "properties": {
                            "node": {"type": "string", "description": "Server domain or short name"},
                            "was_root_cause": {"type": "boolean", "description": "True if it was the root cause, false if not"}
                        }
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "record_incident_fix",
                    "description": "Record what fixed a server's most recent outage (e.g. 'restarted nginx', 'ISP link "
                                   "restored'), so similar outages later come with the fix.",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["node", "fix"],
                        "properties": {
                            "node": {"type": "string", "description": "Server domain or short name"},
                            "fix": {"type": "string", "description": "What fixed it"}
                        }
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "get_learned_memory",
                    "description": "Show what you have learned from past data: remembered facts, recurring outage patterns, "
                                   "recent past outages and the operator's feedback.",
                    "parameters": {"type": "object", "additionalProperties": False, "properties": {}}
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "get_pool_status",
                    "description": "Inspect the In-Memory Telemetry Data Pool performance: cache hits, misses, hit ratio, "
                                   "cached nodes count, cached channels, freshness age, and memory status.",
                    "parameters": {"type": "object", "additionalProperties": False, "properties": {}}
                }
            }
        ]
        try:
            from tools_registry import ChatToolRegistry
            defs.extend(ChatToolRegistry.definitions())
        except Exception:
            pass
        return defs

    _tool_vectors: Dict[str, Any] = {}  # tool name -> embedding of its description (computed once)

    @classmethod
    def definitions_for(cls, text: str, embed=None, learned: Optional[List[str]] = None) -> List[dict]:
        """The tool schemas this question needs: the core tools, the keyword groups (tools_for), the tools whose
        description is closest in meaning to the question (embed: text -> vector), and the tools that produced
        answers rated good for similar questions (learned). Unknown tool names are always kept."""
        wanted = tools_for(text) | set(learned or [])
        defs = cls.definitions()
        if embed is not None:
            wanted |= cls._by_meaning(text, defs, embed)
        known = CORE_TOOLS.union(*(g for _, g in TOOL_GROUPS))
        return [d for d in defs if d["function"]["name"] in wanted or d["function"]["name"] not in known]

    @classmethod
    def _by_meaning(cls, text: str, defs: List[dict], embed, top: int = SEMANTIC_TOP,
                    floor: float = SEMANTIC_MIN) -> set:
        import numpy as np
        q = embed(text)
        if q is None:
            return set()
        q = np.asarray(q, dtype=float)
        q = q / (np.linalg.norm(q) or 1)
        scored = []
        for d in defs:
            name = d["function"]["name"]
            if name in CORE_TOOLS:
                continue
            if name not in cls._tool_vectors:
                phrases = TOOL_EXAMPLES.get(name) or [f"{name.replace('_', ' ')}: {d['function']['description'].split('.')[0]}"]
                vecs = [embed(p) for p in phrases]
                vecs = [np.asarray(v, dtype=float) for v in vecs if v is not None]
                if not vecs:
                    continue
                cls._tool_vectors[name] = np.stack([v / (np.linalg.norm(v) or 1) for v in vecs])
            scored.append((float((cls._tool_vectors[name] @ q).max()), name))  # its closest example
        scored.sort(reverse=True)
        return {name for score, name in scored[:top] if score >= floor}

    @traceable(name="chatbot_tool_execution", run_type="tool")
    def execute(self, call: dict) -> Iterator[dict]:
        """Dispatch a tool call and stream response events."""
        name = call.get("name")
        raw_args = call.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except ValueError:
            args = {}

        if not isinstance(args, dict):
            args = {}
        args.pop("confirmed", None)  # only the user's Allow (CONFIRM act_…) can apply a staged change

        try:
            from tools_registry import ChatToolRegistry
            reg_tool = ChatToolRegistry.get(name)
            if reg_tool:
                yield from reg_tool.execute(self, args)
                return
        except Exception:
            pass

        handler = getattr(self, f"tool_{name}", None)
        if handler and callable(handler):
            yield from handler(args)
        else:
            yield {"type": "token", "text": f"\n[Unknown tool {name!r}]\n"}

    def execute_confirmed_action(self, action_id: str) -> Iterator[dict]:
        """Executes a previously staged action by its ID after user confirmation."""
        _clean_expired_actions()
        action_data = PENDING_ACTIONS.pop(action_id, None)
        if not action_data:
            yield {
                "type": "token",
                "text": f"⚠️ Pending action `{action_id}` not found or has expired. Please re-issue your request.\n"
            }
            return

        action_name = action_data["action"]
        params = action_data["params"]

        handler = getattr(self, f"tool_{action_name}", None)
        if handler and callable(handler):
            events = list(handler(params, confirmed=True))
            try:
                from activity import record_activity
                action_title = action_data.get("title") or action_name
                summary_text = (
                    f"Executed confirmed topology action '{action_title}' on "
                    f"{params.get('domain') or params.get('channel') or 'topology graph'}. Changes committed successfully."
                )
                record_activity(
                    driver=self.driver,
                    embedder=getattr(self.chatbot, "embedder", None),
                    act_type="action",
                    title=f"Action: {action_title}",
                    summary=summary_text,
                    target=str(params.get("domain") or params.get("channel") or ""),
                    details=params,
                )
            except Exception:
                pass
            yield from events
        else:
            yield {"type": "token", "text": f"Error: No executor for action {action_name}.\n"}

    def cancel_action(self, action_id: str) -> Iterator[dict]:
        """Cancels a pending action."""
        action_data = PENDING_ACTIONS.pop(action_id, None)
        if action_data:
            yield {"type": "token", "text": f"✕ Action `{action_id}` ({action_data['title']}) has been cancelled. No changes were made.\n"}
        else:
            yield {"type": "token", "text": f"No active pending action found for `{action_id}`.\n"}

    # ---------- Read-only Cypher ----------

    def tool_query_graph(self, args: dict) -> Iterator[dict]:
        """Runs the model's Cypher read-only: write clauses are refused before anything runs, the session is a READ
        session (Neo4j itself rejects writes), with a time limit and a row cap; vectors are left out of the result."""
        cypher = str(args.get("cypher") or "").strip().rstrip(";")
        purpose = str(args.get("purpose") or "").strip()
        problem = cypher_problem(cypher)
        if problem:
            yield {"type": "token", "text": f"I can't run that query: {problem}\n\n```cypher\n{cypher}\n```\n"}
            return
        import neo4j

        def work(tx):
            result = tx.run(cypher)
            rows = []
            for record in result:
                if len(rows) >= QUERY_MAX_ROWS:
                    return rows, True
                rows.append({k: _plain(v) for k, v in record.items()})
            return rows, False

        try:
            with self.driver.session(default_access_mode=neo4j.READ_ACCESS) as session:
                rows, more = session.execute_read(neo4j.unit_of_work(timeout=QUERY_TIMEOUT_S)(work))
        except Exception as e:  # syntax errors, unknown labels, timeouts, write attempts: say so plainly
            msg = str(getattr(e, "message", None) or e).split("\n")[0][:300]
            yield {"type": "token", "text": f"The query failed: {msg}\n\n```cypher\n{cypher}\n```\n"}
            return
        head = f"**{purpose}**\n\n" if purpose else ""
        yield {"type": "token", "text": head + f"```cypher\n{cypher}\n```\n" + format_rows(rows, more) +
               f"\n<sub>Read-only Cypher · syntax: {CHEAT_SHEET_URL}</sub>\n"}

    # ---------- Learning (memory of past outages, operator facts and feedback) ----------

    def _learner(self):
        learner = getattr(self.chatbot, "learning", None)
        if learner is None:
            import learning
            learner = learning.store()
        return learner

    def _known_nodes(self) -> List[str]:
        return [r["d"] for r in self.driver.execute_query("MATCH (n:Domain) RETURN n.domain AS d").records]

    def _resolve_node(self, name: str) -> Optional[str]:
        """A domain from a full or short name ('ingest1' -> 'ingest1.ottlive.co.in'); None if unknown or ambiguous."""
        name = str(name or "").strip().lower()
        nodes = self._known_nodes()
        if name in nodes:
            return name
        found = [n for n in nodes if n.split(".")[0] == name or n.startswith(name + ".")]
        return found[0] if len(found) == 1 else None

    def tool_remember_fact(self, args: dict) -> Iterator[dict]:
        fact = str(args.get("fact") or "").strip()
        if len(fact) < 3:
            yield {"type": "token", "text": "Tell me what to remember."}
            return
        saved = self._learner().add_fact(fact, self._known_nodes())
        if saved.get("duplicate"):
            yield {"type": "token", "text": f"I already remember that: \"{saved['text']}\"."}
            return
        about = f" (about {', '.join(saved['nodes'])})" if saved["nodes"] else ""
        yield {"type": "token", "text": f"🧠 Remembered{about}: \"{saved['text']}\". I'll use it in later answers; "
                                        f"you can delete it under *What I've learned* on the Agent page."}

    def tool_forget_fact(self, args: dict) -> Iterator[dict]:
        about = str(args.get("about") or "").strip()
        gone = self._learner().forget_fact(text=about) if len(about) >= 3 else []
        if not gone:
            yield {"type": "token", "text": f"I don't remember anything about \"{about}\"."}
            return
        yield {"type": "token", "text": "Forgotten:\n" + "\n".join(f"- {g['text']}" for g in gone)}

    def tool_record_root_cause_feedback(self, args: dict) -> Iterator[dict]:
        node = self._resolve_node(args.get("node"))
        if not node:
            yield {"type": "token", "text": f"I don't know a single server called `{args.get('node')}`."}
            return
        right = bool(args.get("was_root_cause"))
        self._learner().add_feedback("root_cause", 1 if right else -1, node=node)
        factor = self._learner().priors().get(node, 1.0)
        yield {"type": "token", "text": f"Noted: {node} {'was' if right else 'was not'} the root cause. Future "
                                        f"rankings weigh it ×{factor} when it's a candidate."}

    def tool_record_incident_fix(self, args: dict) -> Iterator[dict]:
        node = self._resolve_node(args.get("node"))
        fix = str(args.get("fix") or "").strip()
        if not node or len(fix) < 3:
            yield {"type": "token", "text": "I need a known server and what fixed it."}
            return
        case = self._learner().set_resolution(node, fix)
        if not case:
            yield {"type": "token", "text": f"{node} has no closed outage on record yet, so there's nothing to attach "
                                            f"the fix to. I can remember it as a fact instead."}
            return
        from learning import when_words
        yield {"type": "token", "text": f"Saved: the {node} outage of {when_words(case['opened'])} was fixed by "
                                        f"\"{fix}\". Similar outages will now come with this fix."}

    def tool_get_learned_memory(self, args: dict) -> Iterator[dict]:
        from learning import when_words
        m = self._learner().summary()
        out = [f"**What I've learned** ({m['caseCount']} closed outages on record)\n"]
        out.append("**Facts you told me**")
        out += [f"- {f['text']}" for f in m["facts"]] or ["- none yet"]
        out.append("\n**Patterns**")
        out += [f"- {p['text']}" for p in m["patterns"]] or ["- none yet (patterns need at least 2 similar outages)"]
        out.append("\n**Recent outages**")
        out += [f"- {when_words(c['opened'])}: {c['text'].split('; started')[0]}"
                + (f" — fixed by {c['resolution']}" if c.get("resolution") else "") for c in m["cases"][:5]] or ["- none yet"]
        fb = m["feedback"]
        out.append(f"\n**Feedback**: answers 👍 {fb['answer']['up']} / 👎 {fb['answer']['down']}; "
                   f"root causes right {fb['root_cause']['up']} / wrong {fb['root_cause']['down']}.")
        if m["priors"]:
            out.append("Root-cause weights: " + ", ".join(f"{n} ×{f}" for n, f in sorted(m["priors"].items())))
        yield {"type": "token", "text": "\n".join(out) + "\n"}

    # ---------- Standard Read / Diagnostic Tools ----------

    def tool_generate_report(self, args: dict) -> Iterator[dict]:
        """Report tool: creates full HTML node/channel/all report with deep AI analysis."""
        from chatbot import Snapshot, resolve_report_target

        target = str(args.get("node") or "all")
        found = resolve_report_target(Snapshot(self.driver), target)
        if found.get("problem"):
            yield {"type": "token", "text": found["problem"]}
            return

        query = f"?node={quote(found['node'], safe='')}" if found["node"] else ""
        url = f"/api/report.html{query}"
        yield {
            "type": "report",
            "icon": "📄",
            "title": "Report",
            "node": found["node"],
            "label": found["label"],
            "url": url,
            "download": f"{url}{'&' if query else '?'}download=1",
            "openText": "Open report",
        }
        yield {
            "type": "token",
            "text": f"Here is the complete report for {found['label']}: charts, per-URL health, "
                    f"incidents, traceroutes, embeddings and every stored field.\n\n"
        }
        from report_analysis import analysis_prompt, stream_analysis

        monitor = self.chatbot.monitor_status() or {}
        prepared = analysis_prompt(
            self.driver,
            found["node"],
            {"interval": monitor.get("intervalSeconds")},
            metrics=self.chatbot.metrics,
        )
        if prepared:
            for event in stream_analysis(self.chatbot, prepared):
                if event["type"] != "findings":
                    yield event

    def tool_generate_excel(self, args: dict) -> Iterator[dict]:
        """Excel tool: generates and links full .xlsx workbook export."""
        url = "/api/export.xlsx"
        yield {
            "type": "report",
            "icon": "📊",
            "title": "Complete Project Data (.xlsx)",
            "label": "All nodes, spiders, incident logs, channels, stream links & topology",
            "url": url,
            "download": url,
            "openText": "⬇ Download Complete Excel",
        }
        yield {
            "type": "token",
            "text": "I have prepared the **complete data of this project in Excel (.xlsx)**.\n\n"
                    "Click the button above to download the workbook. Here is what is included:\n\n"
                    "- **Project Overview**: High-level system KPIs, total servers UP/DOWN, channels on backup, active spiders, and system summary.\n"
                    "- **Nodes**: Complete inventory of every server node with primary role, status, IP, ping statistics, latency, RTT, jitter, packet loss %, consecutive counters, and last errors.\n"
                    "- **Spiders**: Every autonomous spider walker with status, stop reasons, Root Cause Analysis (RCA), impact, failover state, step counts, and complete paths walked.\n"
                    "- **Incident Logs**: Detailed historical incident log entries for all nodes (OUTAGE, RECOVERY, BLIP, WARNING), category, duration in seconds, affected streams, and error diagnostics.\n"
                    "- **Channels**: Full channel list with active feed status (Main / Backup / None), pipeline health, and downtime timestamps.\n"
                    "- **Stream Links & Health**: Individual media stream URLs, target segment duration, latest segment age, learned baseline median, warning line, and freshness.\n"
                    "- **Relationships**: Media flow graph topology (`FEEDS` and `PRODUCES` edges between nodes).\n"
        }

    def tool_get_risk_forecast(self, args: dict) -> Iterator[dict]:
        """Predictive risk forecast: evaluates failure risk, MTBF, MTTR, and anomaly causes."""
        target = str(args.get("node_or_channel") or "all").strip().lower()
        records = self.driver.execute_query(
            "MATCH (n:Domain) WHERE NOT n.domain ENDS WITH '.invalid' "
            "RETURN n.domain AS domain, n.status AS status, n.failureRisk AS failureRisk, "
            "n.warningRisk AS warningRisk, n.riskReason AS riskReason, n.riskProfile AS riskProfile "
            "ORDER BY n.failureRisk DESC"
        ).records

        from chatbot import Snapshot, _json
        snap = Snapshot(self.driver)

        if target not in ("", "all", "all nodes", "*") and target in [c.lower() for c in snap.channels]:
            matched_channel = next(c for c in snap.channels if c.lower() == target)
            channel_nodes = {e["node"] for role in snap.channels[matched_channel].values() for e in role}
            records = [r for r in records if r["domain"] in channel_nodes]
            header = f"### Predictive Failure Risk for Channel: **{matched_channel}**\n\n"
        elif target not in ("", "all", "all nodes", "*"):
            records = [r for r in records if target in r["domain"].lower()]
            header = f"### Predictive Failure Risk for: **{target}**\n\n"
        else:
            header = "### Overall Predictive Failure & Risk Ranking\n\n"

        if not records:
            yield {"type": "token", "text": f"No matching nodes found for risk forecast query '{target}'.\n"}
            return

        yield {"type": "token", "text": header}
        lines = []
        for r in records[:8]:
            d = r["domain"]
            f_risk = round(float(r["failureRisk"] or 0) * 100, 1)
            w_risk = round(float(r["warningRisk"] or 0) * 100, 1)
            reason = r["riskReason"] or "Normal operating baseline"
            status = r["status"] or "UP"
            profile = _json(r["riskProfile"])
            mtbf = profile.get("mtbfHours")
            mtbf_str = f" · MTBF: {round(mtbf, 1)}h" if mtbf is not None else ""
            lines.append(f"- **{d}** (`{status}`): **Failure Risk: {f_risk}%** (Warning: {w_risk}%)\n  *Key Factors:* {reason}{mtbf_str}")

        yield {"type": "token", "text": "\n".join(lines) + "\n"}

    def tool_trigger_channel_crawl(self, args: dict) -> Iterator[dict]:
        """Trigger an immediate spider health crawl for a channel or all channels."""
        from chatbot import Snapshot
        raw_ch = str(args.get("channel") or "all").strip().lower()
        snap = Snapshot(self.driver)

        finals = None
        label = "all channels"
        if raw_ch not in ("", "all", "*", "everything"):
            matched = next((c for c in snap.channels if c.lower() == raw_ch), None)
            if not matched:
                yield {"type": "token", "text": f"Channel '{raw_ch}' not found. Available: {', '.join(snap.channels.keys())}.\n"}
                return
            finals = [e["node"] for e in snap.channels[matched].get("FinalLink", [])]
            label = matched

        start_run_fn = getattr(self.chatbot, "start_run", None)
        if not callable(start_run_fn):
            yield {"type": "token", "text": "Spider runner is not accessible from ChatBot.\n"}
            return

        run_id = start_run_fn(finals, source="channel" if finals else "manual")
        if run_id:
            yield {"type": "token", "text": f"🚀 Triggered immediate spider walk for **{label}** (Run ID: `{run_id}`).\n"
                                            f"Health checks and topology traversal are streaming live now.\n"}
        else:
            yield {"type": "token", "text": "A spider walk is currently already running. Please wait a moment and try again.\n"}

    def tool_get_root_cause_ranking(self, args: dict) -> Iterator[dict]:
        """Retrieve the ranked root cause analysis of current failure groups."""
        insights = self.chatbot.insights() if callable(getattr(self.chatbot, "insights", None)) else {}
        ranking = insights.get("ranking") or []
        if not ranking:
            yield {"type": "token", "text": "✓ No active failure root causes. All stream nodes and channels are healthy.\n"}
            return

        yield {"type": "token", "text": "### Current Root Cause Analysis (RCA) Ranking\n\n"}
        for i, group in enumerate(ranking, 1):
            culprit = group.get("culprit") or "Unknown"
            conf = group.get("confidence") or group.get("score")
            conf_str = f" (Confidence: {round(float(conf)*100)}%)" if conf else ""
            impacted = group.get("impacted") or group.get("affected") or []
            channels = group.get("channels") or []
            notes = group.get("reason") or group.get("category") or "Primary upstream failure point"

            yield {"type": "token", "text": f"{i}. **Root Culprit: `{culprit}`**{conf_str}\n"
                                            f"   - **Diagnosis**: {notes}\n"
                                            f"   - **Impacted downstream nodes**: {', '.join(impacted) if impacted else 'None'}\n"
                                            f"   - **Affected output channels**: {', '.join(channels) if channels else 'Infrastructure'}\n\n"}

    def tool_get_early_warnings(self, args: dict) -> Iterator[dict]:
        """Retrieve real-time early warning anomaly signals on running servers."""
        insights = self.chatbot.insights() if callable(getattr(self.chatbot, "insights", None)) else {}
        warnings = insights.get("warnings") or []
        if not warnings:
            yield {"type": "token", "text": "✓ No active early warning anomalies detected. All servers are within latency, jitter, and freshness baselines.\n"}
            return

        yield {"type": "token", "text": "### Active Early Warning Anomalies\n\n"}
        for w in warnings:
            node = w.get("node")
            score = w.get("score", 0)
            items = w.get("warnings") or []
            yield {"type": "token", "text": f"- **{node}** (Anomaly Score: `{score}/10`):\n"}
            for item in items:
                yield {"type": "token", "text": f"  * {item}\n"}
        yield {"type": "token", "text": "\n"}

    def tool_diagnose_hls_stream(self, args: dict) -> Iterator[dict]:
        """Deep HLS stream diagnosis: evaluates STALE_SEGMENTS, NO_SEGMENTS, STALE_SEQUENCE,
        FALLING_BEHIND, HTTP 404, segment age vs baseline, and hop isolation."""
        target = str(args.get("node_or_channel") or "all").strip().lower()
        # Fast In-Memory Data Pool fetch (< 0.1ms)
        node_cache = telemetry_pool.get_nodes(self.driver)

        matched = []
        for domain, n_info in node_cache.items():
            p = n_info.get("raw_properties", {})
            links = n_info.get("links", [])
            labels = n_info.get("labels", [])
            channels = [str(l.get("channel") or "").lower() for l in links]
            if target == "all" or target in domain.lower() or any(target in c for c in channels):
                matched.append((p, labels, links))

        if not matched:
            yield {"type": "token", "text": f"⚠️ No servers or channels matched `{target}`.\n"}
            return

        yield {
            "type": "report",
            "icon": "🩺",
            "title": "HLS Stream Diagnosis",
            "label": f"Analysis for {target} ({len(matched)} nodes)",
            "url": f"/api/report.html{('?node=' + matched[0][0].get('domain')) if len(matched) == 1 else ''}",
            "openText": "Open Interactive Report",
        }

        yield {"type": "token", "text": f"### 🩺 HLS Stream Health & Diagnostics ({'All Nodes' if target == 'all' else target})\n\n"}

        diagnoses = []
        for p, labels, links in matched:
            domain = p.get("domain")
            status = p.get("status") or "UNKNOWN"
            last_err = p.get("lastError") or ""
            latency = p.get("lastLatencyMs")
            rtt = p.get("lastRttMs")
            jitter = p.get("lastJitterMs")
            loss = p.get("lastPacketLoss")
            try:
                url_health = json.loads(p.get("urlHealth") or "{}")
            except Exception:
                url_health = {}

            issues = []
            for link in links:
                u = link.get("url", "")
                ch = link.get("channel", "")
                role = link.get("role", "")
                h = url_health.get(u, {})
                up = h.get("up")
                target_s = h.get("targetS") or 6.0
                age_s = h.get("segmentAgeS")
                baseline = h.get("ageBaseline")
                freshness = h.get("freshness")
                detail = h.get("detail") or h.get("error") or ""

                mode = None
                rca = None
                cmd = f"curl -sIv '{u}' | head -n 15"

                if "404" in last_err or "404" in detail:
                    mode = "HTTP_404 (Missing Playlist or Segment)"
                    rca = "Origin path mismatch or packager has not output index.m3u8 yet."
                elif "NO_SEGMENTS" in last_err or "NO_SEGMENTS" in detail:
                    mode = "NO_SEGMENTS"
                    rca = "Playlist exists but has 0 segment entries (encoder initialized or directory cleaned)."
                elif "STALE_SEQUENCE" in last_err or "STALE_SEQUENCE" in detail:
                    mode = "STALE_SEQUENCE (Media Sequence Frozen)"
                    rca = "#EXT-X-MEDIA-SEQUENCE is not advancing. Video packager is stalled; viewers will buffer/loop."
                elif "STALE_SEGMENTS" in last_err or freshness == "STALE" or (age_s and baseline and age_s > baseline * 3 and age_s > 30):
                    mode = "STALE_SEGMENTS (Stalled Media Generation)"
                    rca = f"Segment age is {round(age_s or 0, 1)}s (baseline ~{round(baseline or 0, 1)}s, target {target_s}s). Upstream encoder clock stopped or packager not writing new chunks."
                elif age_s and target_s and age_s > target_s * 4:
                    mode = "FALLING_BEHIND (Drifting Real-Time Lag)"
                    rca = f"Current segment age {round(age_s, 1)}s is lagging far behind real time. Transcoder CPU/GPU bottlenecked (< 1.0x encode speed)."
                elif up is False:
                    mode = "CONNECTION_REFUSED / TIMEOUT"
                    rca = f"Cannot establish HTTP connection to {domain}. Process down or port blocked."

                if mode:
                    issues.append({
                        "channel": ch, "role": role, "url": u, "mode": mode,
                        "age": age_s, "target": target_s, "baseline": baseline,
                        "rca": rca, "cmd": cmd, "detail": detail
                    })

            diagnoses.append({
                "domain": domain, "status": status, "labels": labels,
                "latency": latency, "rtt": rtt, "jitter": jitter, "loss": loss,
                "issues": issues, "links_count": len(links)
            })

        has_issues = any(d["issues"] for d in diagnoses)
        if not has_issues:
            yield {"type": "token", "text": f"✅ All inspected streams for `{target}` are healthy! New segments are advancing on schedule with healthy segment ages and valid media sequences.\n"}
            return

        for d in diagnoses:
            if not d["issues"]:
                continue
            yield {"type": "token", "text": f"#### 🔴 **Server: `{d['domain']}`** (Status: `{d['status']}` | RTT: `{d['rtt']}ms` | Loss: `{d['loss'] or 0}%`)\n"}
            for iss in d["issues"]:
                yield {"type": "token", "text": (
                    f"- **Channel**: `{iss['channel']}` (`{iss['role']}`)\n"
                    f"  * **Failure Mode**: `{iss['mode']}`\n"
                    f"  * **Root Cause Diagnosis**: {iss['rca']}\n"
                    f"  * **Telemetry**: Segment Age: `{iss['age']}s` | Target: `{iss['target']}s` | Baseline: `{iss['baseline'] or 'N/A'}s`\n"
                    f"  * **Verification Command**:\n"
                    f"    ```bash\n    {iss['cmd']}\n    ```\n"
                )}
            yield {"type": "token", "text": "\n"}

    def tool_get_pool_status(self, args: dict) -> Iterator[dict]:
        """Show In-Memory Telemetry Data Pool metrics, hit ratio, and freshness."""
        st = telemetry_pool.stats()
        text = (
            f"⚡ **In-Memory Telemetry Data Pool Status**\n\n"
            f"- **State**: `ACTIVE` (In-Memory Python Pool)\n"
            f"- **Hit Ratio**: `{st['hit_ratio_pct']}%` ({st['hits']} hits / {st['misses']} misses)\n"
            f"- **Cached Nodes**: `{st['node_count']}` servers\n"
            f"- **Indexed Channels**: `{st['channel_count']}` channels\n"
            f"- **Cache Freshness**: `{st['cached_age_s']}s` ago (TTL: `{st['ttl_s']}s`)\n"
            f"- **In-Memory Writes**: `{st['writes']}` updates\n"
            f"- **Status**: `{'🟢 Fresh (Sub-millisecond access)' if st['is_fresh'] else '🟡 Stale / Refreshing'}`\n\n"
            f"Queries from ChatBot tools and Web UI read directly from this pool in `< 0.1ms` without database I/O.\n"
        )
        yield {"type": "token", "text": text}

    def tool_run_traceroute(self, args: dict) -> Iterator[dict]:
        """Trigger an ICMP hop-by-hop traceroute to diagnose network transit issues."""
        from chatbot import Snapshot, resolve_report_target

        target = str(args.get("node") or "").strip()
        if not target:
            yield {"type": "token", "text": "Please provide a target server domain for traceroute.\n"}
            return

        found = resolve_report_target(Snapshot(self.driver), target)
        if found.get("problem"):
            yield {"type": "token", "text": found["problem"]}
            return
        node_id = found["node"]
        if not node_id:
            yield {"type": "token", "text": "Cannot run traceroute on 'all' nodes at once. Please specify a single server domain.\n"}
            return

        tracer = getattr(self.chatbot, "tracer", None)
        if not tracer:
            yield {"type": "token", "text": "Traceroute manager is not available on this server.\n"}
            return

        records = self.driver.execute_query(
            "MATCH (n:Domain {domain: $id}) RETURN n.domain AS id, n.server_ip AS server_ip", id=node_id
        ).records
        if not records:
            yield {"type": "token", "text": f"Server `{node_id}` not found in topology graph.\n"}
            return

        target_node = dict(records[0])
        try:
            tracer.start(target_node, trigger="manual")
            ip_str = f" (`{target_node.get('server_ip')}`)" if target_node.get("server_ip") else ""
            yield {"type": "token", "text": f"🌐 Initiated background traceroute to **{node_id}**{ip_str}.\n"
                                            f"Hop-by-hop latency and packet drop telemetry will update live on the dashboard.\n"}
        except Exception as e:
            yield {"type": "token", "text": f"Could not start traceroute to `{node_id}`: {e}\n"}

    def tool_check_failover_status(self, args: dict) -> Iterator[dict]:
        """Inspect failover redundancy state across channels."""
        from chatbot import Snapshot
        snap = Snapshot(self.driver)
        raw_ch = str(args.get("channel") or "all").strip().lower()

        channels_to_check = snap.channels
        if raw_ch not in ("", "all", "*", "everything"):
            matched = next((c for c in snap.channels if c.lower() == raw_ch), None)
            if not matched:
                yield {"type": "token", "text": f"Channel '{raw_ch}' not found. Available: {', '.join(snap.channels.keys())}.\n"}
                return
            channels_to_check = {matched: snap.channels[matched]}

        on_backup = []
        on_main = []
        down_no_feed = []
        no_backup_redundancy = []

        for ch, chain in channels_to_check.items():
            feed_state = snap.feed(chain)
            has_backup = bool(chain.get("BackupLink"))
            if not has_backup:
                no_backup_redundancy.append(ch)

            spider = snap.spider_for(ch) or {}
            failover_notes = spider.get("failover") or []

            if feed_state == "backup":
                on_backup.append((ch, chain, failover_notes))
            elif feed_state == "main":
                on_main.append(ch)
            else:
                down_no_feed.append(ch)

        yield {"type": "token", "text": "### 🛡️ Failover & Redundancy Status\n\n"}
        if on_backup:
            yield {"type": "token", "text": f"⚠️ **Active Failover ({len(on_backup)} channels running on BackupLink):**\n"}
            for ch, chain, notes in on_backup:
                note_str = f" — *{notes[0]}*" if notes else " (MainInput is DOWN; BackupLink is serving)"
                yield {"type": "token", "text": f"- **{ch}**: Running on Backup{note_str}\n"}
            yield {"type": "token", "text": "\n"}
        else:
            yield {"type": "token", "text": "✓ No channels are currently running on failover backup links.\n\n"}

        if down_no_feed:
            yield {"type": "token", "text": f"🔴 **Service Outage ({len(down_no_feed)} channels completely DOWN):**\n"}
            for ch in down_no_feed:
                yield {"type": "token", "text": f"- **{ch}**: Both MainInput and BackupLink are down or unreachable!\n"}
            yield {"type": "token", "text": "\n"}

        if no_backup_redundancy:
            yield {"type": "token", "text": f"⚠️ **Redundancy Risk ({len(no_backup_redundancy)} channels lack BackupLink):**\n"}
            yield {"type": "token", "text": f"The following channels have only a single input (Single Point of Failure): {', '.join(no_backup_redundancy)}.\n\n"}

        yield {"type": "token", "text": f"**Summary:** {len(on_main)} operational on MainInput, {len(on_backup)} on BackupLink, {len(down_no_feed)} down.\n"}

    def tool_get_incident_history(self, args: dict) -> Iterator[dict]:
        """Retrieve chronological outage and recovery timeline from server incident logs."""
        from chatbot import Snapshot, ist
        from metrics import store

        target = str(args.get("node_or_channel") or "all").strip().lower()
        limit = int(args.get("limit") or 10)
        limit = max(1, min(limit, 30))

        records = [{"domain": r["domain"]} for r in self.driver.execute_query(
            "MATCH (n:Domain) WHERE NOT n.domain ENDS WITH '.invalid' RETURN n.domain AS domain"
        ).records]
        incidents = store()

        snap = Snapshot(self.driver)
        target_nodes = None
        target_label = "all nodes"
        if target not in ("", "all", "all nodes", "*"):
            if target in [c.lower() for c in snap.channels]:
                matched_channel = next(c for c in snap.channels if c.lower() == target)
                target_nodes = {e["node"] for role in snap.channels[matched_channel].values() for e in role}
                target_label = f"channel {matched_channel}"
            else:
                target_nodes = {r["domain"] for r in records if target in r["domain"].lower()}
                target_label = f"server '{target}'"

        all_events = []
        for r in records:
            d = r["domain"]
            if target_nodes is not None and d not in target_nodes:
                continue
            for entry in incidents.incidents(d, limit=200):
                entry["node"] = d
                all_events.append(entry)

        if not all_events:
            yield {"type": "token", "text": f"No incident logs recorded for {target_label}.\n"}
            return

        def parse_ts(item):
            ts = item.get("timestamp") or item.get("at") or 0
            if isinstance(ts, (int, float)):
                return ts
            try:
                from datetime import datetime
                return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
            except Exception:
                return 0

        all_events.sort(key=parse_ts, reverse=True)
        display_events = all_events[:limit]

        yield {"type": "token", "text": f"### 📜 Incident & Outage Timeline ({target_label})\n\n"}
        for ev in display_events:
            kind = ev.get("type", "EVENT")
            cat = str(ev.get("category") or "").replace("_", " ")
            node = ev.get("node", "unknown")
            ts = ist(ev.get("timestamp")) or "Recently"
            dur = ev.get("durationS")
            dur_str = f" · Duration: {round(dur, 1)}s" if dur is not None else ""
            urls = ev.get("failedUrls") or []
            url_str = f" · Details: {urls[0].get('detail', '')}" if urls and isinstance(urls[0], dict) else ""
            consec = ev.get("consecutiveFailures")
            consec_str = f" ({consec} checks)" if consec else ""

            icon = "🔴" if kind == "OUTAGE" else "✓" if kind == "RECOVERY" else "⚠️"
            yield {"type": "token", "text": f"- {icon} **{ts}** — **{kind}** on `{node}`\n"
                                            f"  *Category:* {cat or 'General'}{dur_str}{consec_str}{url_str}\n"}

        if len(all_events) > limit:
            yield {"type": "token", "text": f"\n*(Showing {limit} of {len(all_events)} total recorded incidents)*\n"}

    def tool_audit_topology(self, args: dict) -> Iterator[dict]:
        """Audit the media stream graph topology for architectural weaknesses."""
        from chatbot import Snapshot
        from nodes import current_topology

        snap = Snapshot(self.driver)
        edges = current_topology(self.driver)

        all_domains = set(snap.nodes.keys())
        connected_domains = set()
        for e in edges:
            connected_domains.add(e.source)
            connected_domains.add(e.target)

        orphan_nodes = all_domains - connected_domains
        missing_backup = [ch for ch, chain in snap.channels.items() if not chain.get("BackupLink")]
        missing_main = [ch for ch, chain in snap.channels.items() if not chain.get("MainInput")]
        missing_final = [ch for ch, chain in snap.channels.items() if not chain.get("FinalLink")]

        feeds_target = {e.target for e in edges if e.type == "FEEDS"}
        produces_source = {e.source for e in edges if e.type == "PRODUCES"}
        transcoders = {d for d, n in snap.nodes.items() if "Transcoding" in n.get("roles", [])}
        transcoders_unfed = transcoders - feeds_target
        transcoders_unproducing = transcoders - produces_source

        yield {"type": "token", "text": "### 🩺 Topology Health & Redundancy Audit\n\n"}
        issues_found = False

        if missing_backup:
            issues_found = True
            yield {"type": "token", "text": f"⚠️ **Single Point of Failure (SPOF) — Missing BackupLink ({len(missing_backup)} channels):**\n"}
            yield {"type": "token", "text": f"These channels have no redundant input configured: {', '.join(missing_backup)}.\n\n"}

        if missing_main:
            issues_found = True
            yield {"type": "token", "text": f"🔴 **No Main Input ({len(missing_main)} channels):**\n"}
            yield {"type": "token", "text": f"These channels have no MainInput feeding them: {', '.join(missing_main)}.\n\n"}

        if missing_final:
            issues_found = True
            yield {"type": "token", "text": f"🔴 **Broken Pipeline — Missing FinalLink ({len(missing_final)} channels):**\n"}
            yield {"type": "token", "text": f"These channels have no output FinalLink node defined: {', '.join(missing_final)}.\n\n"}

        if orphan_nodes:
            issues_found = True
            yield {"type": "token", "text": f"⚠️ **Orphan Servers ({len(orphan_nodes)} nodes):**\n"}
            yield {"type": "token", "text": f"Nodes present in database with no active flow relationships: {', '.join(orphan_nodes)}.\n\n"}

        if transcoders_unfed:
            issues_found = True
            yield {"type": "token", "text": f"⚠️ **Unfed Transcoders ({len(transcoders_unfed)} nodes):**\n"}
            yield {"type": "token", "text": f"Transcoder domains with no incoming `FEEDS` relationship: {', '.join(transcoders_unfed)}.\n\n"}

        if transcoders_unproducing:
            issues_found = True
            yield {"type": "token", "text": f"⚠️ **Transcoders Producing Nothing ({len(transcoders_unproducing)} nodes):**\n"}
            yield {"type": "token", "text": f"Transcoder domains with no outgoing `PRODUCES` relationship to a Final: {', '.join(sorted(transcoders_unproducing))}.\n\n"}

        if not issues_found:
            yield {"type": "token", "text": "✓ **Audit Passed!** Every channel has a Main input, a backup and a Final; every transcoder is fed and produces; no orphan nodes.\n\n"}

        yield {"type": "token", "text": f"**Audit Statistics:** {len(snap.channels)} channels, {len(all_domains)} servers, {len(edges)} pipeline relationships.\n"}

    def tool_scrapy(self, args: dict) -> Iterator[dict]:
        """Scrapy crawler: crawls any target URL to extract, audit, and categorize all hyperlinks.
        Categorizes links into Internal, External Outbound, and Streaming Media links (.m3u8, .ts, .mpd, .mp4)."""
        import httpx
        from html.parser import HTMLParser
        from urllib.parse import urljoin, urlparse, urldefrag
        from collections import Counter

        raw_url = str(args.get("url") or "").strip()
        if not raw_url:
            yield {"type": "token", "text": "⚠️ Please provide a URL to crawl and scrape (e.g. `scrapy https://stream.ottlive.co.in` or `scrapy http://jio.ottlive.co.in/gtcnews`).\n"}
            return

        if not raw_url.startswith(("http://", "https://")):
            raw_url = "https://" + raw_url

        max_links = int(args.get("max_links") or 100)
        filter_type = str(args.get("filter_type") or "all").lower().strip()

        yield {"type": "token", "text": f"🕷️ **Scrapy Crawler initiating scan** on `{raw_url}`...\n\n"}

        t0 = time.monotonic()
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml,application/vnd.apple.mpegurl,*/*;q=0.8",
        }

        try:
            with httpx.Client(timeout=httpx.Timeout(15.0, connect=6.0), follow_redirects=True, headers=headers) as client:
                response = client.get(raw_url)
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                final_url = str(response.url)
                status_code = response.status_code
                content_type = response.headers.get("content-type", "")
                content_length = len(response.content)
                text_content = response.text
        except httpx.ConnectError:
            yield {"type": "token", "text": f"❌ **Scrapy Connection Error**: Could not connect to host at `{raw_url}`. Please verify the domain and port.\n"}
            return
        except httpx.TimeoutException:
            yield {"type": "token", "text": f"⏱️ **Scrapy Timeout**: Target `{raw_url}` did not respond within 15 seconds.\n"}
            return
        except Exception as e:
            yield {"type": "token", "text": f"❌ **Scrapy Error**: Request failed: {e}\n"}
            return

        base_parsed = urlparse(final_url)
        base_domain = base_parsed.netloc.lower()

        # HTML / HLS Link Extractor
        class WebLinkParser(HTMLParser):
            def __init__(self, base: str):
                super().__init__()
                self.base = base
                self.links: List[dict] = []
                self.title = ""
                self._in_title = False
                self._current_tag = None
                self._current_href = None
                self._current_text = []

            def handle_starttag(self, tag, attrs):
                attrs_dict = dict(attrs)
                self._current_tag = tag
                if tag == "title":
                    self._in_title = True
                
                href = attrs_dict.get("href") or attrs_dict.get("src") or attrs_dict.get("data-src")
                if href and tag in ("a", "link", "script", "iframe", "video", "source", "img", "embed"):
                    self._current_href = href.strip()
                    self._current_text = []

            def handle_endtag(self, tag):
                if tag == "title":
                    self._in_title = False
                if self._current_href and tag in ("a", "link", "script", "iframe", "video", "source", "img", "embed"):
                    anchor_text = " ".join("".join(self._current_text).split())
                    self.links.append({
                        "tag": tag,
                        "raw": self._current_href,
                        "text": anchor_text or "(unlabeled)",
                    })
                    self._current_href = None
                    self._current_text = []

            def handle_data(self, data):
                if self._in_title:
                    self.title += data.strip()
                if self._current_href:
                    self._current_text.append(data)

        # Parse links
        page_title = "Unknown Page"
        raw_found_links = []

        # Check if HLS manifest
        is_hls = "#EXTM3U" in text_content or ".m3u8" in final_url.lower() or "mpegurl" in content_type.lower()
        if is_hls:
            page_title = f"HLS Stream Manifest ({base_domain})"
            for line in text_content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    uri_match = re.search(r'URI=["\']([^"\']+)["\']', line)
                    if uri_match:
                        raw_found_links.append({"tag": "hls-key", "raw": uri_match.group(1), "text": "HLS Key/Track"})
                else:
                    raw_found_links.append({"tag": "hls-segment", "raw": line, "text": "Media Chunk / Playlist"})
        else:
            parser = WebLinkParser(final_url)
            try:
                parser.feed(text_content)
                page_title = parser.title or base_domain
                raw_found_links = parser.links
            except Exception:
                page_title = base_domain
                for m in re.finditer(r'<a\s+(?:[^>]*?\s+)?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', text_content, re.IGNORECASE | re.DOTALL):
                    raw_found_links.append({"tag": "a", "raw": m.group(1), "text": re.sub(r'<[^>]+>', '', m.group(2)).strip()})
                for m in re.finditer(r'<(?:source|video|script|link)\s+(?:[^>]*?\s+)?(?:src|href)=["\']([^"\']+)["\']', text_content, re.IGNORECASE):
                    raw_found_links.append({"tag": "media", "raw": m.group(1), "text": ""})

        # Process and categorize links
        internal_links = []
        external_links = []
        media_links = []
        external_domains = Counter()

        seen_urls = set()
        media_extensions = (
            ".m3u8", ".ts", ".mpd", ".mp4", ".m4s", ".aac", ".mp3", ".webm", ".mkv", ".flv", ".f4v"
        )

        for item in raw_found_links:
            raw = item.get("raw", "").strip()
            if not raw or raw.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
                continue

            full_url = urljoin(final_url, raw)
            clean_url, _ = urldefrag(full_url)
            if not clean_url:
                continue

            parsed = urlparse(clean_url)
            if not parsed.scheme or not parsed.netloc:
                continue

            if clean_url in seen_urls:
                continue
            seen_urls.add(clean_url)

            link_entry = {
                "url": clean_url,
                "text": item.get("text") or "(no anchor text)",
                "tag": item.get("tag", "a"),
                "domain": parsed.netloc.lower(),
            }

            path_lower = parsed.path.lower()
            if any(path_lower.endswith(ext) for ext in media_extensions) or "m3u8" in clean_url.lower() or item.get("tag") in ("hls-segment", "video", "source"):
                media_links.append(link_entry)

            target_domain = parsed.netloc.lower()
            if target_domain == base_domain or target_domain.endswith("." + base_domain):
                internal_links.append(link_entry)
            else:
                external_links.append(link_entry)
                external_domains[target_domain] += 1

        total_extracted = len(seen_urls)
        status_icon = "🟢" if status_code < 400 else "🔴"

        # Build output presentation
        output = [
            f"### 🕷️ Scrapy Audit Report: **{page_title}**\n\n",
            f"| Metric | Details |\n",
            f"| :--- | :--- |\n",
            f"| **Target URL** | `{final_url}` |\n",
            f"| **HTTP Status** | {status_icon} `{status_code}` ({elapsed_ms} ms) |\n",
            f"| **Content Type** | `{content_type.split(';')[0]}` ({round(content_length / 1024, 1)} KB) |\n",
            f"| **Total Unique Links** | **{total_extracted}** |\n",
            f"| **Internal Links** | **{len(internal_links)}** ({round(len(internal_links) / max(1, total_extracted) * 100)}%) |\n",
            f"| **External Links** | **{len(external_links)}** ({round(len(external_links) / max(1, total_extracted) * 100)}%) |\n",
            f"| **Media / Streams** | **{len(media_links)}** |\n\n",
        ]

        if external_domains:
            top_ext = ", ".join(f"`{d}` ({cnt})" for d, cnt in external_domains.most_common(5))
            output.append(f"**Top Outbound External Domains:** {top_ext}\n\n")

        # Display links according to filter
        show_internal = filter_type in ("all", "internal")
        show_external = filter_type in ("all", "external")
        show_media = filter_type in ("all", "media")

        # Media Links Section
        if show_media and media_links:
            output.append(f"#### 📺 Streaming Media & Chunk Links ({len(media_links)})\n")
            for m in media_links[:25]:
                output.append(f"- 🎬 [`{m['url']}`]({m['url']})\n")
            if len(media_links) > 25:
                output.append(f"  *(+{len(media_links) - 25} additional media links truncated)*\n")
            output.append("\n")

        # Internal Links Section
        if show_internal:
            output.append(f"#### 🔗 Internal Links ({len(internal_links)} on `{base_domain}`)\n")
            if not internal_links:
                output.append("*(No internal hyperlinks discovered)*\n")
            else:
                lim = min(len(internal_links), max_links if filter_type == "internal" else 25)
                for item in internal_links[:lim]:
                    txt = item["text"]
                    if len(txt) > 40:
                        txt = txt[:37] + "..."
                    output.append(f"- [{txt}]({item['url']}) — `{item['url']}`\n")
                if len(internal_links) > lim:
                    output.append(f"  *(+{len(internal_links) - lim} additional internal links omitted)*\n")
            output.append("\n")

        # External Links Section
        if show_external:
            output.append(f"#### 🌐 External Outbound Links ({len(external_links)})\n")
            if not external_links:
                output.append("*(No external outbound links discovered)*\n")
            else:
                lim = min(len(external_links), max_links if filter_type == "external" else 25)
                for item in external_links[:lim]:
                    txt = item["text"]
                    if len(txt) > 30:
                        txt = txt[:27] + "..."
                    output.append(f"- [{txt}]({item['url']}) → `{item['domain']}`\n")
                if len(external_links) > lim:
                    output.append(f"  *(+{len(external_links) - lim} additional external links omitted)*\n")
            output.append("\n")

        # Persist Scrapy crawl findings in Neo4j with vector embedding for LLM GraphRAG
        try:
            from activity import record_activity
            embedder = getattr(self.chatbot, "embedder", None)
            top_ext_str = ", ".join(f"{d} ({c})" for d, c in external_domains.most_common(5))
            crawl_summary = (
                f"Scrapy crawl on {final_url} (title: '{page_title}'): HTTP {status_code} ({elapsed_ms}ms). "
                f"Discovered {total_extracted} links: {len(internal_links)} internal, {len(external_links)} external, "
                f"{len(media_links)} streaming media. Top outbound domains: {top_ext_str or 'none'}."
            )
            record_activity(
                driver=self.driver,
                embedder=embedder,
                act_type="scrapy",
                title=f"Scrapy Crawl: {page_title or base_domain}",
                summary=crawl_summary,
                target=final_url,
                domain=base_domain,
                details={
                    "url": final_url,
                    "domain": base_domain,
                    "title": page_title,
                    "status_code": status_code,
                    "elapsed_ms": elapsed_ms,
                    "total_links": total_extracted,
                    "internal_count": len(internal_links),
                    "external_count": len(external_links),
                    "media_count": len(media_links),
                    "top_external_domains": dict(external_domains.most_common(10)),
                    "sample_internal": [i["url"] for i in internal_links[:10]],
                    "sample_external": [e["url"] for e in external_links[:10]],
                    "sample_media": [m["url"] for m in media_links[:10]],
                }
            )
            output.append("\n💾 *Activity and extracted links saved to graph database with 384-dim embeddings for LLM context retrieval.*\n")
        except Exception:
            pass

        yield {"type": "token", "text": "".join(output)}

    # ---------- Autonomous CRUD Operations (Strict Double Confirmation) ----------

    def tool_add_channel(self, args: dict, confirmed: bool = False) -> Iterator[dict]:
        """Add several links of one channel with a single confirmation, then wire its pipeline."""
        from nodes import StreamLink, connect_new_channels, group_by_domain, upsert_nodes
        from pydantic import ValidationError

        channel = str(args.get("channel") or "").strip()
        raw = args.get("links") if isinstance(args.get("links"), list) else []
        links, problems = [], []
        for i, item in enumerate(raw):
            item = item if isinstance(item, dict) else {}
            role, url = str(item.get("role") or "").strip(), str(item.get("url") or "").strip()
            try:
                links.append(StreamLink(label=role, url=url, channel=channel))
            except ValidationError as e:
                problems.append(f"link {i + 1} ({role or '?'} {url or '?'}): " +
                                ", ".join(f"{err['loc'][0]} {err['msg']}" for err in e.errors()))
        if problems or not links:
            yield {"type": "token", "text": "❌ I couldn't add that channel:\n" +
                   ("\n".join(f"- {p}" for p in problems) or "- no links given") + "\n"}
            return
        rows = [{"role": l.label, "url": str(l.url), "node_id": l.node_id} for l in links]
        role_name = {"MainInput": "Main", "BackupLink": "Backup", "Transcoding": "Transcoding", "FinalLink": "Final"}

        if not confirmed:
            action_id = f"act_{uuid.uuid4().hex[:8]}"
            summary = f"Add channel '{channel}' with {len(links)} link(s) and connect its pipeline"
            PENDING_ACTIONS[action_id] = {
                "action": "add_channel", "title": f"Add channel {channel}",
                "params": {"channel": channel, "links": [{"role": r["role"], "url": r["url"]} for r in rows]},
                "created_at": time.time(), "summary": summary,
            }
            yield {
                "type": "action_confirm", "action_id": action_id, "action": "add_channel",
                "title": f"Add channel: {channel}", "severity": "info", "summary": summary,
                "preview": [f"{role_name.get(r['role'], r['role'])}: {r['url']} (node {r['node_id']})" for r in rows]
                           + ["Then: Main/Backup FEEDS Transcoding, Transcoding PRODUCES Final"],
                "params": {"channel": channel, "links": rows},
            }
            yield {"type": "token", "text": f"⚠️ **Confirmation required: add channel `{channel}`**\n\n"
                   + "".join(f"- **{role_name.get(r['role'], r['role'])}**: `{r['url']}`\n" for r in rows)
                   + f"\nIts pipeline will be connected too. Click **Allow** above (or reply `CONFIRM {action_id}`).\n"}
            return

        upsert_nodes(self.driver, group_by_domain(links))
        connected = connect_new_channels(self.driver, {channel})
        missing = [n for n, r in (("Main or Backup", ("MainInput", "BackupLink")), ("Final", ("FinalLink",)))
                   if not any(l.label in r for l in links)]
        yield {"type": "token", "text": f"✓ **Channel `{channel}` added** with {len(links)} link(s).\n"
               + (("Connected:\n" + "".join(f"- `{e.source}` {e.type} `{e.target}`\n" for e in connected)) if connected else "")
               + (f"Still missing: {', '.join(missing)}. Add it so the channel can be monitored end to end.\n" if missing else
                  "Its spider starts on the next check (within a few seconds), then it shows on the Workflow page.\n")}

    def tool_add_stream_link(self, args: dict, confirmed: bool = False) -> Iterator[dict]:
        """Add a new stream link entry with double confirmation."""
        from nodes import StreamLink, group_by_domain, upsert_nodes
        from pydantic import ValidationError

        channel = str(args.get("channel") or "").strip()
        role = str(args.get("role") or "").strip()
        url = str(args.get("url") or "").strip()

        try:
            link = StreamLink(label=role, url=url, channel=channel)
        except ValidationError as e:
            err_msg = ", ".join(f"{err['loc'][0]}: {err['msg']}" for err in e.errors())
            yield {"type": "token", "text": f"❌ Validation failed for new stream link: {err_msg}\n"}
            return

        node_id = link.node_id

        # Phase 1: Preview & Confirmation Request
        if not confirmed:
            action_id = f"act_{uuid.uuid4().hex[:8]}"
            summary = f"Add {role} for channel '{channel}' on node '{node_id}' ({url})"
            PENDING_ACTIONS[action_id] = {
                "action": "add_stream_link",
                "title": f"Add {role}",
                "params": {"channel": channel, "role": role, "url": url},
                "created_at": time.time(),
                "summary": summary,
            }

            yield {
                "type": "action_confirm",
                "action_id": action_id,
                "action": "add_stream_link",
                "title": f"Add Stream Link ({role})",
                "severity": "info",
                "summary": summary,
                "warning": None,
                "params": {"channel": channel, "role": role, "url": url, "node_id": node_id},
            }
            yield {
                "type": "token",
                "text": f"⚠️ **Confirmation Required: Add Stream Link**\n\n"
                        f"- **Channel**: `{channel}`\n"
                        f"- **Role**: `{role}`\n"
                        f"- **Stream URL**: `{url}`\n"
                        f"- **Target Domain / Node ID**: `{node_id}`\n\n"
                        f"It will also be connected into `{channel}`'s pipeline (Main/Backup → Transcoding → Final).\n"
                        f"Click **Allow** above (or reply `CONFIRM {action_id}`) to write this entry to Neo4j.\n"
            }
            return

        # Phase 2: Execution upon explicit confirmation
        from nodes import connect_new_channels
        domain_nodes = group_by_domain([link])
        upsert_nodes(self.driver, domain_nodes)
        connected = connect_new_channels(self.driver, {channel})  # wire it into the channel's pipeline

        yield {
            "type": "token",
            "text": f"✓ **Stream Link Created Successfully!**\n\n"
                    f"Saved `{role}` for channel `{channel}` on node `{node_id}`.\n"
                    + (("Connected it into the pipeline:\n" + "".join(f"- `{e.source}` {e.type} `{e.target}`\n" for e in connected))
                       if connected else "No new relationships were needed (nothing else in the channel to connect it to yet).\n")
                    + "The graph canvas has been updated.\n"
        }

    def tool_connect_pipeline_relationship(self, args: dict, confirmed: bool = False) -> Iterator[dict]:
        """Create a pipeline relationship (FEEDS/PRODUCES) with double confirmation."""
        from chatbot import Snapshot
        from nodes import EDGE_SHAPES, Relationship, current_topology, replace_topology

        source = str(args.get("source") or "").strip()
        rel_type = str(args.get("type") or "").strip().upper()
        target = str(args.get("target") or "").strip()

        if rel_type not in ("FEEDS", "PRODUCES"):
            yield {"type": "token", "text": f"❌ Invalid relationship type '{rel_type}'. Must be FEEDS or PRODUCES.\n"}
            return

        snap = Snapshot(self.driver)
        if source not in snap.nodes:
            yield {"type": "token", "text": f"❌ Source node `{source}` not found in graph.\n"}
            return
        if target not in snap.nodes:
            yield {"type": "token", "text": f"❌ Target node `{target}` not found in graph.\n"}
            return

        src_roles = snap.nodes[source].get("roles", [])
        tgt_roles = snap.nodes[target].get("roles", [])
        allowed_src, allowed_tgt = EDGE_SHAPES[rel_type]
        if not (set(src_roles) & set(allowed_src)):
            yield {"type": "token", "text": f"❌ Invalid topology: {rel_type} must originate from {allowed_src}, but `{source}` has roles {src_roles}.\n"}
            return
        if not (set(tgt_roles) & set(allowed_tgt)):
            yield {"type": "token", "text": f"❌ Invalid topology: {rel_type} must terminate at {allowed_tgt}, but `{target}` has roles {tgt_roles}.\n"}
            return

        new_rel = Relationship(source=source, type=rel_type, target=target)
        existing = current_topology(self.driver)
        if any(r.source == source and r.type == rel_type and r.target == target for r in existing):
            yield {"type": "token", "text": f"ℹ️ Relationship `{source}` -[{rel_type}]-> `{target}` already exists.\n"}
            return

        # Phase 1: Preview & Confirmation Request
        if not confirmed:
            action_id = f"act_{uuid.uuid4().hex[:8]}"
            summary = f"Connect `{source}` -[:{rel_type}]-> `{target}`"
            PENDING_ACTIONS[action_id] = {
                "action": "connect_pipeline_relationship",
                "title": f"Connect {rel_type}",
                "params": {"source": source, "type": rel_type, "target": target},
                "created_at": time.time(),
                "summary": summary,
            }

            yield {
                "type": "action_confirm",
                "action_id": action_id,
                "action": "connect_pipeline_relationship",
                "title": f"Connect Relationship ({rel_type})",
                "severity": "info",
                "summary": summary,
                "warning": None,
                "params": {"source": source, "type": rel_type, "target": target},
            }
            yield {
                "type": "token",
                "text": f"⚠️ **Confirmation Required: Create Relationship**\n\n"
                        f"- **Source Node**: `{source}` ({', '.join(src_roles)})\n"
                        f"- **Type**: `{rel_type}`\n"
                        f"- **Target Node**: `{target}` ({', '.join(tgt_roles)})\n\n"
                        f"Click **Allow** above (or reply `CONFIRM {action_id}`) to write this edge to Neo4j.\n"
            }
            return

        # Phase 2: Execution upon explicit confirmation
        updated_rels = existing + [new_rel]
        replace_topology(self.driver, updated_rels)
        telemetry_pool.invalidate()

        yield {
            "type": "token",
            "text": f"✓ **Relationship Connected Successfully!**\n\n"
                    f"Created edge `{source}` -[:{rel_type}]-> `{target}` in Neo4j.\n"
                    f"Topology layout has been updated.\n"
        }

    def tool_update_stream_link(self, args: dict, confirmed: bool = False) -> Iterator[dict]:
        """Update an existing stream link's URL, role, or channel with double confirmation."""
        from pydantic import ValidationError
        from nodes import StreamLink

        old_url = str(args.get("old_url") or "").strip()
        new_url = str(args.get("new_url") or old_url).strip()
        new_channel = args.get("new_channel")
        new_role = args.get("new_role")

        if not old_url:
            yield {"type": "token", "text": "❌ Missing required parameter `old_url`.\n"}
            return

        # Find existing link across all nodes via in-memory telemetry pool
        node_cache = telemetry_pool.get_nodes(self.driver)

        found_node = None
        found_link = None
        for domain, n_info in node_cache.items():
            links = n_info.get("links", [])
            for l in links:
                if l.get("url") == old_url:
                    found_node = domain
                    found_link = l
                    break
            if found_link:
                break

        if not found_link:
            yield {"type": "token", "text": f"❌ Link with URL `{old_url}` not found in any graph node.\n"}
            return

        updated_channel = str(new_channel).strip() if new_channel else found_link.get("channel")
        updated_role = str(new_role).strip() if new_role else found_link.get("role")

        # Phase 1: Preview & Confirmation Request
        if not confirmed:
            action_id = f"act_{uuid.uuid4().hex[:8]}"
            try:
                target_node = StreamLink(channel=updated_channel, label=updated_role, url=new_url).node_id
            except ValidationError as e:
                yield {"type": "token", "text": f"❌ The new link is not valid: {e.errors()[0]['msg']}\n"}
                return
            summary = (f"Update link on `{found_node}` from {found_link.get('role')} ({found_link.get('channel')}) "
                       f"to {updated_role} ({updated_channel})"
                       + (f"; it moves to server `{target_node}`" if target_node != found_node else ""))
            PENDING_ACTIONS[action_id] = {
                "action": "update_stream_link",
                "title": "Update Stream Link",
                "params": {"old_url": old_url, "new_url": new_url, "new_channel": updated_channel, "new_role": updated_role},
                "created_at": time.time(),
                "summary": summary,
            }

            yield {
                "type": "action_confirm",
                "action_id": action_id,
                "action": "update_stream_link",
                "title": "Update Stream Link",
                "severity": "warning",
                "summary": summary,
                "warning": None,
                "params": {
                    "old": found_link,
                    "new": {"url": new_url, "role": updated_role, "channel": updated_channel},
                },
            }
            yield {
                "type": "token",
                "text": f"⚠️ **Confirmation Required: Update Stream Link**\n\n"
                        f"- **Current**: Channel `{found_link.get('channel')}`, Role `{found_link.get('role')}`, URL `{old_url}`\n"
                        f"- **Proposed**: Channel `{updated_channel}`, Role `{updated_role}`, URL `{new_url}`\n\n"
                        f"Click **Allow** above (or reply `CONFIRM {action_id}`) to update this entry.\n"
            }
            return

        # Phase 2: Execution upon explicit confirmation. The link goes to the node of its own server (a new URL on
        # another host moves there), and the channel's edges follow it.
        from nodes import move_link
        try:
            new_link = StreamLink(channel=updated_channel, label=updated_role, url=new_url)
            moved = move_link(self.driver, old_url, new_link)
        except (ValidationError, ValueError) as e:
            yield {"type": "token", "text": f"❌ Could not update the link: {e}\n"}
            return
        telemetry_pool.invalidate()
        where = (f"Moved it from `{moved['from']}` to `{moved['to']}`." if moved["from"] != moved["to"]
                 else f"Updated it on `{moved['to']}`.")
        yield {
            "type": "token",
            "text": f"✓ **Stream Link Updated Successfully!**\n\n{where} Channel `{updated_channel}` ({updated_role}).\n"
                    + ("Removed old connections:\n" + "".join(f"- `{e.source}` {e.type} `{e.target}`\n" for e in moved["removed"])
                       if moved["removed"] else "")
                    + ("Connected it into the pipeline:\n" + "".join(f"- `{e.source}` {e.type} `{e.target}`\n" for e in moved["added"])
                       if moved["added"] else "")
        }

    def tool_delete_node(self, args: dict, confirmed: bool = False) -> Iterator[dict]:
        """Delete a server node with comprehensive blast-radius preview and strict double confirmation."""
        from chatbot import Snapshot

        raw_node = str(args.get("node") or "").strip()

        if not raw_node:
            yield {"type": "token", "text": "❌ Missing required parameter `node`.\n"}
            return

        records = self.driver.execute_query(
            "MATCH (n:Domain {domain: $d}) RETURN n.domain AS domain, n.links AS links, labels(n) AS labels",
            d=raw_node,
        ).records

        if not records:
            # Try fuzzy match
            snap = Snapshot(self.driver)
            matched = [d for d in snap.nodes if raw_node.lower() in d.lower()]
            if len(matched) == 1:
                raw_node = matched[0]
                records = self.driver.execute_query(
                    "MATCH (n:Domain {domain: $d}) RETURN n.domain AS domain, n.links AS links, labels(n) AS labels",
                    d=raw_node,
                ).records
            elif matched:
                yield {"type": "token", "text": f"Multiple nodes matched '{raw_node}': {', '.join(matched)}. Please specify exact domain.\n"}
                return
            else:
                yield {"type": "token", "text": f"❌ Node `{raw_node}` not found in the topology graph.\n"}
                return

        snap = Snapshot(self.driver)

        # Blast Radius Analysis
        impacted_channels = snap.nodes.get(raw_node, {}).get("channels", [])
        roles = snap.nodes.get(raw_node, {}).get("roles", [])

        # Check relationships attached
        edge_records = self.driver.execute_query(
            "MATCH (a:Domain {domain: $d})-[r]->(b:Domain) RETURN a.domain AS src, type(r) AS type, b.domain AS tgt "
            "UNION "
            "MATCH (a:Domain)-[r]->(b:Domain {domain: $d}) RETURN a.domain AS src, type(r) AS type, b.domain AS tgt",
            d=raw_node,
        ).records
        edges_to_delete = [f"`{r['src']}` -[{r['type']}]-> `{r['tgt']}`" for r in edge_records]

        # Check redundancy impact on channels
        critical_warnings = []
        for ch in impacted_channels:
            chain = snap.channels.get(ch, {})
            if "FinalLink" in roles:
                critical_warnings.append(f"Channel **{ch}** will lose its output FinalLink (channel will go COMPLETELY DARK).")
            elif "MainInput" in roles:
                if not chain.get("BackupLink"):
                    critical_warnings.append(f"Channel **{ch}** has NO BackupLink (deletion will cause a COMPLETE OUTAGE).")
                else:
                    critical_warnings.append(f"Channel **{ch}** will lose its primary MainInput and rely exclusively on BackupLink.")
            elif "Transcoding" in roles:
                critical_warnings.append(f"Transcoding node for **{ch}** will be detached.")

        # Phase 1: Preview & Confirmation Request
        if not confirmed:
            action_id = f"act_{uuid.uuid4().hex[:8]}"
            summary = f"Permanently delete node '{raw_node}' and detach {len(edges_to_delete)} relationships"
            warning_text = "\n".join(critical_warnings) if critical_warnings else "Node is not carrying active critical channels."

            PENDING_ACTIONS[action_id] = {
                "action": "delete_node",
                "title": f"Delete Node ({raw_node})",
                "params": {"node": raw_node},
                "created_at": time.time(),
                "summary": summary,
                "warning": warning_text,
            }

            yield {
                "type": "action_confirm",
                "action_id": action_id,
                "action": "delete_node",
                "title": f"Delete Server Node: {raw_node}",
                "severity": "danger",
                "summary": summary,
                "warning": warning_text,
                "params": {"node": raw_node, "roles": roles, "impacted_channels": impacted_channels},
            }
            yield {
                "type": "token",
                "text": f"🔴 **CRITICAL CONFIRMATION: Delete Server Node**\n\n"
                        f"You are about to permanently delete **`{raw_node}`** from the graph.\n\n"
                        f"### 💥 Blast Radius Impact Assessment:\n"
                        f"- **Node Roles**: {', '.join(roles)}\n"
                        f"- **Channels Affected ({len(impacted_channels)})**: {', '.join(impacted_channels) if impacted_channels else 'None'}\n"
                        f"- **Relationships Detached ({len(edges_to_delete)})**: {', '.join(edges_to_delete) if edges_to_delete else 'None'}\n\n"
                        f"**Outage Impact:**\n"
                        + "\n".join(f"  * ⚠️ {w}" for w in critical_warnings) + "\n\n"
                        f"This action is **irreversible**. Click **Allow** above (or reply `CONFIRM {action_id}`) to proceed.\n"
            }
            return

        # Phase 2: Execution upon explicit confirmation
        with self.driver.session() as session:
            # Clean spider runs if any
            session.run("MATCH (s:SpiderRun {finalLinkId: $d}) DETACH DELETE s", d=raw_node)
            # Detach and delete node
            session.run("MATCH (n:Domain {domain: $d}) DETACH DELETE n", d=raw_node)
        telemetry_pool.invalidate()

        yield {
            "type": "token",
            "text": f"✓ **Node Deleted Successfully!**\n\n"
                    f"Permanently removed `{raw_node}` and detached all its relationships from Neo4j.\n"
                    f"The graph canvas has been refreshed.\n"
        }
