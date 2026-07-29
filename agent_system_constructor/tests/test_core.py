"""Ядро: парсинг ответов модели, состояние, фабрика LLM, реестр."""

from __future__ import annotations

import pytest

from aconstructor import REGISTRY, Agent, get_llm, load_registry, mermaid, new_state, settings
from aconstructor.core import parse_json
from aconstructor.llm import EchoChatModel
from aconstructor.textutil import coverage, cosine, jaccard, norm_code


def test_registry_has_all_seven():
    reg = load_registry()
    assert set(reg) == {
        "patent-clearance", "synthetic-buyer", "doc-restorer", "energy-hacker",
        "formula-reverse", "cert-validator", "urban-scout",
    }
    for p in reg.values():
        assert p.title and p.summary and p.agents


@pytest.mark.parametrize("slug", sorted(load_registry()))
def test_graph_compiles_and_draws(slug):
    m = mermaid(slug)
    assert "__start__" in m and "__end__" in m


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('Вот ответ: {"a": 1} — готово', {"a": 1}),
        ("[1, 2]", [1, 2]),
        ("совсем не json", None),
    ],
)
def test_parse_json(raw, expected):
    assert parse_json(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("{'a': 1}", {"a": 1}),                       # одинарные кавычки
        ('{"a": 1, "b": 2,}', {"a": 1, "b": 2}),      # висящая запятая
        ('{"ok": True, "x": None}', {"ok": True, "x": None}),
        ('{"a": 1, // коммент\n "b": 2}', {"a": 1, "b": 2}),
    ],
)
def test_parse_json_tolerates_local_model_quirks(raw, expected):
    """Так отвечают локальные модели через Ollama; без починки — пустой ответ."""
    assert parse_json(raw) == expected


def test_parse_json_recovers_truncated_list():
    """Ответ обрезан лимитом токенов: целые элементы важнее пустоты."""
    out = parse_json('{"elements": [{"n": 1}, {"n": 2}, {"n"')
    assert out == {"elements": [{"n": 1}, {"n": 2}]}


def test_parse_json_keeps_apostrophe():
    assert parse_json('{"name": "Ivan\'s"}') == {"name": "Ivan's"}


def test_echo_model_returns_schema_hint():
    a = Agent(name="t", system="роль", schema_hint={"x": 0, "y": ""}, llm=EchoChatModel())
    assert a.run_json("вопрос") == {"x": 0, "y": ""}


def test_agent_survives_broken_model():
    class Boom(EchoChatModel):
        def _generate(self, *a, **k):
            raise RuntimeError("модель недоступна")

    a = Agent(name="t", system="роль", schema_hint={"x": 1}, llm=Boom())
    with pytest.raises(RuntimeError):
        a.run_json("вопрос")


def test_get_llm_offline_by_default():
    assert isinstance(get_llm(settings()), EchoChatModel)


def test_get_llm_rejects_unknown_provider():
    from dataclasses import replace

    with pytest.raises(ValueError, match="неизвестный провайдер"):
        get_llm(replace(settings(), provider="нетакого"))


def test_new_state_shape():
    s = new_state({"a": 1})
    assert s["task"] == {"a": 1}
    assert s["trace"] == [] and s["findings"] == [] and s["errors"] == []


def test_text_metrics():
    assert jaccard("rolling hash window", "rolling hash window") == 1.0
    assert jaccard("rolling hash", "phase change material") == 0.0
    assert cosine("chunk index fingerprint", "fingerprint chunk index") > 0.9
    assert norm_code("MS-21042 L4") == norm_code("ms21042l4")


def test_coverage_matches_reordered_phrases():
    cov, hits = coverage(
        ["rolling hash window", "distributed chunk index"],
        "we compute a hash while rolling a window; the chunk index is distributed",
    )
    assert cov == 1.0 and len(hits) == 2


def test_coverage_empty_needles():
    assert coverage([], "любой текст") == (0.0, [])
