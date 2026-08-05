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
- [x] Human-in-the-loop UI: текст вмешательства, продолжение, сохранение без запуска, rollback.
- [x] Наглядная обратная связь критика: причина, confidence, observation, problem events, умные быстрые варианты ответа.
- [x] Advisory-mode критика: замечания не блокируют продуктивные шаги с артефактами.
- [x] Внутренние filesystem MCP tools: write_file/read_file/list_dir.
- [x] Устойчивый parser MCP_CALL_JSON: multiline JSON и массивы вызовов.
- [x] Внутренний fetch_many_urls для source/citation pipeline.
- [x] Rollback endpoint и базовый UI выбора checkpoint/iteration.
- [x] Backend SSE endpoint и надежное frontend polling-обновление Live Trace.
- [x] Mission Control UI на русском.
- [x] День/ночь в интерфейсе.
- [x] Индикатор активной работы агента/LLM и блокировка повторного запуска во время submit.
- [x] Просмотр артефактов в UI.
- [x] Скачивание артефактов из UI.
- [x] Панель управления инструментами агента.
- [x] Просмотр настроек агента и состояния задачи в UI.
- [x] Регистрация внешних MCP серверов на задачу через UI/API.
- [x] MCP discovery: подключение к SSE-серверам, initialize, list_tools.
- [x] MCP tool call: вызов внешних tools через call_tool, сохранение результата в artifact.
- [x] Встроенный внутренний MCP-like инструмент `__internal__.fetch_url`.
- [x] Встроенный внутренний MCP-like инструмент `__internal__.run_python` для выполнения Python-кода.
- [x] Глобальный registry MCP серверов: ранее подключенные MCP автоматически попадают в новые задачи.
- [x] Слияние global MCP registry с tool_config при запуске/работе агента.
- [x] UI-шаблоны аргументов MCP tools из input_schema и frontend validation required-полей.
- [x] UI-кнопки удаления MCP сервера и обновления списка tools.
- [x] UI/API удаления задач.
- [x] UI/API редактирования цели, budget JSON и state JSON задачи.
- [x] Сохранение выбранной темы день/ночь в localStorage/cookie.
- [x] Автосинхронизация workspace-файлов в artifacts.
- [x] Последние trace events и artifacts сверху.
- [x] Просмотр артефактов Markdown/HTML с указанием открытого файла.
- [x] Корректный HTML-рендер Markdown-таблиц в артефактах.
- [x] Прикрепление изображений к контексту задачи до запуска и после запуска.
- [x] MCP discovery fallback: SSE + Streamable HTTP endpoint candidates.
- [x] Агент получает discovered MCP tools в prompt и может запросить MCP вызов через `MCP_CALL_JSON`.
- [x] Валидация MCP name/url на frontend и backend.
- [x] Укрепленный responsive UI без горизонтального разъезда при длинном плане/JSON/URL.
- [x] Docker Compose для локального запуска.
- [x] `.env.example` без реальных секретов.
- [x] Базовые тесты.

## Частично реализовано

- [~] **LangGraph**: реализован LangGraph-shaped класс с явными узлами, но не нативный `langgraph.StateGraph`.
- [~] **LangSmith tracing**: архитектурно предусмотрено, но SDK/трейсинг не подключены.
- [~] **pgvector memory**: таблица и extension есть, но полноценные embedding write/retrieval pipeline пока не реализованы.
- [~] **Time-travel debugging**: rollback API и checkpoint UI есть, но нет полноценного branching/lineage.
- [~] **Budget guardrails**: базовые лимиты есть, но нет точного provider-specific cost model.
- [~] **Model routing**: provider abstraction есть, но автоматический выбор cheap/expensive model по сложности шага пока упрощен.
- [~] **Tool management**: LLM/filesystem/code-interpreter реально учитываются; MCP runtime подключен; browser пока остается placeholder.
- [~] **MCP**: SSE discovery/call реализован; Stdio transport и расширенный lifecycle/session pooling еще не реализованы.

## Не реализовано / требует production-hardening

- [ ] Нативный LangGraph `StateGraph` с checkpointing через graph runtime.
- [ ] LangSmith tracing для каждого LLM/tool call.
- [ ] Полноценный Headless Browser: DOM-анализ, screenshot, формы.
- [ ] Stdio MCP transport и persistent MCP session pooling.
- [ ] Embedding pipeline: генерация эмбеддингов, запись в `memory_items.embedding`, semantic retrieval.
- [ ] Object storage для крупных артефактов.
- [ ] RBAC/SSO/production authentication.
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
2. Реализовать полноценный MCP client runtime: discovery, tool schema, вызов tools, запись observations.
3. Реализовать embedding memory pipeline.
4. Добавить branching/lineage после rollback.
5. Подключить Headless Browser как настоящий инструмент.
