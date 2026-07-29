"""Тесты навыка messaging: email (SMTP/IMAP), Telegram Bot API, MAX Bot API.

Проверяется на НАСТОЯЩИХ сокетах, а не на моках библиотек — та же
философия, что у test_retry.py/test_mcp.py:
  * SMTP — через aiosmtpd (реальный сервер в отдельном потоке);
  * IMAP — через fake_imap_server.py (сырой сокет, настоящий текстовый
    протокол IMAP4, как fake_mcp_server.py для MCP);
  * Telegram/MAX — через http.server.ThreadingHTTPServer, оба API это
    обычный HTTPS/JSON, поэтому один фейковый обработчик обслуживает оба.

aiosmtpd — опциональная зависимость только для ЭТОГО теста (сам навык
messaging использует исключительно smtplib из stdlib). Если её нет,
проверки SMTP пропускаются с понятным сообщением, остальные (IMAP,
Telegram, MAX) всё равно выполняются — они зависят только от stdlib.
"""
from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.tools.base import ToolError, Workspace                     # noqa: E402
from agent.tools import messaging as msg_mod                          # noqa: E402
from fake_imap_server import FakeImapServer                           # noqa: E402

PASS, FAIL = 0, 0

try:
    from aiosmtpd.controller import Controller
    from aiosmtpd.smtp import AuthResult, LoginPassword
    HAVE_AIOSMTPD = True
except ImportError:
    HAVE_AIOSMTPD = False


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


# ============================================================ SMTP-фикстура
class _SmtpHandler:
    def __init__(self) -> None:
        self.messages: list[tuple[str, list[str], bytes]] = []

    async def handle_DATA(self, server, session, envelope):
        self.messages.append((envelope.mail_from, envelope.rcpt_tos, envelope.content))
        return "250 OK"


def _start_smtp(user: str, password: str):
    handler = _SmtpHandler()

    def authenticator(server, session, envelope, mechanism, auth_data):
        if isinstance(auth_data, LoginPassword):
            if auth_data.login == user.encode() and auth_data.password == password.encode():
                return AuthResult(success=True)
        return AuthResult(success=False, handled=False)

    port = free_port()
    ctrl = Controller(handler, hostname="127.0.0.1", port=port,
                      auth_required=True, authenticator=authenticator,
                      auth_require_tls=False)
    ctrl.start()
    return ctrl, handler, port


# ======================================================= Telegram/MAX фикстура
class _BotAPIHandler(BaseHTTPRequestHandler):
    """Общий фейковый обработчик для Telegram Bot API и MAX Bot API — оба
    протокола суть HTTPS + JSON, различаются только путями/полями."""

    calls: list[dict] = []       # заполняется классом-наследником в тесте
    fail_next = False

    def log_message(self, *a):
        pass

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n) if n else b""

    def _reply(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        raw = self._body()
        self.calls.append({"path": self.path, "headers": dict(self.headers),
                           "body": raw})
        if type(self).fail_next:
            self._reply(400, {"ok": False, "description": "искусственная ошибка"})
            return
        self._route(raw)

    def do_GET(self):  # noqa: N802
        self.calls.append({"path": self.path, "headers": dict(self.headers),
                           "body": b""})
        self._route(b"")

    def _route(self, raw: bytes) -> None:
        if "/sendMessage" in self.path:
            self._reply(200, {"ok": True, "result": {"message_id": 42}})
        elif "/sendDocument" in self.path:
            self._reply(200, {"ok": True, "result": {"message_id": 43}})
        elif "/getUpdates" in self.path:
            self._reply(200, {"ok": True, "result": [
                {"update_id": 100, "message": {
                    "from": {"username": "u1"}, "chat": {"id": 555},
                    "text": "привет боту"}},
            ]})
        elif self.path.startswith("/messages"):
            self._reply(200, {"message": {"body": {"mid": "m-1"}}})
        elif self.path.startswith("/updates"):
            self._reply(200, {"updates": [
                {"update_type": "message_created",
                 "message": {"sender": {"name": "Ivan"},
                            "body": {"text": "привет боту max"}}},
            ], "marker": 999})
        else:
            self._reply(404, {"ok": False, "description": "not found"})


def _start_bot_api() -> tuple[ThreadingHTTPServer, int]:
    port = free_port()
    _BotAPIHandler.calls = []
    _BotAPIHandler.fail_next = False
    srv = ThreadingHTTPServer(("127.0.0.1", port), _BotAPIHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


# ================================================================== tests
def test_email_send_real_smtp() -> None:
    section("email_send: реальная отправка по SMTP (aiosmtpd)")
    ctrl, handler, port = _start_smtp("bot@example.com", "secret")
    try:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(Path(td) / "ws")
            cfg = msg_mod.EmailConfig(
                smtp_host="127.0.0.1", smtp_port=port, smtp_user="bot@example.com",
                smtp_password="secret", smtp_use_ssl=False, smtp_starttls=False)
            tools = {t.name: t for t in msg_mod.build_email_tools(
                ws, cfg, confirm_sends=False, confirm=None)}

            (ws.root / "note.txt").write_text("вложение", encoding="utf-8")
            out = tools["email_send"].fn(
                to="a@example.com, b@example.com", subject="Тест письма",
                body="Текст письма", cc="c@example.com",
                attachments="note.txt")
            check("отчёт об отправке содержит получателей", "a@example.com" in out, out)
            check("письмо реально дошло до сервера", len(handler.messages) == 1)

            mail_from, rcpts, content = handler.messages[0]
            check("отправитель верный", mail_from == "bot@example.com", mail_from)
            check("все получатели включая cc",
                  set(rcpts) == {"a@example.com", "b@example.com", "c@example.com"},
                  str(rcpts))
            check("тема доставлена", b"=?utf-8?" in content or b"Subject:" in content)

            # НЕГАТИВНЫЙ: неверный пароль -> понятная ошибка, не трейсбек
            cfg_bad = msg_mod.EmailConfig(
                smtp_host="127.0.0.1", smtp_port=port, smtp_user="bot@example.com",
                smtp_password="wrong-password", smtp_use_ssl=False, smtp_starttls=False)
            tools_bad = {t.name: t for t in msg_mod.build_email_tools(
                ws, cfg_bad, confirm_sends=False, confirm=None)}
            try:
                tools_bad["email_send"].fn(to="a@example.com", subject="x", body="y")
                check("отказ при неверном пароле SMTP", False)
            except ToolError:
                check("отказ при неверном пароле SMTP", True)
    finally:
        ctrl.stop()


def test_email_send_negative_without_config() -> None:
    section("email_send: отказ без настроенного SMTP")
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(Path(td) / "ws")
        cfg = msg_mod.EmailConfig()  # пусто
        tools = {t.name: t for t in msg_mod.build_email_tools(
            ws, cfg, confirm_sends=False, confirm=None)}
        try:
            tools["email_send"].fn(to="a@example.com", subject="x", body="y")
            check("отказ без SMTP-настроек", False)
        except ToolError:
            check("отказ без SMTP-настроек", True)


def test_email_send_requires_confirmation() -> None:
    section("email_send: подтверждение оператора перед отправкой")
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(Path(td) / "ws")
        cfg = msg_mod.EmailConfig(smtp_host="127.0.0.1", smtp_port=1,
                                  smtp_user="u", smtp_password="p")

        calls = []

        def deny(detail, action):
            calls.append((detail, action))
            return False

        tools = {t.name: t for t in msg_mod.build_email_tools(
            ws, cfg, confirm_sends=True, confirm=deny)}
        try:
            tools["email_send"].fn(to="a@example.com", subject="x", body="y")
            check("отклонено оператором до попытки соединения", False)
        except ToolError as exc:
            check("отклонено оператором до попытки соединения",
                  "отклонена" in str(exc), str(exc))
        check("оператор реально спрошен", len(calls) == 1)

        def allow(detail, action):
            return True

        tools2 = {t.name: t for t in msg_mod.build_email_tools(
            ws, cfg, confirm_sends=True, confirm=allow)}
        try:
            tools2["email_send"].fn(to="a@example.com", subject="x", body="y")
            check("после согласия идёт реальная попытка соединения", False)
        except ToolError as exc:
            # порт 1 не слушает — соединение не удастся, но это ДРУГАЯ
            # ошибка, чем отказ оператора: значит, подтверждение пройдено
            check("после согласия идёт реальная попытка соединения",
                  "отклонена" not in str(exc), str(exc))


def test_email_list_and_read_real_imap() -> None:
    section("email_list/email_read: реальный IMAP-протокол (fake_imap_server)")
    srv = FakeImapServer()
    try:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(Path(td) / "ws")
            cfg = msg_mod.EmailConfig(
                imap_host="127.0.0.1", imap_port=srv.port,
                imap_user="u", imap_password="p", imap_use_ssl=False)
            tools = {t.name: t for t in msg_mod.build_email_tools(
                ws, cfg, confirm_sends=False, confirm=None)}

            out = tools["email_list"].fn(folder="INBOX", limit=10)
            check("список писем получен", "писем" in out, out)
            check("оба письма видны", "sender1@example.com" in out and
                  "sender2@example.com" in out, out)

            out2 = tools["email_read"].fn(message_id="2")
            check("тело письма прочитано", "Plain second message body" in out2, out2)
            check("тема письма видна", "Second" in out2, out2)
    finally:
        srv.close()


def test_email_negative_without_imap() -> None:
    section("email_list: отказ без настроенного IMAP")
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(Path(td) / "ws")
        tools = {t.name: t for t in msg_mod.build_email_tools(
            ws, msg_mod.EmailConfig(), confirm_sends=False, confirm=None)}
        try:
            tools["email_list"].fn()
            check("отказ без IMAP-настроек", False)
        except ToolError:
            check("отказ без IMAP-настроек", True)


def test_telegram_send_and_updates() -> None:
    section("Telegram: отправка сообщения/файла, получение обновлений")
    srv, port = _start_bot_api()
    try:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(Path(td) / "ws")
            cfg = msg_mod.TelegramConfig(
                bot_token="TEST:TOKEN", api_base=f"http://127.0.0.1:{port}",
                rate_limit=0.0)
            tools = {t.name: t for t in msg_mod.build_telegram_tools(
                ws, cfg, confirm_sends=False, confirm=None)}

            out = tools["telegram_send_message"].fn(chat_id="123", text="Привет")
            check("сообщение отправлено", "message_id=42" in out, out)
            check("бот-токен попал в путь запроса",
                  any("TEST:TOKEN" in c["path"] for c in _BotAPIHandler.calls))

            (ws.root / "report.txt").write_text("отчёт", encoding="utf-8")
            out2 = tools["telegram_send_file"].fn(chat_id="123", file_path="report.txt")
            check("файл отправлен", "отправлен" in out2, out2)

            out3 = tools["telegram_get_updates"].fn(limit=5)
            check("обновления получены", "привет боту" in out3, out3)
            check("следующий offset посчитан", "offset: 101" in out3, out3)

            # НЕГАТИВНЫЙ: API вернул ошибку
            _BotAPIHandler.fail_next = True
            try:
                tools["telegram_send_message"].fn(chat_id="123", text="x")
                check("ошибка API транслируется в ToolError", False)
            except ToolError:
                check("ошибка API транслируется в ToolError", True)
            _BotAPIHandler.fail_next = False
    finally:
        srv.shutdown()


def test_telegram_negative_without_token() -> None:
    section("Telegram: отказ без токена")
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(Path(td) / "ws")
        tools = {t.name: t for t in msg_mod.build_telegram_tools(
            ws, msg_mod.TelegramConfig(), confirm_sends=False, confirm=None)}
        try:
            tools["telegram_send_message"].fn(chat_id="1", text="x")
            check("отказ без токена", False)
        except ToolError:
            check("отказ без токена", True)


def test_telegram_confirmation() -> None:
    section("Telegram: подтверждение оператора перед отправкой")
    srv, port = _start_bot_api()
    try:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(Path(td) / "ws")
            cfg = msg_mod.TelegramConfig(
                bot_token="T", api_base=f"http://127.0.0.1:{port}", rate_limit=0.0)
            tools = {t.name: t for t in msg_mod.build_telegram_tools(
                ws, cfg, confirm_sends=True, confirm=lambda d, a: False)}
            try:
                tools["telegram_send_message"].fn(chat_id="1", text="x")
                check("отклонено оператором, запрос не ушёл", False)
            except ToolError:
                check("отклонено оператором, запрос не ушёл",
                      len(_BotAPIHandler.calls) == 0)
    finally:
        srv.shutdown()


def test_max_send_and_updates() -> None:
    section("MAX: отправка сообщения, получение обновлений")
    srv, port = _start_bot_api()
    try:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(Path(td) / "ws")
            cfg = msg_mod.MaxConfig(
                bot_token="max-token", api_base=f"http://127.0.0.1:{port}",
                rate_limit=0.0)
            tools = {t.name: t for t in msg_mod.build_max_tools(
                ws, cfg, confirm_sends=False, confirm=None)}

            out = tools["max_send_message"].fn(text="Привет", user_id="777")
            check("сообщение отправлено", "message_id=m-1" in out, out)
            call = next(c for c in _BotAPIHandler.calls if "/messages" in c["path"])
            check("токен передан в заголовке Authorization",
                  call["headers"].get("Authorization") == "max-token")
            check("user_id передан параметром", "user_id=777" in call["path"])

            try:
                tools["max_send_message"].fn(text="без адресата")
                check("отказ без user_id и chat_id", False)
            except ToolError:
                check("отказ без user_id и chat_id", True)

            out2 = tools["max_get_updates"].fn(limit=5)
            check("обновления получены", "привет боту max" in out2, out2)
            check("marker передан дальше", "marker: 999" in out2, out2)
    finally:
        srv.shutdown()


def test_max_negative_without_token() -> None:
    section("MAX: отказ без токена")
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(Path(td) / "ws")
        tools = {t.name: t for t in msg_mod.build_max_tools(
            ws, msg_mod.MaxConfig(), confirm_sends=False, confirm=None)}
        try:
            tools["max_send_message"].fn(text="x", user_id="1")
            check("отказ без токена", False)
        except ToolError:
            check("отказ без токена", True)


def test_build_only_configured_channels() -> None:
    section("build(): инструменты только для настроенных каналов")
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(Path(td) / "ws")
        cfg = msg_mod.MessagingConfig()  # ничего не настроено
        tools = msg_mod.build(ws, cfg, confirm=None)
        check("без конфигурации — ноль инструментов", tools == [], str(tools))

        cfg2 = msg_mod.MessagingConfig.from_dict({
            "telegram": {"bot_token": "x"},
        })
        tools2 = {t.name for t in msg_mod.build(ws, cfg2, confirm=None)}
        check("только telegram-инструменты",
              tools2 == {"telegram_send_message", "telegram_send_file",
                        "telegram_get_updates"}, str(tools2))


def test_messaging_config_from_dict() -> None:
    section("MessagingConfig.from_dict: разбор секции конфига")
    cfg = msg_mod.MessagingConfig.from_dict({
        "email": {"smtp_host": "smtp.example.com", "smtp_port": 465,
                  "smtp_use_ssl": True},
        "telegram": {"bot_token": "abc", "rate_limit": 2.0},
        "confirm_sends": False,
    })
    check("email-настройки разобраны", cfg.email.smtp_host == "smtp.example.com")
    check("порт разобран", cfg.email.smtp_port == 465)
    check("telegram-настройки разобраны", cfg.telegram.bot_token == "abc")
    check("confirm_sends переопределён", cfg.confirm_sends is False)
    check("max по умолчанию не настроен", cfg.max.ready() is False)

    # НЕГАТИВНЫЙ: ключи-комментарии (как в examples/config.mcp.json) не
    # должны падать с TypeError — from_dict обязан их игнорировать на
    # любом уровне вложенности
    try:
        cfg2 = msg_mod.MessagingConfig.from_dict({
            "_комментарий": "поясняющий текст прямо в конфиге",
            "email": {"smtp_host": "x", "_комментарий": "и тут тоже"},
            "telegram": {"bot_token": "t", "_note": "и тут"},
        })
        check("ключи-комментарии не ломают разбор", cfg2.email.smtp_host == "x")
    except TypeError as exc:
        check("ключи-комментарии не ломают разбор", False, str(exc))


def test_example_config_loads() -> None:
    section("examples/config.messaging.json грузится без ошибок")
    from agent.config import Config
    root = Path(__file__).resolve().parents[1]
    cfg = Config.load(str(root / "examples" / "config.messaging.json"))
    check("навык messaging указан", "messaging" in cfg.skills)
    check("email.from_addr разобран", cfg.messaging.email.from_addr == "bot@example.com")
    check("confirm_sends по умолчанию true", cfg.messaging.confirm_sends is True)


def test_build_agent_with_messaging_skill() -> None:
    section("Сборка агента с навыком messaging")
    from agent.build import build_agent
    from agent.config import Config
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(provider="ollama", model="m", workspace=td, skills=["messaging"])
        agent = build_agent(cfg)
        check("без настроенных каналов — ноль messaging-инструментов",
              agent.tools.names() == [])

        cfg2 = Config(provider="ollama", model="m", workspace=td, skills=["messaging"])
        cfg2.messaging = msg_mod.MessagingConfig.from_dict(
            {"telegram": {"bot_token": "x"}})
        agent2 = build_agent(cfg2)
        check("с токеном Telegram — инструменты появились",
              "telegram_send_message" in agent2.tools.names())


def main() -> int:
    test_email_send_negative_without_config()
    test_email_send_requires_confirmation()
    test_email_negative_without_imap()
    if HAVE_AIOSMTPD:
        test_email_send_real_smtp()
    else:
        print("aiosmtpd не установлен — тест реальной SMTP-отправки пропущен "
              "(pip install aiosmtpd); email_send всё равно использует только "
              "smtplib из stdlib.")
    test_email_list_and_read_real_imap()
    test_telegram_send_and_updates()
    test_telegram_negative_without_token()
    test_telegram_confirmation()
    test_max_send_and_updates()
    test_max_negative_without_token()
    test_build_only_configured_channels()
    test_messaging_config_from_dict()
    test_example_config_loads()
    test_build_agent_with_messaging_skill()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
