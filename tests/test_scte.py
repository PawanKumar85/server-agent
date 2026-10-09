"""SCTE-35 ad-break markers: detection in every tag style, breaks (closed, overrun, stuck), the 7-day pattern and
its warnings, and ad-edge discontinuities not counted as glitches."""

import time

import httpx
import m3u8

import glitch
import scte

T0 = 1790000000.0  # a fixed moment, so minute/hour patterns are predictable


def iso(t):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def playlist(first_seq, segments, start=T0):
    """segments: list of extra tag lines (or "") placed before each 6 s segment."""
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:6", f"#EXT-X-MEDIA-SEQUENCE:{first_seq}"]
    for i, tags in enumerate(segments):
        lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{iso(start + 6 * i)}")
        lines += [t for t in tags.split("\n") if t]
        lines += ["#EXTINF:6,", f"s{first_seq + i}.ts"]
    return "\n".join(lines) + "\n"


def test_every_marker_style_is_found_with_its_planned_length():
    pl = m3u8.loads(playlist(10, [
        "", "#EXT-X-CUE-OUT:30", "#EXT-X-CUE-OUT-CONT:ElapsedTime=6,Duration=30", "#EXT-X-CUE-IN",
        f'#EXT-X-DATERANGE:ID="ad2",START-DATE="{iso(T0 + 24)}",PLANNED-DURATION=60,SCTE35-OUT=0xFC30',
        f'#EXT-X-DATERANGE:ID="ad2",START-DATE="{iso(T0 + 84)}",SCTE35-IN=0xFC30']))
    marks = [(m["kind"], m["style"], m["seq"], m["planned_s"]) for m in scte.extract_markers(pl, T0 + 60)]
    assert marks == [("OUT", "CUE", 11, 30.0), ("IN", "CUE", 13, None),
                     ("OUT", "DATERANGE", 14, 60.0), ("IN", "DATERANGE", 15, None)]
    # Joining in the middle of a break: its start is put back by the elapsed time.
    mid = m3u8.loads(playlist(20, ["#EXT-X-CUE-OUT-CONT:ElapsedTime=12,Duration=30", "", "#EXT-X-CUE-IN"]))
    (out, _in) = scte.extract_markers(mid, T0 + 60)
    assert out["style"] == "CUE-CONT" and out["at"] == T0 - 12 and _in["kind"] == "IN"


def test_breaks_pair_up_overrun_get_stuck_and_repeat_sightings_count_once(tmp_path):
    s = scte.ScteStore(tmp_path / "m.db")
    url = "https://final.example.test/ch/index.m3u8"
    T0 = time.time() - 3600  # recent, so it's inside the 7-day window
    out = {"kind": "OUT", "seq": 1, "at": T0, "planned_s": 30, "break_id": None, "style": "CUE"}
    cue_in = {"kind": "IN", "seq": 6, "at": T0 + 30, "planned_s": None, "break_id": None, "style": "CUE"}
    s.record(url, "final", "ch", "FinalLink", [out], T0 + 5)
    s.record(url, "final", "ch", "FinalLink", [out], T0 + 65)  # the same marker on the next probe
    s.record(url, "final", "ch", "FinalLink", [out, cue_in], T0 + 125)
    (b,) = s.breaks(url=url, since_s=10 ** 10)
    assert b["status"] == "CLOSED" and b["actual_s"] == 30 and b["planned_s"] == 30

    late = {"kind": "OUT", "seq": 50, "at": T0 + 600, "planned_s": 30, "break_id": None, "style": "CUE"}
    s.record(url, "final", "ch", "FinalLink", [late, {**cue_in, "seq": 60, "at": T0 + 690}], T0 + 700)
    assert s.breaks(url=url, since_s=10 ** 10)[0]["status"] == "OVERRUN"  # 90 s against a planned 30

    stuck = {"kind": "OUT", "seq": 100, "at": T0 + 1200, "planned_s": 60, "break_id": None, "style": "CUE"}
    s.record(url, "final", "ch", "FinalLink", [stuck], T0 + 1210)
    s.record(url, "final", "ch", "FinalLink", [], T0 + 1200 + 60 + scte.STUCK_GRACE_S + 5)
    assert s.breaks(url=url, since_s=10 ** 10)[0]["status"] == "STUCK"
    p = s.pattern(url, T0 + 1400)
    assert p["open"]["status"] == "STUCK" and any("stuck in the ad" in i for i in p["issues"])


def _history(s, url, days=3, every_min=30, at_minute=15, length=60, until=None):
    """Breaks at :15 and :45 every hour (IST minute marks), 60 s long, for `days` days before `until`."""
    until = until or time.time()
    start = until - days * 86400
    t = start - (start % 3600) + at_minute * 60 - scte.IST % 3600
    seq = 0
    while t < until - 600:
        if t >= start:
            seq += 10
            s.record(url, "final", "ch", "FinalLink",
                     [{"kind": "OUT", "seq": seq, "at": t, "planned_s": length, "break_id": None, "style": "CUE"},
                      {"kind": "IN", "seq": seq + 5, "at": t + length, "planned_s": None, "break_id": None, "style": "CUE"}],
                     t + length + 10)
        t += every_min * 60
    return seq


def test_the_weekly_pattern_is_learned_and_the_next_break_predicted(tmp_path):
    s = scte.ScteStore(tmp_path / "m.db")
    url = "https://final.example.test/ch/index.m3u8"
    now = time.time()
    _history(s, url, until=now)
    p = s.pattern(url, now)
    text = " ".join(p["lines"])
    assert "Breaks usually start at :15, :45 past the hour" in text, text
    assert "Typical break: 60 s (most often planned as 60 s)" in text and "Typical gap between breaks: 30 min" in text
    nxt = p["nextExpected"]
    assert nxt and now < nxt <= now + 31 * 60 and int(((nxt + scte.IST) % 3600) // 60) in (15, 45)
    assert p["issues"] == []

    # 2 hours later and nothing since: markers are probably being lost.
    later = s.pattern(url, now + 2 * 3600)
    assert any("No ad breaks for" in i and "usually every 30 min" in i for i in later["issues"])


def test_markers_on_the_main_input_but_not_the_final_are_flagged(tmp_path):
    s = scte.ScteStore(tmp_path / "m.db")
    main = "https://main.example.test/ch/index.m3u8"
    _history(s, main, days=1)
    (c,) = scte.summary(s, [{"node": "final", "url": "https://final.example.test/ch/index.m3u8", "channel": "ch"}],
                        {"ch": [{"node": "main", "url": main}]})
    assert c["mainBreaks24h"] >= 3 and c["breaks24h"] == 0
    assert any("dropped between them" in i for i in c["issues"])


def test_a_discontinuity_at_an_ad_edge_is_not_a_glitch(tmp_path):
    first = playlist(100, ["", "", ""])
    later = playlist(101, ["", "", "#EXT-X-CUE-OUT:30\n#EXT-X-DISCONTINUITY", "#EXT-X-CUE-IN\n#EXT-X-DISCONTINUITY"])
    pages = {"n": 0}

    def handler(request):
        if request.url.path.endswith(".ts"):
            return httpx.Response(200, content=b"x" * 1000)
        pages["n"] += 1
        return httpx.Response(200, text=first if pages["n"] <= 1 else later)  # one playlist read per probe
    store = scte.ScteStore(tmp_path / "m.db")
    p = glitch.GlitchProbe(tmp_path / "m.db", client=httpx.Client(transport=httpx.MockTransport(handler)), scte_store=store)
    url = "https://final.example.test/ch/index.m3u8"
    p.probe(url, "final", "ch")
    result = p.probe(url, "final", "ch")
    assert "DISCONTINUITY" not in {g["kind"] for g in result["glitches"]}
    assert [b["status"] for b in store.breaks(url=url, since_s=10 ** 10)] == ["CLOSED"]



def test_a_carried_over_oatcls_tag_is_not_a_new_break(tmp_path):
    # The real pattern on a live channel: CUE-OUT with OATCLS, CONT, then CUE-IN. The m3u8 library keeps the OATCLS
    # value on the segments after it; that must not open phantom breaks that leave the real one "stuck".
    pl = m3u8.loads(playlist(2938738, [
        "#EXT-X-CUE-OUT:59\n#EXT-OATCLS-SCTE35:/DAlAAAA", "#EXT-X-CUE-OUT-CONT:ElapsedTime=6,Duration=59",
        "#EXT-X-CUE-OUT-CONT:ElapsedTime=12,Duration=59", "", "", "", "", "", "", "", "#EXT-X-CUE-IN", "", ""]))
    marks = [(m["kind"], m["seq"]) for m in scte.extract_markers(pl)]
    assert marks == [("OUT", 2938738), ("IN", 2938748)]
    s = scte.ScteStore(tmp_path / "m.db")
    url = "https://lg.example.test/ch/index.m3u8"
    s.record(url, "final", "ch", "FinalLink", scte.extract_markers(pl, time.time()), time.time())
    (b,) = s.breaks(url=url, since_s=10 ** 10)
    assert b["status"] == "CLOSED" and b["actual_s"] == 60 and b["planned_s"] == 59 and b["style"] == "OATCLS"



def test_a_missed_cue_in_closes_the_break_with_an_estimated_end_not_stuck(tmp_path):
    s = scte.ScteStore(tmp_path / "m.db")
    url = "https://short.example.test/ch/index.m3u8"
    now = time.time()
    start = now - 300
    in_break = m3u8.loads(playlist(500, ["#EXT-X-CUE-OUT:100", "#EXT-X-CUE-OUT-CONT:ElapsedTime=6,Duration=100"], start))
    s.record(url, "final", "ch", "FinalLink", scte.extract_markers(in_break, start + 12), start + 12,
             segments=scte.segment_states(in_break, start + 12))
    # The CUE-IN scrolled out of a 24 s window before the next read; now only normal segments are visible.
    after = m3u8.loads(playlist(530, ["", "", "", ""], start + 180))
    s.record(url, "final", "ch", "FinalLink", scte.extract_markers(after, now), now, segments=scte.segment_states(after, now))
    (b,) = s.breaks(url=url, since_s=10 ** 10)
    assert b["status"] == "CLOSED" and b["style"].endswith("(end estimated)")
    assert b["actual_s"] == 100  # the planned end lies between the last in-break sighting and the first normal segment



def test_duplicate_tags_are_one_break_and_back_to_back_breaks_close_each_other(tmp_path):
    s = scte.ScteStore(tmp_path / "m.db")
    url = "https://pod.example.test/ch/index.m3u8"
    t = time.time() - 600
    mk = lambda kind, seq, at, style="CUE", planned=100: {"kind": kind, "seq": seq, "at": at, "planned_s": planned,
                                                         "break_id": None, "style": style}
    # The same start seen as CUE-OUT and, 4 s off, as a CUE-OUT-CONT estimate; then a second slot 96 s later.
    s.record(url, "final", "ch", "FinalLink", [mk("OUT", 1, t), mk("OUT", 2, t + 4, "CUE-CONT")], t + 10)
    s.record(url, "final", "ch", "FinalLink", [mk("OUT", 17, t + 96), mk("IN", 34, t + 196, planned=None)], t + 200)
    first, second = sorted(s.breaks(url=url, since_s=10 ** 10), key=lambda b: b["start"])
    assert len(s.breaks(url=url, since_s=10 ** 10)) == 2
    assert (first["status"], first["actual_s"]) == ("CLOSED", 96) and (second["status"], second["actual_s"]) == ("CLOSED", 100)


def test_ad_break_tracking_is_off_by_default():
    """SCTE scanning was about a third of the app's CPU: off unless SCTE=1. Nothing scans or summarises."""
    import server
    assert server.SCTE_ENABLED is False
    assert server.glitch_probe.scte is None  # the probe neither records Final markers nor scans the Mains
    assert server.ad_breaks() == []
