"""The one alert log and the "what alerts come next" predictions: learned from the log, from the pipeline, and
scored against what actually happened."""

import time

import alertlog
from metrics import Metrics

TOPO = {
    "channels": {
        "rang": {"final": "cdn/rang", "inputs": {"ingest1": "MainInput"}, "transcoders": ["xcode4"],
                 "chain": ["ingest1", "xcode4", "cdn/rang"]},
        "b24": {"final": "stream/b24", "inputs": {"legitpro": "MainInput", "ingest1": "BackupLink"},
                "transcoders": ["xcode2"], "chain": ["legitpro", "ingest1", "xcode2", "stream/b24"]},
    },
    "status": {"ingest1": "UP", "xcode4": "UP", "legitpro": "UP", "xcode2": "UP"},
}


def test_the_same_alert_twice_counts_once_and_endings_are_logged(tmp_path):
    log = alertlog.AlertLog(tmp_path / "m.db")
    t = time.time()
    assert log.add("OUTAGE", "xcode4", "rang", "STALE_MEDIA", ts=t)
    assert log.add("OUTAGE", "xcode4", "rang", "STALE_MEDIA", ts=t + 30) is None  # same alert, 30 s later
    assert log.add("RECOVERY", "xcode4", "rang", ts=t + 200)
    assert [a["kind"] for a in log.recent()] == ["RECOVERY", "OUTAGE"]


def test_the_pipeline_predicts_off_air_without_a_backup_and_failover_with_one(tmp_path):
    log = alertlog.AlertLog(tmp_path / "m.db")
    log.add("OUTAGE", "xcode4", "rang", "STALE_MEDIA")
    log.add("OUTAGE", "legitpro", "b24", "UNREACHABLE")
    upcoming = {(p["node"], p["kind"]): p for p in log.predict(lambda: TOPO)}
    assert upcoming[("cdn/rang", "OUTAGE")]["source"] == "pipeline"  # the transcoder is the only way through
    failover = upcoming[("stream/b24", "FAILOVER_ACTIVE")]
    assert "backup ingest1 is healthy" in failover["reason"]
    # With the backup also down, the channel goes off air instead.
    log2 = alertlog.AlertLog(tmp_path / "n.db")
    log2.add("OUTAGE", "legitpro", "b24")
    down = {**TOPO, "status": {**TOPO["status"], "ingest1": "DOWN"}}
    assert {(p["node"], p["kind"]) for p in log2.predict(lambda: down)} >= {("stream/b24", "OUTAGE")}


def test_learned_rules_predict_what_followed_before_and_predictions_are_scored(tmp_path):
    log = alertlog.AlertLog(tmp_path / "m.db")
    now = time.time()
    for k in range(4):  # four times: ingest1 drifts, and the Rang Manch Final glitches ~2 min later
        t = now - (k + 1) * 3 * 3600
        log.add("SEGMENT_AGE_HIGH", "ingest1", None, "segment age 70 s", ts=t)
        log.add("GLITCH", "cdn/rang", "rang", "skipped content", ts=t + 120)
    log.add("EARLY_WARNING", "jio", None, "latency high", ts=now - 5 * 3600)  # unrelated noise
    (rule,) = [r for r in log.learn(now) if r["if_node"] == "ingest1"]
    assert (rule["then_node"], rule["then_kind"], rule["share"], rule["lag_s"]) == ("cdn/rang", "GLITCH", 1.0, 120)

    log.add("SEGMENT_AGE_HIGH", "ingest1", None, "segment age 80 s", ts=now - 30)
    (p,) = [p for p in log.predict(now=now) if p["kind"] == "GLITCH"]
    assert p["source"] == "learned" and p["node"] == "cdn/rang" and "followed 4 of 4 times" in p["reason"]

    log.add("GLITCH", "cdn/rang", "rang", "skipped content", ts=now + 60)  # it came true
    assert log.scores()["learned"]["hits"] == 1 and log.scores()["learned"]["medianWarningS"] == 60

    log.add("SEGMENT_AGE_HIGH", "ingest1", None, "again", ts=now + 1000)
    log.predict(now=now + 1001)
    log.evaluate(now + 1000 + 2000)  # its deadline passes and nothing happened
    s = log.scores()["learned"]
    assert s["misses"] == 1 and s["hitRate"] == 0.5


def test_incidents_reach_the_alert_log_through_metrics(tmp_path):
    m = Metrics(tmp_path / "m.db")
    m.alert_log = alertlog.AlertLog(tmp_path / "m.db")
    m.add_incident("xcode4", {"type": "OUTAGE", "timestamp": "2026-10-02T10:00:00+00:00", "category": "STALE_MEDIA",
                              "correlation": [{"channel": "rang"}]})
    (a,) = m.alert_log.recent(since_s=10 ** 9)
    assert (a["kind"], a["node"], a["channel"], a["detail"]) == ("OUTAGE", "xcode4", "rang", "STALE_MEDIA")



def test_history_seeds_the_log_once(tmp_path):
    log = alertlog.AlertLog(tmp_path / "m.db")
    now = time.time()
    events = [{"ts": now - 7200 + k * 600, "node": "xcode4", "kind": "OUTAGE"} for k in range(3)]
    assert log.backfill(events) == 3
    assert log.backfill(events) == 0  # already seeded



def test_alerts_that_happen_together_are_not_a_warning(tmp_path):
    log = alertlog.AlertLog(tmp_path / "m.db")
    now = time.time()
    for k in range(5):  # the same incident noticed on two servers 3 s apart
        log.add("OUTAGE", "cdn/rang", None, ts=now - (k + 1) * 3600)
        log.add("OUTAGE", "xcode4", None, ts=now - (k + 1) * 3600 + 3)
    assert log.learn(now) == []


def test_early_warnings_must_last_and_are_spaced_out(monkeypatch):
    """A score hovering on the threshold logged a new early warning each time it crossed (49,000 in 7 days)."""
    from types import SimpleNamespace
    import server
    added, predicted = [], []
    monkeypatch.setattr(server.alert_log, "add", lambda kind, node, *a, **k: added.append((kind, node)))
    monkeypatch.setattr(server.alert_log, "predict", lambda *a, **k: predicted.append(1))
    for d in (server._warning_since, server._warning_logged):
        d.clear()
    monkeypatch.setattr(server, "_predictions_recorded_at", -1e9)
    clock = [1000.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    result = SimpleNamespace(reports=[])
    warn = [{"node": "cloud", "score": 7.0, "warnings": ["HTTP latency 900 is 7σ above its normal 300"]}]

    def run(at, warnings):
        clock[0] = at
        server.log_run_alerts(result, warnings)
    run(1000, warn); run(1060, warn)
    assert added == []  # not yet held for 90 s
    run(1095, warn)
    assert added == [("EARLY_WARNING", "cloud")]
    run(1100, []); run(1110, warn); run(1300, warn)  # gone and back: has to last again, and 30 min hasn't passed
    assert len(added) == 1
    run(1110 + 1800, warn)
    assert len(added) == 2
    assert len(predicted) <= 6  # predictions recorded at most once a minute, not on every run
