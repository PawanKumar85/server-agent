"""NLP Incident Postmortem Summarizer using Graph-Based TextRank Algorithm.

Applies PageRank over a pairwise TF-IDF sentence cosine similarity graph to extract
the most salient operational findings from cascading outage logs and incident timelines.
Runs in sub-milliseconds with pure NumPy (zero heavy torch dependencies).
"""

import math
import re
from typing import Dict, List, Optional, Any
import numpy as np


class TextRankSummarizer:
    """Graph-based extractive text summarization for incident timelines and log reports."""

    DAMPING = 0.85
    MAX_ITER = 40
    CONVERGENCE = 1e-4

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        words = re.findall(r'[a-zA-Z0-9_\u0900-\u097F]+', (text or "").lower())
        # Filter short noise tokens
        return [w for w in words if len(w) >= 3 and not w.startswith("http")]

    def _build_similarity_matrix(self, sentences: List[str]) -> np.ndarray:
        """Constructs sentence-sentence TF-IDF cosine similarity adjacency matrix."""
        n = len(sentences)
        if n == 0:
            return np.zeros((0, 0), dtype="float32")

        tokenized = [self._tokenize(s) for s in sentences]
        vocab = {}
        for toks in tokenized:
            for t in toks:
                if t not in vocab:
                    vocab[t] = len(vocab)

        if not vocab:
            return np.zeros((n, n), dtype="float32")

        # Compute TF-IDF vectors
        doc_count = n
        df = np.zeros(len(vocab), dtype="float32")
        for toks in tokenized:
            for t in set(toks):
                df[vocab[t]] += 1.0
        idf = np.log((doc_count + 1.0) / (df + 1.0)) + 1.0

        vectors = np.zeros((n, len(vocab)), dtype="float32")
        for i, toks in enumerate(tokenized):
            for t in toks:
                vectors[i, vocab[t]] += idf[vocab[t]]
            norm = np.linalg.norm(vectors[i])
            if norm > 0:
                vectors[i] /= norm

        # Pairwise cosine similarity matrix
        sim_mat = np.dot(vectors, vectors.T)
        # Remove self-loops
        np.fill_diagonal(sim_mat, 0.0)
        return sim_mat.astype("float32")

    def rank_sentences(self, sentences: List[str]) -> List[Dict[str, Any]]:
        """Ranks sentences according to graph TextRank centrality."""
        if not sentences:
            return []

        # Deduplicate identical consecutive lines
        deduped = []
        seen = set()
        for s in sentences:
            clean = s.strip()
            if clean and clean not in seen:
                seen.add(clean)
                deduped.append(clean)

        n = len(deduped)
        if n == 0:
            return []
        if n == 1:
            return [{"sentence": deduped[0], "score": 1.0, "index": 0}]

        sim_mat = self._build_similarity_matrix(deduped)

        # Normalize transition probabilities
        row_sums = sim_mat.sum(axis=1)
        # Avoid division by zero
        transition = np.zeros_like(sim_mat)
        for i in range(n):
            if row_sums[i] > 0:
                transition[i] = sim_mat[i] / row_sums[i]
            else:
                transition[i] = np.ones(n, dtype="float32") / n

        # PageRank power iteration
        scores = np.ones(n, dtype="float32") / n
        for _ in range(self.MAX_ITER):
            next_scores = (1 - self.DAMPING) / n + self.DAMPING * np.dot(transition.T, scores)
            diff = np.linalg.norm(next_scores - scores)
            scores = next_scores
            if diff < self.CONVERGENCE:
                break

        ranked = []
        for i, s in enumerate(deduped):
            ranked.append({
                "sentence": s,
                "score": round(float(scores[i]), 4),
                "index": i
            })

        ranked.sort(key=lambda x: -x["score"])
        return ranked

    def summarize_incident(
        self,
        incident_logs: List[str],
        max_sentences: int = 3,
        channel: str = "",
        server: str = ""
    ) -> Dict[str, Any]:
        """Produces a structured executive incident postmortem summary from raw logs and alerts."""
        if not incident_logs:
            return {
                "headline": f"No incident logs recorded for {channel or server or 'system'}",
                "executive_summary": "System operational with normal telemetry parameters.",
                "key_findings": [],
                "raw_sentence_count": 0
            }

        ranked = self.rank_sentences(incident_logs)
        top_items = ranked[:max_sentences]

        # Order selected sentences chronologically by original index
        top_items_chronological = sorted(top_items, key=lambda x: x["index"])
        summary_text = " ".join(item["sentence"] for item in top_items_chronological)

        # Generate a crisp headline
        headline = f"Incident Report: {channel or server or 'Stream Network'}"
        if any("404" in s.lower() for s in incident_logs):
            headline = f"Outage Report: Manifest 404 & Stream Loss on {channel or server or 'origin'}"
        elif any("stale" in s.lower() or "freeze" in s.lower() for s in incident_logs):
            headline = f"Performance Degradation: Stale Chunks & Video Freeze on {channel or server or 'CDN'}"
        elif any("latency" in s.lower() or "delay" in s.lower() for s in incident_logs):
            headline = f"Broadcast Latency Spike Detected on {channel or server or 'network'}"

        return {
            "headline": headline,
            "executive_summary": summary_text,
            "key_findings": [item["sentence"] for item in top_items],
            "raw_sentence_count": len(incident_logs),
            "ranked_sentences": ranked[:6]
        }


# Global singleton instance
textrank_summarizer = TextRankSummarizer()
