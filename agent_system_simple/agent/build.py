"""Сборка агента из конфигурации: модель + набор инструментов."""
from __future__ import annotations

from typing import Callable

from .config import Config
from .core import Agent, DEFAULT_SYSTEM
from .llm import build_llm
from .llm.router import Router
from .vectors import Embedder
from .skills import cad_openscad
from .skills import comms as comms_skill
from .skills import documents as doc_skill
from .skills import makedocs as makedocs_skill
from .skills import pgonto as pg_skill
from .skills import rag as rag_skill
from .skills import verify as verify_skill
from .tools import fetch as fetch_tools
from .mcp import MCPPool
from .store import Store
from .tools import ask as ask_tools
from .tools import memory as memory_tools
from .tools import present as present_tools
from .tools import files as files_tools
from .tools import dev as dev_tools
from .tools import python as python_tools
from .tools import semantic as semantic_tools
from .tools import shell as shell_tools
from .tools import vcs as vcs_tools
from .tools.base import ToolRegistry, Workspace

# Наборы навыков. Добавить свой = положить модуль с build(ws) -> list[Tool]
# и вписать сюда одну строку.
SKILLS: dict[str, Callable] = {
    "files": lambda ws, cfg, confirm: files_tools.build(ws),
    "shell": lambda ws, cfg, confirm: shell_tools.build(ws, cfg.sandbox, confirm),
    "cad": lambda ws, cfg, confirm: cad_openscad.build(ws),
    "fetch": lambda ws, cfg, confirm: fetch_tools.build(
        ws, allow_private=getattr(cfg, "fetch_allow_private", False)),
    "python": lambda ws, cfg, confirm: python_tools.build(
        ws, timeout=getattr(cfg, "python_timeout", 60)),
    "dev": lambda ws, cfg, confirm: dev_tools.build(ws),
    "vcs": lambda ws, cfg, confirm: vcs_tools.build(
        ws, auto=getattr(cfg, "vcs_auto", True)),
    "comms": lambda ws, cfg, confirm: comms_skill.build(
        ws, getattr(cfg, "comms", None) or comms_skill.CommsConfig(),
        confirm),
    # memory подключается отдельно: ему нужен Store, а не Workspace
}


#: навыки, которым нужен не только Workspace (база, пул MCP, ввод человека)
EXTRA_SKILLS = {"memory", "present", "mcp", "documents", "rag", "verify",
                "pg", "ask", "makedocs", "semantic"}


def known_skills() -> list[str]:
    return sorted([*SKILLS, *EXTRA_SKILLS])


def build_agent(
    cfg: Config,
    confirm: Callable[[str, str], bool] | None = None,
    on_event: Callable[[str, dict], None] | None = None,
    store: Store | None = None,
    run_id_getter: Callable[[], int] | None = None,
    mcp_pool: MCPPool | None = None,
    ask: Callable[[str, list[str]], str] | None = None,
) -> Agent:
    llm = build_llm(
        cfg.provider, cfg.model,
        base_url=cfg.base_url, api_key=cfg.api_key, temperature=cfg.temperature,
    )
    # Маршрутизация: снаружи Router неотличим от одной модели, поэтому
    # ядро о нём не знает и менять его не понадобилось.
    if getattr(cfg, "model_cheap", "") and getattr(cfg, "model_strong", ""):
        common = dict(base_url=cfg.base_url, api_key=cfg.api_key,
                      temperature=cfg.temperature)
        llm = Router(
            build_llm(cfg.provider, cfg.model_cheap, **common),
            build_llm(cfg.provider, cfg.model_strong, **common),
            long_context=getattr(cfg, "route_long_context", 12_000),
        )
    ws = Workspace(cfg.workspace)

    registry = ToolRegistry()
    extra = EXTRA_SKILLS
    unknown = [s for s in cfg.skills if s not in SKILLS and s not in extra]
    if unknown:
        raise ValueError(
            f"Неизвестные наборы навыков: {', '.join(unknown)}. "
            f"Доступны: {', '.join(known_skills())}"
        )
    for name in cfg.skills:
        if name in extra:
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

    # Документы, RAG, верификация, вопросы: используют базу.
    need_store = {"documents", "rag", "verify", "ask", "makedocs",
                  "semantic"} & set(cfg.skills)
    if need_store and store is None:
        store = Store(cfg.db)
    getter = run_id_getter or (lambda: 0)

    # Смысловой поиск: векторы в той же базе SQLite.
    if "semantic" in cfg.skills:
        if store is None:
            store = Store(cfg.db)
        emb = Embedder(url=getattr(cfg, "embed_url", "") or (cfg.base_url or ""),
                       model=getattr(cfg, "embed_model", ""),
                       key=getattr(cfg, "embed_key", "") or (cfg.api_key or ""),
                       dim=getattr(cfg, "embed_dim", 768))
        registry.extend(semantic_tools.build(store, emb, getter))

    # Вопрос человеку. ask=None означает «человека рядом нет»: инструмент
    # не выдумывает ответ, а блокирует пункт плана.
    if "ask" in cfg.skills:
        registry.extend(ask_tools.build(store, getter, ask))

    if "documents" in cfg.skills:
        registry.extend(doc_skill.build(ws, store, getter))
    # Создание документов: граф знаний нужен, чтобы отметить созданный файл.
    if "makedocs" in cfg.skills:
        registry.extend(makedocs_skill.build(ws, store, getter))
    if "rag" in cfg.skills:
        if store is None:
            store = Store(cfg.db)
        registry.extend(rag_skill.build(ws, store, getter))
    if "verify" in cfg.skills:
        registry.extend(verify_skill.build(ws, store, getter))

    # Онтология в PostgreSQL: недоступность БД не ломает агента.
    if "pg" in cfg.skills:
        pg = pg_skill.PgOnto(
            dsn=getattr(cfg, "pg_dsn", ""),
            embed_url=getattr(cfg, "embed_url", "") or (cfg.base_url or ""),
            embed_model=getattr(cfg, "embed_model", ""),
            embed_key=getattr(cfg, "embed_key", "") or (cfg.api_key or ""),
            dim=getattr(cfg, "embed_dim", 768))
        pg.connect()          # ошибка сохранится в pg.error, видна в pg_status
        registry.extend(pg_skill.build(pg))

    # Внешние MCP-серверы: поиск, страницы, картинки, речь.
    # Пул создаётся один раз снаружи и переиспользуется между итерациями —
    # иначе на каждом шаге заново поднимались бы подпроцессы.
    if "mcp" in cfg.skills and cfg.mcp:
        pool = mcp_pool if mcp_pool is not None else MCPPool(cfg.mcp)
        registry.extend(pool.tools())

    # Автоснимок перед каждым шагом: откат нужен именно тогда, когда
    # снимок сделать забыли. Первый снимок фиксирует исходное состояние.
    before_step = None
    if "vcs" in cfg.skills and getattr(cfg, "vcs_auto", True):
        repo = vcs_tools.Repo(ws.root)

        def before_step(n: int, _repo=repo) -> None:
            _repo.commit("исходное состояние" if n == 1 else f"перед шагом {n}")

    return Agent(
        llm=llm,
        tools=registry,
        before_step=before_step,
        system_prompt=cfg.system_prompt or DEFAULT_SYSTEM,
        max_steps=cfg.max_steps,
        tool_result_limit=cfg.tool_result_limit,
        keep_last_results=cfg.keep_last_results,
        on_event=on_event,
    )
