"""HTTP API checks that don't write to the database."""

import re

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    """A signed-in client (every route requires login)."""
    import server
    from tests.conftest import TEST_LOGIN
    server.auth.attempts.clear()
    c = TestClient(server.app)
    r = c.post("/login", data={**TEST_LOGIN, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return c


def test_index_stamps_assets_and_disables_cache(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
    assets = re.findall(r'/static/[\w.-]+\.(?:css|js)\?v=\d+', r.text)
    assert any("styles.css" in a for a in assets) and any("core.js" in a for a in assets) and any("boot.js" in a for a in assets)
    for asset in assets:
        assert client.get(asset).status_code == 200


def test_links_validation_errors_point_at_row_and_field(client):
    r = client.post("/api/links", json={"dry_run": True, "links": [
        {"channel": "ok", "label": "MainInput", "url": "https://a.example.test/x/index.m3u8"},
        {"channel": "", "label": "Bad Label", "url": "rtmp://a.example.test/live"},
    ]})
    assert r.status_code == 422
    errors = {(e["row"], e["field"]) for e in r.json()["errors"]}
    assert errors == {(1, "channel"), (1, "label"), (1, "url")}


def test_links_preview_groups_by_domain_without_saving(client, monkeypatch):
    import server
    monkeypatch.setattr(server, "upsert_nodes", lambda *a, **k: pytest.fail("dry run must not write"))
    r = client.post("/api/links", json={"dry_run": True, "links": [
        {"channel": "c1", "label": "MainInput", "url": "https://a.example.test/c1/index.m3u8"},
        {"channel": "c2", "label": "BackupLink", "url": "https://a.example.test/c2/index.m3u8"},
        {"channel": "", "label": "", "url": ""},  # blank rows are ignored
    ]})
    assert r.status_code == 200
    body = r.json()
    assert body["saved"] is False
    [node] = body["nodes"]
    assert node["domain"] == "a.example.test"
    assert node["labels"] == ["BackupLink", "MainInput"] and node["channels"] == ["c1", "c2"]


class TestScheduler:
    @pytest.fixture(autouse=True)
    def isolated(self, monkeypatch, tmp_path):
        import server
        monkeypatch.setattr(server, "SCHEDULER_FILE", tmp_path / "scheduler.json")
        monkeypatch.setattr(server.scheduler, "enabled", True)
        monkeypatch.setattr(server.scheduler, "interval", 30)
        self.server = server

    def test_defaults_on_every_30_seconds(self, client):
        state = client.get("/api/scheduler").json()
        assert state["enabled"] is True and state["interval"] == 30 and state["next_run_at"] is not None

    def test_stop_and_restart(self, client):
        stopped = client.post("/api/scheduler", json={"enabled": False}).json()
        assert stopped["enabled"] is False and stopped["next_run_at"] is None
        started = client.post("/api/scheduler", json={"enabled": True}).json()
        assert started["enabled"] is True and started["next_run_at"] <= started["server_time"] + 2  # pings right away

    def test_interval_change_is_saved(self, client):
        import json
        assert client.post("/api/scheduler", json={"interval": 45}).json()["interval"] == 45
        assert json.loads(self.server.SCHEDULER_FILE.read_text()) == {"enabled": True, "interval": 45}

    @pytest.mark.parametrize("interval", [0, 9, 3601])
    def test_interval_bounds(self, client, interval):
        assert client.post("/api/scheduler", json={"interval": interval}).status_code == 422


    def test_unknown_channel_is_404(self, client, monkeypatch):
        import server
        monkeypatch.setattr(server.driver, "execute_query", lambda *a, **k: type("R", (), {"records": [{"c": 0}]})())
        assert client.post("/api/run", json={"final": "nope.pytest.invalid/x"}).status_code == 404

    def test_busy_is_409(self, client):
        import server
        server.run_lock.acquire()
        try:
            assert client.post("/api/run", json={}).status_code == 409
        finally:
            server.run_lock.release()

    def test_channel_test_runs_only_that_channel(self, client, monkeypatch):
        import server
        started = {}
        monkeypatch.setattr(server.driver, "execute_query", lambda *a, **k: type("R", (), {"records": [{"c": 1}]})())
        monkeypatch.setattr(server, "start_run", lambda finals=None, source="manual": started.update(finals=finals, source=source) or "r1")
        r = client.post("/api/run", json={"final": "gtc.ottlive.co.in/gtcnews"})
        assert r.status_code == 202
        assert started == {"finals": ["gtc.ottlive.co.in/gtcnews"], "source": "channel"}

    def test_granular_run_lock_concurrency(self):
        from server import GranularRunLock
        lock = GranularRunLock()
        # Acquire channel 1
        assert lock.acquire(["channel-1"], blocking=False) is True
        # Channel 2 can acquire concurrently without blocking!
        assert lock.acquire(["channel-2"], blocking=False) is True
        # Channel 1 duplicate run is rejected
        assert lock.acquire(["channel-1"], blocking=False) is False
        # Full cycle is blocked while channel runs are in progress
        assert lock.acquire(None, blocking=False) is False
        # Release channel 1
        lock.release(["channel-1"])
        assert lock.acquire(["channel-1"], blocking=False) is True
        # Clean up
        lock.release(["channel-1"])
        lock.release(["channel-2"])
        # Global cycle acquires cleanly
        assert lock.acquire(None, blocking=False) is True
        assert lock.acquire(["channel-1"], blocking=False) is False
        lock.release(None)


def test_topology_rejects_unknown_relationship_type(client, monkeypatch):
    import server
    monkeypatch.setattr(server, "replace_topology", lambda *a, **k: pytest.fail("invalid body must not reach Neo4j"))
    r = client.put("/api/topology", json={"edges": [{"source": "a", "type": "CONNECTS", "target": "b"}]})
    assert r.status_code == 422


def test_import_excel_dry_run(client):
    import io
    import pandas as pd
    df = pd.DataFrame([
        {
            "Channel": "ch_import",
            "Main Link": "https://m.example.com/live.m3u8",
            "Backup Link": "https://b.example.com/live.m3u8",
            "Transcoding Link": "https://t.example.com/live.m3u8",
            "Final Link": "https://f.example.com/ch_import/live.m3u8",
        }
    ])
    buf = io.BytesIO()
    df.to_excel(buf, sheet_name="Channels", index=False)

    r = client.post(
        "/api/import/excel?dry_run=true",
        content=buf.getvalue(),
        headers={"Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["saved"] is False
    assert data["channels"] == ["ch_import"]
    assert data["link_count"] == 4
    assert len(data["relationships"]) > 0



# --- Embeddings & Log Clearance ---

def test_embeddings_sync_endpoint_runs_the_shared_job(client, monkeypatch):
    import server
    calls = []
    monkeypatch.setattr(server, "sync_all_embeddings", lambda driver, clear_logs: calls.append(clear_logs) or {
        "servers_updated": 3, "spiders_updated": 2, "logs_cleared": 1, "entries_archived": 5, "clear_logs": clear_logs})
    monkeypatch.setattr(server.scheduler, "record_embedding_sync", lambda result=None: calls.append(("recorded", result["source"])))
    r = client.post("/api/embeddings/sync?clear_logs=true")
    assert r.status_code == 200 and r.json()["servers_updated"] == 3 and r.json()["source"] == "manual"
    assert calls == [True, ("recorded", "manual")]
    assert client.post("/api/embeddings/sync?clear_logs=false").json()["clear_logs"] is False

    server.embedding_lock.acquire()  # a (cron) run in progress
    try:
        assert client.post("/api/embeddings/sync").status_code == 409
    finally:
        server.embedding_lock.release()


def test_embedding_cron_waits_a_week_on_first_start_and_remembers_runs(tmp_path, monkeypatch):
    import json
    import time
    import server
    monkeypatch.setattr(server, "EMBEDDINGS_CRON_FILE", tmp_path / "embeddings_cron.json")
    monkeypatch.setattr(server, "SCHEDULER_FILE", tmp_path / "scheduler.json")
    first = server.Scheduler()
    assert abs(first.last_embedding_sync - time.time()) < 5  # not 0: no clearance on every fresh start
    assert first.state()["next_embedding_sync_at"] == pytest.approx(first.last_embedding_sync + server.WEEK_S)
    first.record_embedding_sync({"servers_updated": 1})
    saved = json.loads((tmp_path / "embeddings_cron.json").read_text())
    assert saved["last_result"] == {"servers_updated": 1}
    again = server.Scheduler()  # a restart keeps the clock
    assert again.last_embedding_sync == saved["last_sync"] and again.last_embedding_result == {"servers_updated": 1}


def test_notifications_endpoints(client, tmp_path, monkeypatch):
    import routes.notifications as notif_mod
    monkeypatch.setattr(notif_mod, "NOTIF_FILE", str(tmp_path / "test_notif.json"))
    
    # 1. GET returns clean empty channels initially with zero samples
    r = client.get("/api/notifications")
    assert r.status_code == 200
    data = r.json()
    assert data["counts"]["total"] == 0
    assert data["counts"]["email"] == 0
    assert data["counts"]["whatsapp"] == 0
    assert data["counts"]["sms"] == 0
    assert "email" in data["channels"]
    assert "whatsapp" in data["channels"]
    assert "sms" in data["channels"]

    # 2. POST creates a new node
    new_node = {
        "channel": "email",
        "label": "Primary NOC Desk",
        "target": "noc@ottlive.in",
        "status": "active"
    }
    r = client.post("/api/notifications", json=new_node)
    assert r.status_code == 200
    created = r.json()["node"]
    assert created["target"] == "noc@ottlive.in"
    node_id = created["id"]

    # 3. POST test sends test alert to node
    r = client.post(f"/api/notifications/{node_id}/test")
    assert r.status_code == 200
    assert "Test alert dispatched" in r.json()["message"]

    # 4. POST test-all broadcasts to active nodes (1 active node)
    r = client.post("/api/notifications/test-all")
    assert r.status_code == 200
    assert r.json()["dispatched_count"] == 1

    # 5. PUT updates node status (mute / active)
    r = client.put(f"/api/notifications/{node_id}", json={"status": "muted"})
    assert r.status_code == 200
    assert r.json()["node"]["status"] == "muted"

    # 6. DELETE removes node
    r = client.delete(f"/api/notifications/{node_id}")
    assert r.status_code == 200
    assert r.json()["deleted_id"] == node_id

