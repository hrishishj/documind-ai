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


class FakeWeb:
    """Scripted web search: records queries, returns canned results or raises. Never touches the network."""

    def __init__(self, results=None, error=None):
        from app.research.search import WebResult
        self.results = results if results is not None else [
            WebResult("Solid-State Battery Technology", "https://www.mdpi.com/1996-1073/19/11/2659",
                      "Solid-state batteries replace the liquid electrolyte with a solid one, enabling higher energy density of "
                      "about 400 Wh/kg in 2026 prototypes.", 0.93, "fake"),
            WebResult("Solid-State Batteries 2026-2036", "https://www.idtechex.com/en/research-report/solid-state-batteries/1130",
                      "The solid-state battery industry is expected to reach commercial production of electric vehicle cells "
                      "by 2028 according to multiple manufacturers.", 0.90, "fake")]
        self.error, self.queries = error, []

    def search(self, query, max_results=5):
        self.queries.append(query)
        if self.error:
            raise self.error
        return self.results[:max_results]
