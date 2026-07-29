"""Клиент MCP (Model Context Protocol) — подключение внешних серверов.

Зачем: веб-поиск, загрузка страниц, генерация картинок и речи не пишутся
в нашем коде. Они подключаются как MCP-серверы — те же, что вы уже
используете в LM Studio. Наша задача — говорить на их протоколе.

Протокол: JSON-RPC 2.0.
  initialize -> notifications/initialized -> tools/list -> tools/call

Два транспорта:
  stdio — сервер запускается как подпроцесс (обычный случай, как в LM Studio)
  http  — сервер уже слушает порт

ГЛАВНЫЙ ПРИНЦИП: недоступный сервер не ломает агента. Если сервер не
поднялся, не ответил или отдал мусор — он просто отключается, в лог
уходит понятная причина, остальная система работает. Иначе одна
битая интеграция обрушила бы восьмичасовой прогон.

Лимиты: у каждого сервера свой минимальный интервал между вызовами.
Поиск обычно платный и квотируемый (раз в 20 с), загрузка страниц —
бесплатная (без лимита).
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .tools.base import Tool, ToolError

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "agent-system", "version": "1.0.0"}


@dataclass
class MCPServerConfig:
    """Описание одного MCP-сервера из конфига."""
    name: str
    transport: str = "stdio"            # stdio | http
    command: str = ""                   # для stdio
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""                       # для http
    headers: dict[str, str] = field(default_factory=dict)
    rate_limit: float = 0.0             # минимум секунд между вызовами; 0 = без лимита
    timeout: int = 60
    enabled: bool = True
    only_tools: list[str] = field(default_factory=list)   # [] = все инструменты


class MCPError(Exception):
    """Ошибка обращения к MCP-серверу."""


# ---------------------------------------------------------------- транспорт
class _StdioTransport:
    """Сервер как подпроцесс: JSON-RPC построчно через stdin/stdout."""

    def __init__(self, cfg: MCPServerConfig) -> None:
        self.cfg = cfg
        self.proc: subprocess.Popen[str] | None = None
        self.lock = threading.Lock()
        self._id = 0

    def start(self) -> None:
        if not self.cfg.command:
            raise MCPError("для transport=stdio нужно поле command")
        env = {**os.environ, **self.cfg.env}
        try:
            self.proc = subprocess.Popen(
                [self.cfg.command, *self.cfg.args],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,      # шум сервера нам не нужен
                text=True, bufsize=1, env=env,
            )
        except (OSError, ValueError) as exc:
            raise MCPError(f"не запустить {self.cfg.command!r}: {exc}") from exc

    def call(self, method: str, params: dict[str, Any] | None,
             notify: bool = False) -> dict[str, Any]:
        if not self.proc or self.proc.poll() is not None:
            raise MCPError("процесс сервера не запущен или завершился")
        with self.lock:
            msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                msg["params"] = params
            if not notify:
                self._id += 1
                msg["id"] = self._id
            try:
                assert self.proc.stdin and self.proc.stdout
                self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise MCPError(f"канал к серверу оборван: {exc}") from exc
            if notify:
                return {}

            deadline = time.time() + self.cfg.timeout
            while time.time() < deadline:
                line = self.proc.stdout.readline()
                if not line:
                    raise MCPError("сервер закрыл поток вывода")
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue            # сервер мог напечатать не-JSON
                # уведомления сервера пропускаем, ждём свой ответ
                if data.get("id") != msg["id"]:
                    continue
                if "error" in data:
                    err = data["error"]
                    raise MCPError(f"{err.get('code')}: {err.get('message')}")
                return data.get("result") or {}
            raise MCPError(f"нет ответа за {self.cfg.timeout} с")

    def close(self) -> None:
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None


class _HttpTransport:
    """Сервер уже слушает порт: JSON-RPC поверх HTTP POST."""

    def __init__(self, cfg: MCPServerConfig) -> None:
        self.cfg = cfg
        self.lock = threading.Lock()
        self._id = 0
        self.session: str | None = None

    def start(self) -> None:
        if not self.cfg.url:
            raise MCPError("для transport=http нужно поле url")

    def call(self, method: str, params: dict[str, Any] | None,
             notify: bool = False) -> dict[str, Any]:
        with self.lock:
            msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                msg["params"] = params
            if not notify:
                self._id += 1
                msg["id"] = self._id
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                **self.cfg.headers,
            }
            if self.session:
                headers["Mcp-Session-Id"] = self.session
            req = urllib.request.Request(
                self.cfg.url, data=json.dumps(msg, ensure_ascii=False).encode(),
                headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
                    sid = resp.headers.get("Mcp-Session-Id")
                    if sid:
                        self.session = sid
                    body = resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                raise MCPError(f"HTTP {exc.code}: "
                               f"{exc.read().decode('utf-8', 'replace')[:200]}") from exc
            except urllib.error.URLError as exc:
                raise MCPError(f"нет связи с {self.cfg.url}: {exc.reason}") from exc
            if notify:
                return {}
            data = self._parse(body)
            if "error" in data:
                err = data["error"]
                raise MCPError(f"{err.get('code')}: {err.get('message')}")
            return data.get("result") or {}

    @staticmethod
    def _parse(body: str) -> dict[str, Any]:
        body = body.strip()
        if not body:
            return {}
        if body.startswith("{"):
            try:
                return json.loads(body)
            except json.JSONDecodeError as exc:
                raise MCPError(f"ответ не JSON: {body[:150]}") from exc
        # server-sent events: берём последний data:
        last = {}
        for line in body.splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk and chunk != "[DONE]":
                    try:
                        last = json.loads(chunk)
                    except json.JSONDecodeError:
                        pass
        if not last:
            raise MCPError(f"пустой SSE-ответ: {body[:150]}")
        return last

    def close(self) -> None:
        self.session = None


# ------------------------------------------------------------------ клиент
class MCPClient:
    """Один MCP-сервер: рукопожатие, список инструментов, вызовы, лимит."""

    def __init__(self, cfg: MCPServerConfig) -> None:
        self.cfg = cfg
        self.transport = (_StdioTransport(cfg) if cfg.transport == "stdio"
                          else _HttpTransport(cfg))
        self.tools: list[dict[str, Any]] = []
        self.ready = False
        self.error = ""
        self._last_call = 0.0
        self._gate = threading.Lock()

    def connect(self) -> bool:
        """Подключиться. Возвращает False вместо исключения: недоступный
        сервер не должен ронять систему."""
        try:
            self.transport.start()
            self.transport.call("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            })
            self.transport.call("notifications/initialized", {}, notify=True)
            res = self.transport.call("tools/list", {})
            tools = res.get("tools") or []
            if self.cfg.only_tools:
                allow = set(self.cfg.only_tools)
                tools = [t for t in tools if t.get("name") in allow]
            self.tools = tools
            self.ready = True
            return True
        except (MCPError, Exception) as exc:
            self.error = str(exc)[:300]
            self.ready = False
            try:
                self.transport.close()
            except Exception:
                pass
            return False

    def wait_for_slot(self) -> float:
        """Соблюсти лимит частоты. Возвращает, сколько пришлось ждать."""
        if self.cfg.rate_limit <= 0:
            return 0.0
        with self._gate:
            delta = time.time() - self._last_call
            wait = self.cfg.rate_limit - delta
            if wait > 0:
                time.sleep(wait)
            else:
                wait = 0.0
            self._last_call = time.time()
            return wait

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if not self.ready:
            raise MCPError(f"сервер {self.cfg.name!r} недоступен: {self.error}")
        waited = self.wait_for_slot()
        res = self.transport.call("tools/call",
                                  {"name": name, "arguments": arguments})
        text = self._render(res)
        if waited >= 1.0:
            text = f"[ожидание лимита {waited:.0f} с]\n{text}"
        return text

    @staticmethod
    def _render(res: dict[str, Any]) -> str:
        """Содержимое MCP-ответа в текст для модели."""
        if res.get("isError"):
            parts = [c.get("text", "") for c in res.get("content", [])
                     if isinstance(c, dict)]
            raise MCPError("сервер вернул ошибку: " + (" ".join(parts)[:300]
                                                       or "без описания"))
        out: list[str] = []
        for c in res.get("content") or []:
            if not isinstance(c, dict):
                continue
            kind = c.get("type")
            if kind == "text":
                out.append(c.get("text", ""))
            elif kind == "image":
                out.append(f"[изображение {c.get('mimeType', 'image')}, "
                           f"{len(c.get('data') or '')} байт base64]")
            elif kind == "resource":
                r = c.get("resource") or {}
                out.append(r.get("text") or f"[ресурс {r.get('uri', '')}]")
            else:
                out.append(json.dumps(c, ensure_ascii=False)[:500])
        if not out and res:
            out.append(json.dumps(res, ensure_ascii=False)[:1000])
        return "\n".join(p for p in out if p).strip() or "(пустой ответ)"

    def close(self) -> None:
        try:
            self.transport.close()
        except Exception:
            pass
        self.ready = False


# -------------------------------------------------------------------- пул
class MCPPool:
    """Все настроенные серверы. Раздаёт инструменты агенту."""

    def __init__(self, configs: list[MCPServerConfig]) -> None:
        self.clients: dict[str, MCPClient] = {}
        self.report: list[str] = []
        for cfg in configs:
            if not cfg.enabled:
                self.report.append(f"{cfg.name}: выключен в конфиге")
                continue
            client = MCPClient(cfg)
            if client.connect():
                lim = (f"не чаще раза в {cfg.rate_limit:g} с"
                       if cfg.rate_limit > 0 else "без лимита")
                self.report.append(
                    f"{cfg.name}: подключён, инструментов {len(client.tools)}, {lim}")
                self.clients[cfg.name] = client
            else:
                self.report.append(f"{cfg.name}: НЕДОСТУПЕН — {client.error}")

    def tools(self) -> list[Tool]:
        """Инструменты MCP в формате нашего реестра.

        Имя префиксуется именем сервера: два сервера могут иметь
        инструмент 'search', и без префикса они бы столкнулись.
        """
        out: list[Tool] = []
        for sname, client in self.clients.items():
            for spec in client.tools:
                tname = spec.get("name", "")
                if not tname:
                    continue
                full = f"{sname}_{tname}"

                def make(c: MCPClient = client, n: str = tname, f: str = full):
                    def run(**kwargs: Any) -> str:
                        try:
                            return c.call_tool(n, kwargs)
                        except MCPError as exc:
                            raise ToolError(f"MCP {f}: {exc}") from exc
                        except Exception as exc:
                            raise ToolError(
                                f"MCP {f} сбой ({type(exc).__name__}): {exc}"
                            ) from exc
                    return run

                schema = spec.get("inputSchema") or {"type": "object",
                                                     "properties": {}}
                desc = (spec.get("description") or f"Инструмент {tname}").strip()
                lim = client.cfg.rate_limit
                if lim > 0:
                    desc += f" (лимит: не чаще раза в {lim:g} с)"
                out.append(Tool(full, desc[:900], schema, make()))
        return out

    def status(self) -> str:
        return "\n".join(self.report) if self.report else "MCP-серверы не заданы"

    def close(self) -> None:
        for c in self.clients.values():
            c.close()
        self.clients.clear()


def configs_from_dict(data: dict[str, Any]) -> list[MCPServerConfig]:
    """Разбор секции mcp из конфига.

    Принимает обе формы записи — с обёрткой "servers" и без неё:
        {"servers": {"web": {...}}}   и   {"web": {...}}
    Иначе легко получить молча пустой список: ключ "servers" будет
    воспринят как имя сервера (эта ошибка была поймана тестом).
    """
    data = data or {}
    if "servers" in data and isinstance(data["servers"], dict):
        data = data["servers"]
    out: list[MCPServerConfig] = []
    for name, raw in data.items():
        if not isinstance(raw, dict):
            continue
        out.append(MCPServerConfig(
            name=name,
            transport=raw.get("transport", "stdio"),
            command=raw.get("command", ""),
            args=list(raw.get("args") or []),
            env={k: str(v) for k, v in (raw.get("env") or {}).items()},
            url=raw.get("url", ""),
            headers=dict(raw.get("headers") or {}),
            rate_limit=float(raw.get("rate_limit", 0) or 0),
            timeout=int(raw.get("timeout", 60)),
            enabled=bool(raw.get("enabled", True)),
            only_tools=list(raw.get("only_tools") or []),
        ))
    return out
