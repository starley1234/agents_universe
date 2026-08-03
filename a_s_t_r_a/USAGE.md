# Usage Guide — A.S.T.R.A. Web UI

## Запуск вебморды (исправленной)

### Быстрый способ (SQLite + mock, без зависимостей)

```bash
cd a_s_t_r_a
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
PYTHONPATH=src uvicorn astra.main:app --port 8101 --reload
# или
make run
```

Откройте:
- http://localhost:8101 — Dashboard
- http://localhost:8101/ui/projects — Projects
- http://localhost:8101/docs — Swagger
- http://localhost:8101/health — health

### Что проверять

1. **Dashboard** должен показывать:
   - Проекты=0, Сессии=0 (изначально)
   - LLM провайдер=mock, модель=unsloth/gemma-4-12b-it
   - DB=ok, Workspace exists

2. **Создайте проект**:
   - Кнопка "Новый проект" → введите название → Создать
   - Должен появиться в списке с ID

3. **Playground**:
   - Перейдите в проект → "Запустить агента" или "Playground"
   - Введите задачу: "Опиши 3 преимущества ASTRA"
   - Нажмите Запустить (Ctrl+Enter)
   - Через 1-2 сек должен появиться результат mock-агента с 4 шагами
   - Проверьте History внизу и Sessions в проекте

4. **Memory**:
   - /ui/projects/{id}/memory должен показать сохранённый чанк с результатом сессии

5. **Graph**:
   - Пока пустой (заполняется через dreaming). Можно запустить dreaming:
   ```bash
   PYTHONPATH=src python -m astra.tasks.dreaming_task
   ```
   Но для mock он создаст фейковые сущности ASTRA→Agent→Memory

6. **Settings**:
   - Показывает конфиг, health, пример .env, ссылки на /docs и /health

### Тест на реальной LLM (если есть LM Studio)

```bash
# Запустите LM Studio с моделью и embeddings (например Qwen3-Embedding)

# В .env или env vars:
export LLM_DEFAULT_PROVIDER=local
export LOCAL_LLM_URL=http://localhost:1234/v1
export LOCAL_LLM_MODEL=unsloth/gemma-4-12b-it
export EMBEDDING_URL=http://localhost:1234/v1
export EMBEDDING_MODEL=text-embedding-qwen3-embedding-0.6b
export DATABASE_URL=sqlite+aiosqlite:///./workspace/astra.db

PYTHONPATH=src uvicorn astra.main:app --port 8101
```

Теперь агент будет делать реальные LLM вызовы вместо mock.

### Docker

```bash
# Prod с Postgres (требует .env с реальным LLM ключом)
docker compose up -d --build
curl http://localhost:8101/health

# Dev без инфры
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build

# Логи
docker logs -f astra_app
```

### Частые проблемы (исправлены в v0.2.0)

- **CREATE EXTENSION vector fails on SQLite** — fixed: conditional execution only for postgres
- **TemplateResponse argument order** — fixed: uses new Starlette API
- **Agent infinite loop** — fixed: graph no longer re-plans each cycle, preserves completed_steps
- **database is locked** — fixed: commit before memory store, retry logic, fallback
- **Planner returns non-JSON** — fixed: mock returns valid JSON, real path has code-fence stripping

### API примеры

```bash
# Создать проект
curl -X POST http://localhost:8101/api/projects/ -H "Content-Type: application/json" -d '{"name":"My Project","description":"demo"}'

# Запустить агента
curl -X POST http://localhost:8101/api/agents/run -H "Content-Type: application/json" -d '{"project_id":"<uuid>","goal":"Сделай анализ"}'

# Статы
curl http://localhost:8101/api/stats

# Память
curl http://localhost:8101/api/projects/<uuid>/memory

# Граф
curl http://localhost:8101/api/projects/<uuid>/graph
```
