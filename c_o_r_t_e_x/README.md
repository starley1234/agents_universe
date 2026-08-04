# C.O.R.T.E.X.

**Cognitive Orchestration & Real-time Tactical Execution eXchange** —
событийный runtime и «центральная нервная система» для распределённых агентов.

C.O.R.T.E.X. превращает статичную инструкцию в наблюдаемый поток выполнения:
агенты подписываются на события, параллельно пишут результат на Shared
Blackboard, используют инструменты через единый каталог, а circuit breaker и
HITL не дают ошибке потерять контекст.

> Сейчас это рабочий **MVP production-shaped**: локальный режим не требует
> Redis/PostgreSQL/FastAPI и запускается из stdlib. Для staging/production
> подключаются FastAPI + SSE, Redis/NATS, PostgreSQL JSONB/RLS, LiteLLM и
> Temporal/LangGraph через optional extras.

## Что уже реализовано

| Область | Реализация |
|---|---|
| Event-driven autonomy | `bus/event_bus.py`: wildcard topics, replay, backpressure, correlation/causation IDs |
| Shared Blackboard | CAS-версии, audit-события `blackboard.updated`, production PostgreSQL JSONB adapter |
| Runtime | lifecycle агентов, `ToolCatalog`, circuit breakers, `tool.call.*` traces |
| Dynamic hot-swap | `POST /api/tools/hot-swap` и `ToolCatalog.hot_swap()` с событием `runtime.tool_hot_swapped` |
| HITL Gateway | approval requests, approve/reject endpoints и MCP router tool |
| First-party tools | `cortex.fetch` для небольших HTTP(S) research ресурсов + remote MCP client (`MCP_AGENT_TOOLKIT=http://localhost:8090/sse`) |
| Agent Toolkit | local sibling discovery, remote MCP client, router access через `cortex.search_tools/cortex.call_tool` |
| Первый workflow | практический аудит всех обнаруженных `agent_toolkit` инструментов с рекомендациями |
| API/UI | REST, self-contained operations dashboard, event stream `/api/stream` |
| MCP | JSON-RPC `initialize`, `tools/list`, `tools/call`; Streamable HTTP POST `/sse`; legacy GET `/sse` + `/messages` SSE |
| Configuration | `.env.example` совместим с предоставленным шаблоном, секреты не попадают в UI/API |

## Архитектура

```text
Telegram / API / MCP client / UI
              │
              ▼
      gateway (FastAPI + MCP SSE)
       │       │          │
       │       │          └── HITL approvals
       │       ▼
       │   WorkflowEngine ───── optional Temporal / LangGraph
       │       │
       ▼       ▼
  ToolCatalog  SharedBlackboard ─── optional PostgreSQL JSONB + RLS
       │              │
       └────── EventBus ─────────── optional Redis / NATS
              │
  planner → code_written → tester/documenter observers
              │
        agent_toolkit / MCP providers / LiteLLM
```

Структура проекта соответствует контракту:

```text
c_o_r_t_e_x/
├── bus/          # InMemory, Redis/NATS probes, SharedBlackboard
├── workflows/    # event-backed engine и toolkit_audit workflow
├── observers/    # health/metrics и context integrity observers
├── gateway/      # REST, Web UI, MCP JSON-RPC + SSE, agent_toolkit adapters
├── runtime/      # lifecycle, catalog, circuit breaker, LiteLLM/PG adapters
└── signals/      # Pydantic dataclasses (stdlib-compatible fallback)
```

## Быстрый старт

Из корня монорепозитория:

```bash
cd c_o_r_t_e_x
make check
make test
make audit
make serve
```

Открыть:

- <http://localhost:8117/ui> — Operations Dashboard;
- <http://localhost:8117/docs> — Swagger после `pip install -e '.[api]'`;
- <http://localhost:8117/health> — health и число инструментов;
- <http://localhost:8117/api/tools?q=антенна> — поиск каталога;
- <http://localhost:8117/api/toolkit/audit> — синхронный практический аудит;
- <http://localhost:8117/api/stream> — SSE поток событий.

Если FastAPI не установлен, `make serve` использует встроенный ThreadingHTTPServer:
REST, UI и Streamable HTTP MCP (`POST /sse`) всё равно доступны. Для полноценного
legacy SSE транспорта установите API profile.

## Подключение agent_toolkit

В development при `CORTEX_TOOLKIT_MODE=auto` C.O.R.T.E.X. сначала пытается
найти sibling-пакет `agent_toolkit` и вызывает его `build_default_registry()`.
Если локального пакета нет, используется удалённый MCP endpoint:

```env
CORTEX_TOOLKIT_MODE=remote
MCP_AGENT_TOOLKIT=http://localhost:8090/sse
```

Проверка соединения:

```bash
python -m c_o_r_t_e_x check
python -m c_o_r_t_e_x mcp-check --json
curl http://localhost:8117/api/tools/search?q=sql
```

Пример JSON-RPC Streamable HTTP:

```bash
curl -s http://localhost:8117/sse \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

MCP показывает LLM десять router-tools вместо сотен схем:

- `cortex.search_tools(query)`;
- `cortex.fetch(url)` — first-party HTTP(S) fetch;
- `cortex.call_tool(name, arguments)`;
- `cortex.submit_task(...)`;
- `cortex.get_task(task_id)`;
- `cortex.run_tool_audit()`;
- `cortex.blackboard_read(key)`;
- `cortex.list_events(pattern, limit)`;
- `cortex.hot_swap_provider(endpoint, reason)`;
- `cortex.request_approval(action, reason)`.

Так сохраняется практичный паттерн `find → call`, а полный каталог остаётся
доступным через REST/UI и управляется policy/circuit breaker.

## Первый workflow: practical agent_toolkit audit

```bash
python -m c_o_r_t_e_x audit
python -m c_o_r_t_e_x audit --json
```

Аудит не считает `tools/list` доказательством работоспособности. Для локального
`agent_toolkit` он использует его `ProductionTester` и создаёт fixtures только в
`WORKSPACE_PATH`. Каждый инструмент получает один явный статус:

- `passed` — вызов реально выполнен;
- `requires_configuration` — логика дошла до провайдера, но не хватает токена,
  бинарника, БД или реквизитов;
- `failed` — непредвиденная ошибка, требующая исправления;
- `skipped_policy` — сеть/опасный side effect намеренно не запускались.

В отчёте есть `tested/total`, latency, preview, hint и рекомендации. Workflow
публикует `toolkit.audit.started`, `toolkit.audit.item`,
`toolkit.audit.completed`, сохраняет snapshot на Blackboard и создаёт task
trace. Поэтому отчёт можно получить и через API:

```bash
curl -s -X POST http://localhost:8117/api/toolkit/audit \
  -H 'content-type: application/json' -d '{"background":false}' | jq
curl -s http://localhost:8117/api/toolkit/audit/latest | jq
```

По умолчанию native audit разрешает локальные fixtures (`CORTEX_AUDIT_ALLOW_SIDE_EFFECTS=true`),
но не разрешает внешний network (`CORTEX_AUDIT_ALLOW_NETWORK=false`). В production
перед включением side effects нужен отдельный sandbox и HITL policy.

## API и SSE контракт

| Метод | Endpoint | Назначение |
|---|---|---|
| GET | `/health`, `/api/health` | liveness/readiness summary |
| GET | `/ui`, `/` | operations dashboard |
| GET | `/api/tools?q=...` | каталог и поиск |
| POST | `/api/tools/{name}/execute` | вызов через ToolCatalog |
| POST | `/api/tools/hot-swap` | переключить provider на MCP endpoint |
| GET | `/api/events` | replay последних событий |
| GET | `/api/stream` | live SSE event bus |
| GET/PUT | `/api/blackboard` | состояние и CAS update |
| GET/POST | `/api/tasks` | workflow tasks |
| POST | `/api/tasks/{id}/run` | запустить pending task |
| POST | `/api/toolkit/audit` | создать и выполнить audit task |
| GET | `/api/toolkit/audit/latest` | последний отчёт |
| POST | `/sse` | MCP Streamable HTTP JSON-RPC |
| GET | `/sse` | MCP legacy SSE handshake |
| POST | `/messages?session_id=...` | сообщение MCP SSE session |

### Hot swap

```bash
curl -s -X POST http://localhost:8117/api/tools/hot-swap \
  -H 'content-type: application/json' \
  -d '{"endpoint":"http://agent-toolkit:8090/sse","reason":"circuit recovery"}'
```

Операция атомарно перестраивает маршруты каталога, публикует старый/новый
список инструментов и не скрывает ошибку подключения.

## Production profile

```bash
pip install -e '.[api,redis,database,llm,observability]'
cp .env.example .env
# DATABASE_URL, MCP_AGENT_TOOLKIT и секреты заполнить вне Git
docker compose up --build
```

- Redis transport — быстрый realtime fan-out; NATS можно выбрать через
  `CORTEX_EVENT_BUS=nats`;
- `runtime/state_store.py` содержит JSONB schema и RLS policy для namespace;
- `runtime/inference.py` унифицирует LiteLLM и OpenAI-compatible custom remote;
- `Temporal/LangGraph` оставлены как backend seam: task snapshot/event contract
  не меняется при замене in-memory engine;
- LangSmith/Arize Phoenix подключаются на уровне observer/LLM proxy, не делая
  tracing обязательным для локального запуска.

## Безопасность и ограничения MVP

1. Секреты не выдаются в `/api/config`, UI и event payloads. Не передавайте
   токены в `arguments` инструментов — используйте env/provider secret store.
2. `allow_network=false` и `dangerous` policy обязательны для generic remote audit.
3. Встроенный EventBus — in-process; для нескольких replicas нужен Redis/NATS,
   иначе SSE-клиент видит только события своей реплики.
4. PostgreSQL RLS требует настроить `app.tenant_id` на соединении и проверить
   policy на staging; SQL в `runtime/state_store.py` — стартовая миграция, не
   замена DBA review.
5. Для production следует включить auth (JWT/mTLS/reverse proxy), rate limits,
   durable task store и real Temporal worker.

## Тесты

```bash
make test   # bus, CAS blackboard, circuit breaker, catalog, audit, MCP, fallback API
make check  # импорт и runtime self-check
```

License: MIT
