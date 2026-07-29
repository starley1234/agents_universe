"""Проверка, что документация не врёт.

Документация разъезжается с кодом незаметно: добавили навык — забыли
строку в README, переименовали профиль — ссылка осталась старой. Читатель
верит написанному, поэтому расхождение хуже отсутствия описания.

Здесь проверяется только то, что можно сверить машинально: списки
навыков и ролей, число инструментов, существование упомянутых файлов,
флаги командной строки и целостность ссылок между документами.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.build import build_agent, known_skills          # noqa: E402
from agent.config import Config                            # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def section(t: str) -> None:
    print(f"\n{t}\n" + "─" * len(t))


README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_links() -> None:
    section("Ссылки между документами не битые")
    files = list(ROOT.glob("*.md")) + list((ROOT / "docs").glob("*.md"))
    bad = []
    for f in files:
        for m in re.finditer(r"\]\(([^)#]+)\)", f.read_text(encoding="utf-8")):
            t = m.group(1)
            if t.startswith(("http", "mailto", "#")):
                continue
            if not (f.parent / t).exists():
                bad.append(f"{f.name} -> {t}")
    check(f"проверено файлов: {len(files)}", len(files) >= 10, str(len(files)))
    check("битых ссылок нет", not bad, "; ".join(bad[:4]))


def test_skills_listed() -> None:
    section("Навыки: описано ровно то, что есть")
    # Берём таблицу наборов, а не весь файл: `agent` в других таблицах
    # не навык, и без сужения проверка ловила бы ложное расхождение.
    block = README.split("| набор | инструменты |")[1].split("\n\n")[0]
    promised = set(re.findall(r"^\| `(\w+)` \|", block, re.M))
    real = set(known_skills())
    check(f"в таблице {len(promised)} наборов", bool(promised), str(promised))
    check("нет обещанных, но несуществующих",
          not (promised - real), str(promised - real))
    check("нет забытых в документации",
          not (real - promised), str(real - promised))


def test_profiles_listed() -> None:
    section("Роли: описано ровно то, что есть")
    block = README.split("**Роли:**")[1].split("\n\n")[0]
    promised = set(re.findall(r"`(\w+)`", block))
    real = set(Config.list_profiles())
    check("список ролей совпадает", promised == real,
          f"лишние {promised - real}, забыты {real - promised}")
    for name in real:
        f = ROOT / "agent" / "profiles" / f"{name}.json"
        check(f"профиль {name} читается", f.exists() and f.stat().st_size > 50)


def test_counts() -> None:
    section("Числа в README — настоящие")
    m = re.search(r"\*\*(\d+) инструментов, (\d+) наборов, (\d+) ролей", README)
    check("строка с числами на месте", m is not None)
    if not m:
        return
    tools_promised, skills_promised, roles_promised = map(int, m.groups())

    tools: set[str] = set()
    for p in Config.list_profiles():
        with tempfile.TemporaryDirectory() as td:
            cfg = Config.load(None, profile=p, provider="ollama", model="m",
                              workspace=td)
            cfg.db = os.path.join(td, "a.db")
            tools |= set(build_agent(cfg).tools.names())

    check(f"инструментов ровно {tools_promised}", len(tools) == tools_promised,
          f"на деле {len(tools)}")
    check(f"наборов ровно {skills_promised}",
          len(known_skills()) == skills_promised, f"на деле {len(known_skills())}")
    check(f"ролей ровно {roles_promised}",
          len(Config.list_profiles()) == roles_promised,
          f"на деле {len(Config.list_profiles())}")


def test_structure() -> None:
    section("Структура из README существует")
    block = README.split("```\nagent/\n")[1].split("```")[0]
    names = re.findall(r"^  ([\w.]+/?)\s", block, re.M)
    check("список файлов извлечён", len(names) > 5, str(len(names)))
    missing = [n for n in names if not (ROOT / "agent" / n).exists()]
    check("все упомянутые файлы на месте", not missing, str(missing))


def test_cli_flags() -> None:
    section("Обещанные команды существуют")
    out = subprocess.run([sys.executable, "-m", "agent", "--help"],
                         capture_output=True, text=True, cwd=str(ROOT))
    help_text = out.stdout + out.stderr
    for flag in ("--do", "--auto", "--route", "--runs", "--check",
                 "--max-usd", "--resume", "--profile"):
        check(f"CLI знает {flag}", flag in help_text)


def test_no_stale_numbers() -> None:
    section("Нет устаревших чисел тестов")
    # Сколько проверок на самом деле — считаем прогоном заголовков.
    files = sorted((ROOT / "tests").glob("test_*.py"))
    check("тестовых файлов не меньше десяти", len(files) >= 10,
          str(len(files)))
    # В документах не должно остаться прежних итогов.
    stale = ["65 проверок", "232 проверки", "336 проверок", "422 провер",
             "494 провер", "563 провер", "621 провер"]
    found = []
    for f in list(ROOT.glob("*.md")) + list((ROOT / "docs").glob("*.md")):
        text = f.read_text(encoding="utf-8")
        for s in stale:
            if s in text:
                found.append(f"{f.name}: {s}")
    check("устаревших итогов нет", not found, "; ".join(found[:4]))


def test_gitignore() -> None:
    section("Порождаемое не попадёт в репозиторий")
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pat, why in [("*.db", "база с памятью у каждого своя"),
                     ("workspace/", "рабочая папка агента"),
                     (".env", "секреты"),
                     ("__pycache__/", "кэш Python"),
                     (".agent-git/", "снимки рабочей папки"),
                     ("outbox/", "черновики писем")]:
        check(f"исключено {pat} ({why})", pat in gi)
    check("пример окружения на месте", (ROOT / ".env.example").exists())
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    for key in ("AGENT_SMTP_PASS", "AGENT_TG_TOKEN", "AGENT_MAX_TOKEN",
                "AGENT_API_TOKEN"):
        check(f"в примере упомянут {key}", key in env)
    # Значение берём ДО комментария: строка «AGENT_SMTP_PASS=  # пароль»
    # пустая, хотя после '=' текст есть. Наивная проверка на .+ ругалась
    # на комментарий и выглядела бы как найденная утечка.
    filled = []
    for line in env.splitlines():
        if not re.match(r"^[A-Z_]+=", line):
            continue
        key, _, rest = line.partition("=")
        value = rest.split("#", 1)[0].strip()
        # OLLAMA_HOST и адреса — не секреты, их значение полезно
        if value and any(w in key for w in ("KEY", "PASS", "TOKEN", "SECRET")):
            filled.append(key)
    check("в примере нет заполненных секретов", not filled,
          f"похоже на настоящие значения: {', '.join(filled)}")


def main() -> int:
    print("=" * 60)
    print("ТЕСТЫ ДОКУМЕНТАЦИИ: описанное должно существовать")
    print("=" * 60)
    test_links()
    test_skills_listed()
    test_profiles_listed()
    test_counts()
    test_structure()
    test_cli_flags()
    test_no_stale_numbers()
    test_gitignore()
    print("\n" + "=" * 60)
    print(f"пройдено: {PASS} · провалено: {FAIL}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
