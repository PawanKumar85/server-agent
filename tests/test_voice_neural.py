"""The local neural voice: Hinglish is written so a Hindi voice reads it right, and it speaks whatever the recorded
library can't say completely."""

import io
import random
import wave

from hinglish_devanagari import to_speakable
from voice import Voice, VoiceStore


def test_hindi_words_become_devanagari_and_english_stays_english():
    out = to_speakable("Arre dhakkan, tnpnews band hai aur koi dekh hi nahi raha! cloud1 theek kar... encoder check karo.")
    assert out == "अरे ढक्कन, tnpnews बंद है और कोई देख ही नहीं रहा! cloud1 ठीक कर, encoder check करो."
    assert to_speakable("main feed wapas lao") == "main feed वापस लाओ"  # English "main", not "मैं"
    assert to_speakable("agli baar main seedha bataunga") == "अगली बार मैं सीधा बताऊँगा"
    assert to_speakable("kuch nhi aata tere ko Anushrav") == "कुछ नहीं आता तेरे को अनुश्रव"


class FakeNeural:
    def __init__(self):
        self.said = []

    @staticmethod
    def voice_for(mood):
        return {"calm": "hi-priyamvada"}.get(mood, "hi-rohan")

    def synthesize(self, text, mood):
        self.said.append((text, mood))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(22050)
            w.writeframes(b"\x00\x00" * 22050)
        return buf.getvalue()


def test_alerts_the_library_cant_say_use_the_neural_voice_with_the_real_names(tmp_path):
    v = Voice(VoiceStore(tmp_path / "voice.db"), rng=random.Random(1))
    v.neural = FakeNeural()
    out = v.alert({"severity": "AGGRESSIVE", "channels": ["tnpnews"], "server": "cloud1.ottlive.co.in", "minutes": 30})
    assert out["audio_url"] and out["speaker"] == "hi-rohan" and out["name"] == "Himanshu"
    assert "tnpnews" in out["text"] and v.neural.said[0][1] == out["style"]
    again = v.alert({"severity": "AGGRESSIVE", "channels": ["tnpnews"], "server": "cloud1.ottlive.co.in", "minutes": 30,
                     "subject": "x", "style": out["style"]})
    if again["text"] == out["text"]:
        assert again["cached"] and len(v.neural.said) == 1  # the same line is synthesized once
    calm = v.alert({"severity": "WARNING", "channels": ["sakshitv"], "subject": "c"})
    assert calm["speaker"] == "hi-priyamvada" and calm["name"] == "Manish"


def test_a_broken_neural_voice_falls_back_to_the_browser(tmp_path):
    v = Voice(VoiceStore(tmp_path / "voice.db"))

    class Broken(FakeNeural):
        def synthesize(self, text, mood):
            raise RuntimeError("onnx failed")
    v.neural = Broken()
    out = v.alert({"severity": "CRITICAL", "channels": ["gtcnews"]})
    assert out["audio_url"] is None and "gtcnews" in out["text"]
