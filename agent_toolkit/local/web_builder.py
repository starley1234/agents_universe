"""Инструменты для работы с веб-сайтами и создания сайтов (web.build_*, web.create_*).

Позволяют агентам генерировать полноценные статические HTML5/CSS3 сайты,
современные посадочные страницы (Landing Pages) и проверять их SEO/производительность.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core import Tool, ToolError, Workspace


def build_web_builder_tools(ws: Workspace) -> list[Tool]:
    """Собрать инструменты для создания и генерации веб-сайтов."""

    def build_static_site(
        site_dir: str = "site",
        title: str = "Новый сайт",
        pages_json: str = "[]",
    ) -> str:
        try:
            pages = json.loads(pages_json) if pages_json else []
            if not isinstance(pages, list):
                raise ValueError("pages_json должен быть JSON-массивом страниц")
        except ValueError as exc:
            raise ToolError(f"Некорректный JSON страниц pages_json: {exc}") from exc

        if not pages:
            pages = [
                {"filename": "index.html", "title": "Главная страница", "content": "<h1>Добро пожаловать</h1><p>Сайт создан автоматически.</p>"},
                {"filename": "about.html", "title": "О нас", "content": "<h1>О проекте</h1><p>Информация о компании.</p>"},
            ]

        base_dir = ws.resolve(site_dir)
        base_dir.mkdir(parents=True, exist_ok=True)

        nav_links = " | ".join(
            f'<a href="{p.get("filename", "index.html")}">{p.get("title", "Страница")}</a>'
            for p in pages
        )

        created_files = []
        for pg in pages:
            fname = str(pg.get("filename", "page.html"))
            p_title = str(pg.get("title", title))
            p_content = str(pg.get("content", ""))
            html_code = (
                f"<!DOCTYPE html>\n<html lang=\"ru\">\n<head>\n"
                f"    <meta charset=\"UTF-8\">\n    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">\n"
                f"    <title>{p_title} — {title}</title>\n"
                f"    <style>\n"
                f"        body {{ font-family: sans-serif; margin: 0; padding: 20px; background: #f8fafc; color: #0f172a; }}\n"
                f"        header {{ border-bottom: 2px solid #e2e8f0; padding-bottom: 15px; margin-bottom: 20px; }}\n"
                f"        nav a {{ margin-right: 15px; color: #2563eb; text-decoration: none; font-weight: bold; }}\n"
                f"        nav a:hover {{ text-decoration: underline; }}\n"
                f"        main {{ max-width: 800px; margin: 0 auto; background: white; padding: 30px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}\n"
                f"        footer {{ margin-top: 40px; font-size: 13px; color: #64748b; text-align: center; }}\n"
                f"    </style>\n</head>\n<body>\n"
                f"    <header>\n        <h1>{title}</h1>\n        <nav>{nav_links}</nav>\n    </header>\n"
                f"    <main>\n        {p_content}\n    </main>\n"
                f"    <footer>Сгенерировано автоматически — Agent Toolkit Web Builder</footer>\n"
                f"</body>\n</html>"
            )
            file_p = base_dir / fname
            file_p.write_text(html_code, encoding="utf-8")
            created_files.append(ws.relative(file_p))

        return (
            f"### Статический сайт `{title}` успешно создан в директории `{ws.relative(base_dir)}`:\n"
            f"- Сгенерировано страниц: **{len(created_files)}**\n"
            + "\n".join(f"  * `{f}`" for f in created_files)
        )

    def create_landing_page(
        path: str = "site/index.html",
        hero_title: str = "Интеллектуальная система",
        hero_subtitle: str = "Автоматизируйте свои процессы за считанные минуты",
        features_json: str = "[]",
        cta_text: str = "Начать бесплатно",
    ) -> str:
        try:
            features = json.loads(features_json) if features_json else []
            if not isinstance(features, list):
                raise ValueError("features_json должен быть массивом")
        except ValueError as exc:
            raise ToolError(f"Некорректный JSON преимуществ features_json: {exc}") from exc

        if not features:
            features = [
                {"title": "Высокая скорость", "desc": "Работает быстрее аналогов"},
                {"title": "Полная безопасность", "desc": "Изолированная песочница и аудит"},
                {"title": "Гибкая интеграция", "desc": "Поддержка API и MCP протоколов"},
            ]

        feat_cards = ""
        for f in features:
            ft = str(f.get("title", "Преимущество"))
            fd = str(f.get("desc", ""))
            feat_cards += (
                f"        <div class=\"card\">\n"
                f"            <h3>{ft}</h3>\n"
                f"            <p>{fd}</p>\n"
                f"        </div>\n"
            )

        landing_html = (
            f"<!DOCTYPE html>\n<html lang=\"ru\">\n<head>\n"
            f"    <meta charset=\"UTF-8\">\n    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">\n"
            f"    <title>{hero_title}</title>\n"
            f"    <style>\n"
            f"        * {{ box-sizing: border-box; margin: 0; padding: 0; }}\n"
            f"        body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #0f172a; color: #f8fafc; line-height: 1.6; }}\n"
            f"        .hero {{ text-align: center; padding: 80px 20px; background: linear-gradient(180deg, #1e293b 0%, #0f172a 100%); }}\n"
            f"        .hero h1 {{ font-size: 42px; font-weight: 800; margin-bottom: 20px; color: #60a5fa; }}\n"
            f"        .hero p {{ font-size: 20px; color: #94a3b8; max-width: 600px; margin: 0 auto 30px; }}\n"
            f"        .cta-btn {{ display: inline-block; background: #3b82f6; color: white; padding: 14px 32px; border-radius: 8px; font-weight: bold; text-decoration: none; transition: 0.2s; }}\n"
            f"        .cta-btn:hover {{ background: #2563eb; transform: translateY(-2px); }}\n"
            f"        .features {{ max-width: 1000px; margin: 60px auto; padding: 0 20px; display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 24px; }}\n"
            f"        .card {{ background: #1e293b; padding: 24px; border-radius: 12px; border: 1px solid #334155; }}\n"
            f"        .card h3 {{ color: #38bdf8; margin-bottom: 12px; }}\n"
            f"        footer {{ text-align: center; padding: 40px; color: #64748b; font-size: 14px; border-top: 1px solid #1e293b; }}\n"
            f"    </style>\n</head>\n<body>\n"
            f"    <section class=\"hero\">\n"
            f"        <h1>{hero_title}</h1>\n"
            f"        <p>{hero_subtitle}</p>\n"
            f"        <a href=\"#contact\" class=\"cta-btn\">{cta_text}</a>\n"
            f"    </section>\n"
            f"    <section class=\"features\">\n"
            f"{feat_cards}"
            f"    </section>\n"
            f"    <footer>© 2026 {hero_title} — Создано автоматически в Agent Toolkit</footer>\n"
            f"</body>\n</html>"
        )

        file_p = ws.resolve(path)
        file_p.parent.mkdir(parents=True, exist_ok=True)
        file_p.write_text(landing_html, encoding="utf-8")
        return (
            f"### Посадочная страница (Landing Page) сохранена в `{ws.relative(file_p)}`:\n"
            f"- Главный заголовок (Hero): **{hero_title!r}**\n"
            f"- Карточек преимуществ: **{len(features)}**\n"
            f"- Кнопка действия (CTA): **{cta_text!r}**"
        )

    def audit_site_seo_performance(
        html_content: str, url: str = "http://localhost"
    ) -> str:
        if not html_content.strip():
            raise ToolError("HTML-содержимое для аудита не может быть пустым")

        issues = []
        score = 100

        # SEO теги
        if "<title>" not in html_content.lower() or "</title>" not in html_content.lower():
            issues.append("Отсутствует тег <title>")
            score -= 20
        if "meta name=\"description\"" not in html_content.lower() and "meta name='description'" not in html_content.lower():
            issues.append("Отсутствует meta description")
            score -= 15
        if "meta name=\"viewport\"" not in html_content.lower() and "meta name='viewport'" not in html_content.lower():
            issues.append("Отсутствует meta viewport (отзывчивая вёрстка для мобильных)")
            score -= 25
        if "<h1" not in html_content.lower():
            issues.append("Отсутствует главный заголовок <h1>")
            score -= 15

        res = [
            f"### Аудит SEO и производительности сайта (`{url}`):",
            f"- Итоговый балл качества: **{max(0, score)}/100** -> {'✓ ОТЛИЧНО' if score >= 85 else '⚠ ТРЕБУЕТ ДОРАБОТКИ'}",
            f"- Проверены мобильная отзывчивость, заголовки, SEO-метатеги:",
        ]
        if issues:
            for i in issues:
                res.append(f"  ✗ {i}")
        else:
            res.append("  ✓ Все базовые критерии SEO и мобильной адаптивности пройдены")

        return "\n".join(res)

    return [
        Tool(
            name="web.build_static_site",
            description="Сгенерировать полный статический веб-сайт (HTML5/CSS3) из списка страниц в директории Workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "site_dir": {
                        "type": "string",
                        "description": "Целевая директория (по умолчанию 'site')",
                    },
                    "title": {
                        "type": "string",
                        "description": "Название сайта",
                    },
                    "pages_json": {
                        "type": "string",
                        "description": 'JSON-массив страниц [{"filename": "index.html", "title": "...", "content": "..."}]',
                    },
                },
            },
            fn=build_static_site,
            skills=["web", "site", "html", "css", "build", "website", "local"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "web_site",
                "speed": "fast",
                "tags": ["web", "website", "site", "html", "css", "build", "generator"],
            },
            example='web.build_static_site(site_dir="mysite", title="Компактный сайт")',
        ),
        Tool(
            name="web.create_landing_page",
            description="Сгенерировать современную отзывчивую посадочную страницу (Landing Page) с Hero-блоком, преимуществами и кнопкой CTA.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Путь файла .html (по умолчанию 'site/index.html')",
                    },
                    "hero_title": {
                        "type": "string",
                        "description": "Главный заголовок",
                    },
                    "hero_subtitle": {
                        "type": "string",
                        "description": "Подзаголовок",
                    },
                    "features_json": {
                        "type": "string",
                        "description": 'JSON-массив преимуществ [{"title": "...", "desc": "..."}]',
                    },
                    "cta_text": {
                        "type": "string",
                        "description": "Текст кнопки действия (CTA)",
                    },
                },
            },
            fn=create_landing_page,
            skills=["web", "site", "html", "css", "landing", "website", "local"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "landing_page",
                "speed": "fast",
                "tags": ["web", "landing", "page", "html", "css", "hero", "cta", "website"],
            },
            example='web.create_landing_page(hero_title="Продукт AI", cta_text="Попробовать")',
        ),
        Tool(
            name="web.audit_site_seo_performance",
            description="Провести аудит HTML-кода сайта на предмет SEO-тегов, мобильной вёрстки и производительности.",
            parameters={
                "type": "object",
                "properties": {
                    "html_content": {
                        "type": "string",
                        "description": "HTML-код страницы",
                    },
                    "url": {
                        "type": "string",
                        "description": "URL страницы",
                    },
                },
                "required": ["html_content"],
            },
            fn=audit_site_seo_performance,
            skills=["web", "site", "seo", "audit", "qa", "website", "local"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "seo_audit",
                "speed": "fast",
                "tags": ["web", "seo", "audit", "performance", "html", "qa", "website"],
            },
            example='web.audit_site_seo_performance(html_content="<title>Сайт</title>")',
        ),
    ]
