"""Тесты maos.config и maos.llm: обязательный DB_DSN, разбор provider::model,
драйвер OpenAI-совместимого чата на настоящем HTTP-сервере, гибридный
роутинг с реальным сетевым fallback.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maos.config import Config, ConfigError                       # noqa: E402
from maos.llm.base import LLMError                                 # noqa: E402
from maos.llm.embeddings import HashEmbedder, cosine, build_embedder  # noqa: E402
from maos.llm.registry import (build_llm, format_model_ref,
                               known_providers, parse_model_ref,
                               provider_billable, provider_context_window)  # noqa: E402
from maos.orchestrator.hybrid import HybridLLM                     # noqa: E402

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


# --------------------------------------------------------------- fixtures
class FakeLLMHandler(BaseHTTPRequestHandler):
    """Простейший OpenAI-совместимый сервер: отвечает по счётчику вызовов."""

    calls = 0
    fail_until = 0     # первые N вызовов вернут 500 (для теста retry/fallback)
    always_500 = False
    last_body: dict | None = None

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n)
        type(self).calls += 1
        try:
            type(self).last_body = json.loads(raw.decode("utf-8"))
        except Exception:
            type(self).last_body = None
        if type(self).always_500 or type(self).calls <= type(self).fail_until:
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        msg = {"role": "assistant",
              "content": f"ответ #{type(self).calls}"}
        out = json.dumps({"choices": [{"message": msg}],
                          "usage": {"prompt_tokens": 5, "completion_tokens": 3}}
                         ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class _Server:
    def __init__(self, handler_cls: type = FakeLLMHandler):
        handler_cls.calls = 0
        handler_cls.fail_until = 0
        # НЕ трогаем always_500 здесь: AlwaysDownHandler должен оставаться
        # "вечно недоступным" сразу после конструирования — если сбросить
        # его в False, тест облачного fallback молча получал бы успешный
        # ответ вместо ошибки и не проверял бы то, что заявлено в имени.
        self.handler_cls = handler_cls
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class AlwaysDownHandler(FakeLLMHandler):
    """Отдельный класс: always_500 не должен влиять на основной FakeLLMHandler,
    т.к. атрибуты класса общие для всех инстансов ThreadingHTTPServer, если
    использовать один и тот же класс на два разных сервера одновременно."""
    calls = 0
    fail_until = 0
    always_500 = True


def main() -> int:
    section("Config: DB_DSN обязателен")
    os.environ.pop("DB_DSN", None)
    cfg = Config.load()
    check("db_dsn пуст без переменной окружения", cfg.db_dsn == "")
    try:
        cfg.require_dsn()
        check("require_dsn кидает ошибку без DSN", False)
    except ConfigError as exc:
        check("require_dsn кидает ConfigError без DSN", True)
        check("сообщение упоминает DB_DSN", "DB_DSN" in str(exc))

    os.environ["DB_DSN"] = "postgresql://u:p@localhost:5432/maos"
    cfg2 = Config.load()
    check("db_dsn подхватывается из окружения", cfg2.db_dsn.startswith("postgresql://"))
    check("require_dsn не падает при заданном DSN", cfg2.require_dsn() == cfg2.db_dsn)
    os.environ.pop("DB_DSN", None)

    section("Config.to_dict маскирует секреты")
    cfg3 = Config(db_dsn="postgresql://user:secretpass@host:5432/db",
                 api_token="topsecret")
    d = cfg3.to_dict()
    check("пароль в DSN замаскирован", "secretpass" not in d["db_dsn"])
    check("пользователь в DSN виден", "user" in d["db_dsn"])
    check("api_token замаскирован", d["api_token"] == "***")

    section("Config: комментарные ключи с префиксом _ игнорируются")
    import tempfile
    tf = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump({"_comment": "просто заметка", "port": 9999}, tf)
    tf.close()
    try:
        cfg4 = Config.load(tf.name)
        check("поле port применилось", cfg4.port == 9999)
        check("_comment не стал атрибутом", not hasattr(cfg4, "_comment"))
    finally:
        os.unlink(tf.name)

    section("parse_model_ref: разбор provider::model")
    check("local::llama3", parse_model_ref("local::llama3") == ("local", "llama3"))
    check("openrouter с слэшем",
         parse_model_ref("openrouter::anthropic/claude-3") ==
         ("openrouter", "anthropic/claude-3"))
    check("openrouter с двоеточием в имени модели (важно: :: как разделитель)",
         parse_model_ref("openrouter::deepseek/deepseek-chat:free") ==
         ("openrouter", "deepseek/deepseek-chat:free"))
    check("алиас lmstudio -> local", parse_model_ref("lmstudio::qwen") == ("local", "qwen"))
    try:
        parse_model_ref("no-separator-here")
        check("ссылка без :: кидает ошибку", False)
    except LLMError:
        check("ссылка без :: кидает ошибку", True)
    try:
        parse_model_ref("")
        check("пустая ссылка кидает ошибку", False)
    except LLMError:
        check("пустая ссылка кидает ошибку", True)
    try:
        parse_model_ref("::model")
        check("пустой провайдер кидает ошибку", False)
    except LLMError:
        check("пустой провайдер кидает ошибку", True)
    check("format_model_ref обратим",
         format_model_ref(*parse_model_ref("local::llama3")) == "local::llama3")
    check("known_providers содержит local/openai/openrouter",
         {"local", "openai", "openrouter"} <= set(known_providers()))
    check("provider_billable(local) == False", provider_billable("local") is False)
    check("provider_billable(openrouter) == True", provider_billable("openrouter") is True)
    check("provider_context_window(local) > 0", provider_context_window("local") > 0)

    try:
        build_llm("unknown_provider", "x")
        check("build_llm с неизвестным провайдером кидает ошибку", False)
    except LLMError:
        check("build_llm с неизвестным провайдером кидает ошибку", True)

    section("OpenAILike драйвер: реальный HTTP-вызов")
    srv = _Server()
    try:
        llm = build_llm("local", "test-model", base_url=srv.base_url, retries=0)
        reply = llm.chat([{"role": "user", "content": "привет"}])
        check("ответ содержит текст", reply.text == "ответ #1")
        check("usage.prompt учтён", llm.usage.prompt == 5)
        check("usage.completion учтён", llm.usage.completion == 3)
        check("billable=False для local", llm.billable is False)

        section("Повтор при 5xx (retryable)")
        FakeLLMHandler.calls = 0
        FakeLLMHandler.fail_until = 2
        llm2 = build_llm("local", "test-model", base_url=srv.base_url,
                        retries=3, retry_base=0.05)
        reply2 = llm2.chat([{"role": "user", "content": "тест"}])
        check("ответ получен после повторов", reply2.text.startswith("ответ #"))
        check("было больше одного вызова", FakeLLMHandler.calls >= 3)

        section("Исчерпание повторов -> LLMError")
        FakeLLMHandler.calls = 0
        FakeLLMHandler.always_500 = True
        llm3 = build_llm("local", "test-model", base_url=srv.base_url,
                        retries=1, retry_base=0.01)
        try:
            llm3.chat([{"role": "user", "content": "тест"}])
            check("после исчерпания повторов кидается LLMError", False)
        except LLMError as exc:
            check("после исчерпания повторов кидается LLMError", True)
            check("ошибка помечена retryable", exc.retryable is True)
        FakeLLMHandler.always_500 = False

        section("HybridLLM: выбор модели по сложности задачи + fallback")
        # "Облачный" провайдер в этом тесте — ДРУГОЙ порт того же фейкового
        # сервера, но с always_500=True: имитация недоступности без
        # реального сетевого таймаута до openrouter.ai (в песочнице DNS
        # к внешним хостам недоступен и уронил бы тест на десятки секунд).
        cloud_down = _Server(handler_cls=AlwaysDownHandler)
        os.environ["LOCAL_BASE_URL"] = srv.base_url
        os.environ["OPENROUTER_BASE_URL"] = cloud_down.base_url
        FakeLLMHandler.calls = 0
        FakeLLMHandler.fail_until = 0  # сброс после теста "исчерпание повторов"
        cfg_h = Config(complexity_char_threshold=20,
                      default_local_model="local::llama3",
                      default_cloud_model="openrouter::gpt-4o-mini",
                      fallback_to_local=True, llm_retries=0)
        hybrid = HybridLLM(cfg_h)
        check("короткая задача выбирает локальную модель",
             hybrid.choose_ref("привет") == "local::llama3")
        check("длинная задача выбирает облачную модель",
             hybrid.choose_ref("а" * 100) == "openrouter::gpt-4o-mini")
        check("явный agent_llm_ref перебивает эвристику",
             hybrid.choose_ref("а" * 100, agent_llm_ref="local::custom")
             == "local::custom")

        FakeLLMHandler.calls = 0
        result = hybrid.chat([{"role": "user", "content": "а" * 100}], "а" * 100)
        check("fallback реально произошёл", result.used_fallback is True)
        check("итоговая модель — локальная", result.provider_model == "local::llama3")
        check("ответ получен через фейковый локальный сервер",
             result.reply.text.startswith("ответ #"))
        os.environ.pop("LOCAL_BASE_URL", None)
        os.environ.pop("OPENROUTER_BASE_URL", None)
        cloud_down.close()
    finally:
        srv.close()

    section("Эмбеддинги: hash-эмбеддер детерминирован и локален")
    e1 = HashEmbedder(dim=64)
    v1 = e1.embed_one("привет мир")
    v2 = e1.embed_one("привет мир")
    v3 = e1.embed_one("совершенно другой текст")
    check("одинаковый текст даёт одинаковый вектор", v1 == v2)
    check("разные тексты дают разные вектора", v1 != v3)
    check("самоподобие ~1.0", abs(cosine(v1, v1) - 1.0) < 1e-6)
    check("billable=False у hash", e1.billable is False)
    check("build_embedder('hash', ...) работает",
         build_embedder("hash", "hash-256").name == "hash")
    try:
        build_embedder("unknown", "m")
        check("build_embedder с неизвестным провайдером кидает ошибку", False)
    except Exception:
        check("build_embedder с неизвестным провайдером кидает ошибку", True)

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
