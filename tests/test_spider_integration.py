"""End-to-end spider behaviour on the real Neo4j, with scripted health results.

Every test builds its own isolated topology (domains ending in .pytest.invalid) through the
`graph` fixture, which deletes it afterwards. Health checks are faked, so nothing is fetched
from the network and results are deterministic.
"""

import pytest

from health import NodeHealth
from spider import SpiderManager
from tests.conftest import TEST_SUFFIX, domain

pytestmark = pytest.mark.integration

# Main + Backup -> Transcoding -> Final (the reference chain)
CHAIN = {"final": ["FinalLink"], "trans": ["Transcoding"], "main": ["MainInput"], "backup": ["BackupLink"]}
CHAIN_EDGES = [("main", "FEEDS", "trans"), ("backup", "FEEDS", "trans"), ("trans", "PRODUCES", "final")]


class Scripted:
    """Fake checker: every node is UP unless listed in `down`."""

    def __init__(self):
        self.down = set()
        self.calls = []

    async def __call__(self, node):
        self.calls.append(node["id"])
        up = node["id"] not in self.down
        return NodeHealth(node_id=node["id"], check_type="HLS", up=up, latency_ms=10, error=None if up else "HTTP_TIMEOUT")


@pytest.fixture
def world(driver, graph, tmp_path):
    """Returns run(down=[...], finals=[...]) -> {final_name: SpiderReport}, checking invariants each cycle."""
    from metrics import Metrics
    checker = Scripted()
    manager = SpiderManager(driver, checker=checker, metrics=Metrics(tmp_path / "world.db"))

    def run(down=(), finals=("final",)):
        checker.down = {domain(d) for d in down}
        result = manager.run_cycle([domain(f) for f in finals])
        # invariants after every cycle: one SpiderRun per FinalLink, each with exactly one AT
        rows = driver.execute_query(
            "MATCH (s:SpiderRun) WHERE s.finalLinkId ENDS WITH $t OPTIONAL MATCH (s)-[a:AT]->() "
            "RETURN s.finalLinkId AS f, count(a) AS ats", t=TEST_SUFFIX,
        ).records
        assert sorted(r["f"] for r in rows) == sorted(domain(f) for f in finals), "one SpiderRun per FinalLink"
        assert all(r["ats"] == 1 for r in rows), "exactly one AT per spider"
        return {r.final_link_id.replace(TEST_SUFFIX, ""): r for r in result.reports}

    run.checker, run.manager = checker, manager
    return run


def node_props(driver, name):
    return driver.execute_query("MATCH (n:Domain {domain: $d}) RETURN properties(n) AS p", d=domain(name)).records[0]["p"]


def walked(report):
    return [s.node_id.replace(TEST_SUFFIX, "") for s in report.rca.path]


def test_healthy_chain_walks_final_to_inputs(graph, world):
    graph(CHAIN, CHAIN_EDGES)
    r = world()["final"]
    assert r.status == "RUNNING"
    assert walked(r) == ["final", "trans", "main", "backup"]
    assert r.rca.root_cause is None and r.rca.impact == "No faults found."
    assert r.alerts == []


def test_healthy_spider_goes_home_to_its_final(driver, graph, world):
    graph(CHAIN, CHAIN_EDGES)
    # First 30s: travels from final to main/backup, stops on main
    r1 = world()["final"]
    assert r1.location == domain("main")
    at1 = driver.execute_query("MATCH (s:SpiderRun {id: $id})-[:AT]->(n) RETURN n.domain AS at", id=r1.spider_id).records[0]["at"]
    assert at1 == domain("main")
    # Second 30s: pings from main/backup back to final, stops on final
    r2 = world()["final"]
    assert r2.location == domain("final")
    assert walked(r2) == ["main", "trans", "final"]
    at2 = driver.execute_query("MATCH (s:SpiderRun {id: $id})-[:AT]->(n) RETURN n.domain AS at", id=r2.spider_id).records[0]["at"]
    assert at2 == domain("final")


def test_stopped_spider_stays_at_the_fault(graph, world):
    graph(CHAIN, CHAIN_EDGES)
    assert world(down=["trans", "final"])["final"].location == domain("trans")
    assert world()["final"].location in (domain("final"), domain("main")), "after recovery it continues"


def test_main_down_with_backup_up_is_failover_not_outage(graph, world):
    graph(CHAIN, CHAIN_EDGES)
    world()
    r = world(down=["main"])["final"]
    assert r.status == "RUNNING"
    assert r.rca.failover and "Failover AVAILABLE" in r.rca.failover[0]
    assert "No current service outage" in r.rca.impact
    assert [a.kind for a in r.alerts] == ["FAILOVER_ACTIVE"]


def test_both_inputs_down_stops_at_main_with_input_dependency_failure(driver, graph, world):
    graph(CHAIN, CHAIN_EDGES)
    r = world(down=["main", "backup"])["final"]
    assert r.status == "STOPPED" and r.location == domain("main")
    assert r.rca.root_cause == "Input dependency failure"
    assert set(r.rca.failed_nodes) == {domain("main"), domain("backup")}
    spider = driver.execute_query(
        "MATCH (s:SpiderRun {id: $id})-[:AT]->(n) RETURN n.domain AS at, s.stopNodeId AS stop, s.currentNodeId AS cur, s.status AS st",
        id=r.spider_id,
    ).records[0]
    assert spider["at"] == spider["stop"] == spider["cur"] == domain("main") and spider["st"] == "STOPPED"
    assert "SPIDER_STOPPED" in [a.kind for a in r.alerts]


def test_transcoder_down_is_the_root_cause(graph, world):
    graph(CHAIN, CHAIN_EDGES)
    r = world(down=["trans", "final"])["final"]
    assert r.status == "STOPPED" and r.location == domain("trans")
    assert r.rca.root_cause == domain("trans") and r.rca.outage
    assert [x.replace(TEST_SUFFIX, "") for x in r.rca.impacted] == ["trans", "final"]


def test_final_alone_down_is_the_root_cause(graph, world):
    graph(CHAIN, CHAIN_EDGES)
    r = world(down=["final"])["final"]
    assert r.status == "STOPPED" and r.location == domain("final") and r.rca.root_cause == domain("final")


def test_everything_down_blames_the_inputs(graph, world):
    graph(CHAIN, CHAIN_EDGES)
    r = world(down=["final", "trans", "main", "backup"])["final"]
    assert r.rca.root_cause == "Input dependency failure" and r.location == domain("main")


def test_spider_stops_without_recording_nodes_upstream_of_the_root(driver, graph, world):
    graph(CHAIN, CHAIN_EDGES)
    world.manager.sweep = False  # the spider-only mode (HEALTH_SWEEP=0)
    r = world(down=["final"])["final"]
    # Probes for every upstream node start early (for speed), but a node's health is only written
    # when a spider actually reaches it: past the root cause nothing is walked or recorded.
    assert walked(r) == ["final", "trans"]
    assert node_props(driver, "main").get("pingCount") is None
    assert node_props(driver, "backup").get("pingCount") is None
    assert node_props(driver, "trans")["pingCount"] == 1  # checked from downstream to confirm it's healthy


def test_sweep_records_every_node_each_cycle_without_changing_the_walk(driver, graph, world):
    graph(CHAIN, CHAIN_EDGES)
    r = world(down=["final"])["final"]  # sweep is on by default
    assert walked(r) == ["final", "trans"] and r.rca.root_cause == domain("final")  # the spider still stops
    for name in ("final", "trans", "main", "backup"):
        assert node_props(driver, name)["pingCount"] == 1  # but every node was checked and recorded


def test_stop_repeat_recover_lifecycle(driver, graph, world):
    graph(CHAIN, CHAIN_EDGES)
    world()
    stopped = world(down=["main", "backup"])["final"]
    assert stopped.status == "STOPPED"

    again = world(down=["main", "backup"])["final"]
    assert again.status == "STOPPED" and again.alerts == [], "no repeated alert while nothing changes"
    main = node_props(driver, "main")
    assert (main["status"], main["failedCount"], main["consecutiveFailures"]) == ("DOWN", 2, 2)

    recovered = world()["final"]
    assert recovered.status == "RECOVERED" and "SPIDER_RECOVERED" in [a.kind for a in recovered.alerts]
    main = node_props(driver, "main")
    assert main["status"] == "UP" and main["consecutiveSuccesses"] == 1 and main.get("lastRecovery") is not None

    assert world()["final"].status == "RUNNING"


def test_node_counters(driver, graph, world):
    graph(CHAIN, CHAIN_EDGES)
    world(); world(down=["final"]); world()
    final = node_props(driver, "final")
    assert final["pingCount"] == 3 and final["failedCount"] == 1
    assert final["consecutiveSuccesses"] == 1 and final["consecutiveFailures"] == 0


def test_pass_through_channel(graph, world):
    graph({"final": ["FinalLink"], "main": ["MainInput"]}, [("main", "FEEDS", "final")])
    assert world()["final"].status == "RUNNING"
    r = world(down=["main"])["final"]
    assert r.status == "STOPPED" and r.location == domain("main")


def test_self_fed_transcoder_survives_backup_loss(graph, world):
    graph({"final": ["FinalLink"], "live1": ["MainInput", "Transcoding"], "ingest3": ["BackupLink"]},
          [("ingest3", "FEEDS", "live1"), ("live1", "PRODUCES", "final")])
    assert world(down=["ingest3"])["final"].status == "RUNNING"


def test_missing_main_input_is_a_topology_error(graph, world):
    graph({"final": ["FinalLink"], "trans": ["Transcoding"], "backup": ["BackupLink"]},
          [("backup", "FEEDS", "trans"), ("trans", "PRODUCES", "final")])
    r = world()["final"]
    assert r.status == "ERROR" and [a.kind for a in r.alerts] == ["SPIDER_ERROR"]


def test_one_spider_per_final_and_shared_nodes_checked_once(driver, graph, world):
    graph({"f1": ["FinalLink"], "f2": ["FinalLink"], "trans": ["Transcoding"], "main": ["MainInput"]},
          [("main", "FEEDS", "trans"), ("trans", "PRODUCES", "f1"), ("trans", "PRODUCES", "f2")])
    reports = world(finals=("f1", "f2"))
    assert set(reports) == {"f1", "f2"} and all(r.status == "RUNNING" for r in reports.values())
    assert node_props(driver, "trans")["pingCount"] == 1, "both spiders share one check of the transcoder"

    down = world(down=["trans"], finals=("f1", "f2"))
    assert all(r.status == "STOPPED" and r.location == domain("trans") for r in down.values())


def test_shared_node_is_pinged_by_one_spider_and_reused_by_the_rest(graph, world):
    graph({"f1": ["FinalLink"], "f2": ["FinalLink"], "f3": ["FinalLink"], "trans": ["Transcoding"], "main": ["MainInput"]},
          [("main", "FEEDS", "trans"), ("trans", "PRODUCES", "f1"), ("trans", "PRODUCES", "f2"), ("trans", "PRODUCES", "f3")])
    events = world.manager.run_cycle([domain(f) for f in ("f1", "f2", "f3")]).events
    for node in (domain("trans"), domain("main")):
        checks = [e for e in events if e["kind"] == "check" and e["node"] == node]
        owners = [e for e in checks if not e.get("shared_from")]
        assert len(checks) == 3, f"all three spiders reach {node}"
        assert len(owners) == 1, f"only one spider pings {node}"
        assert all(e["shared_from"] == owners[0]["spider"] for e in checks if e.get("shared_from"))


def test_ensure_spiders_never_duplicates(driver, graph, world):
    graph(CHAIN, CHAIN_EDGES)
    for _ in range(3):
        world.manager.ensure_spiders([domain("final")])
    rows = driver.execute_query(
        "MATCH (s:SpiderRun {finalLinkId: $f}) OPTIONAL MATCH (s)-[a:AT]->() RETURN count(DISTINCT s) AS spiders, count(a) AS ats",
        f=domain("final"),
    ).records[0]
    assert (rows["spiders"], rows["ats"]) == (1, 1)


def test_automatic_runs_and_dashboard_ignore_test_fixtures(driver, graph, world):
    """*.invalid nodes (like these fixtures) never get real spiders or appear in the UI."""
    from graph_view import fetch_graph
    from nodes import current_topology
    from spider import all_spiders, get_all_final_links
    graph(CHAIN, CHAIN_EDGES)
    world()  # creates this test's SpiderRun
    assert not any(f.endswith(TEST_SUFFIX) for f in get_all_final_links(driver))
    g = fetch_graph(driver)
    assert not any(n["id"].endswith(TEST_SUFFIX) for n in g["nodes"])
    assert not any(s["finalLinkId"].endswith(TEST_SUFFIX) for s in g["spiders"])
    assert not any(e.source.endswith(TEST_SUFFIX) for e in current_topology(driver))
    assert not any(s["spider"]["finalLinkId"].endswith(TEST_SUFFIX) for s in all_spiders(driver))


def test_saving_the_real_topology_leaves_test_fixtures_alone(driver, graph):
    from nodes import current_topology, replace_topology
    graph(CHAIN, CHAIN_EDGES)
    replace_topology(driver, current_topology(driver))  # what the Relationships "Save" button does
    left = driver.execute_query(
        "MATCH (a:Domain)-[r:FEEDS|PRODUCES]->(:Domain) WHERE a.domain ENDS WITH $t RETURN count(r) AS c", t=TEST_SUFFIX,
    ).records[0]["c"]
    assert left == len(CHAIN_EDGES)


def test_events_timeline_is_recorded_in_order(graph, world):
    graph(CHAIN, CHAIN_EDGES)
    events = world.manager.run_cycle([domain("final")]).events
    kinds = [e["kind"] for e in events]
    assert kinds[0] in ("move", "check") and kinds[-1] == "finish"
    times = [e["t"] for e in events]
    assert times == sorted(times)
    checked = [e["node"] for e in events if e["kind"] == "result"]
    assert checked[:2] == [domain("final"), domain("trans")]


def test_alert_consecutive_threshold(graph, world):
    graph(CHAIN, CHAIN_EDGES)
    world.manager.alert_threshold = 10

    # Cycles 1 to 9: node is down, but alerts are suppressed until 10 consecutive failures
    for i in range(1, 10):
        r = world(down=["final"])["final"]
        assert r.status == "STOPPED"
        assert r.alerts == [], f"Alert should be suppressed at cycle {i} (< 10)"

    # Cycle 10: threshold reached -> triggers SPIDER_STOPPED alert
    r10 = world(down=["final"])["final"]
    assert r10.status == "STOPPED"
    assert "SPIDER_STOPPED" in [a.kind for a in r10.alerts]
    assert "[failed 10 consecutive times]" in r10.alerts[0].message

    # Cycle 11: ongoing failure -> no duplicate alert
    r11 = world(down=["final"])["final"]
    assert r11.status == "STOPPED"
    assert r11.alerts == [], "No duplicate alert on cycle 11"

    # Cycle 12: recovery -> triggers SPIDER_RECOVERED alert
    r12 = world()["final"]
    assert r12.status == "RECOVERED"
    assert "SPIDER_RECOVERED" in [a.kind for a in r12.alerts]


def test_transient_failure_under_threshold_never_alerts(graph, world):
    graph(CHAIN, CHAIN_EDGES)
    world.manager.alert_threshold = 10

    # Flap for 3 cycles then recover: should never alert stopped or recovered
    for _ in range(3):
        r = world(down=["final"])["final"]
        assert r.status == "STOPPED"
        assert r.alerts == []

    recovered = world()["final"]
    assert recovered.status == "RECOVERED"
    assert recovered.alerts == [], "No recovery alert if outage never alerted"


def test_incident_opens_after_two_failures_escalates_and_closes_after_two_recoveries(driver, graph, world):
    import json
    graph(CHAIN, CHAIN_EDGES)
    world.manager.alert_threshold = 3
    log = lambda: world.manager.metrics.incidents(domain("final"))  # noqa: E731  (the incident store)

    world()                      # baseline UP
    world(down=["final"])        # 1 failure: not an incident yet
    assert log() == []
    world()                      # recovered after 1 failure: a blip, not an outage
    props = node_props(driver, "final")
    assert log() == [] and props["blipCount"] == 1 and json.loads(props["lastBlip"])["failures"] == 1

    world(down=["final"])        # 1st failure
    world(down=["final"])        # 2nd failure in a row: the incident opens
    entries = log()
    assert [e["type"] for e in entries] == ["OUTAGE"] and entries[0]["consecutiveFailures"] == 2
    assert "correlation" in entries[0] and "pingCount" in entries[0]
    world(down=["final"])        # 3rd = alert threshold: escalated, still one incident
    assert [e["type"] for e in log()] == ["OUTAGE", "ESCALATED"]
    world()                      # 1 healthy check: not resolved yet
    assert len(log()) == 2 and node_props(driver, "final")["incidentOpen"] is True
    world()                      # 2 healthy checks in a row: resolved
    entries = log()
    assert [e["type"] for e in entries] == ["OUTAGE", "ESCALATED", "RECOVERY"]
    assert entries[2]["durationS"] >= 0 and node_props(driver, "final")["incidentOpen"] is False


class UrlScripted:
    """Fake checker at URL level: each node has one URL; `stale` nodes answer with stale media."""

    def __init__(self):
        self.stale = set()

    async def __call__(self, node):
        from health import UrlCheck
        up = node["id"] not in self.stale
        url = f"https://{node['id']}/c/index.m3u8"
        check = UrlCheck(url=url, up=up, detail="master, 1/1 variants live" if up else "STALE_SEGMENTS (45s old)",
                         category=None if up else "STALE_MEDIA", freshness="FRESH" if up else "STALE",
                         segment_age_s=3.0 if up else 45.0, target_duration_s=6.0)
        return NodeHealth(node_id=node["id"], check_type="HLS", up=up, latency_ms=10, urls=[check],
                          error=None if up else f"1/1 URLs failing: {url} {check.detail}")


def test_incident_correlates_the_channel_chain(driver, graph, tmp_path):
    import json
    from metrics import Metrics
    graph(CHAIN, CHAIN_EDGES)
    for name, role in [("main", "MainInput"), ("backup", "BackupLink"), ("trans", "Transcoding"), ("final", "FinalLink")]:
        url = f"https://{domain(name)}/c/index.m3u8"
        driver.execute_query("MATCH (n:Domain {domain: $d}) SET n.url = [$u], n.links = $l", d=domain(name), u=url,
                             l=json.dumps([{"url": url, "role": role, "channel": "c"}]))
    checker = UrlScripted()
    manager = SpiderManager(driver, checker=checker, metrics=Metrics(tmp_path / "m.db"))
    manager.run_cycle([domain("final")])
    checker.stale = {domain("backup")}           # only the Backup input goes stale
    # Inputs are checked on the upstream leg (every other cycle): 2 failed checks in a row take 4 cycles.
    results = [manager.run_cycle([domain("final")]) for _ in range(4)]
    [entry] = manager.metrics.incidents(domain("backup"))
    assert entry["category"] == "STALE_MEDIA" and entry["failedUrls"][0]["segmentAgeS"] == 45.0
    [corr] = entry["correlation"]
    assert corr["channel"] == "c" and corr["verdict"] == "LOCAL_TO_NODE"
    assert {e["role"] for e in corr["chain"]} == {"MainInput", "BackupLink", "Transcoding", "FinalLink"}
    assert any(i["node"] == domain("backup") and i["type"] == "OUTAGE" for r in results for i in r.incidents)


def test_cycle_ranks_the_root_cause_and_attaches_it_to_the_incident(driver, graph, tmp_path):
    import json
    from metrics import Metrics
    graph(CHAIN, CHAIN_EDGES)
    checker = Scripted()
    manager = SpiderManager(driver, checker=checker, metrics=Metrics(tmp_path / "m.db"))
    manager.run_cycle([domain("final")])
    checker.down = {domain("trans"), domain("final")}  # the transcoder breaks; the final follows
    manager.run_cycle([domain("final")])
    result = manager.run_cycle([domain("final")])      # 2nd failure: incidents open, ranking attached
    [group] = result.ranking
    assert group["ranking"][0]["node"] == domain("trans")
    entry = manager.metrics.incidents(domain("trans"))[-1]
    assert entry["type"] == "OUTAGE" and entry["rootCauseRanking"][0]["node"] == domain("trans")
    assert len(manager.metrics.history(domain("main"), since_s=600)) == 3  # the sweep recorded every node each cycle



def test_health_writes_are_batched(driver, graph, tmp_path):
    from metrics import Metrics
    # Three channels' spiders run at once: their first checks finish together and share one write.
    graph({"f1": ["FinalLink"], "f2": ["FinalLink"], "f3": ["FinalLink"], "main": ["MainInput"]},
          [("main", "FEEDS", "f1"), ("main", "FEEDS", "f2"), ("main", "FEEDS", "f3")])
    result = SpiderManager(driver, checker=Scripted(), metrics=Metrics(tmp_path / "m.db")).run_cycle(
        [domain("f1"), domain("f2"), domain("f3")])
    assert sum(result.write_batches) == 4  # every node written once
    assert len(result.write_batches) < 4 and max(result.write_batches) >= 2  # fewer transactions than nodes
