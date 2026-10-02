"""Login: every route is protected; sessions are signed, expire, and can't be forged."""

import time

import pytest
from fastapi.testclient import TestClient

from auth import Auth, safe_next
from tests.conftest import TEST_LOGIN


@pytest.fixture
def anon():
    import server
    server.auth.attempts.clear()
    yield TestClient(server.app)
    server.auth.attempts.clear()  # don't leave the test client rate-limited for other test files


def login(client, email=TEST_LOGIN["email"], password=TEST_LOGIN["password"], next_path="/"):
    return client.post("/login", data={"email": email, "password": password, "next": next_path}, follow_redirects=False)


@pytest.mark.parametrize("path", ["/api/graph", "/api/spiders", "/api/scheduler", "/api/topology", "/api/export.xlsx", "/api/stream"])
def test_api_requires_login(anon, path):
    r = anon.get(path)
    assert r.status_code == 401 and r.json() == {"detail": "Login required"}


@pytest.mark.parametrize("method,path", [("post", "/api/run"), ("post", "/api/scheduler"), ("post", "/api/links"), ("put", "/api/topology")])
def test_writes_require_login(anon, method, path):
    assert getattr(anon, method)(path, json={}).status_code == 401


@pytest.mark.parametrize("path", ["/", "/static/core.js", "/static/styles.css", "/docs"])
def test_pages_and_static_redirect_to_login(anon, path):
    r = anon.get(path, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_login_page_and_health_are_public(anon):
    assert anon.get("/login").status_code == 200 and "Sign in" in anon.get("/login").text
    assert anon.get("/healthz").json() == {"ok": True}


def test_wrong_password_is_rejected(anon):
    r = login(anon, password="nope")
    assert r.status_code == 401 and "Wrong email or password" in r.text
    assert "sg_session" not in r.cookies


def test_wrong_email_is_rejected(anon):
    assert login(anon, email="someone@else.test").status_code == 401


def test_login_sets_cookie_and_returns_to_the_page(anon):
    r = login(anon, next_path="/api/scheduler")
    assert r.status_code == 303 and r.headers["location"] == "/api/scheduler"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert anon.get("/api/scheduler").status_code == 200


def test_email_is_case_insensitive(anon):
    assert login(anon, email=TEST_LOGIN["email"].upper()).status_code == 303


def test_logout_ends_the_session(anon):
    login(anon)
    assert anon.get("/api/scheduler").status_code == 200
    anon.get("/logout", follow_redirects=False)
    anon.cookies.clear()  # the browser drops the cookie the server deleted
    assert anon.get("/api/scheduler").status_code == 401


def test_forged_or_tampered_cookie_is_rejected(anon):
    import server
    good = server.auth.issue(TEST_LOGIN["email"])
    payload, signature = good.rsplit(".", 1)
    for bad in [good[:-2] + "00", payload + ".deadbeef", "garbage", Auth("x@y.z", "p", "other-key").issue(TEST_LOGIN["email"])]:
        anon.cookies.set("sg_session", bad)
        assert anon.get("/api/scheduler").status_code == 401


def test_expired_session_is_rejected(monkeypatch):
    a = Auth(TEST_LOGIN["email"], TEST_LOGIN["password"], "k")
    token = a.issue(TEST_LOGIN["email"])
    assert a.verify(token) == TEST_LOGIN["email"]
    monkeypatch.setattr(time, "time", lambda: 10 ** 12)
    assert a.verify(token) is None


def test_too_many_attempts_are_rate_limited(anon):
    for _ in range(10):
        login(anon, password="wrong")
    assert login(anon).status_code == 429  # even the right password waits


@pytest.mark.parametrize("target,expected", [
    ("/api/graph", "/api/graph"), ("//evil.example", "/"), ("https://evil.example", "/"), ("", "/"), (None, "/"), ("/\\evil", "/"),
])
def test_redirect_after_login_stays_on_this_site(target, expected):
    assert safe_next(target) == expected
