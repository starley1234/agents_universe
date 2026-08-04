# S.P.E.C.T.R.U.M.

**Semantic Processing & Extraction Cluster for ToR, Reports, Unstructured Media**

S.P.E.C.T.R.U.M. — автономный «завод» по переработке сырых данных в структурированный интеллект. Приложение превращает хаос из файлов и ссылок в чистую семантическую базу знаний, с которой можно общаться или поручать выполнение задач на основе этих данных.

---

## Ключевые возможности

### 🔄 Cascade Ingestion (Каскадное поглощение)
Система не пасует перед сложными форматами:
- **Уровень 1:** Прямой парсинг (текст, CSV, Excel/Word)
- **Уровень 2:** OCR (Tesseract/PaddleOCR) для простых сканов
- **Уровень 3:** Vision-LLM (VLM) для понимания сложных схем, чертежей и рукописного текста

### 🔗 Source-to-Chunk Traceability
Каждое утверждение в RAG имеет «цифровой след». Вы всегда видите не только файл-источник, но и конкретную страницу, лист Excel или координаты на картинке, откуда взята информация.

### 🧠 Hybrid Knowledge Base
Создаёт одновременно:
- **Векторный индекс** (ChromaDB / Qdrant) — для поиска по смыслу
- **Семантический граф** (in-memory + JSON) — для понимания связей между документами

### ⚙️ Autonomous Worker Mode
Это не просто чат. Это агент, которому можно дать задание:
> «Проанализируй все PDF в папке и заполни Excel-таблицу сравнения характеристик двигателей»

---

## Быстрый старт

### 1. Установка

```bash
cd s_p_e_c_t_r_u_m
pip install -r requirements.txt
```

### 2. Конфигурация

```bash
cp .env.example .env
# Редактируйте .env под ваши нужды
```

### 3. Запуск

```bash
# REST API
make serve

# Chainlit UI
make ui

# CLI-режим
make cli

# Демонстрация
make demo

# Самопроверка
make check
```

### 4. Тесты

```bash
make test
```

---

## Архитектура

```text
s_p_e_c_t_r_u_m/
├── spectrum/
│   ├── config.py          # Конфигурация из .env
│   ├── env.py             # Загрузка .env
│   ├── ingestor/          # Загрузчики: PDF, URL, Excel, Image
│   │   ├── base.py        # Абстракция Ingestor + IngestResult
│   │   ├── pdf.py         # PDF-парсинг (pymupdf)
│   │   ├── url.py         # Веб-страницы (requests + BS4, Playwright)
│   │   ├── excel.py       # Excel/CSV (openpyxl)
│   │   ├── image.py       # OCR + VLM
│   │   └── factory.py     # Автовыбор ингестора
│   ├── processor/         # Обработка: дробление, VLM-анализ
│   │   ├── chunker.py     # Чанкер с overlap
│   │   ├── vlm_analyzer.py# Vision-LLM анализатор
│   │   └── pipeline.py    # Полный пайплайн обработки
│   ├── storage/           # Хранилища
│   │   ├── vector.py      # ChromaDB / Qdrant
│   │   ├── graph.py       # Семантический граф
│   │   └── file_store.py  # Управление файлами
│   ├── brain/             # RAG и агент
│   │   ├── rag.py         # Retrieval-Augmented Generation
│   │   ├── prompts.py     # Системные промпты
│   │   └── agent.py       # Автономный агент
│   ├── api/               # REST API (FastAPI)
│   │   ├── app.py         # Маршруты
│   ├── ui/                # Chainlit UI
│   │   └── app.py         # Чат + Drag-and-Drop
│   ├── demo.py            # Демонстрация
│   └── __main__.py        # CLI entrypoint
├── tests/                 # Автотесты
├── Makefile               # Команды
├── Dockerfile             # Docker-образ
├── docker-compose.yml     # Docker Compose (app + Qdrant + Redis)
└── .env.example           # Пример конфигурации
```

---

## Сценарий использования

### Загрузка данных

```python
from spectrum.brain.agent import Agent

agent = Agent.from_settings()

# Индексация файла
result = agent.ingest_file("/data/contracts/contract_001.pdf")

# Индексация директории
results = agent.ingest_directory("/data/contracts_2024")

# Индексация URL
result = agent.ingest_url("https://docs.example.com/spec")
```

### Вопрос-Ответ (RAG)

```python
response = agent.ask("Какие сроки поставки указаны во всех договорах?")
print(response.answer)
for source in response.sources:
    print(f"  📎 {source.citation()}")
```

### Автономные задачи

```python
result = agent.execute_task(
    "Проанализируй все договоры и составь сводную таблицу: "
    "поставщик, сумма, сроки, условия оплаты"
)
print(result.result)
```

### REST API

```bash
# Здоровье
curl http://localhost:8118/health

# Загрузка файла
curl -X POST http://localhost:8118/api/ingest/file \
  -F "file=@contract.pdf"

# Вопрос
curl -X POST http://localhost:8118/api/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "Какие договоры на сумму более 1 млн?"}'

# Задача
curl -X POST http://localhost:8118/api/task \
  -H "Content-Type: application/json" \
  -d '{"task": "Сравни все договоры"}'

# Статистика
curl http://localhost:8118/api/stats
```

---

## Технологический стек

| Компонент | Технология | Описание |
|:---|:---|:---|
| **Orchestration** | Custom Pipeline | Пайплайн: ingest → chunk → store → RAG |
| **Vector DB** | ChromaDB / Qdrant | Векторный поиск (ChromaDB — без Docker) |
| **Parsing** | pymupdf, openpyxl, BS4 | PDF, Excel, HTML |
| **OCR** | Tesseract / PaddleOCR | Распознавание сканов |
| **VLM** | OpenAI-compatible API | Анализ схем и чертежей |
| **Embeddings** | sentence-transformers | Векторные представления |
| **API** | FastAPI | REST API |
| **UI** | Chainlit | Web-интерфейс для чата |
| **Task Queue** | Arq (Redis) | Фоновая обработка |
| **Web Scraping** | Playwright | JS-рендеринг |

---

## Настройки (.env)

```bash
# === APP SETTINGS ===
PROJECT_NAME=SPECTRUM
APP_PORT=8118
WORKSPACE_DIR=./knowledge_base

# === PROCESSING ===
CHUNK_SIZE=1024
CHUNK_OVERLAP=200
USE_VLM=false

# === LLM CONFIGURATION ===
LLM_ACTIVE_PROVIDER=fake  # fake, openrouter, custom_remote, ollama

# === VECTOR DB ===
VECTOR_STORE=chroma  # chroma (встроенный) или qdrant (Docker)
```

---

## Docker

```bash
# Запуск (app + Qdrant + Redis)
make docker-up

# Остановка
make docker-down
```

---

## Тесты

```bash
make test
```

Покрытие (**82 проверки**):
- `test_config.py` — конфигурация, .env, профили LLM (9 проверок)
- `test_ingestor.py` — PDF, CSV, URL, Image, Text ингесторы (10 проверок)
- `test_chunker.py` — дробление текста, overlap, стратегии (10 проверок)
- `test_vector.py` — ChromaDB: CRUD, поиск, персистентность (10 проверок)
- `test_graph.py` — семантический граф: узлы, связи, save/load (11 проверок)
- `test_pipeline.py` — полный пайплайн обработки (8 проверок)
- `test_rag.py` — RAG: поиск, контекст, трейсабилити (7 проверок)
- `test_agent.py` — агент: индексация, задачи, статистика (10 проверок)
- `test_api.py` — REST API: маршруты, модели (7 проверок)

---

## Почему S.P.E.C.T.R.U.M.?

- **Полная автономность:** Работает без внешних зависимостей (LLM опционален)
- **Прозрачность:** Source Citation — всегда видно, откуда взята информация
- **Каскадный парсинг:** От простых CSV до сложных чертежей
- **Семантический граф:** Не только векторный поиск, но и связи между документами
- **Автономный агент:** Не просто чат, а исполнитель задач
- **Масштабируемость:** Изолированные базы знаний для разных отделов
