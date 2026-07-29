"""HTTP API MAOS на стандартной библиотеке — без внешних зависимостей.

Эндпоинты REST (ТЗ п.7):
  GET  /health              — жив ли сервис (без токена)
  POST /v1/chat             — {"message": "...", "conversation_id"?,
                               "agent_slug"?} -> ответ агента
  GET  /v1/agents           — список агентов
  GET  /v1/memory/stats     — статистика БД/токенов

Плюс сопутствующие маршруты для Admin Panel/Chat UI/Graph Visualization
(дашборд, /dashboard):
  POST   /v1/agents               — создать агента
  GET    /v1/agents/<slug>        — детали агента
  PATCH  /v1/agents/<slug>        — обновить агента
  DELETE /v1/agents/<slug>        — удалить агента
  GET    /v1/conversations        — список диалогов
  GET    /v1/conversations/<id>   — сообщения диалога
  GET    /v1/graph                — граф онтологии для визуализации
  POST   /v1/chain/start          — {"goal", "agents": [slug, ...]}
  GET    /v1/chain/<id>           — статус+шаги цепочки
  POST   /v1/maintenance/run      — запустить один цикл обслуживания вручную
  GET    /v1/onboarding/status    — пуста ли база / есть ли демо-агенты
                                    (для приветственного баннера дашборда)
  POST   /v1/onboarding/seed      — создать демо-агентов на РЕАЛЬНО
                                    подключённой базе (не только в
                                    quickstart-режиме — полезно и на
                                    "боевом" DB_DSN, если хочется быстро
                                    посмотреть, как всё работает)
  GET    /v1/tts/voices           — список голосов сервера TTS (OmniVoice)
  POST   /v1/tts/speak            — {"text","voice"?,"audio_format"?} ->
                                    БИНАРНЫЙ аудио-ответ (Content-Type:
                                    audio/*), голос агента по умолчанию,
                                    если voice не передан и указан
                                    agent_slug

Токен: если задан MAOS_API_TOKEN (или cfg.api_token), требуется заголовок
Authorization: Bearer <token>. Без него сервер слушает только localhost —
как в agent_system, отказ стартовать при host != 127.0.0.1 без токена.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..config import Config
from ..demo_seed import demo_agents_status, seed_demo_agents
from ..llm.embeddings import build_embedder
from ..maintenance.service import MaintenanceService
from ..memory.store import Store, StoreError
from ..orchestrator.chain import ChainError, ChainRunner
from ..orchestrator.service import Orchestrator
from ..tts.provider import TTSError, build_tts_provider

MAX_BODY = 1_000_000

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_AGENT_WRITABLE_FIELDS = (
    "name", "description", "keywords", "avatar", "voice_provider",
    "voice_id", "llm_ref", "system_prompt", "tools", "enabled",
)


def _make_embedder(cfg: Config):
    provider, model, base_url, api_key, timeout = cfg.resolve_embedding()
    return build_embedder(provider, model, dim=cfg.embedding_dim,
                          base_url=base_url, api_key=api_key, timeout=timeout)


def _make_tts(cfg: Config, voice_override: str = "", format_override: str = ""):
    return build_tts_provider(
        cfg.tts_provider, voice_id=voice_override,
        base_url=cfg.tts_base_url, api_key=cfg.tts_api_key,
        timeout=cfg.tts_timeout,
        audio_format=format_override or cfg.tts_audio_format)


class Handler(BaseHTTPRequestHandler):
    cfg: Config
    token: str | None = None
    server_version = "MAOS/0.1"

    # --- служебное ------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[maos] {self.address_string()} {fmt % args}")

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path: Path) -> None:
        if not path.exists():
            self._send(404, {"error": "страница не найдена"})
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_audio(self, audio: bytes, mime: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", str(len(audio)))
        self.end_headers()
        self.wfile.write(audio)

    def _authorized(self) -> bool:
        if not self.token:
            return True
        got = self.headers.get("Authorization", "")
        return got == f"Bearer {self.token}"

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _store(self) -> Store:
        return Store(self.cfg.require_dsn(), dim=self.cfg.embedding_dim)

    # --- маршруты ---------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        if path == "/health":
            self._send(200, {"status": "ok"})
            return
        if path in ("/", "/dashboard"):
            self._send_html(Path(__file__).resolve().parent.parent / "web" / "dashboard.html")
            return
        if not self._authorized():
            self._send(401, {"error": "нужен заголовок Authorization: Bearer <token>"})
            return
        try:
            self._route_get(path, qs)
        except StoreError as exc:
            self._send(503, {"error": str(exc)})
        except TTSError as exc:
            self._send(502, {"error": str(exc)})
        except (ChainError, ValueError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _route_get(self, path: str, qs: dict[str, list[str]]) -> None:
        def qi(name: str, default: int) -> int:
            v = qs.get(name)
            try:
                return int(v[0]) if v else default
            except ValueError:
                return default

        if path == "/info":
            self._send(200, {"config": self.cfg.to_dict()})
            return
        if path == "/v1/agents":
            with self._store() as st:
                self._send(200, {"agents": st.list_agents()})
            return
        if path.startswith("/v1/agents/"):
            slug = path[len("/v1/agents/"):]
            with self._store() as st:
                agent = st.get_agent(slug)
                if not agent:
                    self._send(404, {"error": f"агент {slug!r} не найден"})
                    return
                self._send(200, {"agent": agent})
            return
        if path == "/v1/memory/stats":
            with self._store() as st:
                self._send(200, st.memory_stats())
            return
        if path == "/v1/conversations":
            with self._store() as st:
                self._send(200, {"conversations": st.list_conversations(qi("limit", 50))})
            return
        if path.startswith("/v1/conversations/"):
            cid = int(path[len("/v1/conversations/"):])
            with self._store() as st:
                conv = st.get_conversation(cid)
                if not conv:
                    self._send(404, {"error": f"диалог {cid} не найден"})
                    return
                self._send(200, {"conversation": conv, "messages": st.messages(cid)})
            return
        if path == "/v1/graph":
            with self._store() as st:
                self._send(200, st.graph_data(qi("limit", 500)))
            return
        if path == "/v1/chains":
            with self._store() as st:
                self._send(200, {"chains": st.list_chains(qi("limit", 50))})
            return
        if path.startswith("/v1/chain/"):
            chain_id = int(path[len("/v1/chain/"):])
            with self._store() as st:
                chain = st.get_chain(chain_id)
                if not chain:
                    self._send(404, {"error": f"цепочка {chain_id} не найдена"})
                    return
                self._send(200, {"chain": chain, "steps": st.chain_steps(chain_id)})
            return
        if path == "/v1/onboarding/status":
            with self._store() as st:
                self._send(200, demo_agents_status(st))
            return
        if path == "/v1/tts/voices":
            tts = _make_tts(self.cfg)
            self._send(200, {"voices": tts.list_voices()})
            return
        self._send(404, {"error": f"нет маршрута {path}"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._send(401, {"error": "нужен заголовок Authorization: Bearer <token>"})
            return
        data = self._read_json()
        if data is None:
            self._send(400, {"error": "ожидается JSON в теле запроса"})
            return
        try:
            self._route_post(self.path, data)
        except StoreError as exc:
            self._send(503, {"error": str(exc)})
        except TTSError as exc:
            self._send(502, {"error": str(exc)})
        except (ChainError, ValueError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _route_post(self, path: str, data: dict[str, Any]) -> None:
        if path == "/v1/chat":
            message = (data.get("message") or "").strip()
            if not message:
                self._send(400, {"error": "поле 'message' обязательно"})
                return
            with self._store() as st:
                embedder = _make_embedder(self.cfg)
                orch = Orchestrator(self.cfg, st, embedder)
                result = orch.chat(
                    message, conversation_id=data.get("conversation_id"),
                    agent_slug=data.get("agent_slug"))
                self._send(200, {
                    "conversation_id": result.conversation_id,
                    "agent_slug": result.agent_slug,
                    "route_method": result.route.method,
                    "route_score": result.route.score,
                    "answer": result.turn.text,
                    "provider_model": result.turn.provider_model,
                    "used_fallback": result.turn.used_fallback,
                    "tokens_used": result.turn.tokens_used,
                })
            return
        if path == "/v1/agents":
            slug = (data.get("slug") or "").strip().lower()
            name = (data.get("name") or "").strip()
            if not slug or not _SLUG_RE.match(slug):
                self._send(400, {"error": "поле 'slug' обязательно: "
                                          "строчные латинские буквы/цифры/-/_"})
                return
            if not name:
                self._send(400, {"error": "поле 'name' обязательно"})
                return
            with self._store() as st:
                embedder = _make_embedder(self.cfg)
                description = data.get("description", "")
                emb = embedder.embed_one(description) if description else None
                aid = st.create_agent(
                    slug, name, description=description,
                    keywords=data.get("keywords", ""),
                    avatar=data.get("avatar", ""),
                    voice_provider=data.get("voice_provider", ""),
                    voice_id=data.get("voice_id", ""),
                    llm_ref=data.get("llm_ref", ""),
                    system_prompt=data.get("system_prompt", ""),
                    tools=data.get("tools", ""),
                    description_embedding=emb)
                self._send(200, {"id": aid, "slug": slug})
            return
        if path.startswith("/v1/agents/") and path.endswith("/delete"):
            slug = path[len("/v1/agents/"):-len("/delete")]
            with self._store() as st:
                ok = st.delete_agent(slug)
            self._send(200 if ok else 404, {"deleted": ok})
            return
        if path.startswith("/v1/agents/"):
            slug = path[len("/v1/agents/"):]
            with self._store() as st:
                fields = {k: v for k, v in data.items()
                          if k in _AGENT_WRITABLE_FIELDS}
                if "description" in fields:
                    embedder = _make_embedder(self.cfg)
                    fields["description_embedding"] = embedder.embed_one(
                        fields["description"]) if fields["description"] else None
                ok = st.update_agent(slug, **fields)
            self._send(200 if ok else 404, {"updated": ok})
            return
        if path == "/v1/chain/start":
            goal = (data.get("goal") or "").strip()
            agents = list(data.get("agents") or [])
            if not agents:
                self._send(400, {"error": "поле 'agents' должно быть непустым списком"})
                return
            with self._store() as st:
                embedder = _make_embedder(self.cfg)
                runner = ChainRunner(self.cfg, st, embedder)
                result = runner.run(goal, agents,
                                    conversation_id=data.get("conversation_id"))
                self._send(200, result)
            return
        if path == "/v1/maintenance/run":
            with self._store() as st:
                embedder = _make_embedder(self.cfg)
                svc = MaintenanceService(self.cfg, st, embedder)
                report = svc.run_once()
                self._send(200, {"distilled": report.distilled,
                                 "deduped": report.deduped,
                                 "merged_entities": report.merged_entities,
                                 "errors": report.errors})
            return
        if path == "/v1/onboarding/seed":
            with self._store() as st:
                embedder = _make_embedder(self.cfg)
                created = seed_demo_agents(st, self.cfg, embedder)
                self._send(200, {"created": created,
                                 "status": demo_agents_status(st)})
            return
        if path == "/v1/tts/speak":
            text = (data.get("text") or "").strip()
            if not text:
                self._send(400, {"error": "поле 'text' обязательно"})
                return
            voice = (data.get("voice") or "").strip()
            audio_format = (data.get("audio_format") or "").strip()
            if not voice and data.get("agent_slug"):
                with self._store() as st:
                    agent = st.get_agent(data["agent_slug"])
                if not agent:
                    self._send(404, {"error": f"агент {data['agent_slug']!r} не найден"})
                    return
                voice = agent.get("voice_id") or ""
            if not voice:
                self._send(400, {"error": "поле 'voice' обязательно (или "
                                          "укажите agent_slug с настроенным голосом)"})
                return
            tts = _make_tts(self.cfg, voice_override=voice,
                            format_override=audio_format)
            audio, mime = tts.synthesize(text)
            self._send_audio(audio, mime)
            return
        self._send(404, {"error": f"нет маршрута {path}"})

    def do_PATCH(self) -> None:  # noqa: N802
        self.do_POST()


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8090,
         token: str | None = None) -> None:
    Handler.cfg = cfg
    Handler.token = token or os.getenv("MAOS_API_TOKEN") or cfg.api_token or None
    if host not in ("127.0.0.1", "localhost") and not Handler.token:
        raise SystemExit(
            "Отказ: сервер открыт наружу без токена. Задайте MAOS_API_TOKEN "
            "или слушайте 127.0.0.1."
        )
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"MAOS: http://{host}:{port}/  "
          f"(токен: {'да' if Handler.token else 'нет, только localhost'})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлен")
    finally:
        srv.server_close()


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="maos-server", description="HTTP API MAOS")
    ap.add_argument("-c", "--config")
    ap.add_argument("--host", default=os.getenv("MAOS_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.getenv("MAOS_PORT", "8090")))
    ap.add_argument("--token", default=None)
    args = ap.parse_args(argv)
    cfg = Config.load(args.config)
    serve(cfg, args.host, args.port, args.token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
