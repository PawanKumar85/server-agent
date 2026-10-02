"""Human-sounding voice alerts (voice.py): speak an alert, play a stored clip, rate it, learn, export the dataset."""

import os
import tempfile
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field

import server as srv

router = APIRouter()


class AlertRequest(BaseModel):
    severity: Literal["CRITICAL", "WARNING", "AGGRESSIVE", "RECOVERY"] = "CRITICAL"
    channels: List[str] = Field(default_factory=list, max_length=20)
    server: Optional[str] = Field(None, max_length=255)
    title: Optional[str] = Field(None, max_length=500)
    detail: Optional[str] = Field(None, max_length=1000)
    minutes: Optional[float] = Field(None, ge=0, le=100000)
    subject: Optional[str] = Field(None, max_length=500)
    style: Optional[str] = Field(None, max_length=20)  # force a mood (the Learning page's preview)


class VoiceSettings(BaseModel):
    profanity: Optional[Literal["off", "mild", "strong"]] = None
    voices: Optional[Dict[str, str]] = None  # voice -> the name it is shown under ("" → Bot)
    moods: Optional[Dict[str, str]] = None  # mood -> the name every alert in it is shown under


class PlayUpdate(BaseModel):
    heard: bool = False
    acked_after_s: Optional[float] = Field(None, ge=0, le=86400)
    rating: Optional[Literal[1, -1]] = None


def history_line(server: Optional[str]) -> str:
    """One sentence from the incident history, so the line can say 'aaj teesri baar'."""
    if not server:
        return ""
    try:
        import time as _t
        from datetime import datetime
        cases = srv.learner.cases(node=server, limit=50)
        day = _t.time() - 86400
        today = [c for c in cases if datetime.fromisoformat(str(c.get("opened")).replace("Z", "+00:00")).timestamp() > day]
        if len(today) >= 2:
            return f"Aaj ye {len(today)} baar pehle bhi down ho chuka hai"
    except Exception:
        pass
    return ""


def learned_advice(server: Optional[str]) -> dict:
    """From the incident history: the fix the operator recorded most recently for this server, and the server that
    was most often the real root cause of its outages (at least twice in the last 20)."""
    out = {"learned_fix": "", "usual_root": ""}
    if not server:
        return out
    try:
        from collections import Counter
        cases = srv.learner.cases(node=server, limit=20)
        out["learned_fix"] = next((c["resolution"] for c in cases if c.get("resolution")), "") or ""
        roots = Counter(c["root_cause"] for c in cases if c.get("root_cause") and c["root_cause"] != server)
        if roots and roots.most_common(1)[0][1] >= 2:
            out["usual_root"] = roots.most_common(1)[0][0]
    except Exception:
        pass
    return out


@router.post("/api/voice/alert")
def speak_alert(body: AlertRequest):
    event = body.model_dump()
    event["history"] = history_line(body.server)
    event.update(learned_advice(body.server))
    return srv.voice.alert(event)


@router.get("/api/voice/clips/{name}")
def clip_audio(name: str):
    """A stored clip (FLAC, or WAV for older ones); '/12', '/12.wav' and '/12.flac' all work."""
    try:
        clip_id = int(name.split(".")[0])
    except ValueError:
        raise HTTPException(404, "No such clip")
    found = srv.voice.store.audio(clip_id)
    if not found:
        raise HTTPException(404, "No such clip")
    from voice import MEDIA_TYPES
    blob, codec = found
    return Response(blob, media_type=MEDIA_TYPES.get(codec, "audio/wav"),
                    headers={"Cache-Control": "private, max-age=86400, immutable"})


@router.post("/api/voice/plays/{play_id}")
def update_play(play_id: int, body: PlayUpdate):
    if not srv.voice.store.update_play(play_id, body.heard, body.acked_after_s, body.rating):
        raise HTTPException(404, "No such play (or nothing to record)")
    return {"ok": True}


@router.get("/api/voice/stats")
def voice_stats():
    return {**srv.voice.store.stats(), "operator": srv.voice.operator, "profanity": srv.voice.profanity,
            "people": srv.voice.people(), "library": srv.voice.library.status()}


@router.post("/api/voice/settings")
def voice_settings(body: VoiceSettings):
    """How rough the angry and furious alerts may get (off, mild, strong), and the names voices are shown under."""
    import json
    from voice import STYLES
    if body.profanity:
        srv.voice.store.set_setting("profanity", body.profanity)
    if body.voices is not None or body.moods is not None:
        saved = json.loads(srv.voice.store.setting("people", "") or "{}")
        known = {v for st in STYLES.values() for v in st["voices"]} | set(srv.voice.people()["voices"])
        for key, given, allowed in (("voices", body.voices, known), ("moods", body.moods, set(STYLES))):
            for k, name in (given or {}).items():
                if k not in allowed:
                    raise HTTPException(422, f"Unknown {key[:-1]} '{k}'")
                saved.setdefault(key, {})[k] = " ".join(name.split())[:40]
        srv.voice.store.set_setting("people", json.dumps(saved))
    return {"profanity": srv.voice.profanity, "people": srv.voice.people()}


@router.get("/api/voice/dataset.zip")
def voice_dataset(min_rating: int = 0):
    """Every stored clip as a training dataset (WAVs + LJSpeech metadata + JSONL with moods and ratings)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    n = srv.voice.store.export(tmp, min_rating=min_rating)
    tmp.close()
    if not n:
        os.unlink(tmp.name)
        raise HTTPException(404, "No clips stored yet")
    return FileResponse(tmp.name, media_type="application/zip", filename="stream-graph-voice-dataset.zip",
                        background=BackgroundTask(os.unlink, tmp.name))
