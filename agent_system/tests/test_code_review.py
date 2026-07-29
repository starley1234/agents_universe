"""Тесты навыка code_review: аудит кода с проверяемыми находками.

ГЛАВНОЕ, что здесь проверяется — тот же принцип, что и в
test_cert_verify.py, только применительно к коду: МОДЕЛЬ НЕ МОЖЕТ
ЗАЯВИТЬ CRITICAL/MAJOR НАХОДКУ ПО ВЫДУМАННОЙ ЦИТАТЕ. Позитивные проверки
— реальная цитата принимается на заявленных строках; негативные —
выдуманная цитата принудительно понижает severity до info, короткая
цитата отклоняется, цитата не в заявленном диапазоне (но реальная в
файле) помечается как неточная локация без понижения серьёзности.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.store import Store                              # noqa: E402
from agent.tools.base import ToolError, Workspace           # noqa: E402
from agent.skills import code_review                        # noqa: E402
from agent.config import Config                              # noqa: E402

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
    tools = {t.name: t for t in code_review.build(ws, store, lambda: 1)}
    return ws, store, tools


SAMPLE_CODE = (
    "def divide(a, b):\n"                    # 1
    "    return a / b\n"                      # 2
    "\n"                                      # 3
    "\n"                                      # 4
    "def build_query(user_id):\n"             # 5
    "    return f\"SELECT * FROM users WHERE id={user_id}\"\n"  # 6
    "\n"                                      # 7
    "\n"                                      # 8
    "def unused_helper():\n"                  # 9
    "    pass\n"                              # 10
)


# ================================================================= positive
def test_critical_with_real_quote_on_correct_lines() -> None:
    section("review_finding: реальная цитата на верных строках -> severity не понижен")
    with tempfile.TemporaryDirectory() as td:
        ws, store, tools = _setup(Path(td))
        (ws.root / "app.py").write_text(SAMPLE_CODE, encoding="utf-8")

        out = tools["review_finding"].fn(
            file="app.py", line_start=5, line_end=6, severity="critical",
            category="security", title="SQL-инъекция через f-string",
            description="user_id подставляется в SQL без параметризации",
            suggestion="использовать параметризованный запрос",
            quote='return f"SELECT * FROM users WHERE id={user_id}"')
        check("severity осталась critical", "critical" in out and
              "ПОНИЖЕНО" not in out, out)
        check("находка создана", "Находка #" in out, out)

        findings = store.review_findings(1)
        check("запись реально в базе", len(findings) == 1)
        check("quote_verified=1 в базе", findings[0]["quote_verified"] == 1)
        check("precise_location=1 (совпали заявленные строки)",
              findings[0]["precise_location"] == 1)


def test_fabricated_quote_is_downgraded() -> None:
    section("review_finding: ВЫДУМАННАЯ цитата -> принудительно info")
    with tempfile.TemporaryDirectory() as td:
        ws, store, tools = _setup(Path(td))
        (ws.root / "app.py").write_text(SAMPLE_CODE, encoding="utf-8")

        out = tools["review_finding"].fn(
            file="app.py", line_start=5, line_end=6, severity="major",
            category="bug", title="Выдуманная проблема",
            quote="этого фрагмента кода в файле нет вообще никогда")
        check("severity понижена до info", "info" in out, out)
        check("явно указано понижение", "ПОНИЖЕНО" in out, out)

        findings = store.review_findings(1)
        check("в базе сохранена ПОНИЖЕННАЯ severity, а не заявленная моделью",
              findings[0]["severity"] == "info", str(findings[0]))
        check("original_severity сохраняет то, что заявила модель",
              findings[0]["original_severity"] == "major", str(findings[0]))
        check("quote_verified=0", findings[0]["quote_verified"] == 0)


def test_quote_found_but_wrong_line_range() -> None:
    section("review_finding: цитата реальна, но строки указаны неверно — не понижаем, а помечаем")
    with tempfile.TemporaryDirectory() as td:
        ws, store, tools = _setup(Path(td))
        (ws.root / "app.py").write_text(SAMPLE_CODE, encoding="utf-8")

        # цитата реально на строке 6, но модель указала диапазон 1-1 —
        # достаточно далеко, чтобы окно поиска (со запасом _LINE_MARGIN=3
        # строки) его не захватило и промах не спутался с "почти верно"
        out = tools["review_finding"].fn(
            file="app.py", line_start=1, line_end=1, severity="critical",
            category="security", title="SQL-инъекция",
            quote='return f"SELECT * FROM users WHERE id={user_id}"')
        check("severity НЕ понижена (цитата реальна, просто не на тех строках)",
              "critical" in out and "ПОНИЖЕНО" not in out, out)
        check("предупреждение о неточной локации показано", "строк" in out, out)

        findings = store.review_findings(1)
        check("quote_verified=1 (код реален)", findings[0]["quote_verified"] == 1)
        check("precise_location=0 (не в заявленном диапазоне)",
              findings[0]["precise_location"] == 0, str(findings[0]))


def test_minor_and_info_do_not_require_quote() -> None:
    section("review_finding: minor/info можно регистрировать без цитаты")
    with tempfile.TemporaryDirectory() as td:
        ws, store, tools = _setup(Path(td))
        (ws.root / "app.py").write_text(SAMPLE_CODE, encoding="utf-8")

        out = tools["review_finding"].fn(
            file="app.py", line_start=9, line_end=10, severity="minor",
            category="maintainability", title="Неиспользуемая функция",
            description="unused_helper нигде не вызывается")
        check("minor без цитаты принят", "minor" in out, out)
        findings = store.review_findings(1)
        check("quote_verified=0 (цитаты не было)",
              findings[0]["quote_verified"] == 0)


def test_line_margin_tolerance() -> None:
    section("review_finding: небольшая погрешность в номере строки (в пределах запаса) не считается ошибкой")
    with tempfile.TemporaryDirectory() as td:
        ws, store, tools = _setup(Path(td))
        (ws.root / "app.py").write_text(SAMPLE_CODE, encoding="utf-8")

        # реальная строка 6, модель указала 7-8 (промах на 1-2 строки)
        tools["review_finding"].fn(
            file="app.py", line_start=7, line_end=8, severity="major",
            category="security", title="SQL-инъекция",
            quote='return f"SELECT * FROM users WHERE id={user_id}"')
        findings = store.review_findings(1)
        check("небольшой промах засчитан как точная локация (запас строк)",
              findings[0]["precise_location"] == 1, str(findings[0]))
        check("severity не понижена", findings[0]["severity"] == "major")


# =============================================================== negative
def test_negative_cases() -> None:
    section("Негативные проверки")
    with tempfile.TemporaryDirectory() as td:
        ws, store, tools = _setup(Path(td))
        (ws.root / "app.py").write_text(SAMPLE_CODE, encoding="utf-8")

        try:
            tools["review_finding"].fn(
                file="app.py", line_start=1, line_end=1, severity="wrong",
                category="bug", title="x")
            check("отказ на неизвестный severity", False)
        except ToolError:
            check("отказ на неизвестный severity", True)

        try:
            tools["review_finding"].fn(
                file="app.py", line_start=1, line_end=1, severity="major",
                category="wrong", title="x", quote="достаточно длинная цитата")
            check("отказ на неизвестный category", False)
        except ToolError:
            check("отказ на неизвестный category", True)

        try:
            tools["review_finding"].fn(
                file="app.py", line_start=1, line_end=1, severity="minor",
                category="bug", title="")
            check("отказ на пустой title", False)
        except ToolError:
            check("отказ на пустой title", True)

        try:
            tools["review_finding"].fn(
                file="app.py", line_start=5, line_end=2, severity="minor",
                category="bug", title="x")
            check("отказ на line_end < line_start", False)
        except ToolError:
            check("отказ на line_end < line_start", True)

        try:
            tools["review_finding"].fn(
                file="app.py", line_start=1, line_end=1, severity="critical",
                category="bug", title="без цитаты")
            check("critical без цитаты отклонён", False)
        except ToolError:
            check("critical без цитаты отклонён", True)

        try:
            tools["review_finding"].fn(
                file="app.py", line_start=1, line_end=1, severity="major",
                category="bug", title="короткая", quote="a")
            check("отказ на слишком короткую цитату", False)
        except ToolError:
            check("отказ на слишком короткую цитату", True)

        # позитивный контроль: minor/info без доказательства РАЗРЕШЕНЫ
        try:
            tools["review_finding"].fn(
                file="app.py", line_start=1, line_end=1, severity="info",
                category="style", title="стилистическое замечание")
            check("info без доказательства разрешён", True)
        except ToolError as exc:
            check("info без доказательства разрешён", False, str(exc))


def test_review_report_readiness() -> None:
    section("review_report: сводка и вывод о готовности к мержу")
    with tempfile.TemporaryDirectory() as td:
        ws, store, tools = _setup(Path(td))
        (ws.root / "app.py").write_text(SAMPLE_CODE, encoding="utf-8")

        out0 = tools["review_report"].fn()
        check("пустой отчёт объясняет, с чего начать", "Находок ещё нет" in out0)

        tools["review_finding"].fn(
            file="app.py", line_start=9, line_end=10, severity="minor",
            category="maintainability", title="неиспользуемая функция")
        out1 = tools["review_report"].fn()
        check("готово к мержу без critical/major", "нет открытых" in out1, out1)

        tools["review_finding"].fn(
            file="app.py", line_start=5, line_end=6, severity="critical",
            category="security", title="SQL-инъекция",
            quote='return f"SELECT * FROM users WHERE id={user_id}"')
        out2 = tools["review_report"].fn()
        check("НЕ готово при открытой critical", "НЕ готово" in out2, out2)

        summary = store.review_summary(1)
        check("сводка считает по severity верно",
              summary["critical"] == 1 and summary["minor"] == 1, str(summary))


def test_status_change_affects_readiness() -> None:
    section("review_set_status: закрытие находки меняет готовность к мержу")
    with tempfile.TemporaryDirectory() as td:
        ws, store, tools = _setup(Path(td))
        (ws.root / "app.py").write_text(SAMPLE_CODE, encoding="utf-8")

        out = tools["review_finding"].fn(
            file="app.py", line_start=5, line_end=6, severity="critical",
            category="security", title="SQL-инъекция",
            quote='return f"SELECT * FROM users WHERE id={user_id}"')
        fid = int(out.split("#")[1].split(" ")[0])

        report_before = tools["review_report"].fn()
        check("не готово, пока находка открыта", "НЕ готово" in report_before)

        tools["review_set_status"].fn(finding_id=fid, status="fixed")
        report_after = tools["review_report"].fn()
        check("готово после закрытия находки", "нет открытых" in report_after,
              report_after)

        try:
            tools["review_set_status"].fn(finding_id=fid, status="bogus")
            check("отказ на неизвестный статус", False)
        except ToolError:
            check("отказ на неизвестный статус", True)

        try:
            tools["review_set_status"].fn(finding_id=99999, status="fixed")
            check("отказ на несуществующую находку", False)
        except ToolError:
            check("отказ на несуществующую находку", True)


def test_review_reset() -> None:
    section("review_reset: начать ревью заново")
    with tempfile.TemporaryDirectory() as td:
        ws, store, tools = _setup(Path(td))
        (ws.root / "app.py").write_text(SAMPLE_CODE, encoding="utf-8")
        tools["review_finding"].fn(file="app.py", line_start=1, line_end=1,
                                   severity="info", category="style", title="x")
        tools["review_finding"].fn(file="app.py", line_start=2, line_end=2,
                                   severity="info", category="style", title="y")
        out = tools["review_reset"].fn()
        check("сброшено 2 записи", "2" in out, out)
        check("после сброса review_report снова пуст",
              "Находок ещё нет" in tools["review_report"].fn())


def test_store_severity_and_category_validation() -> None:
    section("Store.add_review_finding: отказ на уровне БД")
    with tempfile.TemporaryDirectory() as td:
        store = Store(str(Path(td) / "t.db"))
        try:
            store.add_review_finding("f.py", 1, 1, "invalid", "bug", "t")
            check("отказ на невалидный severity в Store", False)
        except ValueError:
            check("отказ на невалидный severity в Store", True)
        try:
            store.add_review_finding("f.py", 1, 1, "minor", "invalid", "t")
            check("отказ на невалидную category в Store", False)
        except ValueError:
            check("отказ на невалидную category в Store", True)
        store.close()


def test_quote_matching_ignores_whitespace_and_case() -> None:
    section("Сопоставление цитаты нормализует пробелы/переносы/регистр")
    with tempfile.TemporaryDirectory() as td:
        ws, store, tools = _setup(Path(td))
        (ws.root / "app.py").write_text(
            "def  f(x):\n    return   x*2\n", encoding="utf-8")
        out = tools["review_finding"].fn(
            file="app.py", line_start=2, line_end=2, severity="major",
            category="bug", title="x", quote="return x*2")
        check("цитата с другими пробелами найдена", "ПОНИЖЕНО" not in out, out)


def test_optional_quote_on_non_required_severity_still_checked() -> None:
    section("review_finding: необязательная цитата у minor/info тоже проверяется, но не блокирует")
    with tempfile.TemporaryDirectory() as td:
        ws, store, tools = _setup(Path(td))
        (ws.root / "app.py").write_text(SAMPLE_CODE, encoding="utf-8")
        out = tools["review_finding"].fn(
            file="app.py", line_start=1, line_end=1, severity="minor",
            category="style", title="x",
            quote="этой строки тут вообще не существует никогда")
        check("minor не падает даже с плохой цитатой", "minor" in out, out)
        check("но отмечает, что цитата не найдена", "не найдена" in out, out)
        findings = store.review_findings(1)
        check("severity осталась заявленной (minor не требует quote)",
              findings[0]["severity"] == "minor")
        check("quote_verified=0", findings[0]["quote_verified"] == 0)


# ============================================================ integration
def test_build_agent_with_code_review_skill() -> None:
    section("Сборка агента с навыком code_review")
    from agent.build import build_agent

    with tempfile.TemporaryDirectory() as td:
        cfg = Config(provider="ollama", model="m", workspace=td,
                    skills=["files", "shell", "code_review"])
        agent = build_agent(cfg)
        names = agent.tools.names()
        check("инструмент review_finding зарегистрирован", "review_finding" in names)
        check("инструмент review_report зарегистрирован", "review_report" in names)
        check("инструмент review_set_status зарегистрирован",
              "review_set_status" in names)
        check("инструмент review_reset зарегистрирован", "review_reset" in names)


def test_code_reviewer_profile() -> None:
    section("Профиль code_reviewer собирается со всеми нужными навыками")
    from agent.build import build_agent

    with tempfile.TemporaryDirectory() as td:
        cfg = Config.load(None, provider="ollama", model="m", workspace=td,
                          profile="code_reviewer")
        agent = build_agent(cfg)
        names = agent.tools.names()
        check("review_finding доступен в code_reviewer", "review_finding" in names)
        check("review_report доступен в code_reviewer", "review_report" in names)
        check("run_command доступен (нужен для git diff/тестов)",
              "run_command" in names)
        check("read_file доступен", "read_file" in names)
        check("present доступен", "present" in names)


def test_router_picks_code_reviewer() -> None:
    section("Роутер выбирает code_reviewer по характерным формулировкам")
    from agent.router import pick_profile

    infos = Config.profile_infos()
    d = pick_profile("проведи код-ревью этого репозитория и найди уязвимости",
                     infos)
    check("code_reviewer выбран для явно ревьюверской задачи",
          d.profile == "code_reviewer", d.profile)


def test_example_config_loads() -> None:
    section("examples/config.code_review.json грузится без ошибок")
    root = Path(__file__).resolve().parents[1]
    cfg = Config.load(str(root / "examples" / "config.code_review.json"))
    check("профиль code_reviewer выбран", cfg.profile == "code_reviewer")
    check("навык code_review подключён", "code_review" in cfg.skills)
    check("комментарные ключи не попали в поля",
          not hasattr(cfg, "_комментарий_профиль"))


def main() -> int:
    test_critical_with_real_quote_on_correct_lines()
    test_fabricated_quote_is_downgraded()
    test_quote_found_but_wrong_line_range()
    test_minor_and_info_do_not_require_quote()
    test_line_margin_tolerance()
    test_negative_cases()
    test_review_report_readiness()
    test_status_change_affects_readiness()
    test_review_reset()
    test_store_severity_and_category_validation()
    test_quote_matching_ignores_whitespace_and_case()
    test_optional_quote_on_non_required_severity_still_checked()
    test_build_agent_with_code_review_skill()
    test_code_reviewer_profile()
    test_router_picks_code_reviewer()
    test_example_config_loads()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
