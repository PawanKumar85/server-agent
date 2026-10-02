"""The voice library: phrases recorded earlier (one clean utterance each, trimmed and loudness-matched, stored in the
voice.db `phrases` table), stitched together at alert time. No network, a few milliseconds.

    opener (calls out the operator)  · channel sentence · "aur bhi channels" · duration · problem
    · usual root cause · "pehle <server> dekho" · fix steps (or the operator's own fix) · closer (swears, by level)

A piece that was never recorded is skipped (or replaced by a generic one: "Ek channel band hai!"); when nothing at
all is recorded for a mood, the browser voice reads a hand-written line instead (voice.py).
"""

import hashlib
import io
import re
from typing import Dict, List, Optional, Tuple

import numpy as np

import voice as V

GAP_S = {"calm": 0.24, "urgent": 0.13, "angry": 0.1, "furious": 0.07, "relieved": 0.22}  # breath between pieces
MINUTES = [2, 3, 5, 7, 10, 15, 20, 25, 30, 45, 60]  # said as "at least N minutes" (the step at or below)
# --- what each mood says (by swear level where it matters) ---
OPENERS = {
    "calm": {"off": ["{name} ji, ek baat suniye.", "{name} ji, zara dhyan dijiye.", "Suniye {name} ji."]},
    "urgent": {"off": ["{name}, suno!", "Arre {name}, jaldi suno!", "{name}, emergency hai!"]},
    "angry": {"off": ["{name}! Ye kya ho raha hai?", "Arre {name}, phir se!"],
              "mild": ["Abe {name}!", "{name}, kya bakwaas hai ye?", "{name}, dimaag kharab hai kya?"]},
    "furious": {"off": ["{name}! Bas!", "{name}! Ab bahut ho gaya!"],
                "mild": ["Abe {name}! Bas!", "What the hell {name}!"],
                "strong": ["Kya chutiyapa hai ye {name}?", "Bloody hell {name}!", "{name}, saala phir se!",
                           "{name}, kya ghanta kaam ho raha hai?"]},
    "relieved": {"off": ["{name}, good news!", "Chalo {name}, shukr hai!", "Arre {name}, sab theek ho gaya!"]},
}
GENERIC_OPENER = {"calm": "Suniye.", "urgent": "Suno!", "angry": "Arre yaar!", "furious": "Bas!", "relieved": "Good news!"}
CLOSERS = {
    "calm": {"off": ["Please ek baar dekh lijiye.", "Shukriya."]},
    "urgent": {"off": ["Jaldi karo!", "Abhi dekho, time mat waste karo!"]},
    "angry": {"off": ["Abhi theek karo!", "Jaldi karo!"],
              "mild": ["Kya bakwaas hai, jaldi karo!", "Dimaag lagao, abhi theek karo!"]},
    "furious": {"off": ["Abhi ke abhi!", "Abhi theek karo, abhi!"],
                "mild": ["Nalayak log, abhi theek karo!", "Damn it, abhi karo!"],
                "strong": ["Bakchodi band kar, abhi theek kar!", "Saala, abhi ke abhi theek kar!",
                           "Kya ghanta dekh rahe ho, jaldi karo!"]},
    "relieved": {"off": ["Jo fix kiya, Learning page pe likh dena.",
                         "Fix Learning page pe note kar dena, agli baar main seedha bataunga."]},
}
CHANNEL_DOWN = {"urgent": "{ch} band ho gaya hai!", "angry": "{ch} phir se down hai!", "furious": "{ch} abhi tak band hai!"}
CHANNEL_WARN = {"calm": "{ch} pe thodi dikkat aa rahi hai.", "urgent": "{ch} pe problem badh rahi hai!",
                "angry": "{ch} pe abhi tak dikkat hai!", "furious": "{ch} pe baar baar dikkat aa rahi hai!"}
CHANNEL_OK = "{ch} wapas aa gaya hai, stream smooth chal rahi hai."
GENERIC_CHANNEL = {"down": "Ek channel band hai!", "warn": "Ek channel pe dikkat aa rahi hai.", "ok": "Sab channels wapas aa gaye hain."}
MORE_CHANNELS = "Aur bhi channels pe asar hai."
DURATION = "{n} minute se zyada ho gaye!"
DURATION_LONG = "Ek ghante se zyada ho gaya!"
SERVER = {"calm": "Pehle {srv} dekh lijiye.", "default": "Pehle {srv} dekho."}
ROOT = "Aksar {root} hi asli wajah hota hai."
STEPS = {"calm": "Phir {steps}.", "default": "Phir {steps}."}
LEARNED = "Pichli baar {fix} se theek hua tha, wahi karo."


def _levels(level: str) -> List[str]:
    return {"off": ["off"], "mild": ["mild"], "strong": ["strong", "mild"]}.get(level, ["off"])


def variants(bank: Dict[str, Dict[str, List[str]]], mood: str, level: str) -> List[str]:
    """The lines for this mood at this swear level (angry never goes past mild); 'off' lines when none."""
    if mood == "angry" and level == "strong":
        level = "mild"
    lines = [t for lv in _levels(level) for t in bank[mood].get(lv, [])]
    return lines or bank[mood]["off"]


def phrase_key(voice: str, mood: str, text: str) -> str:
    return hashlib.sha256(f"{voice}|{mood}|{text}".encode()).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS phrases (
    id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, voice TEXT NOT NULL, mood TEXT NOT NULL, kind TEXT,
    text TEXT NOT NULL, pace REAL, temperature REAL, sample_rate INTEGER, duration_s REAL, audio BLOB, ts REAL);
CREATE INDEX IF NOT EXISTS phrases_voice ON phrases (voice, mood);
"""


FADE_S = 0.015  # each phrase fades in and out over 15 ms: no click where two recordings meet


def _faded(samples: np.ndarray, rate: int) -> np.ndarray:
    n = min(int(rate * FADE_S), len(samples) // 4)
    if n < 2:
        return samples
    out = samples.astype("float32")
    ramp = np.linspace(0.0, 1.0, n, dtype="float32")
    out[:n] *= ramp
    out[-n:] *= ramp[::-1]
    return out.astype("<i2")


class VoiceLibrary:
    def __init__(self, voice: "V.Voice"):
        self.voice = voice
        self.store = voice.store
        self._cache: Dict[str, np.ndarray] = {}
        with self.store._connect() as db:
            db.executescript(SCHEMA)

    def samples(self, voice: str, mood: str, text: str) -> Optional[np.ndarray]:
        key = phrase_key(voice, mood, text)
        if key not in self._cache:
            with self.store._connect() as db:
                r = db.execute("SELECT audio FROM phrases WHERE key = ?", (key,)).fetchone()
            if not (r and r[0]):
                return None
            import soundfile as sf
            self._cache[key] = sf.read(io.BytesIO(r[0]), dtype="int16")[0]
        return self._cache[key]

    def coverage(self, voice: str, mood: str) -> int:
        with self.store._connect() as db:
            return db.execute("SELECT COUNT(*) FROM phrases WHERE voice = ? AND mood = ? AND audio IS NOT NULL",
                              (voice, mood)).fetchone()[0]

    def plan(self, event: dict, mood: str, voice: str, repeats: int, rng) -> List[Tuple[str, str, Optional[str]]]:
        """The pieces to say, in order: (kind, text, fallback text)."""
        level = self.voice.profanity
        name = self.voice.addressee(mood, voice)
        severity = (event.get("severity") or "").upper()
        channels = [c for c in event.get("channels") or [] if c]
        ch = channels[0] if channels else ""
        state = "ok" if mood == "relieved" else ("down" if severity == "CRITICAL" else "warn")
        pieces = [("opener", rng.choice(variants(OPENERS, mood, level)).format(name=name), GENERIC_OPENER[mood])]
        if state == "ok":
            pieces.append(("channel", CHANNEL_OK.format(ch=ch) if ch else GENERIC_CHANNEL["ok"], GENERIC_CHANNEL["ok"]))
        else:
            bank = CHANNEL_DOWN if state == "down" and mood in CHANNEL_DOWN else CHANNEL_WARN
            pieces.append(("channel", bank[mood].format(ch=ch) if ch else GENERIC_CHANNEL[state], GENERIC_CHANNEL[state]))
            if len(channels) > 1:
                pieces.append(("more", MORE_CHANNELS, None))
            minutes = int(round(event.get("minutes") or 0))
            if mood in ("angry", "furious", "urgent") and minutes >= 2:
                n = max(m for m in MINUTES if m <= minutes)  # "at least" — never more than the truth
                pieces.append(("duration", DURATION_LONG if minutes > 60 else DURATION.format(n=n), None))
            problem = event.get("problem") or V.plain_problem(event.get("title", ""), event.get("detail", ""))
            pieces.append(("problem", f"{problem[0].upper()}{problem[1:]}.", None))
            root = V.spoken_server(event.get("usual_root") or "")
            srv = V.spoken_server(event.get("server") or "")
            if root and root != srv:
                pieces.append(("root", ROOT.format(root=root), None))
                srv = root
            if srv:
                pieces.append(("server", SERVER.get(mood, SERVER["default"]).format(srv=srv), None))
            fix = (event.get("learned_fix") or "").strip().rstrip(".")
            playbook = next((s for p, _, s in V.PROBLEMS if re.search(p, f"{event.get('title', '')} {event.get('detail', '')}".lower())),
                            V.DEFAULT_STEPS)
            steps = ("steps", STEPS.get(mood, STEPS["default"]).format(steps=playbook), None)
            pieces.append(("learned", LEARNED.format(fix=fix), steps[1]) if fix else steps)
        pieces.append(("closer", rng.choice(variants(CLOSERS, mood, level)), None))
        return pieces

    def compose(self, event: dict, mood: str, voice: str, repeats: int, rng) -> tuple:
        """Stitches the recorded pieces. Returns (FLAC bytes or None, the text said, pieces missing, seconds)."""
        import soundfile as sf
        chunks, said, missing = [], [], 0
        for kind, text, fallback in self.plan(event, mood, voice, repeats, rng):
            got = self.samples(voice, mood, text)
            if got is None and fallback:
                missing += 1
                text, got = fallback, self.samples(voice, mood, fallback)
            if got is None:
                missing += 1
                continue
            chunks.append(got)
            said.append(text)
        if not chunks:
            return None, "", missing, None
        rate = V.SAMPLE_RATE
        gap = np.zeros(int(rate * GAP_S[mood]), dtype="<i2")
        joined = np.concatenate([x for c in chunks for x in (_faded(c, rate), gap)][:-1])
        buf = io.BytesIO()
        sf.write(buf, joined, rate, format="FLAC", subtype="PCM_16")
        return buf.getvalue(), " ".join(said), missing, round(len(joined) / rate, 2)

    def pick_voice(self, mood: str) -> str:
        """The learner's pick among this mood's voices that have recorded phrases (any of them if none has)."""
        voices = V.STYLES[mood]["voices"]
        ready = [v for v in voices if self.coverage(v, mood) > 0] or voices
        return self.store.choose(ready, self.store.arm_stats(mood), rng=self.voice.rng)

    def status(self) -> dict:
        with self.store._connect() as db:
            size = db.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(audio)), 0), COALESCE(SUM(duration_s), 0) "
                              "FROM phrases WHERE audio IS NOT NULL").fetchone()
            rows = db.execute("SELECT mood, voice, COUNT(*) FROM phrases WHERE audio IS NOT NULL GROUP BY mood, voice").fetchall()
        counts = {(r[0], r[1]): r[2] for r in rows}
        voices = [{"mood": m, "voice": v, "phrases": counts.get((m, v), 0)} for m, st in V.STYLES.items() for v in st["voices"]]
        return {"phrases": size[0], "megabytes": round(size[1] / 1e6, 1), "minutes": round(size[2] / 60, 1),
                "voices": voices}
