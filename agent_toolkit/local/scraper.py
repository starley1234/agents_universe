"""Инструменты скрапинга, DOM-селекторов и RSS-лент (html.*, scraper.*)."""
from __future__ import annotations

import html.parser
import xml.etree.ElementTree as ET
from typing import Any

from ..core import Tool, ToolError, Workspace


class _SelectorParser(html.parser.HTMLParser):
    def __init__(self, tag_name: str, class_name: str = "", id_name: str = "") -> None:
        super().__init__()
        self.tag_name = tag_name.lower()
        self.class_name = class_name.lower()
        self.id_name = id_name.lower()
        self.matches: list[str] = []
        self._current_match: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        adict = {k.lower(): (v or "").lower() for k, v in attrs}
        is_match = False
        if not self.tag_name or tag.lower() == self.tag_name:
            if self.class_name:
                classes = adict.get("class", "").split()
                if self.class_name in classes:
                    is_match = True
            elif self.id_name:
                if adict.get("id", "") == self.id_name:
                    is_match = True
            else:
                is_match = True

        if is_match or self._depth > 0:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self._depth > 0:
            self._depth -= 1
            if self._depth == 0 and self._current_match:
                self.matches.append("".join(self._current_match).strip())
                self._current_match = []

    def handle_data(self, data: str) -> None:
        if self._depth > 0:
            txt = data.strip()
            if txt:
                self._current_match.append(txt + " ")


def build_scraper_tools(ws: Workspace | None = None) -> list[Tool]:
    """Собрать инструменты для скрапинга DOM и парсинга RSS/Atom лент."""

    def extract_by_selector(html_content: str, selector: str) -> str:
        if not html_content.strip() or not selector.strip():
            raise ToolError("HTML-код и селектор не могут быть пустыми")

        sel = selector.strip()
        tag = sel
        cls_name = ""
        id_name = ""
        if "." in sel:
            parts = sel.split(".", 1)
            tag = parts[0]
            cls_name = parts[1]
        elif "#" in sel:
            parts = sel.split("#", 1)
            tag = parts[0]
            id_name = parts[1]

        parser = _SelectorParser(tag_name=tag, class_name=cls_name, id_name=id_name)
        parser.feed(html_content)

        if not parser.matches:
            return f"(Элементы по селектору {selector!r} не найдены)"
        lines = [f"### Извлечённые элементы (`{selector}`):"]
        for idx, m in enumerate(parser.matches, 1):
            lines.append(f"{idx}. {m}")
        return "\n".join(lines)

    def parse_feed(feed_xml: str) -> str:
        if not feed_xml.strip():
            raise ToolError("XML-содержимое ленты не может быть пустым")

        try:
            root = ET.fromstring(feed_xml)
        except ET.ParseError as exc:
            raise ToolError(f"Ошибка разбора XML-ленты: {exc}") from exc

        items: list[str] = []
        # RSS 2.0 (channel/item)
        for item in root.findall(".//item"):
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            pub = item.findtext("pubDate", "")
            items.append(f"- **{title}** ({pub})\n  Ссылка: {link}")

        # Atom (entry)
        for entry in root.findall(".//{http://www.w3.org/2005/Atom}entry") or root.findall(".//entry"):
            title = entry.findtext("title", "") or entry.findtext("{http://www.w3.org/2005/Atom}title", "")
            link = ""
            link_el = entry.find("{http://www.w3.org/2005/Atom}link") or entry.find("link")
            if link_el is not None:
                link = link_el.get("href", "")
            pub = entry.findtext("updated", "") or entry.findtext("{http://www.w3.org/2005/Atom}updated", "")
            items.append(f"- **{title}** ({pub})\n  Ссылка: {link}")

        if not items:
            return "(В ленте не найдено записей RSS/Atom)"
        return "### Записи из ленты новостей:\n" + "\n".join(items)

    return [
        Tool(
            name="html.extract_by_selector",
            description="Извлечь текст элементов HTML по CSS-селектору (например, 'span.price', 'div#main', 'a').",
            parameters={
                "type": "object",
                "properties": {
                    "html_content": {
                        "type": "string",
                        "description": "HTML-код страницы",
                    },
                    "selector": {
                        "type": "string",
                        "description": "CSS селектор ('a', 'span.price', 'div#content')",
                    },
                },
                "required": ["html_content", "selector"],
            },
            fn=extract_by_selector,
            skills=["html", "scraper", "dom", "web", "extract"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "dom_element",
                "speed": "fast",
                "tags": ["html", "css", "selector", "dom", "extract", "scraper"],
            },
            example='html.extract_by_selector(html_content="<span class=\'price\'>100 руб</span>", selector="span.price")',
        ),
        Tool(
            name="scraper.parse_feed",
            description="Разобрать XML-ленту новостей RSS/Atom и извлечь заголовки и ссылки.",
            parameters={
                "type": "object",
                "properties": {
                    "feed_xml": {
                        "type": "string",
                        "description": "XML-код RSS/Atom ленты",
                    }
                },
                "required": ["feed_xml"],
            },
            fn=parse_feed,
            skills=["scraper", "rss", "atom", "xml", "web", "news"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "rss_feed",
                "speed": "fast",
                "tags": ["rss", "atom", "feed", "xml", "news", "scraper"],
            },
            example='scraper.parse_feed(feed_xml="<rss><channel><item><title>Новость</title></item></channel></rss>")',
        ),
    ]
