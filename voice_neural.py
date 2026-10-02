"""A natural-sounding Hindi voice that runs inside the app: Piper neural TTS (offline, no API calls, ~0.3 s a line).

Used for every alert the recorded phrase library can't say completely. The line is written in Devanagari for the Hindi
words first (hinglish_devanagari.py), otherwise the voice reads Hinglish with English rules and sounds robotic.

Voices (rhasspy/piper-voices, hi_IN, medium): rohan and pratham (male), priyamvada (female). Each mood has its voice
and pace: calm is slower and softer, furious is fast and less steady. Models live in PIPER_DIR (baked into the Docker
image); without them this module reports itself unavailable and the browser voice is the fallback.
"""

import io
import os
import threading
import wave
from pathlib import Path
from typing import Dict, Optional

from hinglish_devanagari import to_speakable

PIPER_DIR = Path(os.environ.get("PIPER_DIR", Path(__file__).parent / "models" / "piper"))
VOICES = {"hi-rohan": "hi_IN-rohan-medium", "hi-pratham": "hi_IN-pratham-medium",
          "hi-priyamvada": "hi_IN-priyamvada-medium"}
# mood -> (voice, length_scale: <1 faster, noise_scale: expressiveness, noise_w_scale: rhythm variation)
MOODS = {
    "calm": ("hi-priyamvada", 1.08, 0.6, 0.8),
    "urgent": ("hi-rohan", 0.92, 0.667, 0.8),
    "angry": ("hi-pratham", 0.88, 0.75, 0.9),
    "furious": ("hi-rohan", 0.82, 0.8, 0.95),
    "relieved": ("hi-priyamvada", 1.0, 0.7, 0.85),
}


class NeuralVoice:
    def __init__(self, directory: Path = PIPER_DIR):
        self.dir = Path(directory)
        self._voices: Dict[str, object] = {}
        self._lock = threading.Lock()

    def available(self) -> bool:
        try:
            import piper  # noqa: F401
        except Exception:
            return False
        return all((self.dir / f"{m}.onnx").exists() for m in VOICES.values())

    def _voice(self, voice_id: str):
        with self._lock:
            if voice_id not in self._voices:
                from piper import PiperVoice
                self._voices[voice_id] = PiperVoice.load(str(self.dir / f"{VOICES[voice_id]}.onnx"))
            return self._voices[voice_id]

    @staticmethod
    def voice_for(mood: str) -> str:
        return MOODS.get(mood, MOODS["urgent"])[0]

    def synthesize(self, text: str, mood: str) -> Optional[bytes]:
        """WAV bytes of the line in this mood's voice, or None if the voice isn't available."""
        if not text or not self.available():
            return None
        from piper import SynthesisConfig
        voice_id, length, noise, noise_w = MOODS.get(mood, MOODS["urgent"])
        cfg = SynthesisConfig(length_scale=length, noise_scale=noise, noise_w_scale=noise_w, normalize_audio=True)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            self._voice(voice_id).synthesize_wav(to_speakable(text), w, syn_config=cfg)
        return buf.getvalue()


neural_voice = NeuralVoice()
