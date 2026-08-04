# Чек-лист AetherMind

## Реализовано

- [x] FastAPI backend и API задач.
- [x] Celery worker для автономных итераций.
- [x] Redis broker.
- [x] PostgreSQL schema и Alembic migration.
- [x] Идемпотентная инициализация `pgvector` и enum-типов.
- [x] Снапшоты после итераций.
- [x] Журнал событий задачи.
- [x] Workspace на каждую задачу.
- [x] Scratchpad memory.
- [x] Артефакты в файловой системе и БД.
- [x] Производственный цикл агента: plan / execute / observe / reflect / summarize.
- [x] Реальные LLM-вызовы для планирования, исполнения, критики и summaries.
- [x] Отсутствие молчаливого fake-success fallback в production.
- [x] Budget guardrails.
- [x] Confidence scoring и routing при низкой уверенности.
- [x] Human-in-the-loop статусы и intervention endpoint.
- [x] Pause/resume endpoints.
- [x] Rollback endpoint.
- [x] Docker sandbox для Python interpreter.
- [x] OpenAI-compatible providers для custom remote и OpenRouter.
- [x] Next.js runtime API proxy.
- [x] Next.js/Tailwind UI на русском.
- [x] День/ночь в интерфейсе.
- [x] Просмотр артефактов в UI.
- [x] Скачивание артефактов.
- [x] API и UI для управления инструментами агента.
- [x] Docker Compose.
- [x] `.env.example` без реальных секретов.
- [x] Базовые тесты.

## Рекомендуемое production-hardening

- [ ] Добавить полноценный LangGraph `StateGraph` вместо совместимого класса-обертки.
- [ ] Добавить auth/RBAC.
- [ ] Добавить object storage для крупных артефактов.
- [ ] Добавить browser automation service.
- [ ] Добавить точный tokenizer/cost accounting для каждого провайдера.
- [ ] Добавить secret manager.
- [ ] Добавить CI/CD и deployment manifests.
- [ ] Добавить мониторинг Celery/Redis/Postgres.
