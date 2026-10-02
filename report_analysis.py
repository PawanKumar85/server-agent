"""AI deep analysis of a report: the model reads a compact digest of the report data and the computed findings
(report.insights) and writes risks, performance, incident patterns, redundancy and prioritised
recommendations. Streamed like the ChatBot (OpenRouter, via ChatBot.generate)."""

from typing import Any, Dict, Iterator, List, Optional

from report import channel_table, collect, error_counts, feed_of, insights, outage_count, success_rate

# A deeper read than a chat answer: medium reasoning, reasoning text not sent back (still billed). The cap
# covers reasoning + text: medium reasoning alone used ~2,500 tokens on the full report, so 3,000 cut it off.
GENERATION = {"max_tokens": 8000, "temperature": 0.2, "reasoning": {"effort": "medium", "exclude": True}}

PROMPT = """You are a senior NOC and streaming reliability engineer. Deeply analyse this monitoring report of an OTT \
HLS streaming network ({scope}) and write an analysis for the operations team.

Each channel flows Main or Backup input -> Transcoding -> Final output. Main and Backup are alternatives. \
Spiders walk each channel's chain every {interval} s and stop at the root cause of a fault.

### Report data
{digest}

### Findings computed by the monitor (correct; build on them, don't contradict them)
{findings}

### Write these sections (Markdown, "##" headings, "-" bullets, concise, name servers and numbers)
## Executive summary
2-3 sentences: overall health and the one thing that matters most.
## Key risks
Ranked; for each, why it matters and what could happen next.
## Performance
Latency, jitter, packet loss: what is normal, what stands out, and the likely cause (network vs server).
## Incident patterns
Recurring errors, flapping servers, and what the error types suggest about the cause.
## Topology and redundancy
Single points of failure, shared dependencies (blast radius), backup coverage.
## Recommendations
Numbered, most important first, each concrete and tied to a server or channel.

Use only the data above; if something cannot be concluded from it, say so instead of guessing."""


def _fmt(value, unit="", digits=0) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}{unit}" if isinstance(value, (int, float)) else f"{value}{unit}"


def digest(data: Dict[str, Any], node_id: Optional[str] = None) -> str:
    """A compact text version of the report data (one line per server/channel/spider) to keep tokens low."""
    nodes = data["nodes"]
    channels = channel_table(nodes)
    scope = [n for n in nodes if n["id"] == node_id] if node_id else nodes
    lines = ["Servers (status, roles, check success %, HTTP latency, ICMP RTT/jitter/loss, outages, channels):"]
    for n in scope:
        raw = n["raw"]
        loss = raw.get("lastPacketLoss")
        lines.append(
            f"- {n['id']}: {n['status']}, {'/'.join(r.replace('Input', '').replace('Link', '') for r in n['roles'])}, "
            f"success {_fmt(success_rate(n), '%', 1)} ({raw.get('failedCount') or 0}/{raw.get('pingCount') or 0} failed), "
            f"HTTP {_fmt(raw.get('lastLatencyMs'), ' ms')}, RTT {_fmt(raw.get('lastRttMs'), ' ms')}, "
            f"jitter {_fmt(raw.get('lastJitterMs'), ' ms')}, loss {_fmt(loss * 100 if loss is not None and loss <= 1 else loss, '%')}, "
            f"outages {outage_count(n)}, blips {raw.get('blipCount') or 0}, "
            f"channels {', '.join(sorted({l['channel'] for l in n['links']})) or '—'}"
            + (f", last error: {raw.get('lastError')}" if n["status"] != "UP" and raw.get("lastError") else "")
            + (f", traceroute: {n['traceroute'].get('summary')}" if n["traceroute"] else ""))
    wanted = {l["channel"] for n in scope for l in n["links"]} if node_id else set(channels)
    lines.append("Channels (feed; servers per role):")
    for name in sorted(wanted):
        chain = channels.get(name, {})
        roles = "; ".join(f"{r.replace('Input', '').replace('Link', '')}: "
                          + ", ".join(f"{e['node']}{'' if e['up'] is not False else ' (DOWN)'}" for e in chain.get(r, []))
                          for r in ("MainInput", "BackupLink", "Transcoding", "FinalLink") if chain.get(r))
        lines.append(f"- {name}: feed {feed_of(chain)}; {roles}")
    lines.append("Spiders (status, root cause, impact):")
    for sp in data["spiders"]:
        if node_id and not (sp["at"] == node_id or sp["raw"].get("stopNodeId") == node_id):
            continue
        lines.append(f"- {sp['channel']}: {sp['raw'].get('status')}, root cause {sp['rca'].get('root_cause') or 'none'}, "
                     f"{sp['rca'].get('impact') or ''}")
    verdicts = [f"{n['id']} {e.get('timestamp', '')[:16]} {e.get('category') or ''}: {c.get('verdict')} ({c.get('summary')})"
                for n in scope for e in n["log"] for c in e.get("correlation") or []]
    if verdicts:
        lines.append("Incident correlations (the channel chain at the moment each incident opened):")
        lines += [f"- {v}" for v in verdicts[-12:]]
    errors = error_counts(scope)
    if errors:
        lines.append("Outage error kinds: " + ", ".join(f"{k} ×{v}" for k, v in errors.most_common(8)))
    edges = [f"{e['source']} {e['type']} {e['target']}" for e in data["edges"]
             if not node_id or node_id in (e["source"], e["target"])]
    label = f"Relationships of {node_id}" if node_id else "Relationships (all)"
    lines.append(f"{label}: {len(edges)}: " + (", ".join(edges) or "none"))
    return "\n".join(lines)


def findings_text(items: List[dict]) -> str:
    return "\n".join(f"- [{f['severity']}] {f['area']}: {f['finding']} ({f['evidence']})" for f in items) or "- none"


def analysis_prompt(driver, node_id: Optional[str], monitor: Optional[dict], metrics=None) -> Optional[dict]:
    """The filled prompt and the computed findings, or None if the node doesn't exist."""
    data = collect(driver)
    if metrics is not None:
        data["anomalies"] = {n["id"]: metrics.anomalies(n["id"]) for n in data["nodes"]}
    if node_id and not any(n["id"] == node_id for n in data["nodes"]):
        return None
    items = insights(data, monitor, node_id)
    prompt = PROMPT.format(scope=f"server {node_id}" if node_id else f"all {len(data['nodes'])} servers",
                           interval=(monitor or {}).get("interval") or 30,
                           digest=digest(data, node_id), findings=findings_text(items))
    return {"prompt": prompt, "findings": items}


def stream_analysis(bot, prepared: dict, model: Optional[str] = None) -> Iterator[dict]:
    """findings event, then the model's analysis token by token, then done (or error)."""
    yield {"type": "findings", "findings": prepared["findings"]}
    yield from bot.generate([{"role": "user", "content": prepared["prompt"]}], GENERATION, model=model)
