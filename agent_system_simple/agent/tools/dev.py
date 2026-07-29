"""Прогон тестов и точечная правка кода.

Две вещи, которых не хватало агенту-разработчику.

ПЕРВАЯ: запустить тесты и увидеть, что упало. Через run_command это
делается, но вывод pytest на большом проекте — это тысячи строк, из
которых важны двадцать. Остальное вытесняет из окна саму задачу.
run_tests отдаёт ТОЛЬКО упавшее плюс итоговую строку.

ВТОРАЯ: apply_patch. edit_file требует уникального фрагмента и отвергает
правку, если текст встречается дважды, — модели на этом буксуют, повторяя
одну и ту же попытку. Здесь два запасных пути: унифицированный диф и
замена по номерам строк.

Общий принцип прежний: молчание не считается успехом. Пустой вывод
тестов — это «не удалось прогнать», а не «всё хорошо».
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from .base import Tool, ToolError, Workspace, clear_pycache

TIMEOUT = 600
MAX_OUT = 12_000

#: Признаки провала в выводе разных прогонщиков. Ищем и по коду
#: возврата, и по тексту: свои тесты проекта возвращают 0, даже когда
#: печатают «провалено: 2» — на этом легко обмануться.
FAIL_MARKS = re.compile(
    r"(?i)\b(FAILED|FAIL|ERROR|Traceback|AssertionError|"
    r"провалено:\s*[1-9]|не пройдено|failures=[1-9]|errors=[1-9])")

#: Строки, ради которых стоит показать контекст: итоги прогона.
SUMMARY = re.compile(
    r"(?i)(=+\s*\d+\s+(failed|passed|error)|"
    r"^пройдено:|^ran \d+ test|^ok$|^failed \(|"
    r"\d+\s+(passed|failed|error))")


def _cut(text: str) -> str:
    if len(text) <= MAX_OUT:
        return text
    return (text[:MAX_OUT // 2]
            + f"\n… обрезано {len(text) - MAX_OUT} символов …\n"
            + text[-MAX_OUT // 2:])


def _pick_failures(out: str) -> tuple[list[str], list[str]]:
    """Отобрать строки про провалы и строки-итоги.

    Показываем провал вместе с несколькими последующими строками: без
    них видно «FAILED test_x», но не видно, чем именно он упал.
    """
    lines = out.splitlines()
    keep: list[int] = []
    for i, ln in enumerate(lines):
        if FAIL_MARKS.search(ln):
            for j in range(max(0, i - 1), min(len(lines), i + 6)):
                keep.append(j)
    fails = [lines[i] for i in sorted(set(keep))]
    summary = [ln for ln in lines if SUMMARY.search(ln.strip())]
    return fails, summary


def build(ws: Workspace, timeout: int = TIMEOUT) -> list[Tool]:

    def _run(cmd: list[str], tmo: int) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(cmd, cwd=str(ws.root), capture_output=True,
                                  text=True, timeout=tmo,
                                  stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise ToolError(
                f"Тесты не уложились в {tmo} с и прерваны. Возможно, "
                "тест ждёт ввода или завис в цикле."
            ) from None
        except FileNotFoundError as exc:
            raise ToolError(f"Не запустить: {exc}") from exc

    def _detect() -> list[str]:
        """Чем прогонять, если не сказано явно."""
        if (ws.root / "Makefile").exists():
            mk = (ws.root / "Makefile").read_text(encoding="utf-8",
                                                  errors="replace")
            if re.search(r"^test:", mk, re.M):
                return ["make", "test"]
        if (ws.root / "tests").is_dir() or list(ws.root.glob("test_*.py")):
            try:
                import pytest                            # noqa: F401
                return [sys.executable, "-m", "pytest", "-q"]
            except ImportError:
                pass
            # свои тесты без pytest: запускаем файлы по очереди
            files = sorted(str(p.relative_to(ws.root))
                           for p in ws.root.glob("tests/test_*.py"))
            if files:
                return [sys.executable, files[0]]
        raise ToolError(
            "Не понятно, чем прогонять тесты: нет цели 'test' в Makefile, "
            "нет папки tests/ и файлов test_*.py. Задайте command явно.")

    def run_tests(command: str = "", timeout: int = 0,
                  full: bool = False) -> str:
        """Прогнать тесты и показать ТОЛЬКО упавшее."""
        tmo = timeout if timeout > 0 else TIMEOUT
        cmd = command.split() if command.strip() else _detect()
        # Кэш .pyc сносим ПЕРЕД прогоном. Иначе правка, не изменившая
        # размер файла (`w + h` -> `w * h`), не попадает в прогон: Python
        # считает старый .pyc свежим и выполняет прежний код. Ловилось
        # живьём — агент чинил функцию, а тест падал с той же ошибкой.
        clear_pycache(ws.root)
        proc = _run(cmd, tmo)
        out = (proc.stdout or "") + (proc.stderr or "")

        if not out.strip():
            # Молчание — не успех. Возможно, команда не та.
            return (f"[код возврата {proc.returncode}] Вывод ПУСТОЙ. "
                    f"Команда: {' '.join(cmd)}\n"
                    "Это не значит «тесты прошли» — скорее прогонщик не "
                    "запустился. Проверьте команду.")

        if full:
            return f"[код возврата {proc.returncode}]\n{_cut(out)}"

        fails, summary = _pick_failures(out)
        # Провалом считаем и ненулевой код, и найденные признаки в тексте:
        # самописные прогонщики нередко возвращают 0, печатая «провалено: 2».
        bad = proc.returncode != 0 or bool(fails)
        head = (f"[код возврата {proc.returncode}] "
                f"{'ЕСТЬ ПРОВАЛЫ' if bad else 'всё прошло'}\n"
                f"команда: {' '.join(cmd)}")
        if not bad:
            # Последняя строка вывода как запасной итог. Список может
            # оказаться пустым — обращение к [-1] тогда роняет инструмент.
            rest = out.strip().splitlines()
            tail = "\n".join(summary[-3:]) or (rest[-1] if rest else "")
            return f"{head}\n{tail}".rstrip()
        body = "\n".join(fails) if fails else out
        return (f"{head}\n\n── упавшее ──\n{_cut(body)}"
                + (f"\n\n── итог ──\n{chr(10).join(summary[-3:])}"
                   if summary else "")
                + "\n\nПолный вывод: run_tests с full=true")

    # ------------------------------------------------------------ патч
    def apply_patch(path: str, patch: str = "", start_line: int = 0,
                    end_line: int = 0, replacement: str = "") -> str:
        """Правка двумя способами: унифицированный диф или номера строк.

        Оба нужны, потому что edit_file отказывается работать, когда
        фрагмент встречается несколько раз, — а это обычное дело.
        """
        p = ws.resolve(path)
        if not p.exists():
            raise ToolError(f"Файл {path!r} не найден")
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines(keepends=True)

        if patch.strip():
            new_text, applied = _apply_unified(lines, patch)
            p.write_text(new_text, encoding="utf-8")
            return (f"Изменён {ws.relative(p)}: применено участков "
                    f"{applied}, строк стало {len(new_text.splitlines())}")

        if start_line <= 0:
            raise ToolError(
                "Задайте либо patch (унифицированный диф), либо "
                "start_line/end_line с replacement.")
        if start_line > len(lines) + 1:
            raise ToolError(
                f"В файле {len(lines)} строк, начать с {start_line} нельзя")
        end = end_line if end_line >= start_line else start_line
        end = min(end, len(lines))
        repl = replacement
        if repl and not repl.endswith("\n"):
            repl += "\n"
        was = "".join(lines[start_line - 1:end])
        new_lines = lines[:start_line - 1] + ([repl] if repl else []) \
            + lines[end:]
        p.write_text("".join(new_lines), encoding="utf-8")
        return (f"Изменён {ws.relative(p)}: строки {start_line}-{end} "
                f"заменены\nбыло ({len(was.splitlines())} строк): "
                f"{was[:200]!r}\nстало ({len(repl.splitlines())} строк): "
                f"{repl[:200]!r}")

    return [
        Tool("run_tests",
             "Прогнать тесты и увидеть ТОЛЬКО упавшее. Без аргументов сам "
             "находит, чем прогонять: make test, pytest или tests/. "
             "Полный вывод — full=true.",
             {"type": "object",
              "properties": {
                  "command": {"type": "string",
                              "description": "Команда, если нужна своя"},
                  "timeout": {"type": "integer"},
                  "full": {"type": "boolean",
                           "description": "Показать вывод целиком"}},
              "required": []},
             run_tests),
        Tool("apply_patch",
             "Правка файла, когда edit_file не берёт: унифицированный диф "
             "(patch) либо замена по номерам строк (start_line, end_line, "
             "replacement). Пустой replacement удаляет строки.",
             {"type": "object",
              "properties": {
                  "path": {"type": "string"},
                  "patch": {"type": "string",
                            "description": "Диф вида @@ -10,3 +10,4 @@"},
                  "start_line": {"type": "integer",
                                 "description": "Первая строка, с единицы"},
                  "end_line": {"type": "integer"},
                  "replacement": {"type": "string",
                                  "description": "Новый текст"}},
              "required": ["path"]},
             apply_patch),
    ]


def _apply_unified(lines: list[str], patch: str) -> tuple[str, int]:
    """Применить унифицированный диф.

    Своя реализация, потому что в стандартной библиотеке есть только
    построение дифа (difflib), но не наложение. Заголовки @@ проверяем
    по содержимому: модели часто ошибаются в номерах строк, а вот
    контекстные строки обычно копируют верно, поэтому при несовпадении
    ищем участок рядом, а не падаем.
    """
    out = list(lines)
    applied = 0
    hunks = re.split(r"(?m)^(?=@@)", patch)
    for hunk in hunks:
        if not hunk.startswith("@@"):
            continue
        head, _, body = hunk.partition("\n")
        m = re.match(r"@@\s*-(\d+)(?:,(\d+))?\s*\+(\d+)(?:,(\d+))?\s*@@", head)
        if not m:
            raise ToolError(f"Непонятный заголовок участка: {head[:60]!r}")
        start = int(m.group(1))

        old_block, new_block = [], []
        for ln in body.split("\n"):
            if not ln:
                continue
            tag, content = ln[0], ln[1:]
            if tag == " ":
                old_block.append(content)
                new_block.append(content)
            elif tag == "-":
                old_block.append(content)
            elif tag == "+":
                new_block.append(content)
            elif tag == "\\":       # «\ No newline at end of file»
                continue

        pos = _find_block(out, old_block, start - 1)
        if pos is None:
            raise ToolError(
                "Участок дифа не совпал с файлом. Перечитайте файл "
                f"и постройте диф заново. Искали:\n"
                + "\n".join(old_block[:4]))
        out[pos:pos + len(old_block)] = [x + "\n" for x in new_block]
        applied += 1

    if not applied:
        raise ToolError("В патче нет ни одного участка @@")
    text = "".join(out)
    return text, applied


def _find_block(lines: list[str], block: list[str], hint: int) -> int | None:
    """Найти участок: сначала по подсказке, затем поблизости, затем везде."""
    if not block:
        return None
    plain = [x.rstrip("\n") for x in lines]

    def match_at(i: int) -> bool:
        if i < 0 or i + len(block) > len(plain):
            return False
        return all(plain[i + k] == block[k] for k in range(len(block)))

    if match_at(hint):
        return hint
    for delta in range(1, 60):          # номера строк «уехали» на немного
        for cand in (hint - delta, hint + delta):
            if match_at(cand):
                return cand
    for i in range(len(plain)):         # последняя попытка: где угодно
        if match_at(i):
            return i
    return None
