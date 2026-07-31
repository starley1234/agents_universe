"""Инструменты работы с шаблонами документов (Markdown, HTML, отчёты, счета)."""
from __future__ import annotations

import json
from typing import Any

from ..core import Tool, ToolError, Workspace

BUILTIN_TEMPLATES: dict[str, str] = {
    "report_md": (
        "# {title}\n\n"
        "**Дата генерации:** {date}\n"
        "**Статус:** {status}\n\n"
        "## Исполнительное резюме\n"
        "{summary}\n\n"
        "## Основные разделы\n"
        "{sections}\n\n"
        "## Заключение\n"
        "{conclusion}\n"
    ),
    "audit_summary": (
        "# Протокол аудита: {subject}\n\n"
        "**Проверил:** {auditor}\n"
        "**Оценка соответствия:** {score}%\n\n"
        "### Обнаруженные замечания:\n"
        "{issues}\n"
    ),
}


def build_template_tools(ws: Workspace) -> list[Tool]:
    """Собрать инструменты работы с шаблонами документов."""

    def render_markdown(template_name: str, variables_json: str, path: str = "") -> str:
        if template_name not in BUILTIN_TEMPLATES:
            available = ", ".join(BUILTIN_TEMPLATES.keys())
            raise ToolError(
                f"Шаблон {template_name!r} не найден. Доступны: {available}"
            )
        try:
            vars_dict = json.loads(variables_json) if variables_json else {}
        except ValueError as exc:
            raise ToolError(f"Некорректный JSON в variables_json: {exc}") from exc

        tpl = BUILTIN_TEMPLATES[template_name]
        try:
            rendered = tpl.format_map({k: str(v) for k, v in vars_dict.items()})
        except KeyError as exc:
            raise ToolError(f"Отсутствует обязательная переменная шаблона: {exc}") from exc

        if path:
            p = ws.resolve(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(rendered, encoding="utf-8")
            return f"Шаблон {template_name!r} отрендерен и сохранён в {ws.relative(p)}"
        return rendered

    def render_report(
        title: str,
        summary: str,
        sections_json: str = "[]",
        metrics_json: str = "{}",
        path: str = "report.md",
    ) -> str:
        try:
            sections = json.loads(sections_json) if sections_json else []
            metrics = json.loads(metrics_json) if metrics_json else {}
        except ValueError as exc:
            raise ToolError(f"Некорректный JSON: {exc}") from exc

        lines: list[str] = [f"# {title}", "", f"**Резюме:** {summary}", ""]
        if metrics:
            lines.append("## Ключевые показатели")
            lines.append("| Метрика | Значение |")
            lines.append("| --- | --- |")
            for k, v in metrics.items():
                lines.append(f"| {k} | {v} |")
            lines.append("")

        if sections:
            lines.append("## Детализация")
            for idx, sec in enumerate(sections, 1):
                if isinstance(sec, dict):
                    sec_title = sec.get("title", f"Раздел {idx}")
                    sec_body = sec.get("body", "")
                else:
                    sec_title = f"Раздел {idx}"
                    sec_body = str(sec)
                lines.append(f"### {sec_title}")
                lines.append(sec_body)
                lines.append("")

        text = "\n".join(lines)
        if path:
            p = ws.resolve(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
            return (
                f"Отчёт {title!r} сгенерирован и сохранён в {ws.relative(p)} "
                f"({len(text)} символов)"
            )
        return text

    def create_invoice(
        invoice_number: str,
        customer: str,
        items_json: str,
        path: str = "invoice.md",
    ) -> str:
        try:
            items = json.loads(items_json) if items_json else []
        except ValueError as exc:
            raise ToolError(f"Некорректный JSON товаров в items_json: {exc}") from exc

        lines = [
            f"# Счёт на оплату № {invoice_number}",
            f"**Плательщик:** {customer}",
            "",
            "| Наименование | Кол-во | Цена | Сумма |",
            "| --- | --- | --- | --- |",
        ]
        total = 0.0
        for it in items:
            name = str(it.get("name", "Товар"))
            qty = float(it.get("qty", 1))
            price = float(it.get("price", 0))
            sum_price = qty * price
            total += sum_price
            lines.append(f"| {name} | {qty} | {price:.2f} | {sum_price:.2f} |")

        lines.extend(
            [
                "",
                f"**Итого к оплате:** {total:.2f}",
                "",
                "*Счёт сгенерирован автоматически системой Agent Toolkit*",
            ]
        )
        text = "\n".join(lines)
        p = ws.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return f"Счёт № {invoice_number} сохранён в {ws.relative(p)} (итого: {total:.2f})"

    def list_templates() -> str:
        items = [f"- {name}" for name in BUILTIN_TEMPLATES]
        return "Доступные встроенные шаблоны:\n" + "\n".join(items)

    return [
        Tool(
            name="templates.render_markdown",
            description="Отрендерить Markdown-шаблон с подстановкой переменных JSON.",
            parameters={
                "type": "object",
                "properties": {
                    "template_name": {
                        "type": "string",
                        "description": "Имя шаблона (report_md, audit_summary)",
                    },
                    "variables_json": {
                        "type": "string",
                        "description": 'JSON со значениями переменных (например, \'{"title": "Отчёт"}\')',
                    },
                    "path": {
                        "type": "string",
                        "description": "Опционально: путь для сохранения результата",
                    },
                },
                "required": ["template_name", "variables_json"],
            },
            fn=render_markdown,
            skills=["templates", "documentation", "reports", "local", "markdown"],
            attributes={
                "category": "templates",
                "read_only": False,
                "dangerous": False,
                "resource_type": "template",
                "speed": "fast",
                "tags": ["template", "render", "markdown", "report"],
            },
            example='templates.render_markdown(template_name="audit_summary", variables_json=\'{"subject": "Сайт", "auditor": "Иван", "score": 95, "issues": "Нет"}\')',
        ),
        Tool(
            name="templates.render_report",
            description="Сгенерировать стандартный структурированный отчёт с таблицей метрик.",
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Название отчёта"},
                    "summary": {"type": "string", "description": "Краткое резюме"},
                    "sections_json": {
                        "type": "string",
                        "description": "JSON-массив разделов",
                    },
                    "metrics_json": {
                        "type": "string",
                        "description": "JSON-объект с ключевыми показателями",
                    },
                    "path": {
                        "type": "string",
                        "description": "Путь для сохранения файла .md",
                    },
                },
                "required": ["title", "summary"],
            },
            fn=render_report,
            skills=["templates", "documentation", "reports", "local", "audit"],
            attributes={
                "category": "templates",
                "read_only": False,
                "dangerous": False,
                "resource_type": "report",
                "speed": "fast",
                "tags": ["template", "report", "audit", "summary"],
            },
            example='templates.render_report(title="Аудит", summary="Успешно", metrics_json=\'{"SOS": "45%"}\')',
        ),
        Tool(
            name="templates.create_invoice",
            description="Сгенерировать счёт на оплату (invoice) по списку товаров.",
            parameters={
                "type": "object",
                "properties": {
                    "invoice_number": {"type": "string", "description": "Номер счёта"},
                    "customer": {"type": "string", "description": "Название клиента"},
                    "items_json": {
                        "type": "string",
                        "description": 'JSON-массив позиций [{"name": "...", "qty": 1, "price": 100}]',
                    },
                    "path": {
                        "type": "string",
                        "description": "Путь для сохранения файла",
                    },
                },
                "required": ["invoice_number", "customer", "items_json"],
            },
            fn=create_invoice,
            skills=["templates", "documentation", "local", "finance"],
            attributes={
                "category": "templates",
                "read_only": False,
                "dangerous": False,
                "resource_type": "invoice",
                "speed": "fast",
                "tags": ["template", "invoice", "billing", "finance"],
            },
            example='templates.create_invoice(invoice_number="INV-001", customer="ООО Рога и Копыта", items_json=\'[{"name": "Аудит", "qty": 1, "price": 50000}]\' )',
        ),
        Tool(
            name="templates.list_templates",
            description="Получить список всех доступных встроенных шаблонов.",
            parameters={"type": "object", "properties": {}},
            fn=list_templates,
            skills=["templates", "documentation", "local"],
            attributes={
                "category": "templates",
                "read_only": True,
                "dangerous": False,
                "resource_type": "template",
                "speed": "fast",
                "tags": ["template", "list"],
            },
            example="templates.list_templates()",
        ),
    ]
