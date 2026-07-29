"""Ядро: разбор ответов, приведение чисел, реестр, оффлайн-модель."""

from __future__ import annotations

import pytest

from vlmkit import REGISTRY, get_service, load_registry, run_service, settings
from vlmkit.core import Service, ServiceError, as_float, as_int, parse_json, table
from vlmkit.demo import demo_image
from vlmkit.vlm import FakeVLM, build_message, get_vlm


def test_registry_has_twelve_services():
    reg = load_registry()
    assert len(reg) == 12
    assert set(reg) == {
        "pim-cards", "retail-audit", "site-safety", "blueprint-estimator",
        "ux-critic", "trend-scout", "nutrition-plate", "sight-assistant",
        "doc-extractor", "content-moderator", "appraiser", "repair-guide",
    }


@pytest.mark.parametrize("slug", sorted(load_registry()))
def test_every_service_has_metadata(slug):
    cls = REGISTRY[slug]
    assert cls.title and cls.summary and cls.system
    assert cls.min_images >= 1 and cls.max_images >= cls.min_images


@pytest.mark.parametrize(
    "raw,expected",
    [('{"a":1}', {"a": 1}),
     ('```json\n{"a":1}\n```', {"a": 1}),
     ('Вот: {"a":1} готово', {"a": 1}),
     ("[1,2]", [1, 2]),
     ("не json", None)],
)
def test_parse_json(raw, expected):
    assert parse_json(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [(5, 5.0), ("12,5", 12.5), ("~3 шт", 3.0), ("1 200", 1200.0),
     ("нет", 0.0), (None, 0.0), (True, 0.0)],
)
def test_as_float_handles_model_output(raw, expected):
    """Модель пишет числа как попало — приведение обязано это переживать."""
    assert as_float(raw) == expected


def test_as_int_rounds():
    assert as_int("2,6") == 3 and as_int(None, 7) == 7


def test_table_empty_returns_nothing():
    assert table(["a"], []) == []


def test_fake_vlm_is_default():
    assert isinstance(get_vlm(settings()), FakeVLM)


def test_unknown_provider_rejected():
    from dataclasses import replace

    with pytest.raises(ValueError, match="неизвестный провайдер"):
        get_vlm(replace(settings(), provider="нетакого"))


def test_build_message_carries_images():
    img = demo_image("a.png", {"x": 1})
    msg = build_message("вопрос", [img])
    kinds = [p["type"] for p in msg.content]
    assert kinds == ["text", "image_url"]
    assert msg.content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_build_message_hides_scene_from_real_providers():
    """Описание сцены — костыль оффлайн-режима, реальной модели он не уходит."""
    img = demo_image("a.png", {"secret": "подсказка"})
    real = build_message("вопрос", [img], scenes_for_fake=False)
    fake = build_message("вопрос", [img], scenes_for_fake=True)
    assert "подсказка" not in real.content[0]["text"]
    assert "подсказка" in fake.content[0]["text"]


def test_fake_vlm_fills_schema_from_scene():
    from langchain_core.messages import SystemMessage

    llm = FakeVLM()
    img = demo_image("a.png", {"color": "синий"})
    msg = build_message("q", [img], scenes_for_fake=True)
    sys = SystemMessage(content='JSON_SCHEMA_HINT: {"color": "", "size": ""}')
    out = parse_json(str(llm.invoke([sys, msg]).content))
    assert out["color"] == "синий"
    assert out["size"] == ""


def test_service_rejects_too_few_images():
    svc = get_service("pim-cards")
    with pytest.raises(ServiceError, match="минимум"):
        svc.run([])


def test_service_rejects_too_many_images():
    from vlmkit.images import ImageError

    svc = get_service("pim-cards")  # max_images = 3
    with pytest.raises(ImageError, match="слишком много"):
        svc.run([demo_image(f"{i}.png") for i in range(5)])


def test_unknown_service():
    with pytest.raises(KeyError, match="не найден"):
        get_service("нетакого")


@pytest.mark.parametrize("slug", sorted(load_registry()))
def test_demo_runs_end_to_end(slug):
    r = run_service(slug)
    assert r.service == slug
    assert r.report.startswith("#"), "отчёт должен быть markdown-документом"
    assert r.data, "данные не должны быть пустыми"
    assert r.images, "информация об изображениях обязательна"
    assert r.duration_s >= 0


@pytest.mark.parametrize("slug", sorted(load_registry()))
def test_result_is_json_serializable(slug):
    """Результат уходит в HTTP — он обязан сериализоваться без обходных путей."""
    import json

    json.dumps(run_service(slug).as_dict(), ensure_ascii=False)


def test_unknown_param_is_rejected():
    """Опечатка «min_sos_pc» вместо «min_sos_pct» не должна проходить молча."""
    svc = get_service("retail-audit")
    with pytest.raises(ServiceError, match="неизвестные параметры"):
        svc.run([demo_image("a.png")], min_sos_pc=50)


def test_known_params_listed_in_error():
    svc = get_service("retail-audit")
    try:
        svc.run([demo_image("a.png")], опечатка=1)
    except ServiceError as e:
        assert "min_sos_pct" in str(e) and "our_brand" in str(e)


@pytest.mark.parametrize("slug", sorted(load_registry()))
def test_demo_params_are_valid_for_service(slug):
    """Демо-параметры обязаны совпадать с сигнатурой analyze."""
    svc = get_service(slug)
    unknown = set(svc.demo().get("params", {})) - svc.known_params()
    assert not unknown, f"{slug}: демо передаёт неизвестные параметры {unknown}"


def test_warnings_not_leaked_into_data():
    """`_warnings` — служебное поле, наружу оно идёт отдельным списком."""
    r = run_service("pim-cards")
    assert "_warnings" not in r.data
    assert isinstance(r.warnings, list)
