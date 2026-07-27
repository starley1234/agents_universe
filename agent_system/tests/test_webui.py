"""Тесты веб-морды конфигов/логов: agent/webui.py + маршруты agent/server.py.

Проверяется на реальном HTTP-стеке (agent/server.py, ThreadingHTTPServer)
и реальном SQLite (Store), по той же философии, что test_e2e.py: заглушка
только на месте самой языковой модели. Профили пишутся/читаются с
настоящей файловой системы, но через ПОДМЕНЁННУЮ Config.profiles_dir()
(временная папка) — чтобы тест не портил реальные agent/profiles/*.json.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import Config                                       # noqa: E402
from agent.server import Handler                                      # noqa: E402
from agent import webui                                               # noqa: E402
from agent.store import Store                                         # noqa: E402

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
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _ProfilesDirPatch:
    """Подменяет Config.profiles_dir() на временную папку — тесты записи

    профилей не должны трогать настоящие agent/profiles/*.json."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self._orig = Config.profiles_dir

    def __enter__(self) -> Path:
        real = Path(__file__).resolve().parents[1] / "agent" / "profiles"
        for f in real.glob("*.json"):
            (self.tmp / f.name).write_text(f.read_text(encoding="utf-8"),
                                           encoding="utf-8")
        Config.profiles_dir = staticmethod(lambda: self.tmp)
        return self.tmp

    def __exit__(self, *exc) -> None:
        Config.profiles_dir = self._orig


class FakeLLMHandler(BaseHTTPRequestHandler):
    """Планировщик даёт N пунктов, рефлексия — валидный JSON, рабочие шаги —

    мгновенный текстовый ответ. Опциональная задержка имитирует реальный
    долгий прогон — нужна тесту на остановку по кнопке "Стоп".
    """
    calls = 0
    delay = 0.0
    plan = "первый шаг сделать\nвторой шаг сделать"

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n)
        type(self).calls += 1
        if type(self).delay:
            time.sleep(type(self).delay)
        try:
            body = json.loads(raw.decode("utf-8"))
            text = " ".join(str(m.get("content") or "")
                            for m in body.get("messages", []))
        except Exception:
            text = ""
        if "Ты планировщик" in text:
            msg = {"role": "assistant", "content": type(self).plan}
        elif "ТОЛЬКО валидным JSON" in text:
            msg = {"role": "assistant",
                  "content": json.dumps({"learned": [], "next": "",
                                         "stuck": False})}
        else:
            msg = {"role": "assistant", "content": "готово"}
        out = json.dumps({"choices": [{"message": msg}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class _Ctx:
    """Общая обвязка: временная папка, фейковая LLM, HTTP-сервер agent."""

    def __enter__(self):
        self.td = tempfile.TemporaryDirectory()
        td = Path(self.td.name)
        FakeLLMHandler.calls = 0
        FakeLLMHandler.delay = 0.0
        FakeLLMHandler.plan = "первый шаг сделать\nвторой шаг сделать"
        self.llm_port = free_port()
        self.llm_srv = ThreadingHTTPServer(("127.0.0.1", self.llm_port),
                                           FakeLLMHandler)
        threading.Thread(target=self.llm_srv.serve_forever, daemon=True).start()

        self.cfg = Config(provider="openai", model="fake",
                          base_url=f"http://127.0.0.1:{self.llm_port}/v1",
                          api_key="test", workspace=str(td), skills=["files", "memory"],
                          max_steps=3, db=str(td / "a.db"))
        self.cfg.sandbox.mode = "off"

        Handler.cfg = self.cfg
        Handler.token = "tok"
        Handler.autorun = webui.AutorunManager()  # свежий менеджер на тест
        self.api_port = free_port()
        self.api = ThreadingHTTPServer(("127.0.0.1", self.api_port), Handler)
        threading.Thread(target=self.api.serve_forever, daemon=True).start()
        time.sleep(0.15)
        self.base = f"http://127.0.0.1:{self.api_port}"
        return self

    def __exit__(self, *exc) -> None:
        self.api.shutdown()
        self.llm_srv.shutdown()
        self.td.cleanup()

    def get(self, path: str, token: bool = True):
        req = urllib.request.Request(
            f"{self.base}{path}",
            headers={"Authorization": "Bearer tok"} if token else {})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def get_raw(self, path: str, token: bool = True):
        req = urllib.request.Request(
            f"{self.base}{path}",
            headers={"Authorization": "Bearer tok"} if token else {})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode("utf-8")

    def post(self, path: str, body: dict):
        req = urllib.request.Request(
            f"{self.base}{path}", data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": "Bearer tok", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def wait_state(self, target_states, timeout=15.0):
        deadline = time.time() + timeout
        st = {}
        while time.time() < deadline:
            code, st = self.get("/api/auto/status")
            if st.get("state") in target_states:
                return st
            time.sleep(0.1)
        return st


# ============================================================= профили
def test_profiles_crud() -> None:
    section("agent/webui.py: чтение/запись/удаление профилей (низкий уровень)")
    with tempfile.TemporaryDirectory() as td:
        with _ProfilesDirPatch(Path(td)):
            saved = webui.save_profile("mytest", {
                "description": "тестовый профиль", "skills": ["files", "shell"],
                "max_steps": 12, "system_prompt": "будь краток"})
            check("сохранён с нужными полями",
                  saved["skills"] == ["files", "shell"] and saved["max_steps"] == 12)
            check("имя файла совпадает с полем name", saved["name"] == "mytest")

            got = webui.read_profile("mytest")
            check("перечитан с диска", got["description"] == "тестовый профиль")

            names = [p["file"] for p in webui.list_profiles_full()]
            check("виден в общем списке", "mytest" in names)

            # частичное обновление не стирает system_prompt
            webui.save_profile("mytest", {"max_steps": 20})
            got2 = webui.read_profile("mytest")
            check("частичное обновление сохраняет прежние поля",
                  got2["system_prompt"] == "будь краток" and got2["max_steps"] == 20)

            webui.delete_profile("mytest")
            try:
                webui.read_profile("mytest")
                check("удалён с диска", False)
            except webui.WebUIError:
                check("удалён с диска", True)


def test_profiles_negative_validation() -> None:
    section("agent/webui.py: негативные сценарии валидации профиля")
    with tempfile.TemporaryDirectory() as td:
        with _ProfilesDirPatch(Path(td)):
            for bad in ("../evil", "1bad", "", "with space", "a" * 100):
                try:
                    webui.save_profile(bad, {})
                    check(f"имя {bad!r} отклонено", False)
                except webui.WebUIError:
                    check(f"имя {bad!r} отклонено", True)

            try:
                webui.save_profile("ok_name", {"skills": "not-a-list"})
                check("skills не списком отклонён", False)
            except webui.WebUIError:
                check("skills не списком отклонён", True)

            try:
                webui.save_profile("ok_name", {"max_steps": "abc"})
                check("нечисловой max_steps отклонён", False)
            except webui.WebUIError:
                check("нечисловой max_steps отклонён", True)

            try:
                webui.save_profile("ok_name", {"max_steps": -5})
                check("отрицательный max_steps отклонён", False)
            except webui.WebUIError:
                check("отрицательный max_steps отклонён", True)

            try:
                webui.delete_profile("does-not-exist")
                check("удаление несуществующего профиля -> ошибка", False)
            except webui.WebUIError:
                check("удаление несуществующего профиля -> ошибка", True)

            try:
                webui.read_profile("also-missing")
                check("чтение несуществующего профиля -> ошибка", False)
            except webui.WebUIError:
                check("чтение несуществующего профиля -> ошибка", True)


# ==================================================== HTTP: дашборд/API
def test_dashboard_html_served_without_token() -> None:
    section("GET /dashboard: страница отдаётся без токена (данные — с токеном)")
    with _Ctx() as c:
        code, body = c.get_raw("/dashboard", token=False)
        check("200 без токена", code == 200)
        check("это HTML дашборда", "дашборд" in body.lower()
              or "Агент" in body, body[:200])


def test_api_requires_token() -> None:
    section("GET /api/*: требуется тот же токен, что и /run")
    with _Ctx() as c:
        code, data = c.get("/api/counts", token=False)
        check("без токена — 401", code == 401, str((code, data)))
        code, data = c.get("/api/counts")
        check("с токеном — 200", code == 200, str((code, data)))


def test_api_counts_and_runs_reflect_real_store() -> None:
    section("GET /api/counts, /api/runs: реальные числа из Store, не заглушка")
    with _Ctx() as c:
        code, data = c.get("/api/counts")
        check("изначально всё по нулям", all(v == 0 for v in data.values()), str(data))

        st = Store(c.cfg.db)
        rid = st.start_run("ручная проверка", "coder")
        st.remember("факт для дашборда", run_id=rid)
        st.upsert_entity("part", "AB-01", run_id=rid)
        st.finish_run(rid, "done")
        st.close()

        code, data = c.get("/api/counts")
        check("runs увеличился", data["runs"] == 1, str(data))
        check("facts увеличился", data["facts"] == 1, str(data))
        check("entities увеличился", data["entities"] == 1, str(data))

        code, data = c.get("/api/runs")
        check("прогон виден в списке", data["runs"][0]["goal"] == "ручная проверка",
              str(data))

        code, data = c.get(f"/api/runs/{rid}")
        check("детали прогона отдаются", data["run"]["id"] == rid)

        code, data = c.get("/api/runs/999999")
        check("несуществующий прогон -> 404", code == 404, str((code, data)))


def test_api_facts_search() -> None:
    section("GET /api/facts: поиск по памяти через тот же recall")
    with _Ctx() as c:
        st = Store(c.cfg.db)
        st.remember("зазор щеки редуктора 3.87 мм")
        st.remember("совершенно другой факт про мотор")
        st.close()

        code, data = c.get("/api/facts?query=" + urllib.parse.quote("зазор"))
        texts = [f["text"] for f in data["facts"]]
        check("найден релевантный факт", any("зазор" in t for t in texts), str(texts))
        check("нерелевантный факт не выдвинут первым",
              texts[0] != "совершенно другой факт про мотор", str(texts))


def test_api_ontology() -> None:
    section("GET /api/entities, /api/relations: реальный граф из Store")
    with _Ctx() as c:
        st = Store(c.cfg.db)
        st.link(("part", "AB-01"), "assembled_into", ("assembly", "Редуктор"))
        st.close()

        code, data = c.get("/api/entities")
        kinds = {e["kind"] for e in data["entities"]}
        check("оба типа объектов видны", {"part", "assembly"} <= kinds, str(kinds))
        check("kinds перечислены отдельно", "part" in data["kinds"], str(data["kinds"]))

        code, data = c.get("/api/entities?kind=part")
        check("фильтр по kind работает",
              all(e["kind"] == "part" for e in data["entities"]), str(data))

        code, data = c.get("/api/relations")
        check("связь видна", any(r["pred"] == "assembled_into"
                                 for r in data["relations"]), str(data))


def test_api_profiles_write_delete_over_http() -> None:
    section("POST /api/profiles, DELETE через HTTP: реально пишет/удаляет файл")
    with tempfile.TemporaryDirectory() as pd:
        with _ProfilesDirPatch(Path(pd)):
            with _Ctx() as c:
                code, data = c.post("/api/profiles", {
                    "name": "httptest", "description": "через HTTP",
                    "skills": ["files"], "max_steps": 7})
                check("создан через HTTP", code == 200, str((code, data)))

                code, data = c.get("/api/profiles/httptest")
                check("читается через HTTP", data["max_steps"] == 7, str(data))

                code, data = c.post("/api/profiles", {"name": "../evil"})
                check("недопустимое имя отклонено (400)", code == 400, str((code, data)))

                code, data = c.post("/api/profiles/httptest/delete", {})
                check("удалён через HTTP", code == 200, str((code, data)))

                code, data = c.get("/api/profiles/httptest")
                check("после удаления не читается (400)", code == 400, str((code, data)))


# ===================================================== автономный режим
def test_autorun_start_status_finish() -> None:
    section("POST /api/auto/start: реальный автономный прогон до конца")
    with _Ctx() as c:
        code, data = c.post("/api/auto/start",
                            {"goal": "тестовая автономная цель",
                             "hours": 1, "iterations": 5})
        check("прогон запущен (200)", code == 200, str((code, data)))
        run_id = data["run_id"]
        check("run_id получен", run_id > 0, str(data))

        st = c.wait_state({"done", "error"})
        check("прогон дошёл до конца", st.get("state") == "done", str(st))
        check("завершён как done", st.get("stopped_by") == "done", str(st))

        code, runs = c.get("/api/runs")
        check("прогон появился в истории", any(r["id"] == run_id
                                                for r in runs["runs"]), str(runs))


def test_autorun_conflict_when_already_running() -> None:
    section("POST /api/auto/start: второй запуск отклонён, пока первый идёт")
    with _Ctx() as c:
        FakeLLMHandler.delay = 0.4    # достаточно медленно, чтобы второй запрос
                                      # застал первый прогон ещё активным
        code, data = c.post("/api/auto/start",
                            {"goal": "долгий прогон", "iterations": 10})
        check("первый запуск ок", code == 200, str((code, data)))
        time.sleep(0.15)
        code2, data2 = c.post("/api/auto/start", {"goal": "второй, лишний"})
        check("второй запуск отклонён (400)", code2 == 400, str((code2, data2)))
        c.wait_state({"done", "error"})


def test_autorun_stop() -> None:
    section("POST /api/auto/stop: кооперативная остановка реально прерывает прогон")
    with _Ctx() as c:
        FakeLLMHandler.delay = 0.3
        FakeLLMHandler.plan = ("шаг раз тянуть подольше\n"
                               "шаг два тянуть подольше\n"
                               "шаг три тянуть подольше\n"
                               "шаг четыре тянуть подольше")
        code, data = c.post("/api/auto/start",
                            {"goal": "прогон для остановки", "iterations": 20})
        check("запущен", code == 200, str((code, data)))
        time.sleep(0.2)
        code, data = c.post("/api/auto/stop", {})
        check("остановка принята", code == 200, str((code, data)))

        st = c.wait_state({"done", "error"})
        check("прогон реально остановлен, а не доработал план",
              st.get("stopped_by") == "stopped", str(st))


def test_autorun_stop_without_running_is_error() -> None:
    section("POST /api/auto/stop без активного прогона — ошибка, не тихий успех")
    with _Ctx() as c:
        code, data = c.post("/api/auto/stop", {})
        check("отказ, если нечего останавливать", code == 400, str((code, data)))


def test_autorun_stream_emits_events() -> None:
    section("GET /api/auto/stream: реальные NDJSON-события автономного прогона")
    with _Ctx() as c:
        code, data = c.post("/api/auto/start",
                            {"goal": "цель для потока событий", "iterations": 5})
        check("запуск ок", code == 200, str((code, data)))

        req = urllib.request.Request(
            f"{c.base}/api/auto/stream", headers={"Authorization": "Bearer tok"})
        kinds = []
        with urllib.request.urlopen(req, timeout=15) as r:
            deadline = time.time() + 10
            for line in r:
                if time.time() > deadline:
                    break
                line = line.decode().strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kinds.append(e.get("event"))
                if e.get("event") == "done":
                    break
        check("события реально пришли", len(kinds) > 0, str(kinds))
        check("событие завершения дошло", "done" in kinds, str(kinds))


# =============================================================== negative
def test_store_counts_helpers_directly() -> None:
    section("Store: counts()/list_* методы напрямую (без HTTP)")
    with tempfile.TemporaryDirectory() as td:
        st = Store(str(Path(td) / "a.db"))
        rid = st.start_run("цель", "coder")
        st.add_tasks(rid, ["пункт один", "пункт два"])
        st.set_task(st.tasks(rid)[0]["id"], "done", "готово")
        st.log_event(rid, 1, "tool", "write_file", "создан файл x")
        c = st.counts()
        check("run учтён", c["runs"] == 1, str(c))
        check("событие учтено", c["events"] == 1, str(c))

        events = st.run_events(rid)
        check("run_events хронологический (не как recent_events — не реверс)",
              events[0]["name"] == "write_file", str(events))

        runs = st.list_runs()
        check("list_runs видит прогон", runs[0]["id"] == rid, str(runs))
        st.close()


def main() -> int:
    print("=" * 60)
    print("ТЕСТЫ: веб-морда конфигов/логов (agent/webui.py)")
    print("=" * 60)

    test_profiles_crud()
    test_profiles_negative_validation()
    test_dashboard_html_served_without_token()
    test_api_requires_token()
    test_api_counts_and_runs_reflect_real_store()
    test_api_facts_search()
    test_api_ontology()
    test_api_profiles_write_delete_over_http()
    test_store_counts_helpers_directly()
    test_autorun_start_status_finish()
    test_autorun_conflict_when_already_running()
    test_autorun_stop()
    test_autorun_stop_without_running_is_error()
    test_autorun_stream_emits_events()

    print("\n" + "=" * 60)
    print(f"пройдено: {PASS} · провалено: {FAIL}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
