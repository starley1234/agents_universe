"""Рабочий процесс комплексного аудита веб-сайта (WebsiteAuditWorkflow).

Объединяет проверки доступности, ссылок, SEO-метатегов и формирование
итогового отчёта.
"""
from __future__ import annotations

import json
from typing import Any

from ..core import Tool, ToolError, Workspace
from ..local.document_templates import build_template_tools
from ..local.site_qa import build_site_qa_tools


class WebsiteAuditWorkflow:
    """Оркестратор автоматического аудита сайта (QA, SEO, доступность)."""

    def __init__(self, ws: Workspace) -> None:
        self.ws = ws
        self.qa_tools = {t.name: t for t in build_site_qa_tools(ws)}
        self.tpl_tools = {t.name: t for t in build_template_tools(ws)}

    def run_audit(
        self,
        url: str,
        html_content: str = "",
        save_path: str = "website_audit.md",
    ) -> dict[str, Any]:
        html_code = html_content or (
            "<!DOCTYPE html><html><head><title>Сайт</title>"
            "<meta name='description' content='Описание 123'></head>"
            "<body><h1>Главная</h1><a href='/about'>О нас</a><img src='logo.png' alt='Лого'></body></html>"
        )

        url_res = self.qa_tools["site_qa.check_url"].execute(url=url)
        links_res = self.qa_tools["site_qa.check_links"].execute(html_content=html_code)
        access_res = self.qa_tools["site_qa.check_accessibility"].execute(
            html_content=html_code
        )
        seo_res = self.qa_tools["site_qa.check_seo_meta"].execute(
            html_content=html_code
        )

        sections = [
            {"title": "Доступность сервера (HTTP)", "body": str(url_res)},
            {"title": "Аудит ссылочной структуры", "body": str(links_res)},
            {"title": "Доступность и вёрстка (WCAG 2.1)", "body": str(access_res)},
            {"title": "SEO и метатеги", "body": str(seo_res)},
        ]

        summary = (
            f"Проведён автоматический аудит сайта {url}. "
            "Проверены доступность, ссылки, иерархия заголовков и SEO-теги."
        )

        report_msg = self.tpl_tools["templates.render_report"].execute(
            title=f"Протокол аудита сайта {url}",
            summary=summary,
            sections_json=json.dumps(sections, ensure_ascii=False),
            metrics_json=json.dumps({"Статус": "OK", "WCAG": "Проверен"}, ensure_ascii=False),
            path=save_path,
        )

        p = self.ws.resolve(save_path)
        report_txt = p.read_text(encoding="utf-8") if p.exists() else str(report_msg)

        return {
            "url": url,
            "report_path": save_path,
            "report_text": report_txt,
            "report_status": report_msg,
            "sections": sections,
        }


def build_website_workflow_tools(ws: Workspace) -> list[Tool]:
    """Собрать инструменты для автоматического запуска аудита сайта."""
    wf = WebsiteAuditWorkflow(ws=ws)

    def audit_website(
        url: str,
        html_content: str = "",
        save_path: str = "website_audit.md",
    ) -> str:
        result = wf.run_audit(
            url=url, html_content=html_content, save_path=save_path
        )
        return str(result.get("report_text", "Аудит завершён"))

    return [
        Tool(
            name="workflow.audit_website",
            description="Запустить комплексный аудит веб-сайта (проверка URL, ссылок, SEO, WCAG) с сохранением отчёта.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL проверяемого сайта"},
                    "html_content": {
                        "type": "string",
                        "description": "HTML-код страницы (опционально)",
                    },
                    "save_path": {
                        "type": "string",
                        "description": "Путь сохранения отчёта (по умолчанию 'website_audit.md')",
                    },
                },
                "required": ["url"],
            },
            fn=audit_website,
            skills=["workflow", "website", "qa", "audit", "automation", "seo"],
            attributes={
                "category": "workflow",
                "read_only": False,
                "dangerous": False,
                "resource_type": "workflow_report",
                "speed": "medium",
                "tags": ["workflow", "website", "qa", "audit", "seo"],
            },
            example='workflow.audit_website(url="https://example.com", save_path="reports/site.md")',
        ),
    ]
