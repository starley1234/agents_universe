"""HTTP API на стандартной библиотеке — без внешних зависимостей.

Эндпоинты:
  GET  /health          — жив ли сервис
  GET  /info            — конфигурация и список инструментов
  POST /run             — {"task": "...", "workspace": "..."} -> результат
  POST /run/stream      — то же, но события построчно (NDJSON)
  POST /dispatch        — какой профиль возьмёт задачу и почему
  GET  /questions       — вопросы агента, ждущие ответа
  POST /answer          — {"id": 1, "text": "…"} — ответить агенту
  GET  /runs            — история прогонов
  GET  /runs/N          — прогон целиком: план, расход, журнал
  POST /plan            — {"run_id": N, "task_id": M, "status": "done"}

Токен: если задан AGENT_API_TOKEN, требуется заголовок
Authorization: Bearer <token>. Без него сервер слушает только localhost.
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
from .dispatch import choose_profile
from .store import Store
from .webio import QuestionBox

MAX_BODY = 1_000_000


class Handler(BaseHTTPRequestHandler):
    cfg: Config
    token: str | None = None
    # Ящик вопросов общий на весь сервер. Значение по умолчанию нужно
    # тем, кто поднимает Handler напрямую, минуя serve(): без него
    # первый же запрос падал с AttributeError. Поймано сквозным тестом.
    box: QuestionBox = QuestionBox()
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
        if self.path == "/questions":
            self._send(200, {"questions": self.box.pending()})
            return
        if self.path == "/runs" or self.path.startswith("/runs/"):
            self._runs(self.path)
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
        if not self._authorized():
            self._send(401, {"error": "нужен заголовок Authorization: Bearer <token>"})
            return
        if self.path not in ("/run", "/run/stream", "/answer", "/plan",
                             "/dispatch"):
            self._send(404, {"error": f"нет маршрута {self.path}"})
            return

        data = self._read_json()
        if data is None:
            self._send(400, {"error": "ожидается JSON в теле запроса"})
            return

        if self.path == "/answer":
            self._answer(data)
            return
        if self.path == "/plan":
            self._plan(data)
            return
        if self.path == "/dispatch":
            self._dispatch(data)
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

        if self.path == "/run":
            self._run_plain(cfg, task)
        else:
            self._run_stream(cfg, task)

    def _answer(self, data: dict[str, Any]) -> None:
        try:
            qid = int(data.get("id", 0))
        except (TypeError, ValueError):
            self._send(400, {"error": "поле 'id' должно быть числом"})
            return
        text = str(data.get("text") or "")
        if data.get("skip"):
            ok = self.box.drop(qid)
        else:
            ok = self.box.answer(qid, text)
        if not ok:
            # Вопрос мог истечь по таймауту, пока человек печатал.
            # Молчать нельзя: иначе кажется, что ответ доставлен.
            self._send(404, {"error": f"вопрос #{qid} уже неактуален "
                                      "(истёк таймаут или снят)"})
            return
        self._send(200, {"status": "ok", "id": qid})

    def _dispatch(self, data: dict[str, Any]) -> None:
        """Кто возьмёт задачу. Отдельно от запуска: человек видит выбор
        ДО начала работы и может его заменить."""
        task = (data.get("task") or "").strip()
        if not task:
            self._send(400, {"error": "поле 'task' обязательно"})
            return
        pick = choose_profile(task, Config.list_profiles())
        self._send(200, {"profile": pick.profile, "reason": pick.reason,
                         "autonomous": pick.autonomous,
                         "explain": pick.explain(),
                         "runners_up": pick.runners_up or []})

    def _runs(self, path: str) -> None:
        store = Store(self.cfg.db)
        try:
            tail = path[len("/runs"):].strip("/")
            if not tail:
                self._send(200, {"runs": store.runs(30)})
                return
            try:
                rid = int(tail)
            except ValueError:
                self._send(400, {"error": f"неверный номер прогона {tail!r}"})
                return
            row = store.get_run(rid)
            if not row:
                self._send(404, {"error": f"прогона #{rid} нет"})
                return
            self._send(200, {"run": row, "tasks": store.tasks(rid),
                             "events": store.run_events(rid, limit=200)})
        finally:
            store.close()

    def _plan(self, data: dict[str, Any]) -> None:
        """Правка плана человеком: закрыть, провалить, снять пункт."""
        try:
            task_id = int(data.get("task_id", 0))
        except (TypeError, ValueError):
            self._send(400, {"error": "поле 'task_id' должно быть числом"})
            return
        status = str(data.get("status") or "").strip()
        allowed = ("done", "failed", "skipped", "open")
        if status not in allowed:
            self._send(400, {"error": f"status должен быть одним из "
                                      f"{', '.join(allowed)}"})
            return
        store = Store(self.cfg.db)
        try:
            store.set_task(task_id, status, str(data.get("result") or ""))
            self._send(200, {"status": "ok", "task_id": task_id,
                             "new_status": status})
        finally:
            store.close()

    def _run_plain(self, cfg: Config, task: str) -> None:
        try:
            # ask=None намеренно: у /run нет потока событий, показать
            # вопрос некому. Агент не станет ждать пустоту — инструмент
            # ask_user сам пометит пункт как заблокированный и пойдёт
            # дальше. Ждать 10 минут в тишине было бы хуже всего.
            agent = build_agent(cfg)
            res = agent.run(task)
        except Exception as exc:
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
            return
        self._send(200, {
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
        })

    def _run_stream(self, cfg: Config, task: str) -> None:
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

        try:
            # Вопрос агента уходит в поток событий, ответ приходит
            # отдельным запросом POST /answer.
            def ask(question: str, options: list[str]) -> str:
                return self.box.ask(question, options)

            self.box.on_new = lambda q: emit("ask", q.as_dict())
            agent = build_agent(cfg, on_event=emit, ask=ask)
            res = agent.run(task)
            emit("done", {"answer": res.answer, "stopped_by": res.stopped_by,
                          "steps": len(res.steps)})
        except Exception as exc:
            emit("error", {"message": f"{type(exc).__name__}: {exc}"})


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8080,
          token: str | None = None) -> None:
    Handler.cfg = cfg
    Handler.token = token or os.getenv("AGENT_API_TOKEN")
    Handler.box = QuestionBox()
    # HTTP-заголовки допускают только latin-1. Токен с кириллицей клиент
    # физически не сможет отправить — узнать об этом лучше при старте,
    # а не по непонятной ошибке кодировки у пользователя.
    if Handler.token:
        try:
            Handler.token.encode("latin-1")
        except UnicodeEncodeError:
            raise SystemExit(
                "Отказ: токен содержит символы вне latin-1 (например, "
                "кириллицу). HTTP-заголовок такой токен не передаст — "
                "используйте латиницу, цифры и дефис.")
    if host not in ("127.0.0.1", "localhost") and not Handler.token:
        raise SystemExit(
            "Отказ: сервер открыт наружу без токена. Задайте AGENT_API_TOKEN "
            "или слушайте 127.0.0.1."
        )
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"Веб-интерфейс и API: http://{host}:{port}/  "
          f"(токен: {'да' if Handler.token else 'нет, только localhost'})")
    print(f"модель: {cfg.provider}/{cfg.model} · песочница: {cfg.sandbox.mode}")
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
