"""Root-cause ranking (rca_rank) and the per-check time series with anomaly detection (metrics)."""

import time
from datetime import datetime, timezone

import pytest

import rca_rank
from health import NodeHealth, UrlCheck
from metrics import Metrics, robust, warnings

EDGES = [{"source": "main", "type": "FEEDS", "target": "trans"}, {"source": "backup", "type": "FEEDS", "target": "trans"},
         {"source": "trans", "type": "PRODUCES", "target": "final"}]


def state(up=True, onset=None, category=None):
    return {"up": up, "onsetAt": onset, "onsetPrecisionS": 6.0 if onset else None, "category": category}


def test_the_failing_node_whose_inputs_are_healthy_ranks_first():
    states = {"main": state(), "backup": state(), "trans": state(False), "final": state(False)}
    [group] = rca_rank.rank(states, EDGES, {})
    top = group["ranking"][0]
    assert top["node"] == "trans" and "its inputs are healthy" in top["reasons"]
    assert "1 failing node(s) downstream" in top["reasons"]
    assert sum(r["score"] for r in group["ranking"]) == pytest.approx(1.0, abs=0.01)


def test_the_node_that_stopped_first_wins_when_the_graph_alone_cannot_tell():
    # Both inputs fail (shared upstream); main's stream stopped 40 s before backup's.
    states = {"main": state(False, "2026-09-30T12:00:00+00:00"), "backup": state(False, "2026-09-30T12:00:40+00:00"),
              "trans": state(False, "2026-09-30T12:00:45+00:00"), "final": state(False, "2026-09-30T12:00:50+00:00")}
    [group] = rca_rank.rank(states, EDGES, {})
    ranking = {r["node"]: r for r in group["ranking"]}
    assert group["ranking"][0]["node"] == "main" and "stopped first" in ranking["main"]["reasons"]
    assert ranking["main"]["score"] > ranking["backup"]["score"] > ranking["final"]["score"]


def test_independent_outages_are_ranked_separately_and_anomalies_join_their_group():
    edges = EDGES + [{"source": "x", "type": "FEEDS", "target": "y"}]
    states = {"main": state(), "backup": state(), "trans": state(), "final": state(False), "x": state(False), "y": state()}
    groups = rca_rank.rank(states, edges, {"trans": {"score": 6.0}})
    assert [g["nodes"] for g in groups] == [["final", "trans"], ["x"]]
    first = groups[0]["ranking"]
    assert any("up but anomalous" in " ".join(r["reasons"]) for r in first if r["node"] == "trans")


def test_nothing_down_means_no_ranking_even_with_anomalies():
    assert rca_rank.rank({"a": state(), "b": state()}, [], {"a": {"score": 9.0}}) == []


def test_pagerank_mass_flows_to_the_upstream_cause():
    # symptom c -> b -> a (upstream), with the self-loops rank() adds so the upstream end keeps its mass
    pr = rca_rank.pagerank(["a", "b", "c"], [("c", "b", 1.0), ("b", "a", 1.0), ("a", "a", 0.5), ("b", "b", 0.5),
                                             ("c", "c", 0.5)], {"c": 1.0})
    assert pr["a"] > pr["c"] and sum(pr.values()) == pytest.approx(1.0)


# --- metrics ---

def health(up=True, latency=100.0, age=3.0, rtt=30.0):
    url = UrlCheck(url="https://a.example/x.m3u8", up=up, detail="ok" if up else "STALE_SEGMENTS (40s old)",
                   category=None if up else "STALE_MEDIA", segment_age_s=age, freshness="FRESH" if up else "STALE",
                   latency_ms=int(latency))
    return NodeHealth(node_id="a", check_type="HLS", up=up, latency_ms=latency, rtt_ms=rtt, jitter_ms=2.0,
                      packet_loss=0.0, urls=[url])


def test_metrics_store_and_detect_a_latency_anomaly(tmp_path):
    m = Metrics(tmp_path / "metrics.db")
    t0 = time.time() - 3600
    for i in range(40):  # normal: 100 +- 5 ms
        m.record("a", health(latency=100 + (i % 5) - 2), t0 + i * 30)
    normal = m.anomalies("a")
    assert normal["samples"] == 40 and normal["score"] < 2 and warnings(normal) == []
    for i in range(3):
        m.record("a", health(latency=600), t0 + (40 + i) * 30)
    spiked = m.anomalies("a")
    lat = spiked["metrics"]["latency_ms"]
    assert lat["value"] == 600 and lat["normal"] == pytest.approx(100, abs=3) and lat["z"] >= 10
    assert spiked["score"] == 10.0 and any("HTTP latency 600" in w for w in warnings(spiked))
    m.record("a", health(up=False), t0 + 44 * 30)
    assert m.anomalies("a")["score"] == 10.0 and m.anomalies("a")["down"] is True
    assert len(m.history("a", since_s=7200)) == 44


def test_robust_baseline_ignores_single_spikes_and_needs_enough_samples():
    assert robust([1.0] * 5) is None
    base = robust([10.0] * 30 + [1000.0])
    assert base["median"] == 10.0 and base["mad"] == 0.0


def test_unknown_node_has_no_anomaly(tmp_path):
    assert Metrics(tmp_path / "m.db").anomalies("nope") == {"node": "nope", "score": 0.0, "samples": 0, "metrics": {}, "down": False}


def test_uptime_slots_from_saved_history(tmp_path):
    m = Metrics(tmp_path / "u.db")
    now = time.time()
    m.record("a", health(latency=100), now - 150)   # 4 slots of 60 s cover the last 240 s
    m.record("a", health(up=False), now - 90)       # a failure 90 s ago
    m.record("a", health(latency=300), now - 10)
    data = m.uptime(slots=4, slot_s=60, channel_finals={"ch": ["https://a.example/x.m3u8"]}, now=now)
    slots = data["nodes"]["a"]
    assert len(slots) == 4 and slots[0] is None  # nothing 181-240 s ago
    assert slots[1]["status"] == "UP" and slots[1]["latencyMs"] == 100
    assert slots[2]["status"] == "DOWN" and slots[2]["error"] == "STALE_MEDIA" and slots[2]["consecutiveFailures"] == 1
    assert slots[3]["status"] == "UP" and slots[3]["latencyMs"] == 300
    assert [s and s["status"] for s in data["channels"]["ch"]] == [None, "UP", "DOWN", "UP"]  # from its Final URL


def test_uptime_endpoint(monkeypatch):
    import server
    from fastapi.testclient import TestClient
    from tests.conftest import TEST_LOGIN
    server.auth.attempts.clear()
    c = TestClient(server.app)
    assert c.post("/login", data={**TEST_LOGIN, "next": "/"}, follow_redirects=False).status_code == 303
    monkeypatch.setattr(server.metrics, "uptime", lambda slots, slot, finals: {
        "slots": slots, "slotSeconds": slot, "nodes": {"a.example": [None], "t.pytest.invalid": [None]}, "channels": {}})
    data = c.get("/api/uptime?slots=8&slot=30").json()
    assert data["slots"] == 8 and data["slotSeconds"] == 30 and list(data["nodes"]) == ["a.example"]
    assert c.get("/api/uptime?slots=1").status_code == 422


# --- segment age, learned per stream URL ---

def seg_health(age, up=True, url="https://a.example/x.m3u8"):
    u = UrlCheck(url=url, up=up, detail="ok" if up else "HTTP 404", category=None if up else "PLAYLIST_MISSING",
                 segment_age_s=age if up else None, target_duration_s=6.0)
    return NodeHealth(node_id="a", check_type="HLS", up=up, latency_ms=100.0, urls=[u])


def test_segment_age_alert_is_learned_per_stream_and_needs_three_high_checks(tmp_path):
    m = Metrics(tmp_path / "m.db")
    t0 = time.time() - 3600
    for i in range(40):  # this source's clock runs ~40 s late: 40 s is its normal, not a problem
        m.record("a", seg_health(40 + (i % 3)), t0 + i * 30)
    (a,) = m.segment_alerts("a")
    assert a["baseline"]["median"] == 41 and not a["active"] and warnings(m.anomalies("a")) == []
    assert a["warn"] == pytest.approx(41 + max(4 * a["baseline"]["spread"], 6.0), abs=0.1)
    t = t0 + 40 * 30
    for i in range(2):  # two high checks: not yet
        m.record("a", seg_health(90), t + i * 30)
    assert not m.segment_alerts("a")[0]["active"]
    m.record("a", seg_health(90), t + 60)  # the third in a row warns
    an = m.anomalies("a")
    assert an["segments"][0]["active"] and an["score"] >= 4
    assert any(w.startswith("segment age 90 s is above its usual 41") for w in warnings(an))
    m.record("a", seg_health(47), t + 90)  # between the lines: still warning
    assert m.segment_alerts("a")[0]["active"]
    m.record("a", seg_health(41), t + 120)  # back to normal by itself: cleared, and a bit less sensitive
    (a,) = m.segment_alerts("a")
    assert not a["active"] and a["clearedAlone"] == 1 and a["k"] == 4.25


def test_a_warning_before_an_outage_keeps_it_sensitive_and_too_sensitive_raises_the_line(tmp_path):
    m = Metrics(tmp_path / "m.db")
    t0 = time.time() - 3600
    for i in range(40):
        m.record("a", seg_health(3 + (i % 2)), t0 + i * 30)
    t = t0 + 40 * 30
    for i in range(3):
        m.record("a", seg_health(30), t + i * 30)
    m.record("a", seg_health(None, up=False), t + 90)  # the stream then failed: the warning was right
    (a,) = m.segment_alerts("a")
    assert a["beforeOutage"] == 1 and a["k"] == 3.75 and not a["active"]
    before = a["warn"]
    after = m.loosen_segment_alert("https://a.example/x.m3u8")
    assert after["k"] == 4.75 and after["tooSensitive"] == 1 and after["warn"] >= before
    assert m.loosen_segment_alert("https://nowhere.example/x.m3u8") is None


def test_a_fresh_stream_never_warns_even_if_its_clock_used_to_run_ahead(tmp_path):
    m = Metrics(tmp_path / "m.db")
    t0 = time.time() - 3600
    for i in range(40):  # clock ran 21 s ahead: normal is -21 s
        m.record("a", seg_health(-21 + (i % 2)), t0 + i * 30)
    for i in range(4):  # clock fixed: -1 s is fresh, not a warning
        m.record("a", seg_health(-1), t0 + (40 + i) * 30)
    (a,) = m.segment_alerts("a")
    assert a["warn"] >= 9.0 and not a["active"]


def test_timeline_buckets_a_week_of_checks_with_incidents(tmp_path):
    m = Metrics(tmp_path / "m.db")
    t0 = time.time() - 3 * 3600
    for i in range(60):  # three hours: the middle 10 checks fail
        m.record("a", health(up=not 25 <= i < 35, latency=100 + i, age=3.0 if not 25 <= i < 35 else None),
                 t0 + i * 180)
    m.record("a", health(up=False), t0 + 62 * 180)  # one failed check: a blip, not an outage
    m.record("a", health(), t0 + 63 * 180)
    tl = m.timeline("a", since_s=4 * 3600, buckets=24)
    assert tl["checks"] == 62 and tl["fails"] == 11
    assert tl["clipped"] and tl["from"] == pytest.approx(t0 - 60, abs=1)  # 3 h of data in a 4 h window
    assert tl["bucketS"] == pytest.approx((tl["to"] - tl["from"]) / 24, abs=1)
    assert [p["t"] for p in tl["points"]] == sorted(p["t"] for p in tl["points"])
    assert any(p["up"] < 100 for p in tl["points"]) and all(0 <= p["up"] <= 100 for p in tl["points"])
    # Read from the checks themselves (survives the incident log being archived into totals).
    assert [(i["type"], i.get("checks"), i.get("durationS")) for i in tl["incidents"]] == [
        ("OUTAGE", 10, None), ("RECOVERY", None, 1800), ("BLIP", 1, None)]
    assert tl["incidents"][0]["t"] == pytest.approx(t0 + 25 * 180) and tl["incidents"][0]["category"] == "STALE_MEDIA"
    assert m.timeline("nobody")["points"] == []


def test_old_files_migrate_and_old_checks_fold_into_rollups_without_losing_counts(tmp_path):
    import sqlite3
    import metrics as metrics_mod
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)  # the previous layout: URL and node text on every url_checks row
    old.executescript("""
        CREATE TABLE node_checks (ts REAL NOT NULL, node TEXT NOT NULL, up INTEGER NOT NULL, latency_ms REAL,
            rtt_ms REAL, jitter_ms REAL, loss REAL, segment_age_s REAL, failing_urls INTEGER, category TEXT);
        CREATE TABLE url_checks (ts REAL NOT NULL, node TEXT NOT NULL, url TEXT NOT NULL, up INTEGER NOT NULL,
            latency_ms REAL, segment_age_s REAL, freshness TEXT, category TEXT, detail TEXT, target_s REAL);""")
    now = time.time()
    t0 = now - 5 * 86400  # five days ago: older than the raw window
    for i in range(30):
        up = int(i not in (10, 11))
        old.execute("INSERT INTO node_checks VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (t0 + i * 30, "a", up, 100.0, 30.0, 2.0, 0.0, 3.0, 1 - up, None if up else "STALE_MEDIA"))
        old.execute("INSERT INTO url_checks VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (t0 + i * 30, "a", "https://a.example/x.m3u8", up, 100, 3.0, "FRESH", None,
                     "master, 1/1 variants live" if up else "STALE_SEGMENTS (40s old)", 6.0))
    old.commit(); old.close()

    m = Metrics(path)
    with m._connect() as db:
        assert db.execute("SELECT type FROM sqlite_master WHERE name = 'url_checks'").fetchone()[0] == "view"
        assert db.execute("SELECT COUNT(*) FROM url_checks").fetchone()[0] == 30
        assert db.execute("SELECT COUNT(*) FROM url_checks WHERE up = 1 AND detail IS NOT NULL").fetchone()[0] == 0
    before = m.timeline("a", since_s=7 * 86400)
    assert before["checks"] == 30 and before["fails"] == 2

    m.record("a", health(), now)  # a new check runs the hourly compaction
    with m._connect() as db:
        rows = db.execute("SELECT COUNT(*) FROM node_checks WHERE ts < ?", (now - 86400,)).fetchone()[0]
        url_rows = db.execute("SELECT COUNT(*) FROM url_check_rows WHERE ts < ?", (now - 86400,)).fetchone()[0]
    assert rows <= 4 and url_rows <= 4  # 15 minutes of checks folded into 5-minute rows (30 rows before)
    after = m.timeline("a", since_s=7 * 86400)
    assert after["checks"] == 31 and after["fails"] == 2  # nothing lost in the folding
    assert [i["type"] for i in after["incidents"]][:2] == ["OUTAGE", "RECOVERY"]
