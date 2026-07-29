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
import logging
import re
import signal
import sys
import threading
import time
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

#: Момент старта процесса — для /health и метрик.
_STARTED = time.monotonic()


def _uptime() -> float:
    return time.monotonic() - _STARTED


def setup_logging(level: str = "INFO", json_format: bool = False) -> None:
    """Настроить логи процесса.

    В проде логи собирает не человек, а journald/Docker/ELK, поэтому:
    вывод в stdout (12-factor), время в каждой строке, уровень
    настраивается переменной окружения. JSON-режим — для сборщиков,
    которые парсят структуру; по умолчанию человекочитаемый текст,
    потому что первым делом в него смотрит администратор.
    """
    import logging

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    if json_format:
        class _JsonFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                payload = {
                    "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
                    "level": record.levelname,
                    "logger": record.name,
                    "message": record.getMessage(),
                }
                if record.exc_info:
                    payload["exception"] = self.formatException(record.exc_info)
                return json.dumps(payload, ensure_ascii=False)
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


log = None                      # инициализируется в serve()

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
            else:
                # БД могли перезапустить (обновление, отказ реплики).
                # psycopg сам не восстанавливается: без этой проверки
                # после планового обслуживания базы пришлось бы
                # рестартовать САПС.
                cls._store.ensure_alive()
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
        # Пробы мониторинга дёргаются каждые несколько секунд и в INFO
        # засоряют журнал так, что реальные события в нём теряются.
        message = fmt % args
        level = logging.DEBUG if any(
            p in message for p in ("/health", "/ready", "/metrics")
        ) else logging.INFO
        logging.getLogger("saps.http").log(
            level, "%s %s", self.address_string(), message)

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

    def _send_ready(self) -> None:
        """Проба готовности: база + версия схемы + pgvector."""
        try:
            health = self.store().health()
        except Exception as exc:                                 # noqa: BLE001
            self._send(503, {"status": "not_ready",
                             "error": f"{type(exc).__name__}: {exc}"})
            return
        problems: list[str] = []
        if health.get("database") != "ok":
            problems.append("база недоступна")
        if not health.get("pgvector"):
            problems.append("расширение pgvector не установлено")
        got, want = health.get("schema_version"), health.get(
            "expected_schema_version")
        if got != want:
            problems.append(
                f"версия схемы {got}, код требует {want} — выполните "
                "saps migrate")
        payload = {"status": "ready" if not problems else "not_ready",
                   "version": _version(), **health}
        if problems:
            payload["problems"] = problems
        self._send(200 if not problems else 503, payload)

    def _send_metrics(self) -> None:
        """Метрики в формате Prometheus.

        Текстовый формат вместо клиентской библиотеки: одна зависимость
        меньше, а `prometheus_client` здесь не даёт ничего сверх десятка
        строк форматирования.
        """
        try:
            stats = self.store().stats()
        except Exception as exc:                                 # noqa: BLE001
            body = (f"# СБОЙ сбора метрик: {type(exc).__name__}\n"
                    "saps_up 0\n").encode("utf-8")
            self.send_response(503)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        lines = [
            "# HELP saps_up Сервис доступен и база отвечает",
            "# TYPE saps_up gauge", "saps_up 1",
            "# HELP saps_uptime_seconds Время работы процесса",
            "# TYPE saps_uptime_seconds gauge",
            f"saps_uptime_seconds {_uptime():.1f}",
        ]
        metrics = {
            "requirements": "Всего требований",
            "requirements_approved": "Утверждённых требований",
            "clauses": "Пунктов авиационных правил в справочнике",
            "suggestions_pending": "Предложений агентов, ждущих инженера",
            "low_quality": "Требований ниже порога качества",
            "compliance_items": "Назначенных методов подтверждения",
            "evidence": "Приложенных доказательных документов",
            "documents": "Импортированных документов",
        }
        for key, help_text in metrics.items():
            name = f"saps_{key}"
            lines += [f"# HELP {name} {help_text}", f"# TYPE {name} gauge",
                      f"{name} {int(stats.get(key, 0))}"]
        body = ("\n".join(lines) + "\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
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
            # ЖИВ ЛИ ПРОЦЕСС. Намеренно НЕ трогает базу: liveness-проба
            # должна отвечать даже когда БД лежит, иначе оркестратор
            # начнёт перезапускать исправное приложение из-за чужого сбоя.
            self._send(200, {"status": "ok", "service": "saps",
                             "version": _version(),
                             "uptime_seconds": round(_uptime(), 1)})
            return
        if path == "/ready":
            # ГОТОВ ЛИ ОБСЛУЖИВАТЬ. Здесь база проверяется: инстанс без
            # рабочей БД или с несовместимой схемой обязан быть выведен
            # из ротации, а не отдавать ошибки пользователям.
            self._send_ready()
            return
        if path == "/metrics":
            self._send_metrics()
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
                             "rulesets": list_builtin(),
                             "external_embeddings":
                                 self.cfg.uses_external_embeddings(),
                             "pdf_engines": _pdf_engines()})
            return
        if path == "/v1/embeddings":
            self._send(200, self._embedding_status())
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

        if path == "/v1/load":
            # Приём файла: путь на сервере ЛИБО содержимое в base64.
            # base64 нужен, чтобы инженер мог перетащить файл в браузер,
            # не имея доступа к файловой системе сервера.
            return self._route_load(data)

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
    def _route_load(self, data: dict[str, Any]) -> None:
        """POST /v1/load — загрузить документ (PDF/Word/Excel) целиком."""
        import base64
        import tempfile

        from ..ingest.autoload import autoload
        from ..ingest.pdf import PdfError
        from ..ingest.word import ParseError

        st = self.store()
        raw = data.get("content_base64")
        server_path = str(data.get("path", "") or "").strip()
        tmp_path: Path | None = None

        if raw:
            name = str(data.get("filename", "upload.pdf"))
            suffix = Path(name).suffix or ".pdf"
            try:
                blob = base64.b64decode(raw, validate=True)
            except Exception:                                # noqa: BLE001
                self._send(400, {"error": "content_base64 не декодируется"})
                return
            if len(blob) > MAX_BODY:
                self._send(400, {"error": "файл слишком большой"})
                return
            fd = tempfile.NamedTemporaryFile(delete=False, suffix=suffix,
                                             prefix="saps_upload_")
            fd.write(blob)
            fd.close()
            tmp_path = Path(fd.name)
            target = tmp_path
            display = name
        elif server_path:
            target = Path(server_path)
            display = target.name
            if not target.exists():
                self._send(400, {"error": f"Файл не найден на сервере: "
                                          f"{server_path}"})
                return
        else:
            self._send(400, {"error": "нужен 'path' или 'content_base64'"})
            return

        try:
            result = autoload(
                st, self.cfg, target, actor=self._actor(data),
                kind=str(data.get("as", "") or ""),
                ruleset=str(data.get("ruleset", "") or ""),
                owner=str(data.get("owner", "") or ""),
                node=str(data.get("node", "") or ""),
                engine=str(data.get("engine", "") or ""),
                run_agents=bool(data.get("run_agents", True)),
                promote=bool(data.get("promote", True)),
                force=bool(data.get("force", False)))
        except (ParseError, PdfError) as exc:
            self._send(400, {"error": str(exc)})
            return
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

        payload = result.to_dict()
        payload["path"] = display
        self._send(200, payload)

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

    def _embedding_status(self) -> dict[str, Any]:
        """Состояние модели эмбеддингов и покрытие индексами."""
        from ..llm.embeddings import EmbeddingError, probe_embedding_dim
        cfg = self.cfg
        out: dict[str, Any] = {
            "provider": cfg.embedding_provider,
            "model": cfg.embedding_model,
            "external": cfg.uses_external_embeddings(),
            "configured_dim": cfg.embedding_dim,
            "base_url": cfg.embedding_base_url,
            "batch": cfg.embedding_batch,
            "coverage": self.store().embedding_coverage(),
        }
        if not out["external"]:
            out["status"] = "офлайн-эмбеддер (сравнение слов, не смысла)"
            return out
        try:
            dim = probe_embedding_dim(
                cfg.embedding_provider, cfg.embedding_model,
                base_url=cfg.embedding_base_url,
                api_key=cfg.embedding_api_key, timeout=cfg.embedding_timeout)
        except EmbeddingError as exc:
            out["status"] = "недоступна"
            out["error"] = str(exc)
            return out
        out["model_dim"] = dim
        out["status"] = ("готова" if dim == cfg.embedding_dim
                         else "размерность не совпадает со схемой БД")
        return out

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


def _pdf_engines() -> list[str]:
    from ..ingest.pdf import available_engines
    return available_engines()


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


def serve(cfg: Config | None = None, *, check_schema: bool = True) -> int:
    cfg = cfg or Config.load()
    cfg.require_dsn()
    setup_logging(cfg.log_level, cfg.log_json)
    log = logging.getLogger("saps")

    Handler.cfg = cfg
    Handler.token = cfg.api_token
    Handler._store = None

    # Проверка схемы ДО открытия порта. Инстанс, который не может
    # работать с этой базой, не должен принимать запросы и попадать в
    # балансировщик — лучше упасть на старте с внятной причиной.
    if check_schema:
        from ..db.migrate import MigrationError, check_compatible
        try:
            store = Handler.store()
            check_compatible(store.conn, cfg.db_schema)
        except MigrationError as exc:
            log.error("Схема базы несовместима: %s", exc)
            return 3
        except StoreError as exc:
            log.error("База недоступна: %s", exc)
            return 4

    httpd = ThreadingHTTPServer((cfg.host, cfg.port), Handler)
    # Не даём висящему keep-alive соединению задержать остановку дольше,
    # чем контейнерный оркестратор готов ждать до SIGKILL.
    httpd.daemon_threads = True
    url = f"http://{cfg.host}:{cfg.port}"

    log.info("САПС %s запускается", _version())
    for line in cfg.describe().splitlines():
        log.info("%s", line)
    log.info("Слушаю %s (рабочее место %s/dashboard)", url, url)
    if not cfg.api_token:
        log.warning("Токен не задан — доступ только с localhost")

    stopping = threading.Event()

    def _stop(signum: int, _frame: Any) -> None:
        """Корректная остановка по сигналу.

        Docker и systemd останавливают процесс через SIGTERM и ждут
        считанные секунды до SIGKILL. Без обработчика Python завершается
        немедленно, обрывая запрос инженера на середине; здесь сервер
        перестаёт принимать новые соединения и даёт текущим доиграть.
        """
        if stopping.is_set():
            return
        stopping.set()
        log.info("Получен сигнал %s — останавливаюсь", signal.Signals(signum).name)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _stop)
        except ValueError:
            # serve() вызвали не из главного потока (например, из теста) —
            # обработчик не поставить, и это не повод падать.
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("Прервано с клавиатуры")
    finally:
        httpd.server_close()
        if Handler._store is not None:
            Handler._store.close()
        log.info("Остановлено")
    return 0


def main() -> int:
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
