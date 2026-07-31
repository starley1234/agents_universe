# Agent Toolkit

**Новый независимый проект** в корне монорепозитория, предоставляющий унифицированный, быстрый и умный инструментарий для LLM-агентов с поддержкой Python API, HTTP REST API, MCP (Model Context Protocol) и визуального веб-интерфейса (Web UI).

---

## Ключевые возможности

1. **Быстрый и умный реестр (`ToolRegistry`)**
   - Мгновенный поиск подходящего инструмента по естественному текстовому запросу (`registry.search("create a word document")`), даже если агент не знает точное название.
   - Встроенная система синонимов, токенизации и взвешенного многофакторного ранжирования (работает за **< 0.5 мс** без тяжелых векторных БД).
2. **159 специализированных инструментов и 266 скилсов (`skills`)**
   - Охватывает промышленные БД (PostgreSQL/MySQL, ER-диаграммы), вычисление формул Excel, проведение OData-документов 1С, фиксацию базовых линий Teamcenter PLM, браузерную автоматизацию Playwright/Puppeteer, векторный HNSW индекс, MapReduce, контроль квот ресурсов, лимиты частоты вызовов (Rate Limiting), веб-поиск (DuckDuckGo), вёрстку, файлы, САПР/3D-моделирование, сопромат, радиосвязь/антенны, аэродинамику, акустику, VLM-парсинг PDF, SQL, Git, HITL, субагентов и др.
3. **Двухпротокольный доступ: API и MCP**
   - **Python API SDK** (`ToolkitClient`, `ToolRegistry.execute`) для локального вызова.
   - **HTTP REST API** на FastAPI (`/api/tools`, `/api/tools/search`, `/api/skills`, `/api/artifacts`).
   - **MCP Сервер и Клиент** (`MCPServer`, `MCPClient` в `agent_toolkit.integrations.mcp`), полностью совместимые со спецификацией Model Context Protocol (`tools/list`, `tools/call`, JSON-RPC 2.0).
4. **Визуальный каталог и 3D-вьювер (Web UI / Explorer)**
   - Одностраничное приложение (`GET /ui` или `/`), позволяющее фильтровать инструменты по скилсам, просматривать схемы, выполнять вызовы на лету и визуализировать 3D-меши STL на HTML5 Canvas.
5. **Продакшн-готовность и безопасность по умолчанию**
   - Изоляция рабочей области (`Workspace`), защита от SSRF-атак, полная потокобезопасность (`threading.RLock`) для многоагентных сред, Health Check (`/health`) и Docker / Docker Compose развёртывание.

---

## Быстрый старт (Quickstart)

### 1. Установка пакета
```bash
# Установка в редактируемом режиме со всеми опциональными зависимостями (FastAPI, Office)
pip install -e .[all]
```

### 2. Инициализация реестра в коде Python
```python
from agent_toolkit import build_default_registry

# Создаёт изолированную рабочую область в /tmp/agent_toolkit_ws
# и автоматически регистрирует все 125 встроенных инструментов
registry = build_default_registry()

print(f"Зарегистрировано инструментов: {len(registry.list_tools())}")
print(f"Уникальных скилсов: {len(registry.group_by_skill())}")
```

### 3. Умный поиск нужного инструмента
Агенту не обязательно знать точное имя функции — реестр находит инструмент по смыслу задачи:
```python
# Поиск инструмента для расчёта антенн
hits = registry.search("рассчитать направленную антенну яги dbi", limit=1)
tool, score = hits[0]
print(f"Найдено: {tool.name} (релевантность: {score})")
# -> physics.calc_yagi_uda_antenna

# Поиск инструмента для экспорта в Word
doc_hits = registry.search("создать документ word отчёт docx", limit=1)
print(doc_hits[0][0].name)
# -> office.create_docx
```

### 4. Самопроверка продакшн-окружения из терминала (CLI)
```bash
# Выполнить полную самопроверку прав, схем и безопасности
python3 -m agent_toolkit check

# Просмотреть список инструментов в консоли
python3 -m agent_toolkit list --skill cad

# Выполнить инструмент из командной строки
python3 -m agent_toolkit execute crypto.generate_uuid
```

### 5. Запуск HTTP REST API и Web UI
```bash
# Запустить сервер на порту 8090
python3 -m agent_toolkit serve --port 8090 --host 0.0.0.0

# После запуска откройте в браузере:
#   http://localhost:8090/ui         — Визуальный каталог и 3D-вьювер артефактов
#   http://localhost:8090/docs       — Документация OpenAPI / Swagger
#   http://localhost:8090/health     — Health Check для Kubernetes / Docker
```

---

## Примеры использования по доменам (Usage Examples)

### Пример 1: САПР / CAD и 3D-моделирование (OpenSCAD, FreeCAD, STL)
```python
from agent_toolkit import build_default_registry
reg = build_default_registry()

# 1. Генерация параметрической шестерни OpenSCAD (модуль m=2, зубьев Z=20)
reg.execute("cad.generate_gear", path="gear20.scad", module_mm=2.0, teeth_count=20)

# 2. Рендеринг модели в STL и получение точных геометрических метрик (объём, габариты в мм, watertight)
scad_report = reg.execute("cad.render_openscad", path="gear20.scad")
print(scad_report)

# 3. Точный анализ существующего STL-меша (по теореме Гаусса-Остроградского)
stl_stats = reg.execute("cad.inspect_stl", path="gear20.stl")
print(stl_stats)

# 4. Расчёт массы и моментов инерции детали из алюминия
mass_report = reg.execute("cad.calculate_mass_inertia", path="gear20.stl", material="aluminum")
```

### Пример 2: Инженерные и физические расчёты (Сопромат, Антенны, Аэродинамика, Акустика)
```python
# 1. Сопромат: расчёт напряжения σ, запаса прочности и прогиба стальной балки
strength = reg.execute(
    "physics.calc_strength",
    load_n=10000.0,
    area_mm2=50.0,
    yield_strength_mpa=250.0,
    beam_length_mm=100.0,
    modulus_gpa=200.0,
    inertia_mm4=1000.0,
)

# 2. Антенны: расчёт направленной 5-элементной антенны Уда-Яги на 433.92 МГц
yagi = reg.execute("physics.calc_yagi_uda_antenna", freq_mhz=433.92, elements_count=5)

# 3. Согласование: расчёт Г-образной согласующей LC-цепи (L-Network) для антенны 25 Ом
matching = reg.execute(
    "physics.calc_antenna_matching_network",
    freq_mhz=433.92,
    antenna_r_ohm=25.0,
    antenna_x_ohm=10.0,
)

# 4. Аэродинамика: число Рейнольдса Re и сила сопротивления воздушного потока (20 м/с)
airflow = reg.execute(
    "physics.calc_airflow",
    velocity_m_s=20.0,
    char_length_m=0.5,
    drag_coeff=0.3,
    frontal_area_m2=2.0,
)

# 5. Акустика: расчёт скорости звука при 20°C, SPL в дБ и собственной частоты резонатора Гельмгольца
acoustics = reg.execute(
    "physics.calc_acoustics",
    freq_hz=440.0,
    medium="air",
    temperature_c=20.0,
    pressure_pa=0.2,
)
helmholtz = reg.execute(
    "physics.calc_helmholtz_resonator",
    volume_liter=15.0,
    port_diam_mm=50.0,
    port_length_mm=100.0,
)
```

### Пример 3: Интеллектуальный VLM-парсинг PDF-документов
```python
# 1. Предварительная быстрая классификация типов страниц (без расхода токенов LLM)
cls_report = reg.execute("vision.classify_pdf_pages", path="invoice.pdf", pages="all")

# 2. Умное распознавание счёта-фактуры через VLM с арифметической самопроверкой сумм
vlm_report = reg.execute(
    "vision.parse_pdf_vlm",
    path="invoice.pdf",
    pages="1",
    doc_type_hint="invoice",
)
print(vlm_report)
```

### Пример 4: Инспекция схемы и безопасное выполнение SQL-запросов (SQLite)
```python
# 1. Инспекция таблиц, DDL-схемы и количества строк в базе
schema = reg.execute("sql.inspect_schema", db_path="app.db")

# 2. Выполнение безопасного SELECT-запроса с параметрами
rows = reg.execute(
    "sql.execute_query",
    query="SELECT * FROM products WHERE price > ?",
    params_json="[100.0]",
)
print(rows)
```

### Пример 5: Человек в контуре (HITL) и делегирование субагентам
```python
# 1. Задать оператору интерактивный уточняющий вопрос
answer = reg.execute("ask.human", question="Одобрить публикацию отчёта?", options_json='["Да", "Нет"]')

# 2. Запросить разрешение на опасное действие (dangerous=True)
reg.execute("hitl.request_approval", action="delete_database", reason="Регламентная очистка")

# 3. Делегировать подзадачу специализированному субагенту (researcher, coder, auditor, reporter)
res_sub = reg.execute(
    "agent.call_subagent",
    agent_name="auditor",
    task="Проверить отчёт на соответствие нормативам WCAG 2.1",
)
```

### Пример 6: Генерация официальных отчётов (Word .docx, Excel .xlsx, Markdown)
```python
# 1. Создание документа Word (.docx) из отформатированного текста
reg.execute(
    "office.create_docx",
    path="audit_report.docx",
    title="Протокол аудита",
    content="## Результаты\nПроверка завершена успешно.",
)

# 2. Создание электронной таблицы Excel (.xlsx)
reg.execute(
    "office.create_xlsx",
    path="summary.xlsx",
    sheet_name="Метрики",
    headers_json='["Бренд", "Доля полки SOS"]',
    rows_json='[["Acme", "50.0%"], ["Other", "50.0%"]]',
)

# 3. Генерация счёта на оплату (Invoice) с подсчётом итогов
reg.execute(
    "templates.create_invoice",
    invoice_number="INV-101",
    customer="ООО Ритейл",
    items_json='[{"name": "Аудит полки", "qty": 1, "price": 50000}]',
)
```

---

## Продакшн-адаптация и Docker (Production & Docker Deployment)

Проект полностью адаптирован для эксплуатации в боевых микросервисном и многоагентном окружениях (см. подробности в **`agent_toolkit/docs/PROD.md`**):

1. **Конфигурация через `.env` (`agent_toolkit/config.py`)**:
   - `AGENT_TOOLKIT_ENV=production` — отключает мок-режимы (`mock_mode=False`).
   - `AGENT_TOOLKIT_ALLOW_DANGEROUS=false` — блокирует необратимые операции без явного разрешения.
   - `AGENT_TOOLKIT_READ_ONLY=false` — переключение в режим только для чтения.
2. **Потокобезопасность (`threading.RLock`)**:
   - Все 11 внутренних сервисов и хранилищ (`ToolRegistry`, `ArtifactStore`, `MemoryStore`, `JobStore`, `HitlService`, `SubagentService`, `AuditService`, `MailService`, `MaxService`, `TelegramService`, `S3Service`) полностью потокобезопасны и проверены нагрузочным тестом в 10 параллельных потоков.
3. **Развёртывание в Docker (`Dockerfile`, `docker-compose.yml` — всё необходимое для сервиса в целом)**:
   - Сборка на базе минимального образа `python:3.11-slim` с непривилегированным пользователем `USER agentuser` (Security Best Practice).
   - Включает **полный набор системных зависимостей для работы всех 159 инструментов сервиса**: безголовый браузер Chromium (`chromium`, `chromium-driver`) для веб-скрапинга и Playwright, утилиты обработки PDF (`poppler-utils`, `pandoc`), клиенты БД (`libpq-dev`, `default-mysql-client`, `sqlite3`), мультимедиа и TTS (`ffmpeg`), САПР и 3D-графика (`openscad`, `xvfb`, `fontconfig`, `libgl1`, с поддержкой `xvfb-run -a` для headless-рендеринга и разбором бинарного Binary STL), а также полный стек Python-зависимостей (`pip install .[all]`).
   - Встроенная проверка `HEALTHCHECK CMD curl -f http://localhost:8090/health || exit 1`.
   - Запуск через Docker Compose V2:
     ```bash
     docker compose up -d --build
     ```
4. **Боевой прогон и диагностика инструментов (CLI `test-prod`, Web UI, REST API)**:
   - Быстрая проверка всех 159 инструментов на боевой с выводом превью результатов и статус-лейблов (`✅ Работает`, `⚠️ Требует настройки`, `❌ Ошибка`):
     ```bash
     # Прогнать на боевом сервере и получить таблицу превью
     python3 -m agent_toolkit test-prod

     # Прогнать и автоматически отключить ненастроенные или сбойные инструменты
     python3 -m agent_toolkit test-prod --disable-failed --disable-unconfigured
     ```
   - Также доступно через интерактивную панель **"🧪 Прогон и диагностика на боевой"** во вкладке Settings веб-интерфейса (`GET /ui`) и REST API (`GET/POST /api/tools/test-production`).
5. **Единый стандарт конфигурации монорепозитория (.env и матрица портов `config.*`)**:
   - Реализована унифицированная матрица конфигурации `MONOREPO_PROJECTS_CONFIG` (`agent_toolkit/monorepo_config.py`) и инструменты генерации/валидации конфигурации для всех 10 автономных проектов лаборатории с непересекающимися портами `APP_PORT` (`8090` для `agent_toolkit`, `8101`..`8109` для остальных):
     ```bash
     # Просмотреть всю матрицу портов 10 проектов
     python3 -m agent_toolkit config --list

     # Сгенерировать стандартный .env по единому шаблону для любого проекта
     python3 -m agent_toolkit config --generate data_forge --save data_forge/.env

     # Сгенерировать docker-compose.override.yml для проброса порта 8101..8109 и доступа из браузера
     python3 -m agent_toolkit config --docker-override agent_system --save agent_system/docker-compose.override.yml
     ```

---

## Полный перечень всех 163 зарегистрированных инструментов

<details>
<summary><b>Нажмите, чтобы развернуть список всех 163 инструментов по категориям</b></summary>

### Конфигурация монорепозитория и DevOps (`config`)
- `config.generate_monorepo_env` — Сгенерировать стандартный файл конфигурации (.env) по единому шаблону для любого из 10 проектов монорепозитория с выделенным непересекающимся портом APP_PORT.
- `config.list_monorepo_ports` — Вывести матрицу непересекающихся портов APP_PORT (8090, 8101..8109) и названий PROJECT_NAME для всех 10 проектов лаборатории agents_universe.
- `config.validate_env_settings` — Проверить .env файл на соответствие единому стандарту конфигурации монорепозитория (PROJECT_NAME, APP_PORT, LLM, DATABASE, MAIL, TELEGRAM, MCP, WORKSPACE).
- `config.generate_docker_override` — Сгенерировать файл docker-compose.override.yml для любого проекта (agent_system, saps и др.) с правильным пробросом порта APP_PORT (8101..8109) и AGENT_HOST=0.0.0.0 для доступа из браузера.

### Локальные файлы (`local`)
- `files.read_file` — Чтение текстового файла с номерами строк.
- `files.write_file` — Создание или перезапись текстового файла.
- `files.edit_file` — Точечная замена фрагмента текста в файле.
- `files.list_dir` — Просмотр содержимого директории с размерами файлов.
- `files.find_files` — Поиск файлов по шаблону/маске (glob pattern).
- `files.file_info` — Получить метаданные файла: размер, количество строк и SHA256.
- `files.remove_file` — Удалить файл внутри рабочей области (*dangerous*).

### Офисные документы (`office`)
- `office.create_docx` — Создать офисный документ Word (`.docx`) из текста или Markdown.
- `office.create_xlsx` — Создать электронную таблицу Excel (`.xlsx`) из JSON-данных.
- `office.inspect_docx` — Прочитать и извлечь текст из файла `.docx`.
- `office.inspect_xlsx` — Прочитать ячейки таблицы `.xlsx` в виде текста.

### Шаблоны и отчёты (`templates`)
- `templates.render_markdown` — Отрендерить Markdown-шаблон с подстановкой переменных JSON.
- `templates.render_report` — Сгенерировать стандартный структурированный отчёт с таблицей метрик.
- `templates.create_invoice` — Сгенерировать счёт на оплату (Invoice) по списку товаров.
- `templates.list_templates` — Получить список всех доступных встроенных шаблонов.

### QA сайтов, вёрстки и создание веб-сайтов (`qa` / `local`)
- `site_qa.check_url` — Проверить доступность сайта (HTTP статус, время ответа).
- `site_qa.check_links` — Проанализировать ссылки в HTML-коде (внутренние, внешние, пустые).
- `site_qa.check_accessibility` — Проверить HTML-код на соответствие критериям доступности WCAG 2.1.
- `site_qa.check_seo_meta` — Проверить SEO-метатеги HTML (`title`, `description`, `canonical`, OpenGraph).
- `web.build_static_site` — Сгенерировать полный статический веб-сайт (HTML5/CSS3) из списка страниц в директории Workspace.
- `web.create_landing_page` — Сгенерировать современную отзывчивую посадочную страницу (Landing Page) с Hero-блоком, преимуществами и кнопкой CTA.
- `web.audit_site_seo_performance` — Провести аудит HTML-кода сайта на предмет SEO-тегов, мобильной вёрстки и производительности.

### Базы данных, SQL и промышленные СУБД (`local` / `db`)
- `sql.inspect_schema` — Просмотреть схему базы данных SQLite (таблицы, колонки, DDL).
- `sql.execute_query` — Выполнить SQL-запрос к базе данных (SELECT, INSERT, UPDATE, DELETE).
- `db.postgres_execute` — Выполнить SQL-запрос к промышленной СУБД PostgreSQL (SELECT, INSERT, UPDATE, DELETE) с пулом транзакций.
- `db.mysql_execute` — Выполнить SQL-запрос к промышленной СУБД MySQL (SELECT, INSERT, UPDATE, DELETE) с поддержкой транзакций.
- `db.generate_er_diagram` — Проанализировать схему таблиц и связей базы данных и сгенерировать ER-диаграмму в формате Mermaid.js или Markdown.

### PDF документы (`local`)
- `pdf.read_pages` — Прочитать текстовое содержимое PDF-документа по страницам.
- `pdf.extract_tables` — Извлечь таблицы со страницы PDF-файла в формате Markdown/CSV.

### Git и анализ кода (`local`)
- `git.status` — Посмотреть статус изменений Git-репозитория.
- `git.diff` — Посмотреть diff изменений в коде.
- `git.log` — Посмотреть историю последних коммитов репозитория.
- `code.run_linter` — Провести проверку синтаксиса Python файлов (`compileall`).
- `code.run_tests` — Запустить автоматические тесты (`unittest`/`pytest`).
- `code.apply_patch` — Наложить патч (`unified diff` / `git diff`) на исходный файл.

### Песочница и Shell (`local`)
- `shell.run_command` — Выполнить команду Shell/Bash в песочнице (*dangerous*).
- `python.exec_snippet` — Выполнить фрагмент Python-кода в изолированном субпроцессе (*dangerous*).

### Долговременная память, векторный HNSW индекс и RAG (`local`)
- `memory.save_fact` — Сохранить факт или знание в долговременную память агента.
- `memory.search_facts` — Найти сохранённый факт в памяти агента по ключевому слову или тегу.
- `memory.vector_store_hnsw` — Проиндексировать документ в локальном векторном хранилище HNSW (косинусное сходство) для семантического поиска.
- `memory.vector_search_hnsw` — Семантический поиск в локальном векторном индексе HNSW по косинусному сходству текста запроса.
- `rag.query_kb` — Семантический/текстовый RAG-поиск фрагментов в локальной базе знаний.

### Планировщик задач / Cron (`local`)
- `jobs.schedule_task` — Запланировать регулярное выполнение инструмента по таймеру.
- `jobs.list_tasks` — Посмотреть список всех запланированных задач агента.
- `jobs.run_pending` — Запустить выполнение всех созревших (pending) запланированных задач.

### Человек в контуре / HITL (`local`)
- `ask.human` — Задать уточняющий вопрос человеку/оператору с опциональным списком вариантов.
- `hitl.request_approval` — Запросить у человека разрешение на выполнение опасного действия.

### Многоагентность и оркестрация (`local`)
- `agent.call_subagent` — Делегировать задачу специализированному субагенту (`researcher`, `coder`, `auditor`, `reporter`).
- `agent.list_agents` — Получить список всех доступных в системе субагентов и их компетенций.
- `agent.parallel_map_reduce` — Параллельный запуск субагента по паттерну MapReduce для распределённого выполнения задач и сведения отчёта.

### Контроль квот ресурсов, лимиты частоты вызовов и безопасность (`policy` / `quota`)
- `policy.resource_quota_guard` — Установить и проверить квоты расхода токенов LLM, бюджета USD и вызовов инструментов для защиты от зацикливания.
- `policy.check_quota` — Проверить расход квоты перед вызовом и выбросить ошибку в случае превышения лимитов.
- `policy.reset_quota` — Сбросить счётчики расхода ресурсов и установить новые лимиты квоты.
- `policy.set_tool_rate_limit` — Установить индивидуальный лимит частоты вызовов для инструмента (например, не более 5 запросов `web.search` в минуту).
- `policy.list_rate_limits` — Получить список всех активных индивидуальных лимитов частоты вызовов инструментов.
- `policy.reset_rate_limits` — Сбросить или удалить индивидуальные лимиты частоты вызовов для указанного инструмента или для всех.

### Табличные данные, CSV и формулы Excel (`local`)
- `data.read_csv` — Прочитать CSV-файл в виде структурированной таблицы с заголовками.
- `data.write_csv` — Записать массив строк в формате CSV в файл.
- `data.convert_format` — Преобразовать данные между форматами (`JSON ↔ CSV ↔ YAML ↔ Markdown`).
- `data.aggregate_table` — Агрегировать массив JSON-объектов (`SUM`, `AVG`, `COUNT`, `MIN`, `MAX`) с группировкой.
- `data.excel_formula_eval` — Вычислить формулу Excel (`SUM`, `AVERAGE`, `MIN`, `MAX`, `COUNT`, арифметика) по значениям ячеек в формате JSON.

### DOM и скрапинг (`local`)
- `html.extract_by_selector` — Извлечь текст элементов HTML по CSS-селектору (`span.price`, `div#main`, `a`).
- `scraper.parse_feed` — Разобрать XML-ленту новостей RSS/Atom и извлечь заголовки и ссылки.

### Редактирование текста и RegEx (`local`)
- `text.regex_replace` — Замена фрагментов текста в файле по регулярному выражению (RegEx).

### Аудит и телеметрия (`local`)
- `audit.log_event` — Записать событие в журнал аудита (принятое решение, обоснование, действие).
- `telemetry.record_metrics` — Учесть расход токенов (`Prompt` / `Completion`) и вычислить стоимость вызова в USD.

### Криптография и подписи (`local`)
- `crypto.generate_uuid` — Сгенерировать случайный уникальный идентификатор UUIDv4.
- `crypto.hash_string` — Вычислить хеш строки (`SHA256`, `MD5`, `SHA1`).
- `crypto.verify_signature` — Проверить подлинность и целостность текста по подписи HMAC-SHA256.

### САПР / CAD, OpenSCAD, FreeCAD, 3D-геометрия (`local`)
- `cad.render_openscad` — Отрендерить 3D-модель из кода OpenSCAD (`.scad`) и получить точные числовые геометрические метрики (габариты, объём, `watertight`).
- `cad.render_openscad_views` — Отрендерить STL и изображения 3D-модели OpenSCAD в заданных ракурсах (`isometric`, `top`, `front`, `side`) и получить логи выполнения (`echo`).
- `cad.inspect_stl` — Рассчитать точные геометрические параметры 3D-модели STL (объём в см³, габариты в мм, площадь поверхности, замкнутость `watertight`).
- `cad.freecad_script` — Сгенерировать и сохранить параметрический Python-скрипт для твердотельного моделирования во FreeCAD.
- `cad.generate_gear` — Сгенерировать параметрическую модель прямозубой шестерни (Involute Spur Gear) в формате OpenSCAD по модулю и числу зубьев.
- `cad.generate_enclosure` — Сгенерировать параметрическую модель корпуса прибора с крышкой в формате OpenSCAD.
- `cad.convert_mesh_format` — Конвертировать 3D-меш между форматами (`STL ↔ OBJ ↔ PLY`).
- `cad.calculate_mass_inertia` — Рассчитать массу и моменты инерции $I_{xx}, I_{yy}, I_{zz}$ 3D-детали для конструкционного материала.
- `cad.generate_yagi_openscad` — Сгенерировать параметрическую 3D-модель направленной антенны Яги-Уда (Yagi-Uda) на OpenSCAD.
- `cad.generate_propeller_openscad` — Сгенерировать параметрическую 3D-модель малошумной крыльчатки/пропеллера на OpenSCAD.

### Физика, инженерия, антенны, аэродинамика и акустика (`local`)
- `physics.calc_strength` — Рассчитать механическое напряжение $\sigma$, запас прочности по пределу текучести и максимальный прогиб балок.
- `physics.calc_fatigue_life` — Рассчитать усталостную долговечность детали (S-N Curve / Fatigue Life) при знакопеременной нагрузке.
- `physics.calc_bolt_torque` — Рассчитать усилие предварительной затяжки и рекомендуемый момент затяжки болта М4–М12.
- `physics.calc_em_field` — Рассчитать электромагнитное поле (магнитную индукцию $B$ в мТл) прямого проводника с током и соленоида.
- `physics.calc_antenna` — Рассчитать геометрию антенны (полуволновой диполь $\lambda/2$, штырь $\lambda/4$) и длину волны по частоте в МГц.
- `physics.calc_antenna_vswr` — Рассчитать коэффициент стоячей волны (КСВ / VSWR), возвратные потери (Return Loss дБ) и эффективность согласования.
- `physics.calc_antenna_matching_network` — Рассчитать элементы согласующей LC-цепи (L-Network Matching: $L$ нГн, $C$ пФ) для согласования импеданса с линией 50 Ом.
- `physics.calc_yagi_uda_antenna` — Рассчитать размеры элементов (рефлектор, вибратор, директоры) и усиление dBi направленной антенны Уда-Яги.
- `physics.calc_patch_antenna` — Рассчитать размеры $W$ и $L$ печатной микрополосковой патч-антенны (PCB Patch Antenna) на субстрате FR4/Rogers.
- `physics.calc_rf_link_budget` — Рассчитать бюджет радиолинии (RF Link Budget): потери FSPL, уровень приёма Rx дБм и запас по затуханию (Fade Margin).
- `physics.calc_coaxial_cable` — Рассчитать волновое сопротивление $Z_0$ (Ом) и погонную ёмкость коаксиального кабеля/линии.
- `physics.calc_airflow` — Рассчитать аэродинамические параметры воздушного потока: число Рейнольдса (Re), режим течения и силу сопротивления.
- `physics.calc_fan_cooling` — Рассчитать необходимый расход вентилятора (CFM / м³/ч) для воздушного охлаждения при заданной тепловой мощности.
- `physics.calc_pipe_pressure_drop` — Рассчитать гидравлическое сопротивление / потери давления (Па) воздушного потока в трубе или канале.
- `physics.calc_acoustics` — Рассчитать параметры звуковой волны / акустики: скорость звука, длину волны $\lambda$, уровень SPL (дБ) и резонансы труб.
- `physics.calc_sound_barrier` — Рассчитать звукоизоляцию перегородки/стены (индекс звукоизоляции $R_w$ в дБ) по закону массы.
- `physics.calc_helmholtz_resonator` — Рассчитать собственную резонансную частоту $f_0$ акустического резонатора Гельмгольца/фазоинвертора.
- `physics.calc_propeller_thrust_power` — Рассчитать аэродинамическую тягу (Н), крутящий момент, мощность (Вт) и скорость кончика лопасти пропеллера/вентилятора.
- `physics.calc_propeller_noise` — Рассчитать акустический шум пропеллера/вентилятора (дБА), число Маха кончика лопасти и получить рекомендации по снижению шума.
- `physics.calc_low_noise_blade_geometry` — Рассчитать профиль и закон крутки $\theta(r)$ малошумной лопасти пропеллера по радиусу (25%, 50%, 75%, 100% R).

### Интеграции, сеть, почта, мессенджеры, S3, ERP и Teamcenter PLM (`integration` / `messaging` / `storage`)
- `mcp.list_remote_tools` — Получить список инструментов, доступных на подключённом MCP-сервере.
- `mcp.call_remote_tool` — Вызвать удалённый инструмент через Model Context Protocol (`tools/call`).
- `smtp.send_email` — Отправить электронное письмо через SMTP (*dangerous*).
- `smtp.read_emails` — Прочитать входящие сообщения из почтового ящика (IMAP).
- `max.send_message` — Отправить сообщение в чат мессенджера MAX (*dangerous*).
- `max.get_updates` — Получить последние сообщения/обновления бота MAX.
- `telegram.send_message` — Отправить сообщение в Telegram чат по `chat_id` (*dangerous*).
- `telegram.get_updates` — Получить список последних входящих сообщений бота в Telegram.
- `s3.list_objects` — Просмотреть список файлов (объектов) в S3 бакете.
- `s3.upload_file` — Загрузить локальный файл из рабочей папки в S3 бакет.
- `s3.download_file` — Скачать файл из S3 бакета в локальную рабочую папку.
- `s3.delete_object` — Удалить объект из S3 бакета (*dangerous*).
- `s3.get_url` — Получить публичный или presigned URL для объекта в S3.
- `image.generate` — Сгенерировать изображение по текстовому промпту (искусственный интеллект).
- `image.resize` — Уменьшить разрешение изображения для экономии стоимости VLM.
- `image.get_metadata` — Получить метаданные изображения: формат, размер, хеш SHA256.
- `deploy.check_service` — Проверить доступность TCP-сервиса (хост и порт).
- `deploy.generate_nginx_config` — Сгенерировать конфигурационный файл Nginx reverse-proxy.
- `deploy.generate_systemd_unit` — Сгенерировать файл службы (`.service`) для Systemd.
- `deploy.generate_docker_compose` — Сгенерировать файл `docker-compose.yml` по JSON-конфигурации сервисов.
- `web.search` — Выполнить поиск в интернете по ключевым словам и получить список ссылок и сниппетов.
- `web.fetch_page` — Скачать веб-страницу по URL и извлечь читаемый текст/Markdown.
- `web.search_duckduckgo` — Выполнить поиск в интернете через DuckDuckGo (поддерживает библиотеку `duckduckgo-search`, прямые HTTP-запросы к DuckDuckGo Lite и автономный режим).
- `web.search_news` — Поиск новостей и свежих публикаций в интернете через DuckDuckGo News с датой выхода и источником.
- `web.search_duckduckgo_answers` — Получить мгновенный ответ, определение, факты или карточку энциклопедии по запросу через DuckDuckGo Instant Answers / Wikipedia.
- `web.fetch_markdown` — Скачать веб-страницу по URL и преобразовать HTML в чистый структурированный Markdown (заголовки, списки, ссылки, таблицы, абзацы).
- `web.extract_links` — Извлечь все гиперссылки (URL и текст ссылки) из HTML-кода страницы или веб-сайта с фильтрацией по домену.
- `web.extract_tables_html` — Найти и извлечь таблицы (`<table>`) из HTML-страницы или URL и преобразовать их в таблицы Markdown или CSV.
- `web.extract_metadata_html` — Извлечь метаданные веб-страницы (`title`, `description`, OpenGraph `og:image`/`og:title`, canonical, keywords, RSS/Atom ленты, язык `html lang`).
- `web.check_robots_txt` — Проверить правила `robots.txt` для веб-сайта и выяснить, разрешено ли сканирование указанного URL для заданного User-Agent.
- `web.fetch_sitemap` — Скачать и разобрать XML-карту сайта (`sitemap.xml` или sitemap index) для обнаружения всех доступных страниц сайта.
- `web.extract_forms` — Проанализировать HTML-страницу или URL и найти все веб-формы (`<form>`), их action, метод (GET/POST) и список всех полей ввода.
- `web.submit_form` — Отправить данные веб-формы (POST / GET запрос) на целевой URL (action) с поддержкой JSON-данных или `application/x-www-form-urlencoded` (*dangerous*).
- `web.simulate_form_fill` — Смоделировать и валидировать заполнение HTML-формы перед отправкой: проверить обязательные поля required, типы данных, email, лимиты длины и доступные опции select.
- `web.simulate_browser_action` — Смоделировать последовательность действий браузера (`goto`, `fill`, `click`, `select`, `screenshot`, `wait`) для автоматизации веб-сценариев.
- `http.request` — Отправить HTTP/REST запрос (`GET`, `POST`, `PUT`, `DELETE`) к внешнему API.
- `tts.synthesize_speech` — Синтезировать речь из текста в аудиофайл (`.mp3` / `.wav`) для озвучивания отчёта.
- `erp.fetch_odata` — Запросить сущности из 1С/ERP по OData API (справочники, документы) с фильтрацией.
- `erp.post_odata_document` — Создание и проведение документа (счёт, заказ, накладная) в 1С / ERP через OData API (*dangerous*).
- `tc.login` — Авторизоваться в Teamcenter API (PLM система управления требованиями) по протоколу SOA / REST.
- `tc.get_requirement_item` — Получить требование из Teamcenter по ID (название, текст, статус, ревизия, категория АП).
- `tc.search_requirements` — Найти требования в базе Teamcenter PLM по ключевому слову или фильтру статуса.
- `tc.update_requirement_property` — Обновить свойство требования в Teamcenter (*dangerous*).
- `tc.export_requirements_spec` — Экспортировать всю спецификацию требований из Teamcenter PLM в формат JSON или Markdown.
- `tc.create_requirement_baseline` — Создание базовой линии (Baseline / Revision) спецификации требований в Teamcenter PLM (*dangerous*).
- `tc.compare_requirement_revisions` — Сравнение двух ревизий (Baseline) требования в Teamcenter PLM и вывод отчёта об изменениях.
- `web.playwright_session` — Интеграция с безголовыми браузерами (Playwright / Puppeteer) для загрузки интерактивных SPA-страниц, выполнения сценариев авторизации и динамического рендеринга JS.
- `web.puppeteer_action` — Выполнить точечное действие в браузере Puppeteer/Playwright (`click`, `fill`, `evaluate`, `wait_for_selector`, `screenshot`).
- `web.extract_schema_org` — Извлечь микроразметку Schema.org (`JSON-LD`, Microdata, OpenGraph) из HTML-кода или URL страницы для семантического анализа.
- `web.capture_full_screenshot` — Снять полноразмерный скриншот веб-страницы с автоматической прокруткой для визуального анализа вёрстки через Vision LLM.
- `web.cookie_session_manager` — Управление сессиями, cookie-файлами и авторизационными заголовками для многошаговых сценариев работы агентов (`get`, `set`, `list`, `clear`).

### Компьютерное зрение и рабочие процессы (`vision` / `workflow`)
- `vision.analyze_image` — Проанализировать изображение (фотографию полки, скриншот) с помощью Vision AI.
- `vision.classify_pdf_pages` — Быстрая детерминированная классификация типов страниц PDF без обращения к LLM.
- `vision.parse_pdf_vlm` — Умный парсинг PDF-документа с помощью Vision LLM: распознавание счетов, накладных, чеков, чертежей и таблиц с автоматической проверкой сумм.
- `vision.extract_pdf_structured_vlm` — Интеллектуальное распознавание PDF с помощью VLM: страницы нарезаются на картинки и распознаются в структурированный JSON (с возможностью передать промпт структуры) или Markdown.
- `inventory.audit_shelf` — Провести аудит полки по фото: посчитать фейсинги, долю полки (SOS) и OOS.
- `inventory.check_price_tags` — Проверить наличие ценников на все товары на полке.
- `inventory.calculate_metrics` — Рассчитать долю полки (`SOS %`), заполненность и статистику по брендам.
- `workflow.audit_website` — Запустить комплексный аудит веб-сайта (проверка URL, ссылок, SEO, WCAG) с сохранением отчёта.
- `workflow.create_inventory_report` — Провести ритейл-аудит полки по фото и создать официальный отчёт в Word (`.docx`), Excel (`.xlsx`) или Markdown (`.md`).

</details>

---

## 🚀 Предложения по улучшению и дорожная карта развития (Recommended Improvements & Future Tools)

Для дальнейшего расширения возможностей мультипарадигмальной лаборатории `agents_universe` и проекта `agent_toolkit` рекомендуется поэтапное внедрение следующих специализированных инструментов по четырём ключевым направлениям:

### 1. Веб-инструменты и браузерная автоматизация (Web & Browser Automation AI)
- `web.playwright_session` / `web.puppeteer_action` — Интеграция с безголовыми браузерами (Playwright / Puppeteer) для рендеринга сложного JavaScript, взаимодействия с Single Page Applications (SPA), нажатия кнопок и выполнения интерактивных сценариев авторизации.
- `web.extract_schema_org` — Извлечение микроразметки Schema.org (JSON-LD, Microdata, OpenGraph) из HTML для семантического анализа страниц товаров, новостных статей и корпоративных карточек.
- `web.capture_full_screenshot` — Снятие полноразмерных скриншотов веб-страницы с автоматической прокруткой для визуального анализа сайтов и выявления верстальных артефактов через Vision LLM.
- `web.cookie_session_manager` — Управление сессиями, cookie-файлами и авторизационными заголовками для работы агентов с многошаговыми сценариями входа в личные кабинеты.

### 2. САПР, мультифизика и наукоёмкие расчёты (CAD, Multiphysics & Engineering AI)
- `cad.generate_step_file` — Прямой экспорт моделей и сборок в стандартный обменный формат STEP / IGES для совместимости с SolidWorks, Компас-3D, Inventor и CATIA.
- `cad.simulate_fea_stress` — Упрощённый конечно-элементный анализ (FEA) распределения напряжений и деформаций по 3D-сетке детали под действием внешних механических нагрузок.
- `physics.calc_thermal_dissipation` — Тепловой расчёт радиаторов охлаждения, теплового сопротивления ($R_{th}$, °C/W) и естественной/принудительной конвекции для электронных устройств.
- `physics.calc_gear_strength` — Расчёт контактной прочности и изгиба зубьев цилиндрических и конических шестерен по стандартам ГОСТ / ISO / AGMA.

### 3. Базы данных, PLM и корпоративные системы (Enterprise Data & PLM integrations)
- `db.postgres_execute` / `db.mysql_execute` — Прямые коннекторы к промышленным СУБД PostgreSQL и MySQL с пулом соединений, транзакциями и автоматическим построением ER-диаграмм.
- `data.excel_formula_eval` — Вычисление формул в существующих книгах Excel с проверкой ссылочной целостности и сводных таблиц.
- `erp.post_odata_document` — Создание и проведение документов (счета, расходные накладные, заказы клиентов) в 1С:Предприятие через OData API.
- `tc.create_requirement_baseline` — Создание базовой линии (Baseline / Revision) спецификации требований в Teamcenter PLM и сравнение изменений ревизий.

### 4. Оркестрация, память и безопасность (Orchestration, Memory & Safety)
- `memory.vector_store_hnsw` — Локальный высокопроизводительный векторный индекс (HNSW / FAISS) для семантического поиска по миллионам документов без внешних сервисов.
- `agent.parallel_map_reduce` — Инструмент распределённого выполнения задач (MapReduce), позволяющий главному агенту запустить до 10 субагентов параллельно с объединением отчётов.
- `policy.resource_quota_guard` — Ограничитель расхода токенов, бюджета USD и количества запросов на уровне каждого рабочего процесса для защиты от зацикливания агентов.

---

## Тестирование и самопроверка

В проекте используется автономный тестовый набор, работающий без внешних зависимостей, API-ключей и подключения к интернету:

```bash
# Запустить весь набор тестов (33 модуля, 326 автоматических проверок)
make test

# Провести проверку синтаксиса и импорта
make check

# Запустить бенчмарк поискового движка (151 запрос)
make benchmark
```
