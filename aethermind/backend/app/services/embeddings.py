import hashlib
import math
from dataclasses import dataclass

import httpx

from app.config import settings


@dataclass
class EmbeddingResult:
    embedding: list[float]
    model: str
    provider: str


def deterministic_embedding(text: str, dimensions: int | None = None) -> list[float]:
    """Stable local embedding fallback for tests/dev and provider outages.

    It is not semantically as strong as a real embedding model, but gives a
    deterministic vector so memory retrieval continues to function locally.
    """
    dims = dimensions or settings.embedding_dimensions
    vector = [0.0] * dims
    tokens = [token for token in text.lower().replace("\n", " ").split(" ") if token]
    if not tokens:
        tokens = [text.lower() or "empty"]
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        for offset in range(0, len(digest), 4):
            index = int.from_bytes(digest[offset : offset + 2], "big") % dims
            sign = 1.0 if digest[offset + 2] % 2 == 0 else -1.0
            weight = 1.0 + (digest[offset + 3] / 255.0)
            vector[index] += sign * weight
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def embed_text(text: str) -> EmbeddingResult:
    if settings.embedding_url and settings.embedding_model:
        try:
            payload = {"model": settings.embedding_model, "input": text}
            headers = {}
            if settings.embedding_key:
                headers["Authorization"] = f"Bearer {settings.embedding_key}"
            with httpx.Client(timeout=60) as client:
                response = client.post(f"{settings.embedding_url.rstrip('/')}/embeddings", json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
            embedding = data["data"][0]["embedding"]
            return EmbeddingResult(embedding=[float(value) for value in embedding], model=settings.embedding_model, provider="remote")
        except Exception:
            if not settings.embedding_fallback_deterministic:
                raise
    return EmbeddingResult(embedding=deterministic_embedding(text), model="deterministic-hash", provider="deterministic")


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    length = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(length))
    norm_a = math.sqrt(sum(value * value for value in a[:length])) or 1.0
    norm_b = math.sqrt(sum(value * value for value in b[:length])) or 1.0
    return dot / (norm_a * norm_b)
