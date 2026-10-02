"""One log of every alert and warning, and predictions of the alerts that will follow.

Everything that warns about the streams writes here (kept RETENTION_DAYS):
  OUTAGE · ESCALATED · RECOVERY       incidents (metrics.add_incident)
  SPIDER_STOPPED · FAILOVER_ACTIVE ·  a channel's spider stopped, running on its backup, recovered (each run)
  SPIDER_RECOVERED · SPIDER_ERROR
  EARLY_WARNING · SEGMENT_AGE_HIGH    a server drifting from its normal while still up (when it starts)
  GLITCH                              a glitch on a Final (glitch.py), the glitch kind in the detail
  AD_STUCK · AD_OVERRUN               ad breaks (scte.py)
  NETWORK_SLOW                        the monitor's own connection (node "monitor")
The same alert on the same node within DEDUPE_S counts once.

Predictions ("what alerts come next"), from what's alerting in the last TRIGGER_S:
  pipeline   from the graph itself, works from the first minute: a failing server's downstream channels go off air
             when nothing else feeds them, or switch to their backup when a healthy one exists
  learned    from this log: when A alerts, B followed within FOLLOW_S in at least MIN_SHARE of A's alerts (and
             MIN_LIFT times more often than B happens anyway), seen at least MIN_SUPPORT times; with B's usual delay
Every prediction is recorded and later scored against what really happened: a HIT if the alert came before its
deadline (with how much warning it gave), else a MISS. The hit rate per source is what tells you to trust it.
"""

import json
import math
import sqlite3
import statistics
import threading
import time
from collections import defaultdict
from typing import Callable, Dict, List, Optional

RETENTION_DAYS = 14
DEDUPE_S = 90
TRIGGER_S = 600  # alerts this recent start predictions
FOLLOW_S = 900  # B "follows" A if it alerts within this long after
LEARN_DAYS = 7
MIN_SUPPORT, MIN_SHARE, MIN_LIFT = 3, 0.3, 2.0
MIN_LAG_S = 30  # B usually within seconds of A: they happen together (one incident noticed in some order), no warning
TRIGGER_KINDS = {"OUTAGE", "ESCALATED", "SPIDER_STOPPED", "FAILOVER_ACTIVE", "EARLY_WARNING", "SEGMENT_AGE_HIGH",
                 "GLITCH", "AD_STUCK", "NETWORK_SLOW", "SPIDER_ERROR"}
CALM_KINDS = {"RECOVERY", "SPIDER_RECOVERED"}  # endings: never predicted from, never "followed"
SEVERITY = {"OUTAGE": "error", "ESCALATED": "error", "SPIDER_STOPPED": "error", "SPIDER_ERROR": "error",
            "AD_STUCK": "error", "FAILOVER_ACTIVE": "warn", "EARLY_WARNING": "warn", "SEGMENT_AGE_HIGH": "warn",
            "GLITCH": "warn", "AD_OVERRUN": "warn", "NETWORK_SLOW": "warn", "RECOVERY": "ok", "SPIDER_RECOVERED": "ok"}
WORDS = {"OUTAGE": "goes down", "ESCALATED": "stays down (escalated)", "SPIDER_STOPPED": "its channel stops",
         "FAILOVER_ACTIVE": "switches to its backup", "EARLY_WARNING": "an early warning", "SEGMENT_AGE_HIGH":
         "video falling behind (segment age high)", "GLITCH": "glitches", "AD_STUCK": "stuck in an ad break",
         "AD_OVERRUN": "an ad break overruns", "NETWORK_SLOW": "the monitor's network slows",
         "SPIDER_ERROR": "its checker errors", "RECOVERY": "recovers", "SPIDER_RECOVERED": "its channel recovers"}
PIPELINE_LAG_S = 90  # a downstream Final notices an upstream failure within a couple of checks

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, node TEXT NOT NULL, channel TEXT, kind TEXT NOT NULL,
    severity TEXT, source TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS alerts_ts ON alerts (ts);
CREATE INDEX IF NOT EXISTS alerts_node_kind_ts ON alerts (node, kind, ts);
CREATE TABLE IF NOT EXISTS alert_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, made_at REAL NOT NULL, trigger_id INTEGER, node TEXT NOT NULL,
    kind TEXT NOT NULL, probability REAL, expected_at REAL, deadline REAL NOT NULL, source TEXT, reason TEXT,
    outcome TEXT NOT NULL DEFAULT 'PENDING', hit_at REAL, UNIQUE (trigger_id, node, kind));
CREATE INDEX IF NOT EXISTS alert_predictions_outcome ON alert_predictions (outcome, deadline);
"""


class AlertLog:
    def __init__(self, path: str):
        self.path = str(path)
        self.lock = threading.Lock()
        self._learned: Optional[tuple] = None  # (computed at, rules)
        with self._connect() as db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    # --- writing ---

    def add(self, kind: str, node: str, channel: Optional[str] = None, detail: str = "", source: str = "",
            ts: Optional[float] = None) -> Optional[int]:
        ts = ts or time.time()
        with self.lock, self._connect() as db:
            dup = db.execute("SELECT id FROM alerts WHERE node = ? AND kind = ? AND ABS(ts - ?) < ?",
                             (node, kind, ts, DEDUPE_S)).fetchone()
            if dup:
                return None
            cur = db.execute("INSERT INTO alerts (ts, node, channel, kind, severity, source, detail) VALUES (?,?,?,?,?,?,?)",
                             (ts, node, channel, kind, SEVERITY.get(kind, "warn"), source, (detail or "")[:300]))
            # A prediction it fulfils is a hit, with how much warning it gave.
            db.execute("UPDATE alert_predictions SET outcome = 'HIT', hit_at = ? WHERE outcome = 'PENDING' AND node = ? "
                       "AND kind = ? AND made_at <= ? AND deadline >= ?", (ts, node, kind, ts, ts))
            return cur.lastrowid

    def prune(self, now: Optional[float] = None) -> None:
        cutoff = (now or time.time()) - RETENTION_DAYS * 86400
        with self.lock, self._connect() as db:
            db.execute("DELETE FROM alerts WHERE ts < ?", (cutoff,))
            db.execute("DELETE FROM alert_predictions WHERE made_at < ?", (cutoff,))

    def backfill(self, events: List[dict]) -> int:
        """Seeds the log from history recorded before it existed (once: only when it has nothing older than the
        oldest event). events: [{"ts", "node", "kind", "channel"?, "detail"?}]. Returns how many were added."""
        if not events:
            return 0
        with self._connect() as db:
            oldest = db.execute("SELECT MIN(ts) FROM alerts").fetchone()[0]
        if oldest is not None and oldest <= min(e["ts"] for e in events) + DEDUPE_S:
            return 0  # already seeded
        added = 0
        for e in sorted(events, key=lambda e: e["ts"]):
            if self.add(e["kind"], e["node"], e.get("channel"), e.get("detail", ""), "history", e["ts"]):
                added += 1
        self._learned = None
        return added

    # --- reading ---

    def recent(self, since_s: float = 86400, node: Optional[str] = None, limit: int = 300,
               now: Optional[float] = None) -> List[dict]:
        sql, args = "SELECT * FROM alerts WHERE ts >= ?", [(now or time.time()) - since_s]
        if node:
            sql += " AND node = ?"; args.append(node)
        with self._connect() as db:
            rows = db.execute(sql + " ORDER BY ts DESC LIMIT ?", (*args, limit)).fetchall()
        return [{**dict(r), "words": WORDS.get(r["kind"], r["kind"].lower())} for r in rows]

    # --- learning: which alerts follow which ---

    def learn(self, now: Optional[float] = None) -> List[dict]:
        """Rules "A on X -> B on Y within ~lag", from the last LEARN_DAYS of the log. Cached for 10 minutes."""
        now = now or time.time()
        if self._learned and now - self._learned[0] < 600:
            return self._learned[1]
        with self._connect() as db:
            rows = [dict(r) for r in db.execute("SELECT id, ts, node, kind FROM alerts WHERE ts >= ? ORDER BY ts",
                                                (now - LEARN_DAYS * 86400,))]
        span = max(3600.0, (now - rows[0]["ts"]) if rows else 0.0)
        counts: Dict[tuple, int] = defaultdict(int)
        follows: Dict[tuple, List[float]] = defaultdict(list)
        for i, a in enumerate(rows):
            if a["kind"] in CALM_KINDS:
                continue
            a_key = (a["node"], a["kind"])
            counts[a_key] += 1
            seen = set()
            for b in rows[i + 1:]:
                if b["ts"] - a["ts"] > FOLLOW_S:
                    break
                b_key = (b["node"], b["kind"])
                if b_key == a_key or b["kind"] in CALM_KINDS or b_key in seen:
                    continue
                seen.add(b_key)
                follows[(a_key, b_key)].append(b["ts"] - a["ts"])
        totals: Dict[tuple, int] = defaultdict(int)
        for r in rows:
            totals[(r["node"], r["kind"])] += 1
        rules = []
        for (a_key, b_key), lags in follows.items():
            n, share = len(lags), len(lags) / counts[a_key]
            base = 1 - math.exp(-totals[b_key] / span * FOLLOW_S)  # chance B happens in any window anyway
            lift = share / max(base, 1e-3)
            if n >= MIN_SUPPORT and share >= MIN_SHARE and lift >= MIN_LIFT and statistics.median(lags) >= MIN_LAG_S:
                rules.append({"if_node": a_key[0], "if_kind": a_key[1], "then_node": b_key[0], "then_kind": b_key[1],
                              "share": round(share, 2), "support": n, "of": counts[a_key], "lift": round(lift, 1),
                              "lag_s": round(statistics.median(lags))})
        rules.sort(key=lambda r: (-r["share"], -r["support"]))
        self._learned = (now, rules)
        return rules

    # --- predicting ---

    def predict(self, topology: Optional[Callable[[], dict]] = None, now: Optional[float] = None,
                record: bool = True) -> List[dict]:
        """The alerts likely to follow what's alerting now, soonest and likeliest first; recorded for scoring."""
        now = now or time.time()
        triggers = [a for a in self.recent(TRIGGER_S, now=now, limit=200) if a["kind"] in TRIGGER_KINDS]
        if not triggers:
            self.evaluate(now)
            return []
        with self._connect() as db:
            happened = {(r["node"], r["kind"]): r["ts"] for r in db.execute(
                "SELECT node, kind, MAX(ts) AS ts FROM alerts WHERE ts >= ? GROUP BY node, kind", (now - TRIGGER_S - FOLLOW_S,))}
        rules = self.learn(now)
        topo = topology() if topology else None
        found: Dict[tuple, dict] = {}

        def offer(trigger, node, kind, prob, lag, source, reason):
            key = (node, kind)
            if happened.get(key, 0) >= trigger["ts"]:
                return  # already happened since the trigger: not a prediction any more
            expected = trigger["ts"] + lag
            deadline = trigger["ts"] + max(3 * lag, 600)
            if deadline < now:
                return  # its window passed without it
            cur = found.get(key)
            if not cur or prob > cur["probability"]:
                found[key] = {"node": node, "kind": kind, "words": WORDS.get(kind, kind.lower()), "probability": round(prob, 2),
                              "expectedAt": expected, "deadline": deadline, "source": source, "reason": reason,
                              "trigger": {"id": trigger["id"], "node": trigger["node"], "kind": trigger["kind"],
                                          "ts": trigger["ts"]}}

        for t in triggers:
            for r in rules:
                if r["if_node"] == t["node"] and r["if_kind"] == t["kind"]:
                    offer(t, r["then_node"], r["then_kind"], r["share"], r["lag_s"], "learned",
                          f"after {short(t['node'])} {WORDS.get(t['kind'], t['kind'])}, this followed {r['support']} of "
                          f"{r['of']} times, usually ~{fmt_s(r['lag_s'])} later")
            if topo and t["kind"] in ("OUTAGE", "ESCALATED", "SPIDER_STOPPED"):
                for p in pipeline_followers(topo, t["node"]):
                    offer(t, p["node"], p["kind"], p["probability"], PIPELINE_LAG_S, "pipeline", p["reason"])
        out = sorted(found.values(), key=lambda p: (-p["probability"], p["expectedAt"]))
        if record and out:
            with self.lock, self._connect() as db:
                db.executemany("INSERT OR IGNORE INTO alert_predictions (made_at, trigger_id, node, kind, probability, "
                               "expected_at, deadline, source, reason) VALUES (?,?,?,?,?,?,?,?,?)",
                               [(now, p["trigger"]["id"], p["node"], p["kind"], p["probability"], p["expectedAt"],
                                 p["deadline"], p["source"], p["reason"]) for p in out])
        self.evaluate(now)
        return out

    def evaluate(self, now: Optional[float] = None) -> None:
        """Predictions whose deadline passed without their alert are misses (hits are marked as alerts arrive)."""
        with self.lock, self._connect() as db:
            db.execute("UPDATE alert_predictions SET outcome = 'MISS' WHERE outcome = 'PENDING' AND deadline < ?",
                       (now or time.time(),))

    def scores(self, days: float = 7) -> dict:
        """How the predictions did, per source: made, hits, misses, hit rate, median warning time."""
        with self._connect() as db:
            rows = db.execute("SELECT source, outcome, made_at, hit_at FROM alert_predictions WHERE made_at >= ?",
                              (time.time() - days * 86400,)).fetchall()
        out = {}
        for source in ("pipeline", "learned"):
            mine = [r for r in rows if r["source"] == source]
            hits = [r for r in mine if r["outcome"] == "HIT"]
            decided = [r for r in mine if r["outcome"] != "PENDING"]
            leads = [r["hit_at"] - r["made_at"] for r in hits if r["hit_at"]]
            out[source] = {"made": len(mine), "hits": len(hits), "misses": len(decided) - len(hits),
                           "pending": len(mine) - len(decided),
                           "hitRate": round(len(hits) / len(decided), 2) if decided else None,
                           "medianWarningS": round(statistics.median(leads)) if leads else None}
        return out


def pipeline_followers(topo: dict, node: str) -> List[dict]:
    """From the graph: for each channel whose chain runs through this failing server, what its Final will do —
    go off air (nothing else healthy feeds it) or switch to its backup (a healthy one exists).
    topo: {"channels": {channel: {"final": node, "inputs": {node: role}, "chain": [nodes]}}, "status": {node: UP|DOWN}}"""
    out = []
    status = topo.get("status", {})
    for channel, c in topo.get("channels", {}).items():
        final = c.get("final")
        if not final or final == node or node not in c.get("chain", []):
            continue
        inputs = c.get("inputs", {})
        transcoders = c.get("transcoders", [])
        healthy_inputs = [n for n in inputs if n != node and status.get(n) != "DOWN"]
        if node in transcoders or not healthy_inputs:
            out.append({"node": final, "kind": "OUTAGE", "probability": 0.8,
                        "reason": f"{channel}'s Final depends on {short(node)}"
                                  + ("" if node in transcoders else " and no other input is healthy")})
        elif inputs.get(node) == "MainInput":
            backup = next((n for n in healthy_inputs if inputs[n] == "BackupLink"), None)
            if backup:
                out.append({"node": final, "kind": "FAILOVER_ACTIVE", "probability": 0.7,
                            "reason": f"{channel}'s Main ({short(node)}) is failing; its backup {short(backup)} is healthy"})
    return out


def short(node: str) -> str:
    return str(node or "").replace(".ottlive.co.in", "").replace(".co.in", "")


def fmt_s(s: float) -> str:
    return f"{round(s)} s" if s < 90 else f"{round(s / 60)} min"
