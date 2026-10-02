"""Ping-first, traceroute-on-threshold: parsing, drop-point analysis, the escalation manager, the spider
hook, and the HTTP endpoints. No real traceroute runs (the runner is faked)."""

import json
from types import SimpleNamespace as NS

import pytest

import traceroute as tr
from health import NodeHealth
from spider import SpiderManager
from tests.conftest import domain
from traceroute import Busy, Cooldown, TracerouteManager, analyse, attach_to_rca, host_of, parse_traceroute

MACOS = """traceroute to jio.ottlive.co.in (45.79.120.240), 20 hops max, 40 byte packets
 1  192.168.1.1  4.239 ms  2.580 ms  2.325 ms
 2  223.177.143.255  6.007 ms  17.584 ms  11.149 ms
 3  122.186.81.173  6.513 ms
    122.186.81.177  11.374 ms  97.736 ms
 4  * 116.119.36.179  156.862 ms  141.009 ms
 5  104.124.54.240  48.074 ms  43.053 ms  39.694 ms
 6  45.79.120.240  39.748 ms  41.748 ms  123.371 ms
"""

LINUX_DROP = """traceroute to 203.0.113.9 (203.0.113.9), 20 hops max, 60 byte packets
 1  172.17.0.1  0.050 ms  0.012 ms  0.010 ms
 2  * * *
 3  10.0.0.1  12.301 ms 10.0.0.2  13.120 ms *
 4  198.51.100.4  40.100 ms !H  41.000 ms  39.900 ms
 5  * * *
 6  * * *
 7  * * *
"""


# --- parsing and analysis ---

def test_parses_macos_output_with_continuation_lines_and_partial_replies():
    parsed = parse_traceroute(MACOS)
    assert parsed["target_ip"] == "45.79.120.240"
    hops = {h["hop"]: h for h in parsed["hops"]}
    assert len(hops) == 6
    assert hops[1] == {**hops[1], "ip": "192.168.1.1", "rtt_ms": 3.0, "lost": 0, "status": "ok"}
    assert hops[3]["ips"] == ["122.186.81.173", "122.186.81.177"] and hops[3]["sent"] == 3
    assert hops[4]["lost"] == 1 and hops[4]["loss_pct"] == 33 and hops[4]["status"] == "slow"  # 148.9 ms avg
    assert hops[5]["status"] == "ok" and hops[6]["status"] == "fair"  # 43.6 ms and 68.3 ms averages


def test_parses_linux_output_with_timeouts_and_flags():
    parsed = parse_traceroute(LINUX_DROP)
    hops = {h["hop"]: h for h in parsed["hops"]}
    assert hops[2]["status"] == "timeout" and hops[2]["ip"] is None and hops[2]["loss_pct"] == 100
    assert hops[3]["ips"] == ["10.0.0.1", "10.0.0.2"] and hops[3]["lost"] == 1
    assert hops[4]["rtts_ms"] == [40.1, 41.0, 39.9]  # "!H" annotations are ignored


def test_destination_reached_means_no_drop_even_with_silent_or_lossy_hops():
    result = analyse(parse_traceroute(MACOS))
    assert result["reached"] is True and result["drop_hop"] is None
    assert result["summary"].startswith("Destination 45.79.120.240 reached in 6 hops; high latency from hop 4")


def test_drop_point_is_the_start_of_the_final_silent_run():
    parsed = parse_traceroute(LINUX_DROP)
    result = analyse(parsed)
    assert result["reached"] is False and result["drop_hop"] == 5
    assert result["summary"] == ("Packet drop detected at hop 5 after hop 4 (198.51.100.4): "
                                 "destination 203.0.113.9 not reached")
    hops = {h["hop"]: h for h in parsed["hops"]}
    assert hops[5]["drop"] and not hops[6]["drop"]
    assert not hops[2]["drop"]  # silent router in the middle: traffic still went further
    assert hops[3]["rate_limited"] and not hops[6]["rate_limited"]


def test_silence_past_a_private_gateway_is_inconclusive_not_a_drop():
    # What Docker Desktop on macOS gives: only the container gateway answers.
    output = "traceroute to x (45.79.120.240)\n 1  172.20.0.1  0.07 ms  0.01 ms  0.01 ms\n" + \
             "".join(f"{n:2d}  * * *\n" for n in range(2, 21))
    parsed = parse_traceroute(output)
    result = analyse(parsed)
    assert result["inconclusive"] is True and result["drop_hop"] is None
    assert result["summary"].startswith("Inconclusive: no replies beyond the local gateway (172.20.0.1)")
    assert not any(h["drop"] for h in parsed["hops"])
    # A public first hop that then goes silent is a real drop point.
    public = analyse(parse_traceroute(output.replace("172.20.0.1", "45.79.120.1")))
    assert public["inconclusive"] is False and public["drop_hop"] == 2


def test_no_drop_when_the_last_hop_answered_but_hops_ran_out():
    result = analyse(parse_traceroute("traceroute to x (192.0.2.1)\n 1  10.0.0.1  1 ms  1 ms  1 ms\n"))
    assert result["drop_hop"] is None and "not reached within 1 hops" in result["summary"]


def test_host_is_the_ip_else_the_domain_without_channel_path():
    assert host_of({"id": "gtc.example/gtcnews", "server_ip": "198.51.100.7"}) == "198.51.100.7"
    assert host_of({"id": "gtc.example/gtcnews"}) == "gtc.example"
    assert host_of({"domain": "a.example", "serverIp": None}) == "a.example"


def test_rca_gets_a_summary_without_hops():
    rca = json.loads(attach_to_rca('{"root_cause": "x"}', {"node": "x", "summary": "s", "hops": [1, 2], "drop_hop": 3}))
    assert rca["root_cause"] == "x" and rca["traceroute"]["drop_hop"] == 3 and "hops" not in rca["traceroute"]
    assert attach_to_rca(None, {}) is None


# --- escalation manager ---

class FakeDriver:
    def __init__(self, spiders=()):
        self.queries, self.spiders = [], list(spiders)

    def execute_query(self, query, **params):
        self.queries.append((query, params))
        if "RETURN collect" in query:
            return NS(records=[{"spiders": self.spiders}])
        return NS(records=[])


@pytest.fixture
def manager():
    runs, events = [], []

    def runner(host):
        runs.append(host)
        return {"target_ip": "192.0.2.1", "hops": [{"hop": 1}], "reached": False, "drop_hop": 1, "hop_count": 1,
                "summary": "Packet drop detected at hop 1"}

    driver = FakeDriver(spiders=[{"id": "spider-f", "rca": '{"root_cause": "a.example"}'}])
    m = TracerouteManager(driver, on_update=events.append, runner=runner)
    m.runs, m.events, m.fake = runs, events, driver
    yield m
    m.pool.shutdown(wait=True)


def settle(m):
    m.pool.shutdown(wait=True)


def test_threshold_starts_one_background_trace_then_cools_down(manager, monkeypatch):
    monkeypatch.setattr(tr, "TRACEROUTE_THRESHOLD", 10)
    node = {"id": "a.example", "server_ip": "192.0.2.1"}
    manager.on_node_checked(node, up=False, consecutive_failures=9)
    manager.on_node_checked(node, up=True, consecutive_failures=0)
    assert manager.runs == [] and manager.events == []
    manager.on_node_checked(node, up=False, consecutive_failures=10)
    manager.on_node_checked(node, up=False, consecutive_failures=11)  # same outage: running, then cooling down
    settle(manager)
    assert manager.runs == ["192.0.2.1"]
    assert [e["state"] for e in manager.events] == ["running", "done"]
    assert manager.events[-1]["trigger"] == "threshold" and manager.events[-1]["drop_hop"] == 1
    with pytest.raises(Cooldown) as err:
        manager.start(node, trigger="threshold")
    assert 0 < err.value.retry_in <= tr.TRACEROUTE_COOLDOWN_S + 1


def test_manual_run_ignores_the_auto_cooldown_but_not_double_clicks(manager):
    node = {"id": "a.example", "server_ip": "192.0.2.1"}
    manager.last_auto["192.0.2.1"] = __import__("time").time()  # an automatic trace just ran
    manager.start(node, trigger="manual")
    with pytest.raises((Busy, Cooldown)):
        manager.start(node, trigger="manual")
    settle(manager)
    assert manager.runs == ["192.0.2.1"]
    assert manager.status("a.example") == {"running": False, "host": None}


def test_result_is_stored_on_the_node_and_in_the_stopped_spiders_rca(manager):
    manager.start({"id": "a.example", "server_ip": "192.0.2.1"})
    settle(manager)
    (save_q, save_p), (rca_q, rca_p) = manager.fake.queries
    assert "SET n.traceroute = $result" in save_q and save_p["node"] == "a.example"
    stored = json.loads(save_p["result"])
    assert stored["hops"] == [{"hop": 1}] and stored["host"] == "192.0.2.1" and stored["trigger"] == "manual"
    assert rca_p["id"] == "spider-f" and json.loads(rca_p["rca"])["traceroute"]["drop_hop"] == 1


def test_failed_traceroute_is_stored_as_an_error_and_frees_the_host():
    def broken(host):
        raise RuntimeError("traceroute is not installed on the server")
    events = []
    m = TracerouteManager(FakeDriver(), on_update=events.append, runner=broken)
    m.start({"id": "a.example"})
    m.pool.shutdown(wait=True)
    assert "not installed" in events[-1]["error"] and events[-1]["state"] == "done"
    assert m.running == {}


# --- spider hook and RCA (real Neo4j) ---

class AllDown:
    async def __call__(self, node):
        return NodeHealth(node_id=node["id"], check_type="HLS", up=False, latency_ms=10, error="HTTP_TIMEOUT")


@pytest.mark.integration
def test_cycle_reports_each_check_to_the_hook_and_rca_carries_the_stored_traceroute(driver, graph):
    graph({"final": ["FinalLink"], "main": ["MainInput"]}, [("main", "FEEDS", "final")])
    # Everything down: the spider stops at the input (main), so main's traceroute goes into the RCA.
    stored = {"node": domain("main"), "host": "main.example", "summary": "Packet drop detected at hop 4",
              "drop_hop": 4, "reached": False, "hops": [{"hop": 4, "drop": True}]}
    driver.execute_query("MATCH (n:Domain {domain: $d}) SET n.traceroute = $t, n.tracerouteAt = datetime()",
                         d=domain("main"), t=json.dumps(stored))
    seen = []
    SpiderManager(driver, checker=AllDown(), on_node_checked=lambda n, up, fails: seen.append((n["id"], up, fails))) \
        .run_cycle([domain("final")])
    assert (domain("final"), False, 1) in seen
    rca = json.loads(driver.execute_query("MATCH (s:SpiderRun {finalLinkId: $f}) RETURN s.rca AS r",
                                          f=domain("final")).records[0]["r"])
    assert domain("main") in rca["failed_nodes"]
    assert rca["traceroute"]["drop_hop"] == 4 and "hops" not in rca["traceroute"]


# --- HTTP ---

@pytest.fixture(scope="module")
def client():
    import server
    from fastapi.testclient import TestClient
    from tests.conftest import TEST_LOGIN
    server.auth.attempts.clear()
    c = TestClient(server.app)
    assert c.post("/login", data={**TEST_LOGIN, "next": "/"}, follow_redirects=False).status_code == 303
    return c


def test_traceroute_endpoints(client, monkeypatch):
    import server
    monkeypatch.setattr(server, "node_for_traceroute", lambda node_id: {"id": node_id, "server_ip": "192.0.2.1"})
    monkeypatch.setattr(server, "stored_traceroute", lambda driver, node_id: {"summary": "ok", "node": node_id})
    r = client.get("/api/nodes/gtc.example/gtcnews/traceroute")
    assert r.status_code == 200 and r.json()["result"]["node"] == "gtc.example/gtcnews" and r.json()["running"] is False

    monkeypatch.setattr(server.tracer, "start", lambda node, trigger: {"node": node["id"], "host": "192.0.2.1",
                                                                       "trigger": trigger})
    r = client.post("/api/nodes/gtc.example/gtcnews/traceroute")
    assert r.status_code == 202 and r.json() == {"node": "gtc.example/gtcnews", "host": "192.0.2.1", "trigger": "manual"}

    def cooling(node, trigger):
        raise Cooldown(12)
    monkeypatch.setattr(server.tracer, "start", cooling)
    r = client.post("/api/nodes/a.example/traceroute")
    assert r.status_code == 429 and r.headers["retry-after"] == "12"


def test_traceroute_needs_login_and_a_known_node(client):
    import server
    from fastapi.testclient import TestClient
    assert TestClient(server.app).post("/api/nodes/a.example/traceroute").status_code == 401
    assert client.get("/api/nodes/nothing.pytest.invalid/traceroute").status_code == 404
