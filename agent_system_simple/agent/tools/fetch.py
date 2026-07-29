"""Загрузка веб-страниц: HTTP → текст.

Это встроенный fetch на стандартной библиотеке — работает, когда
MCP-сервер загрузки не настроен. Если у вас есть свой MCP fetch, он
даст лучший разбор; тогда этот навык можно не подключать.

Безопасность: агент может попросить любой URL, включая внутренние
адреса сети. Поэтому по умолчанию блокируются localhost и приватные
диапазоны — иначе агент из песочницы дотянется до вашего роутера или
облачных метаданных (169.254.169.254). Снимается флагом allow_private.
"""
from __future__ import annotations

import gzip
import html
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Any

from .base import Tool, ToolError, Workspace

UA = "Mozilla/5.0 (compatible; agent-system/1.0)"
MAX_BYTES = 5_000_000
MAX_TEXT = 100_000

DROP_TAGS = re.compile(
    r"<(script|style|noscript|svg|iframe|nav|footer|header|form)\b.*?</\1>",
    re.S | re.I)
BLOCK_TAGS = re.compile(r"</?(p|div|br|li|tr|h[1-6]|section|article)\b[^>]*>",
                        re.I)
ANY_TAG = re.compile(r"<[^>]+>")


def _is_private(host: str) -> bool:
    """Внутренний ли адрес. Резолвим имя: 'my.host' может указывать на 127.0.0.1."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False              # не резолвится — пусть упадёт на запросе
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast):
            return True
    return False


def html_to_text(raw: str) -> tuple[str, str]:
    """HTML → (текст, заголовок). Грубо, но предсказуемо."""
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    if m:
        title = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
    body = DROP_TAGS.sub(" ", raw)
    body = BLOCK_TAGS.sub("\n", body)
    body = ANY_TAG.sub(" ", body)
    body = html.unescape(body)
    body = re.sub(r"[ \t\xa0]+", " ", body)
    body = re.sub(r"\n\s*\n\s*\n+", "\n\n", body)
    return body.strip(), title


def build(ws: Workspace, timeout: int = 30,
          allow_private: bool = False) -> list[Tool]:

    def _get(url: str) -> tuple[bytes, str]:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ToolError(f"поддерживаются только http/https, получено "
                            f"{parsed.scheme!r}")
        if not parsed.hostname:
            raise ToolError(f"в URL нет хоста: {url!r}")
        if not allow_private and _is_private(parsed.hostname):
            raise ToolError(
                f"адрес {parsed.hostname} внутренний (localhost или частная "
                "сеть). Загрузка заблокирована: агент не должен ходить во "
                "внутреннюю сеть. Разрешить: allow_private в конфиге.")
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,text/plain,*/*",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "ru,en;q=0.8",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read(MAX_BYTES + 1)
                enc = (resp.headers.get("Content-Encoding") or "").lower()
                ctype = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            raise ToolError(f"HTTP {exc.code} {exc.reason} для {url}") from exc
        except urllib.error.URLError as exc:
            raise ToolError(f"не удалось загрузить {url}: {exc.reason}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ToolError(f"тайм-аут {timeout} с при загрузке {url}") from exc

        if len(data) > MAX_BYTES:
            raise ToolError(f"страница больше {MAX_BYTES // 1_000_000} МБ")
        if enc == "gzip":
            try:
                data = gzip.decompress(data)
            except OSError:
                pass
        elif enc == "deflate":
            try:
                data = zlib.decompress(data)
            except zlib.error:
                pass
        return data, ctype

    def fetch_url(url: str, as_text: bool = True, save_to: str = "") -> str:
        data, ctype = _get(url)
        if save_to:
            p = ws.resolve(save_to)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            return (f"Сохранено: {ws.relative(p)} ({len(data)} Б, "
                    f"{ctype or 'тип неизвестен'})")

        charset = "utf-8"
        m = re.search(r"charset=([\w\-]+)", ctype, re.I)
        if m:
            charset = m.group(1)
        raw = data.decode(charset, "replace")

        if not as_text or "html" not in ctype.lower():
            return raw[:MAX_TEXT]
        text, title = html_to_text(raw)
        head = f"# {title}\n\n" if title else ""
        head += f"Источник: {url}\n\n"
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT] + f"\n\n… обрезано, всего {len(text)} символов"
        return head + (text or "(на странице нет текста)")

    def fetch_links(url: str, limit: int = 50) -> str:
        data, ctype = _get(url)
        raw = data.decode("utf-8", "replace")
        base = url
        out, seen = [], set()
        for m in re.finditer(r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                             raw, re.S | re.I):
            href = urllib.parse.urljoin(base, m.group(1).strip())
            if not href.startswith(("http://", "https://")) or href in seen:
                continue
            seen.add(href)
            text = html.unescape(ANY_TAG.sub("", m.group(2))).strip()
            out.append(f"- {text[:70] or '(без текста)'} → {href}")
            if len(out) >= limit:
                break
        return "\n".join(out) if out else "ссылок не найдено"

    return [
        Tool("fetch_url",
             "Загрузить веб-страницу и получить её текст. Можно сохранить "
             "как файл через save_to (тогда потом читается через doc_read).",
             {"type": "object",
              "properties": {
                  "url": {"type": "string"},
                  "as_text": {"type": "boolean",
                              "description": "true = вычистить HTML"},
                  "save_to": {"type": "string",
                              "description": "путь для сохранения как есть"}},
              "required": ["url"]},
             fetch_url),
        Tool("fetch_links",
             "Собрать ссылки со страницы: текст и адрес.",
             {"type": "object",
              "properties": {"url": {"type": "string"},
                             "limit": {"type": "integer"}},
              "required": ["url"]},
             fetch_links),
    ]
