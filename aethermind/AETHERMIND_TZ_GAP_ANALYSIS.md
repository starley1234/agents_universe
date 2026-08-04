# AetherMind — сверка с AETHERMIND_TZ.md

Дата проверки: 2026-08-04

## Реализовано в текущем проекте

- [x] FastAPI backend.
- [x] Celery worker + Redis broker.
- [x] PostgreSQL persistence.
- [x] Alembic migrations.
- [x] Инициализация `pgvector` extension.
- [x] Таблицы `tasks`, `task_snapshots`, `task_events`, `artifacts`, `memory_items`.
- [x] Agent loop: plan / execute / observe / reflect / summarize / persist / route.
- [x] Снапшоты после итераций.
- [x] Восстановление состояния из `current_state_json` и snapshots на уровне worker loop.
- [x] Workspace на задачу.
- [x] Scratchpad memory.
- [x] File tools: list/read/write/append.
- [x] Docker/Python code interpreter с лимитами.
- [x] LLM provider abstraction для `custom_remote`, `openrouter`, `deterministic` test mode.
- [x] Реальные LLM-вызовы для плана, исполнения, критики и summaries.
- [x] Confidence scoring.
- [x] Budget guardrails по итерациям, токенам и стоимости.
- [x] Human-in-the-loop statuses: pause/resume/intervene/awaiting_user.
- [x] Rollback endpoint.
- [x] Realtime-ish обновление UI через periodic refresh и SSE endpoint на backend.
- [x] Mission Control UI на русском.
- [x] День/ночь в интерфейсе.
- [x] Просмотр артефактов в UI.
- [x] Скачивание артефактов из UI.
- [x] Панель управления инструментами агента.
- [x] Docker Compose для локального запуска.
- [x] `.env.example` без реальных секретов.
- [x] Базовые тесты.

## Частично реализовано

- [~] **LangGraph**: реализован LangGraph-shaped класс с явными узлами, но не нативный `langgraph.StateGraph`.
- [~] **LangSmith tracing**: архитектурно предусмотрено, но SDK/трейсинг не подключены.
- [~] **pgvector memory**: таблица и extension есть, но полноценные embedding write/retrieval pipeline пока не реализованы.
- [~] **Time-travel debugging**: rollback API есть, но UI checkpoint slider и ветвление не завершены.
- [~] **Budget guardrails**: базовые лимиты есть, но нет точного provider-specific cost model.
- [~] **Live Trace**: UI показывает события, но пока через polling; SSE endpoint есть, но frontend не переключен на EventSource.
- [~] **Model routing**: provider abstraction есть, но автоматический выбор cheap/expensive model по сложности шага пока упрощен.
- [~] **Tool management**: UI/API переключатели есть; browser/MCP помечены как зарезервированные, фактическая интеграция не подключена.

## Не реализовано / требует production-hardening

- [ ] Нативный LangGraph `StateGraph` с checkpointing через graph runtime.
- [ ] LangSmith tracing для каждого LLM/tool call.
- [ ] Полноценный Headless Browser: DOM-анализ, screenshot, формы.
- [ ] MCP client runtime для SSE/Stdio серверов.
- [ ] Embedding pipeline: генерация эмбеддингов, запись в `memory_items.embedding`, semantic retrieval.
- [ ] Object storage для крупных артефактов.
- [ ] RBAC/SSO/production authentication.
- [ ] WebSocket/SSE frontend stream вместо polling.
- [ ] Checkpoint slider в UI.
- [ ] UI для rollback/intervene с вводом пользовательской инструкции.
- [ ] Branching после rollback: запуск альтернативной ветки с lineage.
- [ ] Детальный token/cost accounting по каждому провайдеру.
- [ ] Notifications: email/Telegram при низкой уверенности или `AWAITING_USER`.
- [ ] Security hardening Docker sandbox: seccomp profile, read-only FS, user namespace, строгий allowlist volumes.
- [ ] Browser/network allowlist для задач pentest/search.
- [ ] Dead-letter queue и отдельная панель failed jobs.
- [ ] CI/CD pipeline.
- [ ] Production deployment manifests: Kubernetes/Helm/Terraform.
- [ ] Observability stack: Prometheus/Grafana/OpenTelemetry.

## Следующий рекомендуемый шаг

1. Подключить нативный LangGraph + LangSmith.
2. Перевести frontend Live Trace с polling на SSE.
3. Реализовать embedding memory pipeline.
4. Добавить UI для checkpoint slider, rollback и intervention text.
5. Подключить Headless Browser и MCP runtime как настоящие инструменты.
