"""Тесты навыка web (веб-поиск и загрузка страниц без MCP).

Философия та же, что у test_messaging.py/test_mcp.py: реальные сокеты, а
не заглушки внутри Python. DuckDuckGo/SearXNG эмулируются локальным
http.server.ThreadingHTTPServer с РЕАЛИСТИЧНОЙ, но полностью
контролируемой разметкой (см. docstring agent/tools/web.py — реальная
вёрстка DuckDuckGo не документирована и не тестируется напрямую по тем
же причинам, по которым остальная система не полагается на внешние
сервисы в тестах).

Отдельный и самый важный блок — SSRF-защита (test_ssrf_*): без неё
web_fetch — открытая дверь во внутреннюю сеть сервера, на котором
развёрнут агент, если модель "уговорили" промпт-инъекцией из чужой
страницы.
"""
from __future__ import annotations

import socket
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.tools.base import ToolError, Workspace              # noqa: E402
from agent.tools import web as web_mod                          # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}\n" + "─" * len(title))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ============================================================ фейковые серверы
DDG_LITE_HTML = """
<html><body>
<table>
<tr><td>
<a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.python.org%2F&amp;rut=abc" class="result-link">Welcome to Python.org</a>
</td></tr>
<tr><td class="result-snippet">The official home of the Python Programming Language</td></tr>
<tr><td>
<a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FPython&amp;rut=def" class="result-link">Python (programming language) - Wikipedia</a>
</td></tr>
<tr><td class="result-snippet">Python is a high-level programming language.</td></tr>
</table>
</body></html>
"""

DDG_HTML_VARIANT = """
<html><body>
<div class="results">
<div class="result">
<a class="result__a" href="https://real.example.com/direct-link">Прямая ссылка без редиректора</a>
<a class="result__snippet" href="#">Сниппет прямой ссылки — редиректор не всегда используется</a>
</div>
</div>
</body></html>
"""

EMPTY_RESULTS_HTML = "<html><body><p>No results found.</p></body></html>"

SEARXNG_JSON = (
    '{"results": [{"title": "SearXNG Result 1", "url": "https://example.org/1",'
    ' "content": "первый результат searxng"},'
    '{"title": "SearXNG Result 2", "url": "https://example.org/2",'
    ' "content": "второй результат searxng"}]}'
)

PAGE_HTML = """
<html><head><title>Тестовая страница</title>
<script>alert('injected')</script>
<style>body{color:red}</style>
</head><body>
<h1>Заголовок статьи</h1>
<p>Первый содержательный абзац текста страницы.</p>
<p>Второй абзац с <b>жирным</b> и <i>курсивным</i> текстом внутри.</p>
</body></html>
"""


class _StaticHandler(BaseHTTPRequestHandler):
    """Отдаёт HANDLER_BODY/HANDLER_CTYPE, заданные классом-наследником."""
    body = b""
    ctype = "text/html; charset=utf-8"

    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        body = type(self).body
        self.send_response(200)
        self.send_header("Content-Type", type(self).ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start(handler_cls) -> tuple[ThreadingHTTPServer, int]:
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def _tools(ws: Workspace, cfg: web_mod.WebConfig):
    return {t.name: t for t in web_mod.build(ws, cfg)}


# ================================================================= search
def test_search_duckduckgo_lite_parses_real_markup() -> None:
    section("web_search: разбор реалистичной разметки DuckDuckGo lite")

    class H(_StaticHandler):
        body = DDG_LITE_HTML.encode()

    srv, port = _start(H)
    try:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(td)
            cfg = web_mod.WebConfig(
                backend="duckduckgo_lite",
                search_base_url=f"http://127.0.0.1:{port}", rate_limit=0)
            out = _tools(ws, cfg)["web_search"].fn(query="python")
            check("заголовок первого результата виден", "Welcome to Python.org" in out, out)
            check("редирект DuckDuckGo развёрнут в реальный URL",
                  "https://www.python.org/" in out and "duckduckgo.com/l/" not in out,
                  out)
            check("сниппет виден", "official home" in out, out)
            check("второй результат тоже разобран", "Wikipedia" in out, out)
    finally:
        srv.shutdown()


def test_search_duckduckgo_html_direct_links() -> None:
    section("web_search: вариант вёрстки с result__a и прямыми ссылками")

    class H(_StaticHandler):
        body = DDG_HTML_VARIANT.encode()

    srv, port = _start(H)
    try:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(td)
            cfg = web_mod.WebConfig(
                backend="duckduckgo_html",
                search_base_url=f"http://127.0.0.1:{port}", rate_limit=0)
            out = _tools(ws, cfg)["web_search"].fn(query="test")
            check("прямая ссылка (без редиректора) тоже разбирается",
                  "https://real.example.com/direct-link" in out, out)
    finally:
        srv.shutdown()


def test_search_no_results_is_honest() -> None:
    section("web_search: пустая выдача — честное сообщение, не пустая строка")

    class H(_StaticHandler):
        body = EMPTY_RESULTS_HTML.encode()

    srv, port = _start(H)
    try:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(td)
            cfg = web_mod.WebConfig(
                backend="duckduckgo_lite",
                search_base_url=f"http://127.0.0.1:{port}", rate_limit=0)
            out = _tools(ws, cfg)["web_search"].fn(query="ничего-не-найдётся")
            check("сообщение о пустой выдаче", "ничего не найдено" in out, out)
    finally:
        srv.shutdown()


def test_search_max_results_limits_output() -> None:
    section("web_search: max_results ограничивает число результатов")

    class H(_StaticHandler):
        body = DDG_LITE_HTML.encode()

    srv, port = _start(H)
    try:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(td)
            cfg = web_mod.WebConfig(
                backend="duckduckgo_lite",
                search_base_url=f"http://127.0.0.1:{port}", rate_limit=0)
            out = _tools(ws, cfg)["web_search"].fn(query="python", max_results=1)
            check("только первый результат показан", "Wikipedia" not in out, out)
            check("первый результат всё же есть", "Python.org" in out, out)
    finally:
        srv.shutdown()


def test_search_searxng_backend() -> None:
    section("web_search: бэкенд SearXNG (JSON API)")

    class H(_StaticHandler):
        body = SEARXNG_JSON.encode()
        ctype = "application/json"

    srv, port = _start(H)
    try:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(td)
            cfg = web_mod.WebConfig(
                backend="searxng",
                search_base_url=f"http://127.0.0.1:{port}", rate_limit=0)
            out = _tools(ws, cfg)["web_search"].fn(query="test")
            check("оба результата SearXNG разобраны",
                  "SearXNG Result 1" in out and "SearXNG Result 2" in out, out)
            check("сниппет из content виден", "первый результат searxng" in out, out)
    finally:
        srv.shutdown()


def test_searxng_requires_base_url() -> None:
    section("web_search: backend=searxng без search_base_url -> понятная ошибка")
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(td)
        cfg = web_mod.WebConfig(backend="searxng", rate_limit=0)
        try:
            _tools(ws, cfg)["web_search"].fn(query="x")
            check("отказ без search_base_url", False)
        except ToolError:
            check("отказ без search_base_url", True)


def test_unknown_backend_rejected() -> None:
    section("web_search: неизвестный backend отклонён")
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(td)
        cfg = web_mod.WebConfig(backend="google_secret_api", rate_limit=0)
        try:
            _tools(ws, cfg)["web_search"].fn(query="x")
            check("отказ на неизвестный backend", False)
        except ToolError:
            check("отказ на неизвестный backend", True)


def test_search_empty_query_rejected() -> None:
    section("web_search: пустой запрос отклонён")
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(td)
        cfg = web_mod.WebConfig(rate_limit=0)
        try:
            _tools(ws, cfg)["web_search"].fn(query="   ")
            check("отказ на пустой запрос", False)
        except ToolError:
            check("отказ на пустой запрос", True)


def test_search_http_error_becomes_tool_error() -> None:
    section("web_search: HTTP-ошибка сервера поиска не роняет агента")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            self.send_response(503)
            self.end_headers()

    srv, port = _start(H)
    try:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(td)
            cfg = web_mod.WebConfig(
                backend="duckduckgo_lite",
                search_base_url=f"http://127.0.0.1:{port}", rate_limit=0)
            try:
                _tools(ws, cfg)["web_search"].fn(query="x")
                check("HTTP 503 транслируется в ToolError", False)
            except ToolError:
                check("HTTP 503 транслируется в ToolError", True)
    finally:
        srv.shutdown()


# ================================================================== fetch
def test_fetch_extracts_text_strips_scripts() -> None:
    section("web_fetch: текст без тегов, скрипты/стили вырезаны")

    class H(_StaticHandler):
        body = PAGE_HTML.encode()

    srv, port = _start(H)
    try:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(td)
            cfg = web_mod.WebConfig(allow_local=True, rate_limit=0)
            out = _tools(ws, cfg)["web_fetch"].fn(url=f"http://127.0.0.1:{port}/")
            check("заголовок страницы виден", "Тестовая страница" in out, out)
            check("текст абзацев виден", "Первый содержательный абзац" in out, out)
            check("скрипт вырезан", "injected" not in out, out)
            check("CSS вырезан", "color:red" not in out, out)
            check("HTML-теги не просочились", "<p>" not in out and "<h1>" not in out,
                  out)
    finally:
        srv.shutdown()


def test_fetch_max_chars_truncates() -> None:
    section("web_fetch: max_chars обрезает длинный текст")

    class H(_StaticHandler):
        body = ("<html><body>" + "слово " * 5000 + "</body></html>").encode()

    srv, port = _start(H)
    try:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(td)
            cfg = web_mod.WebConfig(allow_local=True, rate_limit=0)
            out = _tools(ws, cfg)["web_fetch"].fn(
                url=f"http://127.0.0.1:{port}/", max_chars=100)
            check("текст обрезан", "обрезан на 100" in out, out)
    finally:
        srv.shutdown()


def test_fetch_rejects_non_text_content_type() -> None:
    section("web_fetch: неподдерживаемый Content-Type отклонён")

    class H(_StaticHandler):
        body = b"\x89PNG\r\n\x1a\nfakeimagedata"
        ctype = "image/png"

    srv, port = _start(H)
    try:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(td)
            cfg = web_mod.WebConfig(allow_local=True, rate_limit=0)
            try:
                _tools(ws, cfg)["web_fetch"].fn(url=f"http://127.0.0.1:{port}/")
                check("отказ на image/png", False)
            except ToolError:
                check("отказ на image/png", True)
    finally:
        srv.shutdown()


def test_fetch_http_error_becomes_tool_error() -> None:
    section("web_fetch: HTTP 404 не роняет агента")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            self.send_response(404)
            self.end_headers()

    srv, port = _start(H)
    try:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(td)
            cfg = web_mod.WebConfig(allow_local=True, rate_limit=0)
            try:
                _tools(ws, cfg)["web_fetch"].fn(url=f"http://127.0.0.1:{port}/missing")
                check("HTTP 404 транслируется в ToolError", False)
            except ToolError:
                check("HTTP 404 транслируется в ToolError", True)
    finally:
        srv.shutdown()


def test_fetch_empty_url_rejected() -> None:
    section("web_fetch: пустой URL отклонён")
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(td)
        cfg = web_mod.WebConfig(rate_limit=0)
        try:
            _tools(ws, cfg)["web_fetch"].fn(url="  ")
            check("отказ на пустой URL", False)
        except ToolError:
            check("отказ на пустой URL", True)


# ==================================================================== SSRF
def test_ssrf_blocks_private_and_special_addresses() -> None:
    section("SSRF-защита: приватные/loopback/link-local адреса отклонены")
    for bad in ("http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/",
               "http://10.0.0.5/", "http://192.168.1.1/", "http://[::1]/",
               "http://172.16.0.1/"):
        try:
            web_mod._check_url_safe(bad, allow_local=False)
            check(f"{bad} отклонён", False)
        except ToolError:
            check(f"{bad} отклонён", True)


def test_ssrf_allows_public_and_allow_local_override() -> None:
    section("SSRF-защита: публичный адрес проходит, allow_local снимает проверку")
    try:
        web_mod._check_url_safe("http://1.1.1.1/", allow_local=False)
        check("публичный IP (1.1.1.1) проходит", True)
    except ToolError as exc:
        check("публичный IP (1.1.1.1) проходит", False, str(exc))

    try:
        web_mod._check_url_safe("http://127.0.0.1/", allow_local=True)
        check("allow_local=True снимает проверку для 127.0.0.1", True)
    except ToolError as exc:
        check("allow_local=True снимает проверку для 127.0.0.1", False, str(exc))


def test_ssrf_rejects_bad_scheme() -> None:
    section("SSRF-защита: недопустимая схема (file://, ftp://) отклонена")
    for scheme_url in ("ftp://example.com/", "file:///etc/passwd",
                       "gopher://example.com/"):
        try:
            web_mod._check_url_safe(scheme_url, allow_local=False)
            check(f"{scheme_url} отклонён", False)
        except ToolError:
            check(f"{scheme_url} отклонён", True)


def test_ssrf_web_fetch_end_to_end() -> None:
    section("web_fetch отклоняет приватный адрес до реального сетевого запроса")
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(td)
        cfg = web_mod.WebConfig(allow_local=False, rate_limit=0)
        try:
            _tools(ws, cfg)["web_fetch"].fn(url="http://192.168.100.1/secret")
            check("web_fetch отклоняет приватный адрес", False)
        except ToolError:
            check("web_fetch отклоняет приватный адрес", True)


def test_ssrf_redirect_to_private_is_blocked() -> None:
    section("SSRF-защита: редирект НА приватный адрес блокируется, "
           "не только исходный URL")
    # Проверяем сам HTTPRedirectHandler напрямую (без реального сетевого
    # запроса ко второму хосту) — redirect_request обязан бросить ДО
    # попытки перехода по новому URL.
    handler = web_mod._SafeRedirectHandler(allow_local=False)
    try:
        handler.redirect_request(None, None, 302, "Found", {},
                                 "http://192.168.50.50/internal-secret")
        check("редирект на приватный адрес заблокирован", False)
    except ToolError:
        check("редирект на приватный адрес заблокирован", True)


def test_ssrf_dns_resolution_failure_is_tool_error() -> None:
    section("SSRF-защита: неразрешимый хост даёт понятную ошибку, не трейсбек")
    try:
        web_mod._check_url_safe(
            "http://this-domain-does-not-exist-agent-test.invalid/", allow_local=False)
        check("неразрешимый хост отклонён", False)
    except ToolError:
        check("неразрешимый хост отклонён", True)


def test_dns_resolve_timeout_does_not_hang_forever() -> None:
    section("SSRF-защита: зависший DNS-резолвер не подвешивает агента навсегда")
    import socket
    import time

    orig_getaddrinfo = socket.getaddrinfo

    def hanging_getaddrinfo(*a, **kw):
        time.sleep(10)          # заведомо дольше тайм-аута ниже
        return orig_getaddrinfo(*a, **kw)

    socket.getaddrinfo = hanging_getaddrinfo
    try:
        t0 = time.time()
        try:
            web_mod._resolve_with_timeout("example.com", timeout=1.0)
            check("зависший DNS даёт тайм-аут, а не висит", False)
        except ToolError as exc:
            dt = time.time() - t0
            check("тайм-аут сработал в разумное время (не 10 с)", dt < 3.0,
                  f"{dt:.2f}s")
            check("сообщение об ошибке понятное", "не уложилось" in str(exc), str(exc))
    finally:
        socket.getaddrinfo = orig_getaddrinfo

    # позитивный контроль: реальный (не подменённый) резолвинг работает
    infos = web_mod._resolve_with_timeout("127.0.0.1", timeout=web_mod.DNS_RESOLVE_TIMEOUT)
    check("нормальный резолвинг не сломан подменой", len(infos) > 0, str(infos))


# ============================================================== rate limit
def test_rate_limit_enforced_between_calls() -> None:
    section("web: лимит частоты выдерживается между вызовами")
    import time

    class H(_StaticHandler):
        body = DDG_LITE_HTML.encode()

    srv, port = _start(H)
    try:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(td)
            cfg = web_mod.WebConfig(
                backend="duckduckgo_lite",
                search_base_url=f"http://127.0.0.1:{port}", rate_limit=0.5)
            tool = _tools(ws, cfg)["web_search"]
            t0 = time.time()
            tool.fn(query="a")
            tool.fn(query="b")
            dt = time.time() - t0
            check("между двумя вызовами выдержана пауза", dt >= 0.4, f"{dt:.2f}s")
    finally:
        srv.shutdown()


# ============================================================= config/build
def test_web_config_from_dict_ignores_comments() -> None:
    section("WebConfig.from_dict: игнорирует ключи-комментарии")
    cfg = web_mod.WebConfig.from_dict({
        "_комментарий": "текст", "backend": "searxng",
        "search_base_url": "http://localhost:8888"})
    check("backend разобран", cfg.backend == "searxng")
    check("search_base_url разобран", cfg.search_base_url == "http://localhost:8888")


def test_build_agent_with_web_skill() -> None:
    section("Сборка агента с навыком web")
    from agent.build import build_agent, known_skills
    from agent.config import Config

    check("web входит в known_skills", "web" in known_skills())
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(provider="ollama", model="m", workspace=td,
                    skills=["files", "web"])
        agent = build_agent(cfg)
        names = agent.tools.names()
        check("web_search зарегистрирован", "web_search" in names)
        check("web_fetch зарегистрирован", "web_fetch" in names)


def test_research_profile_includes_web() -> None:
    section("Профили research/marketing/autonomous подключают навык web")
    from agent.build import build_agent
    from agent.config import Config

    for prof in ("research", "marketing", "autonomous"):
        with tempfile.TemporaryDirectory() as td:
            cfg = Config.load(None, provider="ollama", model="m", workspace=td,
                              profile=prof)
            agent = build_agent(cfg)
            names = agent.tools.names()
            check(f"web_search доступен в профиле {prof}", "web_search" in names,
                  str(names))
            check(f"web_fetch доступен в профиле {prof}", "web_fetch" in names,
                  str(names))


def test_example_config_loads() -> None:
    section("examples/config.web.json грузится без ошибок")
    from agent.config import Config

    root = Path(__file__).resolve().parents[1]
    cfg = Config.load(str(root / "examples" / "config.web.json"))
    check("навык web подключён", "web" in cfg.skills)
    check("backend разобран из примера", cfg.web.backend == "duckduckgo_lite")
    check("allow_local по умолчанию false", cfg.web.allow_local is False)
    check("комментарные ключи не попали в поля web",
          not hasattr(cfg.web, "_комментарий_backend"))


def main() -> int:
    test_search_duckduckgo_lite_parses_real_markup()
    test_search_duckduckgo_html_direct_links()
    test_search_no_results_is_honest()
    test_search_max_results_limits_output()
    test_search_searxng_backend()
    test_searxng_requires_base_url()
    test_unknown_backend_rejected()
    test_search_empty_query_rejected()
    test_search_http_error_becomes_tool_error()
    test_fetch_extracts_text_strips_scripts()
    test_fetch_max_chars_truncates()
    test_fetch_rejects_non_text_content_type()
    test_fetch_http_error_becomes_tool_error()
    test_fetch_empty_url_rejected()
    test_ssrf_blocks_private_and_special_addresses()
    test_ssrf_allows_public_and_allow_local_override()
    test_ssrf_rejects_bad_scheme()
    test_ssrf_web_fetch_end_to_end()
    test_ssrf_redirect_to_private_is_blocked()
    test_ssrf_dns_resolution_failure_is_tool_error()
    test_dns_resolve_timeout_does_not_hang_forever()
    test_rate_limit_enforced_between_calls()
    test_web_config_from_dict_ignores_comments()
    test_build_agent_with_web_skill()
    test_research_profile_includes_web()
    test_example_config_loads()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
