"""Тесты HTTP API: реальные сокеты (ThreadingHTTPServer), без моков.

Проверяется публичная поверхность платформы — та, через которую среду
встраивают в чужие системы. Поэтому здесь важны не только счастливые
пути, но и поведение при ошибках: токен, битое тело запроса,
несуществующий маршрут, попытка запустить неизвестный workflow. Плохой
API — это API, который на ошибку отдаёт 500 и трейсбек.

Отдельно проверяется весь цикл Human-in-the-Loop через HTTP: запуск →
пауза → очередь согласований → ответ человека → продолжение. Это
основной сценарий использования консоли.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import check, section, summary                       # noqa: E402
from awos.api import server as api_server                          # noqa: E402
from awos.config import Config                                     # noqa: E402


class ApiCtx:
    """Поднимает настоящий HTTP-сервер среды на свободном порту."""

    def __init__(self, token: str = "test-token", **cfg_over) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="awos_api_"))
        params = {"db_path": str(self.dir / "awos.db"),
                  "workspace": str(self.dir / "ws"),
                  "provider": "stub", "model": "stub",
                  "hitl_mode": "off", "llm_retries": 0}
        params.update(cfg_over)
        self.cfg = Config(**params)
        api_server.Handler.cfg = self.cfg
        api_server.Handler.token = token
        api_server.Handler._engine = None
        self.token = token
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), api_server.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def call(self, method: str, path: str, body=None, token: str | None = "use"):
        headers = {"Content-Type": "application/json"}
        tok = self.token if token == "use" else token
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, {"raw": raw}

    def raw(self, method: str, path: str, payload: bytes):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=payload, method=method,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)


def main() -> int:
    section("Здоровье и статика — без токена")
    ctx = ApiCtx()
    code, data = ctx.call("GET", "/health", token=None)
    check("/health отвечает без токена", code == 200 and data["status"] == "ok")
    check("в /health есть версия", bool(data.get("version")))
    with urllib.request.urlopen(f"http://127.0.0.1:{ctx.port}/dashboard") as r:
        html = r.read().decode()
    check("дашборд отдаётся", r.status == 200 and "<!DOCTYPE html>" in html)
    check("дашборд — это консоль AWOS", "AWOS" in html)
    check("в дашборде есть вкладка согласований", "Согласования" in html)

    section("Токен обязателен для остальных маршрутов")
    check("без токена — 401", ctx.call("GET", "/v1/runs", token=None)[0] == 401)
    check("с неверным токеном — 401",
          ctx.call("GET", "/v1/runs", token="wrong-token")[0] == 401)
    check("с верным токеном — 200", ctx.call("GET", "/v1/runs")[0] == 200)
    check("POST без токена — 401",
          ctx.call("POST", "/v1/runs", {"workflow": "x"}, token=None)[0] == 401)

    section("Справочные маршруты")
    code, data = ctx.call("GET", "/v1/workflows")
    names = [w["name"] for w in data["workflows"]]
    check("список workflow отдан", code == 200 and "research_brief" in names)
    check("все встроенные workflow валидны",
          not any("error" in w for w in data["workflows"]))
    code, data = ctx.call("GET", "/v1/profiles")
    check("список профилей отдан",
          code == 200 and any(p["name"] == "critic" for p in data["profiles"]))
    code, data = ctx.call("GET", "/v1/tools")
    check("список инструментов отдан", code == 200 and len(data["tools"]) >= 4)
    check("гранты показаны", data["grants"]["shell"] is False)
    code, data = ctx.call("GET", "/v1/config")
    check("конфиг отдан", code == 200)
    check("секреты в конфиге замаскированы",
          data["config"]["api_key"] in ("", "***"))
    code, data = ctx.call("GET", "/v1/stats")
    check("сводка отдана", code == 200 and "runs" in data)

    section("Запуск прогона")
    code, out = ctx.call("POST", "/v1/runs", {
        "workflow": "research_brief", "goal": "проверка",
        "inputs": {"topic": "тема", "audience": "аудитория"}})
    check("прогон запущен", code == 200, str(out)[:200])
    check("прогон завершён (HITL выключен)", out["status"] == "done",
          out.get("detail", ""))
    run_id = out["run_id"]
    check("шаги отчитались", len(out["steps"]) == 2)
    check("результаты на доске", "brief" in out["outputs"])

    code, data = ctx.call("GET", f"/v1/runs/{run_id}")
    check("состояние прогона отдано", code == 200 and data["run"]["id"] == run_id)
    check("в состоянии есть журнал", len(data["events"]) > 0)
    check("в состоянии есть доска", "research_notes" in data["context"])

    code, data = ctx.call("GET", f"/v1/runs/{run_id}/events")
    check("журнал отдан отдельно", code == 200 and len(data["events"]) > 0)
    first_id = data["events"][0]["id"]
    code, data2 = ctx.call("GET", f"/v1/runs/{run_id}/events?after={first_id}")
    check("выборка after работает",
          len(data2["events"]) == len(data["events"]) - 1)

    code, data = ctx.call("GET", f"/v1/runs/{run_id}/context")
    check("доска отдана", code == 200 and "brief" in data["context"])
    code, data = ctx.call("GET", f"/v1/runs/{run_id}/context?key=brief")
    check("история ключа отдана", code == 200 and len(data["history"]) == 1)

    code, data = ctx.call("GET", "/v1/runs?limit=5")
    check("список прогонов отдан", code == 200 and len(data["runs"]) == 1)
    ctx.close()

    section("Полный цикл Human-in-the-Loop через HTTP")
    ctx = ApiCtx(hitl_mode="always")
    code, out = ctx.call("POST", "/v1/runs", {
        "workflow": "research_brief", "goal": "hitl",
        "inputs": {"topic": "т", "audience": "а"}})
    check("прогон встал на человеке", out["status"] == "waiting_human")
    check("точка контроля отдана в ответе", out["checkpoint"] is not None)
    run_id, cp_id = out["run_id"], out["checkpoint"]["id"]

    code, data = ctx.call("GET", "/v1/checkpoints")
    check("очередь согласований непуста",
          code == 200 and len(data["checkpoints"]) == 1)
    check("в очереди та же точка контроля",
          data["checkpoints"][0]["id"] == cp_id)
    check("в точке контроля виден результат шага",
          bool(data["checkpoints"][0]["payload"].get("output")))

    code, out2 = ctx.call("POST", f"/v1/checkpoints/{cp_id}",
                          {"status": "edited", "response": "ПРАВКА ЧЕЛОВЕКА",
                           "actor": "тестировщик"})
    check("ответ человека принят", code == 200)
    check("прогон пошёл дальше", out2["steps"][0]["status"] == "done")
    code, data = ctx.call("GET", f"/v1/runs/{run_id}/context")
    check("правка человека попала на доску",
          data["context"]["research_notes"] == "ПРАВКА ЧЕЛОВЕКА")

    cp2 = out2["checkpoint"]["id"]
    code, out3 = ctx.call("POST", f"/v1/checkpoints/{cp2}", {"status": "approved"})
    check("прогон завершён после второго согласования",
          out3["status"] == "done", out3.get("detail", ""))
    code, data = ctx.call("GET", "/v1/checkpoints")
    check("очередь согласований пуста", data["checkpoints"] == [])

    section("Отмена прогона")
    code, out = ctx.call("POST", "/v1/runs", {
        "workflow": "research_brief", "goal": "отмена",
        "inputs": {"topic": "т", "audience": "а"}})
    rid = out["run_id"]
    code, data = ctx.call("POST", f"/v1/runs/{rid}/cancel", {"reason": "не нужно"})
    check("отмена принята", code == 200 and data["status"] == "cancelled")
    code, data = ctx.call("GET", f"/v1/runs/{rid}")
    check("статус в базе — cancelled", data["run"]["status"] == "cancelled")
    check("висящая точка контроля закрыта", data["checkpoint"] is None)
    ctx.close()

    section("Ошибки: понятный текст и корректный код")
    ctx = ApiCtx()
    code, data = ctx.call("POST", "/v1/runs", {"workflow": "нет_такого"})
    check("неизвестный workflow -> 400", code == 400)
    check("в ошибке перечислены известные", "research_brief" in data["error"])
    code, data = ctx.call("POST", "/v1/runs", {})
    check("запуск без workflow -> 400", code == 400)
    code, data = ctx.call("POST", "/v1/runs",
                          {"workflow": "research_brief", "inputs": "строка"})
    check("inputs не объект -> 400", code == 400)
    code, data = ctx.call("POST", "/v1/runs", {"workflow": "research_brief",
                                               "inputs": {}})
    check("нехватка обязательных входов -> 400", code == 400)
    check("в ошибке названы недостающие входы", "topic" in data["error"])

    code, data = ctx.call("GET", "/v1/runs/99999")
    check("несуществующий прогон -> 404", code == 404)
    code, data = ctx.call("POST", "/v1/checkpoints/99999", {"status": "approved"})
    check("несуществующая точка контроля -> 400", code == 400)
    code, data = ctx.call("POST", "/v1/checkpoints/1", {"status": "непонятно"})
    check("неизвестное решение -> 400", code == 400)
    code, data = ctx.call("GET", "/v1/no-such-route")
    check("несуществующий GET-маршрут -> 404", code == 404)
    code, data = ctx.call("POST", "/v1/no-such-route", {})
    check("несуществующий POST-маршрут -> 404", code == 404)
    code, data = ctx.raw("POST", "/v1/runs", "{это не json}".encode("utf-8"))
    check("битый JSON -> 400", code == 400)
    check("ошибка объяснена текстом", "JSON" in data["error"])
    code, data = ctx.raw("POST", "/v1/runs", '["список"]'.encode("utf-8"))
    check("не-объект в теле -> 400", code == 400)
    ctx.close()

    section("Работа без токена (localhost)")
    ctx = ApiCtx(token="")
    check("без настроенного токена доступ открыт",
          ctx.call("GET", "/v1/runs", token=None)[0] == 200)
    ctx.close()

    return summary("HTTP API")


if __name__ == "__main__":
    raise SystemExit(main())
