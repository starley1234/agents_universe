"""Сборка агента из конфигурации: модель + набор инструментов."""
from __future__ import annotations

from typing import Callable

from .config import Config
from .core import Agent, DEFAULT_SYSTEM
from .llm import build_llm
from .llm.embeddings import build_embedder
from .skills import cad_openscad
from .skills import cert_verify
from .skills import docparse
from .skills import pdf_pipeline
from .skills import pg_ontology
from .skills import rag as rag_skill
from .mcp import MCPPool
from .store import Store
from .tools import memory as memory_tools
from .tools import messaging as messaging_tools
from .tools import office_docs as office_docs_tools
from .tools import present as present_tools
from .tools import files as files_tools
from .tools import shell as shell_tools
from .tools.base import ToolRegistry, Workspace

# Наборы навыков. Добавить свой = положить модуль с build(ws) -> list[Tool]
# и вписать сюда одну строку.
SKILLS: dict[str, Callable] = {
    "files": lambda ws, cfg, confirm: files_tools.build(ws),
    "shell": lambda ws, cfg, confirm: shell_tools.build(ws, cfg.sandbox, confirm),
    "cad": lambda ws, cfg, confirm: cad_openscad.build(ws),
    "office": lambda ws, cfg, confirm: office_docs_tools.build(ws),
    # memory, pdf, docparse, pg_ontology, messaging, rag и cert_verify
    # подключаются отдельно: им нужен Store и/или отдельный драйвер
    # модели (vision/эмбеддинги), либо своя конфигурация каналов связи,
    # а не просто Workspace.
}


def known_skills() -> list[str]:
    return sorted([*SKILLS, "memory", "present", "mcp", "pdf", "docparse",
                  "pg_ontology", "messaging", "rag", "cert_verify"])



def build_agent(
    cfg: Config,
    confirm: Callable[[str, str], bool] | None = None,
    on_event: Callable[[str, dict], None] | None = None,
    store: Store | None = None,
    run_id_getter: Callable[[], int] | None = None,
    mcp_pool: MCPPool | None = None,
) -> Agent:
    llm = build_llm(
        cfg.provider, cfg.model,
        base_url=cfg.base_url, api_key=cfg.api_key, temperature=cfg.temperature,
    )
    ws = Workspace(cfg.workspace)

    registry = ToolRegistry()
    extra = {"memory", "present", "mcp", "pdf", "docparse", "pg_ontology",
            "messaging", "rag", "cert_verify"}
    unknown = [s for s in cfg.skills if s not in SKILLS and s not in extra]
    if unknown:
        raise ValueError(
            f"Неизвестные наборы навыков: {', '.join(unknown)}. "
            f"Доступны: {', '.join(known_skills())}"
        )
    for name in cfg.skills:
        if name in ("memory", "present", "mcp", "pdf", "docparse",
                    "pg_ontology", "messaging", "rag", "cert_verify"):
            continue
        registry.extend(SKILLS[name](ws, cfg, confirm))


    # Память и онтология требуют базы, поэтому подключаются отдельно.
    if "memory" in cfg.skills:
        if store is None:
            store = Store(cfg.db)
        registry.extend(memory_tools.build(store, run_id_getter or (lambda: 0)))

    # Показ артефакта: сборка результата в одну HTML-страницу.
    if "present" in cfg.skills:
        if store is None and "memory" in cfg.skills:
            store = Store(cfg.db)
        registry.extend(present_tools.build(ws, store,
                                            run_id_getter or (lambda: 0)))

    # Внешние MCP-серверы: поиск, страницы, картинки, речь.
    # Пул создаётся один раз снаружи и переиспользуется между итерациями —
    # иначе на каждом шаге заново поднимались бы подпроцессы.
    if "mcp" in cfg.skills and cfg.mcp:
        pool = mcp_pool if mcp_pool is not None else MCPPool(cfg.mcp)
        registry.extend(pool.tools())

    # PDF: читает документ, определяет тип страницы локально (без LLM),
    # затем распознаёт через ОТДЕЛЬНЫЙ, обычно vision-, драйвер модели.
    # Драйвер собирается независимо от основного llm, чтобы можно было
    # держать дешёвую/локальную модель для диалога и модель с поддержкой
    # изображений — для распознавания страниц.
    if "pdf" in cfg.skills:
        v_provider, v_model, v_base_url, v_api_key = cfg.resolve_vision()
        vision_llm = build_llm(
            v_provider, v_model,
            base_url=v_base_url, api_key=v_api_key, temperature=cfg.temperature,
        )
        registry.extend(pdf_pipeline.build(
            ws, vision_llm, dpi=cfg.pdf_dpi,
            max_pages_per_call=cfg.pdf_max_pages_per_call,
        ))

    # Docparse: Word/Excel/CSV/текст -> markdown/JSON, затем (extract_entities)
    # выделение сущностей ТЕКСТОВОЙ моделью в общую онтологию (Store).
    # Пишет в тот же Store, что и memory/present — единая база знаний.
    if "docparse" in cfg.skills:
        if store is None:
            store = Store(cfg.db)
        registry.extend(docparse.build(ws, llm, store,
                                       run_id_getter or (lambda: 0)))

    # PostgreSQL + pgvector: агент ведёт онтологию во «взрослой» СУБД с
    # семантическим (векторным) поиском вместо/в дополнение к SQLite+FTS.
    # Эмбеддер собирается отдельно от диалоговой модели: у неё обычно
    # нет своего API эмбеддингов (Anthropic) либо разумнее использовать
    # более дешёвую специализированную модель.
    if "pg_ontology" in cfg.skills:
        e_provider, e_model, e_base_url, e_api_key = cfg.resolve_embedding()
        embedder = build_embedder(
            e_provider, e_model, base_url=e_base_url, api_key=e_api_key,
        )
        registry.extend(pg_ontology.build(cfg.pg_dsn or "", embedder,
                                          dim=cfg.pg_vector_dim))

    # Связь с внешним миром: email (SMTP/IMAP), Telegram, MAX. Инструмент
    # появляется только для НАСТРОЕННОГО канала (есть токен/пароль) — агент
    # не должен видеть возможность, которой не может воспользоваться.
    # Отправка — необратимое действие, поэтому передаём тот же confirm,
    # что и run_command: без оператора (confirm=None) и без явного
    # messaging.confirm_sends=False отправка будет отклонена.
    if "messaging" in cfg.skills:
        registry.extend(messaging_tools.build(ws, cfg.messaging, confirm))

    # RAG: индексация текста в векторную/полнотекстовую базу и поиск по
    # ней — обычный (по всем фрагментам) и на онтологии (по объектам).
    # Бэкенд выбирается по наличию pg_dsn: с ним — PostgreSQL+pgvector
    # (тот же навык pg_ontology использует), без него — тот же SQLite
    # Store, что и memory/docparse (единая база на одну машину).
    if "rag" in cfg.skills:
        e_provider, e_model, e_base_url, e_api_key = cfg.resolve_embedding()
        embedder = build_embedder(
            e_provider, e_model, base_url=e_base_url, api_key=e_api_key,
        )
        if cfg.pg_dsn:
            # Тот же приём, что в pg_ontology.build: если размерность не
            # задана явно, определяем её по факту первого реального
            # вызова эмбеддера — ЛЕНИВО, при первом использовании
            # инструмента, а не при сборке агента.
            pg_dim = cfg.pg_vector_dim

            def _dim_getter(pg_dim=pg_dim, embedder=embedder) -> int:
                if pg_dim > 0:
                    return pg_dim
                return len(embedder.embed_one("dimension probe"))

            backend = rag_skill._PgBackend(cfg.pg_dsn, _dim_getter)
        else:
            if store is None:
                store = Store(cfg.db)
            backend = rag_skill._SQLiteBackend(store, run_id_getter or (lambda: 0))
        registry.extend(rag_skill.build(
            ws, embedder, backend,
            chunk_size=cfg.rag_chunk_size, chunk_overlap=cfg.rag_chunk_overlap,
            default_top_k=cfg.rag_top_k,
        ))

    # cert_verify: валидация комплекта документов для сертификации
    # (ФАП-21/Росавиация). Требует только Store — та же база, что у
    # memory/docparse/rag, чтобы выводы о соответствии сохранялись между
    # шагами прогона и переживали перезапуск.
    if "cert_verify" in cfg.skills:
        if store is None:
            store = Store(cfg.db)
        registry.extend(cert_verify.build(ws, store, run_id_getter or (lambda: 0)))

    return Agent(
        llm=llm,
        tools=registry,
        system_prompt=cfg.system_prompt or DEFAULT_SYSTEM,
        max_steps=cfg.max_steps,
        tool_result_limit=cfg.tool_result_limit,
        keep_last_results=cfg.keep_last_results,
        on_event=on_event,
    )
