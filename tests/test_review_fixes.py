"""Regression tests for the review fixes: outcome feedback, the learned fix, report numbers, prediction odds."""

import sqlite3
import time

from learning import Learner
from metrics import Metrics


def test_outcome_feedback_matches_the_server_exactly_and_counts_the_alert(tmp_path):
    path = tmp_path / "m.db"
    Metrics(path)
    with sqlite3.connect(path) as db:
        for url, node in (("https://gtc.x/a/index.m3u8", "gtc.x"), ("https://gtcpunjabi.x/a/index.m3u8", "gtcpunjabi.x")):
            db.execute("INSERT INTO segment_alerts (url, node, k) VALUES (?, ?, 3)", (url, node))
    learner = Learner(path, embed=lambda texts: __import__("numpy").ones((len(texts), 4)))
    assert learner.record_alert_outcome("gtc.x", "settled_alone")
    assert learner.record_alert_outcome("gtc.x", "silenced_fast")
    assert not learner.record_alert_outcome("gtc.x", "nonsense")
    with sqlite3.connect(path) as db:
        rows = dict(((n, (r, c)) for n, r, c in db.execute("SELECT node, raised, cleared_alone FROM segment_alerts")))
    assert rows == {"gtc.x": (2, 2), "gtcpunjabi.x": (0, 0)}  # never more cleared than raised; no partial matches


def test_the_outcome_route_accepts_silenced_fast(monkeypatch):
    import server
    from fastapi.testclient import TestClient
    from tests.conftest import TEST_LOGIN
    seen = []
    monkeypatch.setattr(server.learner, "record_alert_outcome", lambda node, outcome: seen.append(outcome) or True)
    server.auth.attempts.clear()
    c = TestClient(server.app)
    c.post("/login", data={**TEST_LOGIN, "next": "/"}, follow_redirects=False)
    assert c.post("/api/learning/record-alert-outcome", json={"node": "x", "outcome": "silenced_fast"}).status_code == 200
    assert seen == ["silenced_fast"]


def test_the_hazard_forecast_uses_real_outage_gaps():
    from report import ml_hazard_forecast_html
    few = ml_hazard_forecast_html([{"id": "a", "log": [{"type": "OUTAGE", "timestamp": "2026-10-01T00:00:00+00:00"}]}])
    assert "Not enough outages yet" in few and "100.0%" not in few
    t0 = time.time() - 10 * 3600
    log = [{"type": "OUTAGE", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(t0 + i * 7200))}
           for i in range(5)]
    html = ml_hazard_forecast_html([{"id": "b", "log": log}])
    assert "Not enough" not in html and "<b>" in html


def test_the_postmortem_reads_real_incident_entries_newest_first():
    import report
    captured = {}

    class TR:
        @staticmethod
        def summarize_incident(logs, **k):
            captured["logs"] = logs
            return {"headline": "h", "executive_summary": "s", "key_findings": []}
    import incident_summarizer
    orig = incident_summarizer.textrank_summarizer
    incident_summarizer.textrank_summarizer = TR
    try:
        log = [{"type": "OUTAGE", "timestamp": f"2026-10-01T{h:02d}:00:00+00:00", "category": "STALE_MEDIA",
                "lastError": f"err {h}"} for h in range(40)][:24] + [{"type": "RECOVERY", "durationS": 30,
                                                                      "timestamp": "2026-10-02T00:00:00+00:00"}]
        report.incident_postmortem_html([{"id": "x", "log": log}])
    finally:
        incident_summarizer.textrank_summarizer = orig
    logs = captured["logs"]
    assert any("err 23" in l for l in logs) and logs[-1].endswith("back up after 30 s")
    assert not any("nominal" in l for l in logs)
