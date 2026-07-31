"""Инструменты QA и проверки веб-сайтов: ссылки, доступность (WCAG), SEO-метатеги."""
from __future__ import annotations

import html.parser
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..core import Tool, ToolError, Workspace

# Норма WCAG 2.1 AA для контраста обычного текста
WCAG_AA_TEXT = 4.5
WCAG_AA_LARGE = 3.0


class _LinkAndTagParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.images_without_alt: int = 0
        self.headings: list[str] = []
        self.title: str = ""
        self._in_title = False
        self.meta_desc: str = ""
        self.og_tags: dict[str, str] = {}
        self.canonical: str = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        adict = {k.lower(): v or "" for k, v in attrs}
        if tag == "a" and "href" in adict:
            self.links.append(adict["href"])
        elif tag == "img":
            if "alt" not in adict or not adict["alt"].strip():
                self.images_without_alt += 1
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.headings.append(tag)
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = adict.get("name", "").lower()
            prop = adict.get("property", "").lower()
            content = adict.get("content", "")
            if name == "description":
                self.meta_desc = content
            elif prop.startswith("og:"):
                self.og_tags[prop] = content
        elif tag == "link" and adict.get("rel") == "canonical":
            self.canonical = adict.get("href", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data


def build_site_qa_tools(ws: Workspace | None = None) -> list[Tool]:
    """Собрать инструменты проверки качества сайта (QA, ссылки, SEO, WCAG)."""

    def check_url(url: str, timeout: int = 5) -> str:
        """Проверка доступности URL и статуса ответа."""
        if not url:
            raise ToolError("Передан пустой URL")

        # Встроенная поддержка офлайн-тестовых/заглушечных URL (mock://)
        if url.startswith("mock://") or url.startswith("test://"):
            return (
                f"URL {url} [MOCK]: Статус 200 OK\n"
                "Время ответа: 12 ms\n"
                "Заголовки: Content-Type: text/html; charset=utf-8\n"
                "Проверка успешно завершена"
            )

        start_t = time.time()
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "AgentToolkit-QA-Bot/1.0"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                elapsed = int((time.time() - start_t) * 1000)
                status = getattr(resp, "status", 200)
                ctype = resp.headers.get("Content-Type", "")
                return (
                    f"URL {url}: Статус {status} OK\n"
                    f"Время ответа: {elapsed} ms\n"
                    f"Content-Type: {ctype}\n"
                    "Проверка доступности пройдена"
                )
        except urllib.error.HTTPError as exc:
            return (
                f"URL {url}: HTTP-ошибка {exc.code} ({exc.reason})\n"
                "Требуется исправление"
            )
        except (urllib.error.URLError, OSError) as exc:
            raise ToolError(f"Ошибка подключения к {url}: {exc}") from exc

    def check_links(html_content: str, base_url: str = "") -> str:
        """Анализ внутренних и внешних ссылок в HTML-коде."""
        parser = _LinkAndTagParser()
        parser.feed(html_content)

        total = len(parser.links)
        anchors = [u for u in parser.links if u.startswith("#")]
        ext = [
            u for u in parser.links if u.startswith("http://") or u.startswith("https://")
        ]
        internal = [
            u
            for u in parser.links
            if u not in anchors and u not in ext and not u.startswith("mailto:")
        ]

        empty = [u for u in parser.links if not u.strip() or u == "#"]

        report = [
            "### Отчёт по ссылкам страницы:",
            f"- Всего найдено ссылок: {total}",
            f"- Внутренних ссылок: {len(internal)}",
            f"- Внешних ссылок: {len(ext)}",
            f"- Якорей (#): {len(anchors)}",
            f"- Пустых/заглушечных ссылок: {len(empty)}",
        ]
        if empty:
            report.append("⚠ Внимание: на странице есть пустые ссылки (#)")
        return "\n".join(report)

    def check_accessibility(html_content: str) -> str:
        """Проверка базовых требований доступности (WCAG 2.1) в HTML."""
        parser = _LinkAndTagParser()
        parser.feed(html_content)

        issues: list[str] = []
        # Проверка H1
        h1_count = parser.headings.count("h1")
        if h1_count == 0:
            issues.append("Отсутствует тег <h1> на странице")
        elif h1_count > 1:
            issues.append(
                f"Найдено {h1_count} тегов <h1> (рекомендуется ровно 1 главный заголовок)"
            )

        # Проверка иерархии заголовков
        last_lvl = 0
        for h in parser.headings:
            lvl = int(h[1])
            if last_lvl > 0 and lvl - last_lvl > 1:
                issues.append(f"Нарушение иерархии заголовков: прыжок с h{last_lvl} на h{lvl}")
            last_lvl = lvl

        # Проверка alt у изображений
        if parser.images_without_alt > 0:
            issues.append(
                f"Найдено {parser.images_without_alt} изображений без атрибута alt"
            )

        score = max(0, 100 - len(issues) * 20)
        lines = [
            "### Отчёт по доступности (WCAG 2.1 / SEO HTML):",
            f"- Оценка доступности: {score}/100",
            f"- Найдено замечаний: {len(issues)}",
        ]
        for iss in issues:
            lines.append(f"  ✗ {iss}")
        if not issues:
            lines.append("  ✓ Страница соответствует базовым критериям доступности HTML")
        return "\n".join(lines)

    def check_seo_meta(html_content: str) -> str:
        """Инспекция SEO-метатегов, заголовка и OpenGraph."""
        parser = _LinkAndTagParser()
        parser.feed(html_content)

        title = parser.title.strip()
        desc = parser.meta_desc.strip()
        lines = [
            "### Отчёт по SEO-метатегам:",
            f"- Title ({len(title)} симв.): {title or '(отсутствует)'}",
            f"- Description ({len(desc)} симв.): {desc or '(отсутствует)'}",
            f"- Canonical URL: {parser.canonical or '(не задан)'}",
            f"- OpenGraph тегов найдено: {len(parser.og_tags)}",
        ]
        if not title:
            lines.append("⚠ Ошибка: Отсутствует тег <title>")
        elif len(title) > 70:
            lines.append("⚠ Предупреждение: Слишком длинный <title> (>70 символов)")
        if not desc:
            lines.append("⚠ Предупреждение: Отсутствует meta description")
        return "\n".join(lines)

    return [
        Tool(
            name="site_qa.check_url",
            description="Проверить доступность сайта (HTTP статус, время ответа).",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL для проверки"},
                    "timeout": {
                        "type": "integer",
                        "description": "Таймаут в секундах (по умолчанию 5)",
                    },
                },
                "required": ["url"],
            },
            fn=check_url,
            skills=["qa", "website", "web", "testing", "monitoring"],
            attributes={
                "category": "qa",
                "read_only": True,
                "dangerous": False,
                "requires_network": True,
                "resource_type": "url",
                "speed": "medium",
                "tags": ["qa", "url", "http", "check", "website"],
            },
            example='site_qa.check_url(url="https://example.com")',
        ),
        Tool(
            name="site_qa.check_links",
            description="Проанализировать ссылки в HTML-коде (внутренние, внешние, пустые).",
            parameters={
                "type": "object",
                "properties": {
                    "html_content": {
                        "type": "string",
                        "description": "HTML-код страницы",
                    },
                    "base_url": {
                        "type": "string",
                        "description": "Базовый URL сайта",
                    },
                },
                "required": ["html_content"],
            },
            fn=check_links,
            skills=["qa", "website", "web", "testing", "html", "links"],
            attributes={
                "category": "qa",
                "read_only": True,
                "dangerous": False,
                "resource_type": "html",
                "speed": "fast",
                "tags": ["qa", "links", "html", "check", "website"],
            },
            example='site_qa.check_links(html_content="<a href=\'/about\'>О нас</a>")',
        ),
        Tool(
            name="site_qa.check_accessibility",
            description="Проверить HTML-код на соответствие критериям доступности WCAG.",
            parameters={
                "type": "object",
                "properties": {
                    "html_content": {
                        "type": "string",
                        "description": "HTML-код страницы",
                    }
                },
                "required": ["html_content"],
            },
            fn=check_accessibility,
            skills=["qa", "website", "web", "testing", "accessibility", "wcag"],
            attributes={
                "category": "qa",
                "read_only": True,
                "dangerous": False,
                "resource_type": "html",
                "speed": "fast",
                "tags": ["qa", "wcag", "accessibility", "html", "website"],
            },
            example='site_qa.check_accessibility(html_content="<h1>Главная</h1>")',
        ),
        Tool(
            name="site_qa.check_seo_meta",
            description="Проверить SEO-метатеги HTML (title, description, canonical, OpenGraph).",
            parameters={
                "type": "object",
                "properties": {
                    "html_content": {
                        "type": "string",
                        "description": "HTML-код страницы",
                    }
                },
                "required": ["html_content"],
            },
            fn=check_seo_meta,
            skills=["qa", "website", "web", "testing", "seo"],
            attributes={
                "category": "qa",
                "read_only": True,
                "dangerous": False,
                "resource_type": "html",
                "speed": "fast",
                "tags": ["qa", "seo", "meta", "html", "website"],
            },
            example='site_qa.check_seo_meta(html_content="<title>Сайт</title>")',
        ),
    ]
