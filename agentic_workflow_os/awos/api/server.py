"""HTTP API среды на стандартной библиотеке — без внешних зависимостей.

Публичная поверхность платформы. Через неё среду встраивают в чужие
системы: запустить конвейер из CI, показать очередь согласований в
корпоративном портале, забрать результат в свою базу.

  GET  /health                        жив ли сервис (без токена)
  GET  /, /dashboard                  веб-консоль (без токена — статика)
  GET  /v1/config                     конфиг среды (секреты замаскированы)
  GET  /v1/workflows                  доступные workflow
  GET  /v1/profiles                   профили агентов
  GET  /v1/tools                      инструменты и гранты
  POST /v1/runs                       запустить: {workflow, goal, inputs}
  GET  /v1/runs                       список прогонов
  GET  /v1/runs/<id>                  состояние прогона целиком
  GET  /v1/runs/<id>/events?after=N   журнал (для «живого» дашборда)
  GET  /v1/runs/<id>/context[?key=]   доска: срез или история ключа
  POST /v1/runs/<id>/resume           продолжить прогон
  POST /v1/runs/<id>/cancel           отменить прогон
  GET  /v1/checkpoints[?run_id=]      очередь на согласование
  POST /v1/checkpoints/<id>           {status, response} — ответ человека
  GET  /v1/stats                      сводка по среде

ПОЧЕМУ ЗАПУСК ПРОГОНА — СИНХРОННЫЙ ВЫЗОВ, А НЕ ОЧЕРЕДЬ ЗАДАЧ. Прогон
всё равно останавливается сам, когда нужен человек: HTTP-ответ приходит
либо с готовым результатом, либо со статусом waiting_human и номером
точки контроля. Отдельный воркер понадобился бы только для прогонов
длиннее HTTP-таймаута — и тогда это ответственность вызывающей стороны
(запустить в фоне и опрашивать /v1/runs/<id>), а не повод тащить в среду
брокер сообщений.

ТОКЕН. Если задан AWOS_API_TOKEN, все маршруты кроме /health и статики
дашборда требуют `Authorization: Bearer <token>`. Слушать не-localhost
без токена среда отказывается ещё на этапе конфига (см. config.py):
здесь исполняется чужой текст и вызываются инструменты, открытый порт
означает отданную машину.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..config import Config
from ..kernel.engine import Engine, EngineError
from ..kernel.store import Store, StoreError
from ..kernel.workflow import WorkflowError, describe_workflows
from ..roles.profile import describe_profiles
from ..tools.registry import build_registry, granted_summary

MAX_BODY = 2_000_000

_RUN_RE = re.compile(r"^/v1/runs/(\d+)(?:/(events|context|resume|cancel))?$")
_CP_RE = re.compile(r"^/v1/checkpoints/(\d+)$")


class Handler(BaseHTTPRequestHandler):
    cfg: Config = Config()
    token: str = ""
    server_version = "AWOS/1.0"

    #: Один Engine на процесс: он держит кэш моделей и рабочую папку.
    #: Store внутри потокобезопасен на уровне SQLite (короткие транзакции).
    _engine: Engine | None = None
    _lock = threading.Lock()

    # --- служебное --------------------------------------------------------
    @classmethod
    def engine(cls) -> Engine:
        with cls._lock:
            if cls._engine is None:
                cls._engine = Engine(cls.cfg)
            return cls._engine

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[awos] {self.address_string()} {fmt % args}")

    def _send(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2,
                          default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path: Path) -> None:
        if not path.exists():
            self._send(404, {"error": "страница не найдена"})
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not self.token:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {self.token}"

    def _body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if length <= 0:
            return {}
        if length > MAX_BODY:
            return None
        try:
            raw = self.rfile.read(length).decode("utf-8")
            data = json.loads(raw) if raw.strip() else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _store(self) -> Store:
        return self.engine().store

    # --- GET ---------------------------------------------------------------
    def do_GET(self) -> None:                                    # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)

        if path == "/health":
            self._send(200, {"status": "ok", "service": "awos",
                             "version": _version()})
            return
        if path in ("/", "/dashboard"):
            self._send_html(Path(__file__).resolve().parent / "web" / "console.html")
            return
        if not self._authorized():
            self._send(401, {"error": "нужен заголовок Authorization: Bearer <token>"})
            return
        try:
            self._route_get(path, qs)
        except StoreError as exc:
            self._send(404, {"error": str(exc)})
        except (WorkflowError, EngineError, ValueError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:                                 # noqa: BLE001
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _route_get(self, path: str, qs: dict[str, list[str]]) -> None:
        def qi(name: str, default: int) -> int:
            try:
                return int(qs.get(name, [default])[0])
            except (ValueError, IndexError):
                return default

        if path == "/v1/config":
            self._send(200, {"config": self.cfg.to_dict()})
            return
        if path == "/v1/workflows":
            self._send(200, {"workflows": describe_workflows(
                self.cfg.resolved_workflows_dir())})
            return
        if path == "/v1/profiles":
            self._send(200, {"profiles": describe_profiles(
                self.cfg.resolved_profiles_dir())})
            return
        if path == "/v1/tools":
            reg = build_registry(self.cfg)
            self._send(200, {"tools": reg.to_list(),
                             "grants": granted_summary(self.cfg)})
            return
        if path == "/v1/stats":
            self._send(200, self._store().stats())
            return
        if path == "/v1/runs":
            self._send(200, {"runs": self._store().list_runs(
                limit=qi("limit", 50), status=qs.get("status", [""])[0])})
            return
        if path == "/v1/checkpoints":
            run_id = qs.get("run_id", [""])[0]
            self._send(200, {"checkpoints": self._store().list_checkpoints(
                int(run_id) if run_id else None,
                status=qs.get("status", ["pending"])[0])})
            return

        m = _RUN_RE.match(path)
        if m:
            run_id, tail = int(m.group(1)), m.group(2)
            if tail is None:
                self._send(200, self.engine().status(run_id))
                return
            if tail == "events":
                self._send(200, {"events": self._store().events(
                    run_id, after_id=qi("after", 0), limit=qi("limit", 300))})
                return
            if tail == "context":
                key = qs.get("key", [""])[0]
                if key:
                    self._send(200, {"key": key,
                                     "history": self._store().ctx_history(run_id, key)})
                else:
                    self._send(200, {"context": self._store().ctx_all(run_id)})
                return

        self._send(404, {"error": f"нет маршрута GET {path}"})

    # --- POST --------------------------------------------------------------
    def do_POST(self) -> None:                                   # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if not self._authorized():
            self._send(401, {"error": "нужен заголовок Authorization: Bearer <token>"})
            return
        body = self._body()
        if body is None:
            self._send(400, {"error": "тело запроса должно быть JSON-объектом"})
            return
        try:
            self._route_post(path, body)
        except StoreError as exc:
            self._send(404, {"error": str(exc)})
        except (WorkflowError, EngineError, ValueError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:                                 # noqa: BLE001
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _route_post(self, path: str, body: dict[str, Any]) -> None:
        if path == "/v1/runs":
            workflow = str(body.get("workflow", "") or "").strip()
            if not workflow:
                self._send(400, {"error": "нужно поле 'workflow'"})
                return
            inputs = body.get("inputs") or {}
            if not isinstance(inputs, dict):
                self._send(400, {"error": "'inputs' должен быть объектом"})
                return
            outcome = self.engine().start(workflow,
                                          goal=str(body.get("goal", "") or ""),
                                          inputs=inputs)
            self._send(200, outcome.to_dict())
            return

        m = _RUN_RE.match(path)
        if m:
            run_id, tail = int(m.group(1)), m.group(2)
            if tail == "resume":
                self._send(200, self.engine().resume(run_id).to_dict())
                return
            if tail == "cancel":
                self.engine().cancel(run_id,
                                     str(body.get("reason", "") or "отменён через API"))
                self._send(200, {"run_id": run_id, "status": "cancelled"})
                return

        m = _CP_RE.match(path)
        if m:
            cp_id = int(m.group(1))
            status = str(body.get("status", "") or "").strip()
            if status not in ("approved", "rejected", "edited", "cancelled"):
                self._send(400, {"error": "status: approved | rejected | edited "
                                          "| cancelled"})
                return
            outcome = self.engine().respond(
                cp_id, status, str(body.get("response", "") or ""),
                actor=str(body.get("actor", "") or "api"))
            self._send(200, outcome.to_dict())
            return

        self._send(404, {"error": f"нет маршрута POST {path}"})


def _version() -> str:
    from .. import __version__
    return __version__


def serve(cfg: Config | None = None) -> int:
    cfg = cfg or Config.load()
    Handler.cfg = cfg
    Handler.token = cfg.api_token
    Handler._engine = None
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), Handler)
    scheme = f"http://{cfg.host}:{cfg.port}"
    print(cfg.describe())
    print(f"\nAWOS слушает {scheme}")
    print(f"  консоль:  {scheme}/dashboard")
    print(f"  здоровье: {scheme}/health")
    if not cfg.api_token:
        print("  токен не задан — доступ только с localhost")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено. Прогоны в статусе waiting_human сохранены в базе.")
    finally:
        httpd.server_close()
    return 0


def main() -> int:
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
