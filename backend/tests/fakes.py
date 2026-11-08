"""Deterministic stand-ins so tests never load the real embedding model or touch the real Pinecone index."""
import hashlib
import re

import numpy as np

from app.rag.retrieve import Indexer
from app.rag.store import MemoryStore

DIM = 256


class FakeEmbedder:
    """Hashed bag-of-words vectors: texts that share words are similar. Same method for queries and documents."""

    model_name = "fake-bow"
    dim = DIM

    @staticmethod
    def _vec(text: str) -> np.ndarray:
        v = np.zeros(DIM, dtype=np.float32)
        for w in re.findall(r"[a-z0-9]{3,}", text.lower().replace(",", "")):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1.0
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed_documents(self, texts, batch_size=32):
        return np.stack([self._vec(t) for t in texts]) if texts else np.zeros((0, DIM), dtype=np.float32)

    def embed_query(self, text):
        return self._vec(text)


def fake_indexer() -> Indexer:
    return Indexer(FakeEmbedder(), MemoryStore())
