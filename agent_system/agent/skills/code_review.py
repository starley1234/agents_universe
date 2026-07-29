"""Навык «code_review»: аудит/ревью кода репозитория с проверяемыми находками.

ГЛАВНЫЙ ПРИНЦИП, ради которого этот навык вообще нужен отдельным кодом,
а не просто промптом "проверь код и найди проблемы": МОДЕЛЬ НЕ МОЖЕТ
ЗАЯВИТЬ СЕРЬЁЗНУЮ НАХОДКУ (critical/major) БЕЗ ПРОВЕРЯЕМОЙ ЦИТАТЫ
РЕАЛЬНОГО КОДА. Ревью-модель славится тем, что уверенно описывает баг,
которого в файле нет (переставила аргументы местами, придумала строку,
обобщила по паттерну без факт-чека). review_finding САМ ищет указанную
цитату в реальном файле (после нормализации пробелов — как в
skills/cert_verify.py) и, если не находит её вовсе, ПРИНУДИТЕЛЬНО
понижает severity до "info" — что бы ни утверждала модель. Если цитата
нашлась, но не в заявленном диапазоне строк, отчёт получает пометку
"неточная локация" — построчная навигация всё равно останется полезной,
но не выдаётся за проверенную с точностью до строки.

Это та же архитектура, что и в cert_verify.py (evidence_quote проверяется
буквально) и cad_openscad.py (геометрия не оценивается на глаз, а
проверяется расчётом) — «тест обязан уметь падать» распространяется и на
находки код-ревью: без проверки цитаты отчёт превращается в список
недоказанных утверждений модели, что бесполезно для реального ревью.

Сам code_review НЕ читает файлы и НЕ ищет диффы — это делают files
(read_file/list_files) и shell (run_command: "git diff", "git log" и
т.п.), уже доступные агенту. code_review отвечает только за
регистрацию/проверку/сводку находок — единый механизм, не зависящий от
того, как агент нашёл проблему (просмотр всего файла, git diff, чужой
статический анализатор через run_command).
"""
from __future__ import annotations

import re

from ..store import Store
from ..tools.base import Tool, ToolError, Workspace

SEVERITIES = {
    "critical": "ломает функциональность/данные/безопасность немедленно",
    "major": "серьёзная проблема, требует исправления до релиза",
    "minor": "стоит исправить, не блокирует релиз",
    "info": "замечание/предложение, не проблема как таковая",
}

CATEGORIES = {
    "bug": "логическая ошибка, даёт неверный результат",
    "security": "уязвимость: инъекция, утечка секрета, небезопасная десериализация и т.п.",
    "performance": "неоправданно медленно/тратит ресурсы",
    "maintainability": "сложно поддерживать: дублирование, запутанность, магические числа",
    "style": "нарушение стиля/соглашений проекта",
    "testing": "недостаточное покрытие тестами или тест не умеет падать",
    "documentation": "отсутствующая/вводящая в заблуждение документация",
    "other": "не подходит под остальные категории",
}

#: severity, для которых evidence (цитата кода) ОБЯЗАТЕЛЬНА — некритичные
#: замечания (style/info) не обязаны цитировать код дословно, серьёзные —
#: обязаны, тот же принцип, что compliant/non_compliant в cert_verify.
_SEVERITY_REQUIRES_QUOTE = {"critical", "major"}

#: на сколько строк вокруг заявленного диапазона ищем цитату ДО того, как
#: считать локацию неточной — модель обычно промахивается на 1-3 строки
#: при подсчёте (комментарии, пустые строки), а не на порядок величины.
_LINE_MARGIN = 3

#: минимальная длина цитаты — короче она находится где угодно и ничего
#: не доказывает (тот же порог принципа, что MIN quote в cert_verify).
_MIN_QUOTE_LEN = 8


def _normalize(text: str) -> str:
    """Пробелы/переносы к одному виду — код форматируется по-разному
    (табы/пробелы, обрезанные пробелы в конце строки), а буквальное
    посимвольное совпадение ловило бы ложные "не найдено"."""
    return re.sub(r"\s+", " ", text).strip()


def _quote_len_ok(quote: str) -> bool:
    return len(quote.strip()) >= _MIN_QUOTE_LEN


def build(ws: Workspace, store: Store, run_id_getter) -> list[Tool]:

    def rid() -> int | None:
        return run_id_getter() if run_id_getter else None

    def _locate_quote(quote: str, file: str, line_start: int,
                      line_end: int) -> tuple[bool, bool]:
        """Вернёт (найдена_в_файле, найдена_в_заявленном_диапазоне).

        Сначала проверяем ДИАПАЗОН (с запасом _LINE_MARGIN) — это самый
        частый и самый полезный случай: модель права и указала верное
        место. Если там не нашлось — проверяем файл целиком: может быть,
        строки посчитаны неверно, но проблема реальна.
        """
        needle = _normalize(quote)
        if not needle:
            return False, False
        try:
            p = ws.resolve(file)
        except ToolError:
            return False, False
        if not p.exists() or not p.is_file():
            return False, False
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return False, False

        lo = max(0, line_start - 1 - _LINE_MARGIN)
        hi = min(len(lines), line_end + _LINE_MARGIN)
        window = _normalize("\n".join(lines[lo:hi]))
        if needle in window:
            return True, True

        whole = _normalize("\n".join(lines))
        return (needle in whole), False

    def review_finding(file: str, line_start: int, line_end: int,
                       severity: str, category: str, title: str,
                       description: str = "", suggestion: str = "",
                       quote: str = "") -> str:
        """Зарегистрировать одну находку код-ревью.

        Для severity=critical/major ОБЯЗАТЕЛЬНА quote — дословный
        фрагмент кода, на который указывает находка. Инструмент сам
        проверяет её наличие в файле (в заявленном диапазоне строк, с
        запасом в несколько строк, либо в файле целиком). Если цитата
        не находится вовсе — severity принудительно понижается до
        "info": находка остаётся видна в отчёте, но не может исказить
        итоговую оценку готовности.
        """
        if severity not in SEVERITIES:
            raise ToolError(
                f"Неизвестный severity {severity!r}. Доступны: "
                + ", ".join(f"{k} ({v})" for k, v in SEVERITIES.items())
            )
        if category not in CATEGORIES:
            raise ToolError(
                f"Неизвестный category {category!r}. Доступны: "
                + ", ".join(f"{k} ({v})" for k, v in CATEGORIES.items())
            )
        if not title.strip():
            raise ToolError("Не указан заголовок находки (title)")
        if line_start <= 0 or line_end < line_start:
            raise ToolError(
                "Некорректный диапазон строк: line_start >= 1 и "
                "line_end >= line_start"
            )

        effective_severity = severity
        note = ""
        precise_location = True
        verified = False

        if severity in _SEVERITY_REQUIRES_QUOTE:
            if not quote.strip():
                raise ToolError(
                    f"Для severity={severity!r} обязательна quote — дословный "
                    "фрагмент кода, подтверждающий находку. Если не можете "
                    "процитировать код буквально, используйте severity=minor "
                    "или info."
                )
            if not _quote_len_ok(quote):
                raise ToolError(
                    f"quote слишком короткая (меньше {_MIN_QUOTE_LEN} символов) "
                    "— такой фрагмент найдётся где угодно и ничего не "
                    "доказывает. Приведите содержательную строку кода."
                )
            found_in_file, in_range = _locate_quote(quote, file, line_start,
                                                     line_end)
            verified = found_in_file
            precise_location = in_range
            if not found_in_file:
                effective_severity = "info"
                note = (f" [ПОНИЖЕНО с {severity}: цитата не найдена в "
                       f"{file!r} — проверьте вручную или уточните находку]")
            elif not in_range:
                note = (" [цитата найдена в файле, но не в заявленных "
                       f"строках {line_start}-{line_end} — уточните диапазон]")
        elif quote.strip() and _quote_len_ok(quote):
            # необязательная, но раз дали — проверим и её тоже, честности ради
            found_in_file, in_range = _locate_quote(quote, file, line_start,
                                                     line_end)
            verified = found_in_file
            precise_location = in_range
            if not found_in_file:
                note = " [цитата не найдена в файле — не влияет на severity]"

        fid = store.add_review_finding(
            file, line_start, line_end, effective_severity, category,
            title, description=description, suggestion=suggestion,
            quote=quote, quote_verified=verified,
            precise_location=precise_location,
            original_severity=severity, run_id=rid())

        return (f"Находка #{fid} в {file}:{line_start}-{line_end}: "
               f"{effective_severity}/{category} — {title}{note}")

    def review_set_status(finding_id: int, status: str) -> str:
        """Изменить статус находки: fixed (исправлено), wontfix (осознанно

        не будем чинить, с обоснованием в description при желании),
        acknowledged (принято к сведению, не блокирует)."""
        try:
            store.set_review_finding_status(finding_id, status)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return f"Находка #{finding_id}: статус изменён на {status!r}"

    def review_report() -> str:
        """Итоговый отчёт код-ревью: находки по убыванию серьёзности,

        сводка по severity/category, вывод о готовности к мержу."""
        findings = store.review_findings(rid())
        if not findings:
            return ("Находок ещё нет. Изучите код (read_file/list_files, "
                   "run_command 'git diff'/'git log') и регистрируйте "
                   "каждую проблему через review_finding.")
        summary = store.review_summary(rid())
        lines = [f"Отчёт код-ревью: {summary['total']} находок "
                f"({summary['open']} открыто)",
                f"  critical: {summary['critical']}",
                f"  major: {summary['major']}",
                f"  minor: {summary['minor']}",
                f"  info: {summary['info']}"]
        if summary["unverified_quotes"]:
            lines.append(f"  ВНИМАНИЕ: {summary['unverified_quotes']} "
                        "цитат(ы) не подтвердились в реальном коде")
        lines.append("")
        mark = {"critical": "[!!]", "major": "[! ]", "minor": "[- ]", "info": "[i ]"}
        for f in findings:
            lines.append(f"{mark[f['severity']]} {f['file']}:"
                        f"{f['line_start']}-{f['line_end']} "
                        f"[{f['category']}] {f['title']} "
                        f"(#{f['id']}, {f['status']})")
            if f["original_severity"] != f["severity"]:
                lines.append(f"    (заявлено как {f['original_severity']}, "
                            "понижено — цитата не подтвердилась)")
            elif f["quote"] and not f["precise_location"]:
                lines.append("    (цитата найдена не в заявленных строках)")
            if f["description"]:
                lines.append(f"    {f['description'][:300]}")
            if f["suggestion"]:
                lines.append(f"    предложение: {f['suggestion'][:300]}")
            if f["quote"]:
                lines.append(f"    код: {f['quote'][:200]}")
        blocking = sum(1 for f in findings
                      if f["status"] == "open"
                      and f["severity"] in ("critical", "major"))
        lines.append("")
        lines.append(
            "ГОТОВНОСТЬ К МЕРЖУ: " +
            ("НЕ готово — есть непроверенные critical/major находки" if blocking
             else "нет открытых critical/major находок")
        )
        return "\n".join(lines)

    def review_reset() -> str:
        """Начать ревью заново — например, после переписывания кода."""
        n = store.clear_review_findings(rid())
        return f"Очищено {n} ранее зарегистрированных находок"

    return [
        Tool("review_finding",
             "Зарегистрировать ОДНУ находку код-ревью: файл, диапазон строк, "
             "серьёзность (critical/major/minor/info), категория (bug/"
             "security/performance/maintainability/style/testing/"
             "documentation/other). Для critical/major ОБЯЗАТЕЛЬНА quote — "
             "дословный фрагмент кода. Инструмент сам проверяет, что цитата "
             "реально есть в файле, и понижает severity до info, если не "
             "находит её. Не выдумывай код: если не уверен в точной цитате "
             "— используй severity=minor/info.",
             {"type": "object",
              "properties": {
                  "file": {"type": "string",
                           "description": "Путь к файлу относительно рабочей папки"},
                  "line_start": {"type": "integer"},
                  "line_end": {"type": "integer"},
                  "severity": {"type": "string", "description": ", ".join(SEVERITIES)},
                  "category": {"type": "string", "description": ", ".join(CATEGORIES)},
                  "title": {"type": "string", "description": "Короткая суть находки"},
                  "description": {"type": "string"},
                  "suggestion": {"type": "string",
                                 "description": "Как исправить"},
                  "quote": {"type": "string",
                           "description": "Дословный фрагмент кода-доказательства"}},
              "required": ["file", "line_start", "line_end", "severity",
                          "category", "title"]},
             review_finding),
        Tool("review_set_status",
             "Изменить статус ранее зарегистрированной находки: fixed, "
             "wontfix, acknowledged, open.",
             {"type": "object",
              "properties": {
                  "finding_id": {"type": "integer"},
                  "status": {"type": "string",
                            "description": "open, fixed, wontfix, acknowledged"}},
              "required": ["finding_id", "status"]},
             review_set_status),
        Tool("review_report",
             "Собрать итоговый отчёт код-ревью: список находок по "
             "убыванию серьёзности, сводка, вывод о готовности к мержу. "
             "Вызывай в конце, после того как код изучен целиком.",
             {"type": "object", "properties": {}, "required": []},
             review_report),
        Tool("review_reset",
             "Очистить ранее зарегистрированные находки — для повторного "
             "ревью после переписывания кода.",
             {"type": "object", "properties": {}, "required": []},
             review_reset),
    ]
