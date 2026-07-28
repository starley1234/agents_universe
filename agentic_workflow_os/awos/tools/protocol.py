"""Протокол вызова инструмента: строгий JSON в тексте ответа модели.

ПОЧЕМУ НЕ НАТИВНЫЙ TOOL-CALLING OpenAI. Среда обязана одинаково
работать с облачной моделью и с локальной 7B в LM Studio. Поддержка
`tools` у локальных сборок неровная: одни игнорируют поле, другие
возвращают его в нестандартном виде, третьи ломаются на параллельных
вызовах. Единый текстовый протокол снимает этот класс проблем целиком
и, что важнее для платформы, делает вызов ВИДИМЫМ: он лежит в тексте,
попадает в журнал как есть и воспроизводится при разборе инцидента.

ФОРМАТ. Модель выводит блок:

    ```tool
    {"tool": "read_file", "args": {"path": "notes.md"}}
    ```

Разбор устроен снисходительно к оформлению и строго к содержанию:
  * блок ищется и с меткой ```tool, и в обычном ```json / ``` — модели
    путают метку постоянно, а намерение однозначно;
  * допускается голый JSON-объект в тексте, если у него есть ключ
    "tool" — так отвечают модели без опыта работы с markdown;
  * НЕ допускается: неизвестное имя инструмента, args не-объект,
    несколько вызовов в одном ответе (среда исполняет по одному, чтобы
    журнал оставался линейным, а подтверждение человека — осмысленным).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

#: ```tool ... ``` / ```json ... ``` / ``` ... ```
FENCE_RE = re.compile(
    r"```[ \t]*(tool|json)?[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)

#: Маркер завершения работы — модель говорит «готово, вот результат».
#: Нужен, чтобы отличить «я закончил» от «я задумался и молчу».
FINAL_MARKERS = ("<final>", "[final]", "финальный ответ:", "final answer:")


@dataclass
class ToolCall:
    tool: str
    args: dict[str, Any]
    raw: str = ""


class ProtocolError(Exception):
    """Модель попыталась вызвать инструмент, но нарушила формат.

    Текст ошибки уходит модели как результат — обычно со второй попытки
    формат становится верным. Это дешевле, чем валить шаг.
    """


def _try_json(chunk: str) -> dict[str, Any] | None:
    chunk = chunk.strip()
    if not chunk.startswith("{"):
        return None
    try:
        data = json.loads(chunk)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _balanced_objects(text: str) -> list[str]:
    """Найти верхнеуровневые {...} без регулярок (они не считают вложенность)."""
    out: list[str] = []
    depth = 0
    start = -1
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    out.append(text[start:i + 1])
                    start = -1
    return out


def extract_call(text: str, known: set[str] | None = None) -> ToolCall | None:
    """Вытащить вызов инструмента из ответа модели. Нет вызова -> None."""
    if not text:
        return None

    candidates: list[tuple[str, dict[str, Any]]] = []
    for m in FENCE_RE.finditer(text):
        data = _try_json(m.group(2))
        if data is not None:
            candidates.append((m.group(0), data))

    if not candidates:
        for chunk in _balanced_objects(text):
            data = _try_json(chunk)
            if data is not None and "tool" in data:
                candidates.append((chunk, data))

    payloads = [(raw, d) for raw, d in candidates if "tool" in d]
    if not payloads:
        return None

    if len(payloads) > 1:
        raise ProtocolError(
            f"В ответе {len(payloads)} вызовов инструментов. Среда выполняет "
            "ровно один вызов за ход — оставьте первый, остальные повторите "
            "следующим сообщением.")

    raw, data = payloads[0]
    name = data.get("tool")
    if not isinstance(name, str) or not name.strip():
        raise ProtocolError('Поле "tool" должно быть непустой строкой с именем '
                            "инструмента.")
    name = name.strip()

    args = data.get("args", {})
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ProtocolError(
            f'Поле "args" должно быть объектом, получено {type(args).__name__}. '
            'Пример: {"tool": "read_file", "args": {"path": "notes.md"}}')

    if known is not None and name not in known:
        raise ProtocolError(
            f"Инструмент {name!r} на этом шаге недоступен. Доступны: "
            f"{', '.join(sorted(known)) or '— ни одного'}.")

    return ToolCall(tool=name, args=args, raw=raw)


def strip_calls(text: str) -> str:
    """Убрать блоки вызовов из текста — остаётся содержательный ответ."""
    cleaned = FENCE_RE.sub("", text)
    return cleaned.strip()


def is_final(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in FINAL_MARKERS)


def protocol_prompt(tool_prompt: str) -> str:
    """Кусок системного промпта с описанием протокола и списком инструментов."""
    if not tool_prompt:
        return ("Инструментов на этом шаге нет — отвечай текстом, опираясь "
                "только на данные из задачи и контекста.")
    return (
        "У тебя есть инструменты среды. Чтобы вызвать инструмент, выведи "
        "РОВНО ОДИН блок и ничего после него:\n"
        "```tool\n"
        '{"tool": "имя", "args": {"аргумент": "значение"}}\n'
        "```\n"
        "Среда выполнит вызов и пришлёт результат следующим сообщением; "
        "затем продолжай. Когда работа готова — отвечай обычным текстом "
        "без блока ```tool.\n\n"
        "Доступные инструменты:\n" + tool_prompt
    )
