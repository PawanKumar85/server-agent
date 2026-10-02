"""Predictive Failure Engine.

Two complementary scoring methods - no new pip dependencies beyond numpy.

1. LIVE SIGNAL SCORING (every cycle, ~1ms/node)
   - latencyMs z-score vs. node's own 60-sample rolling history
   - jitterMs z-score
   - HLS segmentAge / targetDuration ratio (stream freshness)
   - packet loss
   - blip rate (micro-failure frequency)

2. HISTORICAL PATTERN SCORING (startup + weekly retrain)
   - MTBF  (mean time between failures, seconds)
   - MTTR  (mean time to recovery, seconds)
   - Failure trend: INCREASING / STABLE / DECREASING
   - Hour-of-day heatmap (which UTC hours are riskiest for this node)
   - Weekly outage rate

Blended score = 70% live + 30% historical  =>  band: NOMINAL/LOW/MEDIUM/HIGH/CRITICAL

Neo4j writes per node:
  n.failureRisk   float 0-1
  n.warningRisk   band string
  n.riskReason    human explanation
  n.riskProfile   JSON blob
  n.riskUpdatedAt ISO timestamp

Main entry point: predict_all(driver, nodes)
"""

import json, logging, math, time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

log = logging.getLogger(__name__)

W_LATENCY   = 0.25
W_JITTER    = 0.20
W_FRESHNESS = 0.30
W_LOSS      = 0.15
W_BLIP_RATE = 0.10

RISK_LOW      = 0.25
RISK_MEDIUM   = 0.45
RISK_HIGH     = 0.65
RISK_CRITICAL = 0.82

ROLLING_WINDOW         = 60
MIN_SAMPLES_FOR_ZSCORE = 5

_rolling: Dict[str, Dict[str, List[float]]] = {}


def _push(node_id: str, metric: str, value: Optional[float]) -> None:
    if value is None:
        return
    buf = _rolling.setdefault(node_id, {}).setdefault(metric, [])
    buf.append(value)
    if len(buf) > ROLLING_WINDOW:
        buf.pop(0)


def _zscore_norm(node_id: str, metric: str, current: Optional[float]) -> float:
    if current is None:
        return 0.0
    buf = _rolling.get(node_id, {}).get(metric, [])
    if len(buf) < MIN_SAMPLES_FOR_ZSCORE:
        return 0.0
    arr = np.array(buf)
    mean, std = arr.mean(), arr.std()
    if std < 1e-9:
        return 0.0
    return float(min(abs((current - mean) / std) / 4.0, 1.0))


def score_node_live(node: dict) -> Tuple[float, str]:
    nid   = node.get("id") or node.get("domain") or ""
    lat   = node.get("latency") or node.get("lastLatencyMs")
    jit   = node.get("lastJitterMs")
    loss  = float(node.get("lastPacketLoss") or 0.0)
    pings = max(int(node.get("pingCount") or 0), 1)
    blips = int(node.get("blipCount") or 0)

    _push(nid, "latency", lat)
    _push(nid, "jitter",  jit)
    _push(nid, "loss",    loss)

    lat_score = _zscore_norm(nid, "latency", lat)
    jit_score = _zscore_norm(nid, "jitter",  jit)

    freshness_score = 0.0
    url_health = node.get("urlHealth") or {}
    if isinstance(url_health, str):
        try:
            url_health = json.loads(url_health)
        except (ValueError, TypeError):
            url_health = {}
    for _url, h in url_health.items():
        seg_age = h.get("segmentAgeS")
        tgt_dur = h.get("targetS") or h.get("target_duration_s")
        if seg_age and tgt_dur and tgt_dur > 0:
            ratio = seg_age / tgt_dur
            freshness_score = max(freshness_score, min(ratio / 3.0, 1.0))

    loss_score = min(loss * 10.0, 1.0)
    blip_score = min((blips / pings) * 50.0, 1.0)

    score = (W_LATENCY * lat_score + W_JITTER * jit_score +
             W_FRESHNESS * freshness_score + W_LOSS * loss_score +
             W_BLIP_RATE * blip_score)

    cf = int(node.get("consecutiveFailures") or 0)
    if cf == 1:
        score = min(score + 0.15, 1.0)
    elif cf >= 2:
        score = min(score + 0.35, 1.0)

    signals = {
        "latency spike":    lat_score,
        "jitter spike":     jit_score,
        "stream freshness": freshness_score,
        "packet loss":      loss_score,
        "frequent blips":   blip_score,
    }
    top_name  = max(signals, key=signals.get)
    top_value = signals[top_name]
    if top_value < 0.10:
        reason = "All signals nominal"
    else:
        reason = f"Primary: {top_name} ({round(top_value * 100)}% severity)"
        if cf > 0:
            reason += f"; {cf} consecutive failure(s)"
    return round(score, 4), reason


def build_risk_profile(node_id: str,
                       log_entries: List[dict],
                       incident_stats: Optional[dict]) -> dict:
    outage_times:   List[float] = []
    recovery_times: List[float] = []
    hour_counts = [0] * 24

    for e in log_entries:
        ts_raw = e.get("timestamp") or e.get("ts")
        if not ts_raw:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        etype = (e.get("type") or "").upper()
        if etype == "OUTAGE":
            outage_times.append(ts.timestamp())
            hour_counts[ts.hour] += 1
        elif etype == "RECOVERY":
            recovery_times.append(ts.timestamp())

    stats          = incident_stats or {}
    total_outages  = stats.get("outages", 0) + len(outage_times)
    dominant_error = next(iter(stats.get("errors") or {}), None)

    mtbf_s = None
    if len(outage_times) >= 2:
        gaps   = np.diff(sorted(outage_times))
        mtbf_s = round(float(gaps.mean()), 1)

    mttrs = []
    ot = sorted(outage_times)
    rt = sorted(recovery_times)
    ri = 0
    for o in ot:
        while ri < len(rt) and rt[ri] < o:
            ri += 1
        if ri < len(rt):
            mttrs.append(rt[ri] - o)
            ri += 1
    mttr_s = round(float(np.mean(mttrs)), 1) if mttrs else None

    trend = "UNKNOWN"
    if len(ot) >= 4:
        mid   = len(ot) // 2
        span1 = max(ot[mid - 1] - ot[0], 1)
        span2 = max(ot[-1] - ot[mid], 1)
        ratio = (mid / span1) and ((len(ot) - mid) / span2) / (mid / span1)
        if ratio > 1.3:
            trend = "INCREASING"
        elif ratio < 0.7:
            trend = "DECREASING"
        else:
            trend = "STABLE"

    max_h        = max(hour_counts) or 1
    hour_heatmap = [round(c / max_h, 3) for c in hour_counts]

    if len(outage_times) >= 2:
        span_s      = sorted(outage_times)[-1] - sorted(outage_times)[0]
        weekly_rate = round(total_outages / max(span_s / (7 * 86400), 1.0 / 7), 2)
    else:
        weekly_rate = float(total_outages)

    return {
        "mtbf_s":         mtbf_s,
        "mttr_s":         mttr_s,
        "failure_trend":  trend,
        "dominant_error": dominant_error,
        "hour_heatmap":   hour_heatmap,
        "weekly_rate":    weekly_rate,
    }


def profile_risk_score(profile: dict, current_hour: int) -> Tuple[float, str]:
    score = 0.0
    parts = []

    mtbf = profile.get("mtbf_s")
    if mtbf is not None:
        mtbf_score = max(0.0, 1.0 - math.log1p(mtbf / 600) / math.log1p(86400 / 600))
        score += 0.35 * mtbf_score
        if mtbf_score > 0.4:
            parts.append(f"MTBF {round(mtbf / 60)}min")

    trend = profile.get("failure_trend", "UNKNOWN")
    if trend == "INCREASING":
        score += 0.25
        parts.append("outage frequency increasing")
    elif trend == "STABLE":
        score += 0.05

    heatmap = profile.get("hour_heatmap") or [0] * 24
    if len(heatmap) == 24:
        h_risk = heatmap[current_hour]
        score += 0.20 * h_risk
        if h_risk > 0.6:
            parts.append(f"risky hour {current_hour}:00 UTC")

    wr       = profile.get("weekly_rate") or 0
    wr_score = min(wr / 20.0, 1.0)
    score   += 0.20 * wr_score
    if wr_score > 0.5:
        parts.append(f"{wr:.1f} outages/week")

    reason = "; ".join(parts) if parts else "Historical pattern: low risk"
    return round(min(score, 1.0), 4), reason


def risk_band(score: float) -> str:
    if score >= RISK_CRITICAL: return "CRITICAL"
    if score >= RISK_HIGH:     return "HIGH"
    if score >= RISK_MEDIUM:   return "MEDIUM"
    if score >= RISK_LOW:      return "LOW"
    return "NOMINAL"


def predict_all(driver, nodes: List[dict]) -> List[dict]:
    """Score every node and write results to Neo4j.

    Call from server.py after each completed spider cycle:
        from predictor import predict_all
        results = predict_all(driver, G["nodes"])
        hub.publish("predictions", results)
    """
    now_utc      = datetime.now(timezone.utc)
    current_hour = now_utc.hour
    ts           = now_utc.isoformat(timespec="seconds")

    results: List[dict] = []
    writes:  List[dict] = []

    for node in nodes:
        nid = node.get("id") or node.get("domain")
        if not nid:
            continue

        live_score, live_reason = score_node_live(node)

        log_entries = node.get("log") or []
        if isinstance(log_entries, str):
            try:
                log_entries = json.loads(log_entries)
            except (ValueError, TypeError):
                log_entries = []

        inc_stats = node.get("incidentStats")
        if isinstance(inc_stats, str):
            try:
                inc_stats = json.loads(inc_stats)
            except (ValueError, TypeError):
                inc_stats = None

        profile    = build_risk_profile(nid, log_entries, inc_stats)
        prof_score, prof_reason = profile_risk_score(profile, current_hour)

        blended = round(0.70 * live_score + 0.30 * prof_score, 4)
        band    = risk_band(blended)

        parts = []
        if live_reason and "nominal" not in live_reason.lower():
            parts.append(f"Live: {live_reason}")
        if prof_reason and "low risk" not in prof_reason.lower():
            parts.append(f"History: {prof_reason}")
        reason = " | ".join(parts) if parts else "All signals nominal"

        results.append({
            "id":           nid,
            "liveScore":    live_score,
            "profileScore": prof_score,
            "blendedScore": blended,
            "band":         band,
            "reason":       reason,
            "profile":      profile,
            "updatedAt":    ts,
        })
        writes.append({
            "id":      nid,
            "risk":    blended,
            "band":    band,
            "reason":  reason,
            "profile": json.dumps(profile),
            "ts":      ts,
        })

    if writes:
        try:
            driver.execute_query(
                "UNWIND $rows AS row "
                "MATCH (n:Domain {domain: row.id}) "
                "SET n.failureRisk   = row.risk, "
                "    n.warningRisk   = row.band, "
                "    n.riskReason    = row.reason, "
                "    n.riskProfile   = row.profile, "
                "    n.riskUpdatedAt = row.ts",
                rows=writes,
            )
        except Exception as e:
            log.warning("predictor: Neo4j write failed: %s", e)

    at_risk = [r for r in results if r["band"] in ("HIGH", "CRITICAL")]
    if at_risk:
        log.warning(
            "predictor: %d node(s) at HIGH/CRITICAL risk: %s",
            len(at_risk),
            ", ".join(f"{r['id']}={r['band']}({r['blendedScore']})" for r in at_risk),
        )

    return results
