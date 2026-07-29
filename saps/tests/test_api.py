"""Тесты HTTP API и сквозной сценарий CLI.

API — то, через что работает рабочее место инженера, поэтому проверяется
на настоящих сокетах. Отдельно проверяется главный сценарий ТЗ п.6.2:
агент предложил -> инженер увидел diff -> нажал «Принять» -> изменение
попало в требование И в историю ревизий.

CLI проверяется настоящим подпроцессом: команды запускают из cron и CI,
и там важны коды возврата и текст в stdout, а не внутренние вызовы.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import harness                                                    # noqa: E402
from harness import (check, fresh_dsn, sample_docx, section,       # noqa: E402
                     skip_section, summary)

ROOT = Path(__file__).resolve().parents[1]


class Api:
    def __init__(self, dsn: str, token: str = "test-token", **over):
        from saps.api import server as api_server
        from saps.config import Config
        import tempfile
        params = dict(db_dsn=dsn, embedding_provider="hash",
                      embedding_model="hash-64", embedding_dim=64,
                      llm_provider="none", api_token=token,
                      workdir=tempfile.mkdtemp(prefix="saps_api_"))
        params.update(over)
        self.cfg = Config(**params)
        api_server.Handler.cfg = self.cfg
        api_server.Handler.token = token
        api_server.Handler._store = None
        self.token = token
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), api_server.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def call(self, method: str, path: str, body=None, token: str = "use"):
        headers = {"Content-Type": "application/json"}
        tok = self.token if token == "use" else token
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        data = json.dumps(body).encode() if body is not None else None
        # Кириллица в URL обязана быть percent-encoded (HTTP-строка запроса
        # — ASCII). Браузер делает это сам через URLSearchParams; здесь
        # повторяем то же поведение, иначе тест проверял бы не сервер, а
        # ограничение http.client.
        url = urllib.parse.urlsplit(f"http://127.0.0.1:{self.port}{path}")
        safe = urllib.parse.urlunsplit((
            url.scheme, url.netloc, urllib.parse.quote(url.path),
            urllib.parse.quote(url.query, safe="=&"), ""))
        req = urllib.request.Request(safe, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, {"raw": raw}

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def main() -> int:
    if harness.server() is None:
        skip_section("HTTP API", harness.SKIP_REASON)
        return summary("API и CLI")

    from saps.db.store import Store
    from saps.ingest.pipeline import import_file, promote_all
    from saps.llm import build_embedder
    from saps.rules.loader import load_builtin

    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="saps_api_t_"))
    dsn = fresh_dsn()

    # Наполняем базу так, как это сделал бы инженер.
    st = Store(dsn, schema="saps", dim=64)
    st.init_schema()
    emb = build_embedder("hash", "hash-64", dim=64)
    load_builtin(st, embedder=emb)
    doc = sample_docx(tmp / "tz.docx")
    result = import_file(st, doc, actor="engineer")
    promote_all(st, result.document_id, actor="engineer",
                embedder=emb.embed_one)
    st.close()

    api = Api(dsn)

    section("Служебные маршруты")
    code, data = api.call("GET", "/health", token=None)
    check("/health без токена", code == 200 and data["status"] == "ok")
    check("версия в ответе", bool(data.get("version")))
    with urllib.request.urlopen(f"http://127.0.0.1:{api.port}/dashboard") as r:
        html = r.read().decode()
    check("рабочее место отдаётся", r.status == 200 and "САПС" in html)
    check("в интерфейсе есть очередь предложений", "Предложения агентов" in html)
    check("в интерфейсе есть мастер импорта", "Мастер импорта" in html)
    check("в интерфейсе есть индикатор здоровья", "Здоровье сертификации" in html)

    section("Авторизация")
    check("без токена — 401", api.call("GET", "/v1/stats", token=None)[0] == 401)
    check("неверный токен — 401",
          api.call("GET", "/v1/stats", token="wrong")[0] == 401)
    check("верный токен — 200", api.call("GET", "/v1/stats")[0] == 200)

    section("Чтение данных")
    code, data = api.call("GET", "/v1/stats")
    check("статистика отдана", code == 200 and data["requirements"] == 5)
    code, data = api.call("GET", "/v1/config")
    check("конфиг отдан", code == 200)
    check("секреты замаскированы",
          data["config"]["tc_password"] in ("", "***"))
    check("коды MoC приложены", "MC2" in data["moc_codes"])

    code, data = api.call("GET", "/v1/requirements")
    check("список требований", code == 200 and data["count"] == 5)
    code, data = api.call("GET", "/v1/requirements?q=масса")
    check("поиск по тексту", data["count"] == 1, str(data["count"]))
    code, data = api.call("GET", "/v1/requirements?node=АСДБ.04.32")
    check("фильтр по узлу", data["count"] == 2)
    code, data = api.call("GET", "/v1/requirements?owner=Иванов")
    check("фильтр по ответственному", data["count"] == 1)

    code, data = api.call("GET", "/v1/clauses?ruleset=АП-25")
    check("справочник АП отдан", code == 200 and len(data["clauses"]) > 40)
    code, data = api.call("GET", "/v1/documents")
    check("документы отданы", code == 200 and len(data["documents"]) == 1)
    code, data = api.call("GET", f"/v1/staging?document_id={result.document_id}")
    check("staging отдан", code == 200 and len(data["records"]) == 5)
    code, data = api.call("GET", "/v1/nodes")
    check("узлы отданы", code == 200 and len(data["nodes"]) >= 1)

    section("Карточка требования")
    code, data = api.call("GET", "/v1/requirements")
    req_id = [r["id"] for r in data["requirements"]
              if r["external_id"] == "REQ-001"][0]
    code, card = api.call("GET", f"/v1/requirements/{req_id}")
    check("карточка отдана", code == 200)
    check("есть требование", card["requirement"]["external_id"] == "REQ-001")
    check("есть история ревизий", len(card["revisions"]) >= 1)
    check("есть блоки связей и MoC",
          "links" in card and "compliance" in card)
    check("несуществующее требование -> 404",
          api.call("GET", "/v1/requirements/999999")[0] == 404)

    section("Правка требования пишется в историю")
    code, data = api.call("POST", f"/v1/requirements/{req_id}",
                          {"text": "Уточнённая формулировка требования",
                           "reason": "правка на совещании", "actor": "Иванов"})
    check("правка принята", code == 200)
    check("версия выросла", data["version"] >= 2)
    code, card = api.call("GET", f"/v1/requirements/{req_id}")
    last = card["revisions"][-1]
    check("причина в истории", last["reason"] == "правка на совещании")
    check("автор в истории", last["actor"] == "Иванов")

    section("Запуск агентов через API")
    code, data = api.call("POST", "/v1/agents/editor/run", {"actor": "Иванов"})
    check("редактор отработал", code == 200 and data["counts"]["processed"] == 5)
    code, data = api.call("POST", "/v1/agents/classifier/run", {})
    check("классификатор отработал", code == 200)
    check("созданы предложения", data["counts"]["suggestions"] > 0,
          str(data["counts"]))
    code, data = api.call("POST", "/v1/agents/gap/run", {})
    check("gap-аналитик отработал", code == 200)
    check("найдены дыры", data["counts"]["findings"] > 0)
    check("неизвестный агент -> 400",
          api.call("POST", "/v1/agents/выдуманный/run", {})[0] in (400, 404))

    section("Главный сценарий ТЗ п.6.2: diff -> Принять")
    code, data = api.call("GET", "/v1/suggestions?status=pending")
    check("очередь предложений непуста", code == 200 and data["suggestions"])
    sug = next(s for s in data["suggestions"] if s["kind"] == "rule_link")
    check("у предложения есть обоснование", bool(sug["rationale"]))
    check("предложение привязано к требованию", bool(sug["external_id"]))

    target = int(sug["requirement_id"])
    before = api.call("GET", f"/v1/requirements/{target}")[1]
    code, applied = api.call("POST", f"/v1/suggestions/{sug['id']}",
                             {"decision": "accepted", "actor": "Иванов"})
    check("предложение принято", code == 200 and applied["applied"] is True)
    after = api.call("GET", f"/v1/requirements/{target}")[1]
    check("связь стала подтверждённой",
          any(l["confirmed"] for l in after["links"]),
          "принятие человеком — единственный способ подтвердить привязку")
    check("до принятия связь была неподтверждённой",
          not any(l["confirmed"] for l in before["links"]))
    check("повторное решение отвергается",
          api.call("POST", f"/v1/suggestions/{sug['id']}",
                   {"decision": "accepted"})[0] == 400)
    check("неизвестное решение отвергается",
          api.call("POST", f"/v1/suggestions/{sug['id']}",
                   {"decision": "может быть"})[0] == 400)

    section("Индикатор здоровья через API")
    code, data = api.call("GET", "/v1/health")
    check("здоровье посчитано", code == 200 and 0 <= data["health"] <= 1)
    check("есть составляющие и пробелы",
          "factors" in data and "gaps" in data)
    code, data = api.call("GET", "/v1/health/nodes")
    check("здоровье по узлам", code == 200 and isinstance(data["nodes"], list))

    section("Выгрузка через API")
    code, data = api.call("POST", "/v1/export", {"format": "xlsx"})
    check("выгрузка создана", code == 200 and Path(data["file"]).exists())
    code, data = api.call("POST", "/v1/export", {"format": "docx"})
    check("протокол Word создан", code == 200 and Path(data["file"]).exists())
    check("неизвестный формат -> 400",
          api.call("POST", "/v1/export", {"format": "pdf"})[0] == 400)

    section("Плагины через API")
    code, data = api.call("GET", "/v1/plugins")
    check("плагины перечислены", code == 200 and len(data["plugins"]) >= 2)
    code, data = api.call("POST", "/v1/plugins/report/run", {"fmt": "xlsx"})
    check("плагин отчёта запущен", code == 200 and data["counts"]["processed"] == 1)
    check("неизвестный плагин -> 404",
          api.call("POST", "/v1/plugins/нет/run", {})[0] == 404)

    section("Ошибки API")
    check("несуществующий GET -> 404",
          api.call("GET", "/v1/no-such-route")[0] == 404)
    check("несуществующий POST -> 404",
          api.call("POST", "/v1/no-such-route", {})[0] == 404)
    check("promote без ids -> 400",
          api.call("POST", "/v1/staging/promote", {})[0] == 400)
    code, data = api.call("GET", "/v1/audit")
    check("журнал доступен", code == 200 and len(data["audit"]) > 0)
    api.close()

    section("Сквозной сценарий CLI (реальные подпроцессы)")
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("SAPS_"):
            del env[key]
    cli_dsn = fresh_dsn()
    env.update({"SAPS_DB_DSN": cli_dsn, "SAPS_EMBEDDING_DIM": "64",
                "SAPS_EMBEDDING_MODEL": "hash-64",
                "SAPS_WORKDIR": str(tmp / "wd"), "PYTHONIOENCODING": "utf-8"})

    def run(*args, timeout=180):
        p = subprocess.run([sys.executable, "-m", "saps", *args], cwd=str(ROOT),
                           env=env, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout, p.stderr

    rc, out, err = run("init", "--rules")
    check("init создал схему", rc == 0, err[:300])
    check("справочники загружены", "АП-25" in out)

    rc, out, err = run("check")
    check("check успешен", rc == 0, err[:300])
    check("check показывает базу", "PostgreSQL доступен" in out)

    rc, out, err = run("import", str(doc), "--promote", "--owner", "Иванов")
    check("импорт успешен", rc == 0, err[:400])
    check("распознаны требования", "распознано записей: 5" in out)
    check("перенесено в базу", "создано 5" in out)

    rc, out, err = run("import", str(doc))
    check("повторный импорт отклонён", rc == 1)
    check("причина названа", "уже импортирован" in out)

    rc, out, err = run("agent", "editor")
    check("агент-редактор отработал", rc == 0, err[:300])
    rc, out, err = run("agent", "classifier")
    check("классификатор отработал", rc == 0)
    check("созданы предложения", "предложений" in out)

    rc, out, err = run("suggestions")
    check("очередь показана", rc == 0 and "агент" in out)
    sug_id = None
    for line in out.splitlines():
        if line.strip().startswith("["):
            sug_id = line.strip().split("]")[0].lstrip("[").strip()
            break
    check("номер предложения виден", sug_id is not None)
    if sug_id:
        rc, out, err = run("suggestions", "--accept", sug_id)
        check("предложение принято через CLI", rc == 0, err[:300])

    rc, out, err = run("show", "REQ-001")
    check("карточка требования показана", rc == 0 and "REQ-001" in out)
    check("видна история", "История:" in out)

    rc, out, err = run("health")
    check("здоровье посчитано", rc == 0 and "Готовность" in out)
    rc, out, err = run("health", "--by-node")
    check("здоровье по узлам", rc == 0)

    rc, out, err = run("export", "docx")
    check("протокол выгружен", rc == 0 and "Сохранено" in out)
    saved = out.strip().split("Сохранено: ")[-1].strip()
    check("файл существует", Path(saved).exists(), saved)

    rc, out, err = run("requirements", "--owner", "Иванов")
    check("список требований по инженеру", rc == 0)
    rc, out, err = run("plugin", "list")
    check("плагины перечислены", rc == 0 and "code_review" in out)

    rc, out, err = run("show", "REQ-НЕТ")
    check("несуществующее требование -> код 1", rc == 1)
    rc, out, err = run("import", "нет_файла.docx")
    check("несуществующий файл -> код 2", rc == 2)
    rc, out, err = run("tc", "login")
    check("Teamcenter без настроек -> код 2", rc == 2)
    check("причина названа", "tc_url" in (out + err))

    harness.cleanup()
    return summary("API и CLI")


if __name__ == "__main__":
    raise SystemExit(main())
