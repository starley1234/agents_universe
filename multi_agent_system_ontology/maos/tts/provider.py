"""TTS: клиент реального сервера синтеза речи OmniVoice (и совместимых).

OmniVoice — HTTP API из ТЗ:
  POST {base_url}/tts/v1/synthesize
       body: {"text": str, "voice": str, "audio_format": str = "mp3"}
       -> аудио (см. ниже про разбор ответа)
  GET  {base_url}/voices -> список доступных голосов

Спецификация OpenAPI для /tts/v1/synthesize описывает тело ответа как
`content: application/json, schema: {}` — то есть формально НЕ
специфицирует структуру (пустая схема). На практике TTS-сервисы отдают
либо СЫРЫЕ аудио-байты с Content-Type вида `audio/mpeg`, либо JSON с
полем вида `audio`/`audio_base64`/`url`. Клиент ниже обрабатывает ОБА
случая по фактическому Content-Type ответа, а не гадает по документации
заранее — это реальный протокол, который стоит проверить один раз при
интеграции с конкретным развёртыванием OmniVoice, но код не должен
падать на любом разумном варианте ответа.

Остальные провайдеры (OpenAI TTS/ElevenLabs/Piper) остаются
НЕ РЕАЛИЗОВАННЫМИ — только конфигурация и явная ошибка вместо тихой
заглушки (см. TTSProvider.synthesize по умолчанию), т.к. для них нет
согласованной спецификации в этой сессии.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class TTSError(RuntimeError):
    """Ошибка синтеза речи: неизвестный провайдер, нет ключа, сеть и т.п."""


@dataclass
class VoiceConfig:
    provider: str = "none"     # none | omnivoice | openai | elevenlabs | piper
    voice_id: str = ""


class TTSProvider:
    name = "none"

    def __init__(self, voice_id: str = "", base_url: str = "",
                api_key: str = "", timeout: int = 60,
                audio_format: str = "mp3") -> None:
        self.voice_id = voice_id
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.audio_format = audio_format

    def synthesize(self, text: str) -> tuple[bytes, str]:
        """Возвращает (аудио-байты, mime-тип)."""
        raise NotImplementedError(
            f"Провайдер TTS {self.name!r} не реализован в этой сборке MAOS. "
            "См. docstring maos/tts/provider.py — интерфейс готов, "
            "подключение конкретного API (OpenAI/ElevenLabs/Piper) "
            "запрашивается отдельно."
        )

    def list_voices(self) -> list[dict[str, Any]]:
        raise NotImplementedError(
            f"Провайдер TTS {self.name!r} не поддерживает список голосов "
            "в этой сборке MAOS.")


class NoTTSProvider(TTSProvider):
    """Провайдер по умолчанию: TTS выключен, ошибка явная и понятная."""

    name = "none"

    def synthesize(self, text: str) -> tuple[bytes, str]:
        raise TTSError(
            "TTS выключен (TTS_PROVIDER=none). Задайте TTS_PROVIDER=omnivoice "
            "и TTS_BASE_URL, чтобы включить синтез речи."
        )

    def list_voices(self) -> list[dict[str, Any]]:
        raise TTSError("TTS выключен (TTS_PROVIDER=none).")


_MIME_BY_FORMAT = {
    "mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg",
    "opus": "audio/opus", "flac": "audio/flac",
}


class OmniVoiceProvider(TTSProvider):
    """Клиент OmniVoice Official API (см. docstring модуля)."""

    name = "omnivoice"

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _request(self, method: str, path: str,
                body: dict[str, Any] | None = None) -> tuple[bytes, str]:
        if not self.base_url:
            raise TTSError(
                "TTS_BASE_URL не задан — укажите адрес сервера OmniVoice "
                "(например http://localhost:9000)."
            )
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"{self.base_url}{path}", data=data, method=method,
            headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                content_type = resp.headers.get("Content-Type", "")
                return resp.read(), content_type
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:800]
            raise TTSError(
                self._format_error(exc.code, detail)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TTSError(f"Не достучались до {self.base_url}: {exc}") from exc

    @staticmethod
    def _format_error(code: int, body: str) -> str:
        if code == 422:
            try:
                data = json.loads(body)
                msgs = [d.get("msg", "") for d in data.get("detail", [])]
                if msgs:
                    return f"OmniVoice отклонил запрос (422): {'; '.join(msgs)}"
            except (json.JSONDecodeError, AttributeError, TypeError):
                pass
        return f"OmniVoice вернул HTTP {code}: {body}"

    def synthesize(self, text: str) -> tuple[bytes, str]:
        if not text.strip():
            raise TTSError("Пустой текст для синтеза речи")
        if not self.voice_id:
            raise TTSError(
                "Не задан voice (голос агента) — OmniVoice требует поле "
                "'voice' в каждом запросе.")
        body = {"text": text, "voice": self.voice_id,
               "audio_format": self.audio_format}
        raw, content_type = self._request("POST", "/tts/v1/synthesize", body)
        return self._decode_audio(raw, content_type)

    def _decode_audio(self, raw: bytes, content_type: str) -> tuple[bytes, str]:
        """Разбирает ответ синтеза — сырые байты аудио ИЛИ JSON-обёртку.

        Спецификация OmniVoice не фиксирует схему ответа (schema: {}) —
        поддерживаем оба реалистичных варианта, определяя формат по
        фактическому Content-Type, а не по документации.
        """
        ct = (content_type or "").split(";")[0].strip().lower()
        if ct.startswith("audio/"):
            return raw, ct
        if ct.startswith("application/json") or not ct:
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                # не JSON и не заявлен как audio/* — считаем сырыми байтами
                return raw, _MIME_BY_FORMAT.get(self.audio_format, "audio/mpeg")
            for key in ("audio_base64", "audio", "data"):
                val = data.get(key) if isinstance(data, dict) else None
                if isinstance(val, str) and val:
                    try:
                        return (base64.b64decode(val),
                               _MIME_BY_FORMAT.get(self.audio_format, "audio/mpeg"))
                    except Exception as exc:
                        raise TTSError(
                            f"Не удалось декодировать base64-аудио из ответа "
                            f"OmniVoice: {exc}") from exc
            url = data.get("url") if isinstance(data, dict) else None
            if isinstance(url, str) and url:
                audio_raw, audio_ct = self._request("GET", url)
                return audio_raw, audio_ct or _MIME_BY_FORMAT.get(
                    self.audio_format, "audio/mpeg")
            raise TTSError(
                f"Не удалось распознать формат ответа OmniVoice: {str(data)[:300]}")
        # неизвестный Content-Type — отдаём как есть, лучшее, что можно сделать
        return raw, ct

    def list_voices(self) -> list[dict[str, Any]]:
        raw, _ = self._request("GET", "/voices")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise TTSError(f"Ответ /voices не является JSON: {exc}") from exc
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("voices"), list):
            return data["voices"]
        raise TTSError(f"Неожиданный формат ответа /voices: {str(data)[:300]}")


class OpenAITTSProvider(TTSProvider):
    name = "openai"


class ElevenLabsTTSProvider(TTSProvider):
    name = "elevenlabs"


class PiperTTSProvider(TTSProvider):
    name = "piper"


_REGISTRY: dict[str, type[TTSProvider]] = {
    "none": NoTTSProvider,
    "omnivoice": OmniVoiceProvider,
    "openai": OpenAITTSProvider,
    "elevenlabs": ElevenLabsTTSProvider,
    "piper": PiperTTSProvider,
}


def known_providers() -> list[str]:
    return sorted(_REGISTRY)


def build_tts_provider(provider: str, voice_id: str = "", base_url: str = "",
                       api_key: str = "", timeout: int = 60,
                       audio_format: str = "mp3") -> TTSProvider:
    key = (provider or "none").strip().lower()
    cls = _REGISTRY.get(key)
    if cls is None:
        raise TTSError(
            f"Неизвестный провайдер TTS {provider!r}. Доступны: "
            f"{', '.join(known_providers())}")
    return cls(voice_id=voice_id, base_url=base_url, api_key=api_key,
              timeout=timeout, audio_format=audio_format)
