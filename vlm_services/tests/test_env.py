"""Загрузка .env и выбор адреса прослушивания.

Оба механизма ломались молча: .env не читался вовсе, а VLM_HOST
игнорировался — сервис поднимался на localhost, хотя в файле стояло
0.0.0.0, и наружу не отвечал.
"""

from __future__ import annotations

import os

import pytest

from vlmkit.env import find_env, load_env, parse_env


# --- разбор ----------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("A=1", {"A": "1"}),
    ("export A=1", {"A": "1"}),
    ("A = 1", {"A": "1"}),
    ('A="значение с пробелом"', {"A": "значение с пробелом"}),
    ("A='одинарные'", {"A": "одинарные"}),
    ("# коммент\nA=1", {"A": "1"}),
    ("A=1 # хвост", {"A": "1"}),
    ("\n\nA=1\n\n", {"A": "1"}),
    ("сломанная строка без равно", {}),
])
def test_parse_env(text, expected):
    assert parse_env(text) == expected


def test_parse_keeps_hash_inside_quotes():
    """Токен вида abc#def не должен обрезаться по решётке."""
    assert parse_env('VLM_API_TOKEN="abc#def"') == {"VLM_API_TOKEN": "abc#def"}


def test_parse_keeps_url_with_colon():
    assert parse_env("VLM_BASE_URL=https://x.ru/v1")["VLM_BASE_URL"] == "https://x.ru/v1"


def test_parse_handles_empty_value():
    assert parse_env("VLM_API_TOKEN=") == {"VLM_API_TOKEN": ""}


# --- загрузка --------------------------------------------------------------
def test_load_env_sets_variables(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("VLM_TEST_KEY=из_файла\n", encoding="utf-8")
    monkeypatch.delenv("VLM_TEST_KEY", raising=False)
    load_env(f)
    assert os.environ["VLM_TEST_KEY"] == "из_файла"


def test_environment_wins_over_file(tmp_path, monkeypatch):
    """docker run -e и systemd EnvironmentFile не должны перебиваться .env."""
    f = tmp_path / ".env"
    f.write_text("VLM_TEST_KEY=из_файла\n", encoding="utf-8")
    monkeypatch.setenv("VLM_TEST_KEY", "из_окружения")
    load_env(f)
    assert os.environ["VLM_TEST_KEY"] == "из_окружения"


def test_override_flag_forces_file(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("VLM_TEST_KEY=из_файла\n", encoding="utf-8")
    monkeypatch.setenv("VLM_TEST_KEY", "из_окружения")
    load_env(f, override=True)
    assert os.environ["VLM_TEST_KEY"] == "из_файла"


def test_missing_file_is_not_an_error(tmp_path):
    assert load_env(tmp_path / "нет-такого") == {}


def test_find_env_walks_up(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("A=1", encoding="utf-8")
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert find_env(deep) == tmp_path / ".env"


# --- выбор адреса ----------------------------------------------------------
def _serve_args(host=None, port=None):
    import argparse

    return argparse.Namespace(host=host, port=port, log="warning",
                              provider=None, model=None, db=None)


def test_vlm_host_from_env_is_used(monkeypatch):
    """Главный дефект: VLM_HOST=0.0.0.0 игнорировался, сервис слушал localhost."""
    captured = {}
    monkeypatch.setenv("VLM_HOST", "0.0.0.0")
    monkeypatch.setenv("VLM_API_TOKEN", "token-ascii")
    import importlib

    import vlmkit.api as api

    importlib.reload(api)
    import vlmkit.cli as cli

    monkeypatch.setattr("uvicorn.run",
                        lambda app, **kw: captured.update(kw))
    cli.cmd_serve(_serve_args(port=9999))
    assert captured["host"] == "0.0.0.0"
    monkeypatch.delenv("VLM_API_TOKEN", raising=False)


def test_flag_beats_env(monkeypatch):
    captured = {}
    monkeypatch.setenv("VLM_HOST", "0.0.0.0")
    monkeypatch.setenv("VLM_API_TOKEN", "token-ascii")
    import importlib

    import vlmkit.api as api

    importlib.reload(api)
    import vlmkit.cli as cli

    monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.update(kw))
    cli.cmd_serve(_serve_args(host="127.0.0.1", port=9999))
    assert captured["host"] == "127.0.0.1"
    monkeypatch.delenv("VLM_API_TOKEN", raising=False)


def test_public_host_without_token_is_refused(monkeypatch, capsys):
    """Защита обязана пережить починку VLM_HOST."""
    monkeypatch.setenv("VLM_HOST", "0.0.0.0")
    monkeypatch.delenv("VLM_API_TOKEN", raising=False)
    import importlib

    import vlmkit.api as api

    importlib.reload(api)
    import vlmkit.cli as cli

    assert cli.cmd_serve(_serve_args(port=9999)) == 2
    err = capsys.readouterr().err
    assert "нельзя публиковать" in err
    assert "secrets.token_urlsafe" in err, "нужна инструкция, как получить токен"


def test_port_from_env(monkeypatch):
    captured = {}
    monkeypatch.setenv("VLM_PORT", "9123")
    monkeypatch.delenv("VLM_HOST", raising=False)
    monkeypatch.delenv("VLM_API_TOKEN", raising=False)
    import importlib

    import vlmkit.api as api

    importlib.reload(api)
    import vlmkit.cli as cli

    monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.update(kw))
    cli.cmd_serve(_serve_args())
    assert captured["port"] == 9123
