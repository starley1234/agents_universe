"""Тесты определения workflow: валидация ДО запуска — главная ценность.

Смысл этих проверок: опечатка в плейсхолдере или ссылка на ключ доски,
который никто не пишет, обязаны обнаружиться немедленно и с понятным
текстом — а не через десять минут прогона и двести тысяч потраченных
токенов. Поэтому здесь так много негативных сценариев.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import check, check_raises, section, summary          # noqa: E402
from awos.kernel.workflow import (WorkflowError, describe_workflows,  # noqa: E402
                                  list_workflows, load_workflow,
                                  parse_workflow)


def wf(**over):
    base = {"name": "t", "steps": [{"name": "a", "task": "делай {goal}"}]}
    base.update(over)
    return base


def main() -> int:
    section("Разбор корректного определения")
    parsed = parse_workflow({
        "name": "demo", "title": "Демо", "description": "описание",
        "inputs": {"topic": "тема"},
        "steps": [
            {"name": "research", "profile": "researcher",
             "task": "изучи {input.topic} ради {goal}", "writes": "notes",
             "review": {"critic": "critic", "min_score": 0.8,
                        "max_revisions": 3}},
            {"name": "write", "task": "напиши по {ctx.notes} и {step.research}",
             "reads": ["notes"], "writes": "doc", "human": "always"},
        ],
    })
    check("имя разобрано", parsed.name == "demo")
    check("заголовок разобран", parsed.title == "Демо")
    check("входы разобраны", parsed.inputs == {"topic": "тема"})
    check("два шага", len(parsed.steps) == 2)
    check("профиль шага", parsed.steps[0].profile == "researcher")
    check("критик шага", parsed.steps[0].review.critic == "critic")
    check("порог шага", parsed.steps[0].review.min_score == 0.8)
    check("лимит доработок шага", parsed.steps[0].review.max_revisions == 3)
    check("политика человека", parsed.steps[1].human == "always")
    check("reads разобран", parsed.steps[1].reads == ["notes"])
    check("outputs перечисляет ключи", parsed.outputs() == ["notes", "doc"])
    check("поиск шага по имени", parsed.step("write").name == "write")
    check("несуществующий шаг -> None", parsed.step("нет") is None)

    section("Формы поля review")
    check("review=false выключает критика",
          parse_workflow(wf(steps=[{"name": "a", "task": "t", "review": False}])
                         ).steps[0].review.critic == "")
    check("review=true даёт критика по умолчанию",
          parse_workflow(wf(steps=[{"name": "a", "task": "t", "review": True}])
                         ).steps[0].review.critic == "critic")
    check("review='имя' — это критик",
          parse_workflow(wf(steps=[{"name": "a", "task": "t",
                                    "review": "fact_checker"}])
                         ).steps[0].review.critic == "fact_checker")
    check("без review критик не назначен",
          parse_workflow(wf()).steps[0].review.critic == "")

    section("Валидация структуры")
    check_raises("не объект", WorkflowError, parse_workflow, ["список"])
    check_raises("нет имени", WorkflowError, parse_workflow, {"steps": []})
    check_raises("плохое имя", WorkflowError, parse_workflow,
                 wf(name="имя с пробелами"))
    check_raises("нет шагов", WorkflowError, parse_workflow, wf(steps=[]))
    check_raises("шаги не список", WorkflowError, parse_workflow,
                 wf(steps={"a": 1}))
    check_raises("шаг без имени", WorkflowError, parse_workflow,
                 wf(steps=[{"task": "t"}]))
    check_raises("шаг без задачи", WorkflowError, parse_workflow,
                 wf(steps=[{"name": "a"}]))
    check_raises("пустая задача", WorkflowError, parse_workflow,
                 wf(steps=[{"name": "a", "task": "   "}]))
    check_raises("дублирующееся имя шага", WorkflowError, parse_workflow,
                 wf(steps=[{"name": "a", "task": "t"},
                           {"name": "a", "task": "t2"}]))
    check_raises("недопустимый ключ writes", WorkflowError, parse_workflow,
                 wf(steps=[{"name": "a", "task": "t", "writes": "плохой ключ!"}]))
    check_raises("неизвестная политика human", WorkflowError, parse_workflow,
                 wf(steps=[{"name": "a", "task": "t", "human": "может быть"}]))
    check_raises("review.min_score вне диапазона", WorkflowError, parse_workflow,
                 wf(steps=[{"name": "a", "task": "t",
                            "review": {"min_score": 5}}]))
    check_raises("review.max_revisions отрицательный", WorkflowError,
                 parse_workflow, wf(steps=[{"name": "a", "task": "t",
                                            "review": {"max_revisions": -1}}]))

    section("Валидация плейсхолдеров — сердце проверки")
    check_raises("неизвестный плейсхолдер", WorkflowError, parse_workflow,
                 wf(steps=[{"name": "a", "task": "делай {непонятно}"}]))
    check_raises("ссылка на необъявленный вход", WorkflowError, parse_workflow,
                 wf(steps=[{"name": "a", "task": "тема {input.topic}"}]))
    check_raises("ctx-ключ, который никто не пишет", WorkflowError,
                 parse_workflow, wf(steps=[{"name": "a", "task": "{ctx.notes}"}]))
    check_raises("ctx-ключ ДО того, как его записали", WorkflowError,
                 parse_workflow, wf(steps=[
                     {"name": "a", "task": "нужен {ctx.later}"},
                     {"name": "b", "task": "пишу", "writes": "later"}]))
    check_raises("ссылка на невыполненный шаг", WorkflowError, parse_workflow,
                 wf(steps=[{"name": "a", "task": "{step.b}"},
                           {"name": "b", "task": "t"}]))
    check_raises("reads на несуществующий ключ", WorkflowError, parse_workflow,
                 wf(steps=[{"name": "a", "task": "t", "reads": ["нет"]}]))

    ok = parse_workflow(wf(inputs={"topic": "т"}, steps=[
        {"name": "a", "task": "{input.topic} и {goal}", "writes": "k"},
        {"name": "b", "task": "{ctx.k} и {step.a}", "reads": ["k"]}]))
    check("корректные ссылки вперёд-назад проходят", len(ok.steps) == 2)
    check("опечатка в задаче ловится ДО запуска (главный сценарий)",
          _raises(parse_workflow, wf(inputs={"topic": "т"}, steps=[
              {"name": "a", "task": "тема: {input.topik}"}])),
          "опечатка topik вместо topic")

    section("Загрузка из файла")
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "good.json").write_text(json.dumps(wf(name="good")),
                                     encoding="utf-8")
        loaded = load_workflow("good", d)
        check("файл загружен по имени", loaded.name == "good")
        check("загрузка по прямому пути",
              load_workflow(str(d / "good.json")).name == "good")
        check("перечисление файлов", list_workflows(d) == ["good"])
        check_raises("нет такого workflow — понятная ошибка", WorkflowError,
                     load_workflow, "нету", d)

        (d / "broken.json").write_text("{не json", encoding="utf-8")
        check_raises("битый JSON", WorkflowError, load_workflow, "broken", d)

        (d / "invalid.json").write_text(
            json.dumps({"name": "invalid", "steps": [{"name": "a",
                                                      "task": "{ctx.нет}"}]}),
            encoding="utf-8")
        check_raises("невалидное содержимое", WorkflowError, load_workflow,
                     "invalid", d)

        items = describe_workflows(d)
        check("describe перечисляет всё, включая битое", len(items) == 3)
        check("битые помечены полем error",
              sum(1 for i in items if "error" in i) == 2)
        check("исправное описано без error",
              any(i.get("name") == "good" and "error" not in i for i in items),
              "одно битое определение не должно скрывать остальные")

    section("Встроенные определения поставки")
    builtin = describe_workflows()
    check("встроенные workflow есть", len(builtin) >= 3)
    broken = [b for b in builtin if "error" in b]
    check("все встроенные workflow валидны", not broken,
          "; ".join(f"{b['name']}: {b['error']}" for b in broken))

    return summary("Определения workflow")


def _raises(fn, *args) -> bool:
    try:
        fn(*args)
    except WorkflowError:
        return True
    except Exception:                                             # noqa: BLE001
        return False
    return False


if __name__ == "__main__":
    raise SystemExit(main())
