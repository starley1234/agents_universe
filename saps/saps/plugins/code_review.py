"""Плагин ревью кода на соответствие DO-178C (ТЗ п.3.4).

ЧТО ЭТО ДЕЛАЕТ И ЧЕГО НЕ ДЕЛАЕТ — важно понимать до применения.

DO-178C (у нас — КТ-178С) сертифицирует ПРОЦЕСС разработки ПО, а не
исходный текст программы. Ни один инструмент не может «проверить код на
соответствие DO-178C»: соответствие доказывается набором процессов,
данных жизненного цикла и прослеживаемостью от требований к коду и
тестам. Поэтому плагин честно решает ДВЕ ПОДЗАДАЧИ, которые
автоматизируются:

  1. ПРОСЛЕЖИВАЕМОСТЬ «требование -> код» (DO-178C, цели A-4/A-5).
     Ищет в исходниках ссылки на требования — комментарии вида
     `[REQ-123]`, `@requirement REQ-123`, `Требование: REQ-123`. Строит
     двустороннюю картину: у какого требования нет реализации и какой
     код не привязан ни к одному требованию.
  2. ФОРМАЛЬНЫЕ ПРИЗНАКИ, которые в стандарте прямо ограничены для
     уровней A/B: рекурсия, динамическая память в рантайме, goto,
     функции-гиганты, отсутствие обработки ошибок. Это не «проверка
     соответствия», а список мест, на которые обязан посмотреть
     инженер.

Всё детерминированно, без модели: результат должен быть одинаковым при
каждом прогоне и объяснимым построчно.

Поддерживаемые языки: C/C++, Ada, Python. Разбор — регулярными
выражениями по строкам, без построения AST: цель не статический
анализатор (для этого есть сертифицированные инструменты), а связка
кода с базой требований САПС.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..agents.base import AgentReport
from .base import Plugin

#: Расширения -> язык.
LANGUAGES = {
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".cxx": "cpp", ".ada": "ada", ".adb": "ada", ".ads": "ada",
    ".py": "python",
}

#: Ссылка на требование в комментарии кода.
TRACE_PATTERNS = [
    re.compile(r"\[\s*(?P<id>[A-ZА-Я][A-ZА-Я0-9]{1,15}[-_]\d{1,6}"
               r"(?:[.-]\d{1,4})*)\s*\]"),
    re.compile(r"@requirement\s+(?P<id>[A-Za-zА-Яа-я0-9_.-]+)", re.IGNORECASE),
    re.compile(r"(?:требование|requirement)\s*:?\s*"
               r"(?P<id>REQ[-_]\d{1,6}(?:[.-]\d{1,4})*)", re.IGNORECASE),
]

#: Формальные признаки. (код, регулярка, сообщение, тяжесть, языки)
CODE_CHECKS: list[tuple[str, re.Pattern[str], str, str, tuple[str, ...]]] = [
    ("dynamic_memory",
     re.compile(r"\b(?:malloc|calloc|realloc|free)\s*\(|(?<![\w:])new\s+\w"),
     "Динамическое выделение памяти. Для уровней A/B требуется "
     "обоснование детерминированности или отказ от него в пользу "
     "статического выделения", "major", ("c", "cpp")),
    ("goto",
     re.compile(r"^\s*goto\s+\w+", re.MULTILINE),
     "Оператор goto: усложняет анализ потока управления и покрытие "
     "структурного тестирования (MC/DC)", "major", ("c", "cpp")),
    ("setjmp",
     re.compile(r"\b(?:setjmp|longjmp)\s*\("),
     "setjmp/longjmp: нелокальные переходы не поддаются структурному "
     "анализу", "critical", ("c", "cpp")),
    ("float_equality",
     re.compile(r"(?<![=!<>])==\s*[-+]?\d+\.\d+|\d+\.\d+\s*==(?!=)"),
     "Сравнение вещественных чисел на точное равенство — "
     "недетерминированное поведение", "minor", ("c", "cpp", "python")),
    ("no_error_check",
     re.compile(r"^\s*(?:printf|sprintf|strcpy|strcat|gets)\s*\(",
                re.MULTILINE),
     "Небезопасная функция работы со строками (нет контроля границ)",
     "major", ("c", "cpp")),
    ("assert_in_prod",
     re.compile(r"^\s*assert\s*\(", re.MULTILINE),
     "assert в рабочем коде: в сборке с NDEBUG проверка исчезает, "
     "поведение отличается от протестированного", "minor",
     ("c", "cpp", "python")),
    ("bare_except",
     re.compile(r"^\s*except\s*:", re.MULTILINE),
     "Перехват всех исключений без разбора скрывает отказы",
     "major", ("python",)),
    ("eval_exec",
     re.compile(r"\b(?:eval|exec)\s*\("),
     "eval/exec: динамическое исполнение кода не поддаётся "
     "верификации", "critical", ("python",)),
]

#: Порог длины функции: длинные функции трудно покрыть структурными
#: тестами и обосновать MC/DC.
MAX_FUNCTION_LINES = 60

_FUNC_START = {
    "c": re.compile(r"^\s*(?:[\w*\s]+?)\s+\**(\w+)\s*\([^;]*\)\s*\{?\s*$"),
    "cpp": re.compile(r"^\s*(?:[\w*:<>~\s]+?)\s+\**(\w+)\s*\([^;]*\)\s*\{?\s*$"),
    "python": re.compile(r"^\s*def\s+(\w+)\s*\("),
    "ada": re.compile(r"^\s*(?:procedure|function)\s+(\w+)", re.IGNORECASE),
}


@dataclass
class CodeFinding:
    file: str
    line: int
    code: str
    message: str
    severity: str = "major"
    fragment: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "line": self.line, "code": self.code,
                "message": self.message, "severity": self.severity,
                "fragment": self.fragment}


@dataclass
class TraceEntry:
    requirement: str
    file: str
    line: int

    def to_dict(self) -> dict[str, Any]:
        return {"requirement": self.requirement, "file": self.file,
                "line": self.line}


@dataclass
class ReviewResult:
    files: int = 0
    lines: int = 0
    findings: list[CodeFinding] = field(default_factory=list)
    traces: list[TraceEntry] = field(default_factory=list)
    untraced_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"files": self.files, "lines": self.lines,
                "findings": [f.to_dict() for f in self.findings],
                "traces": [t.to_dict() for t in self.traces],
                "untraced_files": self.untraced_files}


def find_traces(text: str) -> list[tuple[int, str]]:
    """Ссылки на требования в тексте: [(номер строки, идентификатор)]."""
    out: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines(), start=1):
        for pattern in TRACE_PATTERNS:
            for m in pattern.finditer(line):
                out.append((i, m.group("id").upper().replace("_", "-")))
    return out


def check_source(text: str, language: str) -> list[tuple[int, str, str, str]]:
    """Формальные признаки: [(строка, код, сообщение, тяжесть)]."""
    out: list[tuple[int, str, str, str]] = []
    lines = text.splitlines()
    for code, pattern, message, severity, langs in CODE_CHECKS:
        if language not in langs:
            continue
        for i, line in enumerate(lines, start=1):
            if _is_comment(line, language):
                continue
            if pattern.search(line):
                out.append((i, code, message, severity))

    # Длина функции — отдельно: это не построчный признак.
    starter = _FUNC_START.get(language)
    if starter is not None:
        current: tuple[str, int] | None = None
        for i, line in enumerate(lines, start=1):
            m = starter.match(line)
            if m:
                if current and i - current[1] > MAX_FUNCTION_LINES:
                    out.append((current[1], "long_function",
                                f"Функция {current[0]!r} длиной "
                                f"{i - current[1]} строк (> "
                                f"{MAX_FUNCTION_LINES}): структурное покрытие "
                                "трудно обосновать", "minor"))
                current = (m.group(1), i)
        if current and len(lines) - current[1] > MAX_FUNCTION_LINES:
            out.append((current[1], "long_function",
                        f"Функция {current[0]!r} длиной "
                        f"{len(lines) - current[1]} строк (> "
                        f"{MAX_FUNCTION_LINES}): структурное покрытие трудно "
                        "обосновать", "minor"))
    return out


def _is_comment(line: str, language: str) -> bool:
    stripped = line.strip()
    if language in ("c", "cpp"):
        return stripped.startswith("//") or stripped.startswith("*") \
            or stripped.startswith("/*")
    if language == "python":
        return stripped.startswith("#")
    if language == "ada":
        return stripped.startswith("--")
    return False


def review_path(root: str | Path, *, max_files: int = 2000) -> ReviewResult:
    """Пройти репозиторий/каталог и собрать находки."""
    base = Path(root)
    if not base.exists():
        raise FileNotFoundError(f"Путь не найден: {base}")
    result = ReviewResult()
    paths: Iterable[Path] = ([base] if base.is_file()
                             else sorted(base.rglob("*")))
    for path in paths:
        if not path.is_file():
            continue
        language = LANGUAGES.get(path.suffix.lower())
        if language is None:
            continue
        if any(part in (".git", "node_modules", "__pycache__", "build", "dist")
               for part in path.parts):
            continue
        if result.files >= max_files:
            break
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(path.relative_to(base) if base.is_dir() else path.name)
        result.files += 1
        result.lines += text.count("\n") + 1

        traces = find_traces(text)
        for line_no, req in traces:
            result.traces.append(TraceEntry(req, rel, line_no))
        if not traces:
            result.untraced_files.append(rel)

        for line_no, code, message, severity in check_source(text, language):
            snippet = text.splitlines()[line_no - 1].strip()[:160] \
                if line_no <= len(text.splitlines()) else ""
            result.findings.append(
                CodeFinding(rel, line_no, code, message, severity, snippet))
    return result


class CodeReviewPlugin(Plugin):
    """Ревью кода: прослеживаемость «требование -> код» и формальные признаки."""

    name = "code_review"
    title = ("Ревью репозитория на прослеживаемость требований (DO-178C) "
             "и формальные признаки, ограниченные для уровней A/B")
    needs_llm = False

    def run(self, *, path: str = "", node_code: str = "",
            create_suggestions: bool = False, **kwargs: Any) -> AgentReport:
        report = self._report()
        report.agent = self.name
        if not path:
            report.errors.append(
                "Не указан путь к репозиторию: plugin run code_review --path ...")
            return report

        try:
            result = review_path(path)
        except FileNotFoundError as exc:
            report.errors.append(str(exc))
            return report

        report.processed = result.files
        for finding in result.findings:
            report.findings.append(finding.to_dict())

        # --- прослеживаемость в обе стороны ---
        traced_ids = {t.requirement for t in result.traces}
        requirements = self.store.list_requirements(node_code=node_code,
                                                    limit=5000)
        known = {r["external_id"].upper(): r for r in requirements}

        without_code = [ext for ext in known if ext not in traced_ids]
        unknown_refs = sorted(traced_ids - set(known))

        report.findings.append({
            "kind": "traceability",
            "files": result.files,
            "lines": result.lines,
            "traced_requirements": sorted(traced_ids & set(known)),
            "requirements_without_code": sorted(without_code),
            "references_to_unknown_requirements": unknown_refs,
            "files_without_traces": result.untraced_files[:50],
        })

        if unknown_refs:
            report.errors.append(
                f"В коде есть ссылки на {len(unknown_refs)} требований, "
                f"которых нет в базе САПС: {', '.join(unknown_refs[:10])}"
                + ("…" if len(unknown_refs) > 10 else "")
                + ". Это либо опечатка в комментарии, либо требование не "
                  "импортировано.")

        # Предложения — только если попросили явно: ревью кода легко
        # порождает сотни записей, и засорять ими очередь инженера по
        # умолчанию неправильно.
        if create_suggestions:
            for ext in sorted(without_code)[:100]:
                req = known[ext]
                self._suggest(
                    report, int(req["id"]), kind="attribute",
                    payload={"attributes": {"do178c_implemented": "нет"}},
                    rationale=(
                        f"В просмотренном коде ({result.files} файлов) не "
                        f"найдено ни одной ссылки на {ext}. Для DO-178C "
                        "требуется прослеживаемость «требование -> код»: "
                        "добавьте ссылку в комментарий реализации или "
                        "обоснуйте отсутствие."))

        self.store.log(f"plugin:{self.name}", "plugin_run",
                       detail=f"{path}: файлов {result.files}, находок "
                              f"{len(result.findings)}",
                       data={"path": str(path), "files": result.files})
        return report
