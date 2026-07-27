"""Автономный режим: агент работает часами сам.

Что отличает долгий прогон от одиночного запуска — и почему без этого
агент через час деградирует:

  1. ЦЕЛЬ ЗАБЫВАЕТСЯ. История обрезается, и в окне остаются последние
     вызовы инструментов. Решение: цель и план подставляются в КАЖДУЮ
     итерацию заново, из базы, а не из истории.

  2. ЗАЦИКЛИВАНИЕ. Модель повторяет один и тот же вызов бесконечно.
     Решение: подпись действия (имя + аргументы). Повтор сверх порога —
     принудительное вмешательство в промпт.

  3. КОНТЕКСТ КОНЧАЕТСЯ. Каждая итерация — свежий короткий контекст:
     цель, план, выжимка из памяти, последние события. История одной
     итерации не тащится в следующую.

  4. НЕТ ОБУЧЕНИЯ. После каждой итерации — рефлексия: что узнал,
     записать в память, что дальше. Иначе агент ходит по кругу.

Цикл: план → взять пункт → работать → рефлексия → повторить.
Останов: план выполнен, вышло время, исчерпаны итерации или застой.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from .core import Agent
from .dispatch import choose_profile
from .llm.base import price_of
from .store import Store

PLANNER = """Ты планировщик. Задача:

{goal}

{known}

Разбей её на 3-7 конкретных проверяемых пунктов. Каждый пункт —
одна строка, начинается с глагола, результат должен быть проверяем.
Не пиши преамбулу и нумерацию, только строки пунктов."""

DECOMPOSE = """Ты планировщик. Разбей задачу на пункты и назначь
исполнителей.

ЗАДАЧА: {goal}

{known}

ДОСТУПНЫЕ ИСПОЛНИТЕЛИ:
{profiles}

Ответь ТОЛЬКО валидным JSON без пояснений:
{{"шаги": [
  {{"что": "действие с проверяемым результатом",
    "кто": "имя исполнителя из списка",
    "после": [номера шагов, без которых этот не сделать],
    "проверка": "чем подтвердится, что шаг выполнен"}}
]}}

Правила:
- 3-7 шагов. Меньше трёх — задача не разбита, больше семи — дробление
  ради дробления.
- «что» начинается с глагола и даёт ПРОВЕРЯЕМЫЙ результат: не «изучить
  вопрос», а «замерить время поиска на 100 тысячах записей».
- «после» — номера шагов в ЭТОМ списке (нумерация с 1). Ставь только
  настоящие зависимости: шаг не сделать, пока не готов другой. Лишние
  зависимости растягивают работу без нужды.
- Колец быть не должно: шаг не может зависеть сам от себя ни прямо,
  ни через цепочку.
- «кто» — ровно одно имя из списка выше. Не подходит никто — пиши
  пустую строку, исполнителя подберут автоматически.
- «проверка» — как убедиться, что сделано: какой файл появится, какое
  число получится, что покажет тест."""

WORKER = """ЦЕЛЬ ПРОГОНА: {goal}

ТЕКУЩИЙ ПУНКТ ПЛАНА: #{task_id} {task}

{memory}
{recent}
{warning}

Работай над ТЕКУЩИМ ПУНКТОМ. Когда он выполнен — вызови plan_done с
кратким результатом. Если пункт невыполним — plan_fail с причиной.
Важные выводы записывай через remember, объекты и связи — через
note_entity и link.

Не пересказывай план, действуй."""

REPLAN = """Цель: {goal}

Уже сделано:
{done}

Не получилось:
{failed}

Осталось в плане:
{left}

План работает плохо: {reason}

Составь НОВЫЙ план оставшейся работы — 2-6 пунктов, каждый с новой
строки, начинается с глагола, результат проверяем. Учти, что уже
сделано, и не повторяй проваленное тем же способом. Только строки
пунктов, без преамбулы."""

REFLECT = """Итерация завершена. Пункт: {task}

Что сделано: {summary}

Ответь ТОЛЬКО валидным JSON без пояснений:
{{"learned": ["факт 1", "факт 2"], "next": "что делать дальше одной фразой", "stuck": false}}

learned — только новое и конкретное, что стоит помнить. Пустой список, если нового нет.
stuck — true, если прогресса нет и нужно менять подход."""


def _json_block(text: str) -> dict[str, Any] | None:
    """Достать JSON из ответа, даже если модель обернула его в текст.

    Тот же защитный разбор, что в рефлексии и у арбитра: модели любят
    добавить «Вот план:» перед объектом и пояснение после.
    """
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        data = json.loads(text[i:j + 1])
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


@dataclass
class AutoResult:
    run_id: int
    iterations: int
    stopped_by: str          # done | time | iterations | stuck | error
    summary: str
    elapsed: float
    tokens: int = 0
    cost: float = 0.0


class AutoRunner:
    def __init__(
        self,
        agent_factory: Callable[[], Agent],
        store: Store,
        max_hours: float = 1.0,
        max_iterations: int = 50,
        repeat_limit: int = 3,
        replan_after_fails: int = 2,
        max_usd: float = 0.0,
        route_tasks: bool = False,
        known_profiles: list[str] | None = None,
        decompose: bool = False,
        profile_hints: dict[str, str] | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.make_agent = agent_factory
        self.store = store
        self.max_seconds = max_hours * 3600
        self.max_iterations = max_iterations
        # Предел расхода за прогон. 0 = не ограничивать: на локальной
        # модели платить не за что, и мешать там незачем.
        self.max_usd = max_usd
        self.repeat_limit = repeat_limit
        self.replan_after_fails = replan_after_fails
        self.on_event = on_event or (lambda k, d: None)
        self.run_id = 0
        # Передача между агентами: каждый пункт плана своему профилю.
        self.route_tasks = route_tasks
        self.known_profiles = known_profiles or []
        self.last_profile: str | None = None
        # Декомпозиция: план с исполнителями и зависимостями вместо
        # плоского списка строк.
        self.decompose = decompose
        self.profile_hints = profile_hints or {}

    def _agent_for(self, profile: str | None) -> Agent:
        """Агент под профиль. Фабрика может его не принимать — тогда
        работаем по-старому, одним набором навыков."""
        if profile is None:
            return self.make_agent()
        try:
            return self.make_agent(profile)          # type: ignore[call-arg]
        except TypeError:
            return self.make_agent()

    def _emit(self, kind: str, **data: Any) -> None:
        try:
            self.on_event(kind, data)
        except Exception:
            pass

    # ------------------------------------------------------------ ход
    @staticmethod
    def _sig(name: str, args: dict[str, Any]) -> str:
        raw = name + json.dumps(args, sort_keys=True, ensure_ascii=False)[:300]
        return hashlib.sha1(raw.encode()).hexdigest()[:12]

    def _plan(self, goal: str) -> None:
        known = ""
        facts = self.store.recall(goal, limit=5)
        if facts:
            known = "Уже известно из прошлых прогонов:\n" + \
                    "\n".join(f"- {f['text']}" for f in facts)

        if self.decompose:
            if self._plan_structured(goal, known):
                return
            # Разбор не удался — не выдумываем структуру, работаем
            # плоским списком. Молча получить план из одного пункта
            # хуже, чем честно откатиться к простому планированию.
            self._emit("warn", message=(
                "структурный план не разобран — планирую списком"))

        agent = self.make_agent()
        res = agent.run(PLANNER.format(goal=goal, known=known))
        titles = [ln.strip(" -•*0123456789.\t")
                  for ln in res.answer.splitlines() if ln.strip()]
        titles = [t for t in titles if len(t) > 8][:7]
        if not titles:
            titles = [goal]
        self.store.add_tasks(self.run_id, titles)
        self._emit("plan", items=titles)

    def _plan_structured(self, goal: str, known: str) -> bool:
        """План с исполнителями и зависимостями. False — не получилось."""
        profiles = "\n".join(
            f"- {n}: {d}" for n, d in (self.profile_hints or {}).items()
        ) or "- (список не задан, оставляй «кто» пустым)"
        agent = self.make_agent()
        res = agent.run(DECOMPOSE.format(goal=goal, known=known,
                                         profiles=profiles))
        data = _json_block(res.answer)
        if not data:
            return False
        raw = data.get("шаги") or data.get("steps") or []
        if not isinstance(raw, list) or len(raw) < 2:
            return False

        steps: list[dict[str, Any]] = []
        for item in raw[:7]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("что") or item.get("title") or "").strip()
            if len(title) < 8:
                continue
            who = str(item.get("кто") or item.get("profile") or "").strip()
            if who and self.known_profiles and who not in self.known_profiles:
                who = ""            # придуманный исполнитель — не исполнитель
            needs = item.get("после") or item.get("needs") or []
            needs = [n for n in needs if isinstance(n, int)] \
                if isinstance(needs, list) else []
            steps.append({"title": title, "profile": who, "needs": needs,
                          "check": str(item.get("проверка")
                                       or item.get("check") or "")})
        if len(steps) < 2:
            return False

        ids = self.store.add_steps(self.run_id, steps)
        # Кольцо в зависимостях сделало бы план невыполнимым целиком.
        # Лучше снять зависимости и работать по порядку, чем встать.
        if self.store.deadlocked(self.run_id) and \
                not self.store.next_ready_task(self.run_id):
            for tid in ids:
                self.store.db.execute("UPDATE task SET needs='' WHERE id=?",
                                      (tid,))
            self.store.db.commit()
            self._emit("warn", message=(
                "в зависимостях кольцо — сняты, работаем по порядку"))

        rows = self.store.tasks(self.run_id)
        self._emit("plan", items=[t["title"] for t in rows],
                   steps=[{"id": t["id"], "title": t["title"],
                           "profile": t["profile"], "needs": t["needs"],
                           "check": t["check_hint"]} for t in rows])
        return True

    def _replan(self, reason: str) -> bool:
        """Переделать план оставшейся работы.

        Нужно, когда исходный план оказался негодным: пункты проваливаются
        или агент буксует. Без этого прогон честно доработает по плохому
        плану до конца бюджета — самый обидный способ потратить 8 часов.
        """
        tasks = self.store.tasks(self.run_id)
        done = [t for t in tasks if t["status"] == "done"]
        failed = [t for t in tasks if t["status"] == "failed"]
        left = [t for t in tasks if t["status"] in ("open", "doing")]
        if not left:
            return False

        goal = self.store.get_run(self.run_id)["goal"]
        fmt = lambda rows: ("\n".join(f"- {t['title']}"
                                      + (f" → {t['result'][:120]}" if t["result"] else "")
                                      for t in rows) or "- (пусто)")
        agent = self.make_agent()
        res = agent.run(REPLAN.format(goal=goal, reason=reason,
                                      done=fmt(done), failed=fmt(failed),
                                      left=fmt(left)))
        titles = [ln.strip(" -•*0123456789.\t")
                  for ln in res.answer.splitlines() if ln.strip()]
        titles = [t for t in titles if len(t) > 8][:6]
        if not titles:
            return False

        dropped = self.store.drop_open_tasks(self.run_id)
        self.store.add_tasks(self.run_id, titles)
        self.store.remember(f"План пересмотрен: {reason}", tags="replan",
                            run_id=self.run_id)
        self.store.log_event(self.run_id, 0, "replan", "replan",
                             f"{reason}; было {dropped}, стало {len(titles)}")
        self._emit("replan", reason=reason, items=titles, dropped=dropped)
        return True

    def _context(self, task: dict[str, Any], warn: str) -> str:
        extra = ""
        if task.get("check_hint"):
            extra = ("ЧЕМ ПОДТВЕРДИТЬ ВЫПОЛНЕНИЕ: "
                     f"{task['check_hint']}\n")
        # Результаты пунктов, от которых этот зависит: без них
        # исполнитель заново добывает уже добытое.
        deps = [d for d in (task.get("needs") or "").split(",")
                if d.strip().isdigit()]
        if deps:
            by_id = {t["id"]: t for t in self.store.tasks(self.run_id)}
            got = [by_id[int(d)] for d in deps if int(d) in by_id]
            lines = [f"- {t['title']}: {(t['result'] or '(без результата)')[:200]}"
                     for t in got if t["status"] == "done"]
            if lines:
                extra += "ГОТОВО В ПРЕДЫДУЩИХ ПУНКТАХ:\n" + "\n".join(lines) + "\n"
        facts = self.store.recall(task["title"], limit=6)
        memory = ("Из памяти:\n" + "\n".join(f"- {f['text']}" for f in facts)
                  if facts else "")
        evs = self.store.recent_events(self.run_id, limit=6)
        recent = ("Последние действия:\n" +
                  "\n".join(f"- {e['name']}: {e['summary'][:100]}"
                            for e in evs if e["kind"] == "tool")) if evs else ""
        return WORKER.format(
            goal=self.store.get_run(self.run_id)["goal"],
            task_id=task["id"], task=task["title"],
            memory=memory, recent=recent, warning=extra + warn)

    def _reflect(self, task: dict[str, Any], summary: str) -> bool:
        """Возвращает True, если модель сигналит о застое."""
        agent = self.make_agent()
        agent.tools = agent.tools           # рефлексия без инструментов
        res = agent.run(REFLECT.format(task=task["title"],
                                       summary=summary[:1500]))
        text = res.answer.strip()
        # вытаскиваем JSON даже если модель обернула его в текст
        i, j = text.find("{"), text.rfind("}")
        if i < 0 or j < i:
            return False
        try:
            data = json.loads(text[i:j + 1])
        except json.JSONDecodeError:
            return False
        for fact in (data.get("learned") or [])[:5]:
            if isinstance(fact, str) and len(fact.strip()) > 8:
                self.store.remember(fact.strip(), tags="reflect",
                                    run_id=self.run_id)
        if data.get("next"):
            self.store.log_event(self.run_id, 0, "reflect", "next",
                                 str(data["next"])[:300])
        self._emit("reflect", learned=data.get("learned") or [],
                   next=data.get("next", ""))
        return bool(data.get("stuck"))

    # ----------------------------------------------------------- запуск
    def run(self, goal: str, profile: str | None = None,
            resume: int | None = None) -> AutoResult:
        t0 = time.time()
        if resume:
            self.run_id = resume
            row = self.store.get_run(resume)
            if not row:
                raise ValueError(f"Прогон {resume} не найден")
            goal = row["goal"]
            self._emit("resume", run_id=resume, goal=goal)
        else:
            self.run_id = self.store.start_run(goal, profile)
            self._emit("start", run_id=self.run_id, goal=goal)
            self._plan(goal)

        stop, it, stuck_streak = "iterations", 0, 0
        replans, replanned_at_fails = 0, 0

        while it < self.max_iterations:
            if time.time() - t0 > self.max_seconds:
                stop = "time"
                break
            # Деньги проверяем ДО начала итерации: остановиться на пороге
            # можно только между шагами, прервать оплаченный запрос нельзя.
            if self.max_usd > 0:
                spent_all = float(
                    (self.store.get_run(self.run_id) or {}).get("cost", 0) or 0)
                if spent_all >= self.max_usd:
                    self._emit("budget", spent=spent_all, limit=self.max_usd)
                    stop = "budget"
                    break
            # По готовности зависимостей, а не по номеру: иначе «собрать
            # отчёт» берётся раньше «посчитать данные».
            task = (self.store.next_ready_task(self.run_id)
                    if self.decompose else self.store.next_task(self.run_id))
            if not task:
                rows = self.store.tasks(self.run_id)
                stuck_deps = (self.store.deadlocked(self.run_id)
                              if self.decompose else [])
                if stuck_deps:
                    # Пункты остались открытыми, но выполнить их нельзя.
                    # Оставить их молча «открытыми» — значит не объяснить
                    # человеку, почему план не доделан.
                    for t in stuck_deps:
                        self.store.set_task(
                            t["id"], "skipped",
                            "не выполнен: провалена или зациклена зависимость")
                    self._emit("deadlock",
                               items=[t["title"] for t in stuck_deps])
                    stop = "deadlock"
                    break
                stop = "blocked" if any(
                    t["status"] == "blocked" for t in rows) else "done"
                break

            it += 1
            self.store.set_task(task["id"], "doing")
            self._emit("iteration", n=it, task=task["title"],
                       task_id=task["id"],
                       left=int(time.time() - t0))

            # предупреждение о повторах, если агент топчется
            warn = ""
            evs = self.store.recent_events(self.run_id, limit=8)
            sigs = [e["sig"] for e in evs if e["kind"] == "tool" and e["sig"]]
            if sigs:
                top = max(set(sigs), key=sigs.count)
                if sigs.count(top) >= self.repeat_limit:
                    warn = ("ВНИМАНИЕ: ты повторяешь одно и то же действие. "
                            "Смени подход или закрой пункт через plan_fail.")

            # Пункт плана может уйти СВОЕМУ агенту: чертёж — конструктору,
            # письмо — делопроизводителю. Профиль выбирается по тексту
            # пункта, а не по общей цели: внутри одной цели темы разные.
            who, why = None, ""
            assigned = (task.get("profile") or "").strip()
            if assigned and assigned in self.known_profiles:
                # Планировщик видел задачу целиком, диспетчер — одну
                # строку. Назначение плана точнее, поэтому оно главнее.
                who, why = assigned, "назначен планом"
            elif self.route_tasks:
                pick = choose_profile(task["title"], self.known_profiles)
                who, why = pick.profile, pick.reason
            if who and who != self.last_profile:
                self._emit("handoff", profile=who, task=task["title"],
                           reason=why)
                self.last_profile = who
            agent = self._agent_for(who)
            calls: list[str] = []

            def watch(kind: str, data: dict[str, Any]) -> None:
                if kind == "tool_start":
                    sig = self._sig(data.get("name", ""), data.get("args") or {})
                    self.store.log_event(self.run_id, it, "tool",
                                         data.get("name", ""),
                                         json.dumps(data.get("args") or {},
                                                    ensure_ascii=False)[:200],
                                         sig)
                    calls.append(data.get("name", ""))
                self._emit(kind, **data)

            agent.on_event = watch
            try:
                res = agent.run(self._context(task, warn))
            except Exception as exc:                    # прогон не должен падать
                self.store.log_event(self.run_id, it, "error", "exception",
                                     str(exc)[:400])
                self._emit("error", message=str(exc))
                self.store.set_task(task["id"], "failed", str(exc)[:300])
                continue

            # Учёт расхода: важно на длинном прогоне — иначе счёт за
            # сутки работы становится сюрпризом.
            spent = 0.0
            price = price_of(agent.llm.model) if agent.llm.billable else (0.0, 0.0)
            if price:
                spent = (res.prompt_tokens * price[0]
                         + res.completion_tokens * price[1]) / 1e6
            self.store.bump_run(self.run_id, steps=len(res.steps),
                                calls=res.tool_calls,
                                chars=sum(len(json.dumps(m, ensure_ascii=False))
                                          for m in res.messages),
                                tok_in=res.prompt_tokens,
                                tok_out=res.completion_tokens,
                                cost=spent)
            if res.tokens:
                self._emit("spend", tokens=res.tokens, cost=spent,
                           total=(self.store.get_run(self.run_id) or {}).get("cost", 0))

            # если агент не закрыл пункт сам — закрываем по факту работы
            fresh = [t for t in self.store.tasks(self.run_id)
                     if t["id"] == task["id"]][0]
            kids = self.store.children(task["id"])
            if fresh["status"] == "doing" and kids:
                # Агент разбил пункт на подшаги — работа не сделана, она
                # только распланирована. Закрыть его сейчас значило бы
                # объявить выполненным то, к чему ещё не приступали.
                self.store.set_task(task["id"], "open",
                                    f"разбит на {len(kids)} подшагов")
                self._emit("split", task=task["title"],
                           items=[k["title"] for k in kids])
            elif fresh["status"] == "doing":
                self.store.set_task(task["id"], "done", res.answer[:500])

            # Родитель закрывается сам, когда все его подшаги завершены.
            for parent in self.store.close_finished_parents(self.run_id):
                self._emit("parent_done", task=parent["title"],
                           result=parent["result"])

            if self._reflect(task, res.answer):
                stuck_streak += 1
            else:
                stuck_streak = 0

            # Плохой план чиним перепланированием, а не упорством.
            fails = sum(1 for t in self.store.tasks(self.run_id)
                        if t["status"] == "failed")
            if (fails - replanned_at_fails) >= self.replan_after_fails:
                if self._replan(f"провалено пунктов: {fails}"):
                    replanned_at_fails = fails
                    stuck_streak = 0
                    replans += 1
            elif stuck_streak >= 2 and replans < 3:
                if self._replan("агент буксует, прогресса нет"):
                    stuck_streak = 0
                    replans += 1

            if stuck_streak >= 3:
                stop = "stuck"
                break

        elapsed = time.time() - t0
        self.store.finish_run(self.run_id,
                              "done" if stop == "done" else "stopped")
        done = [t for t in self.store.tasks(self.run_id) if t["status"] == "done"]
        allt = self.store.tasks(self.run_id)
        e, r = self.store.graph_stats()
        row = self.store.get_run(self.run_id) or {}
        tok = int(row.get("tok_in", 0)) + int(row.get("tok_out", 0))
        spent = float(row.get("cost", 0) or 0)
        # Вопросы, оставшиеся без ответа, — не мелочь: пункт не сделан,
        # и человек должен увидеть это первым делом, а не в логе.
        blocked = [t for t in allt if t["status"] == "blocked"]
        ask_block = ""
        if blocked:
            ask_block = "\n\nЖДУТ ОТВЕТА ЧЕЛОВЕКА:\n" + "\n".join(
                f"  #{t['id']} {t['title']}\n     {t['result']}"
                for t in blocked)
        skipped = [t for t in allt if t["status"] == "skipped"]
        dead_block = ""
        if skipped:
            dead_block = ("\n\nНЕ ВЫПОЛНЕНЫ (зависимость провалена "
                          "или зациклена):\n" + "\n".join(
                              f"  #{t['id']} {t['title']}" for t in skipped))
        summary = (
            f"Прогон #{self.run_id}: {stop}\n"
            f"Итераций: {it}, время: {elapsed/60:.1f} мин\n"
            f"План: {len(done)} из {len(allt)} пунктов"
            + (f", заблокировано вопросом: {len(blocked)}" if blocked else "")
            + "\n"
            f"Память: {self.store.fact_count()} фактов, "
            f"граф: {e} объектов / {r} связей"
            + (f"\nПлан пересматривался: {replans} раз" if replans else "")
            + (f"\nТокенов: {tok:,}" if tok else "")
            + (f", примерно ${spent:.4f}" if spent > 0
               else " (локальная модель, оплаты нет)" if tok else "")
            + (f"\nОСТАНОВЛЕН ПО БЮДЖЕТУ: потрачено ${spent:.4f} при пределе "
               f"${self.max_usd:.2f}. План не доделан — продолжить можно "
               f"командой --resume {self.run_id} с большим --max-usd."
               if stop == "budget" else "")
            + ask_block + dead_block
        )
        self._emit("finish", summary=summary, stopped_by=stop)
        return AutoResult(self.run_id, it, stop, summary, elapsed, tok, spent)
