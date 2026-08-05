# AetherMind

**AetherMind** — платформа для долгоживущих автономных ИИ-агентов: FastAPI control plane, Celery workers, PostgreSQL/pgvector, Redis, Docker sandbox и панель управления Next.js.

## Что реализовано

- Backend на FastAPI с API жизненного цикла задач.
- Celery worker, выполняющий одну сохраненную агентную итерацию за раз.
- Производственный агентный цикл: `plan -> execute -> observe -> reflect -> summarize -> persist`.
- Реальные LLM-вызовы через OpenAI-compatible endpoints: `custom_remote` или `openrouter`.
- Без молчаливого deterministic fallback в production: если LLM недоступна, задача переходит в режим ожидания человека.
- PostgreSQL-модели для задач, снапшотов, событий, артефактов и памяти.
- `pgvector` extension в миграции.
- Workspace для каждой задачи.
- File tools и Docker-backed Python code interpreter.
- Budget guardrails, confidence scoring, pause/resume/intervene/rollback.
- Realtime/API proxy для UI.
- Next.js + Tailwind интерфейс «Центр управления» на русском языке.
- Переключатель темы день/ночь.
- Просмотр и скачивание артефактов из UI.
- Панель управления инструментами агента: LLM, файловая система, Code Interpreter, browser/MCP placeholders, dangerous actions.
- Human-in-the-loop панель для `AWAITING_USER`: вмешательство, продолжение, rollback.
- Просмотр настроек агента и JSON состояния задачи прямо в UI.
- Подключение внешних MCP серверов к задаче через UI/API.
- Live Trace обновляется надежным polling-циклом; SSE endpoint сохранен на backend для дальнейшего production-streaming.
- Укрепленный responsive UI: ограниченная максимальная ширина, отсутствие горизонтального разъезда, перенос длинных JSON/URL/plan titles.
- Docker Compose для локального запуска.
- Тесты guardrails и агентного цикла.

## Быстрый старт

```bash
cd aethermind
cp .env.example .env
```

Перед запуском настройте LLM в `.env`:

```env
LLM_ACTIVE_PROVIDER=custom_remote
CUSTOM_REMOTE_URL=https://your-openai-compatible-endpoint/api/v1
CUSTOM_REMOTE_KEY=your-key
CUSTOM_REMOTE_DEFAULT_MODEL=your-model
```

или OpenRouter:

```env
LLM_ACTIVE_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_DEFAULT_MODEL=google/gemini-2.0-flash-lite:preview
```

Запуск:

```bash
docker compose up --build
```

Открыть:

- UI: http://localhost:8127
- API docs: http://localhost:8128/docs
- API health: http://localhost:8128/api/health

Frontend использует относительные `/api/*` запросы. В Docker они проксируются через Next.js runtime route в `BACKEND_INTERNAL_URL=http://api:8128`.

## Важное отличие production-режима

Агент больше не «симулирует» работу. На каждом смысловом шаге он вызывает LLM:

1. LLM строит дерево стратегии.
2. LLM выполняет исследовательский шаг и пишет Markdown-артефакт.
3. LLM-критик оценивает результат и confidence.
4. Каждые N итераций LLM делает executive summary.
5. Если LLM недоступна или endpoint настроен неверно, задача останавливается в `AWAITING_USER`, а в trace появляется ошибка.

Для тестов можно явно поставить:

```env
LLM_ACTIVE_PROVIDER=deterministic
```

Но это не production-режим.

## MCP серверы

Внешний инструмент MCP подключается к конкретной задаче через панель **Инструменты агента → Добавить внешний MCP сервер**.

Поля:

- `name` — короткое имя, например `search`;
- `url` — SSE endpoint, например `http://your-mcp-server:8001/sse`;
- `transport` — сейчас используется `sse`.

API:

```bash
curl -X POST http://localhost:8128/api/tasks/{task_id}/mcp \
  -H 'Content-Type: application/json' \
  -d '{"name":"search","url":"http://your-mcp-server:8001/sse","transport":"sse","enabled":true}'
```

Текущая реализация делает реальный MCP discovery/call для SSE-серверов через Python SDK `mcp`:

- `GET /api/tasks/{task_id}/mcp/tools` подключается к каждому enabled SSE серверу, вызывает `initialize` и `list_tools`;
- `POST /api/tasks/{task_id}/mcp/call` вызывает выбранный tool через `call_tool`;
- результат сохраняется как artifact `mcp_result` и попадает в Live Trace;
- встроенные внутренние tools доступны без внешнего сервера:
  - `__internal__.fetch_url` скачивает HTTP/HTTPS страницу;
  - `__internal__.run_python` выполняет Python-код в workspace задачи через sandbox/Code Interpreter.

Пример вызова встроенного fetch:

```bash
curl -X POST http://localhost:8128/api/tasks/{task_id}/mcp/call \
  -H 'Content-Type: application/json' \
  -d '{"server_name":"__internal__","tool_name":"fetch_url","arguments":{"url":"https://example.com","max_chars":12000}}'
```

Пример вызова встроенного Python sandbox:

```bash
curl -X POST http://localhost:8128/api/tasks/{task_id}/mcp/call \
  -H 'Content-Type: application/json' \
  -d '{"server_name":"__internal__","tool_name":"run_python","arguments":{"code":"print(2 + 2)"}}'
```

MCP серверы, добавленные через UI/API, сохраняются в глобальный workspace registry `workspace/mcp_servers.json` и автоматически подключаются к новым задачам. При запуске агента registry также сливается с `tool_config` задачи, поэтому ранее добавленные MCP известны агенту без повторного подключения.

Важно про `localhost`: если AetherMind запущен в Docker, то `http://localhost:8090/...` внутри backend-контейнера означает сам контейнер, а не вашу хост-машину. Поэтому Docker Compose добавляет `host.docker.internal`, а MCP runtime автоматически пробует localhost URL как `http://host.docker.internal:8090/...`. Если MCP server запущен в другом контейнере, лучше указывать имя docker-compose сервиса вместо localhost.

Для endpoint `http://localhost:8090/sse/group/files` runtime пробует несколько вариантов: исходный SSE URL, `host.docker.internal`, а также streamable HTTP кандидаты вида `/mcp/group/files`. Это нужно для совместимости с серверами, которые LM Studio обнаруживает автоматически.

В UI у каждого discovered tool есть кнопка **«Подставить JSON»** — она строит шаблон аргументов из `input_schema`. Кнопка **«Выполнить с JSON выше»** вызывает именно выбранный tool с текущим JSON. Если обязательные поля отсутствуют, UI не отправляет некорректный вызов, а подставляет шаблон и показывает, какие поля нужно заполнить.

UI дополнительно поддерживает:

- последние события **Живого trace** и последние **Артефакты** сверху;
- синхронизацию workspace-файлов в артефакты: код, JSON/CSV, результаты MCP, вложения, scratchpad;
- просмотр выбранного артефакта с явным названием открытого файла;
- переключение просмотра артефакта `Markdown` / `HTML`, включая корректный рендер Markdown-таблиц;
- прикрепление изображений к контексту задачи до запуска и после запуска через кнопки `📎`;
- удаление MCP-сервера кнопкой `🗑 удалить`;
- обновление списка инструментов кнопкой `🔄 tools`;
- discovery MCP через SSE и fallback на Streamable HTTP endpoint-кандидаты (`/sse`, base URL, `/mcp`);
- удаление задач из списка задач;
- наглядную Human-in-the-loop панель: причина критика, confidence, observation, последние проблемные события и умные быстрые варианты ответа; вариант «принять риск» теперь действительно помечает текущий шаг выполненным и не дает критику повторно блокировать тот же шаг;
- автономный advisory-mode критика: обычные продуктивные шаги с файлами/артефактами не возвращаются человеку, замечания критика сохраняются как improvement notes, а агент продолжает работу;
- внутренние filesystem MCP tools: `__internal__.write_file`, `__internal__.read_file`, `__internal__.list_dir` для создания реальных файлов, кода и данных;
- устойчивый parser `MCP_CALL_JSON`: поддерживает однострочный JSON, многострочный JSON и массив вызовов;
- schema-aware MCP argument normalization: если внешний tool требует `code`/`source`/`content`, а агент дал `path`, runtime подставит содержимое файла; также заполняются defaults/enum values; для OpenSCAD generic `render` автоматически резолвится в `render_2d_png`/`generate_and_analyze`, если такие tools доступны; validation-error текст от MCP помечается как `is_error=true`;
- внутренний `__internal__.fetch_many_urls` для fetch pipeline с источниками/цитированием `[1]`, `[2]`;
- VLM support: прикрепленные изображения отправляются в OpenAI-compatible LLM message как `image_url` data URL, если `VISION_ENABLED=true`;
- CAD/OpenSCAD workflow hints: для задач по изображению агенту явно предлагается извлечь геометрию, создать `.scad`, вызвать render MCP и итеративно сравнивать рендер;
- optional native LangGraph facade в `app/agent/langgraph_runtime.py` поверх тех же node-функций, при этом durable checkpoints остаются в PostgreSQL snapshots;
- LLM healthcheck в UI/API: `GET /api/llm/test`;
- MCP diagnostics в UI/API: `GET /api/tasks/{task_id}/mcp/diagnostics`, чтобы видеть все transport/url attempts;
- workspace audit на Python-шаге: проверяет созданные файлы и CSV, сохраняет `artifacts/workspace_audit.json`;
- редактирование цели, budget JSON и state JSON в блоке **Настройки агента и состояние**;
- сохранение выбранной темы день/ночь в `localStorage` и cookie `aethermind.theme`.

Агент также получает список discovered MCP tools в prompt и может запросить вызов инструмента через строку `MCP_CALL_JSON: {...}`; runtime выполнит такой вызов и добавит результат в артефакт.

## Настройки агента

В UI есть блок **Настройки агента**. API без секретов:

```bash
curl http://localhost:8128/api/settings
curl http://localhost:8128/api/tasks/{task_id}/tools
```

## Диагностика

Если UI показывает проблему backend:

```bash
cd aethermind
docker compose ps
docker compose logs --tail=200 api frontend worker
curl http://localhost:8128/api/health
curl http://localhost:8127/api/health
```

Проверка LLM изнутри контейнера API:

```bash
docker compose exec api python - <<'PY'
from app.llm.providers import get_llm_provider
r = get_llm_provider().complete_sync([
    {"role": "user", "content": "Ответь одним предложением на русском: LLM доступна."}
])
print(r.model)
print(r.content)
PY
```

## Функциональное тестирование

Добавлен end-to-end pytest-сценарий:

```bash
PYTHONPATH=backend pytest backend/tests/test_functional_autonomy.py -q
```

Сценарий ставит агенту задачу: подготовить mini market intelligence отчет по 3 конкурентам с CSV, Markdown-таблицей, sandbox-проверкой и финальной рекомендацией. Ожидаемый результат:

- задача завершается без `AWAITING_USER`;
- создаются `data/competitors.csv`, `artifacts/comparison.md`, `artifacts/final_market_report.md`;
- агент использует внутренние MCP tools;
- финальный отчет содержит проверяемый вывод.

## Основной поток

1. Пользователь создает задачу через UI/API.
2. Backend сохраняет задачу и ставит `run_agent_iteration` в Celery.
3. Worker загружает последнее состояние и выполняет одну итерацию.
4. Worker сохраняет events, snapshots, artifacts и обновляет задачу.
5. Если задача еще в работе, worker ставит следующую итерацию в очередь.
6. UI отображает дерево стратегии, trace, артефакты и guardrails.

## Безопасность

- Реальные секреты не хранятся в репозитории.
- Используйте `.env` или secret manager.
- Опасные действия должны проходить через human approval.
- Sandbox запускает Python-код с лимитами CPU/RAM/time и без сети.
