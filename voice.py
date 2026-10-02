"""Spoken alerts that sound like a real person in the control room, and that get better with use. No API calls.

1. What it says: the mood the moment calls for (calm heads-up → urgent → angry → furious as a problem lasts or keeps
   coming back; relieved when it's fixed), what is wrong in plain words, and what to do (the operator's own fix
   from last time, the usual root cause, then the playbook).
2. How it sounds: real Indian voices, stitched together from the phrase library (voice_library.py: phrases recorded
   earlier, trimmed and loudness-matched). Anything the library can't say is read by the browser's voice from the
   hand-written lines below.
3. Kept for training: every clip (audio + exact text + voice + mood) is stored in voice.db next to metrics.db and
   can be exported as a dataset (LJSpeech-style metadata + WAVs), together with the library phrases.
4. Learning: each play records the operator's 👍/👎 ("sounded human?") and how fast they reacted (opened the
   alerting node). Those rewards pick the voice per mood (Thompson sampling: mostly the best one, sometimes another
   to keep learning).
"""

import hashlib
import io
import json
import os
import random
import re
import sqlite3
import threading
import time
import wave
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional

try:  # lossless FLAC for stored clips (about half the size of WAV); WAV if it's missing
    import soundfile as sf
except Exception:  # pragma: no cover
    sf = None

MODEL = "bulbul:v3"  # what the library phrases were recorded with (kept in the dataset metadata)
SAMPLE_RATE = 24000
REPEAT_WINDOW_S = 1800  # alerts about the same thing within 30 min count as "again"
QUICK_ACK_S = 120  # the operator reacting within 2 min of hearing it counts as "it worked"
MAX_DB_MB = int(os.getenv("VOICE_DB_MAX_MB", "3000"))  # beyond this, the oldest unrated clips are dropped
PREFERRED_BONUS = 2.0  # the voice the operator picked in the Voice Studio starts with this head start

# Moods, and the voices recorded for each in the phrase library (the learner picks between them).
STYLES: Dict[str, dict] = {
    "calm": {"case": "WARNING", "voices": ["priya", "shubh"],
             "mood": "calm, polite heads-up, a little concerned; nothing is down yet"},
    "urgent": {"case": "CRITICAL", "voices": ["shubh", "rohan"],
               "mood": "urgent and commanding: viewers are affected right now, act immediately"},
    "angry": {"case": "AGGRESSIVE", "voices": ["aditya", "soham"],
              "mood": "annoyed and impatient: it is still broken or happened again and nobody fixed it"},
    "furious": {"case": "AGGRESSIVE", "voices": ["kabir", "aditya"],
                "mood": "really fed up and loud: this keeps happening for a long time"},
    "relieved": {"case": "RECOVERY", "voices": ["simran", "neha"],
                 "mood": "relieved and happy that it is fixed and back to normal"},
}

# Problems in the words people use, and the first steps to fix each (spoken after the alert).
PROBLEMS = [
    (r"404|manifest|playlist missing|not found", "playlist hi nahi mil rahi, 404 aa raha hai",
     "encoder ka push chal raha hai ki nahi dekho, phir origin pe playlist ka path check karo"),
    (r"stale|stopped updating|freeze|frozen|old segment|segment age", "video aage nahi badh raha, stream atak gayi hai",
     "pehle input feed aa rahi hai ki nahi dekho, phir encoder restart karo"),
    (r"unreachable|refused|timeout|connect", "server se connection hi nahi ho raha",
     "server ping karo aur nginx ya service restart karo, na chale toh backup pe switch karo"),
    (r"behind|latency|delay|slow", "stream live se peeche chal rahi hai, delay badh raha hai",
     "encoder ka CPU aur network dekho, zarurat ho toh bitrate thoda kam karo"),
    (r"glitch|skipped|discontinuit|drop", "video mein jhatke aa rahe hain, glitches dikh rahe hain",
     "input feed aur encoder ke dropped frames dekho, network loss bhi check karo"),
    (r"ad break|scte|stuck in the ad", "stream ad break mein atak gayi hai",
     "SCTE cue-in bhejo ya ad insertion reset karo"),
    (r"upstream|input", "upar se aane wali feed hi band hai",
     "pehle main input wala server theek karo, neeche wale apne aap aa jayenge"),
    (r"backup|failover", "main feed gayi, backup pe chal raha hai",
     "main feed wapas lao, backup pe zyada der mat chalao"),
]
RELIEVED_TAIL = "Jo fix kiya, Learning page pe likh dena... agli baar main seedha wahi bataunga."
DEFAULT_STEPS = "encoder aur input feed check karo, phir stream restart karo"

# Hand-written lines, read by the browser voice when the library has nothing for this mood yet. {name} the operator, {ch} channels, {srv} server,
# {problem} plain words, {mins} minutes, {hist} a history sentence (may be empty).
TEMPLATES: Dict[str, List[str]] = {
    "calm": [
        "{name} sir, ek chhoti si baat... {ch} pe {problem}. Abhi down nahi hai, bas ek baar dekh lijiye.",
        "Suniye {name} sir, {ch} thoda gadbad kar raha hai... {problem}. Time pe dekh lenge toh viewers ko pata bhi nahi chalega.",
        "{name} sir, heads up... {ch} pe {problem}. {hist} Please ek nazar daal lijiye.",
        "Sir, {ch} pe halki si dikkat hai... {problem}. Main monitor kar raha hoon, aap bhi ek baar check kar lo.",
    ],
    "urgent": [
        "{name}! {ch} band ho gaya hai... {problem}. Viewers ko abhi kuch nahi dikh raha, jaldi dekho!",
        "Arre {name}, suno! {ch} down hai. {problem}. {srv} turant check karo, abhi!",
        "{name} sir, emergency! {ch} off air hai... {problem}. {hist} Bina time waste kiye encoder dekho!",
        "{name}, jaldi! {ch} ki stream ruk gayi hai. {problem}. Pehle {srv} dekho, phir baaki sab.",
    ],
    "angry": [
        "{name}, tere ko dikhai nahi de raha? {ch} {mins} minute se down pada hai! {srv} sahi kar, abhi!",
        "Arre yaar {name}! Phir se {ch}... {problem}. {hist} Kitni baar bolna padega? Sahi kar isko!",
        "{name}, ye kya chal raha hai? {ch} abhi tak theek nahi hua! {mins} minute ho gaye... {srv} dekh jaldi!",
        "Bhai {name}, {ch} baar baar gir raha hai! {problem}. Koi dekh bhi raha hai ya nahi? Sahi kar!",
    ],
    "furious": [
        "{name}! Bas! {ch} {mins} minute se band hai! {srv}... abhi ke abhi theek kar!",
        "{name}, ye kya mazaak hai? {ch} phir gaya! {hist} Sab kaam chhod, pehle {srv} sahi kar!",
        "Suno {name}! {ch} down... {ch} down! {mins} minute! Abhi fix karo, abhi!",
    ],
    "relieved": [
        "{name} sir, good news... {ch} wapas aa gaya hai. Stream bilkul smooth chal rahi hai.",
        "Chalo, shukr hai! {ch} theek ho gaya, {name} sir. Sab normal hai ab.",
        "{name} sir, {ch} recover ho gaya hai... viewers ko sab theek dikh raha hai. Thanks!",
    ],
}

# Swearing on high alerts (angry: mild words; furious: the level's words). Set on the Learning page or
# VOICE_PROFANITY=off|mild|strong. Everyday office cussing only: abuse of someone's mother/sister, caste, religion,
# gender or anything sexual is never said (BLOCKED guards the lines).
PROFANITY_LEVELS = ("off", "mild", "strong")
SWEARS = {
    "mild": ["abe", "kya bakwaas hai", "dimaag kharab hai kya", "bewakoof", "dhakkan", "nalayak", "kamchor",
             "ullu ke patthe", "kuch nhi aata tere ko", "kya hai bhai"],
    "strong": ["saala", "kya ghanta", "bhaad mein gaya", "kya chutiyapa hai", "bakchodi band kar", "bloody hell"],
}
BLOCKED = re.compile(
    r"\b(maa|ma|mai|teri\s*maa|behen|bahen|bhen)\s*(ki|ke|ka|chod)|madar|behe?nchod|bhenchod|\b(bc|mc)\b|bhosd|randi|"
    r"\bgaand|\bgaandu|\blun?d\b|lau?da|lavda|chhakka|hijra|harami|aulaad|suar\s*ki|chamar|bhangi|\bchura\b|katua|mulla|"
    r"\bfuck(ing)?\b|\bbitch|whore|slut", re.I)

# Hand-written rough lines: (level, text). "mild" lines are used at mild and strong; "strong" only at strong.
ROUGH_TEMPLATES: Dict[str, List[tuple]] = {
    "angry": [
        ("mild", "Abe {name}! {ch} {mins} minute se down pada hai, kya bakwaas hai ye? {srv} sahi kar abhi!"),
        ("mild", "{name}, dimaag kharab hai kya? {ch} phir gaya... {problem}. Jaldi {srv} dekh!"),
        ("mild", "Arre dhakkan, {ch} band hai aur koi dekh hi nahi raha! {srv} theek kar, abhi!"),
        ("strong", "Saala phir se {ch}! {mins} minute ho gaye {name}... {srv} sahi kar, bakchodi band kar!"),
        ("strong", "{name}, kya ghanta monitor ho raha hai? {ch} {mins} minute se down hai! {srv} dekh abhi!"),
    ],
    "furious": [
        ("mild", "Abe {name}! Bas! {ch} {mins} minute se band hai! Nalayak log... {srv} abhi ke abhi theek kar!"),
        ("mild", "kuch nhi aata tere ko {name}! {ch} phir gaya! {hist} Ullu ke patthe, pehle {srv} sahi kar!"),
        ("strong", "Kya chutiyapa hai ye {name}? {ch} {mins} minute se down! {srv}... abhi theek kar, abhi!"),
        ("strong", "Bloody hell {name}! Saala {ch} phir band! Sab bhaad mein gaya... pehle {srv} sahi kar!"),
        ("strong", "{name}! Kya ghanta kaam ho raha hai? {ch} {mins} minute se off air hai! {srv} fix kar, abhi!"),
    ],
}

# The name each voice is shown under (instead of its recording speaker id), and moods shown as one person whatever
# the voice. Change on the Learning page or with POST /api/voice/settings; any other voice is shown as BOT_NAME.
VOICE_PEOPLE = {"rohan": "Sumit", "aditya": "Himanshu", "soham": "Deepanshu", "kabir": "Deepanshu", "shubh": "Manish"}
MOOD_PEOPLE = {"calm": "Om Prakash"}  # the polite tone
BOT_NAME = "Bot"

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, text TEXT NOT NULL, speaker TEXT NOT NULL,
    style TEXT NOT NULL, model TEXT, pace REAL, temperature REAL, sample_rate INTEGER, duration_s REAL,
    bytes INTEGER, sha TEXT UNIQUE, writer TEXT, audio BLOB, codec TEXT DEFAULT 'wav');
CREATE TABLE IF NOT EXISTS plays (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, clip_id INTEGER, subject TEXT, severity TEXT,
    style TEXT, speaker TEXT, writer TEXT, channels TEXT, heard_at REAL, acked_after_s REAL, rating INTEGER);
CREATE INDEX IF NOT EXISTS plays_subject ON plays (subject, ts);
CREATE INDEX IF NOT EXISTS plays_clip ON plays (clip_id);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""


def spoken_server(server: str) -> str:
    """How people say a server: 'xcode4.ottlive.co.in' → 'xcode4', 'cdn.ottlive.co.in/Rang Manch' → 'Rang Manch'."""
    s = (server or "").strip()
    if "/" in s:
        return s.rsplit("/", 1)[-1]
    return s.split(".")[0] if "." in s else s


def plain_problem(*texts: str) -> str:
    joined = " ".join(t for t in texts if t).lower()
    for pattern, words, _ in PROBLEMS:
        if re.search(pattern, joined):
            return words
    return "stream mein problem aa rahi hai"


def fix_steps(*texts: str, learned_fix: str = "", usual_root: str = "", server: str = "") -> str:
    """What to do, best evidence first: the fix the operator recorded last time for this server, then the server
    that was usually the real cause, then the playbook for this kind of problem. At most two steps (it's spoken)."""
    joined = " ".join(t for t in texts if t).lower()
    playbook = next((steps for pattern, _, steps in PROBLEMS if re.search(pattern, joined)), DEFAULT_STEPS)
    steps = []
    if learned_fix:
        steps.append(f"pichli baar {learned_fix.strip().rstrip('.')} se theek hua tha, wahi karo")
    root = spoken_server(usual_root)
    if root and root != spoken_server(server):
        steps.append(f"pehle {root} dekho, aksar asli wajah wahi hota hai")
    steps.append(playbook)
    return ". ".join(steps[:2])


def style_for(severity: str, repeats: int = 0, minutes: float = 0) -> str:
    """The mood: how bad it is, how long it has lasted, and how many times we already said it."""
    sev = (severity or "").upper()
    if sev == "RECOVERY":
        return "relieved"
    if sev == "WARNING":
        return "angry" if repeats >= 3 else "urgent" if repeats >= 1 else "calm"
    if sev == "AGGRESSIVE":
        return "furious" if repeats >= 3 or minutes >= 20 else "angry"
    if repeats >= 3 or minutes >= 15:  # CRITICAL
        return "furious"
    if repeats >= 1 or minutes >= 5:
        return "angry"
    return "urgent"


def wav_duration(audio: bytes) -> Optional[float]:
    try:
        with wave.open(io.BytesIO(audio)) as w:
            return round(w.getnframes() / float(w.getframerate()), 2)
    except Exception:
        return None


def encode(wav: bytes) -> tuple:
    """(blob, codec): FLAC when soundfile is available (lossless, ~half the size), else the WAV as is."""
    if sf is None:
        return wav, "wav"
    try:
        data, rate = sf.read(io.BytesIO(wav), dtype="int16")
        out = io.BytesIO()
        sf.write(out, data, rate, format="FLAC", subtype="PCM_16")
        return out.getvalue(), "flac"
    except Exception:
        return wav, "wav"


def to_wav(blob: bytes, codec: str) -> bytes:
    if codec != "flac" or sf is None:
        return blob
    data, rate = sf.read(io.BytesIO(blob), dtype="int16")
    out = io.BytesIO()
    sf.write(out, data, rate, format="WAV", subtype="PCM_16")
    return out.getvalue()


MEDIA_TYPES = {"flac": "audio/flac", "wav": "audio/wav"}


class VoiceStore:
    def __init__(self, path: str):
        self.path = str(path)
        self.lock = threading.Lock()
        with self._connect() as db:
            db.executescript(SCHEMA)
            if "codec" not in {r[1] for r in db.execute("PRAGMA table_info(clips)")}:
                db.execute("ALTER TABLE clips ADD COLUMN codec TEXT DEFAULT 'wav'")
            if "shown_as" not in {r[1] for r in db.execute("PRAGMA table_info(plays)")}:
                db.execute("ALTER TABLE plays ADD COLUMN shown_as TEXT")  # the voice's display name

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._connect() as db:
            r = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else default

    def set_setting(self, key: str, value: str) -> None:
        with self.lock, self._connect() as db:
            db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                       (key, value))

    # --- clips and plays ---
    def find_clip(self, sha: str) -> Optional[dict]:
        with self._connect() as db:
            r = db.execute("SELECT id, text, speaker, style, writer, duration_s FROM clips WHERE sha = ?", (sha,)).fetchone()
        return dict(r) if r else None

    def add_clip(self, text, speaker, style, pace, temperature, audio: bytes, sha: str, writer: str,
                 codec: Optional[str] = None, duration_s: Optional[float] = None) -> int:
        """audio: WAV (kept as FLAC when possible), or already encoded when `codec` is given."""
        blob, codec = (audio, codec) if codec else encode(audio)
        with self.lock, self._connect() as db:
            return db.execute(
                "INSERT INTO clips (ts, text, speaker, style, model, pace, temperature, sample_rate, duration_s, bytes, "
                "sha, writer, audio, codec) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), text, speaker, style, MODEL, pace, temperature, SAMPLE_RATE,
                 duration_s if duration_s is not None else wav_duration(audio),
                 len(blob), sha, writer, blob, codec)).lastrowid

    def audio(self, clip_id: int) -> Optional[tuple]:
        """(blob, codec) or None."""
        with self._connect() as db:
            r = db.execute("SELECT audio, COALESCE(codec, 'wav') AS codec FROM clips WHERE id = ?", (clip_id,)).fetchone()
        return (r["audio"], r["codec"]) if r and r["audio"] else None

    def compact(self) -> int:
        """Re-encodes clips stored as WAV into FLAC (lossless). Returns how many."""
        if sf is None:
            return 0
        with self._connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM clips WHERE audio IS NOT NULL AND COALESCE(codec, 'wav') = 'wav'")]
        done = 0
        for clip_id in ids:
            with self._connect() as db:
                wav = db.execute("SELECT audio FROM clips WHERE id = ?", (clip_id,)).fetchone()[0]
            blob, codec = encode(wav)
            if codec == "flac":
                with self.lock, self._connect() as db:
                    db.execute("UPDATE clips SET audio = ?, codec = 'flac', bytes = ? WHERE id = ?", (blob, len(blob), clip_id))
                done += 1
        if done:
            with self._connect() as db:
                db.execute("VACUUM")
        return done

    def add_play(self, clip_id, subject, severity, style, speaker, writer, channels, shown_as=None) -> int:
        with self.lock, self._connect() as db:
            return db.execute("INSERT INTO plays (ts, clip_id, subject, severity, style, speaker, writer, channels, shown_as) "
                              "VALUES (?,?,?,?,?,?,?,?,?)", (time.time(), clip_id, subject, severity, style, speaker,
                                                             writer, json.dumps(list(channels)), shown_as)).lastrowid

    def update_play(self, play_id: int, heard: bool = False, acked_after_s: Optional[float] = None,
                    rating: Optional[int] = None) -> bool:
        sets, args = [], []
        if heard:
            sets.append("heard_at = COALESCE(heard_at, ?)")
            args.append(time.time())
        if acked_after_s is not None:
            sets.append("acked_after_s = COALESCE(acked_after_s, ?)")
            args.append(max(0.0, float(acked_after_s)))
        if rating in (1, -1):
            sets.append("rating = ?")
            args.append(rating)
        if not sets:
            return False
        with self.lock, self._connect() as db:
            return db.execute(f"UPDATE plays SET {', '.join(sets)} WHERE id = ?", (*args, play_id)).rowcount > 0

    def repeats(self, subject: str, now: Optional[float] = None) -> int:
        """How many times this was already said in the last 30 min (since it last recovered)."""
        now = now or time.time()
        with self._connect() as db:
            rows = db.execute("SELECT severity FROM plays WHERE subject = ? AND ts > ? ORDER BY ts DESC",
                              (subject, now - REPEAT_WINDOW_S)).fetchall()
        n = 0
        for r in rows:
            if r["severity"] == "RECOVERY":
                break
            n += 1
        return n

    # --- learning ---
    @staticmethod
    def reward(rating: Optional[int], acked_after_s: Optional[float]) -> Optional[float]:
        """1 = it worked, 0 = it didn't; None = no signal. A rating beats the reaction time."""
        if rating in (1, -1):
            return 1.0 if rating > 0 else 0.0
        if acked_after_s is not None:
            return 0.8 if acked_after_s <= QUICK_ACK_S else 0.4
        return None

    def arm_stats(self, style: str, column: str = "speaker") -> Dict[str, dict]:
        """Per voice (or per writer) for a mood: summed rewards from every play with a signal."""
        assert column in ("speaker", "writer")
        with self._connect() as db:
            rows = db.execute(f"SELECT {column} AS arm, rating, acked_after_s FROM plays WHERE style = ?",
                              (style,)).fetchall()
        out: Dict[str, dict] = {}
        for r in rows:
            reward = self.reward(r["rating"], r["acked_after_s"])
            s = out.setdefault(r["arm"], {"plays": 0, "good": 0.0, "bad": 0.0, "up": 0, "down": 0})
            s["plays"] += 1
            s["up"] += r["rating"] == 1
            s["down"] += r["rating"] == -1
            if reward is not None:
                s["good"] += reward
                s["bad"] += 1 - reward
        return out

    def choose(self, arms: Iterable[str], stats: Dict[str, dict], preferred: Optional[str] = None,
               rng: Optional[random.Random] = None) -> str:
        """Thompson sampling: draw a plausible success rate for each arm from what we've seen, take the best."""
        rng = rng or random
        best, best_draw = None, -1.0
        for arm in arms:
            s = stats.get(arm, {})
            a = 1 + s.get("good", 0) + (PREFERRED_BONUS if arm == preferred else 0)
            b = 1 + s.get("bad", 0)
            draw = rng.betavariate(a, b)
            if draw > best_draw:
                best, best_draw = arm, draw
        return best

    def stats(self) -> dict:
        with self._connect() as db:
            c = db.execute("SELECT COUNT(*) AS n, COALESCE(SUM(duration_s), 0) AS secs, COALESCE(SUM(bytes), 0) AS b "
                           "FROM clips").fetchone()
            p = db.execute("SELECT COUNT(*) AS n, SUM(rating = 1) AS up, SUM(rating = -1) AS down, "
                           "SUM(acked_after_s IS NOT NULL) AS acked, AVG(acked_after_s) AS avg_ack FROM plays").fetchone()
            recent = db.execute("SELECT p.id, p.ts, p.style, p.speaker, p.severity, p.rating, p.acked_after_s, c.text, "
                                "c.duration_s, c.id AS clip_id, p.writer, p.shown_as AS name FROM plays p LEFT JOIN clips c ON c.id = p.clip_id "
                                "ORDER BY p.id DESC LIMIT 25").fetchall()
        styles = {}
        for style, cfg in STYLES.items():
            arms = self.arm_stats(style)
            styles[style] = {"mood": cfg["mood"],
                             "voices": [{"speaker": v, **arms.get(v, {"plays": 0, "good": 0, "bad": 0, "up": 0,
                                                                       "down": 0}),
                                         "score": round((1 + arms.get(v, {}).get("good", 0)) /
                                                        (2 + arms.get(v, {}).get("good", 0) + arms.get(v, {}).get("bad", 0)), 2)}
                                        for v in cfg["voices"]]}
        return {"clips": c["n"], "audioMinutes": round(c["secs"] / 60, 1), "megabytes": round(c["b"] / 1e6, 1),
                "plays": p["n"], "up": p["up"] or 0, "down": p["down"] or 0, "acked": p["acked"] or 0,
                "avgAckS": round(p["avg_ack"], 1) if p["avg_ack"] is not None else None,
                "styles": styles, "recent": [dict(r) for r in recent]}

    def export(self, out, min_rating: int = 0) -> int:
        """A training dataset as a zip: wavs/<id>.wav, metadata.csv (LJSpeech: id|text|text) and metadata.jsonl
        with speaker, mood, settings and the operator's verdicts. min_rating=1 keeps only clips rated 👍."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT c.*, COALESCE(SUM(p.rating = 1), 0) AS up, COALESCE(SUM(p.rating = -1), 0) AS down, "
                "COUNT(p.id) AS plays, AVG(p.acked_after_s) AS avg_ack FROM clips c LEFT JOIN plays p ON p.clip_id = c.id "
                "WHERE c.audio IS NOT NULL AND COALESCE(c.writer, '') != 'library' GROUP BY c.id ORDER BY c.id").fetchall()
            has_phrases = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'phrases'").fetchone()
            phrases = db.execute("SELECT * FROM phrases WHERE audio IS NOT NULL ORDER BY id").fetchall() if has_phrases else []
        n = 0
        with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:  # WAV doesn't compress; storing is faster
            csv, jsonl = [], []
            for r in rows:
                if min_rating > 0 and r["up"] <= r["down"]:
                    continue
                name = f"clip_{r['id']:06d}"
                z.writestr(f"wavs/{name}.wav", to_wav(r["audio"], r["codec"] or "wav"))
                text = r["text"].replace("|", " ").replace("\n", " ")
                csv.append(f"{name}|{text}|{text}")
                jsonl.append(json.dumps({"id": name, "file": f"wavs/{name}.wav", "text": r["text"], "speaker": r["speaker"],
                                         "style": r["style"], "model": r["model"], "pace": r["pace"],
                                         "temperature": r["temperature"], "sample_rate": r["sample_rate"],
                                         "duration_s": r["duration_s"], "writer": r["writer"], "plays": r["plays"],
                                         "up": r["up"], "down": r["down"], "avg_ack_s": r["avg_ack"], "ts": r["ts"]},
                                        ensure_ascii=False))
                n += 1
            for r in phrases:  # the library: clean single utterances, each with its exact text
                if min_rating > 0:
                    break
                name = f"phrase_{r['id']:06d}"
                z.writestr(f"wavs/{name}.wav", to_wav(r["audio"], "flac"))
                text = r["text"].replace("|", " ").replace("\n", " ")
                csv.append(f"{name}|{text}|{text}")
                jsonl.append(json.dumps({"id": name, "file": f"wavs/{name}.wav", "text": r["text"], "speaker": r["voice"],
                                         "style": r["mood"], "kind": r["kind"], "model": MODEL, "pace": r["pace"],
                                         "temperature": r["temperature"], "sample_rate": r["sample_rate"],
                                         "duration_s": r["duration_s"], "writer": "library-phrase", "ts": r["ts"]},
                                        ensure_ascii=False))
                n += 1
            z.writestr("metadata.csv", "\n".join(csv) + "\n")
            z.writestr("metadata.jsonl", "\n".join(jsonl) + "\n")
            z.writestr("README.txt", "Stream Graph voice alerts. wavs/: 24 kHz mono 16-bit PCM (phrases recorded with "
                                     "Bulbul v3 voices; clip_* are alerts as played, phrase_* single utterances).\n"
                                     "metadata.csv: LJSpeech format (id|text|normalized text). metadata.jsonl: speaker, "
                                     "style (mood), pace, temperature and operator ratings per clip.\n")
        return n

    def prune(self, max_mb: int = MAX_DB_MB) -> int:
        """Keeps the file under max_mb: drops the audio of the oldest clips nobody rated 👍."""
        with self._connect() as db:
            total = db.execute("SELECT COALESCE(SUM(bytes), 0) FROM clips WHERE audio IS NOT NULL").fetchone()[0]
            if total <= max_mb * 1e6:
                return 0
            rows = db.execute("SELECT c.id, c.bytes FROM clips c WHERE c.audio IS NOT NULL AND NOT EXISTS "
                              "(SELECT 1 FROM plays p WHERE p.clip_id = c.id AND p.rating = 1) ORDER BY c.id").fetchall()
        drop = []
        for r in rows:
            if total <= max_mb * 1e6 * 0.9:
                break
            drop.append(r["id"])
            total -= r["bytes"] or 0
        with self.lock, self._connect() as db:
            db.executemany("UPDATE clips SET audio = NULL WHERE id = ?", [(i,) for i in drop])
        return len(drop)


class Voice:
    """Picks the mood and the voice, says the alert from the phrase library (or hands the text to the browser voice),
    and records the play for learning. No network."""

    def __init__(self, store: VoiceStore, operator: Optional[str] = None, rng: Optional[random.Random] = None):
        from voice_library import VoiceLibrary  # here: it imports this module
        self.store = store
        self.operator = operator or os.getenv("VOICE_OPERATOR_NAME", "Anushrav")
        self.rng = rng or random.Random()
        self.library = VoiceLibrary(self)

    @property
    def profanity(self) -> str:
        level = self.store.setting("profanity", os.getenv("VOICE_PROFANITY", "strong"))
        return level if level in PROFANITY_LEVELS else "strong"

    def swears(self, style: str) -> List[str]:
        """The words allowed in this mood: none unless angry or furious; angry stays mild."""
        level = self.profanity
        if level == "off" or style not in ("angry", "furious"):
            return []
        return SWEARS["mild"] + (SWEARS["strong"] if level == "strong" and style == "furious" else [])

    def people(self) -> dict:
        """{"voices": {speaker: name}, "moods": {style: name}} (saved settings over the defaults)."""
        try:
            saved = json.loads(self.store.setting("people", "") or "{}")
        except ValueError:
            saved = {}
        return {"voices": {**VOICE_PEOPLE, **(saved.get("voices") or {})}, "moods": {**MOOD_PEOPLE, **(saved.get("moods") or {})}}

    def display_name(self, style: str, speaker: Optional[str]) -> str:
        """Who is shown as speaking: the polite tone's person whatever the voice; else the voice's name; else Bot."""
        p = self.people()
        return p["moods"].get(style) or p["voices"].get(speaker or "") or BOT_NAME

    def addressee(self, style: str, speaker: Optional[str]) -> str:
        """Who the line talks to: always the operator."""
        return self.operator

    def facts(self, event: dict, style: str, repeats: int, speaker: Optional[str] = None) -> dict:
        channels = [c for c in event.get("channels") or [] if c][:3]
        ch = " aur ".join([", ".join(channels[:-1]), channels[-1]]) if len(channels) > 1 else (channels[0] if channels else "channel")
        return {"name": self.addressee(style, speaker), "ch": ch, "srv": spoken_server(event.get("server") or "") or ch,
                "problem": event.get("problem") or plain_problem(event.get("title", ""), event.get("detail", "")),
                "mins": max(1, int(round(event.get("minutes") or 0))) if event.get("minutes") else max(1, repeats * 2),
                "hist": (event.get("history") or "").strip(), "repeats": repeats, "style": style,
                "fix": fix_steps(event.get("title", ""), event.get("detail", ""), event.get("problem", ""),
                                 learned_fix=event.get("learned_fix") or "", usual_root=event.get("usual_root") or "",
                                 server=event.get("server") or "")}

    def template_line(self, style: str, f: dict) -> tuple:
        """A hand-written line (rough ones at the chosen swear level) plus the steps, for the browser voice."""
        level = self.profanity
        rough = []
        if level != "off" and style in ROUGH_TEMPLATES:
            rough = [t for lv, t in ROUGH_TEMPLATES[style] if lv == "mild" or (level == "strong" and style == "furious")]
        options = rough or TEMPLATES[style]
        ids = [f"template:{'rough:' + level + ':' if rough else ''}{style}:{i}" for i in range(len(options))]
        pick = self.store.choose(ids, self.store.arm_stats(style, "writer"), rng=self.rng)
        text = options[ids.index(pick)].format(**f) + " " + (
            RELIEVED_TAIL if style == "relieved" else f"Ye karo... {f['fix']}.")
        return re.sub(r"\s+", " ", text.replace(" .", ".")).strip(), pick

    def alert(self, event: dict) -> dict:
        """event: severity (CRITICAL/WARNING/AGGRESSIVE/RECOVERY), channels, server, title/detail or problem,
        minutes, learned_fix, usual_root, subject. Returns what to play (audio_url None → the browser reads `text`)."""
        severity = (event.get("severity") or "CRITICAL").upper()
        subject = event.get("subject") or ",".join(sorted(event.get("channels") or [])) or event.get("server") or "?"
        repeats = 0 if severity == "RECOVERY" else self.store.repeats(subject)
        style = event.get("style") if event.get("style") in STYLES else style_for(severity, repeats, event.get("minutes") or 0)
        speaker = self.library.pick_voice(style)
        audio, text, missing, seconds = self.library.compose(event, style, speaker, repeats, self.rng)
        clip_id, cached, writer = None, False, "library"
        if audio:
            sha = hashlib.sha256(f"library|{speaker}|{style}|{text}".encode()).hexdigest()
            clip = self.store.find_clip(sha)
            cached = bool(clip)
            clip_id = clip["id"] if clip else self.store.add_clip(text, speaker, style, None, None, audio, sha, writer,
                                                                  codec="flac", duration_s=seconds)
        else:  # nothing recorded for this mood: the browser voice reads a hand-written line
            text, writer = self.template_line(style, self.facts(event, style, repeats, speaker))
        shown = self.display_name(style, speaker)
        play_id = self.store.add_play(clip_id, subject, severity, style, speaker, writer, event.get("channels") or [], shown)
        return {"play_id": play_id, "clip_id": clip_id, "text": text, "speaker": speaker, "style": style, "name": shown,
                "repeats": repeats, "writer": writer, "cached": cached, "missing": missing,
                "audio_url": f"/api/voice/clips/{clip_id}" if clip_id else None}


_stores: Dict[str, VoiceStore] = {}


def voice_store(path: Optional[str] = None) -> VoiceStore:
    """voice.db next to METRICS_DB (its own file: audio is big, and the metrics file is backed up nightly)."""
    if not path:
        metrics = os.environ.get("METRICS_DB") or str(Path(__file__).parent / "metrics.db")
        path = os.environ.get("VOICE_DB") or str(Path(metrics).with_name("voice.db"))
    if path not in _stores:
        _stores[path] = VoiceStore(path)
    return _stores[path]
