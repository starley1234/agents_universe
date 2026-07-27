"""Сборка агента из конфигурации: модель + набор инструментов."""
from __future__ import annotations

from typing import Callable

from .config import Config
from .core import Agent, DEFAULT_SYSTEM
from .llm import build_llm
from .skills import cad_openscad
from .skills import pdf_pipeline
from .mcp import MCPPool
from .store import Store
from .tools import memory as memory_tools
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
    # memory и pdf подключаются отдельно: первому нужен Store, а не
    # Workspace, второму — отдельный драйвер модели (vision), а не тот,
    # что ведёт диалог с инструментами.
}


def known_skills() -> list[str]:
    return sorted([*SKILLS, "memory", "present", "mcp", "pdf"])


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
    extra = {"memory", "present", "mcp", "pdf"}
    unknown = [s for s in cfg.skills if s not in SKILLS and s not in extra]
    if unknown:
        raise ValueError(
            f"Неизвестные наборы навыков: {', '.join(unknown)}. "
            f"Доступны: {', '.join(known_skills())}"
        )
    for name in cfg.skills:
        if name in ("memory", "present", "mcp", "pdf"):
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

    return Agent(
        llm=llm,

        tools=registry,
        system_prompt=cfg.system_prompt or DEFAULT_SYSTEM,
        max_steps=cfg.max_steps,
        tool_result_limit=cfg.tool_result_limit,
        keep_last_results=cfg.keep_last_results,
        on_event=on_event,
    )
