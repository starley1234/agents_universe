# ROADMAP — Как развивать A.S.T.R.A.

## Текущее состояние v0.3.0
- Postgres + pgvector (prod), SQLite fallback для тестов
- Plan-Act-Reflect с circuit breaker (исправлен бесконечный цикл)
- SSE streaming (`/run/stream`), TaskIQ async (`/run/async`), JWT auth, FalkorDB опционально, Prompt Registry (YAML), Langfuse tracing, Eval harness (5 golden задач, 80% на mock)
- Web UI: Dashboard, Projects, Playground (3 режима), Graph (vis-network), Memory, Prompts, Eval, Settings, Login
- Docker: app + postgres + redis + falkordb (profile) + worker (profile)

## Ближайшие улучшения (1-2 недели) — критичны для прода

### 1. Устойчивость LLM вызовов
- **Проблема**: LMStudio падает, сеть рвётся, модель перегружена
- **Решение**:
  - Добавить `tenacity` retry с jitter + circuit breaker для LLM провайдера (не путать с agent circuit breaker)
  - Fallback модель: если `unsloth/gemma-4-12b-it` упала — пробовать `gemma-2-9b` или `openrouter` как запасной
  - Health check для LLM: `/api/health/llm` пингует `/v1/models` и кеширует статус
  - В UI: бейдж "LLM offline" и кнопка "Retry"

### 2. Streaming токенов, а не только шагов
- Сейчас SSE стримит события шагов (plan, tool_call), но не токены внутри одного шага
- **Сделать**: использовать `litellm.acompletion(..., stream=True)` и прокидывать `delta` как `token` event
- В Playground: typewriter эффект, как в ChatGPT
- Добавить `POST /api/agents/run/stream/tokens` — чистый token stream

### 3. Нормальная очередь и persistence
- Сейчас TaskIQ job хранит только `session_id`, а result пишется в Postgres после выполнения
- **Улучшить**:
  - Таблица `agent_jobs` (id, session_id, status, attempts, error, created_at, started_at, finished_at)
  - Воркер обновляет статус `running` → `completed` в транзакции, retry 3 раза
  - UI: страница `/ui/jobs` — список всех jobs, логи, возможность рестарта
  - Добавить `celery beat` или TaskIQ scheduler для периодического `dreaming` (раз в час)

### 4. FalkorDB → полноценный граф
- Сейчас FalkorDB опционально пишет, но чтение всё ещё из NetworkX кеша
- **Сделать**:
  - Полный переход: читаем соседей через Cypher `MATCH (n)-[r*1..2]->(m) RETURN ...`
  - Индексы: `CREATE INDEX FOR (n:Entity) ON (n.name)`
  - Миграция: при старте грузить ontology.json в FalkorDB если пусто
  - UI: добавить фильтры по типу узла (concept, person, tool), поиск по имени, экспорт в GraphML

### 5. Auth — RBAC и проектные роли
- Сейчас один `admin/admin` и `AUTH_ENABLED=false` по умолчанию
- **Нужно**:
  - Роли: `admin`, `user`, `viewer`
  - Таблица `project_members` (project_id, user_id, role)
  - `get_current_user` → проверка что user имеет доступ к project_id
  - Refresh tokens: `/api/auth/refresh`, HttpOnly cookie + CSRF
  - OAuth: Google/GitHub login via `authlib`

## Средний срок (1 месяц) — фичи для автономности

### 6. Prompt Registry → полноценный Prompt Studio
- Сейчас YAML файлы в `src/astra/prompts/*.yaml`, hot reload нет, версионирование ручное
- **Сделать**:
  - UI `/ui/prompts/edit/{name}` — Monaco editor, live preview, test run на sample goal
  - Версионирование в БД: таблица `prompts` (name, version, content, created_by, is_active)
  - A/B тестирование: 50% запросов на v2, 50% на v1, метрика success_rate в Langfuse
  - Langfuse integration: каждый prompt linked к Langfuse prompt (fetch via API)

### 7. Evaluation → CI gate
- Сейчас 5 задач, ручной запуск `POST /api/eval/run`, отчёт в памяти
- **Сделать**:
  - GitHub Action: на каждый PR запускает `pytest tests/eval` и постит комментарий с success_rate
  - Таблица `eval_runs` (id, commit_sha, success_rate, avg_steps, created_at)
  - Grafana dashboard: success_rate over time
  - Добавить задачи: `long_context` (10k токенов RAG), `multi_step_tool` (3 tool calls подряд), `hallucination_detection`

### 8. Memory — улучшить retrieval
- Сейчас semantic search = `embedding <=> query_vec` top_k=5, no rerank
- **Улучшить**:
  - Hybrid search: `semantic_score * 0.7 + bm25_score * 0.3` (использовать `ts_rank` для BM25 в Postgres)
  - Reranker: маленькая модель `cross-encoder/ms-marco-MiniLM-L-6-v2` локально на CPU для rerank top 20 → top 5
  - Contextual compression: `LLMChainExtractor` — выжимает из 5 чанков только релевантные предложения
  - В UI Memory добавить: `Add memory` кнопка (ручное добавление факта), `Search` с слайдером top_k и threshold

### 9. MCP — больше инструментов и песочница
- Сейчас только SSE клиент, нет stdio, нет песочницы
- **Сделать**:
  - Поддержка stdio MCP серверов (запуск через `npx @modelcontextprotocol/server-filesystem`)
  - Песочница: каждый MCP tool вызов в Docker контейнере с ограничением CPU/Mem, таймаут 30с
  - Marketplace: `/ui/mcp` — список доступных серверов из `mcp-registry`, one-click install (создаёт env vars)
  - OAuth для MCP: если tool требует OAuth (GitHub, Google Drive), flow с redirect

## Долгий срок (3 месяца) — превратить в платформу

### 10. Multi-agent
- Сейчас один агент Plan-Act-Reflect
- **Сделать Supervisor pattern**:
  - Supervisor LLM решает какие под-агенты нужны: Researcher (поиск), Coder (код), Critic (проверка), Writer (отчёт)
  - Каждый под-агент — отдельный LangGraph с своими prompts и tools
  - Коммуникация через `AgentState` с `shared_memory`
  - UI: граф выполнения с узлами под-агентов, как в LangGraph Studio

### 11. Workflow OS
- Идея из `agentic_workflow_os` в этом репозитории: визуальный конструктор DAG из нод (LLM, Tool, Condition, Loop)
- Drag & drop UI на React Flow, сохранение в `workflows` таблицу (JSON), выполнение через TaskIQ

### 12. Observability
- Сейчас Loguru в файлы + Langfuse опционально
- **Сделать**:
  - OpenTelemetry: traces → Jaeger, metrics → Prometheus, logs → Loki, всё в Grafana
  - Добавить `/metrics` endpoint для Prometheus (latency, token usage, tool calls, circuit breaker trips)
  - Alerting: если success_rate < 70% за последний час → Telegram alert

### 13. Cost & Token management
- Сейчас нет подсчёта токенов
- **Сделать**:
  - Таблица `token_usage` (session_id, prompt_tokens, completion_tokens, cost_usd, model)
  - UI: `/ui/usage` — график токенов по проектам, top expensive sessions
  - Budget: лимит токенов на проект, если превышен — агент стоп

## Что бы я сделал прямо сейчас (приоритет)

1. **Фикс LMStudio** (сделано в этом PR) — `openai/` префикс + `host.docker.internal` + `extra_hosts`
2. **Dashboard иконки** — заменить SVG macro на `|safe` или простые символы (сделано)
3. **Добавить `/health/llm`** — пингует `LOCAL_LLM_URL/v1/models` и возвращает latency
4. **Streaming токенов** — следующий PR
5. **Project Members RBAC** — чтобы не все видели все проекты

## Идеи для монетизации / open-source роста

- Выложить как шаблон `cookiecutter` — `cookiecutter gh:starley1234/agents_universe --directory a_s_t_r_a`
- Лендинг с демо GIF (SSE streaming) и кнопкой Deploy to Railway / Fly.io (one-click Postgres + Redis)
- Интеграция с HuggingFace Spaces: `Dockerfile` уже совместим, добавить `app.py` для Spaces
- Marketplace промптов: пользователи шарат промпты, голосование, fork

---

**Итог:** У вас уже крепкая база — Postgres, LangGraph, SSE, JWT, TaskIQ, FalkorDB, Langfuse, Eval. Следующий шаг — сделать агент устойчивым к падению LLM (retry + fallback + health) и добавить streaming токенов для вау-эффекта в UI. После этого — RBAC и Workflow OS.

