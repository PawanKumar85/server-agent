"""Token-efficient RAG: the last two steps before the LLM, after retrieval, fusion, reranking and relevance filtering
(hybrid_search.py) have picked the documents.

1. Duplicate / overlap removal: every line (fact) is fingerprinted (SHA-1 of its normalised text); a fact already
   given by an earlier document is not repeated, and a document with nothing new left is dropped.
2. Context compression: each document keeps only the lines that bear on the question, scored with the question's
   terms and the names in it, always keeping its first line (what it is about), within a token budget for the whole
   context. Long lists (the overview's "all servers ..." line) keep only the relevant or abnormal items and say how
   many normal ones were left out. A question that asks for everything ("list all", "sab", "how many") is not cut.

The model gets the right context instead of more context: fewer tokens, lower cost, faster, less noise.
"""

import hashlib
import re
from typing import Dict, Iterable, List, Tuple

from hybrid_search import tokens

BUDGET_TOKENS = 1600  # for the whole context (about 4 characters a token)
DOC_MAX_LINES = 8  # lines kept per document (besides the first)
# Background documents share the budget with the ones retrieved for this question: they get a fixed slice, so the
# question's own servers and channels are never squeezed out by general memory.
SHARE_CHARS = {"memory": 900}
SHARE_LINES = {"memory": 6}
HISTORY_LINES = 3  # outage / recovery lines kept per server unless the question is about history
WANTS_ALL = re.compile(r"\b(all|every|list|sab|saare|sabhi|kitne|how many|overview|summary|status of everything)\b", re.I)
WANTS_HISTORY = re.compile(r"\b(histor\w*|past|when|kab|last time|outages?|incidents?|recover\w*|pichl\w*|kitni baar|how often)\b", re.I)
NORMAL_ITEM = re.compile(r"\b(OK, fed from Main|UP \d)", re.I)
HISTORY_LINE = re.compile(r"\b(OUTAGE|RECOVERY|ESCALATED) at \d{4}-\d{2}-\d{2}")
# Words too common to tell lines apart ("is xcode4 ok?" must not make every "OK" line relevant).
GENERIC = {"ok", "up", "down", "status", "fine", "working", "chal", "raha", "rahi", "channel", "channels", "server",
           "servers", "stream", "streams", "now", "right", "abhi", "theek", "check", "fed", "main"}
LIST_LINE = re.compile(r"^(.{0,80}?:)\s*(.+;.+)$")  # "All servers (...): a; b; c"


def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip().lower())


def _fp(line: str) -> str:
    return hashlib.sha1(_norm(line).encode("utf-8")).hexdigest()


def _score(line: str, q_terms: set, names: Iterable[str]) -> float:
    low = line.lower()
    s = sum(1.0 for t in set(tokens(line)) if t in q_terms)
    s += sum(3.0 for n in names if n and n.lower() in low)
    if re.search(r"\b(DOWN|STALE|404|ERROR|backup|failover|failing|stuck|glitch|NO INPUT)", line):
        s += 2.0  # a problem outranks a plain word match: it is what the operator needs to hear
    return s


def _shrink_list(line: str, q_terms: set, names: List[str]) -> str:
    """'Title: a; b; c; ...' -> the relevant or abnormal items, plus how many normal ones were left out."""
    m = LIST_LINE.match(line)
    if not m:
        return line
    head, items = m.group(1), [i.strip() for i in m.group(2).split(";") if i.strip()]
    keep = [i for i in items if _score(i, q_terms, names) > 0 or not NORMAL_ITEM.search(i)]
    dropped = len(items) - len(keep)
    if not dropped:
        return line
    return f"{head} {'; '.join(keep) or '(none relevant)'}{f'; ... {dropped} more, all normal' if dropped else ''}"


def _fold_normal_items(body: List[str], q_terms: set, names: List[str]) -> List[str]:
    """'- gtcnews: OK, fed from Main.' lines that the question doesn't name: folded into one count."""
    out, folded = [], 0
    for ln in body:
        if ln.lstrip().startswith("- ") and NORMAL_ITEM.search(ln) and _score(ln, q_terms, names) == 0:
            folded += 1
            continue
        if folded:
            out.append(f"- ... {folded} more, all normal")
            folded = 0
        out.append(ln)
    if folded:
        out.append(f"- ... {folded} more, all normal")
    return out


def compress(question: str, results: List[dict], names: Iterable[str] = (),
             budget_tokens: int = BUDGET_TOKENS) -> Tuple[List[dict], Dict[str, int]]:
    """results: [{"document": {"id", "text", ...}, ...}] in rank order (overview first). Returns new results with
    compressed text (documents left with nothing new are dropped) and {"before", "after"} token estimates."""
    names = [n for n in names if n]
    q_terms = set(tokens(question)) - GENERIC
    keep_all = bool(WANTS_ALL.search(question))
    keep_history = bool(WANTS_HISTORY.search(question))
    seen: set = set()
    out, used, before = [], 0, 0
    budget = budget_tokens * 4
    for r in results:
        doc = r["document"]
        text = doc.get("text") or ""
        before += len(text)
        lines = [ln for ln in text.split("\n") if ln.strip()]
        if not lines:
            continue
        head, body = lines[0], lines[1:]
        if not keep_all:
            body = [_shrink_list(ln, q_terms, names) for ln in body]
            body = _fold_normal_items(body, q_terms, names)
        if not keep_history:  # past outages: the newest few are enough
            hist = [i for i, ln in enumerate(body) if HISTORY_LINE.search(ln)]
            drop = set(hist[:-HISTORY_LINES]) if len(hist) > HISTORY_LINES else set()
            body = [ln for i, ln in enumerate(body) if i not in drop]
        body = [ln for ln in body if _fp(ln) not in seen]  # overlap: facts an earlier document already gave
        max_lines = SHARE_LINES.get(doc.get("id"), DOC_MAX_LINES)
        if not keep_all and len(body) > max_lines:
            ranked = sorted(range(len(body)), key=lambda i: -_score(body[i], q_terms, names))[:max_lines]
            body = [body[i] for i in sorted(ranked)]  # the best lines, in their original order
        fresh_head = _fp(head) not in seen
        if not body and not fresh_head:
            continue  # nothing new in this document
        kept = ([head] if fresh_head else []) + body
        new_text = "\n".join(kept)
        cap = SHARE_CHARS.get(doc.get("id"))
        if cap and len(new_text) > cap:
            new_text = new_text[:cap].rsplit("\n", 1)[0]
        if used + len(new_text) > budget and out:
            room = budget - used
            if room < 200:
                break
            new_text = new_text[:room].rsplit("\n", 1)[0]  # cut at a line, never mid-fact
        seen.update(_fp(ln) for ln in kept)
        used += len(new_text)
        out.append({**r, "document": {**doc, "text": new_text, "compressed_from": len(text)}})
    return out, {"before": before // 4, "after": used // 4}
