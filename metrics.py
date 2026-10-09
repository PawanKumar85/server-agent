"""Per-check time series (SQLite) and statistical anomaly detection.

Every recorded check adds one row per node (latency, ICMP RTT/jitter/loss, newest-segment age, up) and one per
URL. Anomalies compare each node's latest values with its own recent normal using robust statistics (median and
MAD, so one spike doesn't shift the baseline) and flag slow upward trends; they work while a stream is still
up, as an early warning. Neo4j keeps the graph and current state; history lives here.

It also holds the incident log (outage / escalation / recovery entries with their correlation and root-cause
ranking), with no size cap: INCIDENT_RETENTION_DAYS (default 90) bounds it, and the weekly embeddings job
folds older entries into each node's `incidentStats` totals before deleting them.

METRICS_DB sets the file (a volume in Docker); METRICS_RETENTION_DAYS (default 14) bounds the check history.
"""

import json
import math
import os
import sqlite3
import statistics
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

RETENTION_DAYS = int(os.environ.get("METRICS_RETENTION_DAYS", "14"))
INCIDENT_RETENTION_DAYS = int(os.environ.get("INCIDENT_RETENTION_DAYS", "90"))
WINDOW = 120  # recent checks forming a node's "normal" (about an hour at 30 s)
MIN_SAMPLES = 20  # fewer than this: no baseline yet
RECENT = 5  # checks compared against the baseline for a trend
Z_WARN = 4.0  # robust z-score from which a value counts as anomalous
# Minimum spread per metric, so a metric that is usually constant doesn't turn noise into huge z-scores.
FLOORS = {"latency_ms": 25.0, "rtt_ms": 5.0, "jitter_ms": 5.0, "segment_age_s": 1.5, "loss": 0.1}
METRICS = tuple(FLOORS)
NODE_METRICS = tuple(m for m in METRICS if m != "segment_age_s")  # segment age is learned per URL instead

SCHEMA = """
CREATE TABLE IF NOT EXISTS node_checks (
    ts REAL NOT NULL, node TEXT NOT NULL, up INTEGER NOT NULL, latency_ms REAL, rtt_ms REAL, jitter_ms REAL,
    loss REAL, segment_age_s REAL, failing_urls INTEGER, category TEXT,
    n INTEGER NOT NULL DEFAULT 1, fails INTEGER NOT NULL DEFAULT 0, rolled INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS node_checks_node_ts ON node_checks (node, ts);
CREATE TABLE IF NOT EXISTS urls (id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, node TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS url_check_rows (
    ts REAL NOT NULL, url_id INTEGER NOT NULL, up INTEGER NOT NULL, latency_ms REAL, segment_age_s REAL,
    freshness TEXT, category TEXT, detail TEXT, target_s REAL,
    n INTEGER NOT NULL DEFAULT 1, fails INTEGER NOT NULL DEFAULT 0, rolled INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS url_check_rows_url_ts ON url_check_rows (url_id, ts);
CREATE INDEX IF NOT EXISTS url_check_rows_ts ON url_check_rows (ts);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, node TEXT NOT NULL, type TEXT NOT NULL,
    category TEXT, entry TEXT NOT NULL, UNIQUE (node, ts, type));
CREATE INDEX IF NOT EXISTS incidents_node_ts ON incidents (node, ts);
CREATE TABLE IF NOT EXISTS segment_alerts (
    url TEXT PRIMARY KEY, node TEXT NOT NULL, k REAL NOT NULL, active INTEGER NOT NULL DEFAULT 0, since REAL,
    highs INTEGER NOT NULL DEFAULT 0, raised INTEGER NOT NULL DEFAULT 0, cleared_alone INTEGER NOT NULL DEFAULT 0,
    before_outage INTEGER NOT NULL DEFAULT 0, too_sensitive INTEGER NOT NULL DEFAULT 0, last_ts REAL);
"""

# Per-URL checks are stored against a small id (the URL once, in `urls`); the old row shape stays readable as a
# view, so queries keep saying `FROM url_checks`.
URL_CHECKS_VIEW = """
CREATE VIEW IF NOT EXISTS url_checks AS
SELECT r.ts, u.node, u.url, r.up, r.latency_ms, r.segment_age_s, r.freshness, r.category, r.detail, r.target_s,
       r.n, r.fails, r.rolled
FROM url_check_rows r JOIN urls u ON u.id = r.url_id;
"""
# Full detail for the last RAW_HOURS; older checks are folded into one row per ROLLUP_S per node / URL (with how
# many checks and failures it holds), so two weeks of history stay small. Charts and learned lines read both.
RAW_HOURS = 48
ROLLUP_S = 300
COMPACT_EVERY_S = 3600

# Segment age is judged per stream URL against that URL's own history (a source whose clock runs 40 s late
# is normal at 40 s; a fast one at 3 s), not per server against one hour like the other metrics.
SEGMENT_BASELINE_DAYS = 7
SEGMENT_HOUR_MIN = 60  # checks in this hour of the day (over several days) before the hour gets its own normal
SEGMENT_HIGHS_TO_WARN = 3  # high checks in a row before warning (one slow check is not a trend)
SEGMENT_K = 4.0  # starting sensitivity: warn at normal + K robust spreads (and at least one segment length)
SEGMENT_K_MIN, SEGMENT_K_MAX, SEGMENT_K_STEP = 3.0, 10.0, 0.25
SEGMENT_CACHE_S = 600



def robust(values: List[float]) -> Optional[Dict[str, float]]:
    if len(values) < MIN_SAMPLES:
        return None
    median = statistics.median(values)
    mad = statistics.median(abs(v - median) for v in values)
    return {"median": median, "mad": mad}


def zscore(value: float, base: Dict[str, float], floor: float) -> float:
    return (value - base["median"]) / max(1.4826 * base["mad"], floor)


class Metrics:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.writes = 0
        self._baselines: Dict[str, tuple] = {}  # url -> (computed at, baseline), see segment_baseline
        self._url_ids: Dict[str, int] = {}
        self.alert_log = None  # alertlog.AlertLog, set by the server: incidents also go into the one alert log
        self._compacted_at = 0.0
        self._passed: Optional[tuple] = None  # (computed at, counted urls, result), see checks_passed
        self._passed_lock = threading.Lock()
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")  # safe with WAL, and far fewer disk syncs per check
            self._migrate(db)
            db.executescript(SCHEMA)
            db.executescript(URL_CHECKS_VIEW)

    @staticmethod
    def _migrate(db) -> None:
        """Older files: url_checks was a table with the URL and node text on every row, node_checks had no
        rollup columns. Move the rows into the compact layout once (then the file is vacuumed smaller)."""
        node_cols = {r["name"] for r in db.execute("PRAGMA table_info(node_checks)")}
        if node_cols and "n" not in node_cols:
            for col, default in (("n", 1), ("fails", 0), ("rolled", 0)):
                db.execute(f"ALTER TABLE node_checks ADD COLUMN {col} INTEGER NOT NULL DEFAULT {default}")
            db.execute("UPDATE node_checks SET fails = 1 - up")
        kind = db.execute("SELECT type FROM sqlite_master WHERE name = 'url_checks'").fetchone()
        if kind and kind[0] == "table":
            cols = {r["name"] for r in db.execute("PRAGMA table_info(url_checks)")}
            target = "target_s" if "target_s" in cols else "NULL"
            db.executescript(SCHEMA)
            db.execute("INSERT OR IGNORE INTO urls (url, node) SELECT url, MAX(node) FROM url_checks GROUP BY url")
            db.execute(f"""INSERT INTO url_check_rows (ts, url_id, up, latency_ms, segment_age_s, freshness, category,
                           detail, target_s, n, fails)
                           SELECT c.ts, u.id, c.up, c.latency_ms, c.segment_age_s, c.freshness, c.category,
                                  CASE WHEN c.up = 1 THEN NULL ELSE c.detail END, {target}, 1, 1 - c.up
                           FROM url_checks c JOIN urls u ON u.url = c.url""")
            db.execute("DROP TABLE url_checks")
            db.commit()
            db.execute("VACUUM")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    # --- writing ---

    def record(self, node: str, health, ts: Optional[float] = None) -> None:
        """One row for the node and one per URL, from a NodeHealth."""
        ts = ts or time.time()
        urls = list(health.urls or [])
        ages = [u.segment_age_s for u in urls if u.up and u.segment_age_s is not None]
        failing = [u for u in urls if not u.up and not getattr(u, "ignored", False)]
        category = next((u.category for u in failing if u.category), None)
        with self.lock, self._connect() as db:
            db.execute("INSERT INTO node_checks (ts, node, up, latency_ms, rtt_ms, jitter_ms, loss, segment_age_s, "
                       "failing_urls, category, n, fails) VALUES (?,?,?,?,?,?,?,?,?,?,1,?)", (
                ts, node, int(bool(health.up)), health.latency_ms, health.rtt_ms, health.jitter_ms,
                health.packet_loss, max(ages) if ages else None, len(failing), category, int(not health.up)))
            db.executemany("INSERT INTO url_check_rows (ts, url_id, up, latency_ms, segment_age_s, freshness, category, "
                           "detail, target_s, n, fails) VALUES (?,?,?,?,?,?,?,?,?,1,?)", [
                (ts, self._url_id(db, u.url, node), int(bool(u.up)), u.latency_ms, u.segment_age_s, u.freshness,
                 u.category, None if u.up else u.detail, u.target_duration_s, int(not u.up))  # a live URL's detail says nothing new
                for u in urls])
            for u in urls:
                self._segment_alert_step(db, ts, node, u)
            self.writes += 1
            if ts - self._compacted_at >= COMPACT_EVERY_S:
                self._compact(db, ts)

    def _url_id(self, db, url: str, node: str) -> int:
        if url not in self._url_ids:
            db.execute("INSERT OR IGNORE INTO urls (url, node) VALUES (?, ?)", (url, node))
            self._url_ids[url] = db.execute("SELECT id FROM urls WHERE url = ?", (url,)).fetchone()[0]
        return self._url_ids[url]

    def _compact(self, db, now: float) -> None:
        """Folds checks older than RAW_HOURS into one row per ROLLUP_S per node / URL (counts kept in n and fails;
        averages for the measurements, the worst segment age), and drops anything past RETENTION_DAYS."""
        self._compacted_at = now
        cutoff = now - RAW_HOURS * 3600
        cutoff -= cutoff % ROLLUP_S  # whole buckets only
        bucket = f"CAST(ts / {ROLLUP_S} AS INTEGER) * {ROLLUP_S} + {ROLLUP_S // 2}"
        db.execute(f"""INSERT INTO node_checks (ts, node, up, latency_ms, rtt_ms, jitter_ms, loss, segment_age_s,
                           failing_urls, category, n, fails, rolled)
                       SELECT {bucket}, node, CASE WHEN SUM(fails) = 0 THEN 1 ELSE 0 END, AVG(latency_ms), AVG(rtt_ms),
                              AVG(jitter_ms), AVG(loss), MAX(segment_age_s), MAX(failing_urls), MAX(category),
                              SUM(n), SUM(fails), 1
                       FROM node_checks WHERE ts < ? AND rolled = 0 GROUP BY node, {bucket}""", (cutoff,))
        db.execute("DELETE FROM node_checks WHERE ts < ? AND rolled = 0", (cutoff,))
        db.execute(f"""INSERT INTO url_check_rows (ts, url_id, up, latency_ms, segment_age_s, freshness, category,
                           detail, target_s, n, fails, rolled)
                       SELECT {bucket}, url_id, CASE WHEN SUM(fails) = 0 THEN 1 ELSE 0 END, AVG(latency_ms),
                              AVG(CASE WHEN up = 1 THEN segment_age_s END), MAX(freshness), MAX(category), MAX(detail),
                              MAX(target_s), SUM(n), SUM(fails), 1
                       FROM url_check_rows WHERE ts < ? AND rolled = 0 GROUP BY url_id, {bucket}""", (cutoff,))
        db.execute("DELETE FROM url_check_rows WHERE ts < ? AND rolled = 0", (cutoff,))
        old = now - RETENTION_DAYS * 86400
        db.execute("DELETE FROM node_checks WHERE ts < ?", (old,))
        db.execute("DELETE FROM url_check_rows WHERE ts < ?", (old,))

    # --- segment age, learned per stream URL ---

    def segment_baseline(self, url: str, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """This URL's usual newest-segment age from its healthy checks of the last SEGMENT_BASELINE_DAYS: median
        and robust spread, for this hour of the day when that hour has enough history (some sources are always
        slower at night). None with fewer than MIN_SAMPLES checks. Cached for SEGMENT_CACHE_S."""
        now = now or time.time()
        cached = self._baselines.get(url)
        if cached and now - cached[0] < SEGMENT_CACHE_S:
            return cached[1]
        with self._connect() as db:
            rows = db.execute(
                "SELECT ts, segment_age_s AS age, target_s FROM url_checks WHERE url = ? AND ts >= ? AND up = 1 "
                "AND segment_age_s BETWEEN -300 AND 600", (url, now - SEGMENT_BASELINE_DAYS * 86400)).fetchall()
        base = robust([r["age"] for r in rows])
        result = None
        if base is not None:
            hour = lambda t: int(((t + 19800) % 86400) // 3600)  # IST hour of the day
            this_hour = [r for r in rows if hour(r["ts"]) == hour(now)]
            days = len({int((r["ts"] + 19800) // 86400) for r in this_hour})
            by_hour = len(this_hour) >= SEGMENT_HOUR_MIN and days >= 3
            if by_hour:
                base = robust([r["age"] for r in this_hour])
            targets = [r["target_s"] for r in rows if r["target_s"]]
            span = (max(r["ts"] for r in rows) - min(r["ts"] for r in rows)) / 86400
            result = {"median": round(base["median"], 1), "spread": round(1.4826 * base["mad"], 2),
                      "target": statistics.median(targets) if targets else 6.0, "samples": len(rows),
                      "days": round(span, 1), "byHour": by_hour}
        self._baselines[url] = (now, result)
        return result

    @staticmethod
    def segment_lines(base: Dict[str, Any], k: float) -> Dict[str, float]:
        """Warn above normal + k spreads (at least one segment length above normal), and never while the newest
        piece is fresh by any standard (under 1.5 segment lengths: a source clock running ahead can make its
        normal negative); clear back below halfway."""
        warn = max(base["median"] + max(k * base["spread"], base["target"]), 1.5 * base["target"])
        return {"warn": round(warn, 1), "clear": round(base["median"] + 0.5 * (warn - base["median"]), 1)}

    def _segment_alert_step(self, db, ts: float, node: str, u) -> None:
        """One check of one URL through its alert: SEGMENT_HIGHS_TO_WARN high checks in a row raise it; it clears
        below the clear line. How each warning ends tunes that URL's sensitivity k: cleared on its own → less
        sensitive (it was noise); the stream failed while warned → more sensitive (it was a real early sign)."""
        if getattr(u, "ignored", False):
            return  # an ignored stream raises no early warning either
        row = db.execute("SELECT * FROM segment_alerts WHERE url = ?", (u.url,)).fetchone()
        st = dict(row) if row else {"url": u.url, "node": node, "k": SEGMENT_K, "active": 0, "since": None, "highs": 0,
                                    "raised": 0, "cleared_alone": 0, "before_outage": 0, "too_sensitive": 0}
        st["node"], st["last_ts"] = node, ts
        if not u.up:
            if st["active"]:  # the warning came before a real failure: keep this URL sensitive
                st["before_outage"] += 1
                st["k"] = max(SEGMENT_K_MIN, st["k"] - SEGMENT_K_STEP)
            st["active"], st["highs"], st["since"] = 0, 0, None
        else:
            base = self.segment_baseline(u.url, ts)
            age = u.segment_age_s
            if base is not None and age is not None:
                lines = self.segment_lines(base, st["k"])
                if age > lines["warn"]:
                    st["highs"] += 1
                    if st["highs"] >= SEGMENT_HIGHS_TO_WARN and not st["active"]:
                        st["active"], st["since"], st["raised"] = 1, ts, st["raised"] + 1
                elif age <= lines["clear"] or age <= 1.5 * base["target"]:  # back to normal, or simply fresh
                    if st["active"]:  # it settled by itself: probably noise, so a little less sensitive
                        st["cleared_alone"] += 1
                        st["k"] = min(SEGMENT_K_MAX, st["k"] + SEGMENT_K_STEP)
                    st["active"], st["highs"], st["since"] = 0, 0, None
                else:
                    st["highs"] = 0  # between the lines: no new warning, an active one holds
        db.execute("INSERT OR REPLACE INTO segment_alerts (url, node, k, active, since, highs, raised, cleared_alone, "
                   "before_outage, too_sensitive, last_ts) VALUES (:url, :node, :k, :active, :since, :highs, :raised, "
                   ":cleared_alone, :before_outage, :too_sensitive, :last_ts)", st)

    def segment_alerts(self, node: Optional[str] = None) -> List[Dict[str, Any]]:
        """Each URL's learned alert: its normal, warn/clear lines, sensitivity, latest age, whether it's warning
        now, and how its past warnings ended."""
        sql, args = "SELECT * FROM segment_alerts", ()
        if node:
            sql, args = sql + " WHERE node = ?", (node,)
        with self._connect() as db:
            rows = [dict(r) for r in db.execute(sql + " ORDER BY node, url", args)]
            latest = {r["url"]: db.execute("SELECT segment_age_s, up FROM url_checks WHERE url = ? "
                                           "ORDER BY ts DESC LIMIT 1", (r["url"],)).fetchone() for r in rows}
        out = []
        for r in rows:
            base = self.segment_baseline(r["url"])
            last = latest.get(r["url"]) or {}
            out.append({"url": r["url"], "node": r["node"], "k": round(r["k"], 2), "active": bool(r["active"]),
                        "since": r["since"], "age": last["segment_age_s"] if last else None,
                        "baseline": base, **(self.segment_lines(base, r["k"]) if base else {}),
                        "raised": r["raised"], "clearedAlone": r["cleared_alone"], "beforeOutage": r["before_outage"],
                        "tooSensitive": r["too_sensitive"]})
        return out

    def loosen_segment_alert(self, url: str) -> Optional[Dict[str, Any]]:
        """The operator says this URL's warning is too sensitive: raise its line a full step and clear it."""
        with self.lock, self._connect() as db:
            row = db.execute("SELECT k FROM segment_alerts WHERE url = ?", (url,)).fetchone()
            if not row:
                return None
            db.execute("UPDATE segment_alerts SET k = MIN(?, k + 1), too_sensitive = too_sensitive + 1, active = 0, "
                       "highs = 0, since = NULL WHERE url = ?", (SEGMENT_K_MAX, url))
        return next((a for a in self.segment_alerts() if a["url"] == url), None)

    # --- incidents ---

    def add_incident(self, node: str, entry: dict) -> None:
        with self.lock, self._connect() as db:
            db.execute("INSERT OR IGNORE INTO incidents (ts, node, type, category, entry) VALUES (?,?,?,?,?)",
                       (entry.get("timestamp") or "", node, entry.get("type") or "", entry.get("category"),
                        json.dumps(entry)))
            if entry.get("type") == "RECOVERY" and entry.get("class") == "BLIP":
                # Back within a minute: the outage it closes was a blip (outage_class.py); its opening entry says so.
                row = db.execute("SELECT id, entry FROM incidents WHERE node = ? AND type = 'OUTAGE' ORDER BY id DESC "
                                 "LIMIT 1", (node,)).fetchone()
                if row:
                    opened = json.loads(row["entry"])
                    if opened.get("class") != "BACKUP_FAILURE":
                        opened["class"] = "BLIP"
                        db.execute("UPDATE incidents SET entry = ? WHERE id = ?", (json.dumps(opened), row["id"]))
        if self.alert_log is not None and entry.get("type") in ("OUTAGE", "ESCALATED", "RECOVERY"):
            corr = (entry.get("correlation") or [{}])[0]
            try:
                ts = datetime.fromisoformat(str(entry.get("timestamp")).replace("Z", "+00:00")).timestamp()
            except ValueError:
                ts = None
            kind = entry["type"]
            if kind in ("OUTAGE", "ESCALATED") and entry.get("class") == "BACKUP_FAILURE":
                kind = "BACKUP_FAILURE"  # only a backup feed: shown, but not an outage
            elif kind == "RECOVERY" and entry.get("class") in ("BLIP", "BACKUP_FAILURE"):
                if entry.get("class") == "BLIP" and ts and entry.get("durationS") is not None:
                    self.alert_log.relabel_last(node, "OUTAGE", "BLIP", ts - entry["durationS"] - 120)
                return  # no recovery for something that was never counted as an outage
            self.alert_log.add(kind, node, corr.get("channel"), entry.get("category") or entry.get("lastError") or "",
                               "incident", ts)

    def incidents(self, node: Optional[str] = None, limit: int = 50, since: Optional[str] = None) -> List[dict]:
        """The latest `limit` incident entries (of one node, or all), oldest first; each has its "node"."""
        where, args = [], []
        if node:
            where.append("node = ?"), args.append(node)
        if since:
            where.append("ts >= ?"), args.append(since)
        sql = "SELECT node, entry FROM incidents" + (f" WHERE {' AND '.join(where)}" if where else "")
        with self._connect() as db:
            rows = db.execute(sql + " ORDER BY ts DESC, id DESC LIMIT ?", (*args, limit)).fetchall()
        return [{**json.loads(r["entry"]), "node": r["node"]} for r in reversed(rows)]

    def last_incident(self, node: str, kind: str) -> Optional[dict]:
        with self._connect() as db:
            row = db.execute("SELECT entry FROM incidents WHERE node = ? AND type = ? ORDER BY ts DESC, id DESC LIMIT 1",
                             (node, kind)).fetchone()
        return json.loads(row["entry"]) if row else None

    def update_incident(self, node: str, ts: str, kind: str, **fields) -> None:
        with self.lock, self._connect() as db:
            row = db.execute("SELECT id, entry FROM incidents WHERE node = ? AND ts = ? AND type = ?", (node, ts, kind)).fetchone()
            if row:
                db.execute("UPDATE incidents SET entry = ? WHERE id = ?", (json.dumps({**json.loads(row["entry"]), **fields}), row["id"]))

    def take_incidents_before(self, cutoff_iso: str) -> Dict[str, List[dict]]:
        """Removes and returns entries older than the cutoff, per node (for archiving into totals)."""
        with self.lock, self._connect() as db:
            rows = db.execute("SELECT id, node, entry FROM incidents WHERE ts < ? ORDER BY ts", (cutoff_iso,)).fetchall()
            db.executemany("DELETE FROM incidents WHERE id = ?", [(r["id"],) for r in rows])
        out: Dict[str, List[dict]] = {}
        for r in rows:
            out.setdefault(r["node"], []).append(json.loads(r["entry"]))
        return out

    def mttr_by_category(self) -> Dict[str, Dict[str, Any]]:
        """Calculates Mean Time to Recovery (MTTR) grouped by incident category."""
        with self._connect() as db:
            rows = db.execute("SELECT node, type, category, entry, ts FROM incidents ORDER BY id").fetchall()
        outages: Dict[str, dict] = {}
        durations: Dict[str, List[float]] = {}
        for r in rows:
            node, kind, cat = r["node"], r["type"], r["category"] or "UNKNOWN"
            if kind == "OUTAGE":
                if json.loads(r["entry"] or "{}").get("class") in ("BLIP", "BACKUP_FAILURE"):
                    outages.pop(node, None)  # not an outage (outage_class.py): its recovery isn't counted either
                    continue
                outages[node] = {"ts": r["ts"], "category": cat}
            elif kind == "RECOVERY" and node in outages:
                prev = outages.pop(node)
                try:
                    t_out = datetime.fromisoformat(prev["ts"].replace("Z", "+00:00")).timestamp()
                    t_rec = datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).timestamp()
                    dur_s = max(1.0, t_rec - t_out)
                    durations.setdefault(prev["category"], []).append(dur_s)
                except Exception:
                    pass
        stats = {}
        for cat, durs in durations.items():
            if durs:
                stats[cat] = {
                    "count": len(durs),
                    "median_s": round(statistics.median(durs), 1),
                    "avg_s": round(sum(durs) / len(durs), 1),
                    "min_s": round(min(durs), 1),
                    "max_s": round(max(durs), 1)
                }
        return stats

    # --- reading ---

    def series(self, node: str, limit: int = WINDOW + RECENT) -> List[dict]:
        """The node's latest checks, oldest first."""
        with self._connect() as db:
            rows = db.execute("SELECT * FROM node_checks WHERE node = ? ORDER BY ts DESC LIMIT ?", (node, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    PASSED_CACHE_S = 300  # a 14-day percentage barely moves in 5 minutes; a new ignore tick still shows at once

    def checks_passed(self, counted: set, since_s: Optional[float] = None) -> Dict[str, dict]:
        """node -> {"checks", "failed"} over the kept history (RETENTION_DAYS), counting a check as failed only when a
        URL in `counted` failed in it: a stream the operator ignores, or one no longer on the server (a link moved
        away), doesn't lower its server's checks-passed, including for the failures it caused before. Cached a minute
        (a new ignore changes `counted`, so it shows at once)."""
        key = frozenset(counted)
        with self._passed_lock:
            cached = self._passed
            if cached and cached[1] == key and time.monotonic() - cached[0] < self.PASSED_CACHE_S:
                return cached[2]
            since = time.time() - (since_s if since_s is not None else RETENTION_DAYS * 86400)
            with self._connect() as db:
                rows = db.execute("""
                    SELECT node, SUM(n) AS checks, SUM(MIN(f, n)) AS failed FROM (
                        SELECT u.node AS node, r.ts, MAX(r.n) AS n,
                               MAX(CASE WHEN u.url IN (SELECT value FROM json_each(?)) THEN r.fails ELSE 0 END) AS f
                        FROM url_check_rows r JOIN urls u ON u.id = r.url_id WHERE r.ts >= ?
                        GROUP BY u.node, r.ts)
                    GROUP BY node""", (json.dumps(sorted(key)), since)).fetchall()
            result = {r["node"]: {"checks": r["checks"], "failed": r["failed"]} for r in rows}
            self._passed = (time.monotonic(), key, result)
            return result

    def history(self, node: str, since_s: float = 3600) -> List[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM node_checks WHERE node = ? AND ts >= ? ORDER BY ts",
                              (node, time.time() - since_s)).fetchall()
        return [dict(r) for r in rows]

    def timeline(self, node: str, since_s: float = 7 * 86400, buckets: int = 336) -> Dict[str, Any]:
        """The node's checks over `since_s`, summarised into `buckets` equal time buckets (oldest first, empty
        ones left out) for the history charts: per bucket the checks and failures, average HTTP latency, ICMP
        RTT, jitter and packet loss, and the average and worst newest-segment age (impossible ages, from
        broken source timestamps, left out)."""
        now = time.time()
        start = now - since_s
        with self._connect() as db:
            first = db.execute("SELECT MIN(ts) FROM node_checks WHERE node = ? AND ts >= ?", (node, start)).fetchone()[0]
        # Less history than asked for (a new server): the charts start where its data starts.
        clipped = first is not None and first - start > since_s * 0.05
        if clipped:
            start = first - 60
        size = max(60.0, (now - start) / buckets)
        with self._connect() as db:
            rows = db.execute(
                "SELECT CAST((ts - ?) / ? AS INTEGER) AS b, SUM(n) AS checks, SUM(fails) AS fails, "
                "AVG(latency_ms) AS latency, AVG(rtt_ms) AS rtt, AVG(jitter_ms) AS jitter, AVG(loss) AS loss, "
                "AVG(CASE WHEN segment_age_s BETWEEN -300 AND 3600 THEN segment_age_s END) AS age, "
                "MAX(CASE WHEN segment_age_s BETWEEN -300 AND 3600 THEN segment_age_s END) AS age_max "
                "FROM node_checks WHERE node = ? AND ts >= ? GROUP BY b ORDER BY b",
                (start, size, node, start)).fetchall()
        r2 = lambda v: None if v is None else round(v, 2)
        points = [{"t": round(start + (r["b"] + 0.5) * size), "checks": r["checks"], "fails": r["fails"],
                   "up": round(100 * (r["checks"] - r["fails"]) / r["checks"], 1), "latency": r2(r["latency"]),
                   "rtt": r2(r["rtt"]), "jitter": r2(r["jitter"]), "loss": r2(None if r["loss"] is None else 100 * r["loss"]),
                   "age": r2(r["age"]), "ageMax": r2(r["age_max"])} for r in rows]
        incidents = self._check_incidents(node, start)
        return {"node": node, "from": round(start), "to": round(now), "bucketS": round(size), "points": points,
                "clipped": clipped,
                "incidents": incidents, "segments": [a for a in self.segment_alerts(node) if a["baseline"]],
                "checks": sum(p["checks"] for p in points), "fails": sum(p["fails"] for p in points)}

    def _check_incidents(self, node: str, start: float, min_fails: int = 2) -> List[Dict[str, Any]]:
        """Outages and recoveries read straight from the saved checks (min_fails failed checks in a row is an
        outage, fewer is a blip, as in the spider's own debounce), so they show even after the incident log was
        archived into totals. Each: {"t", "type": OUTAGE | RECOVERY | BLIP, "category", "durationS"}."""
        with self._connect() as db:
            rows = db.execute("SELECT ts, up, category, fails FROM node_checks WHERE node = ? AND ts >= ? ORDER BY ts",
                              (node, start)).fetchall()  # an older row can be a 5-min rollup holding several failures
        out: List[Dict[str, Any]] = []
        run: List[sqlite3.Row] = []

        def close(end: Optional[float]) -> None:
            if not run:
                return
            category = next((r["category"] for r in run if r["category"]), None)
            fails = sum(max(r["fails"], 1) for r in run)
            if fails >= min_fails:
                out.append({"t": run[0]["ts"], "type": "OUTAGE", "category": category, "checks": fails})
                if end is not None:
                    out.append({"t": end, "type": "RECOVERY", "category": category,
                                "durationS": round(end - run[0]["ts"])})
            else:
                out.append({"t": run[0]["ts"], "type": "BLIP", "category": category, "checks": fails})
            run.clear()

        for r in rows:
            if r["up"]:
                close(r["ts"])
            else:
                run.append(r)
        close(None)  # still down at the end of the window
        return out

    def uptime(self, slots: int = 32, slot_s: int = 60, channel_finals: Optional[Dict[str, List[str]]] = None,
               now: Optional[float] = None) -> Dict[str, Any]:
        """The Uptime Matrix from the saved history: per node (and per channel, from its Final URLs: what
        viewers see) a list of `slots` time slots, oldest first, each None (no check) or {timestamp (ms),
        status, latencyMs, error, consecutiveFailures (failed checks in the slot), checks}."""
        now = now or time.time()
        start = now - slots * slot_s
        with self._connect() as db:
            node_rows = db.execute("SELECT ts, node, up, latency_ms, category FROM node_checks WHERE ts >= ?",
                                   (start,)).fetchall()
            url_rows = db.execute("SELECT ts, url, up, latency_ms, category, detail FROM url_checks WHERE ts >= ?",
                                  (start,)).fetchall() if channel_finals else []

        def bucketed(rows, key) -> Dict[str, List[Optional[dict]]]:
            acc: Dict[str, List[dict]] = {}
            for r in rows:
                i = min(slots - 1, max(0, int((r["ts"] - start) // slot_s)))
                slot = acc.setdefault(key(r), [{"n": 0, "down": 0, "lat": [], "err": None} for _ in range(slots)])[i]
                slot["n"] += 1
                if r["latency_ms"] is not None:
                    slot["lat"].append(r["latency_ms"])
                if not r["up"]:
                    slot["down"] += 1
                    slot["err"] = slot["err"] or r["category"] or (r["detail"] if "detail" in r.keys() else None) or "down"
            return {k: [None if not s["n"] else {
                "timestamp": int((start + (i + 0.5) * slot_s) * 1000), "status": "DOWN" if s["down"] else "UP",
                "latencyMs": round(sum(s["lat"]) / len(s["lat"])) if s["lat"] else None,
                "error": s["err"], "consecutiveFailures": s["down"], "checks": s["n"]} for i, s in enumerate(v)]
                for k, v in acc.items()}

        channels: Dict[str, List[Optional[dict]]] = {}
        if channel_finals:
            url_to_channel = {u: c for c, urls in channel_finals.items() for u in urls}
            channels = bucketed([r for r in url_rows if r["url"] in url_to_channel], lambda r: url_to_channel[r["url"]])
        return {"slots": slots, "slotSeconds": slot_s, "nodes": bucketed(node_rows, lambda r: r["node"]),
                "channels": channels}

    NODES_CACHE_S = 300

    def nodes(self) -> List[str]:
        """Every node with checks on record. Cached: it scans every check row, and servers rarely come and go."""
        cached = getattr(self, "_nodes_cache", None)
        if cached and time.monotonic() - cached[0] < self.NODES_CACHE_S:
            return list(cached[1])
        with self._connect() as db:
            found = [r[0] for r in db.execute("SELECT DISTINCT node FROM node_checks")]
        self._nodes_cache = (time.monotonic(), found)
        return list(found)

    def anomalies(self, node: str) -> Dict[str, Any]:
        """Per metric: the latest value, its normal (median), robust z-score and trend; plus an overall score
        (0-10: the largest z-score, 10 when the node is down)."""
        rows = self.series(node)
        if not rows:
            return {"node": node, "score": 0.0, "samples": 0, "metrics": {}, "down": False}
        latest, history = rows[-1], rows[:-1]
        result: Dict[str, Any] = {"node": node, "samples": len(rows), "metrics": {}, "down": not latest["up"]}
        worst = 0.0
        for metric in NODE_METRICS:
            baseline_values = [r[metric] for r in history[:-RECENT] if r[metric] is not None and r["up"]]
            base = robust(baseline_values)
            value = latest[metric]
            if base is None or value is None:
                continue
            z = zscore(value, base, FLOORS[metric])
            recent = [r[metric] for r in rows[-RECENT:] if r[metric] is not None]
            trend = None
            if len(recent) >= 3:
                recent_median = statistics.median(recent)
                rising = recent_median - base["median"]
                if rising > 3 * max(1.4826 * base["mad"], FLOORS[metric]):
                    trend = "rising"
                elif -rising > 3 * max(1.4826 * base["mad"], FLOORS[metric]):
                    trend = "falling"
            result["metrics"][metric] = {"value": round(value, 2), "normal": round(base["median"], 2),
                                         "z": round(z, 1), "trend": trend}
            if z > 0 and not math.isinf(z):  # only "worse than normal" counts (lower latency is good)
                worst = max(worst, z)

        # Multivariate Mahalanobis Distance across correlated network telemetry dimensions:
        # Avoids single-metric jitter false alarms by evaluating covariance dispersion
        z_vector = [max(0.0, m["z"]) for m in result["metrics"].values() if not math.isinf(m.get("z", 0))]
        d_mahalanobis = math.sqrt(sum(zi ** 2 for zi in z_vector) / max(len(z_vector), 1)) if z_vector else 0.0
        result["mahalanobis_d"] = round(d_mahalanobis, 2)

        # Segment age: each URL against its own learned normal (segment_alerts). A warning scores as far past
        # its line as it is (k at the line itself), like a z-score.
        segments = [a for a in self.segment_alerts(node) if a["baseline"]]
        result["segments"] = segments
        for a in segments:
            if a["active"] and a["age"] is not None and not result["down"]:
                over = (a["age"] - a["baseline"]["median"]) / max(a["warn"] - a["baseline"]["median"], 0.1)
                worst = max(worst, a["k"] * over)
        result["score"] = 10.0 if result["down"] else round(min(worst, 10.0), 1)
        return result

    def all_anomalies(self) -> Dict[str, Dict[str, Any]]:
        return {n: self.anomalies(n) for n in self.nodes()}


def warnings(anomaly: Dict[str, Any]) -> List[str]:
    """Readable early warnings for a node that is still up."""
    if anomaly.get("down"):
        return []
    out = []
    names = {"latency_ms": "HTTP latency", "rtt_ms": "ICMP RTT", "jitter_ms": "jitter",
             "segment_age_s": "segment age", "loss": "packet loss"}
    for metric, m in anomaly.get("metrics", {}).items():
        if m["z"] >= Z_WARN:
            out.append(f"{names[metric]} {m['value']} is {m['z']}σ above its normal {m['normal']}")
        elif m.get("trend") == "rising" and m.get("z", 0) >= 2.5:
            # Avoid flagging normal benign broadband/ping fluctuations as early warnings
            if metric == "rtt_ms" and m.get("value", 0) < 120.0:
                continue
            if metric == "latency_ms" and m.get("value", 0) < 350.0:
                continue
            out.append(f"{names[metric]} is trending up ({m['value']} vs normal {m['normal']})")
    for a in anomaly.get("segments", []):
        if a["active"] and a["age"] is not None:
            out.append(f"segment age {round(a['age'])} s is above its usual {a['baseline']['median']} s "
                       f"(warns above {a['warn']} s) on {a['url']}")
    return out


_stores: Dict[str, "Metrics"] = {}
_stores_lock = threading.Lock()


def store(path: Optional[str] = None) -> Metrics:
    """The process-wide store for METRICS_DB (one per file)."""
    path = str(path or os.environ.get("METRICS_DB") or Path(__file__).parent / "metrics.db")
    with _stores_lock:
        if path not in _stores:
            _stores[path] = Metrics(path)
        return _stores[path]
