"""The one text-embedding model: all-MiniLM-L6-v2 (384 dims), run by ONNX Runtime through fastembed (no PyTorch).

Used for node embeddings (Neo4j `embedding`, searched by GraphRAG and compared in reports) and for questions.
Loaded once per process, in the background at startup (`preload`), so the first question doesn't wait.
CHATBOT_EMBED_MODEL picks another fastembed model; FASTEMBED_CACHE_PATH is where its files live.

Every text is fingerprinted (SHA-1 of the model name and the text) before it is embedded, so the same text is never
embedded twice: duplicates inside one batch are embedded once, a fingerprint seen before comes from memory, and the
vectors are also kept in SQLite (EMBED_CACHE_DB, next to METRICS_DB) so they survive restarts and are shared by every
user of embeddings (GraphRAG, hybrid search, learning, activity logs, standards, alerts). A new model means new
fingerprints, so vectors from another model are never reused.
"""

import hashlib
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

MODEL = os.environ.get("CHATBOT_EMBED_MODEL") or "sentence-transformers/all-MiniLM-L6-v2"
DIM = 384

_model = None
_lock = threading.Lock()

DISK_ITEMS = int(os.environ.get("EMBED_CACHE_MAX", "100000"))  # ~150 MB; the least recently used beyond this go
MEMORY_ITEMS = 20000  # fingerprints kept in memory (~30 MB of float32 vectors at 384 dims)
_memory: "OrderedDict[str, np.ndarray]" = OrderedDict()
_memory_lock = threading.Lock()
_stats = {"hits_memory": 0, "hits_disk": 0, "embedded": 0, "batch_duplicates": 0}
_db_path: Optional[str] = None


def fingerprint(text: str) -> str:
    """SHA-1 of the model name and the exact text: the identity of an embedding."""
    return hashlib.sha1(f"{MODEL}\n{text}".encode("utf-8")).hexdigest()


def _db() -> Optional[sqlite3.Connection]:
    global _db_path
    if _db_path is None:
        metrics = os.environ.get("METRICS_DB") or str(Path(__file__).parent / "metrics.db")
        _db_path = os.environ.get("EMBED_CACHE_DB") or str(Path(metrics).with_name("embeddings.db"))
    if _db_path in ("", "off", "0"):
        return None
    try:
        db = sqlite3.connect(_db_path, timeout=5)
        db.execute("CREATE TABLE IF NOT EXISTS vectors (fp TEXT PRIMARY KEY, dim INTEGER NOT NULL, vec BLOB NOT NULL, "
                   "used REAL)")
        if "used" not in {r[1] for r in db.execute("PRAGMA table_info(vectors)")}:
            db.execute("ALTER TABLE vectors ADD COLUMN used REAL")
        return db
    except Exception:
        return None  # a read-only or missing folder: memory cache only


def _remember(fp: str, vec: np.ndarray) -> None:
    with _memory_lock:
        _memory[fp] = vec
        _memory.move_to_end(fp)
        while len(_memory) > MEMORY_ITEMS:
            _memory.popitem(last=False)


_inserts_since_prune = 0


def _prune(db: sqlite3.Connection, max_items: Optional[int] = None) -> int:
    """Keeps the disk cache under DISK_ITEMS vectors (checked every 1,000 new ones): the least recently used go."""
    global _inserts_since_prune
    _inserts_since_prune += 1
    limit = DISK_ITEMS if max_items is None else max_items
    if max_items is None and _inserts_since_prune < 1000:
        return 0
    _inserts_since_prune = 0
    count = db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
    extra = count - limit
    if extra <= 0:
        return 0
    with db:
        db.execute("DELETE FROM vectors WHERE fp IN (SELECT fp FROM vectors ORDER BY COALESCE(used, 0) LIMIT ?)", (extra,))
    return extra


def cache_stats() -> Dict[str, int]:
    with _memory_lock:
        return {**_stats, "in_memory": len(_memory)}


def model():
    global _model
    with _lock:
        if _model is None:
            from fastembed import TextEmbedding  # imported here: onnxruntime is only needed once something embeds
            _model = TextEmbedding(MODEL, cache_dir=os.environ.get("FASTEMBED_CACHE_PATH"))
        return _model


def _embed_new(texts: List[str]) -> np.ndarray:
    vectors = np.asarray(list(model().embed(list(texts))), dtype="float32")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.where(norms == 0, 1, norms)


def encode(texts: List[str]) -> np.ndarray:
    """L2-normalised vectors, one row per text. Only texts whose fingerprint was never seen are run through the model."""
    texts = list(texts)
    if not texts:
        return np.zeros((0, DIM), dtype="float32")
    fps = [fingerprint(t) for t in texts]
    found: Dict[str, np.ndarray] = {}
    with _memory_lock:
        for fp in fps:
            if fp in _memory and fp not in found:
                found[fp] = _memory[fp]
                _memory.move_to_end(fp)
                _stats["hits_memory"] += 1
    wanted = [fp for fp in dict.fromkeys(fps) if fp not in found]
    if wanted:
        db = _db()
        hit_fps: List[str] = []
        if db is not None:
            try:
                for i in range(0, len(wanted), 500):
                    chunk = wanted[i:i + 500]
                    rows = db.execute(f"SELECT fp, dim, vec FROM vectors WHERE fp IN ({','.join('?' * len(chunk))})",
                                      chunk).fetchall()
                    for fp, dim, blob in rows:
                        vec = np.frombuffer(blob, dtype="float32").copy()
                        if dim == len(vec):
                            found[fp] = vec
                            hit_fps.append(fp)
                            _remember(fp, vec)
                            _stats["hits_disk"] += 1
            except Exception:
                pass
        missing = [fp for fp in wanted if fp not in found]
        if missing:
            first = {}  # the first text for each new fingerprint: duplicates in the batch are embedded once
            for t, fp in zip(texts, fps):
                first.setdefault(fp, t)
            _stats["batch_duplicates"] += len(texts) - len(set(fps))
            new = _embed_new([first[fp] for fp in missing])
            _stats["embedded"] += len(missing)
            for fp, vec in zip(missing, new):
                found[fp] = vec
                _remember(fp, vec)
            if db is not None:
                try:
                    now = time.time()
                    with db:
                        db.executemany("INSERT OR IGNORE INTO vectors (fp, dim, vec, used) VALUES (?, ?, ?, ?)",
                                       [(fp, int(found[fp].shape[0]), found[fp].astype("float32").tobytes(), now)
                                        for fp in missing])
                    _prune(db)
                except Exception:
                    pass
        if db is not None and hit_fps:
            try:
                with db:  # least-recently-used bookkeeping, so pruning drops what nobody asks for any more
                    db.executemany("UPDATE vectors SET used = ? WHERE fp = ?", [(time.time(), fp) for fp in hit_fps])
            except Exception:
                pass
        if db is not None:
            db.close()
    return np.stack([found[fp] for fp in fps]).astype("float32")


def preload() -> Optional[threading.Thread]:
    """Loads the model in a background thread (a few seconds), so the first real use is fast."""
    if _model is not None:
        return None
    thread = threading.Thread(target=lambda: model(), name="embedding-preload", daemon=True)
    thread.start()
    return thread
