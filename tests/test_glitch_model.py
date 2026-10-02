"""The trained glitch model: it learns a real early sign from the history, is scored on data it never saw, only
switches itself on when it earns it, and explains its predictions."""

import random
import sqlite3
import time

import glitch
import glitch_model as gm

FINAL = {"node": "final", "url": "https://final.example.test/ch/index.m3u8", "channel": "ch"}


def world(path, hours=48, signal=True, seed=1):
    """Checks every 30 s for a Final and its transcoder. With `signal`, the transcoder starts failing 5 min before
    each of the Final's outages (about one every 2 hours); otherwise failures come out of the blue."""
    glitch.GlitchProbe(path)  # creates the glitch tables
    rnd = random.Random(seed)
    now = time.time()
    start = now - hours * 3600
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE node_checks (ts REAL, node TEXT, up INTEGER, latency_ms REAL, rtt_ms REAL, "
                   "jitter_ms REAL, loss REAL, segment_age_s REAL, failing_urls INTEGER, category TEXT, n INTEGER, "
                   "fails INTEGER, rolled INTEGER)")
        outages = sorted(start + 3600 + rnd.random() * (hours - 1.5) * 3600 for _ in range(hours // 2))
        rows = []
        t = start
        while t < now:
            final_down = any(o <= t < o + 180 for o in outages)
            xcode_down = any(o - 300 <= t < o + 60 for o in outages) if signal else rnd.random() < 0.05
            for node, down in (("final", final_down), ("xcode", xcode_down)):
                rows.append((t, node, int(not down), 200 + rnd.random() * 50, None, None, None, 3.0, int(down), None,
                             1, int(down), 0))
            t += 30
        db.executemany("INSERT INTO node_checks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return now


def test_a_real_early_sign_is_learned_scored_on_unseen_data_and_explained(tmp_path):
    path = str(tmp_path / "m.db")
    now = world(path)
    m = gm.train(path, [FINAL], lambda n: ["xcode"], now=now)
    assert m["samples"] > 1000 and m["positives"] >= gm.MIN_POSITIVES
    assert m["auc"] >= gm.MIN_AUC and m["active"], m["note"]
    assert "hour_sin" not in m["features"]  # two days isn't enough to learn the time of day
    top = max(zip(m["weights"], m["features"]))[1]
    assert top == "upstream_fails_10m"  # it found the cause, not a coincidence
    assert gm.load(path)["trained_at"] == m["trained_at"]

    # Right now the transcoder is failing: the model should call a glitch likely, and say why.
    with sqlite3.connect(path) as db:
        db.executemany("INSERT INTO node_checks (ts, node, up, latency_ms, segment_age_s, n, fails, rolled) "
                       "VALUES (?, 'xcode', 0, 300, 3, 1, 1, 0)", [(now - i * 30,) for i in range(8)])
    hot = gm.predict(path, m, FINAL, ["xcode"], now=now + 1)
    assert hot["probability"] > 0.5 and "failures on the servers feeding it" in hot["drivers"]


def test_no_signal_or_too_little_history_keeps_the_model_switched_off(tmp_path):
    random_world = str(tmp_path / "r.db")
    world(random_world, signal=False)
    m = gm.train(random_world, [FINAL], lambda n: ["xcode"])
    assert not m["active"] and "not used yet" in m["note"]
    assert gm.predict(random_world, m, FINAL, ["xcode"]) is None

    short = str(tmp_path / "s.db")
    world(short, hours=2)
    m = gm.train(short, [FINAL], lambda n: ["xcode"])
    assert not m["active"] and m["note"].startswith("Not enough history yet")
