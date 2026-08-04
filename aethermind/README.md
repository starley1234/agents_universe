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

Текущая реализация сохраняет MCP серверы в `tool_config` и передает их агенту в prompt как доступные внешние инструменты. Полноценный MCP client runtime остается следующим production-hardening шагом.

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
