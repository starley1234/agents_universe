"""Эмбеддинги для векторной памяти (pgvector).

Тот же принцип, что у чат-драйверов: единый интерфейс, провайдеры
отличаются только протоколом. `hash` — локальный, бесплатный,
детерминированный эмбеддинг на хешировании символьных n-грамм: не
семантический, но работает всегда, без сети и ключа — не даёт системе
встать колом, если внешний провайдер эмбеддингов недоступен, и годится
для тестов/офлайн-разработки.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.error
import urllib.request
from typing import Any


class EmbeddingError(RuntimeError):
    """Ошибка векторизации: сеть, авторизация, неверный ответ."""


class BaseEmbedder:
    name = "base"
    billable = True

    def __init__(self, model: str, dim: int = 0) -> None:
        self.model = model
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


class OpenAIEmbedder(BaseEmbedder):
    name = "openai"
    billable = True

    def __init__(self, model: str, base_url: str = "https://api.openai.com/v1",
                 api_key: str | None = None, timeout: int = 60,
                 batch_size: int = 64, **_: Any) -> None:
        super().__init__(model)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.batch_size = batch_size

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            body = json.dumps({"model": self.model, "input": batch}).encode()
            req = urllib.request.Request(
                f"{self.base_url}/embeddings", data=body,
                headers={
                    "Content-Type": "application/json",
                    **({"Authorization": f"Bearer {self.api_key}"}
                       if self.api_key else {}),
                }, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:400]
                raise EmbeddingError(
                    f"HTTP {exc.code} от {self.base_url}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise EmbeddingError(
                    f"Не достучались до {self.base_url}: {exc}") from exc
            try:
                items = sorted(data["data"], key=lambda d: d.get("index", 0))
                vecs = [it["embedding"] for it in items]
            except (KeyError, TypeError) as exc:
                raise EmbeddingError(
                    f"Неожиданный ответ эмбеддингов: {str(data)[:300]}") from exc
            out.extend(vecs)
        if out:
            self.dim = len(out[0])
        return out


_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


class HashEmbedder(BaseEmbedder):
    """Детерминированный офлайн-эмбеддинг: n-граммы символов -> хеш-бакеты."""

    name = "hash"
    billable = False

    def __init__(self, model: str = "hash-256", dim: int = 256, n: int = 3,
                 **_: Any) -> None:
        super().__init__(model, dim=dim)
        self.n = n

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        norm = text.lower().strip()
        tokens = _TOKEN_RE.findall(norm)
        grams: list[str] = []
        for tok in tokens:
            padded = f"^{tok}$"
            if len(padded) <= self.n:
                grams.append(padded)
            else:
                grams.extend(padded[i:i + self.n]
                            for i in range(len(padded) - self.n + 1))
        if not grams:
            return vec
        for g in grams:
            h = int(hashlib.blake2b(g.encode("utf-8"), digest_size=8).hexdigest(), 16)
            idx = h % self.dim
            sign = 1.0 if (h // self.dim) % 2 == 0 else -1.0
            vec[idx] += sign
        norm_val = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm_val for v in vec]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]


_REGISTRY: dict[str, type[BaseEmbedder]] = {
    "openai": OpenAIEmbedder,
    "local": OpenAIEmbedder,   # llama.cpp/vLLM/LM Studio /v1/embeddings — тот же протокол
    "hash": HashEmbedder,
}

#: алиасы: "lmstudio" (и другие частые имена локальных серверов) — это
#: тот же провайдер "local" с точки зрения протокола (OpenAI-совместимый
#: /v1/embeddings), просто более понятное имя в конфиге для тех, кто
#: явно развернул модель в LM Studio, а не в llama.cpp/vLLM/Ollama.
_EMBEDDING_ALIASES = {"lmstudio": "local", "llamacpp": "local", "vllm": "local",
                      "ollama": "local"}


def build_embedder(provider: str, model: str, base_url: str | None = None,
                   api_key: str | None = None, timeout: int | None = None,
                   **kwargs: Any) -> BaseEmbedder:
    """Собрать эмбеддер по имени провайдера.

    base_url/api_key — адрес и ключ ВНЕШНЕГО сервера эмбеддингов (LM
    Studio, свой vLLM и т.п.), если он отличается от того, что использует
    основная диалоговая модель — см. Config.embedding_base_url/
    embedding_api_key и Config.resolve_embedding(). Если не переданы —
    используются переменные окружения LOCAL_BASE_URL/LOCAL_API_KEY
    (для local/lmstudio/llamacpp/vllm/ollama) как разумное умолчание.
    """
    key = _EMBEDDING_ALIASES.get((provider or "").strip().lower(),
                                 (provider or "").strip().lower())
    if key not in _REGISTRY:
        raise EmbeddingError(
            f"Неизвестный провайдер эмбеддингов {provider!r}. Доступны: "
            f"{', '.join(sorted(set(_REGISTRY) | set(_EMBEDDING_ALIASES)))}"
        )
    cls = _REGISTRY[key]
    clean = {k: v for k, v in kwargs.items() if v is not None}
    if base_url:
        clean["base_url"] = base_url
    elif key == "local" and "base_url" not in clean:
        clean["base_url"] = os.getenv("LOCAL_BASE_URL", "http://localhost:11434/v1")
    if api_key:
        clean["api_key"] = api_key
    elif key == "local" and "api_key" not in clean:
        clean["api_key"] = os.getenv("LOCAL_API_KEY")
    if timeout:
        clean["timeout"] = timeout
    return cls(model=model, **clean)


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
