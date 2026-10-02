"""The phrase library: alerts stitched from recorded phrases in milliseconds, with no network."""

import io
import random
import sqlite3

import numpy as np
import soundfile as sf

import voice_library as VL
from voice import Voice, VoiceStore

EVENT = {"severity": "CRITICAL", "channels": ["Rang Manch", "gtcnews"], "server": "xcode4.ottlive.co.in",
         "title": "Stream stopped updating", "minutes": 19, "usual_root": "ingest1.ottlive.co.in",
         "learned_fix": "restarted nginx", "subject": "x"}


def flac(seconds=0.5, rate=24000):
    t = np.arange(int(rate * seconds)) / rate
    buf = io.BytesIO()
    sf.write(buf, (0.1 * np.sin(2 * np.pi * 200 * t) * 32767).astype("<i2"), rate, format="FLAC", subtype="PCM_16")
    return buf.getvalue()


def record(v, voice, mood, texts):
    with sqlite3.connect(v.store.path) as db:
        db.executemany("INSERT OR REPLACE INTO phrases (key, voice, mood, kind, text, sample_rate, duration_s, audio, ts) "
                       "VALUES (?,?,?,?,?,24000,0.5,?,0)", [(VL.phrase_key(voice, mood, t), voice, mood, "x", t, flac()) for t in texts])


def test_an_alert_is_stitched_from_recorded_phrases(tmp_path):
    v = Voice(VoiceStore(tmp_path / "voice.db"), rng=random.Random(3))
    mood, voice = "furious", "kabir"  # 19 minutes down: furious
    for seed in range(30):  # every opener/closer variant the plan may pick
        record(v, voice, mood, [t for _, t, _ in v.library.plan(EVENT, mood, voice, 0, random.Random(seed))])
    v.store.update_play(v.store.add_play(None, "s", "CRITICAL", mood, voice, "library", []), rating=1)
    out = v.alert(EVENT)
    assert out["writer"] == "library" and out["missing"] == 0 and out["audio_url"] and out["speaker"] == voice
    t = out["text"]
    assert "Rang Manch abhi tak band hai!" in t and "Aur bhi channels" in t and "15 minute se zyada ho gaye!" in t
    assert "Aksar ingest1" in t and "Pehle ingest1 dekho." in t and "Pichli baar restarted nginx" in t
    blob, codec = v.store.audio(out["clip_id"])
    data, rate = sf.read(io.BytesIO(blob))
    assert codec == "flac" and len(data) / rate > 4  # nine pieces with breaths
    assert out["name"] == "Deepanshu"


def test_a_missing_piece_means_the_real_name_is_said_instead(tmp_path):
    v = Voice(VoiceStore(tmp_path / "voice.db"), rng=random.Random(1))
    record(v, "shubh", "urgent", [VL.GENERIC_OPENER["urgent"], VL.GENERIC_CHANNEL["down"]] +
           [o.format(name="Anushrav") for o in VL.OPENERS["urgent"]["off"]] + VL.CLOSERS["urgent"]["off"])
    v.neural = None  # no neural voice in this test
    out = v.alert({"severity": "CRITICAL", "channels": ["brand-new channel"], "subject": "n", "style": "urgent"})
    # The recordings can't say this channel's name: no generic "Ek channel band hai", the real name instead
    assert out["missing"] >= 1 and out["audio_url"] is None and "brand-new channel" in out["text"]


def test_status_counts_what_is_recorded(tmp_path):
    v = Voice(VoiceStore(tmp_path / "voice.db"))
    record(v, "priya", "calm", ["Suniye.", "Shukriya."])
    st = v.library.status()
    assert st["phrases"] == 2 and {"mood": "calm", "voice": "priya", "phrases": 2} in st["voices"]
