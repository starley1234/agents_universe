"""Тесты maos.tts.provider: интерфейс, конфигурация, и РЕАЛЬНЫЙ клиент
OmniVoice (см. ТЗ — OpenAPI-спецификация OmniVoice Official API) на
настоящем HTTP-сервере, эмулирующем оба реалистичных варианта ответа
(сырые аудио-байты и JSON с base64), плюс ошибку валидации (422).

OpenAI/ElevenLabs/Piper остаются НЕ РЕАЛИЗОВАННЫМИ — только интерфейс
и честная ошибка вместо тихой заглушки (нет согласованной спецификации
для них в этой сессии).
"""
from __future__ import annotations

import base64
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maos.tts.provider import (TTSError, VoiceConfig, build_tts_provider,  # noqa: E402
                               known_providers)

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


class FakeOmniVoice(BaseHTTPRequestHandler):
    """Эмулирует OmniVoice Official API по заданному режиму (mode)."""

    mode = "raw"          # raw | json_b64 | json_url | invalid | bad_json
    calls: list[dict] = []

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        type(self).calls.append(body)
        mode = type(self).mode
        if mode == "raw":
            out = b"RAW_AUDIO_BYTES"
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        elif mode == "json_b64":
            payload = json.dumps(
                {"audio_base64": base64.b64encode(b"B64_AUDIO_BYTES").decode()}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif mode == "json_url":
            payload = json.dumps({"url": "/audio/result.mp3"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif mode == "invalid":
            payload = json.dumps({"detail": [
                {"loc": ["body", "voice"], "msg": "field required",
                 "type": "value_error.missing"}]}).encode()
            self.send_response(422)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif mode == "bad_json":
            payload = json.dumps({"unexpected_field": 123}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        if self.path == "/voices":
            payload = json.dumps(
                {"voices": [{"id": "alloy", "name": "Alloy"},
                           {"id": "rachel", "name": "Rachel"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif self.path == "/audio/result.mp3":
            out = b"URL_AUDIO_BYTES"
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        elif self.path == "/voices/broken":
            payload = b"not json at all"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)


def main() -> int:
    section("known_providers")
    providers = known_providers()
    check("none/omnivoice/openai/elevenlabs/piper все известны",
         {"none", "omnivoice", "openai", "elevenlabs", "piper"} <= set(providers))

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

    # ------------------------------------------------- OmniVoice: реальный HTTP
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeOmniVoice)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{port}"

    try:
        section("OmniVoiceProvider.synthesize: сырые audio-байты (Content-Type: audio/*)")
        FakeOmniVoice.mode = "raw"
        FakeOmniVoice.calls = []
        omni = build_tts_provider("omnivoice", voice_id="alloy", base_url=base_url)
        audio, mime = omni.synthesize("привет мир")
        check("байты аудио получены как есть", audio == b"RAW_AUDIO_BYTES")
        check("mime-тип соответствует ответу сервера", mime == "audio/mpeg")
        check("реальный HTTP POST дошёл с полем text/voice",
             FakeOmniVoice.calls[-1]["text"] == "привет мир"
             and FakeOmniVoice.calls[-1]["voice"] == "alloy")
        check("audio_format по умолчанию mp3",
             FakeOmniVoice.calls[-1]["audio_format"] == "mp3")

        section("OmniVoiceProvider.synthesize: JSON с base64-аудио")
        FakeOmniVoice.mode = "json_b64"
        audio2, mime2 = omni.synthesize("ещё текст")
        check("base64 корректно декодирован", audio2 == b"B64_AUDIO_BYTES")

        section("OmniVoiceProvider.synthesize: JSON со ссылкой на файл (второй запрос)")
        FakeOmniVoice.mode = "json_url"
        audio3, mime3 = omni.synthesize("текст со ссылкой")
        check("аудио скачано по URL из ответа", audio3 == b"URL_AUDIO_BYTES")

        section("OmniVoiceProvider.synthesize: ошибка валидации 422 -> понятный TTSError")
        FakeOmniVoice.mode = "invalid"
        try:
            omni.synthesize("текст")
            check("422 приводит к TTSError", False)
        except TTSError as exc:
            check("422 приводит к TTSError", True)
            check("сообщение содержит причину валидации",
                 "field required" in str(exc))

        section("OmniVoiceProvider.synthesize: непонятный JSON-ответ -> TTSError")
        FakeOmniVoice.mode = "bad_json"
        try:
            omni.synthesize("текст")
            check("неожиданный JSON приводит к TTSError", False)
        except TTSError:
            check("неожиданный JSON приводит к TTSError", True)

        section("OmniVoiceProvider.synthesize: пустой текст/voice отклоняются заранее")
        try:
            omni.synthesize("")
            check("пустой текст отклонён без сетевого вызова", False)
        except TTSError:
            check("пустой текст отклонён без сетевого вызова", True)
        omni_no_voice = build_tts_provider("omnivoice", voice_id="", base_url=base_url)
        try:
            omni_no_voice.synthesize("текст")
            check("отсутствие voice отклонено без сетевого вызова", False)
        except TTSError:
            check("отсутствие voice отклонено без сетевого вызова", True)

        section("OmniVoiceProvider.synthesize: не задан base_url -> понятная ошибка")
        omni_no_url = build_tts_provider("omnivoice", voice_id="alloy")
        try:
            omni_no_url.synthesize("текст")
            check("отсутствие base_url отклонено с понятным сообщением", False)
        except TTSError as exc:
            check("отсутствие base_url отклонено с понятным сообщением", True)
            check("сообщение упоминает TTS_BASE_URL", "TTS_BASE_URL" in str(exc))

        section("OmniVoiceProvider.list_voices: реальный GET /voices")
        voices = omni.list_voices()
        check("список голосов получен", len(voices) == 2)
        check("голоса содержат id", {v["id"] for v in voices} == {"alloy", "rachel"})

        section("Недостижимый сервер -> TTSError, а не трейсбек")
        omni_down = build_tts_provider("omnivoice", voice_id="alloy",
                                       base_url="http://127.0.0.1:1", timeout=2)
        try:
            omni_down.synthesize("текст")
            check("недостижимый сервер даёт TTSError", False)
        except TTSError:
            check("недостижимый сервер даёт TTSError", True)
    finally:
        httpd.shutdown()
        httpd.server_close()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
