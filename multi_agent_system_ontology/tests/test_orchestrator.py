"""Тесты maos.agents.runtime и maos.orchestrator.service (Orchestrator):
полный цикл /v1/chat — роутинг агента, вызов LLM, запись памяти.

Реальный embedded Postgres+pgvector (pgserver) + реальный HTTP-сервер,
эмулирующий OpenAI-совместимый провайдер (без внешней сети).
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maos.agents.runtime import AgentRuntime                        # noqa: E402
from maos.config import Config                                      # noqa: E402
from maos.llm.embeddings import HashEmbedder                         # noqa: E402
from maos.orchestrator.service import Orchestrator                   # noqa: E402

PASS, FAIL = 0, 0

HAVE_DEPS = True
SKIP_REASON = ""
try:
    import psycopg  # type: ignore
except ImportError:
    HAVE_DEPS = False
    SKIP_REASON = "psycopg не установлен"

_srv = None
if HAVE_DEPS:
    try:
        import pgserver  # type: ignore
        _tmp = tempfile.mkdtemp(prefix="maos_orch_pgserver_")
        _srv = pgserver.get_server(_tmp)
    except Exception as exc:
        HAVE_DEPS = False
        SKIP_REASON = f"не удалось поднять тестовый Postgres: {exc}"


def _fresh_dsn() -> str:
    name = "t_" + uuid.uuid4().hex[:12]
    admin = psycopg.connect(_srv.get_uri(), autocommit=True)
    try:
        admin.execute(f"CREATE DATABASE {name}")
    finally:
        admin.close()
    return re.sub(r"/postgres(\?|$)", f"/{name}\\1", _srv.get_uri())


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}\n" + "─" * len(title))


class EchoingHandler(BaseHTTPRequestHandler):
    """Отвечает эхом последнего user-сообщения с номером вызова — так тест
    видит, что именно ЭТОТ ход реально дошёл до "модели"."""

    calls = 0

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        type(self).calls += 1
        last_user = ""
        for m in reversed(body.get("messages", [])):
            if m.get("role") == "user":
                last_user = m.get("content", "")
                break
        text = f"[вызов {type(self).calls}] эхо: {last_user}"
        out = json.dumps({"choices": [{"message": {"role": "assistant",
                                                    "content": text}}],
                          "usage": {"prompt_tokens": 7, "completion_tokens": 4}}
                         ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def main() -> int:
    if not HAVE_DEPS:
        print(f"test_orchestrator: тесты пропущены — {SKIP_REASON}")
        return 0

    from maos.memory.store import Store

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), EchoingHandler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{port}/v1"

    import os
    os.environ["LOCAL_BASE_URL"] = base_url

    try:
        st = Store(_fresh_dsn(), dim=64)
        emb = HashEmbedder(dim=64)
        cfg = Config(default_local_model="local::llama3",
                    complexity_char_threshold=10_000, llm_retries=0)

        section("AgentRuntime.respond: реальный вызов через HybridLLM")
        st.create_agent("coder", "Coder", description="Пишет код на Python",
                        keywords="код python",
                        system_prompt="Ты программист.",
                        llm_ref="local::llama3")
        agent_row = st.get_agent("coder")
        runtime = AgentRuntime(cfg)
        turn = runtime.respond(agent_row, "Привет, кодер!", [], store=st,
                               embedder=emb)
        check("ответ реально пришёл с фейкового сервера",
             "эхо: Привет, кодер!" in turn.text)
        check("provider_model верный", turn.provider_model == "local::llama3")
        check("tokens_used учтён", turn.tokens_used == 11)

        section("Orchestrator.chat: полный цикл с явным агентом")
        orch = Orchestrator(cfg, st, emb, runtime=runtime)
        result = orch.chat("Помоги с кодом", agent_slug="coder")
        check("agent_slug соответствует запрошенному", result.agent_slug == "coder")
        check("route.method == explicit", result.route.method == "explicit")
        check("conversation_id создан", result.conversation_id > 0)
        check("ответ записан в message",
             any("Помоги с кодом" in m["content"]
                for m in st.messages(result.conversation_id)))
        check("создан квант памяти",
             any(q["question"] == "Помоги с кодом" for q in st.all_quanta()))

        section("Orchestrator.chat: продолжение диалога — история подхватывается")
        result2 = orch.chat("А теперь напиши тесты",
                            conversation_id=result.conversation_id,
                            agent_slug="coder")
        check("тот же conversation_id", result2.conversation_id == result.conversation_id)
        msgs = st.messages(result.conversation_id)
        check("в истории теперь 4 сообщения (2 хода)", len(msgs) == 4)

        section("Orchestrator.chat: неизвестный агент -> ValueError")
        try:
            orch.chat("тест", agent_slug="ghost")
            check("неизвестный agent_slug кидает ValueError", False)
        except ValueError:
            check("неизвестный agent_slug кидает ValueError", True)

        section("Orchestrator.chat: автоматический роутинг (без agent_slug)")
        st.create_agent("writer", "Writer", description="Пишет маркетинговые тексты",
                        keywords="текст маркетинг реклама", llm_ref="local::llama3")
        result3 = orch.chat("Напиши рекламный текст про наш продукт")
        check("роутер выбрал writer", result3.agent_slug == "writer")
        check("route.method == semantic или keyword",
             result3.route.method in ("semantic", "keyword"))

        section("Orchestrator.chat: confidence при fallback ниже, чем без него")
        # Форсируем облачную модель для agent без llm_ref, но с "выключенным"
        # облаком (несуществующий base_url) -> должен сработать fallback.
        os.environ["OPENROUTER_BASE_URL"] = "http://127.0.0.1:1/v1"  # заведомо не слушает
        st.create_agent("analyst", "Analyst", description="Анализирует данные",
                        keywords="данные аналитика")  # без llm_ref -> облако для длинных
        cfg_cloud = Config(default_local_model="local::llama3",
                          default_cloud_model="openrouter::gpt-4o",
                          complexity_char_threshold=5, fallback_to_local=True,
                          llm_retries=0)
        runtime2 = AgentRuntime(cfg_cloud)
        orch2 = Orchestrator(cfg_cloud, st, emb, runtime=runtime2)
        result4 = orch2.chat("Проанализируй данные о продажах за квартал",
                            agent_slug="analyst")
        check("fallback использован (облако недоступно)",
             result4.turn.used_fallback is True)
        msgs4 = st.messages(result4.conversation_id)
        agent_msg = [m for m in msgs4 if m["role"] == "agent"][-1]
        check("confidence_score понижен при fallback",
             agent_msg["confidence_score"] < 1.0)
        os.environ.pop("OPENROUTER_BASE_URL", None)

        st.close()
    finally:
        os.environ.pop("LOCAL_BASE_URL", None)
        httpd.shutdown()
        httpd.server_close()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
