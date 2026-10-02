"""Pluggable, Class-Based Stream & Network Probing Architecture.

Allows developers to extend Stream Graph with new protocols (e.g. DASH, RTMP,
WebRTC, SRT, or proprietary endpoints) simply by subclassing `BaseStreamProbe`
and decorating with `@ProbeRegistry.register("PROTOCOL_NAME")`.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import ClassVar, Dict, Optional, Type

import httpx
from health import NodeHealth, UrlCheck, check_hls_url, check_http_url, icmp_diagnostics


class BaseStreamProbe(ABC):
    """Abstract Base Class for all stream and network probes."""

    protocol: ClassVar[str] = "GENERIC"

    @abstractmethod
    async def check(
        self,
        url: str,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        use_cache: bool = True,
        **kwargs
    ) -> UrlCheck:
        """Perform a single endpoint probe and return a structured UrlCheck."""
        raise NotImplementedError


class ProbeRegistry:
    """Central registry for pluggable protocol probes."""

    _probes: Dict[str, BaseStreamProbe] = {}

    @classmethod
    def register(cls, protocol: str):
        """Decorator to register a probe class for a given protocol."""
        def decorator(subclass: Type[BaseStreamProbe]):
            instance = subclass()
            cls._probes[protocol.upper()] = instance
            return subclass
        return decorator

    @classmethod
    def get(cls, protocol: str) -> Optional[BaseStreamProbe]:
        """Retrieve registered probe instance by protocol name."""
        return cls._probes.get(protocol.upper())

    @classmethod
    def supported_protocols(cls) -> list[str]:
        """List all currently registered probe protocols."""
        return sorted(list(cls._probes.keys()))


# -----------------------------------------------------------------------------
# Standard Built-In Probes
# -----------------------------------------------------------------------------

@ProbeRegistry.register("HLS")
class HlsStreamProbe(BaseStreamProbe):
    """HLS Manifest, Segment Age, and Media Sequence Probe."""

    protocol = "HLS"

    async def check(
        self,
        url: str,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        use_cache: bool = True,
        **kwargs
    ) -> UrlCheck:
        return await check_hls_url(client, sem, url, use_cache=use_cache)


@ProbeRegistry.register("HTTP")
@ProbeRegistry.register("HTTPS")
class HttpStreamProbe(BaseStreamProbe):
    """Generic HTTP/HTTPS Status and Latency Probe."""

    protocol = "HTTP"

    async def check(
        self,
        url: str,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        use_cache: bool = True,
        https_only: bool = False,
        **kwargs
    ) -> UrlCheck:
        return await check_http_url(client, sem, url, https_only=https_only)


@ProbeRegistry.register("ICMP")
class IcmpNetworkProbe(BaseStreamProbe):
    """Host-level ICMP ping, jitter, and packet loss probe."""

    protocol = "ICMP"

    async def check(
        self,
        url: str,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        use_cache: bool = True,
        **kwargs
    ) -> UrlCheck:
        # url can be a hostname or IP
        diag = await icmp_diagnostics(url)
        return UrlCheck(
            url=url,
            up=bool(diag.get("loss", 100) < 100),
            latency_ms=diag.get("rtt"),
            detail=f"RTT: {diag.get('rtt')}ms, Loss: {diag.get('loss')}%",
            category=None if diag.get("loss", 100) < 100 else "ICMP_PACKET_LOSS",
        )
