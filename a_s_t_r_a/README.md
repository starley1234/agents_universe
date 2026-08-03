# A.S.T.R.A. — Autonomous Semantic & Topological Reasoning Agent

> Производственно-готовая агентная система с гибридной памятью (RAG + Knowledge Graph), MCP-интеграцией и циклом Plan-Act-Reflect.

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111+-green)](https://fastapi.tiangolo.com)
[![License MIT](https://img.shields.io/badge/license-MIT-lightgrey)](#license)


## 🆕 v0.3.0 — Новые фичи по запросу

- **SSE Streaming**: `POST /api/agents/run/stream` — `text/event-stream`, события `plan_generated`, `step_start`, `tool_call`, `reflect`, `done`. Playground теперь стримит шаги в реальном времени без блокировки 1-3 мин.
- **JWT Auth**: `auth/jwt.py` с `pbkdf2_sha256`, endpoints `/api/auth/register`, `/login`, `/me`, `AUTH_ENABLED=true` требует Bearer token, UI `/login` + localStorage, default `admin/admin`.
- **TaskIQ очередь**: `tasks/agent_tasks.py` — `run_agent_task` с `job_id`, брокер Redis (fallback InMemory), `docker-compose` сервис `astra-worker` (profile `worker`/`full`), endpoints `/run/async` и `/jobs/{id}` — job не теряется при падении pod.
- **FalkorDB**: `memory/falkor_store.py` — если `USE_FALKORDB=true`, граф пишется в FalkorDB (RedisGraph протокол), иначе NetworkX fallback. Docker `falkordb` service на `6380:6379`, profile `falkor`/`full`, graph API показывает backend.
- **Prompt Registry**: `prompts/*.yaml` версионированные (planner v1/v2, reflector, consolidation, agent_system), `registry.py` с hot reload, используется в planner/reflector/dreaming, UI `/ui/prompts` и API `/api/prompts`.
- **Langfuse**: `llm/tracing/langfuse.py` — опциональная трассировка всех LLM вызовов при `LANGFUSE_ENABLED=true`, логирует prompt name, usage, duration, health/full показывает статус.
- **Eval Harness**: `tests/eval/tasks.json` 5 golden задач, `metrics.py` + `harness.py` — success_rate, avg_steps, duration, API `GET /api/data/eval/tasks` и `POST /api/eval/run`, UI `/ui/eval`.


## ✨ Что было исправлено и улучшено в этой версии (v0.3.0) — Postgres + SSE + JWT + TaskIQ + FalkorDB + Langfuse + Eval

### Критические баги вебморды:
- **SQLite совместимость**: Добавлена поддержка SQLite для dev-режима (раньше падало на `CREATE EXTENSION vector`). Теперь проект стартует без Postgres: `DATABASE_URL=sqlite+aiosqlite:///./workspace/astra.db`
- **Бесконечный цикл агента**: Граф LangGraph имел связь `advance → retrieve → plan`, которая сбрасывала `completed_steps` каждый цикл. Исправлено на `retrieve → plan → (act → reflect → advance → act)*`
- **Сброс плана**: `plan` нода переопределяла `completed_steps=[]` и `current_step_index=0` при каждом вызове. Теперь сохраняет прогресс.
- **SQLite deadlock**: Два коннекта писали одновременно → `database is locked`. Разделил транзакции: `commit()` до `semantic_memory.store()` и best-effort fallback в in-memory.
- **TemplateResponse API**: Переписан на современный `TemplateResponse(request, name, context)` вместо устаревшего порядка аргументов.

### Новые фичи UI:
- **Settings page** `/ui/settings` — отображение провайдера LLM, здоровья системы, примера `.env`
- **Memory page** `/ui/projects/{id}/memory` — просмотр семантической памяти с поиском
- **Delete project** — кнопка удаления с подтверждением
- **Health full** `/api/health/full` — расширенный health с инфо о БД, MCP, workspace
- **Config API** `/api/config` — публичная конфигурация без секретов
- **Toast система** переписана на Alpine.js с нормальными анимациями
- **Dashboard** показывает LLM провайдер/модель, статус БД, воркспейса

### Архитектура:
- **Mock LLM провайдер** — `LLM_DEFAULT_PROVIDER=mock` для CI/тестов без реального LLM, детерминированные эмбеддинги
- **Embedding fallback** — если LLM недоступен, генерирует детерминированные псевдо-эмбеддинги из хеша текста
- **Dreaming** полностью реализована: тянет unconsolidated чанки, просит LLM выделить entity/relation, сохраняет в ontology JSON
- **Engine** теперь поддерживает PostgreSQL и SQLite, автоматически создаёт папку для файла БД
- **Docker**: мультистейдж с кешированием, healthcheck, `docker-compose.dev.yml` для SQLite режима без инфры

## 🚀 Быстрый старт

### Вариант A — без Docker, SQLite + mock (для проверки вебморды за 30 сек)

```bash
cd a_s_t_r_a
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# .env уже не обязателен — по умолчанию mock + sqlite
# Но можно создать из примера:
cp .env.example .env

# Запуск
ENVIRONMENT=development DATABASE_URL=sqlite+aiosqlite:///./workspace/astra.db LLM_DEFAULT_PROVIDER=mock \
PYTHONPATH=src uvicorn astra.main:app --reload --port 8101

# Откройте http://localhost:8101
# API docs: http://localhost:8101/docs
```

### Вариант B — Docker с Postgres (прод режим)

```bash
cd a_s_t_r_a
cp .env.example .env
# Отредактируйте .env: поставьте ключи OpenRouter или local LLM URL

# Прод с Postgres + Redis
docker compose up -d --build

# Или dev без инфры (SQLite + mock)
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

### Вариант C — локально с реальным LLM (LM Studio / Ollama)

```bash
# Запустите LM Studio на http://localhost:1234
# В .env:
LLM_DEFAULT_PROVIDER=local
LOCAL_LLM_URL=http://localhost:1234/v1
LOCAL_LLM_MODEL=unsloth/gemma-4-12b-it
LOCAL_LLM_API_KEY=sk-local

# Эмбеддинги тоже через LM Studio
EMBEDDING_URL=http://localhost:1234/v1
EMBEDDING_MODEL=text-embedding-qwen3-embedding-0.6b
EMBEDDING_DIMENSIONS=1024

# БД: для dev можно SQLite
DATABASE_URL=sqlite+aiosqlite:///./workspace/astra.db
# Для прод:
# DATABASE_URL=postgresql+asyncpg://astra:astra@localhost:5432/astra

PYTHONPATH=src uvicorn astra.main:app --port 8101
```

## 🧪 Тесты на реальной (mock) LLM

```bash
# Все тесты проходят без реального LLM благодаря mock провайдеру
ENVIRONMENT=development DATABASE_URL=sqlite+aiosqlite:///./test.db LLM_DEFAULT_PROVIDER=mock \
PYTHONPATH=src pytest -v

# Тест полного цикла агента:
# tests/test_web.py::test_create_and_list_projects создает проект и запускает агента

# Ручной тест агента:
python - << 'PY'
import os
os.environ["DATABASE_URL"]="sqlite+aiosqlite:///./workspace/astra.db"
os.environ["LLM_DEFAULT_PROVIDER"]="mock"
from fastapi.testclient import TestClient
from astra.main import app
with TestClient(app) as c:
    p = c.post("/api/projects/", json={"name":"Demo","description":"test"}).json()
    print("Project:", p["id"])
    r = c.post("/api/agents/run", json={"project_id": p["id"], "goal": "Опиши архитектуру ASTRA"})
    print(r.json()["result"][:500])
PY
```

## 📡 API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe |
| GET | `/health/ready` | Readiness (DB check) |
| GET | `/api/health/full` | Full health (LLM, MCP, workspace) |
| GET | `/api/config` | Public config without secrets |
| POST | `/api/projects/` | Create project |
| GET | `/api/projects/` | List projects |
| GET | `/api/projects/{id}` | Get project |
| PATCH | `/api/projects/{id}` | Update project |
| DELETE | `/api/projects/{id}` | Delete project |
| GET | `/api/data/projects` | Projects with session count (for UI) |
| GET | `/api/projects/{id}/sessions` | List sessions |
| GET | `/api/projects/{id}/memory` | Semantic memory chunks |
| GET | `/api/projects/{id}/graph` | Ontology graph JSON |
| POST | `/api/agents/run` | Run agent (goal → result) |
| GET | `/api/agents/sessions/{id}` | Get session |
| GET | `/api/mcp/servers` | MCP servers status |
| GET | `/docs` | Swagger UI (dev only) |

## 🖥️ Web UI Routes

| Path | Description |
|------|-------------|
| `/` | Dashboard — stats, LLM status, recent sessions |
| `/ui/projects` | Projects list + create/delete |
| `/ui/projects/{id}` | Project detail — overview + sessions |
| `/ui/projects/{id}/playground` | Playground — run agent with goal |
| `/ui/projects/{id}/graph` | Knowledge Graph vis-network |
| `/ui/projects/{id}/memory` | Semantic memory viewer |
| `/ui/settings` | System settings & health |

## 🏗️ Архитектура (исправленная)

```
┌─────────────┐
│  Retrieve    │  ← Semantic + Ontology context (once)
└──────┬──────┘
       │
┌──────▼──────┐
│   Plan      │  ← Hierarchical plan via LLM (once, preserves progress)
└──────┬──────┘
       │
       └──► ┌─────────┐
            │   Act    │  ← LLM + MCP tools, tool-calling loop (up to 5 rounds)
            └────┬────┘
                 │
            ┌────▼────┐
            │ Reflect  │  ← Progress check, entropy scoring
            └────┬────┘
                 │
       ┌─────────▼─────────┐
       │ Circuit Breaker   │  ← repetition >=3 or entropy <0.15 → halt
       └─────────┬─────────┘
                 │
       ┌─────────▼──────────┐
       │ Router:            │
       │ continue → advance → act (loop)
       │ done/halt → END
       └────────────────────┘
```

## 💾 Память

- **Semantic**: pgvector (prod) или SQLite с Python cosine fallback (dev). `store()` реально сохраняет, `search()` делает embedding similarity + LIKE fallback.
- **Ontology**: NetworkX DiGraph в памяти + персист в `workspace/{project_id}/ontology.json`
- **Dreaming**: background task читает `consolidated=False` чанки, просит LLM выделить entities/relations, обновляет граф, помечает чанки.

## 🔌 MCP

```python
# .env
MCP_SEARCH_URL=http://localhost:8001/sse
MCP_IMAGE_GEN_URL=http://localhost:8002/sse
```
Клиент `MCPClient` использует официальный SDK `sse_client` + `ClientSession`. Registry маппит tool name → server, конвертит схемы в OpenAI format.

## 📁 Структура проекта

```
a_s_t_r_a/
├── src/astra/
│   ├── main.py              # FastAPI + lifespan tolerant
│   ├── config.py            # Settings, is_sqlite, resolved_workspace
│   ├── core/
│   │   ├── agent.py         # Fixed graph, no infinite loop
│   │   ├── planner.py
│   │   ├── reflector.py     # + heuristic fallback
│   │   └── circuit_breaker.py
│   ├── memory/
│   │   ├── semantic.py      # Real store/search + fallback
│   │   ├── ontology.py      # NetworkX + JSON persist
│   │   ├── dreaming.py      # Full consolidation
│   │   └── project_memory.py
│   ├── llm/
│   │   ├── gateway.py       # Mock + retry + backoff
│   │   └── embeddings.py    # Mock deterministic embeddings
│   ├── db/
│   │   ├── engine.py        # PG + SQLite, auto mkdir
│   │   ├── models.py        # Conditional Vector column
│   │   └── repositories.py  # PG vector search + Python fallback
│   ├── api/routes/
│   ├── web/routes.py        # Fixed TemplateResponse, new endpoints
│   └── templates/           # Tailwind + Alpine, settings/memory pages
├── tests/                   # 29 tests, все проходят на mock
├── docker-compose.yml       # Prod: postgres+redis+app
├── docker-compose.dev.yml   # Dev: sqlite+mock, no infra needed
├── Dockerfile               # Multi-stage, healthcheck
└── pyproject.toml
```

## 🐳 Production checklist

- [x] Multi-stage Dockerfile, non-root user
- [x] Healthcheck `/health`
- [x] ENV-driven config, secrets not in code
- [x] DB: Postgres pgvector prod, SQLite dev
- [x] Mock mode для CI
- [x] Logging Loguru с ротацией
- [x] CORS, global exception handler
- [x] Graceful shutdown MCP
- [x] Alembic migrations (PG + SQLite)
- [ ] Добавить Prometheus metrics (TODO)
- [ ] Добавить rate limiting (TODO)
- [ ] Добавить auth (JWT) — сейчас открыт для dev

## 🧠 Соображения по проекту

### Сильные стороны
1. **Чёткая продуктовая идея**: гибрид RAG + KG + Plan-Act-Reflect + MCP — это современный стек для автономных агентов, похоже на то как делает LangGraph + Mem0.
2. **Изоляция проектов**: каждый проект свой namespace для памяти — правильно для прода.
3. **Circuit Breaker**: редкая но важная фича, предотвращает зацикливание на 16Gb VRAM.
4. **MCP** — стандарт от Anthropic, позволяет быстро цеплять новые тулы.

### Недостатки и что исправлено
1. **Вебморда падала без Postgres** — исправлено fallback на SQLite + tolerant lifespan.
2. **Агент уходил в бесконечный цикл** — исправлено топологией графа и сохранением `completed_steps`.
3. **Память была заглушкой** — реализована реальная `store/search` с pgvector и Python fallback.
4. **Нет стриминга** — сейчас `/api/agents/run` синхронный и блокирует 1-3 мин. Для прода нужен SSE/WebSocket стрим токенов и шагов. Можно добавить endpoint `/api/agents/run/stream`.
5. **Нет аутентификации** — ок для локального, но для прода нужен JWT + RBAC по проектам.
6. **Отсутствие очередей для агента** — сейчас агент работает внутри request. Если упадёт pod, сессия потеряется. Нужно вынести в TaskIQ/background worker с Redis.

### Предложения по улучшению
- **Streaming UI**: добавить `EventSource` в playground, чтобы видеть шаги план/акт в реальном времени.
- **Graph persistence**: сейчас граф в памяти, сохраняется только через dreaming. Нужно сохранять после каждого шага в PG JSONB или отдельный сервис (FalkorDB/Memgraph).
- **Evaluations**: добавить `tests/eval/` с golden задачами и метриками (success rate, steps, token usage).
- **Prompt management**: вынести системные промпты в `prompts/` папку с версионированием, использовать Langfuse для трейсинга.
- **Resource optimization**: для 16Gb VRAM добавить локальную маленькую модель-критик (Phi-3) которая будет работать как circuit breaker без вызова дорогого LLM.
- **Multi-agent**: сейчас один агент, но можно добавить Supervisor который делегирует подзадачи специализированным агентам (researcher, coder, critic).

## 📝 License

MIT — см. `LICENSE`.

## 👤 Авторы

Original idea: A.S.T.R.A. spec. Refactored to v0.3.0 prod-ready by Arena agent.
