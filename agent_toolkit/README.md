# Agent Toolkit

**163 инструмента** для LLM-агентов с MCP Gateway, REST API и Web UI.

```
LLM → /sse (3 router tools) → find_tools("антенна") → 5 результатов
                            → call_tool("physics.calc_antenna", {...}) → результат
                            → /sse/group/physics (10 tools напрямую)
```

## Quick Start

```bash
# 1. Установка
cd agent_toolkit
pip install -e .[all]

# 2. Запуск
python -m agent_toolkit serve --port 8090

# 3. Открыть
#    http://localhost:8090      — Web UI + File Manager
#    http://localhost:8090/docs — Swagger API
```

### Docker

```bash
docker compose up --build
# http://localhost:8090
```

## Архитектура

```
┌─────────────────────────────────────────────────────┐
│                    LM Studio / LLM                    │
└───────────┬─────────────────────┬───────────────────┘
            │ POST /sse           │ POST /sse/group/physics
            ▼                     ▼
┌───────────────────┐   ┌─────────────────────┐
│  MCP Gateway      │   │  Group Server       │
│  ┌─────────────┐  │   │  (до 10 tools)      │
│  │ find_tools  │  │   │                     │
│  │ call_tool   │  │   │  physics, cad, web  │
│  │ list_groups │  │   │  files, data, ...   │
│  └─────────────┘  │   └─────────────────────┘
└───────────┬───────┘
            │ проксирует
            ▼
┌───────────────────────────────────────────────────┐
│              Tool Registry (163 tools)              │
│                                                     │
│  Physics (20)  │  CAD (8)  │  Web (30)  │  Files   │
│  Office (4)    │  Data     │  Crypto    │  SMTP    │
│  ERP/1C (2)    │  Teamcenter (7) │ Vision (12)     │
└───────────────────────────────────────────────────┘
```

## LM Studio (mcp.json)

```json
{
  "mcpServers": {
    "agent-toolkit": {
      "url": "http://localhost:8090/sse"
    }
  }
}
```

LLM получит 3 инструмента-роутера:
- **find_tools(query)** — поиск по 163 инструментам
- **call_tool(name, args)** — вызов любого инструмента
- **list_groups()** — список тематических групп

Для прямого доступа к группе:
```json
{
  "mcpServers": {
    "agent-toolkit-physics": {
      "url": "http://localhost:8090/sse/group/physics"
    }
  }
}
```

## Группы инструментов

| Группа | Кол-во | Примеры |
|--------|--------|---------|
| **physics** | 20 | прочность, антенны, аэродинамика, акустика, пропеллеры |
| **cad** | 8 | OpenSCAD рендер, FreeCAD, STL анализ, масса/инерция |
| **web** | 30 | DuckDuckGo, скрапинг, Playwright, формы, sitemap |
| **files** | 8 | чтение, запись, поиск, ZIP-архив |
| **data** | 13 | SQL, CSV, Excel формулы, ER-диаграммы |
| **office** | 4 | Word (.docx), Excel (.xlsx) с форматированием |
| **crypto** | 4 | UUID, SHA256, HMAC подпись |
| **code** | 8 | Git, линтер, тесты, патчи |
| **memory** | 5 | HNSW векторный поиск, RAG |
| **integrations** | 23 | SMTP, Telegram, S3, ERP/1C, Teamcenter, HTTP |
| **vision** | 12 | VLM-анализ изображений, PDF парсинг, аудит полок |

## Конфигурация (.env)

```env
# === SMTP ===
MAIL_SERVER=smtp.example.com
MAIL_PORT=465
MAIL_USERNAME=user@example.com
MAIL_PASSWORD=your-password
MAIL_FROM_ADDRESS=noreply@example.com
SMTP_USE_SSL=true          # true=SSL(465), false=STARTTLS(587)

# === LLM ===
LOCAL_LLM_URL=http://localhost:1234/v1
LOCAL_LLM_MODEL=local-model

# === Режим ===
AGENT_TOOLKIT_MOCK_MODE=false   # false = реальные вызовы
AGENT_TOOLKIT_ENV=production
```

См. `.env.example` для полного списка переменных.

## API Endpoints

| Endpoint | Описание |
|----------|----------|
| `POST /sse` | MCP Gateway (Streamable HTTP) |
| `POST /sse/group/{name}` | MCP группа инструментов |
| `GET /` | Web UI + File Manager |
| `GET /health` | Health check |
| `GET /api/tools` | Каталог 163 инструментов |
| `POST /api/tools/{name}/execute` | Вызов инструмента |
| `GET /api/workspace/list` | Список файлов workspace |
| `GET /api/workspace/download?path=...` | Скачать файл |
| `POST /api/mcp/rpc` | MCP JSON-RPC 2.0 |

## Тестирование

```bash
# Самопроверка
python -m agent_toolkit check

# Прогон всех 163 инструментов
python -m agent_toolkit test-prod

# Полный набор автотестов (342 проверки)
make test
```

## Зависимости

| Пакет | Назначение |
|-------|-----------|
| `fastapi` + `uvicorn` | REST API сервер |
| `sse-starlette` | MCP SSE транспорт |
| `openpyxl` | Excel с форматированием |
| `python-docx` | Word с форматированием |
| `pypdf` | PDF чтение |
| `Pillow` + `trimesh` | Изображения и 3D-меши |
| `duckduckgo-search` | Веб-поиск |
| `cryptography` | HMAC подписи |

Опционально в системе: `openscad`, `freecad`, `chromium` (устанавливаются Dockerfile).

## Лицензия

MIT
