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


class _FakeDriver:
    def __init__(self, rows):
        self.rows = rows

    def execute_query(self, query, **params):
        class R:
            records = self.rows
        return R()


def _graph(monkeypatch, domains, rows):
    import types
    monkeypatch.setattr(agent_hub, "_graph_domains", lambda: domains)
    monkeypatch.setattr(agent_hub, "_srv", lambda: types.SimpleNamespace(driver=_FakeDriver(rows)))


def test_agent_node_map_targets_are_resolved_in_the_graph(monkeypatch):
    rows = [{"d": "ingest1.ottlive.co.in", "l": "[]", "ip": "94.136.185.222"}]
    _graph(monkeypatch, DOMAINS, rows)
    monkeypatch.setenv("AGENT_NODE_MAP", "demo:94.136.185.222, box2=cloud, bad:not-a-server")
    assert agent_hub._find_node_for_agent("demo") == "ingest1.ottlive.co.in"  # an IP becomes its server
    assert agent_hub._find_node_for_agent("box2") == "cloud.ottlive.co.in"
    monkeypatch.setattr(agent_hub, "_srv", lambda: __import__("types").SimpleNamespace(driver=_FakeDriver([])))
    assert agent_hub._find_node_for_agent("bad") is None  # never the raw, unmatched value


def test_an_ip_or_channel_on_several_servers_is_not_guessed(monkeypatch):
    ch = '[{"channel": "rangmanch", "url": "x"}]'
    _graph(monkeypatch, DOMAINS, [{"d": "a.example", "l": ch, "ip": "1.2.3.4"},
                                  {"d": "b.example", "l": ch, "ip": "1.2.3.4"}])
    monkeypatch.delenv("AGENT_NODE_MAP", raising=False)
    assert agent_hub._find_node_for_agent("1.2.3.4") is None
    assert agent_hub._find_node_for_agent("rangmanch") is None
    _graph(monkeypatch, DOMAINS, [{"d": "a.example", "l": ch, "ip": "1.2.3.4"}])
    assert agent_hub._find_node_for_agent("1.2.3.4") == "a.example"
    assert agent_hub._find_node_for_agent("rangmanch") == "a.example"


def test_no_encoder_list_is_not_zero_encoders():
    assert summarize({})["encoders"] is None
    assert summarize({"encoders": []})["encoders"] == 0


def test_host_problems_in_plain_words():
    from agent_hub import host_problem
    assert host_problem({"encoders": 0}, had_encoders=True) == "no encoder process is running"
    assert host_problem({"encoders": 0}, had_encoders=False) is None  # an edge never runs one
    assert host_problem({"disk_max": 97.4}, False) == "the disk is 97% full"
    assert host_problem({"cpu": 50, "memory": 60, "disk_max": 40, "encoders": 2}, True) is None


def test_agent_checks_say_what_each_server_sees_from_inside(monkeypatch):
    now = 1000.0
    monkeypatch.setattr(agent_hub, "_online", {
        "ingest1": {"sid": "s1", "node": "ingest1.example", "lastAt": now - 5, "hlsUrl": "https://i/x.m3u8",
                    "hadEncoders": True, "last": {"hls_status": "FRESH", "hls_age_s": 3, "encoders": 1}},
        "cloud": {"sid": None, "node": "cloud.example", "lastAt": now - 400, "offlineSince": now - 300},
        "quiet": {"sid": "s3", "node": "quiet.example", "lastAt": now - 200},
        "new": {"sid": "s4", "node": "new.example", "lastAt": None},
        "nomatch": {"sid": "s5", "node": None, "lastAt": now},
    })
    c = agent_hub.agent_checks(now)
    assert c["ingest1.example"] == {"agent": "ingest1", "state": "fresh", "ago": 5.0, "hlsUrl": "https://i/x.m3u8",
                                    "hlsStatus": "FRESH", "hlsAge": 3, "hostProblem": None}
    assert c["cloud.example"] == {"agent": "cloud", "state": "lost", "ago": 300.0}
    assert c["quiet.example"]["state"] == "lost"  # connected but silent for over a minute
    assert set(c) == {"ingest1.example", "cloud.example", "quiet.example"}
