"""The one text-embedding model: all-MiniLM-L6-v2 (384 dims), run by ONNX Runtime through fastembed (no PyTorch).

Used for node embeddings (Neo4j `embedding`, searched by GraphRAG and compared in reports) and for questions.
Loaded once per process, in the background at startup (`preload`), so the first question doesn't wait.
CHATBOT_EMBED_MODEL picks another fastembed model; FASTEMBED_CACHE_PATH is where its files live.
"""

import os
import threading
from typing import List, Optional

import numpy as np

MODEL = os.environ.get("CHATBOT_EMBED_MODEL") or "sentence-transformers/all-MiniLM-L6-v2"
DIM = 384

_model = None
_lock = threading.Lock()


def model():
    global _model
    with _lock:
        if _model is None:
            from fastembed import TextEmbedding  # imported here: onnxruntime is only needed once something embeds
            _model = TextEmbedding(MODEL, cache_dir=os.environ.get("FASTEMBED_CACHE_PATH"))
        return _model


def encode(texts: List[str]) -> np.ndarray:
    """L2-normalised vectors, one row per text."""
    if not texts:
        return np.zeros((0, DIM), dtype="float32")
    vectors = np.asarray(list(model().embed(list(texts))), dtype="float32")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.where(norms == 0, 1, norms)


def preload() -> Optional[threading.Thread]:
    """Loads the model in a background thread (a few seconds), so the first real use is fast."""
    if _model is not None:
        return None
    thread = threading.Thread(target=lambda: model(), name="embedding-preload", daemon=True)
    thread.start()
    return thread
