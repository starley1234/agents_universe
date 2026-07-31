"""Инструменты криптографии, хеширования и проверки подписей (crypto.*)."""
from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Any

from ..core import Tool, ToolError


def build_crypto_tools() -> list[Tool]:
    """Собрать инструменты для генерации UUID, хешей и проверки подписей."""

    def generate_uuid() -> str:
        new_id = str(uuid.uuid4())
        return f"UUIDv4: {new_id}"

    def hash_string(text: str, algo: str = "sha256") -> str:
        alg_up = (algo or "sha256").lower()
        if not text:
            raise ToolError("Строка для хеширования не может быть пустой")

        try:
            h = hashlib.new(alg_up)
            h.update(text.encode("utf-8"))
            return f"### Hash ({alg_up.upper()}):\n{h.hexdigest()}"
        except ValueError as exc:
            raise ToolError(f"Алгоритм хеширования {alg_up!r} не поддерживается: {exc}") from exc

    def verify_signature(
        text: str, signature_hex: str, secret_key: str = "default-secret"
    ) -> str:
        if not text or not signature_hex:
            raise ToolError("Текст и подпись (signature_hex) обязательны для проверки")

        key_bytes = (secret_key or "default-secret").encode("utf-8")
        expected = hmac.new(key_bytes, text.encode("utf-8"), hashlib.sha256).hexdigest()

        if hmac.compare_digest(expected.lower(), signature_hex.strip().lower()):
            return "✓ Подпись (HMAC-SHA256) ДЕЙСТВИТЕЛЬНА — целостность документа подтверждена"
        return (
            f"✗ Подпись НЕДЕЙСТВИТЕЛЬНА (ожидалась: {expected[:12]}...)"
        )

    return [
        Tool(
            name="crypto.generate_uuid",
            description="Сгенерировать случайный уникальный идентификатор UUIDv4 (например, для сессии или транзакции).",
            parameters={"type": "object", "properties": {}},
            fn=generate_uuid,
            skills=["crypto", "uuid", "id", "security", "local"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "crypto_id",
                "speed": "fast",
                "tags": ["crypto", "uuid", "id", "token", "generate"],
            },
            example="crypto.generate_uuid()",
        ),
        Tool(
            name="crypto.hash_string",
            description="Вычислить хеш строки (SHA256, MD5, SHA1).",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Индексируемый текст"},
                    "algo": {
                        "type": "string",
                        "description": "Алгоритм (sha256, md5, sha1)",
                    },
                },
                "required": ["text"],
            },
            fn=hash_string,
            skills=["crypto", "hash", "sha256", "security", "local"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "crypto_hash",
                "speed": "fast",
                "tags": ["crypto", "hash", "sha256", "md5", "security"],
            },
            example='crypto.hash_string(text="admin", algo="sha256")',
        ),
        Tool(
            name="crypto.verify_signature",
            description="Проверить подлинность и целостность текста по подписи HMAC-SHA256.",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Подписанный текст"},
                    "signature_hex": {
                        "type": "string",
                        "description": "Проверяемая подпись в hex",
                    },
                    "secret_key": {
                        "type": "string",
                        "description": "Секретный ключ подписи",
                    },
                },
                "required": ["text", "signature_hex"],
            },
            fn=verify_signature,
            skills=["crypto", "signature", "hmac", "security", "audit"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "crypto_sig",
                "speed": "fast",
                "tags": ["crypto", "signature", "verify", "hmac", "security"],
            },
            example='crypto.verify_signature(text="data", signature_hex="abc...")',
        ),
    ]
