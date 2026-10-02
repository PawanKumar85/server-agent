"""Tests for 3D heatmap generation with Seaborn."""

import pytest
from fastapi.testclient import TestClient
from heatmap import clean_node_name, generate_3d_heatmap_base64, generate_3d_heatmap_png


@pytest.fixture
def client():
    import server
    from tests.conftest import TEST_LOGIN
    server.auth.attempts.clear()
    c = TestClient(server.app)
    r = c.post("/login", data={**TEST_LOGIN, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return c


def test_clean_node_name():
    assert clean_node_name("cloud.ottlive.co.in") == "cloud"
    assert clean_node_name("gtc.ottlive.co.in/gtcnews") == "gtc/gtcnew"
    assert clean_node_name("stream.example.com") == "stream.example"
    assert clean_node_name("") == "unknown"


def test_generate_3d_heatmap_png_empty():
    png_bytes = generate_3d_heatmap_png([])
    assert isinstance(png_bytes, bytes)
    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")


def test_generate_3d_heatmap_png_with_down_nodes():
    nodes = [
        {"domain": "cloud.ottlive.co.in", "failedCount": 31, "consecutiveFailures": 0, "pingCount": 215, "status": "UP"},
        {"domain": "ingest1.ottlive.co.in", "failedCount": 15, "consecutiveFailures": 2, "pingCount": 201, "status": "DOWN"},
        {"domain": "gtc.ottlive.co.in/gtcnews", "failedCount": 0, "consecutiveFailures": 0, "pingCount": 102, "status": "UP"},
    ]
    png_bytes = generate_3d_heatmap_png(nodes, theme="dark")
    assert isinstance(png_bytes, bytes)
    assert len(png_bytes) > 1000
    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")


def test_generate_3d_heatmap_base64():
    nodes = [
        {"domain": "cloud.ottlive.co.in", "failedCount": 10, "consecutiveFailures": 1, "pingCount": 50, "status": "DOWN"},
    ]
    b64_str = generate_3d_heatmap_base64(nodes)
    assert b64_str.startswith("data:image/png;base64,")
    assert len(b64_str) > 500


def test_server_heatmap_endpoint(client, monkeypatch):
    import server
    # Mock fetch_graph to avoid external network calls during unit test
    fake_nodes = [
        {"id": "cloud.ottlive.co.in", "domain": "cloud.ottlive.co.in", "failedCount": 31, "consecutiveFailures": 0, "pingCount": 200, "status": "UP", "labels": ["MainInput"]},
        {"id": "ingest1.ottlive.co.in", "domain": "ingest1.ottlive.co.in", "failedCount": 15, "consecutiveFailures": 0, "pingCount": 200, "status": "UP", "labels": ["BackupLink"]},
    ]
    monkeypatch.setattr(server, "fetch_graph", lambda driver: {"nodes": fake_nodes, "edges": [], "spiders": []})

    r = client.get("/api/heatmap.png?theme=dark")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG\r\n\x1a\n")
