"""Vector stores behind one interface: Pinecone for real use, an in-memory twin for tests / no-key setups."""
import logging
import threading
import time
from dataclasses import dataclass
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class Item:
    id: str
    vector: np.ndarray
    file_id: str
    file_name: str
    locator: str
    title: str
    order: int
    text: str


@dataclass
class Hit:
    id: str
    score: float
    file_id: str
    file_name: str
    locator: str
    title: str
    order: int
    text: str


class VectorStore(Protocol):
    def upsert(self, namespace: str, items: list[Item]) -> None: ...

    def query(self, namespace: str, vector: np.ndarray, top_k: int) -> list[Hit]: ...

    def delete_namespace(self, namespace: str) -> None: ...

    def count(self, namespace: str) -> int: ...


# ---------------------------------------------------------------- in-memory
class MemoryStore:
    def __init__(self):
        self._ns: dict[str, dict[str, Item]] = {}
        self._mu = threading.Lock()

    def upsert(self, namespace, items):
        with self._mu:
            d = self._ns.setdefault(namespace, {})
            for it in items:
                d[it.id] = it

    def query(self, namespace, vector, top_k):
        with self._mu:
            items = list(self._ns.get(namespace, {}).values())
        if not items:
            return []
        mat = np.stack([i.vector for i in items])
        scores = mat @ np.asarray(vector, dtype=np.float32)  # vectors are L2-normalised: dot product = cosine
        order = np.argsort(-scores)[:top_k]
        return [Hit(items[j].id, float(scores[j]), items[j].file_id, items[j].file_name, items[j].locator,
                    items[j].title, items[j].order, items[j].text) for j in order]

    def delete_namespace(self, namespace):
        with self._mu:
            self._ns.pop(namespace, None)

    def count(self, namespace):
        with self._mu:
            return len(self._ns.get(namespace, {}))


# ---------------------------------------------------------------- Pinecone
class PineconeStore:
    BATCH = 100

    def __init__(self, api_key: str, index_name: str, dim: int, cloud: str = "aws", region: str = "us-east-1"):
        from pinecone import Pinecone

        self.pc = Pinecone(api_key=api_key)
        self.index_name, self.dim, self.cloud, self.region = index_name, dim, cloud, region
        self._index = None
        self._mu = threading.Lock()

    def ensure_index(self, timeout: float = 120.0):
        """Create the serverless index on first use (idempotent) and wait until it is ready."""
        from pinecone import ServerlessSpec

        with self._mu:
            if self._index is not None:
                return self._index
            if not self.pc.has_index(self.index_name):
                log.info("creating Pinecone index %s (dim=%s, cosine, %s/%s)", self.index_name, self.dim, self.cloud, self.region)
                self.pc.create_index(name=self.index_name, dimension=self.dim, metric="cosine",
                                     spec=ServerlessSpec(cloud=self.cloud, region=self.region))
            else:
                existing = self.pc.describe_index(self.index_name)
                if int(existing.dimension) != self.dim:
                    raise RuntimeError(f"Pinecone index '{self.index_name}' has dimension {existing.dimension}, "
                                       f"but the embedding model produces {self.dim}")
            deadline = time.time() + timeout
            while not self.pc.describe_index(self.index_name).status["ready"]:
                if time.time() > deadline:
                    raise TimeoutError("Pinecone index did not become ready in time")
                time.sleep(2)
            self._index = self.pc.Index(self.index_name)
            return self._index

    def upsert(self, namespace, items):
        index = self.ensure_index()
        for i in range(0, len(items), self.BATCH):
            index.upsert(namespace=namespace, vectors=[
                {"id": it.id, "values": it.vector.tolist(),
                 "metadata": {"file_id": it.file_id, "file_name": it.file_name, "locator": it.locator, "title": it.title,
                              "order": it.order, "text": it.text}}
                for it in items[i:i + self.BATCH]])

    def query(self, namespace, vector, top_k):
        res = self.ensure_index().query(namespace=namespace, vector=np.asarray(vector).tolist(), top_k=top_k, include_metadata=True)
        out = []
        for m in res.matches:
            md = m.metadata or {}
            out.append(Hit(m.id, float(m.score), md.get("file_id", ""), md.get("file_name", ""), md.get("locator", ""),
                           md.get("title", ""), int(md.get("order", 0)), md.get("text", "")))
        return out

    def delete_namespace(self, namespace):
        try:
            self.ensure_index().delete(delete_all=True, namespace=namespace)
        except Exception as e:  # noqa: BLE001 - deleting a namespace that never existed returns 404; that is fine
            if "not found" not in str(e).lower() and "404" not in str(e):
                raise

    def count(self, namespace):
        stats = self.ensure_index().describe_index_stats()
        ns = (stats.namespaces or {}).get(namespace)
        return int(ns.vector_count) if ns else 0
