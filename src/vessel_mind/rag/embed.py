"""Embedders: the Embedder protocol and the default CPU provider, fastembed.

The spec makes embeddings provider-configurable (§2): fastembed on CPU by
default, or an OpenAI-compatible /v1/embeddings endpoint. Only fastembed
exists so far; another provider implements the same protocol.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

# Multilingual (Chinese and English), 8192-token input, 768 dimensions,
# Apache-2.0, 0.64 GB. Questions may be in Traditional Chinese over English
# documents (§5.6), and chunks reach 800 tokens, past the 512 or 128 tokens
# of the smaller multilingual models fastembed offers.
DEFAULT_MODEL = "jinaai/jina-embeddings-v2-base-zh"


class Embedder(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def dim(self) -> int: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class FastEmbedEmbedder:
    """fastembed on CPU. The model loads (and downloads, once) on the first embed call,
    so opening an existing index costs nothing."""

    def __init__(self, model_id: str = DEFAULT_MODEL, *, cache_dir: Path | None = None) -> None:
        from fastembed import TextEmbedding

        dims = {m["model"]: m["dim"] for m in TextEmbedding.list_supported_models()}
        if model_id not in dims:
            raise ValueError(f"fastembed does not support the embedding model {model_id!r}")
        self._model_id = model_id
        self._dim = int(dims[model_id])
        self._cache_dir = cache_dir
        self._model: Any = None

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dim(self) -> int:
        return self._dim

    def _loaded(self) -> Any:
        if self._model is None:
            from fastembed import TextEmbedding

            cache = str(self._cache_dir) if self._cache_dir is not None else None
            self._model = TextEmbedding(self._model_id, cache_dir=cache)
        return self._model

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._loaded().passage_embed(list(texts))]

    def embed_query(self, text: str) -> list[float]:
        [vector] = list(self._loaded().query_embed(text))
        return vector.tolist()
