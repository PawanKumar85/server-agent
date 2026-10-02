"""Excel export shared by the Streamlit app and the web frontend."""

import io
import json
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import openpyxl
import pandas as pd

from nodes import Relationship, StreamLink, current_topology, load_json_list, pipeline_edges
from spider import Rca, all_spiders


def excel_value(value):
    """Excel cells can't hold lists or timezone-aware Neo4j datetimes."""
    if isinstance(value, list):
        return "\n".join(str(v) for v in value)
    if hasattr(value, "to_native"):
        native = value.to_native()
        return native.replace(tzinfo=None) if isinstance(native, datetime) else native
    return value


EXPORT_COLUMNS = {
    "Project Overview": ["Category", "Metric", "Value", "Notes"],
    "Nodes": ["Domain", "Status", "Primary Role", "All Roles", "Server IP", "Provider",
              "Total Checks", "Failed Checks", "Availability %", "HTTP Latency (ms)",
              "ICMP RTT (ms)", "Jitter (ms)", "Packet Loss %", "Consecutive Successes",
              "Consecutive Failures", "Last Check (IST)", "Last Recovery (IST)", "Last Error"],
    "Spiders": ["Spider ID", "Status", "Final Link ID", "Current Node", "Stop Node",
                "Stop Reason", "Root Cause (RCA)", "Impact", "Failover", "Path Walked",
                "Step Count", "Started At (IST)", "Last Step At (IST)", "At Count"],
    "Incident Logs": ["Timestamp (IST)", "Server Node", "Event Type", "Category",
                     "Duration (s)", "Failed Checks", "HTTP Latency (ms)", "ICMP RTT (ms)",
                     "Packet Loss (%)", "Stream URL", "Last Error Details"],
    "Channels": ["Channel", "Active Feed", "Status", "Main Link", "Backup Link",
                 "Transcoding Link", "Final Link", "Last down"],
    "Stream Links": ["Channel", "Role", "Server Node", "Stream URL", "Status",
                    "Target Duration (s)", "Latest Segment Age (s)", "Baseline Median (s)",
                    "Warning Line (s)", "Latency (ms)", "Freshness", "Bitrates", "Resolutions",
                    "Last Down (IST)", "Error / Detail"],
    "Relationships": ["Source", "Type", "Target"],
}

IST_TZ = timezone(timedelta(hours=5, minutes=30), "IST")


def format_ist(value) -> str:
    """Format Neo4j DateTime, ISO timestamp string, or float into 'YYYY-MM-DD HH:MM:SS IST'."""
    if value in (None, ""):
        return "-"
    if hasattr(value, "to_native"):
        value = value.to_native()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, IST_TZ).strftime("%Y-%m-%d %H:%M:%S IST")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return value.astimezone(IST_TZ).strftime("%Y-%m-%d %H:%M:%S IST")
    if isinstance(value, str):
        try:
            # Clean sub-microsecond precision if present
            cleaned = re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00"))
            dt = datetime.fromisoformat(cleaned)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(IST_TZ).strftime("%Y-%m-%d %H:%M:%S IST")
        except Exception:
            return value
    return str(value)


def channel_rows(node_props: List[dict]) -> List[dict]:
    """One row per channel: its URL for each role, active feed, and when any of them last went down."""
    channels: dict = {}
    for props in node_props:
        try:
            url_health = json.loads(props.get("urlHealth") or "{}")
        except ValueError:
            url_health = {}
        for link in load_json_list(props.get("links")):
            row = channels.setdefault(link["channel"], {"roles": {}, "downs": [], "down_now": [], "up_roles": set()})
            row["roles"].setdefault(link["role"], []).append(link["url"])
            health = url_health.get(link["url"], {})
            if health.get("lastDown"):
                row["downs"].append(health["lastDown"])
            if health.get("up") is False:
                row["down_now"].append(link["url"])
            elif health.get("up") is True:
                row["up_roles"].add(link["role"])

    rows = []
    for channel, row in sorted(channels.items()):
        last_down = max(row["downs"]) if row["downs"] else None
        feed = "Main" if "MainInput" in row["up_roles"] else ("Backup" if "BackupLink" in row["up_roles"] else "None")
        status = "Healthy" if not row["down_now"] else ("On Backup" if feed == "Backup" else "Degraded / Down")
        rows.append({
            "Channel": channel,
            "Active Feed": feed,
            "Status": status,
            **{column: "\n".join(sorted(row["roles"].get(role, []))) for column, role in CHANNEL_COLUMNS},
            "Last down": format_ist(last_down) if last_down else "-",
        })
    return rows


CHANNEL_COLUMNS = [
    ("Main Link", "MainInput"),
    ("Backup Link", "BackupLink"),
    ("Transcoding Link", "Transcoding"),
    ("Final Link", "FinalLink"),
]


def build_workbook(driver, metrics=None, **kwargs) -> bytes:
    """Excel workbook: Project Overview, Nodes, Spiders, Incident Logs, Channels, Stream Links, Relationships."""
    records = driver.execute_query(
        "MATCH (n:Domain) WHERE NOT n.domain ENDS WITH '.invalid' "
        "RETURN properties(n) AS p, [l IN labels(n) WHERE l <> 'Domain'] AS labels ORDER BY n.domain"
    ).records

    # 1. Nodes Sheet
    nodes_rows = []
    stream_links_rows = []
    all_incidents = []

    for r in records:
        p = r["p"]
        labels = r["labels"]
        domain = p.get("domain", "")
        primary_role = next((l for l in ["FinalLink", "Transcoding", "MainInput", "BackupLink"] if l in labels), "Other")
        ping_cnt = p.get("pingCount") or 0
        fail_cnt = p.get("failedCount") or 0
        avail_str = f"{round((ping_cnt - fail_cnt) / ping_cnt * 100, 1)}%" if ping_cnt > 0 else "-"
        pkt_loss = p.get("lastPacketLoss")
        pkt_loss_str = f"{round(pkt_loss * 100, 1)}%" if pkt_loss is not None else "-"

        nodes_rows.append({
            "Domain": domain,
            "Status": p.get("status") or "UNKNOWN",
            "Primary Role": primary_role,
            "All Roles": ", ".join(labels),
            "Server IP": p.get("server_ip") or "-",
            "Provider": p.get("provider") or p.get("asn") or "-",
            "Total Checks": ping_cnt,
            "Failed Checks": fail_cnt,
            "Availability %": avail_str,
            "HTTP Latency (ms)": p.get("lastLatencyMs"),
            "ICMP RTT (ms)": p.get("lastRttMs"),
            "Jitter (ms)": p.get("lastJitterMs"),
            "Packet Loss %": pkt_loss_str,
            "Consecutive Successes": p.get("consecutiveSuccesses") or 0,
            "Consecutive Failures": p.get("consecutiveFailures") or 0,
            "Last Check (IST)": format_ist(p.get("lastPing")),
            "Last Recovery (IST)": format_ist(p.get("lastRecovery")),
            "Last Error": p.get("lastError") or "-",
        })

        # Parse per-URL stream links
        try:
            url_health = json.loads(p.get("urlHealth") or "{}")
        except ValueError:
            url_health = {}
        for link in load_json_list(p.get("links")):
            url = link.get("url", "")
            h = url_health.get(url, {})
            up_val = h.get("up")
            st = "UP" if up_val is True else ("DOWN" if up_val is False else "UNKNOWN")
            stream_links_rows.append({
                "Channel": link.get("channel", "-"),
                "Role": link.get("role", "-"),
                "Server Node": domain,
                "Stream URL": url,
                "Status": st,
                "Target Duration (s)": h.get("targetS"),
                "Latest Segment Age (s)": h.get("segmentAgeS"),
                "Baseline Median (s)": h.get("ageBaseline"),
                "Warning Line (s)": h.get("warnLine"),
                "Latency (ms)": h.get("latencyMs"),
                "Freshness": h.get("freshness") or "-",
                "Bitrates": ", ".join(str(b) for b in (h.get("bitrates") or [])) or "-",
                "Resolutions": ", ".join(h.get("resolutions") or []) or "-",
                "Last Down (IST)": format_ist(h.get("lastDown")),
                "Error / Detail": h.get("error") or h.get("detail") or "-",
            })

        # Node logs from Neo4j property if present
        for e in load_json_list(p.get("log")):
            if isinstance(e, dict):
                all_incidents.append({**e, "node": domain})

    # 2. Spiders Sheet
    spiders_rows = []
    for s_ in all_spiders(driver):
        spider = dict(s_["spider"])
        rca = Rca(**json.loads(spider.pop("rca"))) if spider.get("rca") else Rca()
        path_str = "\n".join(f"{step.role} {step.node_id} ({'UP' if step.up else 'DOWN'})" for step in rca.path)
        spiders_rows.append({
            "Spider ID": spider.get("id"),
            "Status": spider.get("status") or "IDLE",
            "Final Link ID": spider.get("finalLinkId") or "-",
            "Current Node": spider.get("currentNodeId") or s_.get("at") or "-",
            "Stop Node": spider.get("stopNodeId") or "-",
            "Stop Reason": spider.get("stopReason") or "-",
            "Root Cause (RCA)": rca.root_cause or "-",
            "Impact": rca.impact or "-",
            "Failover": rca.failover or "-",
            "Path Walked": path_str or "-",
            "Step Count": spider.get("stepCount") or len(rca.path),
            "Started At (IST)": format_ist(spider.get("startedAt")),
            "Last Step At (IST)": format_ist(spider.get("lastStepAt")),
            "At Count": s_.get("atCount", 0),
        })

    # 3. Incident Logs Sheet
    if metrics is None:
        try:
            from metrics import store
            metrics = store()
        except Exception:
            metrics = None
    if metrics is not None:
        try:
            for inc in metrics.incidents(limit=3000):
                all_incidents.append(inc)
        except Exception:
            pass

    # Deduplicate incidents by (node, timestamp, type)
    seen_inc = set()
    deduped_incidents = []
    for inc in all_incidents:
        key = (inc.get("node"), inc.get("timestamp"), inc.get("type"))
        if key not in seen_inc and inc.get("node"):
            seen_inc.add(key)
            deduped_incidents.append(inc)

    # Sort newest first
    deduped_incidents.sort(key=lambda x: str(x.get("timestamp") or ""), reverse=True)

    incident_rows = []
    for inc in deduped_incidents:
        dur = inc.get("durationS")
        pkt = inc.get("lastPacketLoss")
        incident_rows.append({
            "Timestamp (IST)": format_ist(inc.get("timestamp")),
            "Server Node": inc.get("node", "-"),
            "Event Type": inc.get("type", "-"),
            "Category": inc.get("category") or "GENERAL",
            "Duration (s)": round(dur, 1) if isinstance(dur, (int, float)) else "-",
            "Failed Checks": inc.get("failedCount") or inc.get("checks") or "-",
            "HTTP Latency (ms)": inc.get("lastLatencyMs") or "-",
            "ICMP RTT (ms)": inc.get("lastRttMs") or "-",
            "Packet Loss (%)": f"{round(pkt * 100, 1)}%" if isinstance(pkt, (int, float)) else "-",
            "Stream URL": inc.get("url") or inc.get("target") or "-",
            "Last Error Details": inc.get("lastError") or inc.get("detail") or "-",
        })

    # 4. Channels Sheet
    channels_rows = channel_rows([r["p"] for r in records])

    # 5. Relationships Sheet
    rels_rows = [
        {"Source": r.source, "Type": r.type, "Target": r.target}
        for r in current_topology(driver)
    ]

    # 6. Project Overview Sheet
    total_nodes = len(nodes_rows)
    nodes_up = sum(1 for n in nodes_rows if n["Status"] == "UP")
    nodes_down = sum(1 for n in nodes_rows if n["Status"] == "DOWN")
    total_ch = len(channels_rows)
    ch_healthy = sum(1 for c in channels_rows if c["Status"] == "Healthy")
    ch_backup = sum(1 for c in channels_rows if c["Active Feed"] == "Backup")
    ch_down = sum(1 for c in channels_rows if c["Status"] == "Degraded / Down")
    total_spiders = len(spiders_rows)
    spiders_rca = sum(1 for s in spiders_rows if s["Root Cause (RCA)"] != "-")

    now_ist = datetime.now(timezone.utc).astimezone(IST_TZ).strftime("%Y-%m-%d %H:%M:%S IST")
    overview_rows = [
        {"Category": "System Metadata", "Metric": "Report Generated (IST)", "Value": now_ist, "Notes": "Live export timestamp"},
        {"Category": "Infrastructure", "Metric": "Total Server Nodes", "Value": total_nodes, "Notes": "All monitored server domains"},
        {"Category": "Infrastructure", "Metric": "Nodes Status UP", "Value": nodes_up, "Notes": f"{round(nodes_up / max(1, total_nodes) * 100, 1)}% operational"},
        {"Category": "Infrastructure", "Metric": "Nodes Status DOWN", "Value": nodes_down, "Notes": "Nodes currently failing health checks"},
        {"Category": "Channels", "Metric": "Total Live Channels", "Value": total_ch, "Notes": "Configured broadcast stream channels"},
        {"Category": "Channels", "Metric": "Channels Healthy (Main Feed)", "Value": ch_healthy, "Notes": "Operating smoothly on primary source"},
        {"Category": "Channels", "Metric": "Channels on Backup Feed", "Value": ch_backup, "Notes": "Failover active; MainInput degraded"},
        {"Category": "Channels", "Metric": "Channels Degraded / Down", "Value": ch_down, "Notes": "Both Main and Backup degraded"},
        {"Category": "Autonomous Spiders", "Metric": "Total Active Spiders", "Value": total_spiders, "Notes": "Background walker instances"},
        {"Category": "Autonomous Spiders", "Metric": "Root Causes Diagnosed", "Value": spiders_rca, "Notes": "Spiders that pinpointed upstream root causes"},
        {"Category": "Telemetry & Topology", "Metric": "Total Stream Links", "Value": len(stream_links_rows), "Notes": "Individual HTTP live streams monitored"},
        {"Category": "Telemetry & Topology", "Metric": "Media Flow Relationships", "Value": len(rels_rows), "Notes": "FEEDS and PRODUCES connections in graph"},
        {"Category": "Incident History", "Metric": "Total Incidents Recorded", "Value": len(incident_rows), "Notes": "Historical outages, recoveries, and blips"},
    ]

    sheets_to_write = [
        ("Project Overview", overview_rows),
        ("Nodes", nodes_rows),
        ("Spiders", spiders_rows),
        ("Incident Logs", incident_rows),
        ("Channels", channels_rows),
        ("Stream Links", stream_links_rows),
        ("Relationships", rels_rows),
    ]

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, rows in sheets_to_write:
            clean_rows = [{k: excel_value(v) for k, v in row.items()} for row in rows]
            df = pd.DataFrame(clean_rows)
            first = [c for c in EXPORT_COLUMNS.get(name, []) if c in df.columns]
            df = df[first + sorted(c for c in df.columns if c not in first)]
            df.to_excel(writer, sheet_name=name, index=False)
            sheet = writer.sheets[name]

            # Style header row with bold font and subtle styling
            for cell in sheet[1]:
                cell.font = openpyxl.styles.Font(bold=True)

            # Auto-size columns nicely
            for column in sheet.columns:
                max_w = max(len(str(c.value or "").split("\n")[0]) for c in column)
                sheet.column_dimensions[column[0].column_letter].width = min(max(max_w + 3, 11), 60)

    return buffer.getvalue()


# --- Excel Import -----------------------------------------------------------

ROLE_MAP = {
    "main": "MainInput",
    "main link": "MainInput",
    "mainlink": "MainInput",
    "main_link": "MainInput",
    "maininput": "MainInput",
    "main input": "MainInput",
    "primary": "MainInput",
    "backup": "BackupLink",
    "backup link": "BackupLink",
    "backuplink": "BackupLink",
    "backup_link": "BackupLink",
    "backupinput": "BackupLink",
    "secondary": "BackupLink",
    "transcoder": "Transcoding",
    "transcoding": "Transcoding",
    "transcoding link": "Transcoding",
    "transcodinglink": "Transcoding",
    "transcoding_link": "Transcoding",
    "trans": "Transcoding",
    "final": "FinalLink",
    "final link": "FinalLink",
    "finallink": "FinalLink",
    "final_link": "FinalLink",
    "output": "FinalLink",
    "edge": "FinalLink",
}


def normalize_role(name: str) -> Optional[str]:
    """Map arbitrary column headers or text to standard roles."""
    clean = str(name).strip().lower()
    if clean in ROLE_MAP:
        return ROLE_MAP[clean]
    for key, role in ROLE_MAP.items():
        if key in clean:
            return role
    return None


def extract_urls(cell_value) -> List[str]:
    """Extract valid URLs from an Excel cell value."""
    if pd.isna(cell_value):
        return []
    text = str(cell_value).strip()
    if not text or text == "-":
        return []
    lines = [line.strip() for line in re.split(r"[\r\n,;]+", text) if line.strip()]
    urls = []
    for line in lines:
        if line.startswith("http://") or line.startswith("https://"):
            urls.append(line)
        elif "." in line and "/" in line:
            urls.append("https://" + line)
    return urls


def parse_excel_workbook(file_bytes: bytes) -> dict:
    """Parse an uploaded Excel workbook to extract StreamLinks and Relationships.

    Supports:
    1. Multi-sheet export workbook (Channels, Relationships, Nodes).
    2. Wide channel matrices (Channel, Main Link, Backup Link, Transcoding Link, Final Link).
    3. Row-by-row links (Channel, Role, URL).
    4. Auto-connects standard media pipeline topology when explicit relationships are absent.
    """
    sheets = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None)
    links: List[StreamLink] = []
    relationships: List[Relationship] = []
    errors: List[str] = []
    sheets_found = list(sheets.keys())

    # 1. Parse Links & Channels across all sheets
    for sheet_name, df in sheets.items():
        if df.empty:
            continue
        cols_lower = {str(c).strip().lower(): c for c in df.columns}

        # Check for row-based format: Channel, Role/Label, URL/Link
        if "channel" in cols_lower and any(k in cols_lower for k in ("role", "label")) and any(k in cols_lower for k in ("url", "link")):
            chan_col = cols_lower["channel"]
            role_col = cols_lower.get("role") or cols_lower.get("label")
            url_col = cols_lower.get("url") or cols_lower.get("link")
            for i, row in df.iterrows():
                chan = str(row[chan_col]).strip()
                role_raw = str(row[role_col]).strip()
                role = normalize_role(role_raw)
                for u in extract_urls(row[url_col]):
                    if chan and role and u:
                        try:
                            links.append(StreamLink(channel=chan, label=role, url=u))
                        except Exception as e:
                            errors.append(f"Sheet '{sheet_name}' Row {i+2}: {e}")

        # Check for wide format: Channel, Main Link, Backup Link, Transcoding Link, Final Link
        elif "channel" in cols_lower:
            chan_col = cols_lower["channel"]
            role_cols = {}
            for c in df.columns:
                norm = normalize_role(str(c))
                if norm and str(c).strip().lower() != "channel":
                    role_cols[c] = norm

            if role_cols:
                for i, row in df.iterrows():
                    chan = str(row[chan_col]).strip()
                    if not chan or pd.isna(row[chan_col]) or chan == "nan":
                        continue
                    for col_name, role in role_cols.items():
                        for u in extract_urls(row[col_name]):
                            try:
                                links.append(StreamLink(channel=chan, label=role, url=u))
                            except Exception as e:
                                errors.append(f"Sheet '{sheet_name}' Row {i+2} ({role}): {e}")

    # Deduplicate links by (channel, label, url)
    seen_links = set()
    unique_links = []
    for l in links:
        key = (l.channel, l.label, str(l.url))
        if key not in seen_links:
            seen_links.add(key)
            unique_links.append(l)
    links = unique_links

    # 2. Parse Relationships if a sheet exists with source/target
    for sheet_name, df in sheets.items():
        if df.empty:
            continue
        cols_lower = {str(c).strip().lower(): c for c in df.columns}
        if "source" in cols_lower and "target" in cols_lower:
            s_col = cols_lower["source"]
            t_col = cols_lower["target"]
            type_col = cols_lower.get("type")
            for i, row in df.iterrows():
                src = str(row[s_col]).strip()
                tgt = str(row[t_col]).strip()
                t_val = str(row[type_col]).strip().upper() if type_col else ""
                if t_val not in ("FEEDS", "PRODUCES"):
                    t_val = "PRODUCES" if "PRODUC" in t_val else "FEEDS"
                if src and tgt and src != "nan" and tgt != "nan":
                    try:
                        relationships.append(Relationship(source=src, type=t_val, target=tgt))
                    except Exception as e:
                        errors.append(f"Sheet '{sheet_name}' Row {i+2} (Relationship): {e}")

    # Deduplicate relationships
    seen_rels = set()
    unique_rels = []
    for r in relationships:
        key = (r.source, r.type, r.target)
        if key not in seen_rels:
            seen_rels.add(key)
            unique_rels.append(r)
    relationships = unique_rels

    # 3. The standard pipeline per channel, for workbooks without relationships
    unique_auto = pipeline_edges(links)

    channels = sorted(list(set(l.channel for l in links)))
    return {
        "sheets_found": sheets_found,
        "links": links,
        "relationships": relationships,
        "auto_relationships": unique_auto,
        "channels": channels,
        "errors": errors,
    }
