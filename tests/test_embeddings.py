"""The one embedding model (MiniLM via ONNX), the incident store, and the Embeddings & Incident Clearance job."""

import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

import text_embedding
from embeddings import EMBEDDING_DIM, sync_all_embeddings
from metrics import Metrics

LOG = [
    {"type": "OUTAGE", "timestamp": "2026-09-20T10:00:00+00:00", "consecutiveFailures": 1,
     "lastError": "1/2 URLs failing: https://a.example/x/index.m3u8 HTTP 404"},
    {"type": "OUTAGE", "timestamp": "2026-09-20T10:05:00+00:00", "consecutiveFailures": 10,
     "lastError": "1/2 URLs failing: https://a.example/y/index.m3u8 HTTP 404"},
    {"type": "RECOVERY", "timestamp": "2026-09-20T10:09:00+00:00", "consecutiveFailures": 0},
    {"type": "OUTAGE", "timestamp": "2026-09-25T08:00:00+00:00", "consecutiveFailures": 1,
     "lastError": "HTTP_CONNECTERROR after 3 tries"},
]


# --- the model ---

def test_minilm_onnx_vectors_are_normalised_and_semantic():
    vecs = text_embedding.encode(["channel output is down", "the stream went offline", "invoice for coffee beans"])
    assert vecs.shape == (3, EMBEDDING_DIM) and np.allclose(np.linalg.norm(vecs, axis=1), 1.0, atol=1e-4)
    assert vecs[0] @ vecs[1] > vecs[0] @ vecs[2]  # similar meaning, closer vectors
    assert text_embedding.encode([]).shape == (0, EMBEDDING_DIM)


# --- incident history ---

def test_error_kinds_count_the_same_failure_together():
    from embeddings import _error_kind
    assert _error_kind("1/2 URLs failing: https://a.example/x/index.m3u8 HTTP 404") == "HTTP 404"
    assert _error_kind("master, 0/1 variants live (STALE_SEGMENTS (36s old))") == "STALE_SEGMENTS"
    assert _error_kind("HTTP_CONNECTERROR after 3 tries") == "HTTP_CONNECTERROR"
    assert _error_kind(None) is None


def test_summarize_incidents_is_cumulative():
    from embeddings import summarize_incidents
    first = summarize_incidents(LOG[:3])
    assert first == {"outages": 2, "recoveries": 1, "firstIncidentAt": "2026-09-20T10:00:00+00:00",
                     "lastOutageAt": "2026-09-20T10:05:00+00:00", "lastRecoveryAt": "2026-09-20T10:09:00+00:00",
                     "maxConsecutiveFailures": 10, "errors": {"HTTP 404": 2}, "categories": {}, "archivedEntries": 3}
    both = summarize_incidents(LOG[3:], first)  # a later run adds to the earlier totals
    assert both["outages"] == 3 and both["archivedEntries"] == 4
    assert both["errors"] == {"HTTP 404": 2, "HTTP_CONNECTERROR": 1}
    assert first["outages"] == 2  # the previous totals are not modified
    typed = summarize_incidents([{"type": "OUTAGE", "category": "STALE_MEDIA"}, {"type": "OUTAGE", "category": "STALE_MEDIA"},
                                 {"type": "ESCALATED", "category": "STALE_MEDIA"}, {"type": "OUTAGE", "category": "PLAYLIST_MISSING"}])
    assert typed["categories"] == {"STALE_MEDIA": 2, "PLAYLIST_MISSING": 1} and typed["outages"] == 3


def test_incident_store_keeps_order_updates_and_takes_old_entries(tmp_path):
    store = Metrics(tmp_path / "m.db")
    for e in LOG:
        store.add_incident("a.example", e)
    store.add_incident("a.example", LOG[0])  # the same entry again (e.g. a migration re-run) is ignored
    store.add_incident("b.example", {"type": "OUTAGE", "timestamp": "2026-09-30T00:00:00+00:00"})
    assert [e["timestamp"] for e in store.incidents("a.example")] == [e["timestamp"] for e in LOG]
    assert store.incidents(limit=1)[0]["node"] == "b.example"
    assert store.last_incident("a.example", "OUTAGE")["timestamp"] == "2026-09-25T08:00:00+00:00"
    store.update_incident("a.example", "2026-09-25T08:00:00+00:00", "OUTAGE", rootCauseRanking=[{"node": "x"}])
    assert store.last_incident("a.example", "OUTAGE")["rootCauseRanking"] == [{"node": "x"}]
    old = store.take_incidents_before("2026-09-21T00:00:00+00:00")
    assert list(old) == ["a.example"] and len(old["a.example"]) == 3
    assert len(store.incidents("a.example")) == 1  # taken out of the store


# --- the job against Neo4j (only the test's own .pytest.invalid nodes) ---

@pytest.mark.integration
def test_sync_embeds_everything_with_minilm_and_archives_incidents_past_retention(driver, graph, tmp_path):
    from spider import SpiderManager
    from health import NodeHealth
    from tests.conftest import TEST_SUFFIX, domain

    graph({"final": ["FinalLink"], "main": ["MainInput"]}, [("main", "FEEDS", "final")])
    for name, role in [("final", "FinalLink"), ("main", "MainInput")]:
        url = f"https://{domain(name)}/ch/index.m3u8"
        driver.execute_query("MATCH (n:Domain {domain: $d}) SET n.links = $l", d=domain(name),
                             l=json.dumps([{"url": url, "role": role, "channel": "ch"}]))

    class Up:
        async def __call__(self, node):
            return NodeHealth(node_id=node["id"], check_type="HLS", up=True, latency_ms=5)

    store = Metrics(tmp_path / "m.db")
    SpiderManager(driver, checker=Up(), metrics=store).run_cycle([domain("final")])  # creates the SpiderRun
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat(timespec="seconds")
    recent = datetime.now(timezone.utc).isoformat(timespec="seconds")
    store.add_incident(domain("main"), {"type": "OUTAGE", "timestamp": old, "category": "STALE_MEDIA", "consecutiveFailures": 2})
    store.add_incident(domain("main"), {"type": "OUTAGE", "timestamp": recent, "category": "PLAYLIST_MISSING"})

    result = sync_all_embeddings(driver, clear_logs=True, only_suffix=TEST_SUFFIX, incident_store=store)
    assert (result["servers_updated"], result["spiders_updated"]) == (2, 1)
    assert (result["logs_cleared"], result["entries_archived"]) == (1, 1)
    row = driver.execute_query("MATCH (n:Domain {domain: $d}) RETURN n.embedding AS e, n.embeddingHash AS h, "
                               "n.incidentStats AS s", d=domain("main")).records[0]
    assert len(row["e"]) == EMBEDDING_DIM and row["h"]
    assert json.loads(row["s"])["categories"] == {"STALE_MEDIA": 1}  # the old one, now in the totals
    assert [e["category"] for e in store.incidents(domain("main"))] == ["PLAYLIST_MISSING"]  # the recent one stays
    spider = driver.execute_query("MATCH (s:SpiderRun {finalLinkId: $f}) RETURN s.embedding AS e",
                                  f=domain("final")).records[0]
    assert len(spider["e"]) == EMBEDDING_DIM
