"""Spoken alerts: the mood rises as a problem drags on, what to do is said, the operator's ratings and reaction times
teach the voice choice, display names, swearing levels, storage and the training dataset. No API calls anywhere."""

import io
import json
import random
import wave
import zipfile

import voice
from voice import Voice, VoiceStore, fix_steps, plain_problem, spoken_server, style_for


def wav_bytes(seconds=1.0, rate=24000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


def make(tmp_path, seed=1):
    return Voice(VoiceStore(tmp_path / "voice.db"), operator="Anushrav", rng=random.Random(seed))


def test_the_mood_rises_as_a_problem_drags_on():
    assert style_for("WARNING") == "calm" and style_for("WARNING", repeats=1) == "urgent"
    assert style_for("CRITICAL") == "urgent" and style_for("CRITICAL", minutes=6) == "angry"
    assert style_for("CRITICAL", repeats=3) == "furious" and style_for("AGGRESSIVE") == "angry"
    assert style_for("RECOVERY", repeats=9) == "relieved"


def test_servers_and_problems_are_said_the_way_people_say_them():
    assert spoken_server("xcode4.ottlive.co.in") == "xcode4" and spoken_server("cdn.ottlive.co.in/Rang Manch") == "Rang Manch"
    assert "atak gayi" in plain_problem("Stream stopped updating", "")
    assert "404" in plain_problem("", "HTTP 404 manifest")


def test_alerts_say_what_to_do_best_evidence_first():
    assert "encoder restart" in fix_steps("Stream stopped updating")
    learned = fix_steps("Stream stopped updating", learned_fix="restarted nginx on xcode4", usual_root="ingest1.ottlive.co.in",
                        server="xcode4.ottlive.co.in")
    assert learned.startswith("pichli baar restarted nginx on xcode4 se theek hua tha")
    assert "pehle ingest1 dekho" in learned and learned.count(". ") == 1
    assert "ingest1" not in fix_steps("x", usual_root="ingest1.x", server="ingest1.y")


def test_with_nothing_recorded_the_browser_reads_a_natural_line_with_the_steps(tmp_path):
    v = make(tmp_path)
    out = v.alert({"severity": "WARNING", "channels": ["sakshitv"], "server": "cloud.ottlive.co.in", "title": "Falling behind"})
    assert out["audio_url"] is None and out["writer"].startswith("template:warn:calm:")
    assert "sakshitv" in out["text"] and "Anushrav" in out["text"] and "Ye karo..." in out["text"]
    again = v.alert({"severity": "CRITICAL", "channels": ["sakshitv"], "server": "cloud.ottlive.co.in"})
    assert again["repeats"] == 1 and again["style"] == "angry"  # still down: it gets angrier
    v.alert({"severity": "RECOVERY", "channels": ["sakshitv"]})
    assert v.alert({"severity": "CRITICAL", "channels": ["sakshitv"]})["repeats"] == 0
    rec = v.alert({"severity": "RECOVERY", "channels": ["sakshitv"], "subject": "r"})
    assert rec["text"].endswith(voice.RELIEVED_TAIL)


def test_high_alerts_swear_at_the_chosen_level_but_never_cross_the_line(tmp_path):
    v = make(tmp_path)
    all_swears = voice.SWEARS["mild"] + voice.SWEARS["strong"]
    f = v.facts({"channels": ["Rang Manch"], "server": "xcode4.x", "minutes": 20}, "furious", 3)
    v.store.set_setting("profanity", "strong")
    furious = {v.template_line("furious", f)[0] for _ in range(40)}
    assert any(w in line.lower() for line in furious for w in voice.SWEARS["strong"])
    assert not any(voice.BLOCKED.search(line) for line in furious)
    angry = {v.template_line("angry", f)[0] for _ in range(40)}  # angry stays mild even at strong
    assert not any(w in line.lower() for line in angry for w in voice.SWEARS["strong"])
    assert v.swears("urgent") == [] and "saala" in v.swears("furious") and "saala" not in v.swears("angry")
    v.store.set_setting("profanity", "off")
    calm = {v.template_line("furious", f)[0] for _ in range(40)}
    assert not any(w in line.lower() for line in calm for w in all_swears if len(w) > 3)
    assert voice.BLOCKED.search("teri maa ki") and not voice.BLOCKED.search("gandi stream hai, Rang Manch ka haal dekho")


def test_voices_are_shown_under_the_teams_names_and_the_polite_tone_as_om_prakash(tmp_path):
    v = make(tmp_path)
    for i in range(20):
        out = v.alert({"severity": "AGGRESSIVE", "channels": ["Rang Manch"], "minutes": 30, "subject": str(i)})
        assert out["name"] == voice.VOICE_PEOPLE.get(out["speaker"], "Bot") and "Anushrav" in out["text"]
    calm = v.alert({"severity": "WARNING", "channels": ["sakshitv"], "subject": "c"})
    assert calm["style"] == "calm" and calm["name"] == "Manish"
    assert v.display_name("urgent", "rohan") == "Himanshu" and v.display_name("urgent", "kabir") == "Deepanshu"
    assert v.display_name("urgent", "amit") == "Bot" and v.addressee("calm", "rohan") == "Anushrav"
    assert v.store.stats()["recent"][0]["name"] == "Manish"


def test_ratings_and_quick_reactions_teach_the_voice(tmp_path):
    v = make(tmp_path)
    for _ in range(6):  # rohan: 👍 every time; shubh: 👎 every time
        v.store.update_play(v.store.add_play(None, "s", "CRITICAL", "urgent", "rohan", "library", []), rating=1)
        v.store.update_play(v.store.add_play(None, "s", "CRITICAL", "urgent", "shubh", "library", []), rating=-1)
    picks = [v.store.choose(["shubh", "rohan"], v.store.arm_stats("urgent"), rng=random.Random(k)) for k in range(300)]
    assert picks.count("rohan") > 270
    assert VoiceStore.reward(None, 30) == 0.8 and VoiceStore.reward(None, 900) == 0.4
    assert VoiceStore.reward(-1, 5) == 0.0 and VoiceStore.reward(None, None) is None


def test_clips_are_kept_losslessly_exported_and_pruned(tmp_path):
    v = make(tmp_path)
    s = v.store
    good = s.add_clip("Line one, a band hai.", "rohan", "urgent", None, None, wav_bytes(2), "sha1", "library")
    s.add_clip("Line two, b band hai.", "shubh", "urgent", None, None, wav_bytes(1), "sha2", "library-old")
    blob, codec = s.audio(good)
    assert codec == "flac" and blob[:4] == b"fLaC"
    s.update_play(s.add_play(good, "x", "CRITICAL", "urgent", "rohan", "library", []), rating=1)
    buf = io.BytesIO()
    assert s.export(buf) == 1  # stitched library clips are left out; the other clip is in
    z = zipfile.ZipFile(buf)
    meta = [json.loads(line) for line in z.read("metadata.jsonl").decode().splitlines()]
    assert meta[0]["text"] == "Line two, b band hai." and z.read(meta[0]["file"])[:4] == b"RIFF"
    assert s.prune(max_mb=0) == 1 and s.audio(good) is not None  # the liked clip keeps its audio


def test_voice_routes(monkeypatch, tmp_path):
    import server
    from fastapi.testclient import TestClient
    from tests.conftest import TEST_LOGIN
    v = make(tmp_path)
    clip = v.store.add_clip("Suno, x band hai!", "rohan", "urgent", None, None, wav_bytes(1), "s", "library-old")
    monkeypatch.setattr(server, "voice", v)
    server.auth.attempts.clear()
    c = TestClient(server.app)
    assert c.post("/login", data={**TEST_LOGIN, "next": "/"}, follow_redirects=False).status_code == 303
    out = c.post("/api/voice/alert", json={"severity": "CRITICAL", "channels": ["Rang Manch"], "server": "xcode4.x"}).json()
    assert out["audio_url"] is None and "Rang Manch" in out["text"]  # nothing recorded: the browser reads it
    got = c.get(f"/api/voice/clips/{clip}")
    assert got.status_code == 200 and got.headers["content-type"] == "audio/flac"
    assert c.get(f"/api/voice/clips/{clip}.wav").status_code == 200 and c.get("/api/voice/clips/nope").status_code == 404
    assert c.post(f"/api/voice/plays/{out['play_id']}", json={"rating": 1}).json() == {"ok": True}
    stats = c.get("/api/voice/stats").json()
    assert stats["up"] == 1 and stats["library"]["phrases"] == 0 and "sarvam" not in json.dumps(stats).lower()
    assert c.get("/api/voice/dataset.zip").status_code == 200
    assert c.post("/api/voice/alert", json={"severity": "LOUD"}).status_code == 422
    assert c.post("/api/voice/settings", json={"profanity": "mild"}).json()["profanity"] == "mild"
    r = c.post("/api/voice/settings", json={"voices": {"neha": "Ravi"}, "moods": {"calm": ""}}).json()
    assert r["people"]["voices"]["neha"] == "Ravi" and v.display_name("calm", "priya") == "Bot"
    assert v.display_name("relieved", "neha") == "Ravi"
    assert c.post("/api/voice/settings", json={"voices": {"nobody": "x"}}).status_code == 422
    assert c.get("/api/sarvam/voices").status_code == 404 and c.post("/api/voice/library/build").status_code in (404, 405)


def test_the_route_brings_the_learned_fix_and_usual_root(monkeypatch):
    import server
    from routes import voice as voice_routes

    class L:
        def cases(self, node, limit):
            return [{"resolution": None, "root_cause": "ingest1.x"}, {"resolution": "restarted the encoder", "root_cause": "ingest1.x"},
                    {"resolution": "old fix", "root_cause": node}]
    monkeypatch.setattr(server, "learner", L())
    assert voice_routes.learned_advice("xcode4.x") == {"learned_fix": "restarted the encoder", "usual_root": "ingest1.x"}
    assert voice_routes.learned_advice(None) == {"learned_fix": "", "usual_root": ""}


def test_old_wav_clips_are_reencoded(tmp_path):
    import sqlite3
    store = VoiceStore(tmp_path / "v.db")
    with sqlite3.connect(store.path) as db:
        db.execute("INSERT INTO clips (ts, text, speaker, style, sha, audio, bytes, codec) VALUES (1, 't', 's', 'calm', 'x', ?, ?, 'wav')",
                   (wav_bytes(1.0), len(wav_bytes(1.0))))
    assert store.compact() == 1 and store.audio(1)[1] == "flac" and store.compact() == 0


def test_a_warning_is_never_announced_as_an_outage(tmp_path):
    v = make(tmp_path)
    v.neural = None
    outage_words = ("band", "down", "off air", "gaya", "atak")
    for i in range(20):  # a slow server that has stayed slow for 10 minutes, at every level
        out = v.alert({"severity": "AGGRESSIVE", "channels": ["tnpnews"], "server": "cloud1.ottlive.co.in",
                       "title": "HTTP latency 1260 is 9σ above normal", "minutes": 10, "subject": f"w{i}"})
        said = out["text"].lower().split("ye karo")[0]
        assert "latency" in said and not any(w in said for w in outage_words), out["text"]
    down = v.alert({"severity": "CRITICAL", "channels": ["tnpnews"], "title": "Stream stopped updating", "subject": "d"})
    assert any(w in down["text"].lower() for w in outage_words)  # a real outage still says so


def test_newer_alert_types_are_announced_as_what_they_are():
    from voice import fix_steps, plain_problem
    assert plain_problem("Backup feed down", "news's backup feed is failing; the main feed is still on air.") \
        == "sirf backup feed band hai, main feed se channel chal raha hai"
    assert "server ke andar chal rahi hai" in plain_problem("Not confirmed by the server",
                                                             "Server unreachable, seen from outside only.")
    # the agent's cause beats the symptom
    assert plain_problem("Video stopped updating", "Confirmed from inside the server: no encoder process is running.") \
        == "server pe encoder process hi nahi chal raha"
    assert plain_problem("Video stopped updating", "Confirmed from inside the server: the disk is 97% full.") \
        == "server ki disk bhar gayi hai"
    assert plain_problem("Intermittent stream stalls (flapping)", "") == "stream baar baar ruk ke chal rahi hai"
    assert "server mein hai" in plain_problem("lg.ottlive.co.in server down: all its channels (2)", "")
    assert "CDN" in fix_steps("Not confirmed by the server", "seen from outside only")
    assert plain_problem("Video stopped updating", "Newest piece is 40 s old") == "video aage nahi badh raha, stream atak gayi hai"


def test_announcements_are_general_unless_an_operator_is_named(tmp_path, monkeypatch):
    """No person is named by default: the lines address the team, and no recorded clip with a name is used."""
    monkeypatch.delenv("VOICE_OPERATOR_NAME", raising=False)
    v = Voice(VoiceStore(tmp_path / "voice.db"), rng=random.Random(1))
    assert v.operator == "" and v.addressee("urgent", "rohan") == "Team"
    for sev in ("CRITICAL", "WARNING", "RECOVERY"):
        out = v.alert({"severity": sev, "channels": ["tnpnews"], "server": "cloud.ottlive.co.in",
                       "title": "Video stopped updating"})
        assert "Anushrav" not in out["text"] and " sir" not in out["text"].lower(), out["text"]
    import voice_library
    plan = voice_library.VoiceLibrary(v).plan({"severity": "CRITICAL", "channels": ["tnpnews"]}, "urgent", "shubh", 0,
                                                random.Random(1))
    assert plan[0][1] == voice_library.GENERIC_OPENER["urgent"]  # "Suno!", not a clip saying someone's name


def test_the_browser_voice_gets_devanagari_hindi():
    import server
    from fastapi.testclient import TestClient
    from tests.conftest import TEST_LOGIN
    server.auth.attempts.clear()
    c = TestClient(server.app)
    c.post("/login", data={**TEST_LOGIN, "next": "/"}, follow_redirects=False)
    out = c.post("/api/voice/speakable", json={"text": "Final tnpnews down hai! Encoder service turant start karo!"}).json()
    assert out["text"] == "Final tnpnews down है! Encoder service तुरंत start करो!"  # Hindi words for a Hindi voice
