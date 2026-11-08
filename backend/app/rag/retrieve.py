"""Indexing and retrieval: chunks -> vectors -> ranked, citable sources."""
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from app.rag.embeddings import Embedder
from app.rag.extract import extract_chunks
from app.rag.store import Hit, Item, VectorStore

log = logging.getLogger(__name__)

STOP = {"the", "and", "for", "with", "that", "this", "from", "into", "what", "which", "when", "where", "who", "how", "was",
        "were", "are", "is", "does", "did", "have", "has", "had", "about", "tell", "give", "show", "can", "you", "your", "their",
        "there", "than", "then", "been", "being", "its", "per", "all", "any", "one", "not", "but", "year", "total"}
CANDIDATES = 24        # vector results fetched before re-ranking
# final = (1 - LEX_WEIGHT) * cosine + LEX_WEIGHT * lexical overlap.
# 0.15 was chosen by measuring 13 questions on the real sample documents (tests/test_rag_eval.py): best recall@8 and the
# widest score gap between on-topic and off-topic questions. 0.0 and 0.25 were both worse.
LEX_WEIGHT = 0.15


@dataclass
class Source:
    n: int                 # 1-based number the LLM cites as [n]
    file_id: str
    file_name: str
    locator: str
    title: str
    text: str
    score: float           # re-ranked relevance in [0, 1]
    cosine: float
    url: str = ""          # web sources only
    kind: str = "document"  # "document" | "web"


def _terms(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9.,$%-]*", s.lower().replace(",", "")) if len(w) >= 3 and w not in STOP}


def lexical_score(query: str, text: str) -> float:
    """Fraction of the query's distinctive terms (words AND numbers) that appear in the passage.
    Embeddings blur exact figures; this keeps "331839" or "Section 13" findable."""
    q = _terms(query)
    if not q:
        return 0.0
    t = _terms(text)
    return len(q & t) / len(q)


class Indexer:
    """Owns the embedder + store. One namespace per conversation keeps documents from different chats apart."""

    def __init__(self, embedder: Embedder, store: VectorStore):
        self.embedder, self.store = embedder, store
        self._mu = threading.Lock()  # one embedding job at a time: keeps CPU use predictable

    def index_file(self, namespace: str, file_id: str, file_name: str, path: str | Path) -> int:
        chunks = extract_chunks(path)
        if not chunks:
            return 0
        with self._mu:
            vecs = self.embedder.embed_documents([self._embed_text(c) for c in chunks])
        items = [Item(f"{file_id}:{c.order}", vecs[i], file_id, file_name, c.locator, c.title, c.order, c.text)
                 for i, c in enumerate(chunks)]
        self.store.upsert(namespace, items)
        return len(items)

    @staticmethod
    def _embed_text(c) -> str:
        # the heading/sheet/slide title is part of what a passage is *about*, so it is embedded with it
        return f"{c.title}\n{c.text}" if c.title and c.title not in c.text[:200] else c.text

    def search(self, namespace: str, query: str, k: int = 6) -> list[Source]:
        hits: list[Hit] = self.store.query(namespace, self.embedder.embed_query(query), CANDIDATES)
        scored = []
        for h in hits:
            lex = lexical_score(query, h.text + " " + h.title)
            scored.append(((1 - LEX_WEIGHT) * h.score + LEX_WEIGHT * lex, h))
        scored.sort(key=lambda x: -x[0])
        out: list[Source] = []
        seen: set[str] = set()
        for score, h in scored:
            key = re.sub(r"\W+", "", h.text.lower())[:160]
            if key in seen:  # near-duplicate passages (repeated tables across sheets) add nothing
                continue
            seen.add(key)
            out.append(Source(len(out) + 1, h.file_id, h.file_name, h.locator, h.title, h.text, round(score, 4), round(h.score, 4)))
            if len(out) >= k:
                break
        return out
