"""Offline, deterministic RAG harness for knowledge-base retrieval tests.

Seeds an isolated Chroma collection with the dataset's KB articles and runs a
similarity query, mirroring ``services.knowledge_base_service`` behaviour
(``collection.query`` -> ranked results). A deterministic, dependency-free
embedding function is injected so the suite needs no model download and no
network, and produces identical rankings on every run and OS.

The real knowledge base is never touched: this uses an in-memory Chroma client.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import List, Optional

from .schema import KbArticle

_EMBED_DIM = 256


def _tokenize(text: str) -> List[str]:
    return [t for t in "".join(c.lower() if c.isalnum() else " " for c in text).split() if t]


def _embed(text: str) -> List[float]:
    """Deterministic bag-of-words hashing embedding (L2-normalised)."""
    vec = [0.0] * _EMBED_DIM
    for tok in _tokenize(text):
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
        vec[h % _EMBED_DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def _make_embedding_function():
    """Build a Chroma-compatible embedding function (offline, deterministic).

    Subclasses Chroma's official ``EmbeddingFunction`` base so query/document
    embedding delegation (``embed_query`` -> ``__call__``) works across Chroma
    versions. Imported lazily so the suite can skip cleanly when Chroma is
    absent.
    """
    from chromadb.api.types import EmbeddingFunction

    class DeterministicEmbeddingFunction(EmbeddingFunction):
        def __init__(self) -> None:
            pass

        def __call__(self, input):  # noqa: A002 - name mandated by Chroma API
            return [_embed(doc) for doc in input]

        @staticmethod
        def name() -> str:
            return "deterministic-hash-ef"

        def get_config(self) -> dict:
            return {}

        @classmethod
        def build_from_config(cls, config):
            return cls()

    return DeterministicEmbeddingFunction()


@dataclass
class RagResult:
    top_id: Optional[str]
    ranked_ids: List[str]
    distances: List[float]


class RagHarness:
    """Isolated, in-memory Chroma store seeded from a regression case."""

    def __init__(self, collection_name: str = "regression_kb"):
        import chromadb  # imported lazily so tests can skip if unavailable

        self._client = chromadb.EphemeralClient()
        # Recreate cleanly in case of name reuse within a session.
        try:
            self._client.delete_collection(collection_name)
        except Exception:
            pass
        self._collection = self._client.create_collection(
            name=collection_name,
            embedding_function=_make_embedding_function(),
            metadata={"hnsw:space": "cosine"},
        )

    def seed(self, articles: List[KbArticle]) -> None:
        if not articles:
            return
        self._collection.add(
            ids=[a.id for a in articles],
            documents=[f"{a.title}\n{a.content}" for a in articles],
            metadatas=[{**a.metadata, "title": a.title} for a in articles],
        )

    def retrieve(self, query: str, n_results: int = 3) -> RagResult:
        count = self._collection.count()
        if count == 0 or not query:
            return RagResult(top_id=None, ranked_ids=[], distances=[])
        res = self._collection.query(
            query_texts=[query], n_results=min(n_results, count)
        )
        ids = res["ids"][0] if res.get("ids") else []
        distances = res["distances"][0] if res.get("distances") else []
        return RagResult(
            top_id=ids[0] if ids else None,
            ranked_ids=list(ids),
            distances=list(distances),
        )
