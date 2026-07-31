"""Локальные инструменты агента: файлы, офис, шаблоны, QA, SQL, PDF, Git, Sandbox, память, планировщик, HITL, субагенты, данные, скрапер, патчи, аудит, криптография, CAD, физика."""
from __future__ import annotations

from .audit import build_audit_tools
from .cad import build_cad_tools
from .code import build_code_tools
from .crypto import build_crypto_tools
from .data_tools import build_data_tools
from .document_templates import build_template_tools
from .files import build_file_tools
from .hitl import build_hitl_tools
from .jobs import build_jobs_tools
from .memory import build_memory_tools
from .monorepo_tools import build_monorepo_config_tools
from .office import build_office_tools
from .patch import build_patch_tools
from .pdf import build_pdf_tools
from .physics import build_physics_tools
from .quota import build_quota_tools
from .sandbox import build_sandbox_tools
from .scraper import build_scraper_tools
from .site_qa import build_site_qa_tools
from .sql import build_sql_tools
from .subagent import build_subagent_tools
from .web_builder import build_web_builder_tools

__all__ = [
    "build_file_tools",
    "build_office_tools",
    "build_template_tools",
    "build_site_qa_tools",
    "build_web_builder_tools",
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
    "build_monorepo_config_tools",
]
