"""Glitch detection on Final streams (playlist-level glitches, delivery speed) and the glitch forecast."""

import sqlite3
import time

import httpx

import glitch

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=4000000
high/index.m3u8
"""
MASTER_LOW_ONLY = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000
low/index.m3u8
"""


def media(first_seq, segments):
    """segments: [(duration, program_date_time or None, discontinuity)]"""
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:6", f"#EXT-X-MEDIA-SEQUENCE:{first_seq}"]
    for i, (dur, pdt, disc) in enumerate(segments):
        if disc:
            lines.append("#EXT-X-DISCONTINUITY")
        if pdt:
            lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{pdt}")
        lines += [f"#EXTINF:{dur},", f"seg{first_seq + i}.ts"]
    return "\n".join(lines) + "\n"


def pdt(sec):
    return f"2026-10-02T10:00:{sec:06.3f}Z"


class World:
    def __init__(self):
        self.master = MASTER
        self.high = media(100, [(6, pdt(0), False), (6, pdt(6), False), (6, pdt(12), False)])
        self.low = self.high
        self.segment_status = 200
        self.segment_bytes = 600_000

    def handler(self, request):
        path = request.url.path
        if path.endswith(".ts"):
            return httpx.Response(self.segment_status, content=b"x" * self.segment_bytes)
        if path.endswith("/index.m3u8") and "/high/" not in path and "/low/" not in path:
            return httpx.Response(200, text=self.master)
        if "/high/" in path:
            return httpx.Response(200, text=self.high)
        if "/low/" in path:
            return httpx.Response(200, text=self.low)
        return httpx.Response(404)


URL = "https://final.example.test/ch/index.m3u8"


def probe_for(tmp_path, world):
    return glitch.GlitchProbe(tmp_path / "m.db", client=httpx.Client(transport=httpx.MockTransport(world.handler)))


def kinds(result):
    return {g["kind"]: g for g in result["glitches"]}


def test_first_probe_only_learns_then_playlist_glitches_are_found_in_new_segments(tmp_path):
    w = World()
    p = probe_for(tmp_path, w)
    first = p.probe(URL, "final", "ch")
    assert first["glitches"] == [] and first["features"]["ok"] == 1 and first["features"]["new_segments"] == 0

    # Next playlist: a 4 s hole before seg 103, a discontinuity at 104, and a 1.5 s runt at 105.
    w.high = media(101, [(6, pdt(6), False), (6, pdt(12), False), (6, pdt(22), False),
                         (6, pdt(28), True), (1.5, pdt(34), False)])
    found = kinds(p.probe(URL, "final", "ch"))
    assert set(found) == {"CONTENT_GAP", "DISCONTINUITY", "SEGMENT_LENGTH"}
    assert found["CONTENT_GAP"]["value"] == 4.0 and "4.0 s of content missing" in found["CONTENT_GAP"]["detail"]

    # Nothing new since then: nothing to report (old segments are never judged twice).
    assert p.probe(URL, "final", "ch")["glitches"] == []


def test_missing_segment_dropped_quality_and_slow_delivery(tmp_path):
    w = World()
    p = probe_for(tmp_path, w)
    p.probe(URL, "final", "ch")
    p.probe(URL, "final", "ch")  # the two-level ladder has now been seen twice
    w.master = MASTER_LOW_ONLY
    w.segment_status = 404
    found = kinds(p.probe(URL, "final", "ch"))
    assert found["QUALITY_DROPPED"]["detail"] == "missing: high" and "SEGMENT_MISSING" in found

    w.master, w.segment_status = MASTER, 200
    slow = glitch.GlitchProbe(tmp_path / "s.db", client=httpx.Client(transport=httpx.MockTransport(w.handler)))
    real_stream = slow.client.stream

    def sluggish(*args, **kwargs):  # the server takes 5.5 s to start answering
        cm = real_stream(*args, **kwargs)

        class Slow:
            def __enter__(self):
                time.sleep(0)  # (patched clock below does the waiting)
                return cm.__enter__()

            def __exit__(self, *exc):
                return cm.__exit__(*exc)
        return Slow()

    clock = iter([0.0, 5.5, 5.6] * 10)
    slow.client.stream = sluggish
    orig = glitch.time.monotonic
    glitch.time.monotonic = lambda: next(clock)
    try:
        once = slow.probe(URL, "final", "ch")
        twice = slow.probe(URL, "final", "ch")
    finally:
        glitch.time.monotonic = orig
    assert "SLOW_DELIVERY" not in kinds(once) and once["features"]["est_ratio"] > glitch.SLOW_RATIO  # one could be a hiccup
    assert "SLOW_DELIVERY" in kinds(twice)  # slow twice in a row counts


def test_most_finals_slow_at_once_is_the_monitors_network_not_the_streams():
    r = lambda ratio: {"features": {"est_ratio": ratio}, "glitches": []}
    assert glitch.shared_slowness([r(1.4), r(1.2), r(0.9), r(0.3)])["slow"] == 3
    assert glitch.shared_slowness([r(1.4), r(0.3), r(0.2), r(0.4)]) is None  # one slow stream is its own problem
    assert glitch.shared_slowness([r(1.4), r(1.5)]) is None  # too few to tell


def test_forecast_learns_the_normal_rate_and_the_upstream_that_comes_first(tmp_path):
    path = tmp_path / "m.db"
    p = glitch.GlitchProbe(path)
    with sqlite3.connect(path) as db:  # node_checks lives in the same file (metrics.py)
        db.execute("CREATE TABLE node_checks (ts REAL, node TEXT, up INTEGER, fails INTEGER)")
    now = time.time()
    with p._connect() as db:
        for h in range(48, 1, -1):  # two days of probes: about one glitch every 4 hours
            for m in range(0, 60, 10):
                db.execute("INSERT INTO glitch_probes (ts, url, ok) VALUES (?, ?, 1)", (now - h * 3600 + m * 60, URL))
            if h % 4 == 0:
                t = now - h * 3600 + 120
                db.execute("INSERT INTO glitches (ts, node, url, kind) VALUES (?, 'final', ?, 'CONTENT_GAP')", (t, URL))
                db.execute("INSERT INTO node_checks VALUES (?, 'xcode', 0, 1)", (t - 60,))  # xcode failed first
        for m in range(5):  # the last hour: a burst
            db.execute("INSERT INTO glitches (ts, node, url, kind, count) VALUES (?, 'final', ?, 'DISCONTINUITY', 1)",
                       (now - 600 + m * 60, URL))
    (f,) = glitch.forecast(str(path), [{"node": "final", "url": URL, "channel": "ch"}],
                           upstream_of=lambda n: ["xcode", "main"], failing_now=lambda n: n == "xcode", now=now)
    assert f["lastHour"] == 5 and f["normalPerHour"] == 0
    # xcode failed just before all 12 earlier glitches, not before the 5 in the burst: 12 of 17.
    assert f["leads"][0]["node"] == "xcode" and f["leads"][0]["hit"] == 0.71 and f["leads"][0]["lift"] > 2
    assert f["band"] == "HIGH" and any("came before 71% of past glitches" in r for r in f["reasons"])
    assert glitch.model_readiness(str(path))["ready"] is False
