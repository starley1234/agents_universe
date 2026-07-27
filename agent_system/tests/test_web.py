"""Тесты веб-интерфейса: вопросы агента, история прогонов, план.

Всё на НАСТОЯЩЕМ сервере и настоящих сокетах. Заглушка здесь была бы
самообманом: главное, что проверяется, — агент в одном потоке реально
ждёт ответа, который приходит ДРУГИМ HTTP-запросом. Такое взаимодействие
на моках не воспроизводится.

Отдельно проверяется, что агент не зависает навсегда: истёкший вопрос
возвращает пустую строку, а не держит поток.
"""
from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import Config                          # noqa: E402
from agent.server import Handler                         # noqa: E402
from agent.store import Store                            # noqa: E402
from agent.webio import MAX_PENDING, QuestionBox         # noqa: E402

from http.server import ThreadingHTTPServer              # noqa: E402

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


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def req(port: int, path: str, data: dict | None = None,
        token: str = "") -> tuple[int, dict]:
    url = f"http://127.0.0.1:{port}{path}"
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(url, data=body, method="POST" if data
                               is not None else "GET")
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw.strip().startswith(
                ("{", "[")) else {"raw": raw})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


# ══════════════════════ ящик вопросов ═══════════════════════════════
def test_box_basic() -> None:
    section("Ящик вопросов: ожидание и ответ")
    box = QuestionBox(timeout=5)
    got: list[str] = []

    def agent_side() -> None:
        got.append(box.ask("Какой материал?", ["сталь", "алюминий"]))

    t = threading.Thread(target=agent_side, daemon=True)
    t.start()
    time.sleep(0.2)

    pend = box.pending()
    check("вопрос виден ожидающим", len(pend) == 1, str(pend))
    check("текст и варианты сохранены",
          pend[0]["question"] == "Какой материал?"
          and pend[0]["options"] == ["сталь", "алюминий"], str(pend[0]))

    check("ответ принят", box.answer(pend[0]["id"], "сталь"))
    t.join(timeout=5)
    check("агент получил ИМЕННО этот ответ", got == ["сталь"], str(got))
    check("отвеченный вопрос убран из очереди", box.pending() == [])
    check("повторный ответ отвергнут", not box.answer(pend[0]["id"], "х"))


def test_box_timeout() -> None:
    section("Ящик вопросов: агент не ждёт вечно")
    box = QuestionBox(timeout=0.4)
    t0 = time.time()
    answer = box.ask("Ждать ли ответа?")
    took = time.time() - t0
    check("по таймауту вернулась пустая строка", answer == "", repr(answer))
    check(f"ожидание прекращено за {took:.1f} с", took < 3.0, f"{took:.1f}")
    check("истёкший вопрос убран", box.pending() == [])


def test_box_skip_and_limit() -> None:
    section("Ящик вопросов: «решай сам» и защита от лавины")
    box = QuestionBox(timeout=5)
    got: list[str] = []
    threading.Thread(target=lambda: got.append(box.ask("Продолжать?")),
                     daemon=True).start()
    time.sleep(0.2)
    qid = box.pending()[0]["id"]
    check("снятие вопроса принято", box.drop(qid))
    time.sleep(0.3)
    check("при «решай сам» агент получает пустую строку", got == [""],
          str(got))

    # Зациклившийся агент не должен копить вопросы без предела.
    # Таймаут берём заведомо длинный: с коротким вопросы истекают
    # быстрее, чем накапливаются, и предел не проверяется вовсе —
    # тест выглядел бы зелёным при снятой защите.
    box2 = QuestionBox(timeout=30)
    extra = 6
    for i in range(MAX_PENDING + extra):
        threading.Thread(target=lambda i=i: box2.ask(f"вопрос {i}"),
                         daemon=True).start()
    time.sleep(0.6)
    n = len(box2.pending())
    check(f"очередь ограничена {MAX_PENDING}, а не {MAX_PENDING + extra}",
          n == MAX_PENDING, f"в очереди {n}")
    check("лишние вопросы не заблокировали свои потоки навсегда",
          box2.clear() == MAX_PENDING, "разбужено не столько, сколько ждало")


# ══════════════════════ живой сервер ════════════════════════════════
class Server:
    def __init__(self, cfg: Config) -> None:
        self.port = free_port()
        Handler.cfg = cfg
        Handler.token = None
        Handler.box = QuestionBox(timeout=10)
        self.box = Handler.box
        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.th = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.th.start()
        time.sleep(0.15)

    def stop(self) -> None:
        self.box.clear()
        self.srv.shutdown()
        self.srv.server_close()


def test_server_questions() -> None:
    section("Сервер: вопрос агента и ответ другим запросом")
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(provider="ollama", model="m", workspace=td)
        cfg.db = str(Path(td) / "a.db")
        srv = Server(cfg)
        try:
            code, d = req(srv.port, "/questions")
            check("список вопросов пуст", code == 200
                  and d["questions"] == [], str(d))

            # агент в отдельном потоке спрашивает и ждёт
            got: list[str] = []
            threading.Thread(
                target=lambda: got.append(
                    srv.box.ask("Ставить фаску?", ["да", "нет"])),
                daemon=True).start()
            time.sleep(0.2)

            code, d = req(srv.port, "/questions")
            check("вопрос виден через HTTP", code == 200
                  and len(d["questions"]) == 1, str(d))
            qid = d["questions"][0]["id"]
            check("варианты дошли до страницы",
                  d["questions"][0]["options"] == ["да", "нет"], str(d))

            code, d = req(srv.port, "/answer", {"id": qid, "text": "да"})
            check("ответ принят сервером", code == 200, str(d))
            time.sleep(0.3)
            check("агент разблокирован и получил ответ", got == ["да"],
                  str(got))

            # ответ на несуществующий вопрос — честная ошибка
            code, d = req(srv.port, "/answer", {"id": 999, "text": "х"})
            check("ответ на истёкший вопрос отвергнут", code == 404, str(d))
            check("сказано, почему", "неактуален" in str(d.get("error", "")),
                  str(d))

            # «решай сам»
            got2: list[str] = []
            threading.Thread(
                target=lambda: got2.append(srv.box.ask("Продолжать?")),
                daemon=True).start()
            time.sleep(0.2)
            qid2 = req(srv.port, "/questions")[1]["questions"][0]["id"]
            req(srv.port, "/answer", {"id": qid2, "text": "", "skip": True})
            time.sleep(0.3)
            check("«решай сам» доходит до агента", got2 == [""], str(got2))
        finally:
            srv.stop()


def test_server_runs() -> None:
    section("Сервер: история прогонов")
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "a.db")
        st = Store(db)
        rid = st.start_run("собрать отчёт", "cad")
        ids = st.add_tasks(rid, ["измерить", "проверить", "написать"])
        st.set_task(ids[0], "done", "зазор 0.48")
        st.set_task(ids[1], "blocked", "ВОПРОС: какой шаблон?")
        st.bump_run(rid, steps=7, calls=12, tok_in=5000, tok_out=800,
                    cost=0.0123)
        st.log_event(rid, 1, "tool", "stl_check", "дыра в сетке")
        st.finish_run(rid, "stopped")
        st.close()

        cfg = Config(provider="ollama", model="m", workspace=td)
        cfg.db = db
        srv = Server(cfg)
        try:
            code, d = req(srv.port, "/runs")
            check("список прогонов отдан", code == 200
                  and len(d["runs"]) == 1, str(d)[:120])
            r = d["runs"][0]
            check("расход виден", r["tok_in"] == 5000
                  and abs(r["cost"] - 0.0123) < 1e-9, str(r))
            check("роль видна", r["profile"] == "cad", str(r.get("profile")))

            code, d = req(srv.port, f"/runs/{rid}")
            check("прогон целиком отдан", code == 200, str(d)[:120])
            check("план на месте", len(d["tasks"]) == 3, str(len(d["tasks"])))
            check("заблокированный пункт помечен",
                  any(t["status"] == "blocked" for t in d["tasks"]))
            check("журнал на месте", len(d["events"]) == 1, str(d["events"]))

            code, d = req(srv.port, "/runs/999")
            check("несуществующий прогон — 404", code == 404, str(d))
            code, d = req(srv.port, "/runs/abc")
            check("кривой номер — 400", code == 400, str(d))
        finally:
            srv.stop()


def test_server_plan() -> None:
    section("Сервер: правка плана человеком")
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "a.db")
        st = Store(db)
        rid = st.start_run("цель")
        ids = st.add_tasks(rid, ["раз", "два"])
        st.close()

        cfg = Config(provider="ollama", model="m", workspace=td)
        cfg.db = db
        srv = Server(cfg)
        try:
            code, d = req(srv.port, "/plan",
                          {"run_id": rid, "task_id": ids[0],
                           "status": "done", "result": "закрыто вручную"})
            check("пункт закрыт", code == 200, str(d))

            st2 = Store(db)
            row = [t for t in st2.tasks(rid) if t["id"] == ids[0]][0]
            check("состояние сохранено в базе", row["status"] == "done",
                  row["status"])
            check("причина сохранена", "вручную" in (row["result"] or ""),
                  str(row["result"]))
            st2.close()

            code, d = req(srv.port, "/plan",
                          {"run_id": rid, "task_id": ids[1],
                           "status": "чепуха"})
            check("недопустимое состояние отвергнуто", code == 400, str(d))
            check("названы допустимые", "done" in str(d.get("error", "")),
                  str(d))
        finally:
            srv.stop()


def test_server_dispatch() -> None:
    section("Сервер: кто возьмёт задачу")
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(provider="ollama", model="m", workspace=td)
        cfg.db = str(Path(td) / "a.db")
        srv = Server(cfg)
        try:
            code, d = req(srv.port, "/dispatch",
                          {"task": "сделай презентацию по итогам квартала"})
            check("профиль выбран", code == 200 and d["profile"] == "office",
                  str(d))
            check("выбор объяснён", bool(d.get("explain")), str(d))

            code, d = req(srv.port, "/dispatch",
                          {"task": "в течение ночи собери обзор рынка"})
            check("долгая задача помечена автономной", d["autonomous"] is True,
                  str(d))

            code, d = req(srv.port, "/dispatch", {"task": "приберись"})
            check("непонятная задача — без профиля", d["profile"] is None,
                  str(d))

            code, d = req(srv.port, "/dispatch", {"task": ""})
            check("пустая задача отвергнута", code == 400, str(d))
        finally:
            srv.stop()


def test_server_auth() -> None:
    section("Сервер: токен закрывает данные")
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(provider="ollama", model="m", workspace=td)
        cfg.db = str(Path(td) / "a.db")
        srv = Server(cfg)
        Handler.token = "s3cret-token"
        try:
            code, _ = req(srv.port, "/runs")
            check("без токена данные закрыты", code == 401, str(code))
            code, _ = req(srv.port, "/answer", {"id": 1, "text": "x"})
            check("ответ без токена не принят", code == 401, str(code))
            code, d = req(srv.port, "/runs", token="s3cret-token")
            check("с токеном данные открыты", code == 200, str(code))
            code, d = req(srv.port, "/runs", token="wrong-token")
            check("неверный токен отвергнут", code == 401, str(code))

            # страница отдаётся без токена: она пустая оболочка
            code, d = req(srv.port, "/")
            check("страница доступна без токена", code == 200, str(code))
            check("страница — это HTML", "<html" in str(d.get("raw", ""))[:200])
        finally:
            Handler.token = None
            srv.stop()


def test_page_has_parts() -> None:
    section("Страница: нужные части на месте")
    f = Path(__file__).resolve().parents[1] / "agent" / "web" / "index.html"
    html = f.read_text(encoding="utf-8")
    for part, why in [
        ("/questions", "опрос вопросов после перезагрузки вкладки"),
        ("/answer", "отправка ответа"),
        ("/runs", "история прогонов"),
        ("/plan", "правка плана"),
        ("/dispatch", "показ выбранного профиля"),
        ("showAsk", "карточка вопроса"),
        ("escapeHtml", "защита от подстановки разметки"),
    ]:
        check(f"есть {part} ({why})", part in html)
    check("страница осталась одним файлом без зависимостей",
          "<script src" not in html and "cdn" not in html.lower())
    check("размер разумный", len(html) < 40_000, f"{len(html)} байт")


def main() -> int:
    print("=" * 60)
    print("ТЕСТЫ ВЕБ-ИНТЕРФЕЙСА")
    print("=" * 60)
    test_box_basic()
    test_box_timeout()
    test_box_skip_and_limit()
    test_server_questions()
    test_server_runs()
    test_server_plan()
    test_server_dispatch()
    test_server_auth()
    test_page_has_parts()
    print("\n" + "=" * 60)
    print(f"пройдено: {PASS} · провалено: {FAIL}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
