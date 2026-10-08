import re
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

import rca_rank
from learning import Learner
from metrics import Metrics


def bag_of_words(texts):
    """A tiny stand-in for MiniLM: hashed word counts, so texts sharing words are close."""
    out = np.zeros((len(texts), 64), dtype="float32")
    for i, t in enumerate(texts):
        for w in re.findall(r"[a-z0-9]+", t.lower()):
            out[i, hash(w) % 64] += 1
    return out + 1e-6


T0 = datetime(2026, 9, 1, 20, 30, tzinfo=timezone.utc)  # 02:00 IST


def at(minutes=0, days=0):
    return (T0 + timedelta(days=days, minutes=minutes)).isoformat()


def outage(node, start, minutes, category="STALE_MEDIA", root=None, verdict="LOCAL_TO_NODE"):
    open_ = {"type": "OUTAGE", "timestamp": start, "category": category, "lastError": "segment 40 s old",
             "correlation": [{"channel": "gtcnews", "verdict": verdict}]}
    if root:
        open_["rootCauseRanking"] = [{"node": root, "score": 0.8}]
    close = {"type": "RECOVERY", "timestamp": (datetime.fromisoformat(start) + timedelta(minutes=minutes)).isoformat(),
             "durationS": minutes * 60, "category": category}
    return (node, open_), (node, close)


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "m.db"
    metrics = Metrics(path)
    learner = Learner(path, embed=bag_of_words)
    return metrics, learner


def log(metrics, *pairs):
    for pair in pairs:
        for node, entry in pair:
            metrics.add_incident(node, entry)


def test_closed_outages_become_cases_once_and_open_ones_wait(world):
    metrics, learner = world
    log(metrics, outage("ingest1", at(), 8, root="ingest1"), outage("xcode2", at(days=1), 3))
    metrics.add_incident("ingest1", {"type": "OUTAGE", "timestamp": at(days=2), "category": "UNREACHABLE"})
    assert learner.sync_cases(force=True) == 2
    assert learner.sync_cases(force=True) == 0  # nothing new
    cases = {c["node"]: c for c in learner.cases()}
    assert set(cases) == {"ingest1", "xcode2"}
    assert cases["ingest1"]["duration_s"] == 480 and cases["ingest1"]["channels"] == ["gtcnews"]
    assert "lasted 8 min" in cases["ingest1"]["text"] and "local to node" in cases["ingest1"]["text"]


def test_a_recorded_fix_comes_back_with_similar_outages(world):
    metrics, learner = world
    log(metrics, outage("ingest1", at(), 8), outage("xcode2", at(days=3), 5, category="SERVER_ERROR"))
    assert learner.set_resolution("ingest1", "restarted nginx on ingest1")["resolution"] == "restarted nginx on ingest1"
    assert learner.set_resolution("nowhere", "x") is None

    found = learner.context("why is ingest1 stale?", nodes=["ingest1"])
    assert "fixed by: restarted nginx" in found["text"]
    assert found["cases"][0]["node"] == "ingest1"


def test_root_cause_verdicts_become_priors_that_move_the_ranking(world):
    _, learner = world
    assert learner.priors() == {}
    for _ in range(3):
        learner.add_feedback("root_cause", 1, node="ingest2")
    learner.add_feedback("root_cause", -1, node="ingest1")
    learner.add_feedback("root_cause", -1, node="ingest1")
    priors = learner.priors()
    assert priors["ingest2"] == 1.6 and priors["ingest1"] == 0.5
    with pytest.raises(ValueError):
        learner.add_feedback("root_cause", 0, node="x")

    # Two inputs down with no onset times: alike, until the operator's verdicts break the tie.
    states = {n: {"up": False, "onsetAt": None, "category": "UNREACHABLE"} for n in ("ingest1", "ingest2")}
    states["final"] = {"up": False, "onsetAt": None, "category": "STALE_MEDIA"}
    edges = [{"source": "ingest1", "type": "FEEDS", "target": "final"},
             {"source": "ingest2", "type": "FEEDS", "target": "final"}]
    plain = rca_rank.rank(states, edges, {})[0]["ranking"]
    learned = rca_rank.rank(states, edges, {}, priors)[0]["ranking"]
    share = lambda ranking, n: next(r["score"] for r in ranking if r["node"] == n)
    assert share(plain, "ingest1") == share(plain, "ingest2")
    assert learned[0]["node"] == "ingest2"
    assert "confirmed as the root cause before" in learned[0]["reasons"]


def test_a_correction_is_followed_for_similar_questions_only(world):
    _, learner = world
    learner.add_feedback("answer", -1, question="which channel is on backup right now",
                         answer="none", correction="gtcnews runs on backup from ingest2 every night")
    learner.add_feedback("answer", 1, question="how many servers are there")
    assert learner.context("which channel is on backup now")["lessons"][0]["correction"].startswith("gtcnews")
    assert learner.context("show me the latency of xcode2")["lessons"] == []
    assert learner.feedback_stats()["answer"] == {"up": 1, "down": 1, "rephrased": 0}


def test_facts_link_to_known_nodes_skip_duplicates_and_can_be_forgotten(world):
    _, learner = world
    fact = learner.add_fact("xcode2 restarts every night at 03:00", ["xcode2.ottlive.co.in", "ingest1.ottlive.co.in"])
    assert fact["nodes"] == ["xcode2.ottlive.co.in"]
    assert learner.add_fact("XCODE2 restarts every night at 03:00")["duplicate"]
    assert "xcode2 restarts every night" in learner.context("anything")["text"]
    assert [f["text"] for f in learner.forget_fact(text="restarts")] == ["xcode2 restarts every night at 03:00"]
    assert learner.facts() == []


def test_patterns_time_of_day_recurrence_and_nodes_failing_together(world):
    metrics, learner = world
    for day in range(4):  # ingest1 at ~02:00 IST every day; final follows a minute later
        log(metrics, outage("ingest1", at(0, days=day), 6), outage("final", at(1, days=day), 5))
    texts = [p["text"] for p in learner.patterns()]
    assert any(t.startswith("ingest1 has had 4 outages, usually lasting about 6 min, mostly because the video stopped updating") for t in texts)
    assert any("ingest1 usually fails between 02:00 and 04:00 IST" in t for t in texts)
    assert any("ingest1 fails about every" in t for t in texts)
    assert any("have failed together 4 times" in t and "ingest1 usually goes first" in t for t in texts)


def test_context_is_empty_with_nothing_learned(world):
    _, learner = world
    assert learner.context("hello")["text"] == ""


def test_failing_together_orders_by_the_estimated_onset_not_the_detection_time(world):
    metrics, learner = world
    for day in range(2):  # final is detected first, but ingest1's stream stopped earlier
        a, b = outage("final", at(0, days=day), 5), outage("ingest1", at(1, days=day), 5)
        a[0][1]["rootCauseRanking"] = [{"node": "ingest1", "onsetAt": at(-2, days=day)},
                                       {"node": "final", "onsetAt": at(-1, days=day)}]
        b[0][1]["rootCauseRanking"] = [{"node": "ingest1", "onsetAt": at(-2, days=day)}]
        log(metrics, a, b)
    together = next(p for p in learner.patterns() if p["kind"] == "together")
    assert together["nodes"] == ["ingest1", "final"] and "ingest1 usually goes first" in together["text"]


def test_everything_for_the_learning_page(world):
    metrics, learner = world
    log(metrics, outage("ingest1", at(), 8))
    learner.add_fact("ingest1 is a test feed")
    learner.add_feedback("root_cause", 1, node="ingest1")
    learner.add_feedback("answer", -1, question="is ingest1 live", correction="it is a test feed")
    learner.add_feedback("answer", 1, question="hi")
    page = learner.everything()
    assert page["caseCount"] == 1 and page["cases"][0]["onset"]
    assert page["weights"] == [{"node": "ingest1", "factor": 1.333, "right": 1, "wrong": 0}]
    assert [l["correction"] for l in page["lessons"]] == ["it is a test feed"]
    assert len(page["recentFeedback"]) == 3 and page["settings"]["minPattern"] == 2


def test_dynamic_alert_policy_and_feedback(world):
    metrics, learner = world
    # Simulate recurring transient outages on ingest1 and cascading to xcode4
    for day in range(12):
        # outage(node, start, minutes)
        # 0.4 minutes = 24 seconds duration
        t_start = at(minutes=day * 10, days=0)
        t_xcode = (datetime.fromisoformat(t_start) + timedelta(seconds=15)).isoformat()
        log(metrics, 
            outage("ingest1", t_start, 0.4),
            outage("xcode4", t_xcode, 0.3))

    policy = learner.get_dynamic_alert_policy()
    assert "node_profiles" in policy
    assert "cascade_graph" in policy
    assert "ingest1" in policy["node_profiles"]

    profile = policy["node_profiles"]["ingest1"]
    assert profile["outage_count"] == 12
    assert profile["median_duration_s"] == 24.0
    assert profile["is_flapper"] is True
    # Hold down should be clamped between 25 and 45s (24s + 5 = 29s)
    assert profile["hold_down_s"] == 29

    # Check cascade graph
    assert "ingest1" in policy["cascade_graph"]
    followers = policy["cascade_graph"]["ingest1"]
    assert any(f["follower"] == "xcode4" for f in followers)

    # Check audio tuning
    assert "audio_tuning" in profile
    tuning = profile["audio_tuning"]
    assert tuning["speech_rate"] == 1.25
    assert tuning["verbosity"] == "brief"
    assert tuning["anti_fatigue"] is True
    assert tuning["speech_pitch"] <= 0.95
    assert "time_of_day" in policy
    assert "shift" in policy["time_of_day"]

    # Test outcome recording
    success = learner.record_alert_outcome("ingest1", "settled_alone")
    assert success is True
    success_silence = learner.record_alert_outcome("ingest1", "silenced_fast")
    assert success_silence is True



def test_two_frequent_failers_meeting_by_chance_are_not_a_pattern(world):
    metrics, learner = world
    # flappy fails every 5 min and noisy every 6 min, for a day, independently: they often land within 3 min of
    # each other by pure chance, which must not be reported as "failing together".
    for k in range(288):
        log(metrics, outage("flappy", at(5 * k), 0.3))
    for k in range(240):
        log(metrics, outage("noisy", at(6 * k + 2), 0.3))
    assert not [p for p in learner.patterns() if p["kind"] == "together"]


def test_the_pipeline_decides_who_is_the_likely_cause(world):
    metrics, learner = world
    learner.upstream_of = lambda: {"final": ["ingest1"]}  # ingest1 feeds final
    for day in range(4):  # but the checks notice final first every time
        log(metrics, outage("final", at(0, days=day), 5), outage("ingest1", at(1, days=day), 5))
    learner._patterns = None
    together = next(p for p in learner.patterns() if p["kind"] == "together")
    assert together["nodes"] == ["ingest1", "final"]
    assert "ingest1 feeds final, so ingest1 is the likely cause" in together["text"]
    assert "goes first" not in together["text"] and together["lift"] >= 2


def test_one_tap_outage_ratings_teach_the_ranking(world):
    _, learner = world
    k1, k2, k3 = "cloud|2026-10-08T10:00:00+00:00", "ingest1|2026-10-08T11:00:00+00:00", "live1|2026-10-08T12:00:00+00:00"
    learner.rate_outage(k1, "cloud", True)
    learner.rate_outage(k2, "ingest1", False, real="xcode4")  # wrong: it was xcode4
    r = learner.rate_outage(k3, "live1", False, real="network")  # wrong, and no server to credit
    assert (r["rated"], r["right"], r["wrong"], r["target"]) == (3, 1, 2, 50) and r["accuracy"] == 0.333
    assert r["answers"][k2] == {"right": False, "real": "xcode4"} and r["answers"][k1] == {"right": True, "real": None}
    p = learner.priors()
    assert p["cloud"] > 1 and p["xcode4"] > 1  # confirmed, and named as the real culprit
    assert p["ingest1"] < 1 and p["live1"] < 1  # wrongly blamed
    assert "network" not in p
    again = learner.rate_outage(k2, "ingest1", True)  # changed the answer: replaces, never double counts
    assert (again["rated"], again["right"]) == (3, 2) and "xcode4" not in learner.priors()
