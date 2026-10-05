"""Hybrid retrieval: BM25, dense search, Reciprocal Rank Fusion and the reranker."""

import numpy as np

from hybrid_search import BM25, HybridSearch, rrf, tokens

DOCS = {
    "server:xcode4": {"text": "Server xcode4.ottlive.co.in transcoder for Rang Manch. Last error: STALE_SEGMENTS 98s old."},
    "server:ingest1": {"text": "Server ingest1.ottlive.co.in main input for Rang Manch. Status UP, latency 210 ms."},
    "channel:gtcnews": {"text": "Channel gtcnews: final gtc.ottlive.co.in/gtcnews, main input jio. HTTP 404 on playlist."},
    "scheduler": {"text": "Auto-ping is on, every 30 seconds; next run soon."},
}


def test_tokens_keep_domains_whole_and_in_parts():
    t = tokens("Why is xcode4.ottlive.co.in down?")
    assert "xcode4" in t and "xcode4.ottlive.co.in" in t and "is" not in t


def test_bm25_finds_exact_terms():
    bm = BM25([d["text"] for d in DOCS.values()])
    s = dict(zip(DOCS, bm.scores("404 playlist")))
    assert max(s, key=s.get) == "channel:gtcnews" and s["scheduler"] == 0


def test_rrf_rewards_agreement_between_lists():
    fused = rrf({"a": ["x", "y", "z"], "b": ["y", "x"], "c": ["y"]})
    assert [d for d, _, _ in fused][:2] == ["y", "x"] and fused[0][2] == ["a", "b", "c"]


class WordVec:
    VOCAB = ["stale", "segments", "freeze", "404", "playlist", "latency", "auto", "ping"]

    def __call__(self, texts):
        out = []
        for t in texts:
            words = t.lower().replace(":", " ").replace(".", " ").split()
            v = np.array([sum(w.startswith(k) for w in words) for k in self.VOCAB], dtype="float32") + 1e-3
            out.append(v / np.linalg.norm(v))
        return np.stack(out)


class FakeReranker:
    def scores(self, query, docs):
        return [5.0 if "ingest1" in d else 0.0 for d in docs]


def test_fusion_keeps_named_documents_and_the_reranker_reorders_the_rest():
    hs = HybridSearch(WordVec())
    ranked = hs.rank("stale segments on xcode4", DOCS, graph_order=["server:ingest1"], pinned=["server:xcode4"], limit=3)
    assert ranked[0]["id"] == "server:xcode4" and ranked[0]["sources"] == ["entity"]
    assert "scheduler" not in [r["id"] for r in ranked]  # unrelated: neither BM25 nor dense propose it
    hs2 = HybridSearch(WordVec(), FakeReranker())
    ranked2 = hs2.rank("playlist 404", DOCS, graph_order=["channel:gtcnews", "server:ingest1"], limit=2)
    assert ranked2[0]["id"] == "server:ingest1" and ranked2[0]["rerank"] == 5.0  # the cross-encoder's call


def test_without_a_reranker_the_fused_order_is_used():
    ranked = HybridSearch(WordVec()).rank("404 playlist", DOCS, graph_order=[], limit=2)
    assert ranked[0]["id"] == "channel:gtcnews" and set(ranked[0]["sources"]) == {"bm25", "dense"}
    assert ranked[0]["rerank"] is None


def test_clearly_unrelated_candidates_are_dropped_after_reranking():
    class Picky:
        def scores(self, query, docs):
            return [4.0 if "404" in d else -11.0 for d in docs]
    ranked = HybridSearch(WordVec(), Picky()).rank("404 playlist", DOCS, graph_order=list(DOCS), limit=4)
    assert [r["id"] for r in ranked] == ["channel:gtcnews"]  # the rest scored far below: not sent to the model


def test_an_unsure_reranker_steps_aside():
    class Unsure:
        def scores(self, query, docs):
            return [-9.5] * len(docs)  # e.g. a Hinglish question: it understands none of them
    ranked = HybridSearch(WordVec(), Unsure()).rank("404 playlist", DOCS, graph_order=["server:ingest1"], limit=3)
    assert len(ranked) >= 2 and ranked[0]["rerank"] is None


def test_live_numbers_dont_change_the_dense_fingerprint():
    from hybrid_search import stable_text
    a = "Server xcode4 UP, latency 210.4 ms, last ping 2026-10-05T12:01:07Z, 1552 checks, HTTP 404 x1"
    b = "Server xcode4 UP, latency 233.9 ms, last ping 2026-10-05T12:01:37Z, 1553 checks, HTTP 404 x1"
    assert stable_text(a) == stable_text(b) and "404 x1" in stable_text(a)
