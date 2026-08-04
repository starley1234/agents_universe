"""Ингестор URL: скачивание и извлечение текста из веб-страниц."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .base import IngestResult, Ingestor, PageChunk, SourceType


class URLIngestor(Ingestor):
    """Извлекает чистый текст из веб-страниц.

    Каскад:
    1. requests + BeautifulSoup (быстрый, для статичных страниц)
    2. Playwright (для JS-рендеринга, если включён)
    """

    def can_handle(self, source: str | Path) -> bool:
        if isinstance(source, Path):
            return False
        parsed = urlparse(source)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)

    def ingest(self, source: str | Path, render_js: bool = False, **kwargs) -> IngestResult:
        url = str(source)
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]

        if render_js:
            html, metadata = self._fetch_playwright(url)
        else:
            html, metadata = self._fetch_requests(url)

        text, structure = self._extract_text(html)

        chunks: list[PageChunk] = []
        if text.strip():
            # Разбиваем на логические блоки (по заголовкам)
            sections = self._split_by_sections(text)
            for i, section in enumerate(sections):
                chunks.append(PageChunk(
                    text=section["text"],
                    page_number=i + 1,
                    metadata={"heading": section.get("heading", ""), "level": section.get("level", 0)},
                ))

        metadata["url"] = url
        metadata["html_length"] = len(html)
        metadata["extracted_text_length"] = len(text)

        return IngestResult(
            source_path=url,
            source_type=SourceType.URL,
            chunks=chunks,
            file_hash=url_hash,
            total_pages=len(chunks),
            metadata=metadata,
        )

    def _fetch_requests(self, url: str) -> tuple[str, dict[str, Any]]:
        """Быстрый парсинг через requests + BS4."""
        try:
            import requests
            from bs4 import BeautifulSoup
        except ImportError:
            return self._fetch_urllib(url)

        headers = {
            "User-Agent": "SPECTRUM/0.1 (Document Intelligence Agent)",
            "Accept": "text/html,application/xhtml+xml",
        }

        resp = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
        resp.raise_for_status()
        html = resp.text

        metadata: dict[str, Any] = {
            "status_code": resp.status_code,
            "content_type": resp.headers.get("Content-Type", ""),
            "final_url": resp.url,
        }

        return html, metadata

    def _fetch_urllib(self, url: str) -> tuple[str, dict[str, Any]]:
        """Фолбэк на urllib без внешних зависимостей."""
        import urllib.request

        req = urllib.request.Request(url, headers={
            "User-Agent": "SPECTRUM/0.1",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        return html, {"method": "urllib"}

    def _fetch_playwright(self, url: str) -> tuple[str, dict[str, Any]]:
        """JS-рендеринг через Playwright."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            # Фолбэк на requests
            return self._fetch_requests(url)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=30000)
            html = page.content()
            browser.close()

        return html, {"method": "playwright", "js_rendered": True}

    def _extract_text(self, html: str) -> tuple[str, dict[str, Any]]:
        """Извлекает чистый текст из HTML, убирая рекламу, навигацию, футеры."""
        try:
            from bs4 import BeautifulSoup
            return self._extract_bs4(html)
        except ImportError:
            return self._extract_regex(html)

    def _extract_bs4(self, html: str) -> tuple[str, dict[str, Any]]:
        """Парсинг через BeautifulSoup с очисткой мусора."""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")

        # Удаляем нежелательные теги
        for tag in soup.find_all(["script", "style", "nav", "footer", "header",
                                   "aside", "iframe", "noscript", "svg"]):
            tag.decompose()

        # Удаляем типичные рекламные блоки
        for tag in soup.find_all(attrs={"class": re.compile(
                r"(ad|banner|sidebar|footer|header|nav|menu|cookie|popup)", re.I)}):
            tag.decompose()

        # Извлекаем заголовки и текст
        title = soup.title.string if soup.title else ""
        body_text = soup.get_text(separator="\n", strip=True)

        metadata = {"title": title.strip() if title else ""}
        return body_text, metadata

    def _extract_regex(self, html: str) -> tuple[str, dict[str, Any]]:
        """Грубый regex-парсинг без BS4."""
        # Удаляем теги script/style
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S | re.I)
        # Удаляем все теги
        text = re.sub(r"<[^>]+>", " ", text)
        # Декодируем HTML entities
        text = text.replace("&nbsp;", " ").replace("&amp;", "&")
        text = text.replace("&lt;", "<").replace("&gt;", ">")
        text = re.sub(r"\s+", " ", text).strip()

        title_match = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
        metadata = {"title": title_match.group(1).strip() if title_match else ""}
        return text, metadata

    def _split_by_sections(self, text: str) -> list[dict[str, Any]]:
        """Разбивает текст на секции по заголовкам."""
        lines = text.split("\n")
        sections: list[dict[str, Any]] = []
        current_text: list[str] = []
        current_heading = ""
        current_level = 0

        heading_pattern = re.compile(r"^(#{1,6})\s+(.+)$")

        for line in lines:
            match = heading_pattern.match(line.strip())
            if match:
                # Сохраняем предыдущую секцию
                if current_text:
                    sections.append({
                        "heading": current_heading,
                        "level": current_level,
                        "text": "\n".join(current_text).strip(),
                    })
                current_heading = match.group(2).strip()
                current_level = len(match.group(1))
                current_text = []
            else:
                current_text.append(line)

        # Последняя секция
        if current_text:
            sections.append({
                "heading": current_heading,
                "level": current_level,
                "text": "\n".join(current_text).strip(),
            })

        # Если секций нет, возвращаем весь текст как одну секцию
        if not sections:
            sections = [{"heading": "", "level": 0, "text": text}]

        return [s for s in sections if s["text"].strip()]
