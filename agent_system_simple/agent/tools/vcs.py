"""Снимки рабочей папки через git: страховка перед правками.

Зачем: первый прогон на реальной папке страшно запускать именно потому,
что откатить нечего. Здесь откат есть — и он не зависит от того, вёл ли
пользователь git сам.

Как устроено:

  * репозиторий заводится ОТДЕЛЬНЫЙ, в .agent-git внутри рабочей папки
    (GIT_DIR), рабочее дерево — сама папка. Чужой .git не трогаем: если
    у пользователя свой репозиторий, история и индекс остаются его.
  * снимок делается принудительно (`add -A --force`), включая файлы из
    .gitignore пользователя: смысл снимка — вернуть ВСЁ как было. Но
    --force отменяет и наши исключения, поэтому служебные каталоги
    отсекаются pathspec-ом ':(exclude)…' — он действует всегда.
  * откат восстанавливает дерево из снимка и удаляет появившееся после —
    это `checkout` плюс `clean -fd`.

Границы честно: снимок берётся из рабочей папки агента и только из неё.
Изменения вне workspace git не видит и вернуть не может.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .base import Tool, ToolError, Workspace

GIT_DIR = ".agent-git"       # своя история, чужой .git не трогаем
TIMEOUT = 60
MAX_OUT = 8_000

#: Что в снимок не берём ни при каких условиях. Именно pathspec, а не
#: info/exclude: с ключом --force правила исключения игнорируются, и
#: служебный каталог самого git попадал в снимок (проверено тестом).
SKIP = (f":(exclude){GIT_DIR}/**", ":(exclude)**/__pycache__/**",
        ":(exclude)**/*.pyc")


def _short(text: str) -> str:
    return text if len(text) <= MAX_OUT else text[:MAX_OUT] + "\n… обрезано"


def git_available() -> bool:
    try:
        r = subprocess.run(["git", "--version"], capture_output=True,
                           timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class Repo:
    """Обёртка над отдельным репозиторием снимков."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.gitdir = root / GIT_DIR

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", f"--git-dir={self.gitdir}",
                 f"--work-tree={self.root}", *args],
                capture_output=True, text=True, timeout=TIMEOUT,
                cwd=str(self.root))
        except FileNotFoundError as exc:
            raise ToolError("git не установлен — снимки недоступны") from exc
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"git не уложился в {TIMEOUT} с") from exc

    @property
    def ready(self) -> bool:
        return self.gitdir.is_dir()

    def init(self) -> str:
        if self.ready:
            return "уже был"
        r = self._run("init", "--quiet")
        if r.returncode != 0:
            raise ToolError(f"git init не удался: {_short(r.stderr)}")
        # Имя автора обязательно, иначе commit падает на чистой машине.
        self._run("config", "user.email", "agent@localhost")
        self._run("config", "user.name", "agent")
        return "создан"

    def _has_head(self) -> bool:
        return self._run("rev-parse", "--verify", "HEAD").returncode == 0

    def commit(self, message: str) -> tuple[bool, str]:
        """Вернёт (сделан ли новый снимок, короткий хеш).

        «Нечего фиксировать» определяем сравнением индекса, а не разбором
        текста git: формулировки меняются от версии и языка системы, и
        такая проверка тихо ломается.
        """
        self.init()
        # -A с --force: снимок должен покрывать и игнорируемые файлы,
        # иначе «откат» вернёт не всё.
        self._run("add", "-A", "--force", ".", *SKIP)
        if self._has_head() and self._run("diff", "--cached", "--quiet",
                                          "HEAD").returncode == 0:
            return False, self.head()
        r = self._run("commit", "--quiet", "--allow-empty-message",
                      "-m", message[:200])
        if r.returncode != 0:
            raise ToolError(f"снимок не сделан: {_short(r.stderr or r.stdout)}")
        return True, self.head()

    def head(self) -> str:
        return self._run("rev-parse", "--short", "HEAD").stdout.strip()


def build(ws: Workspace, auto: bool = True) -> list[Tool]:
    """auto=True — ядро само делает снимок перед каждым шагом."""
    repo = Repo(ws.root)

    def snapshot(message: str = "снимок") -> str:
        changed, h = repo.commit(message)
        if not changed:
            return f"Изменений нет, снимок не нужен (последний: {h or '—'})"
        return f"Снимок {h}: {message[:120]}"

    def changes(against: str = "") -> str:
        """Что изменилось после снимка (по умолчанию — после последнего)."""
        if not repo.ready:
            return ("Снимков ещё нет. Сделай snapshot перед правками — "
                    "тогда будет с чем сравнивать.")
        repo._run("add", "-A", "--force", ".", *SKIP)
        ref = against.strip() or "HEAD"
        stat = repo._run("diff", "--cached", "--stat", ref)
        if stat.returncode != 0:
            raise ToolError(f"не сравнить с {ref!r}: {_short(stat.stderr)}")
        body = stat.stdout.strip()
        if not body:
            return f"С момента {ref} изменений нет"
        patch = repo._run("diff", "--cached", ref).stdout
        return _short(f"{body}\n\n{patch}")

    def snapshots(limit: int = 10) -> str:
        if not repo.ready:
            return "Снимков ещё нет"
        r = repo._run("log", f"-{max(1, min(limit, 50))}",
                      "--format=%h  %ad  %s", "--date=format:%H:%M:%S")
        return r.stdout.strip() or "Снимков ещё нет"

    def revert(to: str = "") -> str:
        """Вернуть папку к снимку. Пусто = к предыдущему (HEAD~1)."""
        if not repo.ready:
            raise ToolError("Снимков нет — откатывать не к чему")
        ref = to.strip() or "HEAD~1"
        target = repo._run("rev-parse", "--short", f"{ref}^{{commit}}")
        if target.returncode:
            raise ToolError(
                f"Снимка {ref!r} нет. Список — инструментом snapshots.")
        # Текущее состояние сохраняем ПЕРЕД откатом: откат по ошибке
        # не должен уничтожать работу безвозвратно. Если менять нечего,
        # текущее состояние уже лежит в последнем снимке.
        repo.commit(f"перед откатом к {ref}")
        back = repo.head()
        # read-tree --reset -u, а не checkout: checkout восстанавливает
        # файлы из снимка, но НЕ убирает те, что появились после и попали
        # в отслеживаемые. Мусор оставался лежать (поймано тестом).
        r = repo._run("read-tree", "--reset", "-u", target.stdout.strip())
        if r.returncode != 0:
            raise ToolError(f"откат не удался: {_short(r.stderr)}")
        # неотслеживаемое подчищаем отдельно, храня своё хранилище снимков
        repo._run("clean", "-fdq", ".", f":(exclude){GIT_DIR}/**")
        # Фиксируем откат отдельным снимком, чтобы HEAD совпадал с папкой.
        repo.commit(f"откат к {target.stdout.strip()}")
        return (f"Папка возвращена к снимку {target.stdout.strip()}. "
                f"Состояние до отката сохранено в снимке {back} — "
                f"передумаешь, вызови revert с to={back}.")

    tools = [
        Tool("snapshot",
             "Сохранить снимок рабочей папки, чтобы можно было вернуться. "
             "Делай перед рискованной правкой." +
             (" Снимок перед каждым шагом делается автоматически."
              if auto else ""),
             {"type": "object",
              "properties": {"message": {"type": "string",
                                         "description": "Чем примечателен"}},
              "required": []},
             snapshot),
        Tool("changes",
             "Показать, что изменилось в папке после снимка: список файлов "
             "и различия. Так проверяют объём собственных правок.",
             {"type": "object",
              "properties": {"against": {"type": "string",
                                         "description": "Хеш снимка, "
                                                        "по умолчанию последний"}},
              "required": []},
             changes),
        Tool("snapshots",
             "Список сохранённых снимков: хеш, время, описание.",
             {"type": "object",
              "properties": {"limit": {"type": "integer"}},
              "required": []},
             snapshots),
        Tool("revert",
             "Вернуть рабочую папку к снимку, отменив правки после него. "
             "Без аргумента — к предыдущему снимку. Состояние до отката "
             "тоже сохраняется, откат обратим.",
             {"type": "object",
              "properties": {"to": {"type": "string",
                                    "description": "Хеш снимка"}},
              "required": []},
             revert,
             dangerous=True),
    ]
    return tools
