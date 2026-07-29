"""Тесты: прогон тестов, патчи, векторы, маршрутизация, история прогонов.

Главная ловушка этой части — самообман. Прогонщик тестов, который
говорит «всё прошло», когда тесты упали, опаснее его отсутствия:
человек перестаёт смотреть сам. Поэтому здесь проверяется прежде всего
способность УВИДЕТЬ ПРОВАЛ, в том числе когда код возврата нулевой.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import vectors as V                           # noqa: E402
from agent.llm.base import BaseLLM, LLMReply, Usage      # noqa: E402
from agent.llm.router import Router                      # noqa: E402
from agent.store import Store                            # noqa: E402
from agent.tools import dev as dev_tools                 # noqa: E402
from agent.tools import semantic as sem_tools            # noqa: E402
from agent.tools.base import ToolError, Workspace        # noqa: E402

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


# ═══════════════════════ прогон тестов ══════════════════════════════
GREEN = """import sys
print("  ok   сложение")
print("пройдено: 2 · провалено: 0")
"""

RED = """import sys
print("  ok   сложение")
print("  FAIL умножение — получили 10, ждали 9")
print("пройдено: 1 · провалено: 1")
"""

RED_EXIT = """import sys
print("  FAIL деление")
sys.exit(1)
"""


def _mk(td: str, body: str) -> dict:
    root = Path(td)
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_x.py").write_text(body, encoding="utf-8")
    (root / "Makefile").write_text("test:\n\t@python3 tests/test_x.py\n",
                                   encoding="utf-8")
    return {x.name: x for x in dev_tools.build(Workspace(td))}


def test_run_tests() -> None:
    section("run_tests: провал должен быть виден")
    with tempfile.TemporaryDirectory() as td:
        t = _mk(td, GREEN)
        out = t["run_tests"].fn()
        check("зелёный прогон опознан", "всё прошло" in out, out[:120])
        check("итог показан", "провалено: 0" in out, out[:120])

    # ГЛАВНОЕ: код возврата 0, но в тексте есть провал
    with tempfile.TemporaryDirectory() as td:
        t = _mk(td, RED)
        out = t["run_tests"].fn()
        check("провал при нулевом коде возврата опознан",
              "ЕСТЬ ПРОВАЛЫ" in out, out[:150])
        check("упавшая проверка показана", "умножение" in out, out[:200])
        check("причина провала показана", "ждали 9" in out, out[:200])
        check("сказано, как увидеть всё", "full=true" in out)

    with tempfile.TemporaryDirectory() as td:
        t = _mk(td, RED_EXIT)
        out = t["run_tests"].fn()
        check("ненулевой код возврата опознан", "ЕСТЬ ПРОВАЛЫ" in out,
              out[:120])

    # пустой вывод — не успех
    with tempfile.TemporaryDirectory() as td:
        t = _mk(td, GREEN)
        out = t["run_tests"].fn(command="true")
        check("пустой вывод не выдан за успех",
              "ПУСТОЙ" in out and "не значит" in out, out[:150])

    # вывод обрезается, а не топит контекст
    with tempfile.TemporaryDirectory() as td:
        t = _mk(td, "for i in range(9000): print('строка мусора', i)\n"
                    "print('  FAIL финальная проверка')")
        out = t["run_tests"].fn()
        check("длинный вывод обрезан", len(out) < 40_000, str(len(out)))
        check("упавшее найдено в длинном выводе",
              "финальная проверка" in out, out[-200:])

    # Правка, не изменившая РАЗМЕР файла, обязана попасть в прогон.
    # Найдено живьём: Python считает .pyc свежим по паре «время+размер»,
    # замена `w + h` на `w * h` размер не меняет, и тест падал с прежней
    # ошибкой уже после починки.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "tests").mkdir()
        (root / "calc.py").write_text("def area(w, h):\n    return w + h\n",
                                      encoding="utf-8")
        (root / "tests" / "test_c.py").write_text(
            "import sys; sys.path.insert(0, '.')\n"
            "from calc import area\n"
            "ok = area(3, 4) == 12\n"
            "print(('  ok   ' if ok else '  FAIL ') + 'площадь')\n"
            "print(f'пройдено: {int(ok)} · провалено: {1 - int(ok)}')\n",
            encoding="utf-8")
        (root / "Makefile").write_text("test:\n\t@python3 tests/test_c.py\n",
                                       encoding="utf-8")
        t = {x.name: x for x in dev_tools.build(Workspace(td))}
        first = t["run_tests"].fn()
        check("до починки тест падает", "ЕСТЬ ПРОВАЛЫ" in first, first[:80])
        check("кэш .pyc появился", any(root.rglob("*.pyc")))
        t["apply_patch"].fn(path="calc.py", start_line=2,
                            replacement="    return w * h")
        check("размер файла не изменился (условие ловушки)",
              len("    return w + h\n") == len("    return w * h\n"))
        after = t["run_tests"].fn()
        check("после починки тест проходит — устаревший .pyc не помешал",
              "всё прошло" in after, after[:120])

    # нечем прогонять — честный отказ
    with tempfile.TemporaryDirectory() as td:
        t = {x.name: x for x in dev_tools.build(Workspace(td))}
        try:
            t["run_tests"].fn()
            check("отсутствие тестов — честный отказ", False, "промолчал")
        except ToolError as exc:
            check("отсутствие тестов — честный отказ",
                  "Не понятно" in str(exc), str(exc)[:80])


# ═══════════════════════════ патчи ══════════════════════════════════
def test_apply_patch() -> None:
    section("apply_patch: правка там, где edit_file отказывает")
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(td)
        t = {x.name: x for x in dev_tools.build(ws)}
        f = ws.root / "code.py"

        # по номерам строк
        f.write_text("a=1\nb=2\nc=3\nd=4\n", encoding="utf-8")
        out = t["apply_patch"].fn(path="code.py", start_line=2, end_line=3,
                                  replacement="b=20\nc=30")
        check("строки заменены",
              f.read_text(encoding="utf-8") == "a=1\nb=20\nc=30\nd=4\n",
              f.read_text(encoding="utf-8"))
        check("показано было/стало", "было" in out and "стало" in out)

        # удаление строк пустой заменой
        t["apply_patch"].fn(path="code.py", start_line=1, end_line=1,
                            replacement="")
        check("пустая замена удаляет строку",
              f.read_text(encoding="utf-8") == "b=20\nc=30\nd=4\n",
              f.read_text(encoding="utf-8"))

        # унифицированный диф
        f.write_text("один\nдва\nтри\nчетыре\n", encoding="utf-8")
        t["apply_patch"].fn(path="code.py", patch=(
            "@@ -1,4 +1,4 @@\n один\n-два\n+ДВА\n три\n четыре"))
        check("диф применён",
              f.read_text(encoding="utf-8") == "один\nДВА\nтри\nчетыре\n",
              f.read_text(encoding="utf-8"))

        # НЕУНИКАЛЬНЫЙ фрагмент: edit_file отказывается, apply_patch берёт
        f.write_text("x = 0\nx = 0\nx = 0\n", encoding="utf-8")
        from agent.tools import files as files_tools
        ft = {x.name: x for x in files_tools.build(ws)}
        try:
            ft["edit_file"].fn(path="code.py", old_text="x = 0",
                               new_text="x = 9")
            check("edit_file отказывается от неуникального", False, "заменил")
        except ToolError:
            check("edit_file отказывается от неуникального", True)
        t["apply_patch"].fn(path="code.py", start_line=2, end_line=2,
                            replacement="x = 9")
        check("apply_patch правит нужную строку из одинаковых",
              f.read_text(encoding="utf-8") == "x = 0\nx = 9\nx = 0\n",
              f.read_text(encoding="utf-8"))

        # съехавшие номера строк в дифе: содержимое важнее номеров
        f.write_text("шапка\nшапка2\nодин\nдва\nтри\n", encoding="utf-8")
        t["apply_patch"].fn(path="code.py", patch=(
            "@@ -1,3 +1,3 @@\n один\n-два\n+ДВА\n три"))
        check("диф применён, несмотря на неверные номера строк",
              "ДВА" in f.read_text(encoding="utf-8"),
              f.read_text(encoding="utf-8"))

        # несовпадающий диф — отказ, а не порча файла
        before = f.read_text(encoding="utf-8")
        try:
            t["apply_patch"].fn(path="code.py", patch=(
                "@@ -1,2 +1,2 @@\n такого\n-нет\n+совсем"))
            check("несовпадающий диф отвергнут", False, "применён")
        except ToolError as exc:
            check("несовпадающий диф отвергнут", "не совпал" in str(exc),
                  str(exc)[:70])
        check("файл при отказе не испорчен",
              f.read_text(encoding="utf-8") == before)

        try:
            t["apply_patch"].fn(path="code.py")
            check("правка без аргументов отвергнута", False, "прошла")
        except ToolError:
            check("правка без аргументов отвергнута", True)


# ═══════════════════════════ векторы ════════════════════════════════
def _vec(seed: float, dim: int = 8) -> array:
    return V.normalize([seed + i * 0.1 for i in range(dim)])


def test_vectors() -> None:
    section("Векторы в SQLite")
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    vs = V.VectorStore(db)

    a, b, c = _vec(1.0), _vec(1.01), _vec(-5.0)
    vs.add("fact", 1, "крепление узла болтами", a)
    vs.add("fact", 2, "почти то же самое", b)
    vs.add("fact", 3, "совсем про другое", c)
    check("векторы сохранены", vs.count() == 3, str(vs.count()))

    res = vs.search(a, limit=3)
    check("ближайший — он сам", res[0]["ref_id"] == 1, str(res[0]))
    check("похожий выше непохожего",
          [r["ref_id"] for r in res][:2] == [1, 2], str([r["ref_id"] for r in res]))
    check("близость в разумных пределах",
          0.99 <= res[0]["score"] <= 1.0, str(res[0]["score"]))

    # нормализация: длина вектора равна единице
    n = V.normalize([3.0, 4.0])
    check("вектор нормализован", abs(V.dot(n, n) - 1.0) < 1e-6,
          str(V.dot(n, n)))
    check("нулевой вектор не роняет", V.normalize([0.0, 0.0]) is not None)

    # повторная запись обновляет, а не плодит
    vs.add("fact", 1, "новый текст", a)
    check("повтор обновляет запись", vs.count() == 3, str(vs.count()))
    check("текст обновлён",
          vs.search(a, limit=1)[0]["text"] == "новый текст")

    # ВАЖНО: разная размерность не сравнивается
    vs.add("fact", 9, "другая модель", V.normalize([1.0] * 16))
    res2 = vs.search(a, limit=10)
    check("вектор чужой размерности пропущен",
          all(r["ref_id"] != 9 for r in res2),
          str([r["ref_id"] for r in res2]))

    check("порог отсекает далёкое",
          all(r["score"] >= 0.9 for r in vs.search(a, min_score=0.9)))

    st = vs.stats()
    check("статистика считает память", st["bytes"] > 0, str(st))
    check("удаление работает", vs.drop("fact") == 4 and vs.count() == 0)


def test_vectors_memory() -> None:
    section("Векторы: расход памяти на 5000 записей")
    # Проверяем численно то, ради чего выбран array('f'): 5k×384 должно
    # умещаться в единицы мегабайт, иначе на сервере с 1 ГБ нельзя.
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    vs = V.VectorStore(db)
    dim, n = 384, 5000
    base = array("f", [0.05] * dim)
    blob = V.normalize(base)
    import time
    t0 = time.time()
    db.executemany(
        "INSERT INTO vec(ref_kind,ref_id,text,dim,data,created) "
        "VALUES('fact',?,?,?,?,0)",
        [(i, f"факт {i}", dim, blob.tobytes()) for i in range(n)])
    db.commit()
    fill = time.time() - t0

    t0 = time.time()
    res = vs.search(blob, limit=10)
    took = (time.time() - t0) * 1000
    check(f"{n} векторов записаны за {fill:.1f} с", vs.count() == n)
    check(f"поиск по {n}×{dim} занял {took:.0f} мс — приемлемо",
          took < 3000, f"{took:.0f} мс")
    check("поиск вернул результат", len(res) == 10, str(len(res)))
    st = vs.stats()
    mb = st["bytes"] / 1024 / 1024
    check(f"память на векторы {mb:.1f} МБ — помещается в 1 ГБ", mb < 40,
          f"{mb:.1f} МБ")
    check("превышение мягкого предела не объявлено на границе",
          not st["over_limit"], str(st["count"]))


def test_semantic_tools() -> None:
    section("Смысловой поиск: инструменты")

    class FakeEmb(V.Embedder):
        """Вектор из слов: общие слова дают близкие векторы."""

        def __init__(self, works: bool = True) -> None:
            super().__init__(url="http://x", model="m")
            self.works = works

        def embed(self, texts):
            if not self.works:
                self.error = "служба недоступна"
                return None
            out = []
            for t in texts:
                v = [0.0] * 32
                for w in t.lower().split():
                    v[hash(w) % 32] += 1.0
                out.append(V.normalize(v))
            return out

    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "a.db")
        rid = st.start_run("t")
        st.remember("крепление узла выполнено болтами М8")
        st.remember("зазор между щекой и водилом 0.5 мм")
        st.remember("покрытие анодированное чёрное")

        t = {x.name: x for x in sem_tools.build(st, FakeEmb(), lambda: rid)}
        out = t["index_memory"].fn()
        check("факты проиндексированы", "3" in out, out)
        check("повторная индексация не дублирует",
              "уже проиндексированы" in t["index_memory"].fn())

        # ищем словами, которых нет в тексте дословно
        res = t["semantic_recall"].fn(query="болтами крепление")
        check("смысловой поиск нашёл нужное", "болтами" in res, res[:120])

        both = t["smart_recall"].fn(query="зазор")
        check("smart_recall помечает источник",
              "[по словам]" in both, both[:150])

        stt = t["vector_status"].fn()
        check("состояние показывает число векторов", "3" in stt, stt)

        # служба сломалась — честный отказ, а не пустота
        t2 = {x.name: x for x in sem_tools.build(st, FakeEmb(works=False),
                                                 lambda: rid)}
        try:
            t2["semantic_recall"].fn(query="зазор")
            check("сбой службы векторов назван", False, "промолчал")
        except ToolError as exc:
            check("сбой службы векторов назван",
                  "недоступна" in str(exc), str(exc)[:70])
        # а обычный поиск при этом работает
        check("smart_recall переживает сбой службы",
              "зазор" in t2["smart_recall"].fn(query="зазор"))

        # служба не настроена
        t3 = {x.name: x for x in sem_tools.build(st, V.Embedder(), lambda: rid)}
        try:
            t3["semantic_recall"].fn(query="х")
            check("ненастроенная служба названа", False, "промолчал")
        except ToolError as exc:
            check("ненастроенная служба названа", "не настроена" in str(exc))
        check("smart_recall без службы ищет словами",
              "не настроен" in t3["smart_recall"].fn(query="зазор"))
        st.close()


# ═══════════════════════ маршрутизация ══════════════════════════════
class Fake(BaseLLM):
    def __init__(self, model: str) -> None:
        super().__init__(model)
        self.seen = 0

    def _chat_once(self, messages, tools=None):
        self.seen += 1
        return LLMReply(text="ответ", usage=Usage(1000, 200))


def test_router() -> None:
    section("Маршрутизация: дешёвая на рутину, сильная на сложное")
    cheap, strong = Fake("gpt-4o-mini"), Fake("gpt-4o")
    r = Router(cheap, strong)

    def ask(msgs):
        llm, why = r.choose(msgs)
        return ("strong" if llm is strong else "cheap"), why

    kind, why = ask([{"role": "user", "content": "прочитай файл"}])
    check("рутина идёт на дешёвую", kind == "cheap", f"{kind}: {why}")

    kind, why = ask([{"role": "user", "content": "[сложно] спроектируй схему"}])
    check("пометка [сложно] уводит на сильную", kind == "strong", why)

    kind, why = ask([{"role": "user", "content": "[просто] " + "х" * 20000}])
    check("пометка [просто] сильнее длины", kind == "cheap", why)

    kind, why = ask([{"role": "user", "content": "х" * 20000}])
    check("длинный контекст уводит на сильную", kind == "strong", why)

    kind, why = ask([{"role": "user", "content": "почини"},
                     {"role": "tool", "content": "ОШИБКА: файл не найден"}])
    check("ошибка в шаге уводит на сильную", kind == "strong", why)

    kind, why = ask([{"role": "user", "content": "дальше"},
                     {"role": "tool", "content": "готово, 42"}])
    check("успешный шаг остаётся на дешёвой", kind == "cheap", why)

    # деньги: расход считается по обеим моделям
    r.chat([{"role": "user", "content": "рутина"}])
    r.chat([{"role": "user", "content": "[сложно] думай"}])
    check("вызовы разошлись по моделям",
          cheap.seen == 1 and strong.seen == 1,
          f"{cheap.seen}/{strong.seen}")
    check("usage суммируется", r.usage.total == 2400, str(r.usage.total))
    cost = r.cost()
    # 1000×0.15 + 200×0.60 на mini, 1000×2.50 + 200×10.0 на 4o
    expect = (1000 * 0.15 + 200 * 0.60) / 1e6 + (1000 * 2.50 + 200 * 10.0) / 1e6
    check("стоимость считается по каждой модели отдельно",
          cost is not None and abs(cost - expect) < 1e-9,
          f"{cost} вместо {expect}")

    rep = r.spend_report()
    check("отчёт показывает обе модели",
          "gpt-4o-mini" in rep and "gpt-4o:" in rep, rep)
    check("отчёт показывает экономию", "экономия" in rep, rep)
    check("отчёт объясняет выбор", "выбор:" in rep, rep)


def test_router_in_build() -> None:
    section("Маршрутизация подключается через конфиг")
    import os
    from agent.build import build_agent
    from agent.config import Config
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(provider="ollama", model="m", workspace=td,
                     skills=["files"])
        cfg.db = os.path.join(td, "a.db")
        a = build_agent(cfg)
        check("без настройки маршрутизации её нет",
              not isinstance(a.llm, Router), type(a.llm).__name__)

        cfg2 = Config(provider="ollama", model="m", workspace=td,
                      skills=["files"])
        cfg2.db = os.path.join(td, "b.db")
        cfg2.model_cheap = "qwen2.5:7b"
        cfg2.model_strong = "devstral:22b"
        a2 = build_agent(cfg2)
        check("маршрутизация включается двумя ключами",
              isinstance(a2.llm, Router), type(a2.llm).__name__)
        check("обе модели на месте",
              a2.llm.cheap.model == "qwen2.5:7b"
              and a2.llm.strong.model == "devstral:22b")


# ════════════════════ история прогонов ══════════════════════════════
def test_runs_history() -> None:
    section("История прогонов")
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "a.db")
        r1 = st.start_run("первая цель", "cad")
        ids = st.add_tasks(r1, ["раз", "два"])
        st.set_task(ids[0], "done", "сделано")
        st.bump_run(r1, steps=5, calls=9, tok_in=1000, tok_out=200, cost=0.01)
        st.log_event(r1, 1, "tool", "read_file", "прочитал")
        st.log_event(r1, 2, "error", "exception", "упало")
        st.finish_run(r1, "done")
        r2 = st.start_run("вторая цель", "verify")

        rows = st.runs(10)
        check("оба прогона в списке", len(rows) == 2, str(len(rows)))
        check("новые первыми", rows[0]["id"] == r2, str(rows[0]["id"]))
        check("расход сохранён", rows[1]["tok_in"] == 1000
              and abs(rows[1]["cost"] - 0.01) < 1e-9, str(dict(rows[1])))
        check("роль сохранена", rows[1]["profile"] == "cad")

        evs = st.run_events(r1)
        check("журнал по порядку", [e["kind"] for e in evs]
              == ["tool", "error"], str([e["kind"] for e in evs]))
        only = st.run_events(r1, kinds="error")
        check("фильтр по виду события", len(only) == 1
              and only[0]["name"] == "exception", str(only))
        check("чужой прогон не подмешивается", st.run_events(r2) == [])
        st.close()


def main() -> int:
    print("=" * 60)
    print("ТЕСТЫ: тесты, патчи, векторы, маршрутизация, история")
    print("=" * 60)
    test_run_tests()
    test_apply_patch()
    test_vectors()
    test_vectors_memory()
    test_semantic_tools()
    test_router()
    test_router_in_build()
    test_runs_history()
    print("\n" + "=" * 60)
    print(f"пройдено: {PASS} · провалено: {FAIL}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
