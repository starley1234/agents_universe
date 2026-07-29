"""Демо-агенты для быстрого старта (ТЗ: "режим быстрого старта").

Идемпотентно заводит несколько готовых личностей с разумными описаниями
для semantic router — чтобы после `make quickstart` можно было сразу
открыть /dashboard, увидеть непустой список агентов и написать что-то в
чат, а не сначала руками придумывать промпты и slug'и.

Использует ленивую эмбеддинг-модель (обычно `hash` — детерминированную
и офлайн, см. maos/llm/embeddings.py) — если внешний сервер эмбеддингов
недоступен на этапе первого запуска, посев демо-агентов всё равно не
должен падать: описание без эмбеддинга просто участвует в keyword-
фолбэке роутера (maos/orchestrator/router.py), а не в semantic search.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Config
from .llm.embeddings import BaseEmbedder, EmbeddingError
from .memory.store import Store


@dataclass
class DemoAgentSpec:
    slug: str
    name: str
    description: str
    keywords: str
    system_prompt: str
    tools: str = ""


#: Три личности, покрывающие разные роли — специально с непересекающейся
#: лексикой в description/keywords, чтобы semantic router и keyword-
#: фолбэк уверенно различали их даже на офлайн hash-эмбеддере. Каждой
#: назначен характерный навык — чтобы после `make quickstart` сразу было
#: видно, что MAOS-агенты умеют не только синтезировать текст, но и
#: реально работать с файлами/вебом/памятью через инструментальный цикл.
DEMO_AGENTS: tuple[DemoAgentSpec, ...] = (
    DemoAgentSpec(
        slug="coder",
        name="Coder",
        description=("Пишет, объясняет и отлаживает код на Python, "
                     "JavaScript и других языках программирования"),
        keywords="код python javascript баг отладка функция алгоритм",
        system_prompt=("Ты опытный инженер-программист. Отвечай точно и "
                       "по существу, приводи рабочий код, объясняй "
                       "решения кратко. Если задача неоднозначна — "
                       "уточни детали, а не додумывай."),
        tools="files",
    ),
    DemoAgentSpec(
        slug="writer",
        name="Writer",
        description=("Пишет и редактирует тексты: статьи, рекламные "
                     "материалы, письма, описания продуктов"),
        keywords="текст статья реклама письмо маркетинг редактура стиль",
        system_prompt=("Ты профессиональный редактор и копирайтер. "
                       "Пиши ясно, живо, без канцелярита. Подстраивай "
                       "тон под задачу: деловой для писем, яркий для "
                       "рекламы."),
        tools="web",
    ),
    DemoAgentSpec(
        slug="analyst",
        name="Analyst",
        description=("Анализирует данные, считает метрики, строит "
                     "выводы из чисел и таблиц, готовит отчёты"),
        keywords="данные аналитика отчёт метрика статистика таблица вывод",
        system_prompt=("Ты аналитик данных. Опирайся на цифры и факты, "
                       "явно указывай допущения, если данных не хватает "
                       "— так и скажи, не додумывай числа."),
        tools="rag",
    ),
)


def seed_demo_agents(store: Store, cfg: Config,
                     embedder: BaseEmbedder | None = None) -> list[str]:
    """Создаёт демо-агентов, которых ещё нет в базе. Идемпотентно:

    повторный вызов не создаёт дублей и не трогает уже существующие
    записи (в том числе если пользователь их отредактировал вручную) —
    решение принимается по slug, а не по содержимому.

    Возвращает список РЕАЛЬНО СОЗДАННЫХ на этом вызове slug'ов (пустой
    список, если все уже существовали).
    """
    created: list[str] = []
    for spec in DEMO_AGENTS:
        if store.get_agent(spec.slug):
            continue
        emb = None
        if embedder is not None:
            try:
                emb = embedder.embed_one(
                    f"{spec.name} {spec.description} {spec.keywords}")
            except EmbeddingError:
                emb = None
        store.create_agent(
            spec.slug, spec.name, description=spec.description,
            keywords=spec.keywords, llm_ref=cfg.default_local_model,
            system_prompt=spec.system_prompt, tools=spec.tools,
            description_embedding=emb)
        created.append(spec.slug)
    return created


def demo_agents_status(store: Store) -> dict[str, Any]:
    """Для дашборда: сколько демо-агентов уже есть, сколько можно посеять."""
    existing = {a["slug"] for a in store.list_agents()}
    demo_slugs = {spec.slug for spec in DEMO_AGENTS}
    return {
        "total_agents": len(existing),
        "demo_present": sorted(existing & demo_slugs),
        "demo_missing": sorted(demo_slugs - existing),
        "is_empty": len(existing) == 0,
    }
