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


#: Сколько текстов отправлять одним запросом. Справочник АП — это
#: сотни пунктов; по одному запросу на пункт загрузка занимает минуты и
#: создаёт лишнюю нагрузку. Слишком большая пачка упирается в лимит
#: тела запроса у сервера, поэтому 32 — компромисс, проверенный на
#: LM Studio и OpenAI.
DEFAULT_BATCH = 32


class OpenAIEmbedder(BaseEmbedder):
    """Любой сервер с OpenAI-совместимым /v1/embeddings.

    Покрывает внешнюю модель в любом виде: LM Studio на соседней
    машине, vLLM/llama.cpp в контуре, Ollama в режиме /v1, облачный
    OpenAI. Различаются только base_url и ключ.

    ДВЕ ВЕЩИ, КОТОРЫХ НЕТ В НАИВНОЙ РЕАЛИЗАЦИИ И БЕЗ КОТОРЫХ БОЛЬНО:
    пакетная отправка (иначе загрузка справочника на 400 пунктов — это
    400 HTTP-запросов) и автоопределение размерности (иначе первая же
    попытка проиндексировать данные падает на несовпадении с vector(dim)
    в схеме БД).
    """

    name = "openai"

    def __init__(self, dim: int, model: str, base_url: str,
                 api_key: str = "", timeout: int = 60,
                 batch: int = DEFAULT_BATCH) -> None:
        super().__init__(dim)
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.batch = max(1, int(batch))

    # --- транспорт ------------------------------------------------------
    def _post(self, texts: list[str]) -> list[list[float]]:
        body = {"model": self.model, "input": texts}
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
            hint = ""
            if exc.code == 404:
                hint = (f" Проверьте адрес: ожидается базовый URL с /v1, "
                        f"запрос уходит на {self.base_url}/embeddings.")
            elif exc.code in (401, 403):
                hint = " Похоже на проблему с ключом (SAPS_EMBEDDING_API_KEY)."
            elif exc.code == 400:
                hint = (f" Часто это неверное имя модели "
                        f"({self.model!r}) — сверьте его со списком "
                        "загруженных моделей на сервере.")
            raise EmbeddingError(
                f"HTTP {exc.code} от сервера эмбеддингов "
                f"{self.base_url}: {detail}.{hint}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise EmbeddingError(
                f"Не достучались до сервера эмбеддингов {self.base_url}: "
                f"{exc}. Проверьте, что сервер запущен и доступен по сети "
                "(SAPS_EMBEDDING_BASE_URL).") from exc
        except json.JSONDecodeError as exc:
            raise EmbeddingError(
                f"Ответ {self.base_url} не является JSON") from exc

        try:
            items = sorted(data["data"], key=lambda d: d.get("index", 0))
            return [list(map(float, item["embedding"])) for item in items]
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingError(
                f"Неожиданная структура ответа: {str(data)[:300]}") from exc

    def probe_dim(self) -> int:
        """Спросить у модели размерность вектора одним коротким запросом.

        Нужно до создания схемы БД: колонка vector(dim) фиксируется
        навсегда, и ошибиться здесь дороже, чем сделать один запрос.
        """
        vectors = self._post(["проверка размерности"])
        if not vectors:
            raise EmbeddingError(
                "Сервер эмбеддингов вернул пустой ответ на пробный запрос")
        return len(vectors[0])

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        chunk = list(texts)
        for start in range(0, len(chunk), self.batch):
            out.extend(self._post(chunk[start:start + self.batch]))

        if len(out) != len(chunk):
            raise EmbeddingError(
                f"Сервер вернул {len(out)} векторов на {len(chunk)} текстов — "
                "ответ неполный, данные индексировать нельзя.")
        for v in out:
            if len(v) != self.dim:
                raise EmbeddingError(
                    f"Модель {self.model!r} вернула вектор размерности "
                    f"{len(v)}, а схема БД рассчитана на {self.dim}. "
                    "Размерность колонки vector(dim) фиксируется при создании "
                    "схемы и не меняется на лету (ограничение pgvector).\n"
                    f"Решение: задайте SAPS_EMBEDDING_DIM={len(v)} и создайте "
                    "схему заново (saps init) либо используйте отдельную "
                    "схему через SAPS_DB_SCHEMA.")
        return out


#: Провайдеры внешних (сетевых) моделей эмбеддингов.
EXTERNAL_PROVIDERS = ("openai", "local", "lmstudio", "llamacpp", "vllm",
                      "ollama", "external", "openai_like")

#: Умолчания адреса по провайдеру. Ollama слушает 11434, LM Studio — 1234;
#: подставлять один адрес для всех значило бы гарантированную ошибку
#: соединения у половины пользователей.
_DEFAULT_URLS = {
    "openai": "https://api.openai.com/v1",
    "ollama": "http://localhost:11434/v1",
}
_DEFAULT_LOCAL_URL = "http://localhost:1234/v1"


def is_external(provider: str) -> bool:
    return (provider or "hash").strip().lower() in EXTERNAL_PROVIDERS


def build_embedder(provider: str, model: str, *, dim: int,
                   base_url: str = "", api_key: str = "",
                   timeout: int = 60, batch: int = DEFAULT_BATCH
                   ) -> BaseEmbedder:
    key = (provider or "hash").strip().lower()
    if key in ("hash", "offline", ""):
        return HashEmbedder(dim)
    if key in EXTERNAL_PROVIDERS:
        url = base_url or _DEFAULT_URLS.get(key, _DEFAULT_LOCAL_URL)
        return OpenAIEmbedder(dim, model, url, api_key, timeout, batch)
    raise EmbeddingError(
        f"Неизвестный провайдер эмбеддингов {provider!r}. Доступны: hash "
        f"(офлайн), {', '.join(EXTERNAL_PROVIDERS)}.")


def probe_embedding_dim(provider: str, model: str, *, base_url: str = "",
                        api_key: str = "", timeout: int = 60) -> int:
    """Узнать размерность внешней модели, не создавая схему БД.

    Отдельная функция, потому что вызывается ДО того, как известна
    размерность: build_embedder требует dim, а мы его как раз выясняем.
    """
    if not is_external(provider):
        raise EmbeddingError(
            f"Автоопределение размерности имеет смысл только для внешней "
            f"модели; провайдер {provider!r} работает офлайн и его "
            "размерность задаётся параметром.")
    probe = build_embedder(provider, model, dim=1, base_url=base_url,
                           api_key=api_key, timeout=timeout)
    return probe.probe_dim()          # type: ignore[union-attr]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
