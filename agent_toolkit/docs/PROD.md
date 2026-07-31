# Руководство по развёртыванию agent_toolkit в продакшне (Production Guide)

Данное руководство описывает лучшие практики и инструкции по безопасному развёртыванию **agent_toolkit** в боевой среде (Docker, Kubernetes, Bare-metal).

---

## 1. Контейнеризация Docker и Docker Compose (Всё необходимое для сервиса в целом)

Для обеспечения 100% работоспособности всех **159 инструментов и 266 скилсов** без необходимости доустановки внешних библиотек или браузеров, наш продакшн-образ `Dockerfile` и `docker-compose.yml` включают **полный комплект системных зависимостей и драйверов**:
- **Базовые утилиты и безопасность**: `ca-certificates`, `curl`, `wget`, `git`, `make`, `procps`, `openssl`, `openssh-client`, `file`, `jq`, `unzip`, `zip`, `bzip2`, `tar`, `gzip`, `xz-utils`.
- **Клиенты и драйверы СУБД**: `libpq-dev` (PostgreSQL), `default-mysql-client` (MySQL), `sqlite3`.
- **Обработка PDF, документов и отчётов**: `poppler-utils`, `pandoc`.
- **Аудио и мультимедиа TTS**: `ffmpeg`, `libsndfile1`.
- **Графика, САПР (OpenSCAD, xvfb) и 3D STL Canvas**: `openscad`, `xvfb`, `fontconfig`, `fonts-dejavu-core`, `libgl1`, `libglib2.0-0`, `libsm6`, `libxext6`, `libxrender1`, `libgomp1` (поддерживается автоматический запуск через `xvfb-run -a` в бездисплейных Linux-средах и разбор как ASCII, так и бинарного Binary STL).
- **Безголовый браузер Chromium**: Встроенный пакет `chromium` и `chromium-driver` для скрапинга, Playwright и Puppeteer (с переменными `CHROME_BIN=/usr/bin/chromium`, `CHROMEDRIVER_BIN=/usr/bin/chromedriver` и `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1`).
- **Python экосистема**: Устанавливаются все группы зависимостей `pip install .[all]` (FastAPI, Office, Web/Scraping, DB, PDF, Image/3D, Crypto, YAML).

### Запуск сервиса в Docker Compose:
```bash
# Автоматическая сборка и запуск контейнера с монтированием Workspace
docker compose up --build -d
```

---

## 2. Переменные окружения (`.env`)

Все параметры системы настраиваются через переменные окружения с префиксом `AGENT_TOOLKIT_` или стандартными именами интеграций (см. `agent_toolkit/.env.example`):

- **`AGENT_TOOLKIT_ENV=production`** — переводит систему в боевой режим. По умолчанию отключает мок-режимы (`mock_mode=false`).
- **`AGENT_TOOLKIT_WORKSPACE=/var/lib/agent_toolkit/workspace`** — корневой каталог песочницы для изоляции файлов и артефактов.
- **`AGENT_TOOLKIT_CONFIG_PATH=/var/lib/agent_toolkit/workspace/toolkit_config.json`** — путь для автоматической загрузки конфигурации реестра (Configuration as Code).
- **`AGENT_TOOLKIT_ALLOW_DANGEROUS=false`** — запрещает выполнение необратимых действий (удаление файлов, отправка писем SMTP, выполнение shell-команд), если явно не получено разрешение через HITL или политику.
- **`AGENT_TOOLKIT_READ_ONLY=true`** — переводит все инструменты в режим только для чтения (запрещает запись на диск и DML в БД).
- **Реквизиты интеграций**:
  - `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`
  - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
  - `S3_ENDPOINT_URL`, `S3_BUCKET_NAME`
  - `ERP_ODATA_URL`, `TC_ENDPOINT_URL`

---

## 3. Самопроверка окружения (Self-Check CLI)

Перед запуском сервиса в продакшне рекомендуется выполнить автоматическую самопроверку:

```bash
python3 -m agent_toolkit check
```

Проверка контролирует:
- Доступность и права записи в `Workspace`;
- Корректность регистрации всех **159 встроенных инструментов**;
- Валидность схем по спецификации OpenAI / MCP;
- Статус безопасных и опасных инструментов.

---

## 3. Боевой прогон и диагностика инструментов (Production Diagnostics & Test-Prod)

Для проверки работоспособности всех **159 инструментов (266 скилсов)** на боевом сервере с превью результатов выполнения и автоматическим отключением неработающих предусмотрен специальный диагностический движок:

### 1. Запуск через CLI в терминале сервера:
```bash
# Быстрый прогон всех инструментов с выводом таблицы превью результатов
python3 -m agent_toolkit test-prod

# Прогнать и автоматически отключить инструменты с ошибками (❌) и ненастроенными реквизитами (⚠️)
python3 -m agent_toolkit test-prod --disable-failed --disable-unconfigured

# Получить результат в формате JSON
python3 -m agent_toolkit test-prod --json
```

### 2. Запуск через визуальный Web UI (`GET /ui`):
- Перейдите во вкладку **"⚙️ Настройки и профили (Settings)"** -> карточка **"🧪 Прогон и диагностика всех инструментов на боевой (Production Diagnostics)"**.
- Нажмите кнопку **"▶️ Запустить боевой прогон"** для отображения интерактивной таблицы со статусами (`✅ Работает`, `⚠️ Требует настройки`, `❌ Ошибка`), временем ответа (мс) и превью результата или подсказкой по настройке.
- Нажмите **"✕ Прогнать и автоматически отключить неработающие"** или используйте кнопки **[Включить / Отключить]** в строке каждого инструмента, чтобы управлять их доступностью. Состояние автоматически сохраняется в `toolkit_config.json` (Configuration as Code).

### 3. Запуск через REST API:
- **`GET /api/tools/test-production`** — запустить диагностический тест и вернуть отчёт JSON.
- **`POST /api/tools/test-production`** — с телом `{"disable_failed": true, "disable_unconfigured": true}` для автоматической фильтрации реестра.

---

## 4. Запуск HTTP REST API и MCP-сервера

Запуск сервера на боевом порту:
```bash
python3 -m agent_toolkit serve --port 8090 --host 0.0.0.0
```

### Эндпоинты сервера:
- **`GET /health`** (или `/api/health`) — Health Check для Load Balancer / Kubernetes Liveness Probe (`{"status": "ok", "tools_count": 159, ...}`).
- **`GET /api/tools`** — список всех инструментов с возможностью фильтрации по скилсу (`?skill=files`) или категории (`?category=local`).
- **`GET /api/tools/search?query=...`** — быстрый умный поиск инструмента по ключевым словам и синонимам.
- **`POST /api/tools/{name}/execute`** — выполнение инструмента с JSON-телом.
- **`POST /api/mcp/rpc`** — точка входа MCP (Model Context Protocol) JSON-RPC 2.0 (`initialize`, `tools/list`, `tools/call`).
- **`GET /ui`** — визуальный одностраничный веб-интерфейс (SPA) каталога инструментов с 3D-вьювером STL и конструктором форм.

---

## 5. Потокобезопасность (Thread-Safety)

В многоагентных системах несколько потоков или асинхронных воркеров могут обращаться к реестру одновременно. В `agent_toolkit` все ключевые структуры защищены блокировками `threading.RLock`:
- `ToolRegistry` — безопасная конкурентная регистрация, поиск и выполнение инструментов.
- `ArtifactStore` — безопасное параллельное сохранение и индексация артефактов.
- `MemoryStore` — безопасная запись фактов в долговременную память агента.

---

## 6. Защита от SSRF (Server-Side Request Forgery)

Все инструменты сетевых запросов (`web.fetch_page`, `http.request`, `site_qa.check_url`) проверяют целевой URL через `SecurityPolicy`:
- В продакшне блокируются обращения к `localhost`, `127.0.0.1`, `0.0.0.0`, `::1` и внутренним IP-адресам (RFC 1918, Link-local, метаданным облачных инстансов `169.254.169.254`).
