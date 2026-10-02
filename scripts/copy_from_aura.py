"""Copy the stream graph from Neo4j Aura into the local (Docker) Neo4j.

    python copy_from_aura.py

Source: NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD from .env (Aura).
Target: bolt://localhost:7687 as neo4j / NEO4J_LOCAL_PASSWORD (override with TARGET_URI).

Copies every :Domain node (labels + all properties) and the FEEDS / PRODUCES relationships.
Spiders are not copied: the app recreates one per FinalLink on its first run. Safe to re-run:
nodes are merged by `domain`, so nothing is duplicated.
"""

import os
import sys
from typing import Dict, List

from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv()
ROLES = ["MainInput", "BackupLink", "Transcoding", "FinalLink"]


def main() -> None:
    target_uri = os.environ.get("TARGET_URI", "bolt://localhost:7687")
    target_password = os.environ.get("NEO4J_LOCAL_PASSWORD")
    if not target_password:
        sys.exit("Set NEO4J_LOCAL_PASSWORD in .env (the password of the local Neo4j)")

    with GraphDatabase.driver(os.environ["NEO4J_URI"], auth=(os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"])) as src:
        nodes = [dict(r) for r in src.execute_query(
            "MATCH (n:Domain) WHERE NOT n.domain ENDS WITH '.invalid' "
            "RETURN [l IN labels(n) WHERE l <> 'Domain'] AS roles, properties(n) AS props"
        ).records]
        edges = [dict(r) for r in src.execute_query(
            "MATCH (a:Domain)-[r:FEEDS|PRODUCES]->(b:Domain) "
            "WHERE NOT a.domain ENDS WITH '.invalid' AND NOT b.domain ENDS WITH '.invalid' "
            "RETURN a.domain AS source, type(r) AS type, b.domain AS target"
        ).records]
    print(f"Aura: {len(nodes)} nodes, {len(edges)} relationships")

    # Labels can't be query parameters: group nodes by their (validated) role combination.
    by_roles: Dict[str, List[dict]] = {}
    for n in nodes:
        roles = [r for r in n["roles"] if r in ROLES]
        by_roles.setdefault(":".join(sorted(roles)), []).append(n["props"])

    with GraphDatabase.driver(target_uri, auth=("neo4j", target_password)) as dst:
        dst.verify_connectivity()
        dst.execute_query("CREATE CONSTRAINT domain_unique IF NOT EXISTS FOR (n:Domain) REQUIRE n.domain IS UNIQUE")
        for roles, props in by_roles.items():
            labels = "".join(f":{r}" for r in roles.split(":") if r)
            dst.execute_query(
                f"UNWIND $rows AS p MERGE (n:Domain {{domain: p.domain}}) SET n += p, n{labels}", rows=props,
            )
        for rel in ("FEEDS", "PRODUCES"):
            dst.execute_query(
                f"UNWIND $rows AS e WITH e WHERE e.type = '{rel}' "
                f"MATCH (a:Domain {{domain: e.source}}), (b:Domain {{domain: e.target}}) MERGE (a)-[:{rel}]->(b)",
                rows=edges,
            )
        counts = dst.execute_query(
            "MATCH (n:Domain) WITH count(n) AS nodes "
            "OPTIONAL MATCH (:Domain)-[r:FEEDS|PRODUCES]->(:Domain) RETURN nodes, count(r) AS rels"
        ).records[0]
    print(f"Local ({target_uri}): {counts['nodes']} nodes, {counts['rels']} relationships")


if __name__ == "__main__":
    main()
