"""Health checks for resource nodes.

A node's `checkType` picks the check (default HLS). Every URL on the node must
pass for the node to be UP. Where a host is known, ICMP diagnostics (latency,
jitter, packet loss) are collected alongside; traceroute/hop count needs root.
"""

import asyncio
import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional, Tuple

import httpx
import m3u8
from icmplib import async_ping, traceroute
from pydantic import BaseModel

TIMEOUT_S = 5
RETRIES = 2  # extra attempts on timeouts, connection errors and 5xx (Scrapy's default)
CONCURRENCY = 32  # max HTTP requests in flight
STALE_AFTER_TARGET_DURATIONS = 3  # newest segment older than this many target durations = frozen stream
# Freshness grades by newest-segment age, in target durations (so they suit any segment length):
# FRESH <= 1.5, WARNING <= 2, DEGRADED <= 3, STALE beyond (down).
FRESHNESS_GRADES = ((1.5, "FRESH"), (2.0, "WARNING"), (3.0, "DEGRADED"))
CHECK_TYPES = ("HLS", "HTTP", "HTTPS", "ICMP", "PROCESS")


class UnsupportedCheck(Exception):
    """The node's checkType can't be run from here."""


class UrlCheck(BaseModel):
    url: str
    up: bool
    status: Optional[int] = None
    latency_ms: Optional[int] = None
    detail: str
    # Why it failed, as a bucket: STALE_MEDIA | PLAYLIST_MISSING | UNREACHABLE | SERVER_ERROR | HTTP_ERROR |
    # INVALID_PLAYLIST | NO_SEGMENTS (None when up)
    category: Optional[str] = None
    freshness: Optional[str] = None  # FRESH | WARNING | DEGRADED | STALE (HLS media with PROGRAM-DATE-TIME)
    segment_age_s: Optional[float] = None  # newest segment's age (best live variant for a master)
    target_duration_s: Optional[float] = None
    sequences: Dict[str, int] = {}  # variant -> newest segment's media sequence number ("" = the URL itself)
    bitrates: List[int] = []  # ABR bitrate ladder in bps
    resolutions: List[str] = []  # variant resolutions (e.g. 1280x720)
    discontinuities: int = 0  # #EXT-X-DISCONTINUITY count in media playlist
    cdn_cache: Optional[str] = None  # HIT / MISS / EXPIRED from X-Cache or CF-Cache-Status
    server_hdr: Optional[str] = None  # Server response header (e.g. nginx/1.24)


class NodeHealth(BaseModel):
    node_id: str
    check_type: str
    up: bool
    latency_ms: Optional[float] = None
    error: Optional[str] = None
    urls: List[UrlCheck] = []
    consecutive_failures: int = 0
    # ICMP diagnostics (None when unavailable)
    rtt_ms: Optional[float] = None
    jitter_ms: Optional[float] = None
    packet_loss: Optional[float] = None
    hop_count: Optional[int] = None


# --- HTTP / HLS -------------------------------------------------------------


async def fetch(client: httpx.AsyncClient, sem: asyncio.Semaphore, url: str) -> Tuple[httpx.Response, int]:
    """GET with retries; returns the last response (possibly 5xx) or raises the last transport error."""
    for attempt in range(RETRIES + 1):
        async with sem:
            start = time.monotonic()
            try:
                resp = await client.get(url)
            except httpx.TransportError:
                if attempt == RETRIES:
                    raise
                continue
        latency = int((time.monotonic() - start) * 1000)
        if resp.status_code < 500 or attempt == RETRIES:
            return resp, latency
    raise AssertionError("unreachable")


def parse_playlist(resp: httpx.Response, url: str) -> Optional[m3u8.M3U8]:
    if resp.status_code != 200:
        return None
    try:
        return m3u8.loads(resp.text, uri=url)
    except Exception:  # m3u8 raises assorted parse errors on garbage input
        return None


def segment_age_s(playlist: m3u8.M3U8, resp: httpx.Response) -> Optional[float]:
    """Seconds since the newest segment ended, by the server's clock; None without PROGRAM-DATE-TIME."""
    if not playlist.segments:
        return None
    last = playlist.segments[-1]
    if last.current_program_date_time is None:
        return None
    try:
        now = parsedate_to_datetime(resp.headers["Date"])
    except (KeyError, TypeError, ValueError):
        now = datetime.now(timezone.utc)
    end = last.current_program_date_time.timestamp() + (last.duration or 0)
    return now.timestamp() - end


def freshness_grade(age_s: Optional[float], target_s: float) -> Optional[str]:
    if age_s is None:
        return None
    for limit, grade in FRESHNESS_GRADES:
        if age_s <= limit * target_s:
            return grade
    return "STALE"


def status_category(status: int) -> str:
    if status in (404, 410):
        return "PLAYLIST_MISSING"
    return "SERVER_ERROR" if status >= 500 else "HTTP_ERROR"


class MediaState(BaseModel):
    """One media playlist: why it's not live (problem/category, None if live) plus what was measured."""
    problem: Optional[str] = None
    category: Optional[str] = None
    age_s: Optional[float] = None
    target_s: Optional[float] = None
    sequence: Optional[int] = None  # the newest segment's media sequence number
    discontinuities: int = 0


async def media_state(client: httpx.AsyncClient, sem: asyncio.Semaphore, url: str) -> MediaState:
    try:
        resp, _ = await fetch(client, sem, url)
    except httpx.TransportError as e:
        return MediaState(problem=f"HTTP_{type(e).__name__.upper()}", category="UNREACHABLE")
    playlist = parse_playlist(resp, url)
    if playlist is None:
        if resp.status_code != 200:
            return MediaState(problem=f"HTTP {resp.status_code}", category=status_category(resp.status_code))
        return MediaState(problem="INVALID_PLAYLIST", category="INVALID_PLAYLIST")
    if not playlist.segments:
        return MediaState(problem="NO_SEGMENTS", category="NO_SEGMENTS")
    target = float(playlist.target_duration or 6)
    age = segment_age_s(playlist, resp)
    sequence = (playlist.media_sequence or 0) + len(playlist.segments) - 1
    discont = sum(1 for seg in (playlist.segments or []) if getattr(seg, "discontinuity", False))
    state = MediaState(age_s=round(age, 1) if age is not None else None, target_s=target, sequence=sequence, discontinuities=discont)
    if age is not None and age > STALE_AFTER_TARGET_DURATIONS * target:
        state.problem, state.category = f"STALE_SEGMENTS ({int(age)}s old)", "STALE_MEDIA"
    return state


async def media_problem(client: httpx.AsyncClient, sem: asyncio.Semaphore, url: str) -> Optional[str]:
    """None if the media playlist is live and fresh, otherwise why not."""
    return (await media_state(client, sem, url)).problem


HEALTH_CACHE_ENABLED = os.environ.get("HEALTH_CACHE_ENABLED", "1") not in ("0", "false", "False")
URL_CACHE_TTL_S = float(os.environ.get("URL_CACHE_TTL_S", "5.0"))  # dedupe within one cycle only
ICMP_CACHE_TTL_S = float(os.environ.get("ICMP_CACHE_TTL_S", "5.0"))

_URL_CACHE: Dict[str, Tuple[float, UrlCheck]] = {}
_URL_IN_FLIGHT: Dict[str, asyncio.Future] = {}
_ICMP_CACHE: Dict[str, Tuple[float, dict]] = {}
_ICMP_IN_FLIGHT: Dict[str, asyncio.Future] = {}


def clear_health_caches() -> None:
    """Clear in-memory deduplication and health caches."""
    _URL_CACHE.clear()
    _URL_IN_FLIGHT.clear()
    _ICMP_CACHE.clear()
    _ICMP_IN_FLIGHT.clear()


async def _do_check_hls_url(client: httpx.AsyncClient, sem: asyncio.Semaphore, url: str) -> UrlCheck:
    """Up = valid playlist whose newest segments are fresh; a master needs at least one such variant."""
    try:
        resp, latency = await fetch(client, sem, url)
    except httpx.TransportError as e:
        return UrlCheck(url=url, up=False, category="UNREACHABLE",
                        detail=f"HTTP_{type(e).__name__.upper()} after {RETRIES + 1} tries")
    if resp.status_code != 200:
        return UrlCheck(url=url, up=False, status=resp.status_code, latency_ms=latency,
                        detail=f"HTTP {resp.status_code}", category=status_category(resp.status_code))
    playlist = parse_playlist(resp, url)
    if playlist is None:
        return UrlCheck(url=url, up=False, status=200, latency_ms=latency, detail="INVALID_PLAYLIST",
                        category="INVALID_PLAYLIST")

    # Extract HTTP headers for CDN/Origin intelligence
    cdn_cache = resp.headers.get("X-Cache") or resp.headers.get("CF-Cache-Status") or resp.headers.get("X-Proxy-Cache")
    server_hdr = resp.headers.get("Server")
    bitrates: List[int] = []
    resolutions: List[str] = []

    if playlist.is_variant:
        # Follow the master's links like a crawler: each variant is checked for fresh segments.
        names = [p.uri for p in playlist.playlists]
        for p in playlist.playlists:
            if getattr(p, "stream_info", None):
                if getattr(p.stream_info, "bandwidth", None):
                    bitrates.append(int(p.stream_info.bandwidth))
                if getattr(p.stream_info, "resolution", None):
                    resolutions.append(f"{p.stream_info.resolution[0]}x{p.stream_info.resolution[1]}")
        states = await asyncio.gather(*(media_state(client, sem, p.absolute_uri) for p in playlist.playlists))
    else:
        names, states = [""], [await media_state(client, sem, url)]
    live = [st for st in states if st.problem is None]
    measured = [st for st in live if st.age_s is not None] or [st for st in states if st.age_s is not None]
    best = min(measured, key=lambda st: st.age_s) if measured else None
    target = (best or (live[0] if live else states[0])).target_s
    total_discont = sum(st.discontinuities for st in states)
    check = UrlCheck(
        url=url, up=bool(live), status=200, latency_ms=latency,
        segment_age_s=best.age_s if best else None, target_duration_s=target,
        freshness=freshness_grade(best.age_s, target) if best and target else None,
        sequences={n: st.sequence for n, st in zip(names, states) if st.sequence is not None},
        bitrates=bitrates, resolutions=resolutions, discontinuities=total_discont,
        cdn_cache=cdn_cache, server_hdr=server_hdr,
        detail="",
    )
    if playlist.is_variant:
        check.detail = f"master, {len(live)}/{len(states)} variants live"
        if not live:
            first = next(st for st in states if st.problem)
            check.detail += f" ({first.problem})"
            check.category = first.category
    elif live:
        check.detail = f"media, {len(playlist.segments)} fresh segments"
    else:
        check.detail, check.category = states[0].problem, states[0].category
    return check


async def check_hls_url(client: httpx.AsyncClient, sem: asyncio.Semaphore, url: str, use_cache: bool = True) -> UrlCheck:
    """Check HLS URL with deduplication cache and coalesced in-flight requests."""
    if use_cache and HEALTH_CACHE_ENABLED:
        now = time.monotonic()
        if url in _URL_CACHE:
            ts, val = _URL_CACHE[url]
            if now - ts < URL_CACHE_TTL_S and val.up:
                return val.model_copy()
        if url in _URL_IN_FLIGHT:
            try:
                res = await _URL_IN_FLIGHT[url]
                return res.model_copy()
            except Exception:
                pass

    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    if use_cache and HEALTH_CACHE_ENABLED:
        _URL_IN_FLIGHT[url] = fut

    try:
        check = await _do_check_hls_url(client, sem, url)
        if use_cache and HEALTH_CACHE_ENABLED:
            if check.up:
                _URL_CACHE[url] = (time.monotonic(), check)
            else:
                _URL_CACHE.pop(url, None)
        if not fut.done():
            fut.set_result(check)
        return check
    except Exception as e:
        if not fut.done():
            fut.set_exception(e)
        raise
    finally:
        if use_cache and HEALTH_CACHE_ENABLED:
            _URL_IN_FLIGHT.pop(url, None)


async def check_http_url(client: httpx.AsyncClient, sem: asyncio.Semaphore, url: str, https_only: bool) -> UrlCheck:
    if https_only and not url.startswith("https://"):
        return UrlCheck(url=url, up=False, detail="NOT_HTTPS")
    try:
        resp, latency = await fetch(client, sem, url)
    except httpx.TransportError as e:
        return UrlCheck(url=url, up=False, detail=f"HTTP_{type(e).__name__.upper()} after {RETRIES + 1} tries")
    return UrlCheck(url=url, up=resp.status_code < 400, status=resp.status_code, latency_ms=latency, detail=f"HTTP {resp.status_code}")


# --- ICMP diagnostics ---------------------------------------------------------


async def _raw_icmp_diagnostics(host: str) -> dict:
    try:
        result = await async_ping(host, count=4, interval=0.2, timeout=2, privileged=False)
    except Exception:  # unresolvable host, sockets unavailable, ...
        return {}
    diag = {"alive": result.is_alive, "packet_loss": result.packet_loss}
    if result.is_alive:
        diag.update(rtt_ms=result.avg_rtt, jitter_ms=result.jitter)
    if hasattr(os, "geteuid") and os.geteuid() == 0:  # raw sockets: traceroute needs root
        try:
            hops = await asyncio.get_running_loop().run_in_executor(None, lambda: traceroute(host, max_hops=30))
            diag["hop_count"] = len(hops)
        except Exception:
            pass
    return diag


async def icmp_diagnostics(host: str, use_cache: bool = True) -> dict:
    """ICMP diagnostics with host-level deduplication cache and coalesced requests."""
    if use_cache and HEALTH_CACHE_ENABLED:
        now = time.monotonic()
        if host in _ICMP_CACHE:
            ts, val = _ICMP_CACHE[host]
            if now - ts < ICMP_CACHE_TTL_S:
                return dict(val)
        if host in _ICMP_IN_FLIGHT:
            try:
                res = await _ICMP_IN_FLIGHT[host]
                return dict(res)
            except Exception:
                pass

    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    if use_cache and HEALTH_CACHE_ENABLED:
        _ICMP_IN_FLIGHT[host] = fut

    try:
        diag = await _raw_icmp_diagnostics(host)
        if use_cache and HEALTH_CACHE_ENABLED:
            _ICMP_CACHE[host] = (time.monotonic(), diag)
        if not fut.done():
            fut.set_result(diag)
        return diag
    except Exception as e:
        if not fut.done():
            fut.set_exception(e)
        raise
    finally:
        if use_cache and HEALTH_CACHE_ENABLED:
            _ICMP_IN_FLIGHT.pop(host, None)


# --- Node check ---------------------------------------------------------------


async def check_node(node: dict, client: httpx.AsyncClient, sem: asyncio.Semaphore) -> NodeHealth:
    """`node` has id, url (list), server_ip and optional checkType."""
    node_id = node["id"]
    check_type = (node.get("checkType") or "HLS").upper()
    host = node.get("server_ip") or node_id.split("/", 1)[0]  # channel Final ids are "<host>/<channel>"
    urls = node.get("url") or []

    if check_type == "PROCESS":
        raise UnsupportedCheck(f"{node_id}: PROCESS checks need an agent on the host, and none is configured")
    if check_type not in CHECK_TYPES:
        raise UnsupportedCheck(f"{node_id}: unknown checkType {check_type!r}")

    diag_task = asyncio.ensure_future(icmp_diagnostics(host))
    if check_type == "ICMP":
        diag = await diag_task
        up = bool(diag.get("alive"))
        return NodeHealth(
            node_id=node_id, check_type=check_type, up=up, latency_ms=diag.get("rtt_ms"),
            error=None if up else "ICMP_UNREACHABLE", rtt_ms=diag.get("rtt_ms"), jitter_ms=diag.get("jitter_ms"),
            packet_loss=diag.get("packet_loss"), hop_count=diag.get("hop_count"),
        )

    if check_type == "HLS":
        checks = await asyncio.gather(*(check_hls_url(client, sem, u) for u in urls))
    else:
        checks = await asyncio.gather(*(check_http_url(client, sem, u, check_type == "HTTPS") for u in urls))
    diag = await diag_task

    failed = [c for c in checks if not c.up]
    latencies = [c.latency_ms for c in checks if c.latency_ms is not None]
    if not urls:
        error = "NO_URLS"
    elif failed:
        error = f"{len(failed)}/{len(checks)} URLs failing: " + "; ".join(f"{c.url} {c.detail}" for c in failed)
    else:
        error = None
    return NodeHealth(
        node_id=node_id, check_type=check_type, up=error is None,
        latency_ms=max(latencies) if latencies else None,  # worst URL
        error=error, urls=list(checks), rtt_ms=diag.get("rtt_ms"), jitter_ms=diag.get("jitter_ms"),
        packet_loss=diag.get("packet_loss"), hop_count=diag.get("hop_count"),
    )


def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=TIMEOUT_S, limits=httpx.Limits(max_connections=CONCURRENCY), follow_redirects=True
    )
