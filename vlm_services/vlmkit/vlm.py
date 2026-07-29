"""Фабрика VLM и оффлайн-заглушка.

Сервисы не знают, какой провайдер за спиной: просят `get_vlm()` и получают
chat-model LangChain, умеющий принимать картинки.

`FakeVLM` — не «пустышка ради зелёных тестов». Она читает `scene` из
переданных изображений и отвечает так, будто действительно их посмотрела.
Благодаря этому тесты проверяют логику сервиса (пороги, арифметику,
формулировки), а не фантазию модели, и весь набор гоняется без ключей.
"""

from __future__ import annotations

import json
import re
from typing import Any, Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from .config import Settings, settings as default_settings
from .images import ImageRef

SCENE_MARK = "OFFLINE_SCENE:"
SCHEMA_MARK = "JSON_SCHEMA_HINT:"


def build_message(prompt: str, images: Sequence[ImageRef], scenes_for_fake: bool = False
                  ) -> HumanMessage:
    """Собрать мультимодальное сообщение: текст + картинки.

    Для оффлайн-провайдера дополнительно вкладываем описание сцены текстом:
    заглушка не умеет смотреть, зато умеет читать.
    """
    parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for img in images:
        parts.append({"type": "image_url", "image_url": {"url": img.to_data_uri()}})
    if scenes_for_fake:
        scenes = [{"name": i.name or f"img{n}", **i.scene}
                  for n, i in enumerate(images) if i.scene]
        if scenes:
            parts[0] = {"type": "text",
                        "text": prompt + f"\n\n{SCENE_MARK} "
                                + json.dumps(scenes, ensure_ascii=False)}
    return HumanMessage(content=parts)


class FakeVLM(BaseChatModel):
    """Оффлайн-модель: отвечает по описанию сцены и схеме ответа.

    Правила простые и предсказуемые:
    - если в промпте есть `OFFLINE_SCENE:` — раскладываем его в ответ,
      подгоняя под форму `JSON_SCHEMA_HINT:`;
    - иначе возвращаем сам скелет схемы;
    - если схемы нет — короткое текстовое эхо.
    """

    @property
    def _llm_type(self) -> str:
        return "fake-vlm"

    def _generate(self, messages: Sequence[BaseMessage], stop: list[str] | None = None,
                  run_manager: Any = None, **kwargs: Any) -> ChatResult:
        text = _flatten(messages)
        scenes = _extract(text, SCENE_MARK)
        schema = _extract(text, SCHEMA_MARK)

        if schema is not None:
            filled = _fill(schema, scenes if scenes is not None else [])
            return _res(json.dumps(filled, ensure_ascii=False))
        if scenes is not None:
            return _res(json.dumps(scenes, ensure_ascii=False))
        return _res("[offline-vlm] " + text.strip()[-400:])


def _res(content: str) -> ChatResult:
    return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])


def _flatten(messages: Sequence[BaseMessage]) -> str:
    out: list[str] = []
    for m in messages:
        c = m.content
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("type") == "text":
                    out.append(str(part.get("text", "")))
    return "\n".join(out)


def _extract(text: str, marker: str) -> Any | None:
    """Достать JSON, идущий сразу за маркером."""
    if marker not in text:
        return None
    tail = text.split(marker, 1)[1].strip()
    depth, end, instr, esc = 0, None, False, False
    for i, ch in enumerate(tail):
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        return None
    try:
        return json.loads(tail[:end])
    except json.JSONDecodeError:
        return None


def _fill(schema: Any, scenes: Any) -> Any:
    """Подставить данные сцены в скелет ответа.

    Списки в схеме описывают форму одного элемента, поэтому размножаем их
    по числу сцен: так заглушка выдаёт по записи на каждое изображение.
    """
    if isinstance(schema, dict):
        merged: dict[str, Any] = {}
        flat = _merge_scenes(scenes)
        for key, proto in schema.items():
            if key in flat:
                merged[key] = flat[key]
            elif isinstance(proto, (dict, list)):
                merged[key] = _fill(proto, scenes)
            else:
                merged[key] = proto
        return merged
    if isinstance(schema, list):
        if not schema:
            return []
        proto = schema[0]
        items = scenes if isinstance(scenes, list) and scenes else []
        if not items:
            return []
        if isinstance(proto, dict):
            return [_fill(proto, [it]) for it in items]
        return [it if isinstance(it, type(proto)) else proto for it in items]
    return schema


def _merge_scenes(scenes: Any) -> dict[str, Any]:
    """Свести сцены нескольких кадров в одну.

    Списки склеиваются, а не перекрываются: иначе многокадровые сервисы
    (тренды, полка, оценка предмета) в оффлайне видели бы только первое
    изображение, и их логику нельзя было бы протестировать.
    """
    if isinstance(scenes, dict):
        return scenes
    out: dict[str, Any] = {}
    if isinstance(scenes, list):
        for s in scenes:
            if not isinstance(s, dict):
                continue
            for k, v in s.items():
                if isinstance(v, list) and isinstance(out.get(k), list):
                    out[k] = out[k] + v
                else:
                    out.setdefault(k, v)
    return out


def get_vlm(cfg: Settings | None = None, **overrides: Any) -> BaseChatModel:
    cfg = cfg or default_settings()
    provider = str(overrides.pop("provider", cfg.provider)).lower()
    model = overrides.pop("model", None) or cfg.resolved_model()

    if provider in ("fake", "offline", "echo"):
        return FakeVLM(**overrides)

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model, temperature=cfg.temperature, api_key=cfg.api_key,
                          base_url=cfg.base_url, max_tokens=cfg.max_tokens, **overrides)

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=model, temperature=cfg.temperature, api_key=cfg.api_key,
                             max_tokens=cfg.max_tokens, **overrides)

    if provider == "ollama":
        from langchain_openai import ChatOpenAI

        # Ollama говорит по OpenAI-совместимому протоколу, но с оговорками:
        # 1) локальные VLM гораздо охотнее отдают валидный JSON, если явно
        #    попросить формат — иначе они добавляют преамбулу и пояснения;
        # 2) таймаут по умолчанию мал: на CPU кадр обрабатывается минутами.
        opts: dict[str, Any] = {
            "model": model,
            "temperature": cfg.temperature,
            "base_url": cfg.base_url or "http://localhost:11434/v1",
            "api_key": cfg.api_key or "ollama",
            "timeout": cfg.request_timeout_s,
            "max_retries": 0,  # повторы делает наш Runner, со своей паузой
        }
        if cfg.force_json:
            opts["model_kwargs"] = {"response_format": {"type": "json_object"}}
        opts.update(overrides)
        return ChatOpenAI(**opts)

    raise ValueError(f"неизвестный провайдер VLM: {provider!r}")


def is_offline(cfg: Settings) -> bool:
    return str(cfg.provider).lower() in ("fake", "offline", "echo")
