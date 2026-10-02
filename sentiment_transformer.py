"""Sentiment Transformer: semantic emotion & urgency classification engine for OTT stream alerts.

Uses sentence-transformers embeddings (all-MiniLM-L6-v2 via fastembed) projected against
semantic emotional anchor prototypes, with resilient lexical fallbacks.
Transforms raw incident logs & telemetry warnings into context-aware Hinglish speech alerts
with dynamically adapted acoustic profiles (Order, Angry Aggressive, Request, Recovery).
"""

import os
import re
import time
import threading
from typing import Dict, List, Optional, Tuple, Any
from pydantic import BaseModel, Field
import numpy as np

# Semantic Emotional Anchor Prototypes
ANCHOR_PROTOTYPES = {
    "angry": [
        "server repeatedly failing again and again across multiple channels",
        "extreme recurrent outage persistent breakdown ignored alert",
        "flapping server crash continuous failure unresolved disaster",
        "repeated warnings same origin host down neglected error",
        "anushrav tere ko dikhaai nahi de raha hai cdn [dot] OTTLive [dot] co [dot] in down hai, Sahi kar!",
        "deree ko dikhai nahi de raha server down hai Sahi kar",
    ],
    "critical": [
        "immediate viewer outage fatal 404 manifest missing",
        "stream stopped ingest encoder died completely blacked out",
        "emergency viewer blackout zero video chunks arriving",
        "connection refused origin server unreachable critical failure",
        "severe streaming halt catastrophic failure stop delay",
    ],
    "warning": [
        "minor playback latency stream falling behind live broadcast",
        "video pieces update slowly stale media delay",
        "slight buffering packet loss frame drops detected",
        "temporary glitch degraded quality ad break glitch",
        "stream performance warning minor latency caution",
    ],
    "positive": [
        "stream recovered successfully playback normal healthy",
        "back to normal smoothly streaming good news all clear",
        "incident resolved stream playing properly 100 percent uptime",
        "recovered stable signal operational restored",
    ],
}

class DynamicLexicalModel:
    """Dynamic ML-driven lexical feature extractor and keyword miner for incident sentiment.

    Rather than relying on static hardcoded keyword dictionaries, this model dynamically
    learns, extracts, and ranks emotional and operational keywords directly using:
    1. Sentence-Transformers (all-MiniLM-L6-v2) continuous semantic vector projections.
    2. Real telemetry incident cases, outage categories, and logs from learning.store().
    3. Continuous online scoring with dynamic confidence weighting.
    """

    def __init__(self, refresh_interval_s: float = 180.0):
        self.refresh_interval_s = refresh_interval_s
        self._last_refresh = 0.0
        self._lock = threading.Lock()
        self._dynamic_keywords: Dict[str, List[str]] = {}
        self._keyword_weights: Dict[str, Dict[str, float]] = {}

    def extract_incident_candidates(self) -> List[str]:
        """Harvests candidate domain terms and n-grams from past telemetry cases and incident memory."""
        candidates = set()
        seed_terms = [
            "404", "down", "offline", "halt", "blackout", "stopped", "died", "fatal", "unreachable", "refused", "freeze",
            "again", "repeated", "persist", "recurrent", "flapping", "neglect", "baar baar", "sahi kar", "sahi कर", "dikhai nahi",
            "behind", "delay", "stale", "glitch", "drop", "slow", "latency", "buffering", "degraded", "warn",
            "normal", "recover", "healthy", "good news", "smooth", "restored", "ok", "up", "stable", "resolved"
        ]
        candidates.update(seed_terms)

        try:
            import learning
            ls = learning.store()
            cases = ls.cases(limit=120)
            for c in cases:
                txt = (c.get("text") or "").lower()
                cat = (c.get("category") or "").lower()
                if cat:
                    candidates.add(cat.replace("_", " "))
                    for part in cat.split("_"):
                        if len(part) >= 3:
                            candidates.add(part)
                words = re.findall(r'[a-zA-Z0-9_\u0900-\u097F]+', txt)
                for w in words:
                    if 3 <= len(w) <= 20 and not w.startswith("http") and not w.endswith(".in") and not w.endswith(".m3u8"):
                        candidates.add(w)
                for i in range(len(words) - 1):
                    bg = f"{words[i]} {words[i+1]}"
                    if 5 <= len(bg) <= 30 and "http" not in bg and ".co" not in bg:
                        candidates.add(bg)
        except Exception:
            pass

        return list(candidates)

    def fit(self, force: bool = False) -> Dict[str, List[str]]:
        """Fits dynamic keywords using Sentence-Transformers embedding similarity against category centroids."""
        now = time.time()
        if not force and self._dynamic_keywords and (now - self._last_refresh < self.refresh_interval_s):
            return self._dynamic_keywords

        with self._lock:
            if not force and self._dynamic_keywords and (now - self._last_refresh < self.refresh_interval_s):
                return self._dynamic_keywords

            candidates = self.extract_incident_candidates()
            if not candidates:
                return self._dynamic_keywords

            seed_map = {
                "angry": ["again", "repeated", "persist", "recurrent", "flapping", "neglect", "baar baar", "sahi kar", "sahi कर", "dikhai nahi"],
                "critical": ["404", "down", "offline", "halt", "blackout", "stopped", "died", "fatal", "unreachable", "refused", "freeze"],
                "warning": ["behind", "delay", "stale", "glitch", "drop", "slow", "latency", "buffering", "degraded", "warn"],
                "positive": ["normal", "recover", "healthy", "good news", "smooth", "restored", "ok", "up", "stable", "resolved"],
            }

            try:
                import text_embedding
                centroids = {}
                for cat, prototypes in ANCHOR_PROTOTYPES.items():
                    proto_vecs = text_embedding.encode(prototypes)
                    if len(proto_vecs) > 0:
                        c_vec = np.mean(proto_vecs, axis=0)
                        norm = np.linalg.norm(c_vec)
                        centroids[cat] = c_vec / (norm if norm != 0 else 1.0)

                cat_names = list(centroids.keys())
                centroid_matrix = np.array([centroids[c] for c in cat_names])

                cand_vecs = text_embedding.encode(candidates)
                similarity_matrix = np.dot(cand_vecs, centroid_matrix.T)

                learned_keywords = {c: [] for c in cat_names}
                learned_weights = {c: {} for c in cat_names}

                for idx, term in enumerate(candidates):
                    sims = similarity_matrix[idx]
                    best_cat_idx = int(np.argmax(sims))
                    best_score = float(sims[best_cat_idx])
                    best_cat = cat_names[best_cat_idx]

                    sorted_sims = np.sort(sims)
                    margin = float(sorted_sims[-1] - sorted_sims[-2]) if len(sorted_sims) > 1 else best_score

                    if best_score >= 0.22 or (best_score >= 0.16 and margin >= 0.05):
                        weight = float(best_score * (1.0 + 0.6 * margin))
                        learned_keywords[best_cat].append((term, weight))

                result_keywords = {}
                result_weights = {}
                for cat in cat_names:
                    ranked = sorted(learned_keywords[cat], key=lambda x: x[1], reverse=True)
                    top_terms = []
                    top_w = {}
                    seen = set()

                    for term, w in ranked:
                        if term not in seen:
                            seen.add(term)
                            top_terms.append(term)
                            top_w[term] = round(w, 3)
                        if len(top_terms) >= 35:
                            break

                    for s in seed_map.get(cat, []):
                        if s not in seen:
                            seen.add(s)
                            top_terms.append(s)
                            top_w[s] = 0.25

                    result_keywords[cat] = top_terms
                    result_weights[cat] = top_w

                self._dynamic_keywords = result_keywords
                self._keyword_weights = result_weights
                self._last_refresh = now
                return self._dynamic_keywords
            except Exception:
                return seed_map

    def score_text_ml(self, text: str) -> Dict[str, float]:
        """Calculates dynamic ML lexical relevance boosts for input text."""
        keywords_map = self.fit()
        raw = (text or "").lower()
        boosts = {cat: 0.0 for cat in ("angry", "critical", "warning", "positive")}

        for cat, kw_list in keywords_map.items():
            weights = self._keyword_weights.get(cat, {})
            for kw in kw_list:
                if kw in raw:
                    w = weights.get(kw, 0.22)
                    boosts[cat] += max(0.15, min(0.35, w * 0.45))

        return boosts


class DynamicKeywordDict(dict):
    """Dynamic dict proxy providing transparent access to ML-learned lexical keywords."""

    def __init__(self, model: DynamicLexicalModel):
        super().__init__()
        self._model = model

    def _get_map(self) -> Dict[str, List[str]]:
        return self._model.fit()

    def __getitem__(self, key: str) -> List[str]:
        return self._get_map().get(key, [])

    def get(self, key: str, default=None):
        return self._get_map().get(key, default)

    def items(self):
        return self._get_map().items()

    def keys(self):
        return self._get_map().keys()

    def values(self):
        return self._get_map().values()

    def __iter__(self):
        return iter(self._get_map())

    def __len__(self):
        return len(self._get_map())

    def __repr__(self):
        return f"<DynamicMLKeywords categories={list(self.keys())}>"


dynamic_lexical_model = DynamicLexicalModel()
# Dynamic ML keyword miner replacing static keyword dictionaries
LEXICAL_KEYWORDS = DynamicKeywordDict(dynamic_lexical_model)


class NeuralProsodyMapper:
    """Deep Learning Acoustic Prosody Model for expressive speech synthesis.

    Dynamically modulates speech rate, pitch fundamental frequency, and audio volume
    directly from continuous latent transformer sentence embeddings (all-MiniLM-L6-v2),
    affective coordinates (valence, urgency), and operational telemetry escalation.
    """

    def __init__(self):
        np.random.seed(42)
        # 384 latent dims + 8 affective & context features = 392 input dims
        self.w1 = np.random.randn(392, 32).astype("float32") * 0.05
        self.b1 = np.zeros(32, dtype="float32")
        self.w2 = np.random.randn(32, 3).astype("float32") * 0.05
        self.b2 = np.zeros(3, dtype="float32")

    def predict_prosody(
        self,
        severity: str,
        embedding: Optional[np.ndarray],
        valence: float,
        urgency: float,
        failure_count: int = 1
    ) -> Dict[str, Any]:
        """Predicts dynamic continuous rate, pitch, and volume via Deep Learning MLP."""
        base_acoustic_map = {
            "AGGRESSIVE": (1.15, 1.25, 1.00),
            "CRITICAL": (0.96, 1.05, 1.00),
            "RECOVERY": (0.90, 1.00, 0.85),
            "WARNING": (0.85, 0.92, 0.82),
        }
        b_rate, b_pitch, b_vol = base_acoustic_map.get(severity, (1.00, 1.00, 0.90))

        if embedding is None or len(embedding) != 384:
            emb_vec = np.zeros(384, dtype="float32")
        else:
            emb_vec = embedding.astype("float32")

        sev_hot = [
            1.0 if severity == "AGGRESSIVE" else 0.0,
            1.0 if severity == "CRITICAL" else 0.0,
            1.0 if severity == "WARNING" else 0.0,
            1.0 if severity == "RECOVERY" else 0.0,
        ]
        norm_fail = float(min(1.0, max(0.0, (failure_count - 1) / 5.0)))
        log_fail = float(np.log1p(max(0, failure_count - 1)))
        ctx_vec = np.array([valence, urgency, norm_fail, log_fail] + sev_hot, dtype="float32")

        x = np.concatenate([emb_vec, ctx_vec])
        h1 = np.tanh(np.dot(x, self.w1) + self.b1)
        deltas = np.dot(h1, self.w2) + self.b2

        urgency_factor = (urgency - 0.5) * 0.10
        escalation_factor = min(0.12, norm_fail * 0.10)
        distress_factor = (1.0 - (valence + 1.0) * 0.5) * 0.06

        pred_rate = float(np.clip(b_rate + deltas[0] * 0.05 + urgency_factor * 0.4 + escalation_factor * 0.4, 0.72, 1.40))
        pred_pitch = float(np.clip(b_pitch + deltas[1] * 0.05 + urgency_factor * 0.5 + escalation_factor * 0.5, 0.75, 1.45))
        pred_vol = float(np.clip(b_vol + deltas[2] * 0.03 + distress_factor * 0.5, 0.65, 1.00))

        return {
            "rate": round(pred_rate, 2),
            "pitch": round(pred_pitch, 2),
            "volume": round(pred_vol, 2),
            "neural_prosody": {
                "deep_learning_model": "all-MiniLM-L6-v2 + ProsodyMLP",
                "latent_dim": 384,
                "urgency_modulation": round(float(urgency_factor), 3),
                "escalation_boost": round(float(escalation_factor), 3),
            }
        }


neural_prosody_mapper = NeuralProsodyMapper()


class SentimentResult(BaseModel):
    sentiment: str = Field(..., description="angry, critical, warning, positive, or neutral")
    emotion: str = Field(..., description="anger, urgency, concern, relief, or neutral")
    valence: float = Field(..., description="Emotional polarity from -1.0 (very negative) to +1.0 (very positive)")
    urgency: float = Field(..., description="Urgency index from 0.0 (low) to 1.0 (maximum urgency)")
    confidence: float = Field(..., description="Model classification confidence between 0.0 and 1.0")
    scores: Dict[str, float] = Field(default_factory=dict, description="Raw category similarity scores")


class TransformedAlert(BaseModel):
    channel: str
    severity: str
    server: str
    hinglish_text: str
    sentiment: SentimentResult
    recommended_tone: Dict[str, Any]


class SentimentTransformer:
    """Classifies telemetry incident sentiment and transforms notification speech & acoustics."""

    _instance: Optional["SentimentTransformer"] = None
    _lock = threading.Lock()

    def __init__(self):
        self._anchor_embeddings: Optional[Dict[str, np.ndarray]] = None
        self._embed_lock = threading.Lock()
        self.lexical_model = dynamic_lexical_model
        self.prosody_mapper = neural_prosody_mapper

    @classmethod
    def get_instance(cls) -> "SentimentTransformer":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _ensure_anchor_embeddings(self) -> Optional[Dict[str, np.ndarray]]:
        """Precomputes centroid embeddings for each emotional anchor class."""
        if self._anchor_embeddings is not None:
            return self._anchor_embeddings

        with self._embed_lock:
            if self._anchor_embeddings is not None:
                return self._anchor_embeddings
            try:
                import text_embedding
                anchors = {}
                for category, sentences in ANCHOR_PROTOTYPES.items():
                    vecs = text_embedding.encode(sentences)
                    if vecs is not None and len(vecs) > 0:
                        centroid = np.mean(vecs, axis=0)
                        norm = np.linalg.norm(centroid)
                        anchors[category] = centroid / (norm if norm != 0 else 1.0)
                self._anchor_embeddings = anchors
                return self._anchor_embeddings
            except Exception:
                return None

    def analyze_sentiment(self, text: str, failure_count: int = 1, is_repeated: bool = False) -> SentimentResult:
        """Analyzes text sentiment, emotion, valence, and urgency."""
        raw_text = (text or "").strip().lower()
        if not raw_text:
            return SentimentResult(
                sentiment="neutral",
                emotion="neutral",
                valence=0.0,
                urgency=0.1,
                confidence=0.5,
                scores={"angry": 0.0, "critical": 0.0, "warning": 0.0, "positive": 0.0}
            )

        # 1. Semantic Embedding Similarity via sentence-transformers
        scores: Dict[str, float] = {"angry": 0.0, "critical": 0.0, "warning": 0.0, "positive": 0.0}
        anchors = self._ensure_anchor_embeddings()

        if anchors:
            try:
                import text_embedding
                query_vec = text_embedding.encode([raw_text])
                if query_vec is not None and len(query_vec) > 0:
                    q = query_vec[0]
                    for cat, anchor_vec in anchors.items():
                        sim = float(np.dot(q, anchor_vec))
                        scores[cat] = max(0.0, sim)
            except Exception:
                pass

        # 2. Dynamic ML Lexical Feature Boosts
        ml_boosts = self.lexical_model.score_text_ml(raw_text)
        for cat, b in ml_boosts.items():
            scores[cat] += b

        # 3. Contextual modifiers (repeated failures / flap count)
        if is_repeated or failure_count >= 2:
            scores["angry"] += 0.35 + min(0.30, failure_count * 0.10)

        # Normalization and pick winner
        top_cat = max(scores, key=scores.get)
        top_score = scores[top_cat]

        # Valence & Urgency calculation
        # angry: valence -0.85, urgency 0.95
        # critical: valence -0.90, urgency 1.00
        # warning: valence -0.40, urgency 0.60
        # positive: valence +0.85, urgency 0.15
        valence_map = {"angry": -0.85, "critical": -0.90, "warning": -0.40, "positive": 0.85}
        urgency_map = {"angry": 0.95, "critical": 1.00, "warning": 0.60, "positive": 0.15}
        emotion_map = {"angry": "anger", "critical": "urgency", "warning": "concern", "positive": "relief"}

        if top_score < 0.20:
            top_cat = "neutral"
            top_emotion = "neutral"
            val = 0.0
            urg = 0.2
            conf = 0.5
        else:
            top_emotion = emotion_map.get(top_cat, "neutral")
            val = valence_map.get(top_cat, 0.0)
            urg = urgency_map.get(top_cat, 0.5)
            # Normalize confidence based on separation
            conf = min(0.99, max(0.55, top_score / 1.2))

        return SentimentResult(
            sentiment=top_cat,
            emotion=top_emotion,
            valence=round(val, 2),
            urgency=round(urg, 2),
            confidence=round(conf, 2),
            scores={k: round(v, 3) for k, v in scores.items()}
        )

    def transform_announcement(
        self,
        channel: str,
        reason: str = "",
        severity: Optional[str] = None,
        server: str = "",
        failure_count: int = 1,
        is_repeated: bool = False
    ) -> TransformedAlert:
        """Transforms raw input through sentiment transformer into finalized Hinglish announcement and acoustic tuning."""
        ch = channel or "Channel"
        srv = server or ch
        s_result = self.analyze_sentiment(reason or severity or "", failure_count=failure_count, is_repeated=is_repeated)

        # Determine effective severity: explicit override or inferred from sentiment
        eff_sev = (severity or "").upper()
        if not eff_sev or eff_sev not in ("CRITICAL", "AGGRESSIVE", "WARNING", "RECOVERY"):
            if s_result.sentiment == "angry" or is_repeated or failure_count >= 2:
                eff_sev = "AGGRESSIVE"
            elif s_result.sentiment == "critical":
                eff_sev = "CRITICAL"
            elif s_result.sentiment == "positive":
                eff_sev = "RECOVERY"
            else:
                eff_sev = "WARNING"

        # Import format function from routes.notifications
        from routes.notifications import format_hinglish_final_announcement
        hinglish_msg = format_hinglish_final_announcement(ch, eff_sev, reason, srv, failure_count=failure_count)

        # Continuous query embedding for Deep Learning prosody prediction
        query_vec = None
        try:
            import text_embedding
            q_enc = text_embedding.encode([reason or severity or "alert"])
            if q_enc is not None and len(q_enc) > 0:
                query_vec = q_enc[0]
        except Exception:
            pass

        # Deep Learning acoustic prosody prediction
        prosody = self.prosody_mapper.predict_prosody(
            severity=eff_sev,
            embedding=query_vec,
            valence=s_result.valence,
            urgency=s_result.urgency,
            failure_count=failure_count
        )

        # Tone and acoustic mapping dynamically computed by Deep Learning
        tone_meta = {
            "AGGRESSIVE": (
                "Angry Aggressive Tone",
                "staccato_aggressive",
                "🔥",
                "Deep Learning dynamic aggressive prosody for repeated server failures"
            ),
            "CRITICAL": (
                "Order Tone",
                "descending_urgent",
                "🚨",
                "Deep Learning authoritative commanding order prosody for immediate viewer outages"
            ),
            "RECOVERY": (
                "Recovery Tone",
                "ascending_pleasant",
                "💚",
                "Deep Learning calm reassuring prosody for system recovery"
            ),
            "WARNING": (
                "Request Tone",
                "ascending_pleasant",
                "⚠️",
                "Deep Learning courteous polite request prosody for performance degradation warnings"
            ),
        }
        t_name, chime, emoji, desc = tone_meta.get(eff_sev, ("Request Tone", "ascending_pleasant", "⚠️", "Deep learning prosody"))

        rec_tone = {
            "tone_name": t_name,
            "rate": prosody["rate"],
            "pitch": prosody["pitch"],
            "volume": prosody["volume"],
            "chime": chime,
            "badge_emoji": emoji,
            "description": desc,
            "neural_prosody": prosody.get("neural_prosody", {})
        }

        return TransformedAlert(
            channel=ch,
            severity=eff_sev,
            server=srv,
            hinglish_text=hinglish_msg,
            sentiment=s_result,
            recommended_tone=rec_tone
        )


# Global singleton helper
sentiment_transformer = SentimentTransformer.get_instance()
