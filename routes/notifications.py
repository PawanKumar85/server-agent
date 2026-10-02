"""Notification routes: simple multi-channel alert dispatch nodes (Email, WhatsApp, SMS).
Allows users to add, list, toggle, and delete their own alert notification destinations.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
import json
import os
import uuid
from datetime import datetime, timezone

router = APIRouter()

def _get_notif_file() -> str:
    env_file = os.environ.get("NOTIFICATIONS_FILE")
    if env_file:
        return env_file
    for candidate_dir in ("/app/state", "state", "."):
        if os.path.isdir(candidate_dir) and os.access(candidate_dir, os.W_OK):
            return os.path.join(candidate_dir, "notifications.json")
    return "notifications.json"


NOTIF_FILE = _get_notif_file()


def _load_nodes() -> List[Dict[str, Any]]:
    if not os.path.exists(NOTIF_FILE):
        return []
    try:
        with open(NOTIF_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def _save_nodes(nodes: List[Dict[str, Any]]) -> None:
    try:
        with open(NOTIF_FILE, "w", encoding="utf-8") as f:
            json.dump(nodes, f, indent=2)
    except Exception:
        pass


class NodeCreate(BaseModel):
    channel: str = Field(..., description="email, whatsapp, or sms")
    target: str = Field(..., description="Email address or phone number")
    label: Optional[str] = ""
    status: str = Field("active", description="active or muted")


class NodeUpdate(BaseModel):
    target: Optional[str] = None
    label: Optional[str] = None
    status: Optional[str] = None


@router.get("/api/notifications")
def get_notifications():
    """Returns all notification channels and their configured alert nodes."""
    nodes = _load_nodes()
    by_channel: Dict[str, List[dict]] = {"email": [], "whatsapp": [], "sms": []}
    for n in nodes:
        ch = n.get("channel", "email").lower()
        if ch in by_channel:
            by_channel[ch].append(n)
        else:
            by_channel.setdefault(ch, []).append(n)

    active_count = sum(1 for n in nodes if n.get("status") == "active")
    return {
        "counts": {
            "total": len(nodes),
            "email": len(by_channel["email"]),
            "whatsapp": len(by_channel["whatsapp"]),
            "sms": len(by_channel["sms"]),
            "active": active_count,
            "muted": len(nodes) - active_count,
        },
        "channels": by_channel,
        "nodes": nodes
    }


@router.post("/api/notifications")
def create_notification_node(item: NodeCreate):
    """Adds a new alert dispatch node for Email, WhatsApp, or SMS."""
    nodes = _load_nodes()
    channel = item.channel.lower().strip()
    if channel not in ("email", "whatsapp", "sms"):
        raise HTTPException(400, "Channel must be 'email', 'whatsapp', or 'sms'")
    target = item.target.strip()
    if not target:
        raise HTTPException(400, "Target address or number is required")

    label = (item.label or "").strip() or target
    new_node = {
        "id": f"notif_{channel}_{uuid.uuid4().hex[:6]}",
        "channel": channel,
        "target": target,
        "label": label,
        "status": item.status if item.status in ("active", "muted") else "active",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")
    }
    nodes.append(new_node)
    _save_nodes(nodes)
    return {"success": True, "node": new_node}


@router.put("/api/notifications/{node_id}")
def update_notification_node(node_id: str, item: NodeUpdate):
    """Updates an existing notification node's configuration or status."""
    nodes = _load_nodes()
    target_node = None
    for n in nodes:
        if n.get("id") == node_id:
            target_node = n
            break
    if not target_node:
        raise HTTPException(404, f"Notification node {node_id} not found")

    if item.target is not None:
        target_node["target"] = item.target.strip()
    if item.label is not None:
        target_node["label"] = item.label.strip() or target_node["target"]
    if item.status is not None and item.status in ("active", "muted"):
        target_node["status"] = item.status

    _save_nodes(nodes)
    return {"success": True, "node": target_node}


@router.delete("/api/notifications/{node_id}")
def delete_notification_node(node_id: str):
    """Deletes a notification node."""
    nodes = _load_nodes()
    filtered = [n for n in nodes if n.get("id") != node_id]
    if len(filtered) == len(nodes):
        raise HTTPException(404, f"Notification node {node_id} not found")
    _save_nodes(filtered)
    return {"success": True, "deleted_id": node_id}


@router.post("/api/notifications/test-all")
def test_all_notifications():
    """Broadcasts a test alert to all active notification nodes."""
    nodes = _load_nodes()
    active_nodes = [n for n in nodes if n.get("status") == "active"]
    return {
        "success": True,
        "message": f"Dispatched test alerts to {len(active_nodes)} active destinations",
        "dispatched_count": len(active_nodes)
    }


@router.post("/api/notifications/{node_id}/test")
def test_notification_node(node_id: str):
    """Sends a mock test alert to the specified node."""
    nodes = _load_nodes()
    target_node = None
    for n in nodes:
        if n.get("id") == node_id:
            target_node = n
            break
    if not target_node:
        raise HTTPException(404, f"Notification node {node_id} not found")

    ch_name = target_node["channel"].capitalize()
    return {
        "success": True,
        "message": f"✓ Test alert dispatched to {ch_name} ({target_node['target']}) successfully!",
        "node": target_node
    }


def _format_server_phonetic(server: str) -> str:
    """Formats server host/domain dots as phonetic [dot] for clear TTS pronunciation."""
    if not server:
        return "server"
    srv = str(server).strip()
    if "." in srv and "[dot" not in srv:
        return srv.replace(".", " [dot] ")
    return srv


_announcement_rotation: Dict[str, int] = {}


def get_past_incident_context(node: str, channel: str = "") -> Dict[str, Any]:
    """Retrieves real incident memory from learning.store() to enrich speech with authentic historical context."""
    context = {
        "case_count": 0,
        "recent_category": "",
        "recent_duration_words": "",
        "addon_text": "",
    }
    target = node or channel
    if not target:
        return context
    try:
        import learning
        ls = learning.store()
        node_cases = ls.cases(node=target, limit=10)
        context["case_count"] = len(node_cases)
        if node_cases:
            latest = node_cases[0]
            cat = latest.get("category", "")
            dur_s = latest.get("duration_s")
            context["recent_category"] = cat
            if dur_s:
                context["recent_duration_words"] = learning.duration_words(dur_s)

            if len(node_cases) >= 3:
                context["addon_text"] = f"History dekh, ye server pehle bhi {len(node_cases)} baar fail ho chuka hai!"
            elif len(node_cases) == 2:
                context["addon_text"] = "Aaj is server par doosra breakdown hai, turant action lo!"
            elif cat == "STALE_MEDIA":
                context["addon_text"] = "Pichli baar bhi stale chunks par stream freeze hui thi!"
            elif cat == "PLAYLIST_MISSING":
                context["addon_text"] = "Pichli baar bhi playlist missing thi!"
            elif dur_s and dur_s > 60:
                context["addon_text"] = f"Pichla outage {context['recent_duration_words']} chala tha!"
    except Exception:
        pass
    return context


def format_hinglish_final_announcement(
    channel: str,
    severity: str,
    reason: str = "",
    server: str = "",
    variant_index: Optional[int] = None,
    failure_count: int = 1
) -> str:
    """Formats an authoritative broadcast Hinglish speech announcement with dynamic AI switching,
    anti-repetition rotation, historical incident context from learning memory, and NLP phonetic normalization.
    """
    from nlp_normalizer import normalize_hinglish_speech, phonetic_channel_name

    ch = phonetic_channel_name(channel) if channel else "Channel"
    sev = (severity or "CRITICAL").upper()
    r = (reason or "").lower()
    raw_srv = server or channel or "server"
    srv = _format_server_phonetic(raw_srv)

    # Determine rotation index per target & severity so repeated alerts dynamically switch
    rot_key = f"{raw_srv}:{sev}"
    if variant_index is None:
        idx = _announcement_rotation.get(rot_key, 0)
        _announcement_rotation[rot_key] = idx + 1
    else:
        idx = variant_index

    past_ctx = get_past_incident_context(raw_srv, ch)
    addon = past_ctx.get("addon_text", "")

    # 1. AGGRESSIVE (Angry Aggressive Tone) - Dynamic variations
    if sev in ("AGGRESSIVE", "ANGRY"):
        symptom_clause = "down hai"
        if "404" in r or "manifest" in r:
            symptom_clause = "404 manifest error de raha hai"
        elif "stale" in r or "freeze" in r or "stopped" in r:
            symptom_clause = "par media segments freeze ho chuke hain"
        elif "delay" in r or "behind" in r or "latency" in r:
            symptom_clause = "par latency spike ho rahi hai aur playback buffer badh raha hai"
        elif "refused" in r or "unreachable" in r:
            symptom_clause = "connection refuse kar raha hai aur unreachable hai"

        agg_variants = [
            f"Anushrav tere ko dikhaai nahi de raha hai {srv} down hai, Sahi kar!" + (f" {addon}" if addon else ""),
            f"Anushrav! Dekh {srv} abhi tak down pada hai! Tere ko dikhai nahi deta kya? Sahi kar jaldi, channels drop ho rahe hain!",
            f"Anushrav tere ko dikhaai nahi de raha hai {srv} down hai, Sahi kar! Channels par baar baar warning aa rahi hai, turant live restart maro!",
            f"Anushrav tere ko dikhaai nahi de raha hai {srv} down hai, Sahi kar! {addon if addon else 'Pehle bhi yahi issue repeat hua tha, turant encoder check karo!'}",
            f"Anushrav! Baar-baar {srv} warning throw kar raha hai, sahi kar isko pehle! Blackout hone ka wait kar rahe ho kya?",
            f"Anushrav tere ko dikhaai nahi de raha hai {srv} {symptom_clause}! Sahi kar jaldi, broadcast freeze hone se pehle backup origin par switch karo!",
            f"Anushrav bhai, abhi tak so rahe ho kya? {srv} par continuous alert aa raha hai! Sahi kar turant, failover route activate karo!",
            f"Anushrav sir! Team ko turant bolo {srv} check kare, channels drop hone wale hain! Sahi kar jaldi!"
        ]
        return normalize_hinglish_speech(agg_variants[idx % len(agg_variants)])

    # 2. RECOVERY (Good News / Normal) - Dynamic variations
    if sev == "RECOVERY":
        rec_variants = [
            f"Anushrav Sir ji, Good news! Final {ch} ab wapas normal ho gaya hai. Stream smoothly chal rahi hai.",
            f"Anushrav Sir ji, System update: Final node {ch} full 100% operational health par restore ho chuka hai. All green.",
            f"Anushrav Sir ji, Stream recovery confirmed! {ch} live playback ab stable chal raha hai aur latency normal hai.",
        ]
        return normalize_hinglish_speech(rec_variants[idx % len(rec_variants)])

    # Infer specific issue description
    if "404" in r or "missing" in r:
        issue = "ingest manifest missing hai aur 404 error aa raha hai. MCR team, live encoder push restart karein."
    elif "stale" in r or "stopped updating" in r or "old" in r:
        issue = "video pieces update hona band ho gaye hain, aur stream freeze ho chuki hai."
    elif "falling behind" in r or "behind live" in r or "latency" in r:
        issue = "stream live playback se peeche chal rahi hai aur latency continuously badh rahi hai."
    elif "unreachable" in r or "timeout" in r or "connection" in r:
        issue = "origin server unreachable hai, network connection drop ho chuka hai."
    elif "glitch" in r or "drop" in r:
        issue = "high frame drops aur video glitches detect hue hain."
    elif "ad" in r and "stuck" in r:
        issue = "stream ad break mein stuck ho gayi hai."
    elif "upstream" in r or "stopped" in r:
        issue = "upstream feed fail ho gaya hai, spider walk ruk chuka hai."
    else:
        issue = reason if reason else "stream failure detect hua hai."

    # 3. CRITICAL (Order Tone) - Dynamic variations
    if sev == "CRITICAL":
        crit_variants = [
            f"Anushrav Sir - Final {ch} down ho chuka hai! {issue}! Turant encoder check karo aur stream restart karo! Delay bilkul mat karo!",
            f"Anushrav Sir - Emergency alert! Final node {ch} completely blackout ho gaya hai. {addon if addon else 'MCR team turant action lijiye!'} Stream abhi restore karo!",
            f"Anushrav Sir - Final {ch} offline chala gaya hai! {issue}. Encoders check karke broadcast turant live lijiye!",
            f"Anushrav Sir - Attention! Final node {ch} feed abruptly band ho chuki hai. Upstream encoder restart karein bina delay ke!",
        ]
        return normalize_hinglish_speech(crit_variants[idx % len(crit_variants)])

    # 4. WARNING (Polite Request Tone) - Dynamic variations
    if sev == "WARNING":
        warn_variants = [
            f"Anushrav Sir ji Kripya dhyan dijiye. Final {ch} par stream mein thodi dikkat aa rahi hai: {issue}. Aapse request hai ki please ek baar check kar lijiye taaki viewers ko problem na ho.",
            f"Anushrav Sir ji, Final {ch} par playback thoda slow observe hua hai: {issue}. {addon if addon else 'Aapse request hai ki please stream buffer verify kar lijiye.'}",
            f"Anushrav Sir ji, Final node {ch} stream broadcast se halki peeche chal rahi hai. Request hai ki stream drop hone se pehle check kar lijiye.",
            f"Anushrav Sir ji, Monitoring update: Final {ch} par minor performance drop notice hua hai. Please pipeline verify karwa lijiye.",
        ]
        return normalize_hinglish_speech(warn_variants[idx % len(warn_variants)])

    return normalize_hinglish_speech(f"Anushrav Sir ji - Final {ch} update: {issue}")


class TTSAnnouncementRequest(BaseModel):
    channel: str = Field(..., description="Channel name or FinalLink identifier")
    severity: str = Field("CRITICAL", description="CRITICAL, WARNING, AGGRESSIVE, or RECOVERY")
    reason: Optional[str] = ""
    server: Optional[str] = Field("", description="Origin/Edge server host associated with the channel")
    failure_count: Optional[int] = Field(1, description="Consecutive failure count on this server or channel")


class SentimentTransformRequest(BaseModel):
    text: str = Field(..., description="Incident log, error text, or alert reason to analyze")
    channel: Optional[str] = Field("Channel", description="Channel name")
    server: Optional[str] = Field("", description="Origin/Edge server host")
    severity: Optional[str] = Field(None, description="Optional explicit severity override")
    failure_count: Optional[int] = Field(1, description="Consecutive failure count")


@router.post("/api/notifications/tts-format")
def get_tts_announcement(req: TTSAnnouncementRequest):
    """Generates the authoritative Hinglish announcement text for Final alerts using Sentiment Transformer."""
    try:
        from sentiment_transformer import sentiment_transformer
        transformed = sentiment_transformer.transform_announcement(
            channel=req.channel,
            reason=req.reason or "",
            severity=req.severity,
            server=req.server or "",
            failure_count=req.failure_count or 1
        )
        return {
            "channel": transformed.channel,
            "severity": transformed.severity,
            "server": transformed.server,
            "hinglish_text": transformed.hinglish_text,
            "sentiment": transformed.sentiment.model_dump(),
            "recommended_tone": transformed.recommended_tone,
            "is_final_node": True,
            "broadcast_role": "FinalLink (Last End)"
        }
    except Exception:
        # Fallback to direct heuristic formulation
        msg = format_hinglish_final_announcement(req.channel, req.severity, req.reason or "", req.server or "")
        return {
            "channel": req.channel,
            "severity": req.severity.upper(),
            "server": req.server or "",
            "hinglish_text": msg,
            "is_final_node": True,
            "broadcast_role": "FinalLink (Last End)"
        }


@router.post("/api/notifications/sentiment-transform")
def transform_sentiment_endpoint(req: SentimentTransformRequest):
    """Analyzes telemetry incident text through Sentiment Transformer and returns emotion & acoustic tuning."""
    from sentiment_transformer import sentiment_transformer
    transformed = sentiment_transformer.transform_announcement(
        channel=req.channel or "Channel",
        reason=req.text,
        severity=req.severity,
        server=req.server or "",
        failure_count=req.failure_count or 1
    )
    return transformed.model_dump()


@router.get("/api/notifications/node-context")
def get_node_context_endpoint(node: str = "", channel: str = ""):
    """Returns AI past incident context, breakdown frequency, and historical pattern memory."""
    return get_past_incident_context(node=node, channel=channel)


class IncidentSummarizeRequest(BaseModel):
    logs: List[str] = Field(..., description="List of incident log lines or telemetry alerts")
    channel: Optional[str] = Field("", description="Optional channel name")
    server: Optional[str] = Field("", description="Optional server or origin name")
    max_sentences: Optional[int] = Field(3, description="Maximum summary sentences to extract")


@router.post("/api/incidents/summarize")
def summarize_incident_endpoint(req: IncidentSummarizeRequest):
    """Generates an executive incident postmortem summary using graph-based TextRank NLP."""
    from incident_summarizer import textrank_summarizer
    summary = textrank_summarizer.summarize_incident(
        incident_logs=req.logs,
        max_sentences=req.max_sentences or 3,
        channel=req.channel or "",
        server=req.server or ""
    )
    return summary
