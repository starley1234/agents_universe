"""Валидация и верификация комплекта документов.

Задача: подготовка к сертификации (например, по ФАП Росавиации). Здесь
проверяется КОМПЛЕКТНОСТЬ и ПРОСЛЕЖИВАЕМОСТЬ — то, что поддаётся
формальной проверке:

  * все ли обязательные документы есть;
  * каждое ли требование закрыто доказательством;
  * нет ли противоречий в числах между документами;
  * заполнены ли обязательные реквизиты.

ГЛАВНОЕ ОГРАНИЧЕНИЕ, которое нельзя замалчивать: инструмент НЕ выносит
заключение о соответствии. Он находит формальные пробелы и готовит
материал для эксперта. Решение о выдаче сертификата принимает
уполномоченный орган, и никакой агент его не заменяет. Каждый отчёт
заканчивается этой оговоркой — не для галочки, а потому что выдать
машинную проверку за экспертизу означает подвести человека.

Различение терминов, как в отраслевой практике:
  ВАЛИДАЦИЯ    — тот ли документ: комплектность, реквизиты, форма;
  ВЕРИФИКАЦИЯ  — верно ли по существу: требование ↔ доказательство,
                 согласованность данных между документами.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..tools.base import Tool, ToolError, Workspace
from .documents import extract, read_any

#: Обязательные реквизиты документа. Отсутствие — формальный дефект.
REQUISITES = {
    "номер": r"№\s?[\w\-/.]+",
    "дата": r"\b\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}\b",
    "подпись": r"подпис|утвержд|согласован|м\.?п\.?",
    "исполнитель": r"исполнитель|разработчик|составил|заявитель",
}

#: Формулировки требований: то, что должно быть чем-то подтверждено.
#: Строка считается требованием, если содержит слово-маркер.
#: РАНЬШЕ БЫЛО НЕВЕРНО: регулярка требовала ≥15 символов ДО маркера и
#: молча теряла короткие формулировки — «Не допускается…» (4 символа),
#: «Изделие должно…» (12). Из четырёх требований находилось одно, а
#: покрытие показывалось как 100 %. Такая ошибка опаснее отсутствия
#: проверки: она создаёт ложную уверенность.
REQ_MARKER = re.compile(
    r"\b(должен|должна|должно|должны|обязан|обязана|обязано|обязаны|"
    r"не\s+допускается|не\s+допускаются|требуется|требуются|подлежит|"
    r"подлежат|необходимо|надлежит|запрещается)\b", re.I)

#: Слова, указывающие на доказательство выполнения.
EVIDENCE = ["протокол", "акт", "испытан", "измерен", "расчёт", "расчет",
            "заключение", "сертификат", "отчёт", "отчет", "подтвержда",
            "результат", "проверк"]


def _num_facts(text: str) -> dict[str, list[str]]:
    """Числовые утверждения: «масса 12.5 кг» → {масса: [12.5 кг]}.

    Берём только пары «слово + число + единица»: без единицы число
    слишком неоднозначно, чтобы на нём строить вывод о противоречии.
    """
    out: dict[str, list[str]] = {}
    rx = re.compile(
        r"([А-Яа-яЁёA-Za-z][\w\s\-]{2,40}?)\s*[:—–-]?\s*"
        r"(?:не\s+(?:более|менее|выше|ниже)\s+)?"
        r"(\d+(?:[.,]\d+)?)\s*(мм|см|м|кг|г|т|%|°|В|А|Вт|кВт|Гц|ч|мин|с)\b",
        re.I)
    for m in rx.finditer(text):
        key = re.sub(r"\s+", " ", m.group(1)).strip().lower()
        key = re.sub(r"^(и|в|на|с|по|от|для|the|a)\s+", "", key)
        if len(key) < 3:
            continue
        val = f"{m.group(2).replace(',', '.')} {m.group(3)}"
        out.setdefault(key, [])
        if val not in out[key]:
            out[key].append(val)
    return out


def build(ws: Workspace, store=None, run_id_getter=None) -> list[Tool]:

    def _load(paths: list[str]) -> dict[str, str]:
        docs: dict[str, str] = {}
        for p in paths:
            f = ws.resolve(p)
            if f.is_dir():
                for q in sorted(f.rglob("*")):
                    if q.is_file() and q.suffix.lower() in (
                            ".pdf", ".docx", ".xlsx", ".csv", ".md", ".txt"):
                        try:
                            docs[q.name] = read_any(q)[0]
                        except ToolError:
                            pass
            elif f.is_file():
                docs[f.name] = read_any(f)[0]
        return docs

    # ─────────────────────── валидация комплекта ──────────────────
    def check_completeness(path: str, required: str) -> str:
        """Все ли обязательные документы присутствуют."""
        need = [s.strip() for s in required.replace(",", "\n").splitlines()
                if s.strip()]
        if not need:
            raise ToolError("не задан список обязательных документов")
        docs = _load([path])
        if not docs:
            raise ToolError(f"в {path!r} нет читаемых документов")

        names = {n.lower(): n for n in docs}
        found, missing = [], []
        for item in need:
            key = item.lower()
            hit = next((orig for low, orig in names.items()
                        if key in low or key in docs[orig][:3000].lower()), None)
            (found if hit else missing).append((item, hit))

        out = [f"ВАЛИДАЦИЯ КОМПЛЕКТНОСТИ: {path}",
               f"Документов в комплекте: {len(docs)}",
               f"Обязательных позиций: {len(need)}", ""]
        out.append(f"Найдено ({len(found)}):")
        out += [f"  + {i} → {h}" for i, h in found]
        if missing:
            out += ["", f"ОТСУТСТВУЕТ ({len(missing)}):"]
            out += [f"  − {i}" for i, _ in missing]
            out += ["", "Комплект НЕ полон."]
        else:
            out += ["", "Комплект полон по заданному перечню."]
        out += ["", "Проверена только формальная комплектность. Содержание "
                    "документов оценивает эксперт."]
        return "\n".join(out)

    def check_requisites(path: str) -> str:
        """Заполнены ли обязательные реквизиты."""
        docs = _load([path])
        if not docs:
            raise ToolError(f"в {path!r} нет читаемых документов")
        out = ["ВАЛИДАЦИЯ РЕКВИЗИТОВ", ""]
        bad = 0
        for name, text in docs.items():
            miss = [k for k, rx in REQUISITES.items()
                    if not re.search(rx, text, re.I)]
            if miss:
                bad += 1
                out.append(f"  {name}: НЕТ {', '.join(miss)}")
            else:
                out.append(f"  {name}: все реквизиты на месте")
        out += ["", f"С замечаниями: {bad} из {len(docs)}"]
        if bad:
            out.append("Отсутствие реквизита — формальное основание "
                       "для возврата документа.")
        return "\n".join(out)

    # ──────────────────── верификация по существу ─────────────────
    def verify_requirements(req_path: str, evidence_path: str,
                            out_path: str = "") -> str:
        """Каждое ли требование закрыто доказательством."""
        reqs_text = "\n".join(_load([req_path]).values())
        if not reqs_text.strip():
            raise ToolError(f"в {req_path!r} нет текста требований")
        ev_docs = _load([evidence_path])
        if not ev_docs:
            raise ToolError(f"в {evidence_path!r} нет доказательных документов")

        # Разбираем построчно и по предложениям: так короткие
        # формулировки не теряются.
        reqs: list[str] = []
        for line in reqs_text.split("\n"):
            line = line.strip()
            if len(line) < 10:
                continue
            for sent in re.split(r"(?<=[.;])\s+", line):
                sent = re.sub(r"\s+", " ", sent).strip()
                if len(sent) < 10 or len(sent) > 400:
                    continue
                if REQ_MARKER.search(sent) and sent not in reqs:
                    reqs.append(sent)
        if not reqs:
            return ("Требований не найдено. Ищутся формулировки со словами "
                    "«должен», «обязан», «не допускается», «требуется», "
                    "«подлежит», «запрещается». Проверьте, тот ли документ "
                    "указан.")

        covered, uncovered, partial = [], [], []
        for r in reqs:
            # ключевые слова требования — существительные подлиннее
            words = [w.lower() for w in re.findall(r"[А-Яа-яЁёA-Za-z]{5,}", r)]
            words = [w for w in words if w not in
                     ("должен", "должна", "должно", "должны", "обязан",
                      "требуется", "подлежит", "необходимо", "допускается")]
            if not words:
                continue
            key = words[:8]
            best, best_hits, best_ev = None, 0, False
            for dname, dtext in ev_docs.items():
                low = dtext.lower()
                hits = sum(1 for w in key if w in low)
                if hits > best_hits:
                    best, best_hits = dname, hits
                    best_ev = any(e in low for e in EVIDENCE)

            # ПОРОГ. Раньше бонус за слово «протокол» давал +2 и сам по
            # себе перекрывал порог: любой протокол «закрывал» любое
            # требование, включая климатические испытания протоколом
            # нагрузки. Для сертификации ложное «закрыто» опаснее
            # пропуска, поэтому теперь:
            #   * бонус за доказательство НЕ засчитывается в порог;
            #   * нужно совпадение минимум половины ключевых слов
            #     и не меньше двух.
            need = max(2, (len(key) + 1) // 2)
            if best and best_hits >= need:
                covered.append((r, best, best_hits))
            elif best and best_hits >= max(1, need - 1) and best_ev:
                # пограничный случай: похоже, но недостаточно —
                # в «закрыто» не относим, помечаем как спорное
                partial.append((r, best, best_hits))
            else:
                uncovered.append(r)

        out = ["ВЕРИФИКАЦИЯ: требования ↔ доказательства",
               f"Источник требований: {req_path}",
               f"Доказательная база: {evidence_path} ({len(ev_docs)} док.)",
               f"Найдено требований: {len(reqs)}", ""]
        out.append(f"ЗАКРЫТО ({len(covered)}):")
        for r, d, s in covered[:40]:
            out.append(f"  + {r[:110]}")
            out.append(f"      → {d} (совпадений: {s})")
        if partial:
            out += ["", f"СПОРНО, ТРЕБУЕТ ЭКСПЕРТА ({len(partial)}):"]
            for r, d, s_ in partial[:40]:
                out.append(f"  ? {r[:110]}")
                out.append(f"      возможно {d} (совпадений: {s_}) — "
                           f"проверить вручную")
        if uncovered:
            out += ["", f"НЕ ЗАКРЫТО ({len(uncovered)}):"]
            out += [f"  − {r[:130]}" for r in uncovered[:40]]
        total = len(covered) + len(partial) + len(uncovered)
        pct = 100 * len(covered) / max(1, total)
        out += ["", f"Уверенно закрыто: {len(covered)} из {total} "
                    f"({pct:.0f}%)"]
        if partial:
            out.append(f"Спорных: {len(partial)} — в покрытие НЕ включены.")
        out += ["", "ВАЖНО: совпадение слов НЕ доказывает соответствие по "
                    "существу. Это карта для эксперта: где искать и что "
                    "проверить вручную. Заключение выносит уполномоченный "
                    "орган."]
        text = "\n".join(out)
        if out_path:
            o = ws.resolve(out_path)
            o.parent.mkdir(parents=True, exist_ok=True)
            o.write_text(text, encoding="utf-8")
            return f"Отчёт сохранён: {ws.relative(o)}\n\nПокрытие: {pct:.0f}%"
        return text

    def cross_check(path: str) -> str:
        """Противоречия в числах между документами."""
        docs = _load([path])
        if len(docs) < 2:
            raise ToolError("для сверки нужно минимум два документа")

        by_param: dict[str, dict[str, list[str]]] = {}
        for name, text in docs.items():
            for k, vals in _num_facts(text).items():
                by_param.setdefault(k, {})[name] = vals

        conflicts, agreed = [], 0
        for param, per_doc in by_param.items():
            if len(per_doc) < 2:
                continue
            allv = {v for vals in per_doc.values() for v in vals}
            if len(allv) > 1:
                conflicts.append((param, per_doc))
            else:
                agreed += 1

        out = [f"ВЕРИФИКАЦИЯ: сверка данных между документами",
               f"Документов: {len(docs)}, общих параметров: "
               f"{sum(1 for p in by_param.values() if len(p) > 1)}", ""]
        if conflicts:
            out.append(f"РАСХОЖДЕНИЯ ({len(conflicts)}):")
            for param, per_doc in conflicts[:25]:
                out.append(f"  ! {param}")
                for d, vals in per_doc.items():
                    out.append(f"      {d}: {', '.join(vals)}")
        else:
            out.append("Расхождений в числовых данных не обнаружено.")
        out += ["", f"Совпадает параметров: {agreed}"]
        out += ["", "Сверяются пары «параметр + число + единица». Различие "
                    "может быть законным (разные условия, допуски) — "
                    "требуется проверка эксперта."]
        return "\n".join(out)

    def audit_report(req_path: str, evidence_path: str,
                     required: str = "", out_path: str = "audit.md") -> str:
        """Сводный отчёт: комплектность, реквизиты, покрытие, расхождения."""
        parts = ["# Отчёт о проверке комплекта документов", ""]
        parts.append("## 1. Комплектность\n")
        if required:
            try:
                parts.append("```\n" + check_completeness(evidence_path,
                                                          required) + "\n```")
            except ToolError as exc:
                parts.append(f"не выполнено: {exc}")
        else:
            parts.append("перечень обязательных документов не задан — "
                         "проверка пропущена")
        parts.append("\n## 2. Реквизиты\n")
        try:
            parts.append("```\n" + check_requisites(evidence_path) + "\n```")
        except ToolError as exc:
            parts.append(f"не выполнено: {exc}")
        parts.append("\n## 3. Покрытие требований\n")
        try:
            parts.append("```\n" + verify_requirements(req_path,
                                                       evidence_path) + "\n```")
        except ToolError as exc:
            parts.append(f"не выполнено: {exc}")
        parts.append("\n## 4. Согласованность данных\n")
        try:
            parts.append("```\n" + cross_check(evidence_path) + "\n```")
        except ToolError as exc:
            parts.append(f"не выполнено: {exc}")
        parts.append("\n---\n")
        parts.append("**Оговорка.** Это результат ФОРМАЛЬНОЙ проверки: "
                     "комплектность, реквизиты, совпадение формулировок, "
                     "согласованность чисел. Он не является заключением о "
                     "соответствии и не заменяет экспертизу. Решение о "
                     "выдаче сертификата принимает уполномоченный орган.")
        text = "\n".join(parts)
        o = ws.resolve(out_path)
        o.parent.mkdir(parents=True, exist_ok=True)
        o.write_text(text, encoding="utf-8")
        return (f"Сводный отчёт: {ws.relative(o)} ({len(text)} символов)\n"
                "Разделы: комплектность, реквизиты, покрытие требований, "
                "согласованность данных.")

    return [
        Tool("check_completeness",
             "ВАЛИДАЦИЯ: все ли обязательные документы есть в комплекте. "
             "Список позиций через запятую или с новой строки.",
             {"type": "object",
              "properties": {"path": {"type": "string"},
                             "required": {"type": "string"}},
              "required": ["path", "required"]}, check_completeness),
        Tool("check_requisites",
             "ВАЛИДАЦИЯ: заполнены ли обязательные реквизиты (номер, дата, "
             "подпись, исполнитель).",
             {"type": "object", "properties": {"path": {"type": "string"}},
              "required": ["path"]}, check_requisites),
        Tool("verify_requirements",
             "ВЕРИФИКАЦИЯ: каждое ли требование закрыто доказательным "
             "документом. Даёт карту «требование → чем закрыто».",
             {"type": "object",
              "properties": {"req_path": {"type": "string"},
                             "evidence_path": {"type": "string"},
                             "out_path": {"type": "string"}},
              "required": ["req_path", "evidence_path"]}, verify_requirements),
        Tool("cross_check",
             "ВЕРИФИКАЦИЯ: расхождения в числовых данных между документами "
             "комплекта.",
             {"type": "object", "properties": {"path": {"type": "string"}},
              "required": ["path"]}, cross_check),
        Tool("audit_report",
             "Сводный отчёт по всем проверкам в один файл Markdown.",
             {"type": "object",
              "properties": {"req_path": {"type": "string"},
                             "evidence_path": {"type": "string"},
                             "required": {"type": "string"},
                             "out_path": {"type": "string"}},
              "required": ["req_path", "evidence_path"]}, audit_report),
    ]
