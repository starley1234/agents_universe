"""Протокол разбора: итоговая статья по завершённой дискуссии.

Зачем отдельно от вердикта. Вердикт арбитра — одна-две фразы: «хватит
SQLite». Через неделю по нему нельзя понять, ПОЧЕМУ так решили, что
проверяли и с чем не согласилась вторая сторона. Протокол сохраняет
ход рассуждения: доводы, проверенные факты, расхождения.

Два способа собрать, и оба нужны:

  БЕЗ МОДЕЛИ (по умолчанию) — сборка из записей: кто что сказал, что
      проверил исполнитель, чем кончилось. Ничего не выдумывает, потому
      что не сочиняет вовсе. Бесплатно и воспроизводимо.

  С МОДЕЛЬЮ (--summarize) — та же структура, но доводы сгруппированы по
      темам и написаны связным текстом. Дороже и требует доверия к
      модели, поэтому не умолчание.

Главное правило: протокол НЕ добавляет выводов, которых не было в
разборе. Если стороны не сошлись — так и написано, с позицией каждой.
Подменять расхождение бодрым заключением нельзя: человек принимает по
этому документу решение.
"""
from __future__ import annotations

import re
import time
from typing import Any

from .debate import ROLE_A, ROLE_ARB, ROLE_B, ROLE_EXEC
from .llm.base import BaseLLM, LLMError
from .store import Store

#: Пометка о происхождении. Ставится всегда: читатель должен знать,
#: что перед ним запись машинного разбора, а не заключение эксперта.
DISCLAIMER = (
    "Протокол составлен автоматически по записи разбора двух моделей. "
    "Это не экспертное заключение: проверенными считаются только факты, "
    "полученные инструментами (раздел «Что проверено»). Остальное — "
    "рассуждения моделей, требующие проверки человеком."
)

SUMMARIZE = """Ты составляешь протокол разбора. Тебе дана стенограмма
спора двух сторон и результаты проверок.

ВОПРОС: {question}

СТЕНОГРАММА:
{transcript}

{facts}

ИТОГ АРБИТРА: {verdict}

Составь связный текст из трёх частей:
1. О чём спор — одним абзацем.
2. Доводы сторон — сгруппируй по темам, а не по репликам. Для каждой
   темы: что говорит A, что возражает B.
3. Чем кончилось — итог и что осталось нерешённым.

ЖЕСТКИЕ ПРАВИЛА:
- Не добавляй доводов, которых не было в стенограмме.
- Не превращай разногласие в согласие. Не сошлись — так и напиши.
- Не ссылайся на факты, которых нет в разделе проверенного.
- Пиши по-русски, деловым языком, без воды. До 400 слов.

Верни только текст протокола, без заголовка и пояснений."""


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _fmt_time(ts: float | None) -> str:
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(ts or 0))


def _facts_from(turns: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Проверенное исполнителем: (что проверяли, что вышло)."""
    out = []
    for t in turns:
        if t["role"] != ROLE_EXEC:
            continue
        text = t["text"] or ""
        m = re.search(r"Проверялось:\s*(.+?)\s*Результат:\s*(.+)", text, re.S)
        if m:
            out.append((_clean(m.group(1))[:200], _clean(m.group(2))[:400]))
        else:
            out.append(("—", _clean(text)[:400]))
    return out


#: Явное возражение. Слово «но» сюда НЕ входит намеренно: «Принимаю,
#: но нужен план» — это согласие с уточнением, а не спор. Раздел «в чём
#: разошлись» с таким содержимым вводит читателя в заблуждение.
OBJECTION = ("возраж", "не согласен", "не согласна", "неверно", "спорно",
             "ошибочно", "это не так", "проблема в том", "против того")

#: Признаки принятия чужого довода: такую реплику в расхождения не берём.
ACCEPTANCE = ("принимаю", "согласен", "согласна", "соглашусь", "ты прав",
              "вы правы", "справедливо", "не спорю")


def _disagreements(turns: list[dict[str, Any]]) -> list[str]:
    """Реплики с явным возражением — из них видно, где разошлись."""
    out = []
    for t in turns:
        if t["role"] not in (ROLE_A, ROLE_B):
            continue
        low = (t["text"] or "").lower()
        if not any(m in low for m in OBJECTION):
            continue
        if any(a in low for a in ACCEPTANCE):
            continue          # «принимаю, но…» — согласие, не спор
        who = "A" if t["role"] == ROLE_A else "B"
        line = f"{who} (круг {t['round']}): {_clean(t['text'])[:220]}"
        if line not in out:   # повтор одного довода не удлиняет раздел
            out.append(line)
    return out


def _hypotheses(turns: list[dict[str, Any]]) -> list[str]:
    """Всё, что стороны сами пометили как непроверенное."""
    out = []
    for t in turns:
        for m in re.finditer(r"\[ГИПОТЕЗА\]\s*(.{0,200})", t["text"] or "",
                             re.I):
            who = "A" if t["role"] == ROLE_A else "B"
            frag = _clean(m.group(1))
            if frag:
                out.append(f"{who}: {frag}")
    return out


def build_protocol(store: Store, debate_id: int,
                   llm: BaseLLM | None = None) -> str:
    """Собрать протокол. llm=None — только по записям, без выдумок."""
    row = store.get_debate(debate_id)
    if not row:
        raise ValueError(f"Разбора #{debate_id} нет")
    turns = store.turns(debate_id)
    sides = [t for t in turns if t["role"] in (ROLE_A, ROLE_B)]
    facts = _facts_from(turns)

    status_word = {
        "done": "согласие достигнуто",
        "no_consensus": "СОГЛАСИЕ НЕ ДОСТИГНУТО",
        "stuck": "СОГЛАСИЕ НЕ ДОСТИГНУТО (стороны топтались на месте)",
        "budget": "разбор прерван по бюджету",
        "stopped": "разбор прерван человеком",
    }.get(row["status"], row["status"])

    L: list[str] = []
    L.append(f"# Протокол разбора №{debate_id}")
    L.append("")
    L.append(f"**Вопрос:** {row['question']}")
    L.append("")
    L.append(f"**Итог:** {status_word}")
    L.append("")

    # ---- шапка с фактами о самом разборе
    dur = (row.get("finished") or 0) - (row.get("started") or 0)
    L.append("| | |")
    L.append("|---|---|")
    L.append(f"| Дата | {_fmt_time(row.get('started'))} |")
    L.append(f"| Сторона A | {row['model_a'] or '—'} |")
    L.append(f"| Сторона B | {row['model_b'] or '—'} |")
    L.append(f"| Арбитр | {row['model_arbiter'] or '—'} |")
    L.append(f"| Кругов | {row['rounds']} |")
    L.append(f"| Реплик | {len(sides)} |")
    L.append(f"| Проверок инструментами | {len(facts)} |")
    if dur > 0:
        L.append(f"| Длительность | {dur / 60:.1f} мин |")
    tok = int(row.get("tok_out") or 0)
    if tok:
        cost = float(row.get("cost") or 0)
        L.append(f"| Расход | {tok:,} токенов"
                 + (f", ${cost:.4f}" if cost > 0 else ", локальные модели")
                 + " |")
    L.append("")

    # ---- вердикт: главное, ради чего читают
    L.append("## Заключение")
    L.append("")
    L.append(row["verdict"] or "_(итог не сформулирован)_")
    L.append("")

    # ---- проверенное: единственное, чему можно верить без оговорок
    L.append("## Что проверено инструментами")
    L.append("")
    if facts:
        L.append("Только эти данные получены проверкой, а не рассуждением.")
        L.append("")
        for what, got in facts:
            L.append(f"- **{what}** → {got}")
    else:
        L.append("_Ничего не проверялось: разбор шёл только рассуждениями. "
                 "Все утверждения ниже требуют проверки._")
    L.append("")

    # ---- связный текст либо стенограмма
    if llm is not None:
        text = _summarize(llm, row, turns, facts)
        if text:
            L.append("## Ход разбора")
            L.append("")
            L.append(text)
            L.append("")

    L.append("## Позиции сторон")
    L.append("")
    for role, label in ((ROLE_A, "Сторона A"), (ROLE_B, "Сторона B")):
        mine = [t for t in turns if t["role"] == role]
        model = row["model_a"] if role == ROLE_A else row["model_b"]
        L.append(f"### {label} ({model or '—'})")
        L.append("")
        if not mine:
            L.append("_не высказывалась_")
        else:
            L.append(f"**Итоговая позиция:** {_clean(mine[-1]['text'])[:500]}")
            L.append("")
            L.append("Доводы по кругам:")
            for t in mine:
                L.append(f"{t['round']}. {_clean(t['text'])[:300]}")
        L.append("")

    dis = _disagreements(turns)
    if dis:
        L.append("## В чём разошлись")
        L.append("")
        for d in dis[:10]:
            L.append(f"- {d}")
        L.append("")

    hyp = _hypotheses(turns)
    if hyp:
        L.append("## Непроверенные допущения")
        L.append("")
        L.append("Стороны сами пометили это как гипотезы — проверять "
                 "человеку.")
        L.append("")
        for h in hyp[:10]:
            L.append(f"- {h}")
        L.append("")

    arb = [t for t in turns if t["role"] == ROLE_ARB]
    if arb:
        L.append("## Решения арбитра")
        L.append("")
        for t in arb:
            L.append(f"- круг {t['round']}: {_clean(t['text'])[:200]}")
        L.append("")

    L.append("---")
    L.append("")
    L.append(f"_{DISCLAIMER}_")
    return "\n".join(L)


def _summarize(llm: BaseLLM, row: dict[str, Any],
               turns: list[dict[str, Any]],
               facts: list[tuple[str, str]]) -> str:
    """Связный пересказ моделью. Сбой не ломает протокол."""
    lines = []
    for t in turns:
        if t["role"] not in (ROLE_A, ROLE_B):
            continue
        who = "A" if t["role"] == ROLE_A else "B"
        lines.append(f"[круг {t['round']}] {who}: {_clean(t['text'])[:400]}")
    transcript = "\n".join(lines)[:12000]
    fact_block = ("ПРОВЕРЕНО ИНСТРУМЕНТАМИ:\n"
                  + "\n".join(f"- {w} → {g}" for w, g in facts)
                  if facts else "ПРОВЕРОК ИНСТРУМЕНТАМИ НЕ БЫЛО.")
    prompt = SUMMARIZE.format(
        question=row["question"], transcript=transcript,
        facts=fact_block, verdict=row["verdict"] or "(нет)")
    try:
        reply = llm.chat([{"role": "user", "content": prompt}])
    except LLMError:
        # Пересказ — украшение, а не суть. Не получилось — протокол
        # соберётся из записей, и это честнее выдуманного текста.
        return ""
    return (reply.text or "").strip()[:6000]
