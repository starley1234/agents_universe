"""Agent Toolkit: независимый проект инструментария для агентов с поддержкой API и MCP.

Предоставляет:
  * Умный, быстрый реестр (ToolRegistry) с мгновенным поиском по названию,
    ключевым словам, скилсам (skills) и характеристикам (attributes).
  * Поддержку API (Python SDK и HTTP FastAPI) и MCP (Model Context Protocol).
  * Наборы локальных инструментов (файлы, офис, шаблоны, QA, SQL, PDF, Git, песочница, память, планировщик, HITL, субагенты, данные, скрапер, патчи, аудит, криптография).
  * Интеграции (MCP, SMTP/IMAP, Telegram, MAX, S3, генерация изображений, деплой, веб-поиск, HTTP API, синтез речи TTS, 1С/ERP OData).
  * Мультимодальные (Vision/VLM) инструменты и ритейл-аудит полок.
  * Готовые автоматизированные рабочие процессы (Workflows).
"""
from __future__ import annotations

from pathlib import Path

from .api import ToolkitClient, create_api_app
from .core import (
    Artifact,
    ArtifactStore,
    MCPToolCall,
    MCPToolResult,
    SecurityPolicy,
    Tool,
    ToolError,
    ToolExecuteRequest,
    ToolExecuteResponse,
    ToolPolicyError,
    ToolRegistry,
    ToolSearchRequest,
    ToolSearchResponse,
    Workspace,
    WorkspaceError,
)
from .integrations import (
    MCPClient,
    MCPServer,
    build_db_tools,
    build_deployment_tools,
    build_erp_tools,
    build_http_tools,
    build_image_generation_tools,
    build_max_tools,
    build_mcp_tools,
    build_s3_tools,
    build_smtp_tools,
    build_teamcenter_tools,
    build_telegram_tools,
    build_tts_tools,
    build_web_tools,
)
from .local import (
    build_audit_tools,
    build_cad_tools,
    build_code_tools,
    build_crypto_tools,
    build_data_tools,
    build_file_tools,
    build_hitl_tools,
    build_jobs_tools,
    build_memory_tools,
    build_monorepo_config_tools,
    build_office_tools,
    build_patch_tools,
    build_pdf_tools,
    build_physics_tools,
    build_quota_tools,
    build_sandbox_tools,
    build_scraper_tools,
    build_site_qa_tools,
    build_sql_tools,
    build_subagent_tools,
    build_template_tools,
    build_web_builder_tools,
)
from .vision import (
    FacingItem,
    ImageRef,
    InventoryAuditResult,
    VisionClient,
    build_inventory_tools,
    build_pdf_vlm_tools,
    build_vision_tools,
)
from .workflows import (
    InventoryReportWorkflow,
    WebsiteAuditWorkflow,
    build_inventory_workflow_tools,
    build_website_workflow_tools,
)

__version__ = "0.1.0"


def build_default_registry(
    workspace_root: str | Path | Workspace | None = None,
    *,
    include_local: bool = True,
    include_integrations: bool = True,
    include_vision: bool = True,
    include_workflows: bool = True,
) -> ToolRegistry:
    """Создать и заполнить умный реестр (ToolRegistry) всеми встроенными инструментами.

    Позволяет агенту сразу получить доступ к 85+ инструментам с поиском по скилсам и атрибутам.
    """
    if isinstance(workspace_root, Workspace):
        ws = workspace_root
    else:
        root = workspace_root or Path("/tmp/agent_toolkit_ws")
        ws = Workspace(root)

    registry = ToolRegistry()

    if include_local:
        for t in build_file_tools(ws):
            registry.add(t)
        for t in build_office_tools(ws):
            registry.add(t)
        for t in build_template_tools(ws):
            registry.add(t)
        for t in build_site_qa_tools(ws):
            registry.add(t)
        for t in build_web_builder_tools(ws):
            registry.add(t)
        for t in build_sql_tools(ws):
            registry.add(t)
        for t in build_pdf_tools(ws):
            registry.add(t)
        for t in build_code_tools(ws):
            registry.add(t)
        for t in build_sandbox_tools(ws):
            registry.add(t)
        for t in build_memory_tools(ws):
            registry.add(t)
        for t in build_jobs_tools(ws, registry_ref=registry):
            registry.add(t)
        for t in build_hitl_tools(ws):
            registry.add(t)
        for t in build_subagent_tools(ws):
            registry.add(t)
        for t in build_data_tools(ws):
            registry.add(t)
        for t in build_scraper_tools(ws):
            registry.add(t)
        for t in build_patch_tools(ws):
            registry.add(t)
        for t in build_audit_tools(ws):
            registry.add(t)
        for t in build_crypto_tools():
            registry.add(t)
        for t in build_cad_tools(ws):
            registry.add(t)
        for t in build_physics_tools():
            registry.add(t)
        for t in build_quota_tools(registry_ref=registry):
            registry.add(t)
        for t in build_monorepo_config_tools(ws):
            registry.add(t)

    if include_integrations:
        for t in build_db_tools(ws):
            registry.add(t)
        for t in build_mcp_tools():
            registry.add(t)
        for t in build_smtp_tools():
            registry.add(t)
        for t in build_max_tools():
            registry.add(t)
        for t in build_telegram_tools():
            registry.add(t)
        for t in build_s3_tools(ws):
            registry.add(t)
        for t in build_image_generation_tools(ws):
            registry.add(t)
        for t in build_deployment_tools(ws):
            registry.add(t)
        for t in build_web_tools():
            registry.add(t)
        for t in build_http_tools():
            registry.add(t)
        for t in build_tts_tools(ws):
            registry.add(t)
        for t in build_erp_tools():
            registry.add(t)
        for t in build_teamcenter_tools():
            registry.add(t)

    if include_vision:
        for t in build_vision_tools(ws):
            registry.add(t)
        for t in build_inventory_tools(ws):
            registry.add(t)
        for t in build_pdf_vlm_tools(ws):
            registry.add(t)

    if include_workflows:
        for t in build_website_workflow_tools(ws):
            registry.add(t)
        for t in build_inventory_workflow_tools(ws):
            registry.add(t)

    import os
    cfg_path = os.getenv("AGENT_TOOLKIT_CONFIG_PATH")
    if cfg_path:
        p = Path(cfg_path)
        if p.exists():
            try:
                registry.import_config(p.read_text(encoding="utf-8"))
            except Exception:
                pass

    return registry


__all__ = [
    "__version__",
    "Tool",
    "ToolError",
    "ToolRegistry",
    "Workspace",
    "WorkspaceError",
    "Artifact",
    "ArtifactStore",
    "SecurityPolicy",
    "ToolPolicyError",
    "ToolSearchRequest",
    "ToolSearchResponse",
    "ToolExecuteRequest",
    "ToolExecuteResponse",
    "MCPToolCall",
    "MCPToolResult",
    "build_file_tools",
    "build_office_tools",
    "build_template_tools",
    "build_site_qa_tools",
    "build_sql_tools",
    "build_pdf_tools",
    "build_code_tools",
    "build_sandbox_tools",
    "build_memory_tools",
    "build_jobs_tools",
    "build_hitl_tools",
    "build_subagent_tools",
    "build_data_tools",
    "build_scraper_tools",
    "build_patch_tools",
    "build_audit_tools",
    "build_crypto_tools",
    "build_cad_tools",
    "build_physics_tools",
    "build_quota_tools",
    "build_db_tools",
    "build_mcp_tools",
    "build_smtp_tools",
    "build_max_tools",
    "build_telegram_tools",
    "build_s3_tools",
    "build_image_generation_tools",
    "build_deployment_tools",
    "build_web_tools",
    "build_http_tools",
    "build_tts_tools",
    "build_erp_tools",
    "build_vision_tools",
    "build_inventory_tools",
    "build_pdf_vlm_tools",
    "build_website_workflow_tools",
    "build_inventory_workflow_tools",
    "build_default_registry",
    "create_api_app",
    "ToolkitClient",
    "MCPClient",
    "MCPServer",
    "VisionClient",
    "ImageRef",
    "FacingItem",
    "InventoryAuditResult",
    "WebsiteAuditWorkflow",
    "InventoryReportWorkflow",
]
