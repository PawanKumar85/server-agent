"""Glitch detection on each channel's Final stream, and glitch prediction from the history it builds.

The health checks find outages (no playlist, stale video, stuck sequence). A viewer also notices smaller things
between outages: a skip, a freeze while the player buffers, a hiccup when the encoder restarts. Once a minute per
Final this probe looks for those, on its own (it doesn't touch the health checks):

  master playlist     QUALITY_DROPPED    a quality level that was there is gone
  top-quality variant CONTENT_GAP        time missing between two segments (viewers see a jump)
                      TIME_JUMP          segment timestamps going backwards
                      DISCONTINUITY      an encoder restart or source switch (usually a visible hiccup)
                      SEGMENT_LENGTH     a segment far shorter or longer than the target (stutter)
  lowest-quality      SEGMENT_MISSING    the newest segment is listed but can't be downloaded
  variant's newest    SLOW_DELIVERY      at the measured speed, a full-quality segment would take longer than
  segment, timed                         80% of its own length to arrive: full-quality viewers would buffer
                                         (the lowest variant keeps the probe's bandwidth small)

Only new segments are judged, each glitch kind at most once per probe (with a count). Every probe's measurements
are stored too (glitch_probes): the features a real model can be trained on once there are weeks of data.

Prediction (statistics, works from day one): each Final's normal glitch rate per hour (median and spread over 7
days), which upstream failures usually come before its glitches (lift over the base rate), the hour of day they
cluster in, and the delivery margin trend combine into a "glitch risk in the next 10 minutes" with its reasons.
"""

import bisect
import os
import sqlite3
import statistics
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from concurrent.futures import ThreadPoolExecutor

import httpx
import m3u8

import scte

PROBE_S = int(os.environ.get("GLITCH_PROBE_S", "60"))  # the timed segment download (delivery), per Final
PLAYLIST_S = int(os.environ.get("GLITCH_PLAYLIST_S", "15"))  # playlist reads: inside even a ~22 s playlist window,
# so no segment (glitch, CUE-IN) scrolls out of the playlist between two reads
TIMEOUT_S = 8
SLOW_RATIO = 0.8  # a full-quality segment taking longer than this share of its length to download: buffering
SLOW_STREAK = 2  # slow on this many probes in a row before it counts
SHARED_SLOW_SHARE = 0.5  # this share of all Finals slow in the same round: the monitor's own network is the cause
GAP_MIN_S = 1.0
RETENTION_DAYS = 14
BASELINE_DAYS = 7
LEAD_WINDOW_S = 600  # an upstream failure this long before a glitch counts as "came before it"

KIND_WORDS = {
    "CONTENT_GAP": "skipped content", "TIME_JUMP": "timestamps jumped back",
    "DISCONTINUITY": "stream restarted or switched", "SEGMENT_LENGTH": "uneven segment length",
    "SEGMENT_MISSING": "segment missing", "SLOW_DELIVERY": "too slow for full quality",
    "QUALITY_DROPPED": "a quality level disappeared",
    "AUDIO_MISSING": "audio missing (picture without sound)", "VIDEO_MISSING": "video missing (sound without picture)",
    "AUDIO_GAP": "audio cutting out", "AV_DESYNC": "audio and video out of sync",
}
# Inside the delivered segment (ts_inspect.py): audio and video are muxed together, so only the segment can tell.
AV_DESYNC_MS = 500  # healthy streams here are within ~100 ms
AUDIO_GAP_SHARE = 0.5  # audio covering less than half of the video's time in a segment
AV_STREAK = 2  # seen in this many delivered segments in a row before it counts (an ad splice can be odd once)

SCHEMA = """
CREATE TABLE IF NOT EXISTS glitches (
    ts REAL NOT NULL, node TEXT NOT NULL, url TEXT NOT NULL, channel TEXT, kind TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1, value REAL, detail TEXT);
CREATE INDEX IF NOT EXISTS glitches_url_ts ON glitches (url, ts);
CREATE INDEX IF NOT EXISTS glitches_ts ON glitches (ts);
CREATE TABLE IF NOT EXISTS glitch_probes (
    ts REAL NOT NULL, url TEXT NOT NULL, ok INTEGER NOT NULL, new_segments INTEGER, glitches INTEGER,
    ttfb_ms REAL, kbps REAL, top_kbps REAL, est_ratio REAL, target_s REAL);
CREATE INDEX IF NOT EXISTS glitch_probes_url_ts ON glitch_probes (url, ts);
"""


def _get(client: httpx.Client, url: str) -> httpx.Response:
    return client.get(url, timeout=TIMEOUT_S, follow_redirects=True)


class GlitchProbe:
    """Probes Final stream URLs and remembers, per URL, what it saw last time (to judge only new segments)."""

    def __init__(self, path: str, client: Optional[httpx.Client] = None, scte_store=None):
        self.path = str(path)
        self.client = client or httpx.Client(headers={"User-Agent": "StreamGraph-GlitchProbe/1"})
        self.scte = scte_store  # scte.ScteStore: ad-break markers seen in the same playlist read
        self.on_alert: Optional[Callable[..., Any]] = None  # alertlog: (kind, node, channel, detail, source, ts)
        self.state: Dict[str, dict] = {}
        self.lock = threading.Lock()
        with self._connect() as db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    # --- one probe ---

    def probe(self, url: str, node: str, channel: Optional[str] = None, now: Optional[float] = None,
              store: bool = True, deliver: bool = True) -> dict:
        """One look at a Final. store=False returns the result without saving it (the monitor first checks a
        whole round for a shared cause, then saves with store()). deliver=False reads the playlists only (no
        segment download): the frequent reads between two timed deliveries."""
        now = now or time.time()
        st = self.state.setdefault(url, {"seq": None, "variants": [], "first": True, "slow": 0})
        self._store_now = store
        found: Dict[str, dict] = {}
        features = {"ok": 0, "new_segments": 0, "ttfb_ms": None, "kbps": None, "top_kbps": None,
                    "est_ratio": None, "target_s": None}

        def glitch(kind: str, value: Optional[float] = None, detail: str = "") -> None:
            g = found.setdefault(kind, {"kind": kind, "count": 0, "value": value, "detail": detail})
            g["count"] += 1
            if value is not None and (g["value"] is None or abs(value) > abs(g["value"])):
                g["value"], g["detail"] = value, detail or g["detail"]

        try:
            master = m3u8.loads(_get(self.client, url).raise_for_status().text, uri=url)
        except Exception:
            return self._finish(url, node, channel, now, found, features)  # outages are the health checks' job
        if master.is_variant and master.playlists:
            variants = sorted(((p.stream_info.bandwidth or 0, p.absolute_uri) for p in master.playlists), key=lambda v: v[0])
            names = {u.rsplit("/", 2)[-2] if u.count("/") > 3 else u for _, u in variants}
            history = st["variants"]
            if len(history) >= 2 and history[-1] == history[-2]:  # a level that was there twice in a row is gone
                gone = sorted(set(history[-1]) - names)
                if gone:
                    glitch("QUALITY_DROPPED", len(gone), f"missing: {', '.join(gone)}")
            st["variants"] = (history + [sorted(names)])[-3:]
            (_, low_url), (top_bw, top_url) = variants[0], variants[-1]
        else:
            top_bw = 0
            low_url = top_url = url
        features["top_kbps"] = round(top_bw / 1000, 1) if top_bw else None

        try:
            media = master if top_url == url else m3u8.loads(_get(self.client, top_url).raise_for_status().text, uri=top_url)
        except Exception:
            return self._finish(url, node, channel, now, found, features)
        segs = list(media.segments)
        if not segs:
            return self._finish(url, node, channel, now, found, features)
        target = float(media.target_duration or 6)
        features.update(ok=1, target_s=target)
        first_seq = int(media.media_sequence or 0)
        last_seen = st["seq"]
        newest = first_seq + len(segs) - 1
        is_new = (lambda i: first_seq + i > last_seen) if last_seen is not None else (lambda i: False)
        if last_seen is not None and newest < last_seen - 5:  # sequence restarted (encoder restart)
            glitch("DISCONTINUITY", None, "media sequence restarted")
            is_new = lambda i: True  # noqa: E731
        features["new_segments"] = sum(1 for i in range(len(segs)) if is_new(i))
        # "Uneven" against this stream's usual segment length (some declare a loose target, e.g. 16 s for 8 s pieces).
        usual = statistics.median(float(x.duration or 0) for x in segs) or target
        # SCTE-35 ad-break markers in this same playlist (scte.py); a switch into or out of an ad is expected to
        # carry a discontinuity, so those aren't glitches.
        ad_edges = scte.in_break_segments(media)
        if self.scte is not None:
            try:
                changes = self.scte.record(url, node, channel, "FinalLink", scte.extract_markers(media, now), now,
                                           segments=scte.segment_states(media, now))
                self.ad_alerts(changes, node, channel)
            except Exception as e:  # markers are extra: never lose the glitch check over them
                print(f"[scte] {url}: {type(e).__name__}: {e}")
        for i, seg in enumerate(segs):
            if not is_new(i):
                continue
            if seg.discontinuity and i not in ad_edges:
                glitch("DISCONTINUITY", None, "discontinuity tag in the playlist")
            dur = float(seg.duration or 0)
            if dur and (dur < 0.5 * usual or dur > max(1.5 * usual, target * 1.05 + 0.5)):
                glitch("SEGMENT_LENGTH", round(dur, 2), f"{dur:.1f} s segment, usually {usual:.1f} s")
            if i and seg.current_program_date_time and segs[i - 1].current_program_date_time:
                expected = segs[i - 1].current_program_date_time.timestamp() + float(segs[i - 1].duration or 0)
                delta = seg.current_program_date_time.timestamp() - expected
                tol = max(GAP_MIN_S, 0.25 * target)
                if delta > tol:
                    glitch("CONTENT_GAP", round(delta, 1), f"{delta:.1f} s of content missing")
                elif delta < -tol and not seg.discontinuity:
                    glitch("TIME_JUMP", round(delta, 1), f"timestamps jumped back {-delta:.1f} s")
        st["seq"] = newest

        # Delivery: time the newest segment of the lowest variant, then estimate a full-quality one.
        if not deliver:
            return self._finish(url, node, channel, now, found, features, delivered=False)
        try:
            low = media if low_url == top_url else m3u8.loads(_get(self.client, low_url).raise_for_status().text, uri=low_url)
            seg = low.segments[-1]
            t0 = time.monotonic()
            with self.client.stream("GET", seg.absolute_uri, timeout=TIMEOUT_S, follow_redirects=True) as resp:
                t1 = time.monotonic()
                if resp.status_code in (404, 410):
                    glitch("SEGMENT_MISSING", resp.status_code, f"newest segment HTTP {resp.status_code}")
                else:
                    body = b"".join(resp.iter_bytes())
                    size = len(body)
                    t2 = time.monotonic()
                    self._check_av(body, st, glitch, features)
                    ttfb, transfer = t1 - t0, max(t2 - t1, 1e-3)
                    rate = size / transfer  # bytes per second once data flows
                    features["ttfb_ms"] = round(ttfb * 1000)
                    features["kbps"] = round(rate * 8 / 1000)
                    dur = float(seg.duration or target)
                    top_bytes = (top_bw or (size * 8 / dur)) / 8 * dur
                    estimate = ttfb + (top_bytes / rate if transfer > 0.05 else 0)
                    features["est_ratio"] = round(estimate / dur, 3)
                    st["slow"] = st.get("slow", 0) + 1 if estimate > SLOW_RATIO * dur else 0
                    if st["slow"] >= SLOW_STREAK:  # one slow download can be a hiccup of the monitor's own link
                        glitch("SLOW_DELIVERY", round(estimate / dur, 2),
                               f"a full-quality {dur:.0f} s segment would take ~{estimate:.1f} s to arrive")
        except Exception:
            pass
        if st.pop("first", False):
            found.pop("QUALITY_DROPPED", None)  # nothing to compare with on the very first probe
        return self._finish(url, node, channel, now, found, features)

    @staticmethod
    def _check_av(body: bytes, st: dict, glitch, features: dict) -> None:
        """Audio and video inside the segment: present, covering the segment, in sync. Each problem must repeat
        AV_STREAK times before it is a glitch; "missing" only for a track this stream had before."""
        try:
            from ts_inspect import inspect
            av = inspect(body)
        except Exception:
            return
        if not av["ts"]:
            return  # fMP4 or something else: not inspected
        features["audio_s"], features["video_s"], features["av_drift_ms"] = av["audio_s"], av["video_s"], av["drift_ms"]
        st["had_audio"] = st.get("had_audio", False) or av["audio"]
        st["had_video"] = st.get("had_video", False) or av["video"]
        problems = {}
        if av["video"] and not av["audio"] and st["had_audio"]:
            problems["AUDIO_MISSING"] = (None, "the newest segment has video but no audio track data")
        elif av["audio"] and not av["video"] and st["had_video"]:
            problems["VIDEO_MISSING"] = (None, "the newest segment has audio but no video")
        elif av["video"] and av["audio"]:
            if av["video_s"] > 1 and av["audio_s"] < AUDIO_GAP_SHARE * av["video_s"]:
                problems["AUDIO_GAP"] = (av["audio_s"], f"audio covers {av['audio_s']:.1f} s of a {av['video_s']:.1f} s segment")
            if av["drift_ms"] is not None and abs(av["drift_ms"]) > AV_DESYNC_MS:
                problems["AV_DESYNC"] = (av["drift_ms"], f"sound is {abs(av['drift_ms']) / 1000:.1f} s "
                                                         f"{'behind' if av['drift_ms'] > 0 else 'ahead of'} the picture")
        streaks = st.setdefault("av_streak", {})
        for kind in ("AUDIO_MISSING", "VIDEO_MISSING", "AUDIO_GAP", "AV_DESYNC"):
            streaks[kind] = streaks.get(kind, 0) + 1 if kind in problems else 0
            if streaks[kind] >= AV_STREAK:
                glitch(kind, *problems[kind])

    def _finish(self, url, node, channel, now, found, features, delivered: bool = True) -> dict:
        events = [{"ts": now, "node": node, "url": url, "channel": channel, **g} for g in found.values()]
        result = {"url": url, "node": node, "channel": channel, "ts": now, "glitches": events, "features": features,
                  "delivered": delivered}
        if getattr(self, "_store_now", True):
            self.store(result)
        return result

    def ad_alerts(self, changes: List[dict], node: str, channel: Optional[str]) -> None:
        for c in changes or []:
            if self.on_alert and c.get("event") == "stuck":
                self.on_alert("AD_STUCK", node, channel, "no CUE-IN long after the planned end", "scte", None)
            elif self.on_alert and c.get("status") == "OVERRUN":
                self.on_alert("AD_OVERRUN", node, channel, f"ran {round(c.get('actual_s') or 0)} s", "scte", None)

    def store(self, result: dict) -> None:
        events, features, url, now = result["glitches"], result["features"], result["url"], result["ts"]
        if self.on_alert:
            for g in events:
                self.on_alert("GLITCH", g["node"], g.get("channel"), f"{KIND_WORDS.get(g['kind'], g['kind'])}"
                              + (f": {g['detail']}" if g.get("detail") else ""), "glitch", g["ts"])
        with self.lock, self._connect() as db:
            db.executemany("INSERT INTO glitches (ts, node, url, channel, kind, count, value, detail) "
                           "VALUES (:ts, :node, :url, :channel, :kind, :count, :value, :detail)", events)
            if not result.get("delivered", True):
                return  # a playlist-only read: the probe row (the model's features) comes with the timed delivery
            db.execute("INSERT INTO glitch_probes (ts, url, ok, new_segments, glitches, ttfb_ms, kbps, top_kbps, "
                       "est_ratio, target_s) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (now, url, features["ok"], features["new_segments"], sum(g["count"] for g in events),
                        features["ttfb_ms"], features["kbps"], features["top_kbps"], features["est_ratio"],
                        features["target_s"]))

    def prune(self, now: Optional[float] = None) -> None:
        cutoff = (now or time.time()) - RETENTION_DAYS * 86400
        with self.lock, self._connect() as db:
            db.execute("DELETE FROM glitches WHERE ts < ?", (cutoff,))
            db.execute("DELETE FROM glitch_probes WHERE ts < ?", (cutoff,))

    # --- reading ---

    def events(self, node: Optional[str] = None, since_s: float = 86400, limit: int = 500) -> List[dict]:
        sql, args = "SELECT * FROM glitches WHERE ts >= ?", [time.time() - since_s]
        if node:
            sql += " AND node = ?"; args.append(node)
        with self._connect() as db:
            rows = db.execute(sql + " ORDER BY ts DESC LIMIT ?", (*args, limit)).fetchall()
        return [{**dict(r), "words": KIND_WORDS.get(r["kind"], r["kind"])} for r in rows]


# --- prediction ---

def _hourly_counts(db, url: str, start: float, now: float) -> Dict[int, int]:
    """Glitches per hour over the probed hours (hours without probes are unknown, not zero)."""
    probed = {int(r[0]) for r in db.execute("SELECT DISTINCT CAST(ts / 3600 AS INTEGER) FROM glitch_probes "
                                             "WHERE url = ? AND ts >= ? AND ts < ?", (url, start, now - 3600))}
    counts = {h: 0 for h in probed}
    for r in db.execute("SELECT CAST(ts / 3600 AS INTEGER) AS h, SUM(count) AS n FROM glitches WHERE url = ? "
                        "AND ts >= ? AND ts < ? GROUP BY h", (url, start, now - 3600)):
        if r["h"] in counts:
            counts[r["h"]] = r["n"]
    return counts


def lead_patterns(db, url: str, upstream: List[str], start: float) -> List[dict]:
    """Which upstream server's failed checks tend to come within LEAD_WINDOW_S before this Final's glitches:
    how often they did (hit rate) against how often they do before any probe at all (base rate)."""
    glitch_ts = [r[0] for r in db.execute("SELECT ts FROM glitches WHERE url = ? AND ts >= ?", (url, start))]
    probe_ts = [r[0] for r in db.execute("SELECT ts FROM glitch_probes WHERE url = ? AND ts >= ?", (url, start))]
    if len(glitch_ts) < 5 or not probe_ts:
        return []
    out = []
    for node in upstream:
        fails = sorted(r[0] for r in db.execute("SELECT ts FROM node_checks WHERE node = ? AND ts >= ? AND fails > 0",
                                                (node, start - LEAD_WINDOW_S)))
        if not fails:
            continue
        def preceded(t: float) -> bool:
            i = bisect.bisect_right(fails, t)
            return i > 0 and t - fails[i - 1] <= LEAD_WINDOW_S
        hit = sum(preceded(t) for t in glitch_ts) / len(glitch_ts)
        base = sum(preceded(t) for t in probe_ts) / len(probe_ts)
        if hit >= 0.4 and hit >= 2 * max(base, 0.01):
            out.append({"node": node, "hit": round(hit, 2), "base": round(base, 3),
                        "lift": round(hit / max(base, 0.01), 1), "glitches": len(glitch_ts)})
    return sorted(out, key=lambda p: -p["lift"])


def forecast(path: str, finals: List[dict], upstream_of: Callable[[str], List[str]],
             failing_now: Callable[[str], bool], now: Optional[float] = None, network_slow: bool = False,
             model_predict: Optional[Callable[[dict, List[str]], Optional[dict]]] = None) -> List[dict]:
    """Per Final URL: glitches in the last hour against its learned normal, and the risk of glitches in the next
    10 minutes (0-100, band, reasons). finals: [{"node", "url", "channel"}]."""
    now = now or time.time()
    start = now - BASELINE_DAYS * 86400
    hour_now = int(((now + 19800) % 86400) // 3600)  # IST hour of the day
    out = []
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        for f in finals:
            url = f["url"]
            last_hour = db.execute("SELECT kind, SUM(count) AS n FROM glitches WHERE url = ? AND ts >= ? GROUP BY kind",
                                   (url, now - 3600)).fetchall()
            kinds = {r["kind"]: r["n"] for r in last_hour}
            n_hour = sum(kinds.values())
            last10 = db.execute("SELECT COALESCE(SUM(count), 0) FROM glitches WHERE url = ? AND ts >= ?",
                                (url, now - 600)).fetchone()[0]
            hourly = _hourly_counts(db, url, start, now)
            values = list(hourly.values())
            normal = statistics.median(values) if len(values) >= 6 else None
            spread = statistics.median(abs(v - normal) for v in values) * 1.4826 if normal is not None else None
            same_hour = [n for h, n in hourly.items() if int(((h * 3600 + 19800) % 86400) // 3600) == hour_now]
            daily_mean = statistics.mean(values) if values else 0
            ratios = [r[0] for r in db.execute("SELECT est_ratio FROM glitch_probes WHERE url = ? AND est_ratio IS NOT NULL "
                                               "ORDER BY ts DESC LIMIT 10", (url,))]
            probes = db.execute("SELECT COUNT(*) FROM glitch_probes WHERE url = ?", (url,)).fetchone()[0]
            ups = upstream_of(f["node"])
            leads = lead_patterns(db, url, ups, start)

            score, reasons = 0.0, []
            if normal is not None and n_hour >= normal + max(3, 4 * (spread or 0)):
                score += 35
                reasons.append(f"{n_hour} glitches in the last hour (usually about {round(normal)})")
            elif n_hour:
                score += 8 * min(n_hour, 3)
                reasons.append(f"{n_hour} glitch{'es' if n_hour != 1 else ''} in the last hour")
            if last10:
                score += 10
            if network_slow:
                pass  # delivery measurements say more about the monitor's own link right now than about the stream
            elif ratios and ratios[0] >= SLOW_RATIO:
                score += 25
                reasons.append("delivery is too slow for full quality right now")
            elif len(ratios) >= 5 and ratios[0] > 0.5 and ratios[0] > statistics.median(ratios) * 1.5:
                score += 10
                reasons.append("delivery is getting slower")
            failing = [u for u in ups if failing_now(u)]
            if failing:
                score += 20
                lead = next((p for p in leads if p["node"] in failing), None)
                if lead:
                    score += 15
                    reasons.append(f"{failing[0]} is failing, and its failures came before {round(lead['hit'] * 100)}% "
                                   f"of past glitches here")
                else:
                    reasons.append(f"upstream {failing[0]} is failing")
            if len(same_hour) >= 3 and daily_mean > 0 and statistics.mean(same_hour) > 2 * daily_mean:
                score += 10
                reasons.append(f"glitches are usually higher around {hour_now:02d}:00 IST")
            learned = model_predict(f, ups) if model_predict else None  # glitch_model.py, when it has earned it
            if learned:
                score = 0.5 * score + 50 * learned["probability"]
                reasons.append(f"learned model: {round(learned['probability'] * 100)}% chance in the next 10 min"
                               + (f" ({', '.join(learned['drivers'])})" if learned["drivers"] else ""))
            score = round(min(100, score))
            out.append({**f, "learned": learned, "lastHour": n_hour, "lastHourKinds": {KIND_WORDS.get(k, k): n for k, n in kinds.items()},
                        "normalPerHour": None if normal is None else round(normal, 1), "risk": score,
                        "band": "HIGH" if score >= 50 else "MEDIUM" if score >= 25 else "LOW",
                        "reasons": reasons, "leads": leads, "deliveryRatio": ratios[0] if ratios else None,
                        "probes": probes, "learnedHours": len(values)})
    finally:
        db.close()
    return sorted(out, key=lambda x: -x["risk"])


def model_readiness(path: str) -> dict:
    """Whether there's enough labelled history for a trained model (the statistics above work from day one)."""
    db = sqlite3.connect(path, timeout=10)
    try:
        span = db.execute("SELECT (MAX(ts) - MIN(ts)) / 86400 FROM glitch_probes").fetchone()[0] or 0
        events = db.execute("SELECT COUNT(*) FROM glitches").fetchone()[0]
        probes = db.execute("SELECT COUNT(*) FROM glitch_probes").fetchone()[0]
    finally:
        db.close()
    ready = span >= 14 and events >= 200
    return {"days": round(span, 1), "glitchEvents": events, "probes": probes, "ready": ready,
            "note": "Enough history to train a model." if ready else
                    f"A trained model needs about 14 days and 200 glitch events (now {span:.1f} days, {events} events); "
                    "until then the statistics above do the forecasting."}


def shared_slowness(results: List[dict]) -> Optional[dict]:
    """When most Finals look too slow in the same round, the common factor is the monitor's own network, not the
    streams: {"slow": n, "of": total} then, else None."""
    measured = [r for r in results if r["features"].get("est_ratio") is not None]
    slow = [r for r in measured if r["features"]["est_ratio"] > SLOW_RATIO]
    if len(measured) >= 3 and len(slow) / len(measured) >= SHARED_SLOW_SHARE:
        return {"slow": len(slow), "of": len(measured), "at": time.time()}
    return None


class GlitchMonitor:
    """Runs the probe for every Final once per PROBE_S in a background thread (while auto-check is on)."""

    def __init__(self, probe: GlitchProbe, finals: Callable[[], List[dict]], enabled: Callable[[], bool],
                 publish: Callable[[str, Any], None], mains: Optional[Callable[[], List[dict]]] = None,
                 retrain: Optional[Callable[[], Any]] = None):
        self.retrain = retrain  # glitch_model training, run hourly (it takes well under a second)
        self.probe, self.finals, self.enabled, self.publish = probe, finals, enabled, publish
        self.mains = mains  # Main input streams: scanned for ad-break markers only (playlist, no video)
        self.stop = threading.Event()
        self.last_run: Optional[float] = None
        self.network_slow: Optional[dict] = None  # set when most Finals looked slow at once (see shared_slowness)
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="glitch")
        self.thread = threading.Thread(target=self._loop, name="glitch-monitor", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _loop(self) -> None:
        runs, last_delivery = 0, 0.0
        while not self.stop.wait(PLAYLIST_S if self.last_run else 15):
            if not self.enabled():
                continue
            try:
                finals = self.finals()
                if time.time() - last_delivery >= PROBE_S - 1:
                    # Delivery round, one Final at a time: parallel segment downloads would share this machine's
                    # link and all look slow.
                    results = [self.probe.probe(f["url"], f["node"], f.get("channel"), store=False) for f in finals]
                    last_delivery = time.time()
                    was_slow, self.network_slow = self.network_slow, shared_slowness(results)
                    if self.network_slow and not was_slow and self.probe.on_alert:
                        self.probe.on_alert("NETWORK_SLOW", "monitor", None,
                                            f"{self.network_slow['slow']} of {self.network_slow['of']} channels slow at once",
                                            "glitch", None)
                    if self.network_slow:  # not the streams' fault: keep the measurements, drop the blame
                        for r in results:
                            r["glitches"] = [g for g in r["glitches"] if g["kind"] != "SLOW_DELIVERY"]
                    runs += 1
                else:
                    # Playlist-only round (no video downloaded): in parallel, it's just small text files.
                    results = list(self.pool.map(lambda f: self.probe.probe(f["url"], f["node"], f.get("channel"),
                                                                            store=False, deliver=False), finals))
                for r in results:
                    self.probe.store(r)
                self.last_run = time.time()
                new = [g for r in results for g in r["glitches"]]
                if new:
                    self.publish("glitch", {"events": new})
                if self.mains and self.probe.scte is not None:
                    def scan(m):
                        try:
                            found = scte.scan(self.probe.client, m["url"])
                            changes = self.probe.scte.record(m["url"], m["node"], m.get("channel"), "MainInput",
                                                             found["markers"], segments=found["segments"])
                            self.probe.ad_alerts(changes, m["node"], m.get("channel"))
                        except Exception:
                            pass  # an unreachable Main input is the health checks' business
                    list(self.pool.map(scan, self.mains()))
                if runs % 60 == 1 and results and results[0].get("delivered"):  # hourly (every 60th delivery round)
                    self.probe.prune()
                    if self.retrain:
                        try:
                            self.retrain()
                        except Exception as e:
                            print(f"[glitch] model training failed: {type(e).__name__}: {e}")
                    if self.probe.scte is not None:
                        self.probe.scte.prune()
            except Exception as e:  # never let the probe die
                print(f"[glitch] probe failed: {type(e).__name__}: {e}")
