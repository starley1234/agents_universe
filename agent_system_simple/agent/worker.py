"""Фоновый исполнитель: берёт задания из очереди и работает.

Ради чего это написано. Раньше работа жила ровно столько, сколько
открыт терминал: закрыл SSH — прогон умер, перезагрузили сервер —
всё потеряно. Поставить задачу на ночь было нельзя.

Теперь: задание кладётся в очередь (из CLI, из веба, откуда угодно),
исполнитель подхватывает его и ведёт до конца. Он переживает выход
пользователя, а под systemd — и перезагрузку сервера.

Три вещи, без которых это опасно:

  ОТМЕТКА ЖИЗНИ. Исполнитель раз в минуту пишет в базу «я жив».
  Умер по любой причине — задание через 5 минут вернётся в очередь и
  достанется следующему. Без этого «running» висело бы вечно.

  ПРЕДЕЛ ПОПЫТОК. Задание, которое трижды уронило исполнителя, больше
  не берётся: дело в нём самом, а не в случайном сбое. Иначе очередь
  крутит один и тот же сломанный прогон до конца дней.

  ОСТАНОВКА НА ХОДУ. Человек может снять задание из веба; исполнитель
  замечает это между шагами и прекращает работу, а не игнорирует.
"""
from __future__ import annotations

import os
import socket
import time
import traceback
from typing import Any, Callable

from .autorun import AutoRunner
from .build import build_agent
from .config import Config, replace_profile
from .store import Store

#: Как часто отмечаться живым.
BEAT_EVERY = 60.0

#: Через сколько молчания задание считается брошенным.
STALE_AFTER = 300.0

#: Причины, по которым работу СТОИТ продолжить следующим подходом.
#: «done» — закончено, «blocked» — ждёт человека, остальное — просто
#: конец подхода: время, деньги, застой, тупик в плане.
RETRY_REASONS = ("time", "iterations", "budget", "stuck", "deadlock", "error")

#: Пауза перед повтором, если за подход НЕ прибавилось сделанных
#: пунктов. Долбить одно и то же без остановки — сжигать деньги.
RETRY_PAUSE = 60.0

#: Пауза, когда очередь пуста. Больше — дольше ждать старта задания,
#: меньше — лишние обращения к базе на простое.
IDLE_SLEEP = 3.0


def _progress(store: Store, run_id: int) -> str:
    """Отпечаток продвижения: сколько пунктов закрыто.

    Нужен, чтобы отличить «подход дал результат» от «топчемся на месте».
    Во втором случае перед следующим подходом делаем паузу.
    """
    if not run_id:
        return ""
    rows = store.tasks(run_id)
    done = sum(1 for t in rows if t["status"] in ("done", "skipped"))
    return f"{done}/{len(rows)}"


def _done_count(progress: str) -> int:
    """Сколько пунктов закрыто по отпечатку вида «2/6»."""
    try:
        return int(str(progress or "").split("/")[0])
    except (ValueError, IndexError):
        return 0


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class Worker:
    """Один исполнитель. Несколько штук могут работать параллельно."""

    def __init__(self, cfg: Config, poll: float = IDLE_SLEEP,
                 once: bool = False,
                 on_event: Callable[[str, dict[str, Any]], None] | None = None
                 ) -> None:
        self.cfg = cfg
        self.poll = poll
        self.once = once            # взять одно задание и выйти
        self.me = worker_id()
        self.store = Store(cfg.db)
        self.on_event = on_event or (lambda k, d: None)
        self.stopping = False

    def _emit(self, kind: str, **data: Any) -> None:
        try:
            self.on_event(kind, data)
        except Exception:
            pass

    # ------------------------------------------------------------ цикл
    def run(self) -> int:
        """Крутиться, пока не остановят. Возвращает число заданий."""
        self._emit("worker_start", worker=self.me, db=self.cfg.db)
        done = 0
        last_sweep = 0.0
        while not self.stopping:
            # Подбираем задания, чей исполнитель умер. Раз в минуту:
            # чаще незачем, а реже — задание долго лежит мёртвым.
            if time.time() - last_sweep > 60:
                revived = self.store.revive_stale_jobs(STALE_AFTER)
                if revived:
                    self._emit("revived", count=revived)
                last_sweep = time.time()

            job = self.store.take_job(self.me)
            if job is None:
                if self.once:
                    break
                time.sleep(self.poll)
                continue

            self._emit("job_start", job_id=job["id"], goal=job["goal"],
                       attempt=job["attempts"])
            try:
                self._do(job)
                done += 1
            except Exception as exc:            # одно задание не валит всё
                self.store.finish_job(
                    job["id"], "failed",
                    f"{type(exc).__name__}: {exc}\n"
                    + traceback.format_exc()[-1500:])
                self._emit("job_failed", job_id=job["id"], error=str(exc))
            if self.once:
                break
        self._emit("worker_stop", worker=self.me, done=done)
        return done

    # -------------------------------------------------------- задание
    def _do(self, job: dict[str, Any]) -> None:
        cfg = replace_profile(self.cfg, job["profile"]) if job["profile"] \
            else Config(**{**self.cfg.__dict__})
        cfg.sandbox = self.cfg.sandbox

        rid = {"v": 0}
        last_beat = [time.time()]

        def factory(profile: str | None = None):
            use = replace_profile(cfg, profile) if profile and \
                profile != cfg.profile else cfg
            return build_agent(use, store=self.store,
                               run_id_getter=lambda: rid["v"])

        def on_event(kind: str, data: dict[str, Any]) -> None:
            # Отметка жизни привязана к событиям прогона: пока агент
            # что-то делает, задание считается живым.
            now = time.time()
            if now - last_beat[0] > BEAT_EVERY:
                self.store.beat_job(job["id"], rid["v"] or None)
                last_beat[0] = now
            if kind == "iteration":
                # Человек мог снять задание из веба, пока оно шло.
                fresh = self.store.get_job(job["id"])
                if fresh and fresh["status"] == "stopped":
                    self.stopping_job = True
                    raise KeyboardInterrupt("задание снято человеком")
            self._emit(kind, job_id=job["id"], **data)

        # Человек ушёл — решать по ходу больше некому. Оркестратор
        # смотрит на результат каждого шага и правит курс.
        boss = None
        if bool(job["decompose"]):
            from .llm import build_llm
            from .orchestrator import Orchestrator
            model = getattr(cfg, "model_strong", "") or cfg.model
            try:
                boss = Orchestrator(
                    build_llm(cfg.provider, model, base_url=cfg.base_url,
                              api_key=cfg.api_key,
                              temperature=cfg.temperature),
                    Config.profile_hints())
            except Exception:
                boss = None        # без оркестратора работа всё равно идёт

        runner = AutoRunner(
            factory, self.store,
            max_hours=float(job["hours"] or 1.0),
            max_iterations=int(getattr(cfg, "max_iterations", 50)),
            max_usd=float(job["max_usd"] or 0.0),
            route_tasks=bool(job["decompose"]),
            decompose=bool(job["decompose"]),
            known_profiles=Config.list_profiles(),
            profile_hints=Config.profile_hints(),
            orchestrator=boss,
            on_event=on_event)

        orig_start = self.store.start_run

        def start(goal: str, profile: str | None = None) -> int:
            rid["v"] = orig_start(goal, profile)
            self.store.beat_job(job["id"], rid["v"])
            return rid["v"]

        self.store.start_run = start        # type: ignore[assignment]
        try:
            res = runner.run(job["goal"], job["profile"] or None,
                             resume=job["run_id"] or None)
            # Прогон закончился — но, возможно, только этот подход.
        except KeyboardInterrupt:
            self.store.finish_job(job["id"], "stopped",
                                  "остановлено человеком")
            self._emit("job_stopped", job_id=job["id"])
            return
        finally:
            self.store.start_run = orig_start   # type: ignore[assignment]

        # Различаем ТРИ исхода, а не два. Раньше всё, кроме «done»,
        # считалось провалом: кончилось время — задание брошено с
        # одним пунктом из пяти. Это и есть «поставил и проверяй».
        if res.stopped_by == "done":
            self.store.finish_job(job["id"], "done", res.summary)
            self._emit("job_done", job_id=job["id"], status="done",
                       run_id=res.run_id, summary=res.summary)
            self._notify(job, res.summary, "done")
            return

        if res.stopped_by in RETRY_REASONS:
            # Работа не закончена, но и не провалена: продолжим следующим
            # подходом с того же прогона.
            progress = _progress(self.store, res.run_id)
            # Сравниваем ЧИСЛО сделанного, а не строку. Первый подход
            # даёт '0/3' против пустого '' — формально «изменилось»,
            # хотя не сделано ничего, и паузы не было бы.
            moved = _done_count(progress) > _done_count(job["progress"])
            delay = 0.0 if moved else RETRY_PAUSE
            more = self.store.continue_job(
                job["id"], res.summary, progress, delay)
            self._emit("job_continue" if more else "job_failed",
                       job_id=job["id"], run_id=res.run_id,
                       reason=res.stopped_by, progress=progress,
                       moved=moved, error=""
                       if more else "подходы исчерпаны")
            if not more:
                self._notify(job, res.summary, "failed")
            return

        # blocked — ждёт ответа человека; продолжать бессмысленно.
        self.store.finish_job(job["id"], "failed", res.summary)
        self._emit("job_failed", job_id=job["id"],
                   error=f"остановлено: {res.stopped_by}")
        self._notify(job, res.summary, "failed")

    # ------------------------------------------------------ уведомление
    def _notify(self, job: dict[str, Any], summary: str,
                status: str) -> None:
        """Сообщить человеку, что задание закончилось.

        Смысл всей затеи — поставить и забыть. Забыть можно, только
        если система сама позовёт, когда всё готово.
        """
        where = (job["notify"] or "").strip()
        if not where:
            return
        head = ("✅ Задание выполнено" if status == "done"
                else "⚠️ Задание завершилось с ошибкой")
        text = f"{head} #{job['id']}\n{job['goal']}\n\n{summary[:1500]}"
        try:
            from .skills.comms import build as comms_build
            from .tools.base import Workspace
            tools = {t.name: t for t in comms_build(
                Workspace(self.cfg.workspace),
                getattr(self.cfg, "comms", None))}
            if "@" in where and "send_email" in tools:
                tools["send_email"].fn(to=where,
                                       subject=f"{head} #{job['id']}",
                                       body=text)
            elif where.startswith("tg:") and "send_telegram" in tools:
                tools["send_telegram"].fn(text=text, chat=where[3:])
            elif where.startswith("max:") and "send_max" in tools:
                tools["send_max"].fn(text=text, chat=where[4:])
            self._emit("notified", job_id=job["id"], where=where)
        except Exception as exc:
            # Не смогли позвать — не повод считать задание проваленным.
            self._emit("notify_failed", job_id=job["id"], error=str(exc))


def main(argv: list[str] | None = None) -> int:
    import argparse
    from .config import load_dotenv
    load_dotenv()

    ap = argparse.ArgumentParser(
        prog="agent-worker",
        description="Фоновый исполнитель заданий из очереди")
    ap.add_argument("-c", "--config")
    ap.add_argument("--db")
    ap.add_argument("--once", action="store_true",
                    help="взять одно задание и выйти")
    ap.add_argument("--poll", type=float, default=IDLE_SLEEP)
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    cfg = Config.load(args.config)
    if args.db:
        cfg.db = args.db

    def log(kind: str, data: dict[str, Any]) -> None:
        if args.quiet and kind not in ("job_start", "job_done", "job_failed",
                                       "job_continue", "worker_start",
                                       "revived", "orchestrate"):
            return
        stamp = time.strftime("%H:%M:%S")
        if kind == "worker_start":
            print(f"[{stamp}] исполнитель {data['worker']} · база "
                  f"{data['db']}", flush=True)
        elif kind == "job_start":
            print(f"[{stamp}] ▶ задание #{data['job_id']}: {data['goal']}"
                  + (f" (попытка {data['attempt']})"
                     if data["attempt"] > 1 else ""), flush=True)
        elif kind == "job_done":
            print(f"[{stamp}] ✔ #{data['job_id']} {data['status']}",
                  flush=True)
        elif kind == "orchestrate":
            print(f"[{stamp}]   ⚙ {data.get('explain') or data.get('action')}",
                  flush=True)
        elif kind == "job_continue":
            print(f"[{stamp}] ↻ #{data['job_id']} подход окончен "
                  f"({data['reason']}), сделано {data['progress'] or '—'}"
                  + ("" if data["moved"] else ", без продвижения — пауза")
                  + " — продолжу", flush=True)
        elif kind == "job_failed":
            print(f"[{stamp}] ✖ #{data['job_id']}: {data['error'][:120]}",
                  flush=True)
        elif kind == "revived":
            print(f"[{stamp}] возвращено в очередь: {data['count']}",
                  flush=True)
        elif kind == "iteration":
            print(f"[{stamp}]   · {data.get('task', '')[:70]}", flush=True)

    w = Worker(cfg, poll=args.poll, once=args.once, on_event=log)
    try:
        w.run()
    except KeyboardInterrupt:
        print("\nостановлен")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
