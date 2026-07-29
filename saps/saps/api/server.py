"""HTTP API САПС на стандартной библиотеке (ТЗ п.3.3, п.6).

Через этот слой работает рабочее место инженера (веб-консоль) и через
него же САПС встраивается в чужие системы.

  GET  /health                         жив ли сервис (без токена)
  GET  /, /dashboard                   рабочее место инженера
  GET  /v1/config                      конфиг (секреты замаскированы)
  GET  /v1/stats                       сводка по базе
  GET  /v1/nodes                       узлы изделия
  GET  /v1/health/nodes                индикатор здоровья по узлам
  GET  /v1/health?node=&owner=         индикатор здоровья по срезу
  GET  /v1/requirements?owner=&node=&status=&q=
  GET  /v1/requirements/<id>           карточка: связи, MoC, история
  POST /v1/requirements/<id>           правка инженером {text,status,owner}
  GET  /v1/suggestions?status=         очередь предложений агентов
  POST /v1/suggestions/<id>            {decision: accepted|rejected}
  GET  /v1/documents                   импортированные документы
  GET  /v1/staging?document_id=        сырые записи (мастер подготовки)
  POST /v1/staging/promote             {ids:[...]} перенос в production
  GET  /v1/clauses?ruleset=            справочник авиационных правил
  POST /v1/agents/<name>/run           запуск агента (editor|classifier|gap)
  GET  /v1/plugins                     список плагинов
  POST /v1/plugins/<name>/run          запуск плагина
  POST /v1/export                      {format, node, owner} -> файл
  GET  /v1/audit?object_type=&object_id=   журнал

ПОЧЕМУ ПРАВКА ТРЕБОВАНИЯ — ОТДЕЛЬНЫЙ МАРШРУТ, А НЕ PATCH НА ПОЛЕ.
Любое изменение обязано попасть в историю ревизий с указанием автора и
причины (см. Store.update_requirement). Отдельный маршрут делает это
невозможным обойти: нет способа «просто записать поле».

ТОКЕН. Если задан SAPS_API_TOKEN, все маршруты кроме /health и статики
требуют Authorization: Bearer. Слушать не-localhost без токена
конфигурация запрещает: система отдаёт данные сертификации и умеет
писать в промышленный PDM.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..agents import ClassifierAgent, EditorAgent, GapAgent
from ..agents.classifier import index_clauses, index_requirements
from ..config import Config, ConfigError
from ..db.schema import MOC_CODES
from ..db.store import Store, StoreError
from ..export.reports import (compliance_docx, compliance_xlsx, export_path,
                              requirements_xlsx)
from ..ingest.pipeline import promote
from ..llm import build_embedder, build_llm
from ..plugins import base as plugins
from ..rules.loader import RulesError, list_builtin, load_builtin

MAX_BODY = 5_000_000

_REQ_RE = re.compile(r"^/v1/requirements/(\d+)$")
_SUG_RE = re.compile(r"^/v1/suggestions/(\d+)$")
_AGENT_RE = re.compile(r"^/v1/agents/([a-z_]+)/run$")
_PLUGIN_RE = re.compile(r"^/v1/plugins/([a-z_]+)/run$")


class Handler(BaseHTTPRequestHandler):
    cfg: Config = Config()
    token: str = ""
    server_version = "SAPS/1.0"

    _store: Store | None = None
    _lock = threading.Lock()

    # --- инфраструктура ---------------------------------------------------
    @classmethod
    def store(cls) -> Store:
        """Одно соединение на процесс.

        SQL-запросы короткие, а psycopg сериализует доступ к соединению;
        для рабочего места КБ (единицы одновременных пользователей) это
        проще и надёжнее пула, который пришлось бы конфигурировать.
        """
        with cls._lock:
            if cls._store is None:
                cls._store = Store(cls.cfg.require_dsn(),
                                   schema=cls.cfg.db_schema,
                                   dim=cls.cfg.embedding_dim)
            return cls._store

    @classmethod
    def embedder(cls):
        return build_embedder(cls.cfg.embedding_provider, cls.cfg.embedding_model,
                              dim=cls.cfg.embedding_dim,
                              base_url=cls.cfg.embedding_base_url,
                              api_key=cls.cfg.embedding_api_key,
                              timeout=cls.cfg.embedding_timeout)

    @classmethod
    def llm(cls):
        kwargs: dict[str, Any] = {"retries": cls.cfg.llm_retries}
        if cls.cfg.llm_provider not in ("none", "stub"):
            kwargs.update(base_url=cls.cfg.llm_base_url,
                          api_key=cls.cfg.llm_api_key,
                          timeout=cls.cfg.llm_timeout,
                          temperature=cls.cfg.llm_temperature)
        return build_llm(cls.cfg.llm_provider, cls.cfg.llm_model, **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[saps] {self.address_string()} {fmt % args}")

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

    @staticmethod
    def _actor(data: dict[str, Any]) -> str:
        return str(data.get("actor") or "web")

    # --- GET ---------------------------------------------------------------
    def do_GET(self) -> None:                                    # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

        if path == "/health":
            self._send(200, {"status": "ok", "service": "saps",
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
        except (ConfigError, RulesError, ValueError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:                                 # noqa: BLE001
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _route_get(self, path: str, qs: dict[str, str]) -> None:
        st = self.store()

        if path == "/v1/config":
            self._send(200, {"config": self.cfg.to_dict(),
                             "moc_codes": MOC_CODES,
                             "rulesets": list_builtin()})
            return
        if path == "/v1/stats":
            self._send(200, st.stats())
            return
        if path == "/v1/nodes":
            self._send(200, {"nodes": st.list_nodes()})
            return
        if path == "/v1/health":
            agent = GapAgent(self.cfg, st)
            self._send(200, agent.health(node_code=qs.get("node", ""),
                                         owner=qs.get("owner", "")))
            return
        if path == "/v1/health/nodes":
            self._send(200, {"nodes": GapAgent(self.cfg, st).health_by_node()})
            return
        if path == "/v1/requirements":
            limit = _int(qs.get("limit"), 200)
            rows = st.list_requirements(owner=qs.get("owner", ""),
                                        node_code=qs.get("node", ""),
                                        status=qs.get("status", ""),
                                        query=qs.get("q", ""),
                                        limit=limit,
                                        offset=_int(qs.get("offset"), 0))
            self._send(200, {"requirements": rows, "count": len(rows)})
            return
        if path == "/v1/coverage":
            self._send(200, {"coverage": st.coverage(
                node_code=qs.get("node", ""), owner=qs.get("owner", ""))})
            return
        if path == "/v1/suggestions":
            self._send(200, {"suggestions": st.list_suggestions(
                status=qs.get("status", "pending"),
                agent=qs.get("agent", ""),
                req_id=_int_or_none(qs.get("requirement_id")),
                limit=_int(qs.get("limit"), 200))})
            return
        if path == "/v1/documents":
            self._send(200, {"documents": st.list_documents()})
            return
        if path == "/v1/staging":
            self._send(200, {"records": st.staging_records(
                document_id=_int_or_none(qs.get("document_id")),
                status=qs.get("status", ""),
                limit=_int(qs.get("limit"), 500))})
            return
        if path == "/v1/clauses":
            self._send(200, {"clauses": st.list_clauses(
                ruleset=qs.get("ruleset", ""), limit=_int(qs.get("limit"), 1000))})
            return
        if path == "/v1/plugins":
            self._send(200, {"plugins": plugins.describe_all(self.cfg, st)})
            return
        if path == "/v1/audit":
            self._send(200, {"audit": st.audit(
                object_type=qs.get("object_type", ""),
                object_id=_int_or_none(qs.get("object_id")),
                limit=_int(qs.get("limit"), 200))})
            return

        m = _REQ_RE.match(path)
        if m:
            req_id = int(m.group(1))
            req = st.get_requirement(req_id)
            if req is None:
                self._send(404, {"error": f"Требование #{req_id} не найдено"})
                return
            self._send(200, {
                "requirement": req,
                "links": st.requirement_links(req_id),
                "compliance": st.compliance_items(req_id),
                "revisions": st.revisions(req_id),
                "suggestions": st.list_suggestions(req_id=req_id, status=""),
                "audit": st.audit(object_type="requirement", object_id=req_id),
            })
            return

        self._send(404, {"error": f"нет маршрута GET {path}"})

    # --- POST --------------------------------------------------------------
    def do_POST(self) -> None:                                   # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if not self._authorized():
            self._send(401, {"error": "нужен заголовок Authorization: Bearer <token>"})
            return
        data = self._body()
        if data is None:
            self._send(400, {"error": "тело запроса должно быть JSON-объектом"})
            return
        try:
            self._route_post(path, data)
        except StoreError as exc:
            self._send(400, {"error": str(exc)})
        except (ConfigError, RulesError, ValueError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:                                 # noqa: BLE001
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _route_post(self, path: str, data: dict[str, Any]) -> None:
        st = self.store()

        m = _REQ_RE.match(path)
        if m:
            req_id = int(m.group(1))
            version = st.update_requirement(
                req_id,
                text=data.get("text"), status=data.get("status"),
                owner=data.get("owner"), title=data.get("title"),
                node_code=data.get("node_code"),
                reason=str(data.get("reason") or "правка через веб-интерфейс"),
                actor=self._actor(data))
            self._send(200, {"requirement": st.get_requirement(req_id),
                             "version": version})
            return

        m = _SUG_RE.match(path)
        if m:
            decision = str(data.get("decision", "")).strip()
            if decision not in ("accepted", "rejected"):
                self._send(400, {"error": "decision: accepted или rejected"})
                return
            result = st.decide_suggestion(int(m.group(1)), decision,
                                          self._actor(data))
            self._send(200, result)
            return

        if path == "/v1/staging/promote":
            ids = data.get("ids")
            if not isinstance(ids, list) or not ids:
                self._send(400, {"error": "нужен непустой список 'ids'"})
                return
            result = promote(st, [int(i) for i in ids],
                             actor=self._actor(data),
                             default_owner=str(data.get("owner", "")),
                             default_node=str(data.get("node", "")),
                             on_conflict=str(data.get("on_conflict", "skip")),
                             embedder=self.embedder().embed_one)
            self._send(200, result.to_dict())
            return

        if path == "/v1/rules/load":
            result = load_builtin(st, str(data.get("ruleset", "")),
                                  embedder=self.embedder())
            self._send(200, {"loaded": result})
            return

        if path == "/v1/index":
            emb = self.embedder()
            self._send(200, {
                "clauses": index_clauses(st, emb),
                "requirements": index_requirements(st, emb),
            })
            return

        m = _AGENT_RE.match(path)
        if m:
            self._send(200, self._run_agent(m.group(1), st, data))
            return

        m = _PLUGIN_RE.match(path)
        if m:
            name = m.group(1)
            try:
                plugin = plugins.create(name, self.cfg, st)
            except plugins.PluginError as exc:
                self._send(404, {"error": str(exc)})
                return
            kwargs = {k: v for k, v in data.items() if k != "actor"}
            self._send(200, plugin.run(**kwargs).to_dict())
            return

        if path == "/v1/export":
            self._send(200, self._export(st, data))
            return

        self._send(404, {"error": f"нет маршрута POST {path}"})

    # --- операции ----------------------------------------------------------
    def _run_agent(self, name: str, st: Store,
                   data: dict[str, Any]) -> dict[str, Any]:
        owner = str(data.get("owner", ""))
        node = str(data.get("node", ""))
        ids = data.get("requirement_ids")
        ids = [int(i) for i in ids] if isinstance(ids, list) else None

        if name == "editor":
            agent = EditorAgent(self.cfg, st, self.llm())
            return agent.run(requirement_ids=ids, owner=owner, node_code=node,
                             suggest_rewrite=bool(data.get("suggest_rewrite"))
                             ).to_dict()
        if name == "classifier":
            agent = ClassifierAgent(self.cfg, st, self.embedder(), self.llm())
            return agent.run(requirement_ids=ids, owner=owner, node_code=node,
                             ruleset=str(data.get("ruleset", "")),
                             use_llm=bool(data.get("use_llm"))).to_dict()
        if name == "gap":
            return GapAgent(self.cfg, st).run(owner=owner, node_code=node).to_dict()
        raise ValueError(
            f"Агент {name!r} неизвестен. Доступны: editor, classifier, gap")

    def _export(self, st: Store, data: dict[str, Any]) -> dict[str, Any]:
        fmt = str(data.get("format", "xlsx")).lower()
        node = str(data.get("node", ""))
        owner = str(data.get("owner", ""))
        workdir = Path(self.cfg.workdir)
        workdir.mkdir(parents=True, exist_ok=True)

        if fmt == "docx":
            path = compliance_docx(st, self.cfg,
                                   export_path(workdir, "Протокол_соответствия",
                                               "docx"),
                                   node_code=node, owner=owner)
        elif fmt == "xlsx":
            path = compliance_xlsx(st, export_path(workdir,
                                                   "Протокол_соответствия",
                                                   "xlsx"),
                                   node_code=node, owner=owner)
        elif fmt == "requirements":
            path = requirements_xlsx(st, export_path(workdir, "Требования",
                                                     "xlsx"),
                                     node_code=node, owner=owner,
                                     status=str(data.get("status", "")))
        else:
            raise ValueError("format: docx | xlsx | requirements")
        return {"file": str(path), "format": fmt}


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _version() -> str:
    from .. import __version__
    return __version__


def serve(cfg: Config | None = None) -> int:
    cfg = cfg or Config.load()
    cfg.require_dsn()
    Handler.cfg = cfg
    Handler.token = cfg.api_token
    Handler._store = None
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), Handler)
    url = f"http://{cfg.host}:{cfg.port}"
    print(cfg.describe())
    print(f"\nСАПС слушает {url}")
    print(f"  рабочее место: {url}/dashboard")
    print(f"  проверка:      {url}/health")
    if not cfg.api_token:
        print("  токен не задан — доступ только с localhost")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        httpd.server_close()
        if Handler._store is not None:
            Handler._store.close()
    return 0


def main() -> int:
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
