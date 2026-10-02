"""Shared fixtures.

Unit tests need nothing external. Integration tests (marked `integration`) use the Neo4j
from .env, but only ever create nodes whose domain ends in TEST_SUFFIX, and delete them
(and their SpiderRuns) after every test — even when the test fails.
"""

import os
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import pytest
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Tests sign in with their own account, never the real one from .env (set before .env is read).
TEST_LOGIN = {"email": "tester@example.test", "password": "test-passcode"}
os.environ["APP_LOGIN_EMAIL"] = TEST_LOGIN["email"]
os.environ["APP_LOGIN_PASSWORD"] = TEST_LOGIN["password"]
os.environ["APP_SECRET_KEY"] = "test-secret-key"
os.environ["ALERT_CONSECUTIVE_THRESHOLD"] = "1"
os.environ["METRICS_DB"] = str(Path(__import__("tempfile").mkdtemp()) / "metrics.db")  # never the real history
load_dotenv(ROOT / ".env")

TEST_SUFFIX = ".pytest.invalid"


def domain(name: str) -> str:
    return name + TEST_SUFFIX


@pytest.fixture(autouse=True)
def fresh_health_caches():
    """The URL / ICMP / node health caches are process-wide: clear them so no test sees the previous test's result."""
    import health
    import spider
    from routes import notifications
    notifications._announcement_rotation.clear()  # wording rotates per server: start each test at the first line
    health.clear_health_caches()
    spider.clear_spider_caches()
    yield
    health.clear_health_caches()
    spider.clear_spider_caches()


@pytest.fixture(autouse=True)
def no_real_dns(monkeypatch):
    """DomainNode.server_ip does a DNS lookup; keep unit tests offline and deterministic."""
    import nodes
    monkeypatch.setattr(nodes.socket, "gethostbyname", lambda host: "203.0.113.10")


# The throwaway test Neo4j from docker-compose (`docker compose --profile test up -d neo4j-test`): local and in
# memory, so integration tests run in seconds instead of crossing the internet to Aura. TEST_NEO4J_URI overrides.
TEST_NEO4J = (os.environ.get("TEST_NEO4J_URI", "bolt://localhost:7688"), "neo4j",
              os.environ.get("TEST_NEO4J_PASSWORD", "test-password-only"))


def _connect(uri, user, password):
    from neo4j import GraphDatabase
    drv = GraphDatabase.driver(uri, auth=(user, password), connection_timeout=3)
    try:
        drv.verify_connectivity()
        return drv
    except Exception:
        drv.close()
        return None


# Everything the tests import (server.py, the spider, ...) reads NEO4J_URI: point it at the local test database
# when that is running, so no test talks to Aura.
_probe = _connect(*TEST_NEO4J)
if _probe is not None:
    _probe.close()
    os.environ.update(NEO4J_URI=TEST_NEO4J[0], NEO4J_USERNAME=TEST_NEO4J[1], NEO4J_PASSWORD=TEST_NEO4J[2])


@pytest.fixture(scope="session")
def driver():
    drv = _connect(*TEST_NEO4J)  # the local test database when it's running
    if drv is None and os.environ.get("NEO4J_URI"):  # otherwise the one in .env (Aura: slower)
        drv = _connect(os.environ["NEO4J_URI"], os.environ.get("NEO4J_USERNAME", "neo4j"), os.environ.get("NEO4J_PASSWORD", ""))
    if drv is None:
        pytest.skip("No Neo4j reachable (start one: docker compose --profile test up -d neo4j-test)")
    yield drv
    drv.close()


def cleanup(driver) -> None:
    driver.execute_query("MATCH (s:SpiderRun) WHERE s.finalLinkId ENDS WITH $t DETACH DELETE s", t=TEST_SUFFIX)
    driver.execute_query("MATCH (n:Domain) WHERE n.domain ENDS WITH $t DETACH DELETE n", t=TEST_SUFFIX)


@pytest.fixture
def graph(driver):
    """Build an isolated test topology: graph(nodes={name: [labels]}, edges=[(src, TYPE, dst)])."""
    cleanup(driver)  # leftovers from an interrupted run

    def build(nodes: Dict[str, List[str]], edges: Iterable[Tuple[str, str, str]] = ()):
        for name, labels in nodes.items():
            driver.execute_query(f"CREATE (n:Domain:{':'.join(labels)} {{domain: $d, url: []}})", d=domain(name))
        for src, rel, dst in edges:
            driver.execute_query(
                f"MATCH (a:Domain {{domain: $a}}), (b:Domain {{domain: $b}}) CREATE (a)-[:{rel}]->(b)",
                a=domain(src), b=domain(dst),
            )

    yield build
    cleanup(driver)
    left = driver.execute_query(
        "MATCH (n) WHERE n.domain ENDS WITH $t OR n.finalLinkId ENDS WITH $t RETURN count(n) AS c", t=TEST_SUFFIX
    ).records[0]["c"]
    assert left == 0, f"{left} test nodes were left behind"


@pytest.fixture(autouse=True)
def fresh_login_limiter():
    """The suite signs in far faster than a person can; don't let the login rate limit carry over between tests."""
    server = sys.modules.get("server")
    if server is not None:
        server.auth.attempts.clear()
    yield
