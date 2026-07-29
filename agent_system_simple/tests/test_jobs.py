"""Тесты очереди заданий: «поставил и забыл».

Смысл очереди в том, что человек уходит. Значит, проверять надо не
«задание выполнилось», а что система переживает всё, что случается,
пока за ней не смотрят: смерть исполнителя, перезагрузку сервера,
двух исполнителей на одну очередь, задание, которое роняет процесс.

Худший исход здесь — задание, молча застрявшее в «running» навсегда.
Человек считает, что работа идёт, а её нет.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import Config                            # noqa: E402
from agent.store import Store                              # noqa: E402
import agent.autorun as A                                  # noqa: E402
import agent.worker as W                                   # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def section(t: str) -> None:
    print(f"\n{t}\n" + "─" * len(t))


class FakeRes:
    def __init__(self, run_id: int = 1, stopped_by: str = "done") -> None:
        self.run_id = run_id
        self.stopped_by = stopped_by
        self.summary = "план выполнен: 3 из 3"


def _fake_run(delay: float = 0.05, stopped_by: str = "done",
              boom: bool = False):
    def run(self, goal, profile=None, resume=None):
        self.store.start_run(goal, profile)
        time.sleep(delay)
        if boom:
            raise RuntimeError("прогон рухнул")
        return FakeRes(stopped_by=stopped_by)
    return run


def _cfg(td: str) -> Config:
    c = Config(provider="ollama", model="m", workspace=td)
    c.db = str(Path(td) / "a.db")
    return c


# ═══════════════════════ очередь в базе ═════════════════════════════
def test_queue_basics() -> None:
    section("Очередь: постановка, порядок, атомарность")
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "a.db")
        a = st.add_job("первая задача", profile="docs", hours=2,
                       notify="boss@firma.ru")
        b = st.add_job("вторая задача")
        check("задания поставлены", (a, b) == (1, 2), f"{a},{b}")
        check("параметры сохранены",
              st.get_job(a)["notify"] == "boss@firma.ru"
              and st.get_job(a)["hours"] == 2)

        got = st.take_job("srv:1")
        check("берётся первое по очереди", got["id"] == a, str(got["id"]))
        check("отмечено, кто взял", got["worker"] == "srv:1", got["worker"])
        check("статус running", got["status"] == "running", got["status"])

        # Два исполнителя не должны взять одно задание.
        got2 = st.take_job("srv:2")
        check("второй исполнитель берёт СЛЕДУЮЩЕЕ",
              got2 and got2["id"] == b, str(got2 and got2["id"]))
        check("третьему ничего не досталось", st.take_job("srv:3") is None)

        st.finish_job(a, "done", "готово")
        check("завершение записано",
              st.get_job(a)["status"] == "done"
              and st.get_job(a)["result"] == "готово")
        st.close()


def test_queue_race() -> None:
    section("Очередь: гонка десяти исполнителей за одно задание")
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "a.db")
        st.add_job("единственная задача")
        taken: list[dict] = []
        lock = threading.Lock()

        def grab(n: int) -> None:
            s = Store(Path(td) / "a.db")
            j = s.take_job(f"srv:{n}")
            if j:
                with lock:
                    taken.append(j)
            s.close()

        threads = [threading.Thread(target=grab, args=(i,))
                   for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        check("задание досталось ровно одному", len(taken) == 1,
              f"взяли {len(taken)}")

        # Гонка «в лоб» не воспроизводится: SQLite сериализует запись, и
        # потоки успевают по очереди. Настоящая гонка — когда ВСЕ уже
        # прочитали очередь, а пишут одновременно. Барьер это и создаёт.
        # Без условия `AND status='queued'` в UPDATE задание тут достаётся
        # всем восьмерым, и работа выполняется восемь раз.
        st.add_job("вторая единственная задача")
        got: list[int] = []
        barrier = threading.Barrier(8)

        def race(n: int) -> None:
            s = Store(Path(td) / "a.db")
            row = s.db.execute(
                "SELECT id FROM job WHERE status='queued' "
                "ORDER BY id LIMIT 1").fetchone()
            barrier.wait()
            if row:
                cur = s.db.execute(
                    "UPDATE job SET status='running', worker=? "
                    "WHERE id=? AND status='queued'", (f"srv:{n}", row["id"]))
                s.db.commit()
                if cur.rowcount:
                    with lock:
                        got.append(n)
            s.close()

        racers = [threading.Thread(target=race, args=(i,))
                  for i in range(8)]
        for t in racers:
            t.start()
        for t in racers:
            t.join(timeout=10)
        check("при одновременной записи побеждает один", len(got) == 1,
              f"взяли {len(got)} — работа выполнилась бы столько раз")
        st.close()


def test_stale_revive() -> None:
    section("Умерший исполнитель: задание возвращается в очередь")
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "a.db")
        jid = st.add_job("долгая задача")
        st.take_job("srv:умер")
        # Изображаем исполнителя, который молчит десять минут.
        st.db.execute("UPDATE job SET heartbeat=? WHERE id=?",
                      (time.time() - 600, jid))
        st.db.commit()

        check("пока не подобрали — задание висит в running",
              st.get_job(jid)["status"] == "running")
        n = st.revive_stale_jobs(older_than=300)
        check("задание возвращено в очередь", n == 1, str(n))
        check("статус снова queued",
              st.get_job(jid)["status"] == "queued")
        check("другой исполнитель его получит",
              st.take_job("srv:живой")["id"] == jid)

        # Свежая отметка — трогать нельзя, работа идёт.
        st.beat_job(jid)
        check("живое задание не отбирают",
              st.revive_stale_jobs(older_than=300) == 0)
        st.close()


def test_stale_gives_up() -> None:
    section("Задание, роняющее исполнителя, не крутится вечно")
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "a.db")
        jid = st.add_job("ядовитая задача")
        for _ in range(3):
            st.take_job("srv:жертва")
            st.db.execute("UPDATE job SET heartbeat=? WHERE id=?",
                          (time.time() - 600, jid))
            st.db.commit()
            st.revive_stale_jobs(older_than=300)

        row = st.get_job(jid)
        check("после трёх попыток задание признано негодным",
              row["status"] == "failed", row["status"])
        check("причина названа",
              "попытки исчерпаны" in (row["result"] or ""),
              row["result"] or "")
        check("в очередь больше не попадает", st.take_job("srv:x") is None)
        st.close()


# ══════════════════════ фоновый исполнитель ═════════════════════════
def test_worker_runs_jobs() -> None:
    section("Исполнитель: берёт задания и доводит до конца")
    real = A.AutoRunner.run
    A.AutoRunner.run = _fake_run()
    try:
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            st = Store(cfg.db)
            st.add_job("первая работа")
            st.add_job("вторая работа", profile="docs")

            w = W.Worker(cfg, poll=0.05)
            th = threading.Thread(target=w.run, daemon=True)
            th.start()
            deadline = time.time() + 10
            while time.time() < deadline:
                if all(j["status"] == "done" for j in st.jobs()):
                    break
                time.sleep(0.1)
            w.stopping = True
            th.join(timeout=5)

            jobs = st.jobs()
            check("оба задания выполнены",
                  all(j["status"] == "done" for j in jobs),
                  str([(j["id"], j["status"]) for j in jobs]))
            check("итог прогона сохранён в задании",
                  all("план выполнен" in (j["result"] or "") for j in jobs))
            check("прогон привязан к заданию",
                  all(j["run_id"] for j in jobs),
                  str([j["run_id"] for j in jobs]))
            st.close()
    finally:
        A.AutoRunner.run = real


def test_worker_survives_crash() -> None:
    section("Исполнитель: упавшее задание не роняет очередь")
    real = A.AutoRunner.run
    A.AutoRunner.run = _fake_run(boom=True)
    try:
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            st = Store(cfg.db)
            st.add_job("задача, которая рухнет")
            w = W.Worker(cfg, poll=0.05, once=True)
            w.run()
            row = st.get_job(1)
            check("задание помечено провалившимся",
                  row["status"] == "failed", row["status"])
            check("причина сохранена, а не потеряна",
                  "рухнул" in (row["result"] or ""),
                  (row["result"] or "")[:80])
            st.close()

        # Следующее задание после падения всё равно берётся.
        A.AutoRunner.run = _fake_run()
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            st = Store(cfg.db)
            st.add_job("нормальная задача")
            W.Worker(cfg, poll=0.05, once=True).run()
            check("следующее задание выполнено",
                  st.get_job(1)["status"] == "done",
                  st.get_job(1)["status"])
            st.close()
    finally:
        A.AutoRunner.run = real


def test_worker_stop_from_outside() -> None:
    section("Задание можно снять во время работы")
    real = A.AutoRunner.run

    def slow(self, goal, profile=None, resume=None):
        self.store.start_run(goal, profile)
        for i in range(50):
            # Исполнитель проверяет отмену между итерациями.
            self._emit("iteration", n=i, task="шаг", task_id=1, left=0)
            time.sleep(0.05)
        return FakeRes()

    A.AutoRunner.run = slow
    try:
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            st = Store(cfg.db)
            jid = st.add_job("длинная работа")
            w = W.Worker(cfg, poll=0.05, once=True)
            th = threading.Thread(target=w.run, daemon=True)
            th.start()
            time.sleep(0.4)
            check("задание работает",
                  st.get_job(jid)["status"] == "running",
                  st.get_job(jid)["status"])
            st.stop_job(jid)
            th.join(timeout=6)
            row = st.get_job(jid)
            check("задание снято, а не доработало до конца",
                  row["status"] == "stopped", row["status"])
            check("сказано, что остановлено человеком",
                  "человеком" in (row["result"] or ""),
                  row["result"] or "")
            st.close()
    finally:
        A.AutoRunner.run = real


def test_worker_notifies() -> None:
    section("Исполнитель зовёт человека, когда всё готово")
    real = A.AutoRunner.run
    A.AutoRunner.run = _fake_run()
    try:
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            cfg.comms.allow_to = ["boss@firma.ru"]
            st = Store(cfg.db)
            st.add_job("работа с уведомлением", notify="boss@firma.ru")

            events: list[tuple[str, dict]] = []
            w = W.Worker(cfg, poll=0.05, once=True,
                         on_event=lambda k, d: events.append((k, d)))
            w.run()
            kinds = [k for k, _ in events]
            # SMTP не настроен, письмо ляжет черновиком — но попытка
            # должна быть, и о ней должно быть событие.
            check("попытка сообщить сделана",
                  "notified" in kinds or "notify_failed" in kinds,
                  str(set(kinds)))
            drafts = list((Path(td) / "outbox").glob("*.txt"))
            check("без SMTP письмо легло черновиком", len(drafts) == 1,
                  str(drafts))
            if drafts:
                body = drafts[0].read_text(encoding="utf-8")
                check("в черновике есть итог работы",
                      "план выполнен" in body, body[:120])
            st.close()
    finally:
        A.AutoRunner.run = real


def test_worker_no_notify() -> None:
    section("Без адреса никто никого не беспокоит")
    real = A.AutoRunner.run
    A.AutoRunner.run = _fake_run()
    try:
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            st = Store(cfg.db)
            st.add_job("тихая работа")
            W.Worker(cfg, poll=0.05, once=True).run()
            check("черновиков не создано",
                  not (Path(td) / "outbox").exists())
            check("задание всё равно выполнено",
                  st.get_job(1)["status"] == "done")
            st.close()
    finally:
        A.AutoRunner.run = real


# ══════════════════════════ через HTTP ══════════════════════════════
def test_http_jobs() -> None:
    section("Постановка задания через веб")
    import json
    import socket
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer
    from agent.server import Handler
    from agent.webio import QuestionBox

    def free_port() -> int:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p

    def req(port, path, data=None):
        url = f"http://127.0.0.1:{port}{path}"
        body = json.dumps(data).encode() if data is not None else None
        r = urllib.request.Request(url, data=body,
                                   method="POST" if data else "GET")
        r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode() or "{}")

    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(td)
        port = free_port()
        Handler.cfg = cfg
        Handler.token = None
        Handler.box = QuestionBox()
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(0.2)
        try:
            code, d = req(port, "/jobs",
                          {"goal": "разобрать договоры из in/ в реестр",
                           "hours": 3, "notify": "boss@firma.ru"})
            check("задание принято", code == 200 and d["id"] == 1, str(d))
            check("роль подобрана сама", d["profile"] == "docs", str(d))
            check("выбор объяснён человеку", bool(d.get("reason")), str(d))

            code, d = req(port, "/jobs")
            check("очередь отдаётся", code == 200 and len(d["jobs"]) == 1,
                  str(d)[:100])
            check("параметры сохранились",
                  d["jobs"][0]["hours"] == 3
                  and d["jobs"][0]["notify"] == "boss@firma.ru")

            code, d = req(port, "/jobs", {"goal": "   "})
            check("пустая задача отвергнута", code == 400, str(d))

            code, d = req(port, "/jobs/stop", {"id": 1})
            check("задание снимается через веб", code == 200, str(d))
            code, d = req(port, "/jobs")
            check("статус изменился на stopped",
                  d["jobs"][0]["status"] == "stopped",
                  d["jobs"][0]["status"])
            code, d = req(port, "/jobs/stop", {"id": 999})
            check("снятие несуществующего — честная ошибка", code == 404,
                  str(d))
        finally:
            srv.shutdown()
            srv.server_close()


def test_job_shows_current_step() -> None:
    section("Задание показывает, кто работает прямо сейчас")
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "a.db")
        jid = st.add_job("собрать комплект", profile="office")
        st.take_job("srv:1")
        rid = st.start_run("собрать комплект", "office")
        st.beat_job(jid, rid)
        ids = st.add_steps(rid, [
            {"title": "измерить зазоры", "profile": "cad"},
            {"title": "посчитать нагрузку", "profile": "coder", "needs": [1]},
            {"title": "оформить отчёт", "profile": "office", "needs": [2]}])
        st.set_task(ids[0], "done", "0.48 мм")
        st.set_task(ids[1], "doing")
        st.log_event(rid, 0, "orchestrate", "добавить",
                     "замеры на границе допуска — нужна сверка")

        job = st.jobs()[0]
        now = job.get("now") or {}
        # Без этого веб и CLI показывают «работает» часами и всё.
        check("виден текущий шаг", now.get("step") == "посчитать нагрузку",
              str(now.get("step")))
        check("виден агент, который его делает", now.get("agent") == "coder",
              str(now.get("agent")))
        check("шаг помечен как идущий сейчас", now.get("doing") is True)
        check("видно продвижение", (now.get("done"), now.get("total"))
              == (1, 3), f"{now.get('done')}/{now.get('total')}")
        check("видно решение оркестратора",
              "добавить" in (now.get("decision") or ""),
              str(now.get("decision")))

        # Задание без прогона не должно ломать выдачу.
        st.add_job("ещё не начатое задание")
        fresh = [j for j in st.jobs() if j["id"] != jid][0]
        check("непачатое задание отдаётся без ошибки",
              fresh.get("now") == {}, str(fresh.get("now")))
        st.close()


def test_page_shows_orchestrator() -> None:
    section("Страница: решения оркестратора видны, а не тонут в журнале")
    html = (Path(__file__).resolve().parents[1] / "agent" / "web"
            / "index.html").read_text(encoding="utf-8")
    for part, why in [
        ("n.step", "текущий шаг в карточке задания"),
        ("n.agent", "кто его делает"),
        ("n.decision", "решение оркестратора"),
        ("'orchestrate'", "выделение в журнале прогона"),
        ("оркестратор по ходу решает", "объяснение человеку"),
    ]:
        check(f"есть {part} ({why})", part.lower() in html.lower())


def test_page_has_jobs_tab() -> None:
    section("Страница: вкладка заданий на месте")
    html = (Path(__file__).resolve().parents[1] / "agent" / "web"
            / "index.html").read_text(encoding="utf-8")
    for part, why in [
        ('data-view="jobs"', "вкладка заданий"),
        ("loadJobs", "загрузка очереди"),
        ("addJob", "постановка задания"),
        ("stopJob", "снятие задания"),
        ("setInterval", "самообновление списка"),
        ("закрыть вкладку", "объяснение, что можно уйти"),
    ]:
        check(f"есть {part} ({why})", part in html)
    check("задания — первая вкладка",
          html.index('data-view="jobs"') < html.index('data-view="work"'),
          "экран «поставил и забыл» должен открываться первым")


def test_systemd_units() -> None:
    section("Файлы для systemd")
    d = Path(__file__).resolve().parents[1] / "deploy"
    for name in ("agent-worker.service", "agent-web.service",
                 "agent-backup.service", "agent-backup.timer"):
        check(f"{name} на месте", (d / name).is_file())
    unit = (d / "agent-worker.service").read_text(encoding="utf-8")
    check("исполнитель перезапускается сам", "Restart=always" in unit)
    check("память ограничена", "MemoryMax" in unit,
          "на сервере с 1 ГБ без предела опасно")
    check("поднимается после перезагрузки",
          "WantedBy=multi-user.target" in unit)
    check("читает .env", "EnvironmentFile" in unit)


# ════════════════════════ упорство ══════════════════════════════════
def test_continue_after_timeout() -> None:
    section("Упорство: кончилось время — работа продолжается")
    real = A.AutoRunner.run
    state = {"done": 0}

    class Res:
        run_id = 1

        def __init__(self, sb: str, sm: str) -> None:
            self.stopped_by, self.summary = sb, sm

    def stepwise(self, goal, profile=None, resume=None):
        """Каждый подход доделывает 2 пункта из 6 и упирается в время."""
        if not resume:
            rid = self.store.start_run(goal, profile)
            self.store.add_tasks(rid, [f"пункт {i}" for i in range(1, 7)])
        rid = 1
        for t in self.store.tasks(rid):
            if t["status"] == "open" and state["done"] < 6:
                self.store.set_task(t["id"], "done", "ок")
                state["done"] += 1
                if state["done"] % 2 == 0:
                    break
        left = [t for t in self.store.tasks(rid) if t["status"] == "open"]
        return Res("done" if not left else "time",
                   f"Прогон #1\nПлан: {6 - len(left)} из 6 пунктов")

    A.AutoRunner.run = stepwise
    try:
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            st = Store(cfg.db)
            jid = st.add_job("работа на всю ночь", hours=1)

            W.Worker(cfg, poll=0.01, once=True).run()
            row = st.get_job(jid)
            # Раньше здесь было failed и работа бросалась с 2 из 6.
            check("после первого подхода задание НЕ провалено",
                  row["status"] == "queued", row["status"])
            check("продвижение записано", row["progress"] == "2/6",
                  row["progress"])
            check("подход посчитан", row["rounds"] == 1, str(row["rounds"]))

            for _ in range(5):
                st.db.execute("UPDATE job SET next_at=0 WHERE id=?", (jid,))
                st.db.commit()
                W.Worker(cfg, poll=0.01, once=True).run()
                if st.get_job(jid)["status"] in ("done", "failed"):
                    break
            row = st.get_job(jid)
            check("работа доведена до конца сама",
                  row["status"] == "done", row["status"])
            check("все пункты сделаны", "6 из 6" in row["result"],
                  row["result"][:80])
            check("подходов понадобилось несколько", row["rounds"] >= 2,
                  str(row["rounds"]))
            st.close()
    finally:
        A.AutoRunner.run = real


def test_continue_keeps_run() -> None:
    section("Упорство: продолжаем ТОТ ЖЕ прогон, а не начинаем заново")
    real = A.AutoRunner.run
    seen: list = []

    class Res:
        run_id = 1
        stopped_by = "time"
        summary = "Прогон #1\nПлан: 1 из 3 пунктов"

    def remember_resume(self, goal, profile=None, resume=None):
        seen.append(resume)
        if not resume:
            rid = self.store.start_run(goal, profile)
            self.store.add_tasks(rid, ["раз", "два", "три"])
            self.store.set_task(1, "done", "ок")
        return Res()

    A.AutoRunner.run = remember_resume
    try:
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            st = Store(cfg.db)
            jid = st.add_job("длинная работа")
            W.Worker(cfg, poll=0.01, once=True).run()
            st.db.execute("UPDATE job SET next_at=0 WHERE id=?", (jid,))
            st.db.commit()
            W.Worker(cfg, poll=0.01, once=True).run()

            check("первый подход начал новый прогон", seen[0] is None,
                  str(seen[0]))
            # Иначе агент каждый подход делал бы работу с нуля: план,
            # первые пункты, снова план.
            check("второй подход продолжил прежний", seen[1] == 1,
                  str(seen[1]))
            st.close()
    finally:
        A.AutoRunner.run = real


def test_pause_when_no_progress() -> None:
    section("Упорство с умом: без продвижения — пауза, а не долбёжка")
    real = A.AutoRunner.run

    class Res:
        run_id = 1
        stopped_by = "stuck"
        summary = "Прогон #1\nПлан: 0 из 3 пунктов"

    def stuck(self, goal, profile=None, resume=None):
        if not resume:
            rid = self.store.start_run(goal, profile)
            self.store.add_tasks(rid, ["раз", "два", "три"])
        return Res()

    A.AutoRunner.run = stuck
    try:
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            st = Store(cfg.db)
            jid = st.add_job("задача, где агент буксует")
            W.Worker(cfg, poll=0.01, once=True).run()
            row = st.get_job(jid)
            check("задание вернулось в очередь",
                  row["status"] == "queued", row["status"])
            check("назначена пауза перед следующим подходом",
                  row["next_at"] > time.time() + 30,
                  f"через {row['next_at'] - time.time():.0f} с")
            # Пока пауза не вышла, задание не берётся: иначе исполнитель
            # молотит одно и то же в цикле и жжёт деньги.
            check("во время паузы задание не берут",
                  st.take_job("srv:1") is None)
            st.close()
    finally:
        A.AutoRunner.run = real


def test_rounds_limit() -> None:
    section("Упорство не вечно: предел подходов")
    real = A.AutoRunner.run

    class Res:
        run_id = 1
        stopped_by = "stuck"
        summary = "Прогон #1\nПлан: 0 из 3 пунктов"

    def hopeless(self, goal, profile=None, resume=None):
        if not resume:
            self.store.start_run(goal, profile)
        return Res()

    A.AutoRunner.run = hopeless
    try:
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            st = Store(cfg.db)
            jid = st.add_job("невыполнимая задача", max_rounds=3)
            for _ in range(6):
                st.db.execute("UPDATE job SET next_at=0 WHERE id=?", (jid,))
                st.db.commit()
                if not W.Worker(cfg, poll=0.01, once=True).run():
                    break
            row = st.get_job(jid)
            check("после предела подходов задание провалено",
                  row["status"] == "failed", row["status"])
            check("подходов ровно столько, сколько разрешили",
                  row["rounds"] == 3, str(row["rounds"]))
            check("причина названа человеку",
                  "Подходы исчерпаны" in (row["result"] or ""),
                  (row["result"] or "")[-80:])
            check("больше в работу не берётся",
                  st.take_job("srv:1") is None)
            st.close()
    finally:
        A.AutoRunner.run = real


def test_blocked_not_retried() -> None:
    section("Вопрос человеку: продолжать бессмысленно")
    real = A.AutoRunner.run

    class Res:
        run_id = 1
        stopped_by = "blocked"
        summary = "Прогон #1: blocked\nЖДУТ ОТВЕТА ЧЕЛОВЕКА: 1"

    def blocked(self, goal, profile=None, resume=None):
        if not resume:
            self.store.start_run(goal, profile)
        return Res()

    A.AutoRunner.run = blocked
    try:
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            st = Store(cfg.db)
            jid = st.add_job("задача с вопросом")
            W.Worker(cfg, poll=0.01, once=True).run()
            row = st.get_job(jid)
            # Упорство тут ни к чему: без ответа человека агент будет
            # упираться в тот же вопрос сколько угодно раз.
            check("задание не крутится, а ждёт человека",
                  row["status"] == "failed", row["status"])
            check("в итоге виден вопрос",
                  "ЖДУТ ОТВЕТА" in (row["result"] or ""),
                  (row["result"] or "")[:60])
            st.close()
    finally:
        A.AutoRunner.run = real


def main() -> int:
    print("=" * 60)
    print("ТЕСТЫ ОЧЕРЕДИ: поставил и забыл")
    print("=" * 60)
    test_queue_basics()
    test_queue_race()
    test_stale_revive()
    test_stale_gives_up()
    test_worker_runs_jobs()
    test_worker_survives_crash()
    test_worker_stop_from_outside()
    test_continue_after_timeout()
    test_continue_keeps_run()
    test_pause_when_no_progress()
    test_rounds_limit()
    test_blocked_not_retried()
    test_worker_notifies()
    test_worker_no_notify()
    test_http_jobs()
    test_job_shows_current_step()
    test_page_shows_orchestrator()
    test_page_has_jobs_tab()
    test_systemd_units()
    print("\n" + "=" * 60)
    print(f"пройдено: {PASS} · провалено: {FAIL}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
