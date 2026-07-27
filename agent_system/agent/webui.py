"""Веб-морда конфигов/логов: управление профилями и автономными прогонами.

Почему отдельный модуль, а не разбросано по server.py: server.py — это
транспорт (HTTP, JSON, авторизация), а здесь — логика, которую он
вызывает: чтение/запись файлов профилей с проверками (нельзя затереть
чужое поле мимо схемы, нельзя дать файлу выйти за пределы папки
profiles/) и управление ОДНИМ фоновым автономным прогоном на процесс
(AutorunManager). Те же файлы профилей читает agent/config.py — этот
модуль их не подменяет, а работает как второй писатель поверх того же
формата.

ПОЧЕМУ РОВНО ОДИН АВТОНОМНЫЙ ПРОГОН НА ПРОЦЕСС, А НЕ НЕСКОЛЬКО СРАЗУ:
автономный прогон часами пишет в ОДНУ базу (cfg.db) через AutoRunner,
которая ведёт план (task), события (event) и факты (fact) по run_id.
Два таких прогона одновременно из одного процесса — это два потока,
пишущих в общий SQLite-файл вперемешку, и что хуже — при использовании
resume/next_task для того же run_id они начнут забирать друг у друга
пункты плана. Явный отказ ("уже выполняется") на попытку запустить
второй — гораздо честнее тихой порчи состояния.
"""
from __future__ import annotations

import json
import queue
import re
import threading
from pathlib import Path
from typing import Any

from .autorun import AutoRunner
from .build import build_agent
from .config import Config
from .mcp import MCPPool
from .store import Store


#: имя профиля — только это можно положить в имя файла на диске;
#: то же ограничение, что негласно подразумевает Config.list_profiles()
#: (там просто *.json без проверки, здесь проверяем явно при ЗАПИСИ).
_PROFILE_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")

#: поля профиля, которые реально читает Config.apply_profile()/router.py —
#: остальное в JSON-файле — это ШУМ для системы (она его просто не читает),
#: поэтому дашборд отображает только их, но лишние ключи в файле не трогает
#: и не стирает при частичном сохранении (см. save_profile).
_PROFILE_FIELDS = ("name", "description", "keywords", "skills",
                   "max_steps", "system_prompt")


class WebUIError(Exception):
    """Ожидаемая ошибка управления (валидация, конфликт) — не трейсбек."""


# ============================================================ профили
def read_profile(name: str) -> dict[str, Any]:
    if not _PROFILE_NAME_RE.match(name):
        raise WebUIError(f"недопустимое имя профиля: {name!r}")
    f = Config.profiles_dir() / f"{name}.json"
    if not f.exists():
        raise WebUIError(f"профиль {name!r} не найден")
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WebUIError(f"профиль {name!r} повреждён: {exc}") from exc


def list_profiles_full() -> list[dict[str, Any]]:
    """Все профили целиком (для списка в дашборде) — не только имена,

    как Config.list_profiles(), а сразу name/description/skills, чтобы
    не делать N отдельных запросов с фронтенда.
    """
    out = []
    for name in Config.list_profiles():
        try:
            data = read_profile(name)
        except WebUIError:
            continue
        out.append({"file": name, **{k: data.get(k) for k in _PROFILE_FIELDS}})
    return out


def save_profile(name: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Сохранить (создать или обновить) профиль — ТОЛЬКО известные поля.

    Секреты в профиль класть некуда и незачем (см. общий принцип
    "секреты — только в .env", agent/config.py) — вся секция полей
    профиля состоит из skills/промпта/лимитов, ничего чувствительного.
    Неизвестные ключи из fields молча отбрасываются, а НЕИЗВЕСТНЫЕ
    ключи, УЖЕ бывшие в файле (например руками добавленный комментарий
    "_note"), сохраняются — частичное обновление не должно стирать то,
    что сюда не относится.
    """
    if not _PROFILE_NAME_RE.match(name):
        raise WebUIError(
            f"недопустимое имя профиля: {name!r} — разрешены буквы, "
            "цифры, '_', '-', начинается с буквы"
        )
    skills = fields.get("skills")
    if skills is not None and not isinstance(skills, list):
        raise WebUIError("skills должен быть списком строк")
    max_steps = fields.get("max_steps")
    if max_steps is not None:
        try:
            max_steps = int(max_steps)
        except (TypeError, ValueError):
            raise WebUIError("max_steps должен быть целым числом") from None
        if max_steps <= 0:
            raise WebUIError("max_steps должен быть положительным")

    f = Config.profiles_dir() / f"{name}.json"
    existing: dict[str, Any] = {}
    if f.exists():
        try:
            existing = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}

    merged = dict(existing)
    for key in _PROFILE_FIELDS:
        if key in fields:
            merged[key] = fields[key]
    merged["name"] = name          # имя файла и поле "name" не расходятся
    if max_steps is not None:
        merged["max_steps"] = max_steps

    # Проверка ДО записи на диск: битый профиль не должен ронять
    # известные Config.list_profiles()/apply_profile() у уже бегущего
    # сервера — лучше отказ здесь, чем сюрприз в следующем /run.
    try:
        json.dumps(merged, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise WebUIError(f"профиль не сериализуется в JSON: {exc}") from exc

    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
    return merged


def delete_profile(name: str) -> None:
    if not _PROFILE_NAME_RE.match(name):
        raise WebUIError(f"недопустимое имя профиля: {name!r}")
    f = Config.profiles_dir() / f"{name}.json"
    if not f.exists():
        raise WebUIError(f"профиль {name!r} не найден")
    f.unlink()


# ======================================================= .env-редактор
# ГЛАВНЫЙ ПРИНЦИП, как и в остальной системе: секреты храним ТОЛЬКО в
# .env, никогда в JSON. Этот раздел не меняет это правило, а даёт
# управляемый способ редактировать сам .env через дашборд вместо ручного
# редактирования файла по SSH — с маскировкой при чтении и обязательным
# подтверждением перед записью секрета (тот же приём, что confirm_sends
# в messaging: случайно отправленное здесь тоже не отменить).
#
# ЧЕСТНОЕ ОГРАНИЧЕНИЕ: os.getenv() в agent/config.py читается один раз,
# при Config.load() на старте процесса. Запись .env НЕ подхватывается
# уже работающим сервером на лету — save_env_vars() всегда возвращает
# restart_required=True, и дашборд обязан показать это пользователю, а
# не создавать иллюзию мгновенного применения.
ENV_SECRET_RE = re.compile(r"(_KEY|_PASSWORD|_TOKEN|_SECRET|_DSN)$")

_KEY_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def env_file_path() -> Path:
    """Путь к .env — рядом с .env.example, в корне agent_system/.

    Функция, а не константа: тесты подменяют её (как Config.profiles_dir
    для профилей), чтобы не трогать реальный .env репозитория.
    """
    return Path(__file__).resolve().parent.parent / ".env"


def env_example_path() -> Path:
    return Path(__file__).resolve().parent.parent / ".env.example"


def _split_value_and_comment(rest: str) -> tuple[str, str]:
    """rest — всё после '=' в строке KEY=rest.

    Инлайн-комментарий ищем по последовательности ' #' (пробел + решётка),
    как их оформляет .env.example — так отличаем реальный комментарий
    от '#', который теоретически мог быть частью самого значения (URL с
    фрагментом и т.п.). Возвращает (значение, хвост_с_комментарием) —
    хвост включает исходные пробелы перед '#', чтобы при перезаписи
    сохранить выравнивание.
    """
    idx = rest.find(" #")
    if idx == -1:
        return rest.strip(), ""
    return rest[:idx].strip(), rest[idx:]


def _set_line_value(line: str, new_value: str) -> str:
    """Заменить только значение в строке KEY=... , не трогая комментарий."""
    m = _KEY_LINE_RE.match(line.strip())
    if not m:
        return line
    key, rest = m.group(1), m.group(2)
    _, comment = _split_value_and_comment(rest)
    return f"{key}={new_value}{comment}"


def _parse_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        m = _KEY_LINE_RE.match(s)
        if not m:
            continue
        value, _ = _split_value_and_comment(m.group(2))
        # снимаем внешние кавычки, если значение целиком в них — обычная
        # практика .env-файлов (VALUE="с пробелами")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[m.group(1)] = value
    return out


def _read_template_vars() -> list[dict[str, str]]:
    """Имена/описания/примеры значений — источник истины: .env.example.

    Комментарии над переменной (contiguous блок строк с '#', без пустой
    строки между ними и объявлением) становятся её описанием в дашборде.
    """
    path = env_example_path()
    if not path.exists():
        return []
    out: list[dict[str, str]] = []
    pending: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        m = _KEY_LINE_RE.match(s) if s and not s.startswith("#") else None
        if m:
            default_value, _ = _split_value_and_comment(m.group(2))
            comment = " ".join(c.lstrip("#").strip() for c in pending if c.strip())
            out.append({"name": m.group(1), "comment": comment,
                       "example_default": default_value})
            pending = []
        elif s.startswith("#"):
            pending.append(s)
        elif not s:
            pending = []
    return out


def list_env_vars() -> list[dict[str, Any]]:
    """Текущее состояние переменных окружения проекта — для дашборда.

    Значения: из реального .env, если он есть, иначе — пример из
    .env.example (это НЕ секреты, а разумные значения по умолчанию вроде
    AGENT_SANDBOX=auto, их незачем скрывать). Секреты (см. ENV_SECRET_RE)
    на чтение никогда не отдаются целиком — только факт "задано/не
    задано", тот же принцип, что маскировка в Config.to_dict().
    """
    templates = _read_template_vars()
    known_names = {t["name"] for t in templates}
    existing = _parse_dotenv(env_file_path())

    out: list[dict[str, Any]] = []
    for t in templates:
        name = t["name"]
        raw_value = existing.get(name, t["example_default"])
        secret = bool(ENV_SECRET_RE.search(name))
        out.append({
            "name": name, "comment": t["comment"],
            "is_secret": secret, "is_set": bool(raw_value),
            "value": "" if secret else raw_value,
        })
    # переменные, реально заданные в .env, но отсутствующие в
    # .env.example — не прячем, показываем отдельно с пометкой custom
    for name, val in existing.items():
        if name in known_names:
            continue
        secret = bool(ENV_SECRET_RE.search(name))
        out.append({"name": name, "comment": "(нет в .env.example)",
                   "is_secret": secret, "is_set": bool(val),
                   "value": "" if secret else val, "custom": True})
    return out


def save_env_vars(values: dict[str, str] | None = None,
                  clear: list[str] | None = None,
                  confirm: bool = False) -> dict[str, Any]:
    """Записать новые значения в .env.

    Изменяются ТОЛЬКО строго значения запрошенных ключей — комментарии,
    порядок и значения остальных переменных не трогаются (см.
    _set_line_value). Если .env ещё не существует, за основу берётся
    .env.example — так первое сохранение не лишает пользователя всех
    пояснений в файле.

    Ключ должен быть либо описан в .env.example, либо уже реально
    существовать в текущем .env — заводить С ДАШБОРДА произвольные новые
    имена переменных, не относящиеся к проекту, не даём: это сузило бы
    поверхность для случайной опечатки в имени переменной, которая потом
    незаметно ничего не будет делать.
    """
    values = dict(values or {})
    clear = list(clear or [])
    templates = _read_template_vars()
    known_names = {t["name"] for t in templates}
    existing = _parse_dotenv(env_file_path())
    known_names |= set(existing)

    requested = list(values) + clear
    unknown = [k for k in requested if k not in known_names]
    if unknown:
        raise WebUIError(
            f"неизвестные переменные: {', '.join(sorted(unknown))} — через "
            "дашборд можно менять только переменные, уже описанные в "
            ".env.example или реально присутствующие в .env"
        )

    touches_secret = bool(clear) or any(
        ENV_SECRET_RE.search(k) for k in values)
    if touches_secret and not confirm:
        raise WebUIError(
            "изменение или очистка секрета требует явного подтверждения "
            "(confirm=true в теле запроса) — как и отправка сообщений в "
            "messaging, случайно отправленное изменение здесь не отменить"
        )

    if env_file_path().exists():
        base_text = env_file_path().read_text(encoding="utf-8")
    elif env_example_path().exists():
        base_text = env_example_path().read_text(encoding="utf-8")
    else:
        base_text = ""

    seen: set[str] = set()
    out_lines: list[str] = []
    for raw in base_text.splitlines():
        s = raw.strip()
        m = _KEY_LINE_RE.match(s) if s and not s.startswith("#") else None
        if not m:
            out_lines.append(raw)
            continue
        name = m.group(1)
        seen.add(name)
        if name in clear:
            out_lines.append(_set_line_value(raw, ""))
        elif name in values:
            out_lines.append(_set_line_value(raw, values[name].strip()))
        else:
            out_lines.append(raw)   # не трогаем — сохраняем и инлайн-комментарий

    new_names = [k for k in values if k not in seen]
    if new_names:
        out_lines.append("")
        out_lines.append("# --- добавлено через дашборд ---")
        for k in new_names:
            out_lines.append(f"{k}={values[k].strip()}")

    env_file_path().parent.mkdir(parents=True, exist_ok=True)
    env_file_path().write_text("\n".join(out_lines).rstrip("\n") + "\n",
                               encoding="utf-8")
    return {"saved": sorted(set(values) | set(clear)), "restart_required": True}


# ================================================= автономный прогон
class AutorunManager:
    """Один активный автономный прогон на процесс + рассылка событий

    подписчикам (SSE/NDJSON-стриму дашборда) — тот же принцип, что
    server.py._run_stream, но прогон живёт дольше одного HTTP-запроса
    и переживает переподключение клиента (страницу дашборда можно
    закрыть и открыть заново, прогон продолжается).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._subscribers: list[queue.Queue] = []
        self._status: dict[str, Any] = {"state": "idle"}
        self._history: list[dict[str, Any]] = []   # последние события, для
                                                    # клиента, подключившегося
                                                    # уже ПОСЛЕ старта прогона
        self._history_limit = 200

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    # --- подписка на события (для /api/auto/stream) -------------------
    def subscribe(self) -> tuple[queue.Queue, list[dict[str, Any]]]:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self._lock:
            self._subscribers.append(q)
            backlog = list(self._history)
        return q, backlog

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _broadcast(self, kind: str, data: dict[str, Any]) -> None:
        payload = {"event": kind, **data}
        with self._lock:
            self._history.append(payload)
            if len(self._history) > self._history_limit:
                self._history = self._history[-self._history_limit:]
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass  # медленный клиент отстаёт — не блокируем прогон ради него

    # --- управление --------------------------------------------------
    def start(self, cfg: Config, goal: str, profile: str | None,
             hours: float, iterations: int) -> int:
        """Запустить прогон, вернуть run_id как только он появился в базе

        (ждём реальное событие start/resume от AutoRunner, а не гадаем).
        """
        goal = (goal or "").strip()
        if not goal:
            raise WebUIError("нужна цель (goal)")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise WebUIError(
                    "уже выполняется автономный прогон — сначала остановите "
                    "его (POST /api/auto/stop) или дождитесь завершения"
                )
            store = Store(cfg.db)
            run_cfg = Config(**{**cfg.__dict__})
            if profile:
                run_cfg.apply_profile(profile)
            stop_event = threading.Event()
            self._stop_event = stop_event
            self._history = []
            self._status = {"state": "starting", "goal": goal,
                           "profile": run_cfg.profile, "run_id": 0}
            ready = threading.Event()
            pool = (MCPPool(run_cfg.mcp)
                   if ("mcp" in run_cfg.skills and run_cfg.mcp) else None)

            def on_event(kind: str, data: dict[str, Any]) -> None:
                self._broadcast(kind, data)
                with self._lock:
                    if kind in ("start", "resume"):
                        self._status["run_id"] = data.get("run_id", 0)
                        self._status["state"] = "running"
                        ready.set()
                    elif kind == "iteration":
                        self._status["iteration"] = data.get("n")
                        self._status["task"] = data.get("task")
                    elif kind == "finish":
                        self._status["summary"] = data.get("summary")

            def worker() -> None:
                try:
                    runner = AutoRunner(
                        lambda: build_agent(
                            run_cfg, store=store,
                            run_id_getter=lambda: self._status.get("run_id", 0),
                            mcp_pool=pool),
                        store, max_hours=hours, max_iterations=iterations,
                        on_event=on_event, stop_event=stop_event)
                    res = runner.run(goal, run_cfg.profile)
                    with self._lock:
                        self._status.update(state="done",
                                            stopped_by=res.stopped_by,
                                            summary=res.summary)
                    self._broadcast("done", {"stopped_by": res.stopped_by,
                                             "summary": res.summary})
                except Exception as exc:
                    with self._lock:
                        self._status.update(state="error", error=str(exc))
                    self._broadcast("error", {"message": str(exc)})
                    ready.set()   # разбудить start(), если ошибка ДО start-события
                finally:
                    if pool:
                        pool.close()
                    store.close()

            t = threading.Thread(target=worker, daemon=True)
            self._thread = t
            t.start()

        ready.wait(timeout=15)
        return int(self.status().get("run_id") or 0)

    def stop(self) -> None:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            ev = self._stop_event
        if not running or ev is None:
            raise WebUIError("автономный прогон сейчас не выполняется")
        ev.set()
        self._broadcast("stopping", {})
