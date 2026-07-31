"""Тесты инструментов синтеза речи (tts.*)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.core import Workspace
from agent_toolkit.integrations.tts import build_tts_tools
from tests.harness import TempWorkspace, check, section, summary


def run_tests() -> int:
    with TempWorkspace() as tmp:
        ws = Workspace(tmp.path("ws"))
        section("1. Синтез речи (tts.*)")
        tools = {t.name: t for t in build_tts_tools(ws)}
        check("зарегистрирован 1 инструмент tts", len(tools) == 1)

        res_tts = tools["tts.synthesize_speech"].execute(
            text="Аудит прошёл успешно", filename="report.mp3", voice="neutral"
        )
        check("synthesize_speech синтезирует аудиофайл", "сохранён в report.mp3" in res_tts)
        check("файл report.mp3 создан на диске", ws.exists("report.mp3"))

    return summary("Тесты синтеза речи")


def test_tts_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
