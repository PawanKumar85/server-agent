"""Spider logic that needs no database: per-URL 'last down' tracking and dependency rules."""

import json

import pytest

from health import UrlCheck
from spider import Topology, merge_url_health


T1, T2, T3 = "2026-09-30T12:00:00+00:00", "2026-09-30T12:00:30+00:00", "2026-09-30T12:01:00+00:00"


def core(entry):  # the fields these tests are about (onset tracking adds more, tested separately)
    return {k: entry.get(k) for k in ("up", "detail", "lastDown")}


class TestMergeUrlHealth:
    def check(self, url, up, detail="ok"):
        return UrlCheck(url=url, up=up, detail=detail)

    def test_first_check_up_has_no_last_down(self):
        state = json.loads(merge_url_health(None, [self.check("u1", True)], T1))
        assert core(state["u1"]) == {"up": True, "detail": "ok", "lastDown": None}

    def test_first_check_down_records_outage_start(self):
        state = json.loads(merge_url_health(None, [self.check("u1", False, "HTTP 404")], T1))
        assert state["u1"]["lastDown"] == T1 and state["u1"]["up"] is False

    def test_lastdown_is_when_the_outage_started_not_each_failure(self):
        s1 = merge_url_health(None, [self.check("u1", False)], T1)
        s2 = merge_url_health(s1, [self.check("u1", False)], T2)  # still down
        assert json.loads(s2)["u1"]["lastDown"] == T1

    def test_lastdown_kept_after_recovery_and_updated_on_next_outage(self):
        s = merge_url_health(None, [self.check("u1", False)], T1)
        s = merge_url_health(s, [self.check("u1", True)], T2)
        assert core(json.loads(s)["u1"]) == {"up": True, "detail": "ok", "lastDown": T1}
        s = merge_url_health(s, [self.check("u1", False)], T3)
        assert json.loads(s)["u1"]["lastDown"] == T3

    def test_urls_are_tracked_independently(self):
        s = merge_url_health(None, [self.check("a", True), self.check("b", False)], T1)
        s = merge_url_health(s, [self.check("a", False), self.check("b", False)], T2)
        state = json.loads(s)
        assert state["a"]["lastDown"] == T2 and state["b"]["lastDown"] == T1

    def test_no_url_checks_leaves_state_untouched(self):
        assert merge_url_health('{"u1": {"up": true}}', [], T1) is None  # caller keeps the stored value

    def test_corrupt_previous_state_is_ignored(self):
        assert json.loads(merge_url_health("not json", [self.check("u1", True)], T1))["u1"]["up"] is True


class FakeRecords:
    def __init__(self, rows):
        self.records = rows


class FakeDriver:
    """Answers Topology's single query from an in-memory description of the graph."""

    def __init__(self, labels, edges, links=None):
        self.labels, self.edges, self.links = labels, edges, links or {}

    def execute_query(self, query, **params):
        rows = []
        for node, labels in self.labels.items():
            upstream = [{"type": t, "source": s} for s, t, d in self.edges if d == node]
            links = json.dumps(self.links[node]) if node in self.links else None
            rows.append({"id": node, "labels": labels, "url": [], "server_ip": None, "checkType": None,
                         "links": links, "upstream": upstream})
        return FakeRecords(rows)


def topology(labels, edges, finals=("final",), links=None):
    return Topology(FakeDriver(labels, edges, links), list(finals))


def units(topo, node, channel=None):
    return [(u.kind, sorted(u.ids)) for u in topo.units(topo.node(node), channel)]


def link(channel, role):
    return {"url": f"https://x/{channel}/{role}.m3u8", "role": role, "channel": channel}


class TestChannelSpiders:
    """A shared transcoder is fed by different inputs per channel; a channel's spider only follows its own."""

    LABELS = {"final_a": ["FinalLink"], "final_b": ["FinalLink"], "ingest": ["BackupLink", "Transcoding"],
              "jio": ["MainInput"], "cloud": ["MainInput"]}
    EDGES = [("jio", "FEEDS", "ingest"), ("cloud", "FEEDS", "ingest"),
             ("ingest", "PRODUCES", "final_a"), ("ingest", "PRODUCES", "final_b")]
    LINKS = {
        "final_a": [link("gtcnews", "FinalLink")], "final_b": [link("gtcpunjabi", "FinalLink")],
        "jio": [link("gtcnews", "MainInput")], "cloud": [link("gtcpunjabi", "MainInput")],
        "ingest": [link("gtcnews", "BackupLink"), link("gtcpunjabi", "BackupLink"),
                   link("gtcnews", "Transcoding"), link("gtcpunjabi", "Transcoding")],
    }

    def topo(self):
        return topology(self.LABELS, self.EDGES, finals=("final_a", "final_b"), links=self.LINKS)

    def test_final_knows_its_channel(self):
        t = self.topo()
        assert t.node("final_a").final_channel == "gtcnews" and t.node("jio").final_channel is None

    def test_channel_spider_sees_only_its_own_inputs(self):
        t = self.topo()
        # gtcnews: Main jio, Backup ingest itself; cloud (gtcpunjabi's Main) is not its input
        assert units(t, "ingest", "gtcnews") == [("INPUTS", ["ingest", "jio"])]
        assert units(t, "ingest", "gtcpunjabi") == [("INPUTS", ["cloud", "ingest"])]

    def test_without_a_channel_all_inputs_count(self):
        assert units(self.topo(), "ingest") == [("INPUTS", ["cloud", "ingest", "jio"])]

    def test_no_channel_data_falls_back_to_all_inputs(self):
        t = topology(self.LABELS, self.EDGES, finals=("final_a",))
        assert units(t, "ingest", "gtcnews") == [("INPUTS", ["cloud", "ingest", "jio"])]


class TestDependencies:
    def test_final_depends_on_its_transcoder_and_transcoder_on_its_inputs(self):
        t = topology(
            {"final": ["FinalLink"], "trans": ["Transcoding"], "main": ["MainInput"], "backup": ["BackupLink"]},
            [("trans", "PRODUCES", "final"), ("main", "FEEDS", "trans"), ("backup", "FEEDS", "trans")],
        )
        assert units(t, "final") == [("TRANSCODER", ["trans"])]
        # Main and Backup are alternatives: one OR-group, not two required units
        assert units(t, "trans") == [("INPUTS", ["backup", "main"])]
        assert units(t, "main") == []

    def test_every_transcoder_of_a_final_is_required(self):
        t = topology(
            {"final": ["FinalLink"], "t1": ["Transcoding"], "t2": ["Transcoding"]},
            [("t1", "PRODUCES", "final"), ("t2", "PRODUCES", "final")],
        )
        assert units(t, "final") == [("TRANSCODER", ["t1"]), ("TRANSCODER", ["t2"])]

    def test_pass_through_channel_feeds_final_directly(self):
        t = topology(
            {"final": ["FinalLink"], "main": ["MainInput"], "backup": ["BackupLink"]},
            [("main", "FEEDS", "final"), ("backup", "FEEDS", "final")],
        )
        assert units(t, "final") == [("INPUTS", ["backup", "main"])]

    def test_transcoder_with_an_input_role_counts_itself_as_an_input(self):
        # e.g. live1: Main for its own channel and the transcoder for it
        t = topology(
            {"final": ["FinalLink"], "live1": ["MainInput", "Transcoding"], "ingest3": ["BackupLink"]},
            [("live1", "PRODUCES", "final"), ("ingest3", "FEEDS", "live1")],
        )
        assert units(t, "live1") == [("INPUTS", ["ingest3", "live1"])]

    def test_feeds_from_a_node_without_an_input_role_are_ignored(self):
        t = topology(
            {"final": ["FinalLink"], "t1": ["Transcoding"], "t2": ["Transcoding"]},
            [("t1", "PRODUCES", "final"), ("t2", "FEEDS", "t1")],
        )
        assert units(t, "t1") == []

    @pytest.mark.parametrize("labels,edges,expected", [
        # MainInput -> Transcoding -> Final
        ({"final": ["FinalLink"], "t": ["Transcoding"], "m": ["MainInput"]}, [("t", "PRODUCES", "final"), ("m", "FEEDS", "t")], True),
        # MainInput -> Final (no transcoder)
        ({"final": ["FinalLink"], "m": ["MainInput"]}, [("m", "FEEDS", "final")], True),
        # transcoder that is itself a MainInput
        ({"final": ["FinalLink"], "t": ["MainInput", "Transcoding"]}, [("t", "PRODUCES", "final")], True),
        # only a backup: MainInput is required
        ({"final": ["FinalLink"], "t": ["Transcoding"], "b": ["BackupLink"]}, [("t", "PRODUCES", "final"), ("b", "FEEDS", "t")], False),
        # nothing upstream at all
        ({"final": ["FinalLink"]}, [], False),
    ])
    def test_main_input_is_required(self, labels, edges, expected):
        assert topology(labels, edges).has_main_input("final") is expected



# --- media sequence stall, correlation verdicts ---

from datetime import datetime, timedelta, timezone  # noqa: E402

from spider import apply_sequence_stall, verdict  # noqa: E402


def _check(seq, up=True):
    return UrlCheck(url="https://a.example/c.m3u8", up=up, detail="master, 1/1 variants live",
                    sequences={"v1.m3u8": seq}, target_duration_s=6.0)


def test_sequence_stall_marks_a_stuck_playlist_down():
    t0 = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    state = merge_url_health(None, [_check(100)], t0.isoformat())
    same_soon = [_check(100)]
    assert apply_sequence_stall(state, same_soon, t0 + timedelta(seconds=10)) == [] and same_soon[0].up  # < 3 segments
    stuck = [_check(100)]
    assert apply_sequence_stall(state, stuck, t0 + timedelta(seconds=30)) == stuck
    assert not stuck[0].up and stuck[0].category == "STALE_MEDIA"
    assert stuck[0].detail == "STALE_SEQUENCE (media sequence stuck at 100 for 30s)"
    moving = [_check(105)]
    assert apply_sequence_stall(state, moving, t0 + timedelta(seconds=30)) == [] and moving[0].up
    assert apply_sequence_stall(None, [_check(100)], t0) == []  # nothing to compare with yet


def test_merge_keeps_when_a_sequence_number_was_first_seen():
    t0, t1 = "2026-09-30T12:00:00+00:00", "2026-09-30T12:00:30+00:00"
    first = merge_url_health(None, [_check(100)], t0)
    again = json.loads(merge_url_health(first, [_check(100)], t1))["https://a.example/c.m3u8"]["seq"]
    moved = json.loads(merge_url_health(first, [_check(101)], t1))["https://a.example/c.m3u8"]["seq"]
    assert again == {"v1.m3u8": {"n": 100, "since": t0}} and moved == {"v1.m3u8": {"n": 101, "since": t1}}


def _chain(**down):
    roles = {"main": "MainInput", "backup": "BackupLink", "trans": "Transcoding", "final": "FinalLink"}
    return [{"role": r, "node": n, "up": not down.get(n, False)} for n, r in roles.items()]


@pytest.mark.parametrize("node, down, expected", [
    ("backup", {"backup": True}, "LOCAL_TO_NODE"),
    ("backup", {"backup": True, "main": True}, "SHARED_UPSTREAM"),
    ("trans", {"trans": True}, "TRANSCODER"),
    ("trans", {"trans": True, "main": True, "backup": True}, "UPSTREAM"),
    ("final", {"final": True}, "FINAL_ORIGIN"),
    ("final", {"final": True, "trans": True}, "UPSTREAM"),
])
def test_correlation_verdicts(node, down, expected):
    assert verdict("c", node, _chain(**down))["verdict"] == expected


def test_shared_upstream_verdict_names_the_inputs_and_viewer_impact():
    result = verdict("c", "backup", _chain(backup=True, main=True, final=True))
    assert "Every input of c is failing (main (Main), backup (Backup))" in result["summary"]
    assert "viewers affected" in result["summary"]


# --- failure onset ---

from spider import onset_fields  # noqa: E402


def _media(up, age=None, detail="master, 1/1 variants live", category=None):
    return UrlCheck(url="u", up=up, detail=detail, category=category, segment_age_s=age, target_duration_s=6.0)


def test_onset_from_segment_timestamps_corrects_for_the_sources_usual_age():
    # healthy checks: this source's newest segment is usually -18 s "old" (its clock runs ahead)
    old = {"up": True, **onset_fields({}, _media(True, age=-18.0), T1, {})}
    assert old["ageBaseline"] == -18.0 and old["lastOkAt"] == T1
    stale = onset_fields(old, _media(False, age=40.0, detail="STALE_SEGMENTS (40s old)", category="STALE_MEDIA"), T3, {})
    # it stopped 40 - (-18) = 58 s before T3, i.e. 2 s after T1 (not 40 s before T3)
    assert stale["onsetAt"] == "2026-09-30T12:00:02+00:00" and stale["onsetMethod"] == "segment timestamps"
    assert stale["onsetPrecisionS"] == 6.0 and stale["failingSince"] == T3


def test_onset_between_checks_for_http_errors_and_kept_during_the_outage():
    old = {"up": True, "lastOkAt": T1}
    first = onset_fields(old, _media(False, detail="HTTP 404", category="PLAYLIST_MISSING"), T3, {})
    assert first["onsetAt"] == "2026-09-30T12:00:30+00:00" and first["onsetPrecisionS"] == 30.0
    assert first["onsetMethod"] == "between checks"
    later = onset_fields({"up": False, **first}, _media(False, detail="HTTP 404", category="PLAYLIST_MISSING"),
                         "2026-09-30T12:05:00+00:00", {})
    assert later["onsetAt"] == first["onsetAt"] and later["failingSince"] == T3  # kept for the whole outage
    assert "onsetAt" not in onset_fields({"up": False, **first}, _media(True), T3, {})  # cleared on recovery


def test_onset_of_a_stuck_sequence_is_when_that_number_first_appeared():
    fields = onset_fields({"up": True, "lastOkAt": T2},
                          _media(False, detail="STALE_SEQUENCE (media sequence stuck at 5 for 60s)", category="STALE_MEDIA"),
                          T3, {"v": {"n": 5, "since": T1}})
    assert fields["onsetAt"] == T1 and fields["onsetMethod"] == "sequence"


def test_timestamp_glitches_do_not_move_the_usual_age():
    old = {"up": True, **onset_fields({}, _media(True, age=-5.0), T1, {})}
    glitch = onset_fields(old, _media(True, age=-16735.0), T2, {})  # the source's timestamps jumped by hours
    assert glitch["ageBaseline"] == -5.0
    normal = onset_fields(old, _media(True, age=5.0), T2, {})
    assert normal["ageBaseline"] == -4.0  # 0.9 * -5 + 0.1 * 5


def _late(seq, age):
    """A master whose only variant's timestamps look `age` s old (STALE by timestamps)."""
    return UrlCheck(url="https://a.example/c.m3u8", up=False, category="STALE_MEDIA", freshness="STALE",
                    detail=f"master, 0/1 variants live (STALE_SEGMENTS ({int(age)}s old))",
                    sequences={"v1.m3u8": seq}, segment_age_s=age, target_duration_s=6.0)


def test_a_late_source_clock_is_not_a_stale_stream():
    from spider import apply_clock_offset
    t0 = "2026-10-01T07:00:00+00:00"
    before = merge_url_health(None, [_late(100, 41.0)], t0)
    # Sequence moved on and the age held steady: the stream is live, its clock just runs ~40 s late.
    moving = [_late(105, 43.0)]
    assert apply_clock_offset(before, moving) == moving
    assert moving[0].up and moving[0].category is None and moving[0].freshness == "FRESH"
    assert "source clock runs late" in moving[0].detail
    # Frozen: same sequence, or a moved sequence whose age keeps growing, or nothing to compare with.
    for check in ([_late(100, 71.0)], [_late(105, 71.0)]):
        assert apply_clock_offset(before, check) == [] and not check[0].up
    assert apply_clock_offset(None, [_late(105, 43.0)]) == []
    # Other failures are left alone.
    missing = [UrlCheck(url="https://a.example/c.m3u8", up=False, category="PLAYLIST_MISSING", detail="HTTP 404",
                        sequences={"v1.m3u8": 105})]
    assert apply_clock_offset(before, missing) == []


def test_a_stream_slower_than_real_time_stays_down_and_says_so():
    from spider import apply_clock_offset
    before = merge_url_health(None, [_late(100, 69.0)], "2026-10-01T07:00:00+00:00")
    slipping = [_late(104, 95.0)]  # 4 new pieces, but 26 s further behind live
    assert apply_clock_offset(before, slipping) == [] and not slipping[0].up
    assert slipping[0].detail == "FALLING_BEHIND (advancing, but 95s behind live, 26s more than at the last check)"


# --- Deduplication and adaptive intermediate polling tests ---

from spider import HealthRecorder, clear_spider_caches, _GLOBAL_NODE_CACHE
from health import NodeHealth


class TestDeduplicationAndAdaptive:
    @pytest.fixture(autouse=True)
    def clean_cache(self):
        clear_spider_caches()
        yield
        clear_spider_caches()

    def test_shared_node_cache_reuses_healthy_check(self):
        topo = topology({"trans": ["Transcoding"]}, [])
        calls = []

        async def fake_checker(node):
            calls.append(node["id"])
            return NodeHealth(node_id=node["id"], check_type="HLS", up=True, latency_ms=12)

        import asyncio
        loop = asyncio.new_event_loop()
        try:
            class DummyMetrics:
                def add_incident(self, *a, **k): pass
                def record(self, *a, **k): pass

            class DummyDriver:
                def execute_query(self, *a, **k):
                    return FakeRecords([{"id": "trans", "urlHealth": None, "links": None, "incidentOpen": False, "failuresBefore": 0, "errorBefore": None}])

            async def probe(rec, node, **kw):  # _probe needs the running loop
                return await rec._probe(node, **kw)

            rec1 = HealthRecorder(DummyDriver(), topo, fake_checker, metrics=DummyMetrics())
            # Cycle 1: check trans
            h1 = loop.run_until_complete(probe(rec1, "trans"))
            assert len(calls) == 1
            assert h1.up is True

            # Manually update global cache as _check_and_record would
            import time
            _GLOBAL_NODE_CACHE["trans"] = (time.monotonic(), h1)

            # Cycle 2: another recorder arrives within TTL
            rec2 = HealthRecorder(DummyDriver(), topo, fake_checker, metrics=DummyMetrics())
            h2 = loop.run_until_complete(probe(rec2, "trans", max_age_s=60.0))
            assert len(calls) == 1  # 0 extra network calls!
            assert h2.latency_ms == 12
        finally:
            loop.close()



def test_a_url_no_longer_checked_leaves_url_health():
    """The sakshitv ghost: its link moved to another server, but its old DOWN entry stayed on cloud for days."""
    import json
    from health import UrlCheck
    from spider import merge_url_health
    now = "2026-10-08T10:00:00+00:00"
    before = merge_url_health(None, [UrlCheck(url="old", up=False, detail="HTTP 404"),
                                     UrlCheck(url="keep", up=True, detail="ok")], now)
    after = json.loads(merge_url_health(before, [UrlCheck(url="keep", up=True, detail="ok")], now))
    assert set(after) == {"keep"}
