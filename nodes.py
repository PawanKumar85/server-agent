import json
import re
import os
import socket
from functools import cached_property
from typing import Annotated, ClassVar, Dict, List, Literal, Optional, Set, Tuple

from dotenv import load_dotenv
from neo4j import GraphDatabase
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints, ValidationError, computed_field


# Neo4j labels can't be query parameters, so they're interpolated into Cypher;
# restrict them to plain identifiers.
LABEL_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
Label = Field(pattern=LABEL_PATTERN)
Channel = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class StreamLink(BaseModel):
    """One stream URL in the pipeline: its role (`label`, e.g. "Transcoding") in a channel."""

    model_config = ConfigDict(frozen=True)

    label: str = Label
    url: HttpUrl
    channel: Channel

    @computed_field
    @property
    def domain(self) -> str:
        return self.url.host

    @property
    def node_id(self) -> str:
        """The Neo4j node this link belongs to: its channel's own node for a FinalLink, else its domain's."""
        return final_node_id(self.domain, self.channel) if self.label == "FinalLink" else self.domain


def final_node_id(host: str, channel: str) -> str:
    """Every channel has its own FinalLink node (and so its own spider): id "<host>/<channel>"."""
    return f"{host}/{channel}"


def node_host(node_id: str) -> str:
    """The network host of a node id ("gtc.ottlive.co.in/gtcnews" -> "gtc.ottlive.co.in")."""
    return node_id.split("/", 1)[0]


class DomainNode(BaseModel):
    """A Neo4j :Domain node; `domain` is its unique id.

    Input and transcoder links on one domain collapse into one node: their URLs and
    role labels are merged as sets. A FinalLink gets a node per channel instead (id
    "<host>/<channel>"), so each channel has its own spider. `links` keeps each URL's
    own role and channel, stored as a JSON string (Neo4j properties can't hold maps).
    """

    label: ClassVar[str] = "Domain"

    domain: str
    url: Set[HttpUrl]
    labels: Set[str]
    links: List[Dict[str, str]] = []  # [{"url", "role", "channel"}]

    @computed_field
    @cached_property
    def server_ip(self) -> Optional[str]:
        # Resolved via DNS once per instance; None if the host doesn't resolve.
        try:
            return socket.gethostbyname(node_host(self.domain))
        except socket.gaierror:
            return None

    def props(self) -> dict:
        return {
            "domain": self.domain,
            "url": sorted(str(u) for u in self.url),
            "server_ip": self.server_ip,
        }


def prompt_links() -> List[StreamLink]:
    """Ask for links until an empty label is entered; re-ask on invalid input."""
    links: List[StreamLink] = []
    print("Enter stream links (empty label to finish).")
    while True:
        label = input("label:   ").strip()
        if not label:
            return links
        url = input("url:     ").strip()
        channel = input("channel: ").strip()
        try:
            links.append(StreamLink(label=label, url=url, channel=channel))
        except ValidationError as e:
            for err in e.errors():
                print(f"  invalid {err['loc'][0]}: {err['msg']}")


class Relationship(BaseModel):
    """A directed topology edge between two specific domain nodes (media-flow direction)."""

    model_config = ConfigDict(frozen=True)

    source: str  # domain
    type: Literal["FEEDS", "PRODUCES"]
    target: str  # domain


# Allowed shapes, by role label: Main/Backup feed a Transcoding, or a FinalLink directly
# when the channel has no transcoder; a Transcoding produces a FinalLink.
EDGE_SHAPES = {
    "FEEDS": (("MainInput", "BackupLink"), ("Transcoding", "FinalLink")),
    "PRODUCES": (("Transcoding",), ("FinalLink",)),
}


def group_by_domain(links: List[StreamLink]) -> List[DomainNode]:
    """Inputs/transcoders group by domain; each FinalLink channel gets its own node."""
    grouped: Dict[str, dict] = {}
    for link in links:
        entry = grouped.setdefault(
            link.node_id, {"domain": link.node_id, "url": set(), "labels": set(), "links": []}
        )
        entry["url"].add(link.url)
        entry["labels"].add(link.label)
        entry["links"].append({"url": str(link.url), "role": link.label, "channel": link.channel})
    return [DomainNode(**entry) for entry in grouped.values()]


def load_json_list(value: Optional[str]) -> list:
    """Parse a JSON-list node property (`links`), tolerating missing or bad values."""
    try:
        parsed = json.loads(value) if value else []
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


# Appends each item of $new to the stored list only if it isn't already there.
SET_UNION = "reduce(acc = coalesce(n.{prop}, []), x IN ${prop} | CASE WHEN x IN acc THEN acc ELSE acc + x END)"


def ensure_database_indexes(driver) -> None:
    """Create native constraints and indexes for low-latency queries on stream health properties."""
    queries = [
        f"CREATE CONSTRAINT domain_unique IF NOT EXISTS FOR (n:{DomainNode.label}) REQUIRE n.domain IS UNIQUE",
        f"CREATE INDEX domain_segment_age IF NOT EXISTS FOR (n:{DomainNode.label}) ON (n.lastSegmentAgeS)",
        f"CREATE INDEX domain_freshness IF NOT EXISTS FOR (n:{DomainNode.label}) ON (n.streamFreshness)",
        f"CREATE INDEX domain_media_seq IF NOT EXISTS FOR (n:{DomainNode.label}) ON (n.mediaSequence)",
    ]
    with driver.session() as session:
        for q in queries:
            try:
                session.run(q)
            except Exception:
                pass


def upsert_nodes(driver, nodes: List[DomainNode]) -> None:
    with driver.session() as session:
        ensure_database_indexes(driver)
        existing = {
            r["domain"]: load_json_list(r["links"])
            for r in session.run(
                f"MATCH (n:{DomainNode.label}) WHERE n.domain IN $domains RETURN n.domain AS domain, n.links AS links",
                domains=[n.domain for n in nodes],
            )
        }
        for node in nodes:
            # Merge by URL: a re-entered URL takes its new role/channel, others are kept.
            links = {l["url"]: l for l in existing.get(node.domain, [])}
            links.update({l["url"]: l for l in node.links})
            # Labels are validated identifiers (see `Label`); backticks guard them anyway.
            labels = "".join(f":`{label}`" for label in sorted(node.labels))
            session.run(
                f"MERGE (n:{DomainNode.label} {{domain: $domain}}) "
                f"SET n{labels}, "
                f"n.url = {SET_UNION.format(prop='url')}, "
                "n.server_ip = $server_ip, "
                "n.links = $links",
                **node.props(),
                links=json.dumps(sorted(links.values(), key=lambda l: l["url"])),
            )


# ".invalid" is reserved (RFC 2606) and can never be a real host: domains ending in it are test
# fixtures. The dashboard, automatic spider runs and topology saves ignore them.
TEST_TLD = ".invalid"


def current_topology(driver) -> List[Relationship]:
    records = driver.execute_query(
        f"MATCH (a:{DomainNode.label})-[r:FEEDS|PRODUCES]->(b:{DomainNode.label}) "
        "WHERE NOT a.domain ENDS WITH $test AND NOT b.domain ENDS WITH $test "
        "RETURN a.domain AS source, type(r) AS type, b.domain AS target ORDER BY type, target, source",
        test=TEST_TLD,
    ).records
    return [Relationship(**dict(r)) for r in records]


def edge_problem(rel: Relationship, labels: Dict[str, List[str]]) -> Optional[str]:
    """Why `rel` isn't a valid topology edge, or None if it is."""
    if rel.source == rel.target:
        return "source and target are the same node"
    for end in (rel.source, rel.target):
        if end not in labels:
            return f"no node for {end}"
    sources, targets = EDGE_SHAPES[rel.type]
    if not set(labels[rel.source]) & set(sources):
        return f"{rel.type} must start at {' or '.join(sources)}"
    if not set(labels[rel.target]) & set(targets):
        return f"{rel.type} must end at {' or '.join(targets)}"
    return None


def replace_topology(driver, relationships: List[Relationship]) -> List[Tuple[Relationship, str]]:
    """Make the FEEDS/PRODUCES edges exactly `relationships`, in one transaction.

    Invalid edges are skipped and returned with the reason; nothing else is touched.
    """
    records = driver.execute_query(
        f"MATCH (n:{DomainNode.label}) RETURN n.domain AS domain, labels(n) AS labels"
    ).records
    labels = {r["domain"]: r["labels"] for r in records}
    rejected = [(rel, problem) for rel in relationships if (problem := edge_problem(rel, labels))]
    valid = [rel.model_dump() for rel in relationships if rel not in {r for r, _ in rejected}]

    def write(tx):
        tx.run(
            f"MATCH (a:{DomainNode.label})-[r:FEEDS|PRODUCES]->(b:{DomainNode.label}) "
            "WHERE NOT {source: a.domain, type: type(r), target: b.domain} IN $edges "
            "AND NOT a.domain ENDS WITH $test AND NOT b.domain ENDS WITH $test "  # never touch test fixtures
            "DELETE r",
            edges=valid, test=TEST_TLD,
        )
        for rel_type in EDGE_SHAPES:  # relationship types can't be parameters
            tx.run(
                f"UNWIND $edges AS e WITH e WHERE e.type = '{rel_type}' "
                f"MATCH (a:{DomainNode.label} {{domain: e.source}}), (b:{DomainNode.label} {{domain: e.target}}) "
                f"MERGE (a)-[:{rel_type}]->(b)",
                edges=valid,
            )

    with driver.session() as session:
        session.execute_write(write)
    return rejected

def pipeline_edges(links: List[StreamLink]) -> List[Relationship]:
    """The standard pipeline of each channel in `links`: Main/Backup FEEDS its Transcoding, which PRODUCES the
    Final; with no Transcoding, Main/Backup FEED the Final directly."""
    roles: Dict[str, Dict[str, Set[str]]] = {}
    for link in links:
        roles.setdefault(link.channel, {}).setdefault(link.label, set()).add(link.node_id)
    edges: List[Relationship] = []
    for chan in roles.values():
        inputs = sorted(chan.get("MainInput", set()) | chan.get("BackupLink", set()))
        transcoders, finals = sorted(chan.get("Transcoding", set())), sorted(chan.get("FinalLink", set()))
        for final in finals:
            for t in transcoders:
                edges.append(Relationship(source=t, type="PRODUCES", target=final))
            for i in inputs:
                for target in transcoders or [final]:
                    edges.append(Relationship(source=i, type="FEEDS", target=target))
    return list(dict.fromkeys(e for e in edges if e.source != e.target))


def connect_new_channels(driver, channels: Set[str]) -> List[Relationship]:
    """Wires servers that just joined a channel: for each of `channels`, every server not yet linked (FEEDS or
    PRODUCES, either way) to another server of the same channel gets the channel's standard pipeline edges
    (pipeline_edges). A brand-new channel gets its whole pipeline; links added one at a time (Main, then
    Transcoding, then Final) are wired as each arrives; a channel wired by hand has every server linked
    already, so it is left as it is. Returns the edges added."""
    if not channels:
        return []
    records = driver.execute_query(
        f"MATCH (n:{DomainNode.label}) WHERE NOT n.domain ENDS WITH $test RETURN n.links AS links", test=TEST_TLD
    ).records
    links: List[StreamLink] = []
    for r in records:
        for l in load_json_list(r["links"]):
            if l.get("channel") in channels:
                try:
                    links.append(StreamLink(channel=l["channel"], label=l["role"], url=l["url"]))
                except ValidationError:
                    continue
    existing = current_topology(driver)
    have = set(existing)
    by_channel: Dict[str, List[StreamLink]] = {}
    for link in links:
        by_channel.setdefault(link.channel, []).append(link)
    added: List[Relationship] = []
    for chan_links in by_channel.values():
        members = {l.node_id for l in chan_links}
        linked = {n for e in existing if e.source in members and e.target in members for n in (e.source, e.target)}
        loose = members - linked
        added += [e for e in pipeline_edges(chan_links)
                  if (e.source in loose or e.target in loose) and e not in have and e not in added]
    if added:
        rejected = replace_topology(driver, existing + added)
        added = [e for e in added if e not in {r for r, _ in rejected}]
    return added



def move_link(driver, old_url: str, new: StreamLink) -> dict:
    """Replaces the link `old_url` with `new`, on the right node: a URL on another server moves to that server's
    node (made if new), not kept on the old one. The old node loses the URL (and any monitored URL left over from
    an earlier edit that no link has any more), its health, and a role label no other link of it still has; edges between it and a server it no longer shares a channel with are removed;
    the new node is wired into its channel's pipeline. Returns {"from", "to", "removed", "added"}."""
    found = driver.execute_query(
        f"MATCH (n:{DomainNode.label}) WHERE n.links CONTAINS $u RETURN n.domain AS d, n.links AS l, "
        "labels(n) AS labels, n.urlHealth AS h", u=old_url).records
    rec = next((r for r in found if any(l.get("url") == old_url for l in load_json_list(r["l"]))), None)
    if rec is None:
        raise ValueError(f"no node has the link {old_url}")
    old_node = rec["d"]
    kept = [l for l in load_json_list(rec["l"]) if l.get("url") != old_url]
    if new.node_id == old_node:
        kept.append({"url": str(new.url), "role": new.label, "channel": new.channel})
    roles = {l.get("role") for l in kept}
    drop_labels = [r for r in rec["labels"] if re.fullmatch(LABEL_PATTERN, r) and r != DomainNode.label
                   and r not in roles]
    try:
        health = json.loads(rec["h"]) if rec["h"] else None
    except ValueError:
        health = None
    if isinstance(health, dict):
        health = {url: h for url, h in health.items() if url in {l["url"] for l in kept}}
    remove = "".join(f" REMOVE n:`{r}`" for r in drop_labels)
    driver.execute_query(
        f"MATCH (n:{DomainNode.label} {{domain: $d}}) SET n.links = $links, "
        "n.url = [u IN coalesce(n.url, []) WHERE u IN $keep], n.urlHealth = $health" + remove,
        d=old_node, links=json.dumps(sorted(kept, key=lambda l: l["url"])), keep=[l["url"] for l in kept],
        health=json.dumps(health) if isinstance(health, dict) else rec["h"])
    if new.node_id != old_node:
        upsert_nodes(driver, group_by_domain([new]))

    # Edges of the old node to servers it no longer shares any channel with were only there for this link.
    channels = {r["d"]: {l.get("channel") for l in load_json_list(r["l"])} for r in driver.execute_query(
        f"MATCH (n:{DomainNode.label}) RETURN n.domain AS d, n.links AS l").records}
    existing = current_topology(driver)
    stale = [e for e in existing if old_node in (e.source, e.target)
             and not channels.get(e.source, set()) & channels.get(e.target, set())]
    if stale:
        replace_topology(driver, [e for e in existing if e not in stale])
    added = connect_new_channels(driver, {new.channel})
    return {"from": old_node, "to": new.node_id, "removed": stale, "added": added}


if __name__ == "__main__":
    load_dotenv()
    links = prompt_links()
    if not links:
        raise SystemExit("No links entered")
    nodes = group_by_domain(links)
    driver = GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"]),
    )
    with driver:
        driver.verify_connectivity()
        upsert_nodes(driver, nodes)
        for node in nodes:
            print(node.domain, sorted(node.labels), node.server_ip)
    print(f"Upserted {len(nodes)} domain nodes. Edit relationships in the Streamlit app (app.py).")
