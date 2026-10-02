"""Link models, grouping by domain and topology edge rules (no network, no database)."""

import pytest
from pydantic import ValidationError

from nodes import Relationship, StreamLink, edge_problem, group_by_domain, load_json_list

GOOD = "https://jio.ottlive.co.in/gtcnews/gtcnews/index.m3u8"


class TestStreamLink:
    def test_valid_link_derives_domain(self):
        link = StreamLink(label="MainInput", url=GOOD, channel="gtcnews")
        assert link.domain == "jio.ottlive.co.in"

    def test_channel_is_trimmed(self):
        assert StreamLink(label="MainInput", url=GOOD, channel="  gtcnews ").channel == "gtcnews"

    @pytest.mark.parametrize("url", ["rtmp://a.example/live", "ftp://a.example/x", "a.example/x.m3u8", "srt://a:9000"])
    def test_only_http_and_https_urls(self, url):
        with pytest.raises(ValidationError, match="url"):
            StreamLink(label="MainInput", url=url, channel="c")

    @pytest.mark.parametrize("label", ["Bad Label", "9lives", "x`) DETACH DELETE n //", ""])
    def test_label_must_be_a_plain_identifier(self, label):
        # labels are interpolated into Cypher, so anything else must be rejected
        with pytest.raises(ValidationError, match="label"):
            StreamLink(label=label, url=GOOD, channel="c")

    @pytest.mark.parametrize("channel", ["", "   "])
    def test_channel_is_required(self, channel):
        with pytest.raises(ValidationError, match="channel"):
            StreamLink(label="MainInput", url=GOOD, channel=channel)


class TestGroupByDomain:
    def test_links_on_one_domain_become_one_node(self):
        nodes = group_by_domain([
            StreamLink(label="BackupLink", url="https://ingest.ottlive.co.in/gtcnews/gtcnews/index.m3u8", channel="gtcnews"),
            StreamLink(label="Transcoding", url="https://ingest.ottlive.co.in/gtcnewsoutput/index.m3u8", channel="gtcnews"),
            StreamLink(label="MainInput", url=GOOD, channel="gtcnews"),
        ])
        by_domain = {n.domain: n for n in nodes}
        assert set(by_domain) == {"ingest.ottlive.co.in", "jio.ottlive.co.in"}
        ingest = by_domain["ingest.ottlive.co.in"]
        assert ingest.labels == {"BackupLink", "Transcoding"}
        assert len(ingest.url) == 2

    def test_each_url_keeps_its_own_role_and_channel(self):
        [node] = group_by_domain([
            StreamLink(label="BackupLink", url="https://ingest.ottlive.co.in/a/index.m3u8", channel="a"),
            StreamLink(label="Transcoding", url="https://ingest.ottlive.co.in/b/index.m3u8", channel="b"),
        ])
        roles = {l["url"]: (l["role"], l["channel"]) for l in node.links}
        assert roles == {
            "https://ingest.ottlive.co.in/a/index.m3u8": ("BackupLink", "a"),
            "https://ingest.ottlive.co.in/b/index.m3u8": ("Transcoding", "b"),
        }

    def test_each_final_channel_gets_its_own_node(self):
        # 7 channels = 7 FinalLink nodes = 7 spiders, even when channels share a host
        nodes = group_by_domain([
            StreamLink(label="FinalLink", url="https://gtc.ottlive.co.in/gtcnews/index.m3u8", channel="gtcnews"),
            StreamLink(label="FinalLink", url="https://gtc.ottlive.co.in/gtcpunjabi/index.m3u8", channel="gtcpunjabi"),
            StreamLink(label="MainInput", url="https://jio.ottlive.co.in/gtcnews/index.m3u8", channel="gtcnews"),
        ])
        ids = sorted(n.domain for n in nodes)
        assert ids == ["gtc.ottlive.co.in/gtcnews", "gtc.ottlive.co.in/gtcpunjabi", "jio.ottlive.co.in"]
        final = next(n for n in nodes if n.domain == "gtc.ottlive.co.in/gtcnews")
        assert final.labels == {"FinalLink"} and final.server_ip == "203.0.113.10"  # DNS uses the host part

    def test_duplicate_urls_are_stored_once(self):
        [node] = group_by_domain([StreamLink(label="MainInput", url=GOOD, channel="c")] * 3)
        assert node.props()["url"] == [GOOD]

    def test_server_ip_uses_dns(self):
        [node] = group_by_domain([StreamLink(label="MainInput", url=GOOD, channel="c")])
        assert node.server_ip == "203.0.113.10"  # from the no_real_dns fixture


class TestEdgeRules:
    LABELS = {
        "main": ["Domain", "MainInput"], "backup": ["Domain", "BackupLink"], "trans": ["Domain", "Transcoding"],
        "final": ["Domain", "FinalLink"], "main_trans": ["Domain", "MainInput", "Transcoding"],
    }

    @pytest.mark.parametrize("src,rel,dst", [
        ("main", "FEEDS", "trans"),
        ("backup", "FEEDS", "trans"),
        ("main", "FEEDS", "final"),      # pass-through channel without a transcoder
        ("trans", "PRODUCES", "final"),
        ("main_trans", "PRODUCES", "final"),
    ])
    def test_allowed_shapes(self, src, rel, dst):
        assert edge_problem(Relationship(source=src, type=rel, target=dst), self.LABELS) is None

    @pytest.mark.parametrize("src,rel,dst,reason", [
        ("final", "FEEDS", "trans", "must start at"),
        ("backup", "FEEDS", "main", "must end at"),   # valid source, invalid target
        ("main", "PRODUCES", "final", "must start at"),
        ("trans", "PRODUCES", "trans", "same node"),
        ("main", "FEEDS", "ghost", "no node for"),
    ])
    def test_rejected_shapes(self, src, rel, dst, reason):
        assert reason in edge_problem(Relationship(source=src, type=rel, target=dst), self.LABELS)

    def test_unknown_relationship_type_rejected(self):
        with pytest.raises(ValidationError):
            Relationship(source="a", type="CONNECTS", target="b")


def test_layout_puts_every_final_in_the_last_column():
    from graph_view import layout
    nodes = [{"id": i, "labels": l} for i, l in [
        ("main", ["MainInput"]), ("trans", ["Transcoding"]), ("final_t", ["FinalLink"]), ("final_direct", ["FinalLink"]),
    ]]
    edges = [{"source": "main", "type": "FEEDS", "target": "trans"}, {"source": "trans", "type": "PRODUCES", "target": "final_t"},
             {"source": "main", "type": "FEEDS", "target": "final_direct"}]  # pass-through channel
    pos = layout(nodes, edges)
    assert pos["final_direct"]["x"] == pos["final_t"]["x"] > pos["trans"]["x"] > pos["main"]["x"]


def test_layout_one_column_per_role():
    from graph_view import layout
    nodes = [{"id": i, "labels": l} for i, l in [
        ("jio", ["MainInput"]), ("cloud", ["MainInput", "BackupLink"]),
        ("ingest1", ["BackupLink"]), ("ingest", ["BackupLink", "Transcoding"]), ("xcode2", ["Transcoding"]),
        ("f1", ["FinalLink"]), ("f2", ["FinalLink"]),
    ]]
    edges = [{"source": "jio", "type": "FEEDS", "target": "ingest"}, {"source": "ingest1", "type": "FEEDS", "target": "xcode2"},
             {"source": "ingest", "type": "PRODUCES", "target": "f1"}, {"source": "cloud", "type": "FEEDS", "target": "f2"}]
    x = {i: p["x"] for i, p in layout(nodes, edges).items()}
    assert x["jio"] < x["ingest1"] < x["ingest"] < x["f1"]            # Main | Backup | Transcoding | Final
    assert x["ingest"] == x["xcode2"] and x["f1"] == x["f2"]          # similar nodes share a column
    assert x["cloud"] == x["jio"]                                      # Main for some channels -> Main column


@pytest.mark.parametrize("raw,expected", [(None, []), ("", []), ("not json", []), ('{"a": 1}', []), ('[{"a": 1}]', [{"a": 1}])])
def test_load_json_list_tolerates_bad_values(raw, expected):
    assert load_json_list(raw) == expected


def test_pipeline_edges_follow_main_backup_transcoding_final():
    from nodes import Relationship as R, StreamLink, pipeline_edges
    link = lambda ch, role, url: StreamLink(channel=ch, label=role, url=url)
    with_transcoder = [link("Rang Manch", "MainInput", "https://ingest1.ottlive.co.in/rangmanch/index.m3u8"),
                       link("Rang Manch", "Transcoding", "https://xcode4.ottlive.co.in/rangmanchtv/index.m3u8"),
                       link("Rang Manch", "FinalLink", "https://cdn.ottlive.co.in/rangmanchtv/index.m3u8")]
    final = with_transcoder[2].node_id
    assert final == "cdn.ottlive.co.in/Rang Manch"
    assert pipeline_edges(with_transcoder) == [R(source="xcode4.ottlive.co.in", type="PRODUCES", target=final),
                                               R(source="ingest1.ottlive.co.in", type="FEEDS", target="xcode4.ottlive.co.in")]
    direct = [link("ch", "MainInput", "https://a.example.com/x.m3u8"), link("ch", "BackupLink", "https://b.example.com/x.m3u8"),
              link("ch", "FinalLink", "https://f.example.com/x.m3u8")]
    assert {(e.source, e.type) for e in pipeline_edges(direct)} == {("a.example.com", "FEEDS"), ("b.example.com", "FEEDS")}


def test_only_channels_without_edges_get_connected(monkeypatch):
    import json
    from types import SimpleNamespace as NS
    import nodes
    from nodes import Relationship as R

    stored = [
        [{"url": "https://ingest1.ottlive.co.in/rangmanch/index.m3u8", "role": "MainInput", "channel": "Rang Manch"},
         {"url": "https://ingest1.ottlive.co.in/b24/index.m3u8", "role": "BackupLink", "channel": "b24"}],
        [{"url": "https://xcode4.ottlive.co.in/rm/index.m3u8", "role": "Transcoding", "channel": "Rang Manch"}],
        [{"url": "https://cdn.ottlive.co.in/rm/index.m3u8", "role": "FinalLink", "channel": "Rang Manch"}],
        [{"url": "https://xcode2.ottlive.co.in/b24/index.m3u8", "role": "Transcoding", "channel": "b24"}],
        [{"url": "https://stream.ottlive.co.in/b24/index.m3u8", "role": "FinalLink", "channel": "b24"}],
    ]
    driver = NS(execute_query=lambda q, **p: NS(records=[{"links": json.dumps(l)} for l in stored]))
    # b24 is wired by hand (its backup also goes straight to the Final); Rang Manch has nothing yet.
    existing = [R(source="ingest1.ottlive.co.in", type="FEEDS", target="stream.ottlive.co.in/b24"),
                R(source="xcode2.ottlive.co.in", type="PRODUCES", target="stream.ottlive.co.in/b24")]
    written = []
    monkeypatch.setattr(nodes, "current_topology", lambda d: list(existing))
    monkeypatch.setattr(nodes, "replace_topology", lambda d, rels: written.append(rels) or [])

    added = nodes.connect_new_channels(driver, {"Rang Manch", "b24"})
    assert added == [R(source="xcode4.ottlive.co.in", type="PRODUCES", target="cdn.ottlive.co.in/Rang Manch"),
                     R(source="ingest1.ottlive.co.in", type="FEEDS", target="xcode4.ottlive.co.in")]
    assert written == [existing + added]  # b24's hand-made edges are kept; nothing added for b24
    assert nodes.connect_new_channels(driver, set()) == []


def test_links_added_one_at_a_time_are_wired_as_they_arrive(monkeypatch):
    import json
    from types import SimpleNamespace as NS
    import nodes
    from nodes import Relationship as R

    stored = [[{"url": "https://ingest1.ottlive.co.in/rm/index.m3u8", "role": "MainInput", "channel": "Rang Manch"}],
              [{"url": "https://xcode4.ottlive.co.in/rm/index.m3u8", "role": "Transcoding", "channel": "Rang Manch"}],
              [{"url": "https://cdn.ottlive.co.in/rm/index.m3u8", "role": "FinalLink", "channel": "Rang Manch"}]]
    driver = NS(execute_query=lambda q, **p: NS(records=[{"links": json.dumps(l)} for l in stored]))
    # Main and Transcoding were added (and wired) first; the Final arrives now, still unlinked.
    monkeypatch.setattr(nodes, "current_topology", lambda d: [R(source="ingest1.ottlive.co.in", type="FEEDS", target="xcode4.ottlive.co.in")])
    monkeypatch.setattr(nodes, "replace_topology", lambda d, rels: [])
    assert nodes.connect_new_channels(driver, {"Rang Manch"}) == [
        R(source="xcode4.ottlive.co.in", type="PRODUCES", target="cdn.ottlive.co.in/Rang Manch")]
