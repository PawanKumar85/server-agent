"""GraphRAG retrieval for the ChatBot: Neo4j is both the knowledge graph and the vector store.

For a question:
1. Entity linking: server and channel names mentioned in it ("ingest1", "bharat24news") map straight to nodes.
2. Vector search in Neo4j: the question's embedding (all-MiniLM-L6-v2) is matched against `embedding` on
   :Domain (servers) and :SpiderRun (one per channel) through Neo4j vector indexes.
3. Graph expansion: from those seed nodes, Cypher walks FEEDS / PRODUCES to the channel's whole chain and the
   neighbouring servers, and SpiderRun -> its FinalLink, so the model sees a connected subgraph.

What gets embedded is a stable profile of each node (identity, roles, channels, status, error kinds), not its
live numbers, so embeddings only change when the profile does; the live details go to the model as context.
This is the project's one embedding: `embedding` on :Domain and :SpiderRun (MiniLM through ONNX, see
text_embedding), also used by the embeddings sync and the report's similarity charts. `embeddingHash` records
which profile (and model) each vector came from.
"""

import hashlib
import json
import re
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from tracer_langsmith import traceable

RAG_DIM = 384  # all-MiniLM-L6-v2
INDEXES = {"Domain": "domain_embeddings", "SpiderRun": "spider_embeddings", "ActivityLog": "activity_embeddings"}
LEGACY_INDEXES = ("rag_domain", "rag_spider")  # from before the embeddings were unified
VECTOR_K = 5  # nearest nodes per index
MAX_SEEDS = 4
MAX_DOCS = 8  # context documents besides the overview
VECTOR_MIN_SCORE = 0.65  # cosine; below this a "nearest" node is just noise for the question
SCHEDULER_WORDS = re.compile(r"auto ?ping|schedul|interval|next run|last run|cron|how often", re.I)

CREATE_INDEX = """
CREATE VECTOR INDEX {name} IF NOT EXISTS FOR (n:{label}) ON (n.embedding)
OPTIONS {{indexConfig: {{`vector.dimensions`: {dim}, `vector.similarity_function`: 'cosine'}}}}
"""
READ_HASHES = """
MATCH (n:Domain) RETURN 'Domain' AS label, n.domain AS key, properties(n).embeddingHash AS hash
UNION ALL
MATCH (s:SpiderRun) RETURN 'SpiderRun' AS label, s.id AS key, properties(s).embeddingHash AS hash
"""
WRITE_DOMAIN = ("UNWIND $rows AS r MATCH (n:Domain {domain: r.key}) "
                "SET n.embedding = r.vec, n.embeddingHash = r.hash, n.embeddingSyncedAt = datetime()")
WRITE_SPIDER = ("UNWIND $rows AS r MATCH (n:SpiderRun {id: r.key}) "
                "SET n.embedding = r.vec, n.embeddingHash = r.hash, n.embeddingSyncedAt = datetime()")
DROP_LEGACY = """
MATCH (n) WHERE n.ragEmbedding IS NOT NULL OR n.ragHash IS NOT NULL
REMOVE n.ragEmbedding, n.ragHash
"""
VECTOR_QUERY = """
CALL db.index.vector.queryNodes($index, $k, $vec) YIELD node, score
RETURN coalesce(node.domain, node.id) AS key, score
"""
NEIGHBOURS = """
UNWIND $ids AS id
MATCH (n:Domain {domain: id})-[r:FEEDS|PRODUCES]-(m:Domain)
RETURN DISTINCT startNode(r).domain AS source, type(r) AS type, endNode(r).domain AS target
"""
UPSTREAM_OF_FINAL = """
UNWIND $finals AS f
MATCH (final:Domain {domain: f})
OPTIONAL MATCH (u:Domain)-[:FEEDS|PRODUCES*1..3]->(final)
RETURN f AS final, collect(DISTINCT u.domain) AS upstream
"""


def _hash(text: str) -> str:
    from text_embedding import MODEL  # a new model means new vectors
    return hashlib.sha1(f"{MODEL}\n{text}".encode()).hexdigest()


# --- what gets embedded -------------------------------------------------------------

def server_profile(node: dict, error_kinds: Iterable[str] = ()) -> str:
    kinds = ", ".join(sorted(set(error_kinds)))
    return (f"Server {node['domain']} ({node['domain'].split('.')[0]}): {', '.join(node['roles'])} server for channels "
            f"{', '.join(node['channels']) or 'none'}. Status {node['status']}." + (f" Errors: {kinds}." if kinds else ""))


def spider_profile(channel: str, spider: dict) -> str:
    return (f"Channel {channel} and its spider: status {spider.get('status')}, root cause "
            f"{spider.get('rootCause') or 'none'}, impact {spider.get('impact') or 'none'}.")


def profiles(snap) -> Dict[Tuple[str, str], str]:
    """(label, key) -> profile text, for every server and every spider in the snapshot."""
    out: Dict[Tuple[str, str], str] = {}
    for node in snap.nodes.values():
        stats = node.get("incidentStats") or {}
        kinds = list((stats.get("categories") or {})) + list((stats.get("errors") or {}))
        kinds += [e.get("category") for e in node.get("log") or [] if e.get("category")]
        if node.get("lastError") and node["status"] != "UP":
            kinds.append(str(node["lastError"]).split(" (")[0])
        out[("Domain", node["domain"])] = server_profile(node, kinds)
    for channel in snap.channels:
        spider = snap.spider_for(channel)
        final = next(iter(snap.channels[channel].get("FinalLink", [])), None)
        if spider and final and final["node"] in snap.spiders:
            out[("SpiderRun", snap.spiders[final["node"]].get("id"))] = spider_profile(channel, spider)
    return out


# --- entity linking -----------------------------------------------------------------

def link_entities(question: str, snap) -> Dict[str, List[str]]:
    """Servers and channels named in the question: full domains, unambiguous short names ("ingest1", "jio"),
    and channel names, matched as whole words."""
    q = question.lower()
    words = set(re.findall(r"[a-z0-9][a-z0-9.\-/]*", q))
    channels = sorted(c for c in snap.channels if re.search(rf"\b{re.escape(c.lower())}\b", q))
    servers = [d for d in snap.nodes if d.lower() in q]
    by_short: Dict[str, List[str]] = {}
    for d in snap.nodes:
        by_short.setdefault(d.split(".")[0].lower(), []).append(d)
    for short, domains in by_short.items():
        if short in words and len(domains) == 1 and domains[0] not in servers:
            servers.append(domains[0])
    return {"servers": sorted(servers), "channels": channels}


# --- the retriever ------------------------------------------------------------------

class GraphRAG:
    def __init__(self, driver, embed: Callable[[List[str]], "object"]):
        self.driver = driver
        self.embed = embed  # texts -> array of 384-dim vectors
        self._indexes_ready = False
        self.last_sync: Dict[str, int] = {}

    def ensure_indexes(self) -> None:
        if self._indexes_ready:
            return
        for label, name in INDEXES.items():
            self.driver.execute_query(CREATE_INDEX.format(name=name, label=label, dim=RAG_DIM))
        for name in LEGACY_INDEXES:
            self.driver.execute_query(f"DROP INDEX {name} IF EXISTS")
        self.driver.execute_query(DROP_LEGACY)
        self._indexes_ready = True

    def sync(self, snap, force: bool = False) -> int:
        """Embeds the nodes whose profile changed since their last embedding (all of them with `force`);
        returns how many. `last_sync` has the per-label counts."""
        self.ensure_indexes()
        wanted = profiles(snap)
        stored = {(r["label"], r["key"]): r["hash"] for r in self.driver.execute_query(READ_HASHES).records}
        changed = [(k, t) for k, t in wanted.items() if force or stored.get(k) != _hash(t)]
        self.last_sync = {"Domain": sum(k[0] == "Domain" for k, _ in changed),
                          "SpiderRun": sum(k[0] == "SpiderRun" for k, _ in changed), "total": len(wanted)}
        if not changed:
            return 0
        vectors = self.embed([t for _, t in changed])
        rows = {"Domain": [], "SpiderRun": []}
        for ((label, key), text), vec in zip(changed, vectors):
            rows[label].append({"key": key, "vec": [float(x) for x in vec], "hash": _hash(text)})
        if rows["Domain"]:
            self.driver.execute_query(WRITE_DOMAIN, rows=rows["Domain"])
        if rows["SpiderRun"]:
            self.driver.execute_query(WRITE_SPIDER, rows=rows["SpiderRun"])
        return len(changed)

    @traceable(name="graphrag_vector_seeds", run_type="retriever")
    def vector_seeds(self, question: str, snap) -> List[Tuple[str, str, float]]:
        """[(label, key, score)] nearest to the question, limited to nodes in the snapshot."""
        vec = [float(x) for x in self.embed([question])[0]]
        spider_ids = {s.get("id") for s in snap.spiders.values()}
        hits = []
        for label, index in INDEXES.items():
            for r in self.driver.execute_query(VECTOR_QUERY, index=index, k=VECTOR_K, vec=vec).records:
                if label == "Domain":
                    known = r["key"] in snap.nodes
                elif label == "SpiderRun":
                    known = r["key"] in spider_ids
                elif label == "ActivityLog":
                    known = True
                else:
                    known = False
                if known and float(r["score"]) >= VECTOR_MIN_SCORE:
                    hits.append((label, r["key"], float(r["score"])))
        return sorted(hits, key=lambda h: -h[2])

    @traceable(name="graphrag_retrieve", run_type="retriever")
    def retrieve(self, question: str, snap, documents: List[dict]) -> List[dict]:
        """The overview, then documents for the linked/nearest nodes and their graph neighbourhood.
        Each result: {"document": {...}, "score": float, "via": "overview|entity|vector|graph|keyword"}."""
        self.sync(snap)
        docs = {d["id"]: d for d in documents}
        results: List[dict] = [{"document": docs["overview"], "score": 1.0, "via": "overview"}]
        taken = {"overview"}

        def take(doc_id: str, score: float, via: str) -> None:
            if doc_id in docs and doc_id not in taken and len(results) <= MAX_DOCS:
                taken.add(doc_id)
                results.append({"document": docs[doc_id], "score": round(score, 3), "via": via})

        entities = link_entities(question, snap)
        final_to_channel = {e["node"]: c for c, chain in snap.channels.items() for e in chain.get("FinalLink", [])}
        spider_to_channel = {s.get("id"): final_to_channel.get(f) for f, s in snap.spiders.items()}
        # Seeds in order of relevance: named in the question first, then nearest by vector (only one when a name
        # was found, so loosely related nodes don't crowd out the named node's own neighbourhood).
        seeds = [("server", d, 1.0, "entity") for d in entities["servers"]]
        seeds += [("channel", c, 1.0, "entity") for c in entities["channels"]]
        room = 1 if seeds else MAX_SEEDS
        for label, key, score in self.vector_seeds(question, snap):
            if room <= 0:
                break
            if label == "ActivityLog":
                seeds.append(("activity", key, score, "vector"))
                room -= 1
            else:
                kind, name = ("server", key) if label == "Domain" else ("channel", spider_to_channel.get(key))
                if name and all(s[1] != name for s in seeds):
                    seeds.append((kind, name, score, "vector"))
                    room -= 1
        if SCHEDULER_WORDS.search(question):
            take("scheduler", 1.0, "keyword")

        # Each seed, then its graph neighbourhood: an activity record; a server's channels;
        # a channel's whole chain, found by walking FEEDS/PRODUCES upstream from its Final server.
        for kind, name, score, via in seeds:
            if kind == "activity":
                act_doc = self._activity_document(name)
                if act_doc:
                    docs[act_doc["id"]] = act_doc
                    take(act_doc["id"], score, via)
                    if act_doc.get("domain") and act_doc["domain"] in snap.nodes:
                        take(f"server:{act_doc['domain']}", score * 0.85, "graph")
            elif kind == "server":
                take(f"server:{name}", score, via)
                for channel in snap.nodes[name]["channels"]:
                    take(f"channel:{channel}", score * 0.9, "graph")
                around = [name]
                self._take_edges(around, docs, take, score * 0.7)
            else:
                take(f"channel:{name}", score, via)
                around = self._chain(name, snap, final_to_channel)
                for server in around:
                    take(f"server:{server}", score * 0.8, "graph")
                self._take_edges(around, docs, take, score * 0.7)
        return results

    def _activity_document(self, act_id: str) -> Optional[dict]:
        try:
            q = """
            MATCH (a:ActivityLog) WHERE a.id = $id OR a.domain = $id
            OPTIONAL MATCH (d:Domain)-[:HAS_ACTIVITY]->(a)
            RETURN properties(a) AS p, coalesce(d.domain, a.domain) AS domain
            ORDER BY a.createdAt DESC
            LIMIT 1
            """
            records = self.driver.execute_query(q, id=act_id).records
            if not records:
                return None
            p = dict(records[0]["p"])
            domain = records[0]["domain"]
            lines = [
                f"Historical Activity & Scrape Log ({p.get('timestamp') or 'recent'}): {p.get('title', 'System Activity')}",
                f"- Activity Type: {p.get('type')}",
                f"- Target Endpoint: {p.get('target') or domain or 'platform'}",
                f"- Summary: {p.get('summary', '')}"
            ]
            if p.get("details"):
                try:
                    det = json.loads(p["details"]) if isinstance(p["details"], str) else p["details"]
                    if isinstance(det, dict):
                        if "total_links" in det:
                            lines.append(f"- Extracted links: {det.get('total_links')} total ({det.get('internal_count')} internal, {det.get('external_count')} external, {det.get('media_count')} media streams).")
                        if "top_external_domains" in det and det["top_external_domains"]:
                            top_doms = ", ".join(f"{k} ({v})" for k, v in list(det['top_external_domains'].items())[:5])
                            lines.append(f"- Top outbound domains: {top_doms}")
                        if "sample_media" in det and det["sample_media"]:
                            lines.append(f"- Stream URLs discovered: {', '.join(det['sample_media'][:5])}")
                except Exception:
                    pass
            doc_id = f"activity:{act_id}"
            return {
                "id": doc_id,
                "title": f"Activity: {p.get('title') or act_id}",
                "text": "\n".join(lines),
                "domain": domain
            }
        except Exception:
            return None

    def _chain(self, channel: str, snap, final_to_channel: Dict[str, str]) -> List[str]:
        """The channel's servers: its Final server and everything upstream of it that carries the channel."""
        finals = [e["node"] for e in snap.channels.get(channel, {}).get("FinalLink", [])]
        if not finals:
            return []
        members = {e["node"] for entries in snap.channels.get(channel, {}).values() for e in entries}
        chain: List[str] = []
        for r in self.driver.execute_query(UPSTREAM_OF_FINAL, finals=finals).records:
            chain += [r["final"]] + [u for u in r["upstream"] if u in members]  # only this channel's chain
        return list(dict.fromkeys(chain))

    def _take_edges(self, around: List[str], docs: Dict[str, dict], take, score: float) -> None:
        if not around:
            return
        edges = self.driver.execute_query(NEIGHBOURS, ids=around).records
        if not edges:
            return
        lines = sorted({f"{e['source']} {e['type']} {e['target']}" for e in edges})
        doc_id = f"graph:edges:{around[0]}"
        docs[doc_id] = {"id": doc_id, "title": f"relationships around {around[0]}",
                        "text": "Relationships (from the graph):\n" + "\n".join(f"- {line}" for line in lines)}
        take(doc_id, score, "graph")
