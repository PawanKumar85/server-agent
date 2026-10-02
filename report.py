"""Complete HTML report of the monitor: every node, or one selected node, with Seaborn/Matplotlib charts.

It shows everything stored in Neo4j, including what the dashboard never displays ("hidden info"): per-URL
health, raw incident logs and archived incident totals, ICMP RTT / jitter / packet loss, consecutive
successes, traceroutes hop by hop, the 384-dim embeddings (norm, nearest neighbours by cosine similarity),
each spider's full RCA (path walked, failover, alert state), and every raw property.

The page is self-contained (charts are inline PNGs), so it can be saved, mailed or printed to PDF.
Charts use Matplotlib's object API (no pyplot state), which is safe in the web server's threads.
"""

import base64
import html
import io
import json
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import seaborn as sns  # noqa: E402
from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

import rca_rank  # noqa: E402
from chatbot import IST, ist  # noqa: E402
from nodes import TEST_TLD, current_topology, load_json_list  # noqa: E402

ROLES = ["MainInput", "BackupLink", "Transcoding", "FinalLink"]
ROLE_NAMES = {"MainInput": "Main", "BackupLink": "Backup", "Transcoding": "Transcoding", "FinalLink": "Final"}
ROLE_COLORS = {"Main": "#2563eb", "Backup": "#7c3aed", "Transcoding": "#d97706", "Final": "#0d9488"}
STATUS_COLORS = {"UP": "#16a34a", "DOWN": "#dc2626", "UNKNOWN": "#9ca3af", "RUNNING": "#16a34a",
                 "STOPPED": "#dc2626", "ERROR": "#ea580c", "RECOVERED": "#0891b2"}
sns.set_theme(style="whitegrid", context="notebook", font_scale=0.9)


# --- data ------------------------------------------------------------------------

def _json(value, default):
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def _native(value):
    """Neo4j temporal values -> ISO strings, recursively; everything else unchanged."""
    if hasattr(value, "iso_format"):
        return value.iso_format()
    if isinstance(value, dict):
        return {k: _native(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_native(v) for v in value]
    return value


def primary_role(roles: List[str]) -> str:
    for role in ["FinalLink", "Transcoding", "MainInput", "BackupLink"]:
        if role in roles:
            return ROLE_NAMES[role]
    return "Other"


def collect(driver, incidents=None) -> Dict[str, Any]:
    """Every :Domain and :SpiderRun with all properties, parsed, plus the relationships; each node's recent
    incidents come from the incident store (metrics.store() by default)."""
    if incidents is None:
        from metrics import store
        incidents = store()
    nodes = []
    for r in driver.execute_query(
        "MATCH (n:Domain) WHERE NOT n.domain ENDS WITH $tld "
        "RETURN properties(n) AS p, [l IN labels(n) WHERE l <> 'Domain'] AS roles ORDER BY n.domain", tld=TEST_TLD
    ).records:
        p = _native(dict(r["p"]))
        roles = sorted(r["roles"], key=lambda x: ROLES.index(x) if x in ROLES else 9)
        nodes.append({
            "id": p.get("domain"), "roles": roles, "role": primary_role(roles), "raw": p,
            "status": p.get("status") or "UNKNOWN",
            "links": load_json_list(p.get("links")), "urlHealth": _json(p.get("urlHealth"), {}),
            "log": incidents.incidents(p.get("domain"), limit=200),
            "incidentStats": _json(p.get("incidentStats"), None), "traceroute": _json(p.get("traceroute"), None),
            "embedding": p.get("embedding") if isinstance(p.get("embedding"), list) else None,
        })
    spiders = []
    for r in driver.execute_query(
        "MATCH (s:SpiderRun) WHERE NOT s.finalLinkId ENDS WITH $tld "
        "OPTIONAL MATCH (s)-[:AT]->(n) RETURN properties(s) AS p, n.domain AS at ORDER BY s.id", tld=TEST_TLD
    ).records:
        p = _native(dict(r["p"]))
        spiders.append({"id": p.get("id"), "raw": p, "at": r["at"], "rca": _json(p.get("rca"), {}),
                        "channel": str(p.get("finalLinkId", "")).split("/")[-1],
                        "embedding": p.get("embedding") if isinstance(p.get("embedding"), list) else None})
    edges = [r.model_dump() for r in current_topology(driver)]
    return {"nodes": nodes, "spiders": spiders, "edges": edges}


def ranking(data: Dict[str, Any]) -> List[dict]:
    """rca_rank over the collected data: each failure group's likely root cause first."""
    states = {}
    for n in data["nodes"]:
        failing = [h for h in n["urlHealth"].values() if h.get("up") is False]
        onset = min(failing, key=lambda h: h.get("onsetAt") or "~", default={})
        cats = [h.get("category") for h in failing if h.get("category")]
        states[n["id"]] = {"up": n["status"] != "DOWN", "onsetAt": onset.get("onsetAt"),
                           "onsetPrecisionS": onset.get("onsetPrecisionS"),
                           "category": max(set(cats), key=cats.count) if cats else None}
    return rca_rank.rank(states, data["edges"], data.get("anomalies") or {})


def channel_table(nodes: List[dict]) -> Dict[str, Dict[str, List[dict]]]:
    """channel -> role -> [{url, node, up, detail, lastDown}]"""
    channels: Dict[str, Dict[str, List[dict]]] = {}
    for n in nodes:
        for link in n["links"]:
            h = n["urlHealth"].get(link["url"], {})
            channels.setdefault(link["channel"], {}).setdefault(link["role"], []).append(
                {"url": link["url"], "node": n["id"], "up": h.get("up"), "detail": h.get("detail"),
                 "lastDown": h.get("lastDown")})
    return dict(sorted(channels.items()))


def feed_of(chain: Dict[str, List[dict]]) -> str:
    if any(e["up"] is not False for e in chain.get("MainInput", [])):
        return "Main"
    if any(e["up"] is not False for e in chain.get("BackupLink", [])):
        return "Backup"
    return "none"


def cosine(a: List[float], b: List[float]) -> float:
    va, vb = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    denom = np.linalg.norm(va) * np.linalg.norm(vb)
    return float(va @ vb / denom) if denom else 0.0


def _ts(value) -> Optional[datetime]:
    if not value:
        return None
    # Neo4j writes nanoseconds ("…00.123456789+00:00"); fromisoformat takes at most microseconds.
    text = re.sub(r"(\.\d{6})\d+", r"\1", str(value).replace("Z", "+00:00"))
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --- findings (computed, no model) --------------------------------------------------

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


def success_rate(n: dict) -> Optional[float]:
    pings, failed = n["raw"].get("pingCount") or 0, n["raw"].get("failedCount") or 0
    return 100 * (1 - failed / pings) if pings else None


def outage_count(n: dict) -> int:
    return (n["incidentStats"] or {}).get("outages", 0) + sum(e.get("type") == "OUTAGE" for e in n["log"])


def insights(data: Dict[str, Any], monitor: Optional[dict] = None, node_id: Optional[str] = None) -> List[dict]:
    """Ranked findings from the report data: {severity, area, finding, evidence, action}. For one node, only
    what concerns that node and its channels."""
    from embeddings import _error_kind

    nodes, spiders = data["nodes"], data["spiders"]
    channels = channel_table(nodes)
    scope = [n for n in nodes if n["id"] == node_id] if node_id else nodes
    scope_channels = {l["channel"] for n in scope for l in n["links"]} if node_id else set(channels)
    interval = (monitor or {}).get("interval") or 30
    now = datetime.now(timezone.utc)
    out: List[dict] = []

    def add(severity, area, finding, evidence, action):
        out.append({"severity": severity, "area": area, "finding": finding, "evidence": evidence, "action": action})

    for n in scope:
        raw = n["raw"]
        if n["status"] == "DOWN":
            add("high", "Availability", f"{n['id']} is DOWN now",
                f"{raw.get('consecutiveFailures') or 0} failures in a row; last error: {raw.get('lastError') or '—'}",
                "Check the origin/stream on this server; if it is a Main input, fail over to its Backup.")
        rate = success_rate(n)
        if rate is not None and rate < 99:
            add("high" if rate < 95 else "medium", "Reliability", f"{n['id']} passes only {rate:.1f}% of checks",
                f"{raw.get('failedCount')} of {raw.get('pingCount')} checks failed",
                "Look at this server's incident history for a recurring cause (see Errors).")
        outages = outage_count(n)
        if outages >= 3:
            errors = Counter()
            for kind, c in ((n["incidentStats"] or {}).get("errors") or {}).items():
                errors[kind] += c
            for e in n["log"]:
                if e.get("type") == "OUTAGE" and _error_kind(e.get("lastError")):
                    errors[_error_kind(e.get("lastError"))] += 1
            top = ", ".join(f"{k} ×{v}" for k, v in errors.most_common(3)) or "unknown"
            add("medium", "Stability", f"{n['id']} flaps: {outages} outages recorded", f"most common errors: {top}",
                "A recurring error on one server is usually the encoder or packager; fix the source, not the symptom.")
        latency = raw.get("lastLatencyMs")
        if latency is not None and latency > 300:
            add("medium" if latency > 1000 else "low", "Performance", f"{n['id']} is slow: {latency:.0f} ms HTTP latency",
                f"ICMP RTT {raw.get('lastRttMs')} ms, jitter {raw.get('lastJitterMs')} ms",
                "High HTTP time with normal RTT points at the server (disk/CPU/origin), not the network.")
        jitter = raw.get("lastJitterMs")
        if jitter is not None and jitter > 50:
            add("low", "Network", f"{n['id']} has high jitter: {jitter:.0f} ms", f"RTT {raw.get('lastRttMs')} ms",
                "Unstable path; watch for packet loss or check the traceroute.")
        loss = raw.get("lastPacketLoss")
        if loss:
            # Many servers rate-limit ping; loss only matters much when the stream checks fail too.
            add("medium" if n["status"] == "DOWN" else "low", "Network",
                f"{n['id']} loses ICMP packets: {loss * 100 if loss <= 1 else loss:.0f}%",
                f"RTT {raw.get('lastRttMs')} ms; HTTP checks {'fail' if n['status'] == 'DOWN' else 'pass (likely ping rate-limiting)'}",
                "Run a traceroute on this server to find where packets drop.")
        last = _ts(raw.get("lastPing"))
        if last and (now - last).total_seconds() > max(3 * interval, 180):
            add("high", "Monitoring", f"{n['id']} has not been checked for {int((now - last).total_seconds() // 60)} min",
                f"last ping {ist(last.isoformat())}", "The spiders no longer reach it; check the topology links and the scheduler.")
        tr = n["traceroute"]
        if tr and tr.get("drop_hop") is not None:
            add("high" if n["status"] == "DOWN" else "low", "Network", f"Traceroute to {n['id']} drops at hop {tr['drop_hop']}",
                tr.get("summary", ""), "Run the AI Root Cause Analysis on this node for who owns that hop.")

    from metrics import warnings as anomaly_warnings
    for group in ranking(data):
        top = group["ranking"][0]
        if node_id and node_id not in group["nodes"]:
            continue
        add("high", "Root cause (ranked)", f"Likely root cause: {top['node']} ({round(top['score'] * 100)}% of this failure)",
            "; ".join(top["reasons"]) + (f"; stopped {ist(top['onsetAt'])} (±{top['onsetPrecisionS']}s)" if top.get("onsetAt") else ""),
            "Start the investigation here; the other failing nodes in this group are most likely its effects.")
    for n in scope:
        found = anomaly_warnings((data.get("anomalies") or {}).get(n["id"]) or {})
        if found:
            add("medium", "Early warning", f"{n['id']} is drifting from its normal while still up", "; ".join(found),
                "Look at it before it fails: compare with its history chart below.")

    verdicts = Counter()
    for n in scope:
        for e in n["log"]:
            for corr in e.get("correlation") or []:
                verdicts[(corr.get("verdict"), corr.get("channel"), n["id"])] += 1
        blips = n["raw"].get("blipCount") or 0
        if blips >= 5:
            add("low", "Stability", f"{n['id']} had {blips} short blips",
                "failures that recovered before becoming an incident", "Frequent blips often come before real outages.")
    for (kind, channel, node), count in verdicts.items():
        if kind == "SHARED_UPSTREAM":
            add("high" if count >= 2 else "medium", "Root cause",
                f"Every input of {channel} failed together ({count}×)", f"seen from {node}'s incidents",
                "The inputs share an upstream source; fix or diversify that source, not the ingest servers.")
        elif kind == "LOCAL_TO_NODE":
            add("medium" if count >= 2 else "low", "Root cause", f"{node}'s input for {channel} failed on its own ({count}×)",
                "the other input stayed healthy", f"Look at {node}'s HLS producer or the feed into it (needs access on the server).")
        elif kind in ("TRANSCODER", "FINAL_ORIGIN"):
            add("high", "Root cause", f"{node} failed while everything upstream of it was healthy ({count}×)",
                f"channel {channel}", "The problem is on this server's own processing (transcoder/packager).")

    for name in sorted(scope_channels):
        chain = channels.get(name, {})
        if not chain:
            continue
        feed = feed_of(chain)
        if any(e["up"] is False for e in chain.get("FinalLink", [])) or feed == "none":
            add("high", "Channels", f"Channel {name} is down", f"feed: {feed}", "Follow its spider's root cause.")
        elif feed == "Backup":
            add("medium", "Channels", f"Channel {name} runs on its Backup", "Main input is down",
                "Restore the Main input before the Backup also fails.")
        if not chain.get("BackupLink"):
            add("medium", "Redundancy", f"Channel {name} has no Backup input",
                f"Main: {', '.join(e['node'] for e in chain.get('MainInput', [])) or '—'}",
                "Add a BackupLink so a Main failure doesn't take the channel off air.")
        else:
            same = {e["node"] for e in chain.get("MainInput", [])} & {e["node"] for e in chain.get("BackupLink", [])}
            if same:
                add("medium", "Redundancy", f"Channel {name}'s Main and Backup are on the same server",
                    f"both on {', '.join(sorted(same))}",
                    "One server failure takes both inputs; move the Backup to a different server.")

    for n in scope:
        count = len({l["channel"] for l in n["links"]})
        if count >= 3 and "FinalLink" not in n["roles"]:
            add("low", "Redundancy", f"{n['id']} carries {count} channels",
                ", ".join(sorted({l['channel'] for l in n['links']})),
                "Its failure affects all of them at once (large blast radius); make sure each has a Backup elsewhere.")

    for sp in spiders:
        if node_id and not (sp["raw"].get("stopNodeId") == node_id or sp["at"] == node_id):
            continue
        status = sp["raw"].get("status")
        if status in ("STOPPED", "ERROR"):
            add("high", "Spiders", f"Spider for {sp['channel']} is {status}",
                f"root cause: {sp['rca'].get('root_cause') or sp['raw'].get('stopReason') or '—'}; {sp['rca'].get('impact') or ''}",
                "Fix the root cause node; the spider re-checks every cycle.")

    errors = error_counts(scope)
    if errors:
        kind, count = errors.most_common(1)[0]
        add("info", "Errors", f"Most common failure: {kind} ({count} outages)",
            ", ".join(f"{k} ×{v}" for k, v in errors.most_common(5)), "Prioritise fixing this failure type.")
    if not any(f["severity"] in ("high", "medium") for f in out):
        add("info", "Summary", "No current outages or serious risks", f"{len(scope)} server(s) checked",
            "Keep monitoring.")
    return sorted(out, key=lambda f: SEVERITY_ORDER[f["severity"]])


def findings_html(items: List[dict]) -> str:
    colors = {"high": "#dc2626", "medium": "#f59e0b", "low": "#3b82f6", "info": "#6b7080"}
    counts = Counter(f["severity"] for f in items)
    head = " · ".join(f"<b style='color:{colors[k]}'>{counts[k]} {k}</b>" for k in ("high", "medium", "low", "info") if counts[k])
    rows = "".join(
        f"<tr><td><span class='badge' style='--c:{colors[f['severity']]}'>{_e(f['severity'])}</span></td>"
        f"<td>{_e(f['area'])}</td><td><b>{_e(f['finding'])}</b><div class='small muted'>{_e(f['evidence'])}</div></td>"
        f"<td class='small'>{_e(f['action'])}</td></tr>" for f in items)
    return (f"<h2 id='findings'>Key findings</h2><p class='muted'>Computed from the data (no model): {head}</p>"
            f"<table class='grid'><tr><th></th><th>Area</th><th>Finding</th><th>Action</th></tr>{rows}</table>")


def analysis_html(node_id: Optional[str], live: bool) -> str:
    """The AI deep-analysis section: a button that streams it in (only on the live page; a downloaded file
    has no server to ask)."""
    if not live:
        return ""
    target = json.dumps(node_id or "")
    return f"""<h2 id='analysis'>AI deep analysis</h2>
<div class='noprint-empty' id='ai-box'><p class='muted'>A model reads this report's data and the findings above and writes
a deeper analysis: risks, performance, incident patterns, redundancy and prioritised recommendations.</p>
<button class='ai-btn' id='ai-run'>🧠 Deep analysis</button></div>
<div id='ai-out' class='ai-out'></div>
<script>
(() => {{
  const node = {target}, btn = document.getElementById("ai-run"), out = document.getElementById("ai-out");
  const esc = s => String(s).replace(/[&<>]/g, c => ({{"&": "&amp;", "<": "&lt;", ">": "&gt;"}}[c]));
  const md = t => esc(t).split("\\n").map(l => /^#{{1,6}}\\s/.test(l) ? `<h4>${{l.replace(/^#+\\s*/, "")}}</h4>` :
    /^\\s*([-*]|\\d+[.)])\\s+/.test(l) ? `<li>${{l.replace(/^\\s*([-*]|\\d+[.)])\\s+/, "")}}</li>` : l.trim() ? `<p>${{l}}</p>` : "")
    .join("").replace(/\\*\\*([^*]+)\\*\\*/g, "<b>$1</b>");
  btn.onclick = async () => {{
    btn.disabled = true; btn.textContent = "Analysing…"; out.innerHTML = "";
    let text = "", meta = "";
    try {{
      const res = await fetch("/api/report/analysis" + (node ? "?node=" + encodeURIComponent(node) : ""), {{method: "POST"}});
      if (!res.ok) throw new Error((await res.json().catch(() => ({{}}))).detail || res.statusText);
      const reader = res.body.getReader(), dec = new TextDecoder(); let buf = "";
      for (;;) {{
        const {{value, done}} = await reader.read();
        buf += dec.decode(value || new Uint8Array(), {{stream: !done}});
        const lines = buf.split("\\n"); buf = lines.pop();
        for (const line of lines.filter(Boolean)) {{
          const e = JSON.parse(line);
          if (e.type === "token") text += e.text;
          if (e.type === "error") throw new Error(e.message);
          if (e.type === "done") meta = `${{e.model}} · ${{(e.elapsed_ms / 1000).toFixed(1)}} s` +
            (e.tokens ? ` · ${{e.tokens.input}} in / ${{e.tokens.output}} out tokens` : "");
        }}
        out.innerHTML = md(text) + (meta ? `<p class="muted small">${{esc(meta)}} · written by a model from the data above; check it against the findings.</p>` : "");
        if (done) break;
      }}
      btn.textContent = "🧠 Analyse again";
    }} catch (err) {{
      out.innerHTML += `<p class="bad">${{esc(err.message)}}</p>`; btn.textContent = "🧠 Deep analysis";
    }}
    btn.disabled = false;
  }};
}})();
</script>"""


# --- charts ----------------------------------------------------------------------

def _png(fig: Figure) -> str:
    FigureCanvasAgg(fig)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    return base64.b64encode(buf.getvalue()).decode()


def _fig(w: float, h: float) -> Figure:
    return Figure(figsize=(w, h), layout="constrained")


def _short(node_id: str) -> str:
    return node_id.replace(".ottlive.co.in", "").replace(".co.in", "")


def chart_status_overview(nodes, spiders) -> str:
    fig = _fig(10, 3.2)
    ax1, ax2, ax3 = fig.subplots(1, 3)
    for ax, counts, title in [
        (ax1, Counter(n["status"] for n in nodes), "Servers by status"),
        (ax2, Counter(s["raw"].get("status", "?") for s in spiders), "Spiders by status"),
        (ax3, Counter(n["role"] for n in nodes), "Servers by main role"),
    ]:
        labels = list(counts)
        colors = [STATUS_COLORS.get(k, ROLE_COLORS.get(k, "#9ca3af")) for k in labels]
        ax.pie(list(counts.values()), labels=[f"{k} ({v})" for k, v in counts.items()], colors=colors,
               wedgeprops={"width": 0.45, "edgecolor": "white"}, startangle=90)
        ax.set_title(title)
    return _png(fig)


def chart_reliability(nodes) -> str:
    rows = []
    for n in nodes:
        pings, failed = n["raw"].get("pingCount") or 0, n["raw"].get("failedCount") or 0
        rows.append({"node": _short(n["id"]), "role": n["role"], "success %": 100 * (1 - failed / pings) if pings else 0,
                     "pings": pings, "failed": failed})
    df = pd.DataFrame(rows).sort_values("success %")
    fig = _fig(10, 0.32 * len(df) + 1.2)
    ax = fig.subplots()
    sns.barplot(data=df, y="node", x="success %", hue="role", palette=ROLE_COLORS, dodge=False, ax=ax)
    low = max(0.0, df["success %"].min() - 5)
    for i, (_, r) in enumerate(df.iterrows()):
        ax.text(r["success %"] - (100 - low) * 0.01, i, f"{r['success %']:.1f}%  ({r['failed']}/{r['pings']} failed)",
                va="center", ha="right", fontsize=8, color="white", fontweight="bold")
    ax.set_xlim(low, 100)
    ax.set_title("Ping success rate per server (all checks since monitoring started)")
    ax.set_ylabel("")
    ax.legend(title="role", loc="upper left", bbox_to_anchor=(1.01, 1))
    return _png(fig)


def chart_latency(nodes) -> str:
    rows = []
    for n in nodes:
        raw = n["raw"]
        for metric, key in [("HTTP latency", "lastLatencyMs"), ("ICMP RTT", "lastRttMs"), ("ICMP jitter", "lastJitterMs")]:
            if raw.get(key) is not None:
                rows.append({"node": _short(n["id"]), "metric": metric, "ms": float(raw[key])})
    if not rows:
        return ""
    df = pd.DataFrame(rows)
    order = df[df.metric == "HTTP latency"].sort_values("ms", ascending=False)["node"].tolist() or None
    fig = _fig(10, 0.36 * df["node"].nunique() + 1.4)
    ax = fig.subplots()
    sns.barplot(data=df, y="node", x="ms", hue="metric", order=order, palette="viridis", ax=ax)
    ax.axvline(150, color="#16a34a", ls=":", lw=1)
    ax.axvline(300, color="#dc2626", ls=":", lw=1)
    ax.set_title("Latest latency per server: HTTP check vs ICMP round-trip and jitter (dotted: 150 / 300 ms)")
    ax.set_ylabel("")
    return _png(fig)


def chart_packet_loss(nodes) -> str:
    df = pd.DataFrame([{"node": _short(n["id"]), "loss %": float(n["raw"].get("lastPacketLoss") or 0) * (
        100 if (n["raw"].get("lastPacketLoss") or 0) <= 1 else 1), "status": n["status"]} for n in nodes])
    fig = _fig(10, 2.8)
    ax = fig.subplots()
    sns.barplot(data=df, x="node", y="loss %", hue="status", palette=STATUS_COLORS, dodge=False, ax=ax)
    ax.set_ylim(0, 100)
    ax.set_title("Latest ICMP packet loss per server")
    ax.set_xlabel("")
    ax.tick_params(axis="x", rotation=40)
    for label in ax.get_xticklabels():
        label.set_ha("right")
    return _png(fig)


def chart_channel_health(channels) -> str:
    matrix, annot = [], []
    for name, chain in channels.items():
        row, arow = [], []
        for role in ROLES:
            entries = chain.get(role, [])
            if not entries:
                row.append(np.nan)
                arow.append("—")
                continue
            up = sum(e["up"] is not False for e in entries)
            row.append(up / len(entries))
            arow.append(f"{up}/{len(entries)} up")
        matrix.append(row)
        annot.append(arow)
    df = pd.DataFrame(matrix, index=list(channels), columns=[ROLE_NAMES[r] for r in ROLES])
    fig = _fig(8, 0.42 * len(df) + 1.3)
    ax = fig.subplots()
    sns.heatmap(df, annot=np.array(annot), fmt="", cmap=sns.color_palette(["#dc2626", "#f59e0b", "#16a34a"], as_cmap=True),
                vmin=0, vmax=1, linewidths=1, linecolor="white", cbar=False, ax=ax, annot_kws={"fontsize": 8})
    ax.set_title("Channel health: URLs up per role (— = role not used)")
    ax.set_ylabel("channel")
    return _png(fig)


def chart_incident_timeline(nodes) -> str:
    rows = []
    for n in nodes:
        for e in n["log"]:
            t = _ts(e.get("timestamp"))
            if t and e.get("type") in ("OUTAGE", "RECOVERY"):
                rows.append({"node": _short(n["id"]), "time": t.astimezone(IST).replace(tzinfo=None), "event": e["type"]})
        st = n["incidentStats"] or {}
        for key, label in [("lastOutageAt", "OUTAGE (archived, last)"), ("firstIncidentAt", "first incident (archived)")]:
            t = _ts(st.get(key))
            if t:
                rows.append({"node": _short(n["id"]), "time": t.astimezone(IST).replace(tzinfo=None), "event": label})
    if not rows:
        return ""
    df = pd.DataFrame(rows)
    fig = _fig(10, 0.4 * df["node"].nunique() + 1.6)
    ax = fig.subplots()
    palette = {"OUTAGE": "#dc2626", "RECOVERY": "#16a34a", "OUTAGE (archived, last)": "#fca5a5",
               "first incident (archived)": "#9ca3af"}
    markers = {"OUTAGE": "X", "RECOVERY": "o", "OUTAGE (archived, last)": "X", "first incident (archived)": "D"}
    sns.scatterplot(data=df, x="time", y="node", hue="event", style="event", palette=palette, markers=markers,
                    s=70, ax=ax)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b %H:%M"))
    ax.set_title("Incident timeline (IST): raw log entries and archived totals")
    ax.set_ylabel("")
    ax.set_xlabel("")
    ax.legend(loc="upper left", bbox_to_anchor=(1, 1), fontsize=8)
    return _png(fig)


def error_counts(nodes) -> Counter:
    from embeddings import _error_kind
    counts: Counter = Counter()
    for n in nodes:
        for kind, c in ((n["incidentStats"] or {}).get("errors") or {}).items():
            counts[kind] += c
        for e in n["log"]:
            if e.get("type") == "OUTAGE":
                kind = _error_kind(e.get("lastError"))
                if kind:
                    counts[kind] += 1
    return counts


def chart_errors(nodes) -> str:
    counts = error_counts(nodes)
    if not counts:
        return ""
    df = pd.DataFrame(counts.most_common(10), columns=["error", "outages"])
    fig = _fig(9, 0.4 * len(df) + 1.2)
    ax = fig.subplots()
    sns.barplot(data=df, y="error", x="outages", color="#dc2626", ax=ax)
    ax.set_title("What the outages were: error kinds across all servers (raw + archived)")
    ax.set_ylabel("")
    return _png(fig)


def chart_embedding_similarity(items: List[dict], title: str) -> str:
    items = [i for i in items if i.get("embedding")]
    if len(items) < 2:
        return ""
    names = [_short(i["id"]) for i in items]
    m = np.array([[cosine(a["embedding"], b["embedding"]) for b in items] for a in items])
    fig = _fig(0.42 * len(items) + 3, 0.38 * len(items) + 2)
    ax = fig.subplots()
    sns.heatmap(pd.DataFrame(m, index=names, columns=names), cmap="mako", vmin=0, vmax=1, square=True,
                annot=len(items) <= 16, fmt=".2f", annot_kws={"fontsize": 6}, cbar_kws={"shrink": 0.6}, ax=ax)
    ax.set_title(title)
    return _png(fig)


def chart_spiders(spiders) -> str:
    if not spiders:
        return ""
    df = pd.DataFrame([{"spider": s["channel"], "steps": s["raw"].get("stepCount") or 0,
                        "status": s["raw"].get("status", "?")} for s in spiders])
    fig = _fig(9, 2.8)
    ax = fig.subplots()
    sns.barplot(data=df, x="spider", y="steps", hue="status", palette=STATUS_COLORS, dodge=False, ax=ax)
    ax.set_title("Spider hops walked since start, by channel")
    ax.set_xlabel("")
    return _png(fig)


def chart_topology(nodes, edges) -> str:
    column = {"Main": 0, "Backup": 0, "Transcoding": 1, "Final": 2, "Other": 1}
    by_col: Dict[int, List[dict]] = {}
    for n in nodes:
        by_col.setdefault(column[n["role"]], []).append(n)
    pos = {}
    for col, members in by_col.items():
        members.sort(key=lambda n: (n["role"] != "Main", n["id"]))
        for i, n in enumerate(members):
            pos[n["id"]] = (col, -(i - (len(members) - 1) / 2))
    height = max(len(m) for m in by_col.values()) if by_col else 1
    fig = _fig(11, 0.55 * height + 1.5)
    ax = fig.subplots()
    for e in edges:
        if e["source"] in pos and e["target"] in pos:
            (x1, y1), (x2, y2) = pos[e["source"]], pos[e["target"]]
            ax.annotate("", xy=(x2 - 0.12, y2), xytext=(x1 + 0.12, y1),
                        arrowprops={"arrowstyle": "-|>", "color": "#16a34a" if e["type"] == "PRODUCES" else "#64748b",
                                    "lw": 1, "shrinkA": 0, "shrinkB": 0, "alpha": 0.8})
    for n in nodes:
        x, y = pos[n["id"]]
        ax.scatter([x], [y], s=260, color=ROLE_COLORS.get(n["role"], "#9ca3af"),
                   edgecolor=STATUS_COLORS.get(n["status"], "#9ca3af"), linewidth=3, zorder=3)
        ax.text(x, y - 0.32, _short(n["id"]), ha="center", va="top", fontsize=7)
    ax.set_xticks([0, 1, 2], ["Main / Backup", "Transcoding", "Final"])
    ax.set_yticks([])
    ax.set_xlim(-0.5, 2.5)
    ax.set_ylim(-(height / 2) - 0.8, height / 2 + 0.3)
    ax.grid(False)
    ax.set_title("Topology: FEEDS (grey) and PRODUCES (green); fill = role, ring = status")
    return _png(fig)


def chart_history(history: Dict[str, List[dict]], metric: str, title: str, unit: str) -> str:
    """One line per server from the per-check time series (IST); red ticks where a server was down."""
    rows = [{"time": datetime.fromtimestamp(r["ts"], IST).replace(tzinfo=None), "server": _short(node), "value": r[metric],
             "down": not r["up"]} for node, series in history.items() for r in series if r[metric] is not None]
    if len(rows) < 4:
        return ""
    df = pd.DataFrame(rows)
    fig = _fig(10, 3.6)
    ax = fig.subplots()
    sns.lineplot(data=df, x="time", y="value", hue="server", ax=ax, linewidth=1.2, legend=df["server"].nunique() <= 16)
    down = df[df["down"]]
    if not down.empty:
        ax.scatter(down["time"], down["value"], color="#dc2626", marker="|", s=80, zorder=3, label="down")
    if not df.empty and (df["time"].max() - df["time"].min()).total_seconds() > 86400:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d, %H:%M"))
    else:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.set_title(title)
    ax.set_xlabel("")
    ax.set_ylabel(unit)
    if ax.get_legend():
        ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1), fontsize=7)
    return _png(fig)


def ranking_html(groups: List[dict]) -> str:
    if not groups:
        return ""
    parts = ["<h2 id='ranking'>Root-cause ranking</h2><p class='muted'>Each group of failing servers, most likely "
             "cause first: graph position (personalized PageRank over FEEDS/PRODUCES, weighted by anomalies), who "
             "stopped first (from the streams' own timestamps) and whether the server's inputs are healthy.</p>"]
    for g in groups:
        rows = "".join(f"<tr><td><b>{_e(r['node'])}</b></td><td>{round(r['score'] * 100)}%</td>"
                       f"<td>{_time(r.get('onsetAt'))}{' ±' + str(r['onsetPrecisionS']) + 's' if r.get('onsetPrecisionS') else ''}</td>"
                       f"<td class='small'>{_e('; '.join(r['reasons']))}</td></tr>" for r in g["ranking"])
        parts.append(f"<table class='grid'><tr><th>Server</th><th>Likelihood</th><th>Stopped</th><th>Why</th></tr>{rows}</table>")
    return "".join(parts)


def chart_traceroute(trace: dict) -> str:
    hops = trace.get("hops") or []
    if not hops:
        return ""
    df = pd.DataFrame([{"hop": h["hop"], "rtt": h.get("rtt_ms"), "ip": h.get("ip") or "*",
                        "status": "drop" if h.get("drop") else h.get("status")} for h in hops])
    palette = {"ok": "#16a34a", "fair": "#3b82f6", "slow": "#f59e0b", "timeout": "#cbd5e1", "drop": "#dc2626"}
    fig = _fig(10, 3.2)
    ax = fig.subplots()
    plot = df.assign(rtt=df["rtt"].fillna(0))
    sns.barplot(data=plot, x="hop", y="rtt", hue="status", palette=palette, dodge=False, ax=ax)
    for i, r in df.iterrows():
        ax.text(i, (r["rtt"] or 0) + 1, r["ip"], rotation=90, ha="center", va="bottom", fontsize=7)
    ax.set_ylabel("avg RTT (ms)")
    ax.set_title(f"Traceroute to {trace.get('host')}: {trace.get('summary', '')}")
    ax.set_ylim(0, max(10, (df["rtt"].max() or 0) * 1.6))
    return _png(fig)


def chart_node_incidents(node: dict) -> str:
    rows = []
    for e in node["log"]:
        t = _ts(e.get("timestamp"))
        if t:
            rows.append({"time": t.astimezone(IST).replace(tzinfo=None), "event": e.get("type"),
                         "latency ms": e.get("lastLatencyMs"), "consecutive failures": e.get("consecutiveFailures") or 0})
    if not rows:
        return ""
    df = pd.DataFrame(rows)
    fig = _fig(10, 3)
    ax1, ax2 = fig.subplots(1, 2)
    # Every event type the log can hold gets a colour (an unknown one grey), or seaborn refuses to draw.
    colours = {"OUTAGE": "#dc2626", "ESCALATED": "#f59e0b", "RECOVERY": "#16a34a"}
    palette = {event: colours.get(event, "#9ca3af") for event in df["event"].fillna("unknown").unique()}
    sns.scatterplot(data=df.assign(event=df["event"].fillna("unknown")), x="time", y="consecutive failures",
                    hue="event", palette=palette, s=70, ax=ax1)
    sns.lineplot(data=df.dropna(subset=["latency ms"]), x="time", y="latency ms", marker="o", color="#2563eb", ax=ax2)
    for ax in (ax1, ax2):
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b\n%H:%M"))
        ax.set_xlabel("")
    ax1.set_title("Incidents (failures in a row at each event)")
    ax2.set_title("HTTP latency recorded at each incident")
    return _png(fig)


def chart_neighbours(node: dict, others: List[dict]) -> str:
    if not node.get("embedding"):
        return ""
    sims = sorted(((cosine(node["embedding"], o["embedding"]), _short(o["id"]))
                   for o in others if o.get("embedding") and o["id"] != node["id"]), reverse=True)[:8]
    if not sims:
        return ""
    df = pd.DataFrame(sims, columns=["cosine similarity", "node"])
    fig = _fig(8, 0.36 * len(df) + 1.2)
    ax = fig.subplots()
    sns.barplot(data=df, y="node", x="cosine similarity", color="#0d9488", ax=ax)
    ax.set_xlim(0, 1)
    ax.set_title("Most similar servers by embedding (role, status, errors and incident history)")
    ax.set_ylabel("")
    return _png(fig)


# --- HTML ------------------------------------------------------------------------

def _e(value) -> str:
    return html.escape("" if value is None else str(value))


def _img(b64: str, alt: str) -> str:
    return f'<figure><img alt="{_e(alt)}" src="data:image/png;base64,{b64}"></figure>' if b64 else ""


def _time(value) -> str:
    return _e(ist(value) or "—")


def _kv(rows: List[tuple]) -> str:
    return "<table class='kv'>" + "".join(f"<tr><th>{_e(k)}</th><td>{v}</td></tr>" for k, v in rows) + "</table>"


def _badge(status: Optional[str]) -> str:
    s = status or "UNKNOWN"
    return f"<span class='badge' style='--c:{STATUS_COLORS.get(s, '#9ca3af')}'>{_e(s)}</span>"


def _up(up) -> str:
    return "<span class='ok'>✓ up</span>" if up is not False else "<span class='bad'>✗ down</span>"


def _embedding_block(vec: Optional[List[float]]) -> str:
    if not vec:
        return "—"
    arr = np.asarray(vec, dtype=float)
    return (f"{len(arr)} dims · L2 norm {np.linalg.norm(arr):.4f} · non-zero {int(np.count_nonzero(arr))} · "
            f"min {arr.min():.4f} · max {arr.max():.4f}<div class='mono small'>[{', '.join(f'{x:.4f}' for x in arr[:12])}, …]</div>")


def _raw_block(title: str, data: dict) -> str:
    shown = {k: (f"<{len(v)}-dim vector>" if k == "embedding" and isinstance(v, list) else v) for k, v in data.items()}
    return (f"<details><summary>{_e(title)}</summary><pre>{_e(json.dumps(shown, indent=2, default=str))}</pre>"
            "</details>")


def node_section(n: dict, data: dict, full: bool) -> str:
    raw = n["raw"]
    ups = [f"{_e(e['source'])} <span class='muted'>{e['type']}</span>" for e in data["edges"] if e["target"] == n["id"]]
    downs = [f"{_e(e['target'])} <span class='muted'>{e['type']}</span>" for e in data["edges"] if e["source"] == n["id"]]
    at_here = [s for s in data["spiders"] if s["at"] == n["id"]]
    stopped_here = [s for s in data["spiders"] if s["raw"].get("stopNodeId") == n["id"]]
    pings, failed = raw.get("pingCount") or 0, raw.get("failedCount") or 0
    parts = [f"<section class='node' id='node-{_e(n['id'])}'><h3>{_badge(n['status'])} {_e(n['id'])}</h3>",
             f"<p class='muted'>{' · '.join(ROLE_NAMES.get(r, r) for r in n['roles'])} · IP {_e(raw.get('server_ip') or '—')}</p>"]
    parts.append("<div class='grid2'>" + _kv([
        ("Status", _badge(n["status"])),
        ("Pings / failed", f"{pings} / {failed} ({100 * (1 - failed / pings):.2f}% success)" if pings else "—"),
        ("Failures in a row", _e(raw.get("consecutiveFailures"))),
        ("Successes in a row", _e(raw.get("consecutiveSuccesses"))),
        ("Blips", f"{_e(raw.get('blipCount') or 0)} <span class='muted'>(failures too short to be an incident)</span>"),
        ("HTTP latency", f"{_e(raw.get('lastLatencyMs'))} ms"),
        ("Last ping", _time(raw.get("lastPing"))),
        ("Last recovery", _time(raw.get("lastRecovery"))),
    ]) + _kv([
        ("ICMP RTT", f"{_e(raw.get('lastRttMs'))} ms"),
        ("ICMP jitter", f"{_e(raw.get('lastJitterMs'))} ms"),
        ("ICMP packet loss", _e(raw.get("lastPacketLoss"))),
        ("Last error", f"<span class='bad'>{_e(raw.get('lastError'))}</span>" if raw.get("lastError") else "—"),
        ("Upstream", "<br>".join(ups) or "—"),
        ("Downstream", "<br>".join(downs) or "—"),
        ("Spiders here", ", ".join(f"{_e(s['id'])} ({_e(s['raw'].get('status'))})" for s in at_here) or "—"),
    ]) + "</div>")

    rows = "".join(
        f"<tr><td>{_up(n['urlHealth'].get(l['url'], {}).get('up'))}</td><td class='mono'>{_e(l['url'])}</td>"
        f"<td>{_e(ROLE_NAMES.get(l['role'], l['role']))}</td><td>{_e(l['channel'])}</td>"
        f"<td>{_e(n['urlHealth'].get(l['url'], {}).get('detail'))}</td>"
        f"<td>{_time(n['urlHealth'].get(l['url'], {}).get('lastDown'))}</td></tr>" for l in n["links"])
    parts.append("<h4>URLs and per-URL health <span class='hidden-tag'>hidden in dashboard</span></h4>"
                 "<table class='grid'><tr><th></th><th>URL</th><th>Role</th><th>Channel</th><th>Detail</th>"
                 f"<th>Last down</th></tr>{rows}</table>")

    st = n["incidentStats"]
    if st and (st.get("archivedEntries") or st.get("outages")):
        errors = ", ".join(f"{_e(k)} ×{v}" for k, v in (st.get("errors") or {}).items()) or "—"
        parts.append("<h4>Archived incident totals <span class='hidden-tag'>hidden in dashboard</span></h4>" + _kv([
            ("Outages / recoveries", f"{st.get('outages')} / {st.get('recoveries')}"),
            ("Max failures in a row", _e(st.get("maxConsecutiveFailures"))),
            ("First incident", _time(st.get("firstIncidentAt"))),
            ("Last outage / recovery", f"{_time(st.get('lastOutageAt'))} / {_time(st.get('lastRecoveryAt'))}"),
            ("Errors", errors), ("Log entries archived", _e(st.get("archivedEntries"))),
        ]))
    if n["log"]:
        parts.append("<h4>Raw incident log</h4><table class='grid'><tr><th>Time</th><th>Event</th><th>From → to</th>"
                     "<th>Fails in a row</th><th>Latency</th><th>RTT / loss</th><th>Error</th></tr>" + "".join(
                         f"<tr><td>{_time(e.get('timestamp'))}</td><td class='{'ok' if e.get('type') == 'RECOVERY' else 'bad'}'>"
                         f"{_e(e.get('type'))}<div class='small'>{_e(e.get('category') or '')}"
                         f"{' · lasted ' + str(e['durationS']) + ' s' if e.get('durationS') is not None else ''}</div></td><td>{_e(e.get('from'))} → {_e(e.get('to'))}</td>"
                         f"<td>{_e(e.get('consecutiveFailures'))}</td><td>{_e(e.get('lastLatencyMs'))}</td>"
                         f"<td>{_e(e.get('lastRttMs'))} / {_e(e.get('lastPacketLoss'))}</td>"
                         f"<td class='small'>{_e(e.get('lastError'))}"
                         + "".join(f"<div><b>{_e(c.get('verdict'))}</b>: {_e(c.get('summary'))}</div>" for c in e.get('correlation') or [])
                         + "</td></tr>" for e in reversed(n["log"])) + "</table>")
        if full:
            parts.append(_img(chart_node_incidents(n), "incidents"))

    tr = n["traceroute"]
    if tr:
        parts.append(f"<h4>Traceroute <span class='hidden-tag'>hidden in dashboard</span></h4>"
                     f"<p>{_e(tr.get('summary'))} <span class='muted'>· {_e(tr.get('trigger'))} · {_time(tr.get('at'))}"
                     f" · {_e(tr.get('duration_ms'))} ms</span></p>")
        if full:
            parts.append(_img(chart_traceroute(tr), "traceroute"))
        parts.append("<table class='grid'><tr><th>#</th><th>IP</th><th>Avg RTT</th><th>Probes</th><th>Loss</th><th>Status</th></tr>" + "".join(
            f"<tr class='{'drop' if h.get('drop') else ''}'><td>{h['hop']}</td><td class='mono'>{_e(', '.join(h.get('ips') or []) or '*')}</td>"
            f"<td>{_e(h.get('rtt_ms'))}</td><td class='small'>{_e(', '.join(str(x) for x in h.get('rtts_ms') or []))}</td>"
            f"<td>{_e(h.get('loss_pct'))}%</td><td>{'🔴 drop' if h.get('drop') else _e(h.get('status'))}</td></tr>"
            for h in tr.get("hops") or []) + "</table>")

    for s in stopped_here:
        parts.append(f"<h4>Spider {_e(s['id'])} stopped here</h4><p>{_e(s['raw'].get('stopReason'))}</p>")
    parts.append("<h4>Embedding <span class='hidden-tag'>hidden in dashboard</span></h4>"
                 f"<p>{_embedding_block(n['embedding'])}</p><p class='muted'>synced {_time(raw.get('embeddingSyncedAt'))}</p>")
    if full:
        parts.append(_img(chart_neighbours(n, data["nodes"]), "similar servers"))
    parts.append(_raw_block("All stored properties (raw)", raw))
    parts.append("</section>")
    return "".join(parts)


def spider_section(s: dict) -> str:
    rca, raw = s["rca"], s["raw"]
    path = "".join(f"<tr><td>{_e(p.get('node_id'))}</td><td>{_e(p.get('role'))}</td><td>{_up(p.get('up'))}</td>"
                   f"<td>{'walked' if p.get('visited') else 'checked from downstream'}</td><td class='small'>{_e(p.get('error'))}</td></tr>"
                   for p in rca.get("path") or [])
    return (f"<section class='node'><h3>{_badge(raw.get('status'))} {_e(s['id'])}</h3>" + _kv([
        ("Channel / FinalLink", f"{_e(s['channel'])} · {_e(raw.get('finalLinkId'))}"),
        ("At node / direction", f"{_e(s['at'])} · {_e(raw.get('direction'))}"),
        ("Steps / started / last step", f"{_e(raw.get('stepCount'))} · {_time(raw.get('startedAt'))} · {_time(raw.get('lastStepAt'))}"),
        ("Root cause", _e(rca.get("root_cause") or "none")), ("Reason", _e(rca.get("reason") or "—")),
        ("Impact", _e(rca.get("impact") or "—")), ("Failover", _e(", ".join(rca.get("failover") or []) or "—")),
        ("Failures in a row / alerted", f"{_e(rca.get('consecutive_failures', 0))} / {_e(rca.get('alerted', False))}"),
        ("Embedding", _embedding_block(s["embedding"])),
    ]) + (f"<h4>Path walked in the last cycle <span class='hidden-tag'>hidden in dashboard</span></h4>"
          f"<table class='grid'><tr><th>Node</th><th>Role</th><th>Health</th><th>How</th><th>Error</th></tr>{path}</table>"
          if path else "") + _raw_block("All stored properties (raw)", raw) + "</section>")


CSS = """
:root{--text:#1f2330;--muted:#6b7080;--line:#e2e4ea;--bg:#f7f7f9;--panel:#fff;--ok:#16a34a;--bad:#dc2626}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 60px}h1{margin:0 0 4px;font-size:24px}h2{margin:34px 0 10px;font-size:19px;border-bottom:2px solid var(--line);padding-bottom:6px}
h3{margin:0 0 4px;font-size:16px;display:flex;align-items:center;gap:8px}h4{margin:16px 0 6px;font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.muted{color:var(--muted)}.small{font-size:12px}.mono{font-family:ui-monospace,Menlo,monospace;font-size:12px;word-break:break-all}.ok{color:var(--ok)}.bad{color:var(--bad)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:14px 0}.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.card b{display:block;font-size:22px}.card span{color:var(--muted);font-size:12px}
section.node{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:14px 0;break-inside:avoid-page}
figure{margin:10px 0;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px;text-align:center}figure img{max-width:100%;height:auto}
table{border-collapse:collapse;width:100%}table.grid th,table.grid td{border-top:1px solid var(--line);padding:5px 6px;text-align:left;vertical-align:top;font-size:12.5px}table.grid th{font-size:11px;color:var(--muted);text-transform:uppercase}
table.kv th{width:190px;text-align:left;color:var(--muted);font-weight:500;padding:3px 8px 3px 0;vertical-align:top;font-size:12.5px}table.kv td{padding:3px 0;font-size:12.5px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:760px){.grid2{grid-template-columns:1fr}}
.badge{display:inline-block;font-size:11px;font-weight:700;color:#fff;background:var(--c);border-radius:999px;padding:1px 8px}
.hidden-tag{font-size:10px;text-transform:none;letter-spacing:0;background:#ede9fe;color:#6d28d9;border-radius:999px;padding:1px 7px;margin-left:6px}
tr.drop td{background:#fee2e2}details{margin-top:10px}summary{cursor:pointer;color:var(--muted);font-size:12.5px}pre{background:#0f172a;color:#e2e8f0;padding:10px;border-radius:8px;overflow:auto;font-size:11.5px;max-height:420px}
.actions{float:right;display:flex;gap:8px}.actions a,.actions button{font:inherit;font-size:13px;padding:6px 12px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--text);cursor:pointer;text-decoration:none}
.ai-btn{font:inherit;font-size:14px;font-weight:600;padding:8px 16px;border:0;border-radius:8px;background:#ff6d5a;color:#fff;cursor:pointer}.ai-btn:disabled{opacity:.6}
.ai-out{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:0 18px;margin-top:10px}.ai-out:empty{display:none}.ai-out h4{color:var(--text);font-size:14px;text-transform:none;letter-spacing:0;margin:16px 0 6px}.ai-out li{margin:3px 0 3px 18px}
nav.toc{columns:3;font-size:12.5px}nav.toc a{color:#2563eb;text-decoration:none;display:block}
@media print{body{background:#fff}section.node,figure{border-color:#ccc}details{display:none}.noprint{display:none}}
"""


def build_report(driver, node_id: Optional[str] = None, monitor: Optional[dict] = None,
                 actions: str = "", live: bool = False, metrics=None, since: int = 7 * 86400, **kwargs) -> Optional[str]:
    """The report as one HTML page; `node_id` limits it to that node (None if it doesn't exist). With
    `metrics` (metrics.Metrics) it adds anomalies, early warnings and history charts."""
    data = collect(driver)
    history: Dict[str, List[dict]] = {}
    if metrics is not None:
        ids = [node_id] if node_id else [n["id"] for n in data["nodes"]]
        data["anomalies"] = {n: metrics.anomalies(n) for n in ids}
        history = {n: metrics.history(n, since) for n in ids}
    nodes, spiders, channels = data["nodes"], data["spiders"], channel_table(data["nodes"])
    now = datetime.now(timezone.utc)
    head = (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{{title}}</title><style>{CSS}</style></head><body><main>{actions}")
    foot = "</main></body></html>"

    hours = max(1, since / 3600)
    span_label = f"last {int(hours)} h" if hours < 48 else f"last {int(round(hours / 24))} days"

    if node_id:
        node = next((n for n in nodes if n["id"] == node_id), None)
        if node is None:
            return None
        body = [f"<h1>Node report: {_e(node_id)}</h1><p class='muted'>Stream Graph · generated {_time(now)} · {span_label}</p>",
                findings_html(insights(data, monitor, node_id)), ranking_html([g for g in ranking(data) if node_id in g["nodes"]]),
                analysis_html(node_id, live),
                _img(chart_history(history, "latency_ms", f"HTTP latency, {span_label} (IST)", "ms"), "latency history"),
                _img(chart_history(history, "rtt_ms", f"ICMP RTT, {span_label} (IST)", "ms"), "rtt history"),
                _img(chart_history(history, "segment_age_s", f"Newest segment age, {span_label} (IST)", "s"), "segment age history"),
                "<h2>Node details</h2>", node_section(node, data, full=True)]
        related = [s for s in spiders if s["at"] == node_id or s["raw"].get("stopNodeId") == node_id
                   or any(p.get("node_id") == node_id for p in s["rca"].get("path") or [])]
        if related:
            body.append("<h2>Spiders that walk through this node</h2>" + "".join(spider_section(s) for s in related))
        mine = {l["channel"] for l in node["links"]}
        body.append("<h2>Channels this node carries</h2>" + _channel_rows({c: channels[c] for c in sorted(mine) if c in channels}))
        return head.replace("{title}", f"Report · {_e(node_id)}") + "".join(body) + foot

    down = [c for c, chain in channels.items() if any(e["up"] is False for e in chain.get("FinalLink", [])) or feed_of(chain) == "none"]
    backup = [c for c, chain in channels.items() if feed_of(chain) == "Backup"]
    monitor = monitor or {}
    cards = [("Servers", len(nodes)), ("Servers down", sum(n["status"] == "DOWN" for n in nodes)),
             ("Channels", len(channels)), ("Channels down", len(down)), ("On backup", len(backup)),
             ("Spiders", len(spiders)), ("Relationships", len(data["edges"])),
             ("Incidents logged", sum(len(n["log"]) for n in nodes) + sum((n["incidentStats"] or {}).get("archivedEntries", 0) for n in nodes))]
    body = [
        f"<h1>Stream Graph: full monitoring report</h1><p class='muted'>Generated {_time(now)} · auto ping "
        f"{'ON every ' + str(monitor.get('interval')) + ' s' if monitor.get('enabled') else 'OFF'} · last run "
        f"{_time(((monitor.get('last_run') or {}).get('at')))}</p>",
        "<div class='cards'>" + "".join(f"<div class='card'><b>{v}</b><span>{_e(k)}</span></div>" for k, v in cards) + "</div>",
        findings_html(insights(data, monitor)), ranking_html(ranking(data)), analysis_html(None, live),
        "<nav class='toc noprint'>" + "".join(f"<a href='#{a}'>{t}</a>" for a, t in [
            ("findings", "Key findings"), ("analysis", "AI deep analysis"), ("overview", "Overview charts"), ("channels", "Channels"), ("incidents", "Incidents & errors"),
            ("similarity", "Embeddings"), ("servers", "Every server"), ("spiders", "Every spider")]) + "</nav>",
        "<h2 id='overview'>Overview</h2>",
        _img(chart_history(history, "latency_ms", f"HTTP latency per server, {span_label} (IST)", "ms"), "latency history"),
        _img(chart_history(history, "segment_age_s", f"Newest segment age per server, {span_label} (IST)", "s"), "segment age history"),
        _img(chart_status_overview(nodes, spiders), "status"), _img(chart_topology(nodes, data["edges"]), "topology"),
        _img(chart_reliability(nodes), "reliability"), _img(chart_latency(nodes), "latency"),
        _img(chart_packet_loss(nodes), "packet loss"), _img(chart_spiders(spiders), "spiders"),
        "<h2 id='channels'>Channels</h2>", _img(chart_channel_health(channels), "channel health"), _channel_rows(channels),
        "<h2 id='incidents'>Incidents & errors</h2>", _img(chart_incident_timeline(nodes), "timeline"),
        _img(chart_errors(nodes), "errors") or "<p class='muted'>No outages recorded.</p>",
        "<h2 id='similarity'>Embeddings <span class='hidden-tag'>hidden in dashboard</span></h2>"
        "<p class='muted'>Cosine similarity of the 384-dim embeddings: servers (and spiders) that look alike in role, "
        "status, errors and incident history score close to 1.</p>",
        _img(chart_embedding_similarity(nodes, "Server embedding similarity"), "server similarity"),
        _img(chart_embedding_similarity([{**s, "id": s["channel"]} for s in spiders], "Spider embedding similarity"), "spider similarity"),
        "<h2 id='servers'>Every server</h2>" + "".join(node_section(n, data, full=False) for n in nodes),
        "<h2 id='spiders'>Every spider</h2>" + "".join(spider_section(s) for s in spiders),
    ]
    return head.replace("{title}", "Stream Graph report") + "".join(body) + foot


def _channel_rows(channels: Dict[str, Dict[str, List[dict]]]) -> str:
    rows = []
    for name, chain in channels.items():
        cells = []
        for role in ROLES:
            entries = chain.get(role, [])
            cells.append("<br>".join(f"{_up(e['up'])} <span class='mono'>{_e(e['node'])}</span>"
                                     f"{'<div class=small>' + _e(e['detail']) + '</div>' if e['detail'] else ''}"
                                     for e in entries) or "<span class='muted'>—</span>")
        last_down = max((e["lastDown"] for entries in chain.values() for e in entries if e["lastDown"]), default=None)
        rows.append(f"<tr><td><b>{_e(name)}</b><div class='small'>feed: {feed_of(chain)}</div></td>"
                    + "".join(f"<td>{c}</td>" for c in cells) + f"<td>{_time(last_down)}</td></tr>")
    return ("<table class='grid'><tr><th>Channel</th><th>Main</th><th>Backup</th><th>Transcoding</th><th>Final</th>"
            f"<th>Last down</th></tr>{''.join(rows)}</table>")
