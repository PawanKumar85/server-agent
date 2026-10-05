"""SHA-1 fingerprinted embedding cache: the same text is embedded once, in a batch, across calls and across restarts."""

import numpy as np
import pytest

import text_embedding as te


@pytest.fixture
def fake_model(monkeypatch, tmp_path):
    calls = []

    def embed_new(texts):
        calls.append(list(texts))
        rows = [np.array([len(t), sum(map(ord, t)) % 97, 1.0], dtype="float32") for t in texts]
        return np.stack([r / np.linalg.norm(r) for r in rows])
    monkeypatch.setattr(te, "_embed_new", embed_new)
    monkeypatch.setattr(te, "_db_path", str(tmp_path / "embeddings.db"))
    te._memory.clear()
    yield calls
    te._memory.clear()


def test_duplicates_in_a_batch_are_embedded_once(fake_model):
    out = te.encode(["a", "b", "a", "a", "b"])
    assert fake_model == [["a", "b"]] and out.shape == (5, 3)
    assert np.allclose(out[0], out[2]) and np.allclose(out[1], out[4])


def test_seen_texts_come_from_memory_then_from_disk_after_a_restart(fake_model):
    first = te.encode(["xcode4 is down", "rang manch"])
    te.encode(["rang manch"])
    assert len(fake_model) == 1  # memory
    te._memory.clear()  # a restart: memory is gone, the SQLite cache is not
    again = te.encode(["xcode4 is down", "rang manch", "new text"])
    assert fake_model[-1] == ["new text"] and np.allclose(first, again[:2])


def test_the_fingerprint_includes_the_model(monkeypatch):
    fp = te.fingerprint("same text")
    monkeypatch.setattr(te, "MODEL", "another/model")
    assert te.fingerprint("same text") != fp  # vectors from another model are never reused


def test_graphrag_profile_hashes_use_the_same_fingerprint():
    import graphrag
    assert graphrag._hash("server xcode4") == te.fingerprint("server xcode4")


def test_the_disk_cache_keeps_the_most_recently_used(fake_model):
    import time as _t
    te.encode([f"text {i}" for i in range(6)])
    _t.sleep(0.01)
    te._memory.clear()
    te.encode(["text 0", "text 1"])  # used again: these must survive the pruning
    db = te._db()
    assert te._prune(db, max_items=3) == 3
    left = {fp for (fp,) in db.execute("SELECT fp FROM vectors")}
    assert te.fingerprint("text 0") in left and te.fingerprint("text 1") in left and len(left) == 3
