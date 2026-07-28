"""Тесты maos.tts.provider: только интерфейс и конфигурация (реализация
провайдеров осознанно не сделана в этой сессии, см. docstring модуля)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maos.tts.provider import (TTSError, VoiceConfig, build_tts_provider,
                               known_providers)                      # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}\n" + "─" * len(title))


def main() -> int:
    section("known_providers")
    providers = known_providers()
    check("none/openai/elevenlabs/piper все известны",
         {"none", "openai", "elevenlabs", "piper"} <= set(providers))

    section("build_tts_provider")
    p = build_tts_provider("none")
    check("провайдер none собирается", p.name == "none")
    p2 = build_tts_provider("openai", voice_id="alloy")
    check("провайдер openai собирается с voice_id", p2.voice_id == "alloy")
    try:
        build_tts_provider("unknown")
        check("неизвестный провайдер кидает TTSError", False)
    except TTSError:
        check("неизвестный провайдер кидает TTSError", True)

    section("TTSProvider.synthesize: честная ошибка вместо тихой заглушки")
    try:
        p.synthesize("привет")
        check("none.synthesize() кидает TTSError (TTS выключен)", False)
    except TTSError as exc:
        check("none.synthesize() кидает TTSError (TTS выключен)", True)
        check("сообщение объясняет, что делать", "TTS_PROVIDER" in str(exc))

    try:
        p2.synthesize("привет")
        check("openai.synthesize() кидает NotImplementedError (не реализован)", False)
    except NotImplementedError as exc:
        check("openai.synthesize() кидает NotImplementedError (не реализован)", True)
        check("сообщение НЕ выдаёт себя за рабочую реализацию",
             "не реализован" in str(exc))

    section("VoiceConfig: датакласс параметров голоса агента")
    vc = VoiceConfig(provider="elevenlabs", voice_id="rachel")
    check("VoiceConfig хранит provider/voice_id",
         vc.provider == "elevenlabs" and vc.voice_id == "rachel")
    vc_default = VoiceConfig()
    check("VoiceConfig по умолчанию provider=none", vc_default.provider == "none")

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
