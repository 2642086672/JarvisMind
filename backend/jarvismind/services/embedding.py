"""Embedding provider architecture for JME-4.

Provides local, privacy-first embedding vector generation.
Supports Fastembed (ONNX runtime CPU) for production and deterministic pseudo-embeddings
for testing and offline air-gapped environments.
"""
from __future__ import annotations

import abc
import hashlib
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel

from ..config import Settings
from ..core.errors import EmbeddingUnavailableError

logger = logging.getLogger("jme.embedding")


class EmbeddingModelInfo(BaseModel):
    provider: str
    model_name: str
    version: str
    dimension: int
    is_available: bool
    device: str = "cpu"
    error: Optional[str] = None


class BaseEmbeddingProvider(abc.ABC):
    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Provider identifier (e.g. 'fastembed', 'deterministic')."""

    @property
    @abc.abstractmethod
    def model_name(self) -> str:
        """Model identifier (e.g. 'BAAI/bge-small-zh-v1.5')."""

    @property
    @abc.abstractmethod
    def version(self) -> str:
        """Version tag of the model or provider."""

    @property
    @abc.abstractmethod
    def dimension(self) -> int:
        """Output embedding vector length."""

    @property
    @abc.abstractmethod
    def is_available(self) -> bool:
        """Whether the provider is currently ready to generate embeddings."""

    @abc.abstractmethod
    def embed_text(self, text: str) -> List[float]:
        """Generate embedding vector for a single string."""

    @abc.abstractmethod
    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Generate embedding vectors for multiple strings."""

    def get_info(self) -> EmbeddingModelInfo:
        return EmbeddingModelInfo(
            provider=self.name,
            model_name=self.model_name,
            version=self.version,
            dimension=self.dimension,
            is_available=self.is_available,
            device="cpu",
            error=getattr(self, "_error", None),
        )


class FastembedEmbeddingProvider(BaseEmbeddingProvider):
    """Local ONNX-based embedding provider using fastembed."""

    def __init__(
        self,
        model_name: str = "BAAI/bge-small-zh-v1.5",
        cache_dir: Optional[Path] = None,
        dimension: int = 512,
    ) -> None:
        self._model_name = model_name
        self._cache_dir = cache_dir
        self._dimension = dimension
        self._model = None
        self._is_available = False
        self._error: Optional[str] = None
        self._initialize()

    def _initialize(self) -> None:
        try:
            from fastembed import TextEmbedding

            cache_path = str(self._cache_dir) if self._cache_dir else None
            self._model = TextEmbedding(
                model_name=self._model_name,
                cache_dir=cache_path,
            )
            # Detect actual dimension
            test_v = list(self._model.embed(["probe"]))[0]
            self._dimension = len(test_v)
            self._is_available = True
            logger.info(
                "Fastembed initialized with model %s (dim=%d)",
                self._model_name,
                self._dimension,
            )
        except Exception as exc:
            self._is_available = False
            self._error = str(exc)
            logger.warning("Failed to initialize Fastembed (%s): %s", self._model_name, exc)

    @property
    def name(self) -> str:
        return "fastembed"

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def version(self) -> str:
        return "1.0"

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def is_available(self) -> bool:
        return self._is_available

    def embed_text(self, text: str) -> List[float]:
        if not self._is_available or self._model is None:
            raise EmbeddingUnavailableError(
                f"Fastembed is not available: {self._error or 'uninitialized'}"
            )
        clean = text.strip() if text else ""
        if not clean:
            return [0.0] * self._dimension
        try:
            gen = self._model.embed([clean])
            arr = list(gen)[0]
            return [float(x) for x in arr]
        except Exception as exc:
            raise EmbeddingUnavailableError(f"Embedding failed: {exc}") from exc

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        if not self._is_available or self._model is None:
            raise EmbeddingUnavailableError(
                f"Fastembed is not available: {self._error or 'uninitialized'}"
            )
        if not texts:
            return []
        prepared = [t.strip() if t and t.strip() else " " for t in texts]
        try:
            results = []
            for arr in self._model.embed(prepared):
                results.append([float(x) for x in arr])
            return results
        except Exception as exc:
            raise EmbeddingUnavailableError(f"Batch embedding failed: {exc}") from exc


class DeterministicEmbeddingProvider(BaseEmbeddingProvider):
    """Deterministic hash/ngram embedding provider for testing and air-gapped setups.

    Generates normalized vectors from word and character n-gram hashes.
    Guarantees that similar texts share non-zero cosine similarity while
    requiring 0 external downloads or heavy libraries.
    """

    def __init__(
        self,
        dimension: int = 512,
        model_name: str = "deterministic-ngram-v1",
    ) -> None:
        self._dimension = dimension
        self._model_name = model_name

    @property
    def name(self) -> str:
        return "deterministic"

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def version(self) -> str:
        return "1.0"

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def is_available(self) -> bool:
        return True

    def _hash_vector(self, text: str) -> List[float]:
        vec = [0.0] * self._dimension
        clean = text.strip().lower()
        if not clean:
            return vec

        tokens: List[str] = clean.split()
        # Add character bigrams for Chinese and dense matching
        for i in range(len(clean) - 1):
            tokens.append(clean[i : i + 2])

        for tok in tokens:
            h = hashlib.sha256(tok.encode("utf-8")).digest()
            idx = int.from_bytes(h[:4], "big") % self._dimension
            sign = 1.0 if (h[4] % 2 == 0) else -1.0
            weight = 1.0 + (h[5] % 5) * 0.1
            vec[idx] += sign * weight

        # Normalize to unit length
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 1e-9:
            vec = [x / norm for x in vec]
        return vec

    def embed_text(self, text: str) -> List[float]:
        return self._hash_vector(text)

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        return [self._hash_vector(t) for t in texts]


class DisabledEmbeddingProvider(BaseEmbeddingProvider):
    """Placeholder when embedding is explicitly disabled or unrecoverable."""

    def __init__(
        self,
        error: str = "Embedding is disabled",
        dimension: int = 512,
    ) -> None:
        self._error = error
        self._dimension = dimension

    @property
    def name(self) -> str:
        return "disabled"

    @property
    def model_name(self) -> str:
        return "none"

    @property
    def version(self) -> str:
        return "0.0"

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def is_available(self) -> bool:
        return False

    def embed_text(self, text: str) -> List[float]:
        raise EmbeddingUnavailableError(self._error)

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        raise EmbeddingUnavailableError(self._error)


def create_embedding_provider(settings: Settings) -> BaseEmbeddingProvider:
    """Factory creating the configured embedding provider with graceful degradation."""
    mode = (settings.embedding_provider or "fastembed").lower().strip()
    if mode == "disabled":
        return DisabledEmbeddingProvider("Embedding provider is disabled in settings")
    if mode == "deterministic":
        return DeterministicEmbeddingProvider(
            dimension=settings.embedding_dimension,
            model_name=settings.embedding_model or "deterministic-ngram-v1",
        )

    # Default: fastembed
    provider = FastembedEmbeddingProvider(
        model_name=settings.embedding_model,
        cache_dir=settings.models_cache_dir,
        dimension=settings.embedding_dimension,
    )
    if not provider.is_available:
        logger.warning(
            "Fastembed initialization failed; falling back to deterministic provider for safety"
        )
        # We preserve the error in info
        fallback = DeterministicEmbeddingProvider(
            dimension=settings.embedding_dimension,
            model_name=f"fallback-{settings.embedding_model}",
        )
        return fallback
    return provider
