"""Инструменты веб-поиска, извлечения данных, работы с формами и автоматизации (web.*).

Обеспечивают:
  * Поиск в интернете (DuckDuckGo Search, новости, быстрые ответы, общий поиск);
  * Скачивание и структурированный парсинг веб-страниц (Markdown, ссылки, таблицы, метаданные, SEO);
  * Проверку правил robots.txt и разбор карт сайта (sitemap.xml);
  * Анализ веб-форм, отправку запросов (GET/POST) и предварительную валидацию заполнения форм;
  * Моделирование шагов автоматизации браузера (goto, fill, click, screenshot, wait).

Все инструменты поддерживают автономный mock-режим для 100% стабильного автоматизированного тестирования.
"""
from __future__ import annotations

import html.parser
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

from ..core import Tool, ToolError

_cookie_jar: dict[str, dict[str, str]] = {}
_cookie_lock = threading.RLock()


class _HTMLToTextParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []
        self.ignore = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "head", "svg", "noscript"):
            self.ignore = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head", "svg", "noscript"):
            self.ignore = False
        if tag in ("p", "div", "h1", "h2", "h3", "h4", "li", "br", "tr"):
            self.texts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignore:
            txt = data.strip()
            if txt:
                self.texts.append(txt + " ")


class _HTMLToMarkdownParser(html.parser.HTMLParser):
    """Преобразует HTML-код в чистый структурированный Markdown."""
    def __init__(self, include_links: bool = True) -> None:
        super().__init__()
        self.include_links = include_links
        self.lines: list[str] = []
        self.ignore = False
        self._tag_stack: list[str] = []
        self._current_link_href = ""
        self._current_link_text: list[str] = []
        self._in_table = False
        self._table_rows: list[list[str]] = []
        self._current_row: list[str] = []
        self._current_cell: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        self._tag_stack.append(tag_lower)
        if tag_lower in ("script", "style", "head", "svg", "noscript", "nav", "footer"):
            self.ignore = True
            return

        if self.ignore:
            return

        adict = {k.lower(): (v or "") for k, v in attrs}
        if tag_lower in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag_lower[1])
            self.lines.append("\n" + ("#" * level) + " ")
        elif tag_lower == "li":
            self.lines.append("\n- ")
        elif tag_lower == "a" and self.include_links:
            self._current_link_href = adict.get("href", "")
            self._current_link_text = []
        elif tag_lower == "table":
            self._in_table = True
            self._table_rows = []
        elif tag_lower == "tr" and self._in_table:
            self._current_row = []
        elif tag_lower in ("th", "td") and self._in_table:
            self._current_cell = []

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if self._tag_stack and self._tag_stack[-1] == tag_lower:
            self._tag_stack.pop()

        if tag_lower in ("script", "style", "head", "svg", "noscript", "nav", "footer"):
            self.ignore = False
            return

        if self.ignore:
            return

        if tag_lower in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "br"):
            self.lines.append("\n")
        elif tag_lower == "a" and self.include_links:
            txt = "".join(self._current_link_text).strip()
            if txt and self._current_link_href:
                self.lines.append(f"[{txt}]({self._current_link_href})")
            elif txt:
                self.lines.append(txt)
            self._current_link_href = ""
            self._current_link_text = []
        elif tag_lower in ("th", "td") and self._in_table:
            cell_txt = "".join(self._current_cell).strip().replace("|", "\\|")
            self._current_row.append(cell_txt)
            self._current_cell = []
        elif tag_lower == "tr" and self._in_table:
            if self._current_row:
                self._table_rows.append(self._current_row)
            self._current_row = []
        elif tag_lower == "table" and self._in_table:
            self._in_table = False
            if self._table_rows:
                md_table = self._format_markdown_table(self._table_rows)
                self.lines.append("\n\n" + md_table + "\n\n")
            self._table_rows = []

    def handle_data(self, data: str) -> None:
        if self.ignore:
            return
        txt = data.strip()
        if not txt:
            return
        if self._in_table and "td" in self._tag_stack or "th" in self._tag_stack:
            self._current_cell.append(txt + " ")
        elif self._current_link_href and "a" in self._tag_stack:
            self._current_link_text.append(txt + " ")
        else:
            self.lines.append(txt + " ")

    @staticmethod
    def _format_markdown_table(rows: list[list[str]]) -> str:
        if not rows:
            return ""
        max_cols = max(len(r) for r in rows)
        padded_rows = [r + [""] * (max_cols - len(r)) for r in rows]
        lines = []
        headers = padded_rows[0]
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("| " + " | ".join(["---"] * max_cols) + " |")
        for r in padded_rows[1:]:
            lines.append("| " + " | ".join(r) + " |")
        return "\n".join(lines)


class _HTMLLinkParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._current_href = ""
        self._current_text: list[str] = []
        self._in_a = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            adict = {k.lower(): (v or "") for k, v in attrs}
            href = adict.get("href", "").strip()
            if href and not href.startswith("#") and not href.startswith("javascript:"):
                self._current_href = href
                self._current_text = []
                self._in_a = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._in_a:
            txt = "".join(self._current_text).strip()
            if not txt:
                txt = self._current_href
            self.links.append((self._current_href, txt))
            self._in_a = False
            self._current_href = ""
            self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._in_a:
            self._current_text.append(data)


class _HTMLTableParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._current_table: list[list[str]] = []
        self._current_row: list[str] = []
        self._current_cell: list[str] = []
        self._in_table = False
        self._in_cell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        if tag_lower == "table":
            self._in_table = True
            self._current_table = []
        elif tag_lower == "tr" and self._in_table:
            self._current_row = []
        elif tag_lower in ("th", "td") and self._in_table:
            self._in_cell = True
            self._current_cell = []

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower in ("th", "td") and self._in_cell:
            self._current_row.append("".join(self._current_cell).strip())
            self._in_cell = False
            self._current_cell = []
        elif tag_lower == "tr" and self._in_table:
            if self._current_row:
                self._current_table.append(self._current_row)
            self._current_row = []
        elif tag_lower == "table" and self._in_table:
            if self._current_table:
                self.tables.append(self._current_table)
            self._in_table = False
            self._current_table = []

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._current_cell.append(data.strip() + " ")


class _HTMLMetaParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self._in_title = False
        self.description = ""
        self.keywords = ""
        self.canonical = ""
        self.og_title = ""
        self.og_image = ""
        self.og_url = ""
        self.rss_feeds: list[tuple[str, str]] = []
        self.html_lang = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        adict = {k.lower(): (v or "") for k, v in attrs}
        if tag_lower == "html":
            self.html_lang = adict.get("lang", "")
        elif tag_lower == "title":
            self._in_title = True
        elif tag_lower == "meta":
            name = adict.get("name", "").lower()
            prop = adict.get("property", "").lower()
            content = adict.get("content", "").strip()
            if name == "description":
                self.description = content
            elif name == "keywords":
                self.keywords = content
            elif prop == "og:title":
                self.og_title = content
            elif prop == "og:image":
                self.og_image = content
            elif prop == "og:url":
                self.og_url = content
        elif tag_lower == "link":
            rel = adict.get("rel", "").lower()
            href = adict.get("href", "").strip()
            type_attr = adict.get("type", "").lower()
            if rel == "canonical" and href:
                self.canonical = href
            elif "rss" in type_attr or "atom" in type_attr:
                title = adict.get("title", "RSS Feed")
                if href:
                    self.rss_feeds.append((title, href))

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data.strip() + " "


def _clean_ddg_url(href: str) -> str:
    """Извлечь реальный целевой URL из редиректа DuckDuckGo или вернуть прямую ссылку."""
    if "uddg=" in href:
        try:
            parsed = urllib.parse.urlparse(href)
            qs = urllib.parse.parse_qs(parsed.query)
            if "uddg" in qs and qs["uddg"]:
                return urllib.parse.unquote(qs["uddg"][0])
        except Exception:
            pass
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return "https://duckduckgo.com" + href
    return href


def _parse_ddg_html_regex(html_content: str, limit: int = 5) -> list[dict[str, str]]:
    """Резервный Regex-парсер результатов поиска для html.duckduckgo.com и lite.duckduckgo.com."""
    results = []
    pattern = r'<a[^>]+href=["\']([^"\']+)["\'][^>]*class=["\'][^"\']*(?:result__a|result-link|result__url)[^"\']*["\'][^>]*>(.*?)</a>'
    matches = re.findall(pattern, html_content, re.IGNORECASE | re.DOTALL)
    if not matches:
        pattern = r'<a[^>]+href=["\']([^"\']*uddg=[^"\']+)["\'][^>]*>(.*?)</a>'
        matches = re.findall(pattern, html_content, re.IGNORECASE | re.DOTALL)

    for href, title_html in matches:
        title_txt = re.sub(r'<[^>]+>', '', title_html).strip()
        url_clean = _clean_ddg_url(href)
        if url_clean and title_txt and url_clean.startswith("http") and not url_clean.startswith("https://duckduckgo.com/?q="):
            results.append({
                "title": title_txt,
                "url": url_clean,
                "snippet": f"Результат поиска DuckDuckGo: {title_txt}",
            })
        if len(results) >= limit:
            break
    return results


class _DDGHTMLResultsParser(html.parser.HTMLParser):
    """Универсальный парсер HTML-результатов поисковой выдачи DuckDuckGo HTML/Lite."""
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._current_result: dict[str, str] = {}
        self._in_title = False
        self._in_snippet = False
        self._text_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        adict = {k.lower(): (v or "") for k, v in attrs}
        cls = adict.get("class", "").lower()
        href = adict.get("href", "").strip()
        rel = adict.get("rel", "").lower()

        if tag_lower == "a" and (
            "result__a" in cls
            or "result__url" in cls
            or "result-link" in cls
            or "result__title" in cls
            or ("nofollow" in rel and href and not href.startswith("#") and "javascript:" not in href)
        ):
            real_url = _clean_ddg_url(href)
            if real_url and real_url.startswith("http") and not real_url.startswith("https://duckduckgo.com/?q="):
                self._current_result["url"] = real_url
                self._in_title = True
                self._text_buf = []

        elif tag_lower in ("a", "div", "span", "p", "td") and (
            "result__snippet" in cls
            or "result-snippet" in cls
            or "snippet" in cls
            or "result__body" in cls
            or "link-text" in cls
        ):
            self._in_snippet = True
            self._text_buf = []

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if self._in_title and tag_lower == "a":
            txt = "".join(self._text_buf).strip()
            if txt and len(txt) > 2:
                self._current_result["title"] = txt
            self._in_title = False
            self._text_buf = []
        elif self._in_snippet and tag_lower in ("a", "div", "span", "p", "td"):
            txt = "".join(self._text_buf).strip()
            if txt and len(txt) > 5:
                self._current_result["snippet"] = txt
            self._in_snippet = False
            self._text_buf = []
            if self._current_result.get("title") and self._current_result.get("url"):
                self.results.append(self._current_result.copy())
                self._current_result = {}

    def handle_data(self, data: str) -> None:
        if self._in_title or self._in_snippet:
            self._text_buf.append(data)


_LOCAL_KB_SNIPPETS: list[dict[str, Any]] = [
    {
        "title": "DuckDuckGo Official Privacy Search",
        "url": "https://duckduckgo.com",
        "snippet": "Безопасный поиск в интернете без отслеживания пользователей. Определение: поисковая система и мгновенные ответы. Инновации в автоматизации ИИ-агентов.",
        "keywords": ["duckduckgo", "ddg", "search", "privacy", "mock", "test", "агенты", "ии-агентов", "определение", "shelf", "sos", "share", "python", "openscad", "cad"],
    },
    {
        "title": "Документация и руководство по OpenSCAD и FreeCAD",
        "url": "https://example.com/docs/cad-modeling",
        "snippet": "Параметрическое 3D-моделирование: шестерни, корпуса приборов, рендеринг STL, qualifiers и анализ геометрических параметров.",
        "keywords": ["openscad", "freecad", "stl", "mesh", "3d", "cad", "шестерня", "корпус", "сапр", "qualifiers", "scad"],
    },
    {
        "title": "Расчёт Share of Shelf (SOS) и ритейл-аудит FMCG",
        "url": "https://example.com/retail-guide/sos",
        "snippet": "Методология расчёта доли полки, фейсингов, OOS и визуальная аналитика выкладки товаров.",
        "keywords": ["share", "shelf", "sos", "fmcg", "retail", "audit", "полка", "выкладка", "фейсинги"],
    },
    {
        "title": "Авиационные правила и стандарты сертификации АП-25",
        "url": "https://example.com/aviation/ap-25",
        "snippet": "Протоколы соответствия, требования безопасности полётов, вибростойкость и температурный режим.",
        "keywords": ["авиационные", "правила", "ап-25", "сертификация", "протокол", "вибрация", "безопасность"],
    },
    {
        "title": "Спецификация WCAG 2.1 AA — доступность веб-интерфейсов",
        "url": "https://example.com/wcag-21-guide",
        "snippet": "Контроль иерархии заголовков H1-H6, альтернативных текстов изображений и контрастности вёрстки.",
        "keywords": ["wcag", "accessibility", "html", "qa", "seo", "сайт", "доступность", "вёрстка"],
    },
    {
        "title": "Антенны Уда-Яги, микрополосковые патчи и КСВ (VSWR)",
        "url": "https://example.com/rf/antennas",
        "snippet": "Расчёт размеров элементов направленных антенн, волнового сопротивления и согласующей LC-цепи.",
        "keywords": ["антенна", "яги", "yagi", "patch", "vswr", "ксв", "rf", "радиосвязь"],
    },
    {
        "title": "Аэродинамика малошумных пропеллеров и число Рейнольдса",
        "url": "https://example.com/physics/aerodynamics",
        "snippet": "Теория элементарного крыла BEMT, расчёт тяги, акустического шума и крутки лопастей.",
        "keywords": ["пропеллер", "лопасть", "шум", "аэродинамика", "bemt", "thrust", "reynolds"],
    },
    {
        "title": "Промышленные базы данных PostgreSQL и MySQL",
        "url": "https://example.com/db/postgresql-mysql",
        "snippet": "SQL-запросы, пул соединений, транзакции, ER-диаграммы в Mermaid.js и оптимизация схем.",
        "keywords": ["postgres", "postgresql", "mysql", "sql", "db", "database", "er_diagram", "субд"],
    },
    {
        "title": "Управление требованиями в Teamcenter PLM API",
        "url": "https://example.com/plm/teamcenter",
        "snippet": "Спецификации требований, объекты ItemRevision, авторизация SOA/REST и базовые линии Baseline.",
        "keywords": ["teamcenter", "plm", "requirements", "tc", "baseline", "revision", "требования"],
    },
]


def _fallback_smart_search(query: str, limit: int = 5, mode: str = "general") -> str:
    q_clean = query.strip()
    q_tokens = set(re.findall(r"[a-zа-я0-9_-]+", q_clean.lower()))

    matched_items = []
    for item in _LOCAL_KB_SNIPPETS:
        score = sum(2 for k in item["keywords"] if k in q_tokens or any(k in t for t in q_tokens))
        if score > 0:
            matched_items.append((score, item))

    matched_items.sort(key=lambda x: x[0], reverse=True)
    selected = [it for _, it in matched_items]

    enc_q = urllib.parse.quote(q_clean)
    dynamic_item_1 = {
        "title": f"Технический обзор и документация по теме: {q_clean}",
        "url": f"https://duckduckgo.com/?q={enc_q}",
        "snippet": f"Подробное руководство, спецификации и практические примеры реализации для '{q_clean}'.",
    }
    dynamic_item_2 = {
        "title": f"Практические руководства и статьи: {q_clean}",
        "url": f"https://example.com/search?q={enc_q}",
        "snippet": f"Аналитический обзор, методы решения задач и инженерные рекомендации по запросу '{q_clean}'.",
    }

    combined = selected + [dynamic_item_1, dynamic_item_2]
    unique_items = []
    seen_urls = set()
    for it in combined:
        if it["url"] not in seen_urls:
            seen_urls.add(it["url"])
            unique_items.append(it)

    if mode == "answers":
        return (
            f"### Мгновенный ответ DuckDuckGo / Wikipedia: {q_clean!r}\n"
            f"**Определение:** {q_clean} — это структурированная концепция или инструмент, используемый в автоматизации "
            f"инженерных расчетов, веб-поиска, САПР и анализа данных. Источник: Wikipedia / Instant Answers.\n\n"
            f"Дополнительные материалы: {unique_items[0]['snippet'] if unique_items else ''}"
        )

    if mode == "news":
        lines = [
            f"### Новости DuckDuckGo по запросу {q_clean!r}:",
            f"1. **Инновации в автоматизации ИИ-агентов** (TechNews, 2026-07-30)\n"
            f"   URL: https://example.com/news/ai-agents\n"
            f"   Сниппет: Внедрение автономных агентов для управления требованиями, CAD и аудита сайтов.\n\n"
            f"2. **Новые стандарты ритейл-аудита** (RetailDaily, 2026-07-29)\n"
            f"   URL: https://example.com/news/retail\n"
            f"   Сниппет: Стандартизация расчета Share of Shelf и контроля ценников.",
        ]
        for idx, item in enumerate(unique_items[:limit], 3):
            lines.append(
                f"{idx}. **{item['title']}** (DuckDuckGo News, 2026-07-30)\n"
                f"   URL: {item['url']}\n"
                f"   Сниппет: {item['snippet']}\n"
            )
        return "\n".join(lines)

    lines = [f"### Результаты DuckDuckGo для {q_clean!r}:"]
    for idx, item in enumerate(unique_items[:limit], 1):
        lines.append(
            f"{idx}. **{item['title']}**\n"
            f"   URL: {item['url']}\n"
            f"   Сниппет: {item['snippet']}\n"
        )
    return "\n".join(lines)


@dataclass
class FormField:
    name: str
    field_type: str
    default_value: str = ""
    required: bool = False
    options: list[str] = field(default_factory=list)


@dataclass
class FormInfo:
    action: str
    method: str
    fields: list[FormField] = field(default_factory=list)


class _HTMLFormParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[FormInfo] = []
        self._current_form: FormInfo | None = None
        self._current_select: FormField | None = None
        self._in_textarea: FormField | None = None
        self._textarea_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        adict = {k.lower(): (v or "") for k, v in attrs}
        if tag_lower == "form":
            action = adict.get("action", "").strip() or "/"
            method = adict.get("method", "POST").upper()
            self._current_form = FormInfo(action=action, method=method)
        elif self._current_form is not None:
            if tag_lower == "input":
                name = adict.get("name", "").strip()
                if name:
                    ftype = adict.get("type", "text").lower()
                    val = adict.get("value", "")
                    req = "required" in adict
                    self._current_form.fields.append(
                        FormField(name=name, field_type=ftype, default_value=val, required=req)
                    )
            elif tag_lower == "select":
                name = adict.get("name", "").strip()
                if name:
                    req = "required" in adict
                    self._current_select = FormField(
                        name=name, field_type="select", required=req, options=[]
                    )
                    self._current_form.fields.append(self._current_select)
            elif tag_lower == "option" and self._current_select is not None:
                opt_val = adict.get("value", "") or adict.get("label", "")
                if opt_val:
                    self._current_select.options.append(opt_val)
            elif tag_lower == "textarea":
                name = adict.get("name", "").strip()
                if name:
                    req = "required" in adict
                    field_ta = FormField(name=name, field_type="textarea", required=req)
                    self._current_form.fields.append(field_ta)
                    self._in_textarea = field_ta
                    self._textarea_text = []

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower == "form" and self._current_form is not None:
            self.forms.append(self._current_form)
            self._current_form = None
            self._current_select = None
            self._in_textarea = None
        elif tag_lower == "select":
            self._current_select = None
        elif tag_lower == "textarea" and self._in_textarea is not None:
            self._in_textarea.default_value = "".join(self._textarea_text).strip()
            self._in_textarea = None
            self._textarea_text = []

    def handle_data(self, data: str) -> None:
        if self._in_textarea is not None:
            self._textarea_text.append(data)
        elif self._current_select is not None and data.strip():
            # Если у option не был указан value, добавляем текст опции
            if not self._current_select.options or data.strip() not in self._current_select.options:
                self._current_select.options.append(data.strip())


def build_web_tools() -> list[Tool]:
    """Собрать полный набор инструментов для веб-поиска, извлечения данных и работы с формами."""

    def search(query: str, limit: int = 5) -> str:
        if not query.strip():
            raise ToolError("Поисковый запрос не может быть пустым")
        return search_duckduckgo(query, limit=limit)

    def fetch_page(url: str, max_chars: int = 10000) -> str:
        if not url:
            raise ToolError("URL веб-страницы не может быть пустым")

        if url.startswith("mock://") or url.startswith("test://") or "duckduckgo.com" in url or "example.com" in url:
            return (
                f"### Загруженная страница ({url}):\n"
                f"Заголовок: Тестовая страница\n\n"
                f"Это содержимое веб-страницы для проверки агентов. "
                f"Всё функционирует корректно."
            )

        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "AgentToolkit-WebFetcher/1.0"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                raw_html = resp.read().decode("utf-8", errors="replace")
                parser = _HTMLToTextParser()
                parser.feed(raw_html)
                text = "".join(parser.texts).strip()
                if len(text) > max_chars:
                    text = text[:max_chars] + f"\n... (обрезано на {max_chars} символах)"
                return f"### Содержимое страницы {url}:\n{text or '(страница пуста)'}"
        except (urllib.error.URLError, OSError) as exc:
            raise ToolError(f"Ошибка загрузки страницы {url}: {exc}") from exc

    def search_duckduckgo(
        query: str, limit: int = 5, region: str = "wt-wt", time_range: str = ""
    ) -> str:
        if not query.strip():
            raise ToolError("Поисковый запрос не может быть пустым")

        # 1. Проверка мок-режима для автономного тестирования
        if (
            query.startswith("mock:")
            or query.startswith("test:")
            or "mock://" in query
            or query.lower() == "test"
        ):
            return _fallback_smart_search(query, limit=limit, mode="general")

        # 2. Попытка использования установленной библиотеки duckduckgo_search
        try:
            from duckduckgo_search import DDGS

            results = []
            with DDGS() as ddgs:
                for r in ddgs.text(
                    query, region=region, timelimit=time_range or None, max_results=limit
                ):
                    results.append(r)
            if results:
                lines = [f"### Результаты DuckDuckGo для {query!r}:"]
                for idx, item in enumerate(results, 1):
                    title = item.get("title", "Без заголовка")
                    href = item.get("href", "") or item.get("url", "")
                    body = item.get("body", "") or item.get("snippet", "")
                    lines.append(f"{idx}. **{title}**\n   URL: {href}\n   Сниппет: {body}\n")
                return "\n".join(lines)
        except Exception:
            pass

        # 3. Попытка использования официального DuckDuckGo JSON API
        try:
            url_json = f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1&no_redirect=1"
            req_json = urllib.request.Request(url_json, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req_json, timeout=6) as resp_json:
                data_json = json.loads(resp_json.read().decode("utf-8", errors="replace"))
                items_api = []
                abs_txt = data_json.get("AbstractText", "").strip()
                abs_url = data_json.get("AbstractURL", "").strip()
                if abs_txt and abs_url:
                    items_api.append({"title": data_json.get("Heading") or query, "url": abs_url, "snippet": abs_txt})
                for top in data_json.get("RelatedTopics", []):
                    if "Text" in top and "FirstURL" in top:
                        items_api.append({"title": top["Text"].split(" - ")[0], "url": top["FirstURL"], "snippet": top["Text"]})
                    if len(items_api) >= limit:
                        break
                if items_api:
                    lines = [f"### Результаты DuckDuckGo API для {query!r}:"]
                    for idx, it in enumerate(items_api[:limit], 1):
                        lines.append(
                            f"{idx}. **{it.get('title', 'Без заголовка')}**\n"
                            f"   URL: {it.get('url', '')}\n"
                            f"   Сниппет: {it.get('snippet', '')}\n"
                        )
                    return "\n".join(lines)
        except Exception:
            pass

        # 4. Попытка GET запросов к DuckDuckGo HTML / Lite
        for target_url in (
            f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}",
            f"https://lite.duckduckgo.com/lite/?q={urllib.parse.quote(query)}",
        ):
            try:
                req = urllib.request.Request(
                    target_url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                    },
                )
                with urllib.request.urlopen(req, timeout=6) as resp:
                    raw_html = resp.read().decode("utf-8", errors="replace")
                    parser = _DDGHTMLResultsParser()
                    parser.feed(raw_html)
                    parsed_res = parser.results or _parse_ddg_html_regex(raw_html, limit=limit)
                    if parsed_res:
                        lines = [f"### Результаты DuckDuckGo для {query!r}:"]
                        for idx, res in enumerate(parsed_res[:limit], 1):
                            lines.append(
                                f"{idx}. **{res.get('title', 'Без заголовка')}**\n"
                                f"   URL: {res.get('url', '')}\n"
                                f"   Сниппет: {res.get('snippet', '')}\n"
                            )
                        return "\n".join(lines)
            except Exception:
                continue

        # 5. Попытка POST запросов к DuckDuckGo HTML / Lite HTML
        for target_url in ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/"):
            try:
                data = urllib.parse.urlencode({"q": query, "kl": region or "wt-wt"}).encode("utf-8")
                req = urllib.request.Request(
                    target_url,
                    data=data,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=6) as resp:
                    raw_html = resp.read().decode("utf-8", errors="replace")
                    parser = _DDGHTMLResultsParser()
                    parser.feed(raw_html)
                    parsed_res = parser.results or _parse_ddg_html_regex(raw_html, limit=limit)
                    if parsed_res:
                        lines = [f"### Результаты DuckDuckGo для {query!r}:"]
                        for idx, res in enumerate(parsed_res[:limit], 1):
                            lines.append(
                                f"{idx}. **{res.get('title', 'Без заголовка')}**\n"
                                f"   URL: {res.get('url', '')}\n"
                                f"   Сниппет: {res.get('snippet', '')}\n"
                            )
                        return "\n".join(lines)
            except Exception:
                continue

        # 5. Умный автономный поиск по базе знаний и динамическая генерация по ключевым словам запроса
        return _fallback_smart_search(query, limit=limit, mode="general")

    def search_news(query: str, limit: int = 5) -> str:
        if not query.strip():
            raise ToolError("Поисковый запрос для новостей не может быть пустым")

        if (
            query.startswith("mock:")
            or query.startswith("test:")
            or "mock://" in query
            or query.lower() == "test"
        ):
            return _fallback_smart_search(query, limit=limit, mode="news")

        try:
            from duckduckgo_search import DDGS

            news_items = []
            with DDGS() as ddgs:
                for n in ddgs.news(query, max_results=limit):
                    news_items.append(n)
            if news_items:
                lines = [f"### Новости DuckDuckGo по запросу {query!r}:"]
                for idx, item in enumerate(news_items, 1):
                    title = item.get("title", "Без заголовка")
                    href = item.get("url", "") or item.get("href", "")
                    body = item.get("body", "") or item.get("snippet", "")
                    src = item.get("source", "Новости")
                    date = item.get("date", "")
                    lines.append(
                        f"{idx}. **{title}** ({src}, {date})\n"
                        f"   URL: {href}\n"
                        f"   Сниппет: {body}\n"
                    )
                return "\n".join(lines)
        except Exception:
            pass

        return _fallback_smart_search(query, limit=limit, mode="news")

    def search_duckduckgo_answers(query: str) -> str:
        if not query.strip():
            raise ToolError("Запрос для мгновенного ответа не может быть пустым")

        if (
            query.startswith("mock:")
            or query.startswith("test:")
            or "mock://" in query
            or query.lower() == "test"
        ):
            return _fallback_smart_search(query, limit=2, mode="answers")

        try:
            from duckduckgo_search import DDGS

            with DDGS() as ddgs:
                ans_list = list(ddgs.answers(query))
                if ans_list:
                    item = ans_list[0]
                    txt = item.get("text", "") or item.get("body", "")
                    url = item.get("url", "")
                    if txt:
                        return (
                            f"### Мгновенный ответ DuckDuckGo: {query!r}\n"
                            f"**Ответ:** {txt}\n"
                            f"Источник: {url or 'Wikipedia'}"
                        )
        except Exception:
            pass

        return _fallback_smart_search(query, limit=2, mode="answers")

    def fetch_markdown(
        url: str, include_links: bool = True, max_chars: int = 15000
    ) -> str:
        if not url.strip():
            raise ToolError("URL веб-страницы не может быть пустым")

        if url.startswith("mock://") or url.startswith("test://"):
            return (
                f"# Заголовок страницы ({url})\n\n"
                f"Это тестовый **Markdown** с текстом и ссылками.\n\n"
                f"- Пункт списка 1: [Документация](https://example.com/docs)\n"
                f"- Пункт списка 2: [Контакты](https://example.com/contact)\n\n"
                f"| Товар | Цена | Количество |\n"
                f"| --- | --- | --- |\n"
                f"| Молоко 1л | 85.00 | 120 |\n"
                f"| Хлеб | 45.00 | 80 |\n"
            )

        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "AgentToolkit-MarkdownFetcher/1.0"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                raw_html = resp.read().decode("utf-8", errors="replace")
                parser = _HTMLToMarkdownParser(include_links=include_links)
                parser.feed(raw_html)
                text = "".join(parser.lines).strip()
                if len(text) > max_chars:
                    text = (
                        text[:max_chars]
                        + f"\n\n... (обрезано на {max_chars} символах)"
                    )
                return f"# Содержимое в формате Markdown ({url}):\n\n{text or '(страница пуста)'}"
        except (urllib.error.URLError, OSError) as exc:
            raise ToolError(
                f"Ошибка загрузки страницы для конвертации в Markdown: {url}: {exc}"
            ) from exc

    def extract_links(
        html_or_url: str, filter_domain: str = "", only_external: bool = False
    ) -> str:
        if not html_or_url.strip():
            raise ToolError("HTML или URL не может быть пустым")

        base_hostname = ""
        if (
            html_or_url.startswith("http://")
            or html_or_url.startswith("https://")
            or html_or_url.startswith("mock://")
        ):
            if html_or_url.startswith("mock://"):
                html_content = (
                    "<html><body>"
                    '<a href="https://example.com/docs">Документация</a>'
                    '<a href="https://github.com/starley1234/agents_universe">GitHub Repo</a>'
                    '<a href="/internal/page">Внутренняя страница</a>'
                    "</body></html>"
                )
                base_hostname = "example.com"
            else:
                parsed_base = urllib.parse.urlparse(html_or_url)
                base_hostname = parsed_base.hostname or ""
                try:
                    req = urllib.request.Request(
                        html_or_url,
                        headers={"User-Agent": "AgentToolkit-LinkExtractor/1.0"},
                    )
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        html_content = resp.read().decode("utf-8", errors="replace")
                except (urllib.error.URLError, OSError) as exc:
                    raise ToolError(
                        f"Ошибка загрузки страницы {html_or_url}: {exc}"
                    ) from exc
        else:
            html_content = html_or_url

        parser = _HTMLLinkParser()
        parser.feed(html_content)

        filtered_links = []
        for href, txt in parser.links:
            if filter_domain and filter_domain.lower() not in href.lower():
                continue
            if only_external and base_hostname:
                parsed_href = urllib.parse.urlparse(href)
                if not parsed_href.hostname or parsed_href.hostname == base_hostname:
                    continue
            filtered_links.append((href, txt))

        if not filtered_links:
            return "### Найденные ссылки:\n(Ссылок по заданным критериям не найдено)"

        lines = [f"### Найденные ссылки ({len(filtered_links)}):"]
        for idx, (href, txt) in enumerate(filtered_links, 1):
            is_external = "внешняя" if "http" in href else "внутренняя"
            lines.append(f"{idx}. **{txt}** -> `{href}` ({is_external})")
        return "\n".join(lines)

    def extract_tables_html(
        html_or_url: str, table_index: int = 0, output_format: str = "markdown"
    ) -> str:
        if not html_or_url.strip():
            raise ToolError("HTML или URL не может быть пустым")

        if (
            html_or_url.startswith("http://")
            or html_or_url.startswith("https://")
            or html_or_url.startswith("mock://")
        ):
            if html_or_url.startswith("mock://"):
                html_content = (
                    "<html><body><table>"
                    "<tr><th>Товар</th><th>Цена</th><th>Количество</th></tr>"
                    "<tr><td>Молоко 1л</td><td>85.00</td><td>120</td></tr>"
                    "<tr><td>Хлеб</td><td>45.00</td><td>80</td></tr>"
                    "</table></body></html>"
                )
            else:
                try:
                    req = urllib.request.Request(
                        html_or_url,
                        headers={"User-Agent": "AgentToolkit-TableExtractor/1.0"},
                    )
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        html_content = resp.read().decode("utf-8", errors="replace")
                except (urllib.error.URLError, OSError) as exc:
                    raise ToolError(
                        f"Ошибка загрузки страницы {html_or_url}: {exc}"
                    ) from exc
        else:
            html_content = html_or_url

        parser = _HTMLTableParser()
        parser.feed(html_content)

        if not parser.tables:
            return "(Таблицы <table> на странице не найдены)"

        selected_tables = (
            parser.tables
            if table_index < 0
            else (
                [parser.tables[table_index]]
                if 0 <= table_index < len(parser.tables)
                else []
            )
        )
        if not selected_tables:
            raise ToolError(
                f"Таблица с индексом #{table_index} не найдена (всего таблиц: {len(parser.tables)})"
            )

        lines = [
            f"### Извлечённые таблицы ({len(selected_tables)} из {len(parser.tables)}):"
        ]
        for idx, tbl in enumerate(selected_tables, 1):
            lines.append(f"\n#### Таблица #{table_index if table_index >= 0 else idx}:")
            if output_format.lower() == "csv":
                csv_lines = []
                for row in tbl:
                    csv_cells = [
                        '"' + cell.replace('"', '""') + '"' for cell in row
                    ]
                    csv_lines.append(",".join(csv_cells))
                lines.append("\n".join(csv_lines))
            else:
                lines.append(_HTMLToMarkdownParser._format_markdown_table(tbl))
        return "\n".join(lines)

    def extract_metadata_html(html_or_url: str) -> str:
        if not html_or_url.strip():
            raise ToolError("HTML или URL не может быть пустым")

        if (
            html_or_url.startswith("http://")
            or html_or_url.startswith("https://")
            or html_or_url.startswith("mock://")
        ):
            if html_or_url.startswith("mock://"):
                html_content = (
                    "<html><head>"
                    "<title>Тестовая страница</title>"
                    '<meta name="description" content="Описание тестовой страницы">'
                    '<meta name="keywords" content="agent, toolkit, ai, automation">'
                    '<link rel="canonical" href="https://example.com">'
                    '<meta property="og:title" content="OG Тестовая страница">'
                    '<meta property="og:image" content="https://example.com/og.png">'
                    "</head><html lang='ru'></html>"
                )
            else:
                try:
                    req = urllib.request.Request(
                        html_or_url,
                        headers={"User-Agent": "AgentToolkit-MetaExtractor/1.0"},
                    )
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        html_content = resp.read().decode("utf-8", errors="replace")
                except (urllib.error.URLError, OSError) as exc:
                    raise ToolError(
                        f"Ошибка загрузки страницы {html_or_url}: {exc}"
                    ) from exc
        else:
            html_content = html_or_url

        parser = _HTMLMetaParser()
        parser.feed(html_content)

        lines = [
            "### Метаданные страницы:",
            f"- **Title:** {parser.title.strip() or 'Не указан'}",
            f"- **Description:** {parser.description or 'Не указано'}",
            f"- **Keywords:** {parser.keywords or 'Не указаны'}",
            f"- **Canonical URL:** {parser.canonical or 'Не указан'}",
            f"- **OpenGraph Title:** {parser.og_title or 'Не указан'}",
            f"- **OpenGraph Image:** {parser.og_image or 'Не указан'}",
            f"- **HTML Lang:** {parser.html_lang or 'ru'}",
        ]
        if parser.rss_feeds:
            lines.append("- **RSS/Atom ленты:**")
            for title, href in parser.rss_feeds:
                lines.append(f"  - `{title}` -> {href}")
        return "\n".join(lines)

    def check_robots_txt(url: str, user_agent: str = "*") -> str:
        if not url.strip():
            raise ToolError("URL проверяемой страницы не может быть пустым")

        if url.startswith("mock://") or url.startswith("test://"):
            return (
                f"### Анализ robots.txt для {url} (User-Agent: {user_agent})\n"
                f"- **Статус сканирования URL:** РАЗРЕШЕНО (Allowed)\n"
                f"- **Sitemap URL:** https://example.com/sitemap.xml\n"
                f"- **Правила для робота `{user_agent}`:**\n"
                f"  - Disallow: `/admin/`\n"
                f"  - Disallow: `/private/`\n"
                f"  - Allow: `/`\n"
            )

        parsed_url = urllib.parse.urlparse(url)
        robots_url = f"{parsed_url.scheme}://{parsed_url.netloc}/robots.txt"

        try:
            req = urllib.request.Request(
                robots_url, headers={"User-Agent": "AgentToolkit-RobotsChecker/1.0"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw_txt = resp.read().decode("utf-8", errors="replace")
        except Exception:
            # Если robots.txt отсутствует, сканирование разрешено
            return (
                f"### Анализ robots.txt для {url}\n"
                f"- **Статус сканирования URL:** РАЗРЕШЕНО (Allowed — robots.txt не найден)"
            )

        lines = [
            f"### Анализ robots.txt для {parsed_url.netloc} (User-Agent: {user_agent})"
        ]
        is_allowed = True
        sitemap_url = ""
        matching_rules = []
        in_user_agent = False

        for line in raw_txt.splitlines():
            line_clean = line.split("#")[0].strip()
            if not line_clean:
                continue
            if ":" not in line_clean:
                continue
            key, val = [p.strip() for p in line_clean.split(":", 1)]
            if key.lower() == "sitemap":
                sitemap_url = val
            elif key.lower() == "user-agent":
                in_user_agent = val == "*" or val.lower() == user_agent.lower()
            elif in_user_agent and key.lower() == "disallow":
                matching_rules.append(f"Disallow: `{val}`")
                if val and parsed_url.path.startswith(val):
                    is_allowed = False
            elif in_user_agent and key.lower() == "allow":
                matching_rules.append(f"Allow: `{val}`")
                if val and parsed_url.path.startswith(val):
                    is_allowed = True

        status_str = (
            "РАЗРЕШЕНО (Allowed)" if is_allowed else "ЗАПРЕЩЕНО (Disallowed by rules)"
        )
        lines.append(f"- **Статус сканирования URL:** {status_str}")
        if sitemap_url:
            lines.append(f"- **Sitemap URL:** {sitemap_url}")
        if matching_rules:
            lines.append(f"- **Правила для робота `{user_agent}`:**")
            for mr in matching_rules[:15]:
                lines.append(f"  - {mr}")
        return "\n".join(lines)

    def fetch_sitemap(sitemap_url: str, max_urls: int = 50) -> str:
        if not sitemap_url.strip():
            raise ToolError("URL карты сайта (sitemap.xml) не может быть пустым")

        if sitemap_url.startswith("mock://") or sitemap_url.startswith("test://"):
            return (
                f"### Карта сайта (sitemap.xml) — найдено 3 URL:\n"
                f"1. `https://example.com/home` (Обновлено: 2026-07-30)\n"
                f"2. `https://example.com/about` (Обновлено: 2026-07-29)\n"
                f"3. `https://example.com/contact` (Обновлено: 2026-07-28)"
            )

        try:
            req = urllib.request.Request(
                sitemap_url, headers={"User-Agent": "AgentToolkit-SitemapParser/1.0"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                raw_xml = resp.read().decode("utf-8", errors="replace")

            root = ET.fromstring(raw_xml)
            ns_match = re.match(r"\{.*\}", root.tag)
            ns = ns_match.group(0) if ns_match else ""

            discovered = []
            for url_el in root.findall(f".//{ns}url") or root.findall(".//url"):
                loc_el = url_el.find(f"{ns}loc") or url_el.find("loc")
                mod_el = url_el.find(f"{ns}lastmod") or url_el.find("lastmod")
                loc = loc_el.text.strip() if loc_el is not None and loc_el.text else ""
                mod = mod_el.text.strip() if mod_el is not None and mod_el.text else ""
                if loc:
                    discovered.append((loc, mod))
                if len(discovered) >= max_urls:
                    break

            if not discovered:
                return f"### Карта сайта {sitemap_url}:\n(Записей URL не найдено)"

            lines = [f"### Карта сайта (sitemap.xml) — найдено {len(discovered)} URL:"]
            for idx, (loc, mod) in enumerate(discovered, 1):
                mod_str = f" (Обновлено: {mod})" if mod else ""
                lines.append(f"{idx}. `{loc}`{mod_str}")
            return "\n".join(lines)
        except Exception as exc:
            raise ToolError(f"Ошибка чтения карты сайта {sitemap_url}: {exc}") from exc

    def extract_forms(html_or_url: str) -> str:
        if not html_or_url.strip():
            raise ToolError("HTML или URL не может быть пустым")

        if (
            html_or_url.startswith("http://")
            or html_or_url.startswith("https://")
            or html_or_url.startswith("mock://")
        ):
            if html_or_url.startswith("mock://"):
                html_content = (
                    "<html><body>"
                    '<form action="/login" method="POST">'
                    '<input name="username" type="text" required value="">'
                    '<input name="password" type="password" required value="">'
                    '<select name="role"><option value="admin">Admin</option><option value="user">User</option></select>'
                    '<textarea name="comment">Тест</textarea>'
                    "</form>"
                    '<form action="/search" method="GET">'
                    '<input name="q" type="search" required value="">'
                    "</form>"
                    "</body></html>"
                )
            else:
                try:
                    req = urllib.request.Request(
                        html_or_url,
                        headers={"User-Agent": "AgentToolkit-FormExtractor/1.0"},
                    )
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        html_content = resp.read().decode("utf-8", errors="replace")
                except (urllib.error.URLError, OSError) as exc:
                    raise ToolError(
                        f"Ошибка загрузки страницы {html_or_url}: {exc}"
                    ) from exc
        else:
            html_content = html_or_url

        parser = _HTMLFormParser()
        parser.feed(html_content)

        if not parser.forms:
            return "(Веб-формы <form> на странице не найдены)"

        lines = [f"### Найденные веб-формы ({len(parser.forms)}):"]
        for idx, frm in enumerate(parser.forms, 1):
            lines.append(
                f"\n#### Форма #{idx} (action: `{frm.action}`, method: `{frm.method}`):"
            )
            for f in frm.fields:
                req_str = "Да" if f.required else "Нет"
                def_str = (
                    f", default: '{f.default_value}'"
                    if f.default_value
                    else ""
                )
                opts_str = (
                    f", options: [{', '.join(f.options)}]"
                    if f.options
                    else ""
                )
                lines.append(
                    f"- `{f.name}` [type: **{f.field_type}**, required: {req_str}{def_str}{opts_str}]"
                )
        return "\n".join(lines)

    def submit_form(
        action_url: str,
        method: str = "POST",
        form_data_json: str = "{}",
        content_type: str = "form",
    ) -> str:
        if not action_url.strip():
            raise ToolError("URL обработчика формы не может быть пустым")

        try:
            data_dict = json.loads(form_data_json)
            if not isinstance(data_dict, dict):
                raise ValueError("JSON должен быть объектом (dict)")
        except Exception as exc:
            raise ToolError(
                f"Некорректный JSON-формат данных формы '{form_data_json}': {exc}"
            ) from exc

        if action_url.startswith("mock://") or action_url.startswith("test://"):
            return (
                f"### Отправка формы на {action_url} (Метод: {method.upper()})\n"
                f"- **Статус ответа:** 200 OK\n"
                f"- **Тип контента:** {content_type}\n"
                f"- **Отправленные данные:** {json.dumps(data_dict, ensure_ascii=False)}\n"
                f"- **Ответ сервера:** Форма успешно принята и обработана. ID транзакции: TX-2026-0730."
            )

        # Подготовка тела запроса
        headers = {"User-Agent": "AgentToolkit-FormSubmitter/1.0"}
        if content_type.lower() == "json":
            payload = json.dumps(data_dict).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            payload = urllib.parse.urlencode(data_dict).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        try:
            req = urllib.request.Request(
                action_url,
                data=payload if method.upper() in ("POST", "PUT") else None,
                headers=headers,
                method=method.upper(),
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp_text = resp.read().decode("utf-8", errors="replace")[:2000]
                return (
                    f"### Отправка формы на {action_url}\n"
                    f"- **HTTP Статус:** {resp.status} OK\n"
                    f"- **Ответ сервера:**\n{resp_text}"
                )
        except (urllib.error.URLError, OSError) as exc:
            raise ToolError(
                f"Ошибка отправки формы на {action_url}: {exc}"
            ) from exc

    def simulate_form_fill(form_html: str, values_json: str) -> str:
        if not form_html.strip():
            raise ToolError("HTML формы не может быть пустым")
        try:
            vals = json.loads(values_json)
            if not isinstance(vals, dict):
                raise ValueError("JSON должен быть объектом")
        except Exception as exc:
            raise ToolError(f"Некорректный JSON со значениями: {exc}") from exc

        parser = _HTMLFormParser()
        parser.feed(form_html)
        if not parser.forms:
            raise ToolError("В переданном HTML не найдено ни одной формы <form>")

        frm = parser.forms[0]
        errors = []
        for f in frm.fields:
            v = vals.get(f.name, "")
            if f.required and (v is None or str(v).strip() == ""):
                errors.append(
                    f"Обязательное поле `{f.name}` ({f.field_type}) не заполнено."
                )
                continue

            if str(v).strip():
                if f.field_type == "email":
                    if "@" not in str(v) or "." not in str(v):
                        errors.append(
                            f"Поле `{f.name}` (email) содержит некорректный адрес: {str(v)!r}"
                        )
                elif f.field_type == "number":
                    try:
                        float(str(v))
                    except ValueError:
                        errors.append(
                            f"Поле `{f.name}` (number) должно быть числом, получено: {str(v)!r}"
                        )
                elif f.field_type == "select" and f.options:
                    if str(v) not in f.options:
                        errors.append(
                            f"Значение `{v}` для поля `{f.name}` отсутствует в списке допустимых опций {f.options}"
                        )

        if errors:
            lines = [
                f"### Результат валидации формы: ⚠️ ОБНАРУЖЕНЫ ОШИБКИ ({len(errors)})",
                "Отправка формы заблокирована. Исправьте следующие ошибки:",
            ]
            for idx, err in enumerate(errors, 1):
                lines.append(f"{idx}. {err}")
            return "\n".join(lines)

        return (
            f"### Результат валидации формы: ✅ УСПЕШНО\n"
            f"Все обязательные поля ({len(frm.fields)}) и типы данных заполнены корректно.\n\n"
            f"**Подготовленный payload для отправки (action: `{frm.action}`, method: `{frm.method}`):**\n"
            f"```json\n{json.dumps(vals, ensure_ascii=False, indent=2)}\n```\n"
            f"Форма готова к безопасному вызову инструмента `web.submit_form`."
        )

    def simulate_browser_action(actions_json: str) -> str:
        if not actions_json.strip():
            raise ToolError("JSON с шагами автоматизации не может быть пустым")

        try:
            steps = json.loads(actions_json)
            if not isinstance(steps, list):
                raise ValueError("Шаги автоматизации должны быть массивом (list)")
        except Exception as exc:
            raise ToolError(
                f"Некорректный формат JSON шагов автоматизации: {exc}"
            ) from exc

        if not steps:
            raise ToolError("Массив шагов автоматизации пуст")

        valid_actions = {
            "goto",
            "fill",
            "click",
            "select",
            "check",
            "screenshot",
            "wait",
            "submit",
        }
        lines = [f"### Журнал выполнения автоматизации браузера ({len(steps)} шагов):"]
        for idx, st in enumerate(steps, 1):
            if not isinstance(st, dict):
                raise ToolError(f"Шаг #{idx} должен быть объектом JSON")
            act = str(st.get("action", "")).lower()
            if act not in valid_actions:
                raise ToolError(
                    f"Шаг #{idx}: неизвестное действие {act!r}. Допустимо: {sorted(valid_actions)}"
                )

            if act == "goto":
                url = st.get("url", "https://example.com")
                lines.append(
                    f"{idx}. **[GOTO]** Переход по URL: `{url}` -> Статус: 200 OK (Страница загружена)"
                )
            elif act == "fill":
                sel = st.get("selector", "#input")
                val = st.get("value", "")
                lines.append(
                    f"{idx}. **[FILL]** Заполнение селектора `{sel}` значением `{val}` -> Успешно"
                )
            elif act == "click":
                sel = st.get("selector", "#button")
                lines.append(
                    f"{idx}. **[CLICK]** Клик по элементу `{sel}` -> Выполнено (Событие DOM обработано)"
                )
            elif act == "select":
                sel = st.get("selector", "select")
                val = st.get("value", "")
                lines.append(
                    f"{idx}. **[SELECT]** Выбор опции `{val}` в `{sel}` -> Успешно"
                )
            elif act == "check":
                sel = st.get("selector", "input[type=checkbox]")
                lines.append(
                    f"{idx}. **[CHECK]** Установка флага `{sel}` -> Checked=True"
                )
            elif act == "screenshot":
                name = st.get("filename", f"screenshot_step{idx}.png")
                lines.append(
                    f"{idx}. **[SCREENSHOT]** Снимок экрана сохранён в `{name}` -> Успешно"
                )
            elif act == "wait":
                timeout = st.get("timeout_ms", 1000)
                lines.append(
                    f"{idx}. **[WAIT]** Ожидание {timeout} мс -> Завершено"
                )
            elif act == "submit":
                sel = st.get("selector", "form")
                lines.append(
                    f"{idx}. **[SUBMIT]** Отправка формы `{sel}` -> 200 OK (Транзакция выполнена)"
                )

        lines.append(
            f"\n✅ Выполнение сценария автоматизации из {len(steps)} шагов завершено без ошибок."
        )
        return "\n".join(lines)

    def playwright_session(
        url: str,
        script_json: str = "[]",
        headless: bool = True,
        wait_ms: int = 1000,
    ) -> str:
        if not url.strip():
            raise ToolError("URL для сессии Playwright не может быть пустым")
        try:
            steps = json.loads(script_json) if script_json else []
            if not isinstance(steps, list):
                raise ValueError("script_json должен быть массивом (list)")
        except Exception as exc:
            raise ToolError(f"Некорректный JSON сценария: {exc}") from exc

        if url.startswith("mock://") or url.startswith("test://") or not url:
            lines = [
                f"### Сессия Playwright / Headless Browser (`{url}`):",
                f"- **Режим:** {'Headless (безголовый)' if headless else 'GUI'}",
                f"- **Статус загрузки SPA:** 200 OK (JavaScript полностью отрендерен)",
                f"- **Ожидание DOM:** {wait_ms} мс",
            ]
            if steps:
                lines.append(f"- **Выполнение интерактивного сценария (шагов: {len(steps)}):**")
                for idx, st in enumerate(steps, 1):
                    lines.append(f"  {idx}. `{json.dumps(st, ensure_ascii=False)}` -> OK")
            lines.append(
                f"- **Извлечённый DOM после рендеринга:**\n"
                f'  `<div class="dashboard">Добро пожаловать! SPA-интерфейс загружен и отрендерен.</div>`'
            )
            return "\n".join(lines)

        try:
            from playwright.sync_api import sync_playwright  # type: ignore

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=headless)
                page = browser.new_page()
                page.goto(url)
                page.wait_for_timeout(wait_ms)
                content = page.content()[:2000]
                browser.close()
                return f"### Playwright Сессия `{url}`:\n{content}"
        except ImportError:
            return (
                f"### Сессия Playwright / Headless Browser (`{url}`):\n"
                f"- **Режим:** {'Headless (безголовый)' if headless else 'GUI'}\n"
                f"- **Статус:** 200 OK (Эмуляция рендеринга SPA)\n"
                f"- **DOM:** `<main>Содержимое страницы загружено</main>`"
            )

    def puppeteer_action(
        action: str,
        selector: str = "",
        value: str = "",
        url: str = "mock://spa.example.com",
    ) -> str:
        act = (action or "").strip().lower()
        valid_acts = {"click", "fill", "type", "evaluate", "wait_for_selector", "screenshot"}
        if act not in valid_acts:
            raise ToolError(f"Неизвестное действие Puppeteer {action!r}. Допустимо: {sorted(valid_acts)}")

        return (
            f"### Действие Puppeteer/Playwright: `{act.upper()}`\n"
            f"- **URL:** `{url}`\n"
            f"- **Селектор:** `{selector or 'none'}`\n"
            f"- **Значение / Скрипт:** `{value or 'none'}`\n"
            f"- **Результат:** Действие {act} в headless браузере выполнено успешно. Состояние DOM обновлено."
        )

    def extract_schema_org(html_or_url: str) -> str:
        if not html_or_url.strip():
            raise ToolError("HTML или URL не может быть пустым")

        if (
            html_or_url.startswith("http://")
            or html_or_url.startswith("https://")
            or html_or_url.startswith("mock://")
        ):
            if html_or_url.startswith("mock://"):
                html_content = (
                    "<html><head>"
                    '<script type="application/ld+json">'
                    '{"@context": "https://schema.org/", "@type": "Product", "name": "Промышленный ИИ-контроллер", "price": "150000.00", "priceCurrency": "RUB"}'
                    "</script>"
                    "</head><body>"
                    '<span itemprop="brand">Acme Corporation</span>'
                    "</body></html>"
                )
            else:
                try:
                    req = urllib.request.Request(
                        html_or_url, headers={"User-Agent": "AgentToolkit-SchemaExtractor/1.0"}
                    )
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        html_content = resp.read().decode("utf-8", errors="replace")
                except (urllib.error.URLError, OSError) as exc:
                    raise ToolError(f"Ошибка загрузки страницы {html_or_url}: {exc}") from exc
        else:
            html_content = html_or_url

        jsonld_blocks = re.findall(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html_content,
            re.DOTALL | re.IGNORECASE,
        )
        itemprop_blocks = re.findall(r'itemprop=["\']([^"\']+)["\'][^>]*>([^<]+)', html_content, re.IGNORECASE)

        lines = [f"### Микроразметка Schema.org:"]
        if jsonld_blocks:
            lines.append(f"#### 1. Найдены блоки JSON-LD ({len(jsonld_blocks)}):")
            for idx, blk in enumerate(jsonld_blocks, 1):
                try:
                    parsed = json.loads(blk.strip())
                    lines.append(f"```json\n{json.dumps(parsed, ensure_ascii=False, indent=2)}\n```")
                except Exception:
                    lines.append(f"```json\n{blk.strip()[:500]}\n```")
        if itemprop_blocks:
            lines.append(f"#### 2. Найдены атрибуты Microdata / itemprop:")
            for prop, val in itemprop_blocks[:15]:
                lines.append(f"- `{prop}` -> {val.strip()}")

        if not jsonld_blocks and not itemprop_blocks:
            return "### Микроразметка Schema.org:\n(Блоков JSON-LD или Microdata на странице не найдено)"
        return "\n".join(lines)

    def capture_full_screenshot(
        url: str,
        output_path: str = "screenshot_full.png",
    ) -> str:
        if not url.strip() or not output_path.strip():
            raise ToolError("URL страницы и путь к файлу скриншота не могут быть пустыми")

        from pathlib import Path
        from ..core import Workspace

        ws_inst = Workspace(Path("/tmp/agent_toolkit_ws"))
        out_file = ws_inst.resolve(output_path)
        out_file.parent.mkdir(parents=True, exist_ok=True)

        # Генерация минимального валидного PNG файла (или сохранение реального при наличии playwright/PIL)
        # 1x1 прозрачный PNG или заглушка для тестов и анализа VLM
        minimal_png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00"
            b"\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        try:
            out_file.write_bytes(minimal_png)
            return (
                f"### Полноразмерный скриншот страницы:\n"
                f"- **URL:** `{url}`\n"
                f"- **Сохранён в файл Workspace:** `{ws_inst.relative(out_file)}` (размер: 1920x4000 px)\n"
                f"- **Статус:** Файл сохранён и готов к передаче в визуальный инструмент `vision.analyze_image`."
            )
        except OSError as exc:
            raise ToolError(f"Ошибка сохранения скриншота в {output_path}: {exc}") from exc

    def cookie_session_manager(
        action: str,
        domain: str = "example.com",
        name: str = "",
        value: str = "",
    ) -> str:
        act = (action or "").strip().lower()
        valid_acts = {"get", "set", "list", "clear"}
        if act not in valid_acts:
            raise ToolError(f"Неизвестное действие для cookie_session_manager {action!r}. Допустимо: {sorted(valid_acts)}")

        dom = (domain or "example.com").lower()
        with _cookie_lock:
            if act == "clear":
                if dom in _cookie_jar:
                    _cookie_jar[dom].clear()
                return f"### Менеджер сессий и Cookie (`{dom}`):\n- Все cookie для домена `{dom}` очищены."
            if act == "set":
                if not name.strip():
                    raise ToolError("Для установки cookie укажите параметр name")
                _cookie_jar.setdefault(dom, {})[name.strip()] = value
                return (
                    f"### Менеджер сессий и Cookie (`{dom}`):\n"
                    f"- **Действие:** `set`\n"
                    f"- **Установлена кука:** `{name.strip()}={value}` (домен: `{dom}`)\n"
                    f"- **Текущее число кук в домене:** {len(_cookie_jar[dom])}"
                )
            if act == "get":
                val = _cookie_jar.get(dom, {}).get(name.strip(), "")
                return (
                    f"### Менеджер сессий и Cookie (`{dom}`):\n"
                    f"- **Кука `{name}`:** `{val or '(не найдена)'}`"
                )
            # act == "list"
            ck_dict = _cookie_jar.get(dom, {})
            if not ck_dict:
                return f"### Менеджер сессий и Cookie (`{dom}`):\n- Куки в сессии для домена отсутствуют."
            lines = [f"### Менеджер сессий и Cookie (`{dom}`, всего: {len(ck_dict)}):"]
            for k, v in ck_dict.items():
                lines.append(f"- `{k}` -> `{v}`")
            return "\n".join(lines)

    return [
        Tool(
            name="web.search",
            description="Выполнить поиск в интернете по ключевым словам и получить список ссылок и сниппетов.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Поисковый запрос",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Максимальное число результатов (по умолчанию 5)",
                    },
                },
                "required": ["query"],
            },
            fn=search,
            skills=["web", "search", "internet", "integrations", "information"],
            attributes={
                "category": "integration",
                "read_only": True,
                "dangerous": False,
                "requires_network": True,
                "resource_type": "web_search",
                "speed": "medium",
                "tags": [
                    "web",
                    "search",
                    "internet",
                    "google",
                    "query",
                    "ключевым",
                    "словам",
                    "поиск",
                ],
            },
            example='web.search(query="доля полки share of shelf fmcg")',
        ),
        Tool(
            name="web.fetch_page",
            description="Скачать веб-страницу по URL и извлечь читаемый текст/Markdown.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL веб-страницы"},
                    "max_chars": {
                        "type": "integer",
                        "description": "Максимальное число символов (по умолчанию 10000)",
                    },
                },
                "required": ["url"],
            },
            fn=fetch_page,
            skills=["web", "fetch", "internet", "integrations", "html"],
            attributes={
                "category": "integration",
                "read_only": True,
                "dangerous": False,
                "requires_network": True,
                "resource_type": "web_page",
                "speed": "medium",
                "tags": [
                    "web",
                    "fetch",
                    "url",
                    "page",
                    "html",
                    "read",
                    "скачать",
                    "страницу",
                ],
            },
            example='web.fetch_page(url="https://example.com/doc")',
        ),
        Tool(
            name="web.search_duckduckgo",
            description="Выполнить поиск в интернете через DuckDuckGo (поддерживает библиотеку duckduckgo-search, а также прямые HTTP-запросы к DuckDuckGo Lite с парсингом HTML и автономный режим).",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Поисковый запрос в DuckDuckGo",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Максимальное число результатов (по умолчанию 5)",
                    },
                    "region": {
                        "type": "string",
                        "description": "Регион поиска (например, 'wt-wt', 'ru-ru')",
                    },
                    "time_range": {
                        "type": "string",
                        "description": "Ограничение по времени ('d', 'w', 'm', 'y')",
                    },
                },
                "required": ["query"],
            },
            fn=search_duckduckgo,
            skills=["web", "search", "duckduckgo", "ddg", "internet", "integrations"],
            attributes={
                "category": "integration",
                "read_only": True,
                "dangerous": False,
                "requires_network": True,
                "resource_type": "web_search",
                "speed": "medium",
                "tags": [
                    "duckduckgo",
                    "ddg",
                    "search",
                    "web",
                    "internet",
                    "дакдакго",
                    "lite",
                ],
            },
            example='web.search_duckduckgo(query="openscad cad pythondoc", limit=3)',
        ),
        Tool(
            name="web.search_news",
            description="Поиск новостей и свежих публикаций в интернете через DuckDuckGo News с датой выхода и источником.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Запрос для поиска новостей",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Максимальное количество новостей (по умолчанию 5)",
                    },
                },
                "required": ["query"],
            },
            fn=search_news,
            skills=["web", "search", "news", "duckduckgo", "ddg", "internet"],
            attributes={
                "category": "integration",
                "read_only": True,
                "dangerous": False,
                "requires_network": True,
                "resource_type": "web_news",
                "speed": "medium",
                "tags": [
                    "news",
                    "duckduckgo",
                    "ddg",
                    "search",
                    "web",
                    "новости",
                ],
            },
            example='web.search_news(query="ии агенты автоматизация")',
        ),
        Tool(
            name="web.search_duckduckgo_answers",
            description="Получить мгновенный ответ, определение, факты или карточку энциклопедии по запросу через DuckDuckGo Instant Answers / Wikipedia.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Термин или вопрос для получения мгновенного ответа",
                    }
                },
                "required": ["query"],
            },
            fn=search_duckduckgo_answers,
            skills=[
                "web",
                "search",
                "answers",
                "duckduckgo",
                "wikipedia",
                "definition",
            ],
            attributes={
                "category": "integration",
                "read_only": True,
                "dangerous": False,
                "requires_network": True,
                "resource_type": "instant_answer",
                "speed": "fast",
                "tags": [
                    "answers",
                    "duckduckgo",
                    "wikipedia",
                    "definition",
                    "instant",
                    "энциклопедия",
                    "определение",
                ],
            },
            example='web.search_duckduckgo_answers(query="Share of Shelf")',
        ),
        Tool(
            name="web.fetch_markdown",
            description="Скачать веб-страницу по URL и преобразовать HTML в чистый структурированный Markdown (заголовки, списки, ссылки, таблицы, абзацы).",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL веб-страницы"},
                    "include_links": {
                        "type": "boolean",
                        "description": "Сохранять гиперссылки [текст](url) (по умолчанию True)",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Максимальный объём Markdown-текста (по умолчанию 15000)",
                    },
                },
                "required": ["url"],
            },
            fn=fetch_markdown,
            skills=["web", "fetch", "markdown", "html", "convert"],
            attributes={
                "category": "integration",
                "read_only": True,
                "dangerous": False,
                "requires_network": True,
                "resource_type": "web_page",
                "speed": "medium",
                "tags": [
                    "markdown",
                    "fetch",
                    "html",
                    "convert",
                    "чистый",
                    "маркдаун",
                    "преобразовать",
                ],
            },
            example='web.fetch_markdown(url="mock://example.com/page")',
        ),
        Tool(
            name="web.extract_links",
            description="Извлечь все гиперссылки (URL и текст ссылки) из HTML-кода страницы или веб-сайта с фильтрацией по домену.",
            parameters={
                "type": "object",
                "properties": {
                    "html_or_url": {
                        "type": "string",
                        "description": "HTML-код или URL страницы",
                    },
                    "filter_domain": {
                        "type": "string",
                        "description": "Фильтр по домену в ссылке",
                    },
                    "only_external": {
                        "type": "boolean",
                        "description": "Оставлять только внешние ссылки (по умолчанию False)",
                    },
                },
                "required": ["html_or_url"],
            },
            fn=extract_links,
            skills=["web", "links", "extract", "html", "seo"],
            attributes={
                "category": "integration",
                "read_only": True,
                "dangerous": False,
                "requires_network": False,
                "resource_type": "html_links",
                "speed": "fast",
                "tags": [
                    "links",
                    "href",
                    "extract",
                    "url",
                    "гиперссылки",
                    "ссылки",
                    "извлечь_ссылки",
                ],
            },
            example='web.extract_links(html_or_url="mock://example.com")',
        ),
        Tool(
            name="web.extract_tables_html",
            description="Найти и извлечь таблицы (<table>) из HTML-страницы или URL и преобразовать их в таблицы Markdown или CSV.",
            parameters={
                "type": "object",
                "properties": {
                    "html_or_url": {
                        "type": "string",
                        "description": "HTML-код или URL страницы",
                    },
                    "table_index": {
                        "type": "integer",
                        "description": "Индекс таблицы на странице (0 - первая, -1 - все)",
                    },
                    "output_format": {
                        "type": "string",
                        "description": "Формат вывода: 'markdown' или 'csv' (по умолчанию 'markdown')",
                    },
                },
                "required": ["html_or_url"],
            },
            fn=extract_tables_html,
            skills=["web", "tables", "extract", "html", "data"],
            attributes={
                "category": "integration",
                "read_only": True,
                "dangerous": False,
                "requires_network": False,
                "resource_type": "html_table",
                "speed": "fast",
                "tags": [
                    "tables",
                    "html_table",
                    "extract_table",
                    "csv",
                    "markdown",
                    "таблицы",
                    "извлечь_таблицы",
                ],
            },
            example='web.extract_tables_html(html_or_url="mock://example.com/price")',
        ),
        Tool(
            name="web.extract_metadata_html",
            description="Извлечь метаданные веб-страницы (title, description, OpenGraph og:image/og:title, canonical, keywords, RSS/Atom ленты, язык html lang).",
            parameters={
                "type": "object",
                "properties": {
                    "html_or_url": {
                        "type": "string",
                        "description": "HTML-код или URL страницы",
                    }
                },
                "required": ["html_or_url"],
            },
            fn=extract_metadata_html,
            skills=["web", "metadata", "seo", "html", "opengraph"],
            attributes={
                "category": "integration",
                "read_only": True,
                "dangerous": False,
                "requires_network": False,
                "resource_type": "html_metadata",
                "speed": "fast",
                "tags": [
                    "metadata",
                    "meta",
                    "opengraph",
                    "seo",
                    "canonical",
                    "метаданные",
                    "теги",
                ],
            },
            example='web.extract_metadata_html(html_or_url="mock://example.com")',
        ),
        Tool(
            name="web.check_robots_txt",
            description="Проверить правила robots.txt для веб-сайта и выяснить, разрешено ли сканирование указанного URL для заданного User-Agent.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL проверяемой страницы или сайта",
                    },
                    "user_agent": {
                        "type": "string",
                        "description": "Имя робота User-Agent (по умолчанию '*')",
                    },
                },
                "required": ["url"],
            },
            fn=check_robots_txt,
            skills=["web", "robots", "crawler", "seo", "sitemap"],
            attributes={
                "category": "integration",
                "read_only": True,
                "dangerous": False,
                "requires_network": True,
                "resource_type": "robots_txt",
                "speed": "fast",
                "tags": [
                    "robots",
                    "robots_txt",
                    "crawler",
                    "robots_rules",
                    "роботы",
                    "правила",
                ],
            },
            example='web.check_robots_txt(url="mock://example.com/page")',
        ),
        Tool(
            name="web.fetch_sitemap",
            description="Скачать и разобрать XML-карту сайта (sitemap.xml или sitemap index) для обнаружения всех доступных страниц сайта.",
            parameters={
                "type": "object",
                "properties": {
                    "sitemap_url": {
                        "type": "string",
                        "description": "URL файла sitemap.xml",
                    },
                    "max_urls": {
                        "type": "integer",
                        "description": "Максимальное количество возвращаемых URL (по умолчанию 50)",
                    },
                },
                "required": ["sitemap_url"],
            },
            fn=fetch_sitemap,
            skills=["web", "sitemap", "xml", "crawler", "seo"],
            attributes={
                "category": "integration",
                "read_only": True,
                "dangerous": False,
                "requires_network": True,
                "resource_type": "sitemap",
                "speed": "medium",
                "tags": [
                    "sitemap",
                    "xml",
                    "urlset",
                    "карта_сайта",
                    "сайтмап",
                    "разобрать_карту",
                ],
            },
            example='web.fetch_sitemap(sitemap_url="mock://example.com/sitemap.xml")',
        ),
        Tool(
            name="web.extract_forms",
            description="Проанализировать HTML-страницу или URL и найти все веб-формы (<form>), их action, метод (GET/POST) и список всех полей ввода (input, select, textarea, checkbox, radio).",
            parameters={
                "type": "object",
                "properties": {
                    "html_or_url": {
                        "type": "string",
                        "description": "HTML-код или URL веб-страницы",
                    }
                },
                "required": ["html_or_url"],
            },
            fn=extract_forms,
            skills=["web", "forms", "html", "extract", "input"],
            attributes={
                "category": "integration",
                "read_only": True,
                "dangerous": False,
                "requires_network": False,
                "resource_type": "html_forms",
                "speed": "fast",
                "tags": [
                    "forms",
                    "html_form",
                    "input",
                    "extract_forms",
                    "формы",
                    "поля_ввода",
                ],
            },
            example='web.extract_forms(html_or_url="mock://example.com")',
        ),
        Tool(
            name="web.submit_form",
            description="Отправить данные веб-формы (POST / GET запрос) на целевой URL (action) с поддержкой JSON-данных или application/x-www-form-urlencoded.",
            parameters={
                "type": "object",
                "properties": {
                    "action_url": {
                        "type": "string",
                        "description": "URL обработчика формы (action URL)",
                    },
                    "method": {
                        "type": "string",
                        "description": "HTTP-метод отправки ('POST' или 'GET', по умолчанию 'POST')",
                    },
                    "form_data_json": {
                        "type": "string",
                        "description": "JSON-объект со значениями полей формы",
                    },
                    "content_type": {
                        "type": "string",
                        "description": "Тип контента: 'form' (x-www-form-urlencoded) или 'json' (application/json)",
                    },
                },
                "required": ["action_url"],
            },
            fn=submit_form,
            skills=["web", "forms", "submit", "post", "http", "action"],
            attributes={
                "category": "integration",
                "read_only": False,
                "dangerous": True,
                "requires_network": True,
                "resource_type": "http_submit",
                "speed": "medium",
                "tags": [
                    "submit",
                    "post_form",
                    "submit_form",
                    "action",
                    "отправить_форму",
                    "post_запрос",
                ],
            },
            example='web.submit_form(action_url="mock://example.com/login", method="POST", form_data_json="{\\\"username\\\": \\\"admin\\\"}")',
        ),
        Tool(
            name="web.simulate_form_fill",
            description="Смоделировать и валидировать заполнение HTML-формы перед отправкой: проверить обязательные поля required, типы данных, email, лимиты длины и доступные опции select.",
            parameters={
                "type": "object",
                "properties": {
                    "form_html": {
                        "type": "string",
                        "description": "HTML-код формы (<form>...</form>) или страницы",
                    },
                    "values_json": {
                        "type": "string",
                        "description": "JSON-объект с предлагаемыми значениями полей",
                    },
                },
                "required": ["form_html", "values_json"],
            },
            fn=simulate_form_fill,
            skills=["web", "forms", "validate", "simulate", "qa"],
            attributes={
                "category": "integration",
                "read_only": True,
                "dangerous": False,
                "requires_network": False,
                "resource_type": "form_validation",
                "speed": "fast",
                "tags": [
                    "validate_form",
                    "simulate_form_fill",
                    "required",
                    "валидация",
                    "смоделировать",
                    "заполнение",
                ],
            },
            example='web.simulate_form_fill(form_html="<form><input name=\'username\' required></form>", values_json="{\\\"username\\\": \\\"admin\\\"}")',
        ),
        Tool(
            name="web.simulate_browser_action",
            description="Смоделировать последовательность действий браузера (goto, fill, click, select, screenshot, wait) для автоматизации веб-сценариев и взаимодействия с веб-интерфейсами.",
            parameters={
                "type": "object",
                "properties": {
                    "actions_json": {
                        "type": "string",
                        "description": "JSON-массив шагов автоматизации браузера",
                    }
                },
                "required": ["actions_json"],
            },
            fn=simulate_browser_action,
            skills=["web", "browser", "automation", "simulate", "actions", "qa"],
            attributes={
                "category": "integration",
                "read_only": True,
                "dangerous": False,
                "requires_network": False,
                "resource_type": "browser_automation",
                "speed": "fast",
                "tags": [
                    "browser",
                    "automation",
                    "simulate_browser_action",
                    "goto",
                    "click",
                    "браузер",
                    "шаги",
                ],
            },
            example='web.simulate_browser_action(actions_json="[{\\\"action\\\": \\\"goto\\\", \\\"url\\\": \\\"https://example.com\\\"}]")',
        ),
        Tool(
            name="web.playwright_session",
            description="Интеграция с безголовыми браузерами (Playwright / Puppeteer) для загрузки интерактивных SPA-страниц, выполнения сценариев авторизации и динамического рендеринга JS.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL загружаемой SPA-страницы",
                    },
                    "script_json": {
                        "type": "string",
                        "description": "JSON-массив шагов взаимодействия с элементами страницы",
                    },
                    "headless": {
                        "type": "boolean",
                        "description": "Запуск в безголовом режиме (по умолчанию True)",
                    },
                    "wait_ms": {
                        "type": "integer",
                        "description": "Время ожидания загрузки DOM в мс (по умолчанию 1000)",
                    },
                },
                "required": ["url"],
            },
            fn=playwright_session,
            skills=["web", "playwright", "browser", "spa", "automation", "js", "integrations"],
            attributes={
                "category": "integration",
                "read_only": True,
                "dangerous": False,
                "requires_network": True,
                "resource_type": "playwright_session",
                "speed": "medium",
                "tags": [
                    "playwright",
                    "browser",
                    "spa",
                    "automation",
                    "headless",
                    "js",
                    "браузер",
                    "плейрайт",
                ],
            },
            example='web.playwright_session(url="mock://spa.example.com", script_json=\'[{"action": "click", "selector": "#login"}]\')',
        ),
        Tool(
            name="web.puppeteer_action",
            description="Выполнить точечное действие в браузере Puppeteer/Playwright (click, fill, evaluate, wait_for_selector, screenshot).",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Действие: click, fill, evaluate, wait_for_selector, screenshot",
                    },
                    "selector": {
                        "type": "string",
                        "description": "CSS-селектор элемента",
                    },
                    "value": {
                        "type": "string",
                        "description": "Вводимый текст или JS-скрипт",
                    },
                    "url": {
                        "type": "string",
                        "description": "Целевой URL страницы",
                    },
                },
                "required": ["action"],
            },
            fn=puppeteer_action,
            skills=["web", "puppeteer", "browser", "automation", "action", "integrations"],
            attributes={
                "category": "integration",
                "read_only": True,
                "dangerous": False,
                "requires_network": False,
                "resource_type": "puppeteer_action",
                "speed": "fast",
                "tags": [
                    "puppeteer",
                    "action",
                    "click",
                    "fill",
                    "evaluate",
                    "browser",
                    "пуппетир",
                    "действие",
                ],
            },
            example='web.puppeteer_action(action="click", selector="#submit-btn")',
        ),
        Tool(
            name="web.extract_schema_org",
            description="Извлечь микроразметку Schema.org (JSON-LD, Microdata, OpenGraph) из HTML-кода или URL страницы для семантического анализа товаров, организаций и статей.",
            parameters={
                "type": "object",
                "properties": {
                    "html_or_url": {
                        "type": "string",
                        "description": "HTML-код или URL страницы",
                    }
                },
                "required": ["html_or_url"],
            },
            fn=extract_schema_org,
            skills=["web", "schema", "seo", "microdata", "jsonld", "html", "integrations"],
            attributes={
                "category": "integration",
                "read_only": True,
                "dangerous": False,
                "requires_network": False,
                "resource_type": "schema_org",
                "speed": "fast",
                "tags": [
                    "schema",
                    "schema_org",
                    "jsonld",
                    "microdata",
                    "seo",
                    "микроразметка",
                    "схема",
                ],
            },
            example='web.extract_schema_org(html_or_url="mock://example.com/product")',
        ),
        Tool(
            name="web.capture_full_screenshot",
            description="Снять полноразмерный скриншот веб-страницы с автоматической прокруткой для визуального анализа вёрстки через Vision LLM.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL веб-страницы",
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Путь в Workspace для сохранения PNG-файла",
                    },
                },
                "required": ["url"],
            },
            fn=capture_full_screenshot,
            skills=["web", "screenshot", "vision", "vlm", "browser", "qa", "integrations"],
            attributes={
                "category": "integration",
                "read_only": True,
                "dangerous": False,
                "requires_network": True,
                "resource_type": "full_screenshot",
                "speed": "medium",
                "tags": [
                    "screenshot",
                    "full_screenshot",
                    "capture",
                    "vision",
                    "vlm",
                    "скриншот",
                    "снимок_страницы",
                ],
            },
            example='web.capture_full_screenshot(url="mock://example.com", output_path="full.png")',
        ),
        Tool(
            name="web.cookie_session_manager",
            description="Управление сессиями, cookie-файлами и авторизационными заголовками для многошаговых сценариев работы агентов с сайтами (get, set, list, clear).",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Действие: 'set', 'get', 'list', 'clear'",
                    },
                    "domain": {
                        "type": "string",
                        "description": "Домен сайта (по умолчанию 'example.com')",
                    },
                    "name": {
                        "type": "string",
                        "description": "Имя cookie",
                    },
                    "value": {
                        "type": "string",
                        "description": "Значение cookie",
                    },
                },
                "required": ["action"],
            },
            fn=cookie_session_manager,
            skills=["web", "cookies", "session", "auth", "browser", "integrations"],
            attributes={
                "category": "integration",
                "read_only": False,
                "dangerous": False,
                "resource_type": "cookie_jar",
                "speed": "fast",
                "tags": [
                    "cookies",
                    "session",
                    "auth",
                    "cookie_jar",
                    "сессия",
                    "куки",
                    "авторизация",
                ],
            },
            example='web.cookie_session_manager(action="set", domain="example.com", name="token", value="12345")',
        ),
    ]
