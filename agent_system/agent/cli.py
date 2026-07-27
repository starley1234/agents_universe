"""CLI: разовая задача, интерактивный режим, самопроверка."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .autorun import AutoRunner
from .build import build_agent, known_skills
from .mcp import MCPPool
from .store import Store
from .config import Config, replace_profile
from .dispatch import choose_profile
from .llm import available as providers

# --- вывод -----------------------------------------------------------------
C_DIM, C_TOOL, C_OK, C_ERR, C_OFF = "\033[2m", "\033[36m", "\033[32m", "\033[31m", "\033[0m"
C_ACC = "\033[35m"


def _plain(s: str) -> str:
    return s


def make_printer(verbose: bool, color: bool):
    tint = (lambda c, s: f"{c}{s}{C_OFF}") if color else (lambda c, s: s)

    def on_event(kind: str, data: dict[str, Any]) -> None:
        if kind == "thought" and verbose and data.get("text"):
            print(tint(C_DIM, f"  … {data['text'].strip()[:400]}"))
        elif kind == "tool_start":
            args = json.dumps(data.get("args", {}), ensure_ascii=False)
            if len(args) > 160:
                args = args[:160] + "…"
            print(tint(C_TOOL, f"  → {data['name']} {args}"))
        elif kind == "tool_end" and verbose:
            res = (data.get("result") or "").strip()
            head = res.splitlines()[:6]
            for ln in head:
                print(tint(C_DIM, f"    {ln[:200]}"))
            if len(res.splitlines()) > 6:
                print(tint(C_DIM, f"    … ещё {len(res.splitlines()) - 6} строк"))
        elif kind == "error":
            print(tint(C_ERR, f"  ! {data.get('message')}"))
        elif kind == "limit":
            print(tint(C_ERR, f"  ! исчерпан лимит шагов ({data.get('steps')})"))

    return on_event


def ask_human(question: str, options: list[str]) -> str:
    """Вопрос агента человеку в терминал. Пустая строка = не ответил."""
    print(f"\n{C_ACC}ВОПРОС АГЕНТА{C_OFF}: {question}")
    for i, o in enumerate(options, 1):
        print(f"  {i}) {o}")
    try:
        ans = input("ответ (пусто — решай сам): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    # Ответ цифрой — удобство: печатать вариант целиком не нужно.
    if options and ans.isdigit() and 1 <= int(ans) <= len(options):
        return options[int(ans) - 1]
    return ans


def ask_confirm(command: str, reason: str) -> bool:
    print(f"\n{C_ERR}ОПАСНАЯ КОМАНДА{C_OFF} ({reason}):\n  {command}")
    try:
        return input("Выполнить? [y/N] ").strip().lower() in ("y", "yes", "д", "да")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


# --- команды ---------------------------------------------------------------
def cmd_run(cfg: Config, task: str, verbose: bool, color: bool) -> int:
    tint = (lambda c, s: f"{c}{s}{C_OFF}") if color else (lambda c, s: s)
    agent = build_agent(cfg, confirm=ask_confirm, ask=ask_human,
                        on_event=make_printer(verbose, color))
    print(f"{C_DIM}модель: {cfg.provider}/{cfg.model} · песочница: "
          f"{cfg.sandbox.mode} · роль: {cfg.profile or '—'} · "
          f"навыки: {', '.join(cfg.skills)}{C_OFF}\n")
    agent.llm.on_retry = lambda n, why, delay: print(
        tint(C_ERR, f"  ! сбой связи ({why[:70]}), повтор {n} через {delay:.0f} с"))
    res = agent.run(task)
    print("\n" + "─" * 60)
    print(res.answer)
    print("─" * 60)
    mark = C_OK if res.stopped_by == "done" else C_ERR
    print(f"{mark}шагов: {len(res.steps)} · вызовов инструментов: "
          f"{res.tool_calls} · итог: {res.stopped_by}{C_OFF}")
    print(tint(C_DIM, agent.llm.spend_report()))
    return 0 if res.stopped_by == "done" else 1


def cmd_chat(cfg: Config, verbose: bool, color: bool) -> int:
    agent = build_agent(cfg, confirm=ask_confirm, ask=ask_human,
                        on_event=make_printer(verbose, color))
    print(f"{C_DIM}Интерактивный режим. 'exit' — выход, 'reset' — забыть "
          f"историю.{C_OFF}")
    history: list[dict[str, Any]] = []
    while True:
        try:
            task = input(f"\n{C_OK}вы>{C_OFF} ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nпока")
            return 0
        if not task:
            continue
        if task in ("exit", "quit", "выход"):
            return 0
        if task in ("reset", "сброс"):
            history.clear()
            print(f"{C_DIM}история очищена{C_OFF}")
            continue
        # переносим прошлые ответы как контекст
        prefix = ""
        if history:
            prev = "\n".join(f"- {h}" for h in history[-5:])
            prefix = f"Ранее в этой сессии сделано:\n{prev}\n\nНовая задача: "
        res = agent.run(prefix + task)
        print(f"\n{res.answer}")
        history.append(f"{task} -> {res.answer[:200]}")


def cmd_auto(cfg: Config, goal: str, hours: float, iters: int,
             resume: int | None, verbose: bool, color: bool,
             max_usd: float = 0.0, route: bool = False) -> int:
    """Автономный прогон: часы работы без человека."""
    tint = (lambda c, s: f"{c}{s}{C_OFF}") if color else (lambda c, s: s)
    store = Store(cfg.db)
    rid = {"v": 0}
    # Пул MCP поднимается ОДИН раз: иначе на каждой итерации заново
    # запускались бы подпроцессы серверов.
    pool = MCPPool(cfg.mcp) if ("mcp" in cfg.skills and cfg.mcp) else None
    if pool:
        print(tint(C_DIM, "MCP:\n  " + pool.status().replace("\n", "\n  ")))
    printer = make_printer(verbose, color)

    def on_event(kind, data):
        if kind == "start":
            print(tint(C_OK, f"\n▶ прогон #{data['run_id']}: {data['goal']}"))
        elif kind == "resume":
            print(tint(C_OK, f"\n▶ продолжаем прогон #{data['run_id']}"))
        elif kind == "plan":
            print(tint(C_OK, "\nПлан:"))
            for i, t in enumerate(data["items"], 1):
                print(f"  {i}. {t}")
        elif kind == "iteration":
            print(tint(C_ACC, f"\n── итерация {data['n']} · {data['left']//60} мин "
                              f"· #{data['task_id']} {data['task']}"))
        elif kind == "spend":
            c = data.get("cost", 0)
            print(tint(C_DIM, f"  расход: {data['tokens']:,} токенов"
                              + (f", ${c:.4f}" if c else "")))
        elif kind == "handoff":
            print(tint(C_ACC, f"\n→ передано агенту «{data['profile']}» "
                              f"({data['reason']})"))
        elif kind == "budget":
            print(tint(C_ERR, f"\n$ бюджет исчерпан: потрачено "
                              f"${data['spent']:.4f} при пределе "
                              f"${data['limit']:.2f} — останавливаюсь"))
        elif kind == "replan":
            print(tint(C_ERR, f"\n⟲ план пересмотрен: {data['reason']}"))
            for i, t in enumerate(data["items"], 1):
                print(f"  {i}. {t}")
        elif kind == "reflect":
            for f in data.get("learned", []):
                print(tint(C_DIM, f"  запомнил: {f[:150]}"))
            if data.get("next"):
                print(tint(C_DIM, f"  дальше: {data['next'][:150]}"))
        elif kind == "finish":
            print(tint(C_OK, "\n" + "─" * 60))
            print(data["summary"])
            print(tint(C_OK, "─" * 60))
        else:
            printer(kind, data)

    def factory(profile: str | None = None):
        # Профиль меняет НАБОР НАВЫКОВ и промпт, остальное общее:
        # база, рабочая папка и пул MCP у агентов одни на прогон.
        use = cfg
        if profile and profile != cfg.profile:
            use = replace_profile(cfg, profile)
        a = build_agent(use, confirm=ask_confirm, store=store,
                        run_id_getter=lambda: rid["v"], mcp_pool=pool)
        a.llm.on_retry = lambda n, why, delay: print(
            tint(C_ERR, f"  ! сбой связи ({why[:60]}), повтор {n} через {delay:.0f} с"))
        return a

    runner = AutoRunner(factory, store, max_hours=hours,
                        max_iterations=iters, max_usd=max_usd,
                        route_tasks=route,
                        known_profiles=Config.list_profiles(),
                        on_event=on_event)

    # run_id появляется внутри runner — прокидываем его инструментам памяти
    _orig_start = store.start_run

    def _start(goal_: str, profile_=None) -> int:
        rid["v"] = _orig_start(goal_, profile_)
        return rid["v"]

    store.start_run = _start  # type: ignore[assignment]
    if resume:
        rid["v"] = resume

    money = f" / ${max_usd:.2f}" if max_usd > 0 else ""
    print(tint(C_DIM, f"модель: {cfg.provider}/{cfg.model} · роль: "
                      f"{cfg.profile or '—'} · бюджет: {hours} ч / "
                      f"{iters} итераций{money}"))
    print(tint(C_DIM, f"база: {cfg.db}"))
    try:
        if resume:
            rid["v"] = resume
        res = runner.run(goal, cfg.profile, resume=resume)
    except KeyboardInterrupt:
        if pool:
            pool.close()
        print(tint(C_ERR, "\nпрервано пользователем"))
        if rid["v"]:
            store.finish_run(rid["v"], "stopped")
            print(f"Продолжить: python3 -m agent --auto --resume {rid['v']}")
        return 130
    if pool:
        pool.close()
    print(f"\nПродолжить прогон: python3 -m agent --auto --resume {res.run_id}")
    return 0 if res.stopped_by in ("done", "time") else 1



def cmd_do(cfg: Config, task: str, verbose: bool, color: bool,
           hours: float, iters: int, max_usd: float, yes: bool) -> int:
    """Простой режим: человек ставит задачу, система решает остальное.

    Выбор роли объясняется вслух и его можно отменить. Тихо назначить
    исполнителя нельзя: если правило ошиблось, человек должен это
    увидеть до начала работы, а не по итогам часа.
    """
    tint = (lambda c, s: f"{c}{s}{C_OFF}") if color else (lambda c, s: s)
    known = Config.list_profiles()
    pick = choose_profile(task, known)

    print(tint(C_ACC, "Разбор задачи"))
    print(f"  задача : {task[:70]}")
    print(f"  {pick.explain()}")
    mode = "автономный прогон" if pick.autonomous else "одиночный запуск"
    print(f"  режим  : {mode}"
          + (f", до {hours} ч" if pick.autonomous else ""))

    if pick.profile:
        cfg.apply_profile(pick.profile)
    print(tint(C_DIM, f"  навыки : {', '.join(cfg.skills)}"))

    if not yes and sys.stdin.isatty():
        try:
            ans = input("\nНачинать? [Enter — да, имя профиля — заменить, "
                        "n — отмена] ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 130
        if ans.lower() in ("n", "no", "н", "нет"):
            print("отменено")
            return 0
        if ans and ans.lower() not in ("y", "yes", "д", "да"):
            if ans not in known:
                print(f"{C_ERR}Профиля {ans!r} нет. Доступны: "
                      f"{', '.join(known)}{C_OFF}", file=sys.stderr)
                return 2
            cfg.apply_profile(ans)
            print(tint(C_DIM, f"  навыки : {', '.join(cfg.skills)}"))
    print()

    if pick.autonomous:
        # В простом режиме передача между агентами включена: длинная
        # задача почти всегда состоит из разнородных пунктов.
        return cmd_auto(cfg, task, hours, iters, None, verbose, color,
                        max_usd, route=True)
    return cmd_run(cfg, task, verbose, color)


def cmd_debate(cfg: Config, question: str, rounds: int, max_usd: float,
               models: str, verbose: bool, color: bool,
               resume: int = 0, summarize: bool = False) -> int:
    """Разбор вопроса двумя моделями под присмотром арбитра."""
    from .debate import STANCE_A, STANCE_B, Debate
    from .llm import build_llm

    tint = (lambda c, s: f"{c}{s}{C_OFF}") if color else (lambda c, s: s)
    store = Store(cfg.db)
    d = getattr(cfg, "debate", {}) or {}

    def pick(key: str, fallback: str) -> str:
        return d.get(key) or fallback

    names = [x.strip() for x in models.split(",") if x.strip()]
    m_a = names[0] if names else pick("model_a", cfg.model)
    m_b = names[1] if len(names) > 1 else pick("model_b", cfg.model)
    m_arb = names[2] if len(names) > 2 else pick("model_arbiter", m_b)

    common = dict(base_url=cfg.base_url, api_key=cfg.api_key,
                  temperature=cfg.temperature)
    llm_a = build_llm(cfg.provider, m_a, **common)
    llm_b = build_llm(cfg.provider, m_b, **common)
    llm_arb = build_llm(cfg.provider, m_arb, **common)

    # Исполнитель: обычный агент с инструментами. Профиль подбирается
    # по тексту вопроса — «проверь STL» уйдёт в cad, «найди» в rag.
    def executor(task: str) -> str:
        pick_p = choose_profile(task, Config.list_profiles())
        use = replace_profile(cfg, pick_p.profile) if pick_p.profile \
            else replace_profile(cfg, cfg.profile) if cfg.profile \
            else Config(**{**cfg.__dict__})
        # Проверка факта почти всегда требует счёта или чтения файла.
        # Тема может не распознаться («посчитай 300000/5000» не про CAD и
        # не про документы) — тогда профиль не выбран, и без этой строки
        # исполнитель приходил БЕЗ run_python и отвечал «инструмента нет».
        # Поймано живым прогоном.
        use.skills = sorted(set(use.skills) | {"files", "python"})
        use.max_steps = min(use.max_steps, 12)
        print(tint(C_ACC, f"\n  ⚙ проверка: {task[:70]}"
                          + (f" [роль {pick_p.profile}]" if pick_p.profile
                             else "")))
        agent = build_agent(use, confirm=ask_confirm)
        res = agent.run(task)
        return res.answer[:1500]

    NAMES = {"a": "A", "b": "B", "arbiter": "арбитр", "executor": "проверка"}
    COLORS = {"a": C_OK, "b": C_ACC, "arbiter": C_DIM, "executor": C_DIM}

    def on_event(kind, data):
        if kind == "start":
            print(tint(C_OK, f"\n▶ разбор #{data['debate_id']}: "
                             f"{data['question']}"))
            print(tint(C_DIM, f"  A: {data['model_a']} · B: {data['model_b']}"
                              f" · арбитр: {data['model_arbiter']}"))
        elif kind == "resume":
            print(tint(C_OK, f"\n▶ продолжаем разбор #{data['debate_id']}"))
        elif kind == "round":
            print(tint(C_DIM, f"\n── круг {data['n']} из {data['of']}"))
        elif kind == "turn":
            who = NAMES.get(data["role"], data["role"])
            col = COLORS.get(data["role"], C_DIM)
            print(tint(col, f"\n{who}:"))
            print(f"  {data['text'][:900]}")
        elif kind == "arbiter":
            print(tint(C_ACC, f"\n  ⚖ арбитр: {data['decision']} — "
                              f"{data['reason'][:120]}"))
        elif kind == "warn":
            print(tint(C_ERR, f"  ⚠ {data['message']}"))
        elif kind == "arbiter_unparsed":
            print(tint(C_ERR, "  ⚠ ответ арбитра не разобран"))

    deb = Debate(
        llm_a, llm_b, llm_arb, store,
        stance_a=d.get("prompt_a") or STANCE_A,
        stance_b=d.get("prompt_b") or STANCE_B,
        rounds=rounds or int(d.get("rounds", 12)),
        arbiter_every=int(d.get("arbiter_every", 3)),
        max_usd=max_usd,
        max_minutes=float(d.get("max_minutes", 30)),
        executor=executor,
        max_executor_calls=int(d.get("max_executor_calls", 5)),
        on_event=on_event)

    try:
        res = deb.run(question, resume=resume)
    except KeyboardInterrupt:
        print(tint(C_ERR, "\nпрервано"))
        if deb.debate_id:
            store.finish_debate(deb.debate_id, "stopped")
            print(f"Продолжить: python3 -m agent --debate "
                  f"--resume {deb.debate_id}")
        return 130

    print("\n" + "─" * 60)
    print(res.summary())
    print("─" * 60)

    # Протокол пишем ВСЕГДА: вердикт в две строки через неделю ничего не
    # объяснит, а ход рассуждения и проверенные факты — объяснят.
    from .protocol import build_protocol
    try:
        text = build_protocol(store, res.debate_id,
                              llm_arb if summarize else None)
        out = Path(cfg.workspace) / f"протокол-разбора-{res.debate_id}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(tint(C_OK, f"Протокол: {out}"))
    except Exception as exc:               # протокол не важнее разбора
        print(tint(C_ERR, f"Протокол не собран: {exc}"))

    print(tint(C_DIM, f"Продолжить: python3 -m agent --debate --resume "
                      f"{res.debate_id}"))
    return 0 if res.status == "done" else 1


def cmd_protocol(cfg: Config, debate_id: int, summarize: bool,
                 color: bool) -> int:
    """Пересобрать протокол по уже прошедшему разбору."""
    from .protocol import build_protocol
    from .llm import build_llm

    tint = (lambda c, s: f"{c}{s}{C_OFF}") if color else (lambda c, s: s)
    store = Store(cfg.db)
    llm = None
    if summarize:
        d = getattr(cfg, "debate", {}) or {}
        llm = build_llm(cfg.provider, d.get("model_arbiter") or cfg.model,
                        base_url=cfg.base_url, api_key=cfg.api_key,
                        temperature=cfg.temperature)
    try:
        text = build_protocol(store, debate_id, llm)
    except ValueError as exc:
        print(f"{C_ERR}{exc}{C_OFF}", file=sys.stderr)
        return 1
    out = Path(cfg.workspace) / f"протокол-разбора-{debate_id}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(tint(C_DIM, f"\nСохранено: {out}"))
    return 0


def cmd_debates(cfg: Config, color: bool) -> int:
    """Список разборов."""
    tint = (lambda c, s: f"{c}{s}{C_OFF}") if color else (lambda c, s: s)
    store = Store(cfg.db)
    rows = store.debates(30)
    if not rows:
        print(f"В базе {cfg.db} разборов нет")
        return 0
    mark = {"done": C_OK, "active": C_ACC}
    print(f"Разборы в {cfg.db}\n" + "─" * 72)
    for r in rows:
        when = time.strftime("%d.%m %H:%M",
                             time.localtime(r["started"] or 0))
        st = tint(mark.get(r["status"], C_ERR), f"{r['status']:<13}")
        print(f"#{r['id']:<4} {when}  {st} {r['question'][:42]}")
        cost = float(r.get("cost") or 0)
        print(tint(C_DIM, f"      кругов {r['rounds']}, "
                          f"токенов {int(r.get('tok_out') or 0):,}"
                          + (f", ${cost:.4f}" if cost > 0 else "")))
        if r["verdict"]:
            print(tint(C_DIM, f"      → {r['verdict'][:100]}"))
    return 0


def cmd_runs(cfg: Config, run_id: int, limit: int, color: bool) -> int:
    """История прогонов. Данные копились в базе, смотреть их было нечем."""
    tint = (lambda c, s: f"{c}{s}{C_OFF}") if color else (lambda c, s: s)
    store = Store(cfg.db)
    mark = {"done": C_OK, "active": C_ACC, "stopped": C_ERR,
            "failed": C_ERR}

    if not run_id:
        rows = store.runs(limit)
        if not rows:
            print(f"В базе {cfg.db} прогонов нет")
            return 0
        print(f"Прогоны в {cfg.db}\n" + "─" * 72)
        for r in rows:
            when = time.strftime("%d.%m %H:%M",
                                 time.localtime(r["started"] or 0))
            spent = float(r.get("cost") or 0)
            money = f" ${spent:.4f}" if spent > 0 else ""
            tok = int(r.get("tok_in") or 0) + int(r.get("tok_out") or 0)
            c = mark.get(r["status"], C_DIM)
            state = tint(c, f"{r['status']:<8}")
            print(f"#{r['id']:<4} {when}  {state} {r['goal'][:44]}")
            print(tint(C_DIM, f"      шагов {r['steps']}, вызовов "
                              f"{r['tool_calls']}, токенов {tok:,}{money}"
                              + (f", роль {r['profile']}" if r["profile"] else "")))
        print("─" * 72)
        print(tint(C_DIM, "Подробности: python3 -m agent --runs N"))
        return 0

    row = store.get_run(run_id)
    if not row:
        print(f"{C_ERR}Прогона #{run_id} нет{C_OFF}", file=sys.stderr)
        return 1
    print(f"Прогон #{run_id}: {row['goal']}\n" + "─" * 72)
    dur = (row.get("finished") or row.get("updated") or 0) - (row.get("started") or 0)
    print(f"состояние : {row['status']}, {dur / 60:.1f} мин")
    print(f"расход    : шагов {row['steps']}, вызовов {row['tool_calls']}, "
          f"токенов {int(row.get('tok_in') or 0) + int(row.get('tok_out') or 0):,}"
          + (f", ${float(row.get('cost') or 0):.4f}"
             if float(row.get("cost") or 0) > 0 else ""))

    tasks = store.tasks(run_id)
    if tasks:
        sign = {"open": "[ ]", "doing": "[~]", "done": "[x]",
                "failed": "[!]", "skipped": "[-]", "blocked": "[?]"}
        print("\nПлан:")
        for t in tasks:
            line = f"  {sign.get(t['status'], '[ ]')} #{t['id']} {t['title']}"
            print(line)
            if t["result"]:
                print(tint(C_DIM, f"        → {t['result'][:120]}"))
        blocked = [t for t in tasks if t["status"] == "blocked"]
        if blocked:
            print(tint(C_ERR, f"\nЖдут ответа человека: {len(blocked)}"))

    evs = store.run_events(run_id, limit=40, kinds="tool,error,reflect")
    if evs:
        print("\nЖурнал (последние 40 событий):")
        for e in evs:
            when = time.strftime("%H:%M:%S", time.localtime(e["created"] or 0))
            c = C_ERR if e["kind"] == "error" else C_DIM
            print(tint(c, f"  {when} {e['kind']:<7} {e['name'][:24]:<24} "
                          f"{(e['summary'] or '')[:60]}"))
    return 0


def cmd_check(cfg: Config) -> int:
    """Самопроверка: конфиг, песочница, доступность модели."""
    from .tools.shell import docker_available, effective_mode

    ok = True
    print("Проверка окружения\n" + "─" * 40)
    print(f"провайдер     : {cfg.provider}")
    print(f"модель        : {cfg.model}")
    print(f"base_url      : {cfg.base_url}")
    print(f"ключ          : {'задан' if cfg.api_key else 'НЕ ЗАДАН'}")
    if cfg.provider not in ("ollama",) and not cfg.api_key:
        print(f"  {C_ERR}! для этого провайдера обычно нужен ключ в "
              f"переменной окружения{C_OFF}")
    print(f"рабочая папка : {cfg.workspace}")
    eff, note = effective_mode(cfg.sandbox)
    print(f"песочница     : {cfg.sandbox.mode}"
          + (f" -> фактически {eff}" if eff != cfg.sandbox.mode else ""))
    if note:
        print(f"  {C_ERR}{note}{C_OFF}")
    elif eff == "docker":
        print(f"  {C_OK}docker доступен{C_OFF}")
    print(f"роль          : {cfg.profile or '— (без профиля)'}")
    print(f"навыки        : {', '.join(cfg.skills)}")
    if cfg.mcp:
        print("MCP-серверы   :")
        p = MCPPool(cfg.mcp)
        for line in p.status().splitlines():
            bad = "НЕДОСТУПЕН" in line
            print(f"  {C_ERR if bad else C_OK}{line}{C_OFF}")
        p.close()

    try:
        agent = build_agent(cfg)
        print(f"инструментов  : {len(agent.tools)} "
              f"({', '.join(agent.tools.names())})")
    except Exception as exc:
        print(f"  {C_ERR}! сборка агента не удалась: {exc}{C_OFF}")
        return 1

    print("─" * 40)
    print(f"{C_OK}готово к работе{C_OFF}" if ok else
          f"{C_ERR}есть замечания выше{C_OFF}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="agent", description="Универсальный агент с инструментами")
    ap.add_argument("task", nargs="*", help="задача; без неё — интерактив")
    ap.add_argument("-c", "--config", help="файл конфигурации JSON")
    ap.add_argument("-p", "--provider", choices=providers(), help="провайдер")
    ap.add_argument("-m", "--model", help="имя модели")
    ap.add_argument("-w", "--workspace", help="рабочая папка")
    ap.add_argument("-P", "--profile", help="роль: " +
                    ", ".join(Config.list_profiles()))
    ap.add_argument("-s", "--skills", help="навыки через запятую: "
                                           f"{', '.join(known_skills())}")
    ap.add_argument("--sandbox", dest="sandbox_mode",
                    choices=["auto", "docker", "confirm", "off"], help="режим запуска команд")
    ap.add_argument("--max-steps", type=int, dest="max_steps")
    ap.add_argument("--base-url", dest="base_url")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="показывать мысли и вывод инструментов")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--check", action="store_true", help="самопроверка и выход")
    ap.add_argument("--smoke", action="store_true",
                    help="дымовой тест ЖИВОЙ модели: тянет ли инструменты")
    ap.add_argument("--only", default="",
                    help="в --smoke: прогнать только задачи с этим словом")
    ap.add_argument("--auto", action="store_true",
                    help="автономный режим: часы работы без человека")
    ap.add_argument("--hours", type=float, help="бюджет времени, часов")
    ap.add_argument("--iterations", type=int, help="предел итераций")
    ap.add_argument("--max-usd", type=float, dest="max_usd",
                    help="предел расхода за прогон в долларах")
    ap.add_argument("--resume", type=int, help="продолжить прогон по номеру")
    ap.add_argument("--db", help="файл базы состояния")
    ap.add_argument("--runs", nargs="?", const=0, type=int, metavar="N",
                    help="история прогонов; с номером — подробности")
    ap.add_argument("--do", action="store_true",
                    help="простой режим: роль и режим выбираются сами")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="не переспрашивать в простом режиме")
    ap.add_argument("--debate", action="store_true",
                    help="разбор вопроса двумя моделями с арбитром")
    ap.add_argument("--debates", action="store_true",
                    help="список разборов")
    ap.add_argument("--rounds", type=int, default=0,
                    help="в --debate: предел кругов")
    ap.add_argument("--protocol", type=int, metavar="N",
                    help="пересобрать протокол разбора №N")
    ap.add_argument("--summarize", action="store_true",
                    help="в протоколе: связный пересказ моделью")
    ap.add_argument("--models", default="",
                    help="в --debate: модель_a,модель_b,арбитр")
    ap.add_argument("--route", action="store_true",
                    help="в --auto: каждый пункт плана своему агенту")
    args = ap.parse_args(argv)

    overrides: dict[str, Any] = {
        k: getattr(args, k) for k in
        ("provider", "model", "workspace", "max_steps", "base_url",
         "sandbox_mode", "profile")
    }
    if args.db:
        overrides["db"] = args.db
    if args.skills:
        overrides["skills"] = [s.strip() for s in args.skills.split(",") if s.strip()]

    try:
        cfg = Config.load(args.config, **overrides)
    except Exception as exc:
        print(f"{C_ERR}Ошибка конфигурации: {exc}{C_OFF}", file=sys.stderr)
        return 2

    color = not args.no_color and sys.stdout.isatty()
    if args.check:
        return cmd_check(cfg)
    if args.smoke:
        from .smoke import smoke
        # Инструменты нужны все ходовые: без них проверять нечего.
        if not {"files", "python"} <= set(cfg.skills):
            cfg.skills = sorted(set(cfg.skills) | {"files", "python",
                                                   "memory"})
        mark, _ = smoke(cfg, args.verbose, args.only)
        return 0 if mark == "ГОДИТСЯ" else (1 if mark ==
                                            "ГОДИТСЯ С ОГОВОРКАМИ" else 2)
    if args.protocol:
        return cmd_protocol(cfg, args.protocol, args.summarize, color)
    if args.debates:
        return cmd_debates(cfg, color)
    if args.debate:
        goal = " ".join(args.task)
        if not goal and not args.resume:
            print(f"{C_ERR}Для --debate нужен вопрос{C_OFF}", file=sys.stderr)
            return 2
        return cmd_debate(cfg, goal, args.rounds,
                          args.max_usd if args.max_usd is not None
                          else cfg.max_usd,
                          args.models, args.verbose, color,
                          args.resume or 0, args.summarize)
    if args.runs is not None:
        return cmd_runs(cfg, args.runs, 20, color)
    if args.do:
        goal = " ".join(args.task)
        if not goal:
            print(f"{C_ERR}Для --do нужна задача{C_OFF}", file=sys.stderr)
            return 2
        return cmd_do(cfg, goal, args.verbose, color,
                      args.hours or cfg.max_hours,
                      args.iterations or cfg.max_iterations,
                      args.max_usd if args.max_usd is not None else cfg.max_usd,
                      args.yes)
    if args.auto or args.resume:
        goal = " ".join(args.task)
        if not goal and not args.resume:
            print(f"{C_ERR}Для --auto нужна цель{C_OFF}", file=sys.stderr)
            return 2
        return cmd_auto(cfg, goal,
                        args.hours or cfg.max_hours,
                        args.iterations or cfg.max_iterations,
                        args.resume, args.verbose, color,
                        args.max_usd if args.max_usd is not None
                        else cfg.max_usd,
                        args.route)
    try:
        if args.task:
            return cmd_run(cfg, " ".join(args.task), args.verbose, color)
        return cmd_chat(cfg, args.verbose, color)
    except KeyboardInterrupt:
        print("\nпрервано")
        return 130
    except Exception as exc:
        print(f"{C_ERR}Сбой: {type(exc).__name__}: {exc}{C_OFF}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
