"""Per-channel "ignore alerts": stored, listed, and the voice stays silent for muted channels."""

from channel_mute import ChannelMutes


def test_mutes_default_off_and_toggle(tmp_path):
    m = ChannelMutes(tmp_path / "m.db")
    assert m.muted() == {} and not m.is_muted("tnpnews")
    m.set("tnpnews", True, "maintenance")
    assert m.is_muted("tnpnews") and m.muted()["tnpnews"]["note"] == "maintenance"
    assert m.all_muted(["tnpnews"]) and not m.all_muted(["tnpnews", "gtcnews"])  # a live channel still alerts
    m.set("tnpnews", False)
    assert m.muted() == {}


def test_routes_and_the_voice_guard(monkeypatch, tmp_path):
    import server
    from fastapi.testclient import TestClient
    from tests.conftest import TEST_LOGIN
    monkeypatch.setattr(server, "channel_mutes", ChannelMutes(tmp_path / "m.db"))
    spoken = []
    monkeypatch.setattr(server.voice, "alert", lambda event: spoken.append(event) or {"audio_url": None, "text": "x"})
    server.auth.attempts.clear()
    c = TestClient(server.app)
    c.post("/login", data={**TEST_LOGIN, "next": "/"}, follow_redirects=False)
    assert c.get("/api/channels/mutes").json() == {"muted": {}}
    assert c.put("/api/channels/Rang Manch/mute", json={"muted": True}).json()["muted"] is True
    assert "Rang Manch" in c.get("/api/channels/mutes").json()["muted"]
    r = c.post("/api/voice/alert", json={"severity": "CRITICAL", "channels": ["Rang Manch"]}).json()
    assert r["muted"] is True and spoken == []  # nothing said for an ignored channel
    c.post("/api/voice/alert", json={"severity": "CRITICAL", "channels": ["Rang Manch", "gtcnews"]})
    assert len(spoken) == 1  # gtcnews is live: the alert still goes out
    c.put("/api/channels/Rang Manch/mute", json={"muted": False})
    assert c.get("/api/channels/mutes").json() == {"muted": {}}


def test_stream_ignore_routes(monkeypatch, tmp_path):
    import server
    from channel_mute import StreamIgnores
    from fastapi.testclient import TestClient
    from tests.conftest import TEST_LOGIN
    monkeypatch.setattr(server, "stream_ignores", StreamIgnores(tmp_path / "m.db"))
    server.auth.attempts.clear()
    c = TestClient(server.app)
    assert c.put("/api/streams/ignore", json={"url": "https://x.example/a.m3u8"}).status_code == 401  # login first
    c.post("/login", data={**TEST_LOGIN, "next": "/"}, follow_redirects=False)
    url = "https://cloud.example/lokmatbackup/index.m3u8"
    assert c.put("/api/streams/ignore", json={"url": url, "ignored": True}).json()["ignored"] is True
    assert url in c.get("/api/streams/ignored").json()["ignored"]
    c.put("/api/streams/ignore", json={"url": url, "ignored": False})
    assert c.get("/api/streams/ignored").json() == {"ignored": {}}
