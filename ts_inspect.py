"""What is inside an MPEG-TS video segment: which tracks it carries (video, audio) and their timestamps.

Every stream here muxes audio and video into the same .ts segments (CODECS "avc1...,mp4a..."), so the playlists can't
say whether the audio is still there; the segment can. Pure Python, no ffmpeg: reads the PAT -> PMT (the track list),
then the PES headers' presentation timestamps (PTS, 90 kHz) per track.

inspect(data) -> {"ts": bool, "video": bool, "audio": bool, "video_s": float, "audio_s": float, "drift_ms": float|None}
  video_s / audio_s: how many seconds each track covers in this segment; drift_ms: first audio PTS minus first video
  PTS (how far the sound is from the picture at the start of the segment).
"""

from typing import Dict, List, Optional

PACKET = 188
SYNC = 0x47
VIDEO_TYPES = {0x01, 0x02, 0x10, 0x1B, 0x24}  # MPEG-1/2 video, MPEG-4 part 2, H.264, HEVC
AUDIO_TYPES = {0x03, 0x04, 0x0F, 0x11, 0x81, 0x87}  # MPEG audio, AAC (ADTS / LATM), AC-3, E-AC-3
PTS_WRAP = 1 << 33


def _payload(pkt: bytes) -> Optional[bytes]:
    afc = (pkt[3] >> 4) & 0x3
    if afc in (0, 2):
        return None  # no payload
    start = 4
    if afc == 3:
        start += 1 + pkt[4]
    return pkt[start:] if start < PACKET else None


def _section(payload: bytes) -> bytes:
    return payload[1 + payload[0]:]  # skip the pointer field


def _pts(pes: bytes) -> Optional[int]:
    if len(pes) < 14 or pes[0:3] != b"\x00\x00\x01" or not (pes[7] >> 7) & 1:
        return None
    b = pes[9:14]
    return ((b[0] >> 1) & 0x07) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1


def _span(pts: List[int]) -> float:
    if len(pts) < 2:
        return 0.0
    lo, hi = min(pts), max(pts)
    if hi - lo > PTS_WRAP // 2:  # the 33-bit clock wrapped inside this segment
        pts = [p + PTS_WRAP if p < PTS_WRAP // 2 else p for p in pts]
        lo, hi = min(pts), max(pts)
    return (hi - lo) / 90000.0


def inspect(data: bytes) -> Dict[str, object]:
    out = {"ts": False, "video": False, "audio": False, "video_s": 0.0, "audio_s": 0.0, "drift_ms": None}
    start = data.find(bytes([SYNC]))
    while 0 <= start < len(data) - 2 * PACKET and not (data[start + PACKET] == SYNC):
        start = data.find(bytes([SYNC]), start + 1)  # resync on two packets in a row
    if start < 0 or start >= len(data) - PACKET:
        return out
    out["ts"] = True
    pmt_pids, kinds = set(), {}  # pid -> "video" | "audio"
    pts: Dict[str, List[int]] = {"video": [], "audio": []}
    for i in range(start, len(data) - PACKET + 1, PACKET):
        pkt = data[i:i + PACKET]
        if pkt[0] != SYNC:
            continue
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        pusi = pkt[1] & 0x40
        payload = _payload(pkt)
        if payload is None:
            continue
        if pid == 0 and pusi:  # PAT: where the program's track list (PMT) is
            sec = _section(payload)
            length = ((sec[1] & 0x0F) << 8) | sec[2]
            for j in range(8, 3 + length - 4, 4):
                if j + 4 <= len(sec):
                    program = (sec[j] << 8) | sec[j + 1]
                    if program:
                        pmt_pids.add(((sec[j + 2] & 0x1F) << 8) | sec[j + 3])
        elif pid in pmt_pids and pusi:  # PMT: the tracks
            sec = _section(payload)
            length = ((sec[1] & 0x0F) << 8) | sec[2]
            info_len = ((sec[10] & 0x0F) << 8) | sec[11]
            j, end = 12 + info_len, 3 + length - 4
            while j + 5 <= min(end, len(sec)):
                stype, es_pid = sec[j], ((sec[j + 1] & 0x1F) << 8) | sec[j + 2]
                es_info = ((sec[j + 3] & 0x0F) << 8) | sec[j + 4]
                if stype in VIDEO_TYPES:
                    kinds[es_pid] = "video"
                elif stype in AUDIO_TYPES:
                    kinds[es_pid] = "audio"
                j += 5 + es_info
        elif pid in kinds and pusi:
            value = _pts(payload)
            if value is not None:
                pts[kinds[pid]].append(value)
    for kind in ("video", "audio"):
        out[kind] = bool(pts[kind])
        out[f"{kind}_s"] = round(_span(pts[kind]), 3)
    if pts["video"] and pts["audio"]:
        drift = (min(pts["audio"]) - min(pts["video"])) % PTS_WRAP
        if drift > PTS_WRAP // 2:
            drift -= PTS_WRAP
        out["drift_ms"] = round(drift / 90.0, 1)
    return out
