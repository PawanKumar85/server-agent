"""Nightly backups of what can't be rebuilt: the history/learning database (metrics.db: checks, incidents, learned
alert lines, facts, feedback, cases) and the graph (servers, links, relationships, spiders, activity log).

Each run writes two gzipped files into BACKUP_DIR (a folder on the Mac in Docker, see docker-compose.yml) and keeps
the newest BACKUP_KEEP of each kind:
    metrics-YYYYmmdd-HHMM.db.gz   a consistent SQLite copy (online backup API, safe while the app writes)
    graph-YYYYmmdd-HHMM.json.gz   every node's properties (minus embeddings) and every relationship

Restore:
    gunzip -c backups/metrics-….db.gz > state/metrics.db             (with the app stopped)
    python backup.py restore-graph backups/graph-….json.gz            (MERGEs the nodes and relationships back)
"""

import gzip
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", Path(__file__).parent / "backups"))
BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "7"))
DAY_S = 86400

# Node labels and their key property, as MERGE keys on restore.
NODE_KEYS = {"Domain": "domain", "SpiderRun": "id", "ActivityLog": "id"}
SKIP_PROPS = ("embedding", "embeddingHash")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")


def backup_sqlite(path: str, dest: Path) -> Path:
    out = dest / f"metrics-{_stamp()}.db.gz"
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "metrics.db"
        src, dst = sqlite3.connect(path), sqlite3.connect(copy)
        with dst:
            src.backup(dst)  # consistent even while checks are being written
        src.close(); dst.close()
        with open(copy, "rb") as f, gzip.open(out, "wb", compresslevel=6) as g:
            shutil.copyfileobj(f, g)
    return out


def _plain(v):
    if hasattr(v, "iso_format"):
        return {"$datetime": v.iso_format()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v


def export_graph(driver) -> Dict[str, List[dict]]:
    nodes, rels = [], []
    for label, key in NODE_KEYS.items():
        for r in driver.execute_query(f"MATCH (n:{label}) RETURN labels(n) AS labels, properties(n) AS p").records:
            p = {k: _plain(v) for k, v in dict(r["p"]).items() if k not in SKIP_PROPS}
            nodes.append({"label": label, "labels": r["labels"], "key": key, "props": p})
    for r in driver.execute_query(
            "MATCH (a)-[r]->(b) WHERE (a:Domain OR a:SpiderRun) AND (b:Domain OR b:ActivityLog) "
            "RETURN labels(a)[0] AS al, coalesce(a.domain, a.id) AS ak, type(r) AS type, "
            "labels(b)[0] AS bl, coalesce(b.domain, b.id) AS bk, properties(r) AS p").records:
        rels.append({k: _plain(r[k]) if k == "p" else r[k] for k in ("al", "ak", "type", "bl", "bk", "p")})
    return {"version": 1, "exportedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "nodes": nodes, "relationships": rels}


def backup_graph(driver, dest: Path) -> Path:
    out = dest / f"graph-{_stamp()}.json.gz"
    with gzip.open(out, "wt", encoding="utf-8") as g:
        json.dump(export_graph(driver), g)
    return out


def prune(dest: Path, keep: int = BACKUP_KEEP) -> int:
    removed = 0
    for prefix in ("metrics-", "graph-"):
        files = sorted(dest.glob(f"{prefix}*.gz"))
        for f in files[:-keep] if keep > 0 else []:
            f.unlink(); removed += 1
    return removed


def last_backup_age(dest: Path = BACKUP_DIR) -> float:
    """Seconds since the newest backup (infinite when there is none)."""
    files = list(dest.glob("metrics-*.gz")) if dest.exists() else []
    return time.time() - max(f.stat().st_mtime for f in files) if files else float("inf")


def run(metrics_path: str, driver, dest: Path = BACKUP_DIR, keep: int = BACKUP_KEEP) -> dict:
    started = time.monotonic()
    dest.mkdir(parents=True, exist_ok=True)
    db = backup_sqlite(metrics_path, dest)
    graph = backup_graph(driver, dest)
    removed = prune(dest, keep)
    return {"metrics": db.name, "graph": graph.name, "bytes": db.stat().st_size + graph.stat().st_size,
            "removed": removed, "elapsed_ms": int((time.monotonic() - started) * 1000)}


def list_backups(dest: Path = BACKUP_DIR) -> List[dict]:
    if not dest.exists():
        return []
    return [{"name": f.name, "bytes": f.stat().st_size,
             "at": datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")}
            for f in sorted(dest.glob("*.gz"), reverse=True)]


def restore_graph(driver, path: str) -> dict:
    """MERGEs a graph backup back in: nodes by their key (properties overwritten from the backup, labels added),
    then the relationships. Nothing that exists now is deleted."""
    with gzip.open(path, "rt", encoding="utf-8") as g:
        data = json.load(g)

    def value(v):
        if isinstance(v, dict) and "$datetime" in v:
            return v["$datetime"]
        return [value(x) for x in v] if isinstance(v, list) else v

    for n in data["nodes"]:
        props = {k: value(v) for k, v in n["props"].items()}
        extra = "".join(f":`{l}`" for l in n["labels"] if l != n["label"])
        driver.execute_query(f"MERGE (x:{n['label']} {{{n['key']}: $k}}) SET x += $p" + (f" SET x{extra}" if extra else ""),
                             k=props[n["key"]], p=props)
    for r in data["relationships"]:
        ak, bk = NODE_KEYS.get(r["al"], "id"), NODE_KEYS.get(r["bl"], "id")
        driver.execute_query(f"MATCH (a:{r['al']} {{{ak}: $a}}), (b:{r['bl']} {{{bk}: $b}}) "
                             f"MERGE (a)-[x:{r['type']}]->(b) SET x += $p", a=r["ak"], b=r["bk"], p=r["p"] or {})
    return {"nodes": len(data["nodes"]), "relationships": len(data["relationships"])}


if __name__ == "__main__":
    from dotenv import load_dotenv
    from neo4j import GraphDatabase
    load_dotenv()
    drv = GraphDatabase.driver(os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
                               auth=(os.environ.get("NEO4J_USERNAME", "neo4j"),
                                     os.environ.get("NEO4J_PASSWORD") or os.environ.get("NEO4J_LOCAL_PASSWORD")))
    if len(sys.argv) == 3 and sys.argv[1] == "restore-graph":
        print(restore_graph(drv, sys.argv[2]))
    else:
        print(run(os.environ.get("METRICS_DB", "metrics.db"), drv))
