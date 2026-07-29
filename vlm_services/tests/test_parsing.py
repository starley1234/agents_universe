"""Разбор ответов модели.

Отдельный файл, потому что это самое хрупкое место при работе с
локальными моделями: облачные почти всегда отдают чистый JSON, а
Ollama/llama.cpp регулярно нарушают формат. Каждый неразобранный ответ —
это пустой отчёт у пользователя.
"""

from __future__ import annotations

import pytest

from vlmkit.core import get_service, parse_json
from vlmkit.demo import demo_image
from vlmkit.vlm import FakeVLM


@pytest.mark.parametrize("raw,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ('```\n{"a": 1}\n```', {"a": 1}),
    ('Sure! Here is the JSON:\n{"a": 1}', {"a": 1}),
    ('Вот результат: {"a": 1}. Надеюсь, помог!', {"a": 1}),
])
def test_parses_clean_and_wrapped(raw, expected):
    assert parse_json(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("{'a': 1, 'b': 'текст'}", {"a": 1, "b": "текст"}),          # одинарные кавычки
    ('{"a": 1, "b": 2,}', {"a": 1, "b": 2}),                     # висящая запятая
    ('{"a": 1, // коммент\n "b": 2}', {"a": 1, "b": 2}),          # комментарий
    ('{"ok": True, "x": None}', {"ok": True, "x": None}),         # питоньи литералы
    ('{"x": undefined}', {"x": None}),
])
def test_parses_local_model_quirks(raw, expected):
    """Так отвечают локальные VLM. Без починки сервис вернул бы пустышку."""
    assert parse_json(raw) == expected


def test_apostrophe_inside_valid_json_survives():
    """Замена кавычек не должна ломать корректный ответ с апострофом."""
    assert parse_json('{"name": "Ivan\'s shop"}') == {"name": "Ivan's shop"}


def test_recovers_truncated_response():
    """Ответ обрезан лимитом токенов: три товара лучше, чем ничего."""
    raw = ('{"items": [{"name": "a", "count": 1}, {"name": "b", "count": 2}, '
           '{"name": "c"')
    out = parse_json(raw)
    assert out is not None
    assert [i["name"] for i in out["items"]] == ["a", "b"], (
        "оба целых товара обязаны сохраниться, неполный — отброшен")


def test_recovers_truncated_inside_string():
    out = parse_json('{"items": [{"name": "первый"}, {"name": "второ')
    assert out is not None and out["items"][0]["name"] == "первый"


@pytest.mark.parametrize("raw,expected", [
    ('{"a": 1, "b": 2', {"a": 1, "b": 2}),        # плоский объект
    ("[1, 2, 3", [1, 2]),                          # массив чисел
    ('{"a": 1, "b":', {"a": 1}),                   # обрыв на двоеточии
    ('{"a": 1, "b": "незаверш', {"a": 1}),         # обрыв внутри строки
])
def test_recovers_flat_truncation(raw, expected):
    assert parse_json(raw) == expected


@pytest.mark.parametrize("raw", ["{", "[", "{  "])
def test_empty_skeleton_is_not_data(raw):
    """«{» — не пустой результат, а неразобранный ответ; нужен сигнал."""
    assert parse_json(raw) is None


@pytest.mark.parametrize("raw", ["", "   ", "не могу ответить", "```\n```", None])
def test_returns_none_on_garbage(raw):
    """Мусор обязан остаться мусором, а не превратиться в выдуманные данные."""
    assert parse_json(raw) is None


def test_array_response():
    assert parse_json("[1, 2, 3]") == [1, 2, 3]


class BrokenVLM(FakeVLM):
    """Модель, отвечающая прозой вместо JSON."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult

        return ChatResult(generations=[ChatGeneration(
            message=AIMessage(content="Извините, не могу разобрать изображение."))])


def test_unparsable_answer_becomes_visible_warning():
    """Пустой каркас схемы неотличим от «ничего не найдено» — нужен сигнал."""
    svc = get_service("retail-audit")
    svc._vlm = BrokenVLM()
    res = svc.run([demo_image("a.png", {})])
    assert any("не по схеме" in w for w in res.warnings)
    assert any("VLM_FORCE_JSON" in w for w in res.warnings), "нужна подсказка, что делать"


def test_warning_is_first_so_it_is_not_lost():
    svc = get_service("retail-audit")
    svc._vlm = BrokenVLM()
    res = svc.run([demo_image("a.png", {})])
    assert "не по схеме" in res.warnings[0]


def test_good_answer_produces_no_parse_warning():
    svc = get_service("retail-audit")
    res = svc.run([demo_image("a.png", {"facings": [
        {"brand": "A", "product": "x", "count": 3, "price_tag": True}],
        "empty_slots": 0})])
    assert not any("не по схеме" in w for w in res.warnings)


# --- настройка провайдера --------------------------------------------------
def test_ollama_gets_json_mode_and_long_timeout():
    """На CPU кадр считается минутами, а строгий JSON спасает локальные модели."""
    from dataclasses import replace

    from vlmkit.config import settings
    from vlmkit.vlm import get_vlm

    cfg = replace(settings(), provider="ollama", model="qwen2.5vl:7b",
                  request_timeout_s=300.0)
    m = get_vlm(cfg)
    assert m.model_kwargs["response_format"] == {"type": "json_object"}
    assert m.request_timeout == 300.0
    assert "11434" in str(m.openai_api_base)


def test_force_json_can_be_disabled():
    """Не все сборки Ollama принимают response_format — нужен выключатель."""
    from dataclasses import replace

    from vlmkit.config import settings
    from vlmkit.vlm import get_vlm

    cfg = replace(settings(), provider="ollama", force_json=False)
    assert get_vlm(cfg).model_kwargs == {}
