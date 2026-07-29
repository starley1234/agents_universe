"""TTS: интерфейс провайдера синтеза речи (см. maos/tts/provider.py)."""
from __future__ import annotations

from .provider import (ElevenLabsTTSProvider, NoTTSProvider,
                       OpenAITTSProvider, PiperTTSProvider, TTSError,
                       TTSProvider, VoiceConfig, build_tts_provider,
                       known_providers)

__all__ = [
    "TTSError", "TTSProvider", "VoiceConfig", "NoTTSProvider",
    "OpenAITTSProvider", "ElevenLabsTTSProvider", "PiperTTSProvider",
    "build_tts_provider", "known_providers",
]
