"""Тесты навыка cert_verify: валидация/верификация комплекта документов
для сертификации авиационной техники (ФАП-21/Росавиация).

ГЛАВНОЕ, что здесь проверяется — тот же принцип, что и у cad_openscad
("не оценивать геометрию на глаз"), только применительно к доказательной
документации: МОДЕЛЬ НЕ МОЖЕТ ПРОСТАВИТЬ «СООТВЕТСТВУЕТ» ПО ВЫДУМАННОЙ
ЦИТАТЕ. Позитивные проверки — реальная цитата принимается; негативные —
выдуманная цитата принудительно понижает verdict, короткая/вырожденная
цитата отклоняется, compliant/non_compliant без доказательства не
регистрируется вовсе.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.store import Store                             # noqa: E402
from agent.tools.base import ToolError, Workspace          # noqa: E402
from agent.skills import cert_verify                        # noqa: E402

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


def _setup(tmpdir: Path):
    ws = Workspace(tmpdir / "ws")
    store = Store(str(tmpdir / "t.db"))
    tools = {t.name: t for t in cert_verify.build(ws, store, lambda: 1)}
    return ws, store, tools


REPORT_TEXT = (
    "Отчёт об испытаниях на прочность.\n\n"
    "Требование по прочности крыла выполнено полностью, запас прочности "
    "составил 1.8, что превышает нормативный минимум 1.5 по расчётной "
    "нагрузке.\n\n"
    "Испытание на усталостную прочность не проводилось в рамках данного "
    "отчёта."
)


# ================================================================ chunking
def test_checklist_parsing() -> None:
    section("checklist_from_text: разбор пунктов чек-листа")
    with tempfile.TemporaryDirectory() as td:
        ws, store, tools = _setup(Path(td))
        text = (
            "ФАП-21 п.21.16А — применение требований к лётной годности\n"
            "ФАП-21 п.25.301 - прочность конструкции\n"
            "просто пункт без описания\n"
        )
        out = tools["checklist_from_text"].fn(text=text)
        check("выделены все 3 пункта", "3 пункт" in out, out)
        check("описание после тире сохранено",
              "применение требований к лётной годности" in out, out)
        check("пункт без описания тоже выделен",
              "просто пункт без описания" in out, out)

        try:
            tools["checklist_from_text"].fn(text="   \n\n\t")
            check("отказ на пустой чек-лист", False)
        except ToolError:
            check("отказ на пустой чек-лист", True)

    check("parse_checklist: пустая строка не создаёт пункт",
          cert_verify.parse_checklist("\n\n") == [])
    items = cert_verify.parse_checklist("A — B\nC: D\nE - F\nG")
    check("parse_checklist: разные разделители распознаны",
          [i["requirement"] for i in items] == ["A", "C", "E", "G"], str(items))
    check("parse_checklist: описание верно извлечено",
          items[0]["requirement_text"] == "B", str(items))
    check("parse_checklist: пункт без описания -> пустая requirement_text",
          items[3]["requirement_text"] == "", str(items))


# ============================================================ grounding
def test_compliant_with_real_quote() -> None:
    section("cert_check: реальная цитата -> compliant остаётся compliant")
    with tempfile.TemporaryDirectory() as td:
        ws, store, tools = _setup(Path(td))
        (ws.root / "otchet.txt").write_text(REPORT_TEXT, encoding="utf-8")

        out = tools["cert_check"].fn(
            requirement="ФАП-21 п.25.301", verdict="compliant",
            requirement_text="прочность конструкции",
            evidence_source="otchet.txt",
            evidence_quote="Требование по прочности крыла выполнено полностью, "
                          "запас прочности составил 1.8")
        check("verdict остался compliant", "compliant" in out and
              "ПОНИЖЕНО" not in out, out)
        check("цитата подтверждена", "цитата подтверждена: да" in out, out)

        checks = store.cert_checks(1)
        check("запись реально в базе", len(checks) == 1)
        check("quote_verified=1 в базе", checks[0]["quote_verified"] == 1)


def test_fabricated_quote_is_downgraded() -> None:
    section("cert_check: ВЫДУМАННАЯ цитата -> принудительно needs_review")
    with tempfile.TemporaryDirectory() as td:
        ws, store, tools = _setup(Path(td))
        (ws.root / "otchet.txt").write_text(REPORT_TEXT, encoding="utf-8")

        out = tools["cert_check"].fn(
            requirement="ФАП-21 п.21.16А", verdict="compliant",
            evidence_source="otchet.txt",
            evidence_quote="Совершенно другой текст, которого нет в документе вообще")
        check("verdict понижен до needs_review", "needs_review" in out, out)
        check("явно указано понижение", "ПОНИЖЕНО" in out, out)

        checks = store.cert_checks(1)
        check("в базе сохранён ПОНИЖЕННЫЙ verdict, а не заявленный моделью",
              checks[0]["verdict"] == "needs_review", str(checks[0]))
        check("quote_verified=0", checks[0]["quote_verified"] == 0)


def test_non_compliant_with_real_quote() -> None:
    section("cert_check: non_compliant с реальной цитатой о несоответствии")
    with tempfile.TemporaryDirectory() as td:
        ws, store, tools = _setup(Path(td))
        (ws.root / "otchet.txt").write_text(REPORT_TEXT, encoding="utf-8")

        out = tools["cert_check"].fn(
            requirement="ФАП-21 п.25.571", verdict="non_compliant",
            evidence_source="otchet.txt",
            evidence_quote="Испытание на усталостную прочность не проводилось "
                          "в рамках данного отчёта")
        check("non_compliant принят с подтверждённой цитатой",
              "non_compliant" in out and "ПОНИЖЕНО" not in out, out)


def test_quote_from_rag_chunk() -> None:
    section("cert_check: доказательство берётся из чанка, проиндексированного rag")
    with tempfile.TemporaryDirectory() as td:
        ws, store, tools = _setup(Path(td))
        store.add_chunks("протокол.pdf", [
            "Испытание прошло успешно, все параметры в норме и "
            "подтверждают заявленные характеристики изделия."])

        out = tools["cert_check"].fn(
            requirement="ФАП-21 п.21.20С", verdict="compliant",
            evidence_source="протокол.pdf",
            evidence_quote="Испытание прошло успешно, все параметры в норме")
        check("цитата найдена в чанке, а не только в файле workspace",
              "цитата подтверждена: да" in out, out)


def test_quote_matching_ignores_whitespace_and_case() -> None:
    section("Сопоставление цитаты нормализует пробелы/переносы/регистр")
    with tempfile.TemporaryDirectory() as td:
        ws, store, tools = _setup(Path(td))
        (ws.root / "doc.txt").write_text(
            "Требование  выполнено\nполностью\tи   без замечаний экспертизы.",
            encoding="utf-8")
        out = tools["cert_check"].fn(
            requirement="X", verdict="compliant", evidence_source="doc.txt",
            evidence_quote="требование выполнено полностью и без замечаний")
        check("цитата с другим регистром/пробелами найдена",
              "цитата подтверждена: да" in out, out)


# =============================================================== negative
def test_negative_cases() -> None:
    section("Негативные проверки")
    with tempfile.TemporaryDirectory() as td:
        ws, store, tools = _setup(Path(td))
        (ws.root / "doc.txt").write_text(REPORT_TEXT, encoding="utf-8")

        try:
            tools["cert_check"].fn(requirement="x", verdict="выдуманный_вердикт")
            check("отказ на неизвестный verdict", False)
        except ToolError:
            check("отказ на неизвестный verdict", True)

        try:
            tools["cert_check"].fn(requirement="", verdict="not_found")
            check("отказ на пустой requirement", False)
        except ToolError:
            check("отказ на пустой requirement", True)

        try:
            tools["cert_check"].fn(requirement="x", verdict="compliant")
            check("compliant без доказательства не регистрируется", False)
        except ToolError:
            check("compliant без доказательства не регистрируется", True)

        try:
            tools["cert_check"].fn(requirement="x", verdict="non_compliant",
                                   evidence_source="doc.txt")
            check("non_compliant без цитаты не регистрируется", False)
        except ToolError:
            check("non_compliant без цитаты не регистрируется", True)

        try:
            tools["cert_check"].fn(requirement="x", verdict="compliant",
                                   evidence_source="doc.txt",
                                   evidence_quote="коротко")
            check("отказ на вырожденно короткую цитату", False)
        except ToolError:
            check("отказ на вырожденно короткую цитату", True)

        # not_found и needs_review НЕ требуют доказательства
        out = tools["cert_check"].fn(requirement="ФАП-21 п.99.99",
                                     verdict="not_found",
                                     comment="документ отсутствует в комплекте")
        check("not_found без доказательства разрешён", "not_found" in out, out)

        out2 = tools["cert_check"].fn(requirement="ФАП-21 п.88.88",
                                      verdict="needs_review")
        check("needs_review без доказательства разрешён", "needs_review" in out2, out2)


# =========================================================== report/reset
def test_cert_report_readiness() -> None:
    section("cert_report: сводка и вывод о готовности к подаче")
    with tempfile.TemporaryDirectory() as td:
        ws, store, tools = _setup(Path(td))

        empty = tools["cert_report"].fn()
        check("пустой отчёт объясняет, с чего начать", "Проверок ещё нет" in empty,
              empty)

        (ws.root / "doc.txt").write_text(REPORT_TEXT, encoding="utf-8")
        tools["cert_check"].fn(
            requirement="п.1", verdict="compliant", evidence_source="doc.txt",
            evidence_quote="Требование по прочности крыла выполнено полностью")

        report_ready = tools["cert_report"].fn()
        check("при всех compliant — готово к подаче",
              "все пункты подтверждены" in report_ready, report_ready)

        tools["cert_check"].fn(requirement="п.2", verdict="not_found")
        report_not_ready = tools["cert_report"].fn()
        check("не готово, если есть not_found",
              "НЕ готово" in report_not_ready, report_not_ready)
        check("сводка считает по вердиктам верно",
              "не соответствует: 0" in report_not_ready, report_not_ready)


def test_cert_reset() -> None:
    section("cert_reset: начать проверку заново")
    with tempfile.TemporaryDirectory() as td:
        ws, store, tools = _setup(Path(td))
        tools["cert_check"].fn(requirement="п.1", verdict="not_found")
        tools["cert_check"].fn(requirement="п.2", verdict="not_found")
        out = tools["cert_reset"].fn()
        check("сброшено 2 записи", "2" in out, out)
        check("после сброса cert_report снова пуст",
              "Проверок ещё нет" in tools["cert_report"].fn())


def test_store_verdict_validation() -> None:
    section("Store.add_cert_check: отказ на неизвестный verdict на уровне БД")
    with tempfile.TemporaryDirectory() as td:
        store = Store(str(Path(td) / "t.db"))
        try:
            store.add_cert_check("x", "полностью_выдуманный")
            check("отказ на невалидный verdict в Store", False)
        except ValueError:
            check("отказ на невалидный verdict в Store", True)


# ============================================================= build/agent
def test_build_agent_with_cert_verify_skill() -> None:
    section("Сборка агента с навыком cert_verify")
    from agent.build import build_agent
    from agent.config import Config
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(provider="ollama", model="m", workspace=td,
                    skills=["files", "cert_verify"])
        agent = build_agent(cfg)
        names = agent.tools.names()
        for t in ("checklist_from_text", "cert_check", "cert_report", "cert_reset"):
            check(f"инструмент {t} зарегистрирован", t in names)


def test_cert_auditor_profile() -> None:
    section("Профиль cert_auditor собирается со всеми нужными навыками")
    from agent.build import build_agent
    from agent.config import Config
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(provider="ollama", model="m", workspace=td)
        cfg.apply_profile("cert_auditor")
        agent = build_agent(cfg)
        names = agent.tools.names()
        for t in ("cert_check", "cert_report", "pdf_classify", "doc_classify",
                  "rag_query", "present"):
            check(f"инструмент {t} доступен в cert_auditor", t in names)


def test_example_config_loads() -> None:
    section("examples/config.cert_auditor.json грузится без ошибок")
    from agent.config import Config
    root = Path(__file__).resolve().parents[1]
    cfg = Config.load(str(root / "examples" / "config.cert_auditor.json"))
    check("профиль cert_auditor выбран", cfg.profile == "cert_auditor")
    check("навык cert_verify подключён", "cert_verify" in cfg.skills)
    check("комментарные ключи не попали в поля",
          not hasattr(cfg, "_комментарий_профиль"))


def test_router_picks_cert_auditor() -> None:
    section("Роутер выбирает cert_auditor по характерным формулировкам")
    from agent.config import Config
    from agent.router import pick_profile
    infos = Config.profile_infos()
    d = pick_profile("проверь комплект документов на соответствие ФАП-21", infos)
    check("cert_auditor выбран для явно сертификационной задачи",
          d.profile == "cert_auditor", d.profile)


def main() -> int:
    test_checklist_parsing()
    test_compliant_with_real_quote()
    test_fabricated_quote_is_downgraded()
    test_non_compliant_with_real_quote()
    test_quote_from_rag_chunk()
    test_quote_matching_ignores_whitespace_and_case()
    test_negative_cases()
    test_cert_report_readiness()
    test_cert_reset()
    test_store_verdict_validation()
    test_build_agent_with_cert_verify_skill()
    test_cert_auditor_profile()
    test_example_config_loads()
    test_router_picks_cert_auditor()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
