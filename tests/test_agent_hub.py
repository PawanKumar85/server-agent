"""The Node Agent endpoint: matching agents to servers, the numbers kept, and who may connect."""

import agent_hub
from agent_hub import match_node, summarize

DOMAINS = ["ingest1.ottlive.co.in", "ingest.ottlive.co.in", "cloud.ottlive.co.in", "cloud1.ottlive.co.in/tnpnews",
           "cdn.ottlive.co.in/Rang Manch"]


def test_an_agent_is_matched_to_its_server():
    assert match_node("ingest1", DOMAINS) == "ingest1.ottlive.co.in"
    assert match_node("INGEST", DOMAINS) == "ingest.ottlive.co.in"  # not ingest1
    assert match_node("cloud.ottlive.co.in", DOMAINS) == "cloud.ottlive.co.in"
    assert match_node("cdn", DOMAINS) is None  # only channel-path nodes share that label: not guessed
    assert match_node("unknown-box", DOMAINS) is None and match_node("", DOMAINS) is None


def test_the_summary_columns():
    s = summarize({"cpu": {"usage": 91.5, "load1": 3.2}, "memory": {"usage": 70},
                   "disk": [{"usage": 40}, {"usage": 97.5}],
                   "network": [{"rxSec": 1000, "txSec": 500, "rxErrors": 1, "txErrors": 2}, {"rxSec": None}],
                   "encoders": [{"name": "ffmpeg"}], "hls": {"status": "STALE", "segmentAge": 45}})
    assert s == {"cpu": 91.5, "memory": 70, "disk_max": 97.5, "load1": 3.2, "rx_bps": 8000, "tx_bps": 4000,
                 "net_errors": 3, "encoders": 1, "hls_status": "STALE", "hls_age_s": 45}
    assert summarize({})["disk_max"] is None


def test_no_tokens_means_no_agents(monkeypatch):
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    monkeypatch.delenv("AGENT_TOKEN", raising=False)
    assert agent_hub._tokens() == []
    monkeypatch.setenv("AGENT_TOKENS", " a1 , b2 ,")
    assert agent_hub._tokens() == ["a1", "b2"]


def test_the_agent_api_needs_the_dashboard_login_but_the_socket_does_not():
    import server
    from fastapi.testclient import TestClient
    c = TestClient(server.app)
    assert c.get("/api/agents").status_code == 401
    r = c.get("/socket.io/?EIO=4&transport=polling")
    assert r.status_code == 200 and '"sid"' in r.text  # the engine.io handshake answers; the token is checked on connect
