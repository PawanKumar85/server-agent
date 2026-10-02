"""AI on the full history: the facts are computed in code, the model only explains them, answers are cached."""

import json
import time

import history_ai
from history_ai import HistoryAI, facts, prompt

T0 = 1790800000  # 01 Oct ~01:56 IST


def timeline(outage_hours=(10, 10, 10, 15), recover_s=300, fails_late=True):
    points = []
    for k in range(48):  # 48 buckets of 30 min = 24 h
        late = k >= 24
        points.append({"t": T0 + k * 1800, "checks": 10, "fails": (4 if late and fails_late else 1),
                       "latency": 200 + (900 if k == 40 else 0), "loss": 0.0, "age": 3, "ageMax": 70 if k == 41 else 5})
    incidents = []
    for i, h in enumerate(outage_hours):  # outages at given IST hours, on two days
        ist_midnight = T0 - (T0 + 19800) % 86400
        t = ist_midnight + h * 3600 + (86400 if i == 3 else 0)
        incidents += [{"t": t, "type": "OUTAGE", "category": "STALE_MEDIA"},
                      {"t": t + recover_s, "type": "RECOVERY", "category": "STALE_MEDIA", "durationS": recover_s + 60 * i}]
    incidents.append({"t": T0 + 100, "type": "BLIP", "category": "STALE_MEDIA"})
    return {"node": "xcode4.x", "from": T0, "to": T0 + 48 * 1800, "bucketS": 1800, "clipped": True,
            "checks": sum(p["checks"] for p in points), "fails": sum(p["fails"] for p in points), "points": points,
            "incidents": incidents, "segments": [{"channel": "Rang Manch", "normal": 5, "warn": 60}],
            "glitches": [{"kind": "SKIPPED_CONTENT"}], "adBreaks": [{"status": "CLOSED"}, {"status": "STUCK"}]}


def test_the_facts_are_computed_in_code():
    f = facts(timeline(), {"patterns": ["ingest1 usually fails first"], "fixes": ["restarted nginx"],
                           "root_causes": "ingest1.x (5x)", "status": "UP"})
    assert f["outages"] == 4 and f["short_blips"] == 1 and f["uptime_pct"] == round(100 * (480 - 120) / 480, 1)
    assert f["worst_hours"].startswith("10:00-11:00 (3 outages)") and "video stopped updating (4x)" in f["outage_causes"]
    assert f["longest_outage"].startswith("8 min") and f["total_downtime"] == "26 min"
    assert f["failure_rate_first_half_pct"] == 10.0 and f["failure_rate_second_half_pct"] == 40.0  # getting worse
    assert f["response_ms_worst"].startswith("1100") and f["segment_age_worst_s"] == 70 and f["buckets_over_warn_line"] == 1
    assert f["ad_breaks"] == "2 (1 stuck or overran)" and "skipped content (1)" in f["glitches"]
    text = prompt(f)[1]["content"]
    assert "fixes recorded by operator: restarted nginx" in text and "root causes found before: ingest1.x (5x)" in text
    assert "Use ONLY the facts" in prompt(f)[0]["content"] and "**What to do**" in prompt(f)[0]["content"]


class Bot:
    def __init__(self):
        self.calls = 0

    def generate(self, messages, options):
        self.calls += 1
        yield {"type": "token", "text": "**What happened**\n- 4 outages."}
        yield {"type": "done", "model": "fast/model", "elapsed_ms": 900, "tokens": {"input": 300, "output": 40}}


def test_answers_stream_and_repeat_requests_are_cached():
    bot = Bot()
    ai = HistoryAI(bot)
    first = list(ai.stream("xcode4.x", 86400, timeline(), {}))
    assert [e["type"] for e in first] == ["facts", "token", "done"] and first[-1]["cached"] is False
    again = list(ai.stream("xcode4.x", 86400, timeline(), {}))
    assert again[1]["text"] == "**What happened**\n- 4 outages." and again[-1]["cached"] and bot.calls == 1
    list(ai.stream("xcode4.x", 7 * 86400, timeline(), {}))  # another range: a new answer
    assert bot.calls == 2
    ai.cache[("xcode4.x", 86400)]["at"] = time.time() - history_ai.CACHE_S - 1  # stale: asked again
    list(ai.stream("xcode4.x", 86400, timeline(), {}))
    assert bot.calls == 3


def test_no_data_means_no_model_call():
    bot = Bot()
    empty = {"node": "n", "from": T0, "to": T0 + 3600, "checks": 0, "fails": 0, "points": [], "incidents": []}
    events = list(HistoryAI(bot).stream("n", 86400, empty, {}))
    assert bot.calls == 0 and "no checks recorded" in events[1]["text"]


def test_the_route_streams_the_explanation(monkeypatch):
    import server
    from fastapi.testclient import TestClient
    from routes import monitor
    from tests.conftest import TEST_LOGIN
    monkeypatch.setattr(server, "history_ai", HistoryAI(Bot()))
    monkeypatch.setattr(monitor, "node_timeline", lambda node, since, buckets: timeline())
    monkeypatch.setattr(monitor, "history_learned", lambda node: {"fixes": ["restarted nginx"]})
    server.auth.attempts.clear()
    c = TestClient(server.app)
    c.post("/login", data={**TEST_LOGIN, "next": "/"}, follow_redirects=False)
    r = c.post("/api/nodes/cdn.x/Rang Manch/history-ai?since=86400")
    events = [json.loads(line) for line in r.text.splitlines()]
    assert r.status_code == 200 and events[0]["facts"]["fixes_recorded_by_operator"] == ["restarted nginx"]
    assert events[-1]["type"] == "done" and events[1]["text"].startswith("**What happened**")
    assert c.post("/api/nodes/x/history-ai?since=60").status_code == 422
