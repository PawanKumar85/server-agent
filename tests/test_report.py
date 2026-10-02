"""The complete HTML report: every chart renders, and every stored field (including the ones the dashboard
hides) reaches the page. Uses a fake driver; no Neo4j needed."""

import base64
import json
import re
from types import SimpleNamespace as NS

import pytest

import report

LOG = [
    {"type": "OUTAGE", "from": "UP", "to": "DOWN", "timestamp": "2026-09-30T06:30:00+00:00", "consecutiveFailures": 1,
     "lastError": "1/1 URLs failing: https://m.example/ch1/index.m3u8 HTTP 404", "lastLatencyMs": 250.0},
    {"type": "RECOVERY", "from": "DOWN", "to": "UP", "timestamp": "2026-09-30T06:35:00+00:00", "consecutiveFailures": 0,
     "lastLatencyMs": 180.0},
]
TRACE = {"host": "203.0.113.5", "target_ip": "203.0.113.5", "summary": "Packet drop detected at hop 3", "trigger": "manual",
         "at": "2026-09-30T06:40:00+00:00", "duration_ms": 4100, "drop_hop": 3, "hops": [
             {"hop": 1, "ip": "192.168.1.1", "ips": ["192.168.1.1"], "rtt_ms": 2.0, "rtts_ms": [2.0, 2.1, 1.9], "loss_pct": 0, "status": "ok"},
             {"hop": 2, "ip": "45.79.1.1", "ips": ["45.79.1.1"], "rtt_ms": 120.0, "rtts_ms": [120.0], "loss_pct": 66, "status": "slow"},
             {"hop": 3, "ip": None, "ips": [], "rtt_ms": None, "rtts_ms": [], "loss_pct": 100, "status": "timeout", "drop": True}]}
EMB = lambda seed: [((i * seed) % 7) / 7 for i in range(384)]  # noqa: E731


def domain(name, roles, links, **props):
    return {"p": {"domain": name, "status": "UP", "pingCount": 200, "failedCount": 4, "consecutiveFailures": 0,
                  "consecutiveSuccesses": 57, "lastLatencyMs": 180.0, "lastRttMs": 41.5, "lastJitterMs": 3.25,
                  "lastPacketLoss": 0.0, "server_ip": "203.0.113.5", "lastPing": "2026-09-30T07:00:00+00:00",
                  "links": json.dumps(links), "embedding": EMB(len(name)), **props}, "roles": roles}


MAIN_URL, FINAL_URL = "https://m.example/ch1/index.m3u8", "https://f.example/ch1/index.m3u8"
NODES = [
    domain("m.example", ["MainInput"], [{"url": MAIN_URL, "role": "MainInput", "channel": "ch1"}],
           urlHealth=json.dumps({MAIN_URL: {"up": True, "detail": "master, 2/2 variants live", "lastDown": "2026-09-30T06:30:00+00:00"}}),
           log=json.dumps(LOG), traceroute=json.dumps(TRACE),
           incidentStats=json.dumps({"outages": 5, "recoveries": 5, "firstIncidentAt": "2026-09-20T01:00:00+00:00",
                                     "lastOutageAt": "2026-09-29T01:00:00+00:00", "maxConsecutiveFailures": 12,
                                     "errors": {"STALE_SEGMENTS": 4}, "archivedEntries": 10})),
    domain("f.example/ch1", ["FinalLink"], [{"url": FINAL_URL, "role": "FinalLink", "channel": "ch1"}],
           status="DOWN", lastError="HTTP_CONNECTERROR after 3 tries",
           urlHealth=json.dumps({FINAL_URL: {"up": False, "detail": "HTTP_CONNECTERROR"}})),
]
SPIDERS = [{"p": {"id": "spider-f.example/ch1", "finalLinkId": "f.example/ch1", "status": "STOPPED", "stepCount": 42,
                  "direction": "UPSTREAM", "stopNodeId": "f.example/ch1", "stopReason": "HTTP_CONNECTERROR",
                  "startedAt": "2026-09-30T01:00:00+00:00", "embedding": EMB(3),
                  "rca": json.dumps({"root_cause": "f.example/ch1", "impact": "Service outage on f.example/ch1",
                                     "consecutive_failures": 12, "alerted": True, "failover": [],
                                     "path": [{"node_id": "f.example/ch1", "role": "FinalLink", "up": False,
                                               "visited": True, "error": "HTTP_CONNECTERROR"}]})},
            "at": "f.example/ch1"}]


class Driver:
    def execute_query(self, query, **_):
        return NS(records=SPIDERS if "SpiderRun" in query else NODES)


@pytest.fixture(autouse=True)
def incident_store(monkeypatch, tmp_path):
    """The incident log lives in the incident store: m.example's LOG goes there (not into node properties)."""
    import metrics
    store = metrics.Metrics(tmp_path / "m.db")
    for entry in LOG:
        store.add_incident("m.example", entry)
    monkeypatch.setattr(metrics, "store", lambda path=None: store)
    return store


@pytest.fixture(autouse=True)
def topology(monkeypatch):
    monkeypatch.setattr(report, "current_topology", lambda driver: [
        NS(model_dump=lambda: {"source": "m.example", "type": "FEEDS", "target": "f.example/ch1"})])


def images(page):
    return re.findall(r"data:image/png;base64,([A-Za-z0-9+/=]+)", page)


def test_all_nodes_report_has_every_chart_and_every_section():
    page = report.build_report(Driver(), monitor={"enabled": True, "interval": 30})
    pngs = images(page)
    # status, topology, reliability, latency, loss, spiders, channels, timeline, errors, server similarity
    # (spider similarity needs two spiders; this data has one)
    assert len(pngs) == 10
    assert all(base64.b64decode(p)[:8] == b"\x89PNG\r\n\x1a\n" for p in pngs)
    for text in ["Stream Graph: full monitoring report", "auto ping ON every 30 s", "Every server", "Every spider",
                 "<b>2</b><span>Servers</span>", "<b>1</b><span>Channels down</span>"]:
        assert text in page


def test_hidden_fields_reach_the_report():
    page = report.build_report(Driver())
    for text in [
        "master, 2/2 variants live",            # per-URL health detail
        "2026-09-30 12:00:00 IST",              # per-URL last down, in IST
        "41.5 ms", "3.25 ms",                   # ICMP RTT and jitter
        "Successes in a row",                   # consecutiveSuccesses
        "STALE_SEGMENTS ×4",                    # archived incident totals
        "Packet drop detected at hop 3",        # traceroute
        "2.0, 2.1, 1.9",                        # every traceroute probe
        "384 dims · L2 norm",                   # embedding stats
        "walked",                               # spider path
        "Failures in a row / alerted",          # spider RCA alert state
        "All stored properties (raw)",          # every raw property
        "&lt;384-dim vector&gt;",               # raw dump summarises the vector
    ]:
        assert text in page, text
    assert page.count("hidden in dashboard") >= 4


def test_node_report_has_its_charts_spiders_and_channels():
    page = report.build_report(Driver(), "m.example")
    assert "Node report: m.example" in page
    assert len(images(page)) == 3  # incidents, traceroute, similar servers
    assert "Channels this node carries" in page and "ch1" in page
    final = report.build_report(Driver(), "f.example/ch1")
    assert "Spiders that walk through this node" in final and "spider-f.example/ch1 stopped here" in final
    assert report.build_report(Driver(), "missing.example") is None


def test_values_are_html_escaped():
    NODES.append(domain("x.example", ["BackupLink"], [], lastError="<script>alert(1)</script>"))
    try:
        page = report.build_report(Driver(), "x.example")
    finally:
        NODES.pop()
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def test_cosine_and_timestamps():
    assert report.cosine([1, 0], [1, 0]) == pytest.approx(1.0) and report.cosine([0, 0], [1, 0]) == 0.0
    assert report._ts("2026-09-30T06:30:00.123456789+00:00").microsecond == 123456
    assert report._ts("2026-09-30T06:30:00.123456789+00:00").utcoffset().total_seconds() == 0
    assert report._ts("nonsense") is None


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


def test_report_endpoint(client, monkeypatch):
    import server
    from fastapi.testclient import TestClient
    monkeypatch.setattr(server, "build_report", lambda driver, node, monitor, actions="", live=False, metrics=None:
                        None if node == "missing" else f"<html>{node}{actions}</html>")
    r = client.get("/api/report.html?node=gtc.example/gtcnews")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "Print / PDF" in r.text and "download=1" in r.text
    d = client.get("/api/report.html?download=1")
    assert "attachment" in d.headers["content-disposition"] and "all-nodes" in d.headers["content-disposition"]
    assert "Print / PDF" not in d.text
    assert client.get("/api/report.html?node=missing").status_code == 404
    assert TestClient(server.app).get("/api/report.html", follow_redirects=False).status_code == 401


# --- key findings and the AI deep analysis ---

def findings(node_id=None, monitor=None):
    return report.insights(report.collect(Driver()), monitor or {"interval": 30}, node_id)


def test_findings_flag_outages_reliability_redundancy_and_spiders():
    items = findings()
    text = [f"[{f['severity']}] {f['finding']}" for f in items]
    assert "[high] f.example/ch1 is DOWN now" in text
    assert "[high] Channel ch1 is down" in text
    assert "[medium] Channel ch1 has no Backup input" in text
    assert "[high] Spider for ch1 is STOPPED" in text
    assert "[medium] m.example flaps: 6 outages recorded" in text  # 5 archived + 1 in the raw log
    assert [f["severity"] for f in items] == sorted((f["severity"] for f in items), key=report.SEVERITY_ORDER.get)


def test_findings_for_one_node_only_concern_that_node(monkeypatch):
    items = findings("m.example")
    assert all("f.example" not in f["finding"] or f["area"] == "Channels" for f in items)
    assert any(f["finding"] == "m.example flaps: 6 outages recorded" for f in items)


def test_main_and_backup_on_the_same_server_is_flagged():
    both = [{"url": "https://s.example/ch2/a.m3u8", "role": "MainInput", "channel": "ch2"},
            {"url": "https://s.example/ch2/b.m3u8", "role": "BackupLink", "channel": "ch2"}]
    NODES.append(domain("s.example", ["MainInput", "BackupLink"], both))
    try:
        items = findings()
    finally:
        NODES.pop()
    assert any(f["finding"] == "Channel ch2's Main and Backup are on the same server" for f in items)


def test_stale_monitoring_and_slow_servers_are_flagged(monkeypatch):
    NODES.append(domain("slow.example", ["Transcoding"], [], lastLatencyMs=1500.0, lastPing="2020-01-01T00:00:00+00:00"))
    try:
        items = {f["finding"]: f["severity"] for f in findings()}
    finally:
        NODES.pop()
    assert items["slow.example is slow: 1500 ms HTTP latency"] == "medium"
    assert any(k.startswith("slow.example has not been checked for") and v == "high" for k, v in items.items())


def test_report_page_has_findings_and_the_analysis_button_only_when_live():
    live = report.build_report(Driver(), live=True)
    assert "Key findings" in live and "id='ai-run'" in live and "/api/report/analysis" in live
    saved = report.build_report(Driver(), live=False)
    assert "Key findings" in saved and "id='ai-run'" not in saved
    node = report.build_report(Driver(), "m.example", live=True)
    assert "Key findings" in node and '"m.example"' in node


def test_analysis_prompt_digest_and_stream(monkeypatch):
    import report_analysis
    prepared = report_analysis.analysis_prompt(Driver(), None, {"interval": 30})
    prompt = prepared["prompt"]
    assert "all 2 servers" in prompt and "every 30 s" in prompt
    assert "- m.example: UP, Main, success 98.0% (4/200 failed)" in prompt
    assert "- ch1: feed Main;" in prompt and "f.example/ch1 (DOWN)" in prompt
    assert "[high] Availability: f.example/ch1 is DOWN now" in prompt
    assert "## Recommendations" in prompt
    assert report_analysis.analysis_prompt(Driver(), "missing.example", None) is None
    one = report_analysis.analysis_prompt(Driver(), "m.example", None)["prompt"]
    assert "server m.example" in one and "- f.example/ch1: DOWN" not in one

    class Bot:
        def generate(self, messages, options, model=None):
            assert messages == [{"role": "user", "content": prompt}] and options["reasoning"]["effort"] == "medium"
            yield {"type": "token", "text": "## Executive summary"}
            yield {"type": "done", "elapsed_ms": 1, "tokens": {"input": 10, "output": 5, "reasoning": 2}}
    events = list(report_analysis.stream_analysis(Bot(), prepared))
    assert [e["type"] for e in events] == ["findings", "token", "done"] and events[0]["findings"] == prepared["findings"]


def test_analysis_endpoint(client, monkeypatch):
    import server
    monkeypatch.setattr(server, "analysis_prompt", lambda driver, node, monitor, metrics=None: None if node == "missing" else
                        {"prompt": "P", "findings": [{"severity": "info"}]})
    monkeypatch.setattr(server.chatbot, "generate", lambda messages, options, model=None: iter(
        [{"type": "token", "text": "ok"}, {"type": "done", "elapsed_ms": 1}]))
    r = client.post("/api/report/analysis?node=m.example")
    assert [json.loads(line)["type"] for line in r.text.splitlines()] == ["findings", "token", "done"]
    assert client.post("/api/report/analysis?node=missing").status_code == 404


def test_analysis_script_keeps_its_escapes():
    # Python must not turn the JS "\n" into a real line break (that broke the button once).
    js = report.analysis_html("m.example", True).split("<script>")[1].split("</script>")[0]
    assert 'split("\\n")' in js and "/^#{1,6}\\s/" in js
    assert all(line.count('"') % 2 == 0 for line in js.splitlines() if "split(" in line)


def test_node_incident_chart_draws_escalations_and_unknown_events():
    log = [{"type": t, "timestamp": f"2026-10-01T05:0{i}:00+00:00", "consecutiveFailures": i, "lastLatencyMs": 200.0}
           for i, t in enumerate(["OUTAGE", "ESCALATED", "RECOVERY", None, "SOMETHING_NEW"])]
    assert base64.b64decode(report.chart_node_incidents({"log": log})).startswith(b"\x89PNG")


def test_sync_embeddings_button_keeps_recent_incidents(client, monkeypatch):
    import server
    seen = {}
    monkeypatch.setattr(server, "run_embedding_sync",
                        lambda source, clear_logs, all_logs=False: seen.update(clear=clear_logs, all=all_logs) or {})
    assert client.post("/api/embeddings/sync?clear_logs=true").status_code == 200
    assert seen == {"clear": True, "all": False}  # only entries past the 90-day retention are archived
