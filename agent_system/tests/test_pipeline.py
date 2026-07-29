"""Тесты оркестрации: agent/pipeline.py (PipelineRunner) + HTTP API.

ГЛАВНОЕ, что здесь проверяется: конвейер — ДЕТЕРМИНИРОВАННАЯ
последовательность стадий, заданная заранее в JSON, а НЕ агент,
управляющий другими агентами (см. пояснение в шапке agent/pipeline.py и
README.md «Это НЕ мультиагентный оркестратор»). Каждая стадия — это
обычный build_agent(...).run(task), задачи между стадиями связывает
подстановка плейсхолдеров {goal}/{имя_стадии} — не рассуждение модели о
том, что делать дальше.

Философия тестов та же, что у test_webhooks.py/test_webui.py: реальный
HTTP-стек (agent/server.py), реальный Store (SQLite), фейковая LLM
(http.server.ThreadingHTTPServer) — заглушка только на месте самой
модели. Определения конвейеров для тестов кладутся во ВРЕМЕННУЮ папку
(подмена agent.pipeline.pipelines_dir), чтобы не трогать настоящие
agent/pipelines/*.json.
"""
from __future__ import annotations

import json
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
from agent.store import Store                                         # noqa: E402
from agent.server import Handler                                      # noqa: E402
from agent import pipeline as pipeline_mod                            # noqa: E402
from agent import webui                                               # noqa: E402

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


class _PipelinesDirPatch:
    """Подменяет agent.pipeline.pipelines_dir() на временную папку — тесты

    не должны трогать настоящие agent/pipelines/*.json."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self._orig = pipeline_mod.pipelines_dir

    def __enter__(self) -> Path:
        pipeline_mod.pipelines_dir = lambda: self.tmp
        return self.tmp

    def __exit__(self, *exc) -> None:
        pipeline_mod.pipelines_dir = self._orig


TWO_STAGE_DEF = {
    "name": "two_stage",
    "description": "тестовый конвейер из двух стадий",
    "stages": [
        {"name": "first", "profile": "coder",
         "task": "Первая стадия. Цель: {goal}"},
        {"name": "second", "profile": "coder",
         "task": "Вторая стадия использует ответ первой:\n{first}"},
    ],
}


def _write_def(dirpath: Path, data: dict) -> None:
    (dirpath / f"{data['name']}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")


class FakeLLMHandler(BaseHTTPRequestHandler):
    """Отвечает текстом, содержащим номер вызова — так тест видит, что

    именно вторая стадия реально получила ответ первой (по счётчику).
    """
    calls = 0
    delay = 0.0
    #: если задано — вернуть tool_calls на несуществующий инструмент
    #: (гарантированно исчерпает max_steps -> stopped_by='max_steps')
    always_bad_tool = False
    last_prompt = ""

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
            type(self).last_prompt = " ".join(
                str(m.get("content") or "") for m in body.get("messages", []))
        except Exception:
            type(self).last_prompt = ""
        if type(self).always_bad_tool:
            msg = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "x", "type": "function",
                "function": {"name": "nonexistent_tool", "arguments": "{}"}}]}
        else:
            msg = {"role": "assistant",
                  "content": f"ответ #{type(self).calls}: {type(self).last_prompt[:60]}"}
        out = json.dumps({"choices": [{"message": msg}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class _Ctx:
    """Общая обвязка: временная папка конвейеров, фейковая LLM, конфиг."""

    def __enter__(self):
        self.td = tempfile.TemporaryDirectory()
        td = Path(self.td.name)
        (td / "ws").mkdir()
        (td / "pipelines").mkdir()
        FakeLLMHandler.calls = 0
        FakeLLMHandler.delay = 0.0
        FakeLLMHandler.always_bad_tool = False
        self.llm_port = free_port()
        self.llm_srv = ThreadingHTTPServer(("127.0.0.1", self.llm_port),
                                           FakeLLMHandler)
        threading.Thread(target=self.llm_srv.serve_forever, daemon=True).start()

        self.cfg = Config(provider="openai", model="fake",
                          base_url=f"http://127.0.0.1:{self.llm_port}/v1",
                          api_key="test", workspace=str(td / "ws"),
                          skills=["files"], max_steps=2,
                          db=str(td / "a.db"))
        self.cfg.sandbox.mode = "off"
        self.pipelines_dir = td / "pipelines"
        return self

    def __exit__(self, *exc) -> None:
        self.llm_srv.shutdown()
        self.td.cleanup()


# ============================================================= validate
def test_validate_pipeline_negative_cases() -> None:
    section("validate_pipeline: негативные сценарии")
    P = pipeline_mod.PipelineError

    def check_raises(data, label):
        try:
            pipeline_mod.validate_pipeline(data)
            check(f"{label} отклонён", False)
        except P:
            check(f"{label} отклонён", True)

    check_raises({"stages": []}, "пустой stages")
    check_raises({}, "отсутствующий stages")
    check_raises({"stages": [{"name": "a"}]}, "стадия без task")
    check_raises({"stages": [{"task": "x"}]}, "стадия без name")
    check_raises({"stages": [{"name": "a", "task": "x"},
                             {"name": "a", "task": "y"}]}, "дублирующееся имя стадии")
    check_raises({"stages": [{"name": "a", "task": "{b}"}]},
                "ссылка на несуществующую стадию")
    check_raises({"stages": [{"name": "a", "task": "{a}"}]},
                "ссылка стадии на саму себя")
    check_raises({"stages": [{"name": "a", "task": "x"},
                             {"name": "b", "task": "{c}"}]},
                "ссылка на будущую стадию")


def test_validate_pipeline_positive_case() -> None:
    section("validate_pipeline: корректное определение проходит")
    try:
        pipeline_mod.validate_pipeline({
            "stages": [{"name": "a", "task": "используй {goal}"},
                      {"name": "b", "task": "используй {a} и {goal}"}]})
        check("валидное определение принято", True)
    except pipeline_mod.PipelineError as exc:
        check("валидное определение принято", False, str(exc))


# ========================================================== load_pipeline_def
def test_load_pipeline_def_low_level() -> None:
    section("load_pipeline_def: загрузка, неизвестное имя, недопустимое имя")
    with tempfile.TemporaryDirectory() as pd:
        with _PipelinesDirPatch(Path(pd)):
            _write_def(Path(pd), TWO_STAGE_DEF)
            data = pipeline_mod.load_pipeline_def("two_stage")
            check("определение загружено", data["name"] == "two_stage")
            check("список конвейеров видит наш файл",
                  "two_stage" in pipeline_mod.list_pipelines())

            try:
                pipeline_mod.load_pipeline_def("no_such_pipeline")
                check("неизвестное имя отклонено", False)
            except pipeline_mod.PipelineError:
                check("неизвестное имя отклонено", True)

            try:
                pipeline_mod.load_pipeline_def("../evil")
                check("недопустимое имя (path traversal) отклонено", False)
            except pipeline_mod.PipelineError:
                check("недопустимое имя (path traversal) отклонено", True)


# ============================================================== PipelineRunner
def test_pipeline_runner_full_success() -> None:
    section("PipelineRunner: два прогона по очереди, ответ первой стадии "
           "реально попадает в задачу второй")
    with _Ctx() as c:
        with _PipelinesDirPatch(c.pipelines_dir):
            _write_def(c.pipelines_dir, TWO_STAGE_DEF)
            store = Store(c.cfg.db)
            events = []
            runner = pipeline_mod.PipelineRunner(
                c.cfg, store, on_event=lambda k, d: events.append((k, d)))
            result = runner.run("two_stage", "цель конвейера")

            check("конвейер завершён как done", result["status"] == "done",
                  str(result))
            check("обе стадии дали ответ", set(result["answers"]) == {"first", "second"},
                  str(result))
            check("модель вызывалась дважды (по разу на стадию)",
                  FakeLLMHandler.calls == 2, str(FakeLLMHandler.calls))

            stages = store.pipeline_stages(result["pipeline_run_id"])
            check("обе стадии сохранены со статусом done",
                  all(s["status"] == "done" for s in stages), str(stages))
            check("КАЖДАЯ стадия — отдельный run_id в Store",
                  stages[0]["run_id"] != stages[1]["run_id"], str(stages))
            check("плейсхолдер {first} реально подставлен в задачу второй стадии",
                  result["answers"]["first"] in stages[1]["task"], stages[1]["task"])

            run_kinds = [k for k, d in events]
            check("событие pipeline_start пришло", "pipeline_start" in run_kinds)
            check("события stage_start/stage_done пришли по каждой стадии",
                  run_kinds.count("stage_start") == 2
                  and run_kinds.count("stage_done") == 2, str(run_kinds))
            check("событие pipeline_finish пришло последним по конвейеру",
                  run_kinds[-1] == "pipeline_finish", str(run_kinds))
            store.close()


def test_pipeline_runner_stage_failure_stops_pipeline() -> None:
    section("PipelineRunner: провал стадии останавливает конвейер, "
           "последующие стадии помечаются skipped")
    with _Ctx() as c:
        with _PipelinesDirPatch(c.pipelines_dir):
            _write_def(c.pipelines_dir, TWO_STAGE_DEF)
            FakeLLMHandler.always_bad_tool = True   # гарантированно max_steps
            store = Store(c.cfg.db)
            runner = pipeline_mod.PipelineRunner(c.cfg, store)
            result = runner.run("two_stage", "цель")

            check("конвейер завершён как failed", result["status"] == "failed",
                  str(result))
            check("ответов нет вовсе", result["answers"] == {}, str(result))

            stages = store.pipeline_stages(result["pipeline_run_id"])
            check("первая стадия помечена failed",
                  stages[0]["status"] == "failed", str(stages[0]))
            check("причина провала записана",
                  "max_steps" in stages[0]["error"], stages[0]["error"])
            check("вторая стадия НЕ выполнялась, помечена skipped",
                  stages[1]["status"] == "skipped", str(stages[1]))
            store.close()


def test_pipeline_runner_stop_event_skips_remaining() -> None:
    section("PipelineRunner: кооперативная остановка между стадиями")
    with _Ctx() as c:
        with _PipelinesDirPatch(c.pipelines_dir):
            _write_def(c.pipelines_dir, TWO_STAGE_DEF)
            store = Store(c.cfg.db)
            stop_event = threading.Event()
            stop_event.set()   # остановлено ДО старта -> обе стадии skipped
            runner = pipeline_mod.PipelineRunner(c.cfg, store, stop_event=stop_event)
            result = runner.run("two_stage", "цель")

            check("конвейер завершён как stopped", result["status"] == "stopped",
                  str(result))
            stages = store.pipeline_stages(result["pipeline_run_id"])
            check("обе стадии пропущены", all(s["status"] == "skipped" for s in stages),
                  str(stages))
            check("модель не вызывалась вовсе (остановлено ДО первой стадии)",
                  FakeLLMHandler.calls == 0, str(FakeLLMHandler.calls))
            store.close()


# ==================================================== реальные конвейеры проекта
def test_real_pipeline_definitions_are_valid() -> None:
    section("Реальные agent/pipelines/*.json проходят собственную валидацию")
    names = pipeline_mod.list_pipelines()
    check("хотя бы один готовый конвейер существует", len(names) > 0, str(names))
    for name in names:
        try:
            data = pipeline_mod.load_pipeline_def(name)
            check(f"конвейер {name!r} валиден", True)
            for stage in data["stages"]:
                check(f"  {name}/{stage['name']}: указан profile",
                      bool(stage.get("profile")), str(stage))
        except pipeline_mod.PipelineError as exc:
            check(f"конвейер {name!r} валиден", False, str(exc))


def test_real_pipeline_profiles_exist() -> None:
    section("Профили, указанные в реальных конвейерах, реально существуют")
    known = set(Config.list_profiles())
    for name in pipeline_mod.list_pipelines():
        data = pipeline_mod.load_pipeline_def(name)
        for stage in data["stages"]:
            prof = stage.get("profile")
            if prof:
                check(f"{name}/{stage['name']}: профиль {prof!r} существует",
                      prof in known, str(known))


# ==================================================================== HTTP API
class _HttpCtx(_Ctx):
    def __enter__(self):
        super().__enter__()
        Handler.cfg = self.cfg
        Handler.token = "tok"
        Handler.pipelines = webui.PipelineManager()
        self.api_port = free_port()
        self.api = ThreadingHTTPServer(("127.0.0.1", self.api_port), Handler)
        threading.Thread(target=self.api.serve_forever, daemon=True).start()
        time.sleep(0.15)
        self.base = f"http://127.0.0.1:{self.api_port}"
        return self

    def __exit__(self, *exc) -> None:
        self.api.shutdown()
        super().__exit__(*exc)

    def get(self, path: str, token: bool = True):
        req = urllib.request.Request(
            f"{self.base}{path}",
            headers={"Authorization": "Bearer tok"} if token else {})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def post(self, path: str, body: dict):
        req = urllib.request.Request(
            f"{self.base}{path}", data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": "Bearer tok", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def wait_done(self, pid: int, timeout: float = 15.0) -> dict:
        deadline = time.time() + timeout
        st = {}
        while time.time() < deadline:
            code, st = self.get(f"/api/pipeline/{pid}/status")
            if st.get("state") in ("done", "error"):
                return st
            time.sleep(0.1)
        return st


def test_api_pipelines_list_and_requires_token() -> None:
    section("GET /api/pipelines: список, требует тот же токен, что /run")
    with _HttpCtx() as c:
        with _PipelinesDirPatch(c.pipelines_dir):
            _write_def(c.pipelines_dir, TWO_STAGE_DEF)
            code, data = c.get("/api/pipelines", token=False)
            check("без токена — 401", code == 401, str((code, data)))

            code, data = c.get("/api/pipelines")
            names = {p["name"] for p in data["pipelines"]}
            check("наш тестовый конвейер виден", "two_stage" in names, str(names))
            check("стадии перечислены", any(
                s["name"] == "first" for p in data["pipelines"]
                for s in p["stages"]), str(data))


def test_api_pipeline_start_and_stream() -> None:
    section("POST /api/pipeline/start + GET /stream: реальный прогон через HTTP")
    with _HttpCtx() as c:
        with _PipelinesDirPatch(c.pipelines_dir):
            _write_def(c.pipelines_dir, TWO_STAGE_DEF)
            code, data = c.post("/api/pipeline/start",
                                {"pipeline": "two_stage", "goal": "цель через HTTP"})
            check("запуск через HTTP успешен (200)", code == 200, str((code, data)))
            pid = data["pipeline_run_id"]
            check("pipeline_run_id получен", pid > 0, str(data))

            st = c.wait_done(pid)
            check("конвейер дошёл до конца", st.get("state") == "done", str(st))
            check("статус конвейера done", st.get("status") == "done", str(st))

            code, details = c.get(f"/api/pipeline/{pid}")
            check("детали доступны через GET /api/pipeline/<id>", code == 200,
                  str((code, details)))
            check("обе стадии done",
                  all(s["status"] == "done" for s in details["stages"]),
                  str(details))

            code, runs = c.get("/api/pipeline_runs")
            check("конвейер виден в истории", any(r["id"] == pid for r in runs["runs"]),
                  str(runs))


def test_api_pipeline_stream_real_ndjson_events() -> None:
    section("GET /api/pipeline/<id>/stream: реальные NDJSON-события до pipeline_finish")
    with _HttpCtx() as c:
        with _PipelinesDirPatch(c.pipelines_dir):
            _write_def(c.pipelines_dir, TWO_STAGE_DEF)
            FakeLLMHandler.delay = 0.15   # чтобы стрим успел отдать события ДО конца
            code, data = c.post("/api/pipeline/start",
                                {"pipeline": "two_stage", "goal": "цель"})
            pid = data["pipeline_run_id"]

            req = urllib.request.Request(
                f"{c.base}/api/pipeline/{pid}/stream",
                headers={"Authorization": "Bearer tok"})
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
                    if e.get("event") == "pipeline_finish":
                        break
            check("события реально пришли", len(kinds) > 0, str(kinds))
            check("pipeline_start пришёл первым событием", kinds[0] == "pipeline_start",
                  str(kinds))
            check("pipeline_finish пришёл", "pipeline_finish" in kinds, str(kinds))
            check("stage_start/stage_done видны в потоке",
                  "stage_start" in kinds and "stage_done" in kinds, str(kinds))


def test_api_pipeline_stop() -> None:
    section("POST /api/pipeline/<id>/stop: реально прерывает конвейер между стадиями")
    with _HttpCtx() as c:
        with _PipelinesDirPatch(c.pipelines_dir):
            _write_def(c.pipelines_dir, TWO_STAGE_DEF)
            FakeLLMHandler.delay = 0.4
            code, data = c.post("/api/pipeline/start",
                                {"pipeline": "two_stage", "goal": "цель"})
            pid = data["pipeline_run_id"]
            time.sleep(0.15)
            code, res = c.post(f"/api/pipeline/{pid}/stop", {})
            check("остановка принята (200)", code == 200, str((code, res)))

            st = c.wait_done(pid)
            check("конвейер остановлен, а не доработал план",
                  st.get("status") == "stopped", str(st))

            code, details = c.get(f"/api/pipeline/{pid}")
            statuses = [s["status"] for s in details["stages"]]
            check("хотя бы одна стадия помечена skipped после остановки",
                  "skipped" in statuses, str(statuses))


def test_api_pipeline_start_unknown_pipeline_rejected() -> None:
    section("POST /api/pipeline/start: неизвестное имя конвейера отклонено (400)")
    with _HttpCtx() as c:
        code, data = c.post("/api/pipeline/start",
                            {"pipeline": "does_not_exist_at_all", "goal": "x"})
        check("отказ на неизвестный конвейер (400)", code == 400, str((code, data)))


def test_api_pipeline_stop_without_running_is_error() -> None:
    section("POST /api/pipeline/<id>/stop без активного конвейера — ошибка")
    with _HttpCtx() as c:
        code, data = c.post("/api/pipeline/999999/stop", {})
        check("отказ на несуществующий/незапущенный конвейер (400)", code == 400,
              str((code, data)))


def test_api_multiple_pipelines_run_in_parallel() -> None:
    section("PipelineManager: два конвейера выполняются параллельно, не мешая друг другу")
    with _HttpCtx() as c:
        with _PipelinesDirPatch(c.pipelines_dir):
            _write_def(c.pipelines_dir, TWO_STAGE_DEF)
            code1, data1 = c.post("/api/pipeline/start",
                                  {"pipeline": "two_stage", "goal": "первый конвейер"})
            code2, data2 = c.post("/api/pipeline/start",
                                  {"pipeline": "two_stage", "goal": "второй конвейер"})
            check("оба запуска приняты одновременно", code1 == 200 and code2 == 200,
                  str((code1, code2)))
            pid1, pid2 = data1["pipeline_run_id"], data2["pipeline_run_id"]
            check("это РАЗНЫЕ pipeline_run_id", pid1 != pid2, str((pid1, pid2)))

            st1 = c.wait_done(pid1)
            st2 = c.wait_done(pid2)
            check("оба конвейера завершились независимо",
                  st1.get("state") == "done" and st2.get("state") == "done",
                  str((st1, st2)))

            code, d1 = c.get(f"/api/pipeline/{pid1}")
            code, d2 = c.get(f"/api/pipeline/{pid2}")
            check("цели конвейеров не перепутались",
                  d1["run"]["goal"] == "первый конвейер"
                  and d2["run"]["goal"] == "второй конвейер",
                  str((d1["run"]["goal"], d2["run"]["goal"])))


def main() -> int:
    print("=" * 60)
    print("ТЕСТЫ: оркестрация конвейеров (agent/pipeline.py)")
    print("=" * 60)

    test_validate_pipeline_negative_cases()
    test_validate_pipeline_positive_case()
    test_load_pipeline_def_low_level()
    test_pipeline_runner_full_success()
    test_pipeline_runner_stage_failure_stops_pipeline()
    test_pipeline_runner_stop_event_skips_remaining()
    test_real_pipeline_definitions_are_valid()
    test_real_pipeline_profiles_exist()
    test_api_pipelines_list_and_requires_token()
    test_api_pipeline_start_and_stream()
    test_api_pipeline_stream_real_ndjson_events()
    test_api_pipeline_stop()
    test_api_pipeline_start_unknown_pipeline_rejected()
    test_api_pipeline_stop_without_running_is_error()
    test_api_multiple_pipelines_run_in_parallel()

    print("\n" + "=" * 60)
    print(f"пройдено: {PASS} · провалено: {FAIL}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
