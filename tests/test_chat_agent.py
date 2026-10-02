"""The chatbot as an agent: several tool rounds per question, the stronger model for hard questions, tools picked by
meaning and by what worked before, learning from its own answers (👍 examples, rephrased questions), saved
conversations, and the exam that grades it."""

import json
import time

import httpx

import chat_eval
import chatbot
import tools as tools_mod
from chat_store import ChatStore
from learning import Learner
from tests.test_chatbot import WordEmbedder, api_key, bot, client, delta, no_topology, sse, tool_piece  # noqa: F401


def scripted(*replies):
    """An OpenRouter that answers the n-th request with replies[n] (an SSE body); the request bodies are kept."""
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, content=replies[min(len(bodies), len(replies)) - 1])
    return handler, bodies


def fake_tool(b, monkeypatch, outputs):
    ran = []

    def execute(call):
        ran.append((call["name"], call["arguments"]))
        yield {"type": "token", "text": outputs.get(call["name"], "nothing")}
    monkeypatch.setattr(b.tools, "execute", execute)
    return ran


def test_the_agent_runs_tools_over_several_rounds_and_then_answers(api_key, monkeypatch):
    handler, bodies = scripted(
        sse(tool_piece(0, "get_incident_history", '{"node": "main"}', "call-1"), delta("", "tool_calls")),
        sse(tool_piece(0, "get_early_warnings", "{}", "call-2"), delta("", "tool_calls")),
        sse(delta("main was down twice; no warnings now."), delta("", "stop")))
    b = bot(handler)
    b.strong_model = None
    ran = fake_tool(b, monkeypatch, {"get_incident_history": "2 outages: 10:00, 12:00", "get_early_warnings": "none"})
    events = list(b.stream("how has main been?", [], None))

    assert [r[0] for r in ran] == ["get_incident_history", "get_early_warnings"] and len(bodies) == 3
    steps = [e for e in events if e["type"] in ("step", "step_done")]
    assert [(e["type"], e["tool"]) for e in steps] == [("step", "get_incident_history"), ("step_done", "get_incident_history"),
                                                       ("step", "get_early_warnings"), ("step_done", "get_early_warnings")]
    assert steps[1]["summary"] == "2 outages: 10:00, 12:00"
    # The model sees its own call (with the id) and the result under that id.
    second = bodies[1]["messages"]
    assert second[-2]["tool_calls"][0]["id"] == "call-1" and second[-1] == {
        "role": "tool", "tool_call_id": "call-1", "content": "2 outages: 10:00, 12:00"}
    assert "".join(e["text"] for e in events if e["type"] == "token") == "main was down twice; no warnings now."
    done = events[-1]
    assert done["type"] == "done" and done["steps"] == 2 and done["toolsUsed"] == ["get_incident_history", "get_early_warnings"]


def test_a_graph_change_stops_at_its_allow_card(api_key, monkeypatch):
    handler, bodies = scripted(sse(tool_piece(0, "delete_node", '{"domain": "main"}', "c1"), delta("", "tool_calls")))
    b = bot(handler)
    monkeypatch.setattr(b.tools, "execute", lambda call: iter([{"type": "action_confirm", "action_id": "act_123456"}]))
    events = list(b.stream("delete main", [], None))
    assert len(bodies) == 1 and any(e["type"] == "action_confirm" for e in events)  # the model never confirms it


def test_the_loop_ends_and_the_last_round_must_answer(api_key, monkeypatch):
    calls = [sse(tool_piece(0, "query_graph", json.dumps({"cypher": f"RETURN {i}"}), f"c{i}"), delta("", "tool_calls"))
             for i in range(chatbot.MAX_STEPS)]
    handler, bodies = scripted(*calls, sse(delta("done looking"), delta("", "stop")))
    b = bot(handler)
    b.strong_model = None
    fake_tool(b, monkeypatch, {})
    events = list(b.stream("count everything", [], None))
    assert len(bodies) == chatbot.MAX_STEPS + 1 and "tools" not in bodies[-1]
    assert events[-1]["steps"] == chatbot.MAX_STEPS


def test_the_same_call_twice_is_not_run_again(api_key, monkeypatch):
    same = sse(tool_piece(0, "get_early_warnings", "{}", "x"), delta("", "tool_calls"))
    handler, bodies = scripted(same, same, sse(delta("ok"), delta("", "stop")))
    b = bot(handler)
    b.strong_model = None
    ran = fake_tool(b, monkeypatch, {"get_early_warnings": "none"})
    list(b.stream("warnings?", [], None))
    assert len(ran) == 1 and "already made" in bodies[2]["messages"][-1]["content"]


def test_hard_questions_go_to_the_stronger_model(api_key, monkeypatch):
    b = bot()
    b.strong_model = "big/model"
    assert b.pick_model("Why did xcode4 go down?") == "big/model"
    assert b.pick_model("is xcode4 up") == b.model
    b.strong_model = None
    assert b.pick_model("Why did xcode4 go down?") == b.model
    monkeypatch.setenv("CHATBOT_STRONG_MODEL", "off")
    assert bot().strong_model is None


def test_tools_are_picked_by_meaning_and_by_what_worked_before(monkeypatch):
    monkeypatch.setattr(tools_mod.Tools, "_tool_vectors", {})
    monkeypatch.setitem(tools_mod.TOOL_EXAMPLES, "run_traceroute", ["trace the network path to a server"])
    enc = WordEmbedder()
    vocab = WordEmbedder.VOCAB + ["trace", "network", "path"]
    monkeypatch.setattr(WordEmbedder, "VOCAB", vocab)
    embed = lambda text: enc.encode([text])[0]
    names = lambda defs: {d["function"]["name"] for d in defs}
    plain = names(tools_mod.Tools.definitions_for("hello there"))
    assert "run_traceroute" not in plain
    assert "run_traceroute" in names(tools_mod.Tools.definitions_for("show the network path please", embed=embed))
    assert "get_pool_status" in names(tools_mod.Tools.definitions_for("hello there", learned=["get_pool_status"]))


def test_good_answers_become_examples_and_their_tools_are_offered_again(tmp_path):
    learner = Learner(tmp_path / "m.db", embed=WordEmbedder().encode)
    learner.add_feedback("answer", 1, question="is main down?", answer="Yes, since 10:00.", tools=["get_incident_history"])
    learner.add_feedback("answer", -1, question="is main down?", answer="No idea.", tools=["get_pool_status"])
    assert learner.tools_for_question("main down?") == ["get_incident_history"]
    assert learner.tools_for_question("latency of the scheduler") == []
    ctx = learner.context("is main down?")["text"]
    assert "rated good" in ctx and "Yes, since 10:00." in ctx and "No idea" not in ctx


def test_asking_again_right_away_counts_as_a_quiet_thumbs_down(tmp_path):
    learner = Learner(tmp_path / "m.db", embed=WordEmbedder().encode)
    assert learner.note_rephrase("is main down?", "dunno", "main down or not?", ["get_pool_status"])
    assert not learner.note_rephrase("is main down?", "dunno", "what is the latency of the scheduler", [])
    stats = learner.feedback_stats()["answer"]
    assert stats == {"up": 0, "down": 0, "rephrased": 1}
    assert learner.tools_for_question("is main down?") == []  # a rephrase never becomes an example
    (row,) = learner.feedback_list()
    assert row["source"] == "implicit" and row["tools"] == ["get_pool_status"]


def test_conversations_are_saved_reopened_and_deleted(tmp_path):
    store = ChatStore(tmp_path / "m.db")
    conv = store.ensure(None, "  is   main down?  ")
    assert store.ensure(conv) == conv and store.ensure("nope") != conv
    store.add(conv, "user", "is main down?")
    store.add(conv, "assistant", "Yes.", {"toolsUsed": ["get_incident_history"]})
    assert store.history(conv) == [{"role": "user", "content": "is main down?"}, {"role": "assistant", "content": "Yes."}]
    last = store.last_exchange(conv)
    assert last["question"] == "is main down?" and last["tools"] == ["get_incident_history"]
    store.add(conv, "user", "and now?")
    assert store.last_exchange(conv) is None  # the newest question has no answer yet
    (c,) = [c for c in store.conversations() if c["id"] == conv]
    assert c["title"] == "is main down?" and c["n"] == 3
    assert store.delete(conv) and store.messages(conv) == []


def test_the_chat_route_saves_turns_and_notices_a_rephrase(client, monkeypatch, tmp_path):
    import server
    monkeypatch.setattr(server, "chat_store", ChatStore(tmp_path / "c.db"))
    noted = []
    monkeypatch.setattr(server, "learner", type("L", (), {"note_rephrase": lambda self, *a: noted.append(a)})())
    monkeypatch.setattr(server.chatbot, "retrieve", lambda q, history: ([], None))
    monkeypatch.setattr(server.chatbot, "stream", lambda q, results, live, history: iter([
        {"type": "step_done", "tool": "get_early_warnings", "summary": "none"},
        {"type": "token", "text": f"answer to {q} with {len(history)} earlier"},
        {"type": "done", "elapsed_ms": 1, "toolsUsed": ["get_early_warnings"], "model": "m"}]))
    first = [json.loads(line) for line in client.post("/api/chat", json={"message": "any warnings?"}).text.splitlines()]
    conv = first[0]["id"]
    r = client.post("/api/chat", json={"message": "warnings at all?", "conversation_id": conv})
    assert "with 2 earlier" in r.text  # the history came from the saved conversation
    assert noted and noted[0][0] == "any warnings?" and noted[0][3] == ["get_early_warnings"]
    msgs = client.get(f"/api/chat/conversations/{conv}").json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[1]["meta"]["steps"] == [{"tool": "get_early_warnings", "summary": "none"}]
    assert client.get("/api/chat/conversations").json()["conversations"][0]["id"] == conv
    assert client.delete(f"/api/chat/conversations/{conv}").status_code == 200
    assert client.get(f"/api/chat/conversations/{conv}").status_code == 404


class ExamBot:
    model = "fast/model"

    def __init__(self, answers):
        self.answers = answers
        self.tools = type("T", (), {"cancel_action": lambda self, a: iter([])})()

    def retrieve(self, q, history):
        return [], None

    def stream(self, q, results, live, history):
        text, used = self.answers[q]
        yield {"type": "token", "text": text}
        yield {"type": "done", "toolsUsed": used, "model": self.model, "tokens": {"input": 10, "output": 5}}

    def generate(self, messages, options):
        yield {"type": "token", "text": "YES" if "03:00" in messages[1]["content"].split("New answer:")[1] else "NO"}


def test_the_exam_grades_tools_wording_and_corrections_and_keeps_runs(tmp_path):
    learner = Learner(tmp_path / "m.db", embed=WordEmbedder().encode)
    learner.add_feedback("answer", -1, question="when does xcode2 restart?", answer="never",
                         correction="every night at 03:00 IST")
    cases = [{"id": "a", "question": "history?", "tools": ["get_incident_history"], "expect": ["outage"]},
             {"id": "b", "question": "plain?", "tools": [], "expect": [], "forbid": ["MATCH \\("]},
             *chat_eval.correction_cases(learner)]
    bot_ = ExamBot({"history?": ("2 outages", ["get_early_warnings"]), "plain?": ("use MATCH (n)", []),
                    "when does xcode2 restart?": ("At 03:00 IST each night.", [])})
    ev = chat_eval.ChatEval(tmp_path / "m.db", bot_, learner)
    run = ev.run(cases)
    by = {r["id"]: r for r in run["results"]}
    assert "expected one of get_incident_history" in by["a"]["problems"][0]
    assert by["b"]["problems"] == ["answer has /MATCH \\(/"]
    assert [r["passed"] for r in run["results"] if r["source"] == "correction"] == [True]
    assert (run["passed"], run["total"], run["tokens"]) == (1, 3, 45)
    assert ev.runs()[0]["results"][0]["id"] == "a"
    assert len(chat_eval.load_cases()) >= 30


def test_the_exam_route_starts_one_run_at_a_time(client, monkeypatch):
    import server
    started = []
    monkeypatch.setattr(server.chat_eval, "start", lambda only=None: started.append(only) or {"started": True})
    assert client.post("/api/chat/eval", json={"only": ["a"]}).json() == {"started": True} and started == [["a"]]
    assert "runs" in client.get("/api/chat/eval").json()
