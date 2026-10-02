"""The chatbot's exam: real questions (evals/chat_cases.json) plus every 👎 the operator corrected, asked end to end
(retrieval, tools, model) and graded. Run it on demand after changing the prompt, the tools or the model: it costs
tokens. Results are kept (SQLite) so the Learning page can show whether a change made it better or worse.

Grading per case: the right kind of tool was used (any one of `tools`), every `expect` regex matches the answer and
no `forbid` one does, and it never stages a graph change. Corrected 👎 cases are graded by the fast model: does the
new answer agree with the operator's correction?"""

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

CASES = Path(__file__).with_name("evals") / "chat_cases.json"
MAX_CORRECTIONS = 20
JUDGE = {"max_tokens": 5, "temperature": 0, "reasoning": {"enabled": False}}
SCHEMA = """CREATE TABLE IF NOT EXISTS chat_eval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, model TEXT, passed INTEGER, total INTEGER,
    seconds REAL, tokens INTEGER, results TEXT)"""


def load_cases(path: Path = CASES) -> List[dict]:
    return [{**c, "source": "set"} for c in json.loads(Path(path).read_text())["cases"]]


def correction_cases(learner, limit: int = MAX_CORRECTIONS) -> List[dict]:
    """Every answer the operator marked wrong and corrected becomes a case: the next answer must agree."""
    if learner is None:
        return []
    rows = [f for f in learner.feedback_list(500) if f["kind"] == "answer" and f["rating"] < 0
            and f.get("source", "explicit") == "explicit" and f.get("correction") and f.get("question")]
    seen, out = set(), []
    for f in rows:
        key = f["question"].strip().lower()
        if key not in seen:
            seen.add(key)
            out.append({"id": f"correction-{f['id']}", "question": f["question"], "tools": [], "expect": [],
                        "reference": f["correction"], "source": "correction"})
    return out[:limit]


def ask(chatbot, question: str) -> dict:
    """One question, end to end, as the page would ask it (no earlier conversation)."""
    results, live = chatbot.retrieve(question, [])
    text, done, error, staged = [], {}, None, []
    for event in chatbot.stream(question, results, live, []):
        kind = event.get("type")
        if kind == "token":
            text.append(event["text"])
        elif kind == "done":
            done = event
        elif kind == "error":
            error = event.get("message")
        elif kind == "action_confirm":
            staged.append(event.get("action_id"))
    for action_id in staged:  # never leave an exam's change waiting for Allow
        try:
            list(chatbot.tools.cancel_action(action_id))
        except Exception:
            pass
    return {"answer": "".join(text).strip(), "tools": done.get("toolsUsed", []), "model": done.get("model"),
            "tokens": ((done.get("tokens") or {}).get("input") or 0) + ((done.get("tokens") or {}).get("output") or 0),
            "error": error, "staged": staged}


def judge(chatbot, question: str, answer: str, reference: str) -> bool:
    messages = [{"role": "system", "content": "You grade answers. Reply with exactly YES or NO."},
                {"role": "user", "content": f"Question: {question}\n\nThe operator said the right answer is: {reference}"
                                            f"\n\nNew answer: {answer[:3000]}\n\nDoes the new answer agree with the "
                                            "operator (same facts, no contradiction)?"}]
    reply = "".join(e.get("text", "") for e in chatbot.generate(messages, JUDGE) if e.get("type") == "token")
    return reply.strip().upper().startswith("YES")


def grade(case: dict, got: dict, chatbot=None) -> dict:
    problems = []
    if got["error"]:
        problems.append(f"error: {got['error']}")
    if got["staged"]:
        problems.append("staged a graph change nobody asked for")
    if case.get("tools") and not set(case["tools"]) & set(got["tools"]):
        problems.append(f"used {', '.join(got['tools']) or 'no tool'}; expected one of {', '.join(case['tools'])}")
    for rx in case.get("expect", []):
        if not re.search(rx, got["answer"], re.I):
            problems.append(f"answer lacks /{rx}/")
    for rx in case.get("forbid", []):
        if re.search(rx, got["answer"], re.I):
            problems.append(f"answer has /{rx}/")
    if case.get("reference") and not problems and chatbot is not None:
        try:
            if not judge(chatbot, case["question"], got["answer"], case["reference"]):
                problems.append("disagrees with the operator's correction")
        except Exception as e:
            problems.append(f"could not grade: {e}")
    return {"id": case["id"], "question": case["question"], "source": case.get("source", "set"),
            "passed": not problems, "problems": problems, "tools": got["tools"], "model": got["model"],
            "answer": got["answer"][:600]}


class ChatEval:
    """Runs the exam in the background (one at a time) and keeps every run's results."""

    def __init__(self, path: str, chatbot, learner=None):
        self.path, self.chatbot, self.learner = str(path), chatbot, learner
        self.lock = threading.Lock()
        self.progress: Optional[dict] = None
        with sqlite3.connect(self.path) as db:
            db.execute(SCHEMA)

    def start(self, only: Optional[List[str]] = None) -> dict:
        if not self.lock.acquire(blocking=False):
            return {"started": False, "progress": self.progress}
        cases = load_cases() + correction_cases(self.learner)
        if only:
            cases = [c for c in cases if c["id"] in only]
        self.progress = {"done": 0, "total": len(cases), "passed": 0, "started": time.time()}

        def work():
            try:
                self.run(cases)
            finally:
                self.progress = None
                self.lock.release()
        threading.Thread(target=work, name="chat-eval", daemon=True).start()
        return {"started": True, "total": len(cases)}

    def run(self, cases: List[dict], on_case: Optional[Callable[[dict], None]] = None) -> dict:
        started, results, tokens = time.time(), [], 0
        for case in cases:
            try:
                got = ask(self.chatbot, case["question"])
            except Exception as e:
                got = {"answer": "", "tools": [], "model": None, "tokens": 0, "error": str(e), "staged": []}
            tokens += got["tokens"]
            r = grade(case, got, self.chatbot)
            results.append(r)
            if self.progress is not None:
                self.progress = {**self.progress, "done": len(results), "passed": sum(x["passed"] for x in results)}
            if on_case:
                on_case(r)
        run = {"ts": time.time(), "model": self.chatbot.model, "passed": sum(r["passed"] for r in results),
               "total": len(results), "seconds": round(time.time() - started, 1), "tokens": tokens, "results": results}
        with sqlite3.connect(self.path) as db:
            run["id"] = db.execute("INSERT INTO chat_eval_runs (ts, model, passed, total, seconds, tokens, results) "
                                   "VALUES (?,?,?,?,?,?,?)", (run["ts"], run["model"], run["passed"], run["total"],
                                                              run["seconds"], tokens, json.dumps(results))).lastrowid
        return run

    def runs(self, limit: int = 10) -> List[dict]:
        """The latest runs, newest first; the newest with its per-case results."""
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM chat_eval_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for i, r in enumerate(rows):
            item = {k: r[k] for k in ("id", "ts", "model", "passed", "total", "seconds", "tokens")}
            if i == 0:
                item["results"] = json.loads(r["results"] or "[]")
            out.append(item)
        return out

    def status(self) -> Dict:
        return {"running": self.progress, "runs": self.runs(), "cases": len(load_cases()),
                "corrections": len(correction_cases(self.learner))}


if __name__ == "__main__":  # python chat_eval.py [case-id ...]   (inside the app container)
    import sys
    import server as srv
    ev = ChatEval(srv.metrics.path, srv.chatbot, srv.learner)
    picked = [c for c in load_cases() + correction_cases(srv.learner) if not sys.argv[1:] or c["id"] in sys.argv[1:]]
    result = ev.run(picked, on_case=lambda r: print(("PASS " if r["passed"] else "FAIL ") + r["id"],
                                                    "" if r["passed"] else "· " + "; ".join(r["problems"]),
                                                    f"[{', '.join(r['tools'])}]", flush=True))
    print(f"\n{result['passed']}/{result['total']} passed in {result['seconds']} s, {result['tokens']} tokens")
