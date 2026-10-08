"""HLS / HTTP health checks against fake HTTP servers (httpx.MockTransport): no real network."""

import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

import health
from health import UnsupportedCheck, check_hls_url, check_http_url, check_node

START = datetime(2026, 9, 30, 6, 0, 0, tzinfo=timezone.utc)
BASE = "https://cdn.example.test"


def media_playlist(segments=2, pdt=True, target=6):
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{target}", "#EXT-X-MEDIA-SEQUENCE:100"]
    if pdt:
        lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{START.strftime('%Y-%m-%dT%H:%M:%S.000Z')}")
    for i in range(segments):
        lines += [f"#EXTINF:{target}.0,", f"seg{100 + i}.ts"]
    return "\n".join(lines) + "\n"


MASTER = (
    "#EXTM3U\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\nv360/index.m3u8\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=2800000,RESOLUTION=1280x720\nv720/index.m3u8\n"
)


def server_date(seconds_after_start):
    """Date header as the stream server would send it (freshness uses the server's clock)."""
    return format_datetime(START + timedelta(seconds=seconds_after_start), usegmt=True)


class FakeServer:
    """url -> response spec: (status, body[, date_offset_s]) or a list of specs served in order."""

    def __init__(self, routes, fresh_after_s=14):
        self.routes, self.fresh_after_s, self.hits = routes, fresh_after_s, {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.hits[url] = self.hits.get(url, 0) + 1
        spec = self.routes.get(url, (404, "not found"))
        if isinstance(spec, list):
            spec = spec[min(self.hits[url], len(spec)) - 1]
        if isinstance(spec, Exception):
            raise spec
        status, body, *rest = spec
        date = server_date(rest[0] if rest else self.fresh_after_s)
        return httpx.Response(status, text=body, headers={"Date": date, "Content-Type": "application/vnd.apple.mpegurl"})


def run(check, server, *args):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
            return await check(client, asyncio.Semaphore(8), *args)
    return asyncio.run(go())


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(health, "RETRIES", 2)  # 3 attempts, as in production


class TestHlsCheck:
    def test_live_master_all_variants(self):
        server = FakeServer({
            f"{BASE}/ch/index.m3u8": (200, MASTER),
            f"{BASE}/ch/v360/index.m3u8": (200, media_playlist()),
            f"{BASE}/ch/v720/index.m3u8": (200, media_playlist()),
        })
        r = run(check_hls_url, server, f"{BASE}/ch/index.m3u8")
        assert r.up and r.status == 200
        assert r.detail == "master, 2/2 variants live"

    def test_master_up_if_any_variant_live(self):
        server = FakeServer({
            f"{BASE}/ch/index.m3u8": (200, MASTER),
            f"{BASE}/ch/v360/index.m3u8": (200, media_playlist()),
            # v720 missing -> 404
        })
        r = run(check_hls_url, server, f"{BASE}/ch/index.m3u8")
        assert r.up and r.detail == "master, 1/2 variants live"

    def test_master_down_when_every_variant_is_stale(self):
        server = FakeServer({
            f"{BASE}/ch/index.m3u8": (200, MASTER),
            f"{BASE}/ch/v360/index.m3u8": (200, media_playlist(), 60),  # newest segment ended 48 s ago
            f"{BASE}/ch/v720/index.m3u8": (200, media_playlist(), 60),
        })
        r = run(check_hls_url, server, f"{BASE}/ch/index.m3u8")
        assert not r.up
        assert "0/2 variants live" in r.detail and "STALE_SEGMENTS" in r.detail

    def test_media_playlist_fresh(self):
        server = FakeServer({f"{BASE}/m.m3u8": (200, media_playlist(segments=3))})
        r = run(check_hls_url, server, f"{BASE}/m.m3u8")
        assert r.up and r.detail == "media, 3 fresh segments"

    def test_media_playlist_frozen(self):
        server = FakeServer({f"{BASE}/m.m3u8": (200, media_playlist(), 60)})
        r = run(check_hls_url, server, f"{BASE}/m.m3u8")
        assert not r.up and r.detail.startswith("STALE_SEGMENTS")

    def test_freshness_boundary_is_three_target_durations(self):
        # last segment ends at +12 s; limit is 3 x 6 s = 18 s
        just_fresh = FakeServer({f"{BASE}/m.m3u8": (200, media_playlist(), 12 + 17)})
        just_stale = FakeServer({f"{BASE}/m.m3u8": (200, media_playlist(), 12 + 19)})
        assert run(check_hls_url, just_fresh, f"{BASE}/m.m3u8").up
        health.clear_health_caches()  # a later check, not the same one within the dedupe window
        assert not run(check_hls_url, just_stale, f"{BASE}/m.m3u8").up

    def test_no_program_date_time_skips_freshness(self):
        server = FakeServer({f"{BASE}/m.m3u8": (200, media_playlist(pdt=False), 3600)})
        assert run(check_hls_url, server, f"{BASE}/m.m3u8").up

    def test_empty_media_playlist(self):
        server = FakeServer({f"{BASE}/m.m3u8": (200, media_playlist(segments=0))})
        r = run(check_hls_url, server, f"{BASE}/m.m3u8")
        assert not r.up and r.detail == "NO_SEGMENTS"

    def test_http_404(self):
        r = run(check_hls_url, FakeServer({}), f"{BASE}/missing.m3u8")
        assert not r.up and r.status == 404 and r.detail == "HTTP 404"

    def test_not_a_playlist(self):
        server = FakeServer({f"{BASE}/x.m3u8": (200, "<html>maintenance</html>")})
        r = run(check_hls_url, server, f"{BASE}/x.m3u8")
        assert not r.up and r.detail in ("INVALID_PLAYLIST", "NO_SEGMENTS")

    def test_5xx_is_retried(self):
        url = f"{BASE}/m.m3u8"
        server = FakeServer({url: [(503, "busy"), (200, media_playlist())]})
        r = run(check_hls_url, server, url)
        assert r.up
        assert server.hits[url] >= 2

    def test_persistent_5xx_gives_up_after_three_tries(self):
        url = f"{BASE}/m.m3u8"
        server = FakeServer({url: (500, "boom")})
        r = run(check_hls_url, server, url)
        assert not r.up and r.detail == "HTTP 500"
        assert server.hits[url] == 3

    def test_connection_errors_are_retried_then_reported(self):
        url = f"{BASE}/m.m3u8"
        server = FakeServer({url: httpx.ConnectError("refused")})
        r = run(check_hls_url, server, url)
        assert not r.up and r.detail == "HTTP_CONNECTERROR after 3 tries"
        assert server.hits[url] == 3

    def test_timeout_then_success(self):
        url = f"{BASE}/m.m3u8"
        server = FakeServer({url: [httpx.ReadTimeout("slow"), (200, media_playlist())]})
        assert run(check_hls_url, server, url).up


class TestHttpCheck:
    def test_https_only_rejects_plain_http(self):
        r = run(check_http_url, FakeServer({}), "http://cdn.example.test/x", True)
        assert not r.up and r.detail == "NOT_HTTPS"

    @pytest.mark.parametrize("status,up", [(200, True), (302, True), (404, False), (500, False)])
    def test_status_codes(self, status, up):
        url = f"{BASE}/health"
        server = FakeServer({url: (status, "")})
        assert run(check_http_url, server, url, False).up is up


class TestCheckNode:
    @pytest.fixture(autouse=True)
    def no_icmp(self, monkeypatch):
        async def fake_icmp(host):
            return {"alive": True, "rtt_ms": 12.5, "jitter_ms": 1.5, "packet_loss": 0.0}
        monkeypatch.setattr(health, "icmp_diagnostics", fake_icmp)

    def node_check(self, server, **node):
        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
                return await check_node({"id": "n1.example.test", **node}, client, asyncio.Semaphore(8))
        return asyncio.run(go())

    def test_node_up_only_if_every_url_up(self):
        server = FakeServer({f"{BASE}/a.m3u8": (200, media_playlist())})  # b.m3u8 -> 404
        h = self.node_check(server, url=[f"{BASE}/a.m3u8", f"{BASE}/b.m3u8"])
        assert not h.up
        assert h.error.startswith("1/2 URLs failing") and "b.m3u8 HTTP 404" in h.error

    def test_healthy_node_with_icmp_diagnostics(self):
        server = FakeServer({f"{BASE}/a.m3u8": (200, media_playlist())})
        h = self.node_check(server, url=[f"{BASE}/a.m3u8"])
        assert h.up and h.error is None
        assert (h.rtt_ms, h.jitter_ms, h.packet_loss) == (12.5, 1.5, 0.0)

    def test_node_without_urls(self):
        h = self.node_check(FakeServer({}), url=[])
        assert not h.up and h.error == "NO_URLS"

    def test_icmp_check_type(self):
        h = self.node_check(FakeServer({}), url=[], checkType="ICMP")
        assert h.up and h.latency_ms == 12.5

    @pytest.mark.parametrize("check_type", ["PROCESS", "SNMP"])
    def test_unsupported_check_types(self, check_type):
        with pytest.raises(UnsupportedCheck):
            self.node_check(FakeServer({}), url=[], checkType=check_type)


class TestIgnoredStream:
    """A stream ticked "ignore this stream" is still checked, but never makes its server DOWN."""

    @pytest.fixture(autouse=True)
    def no_icmp(self, monkeypatch):
        async def fake_icmp(host):
            return {"alive": True}
        monkeypatch.setattr(health, "icmp_diagnostics", fake_icmp)

    def node_check(self, server, ignored, **node):
        health.ignored_urls = lambda: set(ignored)
        try:
            async def go():
                async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
                    return await check_node({"id": "n1.example.test", **node}, client, asyncio.Semaphore(8))
            return asyncio.run(go())
        finally:
            health.ignored_urls = lambda: set()

    def test_an_ignored_failing_stream_leaves_its_server_up(self):
        server = FakeServer({f"{BASE}/a.m3u8": (200, media_playlist())})  # b.m3u8 -> 404 (a dead backup)
        h = self.node_check(server, [f"{BASE}/b.m3u8"], url=[f"{BASE}/a.m3u8", f"{BASE}/b.m3u8"])
        assert h.up and h.error is None
        dead = next(c for c in h.urls if c.url.endswith("b.m3u8"))
        assert dead.ignored and not dead.up and "404" in dead.detail  # still checked, and it says so

    def test_other_streams_still_count(self):
        server = FakeServer({})  # both 404
        h = self.node_check(server, [f"{BASE}/b.m3u8"], url=[f"{BASE}/a.m3u8", f"{BASE}/b.m3u8"])
        assert not h.up and h.error.startswith("1/2 URLs failing") and "a.m3u8" in h.error and "b.m3u8" not in h.error


def test_the_ignore_store(tmp_path):
    from channel_mute import StreamIgnores
    s = StreamIgnores(tmp_path / "m.db")
    url = "https://cloud.example/lokmatbackup/index.m3u8"
    assert s.urls() == set()
    s.set(url, True, "backup retired")
    assert s.urls() == {url} and s.all()[url]["note"] == "backup retired"  # the change shows at once, no cache wait
    s.set(url, False)
    assert s.urls() == set()


def test_ignored_is_kept_in_url_health_and_skipped_in_root_cause():
    import json
    from health import UrlCheck
    from spider import merge_url_health
    state = json.loads(merge_url_health(None, [UrlCheck(url="u1", up=False, detail="HTTP 404", ignored=True),
                                               UrlCheck(url="u2", up=True, detail="ok")], "2026-10-07T10:00:00+00:00"))
    assert state["u1"]["ignored"] is True and "ignored" not in state["u2"]


def test_checks_passed_leaves_out_ignored_streams(tmp_path):
    """Ticking a dead backup takes back the failures it caused, not just future ones."""
    from health import NodeHealth, UrlCheck
    from metrics import Metrics
    m = Metrics(tmp_path / "m.db")
    main, backup = "https://cloud.example/main.m3u8", "https://cloud.example/backup.m3u8"
    for i in range(10):  # the backup fails every check; the main fails twice
        main_up = i not in (3, 7)
        urls = [UrlCheck(url=main, up=main_up, detail="ok" if main_up else "HTTP 404"),
                UrlCheck(url=backup, up=False, detail="HTTP 404")]
        m.record("cloud.example", NodeHealth(node_id="cloud.example", check_type="HLS", up=False, urls=urls), ts=1_000 + i)
    m.PASSED_CACHE_S = 0
    every = m.checks_passed({main, backup}, since_s=10**10)["cloud.example"]
    assert every == {"checks": 10, "failed": 10}
    without = m.checks_passed({main}, since_s=10**10)["cloud.example"]  # the backup ticked "ignore"
    assert without == {"checks": 10, "failed": 2}
