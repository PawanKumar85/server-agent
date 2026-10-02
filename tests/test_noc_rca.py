"""NOC RCA: network-owner lookup, hop labels, fault-zone classification, the filled prompt, and the endpoint.
No network: whois and Ollama are faked."""

import json

import pytest

import noc_rca
from noc_rca import classify, format_hops, label_hops, org_key, parse_cymru
from tests.conftest import domain

CYMRU = """Bulk mode; whois.cymru.com [2026-09-30 12:59:58 +0000]
24560   | 223.177.143.255  | 223.177.128.0/20    | IN | apnic    | 2010-09-14 | AIRTELBROADBAND-AS-AP - Bharti Airtel Ltd., Telemedia Services, IN
9498    | 122.186.81.177   | 122.186.81.0/24     | IN | apnic    | 2008-07-07 | BBIL-AP - BHARTI Airtel Ltd., IN
20940   | 104.124.54.240   | 104.124.54.0/24     | US | arin     | 2014-04-22 | AKAMAI-ASN1 - Akamai International B.V., NL
NA      | 10.214.32.1      | NA                  |    | other    |            | NA
63949   | 45.79.120.240    | 45.79.120.0/21      | US | arin     | 2015-04-29 | AKAMAI-LINODE-AP - Akamai Connected Cloud, SG
"""
ASNS = parse_cymru(CYMRU)
TARGET = "45.79.120.240"


def hop(n, ip, status="ok", rtt=10.0, **extra):
    return {"hop": n, "ip": ip, "ips": [ip] if ip else [], "rtt_ms": None if status == "timeout" else rtt,
            "status": status, "loss_pct": 100 if status == "timeout" else 0, "drop": False, **extra}


def trace(hops, reached=True, drop=None, **extra):
    return {"target_ip": TARGET, "hops": hops, "reached": reached, "drop_hop": drop, **extra}


PATH = [hop(1, "192.168.1.1"), hop(2, "223.177.143.255"), hop(3, "122.186.81.177"), hop(4, "104.124.54.240"),
        hop(5, "10.214.32.1"), hop(6, TARGET)]


def node(**over):
    base = {"id": "jio.example", "ip": TARGET, "roles": ["MainInput"], "channels": ["gtcnews"], "status": "DOWN",
            "consecutiveFailures": 12, "lastError": "HTTP_CONNECTERROR", "httpCode": None, "rttMs": None,
            "packetLoss": 100.0, "latencyMs": None, "backupUp": [], "traceroute": None}
    return {**base, **over}


# --- owners and labels ---

def test_parse_cymru_skips_unrouted_rows_and_keeps_the_org_name():
    assert ASNS["122.186.81.177"] == {"asn": "9498", "name": "BHARTI Airtel Ltd., IN"}
    assert "10.214.32.1" not in ASNS and len(ASNS) == 4


def test_org_key_matches_one_company_across_its_asns():
    assert org_key("Bharti Airtel Ltd., Telemedia Services, IN") == org_key("BHARTI Airtel Ltd., IN") == "bharti airtel"
    assert org_key("Akamai International B.V., NL") != org_key("Akamai Connected Cloud, SG")
    assert org_key(None) is None


def test_hops_are_labelled_local_isp_transit_and_destination():
    labelled = label_hops(trace(PATH), ASNS)
    assert [h["network"] for h in labelled] == ["local", "isp", "isp", "transit", "destination", "destination"]
    assert labelled[1]["owner"] == "AS24560 Bharti Airtel Ltd., Telemedia Services, IN"
    assert labelled[4]["owner"] == "Private address inside a provider"  # belongs to the network that follows


def test_lookup_asns_asks_whois_once_for_public_ips_and_caches(monkeypatch):
    sent = []

    class Sock:
        def __init__(self):
            self.chunks = [CYMRU.encode()]

        def sendall(self, data):
            sent.append(data.decode())

        def recv(self, n):
            return self.chunks.pop() if self.chunks else b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(noc_rca, "_asn_cache", {})
    monkeypatch.setattr(noc_rca.socket, "create_connection", lambda addr, timeout: Sock())
    found = noc_rca.lookup_asns(["192.168.1.1", "104.124.54.240", TARGET, TARGET])
    assert set(found) == {"104.124.54.240", TARGET} and len(sent) == 1
    assert "192.168.1.1" not in sent[0] and sent[0].count(TARGET) == 1
    noc_rca.lookup_asns([TARGET])
    assert len(sent) == 1  # served from the cache


def test_lookup_asns_is_best_effort_when_whois_is_unreachable(monkeypatch):
    monkeypatch.setattr(noc_rca, "_asn_cache", {})

    def refuse(addr, timeout):
        raise OSError("blocked")
    monkeypatch.setattr(noc_rca.socket, "create_connection", refuse)
    assert noc_rca.lookup_asns([TARGET]) == {}


# --- classification ---

def dropped_after(n):
    hops = [h if h["hop"] <= n else hop(h["hop"], None, "timeout") for h in PATH]
    hops[n]["drop"] = True
    return trace(hops, reached=False, drop=n + 1)


def test_healthy_server_has_no_fault():
    t = trace(PATH)
    healthy = classify(node(status="UP", consecutiveFailures=0, backupUp=["b.example"]), t, label_hops(t, ASNS))
    assert healthy["zone"] == "none" and healthy["action"] == "No action needed; keep monitoring."


def test_break_after_a_transit_hop_blames_that_carrier():
    t = dropped_after(4)
    result = classify(node(), t, label_hops(t, ASNS))
    assert result["zone"] == "transit" and result["last_good"] == "104.124.54.240"
    assert result["drop_point"] == "Hop 5 (no reply after hop 4 104.124.54.240, AS20940 Akamai International B.V., NL)"
    assert "carrier NOC for AS20940 Akamai International" in result["action"]


def test_break_inside_the_local_isp_is_a_local_fault():
    t = dropped_after(3)
    result = classify(node(), t, label_hops(t, ASNS))
    assert result["zone"] == "local" and "local ISP NOC (AS9498 BHARTI Airtel" in result["action"]


def test_reachable_path_but_failing_stream_is_the_server_with_failover_first():
    t = trace(PATH)
    result = classify(node(httpCode=404, backupUp=["backup.example"]), t, label_hops(t, ASNS))
    assert result["zone"] == "server" and "network path reaches the server" in result["drop_point"]
    assert result["action"] == ("Failover to BackupLink (backup.example is up) now; then check the publishing point: "
                                "the stream path is missing on the origin (restart the origin encoder).")


def test_ping_alive_without_a_traceroute_points_at_the_server():
    result = classify(node(packetLoss=0.0, httpCode=503), None, [])
    assert result["zone"] == "server" and "restart the origin server" in result["action"].lower()


def test_inconclusive_or_missing_traceroute_with_dead_ping_is_undetermined():
    t = trace([hop(1, "172.20.0.1")] + [hop(n, None, "timeout") for n in range(2, 6)], reached=False, inconclusive=True)
    result = classify(node(), t, label_hops(t, ASNS))
    assert result["zone"] == "unknown" and "inconclusive" in result["drop_point"]
    assert classify(node(), None, [])["drop_point"] == "Unknown (no traceroute yet)."


def test_hops_are_written_like_the_noc_example():
    t = dropped_after(4)
    text = format_hops(label_hops(t, ASNS), t)
    assert "Hop 2: 223.177.143.255 (10.0 ms) - AS24560 Bharti Airtel Ltd., Telemedia Services, IN [OK]" in text
    assert "Hop 5: * * * - TIMEOUT [PACKET LOSS DETECTED - DROP POINT]" in text
    assert text.endswith(f"Destination {TARGET} - DESTINATION UNREACHABLE")
    assert format_hops([], None) == "No traceroute has been run for this server yet."


# --- model choice ---

def test_rca_model_is_rca_model_else_the_chat_model(monkeypatch):
    class Bot:
        model = "qwen/qwen3.8-27b"

    monkeypatch.delenv("RCA_MODEL", raising=False)
    assert noc_rca.rca_model(Bot()) == "qwen/qwen3.8-27b"
    monkeypatch.setenv("RCA_MODEL", "qwen/qwen3.7-plus")
    assert noc_rca.rca_model(Bot()) == "qwen/qwen3.7-plus"


# --- the node from Neo4j (integration) ---

@pytest.mark.integration
def test_analyse_node_fills_the_prompt_from_the_graph(driver, graph, monkeypatch):
    graph({"main": ["MainInput"], "backup": ["BackupLink"], "final": ["FinalLink"]},
          [("main", "FEEDS", "final"), ("backup", "FEEDS", "final")])
    links = lambda url, role: json.dumps([{"url": url, "role": role, "channel": "ch1"}])
    t = dropped_after(4)
    driver.execute_query(
        "MATCH (n:Domain {domain: $d}) SET n.status = 'DOWN', n.consecutiveFailures = 12, n.lastError = 'HTTP 404', "
        "n.lastPacketLoss = 100.0, n.server_ip = $ip, n.links = $links, n.urlHealth = $health, n.traceroute = $trace",
        d=domain("main"), ip=TARGET, links=links("https://m.example/ch1/index.m3u8", "MainInput"),
        health=json.dumps({"https://m.example/ch1/index.m3u8": {"up": False, "detail": "HTTP 404"}}), trace=json.dumps(t))
    driver.execute_query("MATCH (n:Domain {domain: $d}) SET n.status = 'UP', n.links = $links, n.urlHealth = $health",
                         d=domain("backup"), links=links("https://b.example/ch1/index.m3u8", "BackupLink"),
                         health=json.dumps({"https://b.example/ch1/index.m3u8": {"up": True}}))
    monkeypatch.setattr(noc_rca, "lookup_asns", lambda ips: ASNS)
    import chatbot
    monkeypatch.setattr(chatbot, "TEST_TLD", ".not-hidden-here")  # the app hides test domains; this test needs them
    result = noc_rca.analyse_node(driver, domain("main"))
    assert result["zone"] == "transit" and result["node"] == domain("main")
    prompt = result["prompt"]
    assert f"- Target Server: {domain('main')} ({TARGET})" in prompt
    assert "- Stream Channel: ch1 (Main)" in prompt and "- HTTP Health: 404 (HTTP 404)" in prompt
    # Main is down but its Backup is up: fail over first, then chase the carrier.
    assert result["action"].startswith(f"Failover to BackupLink ({domain('backup')} is up) now; then contact the carrier NOC")
    assert "- Consecutive Ping Failures: 12" in prompt and "(Loss: 100%)" in prompt
    assert "- Fault zone: (b) Upstream transit carrier" in prompt and "PACKET LOSS DETECTED - DROP POINT" in prompt
    assert noc_rca.analyse_node(driver, domain("nothing")) is None


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


def test_rca_endpoint_streams_findings_then_the_write_up(client, monkeypatch):
    import server
    monkeypatch.setattr(server, "analyse_node", lambda driver, node_id: None if node_id == "missing" else {
        "node": node_id, "zone": "transit", "zoneLabel": "(b) Upstream transit carrier", "dropPoint": "Hop 5",
        "action": "Contact carrier NOC", "prompt": "PROMPT"})
    monkeypatch.setenv("RCA_MODEL", "qwen/qwen3.7-plus")
    seen = {}

    def generate(messages, options, model=None):
        seen.update(messages=messages, model=model)
        yield {"type": "token", "text": "### Executive Summary\nTransit."}
        yield {"type": "done", "elapsed_ms": 5, "truncated": False, "model": model}
    monkeypatch.setattr(server.chatbot, "generate", generate)
    r = client.post("/api/nodes/jio.example/rca")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in r.text.splitlines()]
    assert [e["type"] for e in events] == ["findings", "token", "done"]
    assert events[0]["zone"] == "transit" and "prompt" not in events[0] and events[0]["model"] == "qwen/qwen3.7-plus"
    assert seen["messages"] == [{"role": "user", "content": "PROMPT"}]
    assert client.post("/api/nodes/missing/rca").status_code == 404


def test_healthy_server_gets_a_fixed_write_up_without_the_model():
    class Bot:
        def generate(self, *a, **k):
            raise AssertionError("the model must not be asked about a healthy server")

    events = list(noc_rca.stream_rca(Bot(), {"zone": "none", "zoneLabel": "No fault", "prompt": "P"}))
    assert [e["type"] for e in events] == ["findings", "token", "done"]
    assert events[1]["text"] == noc_rca.HEALTHY_RCA and "prompt" not in events[0]
