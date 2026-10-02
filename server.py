# Build: 2026-10-01-notifications-nodes-v2-nosamples
"""HTTP API and static frontend (web/) for the stream graph.

Run:  .venv/bin/uvicorn server:app --port 8000

A background scheduler pings every channel's spider every 30 s by default (stop/start and the
interval are set from the page, see /api/scheduler). Every run streams live to all open pages
through /api/stream; POST /api/run starts one now, for all channels or a single channel.
"""

from chat_eval import ChatEval
from history_ai import HistoryAI
from voice import Voice, voice_store
from chat_store import ChatStore
import asyncio
import json
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from neo4j import GraphDatabase

import alertlog
import backup
import glitch
import glitch_model
import rca_rank
import scte
import text_embedding
from auth import COOKIE, Auth, login_url
from chatbot import ChatBot, ist
from embeddings import sync_all_embeddings
from learning import store as learning_store
from metrics import store as metrics_store, warnings as anomaly_warnings
from nodes import current_topology
from predictor import predict_all
from spider import SpiderManager, migrate_logs
from traceroute import TracerouteManager

# Used by the routes (routes/*.py) as `srv.<name>`: kept here so there is one place to patch them (tests do).
from auth import SESSION_S, login_page, safe_next  # noqa: F401
from export import build_workbook, parse_excel_workbook  # noqa: F401
from graph_view import CARD_H, CARD_W, ROLE_ORDER, fetch_graph, layout  # noqa: F401
from heatmap import generate_3d_heatmap_png  # noqa: F401
from noc_rca import analyse_node, stream_rca  # noqa: F401
from nodes import Relationship, StreamLink, connect_new_channels, group_by_domain, load_json_list, replace_topology, upsert_nodes  # noqa: F401
from report import build_report  # noqa: F401
from report_analysis import analysis_prompt, stream_analysis  # noqa: F401
from spider import Rca, all_spiders, count_spiders  # noqa: F401
from telemetry_pool import telemetry_pool  # noqa: F401
from traceroute import TRACEROUTE_COOLDOWN_S, TRACEROUTE_THRESHOLD, Busy, Cooldown, stored_traceroute  # noqa: F401

load_dotenv()
WEB = Path(__file__).parent / "web"
driver = GraphDatabase.driver(
    os.environ["NEO4J_URI"], auth=(os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"])
)
auth = Auth.from_env()
class GranularRunLock:
    """Thread-safe granular run lock.
    Allows independent channels to be probed concurrently,
    while preventing duplicate runs on the SAME channel or overlapping full sweeps.
    """

    def __init__(self):
        self._meta = threading.Lock()
        self._global_running = False
        self._channel_running: Set[str] = set()

    def acquire(self, final_ids: Optional[List[str]] = None, blocking: bool = False) -> bool:
        with self._meta:
            if final_ids is None:
                if self._global_running or self._channel_running:
                    return False
                self._global_running = True
                return True
            else:
                if self._global_running:
                    return False
                targets = set(final_ids)
                if targets & self._channel_running:
                    return False
                self._channel_running.update(targets)
                return True

    def release(self, final_ids: Optional[List[str]] = None) -> None:
        with self._meta:
            if final_ids is None:
                self._global_running = False
            else:
                self._channel_running.difference_update(set(final_ids))

    def locked(self, final_ids: Optional[List[str]] = None) -> bool:
        with self._meta:
            if final_ids is None:
                return self._global_running or bool(self._channel_running)
            return self._global_running or bool(set(final_ids) & self._channel_running)


run_lock = GranularRunLock()  # Granular per-channel and full-cycle lock
SCHEDULER_FILE = Path(os.environ.get("SCHEDULER_FILE", Path(__file__).parent / "scheduler.json"))  # a volume in Docker
EMBEDDINGS_CRON_FILE = Path(os.environ.get("EMBEDDINGS_CRON_FILE", Path(__file__).parent / "embeddings_cron.json"))
WEEK_S = 7 * 24 * 3600  # 604800 seconds (1 week)
DEFAULT_INTERVAL_S, MIN_INTERVAL_S, MAX_INTERVAL_S = 30, 10, 3600
# Adaptive crawl tier thresholds (used by compute_adaptive_interval)
ADAPTIVE_TIER_TURBO_S   =  5   # Active outage — rapid recovery detection
ADAPTIVE_TIER_URGENT_S  = 10   # Running on backup only (SPOF — zero redundancy)
ADAPTIVE_TIER_WATCH_S   = 15   # Flapping / recent incidents in last 24 h
ADAPTIVE_TIER_RELAXED_S = 75   # Rock-solid: 100 % uptime for 7+ days
# What the page needs from each incident to raise a typed alert (the chain detail stays in the node's log).
INCIDENT_FIELDS = ("node", "type", "category", "timestamp", "consecutiveFailures", "durationS", "failedUrls",
                   "correlation", "lastRttMs", "lastPacketLoss", "rootCauseRanking")


class Hub:
    """Fans every run's events out to all open pages (Server-Sent Events), from any thread."""

    def __init__(self):
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.subscribers: Set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)

    def publish(self, kind: str, data) -> None:
        if not self.loop:
            return
        payload = json.dumps(data, default=str)

        def deliver():
            for queue in list(self.subscribers):
                if not queue.full():  # a stuck page must not block the others
                    queue.put_nowait((kind, payload))
        self.loop.call_soon_threadsafe(deliver)


hub = Hub()
current_run: Dict[str, dict] = {}  # the run in progress, if any (for pages that join mid-run)
# Per-check time series and anomaly detection (SQLite; a volume in Docker).
metrics = metrics_store()
learner = learning_store(metrics.path)  # what the agent learned (same SQLite file)
chat_store = ChatStore(metrics.path)  # saved chatbot conversations (same SQLite file)
latest_ranking: Dict[str, Any] = {"at": None, "groups": []}  # from the last run that found failures
# Escalation: a server failing TRACEROUTE_THRESHOLD times in a row gets a background traceroute (per-host cooldown).
tracer = TracerouteManager(driver, on_update=lambda event: hub.publish("traceroute", event))

# Adaptive crawl state: final_link_id -> {interval_s, next_run_at, tier, reason}
_adaptive: Dict[str, dict] = {}
_new_finals_tried: Dict[str, float] = {}  # Final -> when a run was last started for it without a spider

# Last predictions cache: published via /api/predictions and hub
_last_predictions: List[dict] = []


def compute_adaptive_interval(spider_state: dict, base_interval: int) -> dict:
    """Return the recommended crawl interval (seconds) and tier for a single spider,
    based on its current status, failover state and incident statistics.

    Tier priority (highest wins):
      TURBO   – spider is actively STOPPED (root-cause outage) → every 5 s
      URGENT  – spider is RUNNING but on failover (Main DOWN, Backup serving) → every 10 s
      WATCH   – spider was STOPPED within last 24 h or recently recovered → every 15 s
      NORMAL  – default operating state → base_interval (30 s)
      RELAXED – spider has been RUNNING continuously for >= 7 days with no incidents → 75 s
    """
    status = spider_state.get("status", "RUNNING")
    rca_raw = spider_state.get("rca") or "{}"
    try:
        rca = json.loads(rca_raw) if isinstance(rca_raw, str) else (rca_raw or {})
    except (ValueError, TypeError):
        rca = {}

    failover_active = bool(rca.get("failover"))
    alerted = bool(rca.get("alerted"))
    last_step_raw = spider_state.get("lastStepAt") or spider_state.get("last_step_at")
    now = time.time()

    if status == "STOPPED":
        return {"interval_s": ADAPTIVE_TIER_TURBO_S, "tier": "TURBO",
                "reason": "Active outage – rapid recovery detection"}

    if failover_active:
        return {"interval_s": ADAPTIVE_TIER_URGENT_S, "tier": "URGENT",
                "reason": "Running on backup link – SPOF, zero redundancy"}

    if alerted:
        return {"interval_s": ADAPTIVE_TIER_WATCH_S, "tier": "WATCH",
                "reason": "Flapping / recent incident – watchlist monitoring"}

    # Relaxed tier: stable for >= 7 days with no alerts
    if last_step_raw:
        try:
            from datetime import timezone
            last_step_dt = datetime.fromisoformat(str(last_step_raw).replace("Z", "+00:00"))
            age_s = now - last_step_dt.timestamp()
            if age_s >= 7 * 24 * 3600 and not alerted:
                return {"interval_s": ADAPTIVE_TIER_RELAXED_S, "tier": "RELAXED",
                        "reason": "Rock-solid: 7+ days stable – reduced polling"}
        except (ValueError, TypeError):
            pass

    return {"interval_s": base_interval, "tier": "NORMAL", "reason": "Standard polling interval"}


class Scheduler:
    """Runs every spider on a fixed interval; on by default, every 30 s. The on/off state and
    interval are saved to scheduler.json so a stop survives a server restart. Also manages weekly
    background embedding sync for Server and Spider nodes."""

    def __init__(self):
        saved = {}
        try:
            saved = json.loads(SCHEDULER_FILE.read_text())
        except (OSError, ValueError):
            pass
        self.enabled: bool = bool(saved.get("enabled", True))
        self.interval: int = int(saved.get("interval", DEFAULT_INTERVAL_S))
        self.next_run_at: float = time.time() + 3  # first ping shortly after startup
        self.last_run: Optional[dict] = None
        self.backup_running = False  # nightly backup (backup.py) in progress
        self.last_backup_check = 0.0

        cron_state = {}
        try:
            if EMBEDDINGS_CRON_FILE.exists():
                cron_state = json.loads(EMBEDDINGS_CRON_FILE.read_text())
        except (OSError, ValueError):
            pass
        self.embedding_running = False
        self.last_embedding_result: Optional[dict] = cron_state.get("last_result")
        if cron_state.get("last_sync"):
            self.last_embedding_sync: float = float(cron_state["last_sync"])
        else:
            # Never synced here: count the week from now. (The sync clears incident logs, so it must not
            # run on every start just because the state file is missing.)
            self.record_embedding_sync()

    def state(self) -> dict:
        return {
            "enabled": self.enabled, "interval": self.interval,
            "next_run_at": self.next_run_at if self.enabled else None,
            "running": run_lock.locked(), "last_run": self.last_run, "server_time": time.time(),
            "last_embedding_sync": self.last_embedding_sync,
            "next_embedding_sync_at": self.last_embedding_sync + WEEK_S,
            "embedding_running": self.embedding_running, "last_embedding_result": self.last_embedding_result,
        }

    def record_embedding_sync(self, result: Optional[dict] = None) -> None:
        self.last_embedding_sync = time.time()
        if result is not None:
            self.last_embedding_result = result
        try:
            EMBEDDINGS_CRON_FILE.write_text(json.dumps({
                "last_sync": self.last_embedding_sync,
                "interval_seconds": WEEK_S,
                "next_sync": self.last_embedding_sync + WEEK_S,
                "last_result": self.last_embedding_result,
            }, indent=2))
        except OSError as e:
            print(f"[embeddings] could not save {EMBEDDINGS_CRON_FILE}: {e}")

    def update(self, enabled: Optional[bool], interval: Optional[int]) -> None:
        if interval is not None:
            self.interval = interval
        if enabled is not None and enabled != self.enabled:
            self.enabled = enabled
            if enabled:
                self.next_run_at = time.time() + 1  # resume: ping right away
        if self.enabled:
            self.next_run_at = min(self.next_run_at, time.time() + self.interval)
        SCHEDULER_FILE.write_text(json.dumps({"enabled": self.enabled, "interval": self.interval}))
        hub.publish("scheduler", self.state())

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            now = time.time()

            if self.enabled:
                # --- Adaptive crawl: per-spider dynamic polling with staggered starts ---
                try:
                    all_spider_states = [
                        dict(r) for r in driver.execute_query(
                            "MATCH (s:SpiderRun) WHERE NOT s.finalLinkId ENDS WITH '.invalid' "
                            "RETURN s.finalLinkId AS finalLinkId, s.status AS status, "
                            "s.rca AS rca, toString(s.lastStepAt) AS lastStepAt"
                        ).records
                    ]
                except Exception:
                    all_spider_states = []

                due_finals: List[str] = []
                n_spiders = max(len(all_spider_states), 1)

                # A Final added after start-up has no SpiderRun yet, so the timers below never see it: check it
                # now (the run creates its spider), retrying at most once per interval if that run fails.
                try:
                    new_finals = [r["f"] for r in driver.execute_query(
                        "MATCH (f:Domain:FinalLink) WHERE NOT f.domain ENDS WITH '.invalid' "
                        "AND NOT EXISTS { MATCH (:SpiderRun {finalLinkId: f.domain}) } RETURN f.domain AS f").records]
                except Exception:
                    new_finals = []
                for fid in new_finals:
                    if now - _new_finals_tried.get(fid, 0) >= self.interval:
                        _new_finals_tried[fid] = now
                        due_finals.append(fid)

                for idx, s in enumerate(all_spider_states):
                    fid = s.get("finalLinkId")
                    if not fid:
                        continue

                    adaptive = compute_adaptive_interval(s, self.interval)
                    prev = _adaptive.get(fid, {})
                    prev_tier = prev.get("tier")

                    if fid not in _adaptive:
                        # FIRST BOOT: stagger each spider's initial fire time evenly
                        # across the interval window so they never all run at once.
                        # Spider #i fires at: now + 2s + (interval / n_spiders) * i
                        # TURBO / URGENT (active outage) always fire immediately.
                        if adaptive["tier"] in ("TURBO", "URGENT"):
                            stagger_s = 0.0
                        else:
                            stagger_s = (adaptive["interval_s"] / n_spiders) * idx
                        _adaptive[fid] = {
                            **adaptive,
                            "next_run_at": now + 2 + stagger_s,
                            "final_link_id": fid,
                        }
                        # Don't add to due_finals — the staggered timer will fire it.

                    elif prev_tier != adaptive["tier"]:
                        # TIER CHANGED (e.g. node went DOWN → TURBO):
                        # fire this spider immediately and reset its individual timer.
                        _adaptive[fid] = {
                            **adaptive,
                            "next_run_at": now + adaptive["interval_s"],
                            "final_link_id": fid,
                        }
                        due_finals.append(fid)

                    elif now >= prev.get("next_run_at", 0):
                        # INDIVIDUAL TIMER EXPIRED: this spider is due now.
                        _adaptive[fid] = {
                            **adaptive,
                            "next_run_at": now + adaptive["interval_s"],
                            "final_link_id": fid,
                        }
                        due_finals.append(fid)
                    # else: not due this tick — skip

                if not all_spider_states and now >= self.next_run_at:
                    # Empty topology or first boot before SpiderRun nodes exist.
                    self.next_run_at = now + self.interval
                    start_run(source="scheduler")
                elif due_finals:
                    # Only run the specific spiders that are due this tick.
                    start_run(final_ids=due_finals, source="scheduler-adaptive")

                hub.publish("scheduler", self.state())

            # Nightly backup of the history/learning database and the graph (backup.py), in the background.
            if not self.backup_running and now - self.last_backup_check >= 600:
                self.last_backup_check = now
                if backup.last_backup_age() >= backup.DAY_S:
                    self.backup_running = True
                    asyncio.get_running_loop().run_in_executor(None, run_backup)

            # Weekly cron: embeddings + incident-log clearance, in the background (never blocks the pings).
            if not self.embedding_running and (now - self.last_embedding_sync) >= WEEK_S:
                self.embedding_running = True  # until the job starts, so the next tick doesn't start another
                asyncio.get_running_loop().run_in_executor(None, cron_embedding_sync)

scheduler = Scheduler()
embedding_lock = threading.Lock()


# --- Glitch detection on the Final streams (glitch.py) ---

def final_streams() -> List[dict]:
    """Every channel's Final stream URL with its node and channel."""
    out = []
    for r in driver.execute_query("MATCH (n:Domain:FinalLink) WHERE NOT n.domain ENDS WITH '.invalid' "
                                  "RETURN n.domain AS node, n.links AS links").records:
        for link in load_json_list(r["links"]):
            if link.get("role") == "FinalLink":
                out.append({"node": r["node"], "url": link["url"], "channel": link.get("channel")})
    return out


def upstream_map() -> Dict[str, List[str]]:
    return {r["f"]: r["ups"] for r in driver.execute_query(
        "MATCH (u:Domain)-[:FEEDS|PRODUCES*1..4]->(f:Domain:FinalLink) RETURN f.domain AS f, collect(DISTINCT u.domain) AS ups").records}


def train_glitch_model() -> dict:
    """Retrains the glitch model on the saved history (hourly from the glitch monitor, or POST /api/glitches/train)."""
    ups = upstream_map()
    return glitch_model.train(metrics.path, final_streams(), lambda n: ups.get(n, []))


def glitch_forecast() -> List[dict]:
    finals = final_streams()
    model = glitch_model.load(metrics.path)
    statuses = {r["d"]: r["s"] for r in driver.execute_query("MATCH (n:Domain) RETURN n.domain AS d, n.status AS s").records}
    active_alerts = {a["node"] for a in metrics.segment_alerts() if a["active"]}
    upstream: Dict[str, List[str]] = {}
    for r in driver.execute_query("MATCH (u:Domain)-[:FEEDS|PRODUCES*1..4]->(f:Domain:FinalLink) "
                                  "RETURN f.domain AS f, collect(DISTINCT u.domain) AS ups").records:
        upstream[r["f"]] = r["ups"]
    slow = glitch_monitor.network_slow
    return glitch.forecast(metrics.path, finals, lambda n: upstream.get(n, []),
                           lambda n: statuses.get(n) == "DOWN" or n in active_alerts,
                           network_slow=bool(slow and time.time() - slow["at"] < 3 * glitch.PROBE_S),
                           model_predict=(lambda f, ups: glitch_model.predict(metrics.path, model, f, ups))
                           if model and model.get("active") else None)


def main_streams() -> List[dict]:
    """Every channel's Main input stream URL (scanned for SCTE-35 ad-break markers)."""
    out = []
    for r in driver.execute_query("MATCH (n:Domain:MainInput) WHERE NOT n.domain ENDS WITH '.invalid' "
                                  "RETURN n.domain AS node, n.links AS links").records:
        for link in load_json_list(r["links"]):
            if link.get("role") == "MainInput":
                out.append({"node": r["node"], "url": link["url"], "channel": link.get("channel")})
    return out


def ad_breaks() -> List[dict]:
    mains: Dict[str, List[dict]] = {}
    for m in main_streams():
        mains.setdefault(m["channel"], []).append(m)
    return scte.summary(scte_store, final_streams(), mains)


# --- One alert log, and predictions of the alerts that follow (alertlog.py) ---
alert_log = alertlog.AlertLog(metrics.path)
metrics.alert_log = alert_log  # incidents (OUTAGE / ESCALATED / RECOVERY)
_live_warnings: Set[str] = set()


def alert_topology() -> dict:
    """The pipeline per channel for the "what follows" predictions: its Final, inputs and transcoders, all of its
    servers, and every server's status."""
    channels: Dict[str, dict] = {}
    status = {}
    for r in driver.execute_query("MATCH (n:Domain) WHERE NOT n.domain ENDS WITH '.invalid' "
                                  "RETURN n.domain AS node, n.status AS status, n.links AS links").records:
        status[r["node"]] = r["status"]
        for link in load_json_list(r["links"]):
            c = channels.setdefault(link["channel"], {"final": None, "inputs": {}, "transcoders": [], "chain": []})
            if r["node"] not in c["chain"]:
                c["chain"].append(r["node"])
            role = link.get("role")
            if role == "FinalLink":
                c["final"] = r["node"]
            elif role in ("MainInput", "BackupLink"):
                c["inputs"][r["node"]] = role
            elif role == "Transcoding" and r["node"] not in c["transcoders"]:
                c["transcoders"].append(r["node"])
    return {"channels": channels, "status": status}


def log_run_alerts(result, warnings: List[dict]) -> None:
    """After every run: the spiders' alerts, and early warnings / segment-age alerts as they start; then refresh the
    predictions (recorded, so they can be scored)."""
    for rep in result.reports:
        for a in rep.alerts:
            alert_log.add(a.kind, a.spider_id.removeprefix("spider-"), None, a.message, "spider")
    current = set()
    for w in warnings:
        for text in w["warnings"]:
            kind = "SEGMENT_AGE_HIGH" if text.startswith("segment age") else "EARLY_WARNING"
            key = f"{w['node']}|{kind}|{text.split(' ')[0]}"
            current.add(key)
            if key not in _live_warnings:
                alert_log.add(kind, w["node"], None, text, "anomaly")
    _live_warnings.clear(); _live_warnings.update(current)
    alert_log.predict(alert_topology)


def backfill_alerts() -> int:
    """Seeds the alert log once from the history kept before it existed: outages and recoveries read from the
    saved checks (last 7 days), glitches, and stuck or overrun ad breaks, so "what follows what" is learned now."""
    since = time.time() - alertlog.LEARN_DAYS * 86400
    events = []
    for node in metrics.nodes():
        for i in metrics._check_incidents(node, since):
            if i["type"] in ("OUTAGE", "RECOVERY"):
                events.append({"ts": i["t"], "node": node, "kind": i["type"], "detail": i.get("category") or ""})
    for g in glitch_probe.events(since_s=alertlog.LEARN_DAYS * 86400, limit=100000):
        events.append({"ts": g["ts"], "node": g["node"], "kind": "GLITCH", "channel": g.get("channel"), "detail": g["words"]})
    for b in scte_store.breaks(since_s=alertlog.LEARN_DAYS * 86400, limit=100000):
        if b["status"] in ("STUCK", "OVERRUN"):
            events.append({"ts": b["start"], "node": b["node"], "kind": "AD_" + b["status"], "channel": b.get("channel")})
    return alert_log.backfill(events)


scte_store = scte.ScteStore(metrics.path)
glitch_probe = glitch.GlitchProbe(metrics.path, scte_store=scte_store)
glitch_probe.on_alert = lambda kind, node, channel, detail, source, ts: alert_log.add(kind, node, channel, detail, source, ts)
glitch_monitor = glitch.GlitchMonitor(glitch_probe, final_streams, lambda: scheduler.enabled, hub.publish,
                                      mains=main_streams, retrain=train_glitch_model)


def run_backup() -> dict:
    """One backup run (nightly from the scheduler, or POST /api/backup)."""
    try:
        result = backup.run(metrics.path, driver)
        hub.publish("backup", {"state": "done", **result})
        return result
    except Exception as e:  # a failed backup must never stop monitoring
        hub.publish("backup", {"state": "error", "message": str(e)})
        raise
    finally:
        scheduler.backup_running = False


class SyncBusy(Exception):
    pass


def cron_embedding_sync() -> None:
    try:
        run_embedding_sync("cron", clear_logs=True)
    except SyncBusy:
        pass  # a manual sync is running; that counts
    except Exception as e:  # reported to the pages by run_embedding_sync
        print(f"[embeddings] weekly sync failed: {type(e).__name__}: {e}")
    finally:
        scheduler.embedding_running = embedding_lock.locked()


def run_embedding_sync(source: str, clear_logs: bool = True, all_logs: bool = False) -> dict:
    """The one Embeddings & Log Clearance run, for the weekly cron and the dashboard button alike:
    embed every node, archive + clear incident logs, then restart the weekly clock."""
    if not embedding_lock.acquire(blocking=False):
        raise SyncBusy("An embeddings sync is already running.")
    scheduler.embedding_running = True
    hub.publish("embeddings", {"state": "running", "source": source})
    try:
        try:
            res = sync_all_embeddings(driver, clear_logs=clear_logs, all_logs=all_logs)
        except TypeError:
            res = sync_all_embeddings(driver, clear_logs=clear_logs)
        result = {**res, "source": source, "at": time.time()}
        scheduler.record_embedding_sync(result)
        hub.publish("embeddings", {"state": "done", **result})
        return result
    except Exception as e:
        hub.publish("embeddings", {"state": "error", "source": source, "message": f"{type(e).__name__}: {e}"})
        if source == "cron":  # try again tomorrow instead of every second
            scheduler.last_embedding_sync = time.time() - WEEK_S + 24 * 3600
        raise
    finally:
        scheduler.embedding_running = False
        embedding_lock.release()
        hub.publish("scheduler", scheduler.state())


def start_run(final_ids: Optional[List[str]] = None, source: str = "manual") -> Optional[str]:
    """Start a spider cycle in the background (all spiders, or just `final_ids`), streaming its
    events through the hub. Returns the run id, or None if a cycle is already running."""
    if not run_lock.acquire(final_ids, blocking=False):
        return None
    run_id = uuid.uuid4().hex[:8]
    info = {"run": run_id, "source": source, "finals": final_ids, "started_at": time.time()}
    current_run.clear()
    current_run.update(info)
    hub.publish("start", info)

    def work() -> None:
        try:
            result = SpiderManager(driver, on_node_checked=tracer.on_node_checked, metrics=metrics, learning=learner).run_cycle(final_ids, listener=lambda e: hub.publish("step", {**e, "run": run_id}))
            summary = {
                "run": run_id, "source": source, "finals": final_ids,
                "reports": [r.model_dump() for r in result.reports],
                "transitions": [t.model_dump() for t in result.node_transitions],
                "incidents": [{k: v for k, v in i.items() if k in INCIDENT_FIELDS} for i in result.incidents],
                "ranking": result.ranking, "warnings": early_warnings(),
                "active": result.active_spiders, "stopped": result.stopped_spiders, "elapsed_ms": result.elapsed_ms,
            }
            if result.ranking:
                latest_ranking.update(at=time.time(), groups=result.ranking)
            scheduler.last_run = {"run": run_id, "source": source, "at": time.time(), "elapsed_ms": result.elapsed_ms,
                                  "spiders": len(result.reports), "stopped": result.stopped_spiders}
            hub.publish("done", summary)
            try:
                log_run_alerts(result, summary["warnings"])
            except Exception as e:  # logging alerts must never break a run
                print(f"[alerts] {type(e).__name__}: {e}")

            # Run predictive failure scoring in background — never blocks the health cycle.
            def _run_predictions() -> None:
                global _last_predictions
                try:
                    G = fetch_graph(driver)
                    preds = predict_all(driver, G["nodes"])
                    _last_predictions = preds
                    high = [p for p in preds if p["band"] in ("HIGH", "CRITICAL")]
                    hub.publish("predictions", {"predictions": preds, "highRisk": high})
                except Exception as pe:
                    pass  # prediction errors must never break the health pipeline
            threading.Thread(target=_run_predictions, daemon=True).start()
        except Exception as e:  # surfaced to every page instead of a silent failure
            hub.publish("error", {"run": run_id, "message": f"{type(e).__name__}: {e}"})
        finally:
            current_run.clear()
            run_lock.release(final_ids)
            hub.publish("scheduler", scheduler.state())

    threading.Thread(target=work, daemon=True).start()
    return run_id


@asynccontextmanager
async def lifespan(_app: FastAPI):
    hub.loop = asyncio.get_running_loop()
    text_embedding.preload()  # the ChatBot's first question shouldn't wait for the model
    try:
        from nodes import ensure_database_indexes
        ensure_database_indexes(driver)
    except Exception as e:
        print(f"[neo4j] index initialization: {e}")
    try:
        moved = migrate_logs(driver, metrics)
        if moved:
            print(f"[incidents] moved {moved} log entries from Neo4j into the incident store")
    except Exception as e:  # never block startup on it
        print(f"[incidents] migration skipped: {type(e).__name__}: {e}")
    task = asyncio.create_task(scheduler.loop())
    glitch_monitor.start()  # its own thread; probes the Finals once a minute while auto-check is on

    def _seed_alerts():
        try:
            print(f"[alerts] seeded {backfill_alerts()} alerts from history")
        except Exception as e:
            print(f"[alerts] seeding skipped: {type(e).__name__}: {e}")
        try:
            chat_store.prune()  # conversations untouched for 90 days
            voice.store.prune()  # voice clips beyond VOICE_DB_MAX_MB (unrated, oldest first)
            converted = voice.store.compact()  # older WAV clips → FLAC (lossless, about half the size)
            if converted:
                print(f"[voice] re-encoded {converted} clips as FLAC")
        except Exception as e:
            print(f"[startup] housekeeping skipped: {type(e).__name__}: {e}")
    threading.Thread(target=_seed_alerts, name="alert-backfill", daemon=True).start()
    try:
        yield
    finally:
        glitch_monitor.stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        tracer.shutdown()
        driver.close()


app = FastAPI(title="Stream Graph", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=WEB), name="static")

# Everything needs a signed-in session except the login flow and a bare health check.
PUBLIC_PATHS = {"/login", "/logout", "/healthz"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    if request.url.path in PUBLIC_PATHS or auth.verify(request.cookies.get(COOKIE)):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Login required"}, status_code=401)
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(login_url(target), status_code=303)


# Responses are gzipped (the graph JSON is ~300 KB and every open page fetches it every few seconds; gzip makes it
# ~10x smaller), except live streams, whose events must leave at once. Versioned static files (index.html stamps
# ?v=<mtime>) can be cached by the browser for good; a changed file gets a new URL.
STREAM_PATH_SUFFIXES = ("/api/stream", "/api/chat", "/analysis", "/rca")


class FastResponses:
    def __init__(self, app):
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=1024, compresslevel=5)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path.startswith("/static/") and b"v=" in scope.get("query_string", b""):
            inner = send

            async def send(message):  # noqa: F811 - wraps the original send
                if message["type"] == "http.response.start":
                    message["headers"] = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                    message["headers"].append((b"cache-control", b"public, max-age=31536000, immutable"))
                await inner(message)
        if path.endswith(STREAM_PATH_SUFFIXES) or path.startswith("/api/stream"):
            return await self.app(scope, receive, send)
        return await self.gzip(scope, receive, send)


app.add_middleware(FastResponses)


def jsonable(value):
    """Neo4j temporal values -> ISO strings; containers recursively."""
    if hasattr(value, "iso_format"):
        return value.iso_format()
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [jsonable(v) for v in value]
    return value


VECTOR_PROPS = ("embedding", "embeddingHash", "ragEmbedding", "ragHash")


def slim(props: Optional[dict]) -> Optional[dict]:
    """Props without the 384-number vectors (the page only shows their size): keeps each refresh small."""
    if not props:
        return props
    out = {k: v for k, v in props.items() if k not in VECTOR_PROPS}
    if isinstance(props.get("embedding"), list):
        out["embeddingDims"] = len(props["embedding"])
    return out


def monitor_status() -> dict:
    """The scheduler facts the ChatBot can retrieve: the scheduler and any run in progress, times in IST."""
    last = scheduler.last_run
    return {
        "autoPing": scheduler.enabled, "intervalSeconds": scheduler.interval,
        "nextRun": ist(scheduler.next_run_at) if scheduler.enabled else None,
        "runInProgress": dict(current_run, started_at=ist(current_run.get("started_at"))) if current_run else None,
        "lastRun": {**last, "at": ist(last["at"])} if last else None,
        "now": ist(time.time()),
    }


chatbot = ChatBot(driver, monitor_status)
chatbot.metrics = metrics
chatbot.learning = learner
chat_eval = ChatEval(metrics.path, chatbot, learner)  # the chatbot's exam, run on demand
history_ai = HistoryAI(chatbot)  # plain-words reading of a server's full history (history_ai.py)


# Human-sounding voice alerts from the phrase library, no API calls; clips kept in voice.db for training (voice.py).
voice = Voice(voice_store())


def _glitch_insights() -> List[dict]:
    try:
        return [g for g in glitch_forecast() if g["band"] != "LOW" or g["lastHour"]]
    except Exception:  # the chat must answer even if the glitch history can't be read
        return []


def _ad_insights() -> List[dict]:
    try:
        return [c for c in ad_breaks() if c["breaks7d"] or c["issues"]]
    except Exception:
        return []


def _upcoming_insights() -> dict:
    try:
        return {"upcoming": alert_log.predict(alert_topology, record=False)[:8], "scores": alert_log.scores()}
    except Exception:
        return {}


chatbot.insights = lambda: {"ranking": current_ranking()["groups"], "warnings": early_warnings(),
                            "glitches": _glitch_insights(), "adBreaks": _ad_insights(), **_upcoming_insights()}
chatbot.tracer = tracer
chatbot.start_run = start_run


def node_for_traceroute(node_id: str) -> dict:
    records = driver.execute_query("MATCH (n:Domain {domain: $id}) RETURN n.domain AS id, n.server_ip AS server_ip",
                                   id=node_id).records
    if not records:
        raise HTTPException(404, f"No node {node_id!r}")
    return dict(records[0])


def early_warnings() -> List[dict]:
    """Servers still up whose metrics are far from their normal (anomaly score or rising trend)."""
    out = []
    for node, a in metrics.all_anomalies().items():
        found = anomaly_warnings(a)
        if found and not node.endswith(".invalid"):
            out.append({"node": node, "score": a["score"], "warnings": found})
    return sorted(out, key=lambda w: -w["score"])


def current_ranking() -> dict:
    """The likely root cause of each current failure group, ranked (computed now from the live state)."""
    from spider import node_states
    ids = [r["d"] for r in driver.execute_query(
        "MATCH (n:Domain) WHERE NOT n.domain ENDS WITH '.invalid' RETURN n.domain AS d").records]
    states = node_states(driver, ids)
    edges = [r.model_dump() for r in current_topology(driver)]
    groups = rca_rank.rank(states, edges, {n: metrics.anomalies(n) for n in ids}, learner.priors())
    return {"groups": groups, "down": [n for n, s in states.items() if not s["up"]]}


@app.get("/api/pool/stats")
async def api_pool_stats():
    """Operational telemetry pool statistics (cache hits, misses, node count, freshness)."""
    return telemetry_pool.stats()


# The routes live in routes/ (imported last: they read this module's state at call time).
from routes import chat, diagnostics, learning, monitor, notifications, pages, reports, skills, voice as voice_routes  # noqa: E402

for _module in (pages, monitor, diagnostics, reports, chat, skills, learning, notifications, voice_routes):
    app.include_router(_module.router)

