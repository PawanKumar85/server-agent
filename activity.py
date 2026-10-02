"""Activity & Audit Logging with Graph Vector Embeddings for Stream Graph.

Stores all autonomous tool activities, Scrapy crawler audits, safe actions, and diagnostics
as :ActivityLog nodes in Neo4j with 384-dimensional vector embeddings (all-MiniLM-L6-v2).
Enables the ChatBot LLM to recall, ground, and cite past activities via GraphRAG vector search.
"""

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def record_activity(
    driver,
    embedder=None,
    act_type: str = "general",
    title: str = "",
    summary: str = "",
    target: str = "",
    details: Optional[Dict[str, Any]] = None,
    domain: Optional[str] = None,
) -> dict:
    """Creates a new :ActivityLog node in Neo4j with vector embedding and links to relevant :Domain."""
    if not driver:
        return {}

    act_id = f"act_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    details_json = json.dumps(details or {}, default=str)

    # Generate 384-dim vector embedding
    embedding_vec: List[float] = []
    if embedder is not None and summary:
        try:
            if hasattr(embedder, "encode"):
                vec = embedder.encode([summary])[0]
            elif callable(embedder):
                vec = embedder([summary])[0]
            else:
                vec = None
            if vec is not None:
                embedding_vec = [float(x) for x in vec]
        except Exception:
            embedding_vec = []

    if not embedding_vec and summary:
        try:
            import text_embedding
            vec = text_embedding.encode([summary])[0]
            embedding_vec = [float(x) for x in vec]
        except Exception:
            embedding_vec = []

    # Infer domain from target if not provided
    if not domain and target:
        from urllib.parse import urlparse
        parsed = urlparse(target)
        domain = (parsed.netloc or target).lower().split(":")[0]

    labels = ["ActivityLog"]
    if act_type == "scrapy":
        labels.append("ScrapyRun")
    elif act_type in ("action", "crud"):
        labels.append("AuditLog")
    elif act_type in ("traceroute", "diagnostic"):
        labels.append("DiagnosticLog")

    labels_str = ":".join(labels)

    cypher = f"""
    CREATE (a:{labels_str} {{
        id: $id,
        type: $type,
        title: $title,
        summary: $summary,
        target: $target,
        domain: $domain,
        details: $details,
        embedding: $vec,
        timestamp: $ts,
        createdAt: datetime()
    }})
    """
    driver.execute_query(
        cypher,
        id=act_id,
        type=act_type,
        title=title or f"{act_type.capitalize()} Activity",
        summary=summary,
        target=target,
        domain=domain or "",
        details=details_json,
        vec=embedding_vec,
        ts=now_iso,
    )

    # Link to Domain if it exists in the graph
    if domain:
        link_cypher = """
        MATCH (d:Domain) WHERE d.domain = $domain OR d.domain ENDS WITH ('.' + $domain) OR $domain ENDS WITH ('.' + d.domain)
        MATCH (a:ActivityLog {id: $id})
        MERGE (d)-[:HAS_ACTIVITY]->(a)
        """
        driver.execute_query(link_cypher, domain=domain, id=act_id)

    return {
        "id": act_id,
        "type": act_type,
        "title": title,
        "summary": summary,
        "target": target,
        "domain": domain,
        "timestamp": now_iso,
        "has_embedding": len(embedding_vec) == 384,
    }


def list_activities(driver, limit: int = 50, act_type: Optional[str] = None) -> List[dict]:
    """Returns recent activities from Neo4j."""
    if not driver:
        return []
    where = "WHERE a.type = $act_type" if act_type else ""
    q = f"""
    MATCH (a:ActivityLog)
    {where}
    OPTIONAL MATCH (d:Domain)-[:HAS_ACTIVITY]->(a)
    RETURN properties(a) AS p, d.domain AS domain
    ORDER BY a.createdAt DESC
    LIMIT $limit
    """
    records = driver.execute_query(q, act_type=act_type, limit=limit).records
    out = []
    for r in records:
        p = dict(r["p"])
        p.pop("embedding", None)  # exclude raw float vector from API list
        p["associatedDomain"] = r["domain"]
        if isinstance(p.get("details"), str):
            try:
                p["details"] = json.loads(p["details"])
            except Exception:
                pass
        out.append(p)
    return out
