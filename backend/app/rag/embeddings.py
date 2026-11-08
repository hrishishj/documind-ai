"""Local sentence-transformer embeddings (no API key, no per-token cost, documents never leave the machine)."""
import logging
import threading

import numpy as np

from app.core.config import get_settings

log = logging.getLogger(__name__)
# bge models are trained so that *queries* carry this instruction while documents do not
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class Embedder:
    def __init__(self, model_name: str | None = None):
        self.model_name = model_name or get_settings().embedding_model
        self._model = None
        self._lock = threading.Lock()

    @property
    def model(self):
        with self._lock:  # load once, even if several uploads trigger indexing at the same time
            if self._model is None:
                from sentence_transformers import SentenceTransformer

                log.info("loading embedding model %s", self.model_name)
                self._model = SentenceTransformer(self.model_name, device="cpu")
            return self._model

    @property
    def dim(self) -> int:
        return int(self.model.get_sentence_embedding_dimension())

    def embed_documents(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.asarray(self.model.encode(texts, batch_size=batch_size, normalize_embeddings=True,
                                            show_progress_bar=False), dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        return np.asarray(self.model.encode([QUERY_PREFIX + text], normalize_embeddings=True,
                                            show_progress_bar=False)[0], dtype=np.float32)


_default: Embedder | None = None


def get_embedder() -> Embedder:
    global _default
    if _default is None:
        _default = Embedder()
    return _default
