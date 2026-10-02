"""SCTE-35 ad-break markers in the HLS playlists: detected, kept for 7 days, checked, and learned.

The glitch probe (glitch.py) reads every channel's Final playlist once a minute and hands the segments here; each
channel's Main input playlist is scanned too (playlist only), so markers lost between Main and Final show up.

Markers understood (whichever the packager writes):
  #EXT-X-CUE-OUT:<s>  …  #EXT-X-CUE-OUT-CONT  …  #EXT-X-CUE-IN        (break starts / continues / ends)
  #EXT-X-DATERANGE:…SCTE35-OUT=…,PLANNED-DURATION=<s>   …SCTE35-IN=…  (the standard form)
  #EXT-OATCLS-SCTE35:<base64>                                          (read alongside CUE-OUT, OATCLS style)

Each marker is stored once (keyed by the segment's media sequence number, so a marker seen on several probes isn't
counted twice). OUT and IN pair up into breaks: start, end, planned and actual length, and a status:
  OPEN      in the break right now
  CLOSED    ended normally
  OVERRUN   ran more than 20% (and 10 s) over its planned length
  STUCK     still no CUE-IN long after it should have ended (the channel may be stuck in an ad)
A CUE-IN missed between two playlist reads (short windows, a failed read) doesn't leave a break "stuck": once the
playlist shows normal segments after it, it's closed with an estimated end (see _close_from_segments).

Learned from the last 7 days, per channel: breaks per hour and the hours they cluster in, the minutes of the
hour they usually start at, typical length and gap, when the next one is expected, and warnings when the pattern
breaks (no breaks for far longer than usual in hours that normally have them; Main has markers the Final lacks).
"""

import sqlite3
import statistics
import threading
import time
from collections import Counter
from datetime import datetime
from typing import Dict, List, Optional

RETENTION_DAYS = 7
STUCK_GRACE_S = 120  # past the planned end with no CUE-IN for this long: stuck
UNPLANNED_MAX_S = 600  # a break with no planned length is stuck after this long
OVERRUN_SHARE, OVERRUN_MIN_S = 1.2, 10
SAME_BREAK_S = 20  # two break starts this close are the same break seen through different tags
IST = 19800  # seconds east of UTC

SCHEMA = """
CREATE TABLE IF NOT EXISTS scte_markers (
    ts REAL NOT NULL, url TEXT NOT NULL, node TEXT NOT NULL, channel TEXT, role TEXT, kind TEXT NOT NULL,
    seq INTEGER NOT NULL, at REAL NOT NULL, planned_s REAL, break_id TEXT, style TEXT,
    UNIQUE (url, kind, seq));
CREATE INDEX IF NOT EXISTS scte_markers_url_at ON scte_markers (url, at);
CREATE TABLE IF NOT EXISTS scte_breaks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL, node TEXT NOT NULL, channel TEXT, role TEXT,
    start REAL NOT NULL, end REAL, planned_s REAL, actual_s REAL, status TEXT NOT NULL, style TEXT,
    last_cue_at REAL, UNIQUE (url, start));
CREATE INDEX IF NOT EXISTS scte_breaks_url_start ON scte_breaks (url, start);
"""


def _ts(value) -> Optional[float]:
    if value is None:
        return None
    if hasattr(value, "timestamp"):
        return value.timestamp()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _num(value) -> Optional[float]:
    """A duration from a tag; 0 or garbage means "not given" (some packagers write CUE-OUT:0)."""
    try:
        v = float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
    return v if v and v > 0 else None


def extract_markers(media, now: Optional[float] = None) -> List[dict]:
    """Every SCTE-35 marker in a parsed media playlist: {"kind": OUT | IN, "seq", "at", "planned_s", "break_id",
    "style", "elapsed_s"}. "at" is the segment's PROGRAM-DATE-TIME, or estimated back from the live edge."""
    now = now or time.time()
    segs = list(media.segments)
    first = int(media.media_sequence or 0)
    # Without timestamps, place each segment by its distance from the newest one (which ends about now).
    edge, ends = now, []
    for seg in reversed(segs):
        ends.append(edge)
        edge -= float(seg.duration or 0)
    ends.reverse()
    out = []
    for i, seg in enumerate(segs):
        seq = first + i
        at = _ts(seg.current_program_date_time) or (ends[i] - float(seg.duration or 0))

        def add(kind, style, planned=None, break_id=None, elapsed=None, when=None):
            out.append({"kind": kind, "seq": seq, "at": when or at, "planned_s": planned, "break_id": break_id,
                        "style": style, "elapsed_s": elapsed})
        for d in seg.dateranges or []:
            when = _ts(getattr(d, "start_date", None)) or at
            planned = _num(getattr(d, "planned_duration", None)) or _num(getattr(d, "duration", None))
            if getattr(d, "scte35_out", None):
                add("OUT", "DATERANGE", planned, getattr(d, "id", None), when=when)
            if getattr(d, "scte35_in", None):
                add("IN", "DATERANGE", None, getattr(d, "id", None), when=when)
        if seg.cue_out_start:
            add("OUT", "OATCLS" if seg.oatcls_scte35 else "CUE", _num(seg.scte35_duration))
        # (An #EXT-OATCLS-SCTE35 tag is only read alongside CUE-OUT: the m3u8 library carries its value forward
        # onto later segments, so on its own it would turn every following segment into a "new break".)
        if seg.cue_in:
            add("IN", "CUE")
        if seg.cue_out and not seg.cue_out_start and i == 0:
            # Joined in the middle of a break (CUE-OUT-CONT on the first segment we can see): its start is back
            # by the elapsed time.
            elapsed = _num(seg.scte35_elapsedtime)
            if elapsed is not None:
                add("OUT", "CUE-CONT", _num(seg.scte35_duration), elapsed=elapsed, when=at - elapsed)
    return out


def in_break_segments(media) -> set:
    """Indexes of segments at a break's edge or inside one: discontinuities there are expected, not glitches."""
    idx = set()
    for i, seg in enumerate(media.segments):
        if seg.cue_out or seg.cue_out_start or seg.cue_in or any(
                getattr(d, "scte35_out", None) or getattr(d, "scte35_in", None) for d in (seg.dateranges or [])):
            idx.update({i, i + 1})
    return idx


class ScteStore:
    def __init__(self, path: str):
        self.path = str(path)
        self.lock = threading.Lock()
        with self._connect() as db:
            db.executescript(SCHEMA)
            if "last_cue_at" not in {r["name"] for r in db.execute("PRAGMA table_info(scte_breaks)")}:
                db.execute("ALTER TABLE scte_breaks ADD COLUMN last_cue_at REAL")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def record(self, url: str, node: str, channel: Optional[str], role: str, markers: List[dict],
               now: Optional[float] = None, segments: Optional[List[dict]] = None) -> List[dict]:
        """Stores new markers, pairs them into breaks, marks stuck ones. Returns the breaks that changed.
        segments (segment_states) lets a break whose CUE-IN was missed be closed from what the playlist shows now."""
        now = now or time.time()
        changed = []
        with self.lock, self._connect() as db:
            for m in sorted(markers, key=lambda m: (m["at"], m["kind"] == "OUT")):  # back to back: close, then open
                cur = db.execute("INSERT OR IGNORE INTO scte_markers (ts, url, node, channel, role, kind, seq, at, "
                                 "planned_s, break_id, style) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                 (now, url, node, channel, role, m["kind"], m["seq"], m["at"], m["planned_s"],
                                  m["break_id"], m["style"]))
                if not cur.rowcount:
                    continue  # seen on an earlier probe
                open_ = db.execute("SELECT * FROM scte_breaks WHERE url = ? AND status IN ('OPEN', 'STUCK') "
                                   "ORDER BY start DESC LIMIT 1", (url,)).fetchone()
                if m["kind"] == "OUT":
                    same = db.execute("SELECT id FROM scte_breaks WHERE url = ? AND ABS(start - ?) < ?",
                                      (url, m["at"], SAME_BREAK_S)).fetchone()
                    if same:
                        continue  # the same break seen through another tag (CUE-OUT vs CUE-OUT-CONT estimate)
                    if open_ and m["at"] > open_["start"]:
                        # Back to back (an ad pod in consecutive slots, no CUE-IN between): the next one starting
                        # ends the previous one.
                        actual = m["at"] - open_["start"]
                        planned = open_["planned_s"]
                        status = "OVERRUN" if planned and actual > max(planned * OVERRUN_SHARE, planned + OVERRUN_MIN_S) else "CLOSED"
                        db.execute("UPDATE scte_breaks SET end = ?, actual_s = ?, status = ? WHERE id = ?",
                                   (m["at"], round(actual, 1), status, open_["id"]))
                        changed.append({"url": url, "event": "end", "at": m["at"], "actual_s": actual, "status": status})
                    db.execute("INSERT OR IGNORE INTO scte_breaks (url, node, channel, role, start, planned_s, status, "
                               "style) VALUES (?,?,?,?,?,?,'OPEN',?)",
                               (url, node, channel, role, m["at"], m["planned_s"], m["style"]))
                    changed.append({"url": url, "event": "start", "at": m["at"], "planned_s": m["planned_s"]})
                elif open_ and m["at"] >= open_["start"]:
                    actual = m["at"] - open_["start"]
                    planned = open_["planned_s"]
                    status = "OVERRUN" if planned and actual > max(planned * OVERRUN_SHARE, planned + OVERRUN_MIN_S) else "CLOSED"
                    db.execute("UPDATE scte_breaks SET end = ?, actual_s = ?, status = ? WHERE id = ?",
                               (m["at"], round(actual, 1), status, open_["id"]))
                    changed.append({"url": url, "event": "end", "at": m["at"], "actual_s": actual, "status": status})
            if segments:
                changed += self._close_from_segments(db, url, segments, now)
            for b in db.execute("SELECT * FROM scte_breaks WHERE url = ? AND status = 'OPEN'", (url,)).fetchall():
                limit = (b["planned_s"] + STUCK_GRACE_S) if b["planned_s"] else UNPLANNED_MAX_S
                if now - b["start"] > limit:
                    db.execute("UPDATE scte_breaks SET status = 'STUCK' WHERE id = ?", (b["id"],))
                    changed.append({"url": url, "event": "stuck", "at": b["start"]})
        return changed

    @staticmethod
    def _close_from_segments(db, url: str, segments: List[dict], now: float) -> List[dict]:
        """A safety net for a missed CUE-IN (a short playlist window, a failed read): an open break whose playlist
        now shows normal segments after its start is over. With the switch visible (an in-break segment followed
        by a normal one), its end is exact; otherwise it's estimated between the last time it was seen in the
        break and the first normal segment, and overrun isn't claimed (the true end is uncertain)."""
        changed = []
        for b in db.execute("SELECT * FROM scte_breaks WHERE url = ? AND status IN ('OPEN', 'STUCK')", (url,)).fetchall():
            after = [s for s in segments if s["at"] > b["start"] + 1]
            if not after:
                continue
            if b["style"] == "DATERANGE":  # no in-break flag on segments: over once segments past its planned end show
                if b["planned_s"] and any(s["at"] >= b["start"] + b["planned_s"] for s in after):
                    end = b["start"] + b["planned_s"]
                    db.execute("UPDATE scte_breaks SET end = ?, actual_s = ?, status = 'CLOSED', style = ? WHERE id = ?",
                               (end, b["planned_s"], "DATERANGE (end estimated)", b["id"]))
                    changed.append({"url": url, "event": "end", "at": end, "estimated": True})
                continue
            cue = [s["at"] for s in after if s["in_cue"]]
            normal = [s["at"] for s in after if not s["in_cue"]]
            if cue:
                db.execute("UPDATE scte_breaks SET last_cue_at = ? WHERE id = ?", (max(cue + [b["last_cue_at"] or 0]), b["id"]))
            if not normal or (cue and max(cue) > min(normal)):
                continue  # still in the break (or it went back into one)
            first_normal = min(normal)
            if cue:  # the switch back is in view: exact
                end, estimated = first_normal, False
            else:
                lower, planned_end = b["last_cue_at"] or b["start"], b["start"] + (b["planned_s"] or 0)
                end = planned_end if b["planned_s"] and lower <= planned_end <= first_normal else (lower + first_normal) / 2
                estimated = True
            actual = round(end - b["start"], 1)
            status = "CLOSED"
            if not estimated and b["planned_s"] and actual > max(b["planned_s"] * OVERRUN_SHARE, b["planned_s"] + OVERRUN_MIN_S):
                status = "OVERRUN"
            db.execute("UPDATE scte_breaks SET end = ?, actual_s = ?, status = ?, style = ? WHERE id = ?",
                       (end, actual, status, f"{b['style']} (end estimated)" if estimated else b["style"], b["id"]))
            changed.append({"url": url, "event": "end", "at": end, "estimated": estimated})
        return changed

    def prune(self, now: Optional[float] = None) -> None:
        cutoff = (now or time.time()) - RETENTION_DAYS * 86400
        with self.lock, self._connect() as db:
            db.execute("DELETE FROM scte_markers WHERE at < ?", (cutoff,))
            db.execute("DELETE FROM scte_breaks WHERE start < ? AND status != 'OPEN'", (cutoff,))

    def breaks(self, url: Optional[str] = None, node: Optional[str] = None, since_s: float = RETENTION_DAYS * 86400,
               limit: int = 2000, now: Optional[float] = None) -> List[dict]:
        sql, args = "SELECT * FROM scte_breaks WHERE start >= ?", [(now or time.time()) - since_s]
        if url:
            sql += " AND url = ?"; args.append(url)
        if node:
            sql += " AND node = ?"; args.append(node)
        with self._connect() as db:
            return [dict(r) for r in db.execute(sql + " ORDER BY start DESC LIMIT ?", (*args, limit))]

    # --- learning ---

    def pattern(self, url: str, now: Optional[float] = None) -> dict:
        """What the last 7 days of breaks on this stream look like, and whether now fits."""
        now = now or time.time()
        rows = sorted(self.breaks(url=url, now=now), key=lambda b: b["start"])
        done = [b for b in rows if b["status"] in ("CLOSED", "OVERRUN")]
        out: Dict = {"breaks7d": len(rows), "lines": [], "issues": [], "nextExpected": None, "open": None}
        current = next((b for b in reversed(rows) if b["status"] in ("OPEN", "STUCK")), None)
        if current:
            out["open"] = {"start": current["start"], "planned_s": current["planned_s"], "status": current["status"],
                           "elapsed_s": round(now - current["start"])}
            if current["status"] == "STUCK":
                out["issues"].append(f"In an ad break for {round((now - current['start']) / 60)} min with no CUE-IN "
                                     f"(planned {round(current['planned_s'] or 0)} s): the channel may be stuck in the ad")
        overruns = [b for b in done if b["status"] == "OVERRUN"]
        if len(overruns) >= 2:
            out["issues"].append(f"{len(overruns)} breaks ran over their planned length this week")
        if len(rows) < 3:
            out["lines"].append("Not enough ad breaks seen yet to learn a pattern." if rows else
                                "No SCTE-35 ad-break markers seen in the last 7 days.")
            return out

        starts = [b["start"] for b in rows]
        days = max(1.0, (now - starts[0]) / 86400)
        hours = Counter(int(((t + IST) % 86400) // 3600) for t in starts)
        busy = [h for h, n in hours.most_common(3)]
        out["lines"].append(f"About {len(rows) / days / 24:.1f} breaks per hour on average; busiest around "
                            + ", ".join(f"{h:02d}:00" for h in sorted(busy)) + " IST.")
        minutes = Counter(int(((t + IST) % 3600) // 60) for t in starts)
        clusters = []
        circular = lambda a, b: min(abs(a - b), 60 - abs(a - b))  # noqa: E731 - minutes wrap around the hour
        for m in sorted(range(60), key=lambda x: -minutes[x]):  # peak minutes first
            if not minutes[m]:
                break
            near = sum(minutes[(m + d) % 60] for d in range(-2, 3))  # breaks within ±2 min of this mark
            if near >= max(3, 0.25 * len(rows)) and all(circular(m, c) > 4 for c in clusters):
                clusters.append(m)
        clusters.sort()
        if clusters:
            out["lines"].append("Breaks usually start at " + ", ".join(f":{c:02d}" for c in clusters) + " past the hour.")
        durations = [b["actual_s"] for b in done if b["actual_s"]]
        planned = Counter(round(b["planned_s"]) for b in rows if b["planned_s"])
        if durations:
            out["lines"].append(f"Typical break: {round(statistics.median(durations))} s"
                                + (f" (most often planned as {planned.most_common(1)[0][0]} s)" if planned else "") + ".")
        gaps = [b - a for a, b in zip(starts, starts[1:]) if 0 < b - a < 6 * 3600]
        gap = statistics.median(gaps) if gaps else None
        if gap:
            out["lines"].append(f"Typical gap between breaks: {round(gap / 60)} min.")

        # When's the next one: the next usual minute mark in an hour that usually has breaks, else last + gap.
        if clusters:
            for ahead in range(0, 3 * 3600, 60):
                t = now + ahead
                h, m = int(((t + IST) % 86400) // 3600), int(((t + IST) % 3600) // 60)
                if m in clusters and hours.get(h, 0) and t > now + 30:
                    out["nextExpected"] = round(t - (t % 60)); break
        elif gap:
            out["nextExpected"] = round(starts[-1] + gap)
        if gap and len(rows) >= 5 and not current:
            silent = now - starts[-1]
            if silent > max(2 * gap, gap + 1200) and hours.get(int(((now + IST) % 86400) // 3600), 0):
                out["issues"].append(f"No ad breaks for {round(silent / 60)} min, usually every {round(gap / 60)} min "
                                     "at this hour: markers may be getting lost")
        return out


def segment_states(media, now: Optional[float] = None) -> List[dict]:
    """Each visible segment's time and whether it's inside a CUE-OUT break (for closing breaks whose CUE-IN was
    missed)."""
    now = now or time.time()
    segs = list(media.segments)
    first = int(media.media_sequence or 0)
    edge, out = now, []
    for i in range(len(segs) - 1, -1, -1):
        seg = segs[i]
        at = _ts(seg.current_program_date_time) or (edge - float(seg.duration or 0))
        edge -= float(seg.duration or 0)
        out.append({"seq": first + i, "at": at, "in_cue": bool(seg.cue_out or seg.cue_out_start)})
    return list(reversed(out))


def scan(client, url: str, now: Optional[float] = None) -> dict:
    """Markers and segment states in a stream's playlist (its top variant for a master), without any video."""
    import m3u8
    pl = m3u8.loads(client.get(url, timeout=8, follow_redirects=True).raise_for_status().text, uri=url)
    if pl.is_variant and pl.playlists:
        top = max(pl.playlists, key=lambda p: p.stream_info.bandwidth or 0)
        pl = m3u8.loads(client.get(top.absolute_uri, timeout=8, follow_redirects=True).raise_for_status().text,
                        uri=top.absolute_uri)
    return {"markers": extract_markers(pl, now), "segments": segment_states(pl, now)}


def scan_markers(client, url: str, now: Optional[float] = None) -> List[dict]:
    """Markers in a stream's playlist (its top variant for a master), without downloading any video."""
    import m3u8
    pl = m3u8.loads(client.get(url, timeout=8, follow_redirects=True).raise_for_status().text, uri=url)
    if pl.is_variant and pl.playlists:
        top = max(pl.playlists, key=lambda p: p.stream_info.bandwidth or 0)
        pl = m3u8.loads(client.get(top.absolute_uri, timeout=8, follow_redirects=True).raise_for_status().text,
                        uri=top.absolute_uri)
    return extract_markers(pl, now)


def summary(store: ScteStore, finals: List[dict], mains: Dict[str, List[dict]], now: Optional[float] = None) -> List[dict]:
    """Per channel: its Final's breaks and learned pattern, and whether its Main input carries markers the Final
    doesn't. finals: [{"node", "url", "channel"}]; mains: channel -> [{"node", "url"}]."""
    now = now or time.time()
    out = []
    for f in finals:
        p = store.pattern(f["url"], now)
        recent = store.breaks(url=f["url"], limit=10, now=now)
        day_final = len(store.breaks(url=f["url"], since_s=86400, now=now))
        main_day = sum(len(store.breaks(url=m["url"], since_s=86400, now=now)) for m in mains.get(f.get("channel"), []))
        if main_day >= 3 and day_final < 0.5 * main_day:
            p["issues"].append(f"The Main input had {main_day} ad breaks in the last 24 h but the Final only "
                               f"{day_final}: markers are being dropped between them")
        out.append({**f, **p, "recent": recent, "breaks24h": day_final, "mainBreaks24h": main_day})
    return sorted(out, key=lambda c: (-len(c["issues"]), -c["breaks7d"]))
