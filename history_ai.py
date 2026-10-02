"""AI on a server's full history: the facts are worked out here, in code (uptime, downtime, longest outage, worst
hours, trend, response time, segment age, glitches, what was learned about this server), and the model only puts
them into plain words: what happened, when it was worst, the likely cause, and what to do. It never sees the raw
points, so it can't misread them, and it is told to use only these numbers.
"""

import statistics
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterator, List, Optional

IST = timezone(timedelta(hours=5, minutes=30))
CACHE_S = 600  # the same server and range within 10 min: the same answer, no new model call
GENERATION = {"max_tokens": 650, "temperature": 0.2, "reasoning": {"enabled": False}}

CATEGORY_WORDS = {
    "STALE_MEDIA": "the video stopped updating", "PLAYLIST_MISSING": "the playlist was missing (404)",
    "UNREACHABLE": "the server couldn't be reached", "HTTP_ERROR": "the server returned errors",
    "FALLING_BEHIND": "the stream fell behind live", "NO_SEGMENTS": "no video pieces arrived",
    "UPSTREAM": "its input feed failed",
}

SYSTEM = (
    "You explain a video streaming server's monitoring history to a TV channel operator in plain, simple words (no "
    "jargon; if a technical word is needed, say what it means). Use ONLY the facts given: never invent numbers, "
    "times, causes or servers. Write short markdown with exactly these sections, each 1-3 short bullet points:\n"
    "**What happened**\n**When it was worst**\n**Likely cause**\n**What to do**\n"
    "Then one line starting with 'Trend:' saying if it is getting better, worse or steady. Times are IST. If the "
    "history is short or quiet, say so plainly instead of stretching. Under 180 words.")


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts, IST).strftime("%d %b %H:%M")


def _dur(seconds: Optional[float]) -> str:
    if seconds is None:
        return "?"
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    return f"{seconds / 3600:.1f} h"


def facts(tl: dict, learned: Optional[dict] = None) -> Dict[str, object]:
    """The numbers the model may use. tl: metrics.timeline() (+ glitches, adBreaks); learned: patterns and fixes."""
    learned = learned or {}
    pts = [p for p in tl.get("points") or [] if p.get("checks")]
    span_h = max(1, (tl.get("to", 0) - tl.get("from", 0)) / 3600)
    checks, fails = tl.get("checks") or 0, tl.get("fails") or 0
    incidents = tl.get("incidents") or []
    outages = [i for i in incidents if i["type"] == "OUTAGE"]
    recoveries = [i for i in incidents if i["type"] == "RECOVERY" and i.get("durationS") is not None]
    blips = [i for i in incidents if i["type"] == "BLIP"]
    out: Dict[str, object] = {
        "server": tl.get("node"), "history": f"{span_h:.0f} h" if span_h < 48 else f"{span_h / 24:.1f} days",
        "from": _when(tl["from"]) if tl.get("from") else None, "to": _when(tl["to"]) if tl.get("to") else None,
        "checks": checks, "uptime_pct": round(100 * (checks - fails) / checks, 1) if checks else None,
        "outages": len(outages), "short_blips": len(blips),
    }
    if recoveries:
        total = sum(r["durationS"] for r in recoveries)
        longest = max(recoveries, key=lambda r: r["durationS"])
        out.update(total_downtime=_dur(total), typical_outage=_dur(statistics.median(r["durationS"] for r in recoveries)),
                   longest_outage=f"{_dur(longest['durationS'])}, ended {_when(longest['t'])}")
    if outages:
        cats = Counter(CATEGORY_WORDS.get(o.get("category"), (o.get("category") or "down").lower().replace("_", " "))
                       for o in outages)
        out["outage_causes"] = ", ".join(f"{c} ({n}x)" for c, n in cats.most_common(3))
        out["last_outage"] = _when(outages[-1]["t"])
        hours = Counter(datetime.fromtimestamp(o["t"], IST).hour for o in outages)
        top = hours.most_common(3)
        if top and top[0][1] >= 2:
            out["worst_hours"] = ", ".join(f"{h:02d}:00-{(h + 1) % 24:02d}:00 ({n} outage{'s' if n > 1 else ''})"
                                           for h, n in top)
        days = Counter(datetime.fromtimestamp(o["t"], IST).strftime("%d %b") for o in outages)
        if len(days) > 1:
            d, n = days.most_common(1)[0]
            out["worst_day"] = f"{d} ({n} outages)"
        gaps = [b["t"] - a["t"] for a, b in zip(outages, outages[1:])]
        if len(gaps) >= 3:
            out["typical_gap_between_outages"] = _dur(statistics.median(gaps))
    if len(pts) >= 8:  # trend: the first half against the second half
        half = len(pts) // 2
        first = sum(p["fails"] for p in pts[:half]) / max(1, sum(p["checks"] for p in pts[:half]))
        second = sum(p["fails"] for p in pts[half:]) / max(1, sum(p["checks"] for p in pts[half:]))
        out["failure_rate_first_half_pct"] = round(100 * first, 1)
        out["failure_rate_second_half_pct"] = round(100 * second, 1)
        recent = pts[-max(4, len(pts) // 12):]
        out["failure_rate_latest_pct"] = round(100 * sum(p["fails"] for p in recent) / max(1, sum(p["checks"] for p in recent)), 1)
    lat = [p["latency"] for p in pts if p.get("latency") is not None]
    if lat:
        out["response_ms_median"] = round(statistics.median(lat))
        worst = max(pts, key=lambda p: p.get("latency") or 0)
        out["response_ms_worst"] = f"{round(worst['latency'])} at {_when(worst['t'])}"
    loss = [p["loss"] for p in pts if p.get("loss")]
    if loss:
        out["packet_loss_seen_in_buckets"] = len(loss)
    ages = [p["ageMax"] for p in pts if p.get("ageMax") is not None and p["ageMax"] >= 0]
    lines = tl.get("segments") or []
    if ages:
        out["segment_age_worst_s"] = round(max(ages), 1)
        warn = min((s["warn"] for s in lines if s.get("warn")), default=None)
        if warn:
            out["segment_age_warn_line_s"] = round(warn, 1)
            out["buckets_over_warn_line"] = sum(a > warn for a in ages)
    glitches = tl.get("glitches") or []
    if glitches:
        kinds = Counter(g.get("kind") for g in glitches)
        out["glitches"] = ", ".join(f"{k.lower().replace('_', ' ')} ({n})" for k, n in kinds.most_common(3))
    ads = tl.get("adBreaks") or []
    if ads:
        out["ad_breaks"] = f"{len(ads)} ({sum(a.get('status') in ('STUCK', 'OVERRUN') for a in ads)} stuck or overran)"
    if learned.get("patterns"):
        out["learned_patterns"] = learned["patterns"][:5]
    if learned.get("fixes"):
        out["fixes_recorded_by_operator"] = learned["fixes"][:3]
    if learned.get("root_causes"):
        out["root_causes_found_before"] = learned["root_causes"]
    if learned.get("status"):
        out["right_now"] = learned["status"]
    return out


def prompt(f: dict) -> List[dict]:
    lines = [f"- {k.replace('_', ' ')}: {', '.join(map(str, v)) if isinstance(v, list) else v}"
             for k, v in f.items() if v not in (None, "", [])]
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": "Facts about this server's history:\n" + "\n".join(lines)}]


class HistoryAI:
    def __init__(self, bot):
        self.bot = bot
        self.cache: Dict[tuple, dict] = {}

    def stream(self, node: str, since: int, tl: dict, learned: dict) -> Iterator[dict]:
        """facts, then the explanation token by token, then done; a cached answer comes back at once."""
        f = facts(tl, learned)
        key = (node, since)
        hit = self.cache.get(key)
        if hit and time.time() - hit["at"] < CACHE_S and hit["checks"] == f.get("checks"):
            yield {"type": "facts", "facts": f}
            yield {"type": "token", "text": hit["text"]}
            yield {"type": "done", "cached": True, **hit["done"]}
            return
        yield {"type": "facts", "facts": f}
        if not f.get("checks"):
            text = "There are no checks recorded for this server in this period yet, so there is nothing to explain."
            yield {"type": "token", "text": text}
            yield {"type": "done", "cached": False}
            return
        text, done = [], {}
        for event in self.bot.generate(prompt(f), GENERATION):
            if event["type"] == "token":
                text.append(event["text"])
            elif event["type"] == "done":
                done = {k: event.get(k) for k in ("model", "elapsed_ms", "tokens")}
                continue
            yield event
        if text:
            self.cache[key] = {"at": time.time(), "checks": f.get("checks"), "text": "".join(text), "done": done}
        yield {"type": "done", "cached": False, **done}
