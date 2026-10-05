"""Hybrid retrieval for the chatbot: BM25 + dense search, fused with RRF, then a cross-encoder reranker.

Four rankings of the same context documents (servers, channels, relationships, scheduler, ...):
  graph  - GraphRAG: names in the question, Neo4j vector seeds and their FEEDS/PRODUCES neighbourhood (graphrag.py)
  bm25   - Okapi BM25 over the document text: exact terms win ("404", "xcode4", "STALE_MEDIA", "jitter")
  dense  - cosine between the question and each document's MiniLM embedding: meaning wins ("picture keeps freezing")
They are merged with Reciprocal Rank Fusion (score = sum of 1 / (RRF_K + rank)), which needs no score calibration
between the lists. The best RERANK_POOL then go through a cross-encoder (ms-marco-MiniLM-L-6-v2, ONNX via
fastembed) that reads question and document together, and the top `limit` are kept. Documents named in the question
are always kept. Without the reranker model it falls back to the RRF order.
"""

import hashlib
import math
import re
import threading
from collections import Counter
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

RRF_K = 60  # the usual constant: dampens the weight of rank 1 vs rank 2
BM25_K1, BM25_B = 1.5, 0.75
LIST_DEPTH = 12  # each ranking contributes its top this many to the fusion
RERANK_POOL = 12  # fused candidates the cross-encoder re-scores
# The reranker's score is a logit: relevant passages score above 0, unrelated ones far below. A candidate is dropped
# when it is clearly unrelated (below RERANK_MIN) or far behind the best one (more than RERANK_GAP below it), so the
# model gets a few relevant documents instead of filler.
RERANK_MIN, RERANK_GAP = -9.0, 5.5
# The reranker is English-only: when even its best candidate scores below this (typical for Hinglish questions), it
# isn't sure of anything, so the fused BM25 + dense + graph order is used instead.
RERANK_TRUST = -7.0
RERANKER_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
DENSE_MIN = 0.3  # cosine below which a document is unrelated to the question (MiniLM): it is not proposed
DOC_CHARS = 700  # what the reranker reads of each document (it is trained on short passages; less is faster)

_TOKEN = re.compile(r"[a-z0-9]+")
STOP = {"the", "a", "an", "is", "are", "of", "to", "in", "on", "and", "or", "for", "it", "its", "be", "was", "what",
        "which", "how", "why", "kya", "hai", "ka", "ki", "ke", "me", "mein", "se", "ko", "aur", "ya", "pe", "this"}


_VOLATILE = [
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2}| IST)?"), "<time>"),
    (re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?\b"), "<time>"),
    (re.compile(r"\b\d+\.\d+\b"), "<num>"),            # 210.4 ms, 0.98, 99.9%
    (re.compile(r"\b\d{4,}\b"), "<num>"),                # counters, epoch seconds, sequence numbers
]


def stable_text(text: str) -> str:
    """The document without its live numbers (times, measurements, counters), for the dense embedding: the meaning is
    the same from one check to the next, so its SHA-1 fingerprint is too and the cached vector is reused. Short codes
    (404, x19) stay. BM25 still reads the full text."""
    for pattern, mask in _VOLATILE:
        text = pattern.sub(mask, text)
    return text


def tokens(text: str) -> List[str]:
    """Lowercase words and numbers; a domain or URL splits into its parts ("xcode4.ottlive.co.in" -> xcode4, ottlive,
    co, in) and also stays whole, so both "xcode4" and the full name match."""
    text = (text or "").lower()
    out = [t for t in _TOKEN.findall(text) if t not in STOP]
    out += [d for d in re.findall(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", text)]
    return out


class BM25:
    def __init__(self, docs: Sequence[str], k1: float = BM25_K1, b: float = BM25_B):
        self.k1, self.b = k1, b
        self.tf = [Counter(tokens(d)) for d in docs]
        self.len = [sum(c.values()) for c in self.tf]
        self.avg = (sum(self.len) / len(self.len)) if self.len else 0.0
        n = len(docs)
        df = Counter(t for c in self.tf for t in c)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: str) -> List[float]:
        q = tokens(query)
        out = []
        for tf, length in zip(self.tf, self.len):
            s = 0.0
            for t in q:
                f = tf.get(t)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * length / (self.avg or 1)))
            out.append(s)
        return out


def rrf(rankings: Dict[str, List[str]], k: int = RRF_K, depth: int = LIST_DEPTH) -> List[Tuple[str, float, List[str]]]:
    """[(doc_id, fused score, [lists it came from])], best first."""
    score: Dict[str, float] = {}
    sources: Dict[str, List[str]] = {}
    for name, ranked in rankings.items():
        for rank, doc_id in enumerate(ranked[:depth], start=1):
            score[doc_id] = score.get(doc_id, 0.0) + 1.0 / (k + rank)
            sources.setdefault(doc_id, []).append(name)
    return sorted(((d, s, sources[d]) for d, s in score.items()), key=lambda x: -x[1])


class Reranker:
    """Cross-encoder: loaded on first use; unavailable (None scores) if the model can't be loaded."""

    def __init__(self, model: str = RERANKER_MODEL, cache_dir: Optional[str] = None):
        self.model_name, self.cache_dir = model, cache_dir
        self._model = None
        self._failed = False
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._model is None and not self._failed:
                try:
                    from fastembed.rerank.cross_encoder import TextCrossEncoder
                    self._model = TextCrossEncoder(self.model_name, cache_dir=self.cache_dir)
                except Exception as e:
                    print(f"[rerank] reranker unavailable ({type(e).__name__}: {e}); using the fused order")
                    self._failed = True
        return self._model

    def scores(self, query: str, docs: Sequence[str]) -> Optional[List[float]]:
        model = self._load()
        if model is None or not docs:
            return None
        return [float(s) for s in model.rerank(query, [d[:DOC_CHARS] for d in docs])]


class HybridSearch:
    """Ranks context documents for a question. embed: texts -> 2-D array of unit-length vectors."""

    def __init__(self, embed: Callable[[List[str]], "np.ndarray"], reranker: Optional[Reranker] = None):
        self.embed = embed
        self.reranker = reranker
        self._bm25: Tuple[Optional[str], Optional[BM25]] = (None, None)

    def _doc_vectors(self, texts: List[str]) -> np.ndarray:
        """Unit vectors for the documents; repeats are free (text_embedding's SHA-1 fingerprint cache)."""
        vecs = np.asarray(self.embed(texts), dtype="float32")
        return vecs / np.where(np.linalg.norm(vecs, axis=1, keepdims=True) == 0, 1, np.linalg.norm(vecs, axis=1, keepdims=True))

    def _bm25_for(self, ids: List[str], texts: List[str]) -> BM25:
        key = hashlib.sha1("\x00".join(texts).encode()).hexdigest()
        if self._bm25[0] != key:
            self._bm25 = (key, BM25(texts))
        return self._bm25[1]

    def rank(self, question: str, docs: Dict[str, dict], graph_order: List[str], pinned: Iterable[str] = (),
             limit: int = 8) -> List[dict]:
        """docs: id -> {"text", ...} (the overview excluded). graph_order: GraphRAG's ranking. Returns up to `limit`
        [{"id", "score", "sources", "rrf", "rerank"}], pinned first, then the reranked rest."""
        ids = [i for i in docs if docs[i].get("text")]
        if not ids:
            return []
        texts = [docs[i]["text"] for i in ids]
        bm = self._bm25_for(ids, texts).scores(question)
        stable = [stable_text(t) for t in texts]
        bm25_order = [ids[j] for j in np.argsort(bm)[::-1] if bm[j] > 0]
        try:
            q = np.asarray(self.embed([question])[0], dtype="float32")
            q = q / (np.linalg.norm(q) or 1.0)
            with np.errstate(all="ignore"):  # Apple Accelerate reports spurious matmul warnings
                dense = self._doc_vectors(stable) @ q
            dense_order = [ids[j] for j in np.argsort(dense)[::-1] if dense[j] >= DENSE_MIN]
        except Exception:
            dense, dense_order = None, []
        fused = rrf({"graph": [g for g in graph_order if g in docs], "bm25": bm25_order, "dense": dense_order})
        pool = fused[:RERANK_POOL]
        rr = self.reranker.scores(question, [docs[d]["text"] for d, _, _ in pool]) if self.reranker else None
        if rr is not None and (not rr or max(rr) < RERANK_TRUST):
            rr = None  # not confident about any candidate: trust the fusion
        if rr is not None:
            floor = max(RERANK_MIN, max(rr) - RERANK_GAP) if rr else RERANK_MIN
            order = [i for i in sorted(range(len(pool)), key=lambda i: -rr[i]) if rr[i] >= floor]
        else:
            order = list(range(len(pool)))
        pinned = [p for p in pinned if p in docs]
        out, seen = [], set()
        for p in pinned:  # named in the question: always in, first
            j = next((i for i, (d, _, _) in enumerate(pool) if d == p), None)
            out.append({"id": p, "score": 1.0, "sources": ["entity"], "rrf": round(pool[j][1], 4) if j is not None else None,
                        "rerank": round(rr[j], 3) if rr is not None and j is not None else None})
            seen.add(p)
        for i in order:
            if len(out) >= limit:
                break
            d, s, src = pool[i]
            if d in seen:
                continue
            seen.add(d)
            out.append({"id": d, "score": round(1 / (1 + math.exp(-rr[i])), 3) if rr is not None else round(s * RRF_K / 3, 3),
                        "sources": src, "rrf": round(s, 4), "rerank": round(rr[i], 3) if rr is not None else None})
        return out[:max(limit, len(pinned))]
