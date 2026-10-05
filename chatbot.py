"""ChatBot: answers questions about the streams with an OpenRouter model, grounded by GraphRAG over Neo4j.

For every question the live data is written out as short text documents (an overview, one per channel, one per
server, the scheduler). graphrag.GraphRAG picks the ones to use from the graph itself: names in the question
(entity linking), nearest nodes by a Neo4j vector index over all-MiniLM-L6-v2 embeddings, and their
FEEDS / PRODUCES neighbourhood. The model's answer streams back token by token.

Needs OPENROUTER_API_KEY (https://openrouter.ai/keys). CHATBOT_MODEL (any OpenRouter model id),
CHATBOT_EMBED_MODEL and OPENROUTER_BASE_URL override the defaults. The embeddings are computed locally.
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterator, List, Optional, Tuple
from urllib.parse import quote

import httpx
import numpy as np

from graphrag import GraphRAG
from nodes import TEST_TLD, current_topology, load_json_list
from tools import Tools
from tracer_langsmith import get_langsmith_status, traceable

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "qwen/qwen3.8-27b"
DEFAULT_EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
# max_new_tokens=100, temperature=0.1. Reasoning off: answers are lookups in the retrieved facts, and reasoning
# tokens are billed as output and count against max_tokens (they could use it all up and leave no answer).
GENERATION = {"max_tokens": 700, "temperature": 0.1, "reasoning": {"enabled": False}}
MAX_STEPS = 5  # tool rounds per question before the model must answer
TOOL_RESULT_CHARS = 3500  # each tool result sent back to the model is cut to this
# Tools that change the graph stop the loop at their Allow/Deny card; reports and workbooks are shown as they are.
CHANGE_TOOLS = {"add_channel", "add_stream_link", "connect_pipeline_relationship", "update_stream_link", "delete_node"}
SHOW_TOOLS = {"generate_report", "generate_excel"}
# Questions that need reasoning go to the stronger model (CHATBOT_STRONG_MODEL) when one is configured.
HARD_QUESTION = re.compile(r"\b(why|root cause|cause|analy[sz]e|analysis|compare|explain|diagnos\w*|investigate|plan|"
                           r"what should|recommend|predict|forecast|correlat\w*|trend|impact|and then|step by step)\b", re.I)
DEFAULT_STRONG_MODEL = "deepseek/deepseek-v4-pro"  # larger than qwen3.8-27b and cheaper per token (OpenRouter, 2026-10)
AGENT_RULES = (
    "\n\nWorking with tools: you may call tools several times in a row, one step building on the last (e.g. rank the "
    "root causes, then run a traceroute to the top one). After the tools, answer in plain words: what you found, what "
    "it means, and what to do. Don't paste raw tables or tool output unless asked; quote the key numbers. If a tool "
    "fails, say so and use what you have. Changes to the graph only happen after the operator clicks Allow."
)
HISTORY_TURNS = 6  # earlier messages of the conversation sent with each question (3 questions + answers)
HISTORY_CHARS = 3000  # each earlier message is cut to this, so a long report analysis stays affordable
IST = timezone(timedelta(hours=5, minutes=30), "IST")  # no DST, so a fixed offset (slim images have no tzdata)
ROLES = ["MainInput", "BackupLink", "Transcoding", "FinalLink"]
ROLE_NAMES = {"MainInput": "Main", "BackupLink": "Backup", "Transcoding": "Transcoding", "FinalLink": "Final"}

# Kept plain and literal: small models follow short, direct instructions best.
SYSTEM = (
    "You answer questions about video channels using only the context and the conversation so far. "
    "Write for someone who is not an engineer: plain everyday words, short sentences, and explain any technical "
    "term in a few words the first time (e.g. 'segment age: how old the newest video piece is'). "
    "Keep it short: one or two sentences for a simple question; when asked for a summary, list or bullet points, "
    "give 3 to 6 short bullets. Words like 'it', 'its' or 'that' refer to what the conversation was just about. "
    "Copy names and times exactly. If a list in the context is none, say so.\n"
    "You have specialized tools you can call when requested:\n"
    "- generate_report: Full HTML report with charts and deep AI analysis\n"
    "- generate_excel: Download complete project data in Excel (.xlsx) with all nodes, spiders, incident logs, channels, streams & topology\n"
    "- diagnose_hls_stream: Deep diagnosis of HLS live stream failure modes (STALE_SEGMENTS, NO_SEGMENTS, STALE_SEQUENCE, FALLING_BEHIND, HTTP 404, freshness)\n"
    "- get_risk_forecast: Failure risk percentages, MTBF, MTTR, and anomaly causes\n"
    "- trigger_channel_crawl: Run an immediate upstream spider crawl for a channel or all channels\n"
    "- get_root_cause_ranking: Root cause analysis of current failure groups\n"
    "- get_early_warnings: Early warning anomaly signals (jitter, drift, loss) on running servers\n"
    "- run_traceroute: Start an ICMP hop-by-hop traceroute to diagnose network transit issues\n"
    "- check_failover_status: Inspect channels running on BackupLink and redundancy gaps\n"
    "- get_incident_history: Timeline of past OUTAGE, RECOVERY, and downtime incidents\n"
    "- audit_topology: Audit graph topology for SPOF (missing backups) and orphan servers\n"
    "- add_channel: Add a whole channel (several links: Main, Backup, Transcoding, Final) in one go and wire it\n"
    "- add_stream_link: Add ONE stream link to an existing channel (for two or more links use add_channel)\n"
    "- connect_pipeline_relationship: Connect FEEDS or PRODUCES relationship between nodes\n"
    "- update_stream_link: Modify channel, role, or stream URL for an existing link\n"
    "- delete_node: Delete a server node with blast-radius preview and confirmation card\n"
    "- scrapy: Crawl and scrape any URL to extract and audit internal links, external links, and media streams\n"
    "- remember_fact / forget_fact: Keep or drop a fact the operator asks you to remember\n"
    "- record_root_cause_feedback: The operator says which server was (or wasn't) the root cause\n"
    "- record_incident_fix: The operator says what fixed a server's outage\n"
    "- get_learned_memory: Facts, patterns and past outages you have learned\n"
    "- query_graph: Read-only Cypher on the graph, for questions the other tools don't answer (counts, filters, paths)\n"
    "Call the matching tool whenever the user's intent matches. When the context has facts, corrections or "
    "similar past outages you learned, use them and say so.\n"
    "Language handling: If the user writes in Hindi or Hinglish, or asks for explanation in Hinglish, answer "
    "conversationally in natural, clear MCR/NOC engineer Hinglish (mixing Hindi and English technical terms naturally)."
)

# The model calls this when someone asks for a report; the server resolves the name and the page shows a link
# (the report itself is built by report.py when opened), so no second model call is needed.
REPORT_TOOL = {"type": "function", "function": {
    "name": "generate_report",
    "description": "Create the complete monitoring report (charts, every stored field including hidden ones) for one "
                   "server, one channel, or all nodes. Use it whenever the user asks for a report, summary document, "
                   "full details or charts.",
    "parameters": {"type": "object", "additionalProperties": False, "required": ["node"], "properties": {
        "node": {"type": "string", "description": "A server domain (e.g. jio.ottlive.co.in or just jio), a channel "
                                                  "name (e.g. gtcnews), or 'all' for every node"}}},
}}

PROMPT = """
Answer the question using the provided context.

Context:
{context}

Question:
{query}
"""


def ist(value) -> Optional[str]:
    """Neo4j DateTime or ISO string (UTC) -> 'YYYY-MM-DD HH:MM:SS IST'."""
    if value in (None, ""):
        return None
    if hasattr(value, "to_native"):
        value = value.to_native()
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, timezone.utc)
    if isinstance(value, str):
        # Neo4j writes nanoseconds ("…56.344000000+00:00"); fromisoformat takes at most microseconds.
        text = re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00"))
        try:
            value = datetime.fromisoformat(text)
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def _json(value) -> dict:
    try:
        parsed = json.loads(value) if value else {}
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class Snapshot:
    """Nodes and spiders read once per question, with each channel's chain assembled from the nodes' links."""

    def __init__(self, driver, only_suffix: Optional[str] = None, incidents=None):
        """All nodes except test fixtures, or (only_suffix) just those ending in it. `incidents` is the incident
        store (metrics.store() by default)."""
        where = "n.domain ENDS WITH $suffix" if only_suffix else "NOT n.domain ENDS WITH $tld"
        records = driver.execute_query(
            f"MATCH (n:Domain) WHERE {where} "
            "RETURN properties(n) AS p, [l IN labels(n) WHERE l <> 'Domain'] AS roles ORDER BY n.domain",
            tld=TEST_TLD, suffix=only_suffix,
        ).records
        if incidents is None:
            from metrics import store
            incidents = store()
        self.nodes: Dict[str, dict] = {}
        self.channels: Dict[str, Dict[str, List[dict]]] = {}
        for r in records:
            p = dict(r["p"])
            node = {
                "domain": p.get("domain"), "roles": sorted(r["roles"], key=lambda x: ROLES.index(x) if x in ROLES else 9),
                "status": p.get("status", "UNKNOWN"), "latencyMs": p.get("lastLatencyMs"),
                "consecutiveFailures": p.get("consecutiveFailures"), "pingCount": p.get("pingCount"),
                "failedCount": p.get("failedCount"), "lastError": p.get("lastError"),
                "lastPing": ist(p.get("lastPing")), "lastRecovery": ist(p.get("lastRecovery")),
                "serverIp": p.get("server_ip"), "channels": [],
                "log": incidents.incidents(p.get("domain"), limit=20),
                "incidentStats": _json(p.get("incidentStats")), "blipCount": p.get("blipCount") or 0,
            }
            self.nodes[node["domain"]] = node
            health = _json(p.get("urlHealth"))
            for link in load_json_list(p.get("links")):
                h = health.get(link["url"], {})
                entry = {"url": link["url"], "node": node["domain"], "up": h.get("up"),
                         "detail": h.get("detail"), "lastDown": ist(h.get("lastDown"))}
                self.channels.setdefault(link["channel"], {}).setdefault(link["role"], []).append(entry)
                if link["channel"] not in node["channels"]:
                    node["channels"].append(link["channel"])
        self.spiders: Dict[str, dict] = {}
        spider_where = "s.finalLinkId ENDS WITH $suffix" if only_suffix else "NOT s.finalLinkId ENDS WITH $tld"
        for r in driver.execute_query(
            f"MATCH (s:SpiderRun) WHERE {spider_where} RETURN properties(s) AS p", tld=TEST_TLD, suffix=only_suffix
        ).records:
            p = dict(r["p"])
            self.spiders[p.get("finalLinkId")] = p

    def spider_for(self, channel: str, detailed: bool = False) -> Optional[dict]:
        finals = self.channels.get(channel, {}).get("FinalLink", [])
        spider = next((self.spiders[f["node"]] for f in finals if f["node"] in self.spiders), None)
        if not spider:
            return None
        rca = _json(spider.get("rca"))
        out = {
            "status": spider.get("status"), "at": spider.get("currentNodeId"),
            "stoppedAt": spider.get("stopNodeId"), "stopReason": spider.get("stopReason"),
            "lastStep": ist(spider.get("lastStepAt")),
            "rootCause": rca.get("root_cause"), "reason": rca.get("reason"), "impact": rca.get("impact"),
            "failover": rca.get("failover") or [],
        }
        if detailed:
            out["failedNodes"] = rca.get("failed_nodes") or []
            out["consecutiveFailures"] = rca.get("consecutive_failures")
            out["path"] = [{k: step.get(k) for k in ("node_id", "role", "up", "error", "visited")}
                           for step in rca.get("path") or []]
        return out

    @staticmethod
    def feed(chain: Dict[str, List[dict]]) -> str:
        """Which input the channel is fed from right now: main, backup, or none."""
        if any(e["up"] is not False for e in chain.get("MainInput", [])):
            return "main"
        if any(e["up"] is not False for e in chain.get("BackupLink", [])):
            return "backup"
        return "none"

    def channel_summary(self, channel: str) -> dict:
        chain = self.channels[channel]
        finals = chain.get("FinalLink", [])
        downs = [e["lastDown"] for entries in chain.values() for e in entries if e["lastDown"]]
        spider = self.spider_for(channel)
        return {
            "channel": channel,
            "finalUp": bool(finals) and all(e["up"] is not False for e in finals),
            "feed": self.feed(chain),
            "downNow": [{"role": role, "url": e["url"], "detail": e["detail"]}
                        for role in ROLES for e in chain.get(role, []) if e["up"] is False],
            "lastDown": max(downs) if downs else None,
            "missingRequired": [r for r in ("MainInput", "FinalLink") if not chain.get(r)],
            "spider": {k: spider[k] for k in ("status", "rootCause", "impact")} if spider else None,
        }




# --- documents ---
# Each document's "text" is both what is embedded and what the model reads as context.

def _url_line(channel: str, role: str, e: dict) -> str:
    line = (f"{ROLE_NAMES[role]} server of {channel} is {e['node']}; its URL {e['url']} is "
            f"{'UP' if e['up'] is not False else 'DOWN'}")
    if e.get("detail"):
        line += f" ({e['detail']})"
    if e.get("lastDown"):
        line += f". This {ROLE_NAMES[role]} URL last went down at {e['lastDown']}"
    return line + "."


def incident_lines(n: dict) -> List[str]:
    """The server's incident history: archived totals, blips, and the latest incidents with their type and the
    correlation verdict recorded when each opened."""
    lines, st = [], n.get("incidentStats") or {}
    if st.get("outages") or st.get("recoveries"):
        errors = ", ".join(f"{k} x{v}" for k, v in (st.get("errors") or {}).items())
        lines.append(f"Server {n['domain']} earlier history: {st.get('outages', 0)} outages, {st.get('recoveries', 0)} "
                     f"recoveries" + (f", errors {errors}" if errors else "") + ".")
    if n.get("blipCount"):
        lines.append(f"Server {n['domain']} had {n['blipCount']} short blips (failures too short to be incidents).")
    for e in [e for e in n.get("log") or [] if e.get("type") in ("OUTAGE", "RECOVERY")][-4:]:
        kind = e.get("category") or (str(e.get("lastError") or "").split(": ")[-1][:80] if e.get("type") == "OUTAGE" else "")
        line = f"Server {n['domain']} {e['type']} at {ist(e.get('timestamp'))}" + (f": {kind}" if kind else "")
        if e.get("durationS") is not None:
            line += f", lasted {e['durationS']} s"
        for corr in e.get("correlation") or []:
            line += f". {corr.get('verdict')}: {corr.get('summary')}"
        lines.append(line + ".")
    return lines


def _feed_words(feed: str) -> str:
    return {"main": "fed from Main", "backup": "ON BACKUP (Main is down, Backup is serving)",
            "none": "NO INPUT (Main and Backup are both down)"}[feed]


def _scheduler_text(monitor: dict) -> str:
    on = bool(monitor.get("autoPing"))
    lines = [f"Auto ping is {'ON' if on else 'OFF'}."]
    if on:
        lines.append(f"Auto ping runs every {monitor.get('intervalSeconds')} seconds; the next run is at "
                     f"{monitor.get('nextRun')}.")
    last = monitor.get("lastRun")
    if last:
        lines.append(f"The last run was at {last.get('at')} and took {last.get('elapsed_ms')} ms.")
    lines.append(f"A run is {'in progress' if monitor.get('runInProgress') else 'not in progress'} right now.")
    lines.append(f"The time now is {monitor.get('now')}.")
    return "Scheduler (auto ping). " + " ".join(lines)


def build_documents(snap: Snapshot, monitor: dict, topology: List[dict]) -> List[dict]:
    overview, docs, down, on_backup = [], [], [], []
    for name in sorted(snap.channels):
        s, chain = snap.channel_summary(name), snap.channels[name]
        ok = s["finalUp"] and s["feed"] != "none"
        if not ok:
            down.append(name)
        if s["feed"] == "backup":
            on_backup.append(name)
        overview.append(f"- {name}: {'OK' if ok else 'DOWN'}, {_feed_words(s['feed'])}.")
        lines = [f"Channel {name} is {'OK' if ok else 'DOWN'}: its output is "
                 f"{'UP' if s['finalUp'] else 'DOWN'} and it is {_feed_words(s['feed'])}."]
        lines += [_url_line(name, role, e) for role in ROLES for e in chain.get(role, [])]
        if s["missingRequired"]:
            lines.append(f"Missing required links: {', '.join(ROLE_NAMES[r] for r in s['missingRequired'])}.")
        spider = snap.spider_for(name, detailed=True)
        if spider:
            lines.append(f"The spider of {name} is {spider['status']}, at {spider['at']}, last step {spider['lastStep']}.")
            if spider["rootCause"]:
                lines.append(f"Root cause: {spider['rootCause']}. {spider['reason'] or ''}".strip())
            if spider["impact"]:
                lines.append(f"Impact: {spider['impact']}")
            if spider["failover"]:
                lines.append(f"Failover to: {', '.join(spider['failover'])}.")
        docs.append({"id": f"channel:{name}", "title": f"channel {name}", "text": "\n".join(lines)})
    for n in snap.nodes.values():
        roles = " and ".join(ROLE_NAMES.get(r, r) for r in n["roles"])
        lines = [f"Server {n['domain']} is a {roles} server, status {n['status']}, "
                 f"carrying channels {', '.join(n['channels'])}.",
                 f"Server {n['domain']}: latency {n['latencyMs']} ms, last ping {n['lastPing']}, "
                 f"{n['consecutiveFailures'] or 0} failures in a row, {n['failedCount'] or 0} failed of "
                 f"{n['pingCount'] or 0} pings."]
        if n["serverIp"]:
            lines.append(f"Server {n['domain']} has IP address {n['serverIp']}.")
        if n["lastError"] and n["status"] != "UP":
            lines.append(f"Server {n['domain']} error: {n['lastError']}")
        if n["lastRecovery"]:
            lines.append(f"Server {n['domain']} last recovered at {n['lastRecovery']}.")
        lines += incident_lines(n)
        docs.append({"id": f"server:{n['domain']}", "title": f"server {n['domain']}", "text": "\n".join(lines)})
    docs.append({"id": "topology", "title": "topology",
                 "text": "Topology, the relationships between servers (which server FEEDS or PRODUCES which):\n" + "\n".join(f"- {e['source']} {e['type']} {e['target']}"
                                                            for e in topology)})
    docs.append({"id": "scheduler", "title": "scheduler",
                 "text": _scheduler_text(monitor)})
    servers = "; ".join(f"{d} {n['status']} {n['latencyMs'] if n['latencyMs'] is not None else '?'} ms"
                        for d, n in sorted(snap.nodes.items()))
    summary = (f"Overview of all channels. Channels DOWN right now: {', '.join(down) or 'none'}. "
               f"Channels ON BACKUP right now: {', '.join(on_backup) or 'none'}.")
    return [{"id": "overview", "title": "overview", "down": down, "onBackup": on_backup, "channels": len(overview),
             "text": f"{summary}\nAll {len(overview)} channels:\n" + "\n".join(overview)
                     + f"\nAll servers (status, HTTP latency): {servers}."}] + docs


class KeyPool:
    """Thread-safe round-robin pool of OpenRouter API keys with automatic cooldown and failover.

    Supports Strategy B: Round-Robin Rotation (Active-Active) to distribute requests evenly across
    multiple keys, protecting against rate limits (429) and credit exhaustion (402).
    """

    def __init__(self, keys: Optional[List[str]] = None, default_cooldown: float = 60.0):
        self._explicit_keys = [k.strip().strip("'\"") for k in keys if k and k.strip().strip("'\"")] if keys is not None else None
        self._default_cooldown = default_cooldown
        self._lock = threading.Lock()
        self._index = 0
        self._cooldowns: Dict[str, float] = {}  # key -> monotonic timestamp
        self._last_env: Tuple[Optional[str], Optional[str]] = (None, None)
        self._cached_keys: List[str] = []

    def _sync_keys(self) -> List[str]:
        if self._explicit_keys is not None:
            return list(self._explicit_keys)
        env_keys = os.environ.get("OPENROUTER_API_KEYS")
        env_single = os.environ.get("OPENROUTER_API_KEY")
        if (env_keys, env_single) != self._last_env:
            raw_list = []
            if env_keys and env_keys.strip():
                raw_list.extend(re.split(r"[,;\n\r]+", env_keys))
            elif env_single and env_single.strip():
                raw_list.append(env_single)

            seen = set()
            deduped = []
            for item in raw_list:
                k = item.strip().strip("'\"")
                if k and k not in seen:
                    seen.add(k)
                    deduped.append(k)
            self._cached_keys = deduped
            self._last_env = (env_keys, env_single)
        return list(self._cached_keys)

    def all_keys(self) -> List[str]:
        with self._lock:
            return self._sync_keys()

    def is_in_cooldown(self, key: str) -> bool:
        with self._lock:
            exp = self._cooldowns.get(key)
            if exp is None:
                return False
            if time.monotonic() >= exp:
                self._cooldowns.pop(key, None)
                return False
            return True

    def mark_cooldown(self, key: str, status_code: int = 429, duration: Optional[float] = None) -> None:
        """Puts a key on cooldown. Status 401/402/403 get 300s, 429 gets default (60s)."""
        if not key:
            return
        if duration is None:
            duration = 300.0 if status_code in (401, 402, 403) else self._default_cooldown
        with self._lock:
            self._cooldowns[key] = time.monotonic() + duration

    def reset_cooldown(self, key: str) -> None:
        with self._lock:
            self._cooldowns.pop(key, None)

    def next_key(self) -> Optional[str]:
        """Returns the next available key in round-robin sequence.
        If all keys are on cooldown, returns the one closest to expiry."""
        with self._lock:
            keys = self._sync_keys()
            if not keys:
                return None
            if len(keys) == 1:
                return keys[0]

            now = time.monotonic()
            for k in list(self._cooldowns.keys()):
                if now >= self._cooldowns[k]:
                    del self._cooldowns[k]

            n = len(keys)
            for i in range(n):
                idx = (self._index + i) % n
                cand = keys[idx]
                if cand not in self._cooldowns:
                    self._index = (idx + 1) % n
                    return cand

            # All are in cooldown; pick the one expiring earliest
            best_key = min(keys, key=lambda k: self._cooldowns.get(k, 0))
            self._index = (keys.index(best_key) + 1) % n
            return best_key

    def status(self) -> dict:
        with self._lock:
            keys = self._sync_keys()
            now = time.monotonic()
            for k in list(self._cooldowns.keys()):
                if now >= self._cooldowns[k]:
                    del self._cooldowns[k]

            cooldown_count = len([k for k in keys if k in self._cooldowns])
            active_count = max(0, len(keys) - cooldown_count)
            return {
                "strategy": "Round-Robin Rotation (Active-Active)",
                "total_keys": len(keys),
                "active_keys": active_count,
                "cooldown_keys": cooldown_count,
                "keys": [
                    {
                        "index": i + 1,
                        "masked": f"{k[:7]}...{k[-4:]}" if len(k) > 12 else "***",
                        "status": "cooldown" if k in self._cooldowns else "active",
                        "cooldown_remaining_sec": max(0, int(self._cooldowns[k] - now)) if k in self._cooldowns else 0,
                    }
                    for i, k in enumerate(keys)
                ],
            }


def is_complete_data_excel_request(query: str) -> bool:
    q = query.strip().lower()
    # Match "Give me complete data of this project" and variations
    if re.search(r"\b(complete|all|full|whole)\s+data\s+(of|for|about)\s+this\s+project\b", q):
        return True
    if re.search(r"\b(complete|all|full|whole)\s+data\s+of\s+project\b", q):
        return True
    if re.search(r"\b(complete|all|full)\s+(data|report|info|information|details)\b.*\b(excel|\.xlsx|spreadsheet)\b", q):
        return True
    if re.search(r"\b(excel|\.xlsx)\b.*\b(complete|all|full)\s+(data|report|info|details|project)\b", q):
        return True
    if re.search(r"\b(give|show|send|get|download|export)\s+(me\s+)?(complete|all|full|whole)\s+(data|info|report|details|dump|workbook)\b", q):
        return True
    if re.search(r"\b(export|download)\s+(all|complete|full)\s+(to\s+)?excel\b", q):
        return True
    if re.search(r"\bcomplete\s+info\s+(in\s+)?excel\b", q):
        return True
    return False


class ChatBot:
    def __init__(self, driver, monitor_status: Callable[[], dict], http: Optional[httpx.Client] = None,
                 embedder=None):
        self.driver = driver
        self.monitor_status = monitor_status
        # Read here, after the app's load_dotenv().
        self.url = (os.environ.get("OPENROUTER_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.model = os.environ.get("CHATBOT_MODEL") or DEFAULT_MODEL
        # For questions that need reasoning; "none" turns it off (everything on the fast model).
        strong = os.environ.get("CHATBOT_STRONG_MODEL", DEFAULT_STRONG_MODEL).strip()
        self.strong_model = None if strong.lower() in ("", "none", "off") else strong
        self.embed_model = os.environ.get("CHATBOT_EMBED_MODEL") or DEFAULT_EMBED_MODEL
        self.http = http or httpx.Client(timeout=httpx.Timeout(120.0, connect=5.0))
        self.key_pool = KeyPool()
        self._embedder = embedder
        self._embedder_lock = threading.Lock()
        from hybrid_search import HybridSearch, Reranker
        self.graph = GraphRAG(driver, lambda texts: self.embedder.encode(texts),
                              hybrid=HybridSearch(lambda texts: self.embedder.encode(texts),
                                                  Reranker(cache_dir=os.environ.get("FASTEMBED_CACHE_PATH"))
                                                  if os.environ.get("CHATBOT_RERANKER", "1") != "0" else None))
        self.metrics = None  # metrics.Metrics, set by the server: anomalies for the analysis
        self.learning = None  # learning.Learner, set by the server: memory of past outages, facts and feedback
        # () -> {"ranking": [...groups], "warnings": [...]}, set by the server; shown in the overview
        self.insights: Callable[[], dict] = lambda: {}
        self.tools = Tools(self)

    @property
    def embedder(self):
        """Something with .encode(texts): the shared ONNX MiniLM (text_embedding), unless one was injected."""
        with self._embedder_lock:
            if self._embedder is None:
                import text_embedding
                self._embedder = text_embedding
            return self._embedder

    def api_key(self) -> Optional[str]:
        if hasattr(self, "key_pool"):
            keys = self.key_pool.all_keys()
            return keys[0] if keys else None
        return os.environ.get("OPENROUTER_API_KEYS") or os.environ.get("OPENROUTER_API_KEY") or None

    def _headers(self, key: Optional[str] = None) -> dict:
        k = key or self.api_key()
        return {"Authorization": f"Bearer {k}", "X-Title": "Stream Graph"}

    def status(self) -> dict:
        """Whether an OpenRouter key is set and accepted, for the page's setup notice."""
        info = {
            "model": self.model,
            "embedModel": self.embed_model,
            "provider": "OpenRouter",
            "keyPool": self.key_pool.status(),
            "langsmith": get_langsmith_status(),
            "embeddingCache": _embedding_cache_stats(),
        }
        if not self.api_key():
            return {**info, "configured": False, "problem": "Add OPENROUTER_API_KEY to .env and restart."}
        try:
            r = self.http.get(f"{self.url}/key", headers=self._headers(), timeout=5.0)
        except httpx.HTTPError:
            return {**info, "configured": False, "problem": f"Can't reach OpenRouter at {self.url}."}
        if r.status_code in (401, 403):
            return {**info, "configured": False, "problem": "OpenRouter rejected OPENROUTER_API_KEY."}
        return {**info, "configured": True}

    @traceable(name="chatbot_retrieve", run_type="chain")
    def retrieve(self, query: str, history: Optional[List[dict]] = None) -> Tuple[List[dict], dict]:
        """The documents GraphRAG picks for the query (overview, linked and nearest nodes, their graph
        neighbourhood), and the live status straight from the data (shown beside the answer). A follow-up
        ("give me its summary") is searched together with the previous question, so "its" finds the same nodes."""
        last_question = next((h["content"] for h in reversed(history or []) if h.get("role") == "user"), "")
        if last_question:
            query = f"{last_question}\n{query}"
        snap = Snapshot(self.driver)
        documents = build_documents(snap, self.monitor_status(), [r.model_dump() for r in current_topology(self.driver)])
        documents[0] = with_insights(documents[0], self.insights())
        overview = documents[0]
        live = {"channels": overview["channels"], "down": overview["down"], "onBackup": overview["onBackup"]}
        results = self.graph.retrieve(query, snap, documents)
        memory = self.memory_document(query, snap)
        if memory:
            results.insert(1, {"document": memory, "score": 1.0, "via": "memory"})
        if os.environ.get("CHATBOT_COMPRESS", "1") != "0":
            # Token-efficient RAG: no repeated facts, only the lines that bear on the question (context_compress.py)
            from context_compress import compress
            from graphrag import link_entities
            ent = link_entities(query, snap)
            names = list(ent["servers"]) + [d.split(".")[0] for d in ent["servers"]] + list(ent["channels"])
            results, self.last_compression = compress(query, results, names)
        return results, live

    def memory_document(self, query: str, snap: "Snapshot") -> Optional[dict]:
        """What the agent learned from past data that bears on this question (learning.Learner.context): operator
        facts, corrections of similar answers, similar past outages, patterns of the nodes in play."""
        if self.learning is None:
            return None
        from graphrag import link_entities
        entities = link_entities(query, snap)
        nodes = set(entities["servers"])
        for channel in entities["channels"]:
            nodes |= {e["node"] for entries in snap.channels.get(channel, {}).values() for e in entries}
        failing = [d for d, n in snap.nodes.items() if n["status"] == "DOWN"]
        categories = {(n["log"] or [{}])[-1].get("category") for d, n in snap.nodes.items() if d in failing}
        try:
            found = self.learning.context(query, nodes, failing, categories - {None})
        except Exception:
            return None  # memory is a bonus; never fail an answer over it
        if not found["text"]:
            return None
        return {"id": "memory", "title": "learned from past data", "text": found["text"],
                "facts": len(found["facts"]), "cases": len(found["cases"]), "lessons": len(found["lessons"]),
                "patterns": len(found["patterns"])}


    # --- conversation & streaming ---

    @staticmethod
    def build_prompt(query: str, results: List[dict]) -> str:
        context = "\n\n".join(result["document"]["text"] for result in results)
        return PROMPT.format(context=context, query=query)

    def stream(self, query: str, results: List[dict], live: Optional[dict] = None,
               history: Optional[List[dict]] = None) -> Iterator[dict]:
        """Streams the answer as events: sources (with the live status), then token after token, then done
        (or error)."""
        # Intercept double-confirmation commands
        confirm_match = re.search(r"\bCONFIRM\s+(act_[a-f0-9]{6,12})\b", query, re.IGNORECASE)
        if confirm_match:
            yield {"type": "sources", "live": live, "sources": []}
            yield from self.tools.execute_confirmed_action(confirm_match.group(1).lower())
            yield {"type": "done", "elapsed_ms": 10, "truncated": False, "model": self.model, "tokens": None}
            return

        cancel_match = re.search(r"\bCANCEL\s+(act_[a-f0-9]{6,12})\b", query, re.IGNORECASE)
        if cancel_match:
            yield {"type": "sources", "live": live, "sources": []}
            yield from self.tools.cancel_action(cancel_match.group(1).lower())
            yield {"type": "done", "elapsed_ms": 10, "truncated": False, "model": self.model, "tokens": None}
            return

        # Intercept requests for complete project data in Excel
        if is_complete_data_excel_request(query):
            yield {"type": "sources", "live": live, "sources": []}
            yield from self.tools.tool_generate_excel({})
            yield {"type": "done", "elapsed_ms": 15, "truncated": False, "model": self.model, "tokens": None}
            return

        prompt_text = self.build_prompt(query, results)
        sources_payload = [
            {
                "id": r["document"].get("id", r["document"].get("title", "")),
                "title": r["document"].get("title", ""),
                "via": r.get("via", "overview"),
                "score": r.get("score"),
                "text": r["document"].get("text", "")
            }
            for r in results
        ]
        yield {
            "type": "sources",
            "live": live,
            "sources": sources_payload,
            "prompt": prompt_text,
            "system": SYSTEM,
            "model": self.model
        }
        messages = [{"role": "system", "content": SYSTEM + AGENT_RULES}, *conversation(history),
                    {"role": "user", "content": prompt_text}]
        yield from self.agent_loop(query, messages, history)

    def agent_loop(self, query: str, messages: List[dict], history: Optional[List[dict]] = None) -> Iterator[dict]:
        """Up to MAX_STEPS rounds of: the model answers or calls tools; read-only tools run and their results go
        back to it; then it explains. A change to the graph stops at its Allow/Deny card; a report or workbook is
        shown as it is. Each step is streamed as step / step_done events, so the page shows the work as it happens."""
        last_question = next((h.get("content", "") for h in reversed(history or []) if h.get("role") == "user"), "")
        tool_defs = self.tools.definitions_for(f"{last_question}\n{query}", embed=self._embed_one,
                                               learned=self.learned_tools(query))
        model = self.pick_model(query)
        done, seen, n, tools_used = None, set(), 0, []
        for step in range(MAX_STEPS + 1):
            use_tools = step < MAX_STEPS  # the last round must answer
            calls, text = [], ""
            for event in self.generate(messages, GENERATION, model=model, tools=tool_defs if use_tools else None):
                if event["type"] == "tool_calls":
                    calls += event["calls"]
                elif event["type"] == "done":
                    done = merge_done(done, event)
                elif event["type"] == "error":
                    yield event
                    return
                else:
                    if event["type"] == "token":
                        text += event["text"]
                    yield event
            if not calls:
                break
            if text.strip():
                yield {"type": "token", "text": "\n\n"}  # what it said before the tools, apart from what follows
            if step == 0 and model != self.strong_model and len(calls) > 1 and self.strong_model:
                model = self.strong_model  # several tools at once: a bigger job, hand the rest to the stronger model
            ids = [c.get("id") or f"call_{step}_{i}" for i, c in enumerate(calls)]
            messages.append({"role": "assistant", "content": text or None, "tool_calls": [
                {"id": cid, "type": "function", "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                for cid, c in zip(ids, calls)]})
            stop = False
            for cid, call in zip(ids, calls):
                n += 1
                name = call["name"]
                try:
                    args = json.loads(call["arguments"] or "{}")
                except ValueError:
                    args = {}
                tools_used.append(name)
                yield {"type": "step", "index": n, "tool": name, "args": args}
                key = (name, json.dumps(args, sort_keys=True))
                if key in seen:
                    result = "This exact call was already made above; use its result."
                elif name in CHANGE_TOOLS or name in SHOW_TOOLS:
                    shown = []
                    for event in self.tools.execute(call):  # straight to the operator: Allow card, report card, ...
                        if event["type"] == "done":
                            done = merge_done(done, event)
                            continue
                        if event["type"] == "token":
                            shown.append(event["text"])
                        yield event
                    result = ("Staged for the operator's Allow/Deny. Tell them in one sentence what will change and "
                              "that they need to click Allow." if name in CHANGE_TOOLS else
                              "Already shown to the operator: " + "".join(shown)[:400])
                    stop = True
                else:
                    parts = []
                    for event in self.tools.execute(call):
                        if event["type"] == "token":
                            parts.append(event["text"])
                        elif event["type"] == "done":
                            done = merge_done(done, event)
                        else:
                            yield event  # cards and the like still reach the page
                    result = "".join(parts).strip() or "(no output)"
                seen.add(key)
                yield {"type": "step_done", "index": n, "tool": name, "summary": first_line(result),
                       "output": result[:4000]}
                messages.append({"role": "tool", "tool_call_id": cid, "content": result[:TOOL_RESULT_CHARS]})
            if stop:
                break
        if done:
            yield {**done, "steps": n, "toolsUsed": tools_used, "model": model}

    def pick_model(self, query: str) -> str:
        """Quick lookups on the fast model; reasoning (why, root cause, compare, plan, several things at once) on the
        stronger one when it's configured."""
        if self.strong_model and HARD_QUESTION.search(query or ""):
            return self.strong_model
        return self.model

    def _embed_one(self, text: str):
        try:
            return self.embedder.encode([text])[0]
        except Exception:
            return None

    def learned_tools(self, query: str) -> List[str]:
        """Tools that produced answers the operator rated good for similar questions (learning.py)."""
        if self.learning is None:
            return []
        try:
            return self.learning.tools_for_question(query)
        except Exception:
            return []

    def report_events(self, arguments: str) -> Iterator[dict]:
        """The report card (and a line of text) for a generate_report call."""
        raw = json.loads(arguments or "{}") if isinstance(arguments, str) else (arguments or {})
        yield from self.tools.tool_generate_report(raw)

    @traceable(name="openrouter_generate", run_type="llm")
    def generate(self, messages: List[dict], options: dict, model: Optional[str] = None,
                 tools: Optional[List[dict]] = None) -> Iterator[dict]:
        """Streams a reply from OpenRouter (OpenAI-style chat completions over SSE) as token events, then done
        (or error). With `tools`, calls the model makes arrive as one tool_calls event before done. Never raises.
        Strategy B: Round-Robin Rotation with failover across multiple keys on 429/402/401/403."""
        started = time.monotonic()
        model = model or self.model
        keys = self.key_pool.all_keys()
        if not keys:
            yield {"type": "error", "message": "Add OPENROUTER_API_KEY to .env and restart."}
            return
        body = {"model": model, "stream": True, "messages": messages, **options}
        if tools:
            body.update(tools=tools, tool_choice="auto")

        max_attempts = max(1, len(keys))
        for attempt in range(max_attempts):
            key = self.key_pool.next_key()
            finish, usage = None, None
            calls: Dict[int, dict] = {}  # tool calls stream in pieces, keyed by index
            tokens_streamed = False
            try:
                headers = self._headers(key)
                with self.http.stream("POST", f"{self.url}/chat/completions", json=body, headers=headers) as response:
                    if response.status_code >= 400:
                        err_bytes = response.read()
                        err_msg = api_error(response.status_code, err_bytes, model)
                        # If rate limited (429) or credit/auth error (402/401/403) and more keys exist, mark cooldown & retry
                        if response.status_code in (429, 402, 401, 403) and len(keys) > 1 and attempt < max_attempts - 1:
                            self.key_pool.mark_cooldown(key, response.status_code)
                            continue
                        raise ChatSetupError(err_msg, status_code=response.status_code)

                    for line in response.iter_lines():
                        if not line.startswith("data:"):
                            continue  # blank lines and ": OPENROUTER PROCESSING" keep-alive comments
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        chunk = json.loads(data)
                        if chunk.get("error"):  # failed after the stream started (HTTP status was already 200)
                            raise ChatSetupError(f"OpenRouter: {chunk['error'].get('message', chunk['error'])}")
                        usage = chunk.get("usage") or usage  # sent in the last chunk
                        for choice in chunk.get("choices") or []:
                            delta = choice.get("delta") or {}
                            if delta.get("content"):
                                tokens_streamed = True
                                yield {"type": "token", "text": delta["content"]}
                            for piece in delta.get("tool_calls") or []:
                                call = calls.setdefault(piece.get("index", 0), {"name": "", "arguments": "", "id": ""})
                                call["id"] = call["id"] or piece.get("id") or ""
                                function = piece.get("function") or {}
                                call["name"] += function.get("name") or ""
                                call["arguments"] += function.get("arguments") or ""
                            finish = choice.get("finish_reason") or finish

                if calls:
                    yield {"type": "tool_calls", "calls": [calls[i] for i in sorted(calls)]}
                yield {"type": "done", "elapsed_ms": int((time.monotonic() - started) * 1000),
                       "truncated": finish == "length", "model": model, "tokens": token_counts(usage)}
                return
            except ChatSetupError as e:
                if not tokens_streamed and e.status_code in (429, 402, 401, 403) and len(keys) > 1 and attempt < max_attempts - 1:
                    self.key_pool.mark_cooldown(key, e.status_code or 429)
                    continue
                yield {"type": "error", "message": str(e)}
                return
            except httpx.ConnectError:
                yield {"type": "error", "message": f"Can't reach OpenRouter at {self.url}; check the internet connection."}
                return
            except httpx.HTTPError as e:
                yield {"type": "error", "message": f"OpenRouter error: {e}"}
                return


def conversation(history: Optional[List[dict]]) -> List[dict]:
    """The last HISTORY_TURNS messages as chat messages, each cut to HISTORY_CHARS (oldest first)."""
    out = []
    for h in (history or [])[-HISTORY_TURNS:]:
        role, text = h.get("role"), str(h.get("content") or "").strip()
        if role in ("user", "assistant") and text:
            out.append({"role": role, "content": text if len(text) <= HISTORY_CHARS else text[:HISTORY_CHARS] + " …"})
    return out


def with_insights(overview: dict, insights: dict) -> dict:
    """The overview plus the current root-cause ranking and early warnings (computed, not the model's guess)."""
    lines = []
    for group in insights.get("ranking") or []:
        top = group["ranking"][0]
        lines.append(f"Likely root cause of the current failure of {', '.join(group['nodes'])}: {top['node']} "
                     f"({round(top['score'] * 100)}%; {'; '.join(top['reasons'])}"
                     + (f"; stopped {ist(top['onsetAt'])}" if top.get("onsetAt") else "") + ").")
    for w in insights.get("warnings") or []:
        lines.append(f"Early warning for {w['node']} (still up): {'; '.join(w['warnings'])}.")
    for p in insights.get("upcoming") or []:  # alerts likely to follow what's happening now (alertlog.py)
        mins = max(0, round((p["expectedAt"] - time.time()) / 60))
        lines.append(f"Likely next alert: {p['node']} {p['words']} in about {mins} min ({round(p['probability'] * 100)}%, "
                     f"{p['source']}): {p['reason']}.")
    scores = insights.get("scores") or {}
    for source, sc in scores.items():
        if sc.get("hitRate") is not None:
            lines.append(f"Accuracy of past '{source}' alert predictions this week: right {round(sc['hitRate'] * 100)}% "
                         f"of {sc['hits'] + sc['misses']}.")
    for a in insights.get("adBreaks") or []:  # SCTE-35 ad-break markers on a Final (scte.py)
        now_text = ""
        if a.get("open"):
            now_text = f" In an ad break now ({a['open']['elapsed_s']} s{'; STUCK' if a['open']['status'] == 'STUCK' else ''})."
        nxt = f" Next break expected at {ist(a['nextExpected'])}." if a.get("nextExpected") else ""
        lines.append(f"Ad breaks on {a.get('channel') or a['node']}: {a['breaks7d']} in the last 7 days. "
                     + " ".join(a.get("lines") or []) + now_text + nxt
                     + (f" Problems: {'; '.join(a['issues'])}." if a.get("issues") else ""))
    for g in insights.get("glitches") or []:  # small viewer-visible problems on a Final that is still up (glitch.py)
        kinds = ", ".join(f"{k} x{n}" for k, n in (g.get("lastHourKinds") or {}).items())
        lines.append(f"Glitches on {g.get('channel') or g['node']} ({g['node']}): {g['lastHour']} in the last hour"
                     + (f" ({kinds})" if kinds else "")
                     + (f", usually {g['normalPerHour']}" if g.get("normalPerHour") is not None else "")
                     + f"; risk of glitches in the next 10 minutes {g['band'].lower()} ({g['risk']}/100)"
                     + (f": {'; '.join(g['reasons'])}" if g.get("reasons") else "") + ".")
    if not lines:
        return overview
    return {**overview, "text": overview["text"] + "\n" + "\n".join(lines)}


def resolve_report_target(snap: "Snapshot", target: str) -> dict:
    """'all' / a domain / part of a domain / a channel -> {"node": domain or None (= all), "label"} or {"problem"}."""
    t = target.strip().lower()
    if t in ("", "all", "all nodes", "all servers", "everything", "*", "full"):
        return {"node": None, "label": "all nodes"}
    domains = sorted(snap.nodes)
    exact = [d for d in domains if d.lower() == t]
    if exact:
        return {"node": exact[0], "label": exact[0]}
    channel = next((c for c in snap.channels if c.lower() == t), None)
    if channel:
        finals = sorted({e["node"] for e in snap.channels[channel].get("FinalLink", [])})
        if len(finals) == 1:
            return {"node": finals[0], "label": f"{finals[0]} (the {channel} channel's Final server)"}
    short = [d for d in domains if d.lower().split(".", 1)[0] == t]  # "ingest" -> ingest.ottlive.co.in
    if len(short) == 1:
        return {"node": short[0], "label": short[0]}
    partial = [d for d in domains if t in d.lower()]
    if len(partial) == 1:
        return {"node": partial[0], "label": partial[0]}
    if partial:
        return {"problem": f"'{target}' matches several servers: {', '.join(partial)}. Which one?"}
    return {"problem": f"I can't find a server or channel called '{target}'. Servers: {', '.join(domains)}."}


def _embedding_cache_stats() -> Optional[dict]:
    """How many embeddings the SHA-1 fingerprint cache saved (text_embedding.py)."""
    try:
        import text_embedding
        return text_embedding.cache_stats()
    except Exception:
        return None


def first_line(text: str) -> str:
    """A one-line summary of a tool's output for the step list."""
    for line in (text or "").splitlines():
        line = re.sub(r"[*#`>|]+", "", line).strip(" -:")
        if len(line) > 3:
            return line[:140]
    return ""


def merge_done(first: Optional[dict], second: dict) -> dict:
    """One done event for a turn that made two model calls: summed tokens, the later call's other fields."""
    if not first:
        return second
    tokens = [t for t in (first.get("tokens"), second.get("tokens")) if t]
    total = {k: sum(t.get(k) or 0 for t in tokens) for k in ("input", "output", "reasoning")} if tokens else None
    return {**second, "elapsed_ms": first.get("elapsed_ms", 0) + second.get("elapsed_ms", 0), "tokens": total}


def token_counts(usage: Optional[dict]) -> Optional[dict]:
    """{"input", "output", "reasoning"} from OpenRouter's usage (output includes reasoning)."""
    if not usage:
        return None
    return {"input": usage.get("prompt_tokens"), "output": usage.get("completion_tokens"),
            "reasoning": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0}


def api_error(status: int, body: bytes, model: str) -> str:
    try:
        message = json.loads(body)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        message = body.decode(errors="replace")[:200]
    hint = {401: "OpenRouter rejected OPENROUTER_API_KEY", 402: "The OpenRouter account is out of credits",
            429: "OpenRouter rate limit reached; try again shortly"}.get(status)
    if status in (400, 404) and "model" in message.lower():
        hint = f"OpenRouter doesn't know the model {model!r}; check CHATBOT_MODEL / RCA_MODEL"
    return f"{hint or f'OpenRouter error {status}'}: {message}"


class ChatSetupError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code
