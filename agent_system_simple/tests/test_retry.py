"""Тесты повтора при сбое и учёта токенов.

Повтор проверяется на НАСТОЯЩЕМ HTTP-сервере, который сначала падает,
а потом начинает отвечать. Так проверяется реальный путь через сокеты,
а не подменённый метод.
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.core import Agent                                    # noqa: E402
from agent.llm.base import BaseLLM, LLMError, LLMReply, Usage, price_of  # noqa: E402
from agent.llm.openai_like import OpenAILike                    # noqa: E402
from agent.tools.base import ToolRegistry                       # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def section(t: str) -> None:
    print(f"\n{t}\n" + "─" * len(t))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ------------------------------------------------- нестабильный сервер
class Flaky(BaseHTTPRequestHandler):
    """Первые fail_times запросов падают, дальше отвечает нормально."""
    fail_times = 2
    code = 503
    seen = 0

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        type(self).seen += 1
        if type(self).seen <= type(self).fail_times:
            self.send_response(type(self).code)
            body = b'{"error":"temporarily unavailable"}'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "готово"}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Flaky)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)
    return srv


# ============================================================== повторы
def test_retry_recovers() -> None:
    section("Повтор при сбое: сеть моргнула — прогон продолжился")
    port = free_port()
    Flaky.seen, Flaky.fail_times, Flaky.code = 0, 2, 503
    srv = serve(port)
    try:
        llm = OpenAILike("gpt-4o-mini", base_url=f"http://127.0.0.1:{port}/v1",
                         api_key="k", retries=3, retry_base=0.1)
        notes: list[tuple[int, float]] = []
        llm.on_retry = lambda n, why, d: notes.append((n, d))
        t0 = time.time()
        reply = llm.chat([{"role": "user", "content": "привет"}])
        dt = time.time() - t0
        check("ответ получен после двух сбоев", reply.text == "готово",
              reply.text)
        check("повторов было ровно два", llm.retried == 2, str(llm.retried))
        check("сервер видел три запроса", Flaky.seen == 3, str(Flaky.seen))
        check("наблюдатель получил уведомления", len(notes) == 2, str(notes))
        check("пауза растёт", notes[1][1] > notes[0][1], str(notes))
        check("ждали недолго при малой базе", dt < 3, f"{dt:.2f} с")
    finally:
        srv.shutdown()


def test_retry_gives_up() -> None:
    section("Повтор не бесконечен")
    port = free_port()
    Flaky.seen, Flaky.fail_times, Flaky.code = 0, 99, 503
    srv = serve(port)
    try:
        llm = OpenAILike("gpt-4o-mini", base_url=f"http://127.0.0.1:{port}/v1",
                         api_key="k", retries=2, retry_base=0.05)
        try:
            llm.chat([{"role": "user", "content": "x"}])
            check("сдаётся после лимита попыток", False, "не упал!")
        except LLMError as exc:
            check("сдаётся после лимита попыток", "503" in str(exc), str(exc)[:80])
        check("попыток было 1 + 2 повтора", Flaky.seen == 3, str(Flaky.seen))
    finally:
        srv.shutdown()


def test_no_retry_on_client_error() -> None:
    section("Ошибки настройки не повторяются")
    port = free_port()
    Flaky.seen, Flaky.fail_times, Flaky.code = 0, 99, 401
    srv = serve(port)
    try:
        llm = OpenAILike("gpt-4o-mini", base_url=f"http://127.0.0.1:{port}/v1",
                         api_key="bad", retries=3, retry_base=0.05)
        t0 = time.time()
        try:
            llm.chat([{"role": "user", "content": "x"}])
            check("401 поднимается сразу", False)
        except LLMError as exc:
            check("401 поднимается сразу", "401" in str(exc))
        check("повторов не было", llm.retried == 0, str(llm.retried))
        check("запрос был ровно один", Flaky.seen == 1, str(Flaky.seen))
        check("не тратили время на паузы", time.time() - t0 < 1)
    finally:
        srv.shutdown()


def test_retry_unreachable() -> None:
    section("Недоступный хост: повтор и понятная ошибка")
    llm = OpenAILike("gpt-4o-mini", base_url="http://127.0.0.1:1/v1",
                     api_key="k", retries=2, retry_base=0.05)
    try:
        llm.chat([{"role": "user", "content": "x"}])
        check("недоступный хост даёт ошибку", False)
    except LLMError as exc:
        check("недоступный хост даёт ошибку", "достучал" in str(exc).lower(),
              str(exc)[:80])
    check("повторы были", llm.retried == 2, str(llm.retried))


# =============================================================== токены
def test_usage() -> None:
    section("Учёт токенов")
    port = free_port()
    Flaky.seen, Flaky.fail_times = 0, 0
    srv = serve(port)
    try:
        llm = OpenAILike("gpt-4o-mini", base_url=f"http://127.0.0.1:{port}/v1",
                         api_key="k")
        r1 = llm.chat([{"role": "user", "content": "x"}])
        check("usage разобран из ответа",
              r1.usage.prompt == 120 and r1.usage.completion == 30,
              str(r1.usage))
        llm.chat([{"role": "user", "content": "y"}])
        check("расход накапливается", llm.usage.total == 300,
              str(llm.usage.total))
        check("вызовы посчитаны", llm.calls == 2, str(llm.calls))

        cost = llm.cost()
        # 240 вход * 0.15/1e6 + 60 выход * 0.60/1e6
        want = (240 * 0.15 + 60 * 0.60) / 1e6
        check("стоимость посчитана верно", abs(cost - want) < 1e-9,
              f"{cost} против {want}")
        check("отчёт содержит числа", "300" in llm.spend_report(),
              llm.spend_report())
    finally:
        srv.shutdown()


def test_prices() -> None:
    section("Цены и локальные модели")
    check("известная модель имеет цену", price_of("gpt-4o-mini") == (0.15, 0.60))
    check("модель по подстроке", price_of("openai/gpt-4o") is not None)
    check("неизвестная модель — None", price_of("какая-то-своя") is None)

    class Local(BaseLLM):
        billable = False

        def _chat_once(self, messages, tools=None):
            return LLMReply(text="ok", usage=Usage(1000, 500))

    l = Local("my-local-model")
    l.chat([])
    check("локальная модель бесплатна", l.cost() == 0.0, str(l.cost()))
    check("в отчёте сказано про локальную",
          "локальн" in l.spend_report(), l.spend_report())

    class Unknown(BaseLLM):
        def _chat_once(self, messages, tools=None):
            return LLMReply(text="ok", usage=Usage(1000, 500))

    u = Unknown("неведомая-модель")
    u.chat([])
    check("неизвестная цена не выдумывается", u.cost() is None)
    check("в отчёте честно сказано", "неизвестна" in u.spend_report(),
          u.spend_report())


def test_result_tokens() -> None:
    section("Расход попадает в результат прогона")

    class Counting(BaseLLM):
        def _chat_once(self, messages, tools=None):
            return LLMReply(text="всё", usage=Usage(50, 20))

    llm = Counting("gpt-4o-mini")
    res = Agent(llm, ToolRegistry(), max_steps=3).run("задача")
    check("токены в результате", res.tokens == 70, str(res.tokens))
    check("вход и выход раздельно",
          res.prompt_tokens == 50 and res.completion_tokens == 20)

    # второй прогон не должен приплюсовать первый
    res2 = Agent(llm, ToolRegistry(), max_steps=3).run("ещё")
    check("расход считается за прогон, а не накопительно",
          res2.tokens == 70, str(res2.tokens))
    check("у драйвера накопительно", llm.usage.total == 140,
          str(llm.usage.total))


def main() -> int:
    print("=" * 60)
    print("ТЕСТЫ ПОВТОРА И УЧЁТА ТОКЕНОВ")
    print("=" * 60)
    test_retry_recovers()
    test_retry_gives_up()
    test_no_retry_on_client_error()
    test_retry_unreachable()
    test_usage()
    test_prices()
    test_result_tokens()
    print("\n" + "=" * 60)
    print(f"пройдено: {PASS} · провалено: {FAIL}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
