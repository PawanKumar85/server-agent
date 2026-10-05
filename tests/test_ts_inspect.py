"""Audio / video inside a segment: the TS reader, and the glitch probe's audio/video checks."""

import httpx

import glitch
from ts_inspect import inspect

VIDEO_PID, AUDIO_PID, PMT_PID = 0x100, 0x101, 0x1000


def _packet(pid, payload, pusi=True):
    head = bytes([0x47, (0x40 if pusi else 0) | (pid >> 8), pid & 0xFF, 0x10])
    return (head + payload + b"\xff" * 184)[:188]


def _pat():
    sec = bytes([0x00, 0xB0, 13, 0, 1, 0xC1, 0, 0, 0, 1, 0xE0 | (PMT_PID >> 8), PMT_PID & 0xFF]) + b"\0\0\0\0"
    return _packet(0, b"\x00" + sec)


def _pmt(audio=True):
    streams = bytes([0x1B, 0xE0 | (VIDEO_PID >> 8), VIDEO_PID & 0xFF, 0xF0, 0])
    if audio:
        streams += bytes([0x0F, 0xE0 | (AUDIO_PID >> 8), AUDIO_PID & 0xFF, 0xF0, 0])
    length = 9 + len(streams) + 4
    sec = bytes([0x02, 0xB0, length, 0, 1, 0xC1, 0, 0, 0xE1, 0x00, 0xF0, 0]) + streams + b"\0\0\0\0"
    return _packet(PMT_PID, b"\x00" + sec)


def _pes(pid, pts):
    b = bytes([0x21 | ((pts >> 29) & 0x0E), (pts >> 22) & 0xFF, ((pts >> 14) & 0xFE) | 1, (pts >> 7) & 0xFF,
               ((pts << 1) & 0xFE) | 1])
    return _packet(pid, b"\x00\x00\x01" + bytes([0xE0 if pid == VIDEO_PID else 0xC0, 0, 0, 0x80, 0x80, 5]) + b)


def segment(audio=True, audio_shift_s=0.0, audio_cover=1.0):
    out = _pat() + _pmt(audio)
    for i in range(7):  # 6 s of video frames at 1 s steps
        out += _pes(VIDEO_PID, 900000 + i * 90000)
        if audio and i <= 6 * audio_cover:
            out += _pes(AUDIO_PID, 900000 + int(audio_shift_s * 90000) + i * 90000)
    return out


def test_the_reader_finds_tracks_lengths_and_drift():
    r = inspect(segment())
    assert r["ts"] and r["video"] and r["audio"] and r["video_s"] == 6.0 and r["drift_ms"] == 0
    assert inspect(segment(audio=False))["audio"] is False
    assert inspect(segment(audio_shift_s=1.2))["drift_ms"] == 1200
    assert inspect(b"not a ts file")["ts"] is False


def probe_with(segments):
    """A probe whose newest segment is each of `segments` in turn."""
    media = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:{seq}\n#EXTINF:6,\ns{seq}.ts\n"
    state = {"n": 0}

    def handler(request):
        if request.url.path.endswith(".ts"):
            return httpx.Response(200, content=segments[min(state["n"] - 1, len(segments) - 1)])
        state["n"] += 1
        return httpx.Response(200, text=media.format(seq=100 + state["n"]))
    return glitch.GlitchProbe(":memory:", client=httpx.Client(transport=httpx.MockTransport(handler)))


def kinds(result):
    return {g["kind"] for g in result["glitches"]}


def test_audio_loss_is_reported_after_two_segments_in_a_row(tmp_path):
    p = probe_with([segment(), segment(audio=False), segment(audio=False)])
    url = "https://final.example.test/ch/index.m3u8"
    assert "AUDIO_MISSING" not in kinds(p.probe(url, "final", "ch", store=False))
    assert "AUDIO_MISSING" not in kinds(p.probe(url, "final", "ch", store=False))  # once: could be a splice
    assert "AUDIO_MISSING" in kinds(p.probe(url, "final", "ch", store=False))


def test_a_stream_without_audio_from_the_start_is_not_an_alarm_and_desync_is(tmp_path):
    silent = probe_with([segment(audio=False)] * 3)
    url = "https://final.example.test/ch/index.m3u8"
    assert not any("AUDIO" in k for _ in range(3) for k in kinds(silent.probe(url, "final", "ch", store=False)))
    drift = probe_with([segment(audio_shift_s=1.5)] * 3)
    found = set()
    for _ in range(3):
        found |= kinds(drift.probe(url, "final", "ch", store=False))
    assert "AV_DESYNC" in found
