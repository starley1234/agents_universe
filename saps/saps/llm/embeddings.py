"""Эмбеддинги для семантического поиска (ТЗ п.4: pgvector).

Два провайдера:

  * hash — офлайн-эмбеддер без сети и ключей. Работает всегда, не
    понимает синонимы. Нужен потому, что в закрытом контуре КБ внешнего
    API может не быть вообще, а система должна запускаться и
    тестироваться в первый день. Честно: подбор пункта АП на hash-
    эмбеддингах работает по общим словам, а не по смыслу.
  * openai/local — любой сервер с /v1/embeddings.

ПОЧЕМУ HASH-ЭМБЕДДЕР ИМЕННО ТАКОЙ. Мешок слов с хешированием в
фиксированную размерность (hashing trick) + сублинейное сглаживание
частот и L2-нормировка. Это не «случайные числа»: у двух текстов с
общими словами векторы близки, и на кириллице это работает так же, как
на латинице, потому что нормализация приводит слова к нижнему регистру
и отбрасывает пунктуацию. Для дедупликации почти одинаковых требований
такого качества достаточно; для подбора пункта АП по СМЫСЛУ — нет, и об
этом прямо сказано в README.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.error
import urllib.request
from typing import Sequence

#: Морфология русского здесь не нужна целиком: достаточно отсечь
#: наиболее частые окончания, чтобы «требования» и «требование» попали в
#: одну корзину. Полноценный стеммер потребовал бы зависимости.
_SUFFIXES = ("ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими", "ов",
             "ев", "ий", "ый", "ой", "ая", "яя", "ое", "ее", "ые", "ие",
             "ам", "ям", "ах", "ях", "ом", "ем", "ах", "у", "ю", "а", "я",
             "ы", "и", "е", "о")


class EmbeddingError(RuntimeError):
    """Ошибка получения эмбеддинга."""


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[а-яёa-z0-9]+", (text or "").lower())
    out: list[str] = []
    for w in words:
        if len(w) > 5:
            for suf in _SUFFIXES:
                if w.endswith(suf) and len(w) - len(suf) >= 4:
                    w = w[: -len(suf)]
                    break
        out.append(w)
    return out


class BaseEmbedder:
    name = "base"

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise NotImplementedError


class HashEmbedder(BaseEmbedder):
    """Офлайн-эмбеддер: hashing trick по словам."""

    name = "hash"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            counts: dict[int, float] = {}
            for token in _tokens(text):
                h = hashlib.blake2b(token.encode("utf-8"), digest_size=8)
                idx = int.from_bytes(h.digest(), "big") % self.dim
                counts[idx] = counts.get(idx, 0.0) + 1.0
            for idx, count in counts.items():
                # Сублинейное сглаживание: одно слово, повторённое 20 раз,
                # не должно затмевать все остальные.
                vec[idx] = 1.0 + math.log(count)
            norm = math.sqrt(sum(v * v for v in vec))
            out.append([v / norm for v in vec] if norm else vec)
        return out


class OpenAIEmbedder(BaseEmbedder):
    """Любой сервер с OpenAI-совместимым /v1/embeddings."""

    name = "openai"

    def __init__(self, dim: int, model: str, base_url: str,
                 api_key: str = "", timeout: int = 60) -> None:
        super().__init__(dim)
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        body = {"model": self.model, "input": list(texts)}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise EmbeddingError(
                f"HTTP {exc.code} от сервера эмбеддингов: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise EmbeddingError(
                f"Не достучались до {self.base_url}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise EmbeddingError("Ответ сервера эмбеддингов не JSON") from exc

        try:
            items = sorted(data["data"], key=lambda d: d.get("index", 0))
            vectors = [list(map(float, item["embedding"])) for item in items]
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingError(
                f"Неожиданная структура ответа: {str(data)[:300]}") from exc

        for v in vectors:
            if len(v) != self.dim:
                raise EmbeddingError(
                    f"Модель вернула вектор размерности {len(v)}, а схема БД "
                    f"рассчитана на {self.dim}. Размерность фиксируется при "
                    "создании схемы (vector(dim)) и не меняется на лету: "
                    "задайте SAPS_EMBEDDING_DIM под вашу модель ДО первого "
                    "запуска или создайте новую схему.")
        return vectors


def build_embedder(provider: str, model: str, *, dim: int,
                   base_url: str = "", api_key: str = "",
                   timeout: int = 60) -> BaseEmbedder:
    key = (provider or "hash").strip().lower()
    if key in ("hash", "offline", ""):
        return HashEmbedder(dim)
    if key in ("openai", "local", "lmstudio", "llamacpp", "vllm", "ollama"):
        url = base_url or ("https://api.openai.com/v1" if key == "openai"
                           else "http://localhost:1234/v1")
        return OpenAIEmbedder(dim, model, url, api_key, timeout)
    raise EmbeddingError(
        f"Неизвестный провайдер эмбеддингов {provider!r}. Доступны: hash "
        "(офлайн), openai, local/lmstudio/llamacpp/vllm/ollama.")


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
