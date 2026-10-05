"""Per-channel settings: the "ignore alerts" switch (channel_mute.py)."""

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
