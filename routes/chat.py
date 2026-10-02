"""Chat routes (split out of server.py). Shared state and helpers are read from the server
module at call time as `srv.<name>`, so there is one copy of each and tests can patch them there."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pydantic import Field
from typing import List, Literal, Optional
import json
import re
import time

import server as srv

router = APIRouter()


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=20000)


class EvalRequest(BaseModel):
    only: List[str] = Field(default_factory=list, max_length=100)  # case ids; empty = all


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    history: List[Turn] = Field(default_factory=list, max_length=20)  # earlier messages (else the saved ones)
    conversation_id: Optional[str] = Field(None, max_length=40)


REPHRASE_WINDOW_S = 120  # the same question again within 2 min: the last answer probably missed
COMMAND = re.compile(r"^\s*(CONFIRM|CANCEL)\s+act_", re.I)


@router.get("/api/chat")
def chat_info():
    return srv.chatbot.status()


@router.get("/api/tools")
def list_tools():
    """Returns every chatbot tool with category metadata for dashboard & ChatBot."""
    defs = srv.chatbot.tools.definitions()
    tool_meta = {
        "generate_report": {"category": "Reporting", "icon": "📄", "prompt": "Generate a complete monitoring report of all nodes"},
        "generate_excel": {"category": "Export", "icon": "📊", "prompt": "Give me complete data of this project in Excel"},
        "diagnose_hls_stream": {"category": "Diagnostics", "icon": "🩺", "prompt": "Diagnose HLS stream failures: stale segments, frozen media sequence, and 404s"},
        "get_pool_status": {"category": "Telemetry Pool", "icon": "⚡", "prompt": "Check In-Memory Data Pool metrics, hit ratio, and cache freshness"},
        "get_risk_forecast": {"category": "ML Predictor", "icon": "🔮", "prompt": "Which channels or nodes have the highest failure risk right now? Show risk assessment."},
        "trigger_channel_crawl": {"category": "Diagnostics", "icon": "🕷", "prompt": "Trigger an immediate spider health crawl on all channels"},
        "get_root_cause_ranking": {"category": "RCA Engine", "icon": "🎯", "prompt": "What is the root cause of any current stopped streams or outages? Rank the culprits."},
        "get_early_warnings": {"category": "Telemetry", "icon": "⚡", "prompt": "Are there any early warning signals or abnormal metrics on our servers right now?"},
        "run_traceroute": {"category": "Network", "icon": "🌐", "prompt": "Run traceroute to stream.ottlive.co.in and analyze hop latency"},
        "check_failover_status": {"category": "Redundancy", "icon": "🛡️", "prompt": "Check failover status across all channels. Is any channel running on its backup link?"},
        "get_incident_history": {"category": "Incident Log", "icon": "📜", "prompt": "Show all past outage and recovery incidents recorded in the server logs"},
        "audit_topology": {"category": "Graph Audit", "icon": "🩺", "prompt": "Audit our graph topology for channels lacking backup redundancy or orphan nodes"},
        "query_graph": {"category": "Graph Query", "icon": "🔎", "prompt": "Which servers carry more than 2 channels? Use a Cypher query."},
        "add_channel": {"category": "Safe Action · 2-Phase", "icon": "📺", "prompt": "Add channel sakshitv: Main https://…/index.m3u8, Backup …, Transcoding …, Final …"},
        "add_stream_link": {"category": "Safe Action · 2-Phase", "icon": "➕", "prompt": "Add a new stream link for channel 'gtcnews' with role MainInput on jio.ottlive.co.in"},
        "connect_pipeline_relationship": {"category": "Safe Action · 2-Phase", "icon": "🔗", "prompt": "Connect pipeline relationship FEEDS from ingest.ottlive.co.in to transcode.ottlive.co.in"},
        "update_stream_link": {"category": "Safe Action · 2-Phase", "icon": "✏️", "prompt": "Update stream link on jio.ottlive.co.in for channel gtcnews"},
        "delete_node": {"category": "Danger · High Impact", "icon": "🗑️", "prompt": "Delete node jio.ottlive.co.in from the topology with blast radius preview"},
        "scrapy": {"category": "Web & Stream Crawler", "icon": "🕸️", "prompt": "Scrapy crawl and extract all internal and external links from https://example.com"},
        "remember_fact": {"category": "Learning", "icon": "🧠", "prompt": "Remember that xcode2 restarts every night at 03:00 IST"},
        "forget_fact": {"category": "Learning", "icon": "🧹", "prompt": "Forget what you remember about xcode2"},
        "record_root_cause_feedback": {"category": "Learning", "icon": "✅", "prompt": "The real root cause of the last outage was ingest1"},
        "record_incident_fix": {"category": "Learning", "icon": "🔧", "prompt": "ingest1's last outage was fixed by restarting nginx"},
        "get_learned_memory": {"category": "Learning", "icon": "📚", "prompt": "What have you learned from past outages?"},
    }
    try:
        from tools_registry import ChatToolRegistry
        tool_meta.update(ChatToolRegistry.metadata_map())
    except Exception:
        pass
    enriched = []
    for d in defs:
        fn = d.get("function", {})
        name = fn.get("name", "")
        meta = tool_meta.get(name, {"category": "Tool", "icon": "🛠", "prompt": f"Run {name}"})
        enriched.append({
            "name": name,
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {}),
            "category": meta["category"],
            "icon": meta["icon"],
            "prompt": meta["prompt"],
        })
    return {"count": len(enriched), "tools": enriched}


@router.post("/api/chat")
def chat(body: ChatRequest):
    """Answers one question. Retrieval runs first (so its failures are plain HTTP errors); the answer then
    streams as NDJSON, one event per line: {"type": "conversation", "id"}, {"type": "sources"}, step / step_done
    while tools run, {"type": "token", "text"}..., then {"type": "done"} or {"type": "error", "message"}.
    Both sides of the exchange are saved in the conversation."""
    query = body.message.strip()
    store = srv.chat_store
    conv = store.ensure(body.conversation_id, query)
    history = [t.model_dump() for t in body.history] or store.history(conv)
    if not COMMAND.match(query):
        notice_rephrase(store.last_exchange(conv), query)
    store.add(conv, "user", query)
    results, live = srv.chatbot.retrieve(query, history)

    def events():
        yield {"type": "conversation", "id": conv}
        text, steps, done, error = [], [], {}, None
        try:
            for event in srv.chatbot.stream(query, results, live, history):
                kind = event.get("type")
                if kind == "token":
                    text.append(event["text"])
                elif kind == "step_done":
                    steps.append({"tool": event["tool"], "summary": event.get("summary", "")})
                elif kind == "done":
                    done = event
                elif kind == "error":
                    error = event.get("message")
                yield event
        finally:  # saved even if the page goes away mid-answer
            answer = "".join(text).strip() or (f"(error: {error})" if error else "")
            if answer:
                store.add(conv, "assistant", answer, {"steps": steps, "toolsUsed": done.get("toolsUsed", []),
                                                      "model": done.get("model"), "elapsed_ms": done.get("elapsed_ms")})

    return StreamingResponse((json.dumps(e) + "\n" for e in events()), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def notice_rephrase(previous: Optional[dict], query: str) -> None:
    if not previous or time.time() - previous["ts"] > REPHRASE_WINDOW_S or srv.learner is None:
        return
    try:
        srv.learner.note_rephrase(previous["question"], previous["answer"], query, previous["tools"])
    except Exception:
        pass  # learning is a bonus; the answer must still come


@router.get("/api/chat/conversations")
def list_conversations(limit: int = 50):
    return {"conversations": srv.chat_store.conversations(min(max(limit, 1), 200))}


@router.get("/api/chat/conversations/{conv_id}")
def get_conversation(conv_id: str):
    messages = srv.chat_store.messages(conv_id)
    if not messages:
        raise HTTPException(404, "No such conversation")
    return {"id": conv_id, "messages": messages}


@router.delete("/api/chat/conversations/{conv_id}")
def delete_conversation(conv_id: str):
    if not srv.chat_store.delete(conv_id):
        raise HTTPException(404, "No such conversation")
    return {"deleted": conv_id}


@router.get("/api/chat/eval")
def eval_status():
    """The chatbot's exam: whether one is running, and the latest results."""
    return srv.chat_eval.status()


@router.post("/api/chat/eval")
def eval_start(body: EvalRequest):
    """Starts the exam in the background (it calls the model for every case, so it costs tokens)."""
    return srv.chat_eval.start(body.only or None)
