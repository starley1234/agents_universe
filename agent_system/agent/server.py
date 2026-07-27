"""HTTP API на стандартной библиотеке — без внешних зависимостей.

Эндпоинты:
  GET  /health          — жив ли сервис
  GET  /info            — конфигурация и список инструментов
  POST /run             — {"task": "...", "workspace": "..."} -> результат
  POST /run/stream      — то же, но события построчно (NDJSON)
  POST /webhook/telegram — приём входящих сообщений Telegram (см. ниже)
  POST /webhook/max      — приём входящих сообщений MAX (см. ниже)

Токен: если задан AGENT_API_TOKEN, требуется заголовок
Authorization: Bearer <token>. Без него сервер слушает только localhost.

Вебхуки /webhook/* — ОТДЕЛЬНАЯ модель доверия, не токен API: платформа
(Telegram/MAX) не умеет присылать Authorization: Bearer, зато подписывает
запрос своим секретом в отдельном заголовке (см. agent/webhooks.py:
verify_telegram_secret/verify_max_secret). Маршрут появляется, только
если соответствующий webhook_secret задан в конфиге — без секрета мы не
можем отличить платформу от произвольного POST из интернета, поэтому
такой маршрут просто не регистрируется (см. serve()).
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .build import build_agent
from .config import Config
from .router import route_and_apply
from . import webhooks

MAX_BODY = 1_000_000




class Handler(BaseHTTPRequestHandler):
    cfg: Config
    token: str | None = None
    server_version = "AgentAPI/1.0"

    # --- служебное ----------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[api] {self.address_string()} {fmt % args}")

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not self.token:
            return True
        got = self.headers.get("Authorization", "")
        return got == f"Bearer {self.token}"

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _cfg_for(self, data: dict[str, Any]) -> Config:
        """Позволяем переопределить рабочую папку и модель на запрос."""
        cfg = Config(**{**self.cfg.__dict__})
        cfg.sandbox = self.cfg.sandbox
        if data.get("profile"):
            cfg.apply_profile(data["profile"])
        for key in ("workspace", "model", "provider", "max_steps"):
            if data.get(key):
                setattr(cfg, key, data[key])
        if data.get("skills"):
            cfg.skills = list(data["skills"])
        return cfg

    # --- маршруты -----------------------------------------------------
    def _send_html(self) -> None:
        f = Path(__file__).resolve().parent / "web" / "index.html"
        if not f.exists():
            self._send(404, {"error": "веб-интерфейс не найден"})
            return
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send(200, {"status": "ok"})
            return
        if self.path in ("/", "/index.html"):
            # Саму страницу отдаём без токена: она пустая оболочка,
            # все данные всё равно требуют Authorization.
            self._send_html()
            return
        if not self._authorized():
            self._send(401, {"error": "нужен заголовок Authorization: Bearer <token>"})
            return
        if self.path == "/info":
            try:
                agent = build_agent(self.cfg)
                tools = agent.tools.names()
            except Exception as exc:
                self._send(500, {"error": str(exc)})
                return
            self._send(200, {"config": self.cfg.to_dict(), "tools": tools,
                             "profiles": Config.list_profiles()})
            return
        self._send(404, {"error": f"нет маршрута {self.path}"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/webhook/telegram":
            self._webhook_telegram()
            return
        if self.path == "/webhook/max":
            self._webhook_max()
            return
        if not self._authorized():
            self._send(401, {"error": "нужен заголовок Authorization: Bearer <token>"})
            return
        if self.path not in ("/run", "/run/stream"):
            self._send(404, {"error": f"нет маршрута {self.path}"})
            return

        data = self._read_json()
        if data is None:
            self._send(400, {"error": "ожидается JSON в теле запроса"})
            return
        task = (data.get("task") or "").strip()
        if not task:
            self._send(400, {"error": "поле 'task' обязательно"})
            return

        try:
            cfg = self._cfg_for(data)
        except Exception as exc:
            self._send(400, {"error": f"неверные параметры: {exc}"})
            return

        route_note = None
        if data.get("auto_route") and not data.get("profile"):
            # роутинг только если профиль не задан явно в запросе — тот
            # же принцип, что и в CLI (--auto-route несовместим с -P)
            try:
                decision = route_and_apply(cfg, task)
                route_note = (f"роль подобрана автоматически: "
                             f"{decision.profile} ({decision.method}; "
                             f"{decision.reason})")
            except Exception as exc:
                self._send(400, {"error": f"не удалось подобрать роль: {exc}"})
                return

        if self.path == "/run":
            self._run_plain(cfg, task, route_note)
        else:
            self._run_stream(cfg, task, route_note)

    # --- вебхуки: приём входящих сообщений/вложений от платформ ------
    def _webhook_telegram(self) -> None:
        tg_cfg = self.cfg.messaging.telegram
        if not tg_cfg.webhook_secret:
            # Маршрут в принципе не должен был активироваться (см. serve()),
            # но если конфиг подменили между стартом и запросом — отказ,
            # а не тихий приём непроверяемых сообщений.
            self._send(404, {"error": f"нет маршрута {self.path}"})
            return
        if not webhooks.verify_telegram_secret(self.headers, tg_cfg):
            self._send(401, {"error": "неверный X-Telegram-Bot-Api-Secret-Token"})
            return
        data = self._read_json()
        if data is None:
            self._send(400, {"error": "ожидается JSON в теле запроса"})
            return
        # Отвечаем 200 немедленно: сама обработка (скачивание вложения,
        # прогон агента) уходит в фон — см. шапку модуля webhooks.py про
        # требования Telegram/MAX к времени ответа.
        webhooks.dispatch_telegram(self.cfg, data)
        self._send(200, {"ok": True})

    def _webhook_max(self) -> None:
        max_cfg = self.cfg.messaging.max
        if not max_cfg.webhook_secret:
            self._send(404, {"error": f"нет маршрута {self.path}"})
            return
        if not webhooks.verify_max_secret(self.headers, max_cfg):
            self._send(401, {"error": "неверный X-Max-Bot-Api-Secret"})
            return
        data = self._read_json()
        if data is None:
            self._send(400, {"error": "ожидается JSON в теле запроса"})
            return
        webhooks.dispatch_max(self.cfg, data)
        self._send(200, {"ok": True})

    def _run_plain(self, cfg: Config, task: str, route_note: str | None = None) -> None:
        try:
            agent = build_agent(cfg)
            res = agent.run(task)
        except Exception as exc:
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
            return
        payload = {
            "answer": res.answer,
            "stopped_by": res.stopped_by,
            "steps": len(res.steps),
            "tool_calls": res.tool_calls,
            "trace": [{"step": s.n,
                       "text": s.text,
                       "calls": [{"name": c["name"], "args": c["args"],
                                  "result": c["result"][:4000]}
                                 for c in s.calls]}
                      for s in res.steps],
        }
        if route_note:
            payload["profile"] = cfg.profile
            payload["route"] = route_note
        self._send(200, payload)


    def _run_stream(self, cfg: Config, task: str, route_note: str | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        lock = threading.Lock()

        def emit(kind: str, data: dict[str, Any]) -> None:
            payload = {"event": kind, **{
                k: (v[:2000] if isinstance(v, str) else v) for k, v in data.items()
            }}
            line = json.dumps(payload, ensure_ascii=False) + "\n"
            with lock:
                try:
                    self.wfile.write(line.encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        if route_note:
            emit("route", {"profile": cfg.profile, "message": route_note})
        try:
            agent = build_agent(cfg, on_event=emit)
            res = agent.run(task)
            emit("done", {"answer": res.answer, "stopped_by": res.stopped_by,
                          "steps": len(res.steps)})
        except Exception as exc:
            emit("error", {"message": f"{type(exc).__name__}: {exc}"})



def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8080,
          token: str | None = None) -> None:
    Handler.cfg = cfg
    Handler.token = token or os.getenv("AGENT_API_TOKEN")
    if host not in ("127.0.0.1", "localhost") and not Handler.token:
        raise SystemExit(
            "Отказ: сервер открыт наружу без токена. Задайте AGENT_API_TOKEN "
            "или слушайте 127.0.0.1."
        )
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"Веб-интерфейс и API: http://{host}:{port}/  "
          f"(токен: {'да' if Handler.token else 'нет, только localhost'})")
    print(f"модель: {cfg.provider}/{cfg.model} · песочница: {cfg.sandbox.mode}")
    if cfg.messaging.telegram.webhook_secret:
        print(f"webhook Telegram: POST http://{host}:{port}/webhook/telegram "
              f"(зарегистрируйте адрес через webhooks.register_telegram_webhook)")
    if cfg.messaging.max.webhook_secret:
        print(f"webhook MAX: POST http://{host}:{port}/webhook/max "
              f"(зарегистрируйте адрес через webhooks.register_max_webhook)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлен")
    finally:
        srv.server_close()


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="agent-server", description="HTTP API агента")
    ap.add_argument("-c", "--config")
    ap.add_argument("--host", default=os.getenv("AGENT_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.getenv("AGENT_PORT", "8080")))
    ap.add_argument("--token", default=None)
    ap.add_argument("-p", "--provider")
    ap.add_argument("-m", "--model")
    ap.add_argument("-w", "--workspace")
    ap.add_argument("--sandbox", dest="sandbox_mode",
                    choices=["auto", "docker", "confirm", "off"])
    args = ap.parse_args(argv)
    cfg = Config.load(args.config, provider=args.provider, model=args.model,
                      workspace=args.workspace, sandbox_mode=args.sandbox_mode)
    serve(cfg, args.host, args.port, args.token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
