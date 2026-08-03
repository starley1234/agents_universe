# LangGraph Autonomous Agent

Автономный AI-агент на базе **LangGraph** с онтологической памятью,
комбинированным RAG и веб-интерфейсом.

## Быстрый старт

```bash
# Docker (рекомендуется)
cp .env.example .env          # заполнить POSTGRES_PASSWORD, LLM-ключи
docker compose up -d          # http://127.0.0.1:8112

# Локально
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-app.txt
# нужен PostgreSQL с pgvector: docker run -d --name pgvector -p 5432:5432
#   -e POSTGRES_USER=agent -e POSTGRES_PASSWORD=<pass> -e POSTGRES_DB=agent_universe
#   pgvector/pgvector:pg16
cp .env.example .env
uvicorn src.main:app --reload --port 8112
```

## Архитектура

```
┌─────────────────────────────────────────────────────────────┐
│                    FastAPI (REST + WS + Web UI)              │
├─────────────────────────────────────────────────────────────┤
│  LangGraph Agent Graph                                      │
│  plan → retrieve_memory → execute → reflect → (loop|finalize)│
├─────────────────────────────────────────────────────────────┤
│  Combined RAG: semantic (pgvector) + ontology (graph) + kw  │
├─────────────────────────────────────────────────────────────┤
│  PostgreSQL + pgvector │ MCP Servers │ SMTP │ Telegram       │
└─────────────────────────────────────────────────────────────┘
```

| Компонент | Что делает |
|---|---|
| **plan** | LLM создаёт 3-15 шагов; ре-планирует после reflection |
| **retrieve_memory** | Semantic + ontological + keyword поиск |
| **execute** | Выполняет шаг через tools (file ops, code, web, MCP) |
| **reflect** | Оценивает качество (0-1), решает: continue / complete |
| **finalize** | Компилирует результат, сохраняет в память, шлёт отчёт |

## Конфигурация (.env)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `LLM_PROVIDER` | `local` | `local` (Ollama) / `openrouter` |
| `LOCAL_LLM_MODEL` | `unsloth/gemma-4-12b-it` | Модель |
| `AGENT_MAX_ITERATIONS` | `20` | Лимит итераций |
| `AGENT_QUALITY_THRESHOLD` | `0.8` | Порог остановки |
| `AGENT_MAX_HOURS` | `2.0` | Бюджет времени |
| `POSTGRES_PASSWORD` | — | **Обязательно** |
| `APP_API_TOKEN` | — | Обязателен если host ≠ 127.0.0.1 |

## API

| Метод | Путь | Описание |
|---|---|---|
| `POST` | `/api/tasks` | Создать задачу |
| `GET` | `/api/tasks` | Список задач |
| `GET` | `/api/tasks/{id}` | Детали |
| `DELETE` | `/api/tasks/{id}` | Отменить |
| `GET` | `/api/dashboard/stats` | Статистика |
| `POST` | `/api/memory/search` | RAG-поиск |
| `WS` | `/api/ws/dashboard` | Dashboard real-time |
| `WS` | `/api/ws/tasks/{id}` | Task real-time |
| `GET` | `/health` | Health check |

## Безопасность

- Non-root user в контейнере
- Read-only filesystem + tmpfs
- `cap_drop: ALL` + `no-new-privileges`
- API-токен обязателен при `host ≠ 127.0.0.1`
- Порт наружу только на `127.0.0.1`
- Секреты ТОЛЬКО из переменных окружения
- Path traversal protection во всех file operations
