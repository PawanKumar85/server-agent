"""ChatBot: documents from the live data, FAISS retrieval, the prompt, and the streamed Ollama answer.
No network and no model download: the embedder is a tiny bag-of-words stand-in and Ollama is mocked."""

import json
import re
from types import SimpleNamespace as NS

import httpx
import numpy as np
import pytest

import chatbot
import graphrag
from chatbot import ChatBot, Snapshot, build_documents, ist


class WordEmbedder:
    """`.encode(texts)` like a SentenceTransformer: one dimension per known word, L2-normalised."""
    VOCAB = ["backup", "down", "latency", "scheduler", "ping", "ch1", "main", "server", "overview", "graph",
             "database", "mongodb", "relationships", "feeds"]

    def encode(self, texts):
        rows = []
        for text in texts:
            words = re.findall(r"\w+", text.lower())
            v = np.zeros(graphrag.RAG_DIM, dtype="float32")  # 384 dims like MiniLM; the first few are the vocabulary
            v[:len(self.VOCAB)] = [words.count(w) for w in self.VOCAB]
            v += 1e-3
            rows.append(v / np.linalg.norm(v))
        return np.stack(rows)


EDGES = [("main.example.test", "FEEDS", "final.example.test/ch1"), ("backup.example.test", "FEEDS", "final.example.test/ch1")]


class FakeDriver:
    """Answers the snapshot queries and GraphRAG's: stored ragEmbeddings, a cosine vector index, and the
    FEEDS/PRODUCES walks over EDGES."""

    def __init__(self, nodes, spiders=(), edges=EDGES):
        self.nodes, self.spiders, self.edges = nodes, list(spiders), list(edges)
        self.rag, self.writes, self.indexes = {}, 0, set()

    def execute_query(self, query, **params):
        records = []
        if "CREATE VECTOR INDEX" in query:
            self.indexes.add(query.split("INDEX")[1].split()[0])
        elif query.startswith("DROP INDEX") or "REMOVE n.ragEmbedding" in query:
            pass  # legacy clean-up
        elif "embeddingHash AS hash" in query:
            keys = [("Domain", p["domain"]) for p, _ in self.nodes] + [("SpiderRun", s["id"]) for s in self.spiders]
            records = [{"label": l, "key": k, "hash": (self.rag.get((l, k)) or (None, None))[1]} for l, k in keys]
        elif "SET n.embedding" in query:
            label = "Domain" if "Domain" in query else "SpiderRun"
            for row in params["rows"]:
                self.rag[(label, row["key"])] = (row["vec"], row["hash"])
                self.writes += 1
        elif "queryNodes" in query:
            label = "Domain" if params["index"] == graphrag.INDEXES["Domain"] else "SpiderRun"
            scored = [(k, float(np.dot(v, params["vec"]))) for (l, k), (v, _) in self.rag.items() if l == label]
            records = [{"key": k, "score": sc} for k, sc in sorted(scored, key=lambda x: -x[1])[:params["k"]]]
        elif "startNode(r)" in query:
            ids = set(params["ids"])
            records = [{"source": a, "type": t, "target": b} for a, t, b in self.edges if a in ids or b in ids]
        elif "collect(DISTINCT u.domain)" in query:
            records = [{"final": f, "upstream": [a for a, _, b in self.edges if b == f]} for f in params["finals"]]
        elif "RETURN n.domain AS d" in query:
            records = [{"d": p["domain"]} for p, _ in self.nodes]
        elif "SpiderRun" in query:
            records = [{"p": s} for s in self.spiders]
        else:
            records = [{"p": p, "roles": roles} for p, roles in self.nodes]
        return NS(records=records)


def node(domain, roles, links, health=None, **props):
    return ({"domain": domain, "status": "UP", "links": json.dumps(links),
             "urlHealth": json.dumps(health or {}), **props}, roles)


MAIN = "https://main.example.test/ch1/index.m3u8"
BACKUP = "https://backup.example.test/ch1/index.m3u8"
FINAL = "https://final.example.test/ch1/index.m3u8"
DRIVER = FakeDriver(
    [
        node("main.example.test", ["MainInput"], [{"url": MAIN, "role": "MainInput", "channel": "ch1"}],
             {MAIN: {"up": False, "detail": "HTTP 404", "lastDown": "2026-09-30T06:30:00.123456789+00:00"}},
             status="DOWN", lastError="HTTP 404", lastLatencyMs=321.0, server_ip="203.0.113.7"),
        node("backup.example.test", ["BackupLink"], [{"url": BACKUP, "role": "BackupLink", "channel": "ch1"}],
             {BACKUP: {"up": True, "detail": "live"}}),
        node("final.example.test/ch1", ["FinalLink"], [{"url": FINAL, "role": "FinalLink", "channel": "ch1"}],
             {FINAL: {"up": True, "detail": "live"}}),
    ],
    [{"id": "spider-final.example.test/ch1", "finalLinkId": "final.example.test/ch1", "status": "RUNNING", "currentNodeId": "final.example.test/ch1",
      "rca": json.dumps({"impact": "Main down; on backup", "failover": ["backup.example.test"]})}],
)
MONITOR = {"autoPing": True, "intervalSeconds": 30, "nextRun": "2026-09-30 18:30:00 IST", "now": "2026-09-30 18:29:40 IST",
           "lastRun": {"at": "2026-09-30 18:29:10 IST", "elapsed_ms": 900}}


@pytest.fixture(autouse=True)
def no_topology(monkeypatch):
    monkeypatch.setattr(chatbot, "current_topology", lambda driver: [
        NS(model_dump=lambda: {"source": "main.example.test", "type": "FEEDS", "target": "final.example.test/ch1"})])


def ollama(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def bot(handler=None):
    handler = handler or (lambda request: httpx.Response(500))
    return ChatBot(DRIVER, lambda: MONITOR, http=ollama(handler), embedder=WordEmbedder())


# --- helpers ---

def test_ist_converts_neo4j_utc_strings_and_epoch_seconds():
    assert ist("2026-09-30T06:30:00.123456789+00:00") == "2026-09-30 12:00:00 IST"
    assert ist("2026-09-30T18:45:00Z") == "2026-10-01 00:15:00 IST"
    assert ist(0) == "1970-01-01 05:30:00 IST"
    assert ist(None) is None and ist("") is None


# --- documents ---

def test_documents_describe_channels_servers_topology_and_scheduler():
    docs = {d["id"]: d for d in build_documents(Snapshot(DRIVER), MONITOR, [
        {"source": "main.example.test", "type": "FEEDS", "target": "final.example.test/ch1"}])}
    overview = docs["overview"]
    assert overview["down"] == [] and overview["onBackup"] == ["ch1"] and overview["channels"] == 1
    assert "Channels ON BACKUP right now: ch1" in overview["text"]

    channel = docs["channel:ch1"]["text"]
    assert "ON BACKUP" in channel and "Main server of ch1 is main.example.test" in channel
    assert f"{MAIN} is DOWN (HTTP 404). This Main URL last went down at 2026-09-30 12:00:00 IST" in channel
    assert "Backup server of ch1 is backup.example.test" in channel and "Impact: Main down; on backup" in channel

    server = docs["server:main.example.test"]["text"]
    assert "latency 321.0 ms" in server and "203.0.113.7" in server and "error: HTTP 404" in server
    assert "error" not in docs["server:backup.example.test"]["text"]  # old errors of healthy servers are left out
    assert "main.example.test FEEDS final.example.test/ch1" in docs["topology"]["text"]
    assert docs["scheduler"]["text"].startswith("Scheduler (auto ping). Auto ping is ON.")
    assert "every 30 seconds" in docs["scheduler"]["text"]


def test_scheduler_text_when_off_and_never_run():
    docs = {d["id"]: d for d in build_documents(Snapshot(DRIVER), {"autoPing": False, "now": "x"}, [])}
    text = docs["scheduler"]["text"]
    assert "Auto ping is OFF." in text and "next run" not in text and "last run" not in text


def test_graphrag_links_names_in_the_question_and_expands_the_graph():
    results, live = bot().retrieve("How is the ch1 channel?")
    got = {r["document"]["id"]: r["via"] for r in results}
    assert results[0]["document"]["id"] == "overview" and got["channel:ch1"] == "entity"
    # the channel's whole chain is in (found by the vector index or by walking FEEDS upstream from its Final)
    for server in ("final.example.test/ch1", "main.example.test", "backup.example.test"):
        assert got[f"server:{server}"] in ("vector", "graph")
    assert any(k.startswith("graph:edges") and v == "graph" for k, v in got.items())
    edges = next(r for r in results if r["document"]["id"].startswith("graph:edges"))["document"]["text"]
    assert "main.example.test FEEDS final.example.test/ch1" in edges
    assert live == {"channels": 1, "down": [], "onBackup": ["ch1"]}


def test_graphrag_short_server_names_and_their_channels():
    results, _ = bot().retrieve("Why is main failing?")
    got = {r["document"]["id"]: r["via"] for r in results}
    assert got["server:main.example.test"] == "entity" and got["channel:ch1"] == "graph"


def test_graphrag_vector_search_runs_in_neo4j_and_embeds_only_changed_profiles():
    driver = FakeDriver(DRIVER.nodes, DRIVER.spiders)
    b = ChatBot(driver, lambda: MONITOR, http=ollama(lambda r: httpx.Response(500)), embedder=WordEmbedder())
    results, _ = b.retrieve("which server is down")  # no names: seeds come from the vector index
    assert {"domain_embeddings", "spider_embeddings"} <= driver.indexes and driver.writes == 4  # 3 servers + 1 spider
    assert any(r["via"] == "vector" for r in results)
    b.retrieve("which server is down")
    assert driver.writes == 4  # profiles unchanged: nothing re-embedded


def test_graphrag_scheduler_keyword_and_score_floor(monkeypatch):
    results, _ = bot().retrieve("Is auto ping on?")
    assert {r["document"]["id"] for r in results} >= {"overview", "scheduler"}
    monkeypatch.setattr(graphrag, "VECTOR_MIN_SCORE", 1.01)  # nothing is close enough
    results, _ = bot().retrieve("tell me something")
    assert [r["via"] for r in results] == ["overview"]


def test_link_entities_skips_ambiguous_short_names():
    snap = Snapshot(DRIVER)
    assert graphrag.link_entities("is ch1 ok on main?", snap) == {"servers": ["main.example.test"], "channels": ["ch1"]}
    assert graphrag.link_entities("how is example?", snap) == {"servers": [], "channels": []}  # 3 servers share it
    assert graphrag.link_entities("check final.example.test/ch1", snap)["servers"] == ["final.example.test/ch1"]


def test_prompt_follows_the_context_question_template():
    results = [{"document": {"text": "fact one"}}, {"document": {"text": "fact two"}}]
    prompt = ChatBot.build_prompt("Is it up?", results)
    assert prompt == "\nAnswer the question using the provided context.\n\nContext:\nfact one\n\nfact two\n\nQuestion:\nIs it up?\n"


# --- streaming the answer ---

def sse(*chunks, done=True):
    """An OpenRouter (OpenAI-style) stream: keep-alive comment, data: lines, [DONE]."""
    lines = [": OPENROUTER PROCESSING", ""] + [f"data: {json.dumps(c)}\n" for c in chunks]
    return ("\n".join(lines) + ("\ndata: [DONE]\n" if done else "")).encode()


def delta(text, finish=None):
    return {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}]}


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEYS", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")


def test_stream_yields_sources_tokens_and_done_with_generation_settings(api_key):
    seen = {}

    def handler(request):
        seen["url"], seen["body"], seen["auth"] = str(request.url), json.loads(request.content), request.headers["authorization"]
        return httpx.Response(200, content=sse(delta("ch1 is "), delta("on backup."), delta("", "stop")))

    b = bot(handler)
    results, live = b.retrieve("Is ch1 on backup?")
    events = list(b.stream("Is ch1 on backup?", results, live))
    assert events[0]["type"] == "sources" and events[0]["live"] == live and events[0]["sources"][0]["via"] == "overview"
    assert "".join(e["text"] for e in events if e["type"] == "token") == "ch1 is on backup."
    assert events[-1]["type"] == "done" and events[-1]["truncated"] is False
    assert events[-1]["model"] == "qwen/qwen3.8-27b"

    body = seen["body"]
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions" and seen["auth"] == "Bearer sk-or-test"
    assert body["model"] == "qwen/qwen3.8-27b" and body["stream"] is True
    assert body["max_tokens"] == 700 and body["temperature"] == 0.1
    assert body["messages"][0] == {"role": "system", "content": chatbot.SYSTEM + chatbot.AGENT_RULES}
    assert body["messages"][1]["content"] == ChatBot.build_prompt("Is ch1 on backup?", results)


def test_stream_marks_answers_cut_at_the_token_limit(api_key):
    b = bot(lambda r: httpx.Response(200, content=sse(delta("long"), delta("", "length"))))
    assert list(b.stream("q", []))[-1]["truncated"] is True


@pytest.mark.parametrize("handler, expected", [
    (lambda r: httpx.Response(401, json={"error": {"message": "No auth credentials found", "code": 401}}),
     "OpenRouter rejected OPENROUTER_API_KEY: No auth credentials found"),
    (lambda r: httpx.Response(402, json={"error": {"message": "Insufficient credits", "code": 402}}), "out of credits"),
    (lambda r: httpx.Response(400, json={"error": {"message": "bad/model is not a valid model ID", "code": 400}}),
     "doesn't know the model 'qwen/qwen3.8-27b'"),
    (lambda r: httpx.Response(200, content=sse(delta("par"), {"error": {"code": "server_error", "message": "upstream died"},
                                                              "choices": [{"finish_reason": "error"}]}, done=False)),
     "OpenRouter: upstream died"),
    (lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused")), "Can't reach OpenRouter"),
])
def test_stream_reports_openrouter_problems_as_an_error_event(api_key, handler, expected):
    events = list(bot(handler).stream("q", []))
    assert events[-1]["type"] == "error" and expected in events[-1]["message"]


def test_no_api_key_means_no_request(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEYS", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    b = bot(lambda r: pytest.fail("must not call OpenRouter without a key"))
    assert list(b.generate([], {}))[-1] == {"type": "error", "message": "Add OPENROUTER_API_KEY to .env and restart."}
    assert b.status()["configured"] is False and "OPENROUTER_API_KEY" in b.status()["problem"]


def test_status_checks_the_key_with_openrouter(api_key, monkeypatch):
    assert bot(lambda r: httpx.Response(200, json={"data": {"label": "k"}})).status()["configured"] is True
    rejected = bot(lambda r: httpx.Response(401, json={"error": {"message": "bad"}})).status()
    assert rejected["configured"] is False and "rejected" in rejected["problem"]
    offline = bot(lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused"))).status()
    assert offline["configured"] is False and "Can't reach OpenRouter" in offline["problem"]
    monkeypatch.setenv("CHATBOT_MODEL", "qwen/qwen3.7-flash")
    assert bot(lambda r: httpx.Response(200, json={})).status()["model"] == "qwen/qwen3.7-flash"


# --- HTTP ---

@pytest.fixture(scope="module")
def client():
    import server
    from fastapi.testclient import TestClient
    from tests.conftest import TEST_LOGIN
    server.auth.attempts.clear()
    c = TestClient(server.app)
    assert c.post("/login", data={**TEST_LOGIN, "next": "/"}, follow_redirects=False).status_code == 303
    return c


def test_chat_requires_login():
    import server
    from fastapi.testclient import TestClient
    assert TestClient(server.app).post("/api/chat", json={"message": "hi"}).status_code == 401


def test_chat_streams_ndjson_events(client, monkeypatch):
    import server
    monkeypatch.setattr(server.chatbot, "retrieve", lambda q, history: (["r"], {"channels": 1, "down": [], "onBackup": []}))
    monkeypatch.setattr(server.chatbot, "stream", lambda q, results, live, history: iter([
        {"type": "sources", "sources": [], "live": live}, {"type": "token", "text": f"echo {q} after {len(history)}"},
        {"type": "done", "elapsed_ms": 1, "truncated": False}]))
    r = client.post("/api/chat", json={"message": "  hi  "})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in r.text.splitlines()]
    assert [e["type"] for e in events] == ["conversation", "sources", "token", "done"]
    assert events[2]["text"] == "echo hi after 0"
    history = [{"role": "user", "content": "report of ingest1"}, {"role": "assistant", "content": "Here it is"}]
    r = client.post("/api/chat", json={"message": "summary?", "history": history})
    assert "echo summary? after 2" in r.text
    assert client.post("/api/chat", json={"message": "x", "history": [{"role": "system", "content": "y"}]}).status_code == 422
    assert client.post("/api/chat", json={"message": ""}).status_code == 422


# --- report tool ---

@pytest.fixture
def no_analysis(monkeypatch):
    """Report-card tests: skip the deep analysis that follows the card (tested on its own below)."""
    import report_analysis
    monkeypatch.setattr(report_analysis, "analysis_prompt", lambda driver, node, monitor, metrics=None: None)

def tool_piece(index, name=None, args="", call_id=None):
    function = {"arguments": args}
    if name:
        function["name"] = name
    return {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": index, "id": call_id, "type": "function",
                                                                "function": function}]}, "finish_reason": None}]}


def test_report_request_calls_the_tool_and_returns_a_report_card(api_key, no_analysis):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        # the call arrives in pieces: name first, then the arguments split across chunks
        return httpx.Response(200, content=sse(tool_piece(0, "generate_report", "", "call-1"),
                                               tool_piece(0, args='{"node": "fin'), tool_piece(0, args='al"}'),
                                               delta("", "tool_calls")))

    b = bot(handler)
    events = list(b.stream("Make a report on final", [], None))
    report = next(e for e in events if e["type"] == "report")
    assert report["node"] == "final.example.test/ch1"
    assert report["url"] == "/api/report.html?node=final.example.test%2Fch1"
    assert report["download"] == report["url"] + "&download=1"
    assert "complete report for final.example.test/ch1" in "".join(e["text"] for e in events if e["type"] == "token")
    assert events[-1]["type"] == "done" and not any(e["type"] == "tool_calls" for e in events)
    assert any(t["function"]["name"] == "generate_report" for t in seen["body"]["tools"]) and seen["body"]["tool_choice"] == "auto"


def test_report_for_everything_and_for_unknown_names(api_key, no_analysis):
    all_nodes = bot(lambda r: httpx.Response(200, content=sse(tool_piece(0, "generate_report", '{"node": "all"}'))))
    report = next(e for e in all_nodes.stream("full report", [], None) if e["type"] == "report")
    assert report["node"] is None and report["url"] == "/api/report.html" and report["download"] == "/api/report.html?download=1"

    unknown = bot(lambda r: httpx.Response(200, content=sse(tool_piece(0, "generate_report", '{"node": "nope"}'))))
    events = list(unknown.stream("report on nope", [], None))
    assert not any(e["type"] == "report" for e in events)
    assert "can't find a server or channel called 'nope'" in "".join(e["text"] for e in events if e["type"] == "token")


def test_resolve_report_target():
    from chatbot import resolve_report_target
    snap = Snapshot(DRIVER)
    assert resolve_report_target(snap, "ALL") == {"node": None, "label": "all nodes"}
    assert resolve_report_target(snap, "main.example.test")["node"] == "main.example.test"
    assert resolve_report_target(snap, "main")["node"] == "main.example.test"  # short name
    channel = resolve_report_target(snap, "ch1")
    assert channel["node"] == "final.example.test/ch1" and "ch1 channel's Final server" in channel["label"]
    assert "matches several servers" in resolve_report_target(snap, "example")["problem"]
    assert "can't find" in resolve_report_target(snap, "zzz")["problem"]


def test_report_in_chat_is_followed_by_the_deep_analysis_with_one_token_total(api_key, monkeypatch):
    import report_analysis
    monkeypatch.setattr(report_analysis, "analysis_prompt", lambda driver, node, monitor, metrics=None: {"prompt": "P", "findings": []})
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if "tools" in body:  # the chat turn: the model asks for the report
            return httpx.Response(200, content=sse(tool_piece(0, "generate_report", '{"node": "all"}'),
                                                   {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 10}}))
        return httpx.Response(200, content=sse(delta("## Executive summary\nAll good."),
                                               {"choices": [], "usage": {"prompt_tokens": 500, "completion_tokens": 300,
                                                                         "completion_tokens_details": {"reasoning_tokens": 200}}}))

    events = list(bot(handler).stream("full report please", [], None))
    types = [e["type"] for e in events]
    assert types.index("report") < types.index("done") and types.count("done") == 1
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert "complete report for all nodes" in text and "## Executive summary" in text
    assert events[-1]["tokens"] == {"input": 600, "output": 310, "reasoning": 200}
    assert calls[1]["messages"] == [{"role": "user", "content": "P"}] and calls[1]["reasoning"]["effort"] == "medium"


@pytest.mark.integration
def test_graphrag_cypher_on_real_neo4j(driver, graph, monkeypatch):
    """The vector indexes, embedding writes, vector query and graph walks against a real Neo4j."""
    from tests.conftest import domain
    graph({"main": ["MainInput"], "backup": ["BackupLink"], "final": ["FinalLink"]},
          [("main", "FEEDS", "final"), ("backup", "FEEDS", "final")])
    for name, role in [("main", "MainInput"), ("backup", "BackupLink"), ("final", "FinalLink")]:
        url = f"https://{domain(name)}/ch9/index.m3u8"
        driver.execute_query("MATCH (n:Domain {domain: $d}) SET n.status = 'UP', n.links = $l", d=domain(name),
                             l=json.dumps([{"url": url, "role": role, "channel": "ch9"}]))
    monkeypatch.setattr(chatbot, "TEST_TLD", ".not-hidden-here")  # the app hides test domains; this test needs them
    b = ChatBot(driver, lambda: MONITOR, http=ollama(lambda r: httpx.Response(500)), embedder=WordEmbedder())
    snap = Snapshot(driver)
    assert b.graph.sync(snap) >= 3  # our 3 servers (the rest of the database may be embedded too)
    stored = driver.execute_query("MATCH (n:Domain {domain: $d}) RETURN size(n.embedding) AS dims, n.embeddingHash AS h",
                                  d=domain("main")).records[0]
    assert stored["dims"] == graphrag.RAG_DIM and stored["h"]
    results = b.graph.retrieve("How is the ch9 channel?", snap, build_documents(snap, MONITOR, []))
    got = {r["document"]["id"] for r in results}
    assert {"channel:ch9", f"server:{domain('main')}", f"server:{domain('backup')}", f"server:{domain('final')}"} <= got
    edges = next(r for r in results if r["document"]["id"].startswith("graph:edges"))["document"]["text"]
    assert f"{domain('main')} FEEDS {domain('final')}" in edges
    hits = b.graph.vector_seeds("main server", snap)  # the index answers (population can lag a moment)
    assert all(label in ("Domain", "SpiderRun", "ActivityLog") for label, _, _ in hits)


def test_overview_carries_the_computed_root_cause_and_early_warnings():
    overview = {"id": "overview", "text": "Channels DOWN right now: ch1."}
    insights = {"ranking": [{"nodes": ["a", "b"], "ranking": [{"node": "a", "score": 0.7, "reasons": ["stopped first"],
                                                                 "onsetAt": "2026-09-30T06:30:00+00:00"}]}],
                "warnings": [{"node": "c", "score": 6.0, "warnings": ["segment age 20 is 6σ above its normal 3"]}]}
    text = chatbot.with_insights(overview, insights)["text"]
    assert "Likely root cause of the current failure of a, b: a (70%; stopped first; stopped 2026-09-30 12:00:00 IST)" in text
    assert "Early warning for c (still up): segment age 20 is 6σ above its normal 3." in text
    assert chatbot.with_insights(overview, {}) is overview


# --- Strategy B: Multi-Key Pool & Round-Robin Rotation ---

def test_keypool_round_robin_rotation(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEYS", "sk-key-alpha, sk-key-beta, sk-key-gamma")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from chatbot import KeyPool
    pool = KeyPool()
    assert pool.all_keys() == ["sk-key-alpha", "sk-key-beta", "sk-key-gamma"]
    # Round-robin successive calls
    assert pool.next_key() == "sk-key-alpha"
    assert pool.next_key() == "sk-key-beta"
    assert pool.next_key() == "sk-key-gamma"
    assert pool.next_key() == "sk-key-alpha"


def test_keypool_cooldown_and_skipping(monkeypatch):
    from chatbot import KeyPool
    pool = KeyPool(["sk-1", "sk-2", "sk-3"])
    pool.mark_cooldown("sk-1", status_code=429, duration=10.0)
    assert pool.is_in_cooldown("sk-1") is True
    assert pool.is_in_cooldown("sk-2") is False

    # Round-robin skips sk-1 and returns sk-2, then sk-3, then sk-2
    assert pool.next_key() == "sk-2"
    assert pool.next_key() == "sk-3"
    assert pool.next_key() == "sk-2"


def test_generate_automatic_failover_on_429(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEYS", "sk-key-1, sk-key-2")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    auth_headers = []

    def handler(request):
        auth = request.headers.get("authorization")
        auth_headers.append(auth)
        if auth == "Bearer sk-key-1":
            # Key 1 fails with 429 rate limit
            return httpx.Response(429, json={"error": {"message": "Rate limit exceeded", "code": 429}})
        # Key 2 succeeds
        return httpx.Response(200, content=sse(delta("recovered via key 2")))

    b = bot(handler)
    events = list(b.generate([{"role": "user", "content": "hi"}], {}))
    # Tokens should stream from key 2
    tokens = "".join(e["text"] for e in events if e["type"] == "token")
    assert tokens == "recovered via key 2"
    # Both keys were hit: key 1 failed, key 2 succeeded
    assert auth_headers == ["Bearer sk-key-1", "Bearer sk-key-2"]
    # Key 1 is now marked on cooldown
    assert b.key_pool.is_in_cooldown("sk-key-1") is True
    # Subsequent call immediately uses key 2 without trying key 1
    auth_headers.clear()
    list(b.generate([{"role": "user", "content": "next"}], {}))
    assert auth_headers == ["Bearer sk-key-2"]


def test_generate_automatic_failover_on_402_out_of_credits(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEYS", "sk-exhausted, sk-funded")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    auth_headers = []

    def handler(request):
        auth = request.headers.get("authorization")
        auth_headers.append(auth)
        if auth == "Bearer sk-exhausted":
            return httpx.Response(402, json={"error": {"message": "Insufficient credits", "code": 402}})
        return httpx.Response(200, content=sse(delta("success with funded key")))

    b = bot(handler)
    events = list(b.generate([{"role": "user", "content": "hi"}], {}))
    tokens = "".join(e["text"] for e in events if e["type"] == "token")
    assert tokens == "success with funded key"
    assert auth_headers == ["Bearer sk-exhausted", "Bearer sk-funded"]
    assert b.key_pool.is_in_cooldown("sk-exhausted") is True


def test_status_reports_keypool_metrics(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEYS", "sk-key-a1234567890, sk-key-b1234567890")
    b = bot(lambda r: httpx.Response(200, json={"data": {}}))
    st = b.status()
    assert st["configured"] is True
    assert "keyPool" in st
    kp = st["keyPool"]
    assert kp["strategy"] == "Round-Robin Rotation (Active-Active)"
    assert kp["total_keys"] == 2
    assert kp["active_keys"] == 2
    assert kp["cooldown_keys"] == 0
    assert len(kp["keys"]) == 2


def test_scrapy_tool_extracts_internal_and_external_links(monkeypatch):
    b = bot()
    # 1. Missing URL returns a prompt
    empty_events = list(b.tools.execute({"name": "scrapy", "arguments": "{}"}))
    assert any("Please provide a URL" in e.get("text", "") for e in empty_events)

    # 2. Mock HTTP HTML response with internal, external, and streaming media links
    html_sample = """
    <!DOCTYPE html>
    <html>
    <head><title>OTT Live Streaming Platform</title></head>
    <body>
        <h1>Welcome</h1>
        <a href="/channels/gtcnews">GTC News Channel</a>
        <a href="https://stream.ottlive.co.in/live/master.m3u8">HLS Master Playlist</a>
        <a href="https://external-cdn.akamai.com/live/segment.ts">Akamai Segment</a>
        <a href="https://github.com/google/gemini">Antigravity GitHub</a>
        <video src="https://stream.ottlive.co.in/promo.mp4"></video>
    </body>
    </html>
    """

    class MockClient:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url, *args, **kwargs):
            req = httpx.Request("GET", url)
            return httpx.Response(200, request=req, headers={"content-type": "text/html; charset=utf-8"}, text=html_sample)

    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: MockClient())

    events = list(b.tools.execute({"name": "scrapy", "arguments": json.dumps({"url": "https://stream.ottlive.co.in"})}))
    text = "".join(e.get("text", "") for e in events if e.get("type") == "token")

    assert "Scrapy Audit Report: **OTT Live Streaming Platform**" in text
    assert "Internal Links" in text
    assert "External Outbound Links" in text
    assert "external-cdn.akamai.com" in text
    assert "github.com" in text
    assert "HLS Master Playlist" in text
    assert "Streaming Media & Chunk Links" in text


def test_activity_stored_in_neo4j_and_retrieved_in_llm_context(monkeypatch):
    from activity import record_activity, list_activities
    
    recorded_cypher = []
    class FakeDriver:
        def execute_query(self, query, **kwargs):
            recorded_cypher.append((query, kwargs))
            class FakeRecord:
                def __init__(self, data):
                    self._data = data
                def __getitem__(self, item):
                    return self._data[item]
            class FakeResult:
                records = []
            return FakeResult()

    driver = FakeDriver()
    act = record_activity(
        driver=driver,
        embedder=lambda texts: [[0.1] * 384],
        act_type="scrapy",
        title="Scrapy Crawl: test.example.com",
        summary="Found 15 links on test.example.com including 2 m3u8 streams",
        target="https://test.example.com",
        details={"total_links": 15, "media_count": 2},
    )
    assert act["id"].startswith("act_")
    assert act["has_embedding"] is True
    assert len(recorded_cypher) >= 1
    # Verify Cypher query writes ActivityLog with embedding
    create_call = recorded_cypher[0]
    assert "ActivityLog" in create_call[0]
    assert "ScrapyRun" in create_call[0]
    assert create_call[1]["summary"] == "Found 15 links on test.example.com including 2 m3u8 streams"
    assert len(create_call[1]["vec"]) == 384





def test_changes_always_wait_for_the_users_allow(monkeypatch):
    import nodes
    import tools as tools_mod
    writes = []
    monkeypatch.setattr(nodes, "upsert_nodes", lambda driver, ns: writes.append(ns))
    monkeypatch.setattr(nodes, "connect_new_channels", lambda driver, channels: [])
    monkeypatch.setattr(tools_mod, "PENDING_ACTIONS", {})
    b = bot()

    for d in b.tools.definitions():
        assert "confirmed" not in d["function"]["parameters"]["properties"], d["function"]["name"]

    # The model claiming confirmation still only stages the change and asks the user.
    args = {"channel": "gtcnews", "role": "BackupLink", "url": "http://ingest9.ottlive.co.in/live/gtc.m3u8", "confirmed": True}
    events = list(b.tools.execute({"name": "add_stream_link", "arguments": json.dumps(args)}))
    card = next(e for e in events if e["type"] == "action_confirm")
    assert writes == []

    # Deny drops it; the same id cannot be allowed afterwards.
    list(b.tools.cancel_action(card["action_id"]))
    assert not any("Created" in e.get("text", "") for e in b.tools.execute_confirmed_action(card["action_id"]))
    assert writes == []

    # Allow (the user's CONFIRM) applies it once.
    events = list(b.tools.execute({"name": "add_stream_link", "arguments": json.dumps(args)}))
    action_id = next(e for e in events if e["type"] == "action_confirm")["action_id"]
    list(b.tools.execute_confirmed_action(action_id))
    assert len(writes) == 1
    list(b.tools.execute_confirmed_action(action_id))
    assert len(writes) == 1


def test_the_agent_learns_facts_fixes_and_verdicts_and_uses_them_in_answers(tmp_path):
    from learning import Learner
    from metrics import Metrics
    b = bot()
    b.learning = Learner(tmp_path / "m.db", embed=WordEmbedder().encode)
    run = lambda name, **args: "".join(e.get("text", "") for e in b.tools.execute({"name": name, "arguments": json.dumps(args)}))

    assert "Remembered (about backup.example.test)" in run("remember_fact", fact="backup is the night-time feed")
    assert "already remember" in run("remember_fact", fact="Backup is the night-time feed")
    assert "Noted: main.example.test was the root cause" in run("record_root_cause_feedback", node="main", was_root_cause=True)
    assert b.learning.priors() == {"main.example.test": 1.333}
    assert "no closed outage on record" in run("record_incident_fix", node="main", fix="restarted the encoder")
    assert "don't know a single server" in run("record_root_cause_feedback", node="example", was_root_cause=False)

    metrics = Metrics(tmp_path / "m.db")
    metrics.add_incident("main.example.test", {"type": "OUTAGE", "timestamp": "2026-09-30T06:00:00+00:00", "category": "HTTP_ERROR"})
    metrics.add_incident("main.example.test", {"type": "RECOVERY", "timestamp": "2026-09-30T06:05:00+00:00", "durationS": 300})
    assert "was fixed by" in run("record_incident_fix", node="main.example.test", fix="restarted the encoder")
    memory = run("get_learned_memory")
    assert "backup is the night-time feed" in memory and "fixed by restarted the encoder" in memory

    # The main server is down in the fake graph: its past outage, the fix and the fact come with the question.
    results, _ = b.retrieve("why is main down?")
    doc = next(r["document"] for r in results if r["via"] == "memory")
    assert "night-time feed" in doc["text"] and "fixed by: restarted the encoder" in doc["text"]
    assert "confirmed main.example.test as the root cause 1 time(s)" in doc["text"]

    assert "Forgotten" in run("forget_fact", about="night-time")
    assert b.learning.facts() == []


def test_follow_ups_carry_the_conversation(api_key):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=sse(delta("ok", "stop")))

    b = bot(handler)
    history = [{"role": "user", "content": "how is main.example.test"},
               {"role": "assistant", "content": "x" * 5000}, {"role": "tool", "content": "dropped"}]
    results, _ = b.retrieve("give me its summary", history)
    assert any(r["document"]["id"] == "server:main.example.test" for r in results)  # "its" = the server asked about
    list(b.stream("give me its summary", results, None, history))
    roles = [m["role"] for m in seen["body"]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    assert len(seen["body"]["messages"][2]["content"]) < 3100


def test_a_whole_channel_is_added_with_one_allow_and_wired(monkeypatch):
    import nodes
    import tools as tools_mod
    from nodes import Relationship as R
    written, wired = [], []
    monkeypatch.setattr(nodes, "upsert_nodes", lambda driver, ns: written.append(ns))
    monkeypatch.setattr(nodes, "connect_new_channels", lambda driver, chans: wired.append(chans) or [
        R(source="xcode3.ottlive.co.in", type="PRODUCES", target="stream.ottlive.co.in/sakshitv")])
    monkeypatch.setattr(tools_mod, "PENDING_ACTIONS", {})
    b = bot()
    links = [{"role": "MainInput", "url": "https://cloud.ottlive.co.in/sakshitv/sakshitv/index.m3u8"},
             {"role": "BackupLink", "url": "https://ingest.ottlive.co.in/sakshitvbackup/sakshitvbackup/index.m3u8"},
             {"role": "Transcoding", "url": "https://xcode3.ottlive.co.in/sakshitv/index.m3u8"},
             {"role": "FinalLink", "url": "https://stream.ottlive.co.in/sakshitv/index.m3u8"}]
    events = list(b.tools.execute({"name": "add_channel", "arguments": json.dumps(
        {"channel": "sakshitv", "links": links, "confirmed": True})}))
    cards = [e for e in events if e["type"] == "action_confirm"]
    assert len(cards) == 1 and written == []  # one card for all four; nothing written before Allow
    assert len(cards[0]["preview"]) == 5 and cards[0]["preview"][0].startswith("Main: https://cloud.ottlive.co.in")

    done = "".join(e.get("text", "") for e in b.tools.execute_confirmed_action(cards[0]["action_id"]))
    (nodes_written,) = written
    assert sorted(n.domain for n in nodes_written) == ["cloud.ottlive.co.in", "ingest.ottlive.co.in",
                                                       "stream.ottlive.co.in/sakshitv", "xcode3.ottlive.co.in"]
    assert wired == [{"sakshitv"}] and "Channel `sakshitv` added" in done and "PRODUCES" in done

    bad = "".join(e.get("text", "") for e in b.tools.execute({"name": "add_channel", "arguments": json.dumps(
        {"channel": "x", "links": [{"role": "MainInput", "url": "not a url"}]})}))
    assert "couldn't add that channel" in bad


def test_query_graph_refuses_writes_and_formats_rows():
    from tools import cypher_problem, format_rows
    assert cypher_problem("MATCH (n:Domain) RETURN n.domain LIMIT 5") is None
    assert cypher_problem("MATCH (n) WHERE n.lastError CONTAINS 'SET timeout' RETURN n") is None  # inside a string
    assert cypher_problem("MATCH (n) WHERE n.status = 'x' RETURN COUNT { (n)-[:FEEDS]->() } AS c") is None
    for q in ["MATCH (n) DETACH DELETE n", "MATCH (n) SET n.status = 'UP'", "CREATE (:X)", "MERGE (a:A)",
              "MATCH (n) REMOVE n.x", "CALL apoc.create.node(['X'], {})", "LOAD CSV FROM 'x' AS r RETURN r",
              "MATCH (n) RETURN n; MATCH (m) DELETE m", "match (n) set n.a = 1"]:
        assert cypher_problem(q), q
    table = format_rows([{"domain": "a.example", "feeds": 3}, {"domain": "b|c", "feeds": None}], False)
    assert "| domain | feeds |" in table and "b\\|c" in table and "| — |" in table
    assert format_rows([], False) == "No rows.\n"


def test_the_cheat_sheet_and_schema_reach_the_model():
    b = bot()
    (q,) = [d for d in b.tools.definitions() if d["function"]["name"] == "query_graph"]
    desc = q["function"]["description"]
    assert "neo4j.com/docs/cypher-manual/current/cheat-sheet" in desc and "FEEDS" in desc and "no GROUP BY" in desc


def test_each_question_gets_only_the_tools_it_needs():
    from tools import Tools
    names = lambda q: {d["function"]["name"] for d in Tools.definitions_for(q)}
    plain = names("why is rang manch down?")
    assert "get_root_cause_ranking" in plain and "query_graph" not in plain and "delete_node" not in plain
    assert {"add_channel", "add_stream_link"} <= names("Add channel sakshitv Main: https://a.example/x.m3u8")
    assert "query_graph" in names("how many servers carry more than 2 channels")
    assert "remember_fact" in names("remember that xcode2 restarts at 3am")
    assert "generate_excel" in names("export an excel workbook")
    assert len(Tools.definitions_for("hi")) < len(Tools.definitions()) / 2
