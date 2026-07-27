"""Тесты приёма вложений через Webhook (Telegram/MAX) — agent/webhooks.py.

Философия та же, что в test_messaging.py/test_e2e.py: реальные сокеты,
реальный HTTP-сервер agent/server.py (Handler), фейковые Telegram/MAX API
(тот же приём — http.server.ThreadingHTTPServer) и фейковая LLM (как в
test_e2e.py, FakeLLMHandler) — заглушка стоит только на месте самой
модели, весь остальной путь настоящий: HTTP POST -> проверка секрета ->
скачивание вложения по сети -> сохранение файла в workspace -> прогон
агента -> ответ отправлен обратно тем же HTTP-запросом к фейковому API.
"""
from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import Config                                       # noqa: E402
from agent.server import Handler                                      # noqa: E402
from agent import webhooks                                            # noqa: E402
from agent.tools import messaging as msg_mod                          # noqa: E402

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


def wait_until(fn, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """Опрос вместо фиксированного sleep — обработка уходит в фоновый

    поток (см. webhooks.dispatch_telegram/dispatch_max), и её длительность
    не детерминирована по конструкции (реальные HTTP-вызовы к фейковому
    API). Фиксированный sleep либо слишком долгий, либо флейковый.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(interval)
    return fn()


# ================================================== фейковая LLM (как e2e)
class FakeLLMHandler(BaseHTTPRequestHandler):
    """Отвечает текстом сразу — вложений тут не нужно распознавать,

    важно лишь убедиться, что задача с текстом про файл дошла до модели.
    """
    calls = 0
    last_prompt = ""

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n)
        type(self).calls += 1
        try:
            body = json.loads(raw.decode("utf-8"))
            msgs = body.get("messages", [])
            type(self).last_prompt = json.dumps(msgs, ensure_ascii=False)
        except Exception:
            type(self).last_prompt = ""
        msg = {"role": "assistant", "content": "Готово: файл разобран."}
        out = json.dumps({"choices": [{"message": msg}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


# ============================================= фейковые Telegram/MAX API
class _TelegramApiHandler(BaseHTTPRequestHandler):
    """Обслуживает getFile + скачивание файла + sendMessage."""

    calls: list[str] = []
    file_bytes = b"fake pdf content"
    file_path = "documents/file_1.pdf"

    def log_message(self, *a):
        pass

    def _reply_json(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        type(self).calls.append(self.path)
        if "getFile" in self.path:
            self._reply_json({"ok": True, "result": {
                "file_path": type(self).file_path,
                "file_size": len(type(self).file_bytes)}})
        elif "sendMessage" in self.path:
            self._reply_json({"ok": True, "result": {"message_id": 1}})
        else:
            self._reply_json({"ok": False, "description": "not found"})

    def do_GET(self):  # noqa: N802
        type(self).calls.append(self.path)
        if self.path.startswith("/file/bot") and type(self).file_path in self.path:
            body = type(self).file_bytes
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._reply_json({"ok": False, "description": "not found"})


class _MaxApiHandler(BaseHTTPRequestHandler):
    calls: list[str] = []
    file_bytes = b"fake image bytes"

    def log_message(self, *a):
        pass

    def _reply_json(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        type(self).calls.append(self.path)
        if self.path.startswith("/messages"):
            self._reply_json({"message": {"body": {"mid": "m-1"}}})
        else:
            self._reply_json({"ok": False})

    def do_GET(self):  # noqa: N802
        type(self).calls.append(self.path)
        if self.path == "/download-attachment":
            body = type(self).file_bytes
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._reply_json({"ok": False})


def start_server(handler_cls) -> tuple[ThreadingHTTPServer, int]:
    port = free_port()
    handler_cls.calls = []
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


# ============================================================= helpers
def _secret_check(name: str, verify_fn, ok_headers, bad_headers, cfg) -> None:
    check(f"{name}: верный секрет проходит", verify_fn(ok_headers, cfg) is True)
    check(f"{name}: неверный секрет отклонён", verify_fn(bad_headers, cfg) is False)


class _Headers(dict):
    def get(self, k, default=None):
        return dict.get(self, k, default)


# ================================================================= tests
def test_verify_secret_functions() -> None:
    section("verify_telegram_secret / verify_max_secret: сравнение секрета")
    tg_cfg = msg_mod.TelegramConfig(bot_token="t", webhook_secret="s3cr3t")
    _secret_check(
        "telegram", webhooks.verify_telegram_secret,
        _Headers({"X-Telegram-Bot-Api-Secret-Token": "s3cr3t"}),
        _Headers({"X-Telegram-Bot-Api-Secret-Token": "wrong"}), tg_cfg)
    check("telegram: пустой секрет в конфиге -> всегда False (маршрут не активен)",
          webhooks.verify_telegram_secret(
              _Headers({"X-Telegram-Bot-Api-Secret-Token": ""}),
              msg_mod.TelegramConfig(bot_token="t")) is False)

    max_cfg = msg_mod.MaxConfig(bot_token="m", webhook_secret="max-secret")
    _secret_check(
        "max", webhooks.verify_max_secret,
        _Headers({"X-Max-Bot-Api-Secret": "max-secret"}),
        _Headers({"X-Max-Bot-Api-Secret": "wrong"}), max_cfg)


def test_safe_name_blocks_path_traversal() -> None:
    section("_safe_name: имя файла от платформы не выходит за пределы inbox")
    check("../../etc/passwd обезврежен",
          ".." not in webhooks._safe_name("../../etc/passwd"))
    check("абсолютный путь обезврежен",
          not webhooks._safe_name("/etc/passwd").startswith("/"))
    check("пустое имя не ломает", webhooks._safe_name("") == "file")
    check("обычное имя не портится",
          webhooks._safe_name("отчёт.pdf") == "отчёт.pdf")


def test_build_task_mentions_files_and_text() -> None:
    section("_build_task: формулировка задачи для агента")
    t1 = webhooks._build_task("привет", ["inbox/telegram/1/doc.pdf"])
    check("текст присутствует", "привет" in t1)
    check("файл упомянут", "doc.pdf" in t1)
    t2 = webhooks._build_task("", [])
    check("пустое сообщение -> просьба уточнить", "уточни" in t2)


def test_telegram_webhook_end_to_end() -> None:
    section("POST /webhook/telegram: секрет, скачивание вложения, ответ")
    llm_port = free_port()
    llm_srv = ThreadingHTTPServer(("127.0.0.1", llm_port), FakeLLMHandler)
    threading.Thread(target=llm_srv.serve_forever, daemon=True).start()
    tg_srv, tg_port = start_server(_TelegramApiHandler)
    try:
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(provider="openai", model="fake",
                        base_url=f"http://127.0.0.1:{llm_port}/v1",
                        api_key="test", workspace=td, skills=["files"],
                        max_steps=4)
            cfg.sandbox.mode = "off"
            cfg.messaging.telegram = msg_mod.TelegramConfig(
                bot_token="TEST:TOKEN",
                api_base=f"http://127.0.0.1:{tg_port}",
                webhook_secret="my-secret", rate_limit=0.0)

            api_port = free_port()
            Handler.cfg = cfg
            Handler.token = "api-token"
            api = ThreadingHTTPServer(("127.0.0.1", api_port), Handler)
            threading.Thread(target=api.serve_forever, daemon=True).start()
            time.sleep(0.2)
            base = f"http://127.0.0.1:{api_port}"

            update = {
                "update_id": 1,
                "message": {
                    "chat": {"id": 555},
                    "text": "Что в документе?",
                    "document": {"file_id": "abc123", "file_name": "report.pdf"},
                },
            }

            # НЕГАТИВНЫЙ: без секрета в заголовке — отказ, вложение НЕ скачивается
            _TelegramApiHandler.calls = []
            req = urllib.request.Request(
                f"{base}/webhook/telegram",
                data=json.dumps(update).encode(), method="POST",
                headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=10)
                check("запрос без секрета отклонён", False, "пустили!")
            except urllib.error.HTTPError as e:
                check("запрос без секрета отклонён", e.code == 401, str(e.code))
            time.sleep(0.2)
            check("без верного секрета вложение не скачивалось",
                  not any("getFile" in c for c in _TelegramApiHandler.calls),
                  str(_TelegramApiHandler.calls))

            # НЕГАТИВНЫЙ: неверный секрет
            req = urllib.request.Request(
                f"{base}/webhook/telegram",
                data=json.dumps(update).encode(), method="POST",
                headers={"Content-Type": "application/json",
                         "X-Telegram-Bot-Api-Secret-Token": "wrong"})
            try:
                urllib.request.urlopen(req, timeout=10)
                check("неверный секрет отклонён", False, "пустили!")
            except urllib.error.HTTPError as e:
                check("неверный секрет отклонён", e.code == 401, str(e.code))

            # ПОЗИТИВНЫЙ: верный секрет -> 200 сразу, обработка в фоне
            _TelegramApiHandler.calls = []
            FakeLLMHandler.calls = 0
            t0 = time.time()
            req = urllib.request.Request(
                f"{base}/webhook/telegram",
                data=json.dumps(update).encode(), method="POST",
                headers={"Content-Type": "application/json",
                         "X-Telegram-Bot-Api-Secret-Token": "my-secret"})
            with urllib.request.urlopen(req, timeout=10) as r:
                resp = json.load(r)
            elapsed = time.time() - t0
            check("вебхук отвечает 200 сразу (не ждёт агента)",
                  resp.get("ok") is True and elapsed < 2.0, f"{elapsed:.2f}s")

            ok = wait_until(lambda: FakeLLMHandler.calls > 0, timeout=10)
            check("агент реально запущен в фоне", ok)

            inbox = Path(td) / "inbox" / "telegram" / "555"
            ok_file = wait_until(lambda: inbox.exists() and
                                 any(inbox.iterdir()), timeout=10)
            check("вложение сохранено в workspace/inbox", ok_file,
                  str(list(inbox.iterdir()) if inbox.exists() else "нет папки"))
            if ok_file:
                saved = next(inbox.iterdir())
                check("содержимое файла реально скачано",
                      saved.read_bytes() == _TelegramApiHandler.file_bytes)
                check("имя файла сохранено",
                      saved.name.endswith(".pdf"), saved.name)

            ok_reply = wait_until(
                lambda: any("sendMessage" in c for c in _TelegramApiHandler.calls),
                timeout=10)
            check("ответ отправлен обратно в Telegram", ok_reply,
                  str(_TelegramApiHandler.calls))
            check("текст задачи содержал имя файла",
                  "report" in FakeLLMHandler.last_prompt.lower()
                  or "pdf" in FakeLLMHandler.last_prompt.lower(),
                  FakeLLMHandler.last_prompt[:300])

            api.shutdown()
    finally:
        tg_srv.shutdown()
        llm_srv.shutdown()


def test_telegram_webhook_route_absent_without_secret() -> None:
    section("POST /webhook/telegram: маршрут не активен без webhook_secret")
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(provider="openai", model="fake",
                    base_url="http://127.0.0.1:1/v1", api_key="k",
                    workspace=td, skills=["files"], max_steps=1)
        cfg.sandbox.mode = "off"
        # telegram настроен (есть токен), но БЕЗ webhook_secret
        cfg.messaging.telegram = msg_mod.TelegramConfig(bot_token="t")
        api_port = free_port()
        Handler.cfg = cfg
        Handler.token = None
        api = ThreadingHTTPServer(("127.0.0.1", api_port), Handler)
        threading.Thread(target=api.serve_forever, daemon=True).start()
        time.sleep(0.2)
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{api_port}/webhook/telegram",
                data=b"{}", method="POST",
                headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=10)
                check("без webhook_secret маршрут закрыт (404)", False, "пустили!")
            except urllib.error.HTTPError as e:
                check("без webhook_secret маршрут закрыт (404)", e.code == 404,
                      str(e.code))
        finally:
            api.shutdown()


def test_max_webhook_end_to_end() -> None:
    section("POST /webhook/max: секрет, скачивание вложения по прямой ссылке, ответ")
    llm_port = free_port()
    llm_srv = ThreadingHTTPServer(("127.0.0.1", llm_port), FakeLLMHandler)
    threading.Thread(target=llm_srv.serve_forever, daemon=True).start()
    max_srv, max_port = start_server(_MaxApiHandler)
    try:
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(provider="openai", model="fake",
                        base_url=f"http://127.0.0.1:{llm_port}/v1",
                        api_key="test", workspace=td, skills=["files"],
                        max_steps=4)
            cfg.sandbox.mode = "off"
            cfg.messaging.max = msg_mod.MaxConfig(
                bot_token="max-token",
                api_base=f"http://127.0.0.1:{max_port}",
                webhook_secret="max-secret", rate_limit=0.0)

            api_port = free_port()
            Handler.cfg = cfg
            Handler.token = "api-token"
            api = ThreadingHTTPServer(("127.0.0.1", api_port), Handler)
            threading.Thread(target=api.serve_forever, daemon=True).start()
            time.sleep(0.2)
            base = f"http://127.0.0.1:{api_port}"

            update = {
                "update_type": "message_created",
                "message": {
                    "sender": {"user_id": 42},
                    "recipient": {"chat_id": 777},
                    "body": {
                        "text": "гляди фото",
                        "attachments": [{
                            "type": "image",
                            "payload": {
                                "url": f"http://127.0.0.1:{max_port}/download-attachment"},
                        }],
                    },
                },
            }

            # НЕГАТИВНЫЙ: неверный секрет
            req = urllib.request.Request(
                f"{base}/webhook/max", data=json.dumps(update).encode(),
                method="POST", headers={"Content-Type": "application/json",
                                        "X-Max-Bot-Api-Secret": "wrong"})
            try:
                urllib.request.urlopen(req, timeout=10)
                check("неверный секрет MAX отклонён", False, "пустили!")
            except urllib.error.HTTPError as e:
                check("неверный секрет MAX отклонён", e.code == 401, str(e.code))

            _MaxApiHandler.calls = []
            FakeLLMHandler.calls = 0
            req = urllib.request.Request(
                f"{base}/webhook/max", data=json.dumps(update).encode(),
                method="POST", headers={"Content-Type": "application/json",
                                        "X-Max-Bot-Api-Secret": "max-secret"})
            with urllib.request.urlopen(req, timeout=10) as r:
                resp = json.load(r)
            check("вебхук MAX отвечает 200", resp.get("ok") is True)

            inbox = Path(td) / "inbox" / "max" / "777"
            ok_file = wait_until(lambda: inbox.exists() and
                                 any(inbox.iterdir()), timeout=10)
            check("вложение MAX сохранено", ok_file,
                  str(list(inbox.iterdir()) if inbox.exists() else "нет папки"))
            if ok_file:
                saved = next(inbox.iterdir())
                check("содержимое вложения MAX реально скачано",
                      saved.read_bytes() == _MaxApiHandler.file_bytes)

            ok_reply = wait_until(
                lambda: any("/messages" in c for c in _MaxApiHandler.calls),
                timeout=10)
            check("ответ отправлен обратно в MAX (по chat_id, не user_id)", ok_reply,
                  str(_MaxApiHandler.calls))
            check("chat_id использован как приоритетный адресат",
                  any("chat_id=777" in c for c in _MaxApiHandler.calls),
                  str(_MaxApiHandler.calls))

            api.shutdown()
    finally:
        max_srv.shutdown()
        llm_srv.shutdown()


def test_max_webhook_ignores_non_message_updates() -> None:
    section("MAX: типы обновлений без сообщения игнорируются, не роняют вебхук")
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(provider="openai", model="fake",
                    base_url="http://127.0.0.1:1/v1", api_key="k",
                    workspace=td, skills=["files"], max_steps=1)
        cfg.sandbox.mode = "off"
        cfg.messaging.max = msg_mod.MaxConfig(bot_token="t", webhook_secret="s")
        try:
            webhooks.process_max_update(cfg, {"update_type": "bot_started"})
            check("bot_started не роняет обработку", True)
        except Exception as exc:
            check("bot_started не роняет обработку", False, str(exc))


def test_register_webhook_requires_secret() -> None:
    section("register_telegram_webhook / register_max_webhook требуют секрет")
    try:
        webhooks.register_telegram_webhook(
            msg_mod.TelegramConfig(bot_token="t"), "https://example.com/webhook")
        check("telegram: отказ без webhook_secret", False)
    except ValueError:
        check("telegram: отказ без webhook_secret", True)

    try:
        webhooks.register_max_webhook(
            msg_mod.MaxConfig(bot_token="m"), "https://example.com/webhook")
        check("max: отказ без webhook_secret", False)
    except ValueError:
        check("max: отказ без webhook_secret", True)


def test_messaging_config_webhook_fields() -> None:
    section("MessagingConfig.from_dict: webhook_secret и webhook_profile")
    cfg = msg_mod.MessagingConfig.from_dict({
        "telegram": {"bot_token": "t", "webhook_secret": "s1"},
        "max": {"bot_token": "m", "webhook_secret": "s2"},
        "webhook_profile": "intake",
    })
    check("telegram.webhook_secret разобран", cfg.telegram.webhook_secret == "s1")
    check("max.webhook_secret разобран", cfg.max.webhook_secret == "s2")
    check("webhook_profile разобран", cfg.webhook_profile == "intake")

    cfg2 = msg_mod.MessagingConfig()
    check("webhook_profile по умолчанию пуст", cfg2.webhook_profile == "")


def test_config_masks_webhook_secrets() -> None:
    section("Config.to_dict(): секреты вебхуков маскируются")
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(provider="openai", model="x", api_key="k", workspace=td)
        cfg.messaging.telegram = msg_mod.TelegramConfig(
            bot_token="tok", webhook_secret="topsecret")
        cfg.messaging.max = msg_mod.MaxConfig(
            bot_token="tok2", webhook_secret="anothersecret")
        d = cfg.to_dict()
        check("telegram.webhook_secret замаскирован",
              d["messaging"]["telegram"]["webhook_secret"] == "***")
        check("max.webhook_secret замаскирован",
              d["messaging"]["max"]["webhook_secret"] == "***")
        check("telegram.bot_token тоже замаскирован",
              d["messaging"]["telegram"]["bot_token"] == "***")


def test_config_env_reads_webhook_secrets() -> None:
    section("Config.load: секреты вебхуков читаются из окружения")
    import os
    old_tg = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    old_max = os.environ.get("MAX_WEBHOOK_SECRET")
    try:
        os.environ["TELEGRAM_WEBHOOK_SECRET"] = "env-secret-tg"
        os.environ["MAX_WEBHOOK_SECRET"] = "env-secret-max"
        with tempfile.TemporaryDirectory() as td:
            cfg = Config.load(None, provider="openai", model="x", workspace=td)
        check("TELEGRAM_WEBHOOK_SECRET подхвачен",
              cfg.messaging.telegram.webhook_secret == "env-secret-tg")
        check("MAX_WEBHOOK_SECRET подхвачен",
              cfg.messaging.max.webhook_secret == "env-secret-max")
    finally:
        for k, v in (("TELEGRAM_WEBHOOK_SECRET", old_tg),
                     ("MAX_WEBHOOK_SECRET", old_max)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main() -> int:
    print("=" * 60)
    print("ТЕСТЫ: приём вложений через Webhook (Telegram/MAX)")
    print("=" * 60)

    test_verify_secret_functions()
    test_safe_name_blocks_path_traversal()
    test_build_task_mentions_files_and_text()
    test_messaging_config_webhook_fields()
    test_config_masks_webhook_secrets()
    test_config_env_reads_webhook_secrets()
    test_register_webhook_requires_secret()
    test_max_webhook_ignores_non_message_updates()
    test_telegram_webhook_route_absent_without_secret()
    test_telegram_webhook_end_to_end()
    test_max_webhook_end_to_end()

    print("\n" + "=" * 60)
    print(f"пройдено: {PASS} · провалено: {FAIL}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
