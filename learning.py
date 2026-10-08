"""What the agent learns from past data, kept in SQLite next to the metrics history (METRICS_DB).

- Incident memory: every closed outage (an OUTAGE entry and the RECOVERY that ended it) becomes a short case:
  node, category, where the fault was (the correlation verdict), the likely root cause, how long it lasted, and
  what fixed it once the operator says. Cases are built from the incident log on demand, so outages recorded
  before this existed count too.
- Feedback: 👍/👎 on answers (a 👎 can carry the right answer, kept as a lesson for similar questions), and
  Right/Wrong on the likely root cause. Root-cause verdicts become per-node priors that rca_rank multiplies into
  its scores, so a ranking the operator corrected doesn't come back the same way.
- Operator facts: things the operator tells the agent to remember ("xcode2 restarts every night at 03:00").
- Patterns: computed from the cases: when a node usually fails, how often, for how long, and which nodes fail
  together (and which one first). Plain counting; no model to train, so it works with a handful of incidents.

Questions are matched by embedding (the shared MiniLM). With a few hundred rows a NumPy dot product is enough.
"""

import json
from outage_class import BACKUP_FAILURE, BLIP, BLIP_S, NOT_OUTAGES, classify
import os
import re
import sqlite3
import statistics
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

import numpy as np

IST = timezone(timedelta(hours=5, minutes=30))
CASE_SYNC_S = 60  # rebuild cases from the incident log at most this often
PATTERN_CACHE_S = 300
TOGETHER_S = 180  # outages starting this close together count as failing together
TOGETHER_LIFT = 2.0  # ...but only when that happens at least this many times more often than chance
FIRST_SHARE = 0.7  # "X usually goes first" only when X really is first at least this often
MIN_PATTERN = 2  # occurrences before something counts as a pattern
SIMILAR_MIN = 0.45  # cosine (plus bonuses) from which a past case counts as similar
LESSON_MIN = 0.6  # cosine between questions from which a past correction applies
EXAMPLE_MIN = 0.7  # cosine between questions from which a 👍 answer is offered as an example
REPHRASE_MIN = 0.8  # the next question this close to the last one, right away: the last answer probably missed
MAX_CASES, MAX_LESSONS, MAX_FACTS, MAX_PATTERNS = 3, 2, 8, 6

# Incident categories in plain words (pattern text is shown to people as well as to the model).
PROBLEM_WORDS = {
    "STALE_MEDIA": "the video stopped updating", "PLAYLIST_MISSING": "the playlist was missing",
    "UNREACHABLE": "the server could not be reached", "SERVER_ERROR": "server errors", "HTTP_ERROR": "HTTP errors",
    "NO_SEGMENTS": "no video pieces", "INVALID_PLAYLIST": "a broken playlist", "down": "down",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    node TEXT NOT NULL, opened TEXT NOT NULL, closed TEXT, category TEXT, verdict TEXT, root_cause TEXT,
    duration_s INTEGER, channels TEXT, text TEXT NOT NULL, resolution TEXT, vec BLOB, onset TEXT,
    PRIMARY KEY (node, opened));
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL, rating INTEGER NOT NULL,
    node TEXT, question TEXT, answer TEXT, correction TEXT, vec BLOB);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, text TEXT NOT NULL, nodes TEXT, vec BLOB);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, node TEXT NOT NULL, type TEXT NOT NULL,
    category TEXT, entry TEXT NOT NULL, UNIQUE (node, ts, type));
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dt(iso: Optional[str]) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def short(node: str) -> str:
    return str(node or "").replace(".ottlive.co.in", "").replace(".co.in", "")


def duration_words(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown time"
    s = int(seconds)
    return f"{s} s" if s < 90 else f"{round(s / 60)} min" if s < 5400 else f"{s / 3600:.1f} h"


def when_words(iso: Optional[str]) -> str:
    d = _dt(iso)
    return d.astimezone(IST).strftime("%d %b %H:%M IST") if d else "unknown time"


class Learner:
    upstream_of: Optional[Callable[[], Dict[str, Iterable[str]]]] = None  # node -> its upstream nodes (pipeline)

    def __init__(self, path: str, embed: Optional[Callable[[List[str]], "np.ndarray"]] = None):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self._embed = embed
        self._synced_at = 0.0
        self._patterns: Optional[List[dict]] = None
        self._patterns_at = 0.0
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            fb_cols = {r["name"] for r in db.execute("PRAGMA table_info(feedback)")}
            for col in ("tools", "source"):  # added later: which tools an answer used; explicit 👍/👎 or implicit
                if col not in fb_cols:
                    db.execute(f"ALTER TABLE feedback ADD COLUMN {col} TEXT")
            if "onset" not in {r["name"] for r in db.execute("PRAGMA table_info(cases)")}:
                # Added later: rebuild the cases (none have a fix recorded yet) so they get their onset.
                db.execute("ALTER TABLE cases ADD COLUMN onset TEXT")
                db.execute("DELETE FROM cases WHERE resolution IS NULL")
            if "class" not in {r["name"] for r in db.execute("PRAGMA table_info(cases)")}:
                # Added later (outage_class.py): rebuild the cases from the incident log so each gets its class;
                # one with a fix recorded keeps its row and is classed by how long it lasted.
                db.execute("ALTER TABLE cases ADD COLUMN class TEXT")
                db.execute("DELETE FROM cases WHERE resolution IS NULL")
                db.execute("UPDATE cases SET class = CASE WHEN duration_s < ? THEN 'BLIP' ELSE 'OUTAGE' END",
                           (BLIP_S,))

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def embed(self, texts: List[str]) -> np.ndarray:
        if self._embed is None:
            import text_embedding
            self._embed = text_embedding.encode
        vecs = np.asarray(self._embed(texts), dtype="float32")
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        return vecs / np.where(norms == 0, 1, norms)

    def _blob(self, text: str) -> Optional[bytes]:
        try:
            return self.embed([text])[0].tobytes()
        except Exception:
            return None  # stored without a vector; still found by node name

    @staticmethod
    def _sim(query: Optional[np.ndarray], blob: Optional[bytes]) -> float:
        if query is None or not blob:
            return 0.0
        vec = np.frombuffer(blob, dtype="float32")
        return float(vec @ query) if vec.shape == query.shape else 0.0

    # --- incident memory ---

    def sync_cases(self, force: bool = False) -> int:
        """Turns closed outages in the incident log into cases (new ones only). Returns how many were added."""
        if not force and time.time() - self._synced_at < CASE_SYNC_S:
            return 0
        self._synced_at = time.time()
        with self._connect() as db:
            rows = db.execute("SELECT node, ts, type, entry FROM incidents WHERE type IN ('OUTAGE', 'RECOVERY') "
                              "ORDER BY node, ts, id").fetchall()
            known = {(r["node"], r["opened"]) for r in db.execute("SELECT node, opened FROM cases")}
        new, opened = [], {}
        for r in rows:
            if r["type"] == "OUTAGE":
                opened[r["node"]] = r
            elif r["node"] in opened:
                start = opened.pop(r["node"])
                if (r["node"], start["ts"]) not in known:
                    new.append(self._case(r["node"], json.loads(start["entry"]), json.loads(r["entry"])))
        if not new:
            return 0
        vecs = self._vectors([c["text"] for c in new])
        with self.lock, self._connect() as db:
            db.executemany(
                "INSERT OR IGNORE INTO cases (node, opened, closed, category, verdict, root_cause, duration_s, "
                "channels, text, vec, onset, class) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [(c["node"], c["opened"], c["closed"], c["category"], c["verdict"], c["root_cause"], c["duration_s"],
                  json.dumps(c["channels"]), c["text"], v, c["onset"], c["class"]) for c, v in zip(new, vecs)])
        self._patterns = None
        return len(new)

    def _vectors(self, texts: List[str]) -> List[Optional[bytes]]:
        try:
            return [v.tobytes() for v in self.embed(texts)]
        except Exception:
            return [None] * len(texts)

    @staticmethod
    def _case(node: str, outage: dict, recovery: dict) -> dict:
        correlation = outage.get("correlation") or []
        verdicts = sorted({c.get("verdict") for c in correlation if c.get("verdict")})
        channels = sorted({c.get("channel") for c in correlation if c.get("channel")})
        ranking = outage.get("rootCauseRanking") or []
        root = ranking[0]["node"] if ranking else None
        # When the stream really stopped (estimated from segment times) beats when the check noticed it.
        onset = next((r["onsetAt"] for r in ranking if r.get("node") == node and r.get("onsetAt")), None)
        duration = recovery.get("durationS")
        if duration is None and _dt(outage.get("timestamp")) and _dt(recovery.get("timestamp")):
            duration = int((_dt(recovery["timestamp"]) - _dt(outage["timestamp"])).total_seconds())
        category = outage.get("category") or recovery.get("category")
        kind = recovery.get("class") or classify(outage, duration)
        noun = {BLIP: "blip", BACKUP_FAILURE: "backup feed failure"}.get(kind, "outage")
        text = (f"{node} {noun} ({category or 'down'})"
                + (f" on {', '.join(channels)}" if channels else "")
                + (f"; fault {', '.join(verdicts).lower().replace('_', ' ')}" if verdicts else "")
                + (f"; likely root cause {root}" if root and root != node else "")
                + f"; error: {outage.get('lastError') or 'none'}"
                + f"; lasted {duration_words(duration)}; started {when_words(outage.get('timestamp'))}")
        return {"node": node, "opened": outage.get("timestamp"), "closed": recovery.get("timestamp"),
                "category": category, "verdict": ", ".join(verdicts) or None, "root_cause": root,
                "duration_s": duration, "channels": channels, "text": text, "onset": onset or outage.get("timestamp"),
                "class": kind}

    def cases(self, node: Optional[str] = None, limit: int = 20) -> List[dict]:
        self.sync_cases()
        sql, args = "SELECT * FROM cases", []
        if node:
            sql, args = sql + " WHERE node = ?", [node]
        with self._connect() as db:
            rows = db.execute(sql + " ORDER BY opened DESC LIMIT ?", (*args, limit)).fetchall()
        return [self._case_row(r) for r in rows]

    @staticmethod
    def _case_row(r) -> dict:
        out = {k: r[k] for k in r.keys() if k != "vec"}
        out["channels"] = json.loads(out.get("channels") or "[]")
        return out

    def set_resolution(self, node: str, resolution: str, opened: Optional[str] = None) -> Optional[dict]:
        """Records what fixed an outage (the node's latest closed one unless `opened` names it)."""
        self.sync_cases(force=True)
        with self.lock, self._connect() as db:
            row = db.execute("SELECT * FROM cases WHERE node = ? " + ("AND opened = ? " if opened else "")
                             + "ORDER BY opened DESC LIMIT 1", (node, opened) if opened else (node,)).fetchone()
            if not row:
                return None
            text = row["text"].split("; fixed by: ")[0] + f"; fixed by: {resolution}"
            db.execute("UPDATE cases SET resolution = ?, text = ?, vec = ? WHERE node = ? AND opened = ?",
                       (resolution, text, self._blob(text), row["node"], row["opened"]))
        return {**self._case_row(row), "resolution": resolution, "text": text}

    def similar_cases(self, query_vec: Optional[np.ndarray], nodes: Iterable[str] = (),
                      categories: Iterable[str] = (), limit: int = MAX_CASES) -> List[dict]:
        self.sync_cases()
        nodes, categories = set(nodes), set(categories)
        with self._connect() as db:
            rows = db.execute("SELECT * FROM cases ORDER BY opened DESC LIMIT 500").fetchall()
        scored = []
        for r in rows:
            score = self._sim(query_vec, r["vec"])
            score += 0.3 if r["node"] in nodes or r["root_cause"] in nodes else 0
            score += 0.15 if r["category"] in categories else 0
            if score >= SIMILAR_MIN:
                scored.append((score, r))
        scored.sort(key=lambda s: (-s[0], s[1]["opened"]))
        return [{**self._case_row(r), "score": round(s, 3)} for s, r in scored[:limit]]

    # --- feedback ---

    def add_feedback(self, kind: str, rating: int, node: Optional[str] = None, question: Optional[str] = None,
                     answer: Optional[str] = None, correction: Optional[str] = None, tools: Optional[List[str]] = None,
                     source: str = "explicit") -> dict:
        """kind "answer" (rating ±1 on a reply; a correction is kept as a lesson; a 👍 answer becomes an example and
        its tools are remembered for similar questions) or "root_cause" (±1: was `node` the root cause).
        source "implicit": inferred, e.g. the operator asked the same thing again straight away."""
        if kind not in ("answer", "root_cause") or rating not in (1, -1):
            raise ValueError("kind must be answer or root_cause, rating 1 or -1")
        vec = self._blob(question) if question and kind == "answer" else None
        with self.lock, self._connect() as db:
            cur = db.execute("INSERT INTO feedback (ts, kind, rating, node, question, answer, correction, vec, tools, "
                             "source) VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (_now(), kind, rating, node, question, (answer or "")[:2000], correction, vec,
                              json.dumps(sorted(set(tools or []))) if tools else None, source))
        return {"id": cur.lastrowid, "kind": kind, "rating": rating, "node": node, "source": source}

    def good_examples(self, query_vec: Optional[np.ndarray], limit: int = 2) -> List[dict]:
        """Answers the operator rated 👍 for questions like this one: shown to the model as examples to follow."""
        with self._connect() as db:
            rows = db.execute("SELECT * FROM feedback WHERE kind = 'answer' AND rating = 1 AND vec IS NOT NULL "
                              "AND COALESCE(source, 'explicit') = 'explicit' ORDER BY id DESC LIMIT 300").fetchall()
        scored = sorted(((self._sim(query_vec, r["vec"]), r) for r in rows), key=lambda x: -x[0])
        return [{"question": r["question"], "answer": (r["answer"] or "")[:600], "tools": json.loads(r["tools"] or "[]"),
                 "score": round(sc, 3)} for sc, r in scored if sc >= EXAMPLE_MIN][:limit]

    def tools_for_question(self, question: str) -> List[str]:
        """Tools used in 👍 answers to similar questions (the chatbot always offers them for this one)."""
        try:
            qvec = self.embed([question])[0]
        except Exception:
            return []
        return sorted({t for e in self.good_examples(qvec, limit=5) for t in e["tools"]})

    def note_rephrase(self, previous_question: str, previous_answer: str, new_question: str,
                      tools: Optional[List[str]] = None) -> bool:
        """The operator asked nearly the same thing again right away: the previous answer probably missed. Kept as
        an implicit 👎 (counted separately; it never becomes a correction or an example)."""
        try:
            a, b = self.embed([previous_question, new_question])
        except Exception:
            return False
        if float(a @ b) < REPHRASE_MIN:
            return False
        self.add_feedback("answer", -1, question=previous_question, answer=previous_answer, tools=tools, source="implicit")
        return True

    def priors(self) -> Dict[str, float]:
        """Per-node factor for the root-cause ranking from the operator's Right/Wrong verdicts: 1.0 with no
        verdicts, up to 1.6 when confirmed, down to 0.5 when repeatedly wrong (Laplace-smoothed)."""
        with self._connect() as db:
            rows = db.execute("SELECT node, SUM(rating > 0) AS yes, SUM(rating < 0) AS no FROM feedback "
                              "WHERE kind = 'root_cause' AND node IS NOT NULL GROUP BY node").fetchall()
        return {r["node"]: round(min(1.6, max(0.5, 2 * (r["yes"] + 1) / (r["yes"] + r["no"] + 2))), 3)
                for r in rows}

    OUTAGE_PREFIX = "outage:"  # feedback.question of a one-tap outage rating: "outage:<blamed node>|<onset>"
    RATING_TARGET = 50  # rated outages before the accuracy figure means much

    def rate_outage(self, key: str, blamed: str, right: bool, real: Optional[str] = None) -> dict:
        """One tap on "Was this the right cause?" for one outage. Right: a 👍 for the blamed server. Wrong: a 👎 for it,
        and a 👍 for the real culprit when the operator picks one (real may also be "network" or "other": no server
        to credit). Rating the same outage again replaces the earlier answer. Feeds priors() like any verdict."""
        question = self.OUTAGE_PREFIX + key
        real = (real or "").strip() or None
        with self.lock, self._connect() as db:
            db.execute("DELETE FROM feedback WHERE kind = 'root_cause' AND question = ?", (question,))
            rows = [(blamed, 1 if right else -1, None if right else real)]
            if not right and real and real not in ("network", "other") and real != blamed:
                rows.append((real, 1, None))
            for node, rating, correction in rows:
                db.execute("INSERT INTO feedback (ts, kind, rating, node, question, answer, correction, source) "
                           "VALUES (?, 'root_cause', ?, ?, ?, ?, ?, 'explicit')",
                           (_now(), rating, node, question, f"blamed {blamed}", correction))
        return {"key": key, "blamed": blamed, "right": right, "real": real, **self.outage_ratings(keys_only=False)}

    def outage_ratings(self, keys_only: bool = False) -> dict:
        """Progress towards RATING_TARGET rated outages, the accuracy so far, and each rated outage's answer
        ({key: {"right", "real"}}, newest 300) so every screen shows it as answered."""
        with self._connect() as db:
            rows = db.execute("SELECT question, rating, correction, node, answer FROM feedback WHERE kind = 'root_cause' "
                              "AND question LIKE ? ORDER BY id DESC", (self.OUTAGE_PREFIX + "%",)).fetchall()
        answers: Dict[str, dict] = {}
        for r in rows:
            key = r["question"][len(self.OUTAGE_PREFIX):]
            if f"blamed {r['node']}" != r["answer"]:
                continue  # the 👍 for the real culprit of a wrong call: the blamed row carries the answer
            answers.setdefault(key, {"right": r["rating"] > 0, "real": r["correction"]})
        right = sum(a["right"] for a in answers.values())
        out = {"rated": len(answers), "right": right, "wrong": len(answers) - right, "target": self.RATING_TARGET,
               "accuracy": round(right / len(answers), 3) if answers else None}
        if not keys_only:
            out["answers"] = dict(list(answers.items())[:300])
        return out

    def verdicts(self) -> Dict[str, Dict[str, int]]:
        with self._connect() as db:
            rows = db.execute("SELECT node, SUM(rating > 0) AS yes, SUM(rating < 0) AS no FROM feedback "
                              "WHERE kind = 'root_cause' AND node IS NOT NULL GROUP BY node").fetchall()
        return {r["node"]: {"right": r["yes"], "wrong": r["no"]} for r in rows}

    def lessons(self, query_vec: Optional[np.ndarray], limit: int = MAX_LESSONS) -> List[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM feedback WHERE kind = 'answer' AND correction IS NOT NULL "
                              "AND correction != '' ORDER BY id DESC LIMIT 300").fetchall()
        scored = [(self._sim(query_vec, r["vec"]), r) for r in rows]
        scored = sorted((s for s in scored if s[0] >= LESSON_MIN), key=lambda s: -s[0])[:limit]
        return [{"question": r["question"], "correction": r["correction"], "ts": r["ts"], "score": round(s, 3)}
                for s, r in scored]

    def feedback_stats(self) -> dict:
        with self._connect() as db:
            rows = db.execute("SELECT kind, rating, COALESCE(source, 'explicit') AS source, COUNT(*) AS n FROM feedback "
                              "GROUP BY kind, rating, source").fetchall()
        out = {"answer": {"up": 0, "down": 0, "rephrased": 0}, "root_cause": {"up": 0, "down": 0}}
        for r in rows:
            if r["source"] == "implicit":
                out["answer"]["rephrased"] += r["n"]
            else:
                out[r["kind"]]["up" if r["rating"] > 0 else "down"] += r["n"]
        return out

    # --- operator facts ---

    def add_fact(self, text: str, known_nodes: Iterable[str] = ()) -> dict:
        text = " ".join(str(text or "").split())
        if not text:
            raise ValueError("empty fact")
        lowered = text.lower()
        nodes = sorted({n for n in known_nodes if n.lower() in lowered
                        or len(n.split(".")[0]) > 2 and re.search(rf"\b{re.escape(n.split('.')[0].lower())}\b", lowered)})
        with self.lock, self._connect() as db:
            dup = db.execute("SELECT id FROM facts WHERE lower(text) = ?", (lowered,)).fetchone()
            if dup:
                return {"id": dup["id"], "text": text, "nodes": nodes, "duplicate": True}
            cur = db.execute("INSERT INTO facts (ts, text, nodes, vec) VALUES (?,?,?,?)",
                             (_now(), text, json.dumps(nodes), self._blob(text)))
        return {"id": cur.lastrowid, "text": text, "nodes": nodes}

    def facts(self) -> List[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT id, ts, text, nodes FROM facts ORDER BY id").fetchall()
        return [{"id": r["id"], "ts": r["ts"], "text": r["text"], "nodes": json.loads(r["nodes"] or "[]")} for r in rows]

    def forget_fact(self, fact_id: Optional[int] = None, text: Optional[str] = None) -> List[dict]:
        """Deletes a fact by id, or every fact containing `text`. Returns what was deleted."""
        with self.lock, self._connect() as db:
            if fact_id is not None:
                rows = db.execute("SELECT id, text FROM facts WHERE id = ?", (int(fact_id),)).fetchall()
            elif text:
                rows = db.execute("SELECT id, text FROM facts WHERE lower(text) LIKE ?",
                                  (f"%{text.lower()}%",)).fetchall()
            else:
                rows = []
            db.executemany("DELETE FROM facts WHERE id = ?", [(r["id"],) for r in rows])
        return [{"id": r["id"], "text": r["text"]} for r in rows]

    def relevant_facts(self, query_vec: Optional[np.ndarray], nodes: Iterable[str] = (),
                       limit: int = MAX_FACTS) -> List[dict]:
        nodes = set(nodes)
        with self._connect() as db:
            rows = db.execute("SELECT * FROM facts").fetchall()
        if len(rows) <= limit:  # few enough to always include: the operator said them for a reason
            return [{"id": r["id"], "text": r["text"]} for r in rows]
        scored = [(self._sim(query_vec, r["vec"]) + (0.4 if nodes & set(json.loads(r["nodes"] or "[]")) else 0), r)
                  for r in rows]
        scored.sort(key=lambda s: -s[0])
        return [{"id": r["id"], "text": r["text"]} for _, r in scored[:limit]]

    # --- patterns ---

    def _upstream(self) -> Dict[str, set]:
        """node -> every node upstream of it in the pipeline (set by the server from the graph; empty in tests)."""
        try:
            return {k: set(v) for k, v in (self.upstream_of() if self.upstream_of else {}).items()}
        except Exception:
            return {}

    def patterns(self) -> List[dict]:
        """Recurring things in the closed outages, strongest first: {"kind", "nodes", "count", "text"}."""
        self.sync_cases()
        if self._patterns is not None and time.time() - self._patterns_at < PATTERN_CACHE_S:
            return self._patterns
        with self._connect() as db:
            every = [dict(r) for r in db.execute("SELECT node, coalesce(onset, opened) AS opened, category, duration_s, "
                                                 "class FROM cases ORDER BY opened")]
        # Blips and backup-feed failures are not outages (outage_class.py): they'd make a server with one noisy
        # backup look like the worst one, and pair it with everything by chance. Counted apart, said once.
        rows = [r for r in every if r["class"] not in NOT_OUTAGES]
        found: List[dict] = []
        noise: Dict[str, Counter] = {}
        for r in every:
            if r["class"] in NOT_OUTAGES:
                noise.setdefault(r["node"], Counter())[r["class"]] += 1
        for node, c in noise.items():
            if sum(c.values()) >= MIN_PATTERN:
                parts = [f"{c[BACKUP_FAILURE]} backup-feed failure{'s' if c[BACKUP_FAILURE] != 1 else ''}"] if c[BACKUP_FAILURE] else []
                parts += [f"{c[BLIP]} short blip{'s' if c[BLIP] != 1 else ''} (back within a minute)"] if c[BLIP] else []
                found.append({"kind": "noise", "nodes": [node], "count": sum(c.values()),
                              "text": f"{short(node)} also had {' and '.join(parts)}: not counted as outages."})
        by_node: Dict[str, List[dict]] = {}
        for r in rows:
            if _dt(r["opened"]):
                by_node.setdefault(r["node"], []).append(r)

        for node, cs in by_node.items():
            n = len(cs)
            if n < MIN_PATTERN:
                continue
            durations = [c["duration_s"] for c in cs if c["duration_s"] is not None]
            category, cat_n = Counter(c["category"] or "down" for c in cs).most_common(1)[0]
            text = f"{short(node)} has had {n} outages"
            if durations:
                text += f", usually lasting about {duration_words(statistics.median(durations))}"
            if cat_n / n >= 0.6:
                text += f", mostly because {PROBLEM_WORDS.get(category, category)}"
            found.append({"kind": "history", "nodes": [node], "count": n, "text": text + "."})

            hours = [_dt(c["opened"]).astimezone(IST).hour for c in cs]
            best = max(sorted(set(hours)), key=lambda h: sum((x - h) % 24 < 2 for x in hours))  # windows start at a real hour
            hits = sum((x - best) % 24 < 2 for x in hours)
            if hits >= max(MIN_PATTERN + 1, 0.6 * n):
                found.append({"kind": "time_of_day", "nodes": [node], "count": hits,
                              "text": f"{short(node)} usually fails between {best:02d}:00 and {(best + 2) % 24:02d}:00 IST "
                                      f"({hits} of {n} outages)."})

            if n >= 3:
                starts = [_dt(c["opened"]).timestamp() for c in cs]
                gaps = [b - a for a, b in zip(starts, starts[1:]) if b > a]
                if gaps:
                    found.append({"kind": "recurrence", "nodes": [node], "count": n,
                                  "text": f"{short(node)} fails about every {duration_words(statistics.median(gaps))}."})

        # Nodes failing together: outages on different nodes that start within TOGETHER_S, and which came first.
        # A server that fails every few minutes lands near any other outage by pure chance, so a pair only counts
        # when it happens TOGETHER_LIFT times more often than chance (expected = n_a * n_b * 2 * TOGETHER_S / span).
        # Who leads: the pipeline (an input before what it feeds) when the two are connected; otherwise the timing,
        # and only when it is consistent.
        pairs: Counter = Counter()
        first: Counter = Counter()
        lag: Dict[tuple, List[float]] = {}
        stamped = sorted(((_dt(r["opened"]).timestamp(), r["node"]) for r in rows if _dt(r["opened"])))
        for i, (ta, a) in enumerate(stamped):
            for tb, b in stamped[i + 1:]:
                if tb - ta > TOGETHER_S:
                    break
                if a != b:
                    key = tuple(sorted((a, b)))
                    pairs[key] += 1
                    first[(a, b)] += 1
                    lag.setdefault(key, []).append(tb - ta)
        span = max(86400.0, stamped[-1][0] - stamped[0][0]) if stamped else 86400.0
        counts = Counter(node for _, node in stamped)
        upstream = self._upstream()
        for (a, b), n in pairs.items():
            if n < MIN_PATTERN:
                continue
            expected = counts[a] * counts[b] * 2 * TOGETHER_S / span
            lift = n / expected if expected else float("inf")
            if lift < TOGETHER_LIFT:
                continue  # no more often than two frequent failers would meet by chance
            a_feeds_b, b_feeds_a = a in upstream.get(b, ()), b in upstream.get(a, ())
            by_time = (a, b) if first[(a, b)] >= first[(b, a)] else (b, a)
            share = first[by_time] / n
            lift_words = f"{lift:.0f}x more often than chance" if lift < 100 else "far more often than chance"
            text = f"{short(a)} and {short(b)} have failed together {n} times ({lift_words})"
            if a_feeds_b or b_feeds_a:
                lead, follow = (a, b) if a_feeds_b else (b, a)
                text += f"; {short(lead)} feeds {short(follow)}, so {short(lead)} is the likely cause"
                if by_time == (lead, follow) and share >= FIRST_SHARE:
                    text += f" (it fails first, {short(follow)} follows within " \
                            f"{duration_words(statistics.median(lag[(a, b)]) or 1)})"
            else:
                lead, follow = by_time
                if share >= FIRST_SHARE:
                    text += f"; {short(lead)} usually goes first and {short(follow)} follows within " \
                            f"{duration_words(statistics.median(lag[(a, b)]) or 1)}"
                else:
                    text += "; neither one consistently fails first"
            found.append({"kind": "together", "nodes": [lead, follow], "count": n, "lift": round(lift, 1),
                          "text": text + "."})
        # Category MTTR (Mean Time to Recovery) patterns
        try:
            from metrics import store as metrics_store
            mttr_stats = metrics_store(self.path).mttr_by_category()  # this Learner's own file
            for cat, stat in mttr_stats.items():
                if stat["count"] >= 2:
                    cat_name = PROBLEM_WORDS.get(cat, cat)
                    med_w = duration_words(stat["median_s"])
                    found.append({
                        "kind": "mttr",
                        "nodes": [],
                        "count": stat["count"],
                        "text": f"Outages caused by {cat_name} have a median recovery time (MTTR) of {med_w} (observed across {stat['count']} recovery cycles)."
                    })
        except Exception:
            pass

        found.sort(key=lambda p: (-p["count"], p["text"]))
        self._patterns, self._patterns_at = found, time.time()
        return found

    # --- what goes into an answer ---

    def context(self, question: str, nodes: Iterable[str] = (), failing: Iterable[str] = (),
                categories: Iterable[str] = ()) -> dict:
        """Everything learned that bears on this question: operator facts, corrections of similar past answers,
        similar past outages, and the patterns of the nodes in play. {"text", "facts", "lessons", "cases",
        "patterns"}; text is "" when there's nothing."""
        nodes, failing = list(nodes), list(failing)
        in_play = set(nodes) | set(failing)
        try:
            qvec = self.embed([question])[0]
        except Exception:
            qvec = None
        facts = self.relevant_facts(qvec, in_play)
        lessons = self.lessons(qvec)
        examples = self.good_examples(qvec)
        cases = self.similar_cases(qvec, in_play, categories)
        patterns = self.patterns()
        mine = [p for p in patterns if in_play & set(p["nodes"])]
        patterns = (mine + [p for p in patterns if p not in mine])[:MAX_PATTERNS]

        lines = []
        if facts:
            lines.append("Facts the operator told you to remember:")
            lines += [f"- {f['text']}" for f in facts]
        if lessons:
            lines.append("Corrections the operator gave to earlier answers (follow them):")
            lines += [f"- Asked \"{l['question']}\", the right answer was: {l['correction']}" for l in lessons]
        if examples:
            lines.append("Answers the operator rated good for similar questions (follow their approach, with today's data):")
            lines += [f"- Asked \"{e['question']}\"" + (f" (tools: {', '.join(e['tools'])})" if e["tools"] else "")
                      + f", answered: {e['answer']}" for e in examples]
        if cases:
            lines.append("Similar past outages (from the incident history):")
            lines += [f"- {c['text']}" + ("" if c.get("resolution") else "; fix not recorded") for c in cases]
        if patterns:
            lines.append("Patterns learned from past outages:")
            lines += [f"- {p['text']}" for p in patterns]
        verdicts = {n: v for n, v in self.verdicts().items() if n in in_play}
        for n, v in verdicts.items():
            lines.append(f"- The operator confirmed {n} as the root cause {v['right']} time(s) and rejected it "
                         f"{v['wrong']} time(s).")
        text = ("What you learned from past data (cite it when it helps, e.g. \"this looks like the outage on …\"):\n"
                + "\n".join(lines)) if lines else ""
        return {"text": text, "facts": facts, "lessons": lessons, "examples": examples, "cases": cases,
                "patterns": patterns}

    def summary(self) -> dict:
        """For the page: facts, patterns, recent cases, feedback counts and the ranking priors."""
        return {"facts": self.facts(), "patterns": self.patterns()[:12], "cases": self.cases(limit=10),
                "caseCount": self.case_count(), "feedback": self.feedback_stats(), "priors": self.priors()}

    def feedback_list(self, limit: int = 100) -> List[dict]:
        """The latest feedback, newest first (answers with their question and correction, root-cause verdicts)."""
        with self._connect() as db:
            rows = db.execute("SELECT id, ts, kind, rating, node, question, answer, correction, tools, "
                              "COALESCE(source, 'explicit') AS source FROM feedback ORDER BY id DESC LIMIT ?",
                              (limit,)).fetchall()
        return [{**dict(r), "tools": json.loads(r["tools"] or "[]")} for r in rows]

    def everything(self) -> dict:
        """Everything learned, for the Learning page: all patterns, cases, facts, lessons, feedback and the
        root-cause weights with the verdicts behind them."""
        priors, verdicts = self.priors(), self.verdicts()
        weights = [{"node": n, "factor": priors.get(n, 1.0), **v} for n, v in verdicts.items()]
        feedback = self.feedback_list(200)
        return {
            "facts": self.facts(), "patterns": self.patterns(), "cases": self.cases(limit=500),
            "caseCount": self.case_count(), "feedback": self.feedback_stats(),
            "weights": sorted(weights, key=lambda w: -w["factor"]),
            "lessons": [f for f in feedback if f["kind"] == "answer" and f["correction"]],
            "recentFeedback": feedback[:50],
            "settings": {"togetherS": TOGETHER_S, "minPattern": MIN_PATTERN, "similarMin": SIMILAR_MIN,
                         "lessonMin": LESSON_MIN},
        }

    def case_count(self) -> int:
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM cases").fetchone()[0]

    def get_dynamic_alert_policy(self) -> Dict[str, Any]:
        """Calculates adaptive dynamic hold-down debounce periods and cascading suppression rules
        by synthesizing closed outage cases, recurring flapping intervals, and paired lead-follow patterns.
        """
        self.sync_cases()
        pats = self.patterns()

        # 1. Cascading relationships (lead -> list of followers)
        cascade_graph: Dict[str, List[Dict[str, Any]]] = {}
        for p in pats:
            if p.get("kind") == "together" and len(p.get("nodes", [])) == 2:
                lead, follow = p["nodes"][0], p["nodes"][1]
                cnt = p.get("count", 0)
                m = re.search(r"follows within (\d+)\s*(s|min)", p.get("text", ""))
                lag_s = 60
                if m:
                    val = int(m.group(1))
                    unit = m.group(2)
                    lag_s = val * 60 if unit == "min" else val
                cascade_graph.setdefault(short(lead), []).append({
                    "follower": short(follow),
                    "full_follower": follow,
                    "lead": short(lead),
                    "full_lead": lead,
                    "count": cnt,
                    "lag_s": lag_s,
                    "suppress_window_s": min(180, lag_s + 45)
                })

        # 2. Node profiles (duration, flapping, hold-down)
        node_profiles: Dict[str, Dict[str, Any]] = {}
        with self._connect() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT node, duration_s, category, resolution, coalesce(onset, opened) AS opened FROM cases"
            )]

        by_node: Dict[str, List[dict]] = {}
        for r in rows:
            by_node.setdefault(r["node"], []).append(r)

        seg_stats_by_node: Dict[str, List[dict]] = {}
        try:
            from metrics import store as metrics_store
            mstore = metrics_store(self.path)
            for sa in mstore.segment_alerts():
                n_key = sa.get("node") or ""
                s_key = short(n_key)
                seg_stats_by_node.setdefault(s_key, []).append(sa)
        except Exception:
            pass

        for node, cs in by_node.items():
            s_name = short(node)
            durations = [c["duration_s"] for c in cs if c["duration_s"] is not None]
            outage_cnt = len(cs)
            med_dur = statistics.median(durations) if durations else 20.0

            seg_list = seg_stats_by_node.get(s_name, [])
            total_raised = sum(sa.get("raised", 0) for sa in seg_list)
            total_settled = sum(sa.get("clearedAlone", 0) for sa in seg_list)
            settled_ratio = (total_settled / total_raised) if total_raised >= 5 else 0.5

            # Dynamic Hold-Down calculation:
            # If high self-settling or median outage is transient (<= 40s) with high frequency
            is_flapper = (outage_cnt >= 10 and med_dur <= 40) or (settled_ratio >= 0.75)

            if is_flapper:
                hold_down_s = int(min(45, max(25, med_dur + 5)))
            elif outage_cnt >= 3 and med_dur <= 20:
                hold_down_s = int(min(25, max(15, med_dur + 3)))
            else:
                hold_down_s = 5

            # Dynamic Audio Tuning learned from past MTTR, frequency & operator actions:
            if med_dur <= 35:
                # Short transient outage (e.g. ingest1 MTTR 27s): fast cadence, brief, anti-fatigue
                speech_rate = 1.25
                verbosity = "brief"
                siren_notes = [440.0, 554.37]
                speech_pitch = 0.95 if is_flapper else 1.02
                chime_vol = 0.15
            elif med_dur >= 180 or (outage_cnt <= 2 and not is_flapper):
                # Rare, long fatal crash: authoritative, detailed diagnostic tone
                speech_rate = 0.96
                verbosity = "detailed"
                siren_notes = [659.25, 880.0, 1108.73]
                speech_pitch = 1.15
                chime_vol = 0.28
            else:
                speech_rate = 1.05
                verbosity = "standard"
                siren_notes = [554.37, 440.0, 369.99]
                speech_pitch = 1.02
                chime_vol = 0.20

            if is_flapper:
                speech_pitch = min(speech_pitch, 0.94)
                siren_notes = [440.0, 493.88]  # calm, non-screaming dual ping

            learned_fix = next((c["resolution"] for c in cs if c.get("resolution")), "") or ""

            node_profiles[s_name] = {
                "node": node,
                "short_name": s_name,
                "outage_count": outage_cnt,
                "median_duration_s": round(med_dur, 1),
                "settled_ratio": round(settled_ratio, 2),
                "is_flapper": is_flapper,
                "hold_down_s": hold_down_s,
                "flapping_threshold_minutes": 5 if is_flapper else 15,
                "audio_tuning": {
                    "speech_rate": round(speech_rate, 2),
                    "speech_pitch": round(speech_pitch, 2),
                    "verbosity": verbosity,
                    "siren_notes": siren_notes,
                    "chime_vol": round(chime_vol, 2),
                    "learned_fix": learned_fix,
                    "anti_fatigue": is_flapper,
                    "mttr_s": round(med_dur, 1)
                }
            }

        # Dynamic Audio Adaptation based on Time-of-Day Patterns in IST
        utc_now = datetime.now(timezone.utc)
        ist_now = utc_now.astimezone(IST)
        ist_hour = ist_now.hour

        # Night shift hours 02:00 to 06:00 IST (known high outage & break period, drowsiness risk)
        is_night_shift = 2 <= ist_hour < 6
        # Peak busy daytime office hours 10:00 to 18:00 IST (high ambient noise in control room)
        is_busy_day = 10 <= ist_hour < 18

        time_of_day_audio = {
            "ist_hour": ist_hour,
            "shift": "night" if is_night_shift else ("busy_day" if is_busy_day else "normal"),
            "attention_chime_boost": 0.05 if is_night_shift else 0.0,
            "volume_boost": 0.12 if is_busy_day else 0.0,
            "pitch_mod": 0.04 if is_night_shift else 0.0
        }

        return {
            "node_profiles": node_profiles,
            "cascade_graph": cascade_graph,
            "time_of_day": time_of_day_audio,
            "updated_at": time.time()
        }

    def record_alert_outcome(self, node: str, outcome: str) -> bool:
        """Records alert outcome feedback to self-tune segment_alerts and learning store priors."""
        try:
            from metrics import store as metrics_store
            mstore = metrics_store(self.path)
            # Exact match: the server's domain, or a URL on it (never a name that merely contains the text,
            # e.g. "gtc" must not hit "gtcpunjabi"). Each outcome counts one alert raised and its ending.
            where = "WHERE node = ? OR url LIKE ? OR url LIKE ?"
            args = (node, f"%://{node}/%", f"%://{node}:%")
            column = {"settled_alone": "cleared_alone", "real_outage": "before_outage",
                      "silenced_fast": "cleared_alone"}.get(outcome)
            if not column:
                return False
            with mstore._connect() as db:
                db.execute(f"UPDATE segment_alerts SET raised = raised + 1, {column} = {column} + 1 {where}", args)
            return True
        except Exception:
            return False




_stores: Dict[str, Learner] = {}
_stores_lock = threading.Lock()


def store(path: Optional[str] = None) -> Learner:
    """The shared Learner for METRICS_DB (the same file as the metrics and the incident log)."""
    path = str(path or os.environ.get("METRICS_DB") or Path(__file__).parent / "metrics.db")
    with _stores_lock:
        if path not in _stores:
            _stores[path] = Learner(path)
        return _stores[path]
