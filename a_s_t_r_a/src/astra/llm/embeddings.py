"""Embedding service — generates vectors via LiteLLM / OpenAI-compatible API."""

from __future__ import annotations

import litellm
from loguru import logger

from astra.config import settings


class EmbeddingService:
    """Async embedding generation."""

    async def embed(self, text: str) -> list[float]:
        """Return the embedding vector for a single text."""
        return (await self.embed_batch([text]))[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Return embedding vectors for a batch of texts."""
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
            logger.error("Embedding call failed: {}", exc)
            raise


embedding_service = EmbeddingService()
