"""Per-channel settings: the "ignore alerts" switch, and the per-stream "ignore this stream" tick (channel_mute.py)."""

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

import server as srv

router = APIRouter()


class MuteRequest(BaseModel):
    muted: bool = False
    note: Optional[str] = Field(None, max_length=200)  # e.g. "maintenance until 6 pm"


@router.get("/api/channels/mutes")
def list_mutes():
    """Channels whose alerts are ignored (every other channel alerts normally)."""
    return {"muted": srv.channel_mutes.muted()}


@router.put("/api/channels/{channel}/mute")
def set_mute(channel: str, body: MuteRequest):
    """Turn "ignore alerts" on or off for one channel. Checks and history continue either way."""
    return srv.channel_mutes.set(channel.strip(), body.muted, (body.note or "").strip() or None)


class StreamIgnoreRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=2000)
    ignored: bool = True
    note: Optional[str] = Field(None, max_length=200)  # e.g. "backup encoder retired"


@router.get("/api/streams/ignored")
def list_ignored_streams():
    """Stream URLs ticked as ignored (still checked, never counted as a failure)."""
    return {"ignored": srv.stream_ignores.all()}


@router.put("/api/streams/ignore")
def set_stream_ignore(body: StreamIgnoreRequest):
    """Tick or untick "ignore this stream" for one URL. Takes effect from the next health check."""
    return srv.stream_ignores.set(body.url.strip(), body.ignored, (body.note or "").strip() or None)
