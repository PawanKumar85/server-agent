"""Root-cause ranking over the dependency graph (the MicroRCA idea, with onset times).

When servers fail, which one started it? Three signals are combined:
1. Graph position, by personalized PageRank: walk from each symptom back along FEEDS / PRODUCES towards
   upstream causes, weighted by how anomalous each node is. Mass collects on the upstream node that
   explains the most symptoms.
2. Onset: when each failing URL actually stopped (from its stream timestamps, see spider.merge_url_health).
   The earliest onset in a group is the strongest hint of the first failure.
3. The frontier: a failing node whose own inputs are all healthy is where the failure enters the graph.

Failing and anomalous nodes are grouped into connected components, so independent outages are ranked apart.
"""

from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

ANOMALY_CANDIDATE = 4.0  # an up node joins the ranking when its anomaly score reaches this
DAMPING, ITERATIONS = 0.85, 60


def _ts(value: Optional[str]) -> Optional[float]:
    try:
        return datetime.fromisoformat(value).timestamp() if value else None
    except ValueError:
        return None


def pagerank(nodes: List[str], edges: Iterable[Tuple[str, str, float]], personalization: Dict[str, float]) -> Dict[str, float]:
    """Personalized PageRank by power iteration; edges are (from, to, weight)."""
    out: Dict[str, List[Tuple[str, float]]] = {n: [] for n in nodes}
    for a, b, w in edges:
        if a in out and b in out and w > 0:
            out[a].append((b, w))
    total_p = sum(personalization.values()) or 1.0
    p = {n: personalization.get(n, 0.0) / total_p for n in nodes}
    rank = dict(p)
    for _ in range(ITERATIONS):
        new = {n: (1 - DAMPING) * p[n] for n in nodes}
        for n in nodes:
            targets = out[n]
            if not targets:  # dangling: return to the personalization
                for m in nodes:
                    new[m] += DAMPING * rank[n] * p[m]
                continue
            weight = sum(w for _, w in targets)
            for m, w in targets:
                new[m] += DAMPING * rank[n] * w / weight
        rank = new
    return rank


def components(nodes: Iterable[str], edges: Iterable[Tuple[str, str]]) -> List[List[str]]:
    nodes = list(nodes)
    parent = {n: n for n in nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        if a in parent and b in parent:
            parent[find(a)] = find(b)
    groups: Dict[str, List[str]] = {}
    for n in nodes:
        groups.setdefault(find(n), []).append(n)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: g[0])


def rank(states: Dict[str, dict], edges: List[dict], anomalies: Dict[str, dict],
         priors: Optional[Dict[str, float]] = None) -> List[dict]:
    """states: node -> {"up": bool, "onsetAt": iso or None, "onsetPrecisionS": float or None, "category": str}
    edges: [{"source", "type", "target"}] (source feeds target). priors: node -> factor learned from the
    operator's Right/Wrong verdicts on earlier rankings (learning.Learner.priors; 1.0 = no verdicts).
    Returns one entry per failure group:
    {"nodes": [...], "ranking": [{"node", "score", "onsetAt", "reasons"}]} with the likeliest cause first."""
    score = {n: 10.0 if not s["up"] else float((anomalies.get(n) or {}).get("score") or 0) for n, s in states.items()}
    candidates = [n for n in states if not states[n]["up"] or score[n] >= ANOMALY_CANDIDATE]
    if not any(not states[n]["up"] for n in candidates):
        return []
    links = [(e["source"], e["target"]) for e in edges if e["source"] in states and e["target"] in states]
    upstream = {n: [a for a, b in links if b == n] for n in states}
    downstream = {n: [b for a, b in links if a == n] for n in states}

    def descendants(n: str) -> List[str]:
        seen, stack = set(), list(downstream[n])
        while stack:
            m = stack.pop()
            if m not in seen:
                seen.add(m)
                stack += downstream[m]
        return sorted(seen)

    groups = []
    candidate_links = [(a, b) for a, b in links if a in candidates and b in candidates]
    for group in components(candidates, candidate_links):
        if all(states[n]["up"] for n in group):
            continue  # only anomalies, no failure: that's an early warning, not an incident
        # Walk from symptoms (downstream) back to causes (upstream), preferring anomalous nodes; a self-loop
        # keeps some mass on nodes that are themselves strongly anomalous.
        walk = [(b, a, 0.5 + score[a] / 10) for a, b in candidate_links if a in group and b in group]
        walk += [(n, n, score[n] / 20) for n in group]
        pr = pagerank(group, walk, {n: score[n] / 10 for n in group})
        onsets = {n: _ts(states[n].get("onsetAt")) for n in group if not states[n]["up"]}
        known = [t for t in onsets.values() if t is not None]
        first, span = (min(known), (max(known) - min(known)) or 1.0) if known else (None, 1.0)
        ranking = []
        for n in group:
            s = pr[n]
            reasons = []
            if not states[n]["up"]:
                reasons.append(f"failing ({states[n].get('category') or 'down'})")
            t = onsets.get(n)
            if t is not None and first is not None:
                s *= 1 + 0.6 * (1 - (t - first) / span)
                if t == first and len(known) > 1:
                    reasons.append("stopped first")
            ups = upstream[n]
            if not states[n]["up"] and (not ups or all(states[u]["up"] for u in ups)):
                s *= 1.3
                reasons.append("its inputs are healthy" if ups else "no inputs in the graph")
            affected = [m for m in descendants(n) if not states[m]["up"]]
            if affected:
                reasons.append(f"{len(affected)} failing node(s) downstream")
            prior = (priors or {}).get(n, 1.0)
            if prior != 1.0:
                s *= prior
                reasons.append("confirmed as the root cause before" if prior > 1 else "rejected as the root cause before")
            if states[n]["up"] and score[n] >= ANOMALY_CANDIDATE:
                reasons.append(f"up but anomalous (score {score[n]})")
            ranking.append({"node": n, "score": s, "onsetAt": states[n].get("onsetAt"),
                            "onsetPrecisionS": states[n].get("onsetPrecisionS"), "reasons": reasons})
        total = sum(r["score"] for r in ranking) or 1.0
        for r in ranking:
            r["score"] = round(r["score"] / total, 3)  # share of the group's likelihood
        ranking.sort(key=lambda r: -r["score"])
        groups.append({"nodes": group, "ranking": ranking})
    return groups
