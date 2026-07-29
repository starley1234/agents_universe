"""Тесты хранилища: реальный SQLite во временном файле, без моков.

Хранилище проверяется отдельно и придирчиво, потому что на нём держатся
два обещания платформы: пауза на человеке переживает перезапуск
процесса, а история доски позволяет разобрать инцидент задним числом.
Особое внимание — версионности контекста и тому, что закрытую точку
контроля нельзя закрыть повторно.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import check, check_raises, section, summary          # noqa: E402
from awos.kernel.store import Store, StoreError                    # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="awos_store_"))
    store = Store(tmp / "state.db")

    section("Прогоны")
    run_id = store.create_run("wf", "цель прогона")
    check("прогон создан", run_id > 0)
    run = store.get_run(run_id)
    check("статус по умолчанию running", run["status"] == "running")
    check("цель сохранена", run["goal"] == "цель прогона")
    check("несуществующий прогон -> None", store.get_run(99999) is None)
    check_raises("require_run падает понятно", StoreError, store.require_run, 99999)

    store.bump_run(run_id, steps=1, tools=2, tokens_in=100, tokens_out=50,
                   llm_calls=3)
    store.bump_run(run_id, steps=1, tokens_in=10)
    run = store.get_run(run_id)
    check("счётчики накапливаются", run["steps_done"] == 2 and
          run["tokens_in"] == 110 and run["llm_calls"] == 3,
          f"{run['steps_done']}/{run['tokens_in']}/{run['llm_calls']}")

    store.set_run_status(run_id, "done")
    run = store.get_run(run_id)
    check("финальный статус проставлен", run["status"] == "done")
    check("время завершения записано", run["finished"] is not None)

    other = store.create_run("wf2", "вторая")
    check("список прогонов по статусу",
          [r["id"] for r in store.list_runs(status="running")] == [other])

    section("Шаги")
    r2 = store.create_run("wf", "шаги")
    s1 = store.add_step(r2, 0, "first", "researcher")
    s2 = store.add_step(r2, 1, "second", "writer")
    check("шаги созданы", s1 > 0 and s2 > s1)
    check("порядок сохранён",
          [s["name"] for s in store.steps(r2)] == ["first", "second"])
    nxt = store.next_pending_step(r2)
    check("следующий шаг — первый по порядку", nxt["name"] == "first")

    store.update_step(s1, status="done", output="итог", score=0.9, revisions=1)
    row = store.get_step(s1)
    check("статус шага обновлён", row["status"] == "done")
    check("результат сохранён", row["output"] == "итог")
    check("оценка сохранена", row["score"] == 0.9)
    check("время завершения шага записано", row["finished"] is not None)
    check("следующий pending — второй",
          store.next_pending_step(r2)["name"] == "second")

    store.update_step(s2, status="waiting_human")
    check("waiting_human остаётся в очереди на исполнение",
          store.next_pending_step(r2)["name"] == "second",
          "после ответа человека прогон обязан продолжить ЭТОТ шаг")

    section("Доска контекста: версионность")
    r3 = store.create_run("wf", "контекст")
    v1 = store.ctx_put(r3, "notes", "первая версия", author="step:a")
    v2 = store.ctx_put(r3, "notes", "вторая версия", author="human")
    check("версии нумеруются подряд", (v1, v2) == (1, 2))
    check("чтение отдаёт последнюю версию",
          store.ctx_get(r3, "notes") == "вторая версия")
    history = store.ctx_history(r3, "notes")
    check("история хранит обе версии", len(history) == 2)
    check("старая версия НЕ затёрта", history[0]["value"] == "первая версия")
    check("автор записи сохранён", history[1]["author"] == "human")
    check("отсутствующий ключ -> default",
          store.ctx_get(r3, "нет", "по умолчанию") == "по умолчанию")
    check_raises("пустой ключ отвергается", StoreError, store.ctx_put, r3, "", "x")

    store.ctx_put(r3, "structured", {"a": [1, 2], "b": "текст"})
    value = store.ctx_get(r3, "structured")
    check("словарь сохраняется и читается как словарь",
          value == {"a": [1, 2], "b": "текст"}, str(value))
    store.ctx_put(r3, "unicode", "русский текст без \\u-эскейпов")
    check("юникод сохранён",
          store.ctx_get(r3, "unicode") == "русский текст без \\u-эскейпов")

    snapshot = store.ctx_all(r3)
    check("срез доски — только последние версии",
          snapshot["notes"] == "вторая версия" and len(snapshot) == 3,
          str(snapshot))
    check("ctx_keys перечисляет ключи",
          store.ctx_keys(r3) == ["notes", "structured", "unicode"])

    section("Точки контроля")
    r4 = store.create_run("wf", "hitl")
    s = store.add_step(r4, 0, "step", "")
    cp = store.create_checkpoint(r4, s, "approval", "Утвердить?",
                                 {"output": "текст"})
    check("точка контроля создана", cp > 0)
    row = store.get_checkpoint(cp)
    check("статус pending", row["status"] == "pending")
    check("payload разобран как объект", row["payload"]["output"] == "текст")
    check("pending_checkpoint находит открытую",
          store.pending_checkpoint(r4)["id"] == cp)

    store.resolve_checkpoint(cp, "edited", "правка человека", actor="tester")
    row = store.get_checkpoint(cp)
    check("решение записано", row["status"] == "edited")
    check("текст правки сохранён", row["response"] == "правка человека")
    check("автор решения сохранён", row["actor"] == "tester")
    check("время решения записано", row["resolved"] is not None)
    check("открытых точек не осталось", store.pending_checkpoint(r4) is None)
    check_raises("повторное закрытие отвергается", StoreError,
                 store.resolve_checkpoint, cp, "approved")
    check_raises("неизвестное решение отвергается", StoreError,
                 store.resolve_checkpoint, cp, "может_быть")

    section("Журнал и вызовы инструментов")
    r5 = store.create_run("wf", "журнал")
    e1 = store.log(r5, "run_start", "поехали")
    store.log(r5, "worker", "ход исполнителя", role="worker", data={"n": 1})
    events = store.events(r5)
    check("события записаны по порядку", len(events) == 2)
    check("роль сохранена", events[1]["role"] == "worker")
    check("данные разобраны", events[1]["data"] == {"n": 1})
    check("выборка after_id работает",
          len(store.events(r5, after_id=e1)) == 1)

    store.log_tool_call(r5, None, "read_file", {"path": "a.md"}, True, "ok", 0.01)
    store.log_tool_call(r5, None, "shell", {"command": "ls"}, False, "запрещено", 0.02)
    calls = store.tool_calls(r5)
    check("вызовы инструментов записаны", len(calls) == 2)
    check("аргументы разобраны", calls[1]["args"] == {"path": "a.md"})
    check("признак неудачи сохранён", calls[0]["ok"] == 0)

    section("Сводка и удаление")
    stats = store.stats()
    check("сводка считает прогоны", stats["runs"] >= 5)
    check("сводка считает вызовы инструментов", stats["tool_calls"] == 2)
    check("удаление прогона", store.delete_run(r5) is True)
    check("каскадом удалились события", store.events(r5) == [],
          "внешние ключи должны быть включены (PRAGMA foreign_keys)")
    check("повторное удаление -> False", store.delete_run(r5) is False)

    section("Переоткрытие базы: состояние переживает перезапуск")
    store.close()
    reopened = Store(tmp / "state.db")
    check("прогон на месте после переоткрытия",
          reopened.get_run(r3)["goal"] == "контекст")
    check("доска на месте после переоткрытия",
          reopened.ctx_get(r3, "notes") == "вторая версия")
    check("история доски на месте",
          len(reopened.ctx_history(r3, "notes")) == 2)
    reopened.close()

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    return summary("Хранилище")


if __name__ == "__main__":
    raise SystemExit(main())
