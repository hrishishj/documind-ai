"""One contract, run against MemoryStore (always) and the REAL PineconeStore (when a key is configured).

Pinecone serverless is eventually consistent, so assertions about counts/queries poll briefly.
"""
import time
import uuid

import numpy as np
import pytest

from app.core.config import get_settings
from app.rag.store import Item, MemoryStore, PineconeStore

DIM = 384  # the real index dimension, so the same vectors work on both backends


def vec(i: int) -> np.ndarray:
    v = np.zeros(DIM, dtype=np.float32)
    v[i % DIM] = 1.0
    return v


def items(n=5, file_id="f1"):
    return [Item(f"{file_id}:{i}", vec(i), file_id, "doc.docx", f"¶{i}", "Intro", i, f"passage {i}") for i in range(n)]


def wait_until(pred, timeout=75.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(1.5)
    return pred()


@pytest.fixture(params=["memory", "pinecone"])
def store(request):
    if request.param == "memory":
        yield MemoryStore()
        return
    s = get_settings()
    if not s.pinecone_api_key:
        pytest.skip("no Pinecone key configured")
    yield PineconeStore(s.pinecone_api_key, s.pinecone_index_name, s.embedding_dim)


@pytest.fixture()
def ns(store):
    names = []

    def make(suffix=""):
        n = f"test-{uuid.uuid4().hex[:12]}{suffix}"
        names.append(n)
        return n
    yield make
    for n in names:
        store.delete_namespace(n)


def test_nearest_vector_wins_and_results_are_ranked(store, ns):
    n = ns()
    store.upsert(n, items(5))
    assert wait_until(lambda: store.count(n) == 5)
    hits = store.query(n, vec(3), top_k=3)
    assert hits[0].id == "f1:3" and hits[0].score == pytest.approx(1.0, abs=1e-3)
    assert [h.score for h in hits] == sorted([h.score for h in hits], reverse=True)
    assert (hits[0].text, hits[0].locator, hits[0].file_name, hits[0].title, hits[0].order) == ("passage 3", "¶3", "doc.docx", "Intro", 3)


def test_namespaces_are_isolated_and_deletable(store, ns):
    a, b = ns(), ns("-other")
    store.upsert(a, items(3, "a"))
    store.upsert(b, items(3, "b"))
    assert wait_until(lambda: store.count(a) == 3 and store.count(b) == 3)
    assert {h.file_id for h in store.query(a, vec(0), 10)} == {"a"}
    store.delete_namespace(a)
    assert wait_until(lambda: store.count(a) == 0)
    assert store.query(a, vec(0), 10) == []
    assert store.count(b) == 3  # deleting one conversation never touches another


def test_upsert_is_idempotent_by_id(store, ns):
    n = ns()
    store.upsert(n, items(3))
    store.upsert(n, items(3))
    assert wait_until(lambda: store.count(n) == 3)
    time.sleep(1)
    assert store.count(n) == 3  # re-indexing the same file must not duplicate passages
    store.upsert(n, [Item("f1:0", vec(0), "f1", "doc.docx", "¶0", "Intro", 0, "REPLACED")])
    assert wait_until(lambda: store.query(n, vec(0), 1)[0].text == "REPLACED")


def test_empty_namespace_and_deleting_a_missing_one_are_harmless(store, ns):
    n = ns()
    assert store.query(n, vec(0), 5) == []
    store.delete_namespace("never-existed-" + n)  # must not raise


def test_batches_larger_than_the_pinecone_limit_are_all_stored(store, ns):
    n = ns()
    big = [Item(f"f:{i}", vec(i), "f", "d.pdf", f"p.{i}", "", i, f"t{i}") for i in range(250)]  # > 2 batches of 100
    store.upsert(n, big)
    assert wait_until(lambda: store.count(n) == 250)


def test_real_embeddings_end_to_end():
    """Real model + real store: the semantically right passage is returned first."""
    s = get_settings()
    if not s.pinecone_api_key:
        pytest.skip("no Pinecone key configured")
    from app.rag.embeddings import get_embedder
    emb, store = get_embedder(), PineconeStore(s.pinecone_api_key, s.pinecone_index_name, s.embedding_dim)
    n = f"test-{uuid.uuid4().hex[:12]}"
    texts = ["Total revenue for fiscal 2026 was 331.8 billion dollars.", "The cat sat on the windowsill."]
    vecs = emb.embed_documents(texts)
    try:
        store.upsert(n, [Item(f"f:{i}", vecs[i], "f", "r.pdf", f"p.{i + 1}", "", i, t) for i, t in enumerate(texts)])
        assert wait_until(lambda: store.count(n) >= 2)
        hits = store.query(n, emb.embed_query("what was the revenue in 2026?"), 2)
        assert hits and hits[0].id == "f:0" and hits[0].locator == "p.1" and hits[0].score > hits[1].score
    finally:
        store.delete_namespace(n)
