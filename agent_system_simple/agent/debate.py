"""Дискуссия двух моделей: круг доводов, арбитр, исполнитель.

Задание целиком — в docs/DEBATE.md. Здесь реализация первых двух
этапов: ядро с пределами и свёрткой, арбитр с триггерами и защитой от
бесконечного спора.

Главное разделение, на котором всё держится:

    КОГДА звать арбитра решают ПРАВИЛА (дёшево, воспроизводимо,
    проверяется тестами), а ЧТО делать решает МОДЕЛЬ (суждение
    правилами не заменить).

Отдай модели и то и другое — дискуссия не кончится никогда, потому что
«ещё один круг» всегда выглядит разумно.

Стороны намеренно БЕЗ инструментов: их дело рассуждать. Всё, что надо
проверить, уходит исполнителю — обычному Agent из core.py. Только его
результат считается фактом.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .llm.base import BaseLLM, LLMError, price_of
from .store import Store

# ─────────────────────────── пределы ────────────────────────────────
#: Кругов по умолчанию. Больше — контекст растёт квадратично, а новых
#: доводов уже нет: проверено на длинных прогонах автономного режима.
DEFAULT_ROUNDS = 12

#: Через сколько кругов звать арбитра, даже если ничего не случилось.
ARBITER_EVERY = 3

#: Сколько раз подряд арбитр может сказать «продолжить». Без предела
#: арбитр-соглашатель растянет спор до упора в деньги.
MAX_CONTINUE = 3

#: Повтор довода: столько одинаковых подписей — топтание.
REPEAT_LIMIT = 2

#: Столько кругов топтания подряд — остановка.
STUCK_ROUNDS = 3

#: Реплик в контекст целиком. Остальное — однострочными итогами.
KEEP_TURNS = 4

#: Длина реплики. Просим коротко: длинные доводы не читают ни модели,
#: ни человек, а платим за каждый токен.
MAX_TURN_CHARS = 1200

#: Нечитаемых ответов арбитра подряд, после которых он признан негодным.
MAX_BAD_ARBITER = 3

#: Служебные слова: в подпись довода не идут.
STOP_WORDS = {
    "это", "как", "что", "для", "при", "или", "если", "уже", "ещё", "еще",
    "так", "там", "тут", "все", "всё", "они", "оно", "она", "чем", "над",
    "под", "без", "меня", "нас", "вас", "его", "их", "мы", "вы", "но", "а",
    "и", "в", "на", "с", "к", "по", "не", "же", "бы", "ли", "то", "из",
    "до", "за", "от", "об", "про", "быть", "есть", "может", "можно",
    "нужно", "надо", "будет", "думаю", "считаю", "согласен",
}

ROLE_A, ROLE_B, ROLE_ARB, ROLE_EXEC = "a", "b", "arbiter", "executor"

#: Позиции сторон по умолчанию. Разные критерии оценки — иначе
#: получится монолог в двух лицах.
STANCE_A = ("Ты за простое решение: меньше частей, меньше зависимостей, "
            "меньше того, что может сломаться.")
STANCE_B = ("Ты за запас прочности: система должна выдержать рост "
            "нагрузки и нештатные случаи.")

# ─────────────────────────── промпты ────────────────────────────────
SIDE = """Ты участник разбора вопроса. Твоя сторона: {stance}

ВОПРОС: {question}
{assumption}
{history}
{last}
{facts}
{nudge}

Как отвечать:
- Коротко: до 5 предложений. Один довод за ход, самый сильный.
- Возражай по существу предыдущей реплики, а не пересказывай своё.
- Не повторяй уже сказанное другими словами — это видно и засчитывается
  как топтание.
- Проверенным считается ТОЛЬКО то, что подтвердил исполнитель. Своё
  непроверенное помечай [ГИПОТЕЗА].
- Нужен факт, который можно проверить инструментами (прочитать файл,
  запустить код, замерить) — напиши [ФАКТ?] и что именно проверить.
- Если оппонент прав — скажи прямо. Соглашаться ради вежливости нельзя,
  но и спорить ради спора тоже.

Ответь только своей репликой, без пояснений и заголовков."""

FIRST_A = """Начни разбор. Сформулируй свою позицию и главный довод.
Обязательно назови хотя бы одно СЛАБОЕ место своей позиции — честный
разбор начинается с этого."""

FIRST_B = """Ответь на позицию оппонента. Обязательно назови хотя бы
одно конкретное возражение: согласиться сразу нельзя, разбор для того
и нужен, чтобы найти слабые места."""

ARBITER = """Ты арбитр разбора. Ты НЕ участвуешь в споре и НЕ добавляешь
своих доводов. Твоё дело — решить, что делать дальше.

ВОПРОС: {question}

{history}

ПОСЛЕДНИЕ РЕПЛИКИ:
{last}

{facts}

Причина, по которой тебя позвали: {reason}
Кругов пройдено: {rounds} из {max_rounds}
{limits}

Ответь ТОЛЬКО валидным JSON, без пояснений до и после:
{{"решение": "продолжить|исполнитель|завершить",
  "почему": "одной фразой",
  "вопрос": "что проверить инструментами — только для «исполнитель»",
  "итог": "ответ на исходный вопрос — только для «завершить»"}}

Когда что выбирать:
- «завершить» — стороны пришли к согласию ЛИБО расхождение ясно и
  дальше спорить бессмысленно. В «итог» напиши ответ на исходный
  вопрос. Если согласия нет — так и напиши, с позицией каждой стороны.
- «исполнитель» — спор упёрся в факт, который можно проверить:
  прочитать файл, запустить код, замерить. В «вопрос» — что проверить.
- «продолжить» — доводы ещё не исчерпаны.

Если обе позиции верны при РАЗНЫХ условиях — это не повод тянуть:
выбирай «завершить» и в «итоге» назови оба условия и что делать в
каждом. Разбор ведётся одной веткой."""


# ─────────────────────── вспомогательное ────────────────────────────
def argument_sig(text: str) -> str:
    """Подпись довода: ловит повтор той же мысли другими словами.

    Слова приводятся к основам, служебные отбрасываются, остаток
    сортируется. «ресурсов не хватит» и «не хватит ресурсов» дают одну
    подпись; «ресурсов хватит» — другую, потому что «не» здесь значимо.

    Отрицания намеренно НЕ выбрасываем: без них противоположные
    утверждения слились бы в одно, и согласие выглядело бы как спор.

    ГРАНИЦА ЧЕСТНО: синонимы подпись не ловит. «Справится с нагрузкой» и
    «нагрузка по силам» — разные подписи, хотя мысль одна. Для этого
    нужны эмбеддинги, а они не всегда настроены. Такое топтание ловит
    второй рубеж — арбитр, которого зовут каждые N кругов.
    """
    words = re.findall(r"\w+", (text or "").lower(), flags=re.UNICODE)
    keep = []
    for w in words:
        if w in ("не", "нет", "без"):        # значимое отрицание
            keep.append("!")
            continue
        if w.isdigit():
            # Числа берём целиком: «нужно 3 сервера» и «нужно 30» — разные
            # утверждения. Через основу слова они бы слились, и подпись
            # объявила бы топтанием нормальный спор о величине.
            keep.append(w)
            continue
        if w in STOP_WORDS or len(w) < 3:
            continue
        keep.append(Store._stem(w))
    if not keep:
        return ""
    core = " ".join(sorted(set(keep)))
    return hashlib.sha1(core.encode()).hexdigest()[:12]


def _agrees(text: str) -> bool:
    """Согласие без нового довода: признак поддакивания."""
    low = (text or "").lower()
    yes = any(w in low for w in (
        "согласен", "согласна", "соглашусь", "верно", "ты прав",
        "вы правы", "принимаю", "не спорю", "справедливо"))
    objects = any(w in low for w in (
        "но ", "однако", "возраж", "не согласен", "при этом", "хотя",
        "с другой стороны", "проблема в"))
    return yes and not objects


def _wants_fact(text: str) -> str:
    """Запрошен ли факт. Возвращает, что проверить, или пустую строку."""
    m = re.search(r"\[ФАКТ\?\]\s*(.{0,300})", text or "", re.S | re.I)
    return m.group(1).strip() if m else ""


@dataclass
class Turn:
    role: str
    text: str
    round: int = 0
    model: str = ""
    sig: str = ""
    tokens: int = 0
    cost: float = 0.0


@dataclass
class DebateResult:
    debate_id: int
    status: str                 # done|no_consensus|budget|stuck|error
    verdict: str
    rounds: int
    turns: list[Turn]
    cost: float = 0.0
    tokens: int = 0
    seconds: float = 0.0

    def summary(self) -> str:
        head = {
            "done": "Разбор завершён",
            "no_consensus": "Согласия не достигнуто (исчерпаны круги)",
            "budget": "Остановлено по бюджету",
            "stuck": "Остановлено: стороны топчутся на месте",
            "error": "Прервано ошибкой",
        }.get(self.status, self.status)
        lines = [f"{head}. Кругов: {self.rounds}, реплик: {len(self.turns)}"]
        if self.tokens:
            lines.append(f"Токенов: {self.tokens:,}"
                         + (f", ${self.cost:.4f}" if self.cost > 0
                            else " (локальные модели)"))
        lines.append("")
        lines.append(self.verdict or "(итог не сформулирован)")
        return "\n".join(lines)


class Debate:
    """Круг доводов двух моделей под присмотром арбитра."""

    def __init__(
        self,
        side_a: BaseLLM,
        side_b: BaseLLM,
        arbiter: BaseLLM,
        store: Store,
        stance_a: str = STANCE_A,
        stance_b: str = STANCE_B,
        rounds: int = DEFAULT_ROUNDS,
        arbiter_every: int = ARBITER_EVERY,
        max_usd: float = 0.0,
        max_minutes: float = 30.0,
        executor: Callable[[str], str] | None = None,
        max_executor_calls: int = 5,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.a, self.b, self.arb = side_a, side_b, arbiter
        self.store = store
        self.stance_a, self.stance_b = stance_a, stance_b
        self.max_rounds = max(1, rounds)
        self.arbiter_every = max(1, arbiter_every)
        self.max_usd = max_usd
        self.max_seconds = max_minutes * 60
        self.executor = executor
        self.max_executor_calls = max_executor_calls
        self.on_event = on_event or (lambda k, d: None)

        self.debate_id = 0
        self.turns: list[Turn] = []
        self.facts: list[str] = []          # подтверждённое исполнителем
        self.cost = 0.0
        self.tokens = 0
        self._continues = 0                 # «продолжить» подряд
        self._bad_arbiter = 0
        self._stuck = 0
        self._exec_calls = 0

    # ------------------------------------------------------------ ход
    def _emit(self, kind: str, **data: Any) -> None:
        try:
            self.on_event(kind, data)
        except Exception:                   # наблюдатель не ломает разбор
            pass

    def _spend(self, llm: BaseLLM, prompt: int, completion: int) -> float:
        price = price_of(llm.model) if llm.billable else None
        c = ((prompt * price[0] + completion * price[1]) / 1e6
             if price else 0.0)
        self.cost += c
        self.tokens += prompt + completion
        return c

    def _history(self, upto: int = -KEEP_TURNS) -> str:
        """Свёртка: старое одной строкой, свежее целиком.

        Без этого 12 кругов дают около 120 тысяч токенов только на вход:
        каждая сторона каждый раз получает всю переписку.
        """
        old = self.turns[:upto] if upto else self.turns
        if not old:
            return ""
        lines = []
        for t in old:
            who = {"a": "A", "b": "B", ROLE_ARB: "Арбитр",
                   ROLE_EXEC: "Проверено"}.get(t.role, t.role)
            head = re.sub(r"\s+", " ", t.text).strip()[:110]
            lines.append(f"  круг {t.round} {who}: {head}")
        return "Коротко о пройденном:\n" + "\n".join(lines)

    def _last(self, n: int = KEEP_TURNS) -> str:
        tail = self.turns[-n:] if n else []
        if not tail:
            return ""
        out = []
        for t in tail:
            who = {"a": "Сторона A", "b": "Сторона B", ROLE_ARB: "Арбитр",
                   ROLE_EXEC: "ПРОВЕРЕНО ИСПОЛНИТЕЛЕМ"}.get(t.role, t.role)
            out.append(f"{who}:\n{t.text}")
        return "\n\n".join(out)

    def _facts_block(self) -> str:
        if not self.facts:
            return ""
        return ("ПРОВЕРЕННЫЕ ФАКТЫ (только они считаются достоверными):\n"
                + "\n".join(f"- {f}" for f in self.facts))

    def _ask_side(self, role: str, question: str, assumption: str,
                  nudge: str) -> Turn:
        llm = self.a if role == ROLE_A else self.b
        stance = self.stance_a if role == ROLE_A else self.stance_b
        prompt = SIDE.format(
            stance=stance, question=question,
            assumption=(f"ПРЕДПОЛОЖЕНИЕ ВЕТКИ: {assumption}"
                        if assumption else ""),
            history=self._history(), last=(
                "ПОСЛЕДНИЕ РЕПЛИКИ:\n" + self._last() if self.turns else ""),
            facts=self._facts_block(), nudge=nudge)
        reply = llm.chat([{"role": "user", "content": prompt}])
        text = (reply.text or "").strip()[:MAX_TURN_CHARS]
        if not text:
            # Пустая реплика — не согласие и не довод. Отмечаем прямо:
            # молчание не должно выглядеть как участие.
            text = "(модель не дала ответа)"
        cost = self._spend(llm, reply.usage.prompt, reply.usage.completion)
        return Turn(role, text, model=llm.model, sig=argument_sig(text),
                    tokens=reply.usage.total, cost=cost)

    def _save(self, t: Turn, round_no: int,
              branch_id: int | None = None) -> None:
        t.round = round_no
        self.turns.append(t)
        self.store.add_turn(self.debate_id, t.role, t.text, round_no,
                            t.model, t.sig, t.tokens, t.cost, branch_id)
        self._emit("turn", role=t.role, text=t.text, round=round_no,
                   model=t.model)

    # -------------------------------------------------------- арбитр
    def _need_arbiter(self, round_no: int) -> str:
        """Правило, по которому зовём арбитра. Пусто — не зовём."""
        last_two = [t for t in self.turns if t.role in (ROLE_A, ROLE_B)][-2:]

        for t in last_two:
            if _wants_fact(t.text):
                return "сторона запросила проверку факта"

        if len(last_two) == 2:
            repeats = max(
                self.store.sig_repeats(self.debate_id, t.sig)
                for t in last_two if t.sig)
            if repeats > REPEAT_LIMIT:
                return "стороны повторяют одни и те же доводы"
            if all(_agrees(t.text) for t in last_two) and round_no >= 2:
                return "обе стороны согласны"

        if self.max_usd > 0 and self.cost >= self.max_usd * 0.8:
            return "бюджет на исходе"
        if round_no % self.arbiter_every == 0:
            return f"пройдено {round_no} кругов"
        return ""

    def _ask_arbiter(self, question: str, reason: str,
                     round_no: int) -> dict[str, Any]:
        limits = ""
        if self._continues >= MAX_CONTINUE:
            limits = ("ВАЖНО: вариант «продолжить» больше НЕДОСТУПЕН — "
                      "он использован подряд максимальное число раз. "
                      "Выбери один из трёх остальных.")
        prompt = ARBITER.format(
            question=question, history=self._history(0),
            last=self._last(6), facts=self._facts_block(), reason=reason,
            rounds=round_no, max_rounds=self.max_rounds, limits=limits)
        try:
            reply = self.arb.chat([{"role": "user", "content": prompt}])
        except LLMError as exc:
            self._bad_arbiter += 1
            return {"решение": "продолжить", "почему": f"арбитр недоступен: {exc}"}
        self._spend(self.arb, reply.usage.prompt, reply.usage.completion)

        data = _parse_json(reply.text or "")
        if data is None:
            # Защитный разбор как в autorun._reflect: не падаем, но и не
            # делаем вид, что всё в порядке.
            self._bad_arbiter += 1
            self._emit("arbiter_unparsed", text=(reply.text or "")[:200])
            return {"решение": "продолжить", "почему": "ответ арбитра не разобран"}
        self._bad_arbiter = 0

        choice = str(data.get("решение") or data.get("decision") or "").lower()
        for key in ("продолжить", "исполнитель", "завершить"):
            if key in choice:
                choice = key
                break
        else:
            choice = "продолжить"
        data["решение"] = choice
        return data

    # ----------------------------------------------------------- цикл
    def run(self, question: str, assumption: str = "",
            branch_id: int | None = None, resume: int = 0) -> DebateResult:
        t0 = time.time()
        if resume:
            self.debate_id = resume
            row = self.store.get_debate(resume)
            if not row:
                raise ValueError(f"Дискуссии #{resume} нет")
            question = row["question"]
            for r in self.store.turns(resume):
                self.turns.append(Turn(r["role"], r["text"], r["round"],
                                       r["model"] or "", r["sig"] or "",
                                       r["tokens"], r["cost"]))
                if r["role"] == ROLE_EXEC:
                    self.facts.append(r["text"])
            self.cost = float(row.get("cost") or 0)
            self._emit("resume", debate_id=resume, question=question)
        else:
            self.debate_id = self.store.start_debate(
                question, self.a.model, self.b.model, self.arb.model)
            self._emit("start", debate_id=self.debate_id, question=question,
                       model_a=self.a.model, model_b=self.b.model,
                       model_arbiter=self.arb.model)
            if self.a.model == self.b.model and self.stance_a == self.stance_b:
                # Монолог в двух лицах: предупреждаем, но не отказываем —
                # человек мог сделать это намеренно.
                self._emit("warn", message=(
                    "стороны одинаковы (та же модель и та же роль) — "
                    "разбора не выйдет, будет монолог в двух лицах"))

        status, verdict = "no_consensus", ""
        start_round = max((t.round for t in self.turns), default=0)

        for rnd in range(start_round + 1, self.max_rounds + 1):
            if time.time() - t0 > self.max_seconds:
                status = "budget"
                verdict = self._positions("Вышло отведённое время.")
                break
            if self.max_usd > 0 and self.cost >= self.max_usd:
                status = "budget"
                verdict = self._positions(
                    f"Исчерпан бюджет ${self.max_usd:.2f}.")
                break

            self._emit("round", n=rnd, of=self.max_rounds)

            first = not any(t.role == ROLE_A for t in self.turns)
            ta = self._ask_side(ROLE_A, question, assumption,
                                FIRST_A if first else "")
            self._save(ta, rnd, branch_id)

            firstb = not any(t.role == ROLE_B for t in self.turns)
            tb = self._ask_side(ROLE_B, question, assumption,
                                FIRST_B if firstb else "")
            self._save(tb, rnd, branch_id)

            # Топтание: обе стороны повторились — считаем круг пустым.
            reps = [self.store.sig_repeats(self.debate_id, t.sig)
                    for t in (ta, tb) if t.sig]
            self._stuck = self._stuck + 1 if reps and min(reps) > REPEAT_LIMIT \
                else 0
            if self._stuck >= STUCK_ROUNDS:
                status = "stuck"
                verdict = self._positions(
                    "Стороны повторяют одни и те же доводы.")
                break

            reason = self._need_arbiter(rnd)
            if not reason:
                continue

            data = self._ask_arbiter(question, reason, rnd)
            choice = data["решение"]
            why = str(data.get("почему") or "")[:300]
            self._emit("arbiter", decision=choice, reason=why, round=rnd)
            self._save(Turn(ROLE_ARB, f"[{choice}] {why}",
                            model=self.arb.model), rnd, branch_id)

            if self._bad_arbiter >= MAX_BAD_ARBITER:
                status = "no_consensus"
                verdict = self._positions(
                    "Арбитр не отвечает по протоколу — разбор остановлен.")
                break

            if choice == "завершить":
                status = "done"
                verdict = str(data.get("итог") or "").strip() \
                    or self._positions("Арбитр не сформулировал итог.")
                break

            if choice == "исполнитель":
                self._run_executor(data, rnd, branch_id)
                self._continues = 0
                continue

            self._continues += 1
            if self._continues > MAX_CONTINUE:
                # Арбитр тянет: решаем за него, но честно об этом говорим.
                status = "no_consensus"
                verdict = self._positions(
                    f"Арбитр {MAX_CONTINUE} раза подряд предлагал "
                    "продолжать, не приходя к решению.")
                break
        else:
            verdict = self._positions("Исчерпаны отведённые круги.")

        rounds_done = max((t.round for t in self.turns), default=0)
        self.store.finish_debate(self.debate_id, status, verdict)
        res = DebateResult(self.debate_id, status, verdict, rounds_done,
                           self.turns, self.cost, self.tokens,
                           time.time() - t0)
        self._emit("finish", status=status, verdict=verdict,
                   rounds=rounds_done, cost=self.cost)
        return res

    # ------------------------------------------------------ помощники
    def _run_executor(self, data: dict[str, Any], rnd: int,
                      branch_id: int | None) -> None:
        task = str(data.get("вопрос") or data.get("question") or "").strip()
        if not task:
            self._emit("warn", message="арбитр не сказал, что проверять")
            return
        if self.executor is None:
            self._save(Turn(ROLE_EXEC,
                            f"Проверить «{task[:120]}» не удалось: "
                            "исполнитель не подключён.",
                            model="—"), rnd, branch_id)
            return
        if self._exec_calls >= self.max_executor_calls:
            self._save(Turn(ROLE_EXEC,
                            f"Проверка «{task[:80]}» не выполнена: "
                            f"исчерпан предел вызовов исполнителя "
                            f"({self.max_executor_calls}).",
                            model="—"), rnd, branch_id)
            return
        self._exec_calls += 1
        self._emit("executor", task=task, n=self._exec_calls)
        try:
            answer = self.executor(task)
        except Exception as exc:            # исполнитель не роняет разбор
            answer = f"проверка не удалась: {type(exc).__name__}: {exc}"
        text = f"Проверялось: {task}\nРезультат: {answer}".strip()[:2000]
        self.facts.append(text)
        self._save(Turn(ROLE_EXEC, text, model="исполнитель"), rnd, branch_id)

    def _positions(self, why: str) -> str:
        """Итог, когда согласия нет. Отсутствие согласия — законный
        результат: подменять его мнением последнего говорившего нельзя."""
        def last_of(role: str) -> str:
            for t in reversed(self.turns):
                if t.role == role:
                    return re.sub(r"\s+", " ", t.text).strip()[:400]
            return "(не высказывалась)"

        parts = [f"СОГЛАСИЕ НЕ ДОСТИГНУТО. {why}", "",
                 f"Позиция A ({self.a.model}):", last_of(ROLE_A), "",
                 f"Позиция B ({self.b.model}):", last_of(ROLE_B)]
        if self.facts:
            parts += ["", "Проверенное исполнителем:"]
            parts += [f"- {f.splitlines()[-1][:200]}" for f in self.facts]
        return "\n".join(parts)


def _parse_json(text: str) -> dict[str, Any] | None:
    """Достать JSON из ответа, даже если модель обернула его в текст."""
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        data = json.loads(text[i:j + 1])
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None
