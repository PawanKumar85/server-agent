import json
import os
import re
from datetime import datetime
from typing import List

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv
from neo4j import GraphDatabase
from pydantic import ValidationError

from nodes import (
    LABEL_PATTERN,
    Relationship,
    StreamLink,
    current_topology,
    group_by_domain,
    replace_topology,
    upsert_nodes,
)
from export import build_workbook
from graph_view import fetch_graph, render_html
from spider import Rca, SpiderManager, all_spiders, count_spiders

load_dotenv()

st.set_page_config(page_title="Stream Graph", page_icon="📡", layout="wide")


@st.cache_resource
def get_driver():
    driver = GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"]),
    )
    driver.verify_connectivity()
    return driver


def parse_rows(df: pd.DataFrame, model, columns: List[str]):
    """Validate each non-empty row with `model`; returns (items, errors)."""
    items, errors = [], []
    for i, row in df.iterrows():
        values = {c: str(row[c]).strip() if pd.notna(row[c]) else "" for c in columns}
        if not any(values.values()):
            continue
        try:
            items.append(model(**values))
        except ValidationError as e:
            for err in e.errors():
                errors.append(f"Row {i + 1} · {err['loc'][0]}: {err['msg']}")
    return items, errors


DEFAULT_LABELS = ["MainInput", "BackupLink", "Transcoding", "FinalLink"]
if "labels" not in st.session_state:
    st.session_state.labels = list(DEFAULT_LABELS)


def add_label():
    new = st.session_state.new_label.strip()
    if not new:
        return
    if not re.fullmatch(LABEL_PATTERN, new):
        st.session_state.label_error = f"'{new}' isn't a valid label: letters, digits and _ only, not starting with a digit."
    elif new not in st.session_state.labels:
        st.session_state.labels.append(new)
    st.session_state.new_label = ""


st.title("📡 Stream Graph")
st.caption("Enter stream links; links on the same domain become one Neo4j node.")

# --- Links ---------------------------------------------------------------
st.subheader("1. Links")
with st.expander("Add a new label"):
    st.text_input("New label", key="new_label", on_change=add_label, placeholder="e.g. CDNEdge, then press Enter")
    if "label_error" in st.session_state:
        st.error(st.session_state.pop("label_error"))
label_options = st.session_state.labels

links_df = st.data_editor(
    pd.DataFrame(columns=["channel", "label", "url"], dtype=str),
    num_rows="dynamic",
    width="stretch",
    column_config={
        "channel": st.column_config.TextColumn("Channel", help="Channel this link carries, e.g. gtcnews", required=True),
        "label": st.column_config.SelectboxColumn("Label", options=label_options, help="Role of this link", required=True),
        "url": st.column_config.TextColumn("URL", help="http(s) stream URL", required=True, width="large"),
    },
    key="links",
)
links, link_errors = parse_rows(links_df, StreamLink, ["channel", "label", "url"])
for err in link_errors:
    st.error(err)

# --- Preview -------------------------------------------------------------
st.subheader("2. Preview")
nodes = group_by_domain(links)
if nodes:
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "domain": n.domain,
                    "labels": ", ".join(sorted(n.labels)),
                    "url": "\n".join(sorted(str(u) for u in n.url)),
                    "channels": ", ".join(sorted({l["channel"] for l in n.links})),
                    "server_ip": n.server_ip or "unresolved",
                }
                for n in nodes
            ]
        ),
        width="stretch",
        hide_index=True,
    )
else:
    st.info("Add at least one link above.")

if st.button("Save links to Neo4j", type="primary", disabled=not nodes or bool(link_errors)):
    try:
        with st.spinner("Saving…"):
            upsert_nodes(get_driver(), nodes)
    except Exception as e:  # connection/auth/query errors surface in the UI
        st.error(f"Neo4j error: {e}")
    else:
        st.success(f"Saved {len(nodes)} domain nodes.")
        st.session_state.pop("topology", None)  # reload domain choices below

# --- Relationships -------------------------------------------------------
st.divider()
st.subheader("3. Relationships")
st.caption(
    "Media flow between specific domains: MainInput/BackupLink FEEDS a Transcoding (or a FinalLink directly "
    "when there's no transcoder); a Transcoding PRODUCES a FinalLink. The table is the full topology: rows you "
    "delete here are removed from Neo4j on save."
)
if "topology" not in st.session_state:
    try:
        driver = get_driver()
        st.session_state.topology = [r.model_dump() for r in current_topology(driver)]
        st.session_state.domains = sorted(
            r["d"] for r in driver.execute_query("MATCH (n:Domain) RETURN n.domain AS d").records
        )
    except Exception as e:
        st.error(f"Couldn't load the topology from Neo4j: {e}")
if "topology" in st.session_state:
    domain_options = sorted(set(st.session_state.domains) | {n.domain for n in nodes})
    rels_df = st.data_editor(
        pd.DataFrame(st.session_state.topology, columns=["source", "type", "target"]),
        num_rows="dynamic",
        width="stretch",
        column_config={
            "source": st.column_config.SelectboxColumn("Source (upstream)", options=domain_options, required=True),
            "type": st.column_config.SelectboxColumn("Type", options=["FEEDS", "PRODUCES"], required=True),
            "target": st.column_config.SelectboxColumn("Target (downstream)", options=domain_options, required=True),
        },
        key="rels",
    )
    rels, rel_errors = parse_rows(rels_df, Relationship, ["source", "type", "target"])
    for err in rel_errors:
        st.error(err)
    if st.button("Save relationships", disabled=bool(rel_errors)):
        try:
            with st.spinner("Saving…"):
                rejected = replace_topology(get_driver(), rels)
        except Exception as e:
            st.error(f"Neo4j error: {e}")
        else:
            st.success(f"Saved {len(rels) - len(rejected)} relationships.")
            for rel, problem in rejected:
                st.warning(f"Skipped {rel.source} -[:{rel.type}]-> {rel.target}: {problem}")
            st.session_state.pop("topology", None)

# --- Spiders -------------------------------------------------------------
st.divider()
st.subheader("4. Spiders")
st.caption(
    "One spider per FinalLink walks upstream (Final → Transcoding → Main/Backup), health-checks each node "
    "and stops at the root cause. Main and Backup are alternatives: one healthy input is enough."
)

SPIDER_ICONS = {"RUNNING": "🟢", "STOPPED": "🔴", "RECOVERED": "🔵", "ERROR": "🟠"}
ALERT_STYLE = {"SPIDER_STOPPED": st.error, "SPIDER_ERROR": st.error, "SPIDER_RECOVERED": st.success, "FAILOVER_ACTIVE": st.warning}


def fmt_time(value) -> str:
    return value.to_native().strftime("%Y-%m-%d %H:%M:%S UTC") if value is not None else "-"


def render_spiders(driver, events=None) -> None:
    st.markdown("##### Workflow")
    st.caption(
        "Drag to pan · scroll to zoom · drag cards to rearrange · click a card for URLs and health"
        + (" · playing back the walk that just ran (▶ Replay to watch again)" if events else "")
    )
    components.html(render_html(fetch_graph(driver), height=620, replay=events), height=630)

    spiders = all_spiders(driver)
    if not spiders:
        st.info("No spiders yet. Run them once to create one per FinalLink.")
        return
    lines = [
        f"{SPIDER_ICONS.get(s['spider']['status'], '')} **{s['spider']['id']}** → {s['spider']['finalLinkId']} → "
        f"{s['spider']['status']} @ {s['spider'].get('currentNodeId')}"
        for s in spiders
    ]
    st.markdown("  \n".join(lines))
    c1, c2, c3 = st.columns(3)
    c1.metric("Active spiders", count_spiders(driver, "RUNNING"))
    c2.metric("Stopped spiders", count_spiders(driver, "STOPPED"))
    c3.metric("Errors", count_spiders(driver, "ERROR"))

    for s in spiders:
        spider, node = s["spider"], s["node"] or {}
        rca = Rca(**json.loads(spider["rca"])) if spider.get("rca") else Rca()
        with st.container(border=True):
            st.markdown(f"#### {SPIDER_ICONS.get(spider['status'], '')} {spider['id']} · {spider['status']}")
            for step in rca.path:
                mark = "✓" if step.up else "✗ DOWN"
                how = "" if step.visited else " _(checked from downstream)_"
                st.markdown(f"**{step.role}** · {step.node_id} · {mark}{how}")
                if not step.up and step.error:
                    st.caption(step.error)
                if spider["status"] == "STOPPED" and step.node_id == spider.get("currentNodeId"):
                    st.markdown("⬆️ **SPIDER STOPPED HERE**")
            if s["atCount"] != 1:
                st.error(f"Spider has {s['atCount']} AT relationships (expected exactly 1).")
            a, b, c, d = st.columns(4)
            a.metric("Current location", node.get("domain", "-"))
            b.metric("Ping count", node.get("pingCount", 0))
            c.metric("Failed count", node.get("failedCount", 0))
            d.metric("Last latency", f"{node['lastLatencyMs']:.0f} ms" if node.get("lastLatencyMs") is not None else "-")
            st.caption(
                f"Last ping: {fmt_time(node.get('lastPing'))} · Node status: {node.get('status', '-')} · "
                f"RTT {node.get('lastRttMs', '-')} ms · jitter {node.get('lastJitterMs', '-')} ms · "
                f"loss {node.get('lastPacketLoss', '-')} · steps {spider.get('stepCount', 0)}"
            )
            if spider.get("stopReason"):
                st.markdown(f"**Reason:** {spider['stopReason']}")
            if rca.root_cause:
                st.markdown(f"**Root cause:** {rca.root_cause}")
            st.markdown(f"**Impact:** {rca.impact}")
            for note in rca.failover:
                st.warning(note)


def run_and_render() -> None:
    driver = get_driver()
    events = None
    if st.session_state.get("auto_run") or st.session_state.pop("run_now", False):
        with st.spinner("Spiders walking… the canvas plays the walk back as soon as it finishes"):
            result = SpiderManager(driver).run_cycle()
        events = result.events
        st.caption(f"Last cycle: {result.elapsed_ms} ms · {len(result.reports)} spiders")
        for alert in (a for r in result.reports for a in r.alerts):
            ALERT_STYLE.get(alert.kind, st.info)(f"{alert.kind} · {alert.spider_id}: {alert.message}")
        for t in result.node_transitions:
            st.caption(f"Node {t.node_id}: {t.previous} → {t.current}")
    render_spiders(driver, events)


left, right = st.columns([1, 3])
if left.button("Run spiders now"):
    st.session_state.run_now = True
right.toggle("Auto-run every 60 s", key="auto_run")


@st.fragment(run_every=60 if st.session_state.get("auto_run") else None)
def spiders_fragment() -> None:
    try:
        run_and_render()
    except Exception as e:
        st.error(f"Spider error: {e}")


spiders_fragment()


# --- Export --------------------------------------------------------------
st.divider()
st.subheader("5. Export")


@st.cache_data(ttl=30, show_spinner=False)
def export_workbook() -> bytes:
    return build_workbook(get_driver())


try:
    st.download_button(
        "Download Excel",
        data=export_workbook(),
        file_name=f"stream-graph-{datetime.now():%Y%m%d-%H%M}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        help="Channels (Main/Backup/Transcoding/Final link + last down), nodes, relationships and spiders; refreshed every 30 s",
    )
except Exception as e:
    st.error(f"Couldn't build the export: {e}")
