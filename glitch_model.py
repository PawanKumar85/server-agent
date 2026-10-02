"""A trained model for "will this Final glitch or fail in the next 10 minutes?", learned from the saved history.

glitch.forecast() predicts with hand-set statistics from day one; this learns the weights from what actually
happened. Each sample is one Final at one moment (every SAMPLE_S through the history); its label is whether that
Final had a glitch (glitch.py) or a failed check within the next HORIZON_S. The features are what was known at that
moment:

  own_fails_10m / own_fails_60m   its failed checks in the last 10 / 60 minutes
  own_glitches_10m / _60m         its glitches in the last 10 / 60 minutes
  latency_ms, segment_age_s       its latest response time and newest-segment age
  age_drift_s                     newest-segment age now minus its median over the last hour
  delivery_ratio                  how long a full-quality segment takes to arrive, as a share of its length
  upstream_fails_10m              failed checks on the servers that feed it, last 10 minutes
  upstream_age_max_s              the oldest newest-segment age among them, latest
  hour_sin, hour_cos              the time of day (IST)

The model is a logistic regression (numpy only, fitted by Newton's method; heavy-tailed counts log-scaled, features
standardized, classes balanced, L2-regularized). Time of day is left out until there are TIME_OF_DAY_DAYS of
history (with less it just memorises when yesterday's outages were). It trains on the older part of the history
and is scored on the newest part it never saw (AUC, precision and recall). It is only *used* when that score is
clearly better than chance (AUC >= MIN_AUC) AND at least as good as the best single signal on its own; otherwise
the statistical forecast stands alone. Retrained every hour as the history grows.
"""

import bisect
import json
import math
import sqlite3
import statistics
import time
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

HORIZON_S = 600
SAMPLE_S = 120
MIN_SAMPLES = 300
MIN_POSITIVES = 30
MIN_AUC = 0.70
TEST_SHARE = 0.25
TIME_OF_DAY_DAYS = 7  # history needed before the time of day is used as a feature
IST = 19800
FEATURES = ["own_fails_10m", "own_fails_60m", "own_glitches_10m", "own_glitches_60m", "latency_ms", "segment_age_s",
            "age_drift_s", "delivery_ratio", "upstream_fails_10m", "upstream_age_max_s", "hour_sin", "hour_cos"]
WORDS = {"own_fails_10m": "its failed checks in the last 10 min", "own_fails_60m": "its failed checks in the last hour",
         "own_glitches_10m": "its glitches in the last 10 min", "own_glitches_60m": "its glitches in the last hour",
         "latency_ms": "its response time", "segment_age_s": "how old its newest video piece is",
         "age_drift_s": "its video pieces getting older than usual", "delivery_ratio": "slow delivery",
         "upstream_fails_10m": "failures on the servers feeding it", "upstream_age_max_s": "stale video upstream",
         "hour_sin": "the time of day", "hour_cos": "the time of day"}

SCHEMA = "CREATE TABLE IF NOT EXISTS glitch_model (id INTEGER PRIMARY KEY, trained_at REAL NOT NULL, model TEXT NOT NULL);"


class History:
    """The checks, glitches and probes of the relevant nodes, loaded once, with fast window counts."""

    def __init__(self, db, nodes: List[str], urls: List[str], start: float):
        self.checks: Dict[str, Tuple[list, list, list, list]] = {}
        for node in nodes:
            rows = db.execute("SELECT ts, fails, latency_ms, segment_age_s FROM node_checks WHERE node = ? AND ts >= ? "
                              "ORDER BY ts", (node, start)).fetchall()
            self.checks[node] = ([r[0] for r in rows], [r[1] or 0 for r in rows], [r[2] for r in rows], [r[3] for r in rows])
        self.glitch_ts: Dict[str, list] = {}
        self.ratio: Dict[str, Tuple[list, list]] = {}
        for url in urls:
            self.glitch_ts[url] = [r[0] for r in db.execute(
                "SELECT ts FROM glitches WHERE url = ? AND ts >= ? ORDER BY ts", (url, start))]
            rows = db.execute("SELECT ts, est_ratio FROM glitch_probes WHERE url = ? AND est_ratio IS NOT NULL "
                              "AND ts >= ? ORDER BY ts", (url, start)).fetchall()
            self.ratio[url] = ([r[0] for r in rows], [r[1] for r in rows])

    def fails(self, node: str, a: float, b: float) -> int:
        ts, fails, _, _ = self.checks.get(node, ([], [], [], []))
        i, j = bisect.bisect_right(ts, a), bisect.bisect_right(ts, b)
        return int(sum(fails[i:j]))

    def latest(self, node: str, t: float) -> Tuple[Optional[float], Optional[float]]:
        ts, _, lat, age = self.checks.get(node, ([], [], [], []))
        i = bisect.bisect_right(ts, t) - 1
        if i < 0 or t - ts[i] > 300:
            return None, None
        return lat[i], age[i]

    def age_median(self, node: str, a: float, b: float) -> Optional[float]:
        ts, _, _, age = self.checks.get(node, ([], [], [], []))
        vals = [v for v in age[bisect.bisect_right(ts, a):bisect.bisect_right(ts, b)] if v is not None and -300 < v < 3600]
        return statistics.median(vals) if vals else None

    def glitches(self, url: str, a: float, b: float) -> int:
        ts = self.glitch_ts.get(url, [])
        return bisect.bisect_right(ts, b) - bisect.bisect_right(ts, a)

    def delivery(self, url: str, t: float) -> Optional[float]:
        ts, vals = self.ratio.get(url, ([], []))
        i = bisect.bisect_right(ts, t) - 1
        return vals[i] if i >= 0 and t - ts[i] <= 300 else None

    def span(self) -> Tuple[Optional[float], Optional[float]]:
        all_ts = [ts[0] for ts, *_ in self.checks.values() if ts] + [ts[-1] for ts, *_ in self.checks.values() if ts]
        return (min(all_ts), max(all_ts)) if all_ts else (None, None)


def features_at(h: History, final: dict, ups: List[str], t: float) -> Dict[str, Optional[float]]:
    node, url = final["node"], final["url"]
    lat, age = h.latest(node, t)
    med = h.age_median(node, t - 3600, t)
    up_ages = [a for a in (h.latest(u, t)[1] for u in ups) if a is not None and -300 < a < 3600]
    hour = ((t + IST) % 86400) / 3600
    return {
        "own_fails_10m": h.fails(node, t - 600, t), "own_fails_60m": h.fails(node, t - 3600, t),
        "own_glitches_10m": h.glitches(url, t - 600, t), "own_glitches_60m": h.glitches(url, t - 3600, t),
        "latency_ms": lat, "segment_age_s": age if age is None or -300 < age < 3600 else None,
        "age_drift_s": (age - med) if age is not None and med is not None and -300 < age < 3600 else None,
        "delivery_ratio": h.delivery(url, t),
        "upstream_fails_10m": sum(h.fails(u, t - 600, t) for u in ups),
        "upstream_age_max_s": max(up_ages) if up_ages else None,
        "hour_sin": math.sin(2 * math.pi * hour / 24), "hour_cos": math.cos(2 * math.pi * hour / 24),
    }


def label_at(h: History, final: dict, t: float) -> int:
    return int(h.fails(final["node"], t, t + HORIZON_S) > 0 or h.glitches(final["url"], t, t + HORIZON_S) > 0)


def build_dataset(path: str, finals: List[dict], upstream_of: Callable[[str], List[str]],
                  days: float = 14, now: Optional[float] = None):
    now = now or time.time()
    start = now - days * 86400
    nodes = sorted({f["node"] for f in finals} | {u for f in finals for u in upstream_of(f["node"])})
    db = sqlite3.connect(path, timeout=10)
    try:
        h = History(db, nodes, [f["url"] for f in finals], start - 3600)
    finally:
        db.close()
    rows, labels, times = [], [], []
    for f in finals:
        ts = h.checks.get(f["node"], ([],))[0]
        if not ts:
            continue
        ups = upstream_of(f["node"])
        t = max(ts[0] + 3600, start)  # an hour of lead-in, so the windows are full
        while t < now - HORIZON_S:
            rows.append(features_at(h, f, ups, t)); labels.append(label_at(h, f, t)); times.append(t)
            t += SAMPLE_S
    return rows, np.array(labels, dtype=float), np.array(times)


# Heavy tails squashed so one outage (hundreds of failed checks, a 5 s response) doesn't swamp the rest.
SHAPE = {"own_fails_10m": "log", "own_fails_60m": "log", "own_glitches_10m": "log", "own_glitches_60m": "log",
         "upstream_fails_10m": "log", "latency_ms": "log", "segment_age_s": (-60, 300), "age_drift_s": (-120, 300),
         "upstream_age_max_s": (-60, 300), "delivery_ratio": (0, 5)}


def _shape(name: str, v: float) -> float:
    rule = SHAPE.get(name)
    if rule == "log":
        return math.log1p(max(v, 0.0))
    if rule:
        return min(max(v, rule[0]), rule[1])
    return v


def _matrix(rows: List[dict], means: Optional[np.ndarray] = None,
            features: Optional[List[str]] = None) -> Tuple[np.ndarray, np.ndarray]:
    """Rows -> matrix; a missing value becomes the training mean (its column's 'nothing unusual')."""
    features = features or FEATURES
    raw = np.array([[np.nan if r.get(k) is None else _shape(k, float(r[k])) for k in features] for r in rows], dtype=float)
    if means is None:
        with np.errstate(all="ignore"):
            means = np.nan_to_num(np.nanmean(raw, axis=0), nan=0.0) if len(raw) else np.zeros(len(features))
    return np.where(np.isnan(raw), means, raw), means


def auc(scores: np.ndarray, y: np.ndarray) -> Optional[float]:
    pos, neg = scores[y == 1], scores[y == 0]
    if not len(pos) or not len(neg):
        return None
    order = np.argsort(np.concatenate([pos, neg]))
    ranks = np.empty(len(order)); ranks[order] = np.arange(1, len(order) + 1)
    return float((ranks[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def fit(X: np.ndarray, y: np.ndarray, l2: float = 1.0, steps: int = 25) -> Tuple[np.ndarray, float]:
    """Logistic regression by Newton's method (IRLS), positives and negatives weighted equally, L2 on the weights
    (not the intercept). Converges in a few steps for a dozen features."""
    n, k = X.shape
    A = np.hstack([X, np.ones((n, 1))])
    beta = np.zeros(k + 1)
    pos = max(float(y.mean()), 1e-6)
    sw = np.where(y == 1, 0.5 / pos, 0.5 / max(1 - pos, 1e-6))
    reg = np.full(k + 1, l2); reg[-1] = 0.0
    with np.errstate(all="ignore"):
        for _ in range(steps):
            p = 1 / (1 + np.exp(-np.clip(A @ beta, -30, 30)))
            grad = A.T @ (sw * (p - y)) + reg * beta
            H = (A * (sw * p * (1 - p))[:, None]).T @ A + np.diag(reg + 1e-6)
            step = np.linalg.solve(H, grad)
            beta -= step
            if np.abs(step).max() < 1e-6:
                break
    return beta[:-1], float(beta[-1])


def _sigmoid(z):
    return 1 / (1 + np.exp(-np.clip(z, -30, 30)))


def train(path: str, finals: List[dict], upstream_of: Callable[[str], List[str]], now: Optional[float] = None) -> dict:
    """Builds the dataset, trains on the older part, scores on the newest, and stores the result."""
    rows, y, times = build_dataset(path, finals, upstream_of, now=now)
    days = float((times.max() - times.min()) / 86400) if len(times) else 0.0
    # Time of day can only be learned from several days; with less it just memorises when yesterday's outages were.
    features = FEATURES if days >= TIME_OF_DAY_DAYS else [f for f in FEATURES if not f.startswith("hour_")]
    result = {"trained_at": time.time(), "samples": int(len(y)), "positives": int(y.sum()), "features": features,
              "horizon_s": HORIZON_S, "history_days": round(days, 1), "active": False}
    if len(y) < MIN_SAMPLES or y.sum() < MIN_POSITIVES:
        result["note"] = (f"Not enough history yet: {len(y)} samples with {int(y.sum())} glitches or failures "
                          f"(needs {MIN_SAMPLES} and {MIN_POSITIVES}).")
        _save(path, result)
        return result
    order = np.argsort(times)
    cut = int(len(order) * (1 - TEST_SHARE))
    tr, te = order[:cut], order[cut:]
    X, means = _matrix([rows[i] for i in tr], features=features)
    mu, sd = X.mean(axis=0), X.std(axis=0); sd[sd == 0] = 1
    w, b = fit((X - mu) / sd, y[tr])
    X_test, _ = _matrix([rows[i] for i in te], means, features)
    p_test = _sigmoid(((X_test - mu) / sd) @ w + b)
    score = auc(p_test, y[te])
    # The honest bar: the model has to beat the best single signal on its own, not just chance.
    singles = {f: auc(X_test[:, j], y[te]) for j, f in enumerate(features) if not f.startswith("hour_")}
    best_single, best_auc = max(((f, a) for f, a in singles.items() if a is not None), key=lambda x: x[1], default=(None, None))
    threshold = 0.5
    pred = p_test >= threshold
    tp = int((pred & (y[te] == 1)).sum())
    precision, recall = tp / max(int(pred.sum()), 1), tp / max(int(y[te].sum()), 1)
    # The final model learns from everything; the scores above are what it achieved on data it hadn't seen.
    X_all, means = _matrix(rows, features=features)
    mu, sd = X_all.mean(axis=0), X_all.std(axis=0); sd[sd == 0] = 1
    w_all, b_all = fit((X_all - mu) / sd, y)
    active = bool(score is not None and score >= MIN_AUC and y[te].sum() >= 5
                  and (best_auc is None or score >= best_auc - 0.01))
    result.update(weights=w_all.round(4).tolist(), bias=round(b_all, 4), mean=mu.tolist(), std=sd.tolist(),
                  fill=means.tolist(), auc=None if score is None else round(score, 3), threshold=threshold,
                  precision=round(precision, 3), recall=round(recall, 3), test_samples=int(len(te)),
                  base_rate=round(float(y.mean()), 3), best_single=best_single,
                  best_single_auc=None if best_auc is None else round(best_auc, 3), active=active)
    if active:
        result["note"] = (f"Active: on {len(te)} recent samples it hadn't seen, AUC {score:.2f} (0.5 = guessing; best "
                          f"single signal {best_auc:.2f}), precision {precision:.0%}, recall {recall:.0%}.")
    else:
        result["note"] = (f"Trained but not used yet: AUC {score if score is not None else 0:.2f} on recent data it hadn't "
                          f"seen (needs {MIN_AUC} and to beat the best single signal, "
                          f"{WORDS.get(best_single, best_single)} at {best_auc if best_auc is not None else 0:.2f}).")
    _save(path, result)
    return result


def _save(path: str, model: dict) -> None:
    with sqlite3.connect(path, timeout=10) as db:
        db.execute(SCHEMA)
        db.execute("INSERT INTO glitch_model (trained_at, model) VALUES (?, ?)", (model["trained_at"], json.dumps(model)))
        db.execute("DELETE FROM glitch_model WHERE id NOT IN (SELECT id FROM glitch_model ORDER BY id DESC LIMIT 20)")


def load(path: str) -> Optional[dict]:
    with sqlite3.connect(path, timeout=10) as db:
        db.execute(SCHEMA)
        row = db.execute("SELECT model FROM glitch_model ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(row[0]) if row else None


def predict(path: str, model: dict, final: dict, ups: List[str], now: Optional[float] = None) -> Optional[dict]:
    """The model's chance of a glitch or failure on this Final in the next 10 minutes, with the factors pushing
    it up most (in plain words)."""
    if not model or not model.get("active"):
        return None
    now = now or time.time()
    db = sqlite3.connect(path, timeout=10)
    try:
        h = History(db, [final["node"], *ups], [final["url"]], now - 3700)
    finally:
        db.close()
    feats = model.get("features") or FEATURES
    x, _ = _matrix([features_at(h, final, ups, now)], np.array(model["fill"]), feats)
    z = (x[0] - np.array(model["mean"])) / np.array(model["std"])
    contrib = z * np.array(model["weights"])
    p = float(_sigmoid(contrib.sum() + model["bias"]))
    drivers = [WORDS[feats[i]] for i in np.argsort(-contrib) if contrib[i] > 0.3][:3]
    return {"probability": round(p, 3), "drivers": list(dict.fromkeys(drivers))}
