"""Драйвер Anthropic Messages API.

Отличия от OpenAI, которые приходится разруливать здесь:
  * system — отдельное поле, а не первое сообщение;
  * инструменты описываются как input_schema, без обёртки "function";
  * ответ — массив блоков content, вызовы инструментов лежат блоками
    type="tool_use";
  * результат инструмента возвращается блоком tool_result внутри
    сообщения роли "user", а не отдельной ролью "tool".
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .base import BaseLLM, LLMError, LLMReply, ToolCall

API_VERSION = "2023-06-01"


class Anthropic(BaseLLM):
    name = "anthropic"
    supports_vision = True


    def __init__(
        self,
        model: str,
        base_url: str = "https://api.anthropic.com/v1",
        api_key: str | None = None,
        timeout: int = 120,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> None:
        super().__init__(model, **kwargs)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens

    # --- зрение: формат Anthropic отличается от базового (OpenAI-подобного):
    # блок image с source={type: base64, media_type, data}, без data:-URI.
    def build_vision_message(
        self, instruction: str, images: list[tuple[bytes, str]],
    ) -> dict[str, Any]:
        import base64
        content: list[dict[str, Any]] = [{"type": "text", "text": instruction}]
        for data, mime in images:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime,
                    "data": base64.b64encode(data).decode(),
                },
            })
        return {"role": "user", "content": content}

    # --- конвертация истории из внутреннего (OpenAI-подобного) вида ----
    @staticmethod
    def _split(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:

        system_parts: list[str] = []
        out: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role")
            if role == "system":
                system_parts.append(m.get("content") or "")
            elif role == "tool":
                # результат инструмента -> блок tool_result в user-сообщении
                out.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": m.get("tool_call_id", ""),
                        "content": m.get("content") or "",
                    }],
                })
            elif role == "assistant" and m.get("tool_calls"):
                blocks: list[dict[str, Any]] = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m["tool_calls"]:
                    fn = tc.get("function", {})
                    args = fn.get("arguments") or "{}"
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": args,
                    })
                out.append({"role": "assistant", "content": blocks})
            else:
                out.append({"role": role, "content": m.get("content") or ""})
        return "\n\n".join(p for p in system_parts if p), out

    def _chat_once(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMReply:
        system, msgs = self._split(messages)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": msgs,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [{
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t.get("parameters", {"type": "object"}),
            } for t in tools]

        req = urllib.request.Request(
            f"{self.base_url}/messages",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "anthropic-version": API_VERSION,
                **({"x-api-key": self.api_key} if self.api_key else {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            again = exc.code == 429 or 500 <= exc.code < 600
            raise LLMError(f"HTTP {exc.code} от {self.base_url}: {detail}",
                           retryable=again) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"Не достучались до {self.base_url}: {exc}",
                           retryable=True) from exc

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append(ToolCall(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    arguments=block.get("input") or {},
                ))
        return LLMReply(text="".join(text_parts), tool_calls=calls,
                        usage=self._usage_from(data), raw=data)
