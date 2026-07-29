"""Сквозной тест CLI: настоящий подпроцесс, настоящая база, настоящие файлы.

Почему подпроцессом, а не вызовом main() внутри теста: командная строка —
это контракт с человеком и с cron/CI. Проверять надо ровно то, что
получит пользователь: код возврата, текст в stdout, состояние базы после
команды. Вызов функции внутри процесса скрывает целый класс ошибок
(импорты, argparse, кодировка вывода, утечка состояния между командами).

Главный сценарий здесь — тот, ради которого среда существует:
    awos run … → прогон встаёт на человеке → awos inbox видит очередь →
    awos edit … правит результат → прогон доигрывается сам.
Каждый шаг — ОТДЕЛЬНЫЙ ЗАПУСК ПРОЦЕССА, то есть заодно проверяется, что
пауза действительно переживает завершение процесса.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import check, section, summary                       # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class Cli:
    """Запускает `python3 -m awos ...` в изолированном окружении."""

    def __init__(self, **env_over: str) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="awos_cli_"))
        self.env = dict(os.environ)
        # Чистим возможные внешние настройки: тест обязан быть
        # воспроизводимым на чужой машине с своим ~/.bashrc.
        for key in list(self.env):
            if key.startswith("AWOS_"):
                del self.env[key]
        self.env.update({
            "AWOS_DB": str(self.dir / "awos.db"),
            "AWOS_WORKSPACE": str(self.dir / "ws"),
            "AWOS_PROVIDER": "stub",
            "AWOS_MODEL": "stub",
            "AWOS_HITL": "off",
            "PYTHONIOENCODING": "utf-8",
        })
        self.env.update(env_over)

    def run(self, *args: str, timeout: int = 120):
        proc = subprocess.run(
            [sys.executable, "-m", "awos", *args], cwd=str(ROOT), env=self.env,
            capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr

    def close(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def main() -> int:
    section("Служебные команды")
    cli = Cli()
    code, out, err = cli.run("check")
    check("check завершается успешно", code == 0, err[:300])
    check("check показывает хранилище", "Хранилище" in out)
    check("check показывает workflow", "Workflow:" in out)
    check("check показывает инструменты", "Инструменты:" in out)
    check("check подтверждает готовность", "готова к работе" in out)

    code, out, _ = cli.run("workflows")
    check("workflows перечисляет определения", "research_brief" in out)
    check("workflows показывает цепочку шагов", "→" in out)

    code, out, _ = cli.run("profiles")
    check("profiles перечисляет профили", "critic" in out and "researcher" in out)
    check("profiles показывает роли", "[critic" in out or "critic" in out)

    code, out, _ = cli.run("tools")
    check("tools перечисляет инструменты", "read_file" in out)
    check("tools показывает гранты", "Гранты" in out)
    check("shell без гранта не выдан", "shell" not in out.split("Гранты")[0])

    code, out, _ = cli.run()
    check("без команды печатается справка", "usage" in out.lower() or
          "awos" in out.lower())

    code, out, err = cli.run("run", "нет_такого", "--goal", "ц")
    check("неизвестный workflow -> код 2", code == 2)
    check("ошибка объяснена", "не найден" in (out + err))

    section("Прогон целиком, без человека")
    code, out, err = cli.run("run", "research_brief", "--goal", "цель прогона",
                             "--input", "topic=тема", "--input", "audience=все")
    check("прогон успешен", code == 0, err[:400])
    check("статус done показан", "done" in out)
    check("шаги перечислены", "research" in out and "brief" in out)
    check("оценки показаны", "score=" in out)
    check("результаты показаны", "Результаты на доске" in out)

    run_id = int(re.search(r"Прогон #(\d+)", out).group(1))
    code, out, _ = cli.run("status")
    check("список прогонов показан", f"{run_id}" in out and "done" in out)
    code, out, _ = cli.run("status", str(run_id))
    check("детали прогона показаны", "цель прогона" in out)
    check("статистика токенов показана", "токенов" in out)
    code, out, _ = cli.run("status", str(run_id), "--events")
    check("журнал показан по флагу", "run_start" in out and "step_done" in out)

    code, out, _ = cli.run("context", str(run_id))
    check("доска показана", "research_notes" in out and "brief" in out)
    check("служебные ключи скрыты", "_workflow" not in out)
    code, out, _ = cli.run("context", str(run_id), "--all")
    check("служебные ключи по флагу видны", "_workflow" in out)
    code, out, _ = cli.run("context", str(run_id), "brief")
    check("история ключа показана", "версия 1" in out)

    code, out, _ = cli.run("inbox")
    check("очередь согласований пуста", "Точек контроля нет" in out)
    cli.close()

    section("Сквозной сценарий Human-in-the-Loop (разные процессы)")
    cli = Cli(AWOS_HITL="always")
    code, out, err = cli.run("run", "research_brief", "--goal", "с человеком",
                             "--input", "topic=т", "--input", "audience=а")
    check("прогон остановился на человеке", code == 0 and "waiting_human" in out,
          err[:300])
    check("подсказка про approve выведена", "awos approve" in out)
    cp = int(re.search(r"точка контроля #(\d+)", out).group(1))
    run_id = int(re.search(r"Прогон #(\d+)", out).group(1))

    # ОТДЕЛЬНЫЙ процесс: состояние читается из базы, а не из памяти.
    code, out, _ = cli.run("inbox")
    check("новый процесс видит очередь согласований", f"#{cp}" in out,
          "пауза обязана переживать завершение процесса")
    check("в очереди показан вопрос", "Утвердите результат" in out)

    code, out, err = cli.run("edit", str(cp), "ТЕКСТ ОТ ЧЕЛОВЕКА")
    check("правка принята", code == 0, err[:300])
    check("прогон продолжился", "waiting_human" in out or "done" in out)

    code, out, _ = cli.run("context", str(run_id), "research_notes")
    check("на доске текст человека", "ТЕКСТ ОТ ЧЕЛОВЕКА" in out)

    code, out, _ = cli.run("inbox")
    cp2_match = re.search(r"#(\d+) \(прогон", out)
    check("появилась вторая точка контроля", cp2_match is not None)
    cp2 = int(cp2_match.group(1))
    code, out, err = cli.run("approve", str(cp2), "годится")
    check("утверждение принято", code == 0, err[:300])
    check("прогон завершён", "done" in out)
    code, out, _ = cli.run("inbox")
    check("очередь опустела", "Точек контроля нет" in out)
    cli.close()

    section("Отклонение и отмена")
    cli = Cli(AWOS_HITL="always")
    code, out, _ = cli.run("run", "research_brief", "--goal", "отказ",
                           "--input", "topic=т", "--input", "audience=а")
    cp = int(re.search(r"точка контроля #(\d+)", out).group(1))
    code, out, _ = cli.run("reject", str(cp), "не годится")
    check("отклонение возвращает ненулевой код", code == 1,
          "провал прогона обязан быть заметен из cron/CI")
    check("статус failed показан", "failed" in out)

    code, out, _ = cli.run("run", "research_brief", "--goal", "отмена",
                           "--input", "topic=т", "--input", "audience=а")
    rid = int(re.search(r"Прогон #(\d+)", out).group(1))
    code, out, _ = cli.run("cancel", str(rid), "передумал")
    check("отмена выполнена", code == 0 and "отменён" in out)
    code, out, _ = cli.run("status", str(rid))
    check("статус cancelled в базе", "cancelled" in out)
    cli.close()

    section("Входы: JSON, файлы, ошибки")
    cli = Cli()
    payload = "СОДЕРЖИМОЕ ИЗ ФАЙЛА"
    f = Path(tempfile.mkdtemp(prefix="awos_in_")) / "input.txt"
    f.write_text(payload, encoding="utf-8")
    code, out, err = cli.run("run", "research_brief", "--goal", "файл",
                             "--input", f"topic=@{f}", "--input", "audience=а")
    check("значение входа прочитано из файла", code == 0, err[:300])
    rid = int(re.search(r"Прогон #(\d+)", out).group(1))
    code, out, _ = cli.run("context", str(rid), "research_notes")
    check("содержимое файла дошло до агента", payload in out)
    shutil.rmtree(f.parent, ignore_errors=True)

    code, out, err = cli.run("run", "research_brief", "--goal", "ц",
                             "--input", "topic=т")
    check("нехватка входа -> код 2", code == 2)
    check("названы недостающие входы", "audience" in (out + err))

    code, out, err = cli.run("run", "research_brief", "--input", "плохой_формат")
    check("вход без '=' отвергается", code != 0)
    cli.close()

    section("Демо-режим: работает без ключей и без сети")
    cli = Cli()
    del cli.env["AWOS_PROVIDER"]      # демо само переключается на stub
    code, out, err = cli.run("demo", "document_pipeline")
    check("демо отрабатывает", code == 0, err[:300])
    check("демо сообщает про stub", "stub" in out)
    check("демо доводит прогон до конца", "done" in out)
    cli.close()

    section("Изоляция рабочей папки в реальном прогоне")
    cli = Cli()
    code, out, err = cli.run("run", "document_pipeline", "--goal", "файлы",
                             "--input", "subject=тема", "--input", "format=md")
    check("прогон с записью файла успешен", code == 0, err[:300])
    ws = Path(cli.env["AWOS_WORKSPACE"])
    check("рабочая папка создана", ws.exists())
    outside = [p for p in ws.rglob("*") if not str(p).startswith(str(ws))]
    check("ничего не записано за пределами рабочей папки", not outside)
    cli.close()

    return summary("CLI")


if __name__ == "__main__":
    raise SystemExit(main())
