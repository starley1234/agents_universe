"""Навык «cert_verify»: валидация и верификация комплекта документов для
процесса сертификации авиационной техники (Росавиация/Авиарегистр,
Федеральные авиационные правила Часть 21 — ФАП-21).

ТЕРМИНОЛОГИЯ ИЗ ФАП-21 (используется как есть, чтобы отчёт был понятен
инженеру по сертификации, а не изобретённым языком):
  * Сертификационный базис — набор применимых требований к лётной
    годности, распространённых на образец авиационной техники.
  * Доказательная документация — документы с результатами расчётов,
    проверок, испытаний и оценок, устанавливающие соответствие
    применимым требованиям (п.21.4С ФАП-21).
  * Акт о соответствии/несоответствии — итоговый документ проверки:
    по каждому пункту требования — вывод и ссылка на доказательство.

ГЛАВНЫЙ ПРИНЦИП, ради которого этот навык вообще нужен отдельным кодом,
а не просто промптом "проверь документы на соответствие требованиям":
МОДЕЛЬ НЕ МОЖЕТ ПРОСТАВИТЬ «СООТВЕТСТВУЕТ» НА ОСНОВЕ ВЫДУМАННОЙ ЦИТАТЫ.
Каждая проверка (cert_check) обязана указывать evidence_source (файл) и
evidence_quote (дословную цитату из него). Инструмент cert_check САМ
ищет эту цитату в реальном тексте источника (после нормализации
пробелов/регистра — не после семантического сходства, это должно быть
буквальное вхождение). Если цитата не находится — verdict ПРИНУДИТЕЛЬНО
понижается до needs_review, что бы ни утверждала модель. Это тот же
приём, что доказательные проверки в skills/cad_openscad.py: НЕ ОЦЕНИВАТЬ
НА ГЛАЗ (там — геометрию, здесь — соответствие цитаты источнику).

Источник для проверки цитаты — ЛЮБОЙ уже прочитанный текст: содержимое
файла из workspace (документы уже распознаны pdf_extract/doc_extract) и
опционально фрагменты, ранее проиндексированные навыком rag (chunk).
Сам cert_verify документы не парсит — это делают pdf/docparse/rag.

Второй инструмент, checklist_from_text, — вспомогательный: разбирает
обычный текстовый чек-лист (по одному пункту требования на строку,
опционально с описанием после `-`/`:`) в структуру, которую агенту
проще пройти по порядку, не пропуская пункты.
"""
from __future__ import annotations

import re

from ..store import Store
from ..tools.base import Tool, ToolError, Workspace

VERDICTS = {
    "compliant": "требование выполнено, подтверждено доказательством",
    "non_compliant": "требование НЕ выполнено — есть доказательство несоответствия",
    "not_found": "в предоставленных документах нет доказательства ни за, ни против",
    "needs_review": "требует ручной проверки эксперта (в т.ч. цитата не подтвердилась)",
}


def _normalize(text: str) -> str:
    """Пробелы/переносы строк к одному виду, регистр — к нижнему. Нужна
    для сопоставления цитаты с исходником: PDF/Word часто рвут
    строки посреди фразы, а требовать точного посимвольного совпадения
    с переносами — значит ловить ложные "цитата не найдена" на пустом
    месте."""
    return re.sub(r"\s+", " ", text).strip().lower()


def _quote_len_ok(quote: str) -> bool:
    """Отсекаем вырожденные "цитаты" вроде одного слова или пустой
    строки — иначе любое слово будет "находиться" в любом документе,
    и проверка цитаты потеряет смысл."""
    return len(quote.strip()) >= 15


# ------------------------------------------------------ разбор чек-листа
_CHECKLIST_LINE = re.compile(r"^\s*(?:[-*]|\d+[.)])?\s*(.+?)\s*$")


def parse_checklist(text: str) -> list[dict[str, str]]:
    """Строка чек-листа -> {"requirement": ..., "requirement_text": ...}.

    Формат гибкий: 'ФАП-21 п.21.16А — применимые требования к лётной
    годности' или просто 'п.21.16А'. Разделитель описания — первое ' — ',
    ' - ' или ':' после кода требования (эвристика, не строгий парсер —
    цель дать агенту стартовую структуру, а не заменить эксперта).
    """
    items: list[dict[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip(" \t")
        if not line:
            continue
        m = _CHECKLIST_LINE.match(line)
        body = m.group(1) if m else line
        for sep in (" — ", " – ", " - ", ": "):
            if sep in body:
                req, _, desc = body.partition(sep)
                items.append({"requirement": req.strip(),
                             "requirement_text": desc.strip()})
                break
        else:
            items.append({"requirement": body.strip(), "requirement_text": ""})
    return items


# ==================================================================== build
def build(ws: Workspace, store: Store, run_id_getter) -> list[Tool]:

    def rid() -> int | None:
        return run_id_getter() if run_id_getter else None

    def _find_quote(quote: str, source: str) -> bool:
        """Ищет ДОСЛОВНОЕ (после нормализации пробелов/регистра) вхождение
        цитаты в указанном источнике: сначала как файл workspace, если
        такого нет — среди чанков, проиндексированных навыком rag под
        этим именем source."""
        needle = _normalize(quote)
        if not needle:
            return False

        try:
            p = ws.resolve(source)
        except ToolError:
            p = None
        if p is not None and p.exists() and p.is_file():
            try:
                body = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                body = ""
            if needle in _normalize(body):
                return True

        for chunk in store.all_chunks(source=source):
            if needle in _normalize(chunk["text"]):
                return True
        return False

    def checklist_from_text(text: str) -> str:
        """Разобрать текстовый чек-лист требований в пункты — по одному
        на строку, чтобы дальше пройти их по порядку через cert_check."""
        items = parse_checklist(text)
        if not items:
            raise ToolError("Не удалось выделить ни одного пункта чек-листа")
        out = [f"Выделено {len(items)} пункт(ов) требований:"]
        for i, it in enumerate(items, 1):
            desc = f" — {it['requirement_text']}" if it["requirement_text"] else ""
            out.append(f"  {i}. {it['requirement']}{desc}")
        out.append("\nПройдите каждый пункт через cert_check: для "
                  "compliant/non_compliant обязательны evidence_source и "
                  "evidence_quote — дословная цитата, которую инструмент "
                  "сам проверит на наличие в источнике.")
        return "\n".join(out)

    def cert_check(requirement: str, verdict: str, requirement_text: str = "",
                   evidence_source: str = "", evidence_quote: str = "",
                   comment: str = "") -> str:
        """Зарегистрировать вывод по одному пункту требования.

        Для verdict=compliant/non_compliant evidence_source и
        evidence_quote ОБЯЗАТЕЛЬНЫ — без доказательства такой вывод не
        имеет смысла. Цитата проверяется на дословное наличие в
        источнике; если не находится — verdict принудительно
        понижается до needs_review, а не остаётся как заявлено моделью.
        """
        if not requirement.strip():
            raise ToolError("Не указан пункт требования (requirement)")
        if verdict not in VERDICTS:
            raise ToolError(
                f"Неизвестный verdict {verdict!r}. Доступны: "
                + ", ".join(f"{k} ({v})" for k, v in VERDICTS.items())
            )

        effective_verdict = verdict
        note = ""
        if verdict in ("compliant", "non_compliant"):
            if not evidence_source.strip() or not evidence_quote.strip():
                raise ToolError(
                    f"Для verdict={verdict!r} обязательны evidence_source "
                    "и evidence_quote — вывод о соответствии без "
                    "доказательства не регистрируется. Если доказательства "
                    "нет, используйте verdict=not_found."
                )
            if not _quote_len_ok(evidence_quote):
                raise ToolError(
                    "evidence_quote слишком короткая (меньше 15 символов) "
                    "— такая 'цитата' найдётся в любом тексте и ничего не "
                    "доказывает. Приведите содержательный фрагмент."
                )
            verified = _find_quote(evidence_quote, evidence_source)
            if not verified:
                effective_verdict = "needs_review"
                note = (f" [ПОНИЖЕНО с {verdict}: цитата не найдена дословно "
                       f"в {evidence_source!r} — проверьте вручную]")
        else:
            verified = False

        cid = store.add_cert_check(
            requirement, effective_verdict, requirement_text=requirement_text,
            evidence_source=evidence_source, evidence_quote=evidence_quote,
            quote_verified=verified, comment=comment, run_id=rid())

        return (f"Пункт {requirement!r}: {effective_verdict}{note} "
               f"(id={cid}, цитата подтверждена: {'да' if verified else 'нет'})")

    def cert_report() -> str:
        """Итоговый акт: сводка по всем зарегистрированным проверкам —
        аналог акта о соответствии/несоответствии."""
        checks = store.cert_checks(rid())
        if not checks:
            return ("Проверок ещё нет. Сначала checklist_from_text (если "
                   "есть текстовый чек-лист) или сразу cert_check по "
                   "каждому пункту требования.")
        summary = store.cert_summary(rid())
        lines = [f"Акт проверки: {summary['total']} пункт(ов)",
                f"  соответствует: {summary['compliant']}",
                f"  не соответствует: {summary['non_compliant']}",
                f"  доказательство не найдено: {summary['not_found']}",
                f"  требует ручной проверки: {summary['needs_review']}"]
        if summary["unverified_quotes"]:
            lines.append(f"  ВНИМАНИЕ: {summary['unverified_quotes']} "
                        "цитат(ы) не подтвердились дословно в источнике")
        lines.append("")
        for c in checks:
            mark = {"compliant": "[+]", "non_compliant": "[-]",
                    "not_found": "[?]", "needs_review": "[!]"}[c["verdict"]]
            lines.append(f"{mark} {c['requirement']}"
                        + (f" — {c['requirement_text']}"
                           if c["requirement_text"] else ""))
            if c["evidence_source"]:
                vtag = "подтверждена" if c["quote_verified"] else "НЕ подтверждена"
                lines.append(f"    источник: {c['evidence_source']} "
                            f"(цитата {vtag})")
                lines.append(f"    цитата: {c['evidence_quote'][:200]}")
            if c["comment"]:
                lines.append(f"    комментарий: {c['comment']}")
        readiness = (summary["non_compliant"] == 0
                    and summary["not_found"] == 0
                    and summary["needs_review"] == 0
                    and summary["total"] > 0)
        lines.append("")
        lines.append(
            "ГОТОВНОСТЬ К ПОДАЧЕ: " +
            ("все пункты подтверждены доказательствами" if readiness else
             "НЕ готово — есть пункты без подтверждённого доказательства "
             "(см. выше)"))
        return "\n".join(lines)

    def cert_reset() -> str:
        """Начать проверку заново (например, для нового комплекта
        документов в том же прогоне)."""
        n = store.clear_cert_checks(rid())
        return f"Очищено {n} ранее зарегистрированных проверок"

    return [
        Tool("checklist_from_text",
             "Разобрать текстовый чек-лист требований (по одному пункту "
             "на строку, например из Сертификационного базиса) в "
             "структурированный список, чтобы пройти его по порядку.",
             {"type": "object",
              "properties": {"text": {"type": "string"}},
              "required": ["text"]},
             checklist_from_text),
        Tool("cert_check",
             "Зарегистрировать вывод по ОДНОМУ пункту требования "
             "сертификационного базиса. Для verdict=compliant/non_compliant "
             "ОБЯЗАТЕЛЬНЫ evidence_source (файл-доказательство) и "
             "evidence_quote (дословная цитата из него) — инструмент сам "
             "проверяет, что цитата реально есть в источнике, и понижает "
             "вывод до needs_review, если не находит её. Не выдумывай "
             "цитаты: если доказательства нет — используй verdict=not_found.",
             {"type": "object",
              "properties": {
                  "requirement": {"type": "string",
                                  "description": "Пункт требования, напр. 'ФАП-21 п.21.16А'"},
                  "verdict": {"type": "string",
                              "description": ", ".join(VERDICTS)},
                  "requirement_text": {"type": "string",
                                       "description": "Текст/суть требования"},
                  "evidence_source": {"type": "string",
                                      "description": "Файл-доказательство из workspace "
                                                    "или source, ранее проиндексированный rag"},
                  "evidence_quote": {"type": "string",
                                     "description": "Дословная цитата из источника"},
                  "comment": {"type": "string"}},
              "required": ["requirement", "verdict"]},
             cert_check),
        Tool("cert_report",
             "Собрать итоговый акт проверки: сводка по всем пунктам, "
             "непосредственно вывод о готовности комплекта документов к "
             "подаче. Вызывай в конце, после того как пройдены все пункты "
             "чек-листа.",
             {"type": "object", "properties": {}, "required": []},
             cert_report),
        Tool("cert_reset",
             "Очистить ранее зарегистрированные проверки — для повторного "
             "прогона на обновлённом комплекте документов.",
             {"type": "object", "properties": {}, "required": []},
             cert_reset),
    ]
