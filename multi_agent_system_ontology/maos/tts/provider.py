"""TTS: интерфейс провайдера синтеза речи (ТЗ п.8: TTS_PROVIDER).

В этом проходе реализован ТОЛЬКО интерфейс и конфигурация — параметры
голоса хранятся в модели агента (agent.voice_provider, agent.voice_id, см.
maos/memory/store.py), а фабрика build_tts_provider умеет собрать
провайдер по имени с ленивым импортом. Сама генерация звука через
реальные внешние API (OpenAI TTS, ElevenLabs) НЕ реализована по решению
пользователя в этой сессии — TTSProvider.synthesize() поднимает
NotImplementedError с понятным сообщением, что делать дальше.

Это осознанный технический долг, а не забытая часть: реализация без
реальной проверки (настоящий HTTP-вызов к платному API) не проходила бы
по принятой в проекте философии "тест обязан уметь падать" — сначала
нужно решить, на каких провайдерах и с какими тестовыми ключами это
проверять.
"""
from __future__ import annotations

from dataclasses import dataclass


class TTSError(RuntimeError):
    """Ошибка синтеза речи: неизвестный провайдер, нет ключа, сеть и т.п."""


@dataclass
class VoiceConfig:
    provider: str = "none"     # none | openai | elevenlabs | piper
    voice_id: str = ""


class TTSProvider:
    name = "none"

    def __init__(self, voice_id: str = "") -> None:
        self.voice_id = voice_id

    def synthesize(self, text: str) -> bytes:
        raise NotImplementedError(
            f"Провайдер TTS {self.name!r} не реализован в этой сборке MAOS. "
            "См. docstring maos/tts/provider.py — интерфейс готов, "
            "подключение конкретного API (OpenAI/ElevenLabs/Piper) "
            "запрашивается отдельно."
        )


class NoTTSProvider(TTSProvider):
    """Провайдер по умолчанию: TTS выключен, ошибка явная и понятная."""

    name = "none"

    def synthesize(self, text: str) -> bytes:
        raise TTSError(
            "TTS выключен (TTS_PROVIDER=none). Задайте TTS_PROVIDER "
            "и голос агента, чтобы включить синтез речи."
        )


class OpenAITTSProvider(TTSProvider):
    name = "openai"


class ElevenLabsTTSProvider(TTSProvider):
    name = "elevenlabs"


class PiperTTSProvider(TTSProvider):
    name = "piper"


_REGISTRY: dict[str, type[TTSProvider]] = {
    "none": NoTTSProvider,
    "openai": OpenAITTSProvider,
    "elevenlabs": ElevenLabsTTSProvider,
    "piper": PiperTTSProvider,
}


def known_providers() -> list[str]:
    return sorted(_REGISTRY)


def build_tts_provider(provider: str, voice_id: str = "") -> TTSProvider:
    key = (provider or "none").strip().lower()
    cls = _REGISTRY.get(key)
    if cls is None:
        raise TTSError(
            f"Неизвестный провайдер TTS {provider!r}. Доступны: "
            f"{', '.join(known_providers())}")
    return cls(voice_id=voice_id)
