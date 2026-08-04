"""Тесты криптографии, хеширования и подписей (crypto.*)."""
from __future__ import annotations

import hmac
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.local.crypto import build_crypto_tools
from tests.harness import check, section, summary


def run_tests() -> int:
    section("1. Инструменты криптографии и подписей (crypto.*)")
    tools = {t.name: t for t in build_crypto_tools()}
    check("зарегистрировано 4 инструмента crypto", len(tools) == 4)

    res_uuid = tools["crypto.generate_uuid"].execute()
    check("generate_uuid генерирует UUIDv4", "UUIDv4:" in res_uuid and "-" in res_uuid)

    res_hash = tools["crypto.hash_string"].execute(text="hello", algo="sha256")
    check("hash_string вычисляет sha256", "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824" in res_hash)

    sig = hmac.new(b"secret", b"doc data", hashlib.sha256).hexdigest()
    res_ver = tools["crypto.verify_signature"].execute(
        text="doc data", signature_hex=sig, secret_key="secret"
    )
    check("verify_signature подтверждает корректную подпись", "ДЕЙСТВИТЕЛЬНА" in res_ver)

    res_ver_bad = tools["crypto.verify_signature"].execute(
        text="doc data", signature_hex="000000", secret_key="secret"
    )
    check("verify_signature отклоняет фальшивую подпись", "НЕДЕЙСТВИТЕЛЬНА" in res_ver_bad)

    return summary("Тесты криптографии")


def test_crypto_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
