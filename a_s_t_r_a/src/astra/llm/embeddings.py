"""Embedding service — generates vectors via LiteLLM / OpenAI-compatible API with mock fallback."""

from __future__ import annotations

import hashlib
import math
import random
from typing import List

import litellm
from loguru import logger

from astra.config import LLMProvider, settings


def _deterministic_fake_embedding(text: str, dim: int = 1024) -> List[float]:
    """Create a deterministic pseudo-embedding from text hash — useful for tests."""
    seed = int(hashlib.md5(text.encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)
    vec = [rng.uniform(-1, 1) for _ in range(dim)]
    # Normalize
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class EmbeddingService:
    """Async embedding generation with mock fallback."""

    async def embed(self, text: str) -> List[float]:
        """Return the embedding vector for a single text."""
        return (await self.embed_batch([text]))[0]

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Return embedding vectors for a batch of texts."""
        if not texts:
            return []

        # Mock provider
        if settings.llm_default_provider == LLMProvider.MOCK:
            dim = settings.embedding_dimensions
            return [_deterministic_fake_embedding(t, dim) for t in texts]

        logger.debug("Embedding batch of {} texts", len(texts))
        try:
            response = await litellm.aembedding(
                model=f"openai/{settings.embedding_model}",
                input=texts,
                api_base=settings.embedding_url,
                api_key=settings.embedding_key,
            )
            vectors = [item["embedding"] for item in response.data]
            logger.debug("Got {} embeddings, dim={}", len(vectors), len(vectors[0]) if vectors else 0)
            return vectors
        except Exception as exc:
            logger.warning("Embedding call failed ({}), using deterministic fake embeddings", exc)
            dim = settings.embedding_dimensions
            return [_deterministic_fake_embedding(t, dim) for t in texts]


embedding_service = EmbeddingService()
