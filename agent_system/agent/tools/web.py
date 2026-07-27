"""Веб-поиск и загрузка страниц — БЕЗ MCP и без единой pip-зависимости.

Зачем отдельно от agent/mcp.py: MCP-серверы (agent/mcp.py, MCP.md) — это
внешний процесс/сервис (npx/uvx/docker) и его собственная настройка
(ключ Brave Search и т.п.). На многих серверах доступа к npm/pypi для
установки такого сервера попросту нет (как и в этой самой песочнице —
проверено: `npx`/`uvx` недоступны без интернета до реестров пакетов).
Этот навык даёт минимальный, но РАБОЧИЙ поиск и загрузку страниц прямо
из stdlib: `urllib.request` + `html.parser`, ноль внешних процессов и
ноль `pip install`. Кому не хватит — MCP по-прежнему доступен отдельным
навыком `mcp` и не конфликтует с этим (разные инструменты, оба можно
включить одновременно).

ГЛАВНАЯ ОПАСНОСТЬ, которую здесь отдельно проверяем: SSRF (Server-Side
Request Forgery). `web_fetch` скачивает URL, который МОДЕЛЬ получила
откуда угодно — из задачи пользователя, из текста веб-страницы, из
результата поиска. Если агент развёрнут на сервере с доступом к
внутренней сети (или в облаке — метаданные инстанса часто отдаются по
http://169.254.169.254/... без авторизации), а модель на это
«уговорили» промпт-инъекцией из чужой веб-страницы — это не гипотетика,
а реальный класс атак на LLM-агентов с доступом в сеть. Поэтому
`web_fetch` резолвит хост ЧЕРЕЗ DNS ПЕРЕД запросом (и повторно на КАЖДОМ
редиректе — иначе первую проверку легко обойти открытым редиректом на
внешнем, «безопасном» домене) и отклоняет приватные/loopback/
link-local/multicast/зарезервированные адреса, если явно не разрешено
(`web.allow_local` — только для интранета/тестов, не для агента с
доступом в интернет).

Поиск: DuckDuckGo без API-ключа (два бэкенда — `duckduckgo_lite`,
`duckduckgo_html`), либо самостоятельно поднятый SearXNG (JSON API,
`backend="searxng"`, `search_base_url=<свой инстанс>`) — для продакшна
свой SearXNG надёжнее и не зависит от того, не поменяет ли DuckDuckGo
вёрстку без предупреждения.

ЧЕСТНОЕ ОГРАНИЧЕНИЕ: разметка результатов DuckDuckGo НЕ документирована
официально и может измениться без предупреждения. Парсер терпим к
вариациям (совпадение по подстроке класса, а не по точной структуре
DOM — «result__a»/«result-link» и т.п.), но если DuckDuckGo сменит
вёрстку кардинально, придётся поправить `_DuckDuckGoResultParser` здесь
же. Тесты (tests/test_web.py) проверяют парсинг на ЛОКАЛЬНОМ фейковом
сервере с реалистичной, но полностью контролируемой разметкой — как и
остальная система, не полагаемся на доступность/стабильность внешнего
сервиса в тестах.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from .base import Tool, ToolError, Workspace

_DEFAULT_SEARCH_URLS = {
    "duckduckgo_lite": "https://lite.duckduckgo.com/lite/",
    "duckduckgo_html": "https://html.duckduckgo.com/html/",
}

#: socket.getaddrinfo() САМ ПО СЕБЕ не имеет тайм-аута — на зависшем или
#: намеренно медленном DNS-сервере (в т.ч. подконтрольном атакующему,
#: если домен указывает на его же NS) проверка SSRF повисла бы навсегда
#: и держала бы поток агента. DNS_RESOLVE_TIMEOUT ограничивает именно
#: этот шаг отдельно от cfg.timeout (тот — про сам HTTP-запрос).
DNS_RESOLVE_TIMEOUT = 5.0


@dataclass
class WebConfig:
    # duckduckgo_lite | duckduckgo_html | searxng
    backend: str = "duckduckgo_lite"
    # для searxng — ОБЯЗАТЕЛЕН (адрес своего инстанса); для duckduckgo_*
    # переопределяет умолчание из _DEFAULT_SEARCH_URLS, если задан
    search_base_url: str = ""
    timeout: int = 15
    rate_limit: float = 1.0            # минимум секунд между сетевыми вызовами
    max_results: int = 8
    max_fetch_bytes: int = 2_000_000   # ограничение скачивания ДО разбора
    max_fetch_chars: int = 8_000       # ограничение текста ПОСЛЕ разбора
    user_agent: str = "Mozilla/5.0 (compatible; agent-system/1.0)"
    # Разрешить доступ к приватным/loopback-адресам. Только для
    # интранет-сценариев (свой SearXNG на localhost) или тестов — с
    # доступом в интернет включать НЕЛЬЗЯ, см. пояснение о SSRF выше.
    allow_local: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "WebConfig":
        clean = {k: v for k, v in (data or {}).items() if not k.startswith("_")}
        return cls(**clean)


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _resolve_with_timeout(host: str, timeout: float) -> list:
    """socket.getaddrinfo() с тайм-аутом через отдельный поток.

    Сам getaddrinfo не принимает timeout — на зависшем DNS это значило
    бы, что проверка SSRF (и с ней весь шаг агента) висит бесконечно.
    Поток-резолвер остаётся daemon и после тайм-аута — не самый чистый
    способ "отменить" системный вызов, но getaddrinfo нельзя прервать
    иначе средствами stdlib, а утечка одного зависшего потока на редкий
    случай безопаснее, чем подвешенный агент.
    """
    result: dict[str, Any] = {}

    def worker() -> None:
        try:
            result["infos"] = socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            result["error"] = exc

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise ToolError(
            f"Разрешение хоста {host!r} не уложилось в {timeout:g} с — "
            "DNS не отвечает или отвечает слишком медленно"
        )
    if "error" in result:
        raise ToolError(f"Не удалось разрешить хост {host!r}: {result['error']}")
    return result.get("infos", [])


# ============================================================ SSRF-защита
def _check_url_safe(url: str, allow_local: bool) -> None:
    """Отклонить схему/хост, небезопасные для исходящего запроса модели.

    ВАЖНО: резолвит хост по-настоящему (socket.getaddrinfo), а не
    смотрит на текстовый вид хоста — иначе `evil.example.com`,
    указывающий по DNS на 127.0.0.1, прошёл бы проверку "на глаз".
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ToolError(
            f"Недопустимая схема {parsed.scheme!r} в {url!r} — разрешены "
            "только http/https"
        )
    host = parsed.hostname
    if not host:
        raise ToolError(f"Не удалось определить хост в URL {url!r}")
    if allow_local:
        return
    infos = _resolve_with_timeout(host, DNS_RESOLVE_TIMEOUT)
    for info in infos:
        raw_ip = info[4][0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            raise ToolError(
                f"Отказ: {host!r} резолвится в приватный/служебный адрес "
                f"{raw_ip} — доступ к внутренним сетям заблокирован "
                "(защита от SSRF). Если это осознанно нужный интранет-"
                "адрес, включите web.allow_local в конфиге."
            )


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Проверяет КАЖДЫЙ редирект той же проверкой, что и исходный URL —

    иначе достаточно один раз пройти проверку внешним доменом, а затем
    редиректнуть на внутренний адрес, и защита выше была бы бесполезна.
    """

    def __init__(self, allow_local: bool) -> None:
        self.allow_local = allow_local

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        _check_url_safe(newurl, self.allow_local)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# ============================================================ rate limit
class _RateLimiter:
    """Тот же приём, что в tools/messaging.py: не хотим зависимости между

    модулями ради трёх строк общего кода."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        delta = time.time() - self._last
        remaining = self.min_interval - delta
        if remaining > 0:
            time.sleep(remaining)
        self._last = time.time()


# ==================================================== парсинг HTML-текста
_SKIP_TAGS = {"script", "style", "noscript", "head", "svg", "iframe"}
_BLOCK_TAGS = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6",
              "tr", "section", "article"}


class _TextExtractor(HTMLParser):
    """Голый текст страницы: убирает теги/скрипты/стили, сохраняет разбивку

    по абзацам/строкам для читаемости — этого достаточно модели, чтобы
    понять содержание, не вытягивая её в HTML-суп."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []
        self._in_title = False
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            # title читаем ДАЖЕ внутри <head> (который в _SKIP_TAGS) —
            # это единственное, что нас интересует внутри head.
            self._title_parts.append(data)
            return
        if self._skip_depth:
            return
        self._parts.append(data)

    @property
    def title(self) -> str:
        return _normalize_ws("".join(self._title_parts))

    def get_text(self) -> str:
        raw = "".join(self._parts)
        lines = [_normalize_ws(ln) for ln in raw.splitlines()]
        return "\n".join(ln for ln in lines if ln)


# ================================================= парсинг результатов ПС
def _unwrap_ddg_redirect(href: str) -> str:
    """DuckDuckGo оборачивает внешние ссылки в свой редирект

    (`//duckduckgo.com/l/?uddg=<закодированный URL>&rut=...`) — модели
    нужна РЕАЛЬНАЯ ссылка (и ей же потом идти в web_fetch), а не адрес
    редиректора DuckDuckGo.
    """
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if "duckduckgo.com" in (parsed.netloc or "") and parsed.path.startswith("/l/"):
        qs = urllib.parse.parse_qs(parsed.query)
        target = qs.get("uddg", [""])[0]
        if target:
            return urllib.parse.unquote(target)
    return href


class _DuckDuckGoResultParser(HTMLParser):
    """Терпимый парсер результатов DuckDuckGo (lite/html) — совпадение по

    ПОДСТРОКЕ класса CSS, а не по точной структуре DOM, чтобы пережить
    мелкие правки вёрстки: `result__a`/`result__snippet` (html-версия) и
    `result-link`/`result-snippet` (lite-версия) — оба варианта одним
    парсером.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._title_tag: str | None = None
        self._title_parts: list[str] | None = None
        self._title_href = ""
        self._snippet_tag: str | None = None
        self._snippet_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        adict = dict(attrs)
        cls = adict.get("class") or ""
        if tag == "a" and ("result__a" in cls or "result-link" in cls):
            self._title_tag = tag
            self._title_parts = []
            self._title_href = adict.get("href", "")
        elif "result__snippet" in cls or "result-snippet" in cls:
            self._snippet_tag = tag
            self._snippet_parts = []

    def handle_data(self, data: str) -> None:
        if self._title_parts is not None:
            self._title_parts.append(data)
        if self._snippet_parts is not None:
            self._snippet_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._title_parts is not None and tag == self._title_tag:
            title = _normalize_ws("".join(self._title_parts))
            url = _unwrap_ddg_redirect(self._title_href)
            if title and url:
                self.results.append({"title": title, "url": url, "snippet": ""})
            self._title_tag = None
            self._title_parts = None
            self._title_href = ""
        if self._snippet_parts is not None and tag == self._snippet_tag:
            snippet = _normalize_ws("".join(self._snippet_parts))
            if self.results:
                self.results[-1]["snippet"] = snippet
            self._snippet_tag = None
            self._snippet_parts = None


def _resolve_search_url(cfg: WebConfig) -> str:
    if cfg.search_base_url:
        return cfg.search_base_url
    if cfg.backend == "searxng":
        raise ToolError(
            "backend='searxng' требует web.search_base_url — адрес "
            "вашего инстанса SearXNG (например http://127.0.0.1:8888)"
        )
    url = _DEFAULT_SEARCH_URLS.get(cfg.backend)
    if not url:
        raise ToolError(
            f"Неизвестный backend поиска {cfg.backend!r}. Доступны: "
            f"{', '.join(_DEFAULT_SEARCH_URLS)}, searxng"
        )
    return url


def _search_duckduckgo(base_url: str, query: str, cfg: WebConfig
                       ) -> list[dict[str, str]]:
    url = base_url.rstrip("/") + "/?" + urllib.parse.urlencode({"q": query})
    req = urllib.request.Request(
        url, headers={"User-Agent": cfg.user_agent,
                      "Accept": "text/html,application/xhtml+xml"})
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
            raw = resp.read(cfg.max_fetch_bytes)
            charset = resp.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as exc:
        raise ToolError(f"Поиск вернул HTTP {exc.code} ({base_url})") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ToolError(f"Не удалось обратиться к поиску {base_url}: {exc}") from exc

    parser = _DuckDuckGoResultParser()
    parser.feed(raw.decode(charset, errors="replace"))
    return parser.results


def _search_searxng(base_url: str, query: str, cfg: WebConfig
                    ) -> list[dict[str, str]]:
    import json as _json
    url = base_url.rstrip("/") + "/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json"})
    req = urllib.request.Request(
        url, headers={"User-Agent": cfg.user_agent, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
            raw = resp.read(cfg.max_fetch_bytes)
    except urllib.error.HTTPError as exc:
        raise ToolError(f"SearXNG вернул HTTP {exc.code} ({base_url})") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ToolError(f"Не удалось обратиться к SearXNG {base_url}: {exc}") from exc
    try:
        data = _json.loads(raw.decode("utf-8", errors="replace"))
    except _json.JSONDecodeError as exc:
        raise ToolError(
            f"SearXNG {base_url} вернул не JSON — убедитесь, что в "
            "settings.yml инстанса разрешён format: json"
        ) from exc
    out = []
    for item in (data.get("results") or []):
        out.append({"title": item.get("title", "") or "",
                   "url": item.get("url", "") or "",
                   "snippet": item.get("content", "") or ""})
    return out


# ==================================================================== build
def build(ws: Workspace, cfg: WebConfig) -> list[Tool]:
    limiter = _RateLimiter(cfg.rate_limit)

    def web_search(query: str, max_results: int = 0) -> str:
        query = query.strip()
        if not query:
            raise ToolError("Пустой поисковый запрос")
        n = max_results if max_results and max_results > 0 else cfg.max_results
        n = max(1, min(n, 20))

        base_url = _resolve_search_url(cfg)
        limiter.wait()
        if cfg.backend == "searxng":
            results = _search_searxng(base_url, query, cfg)
        else:
            results = _search_duckduckgo(base_url, query, cfg)

        if not results:
            return (f"По запросу {query!r} ничего не найдено — либо "
                    "результатов действительно нет, либо сервис поиска "
                    "изменил формат ответа (проверьте вручную).")

        lines = [f"Результаты поиска по {query!r}:"]
        for i, r in enumerate(results[:n], 1):
            lines.append(f"{i}. {r['title']}\n   {r['url']}"
                        + (f"\n   {r['snippet']}" if r["snippet"] else ""))
        return "\n".join(lines)

    def web_fetch(url: str, max_chars: int = 0) -> str:
        url = url.strip()
        if not url:
            raise ToolError("Пустой URL")
        limiter.wait()
        _check_url_safe(url, cfg.allow_local)

        opener = urllib.request.build_opener(_SafeRedirectHandler(cfg.allow_local))
        req = urllib.request.Request(
            url, headers={"User-Agent": cfg.user_agent,
                         "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1"})
        try:
            with opener.open(req, timeout=cfg.timeout) as resp:
                ctype = (resp.headers.get_content_type() or "").lower()
                if ctype not in ("text/html", "text/plain",
                                 "application/xhtml+xml", ""):
                    raise ToolError(
                        f"Неподдерживаемый тип содержимого {ctype!r} по "
                        f"{url} — web_fetch читает только текст/HTML"
                    )
                raw = resp.read(cfg.max_fetch_bytes + 1)
                final_url = resp.geturl()
                charset = resp.headers.get_content_charset() or "utf-8"
        except ToolError:
            raise
        except urllib.error.HTTPError as exc:
            raise ToolError(f"HTTP {exc.code} при загрузке {url}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ToolError(f"Не удалось загрузить {url}: {exc}") from exc

        truncated_download = len(raw) > cfg.max_fetch_bytes
        if truncated_download:
            raw = raw[:cfg.max_fetch_bytes]
        try:
            body = raw.decode(charset, errors="replace")
        except LookupError:
            body = raw.decode("utf-8", errors="replace")

        if ctype == "text/plain":
            text, title = body, ""
        else:
            extractor = _TextExtractor()
            extractor.feed(body)
            text, title = extractor.get_text(), extractor.title

        lim = max_chars if max_chars and max_chars > 0 else cfg.max_fetch_chars
        body_truncated = len(text) > lim
        if body_truncated:
            text = text[:lim]

        header = f"URL: {final_url}" + (f"\nЗаголовок: {title}" if title else "")
        note = ""
        if truncated_download:
            note += f"\n[скачивание обрезано на {cfg.max_fetch_bytes} байт]"
        if body_truncated:
            note += f"\n[текст обрезан на {lim} символах]"
        return f"{header}{note}\n\n{text.strip() or '(текст не найден на странице)'}"

    return [
        Tool("web_search",
             "Найти страницы в интернете по запросу (DuckDuckGo или "
             "самостоятельный SearXNG — без API-ключа). Возвращает "
             "заголовок/URL/сниппет каждого результата. Дальше — "
             "web_fetch по интересующей ссылке за полным текстом.",
             {"type": "object",
              "properties": {
                  "query": {"type": "string"},
                  "max_results": {"type": "integer",
                                  "description": "0 = взять значение из конфига"}},
              "required": ["query"]},
             web_search),
        Tool("web_fetch",
             "Скачать страницу по URL и вернуть её текст без HTML-тегов "
             "(скрипты/стили вырезаны). Только http/https, только "
             "текст/HTML — доступ к внутренним/служебным адресам "
             "заблокирован (защита от SSRF).",
             {"type": "object",
              "properties": {
                  "url": {"type": "string"},
                  "max_chars": {"type": "integer",
                               "description": "0 = взять значение из конфига"}},
              "required": ["url"]},
             web_fetch),
    ]
