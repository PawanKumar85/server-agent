"""Nightly backups: a consistent SQLite copy, a graph export, pruning, and restoring the graph."""

import gzip
import json
import sqlite3
from types import SimpleNamespace as NS

import backup


class FakeDriver:
    def __init__(self):
        self.writes = []

    def execute_query(self, query, **params):
        if query.startswith("MATCH (n:Domain)"):
            return NS(records=[{"labels": ["Domain", "MainInput"],
                                "p": {"domain": "a.example", "status": "UP", "embedding": [0.1] * 384}}])
        if query.startswith("MATCH (n:"):
            return NS(records=[])
        if query.startswith("MATCH (a)-[r]->(b)"):
            return NS(records=[{"al": "Domain", "ak": "a.example", "type": "FEEDS", "bl": "Domain", "bk": "b.example", "p": {}}])
        self.writes.append((query, params))
        return NS(records=[])


def test_backup_run_writes_both_files_prunes_and_restores(tmp_path):
    db = tmp_path / "metrics.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE facts (text TEXT)"); c.execute("INSERT INTO facts VALUES ('xcode2 restarts at 03:00')")
    dest = tmp_path / "backups"
    for i in range(9):  # more runs than kept
        (dest).mkdir(exist_ok=True)
        (dest / f"metrics-2026010{i}-0000.db.gz").write_bytes(b"x")
        (dest / f"graph-2026010{i}-0000.json.gz").write_bytes(b"x")
    result = backup.run(str(db), FakeDriver(), dest=dest, keep=7)
    assert len(list(dest.glob("metrics-*.gz"))) == 7 and len(list(dest.glob("graph-*.gz"))) == 7
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress((dest / result["metrics"]).read_bytes()))
    assert sqlite3.connect(restored).execute("SELECT text FROM facts").fetchone()[0] == "xcode2 restarts at 03:00"
    data = json.loads(gzip.decompress((dest / result["graph"]).read_bytes()))
    assert data["nodes"][0]["props"] == {"domain": "a.example", "status": "UP"}  # no embedding vectors
    assert data["relationships"][0]["type"] == "FEEDS"
    assert backup.last_backup_age(dest) < 60

    drv = FakeDriver()
    assert backup.restore_graph(drv, str(dest / result["graph"])) == {"nodes": 1, "relationships": 1}
    assert drv.writes[0][0].startswith("MERGE (x:Domain {domain: $k}) SET x += $p SET x:`MainInput`")
    assert "MERGE (a)-[x:FEEDS]->(b)" in drv.writes[1][0]
