# DataForge — платформа интеграции данных, качества, MDM, Ontology и lineage

Ядро платформы интеграции корпоративных данных из большого ТЗ на
DataForge: подключение источников (Connect Hub), профилирование и
контроль качества данных (Quality Engine), сборка «золотой записи» из
нескольких источников (MDM/матчинг), бизнес-язык поверх golden record
с типизированными объектами/связями/действиями (Ontology),
прослеживаемость происхождения данных (lineage), поверх которых —
REST API и веб-дашборд.

Независимое приложение, живущее рядом с `agent_system/`,
`multi_agent_system_ontology/` и `erp_ai/` в этом репозитории — не
зависит от них в рантайме. Общий стиль (докстрины "почему", тесты на
реальной инфраструктуре, секреты только в окружении, `Config._SECRET_FIELDS`,
неизменяемый `audit_log`) позаимствован осознанно.

## Честная граница объёма — что реализовано, а что нет

Полное ТЗ описывает промышленную платформу на Kubernetes с Dagster,
Temporal, Debezium+Redpanda, Splink, OpenMetadata, Keycloak, React+TS,
семантической онтологией и оркестрацией бизнес-процессов — продукт на
многие месяцы разработки командой. Вместо имитации широты через
заглушки выбран противоположный подход: реализовать **три несущих
модуля платформы полностью и по-настоящему** (Connect Hub + Quality
Engine + MDM/матчинг, со сквозной прослеживаемостью), на реальной
инфраструктуре, с честными тестами — и явно не реализовывать то, что
не влезло в объём одной сессии.

### Реализовано (по-настоящему, с тестами на реальной инфраструктуре)

- **Connect Hub** (`dataforge/connectors/`) — единый протокол
  `Connector` (`discover/read_full/read_changes/write_back`, см. ТЗ
  §5.1) с тремя РЕАЛЬНЫМИ реализациями:
  - `FileConnector` — CSV/XLSX/JSON/XML, автоматическое профилирование
    схемы по выборке записей (batch-режим, источник только для чтения);
  - `SqlConnector` — PostgreSQL и SQLite через общий код на DB-API 2.0
    (PEP 249): `discover` через `information_schema`/`PRAGMA
    table_info`, инкрементальное чтение по монотонному курсору,
    `write_back` через UPDATE/INSERT. ЧЕСТНО: в песочнице этой сессии
    нет реальных MySQL/MSSQL/Oracle серверов (и Docker для их подъёма)
    — вместо тестирования этих диалектов моками выбраны два РЕАЛЬНЫХ,
    но доступных здесь движка (PostgreSQL и SQLite); добавление
    MySQL/MSSQL/Oracle — это регистрация ещё одного диалекта в
    `SqlConnector._detect_dialect`/`_connect`, без переписывания ядра;
  - `OneCODataConnector` — 1С:Предприятие через стандартный OData:
    `discover()` парсит EDMX `$metadata`, `read_full()` — пагинация
    `$top/$skip`, `read_changes()` — план обмена `SelectChanges` с
    параметрами `DataExchangePoint`/`MessageNo` (взяты из официальной
    документации протокола, не придуманы) + `NotifyChangesReceived`
    для подтверждения курсора, fallback `filter_by_date`, `write_back()`
    — `PATCH` с `If-Match`/`DataVersion` (оптимистичная блокировка,
    обработка HTTP 412 как построчной ошибки, не исключения), Basic/
    Bearer аутентификация. ЧЕСТНО: тестируется на локальном
    fake-HTTP-сервере, эмулирующем именно эти документированные
    эндпоинты — реального сервера 1С:Предприятия в этой сборке нет.
  - `dataforge/connectors/factory.py` — сборка коннектора из записи
    `source` в БД; секреты (DSN источника, ключ 1С) НИКОГДА не хранятся
    в JSONB-конфиге источника — только имя переменной окружения
    (`config.dsn_env`) или общая конфигурация приложения.
- **Ingest-пайплайн** (`dataforge/pipeline/ingest.py`) —
  `ingest_full`/`ingest_changes`: Connector → Bronze (append-only,
  история выгрузок сохраняется) + автоматическая запись lineage-ребра
  `source:... -> bronze:dataset:...`; `promote_quality` — Bronze →
  Silver/карантин через Quality Engine + lineage-рёбра
  `bronze:record:... -> silver:record:...` на КАЖДУЮ продвинутую
  запись.
- **Quality Engine** (`dataforge/quality/engine.py`, ТЗ K2) —
  `profile_dataset()`: автоматическая статистика по полям
  (total/null/distinct/min/max/примеры). `run_quality_checks()`:
  декларативные правила без кода (`not_null`, `unique`, `regex`,
  `range`, `allowed_values`), severity `error`/`warning` — нарушение
  `error` уводит запись в карантин вместо Silver, `warning` фиксируется
  в отчёте, но не блокирует. Без Great Expectations (решение
  пользователя "минимум зависимостей"), но семантика правил намеренно
  похожа на GX-expectations для прямолинейной миграции при росте
  объёма.
- **MDM / матчинг** (`dataforge/mdm/matching.py`, ТЗ K1, §3.3) —
  `find_match_candidates()`: попарное сравнение Silver-записей через
  `rapidfuzz` (нечёткие строки, регистронезависимо) + точное сравнение
  чисел, взвешенные поля; `apply_survivorship()`: приоритет источников
  на уровне ОТДЕЛЬНОГО ПОЛЯ (`survivorship_rule`), с детерминированным
  fallback на первое непустое значение; `merge_candidate()`: сборка
  «золотой записи» с привязкой всех исходных Silver-записей и
  lineage-рёбер; `auto_merge_high_confidence()`: guardrail — численный
  порог (`match_auto_threshold`), НЕ "на глаз", ниже порога кандидат
  остаётся в stewardship-очереди для человека; `reject_candidate()`.
- **Lineage** (`dataforge/db/store.py`: `lineage_edge`,
  `trace_lineage()`, ТЗ K4) — граф рёбер `from_asset -> to_asset`,
  обратный обход от любого актива (например `gold:entity:5`) до
  исходного источника через рекурсивный BFS; каждый шаг пайплайна
  (`ingest_full`, `promote_quality`, `merge_candidate`) САМ пишет свои
  рёбра — никакого централизованного "оркестратора lineage".
- **Неизменяемый аудит** (`audit_log`) — только `INSERT` в коде
  `Store`, как в остальных проектах репозитория.
- **HTTP API** на FastAPI (`dataforge/api/server.py`) — REST,
  токен-защита, доменные ошибки как 400/404/502/503 (не 500).
- **Веб-дашборд** (`dataforge/web/dashboard.html`) — вкладки: обзор,
  источники (регистрация, discover, ingest), каталог данных
  (Bronze/Silver/Gold с числом строк), качество (профилирование,
  прогон проверок, карантин с кнопкой запуска процесса коррекции),
  процессы (Process Orchestrator), AI Copilot (диалог, история), MDM
  (поиск дублей, stewardship-очередь, слияние/отклонение), золотые
  записи (с кнопкой материализации в Ontology), Ontology (типы
  объектов, actions, экземпляры, карточка объекта со связями),
  построение цепочки lineage, журнал аудита.
- **Ontology / семантическая модель** (`dataforge/ontology/`, ТЗ §3.2,
  K1) — бизнес-язык поверх Gold-слоя:
  - `model.py`: `define_object_type()` — регистрация типа бизнес-
    объекта ("Контрагент", "Деталь") с декларативной схемой атрибутов
    (`{"name","type","required"}`, типы `string`/`number`/`boolean`);
    `validate_attributes()` — проверка атрибутов против схемы, не
    бросает исключение сама (вызывающий код решает, блокировать или
    только предупредить); `materialize_from_gold()` — создаёт или
    обновляет `ObjectInstance` из golden record по привязке
    `gold_entity_type`, с опциональным `strict=True` (guardrail —
    отказ материализовать данные, нарушающие обязательные поля схемы);
    `link_instances()` — типизированная связь между двумя объектами с
    проверкой существования обеих сторон; `instance_neighborhood()` —
    "карточка объекта" (сам объект + исходящие/входящие связи +
    исходные Silver-записи, слившиеся в golden record).
  - `actions.py`: реестр КОНКРЕТНЫХ обработчиков действий (не
    универсальный движок правил/скриптов — сознательно узко и
    безопасно), `execute_action()` — единая точка выполнения с
    обязательным audit trail НА ЛЮБОЙ исход (включая неудачные
    попытки — видно не только что сделано, но и что пытались сделать
    и не получилось). Встроены `correct_attribute` (ТЗ пример
    "скорректировать остаток" — обязательное `reason`, тот же принцип
    explainability, что у `ProcurementAgent` в `erp_ai/`) и `link_to`.

- **Process Orchestrator** (`dataforge/pipeline/orchestrator.py`, ТЗ
  K3) — единственный реализованный сквозной процесс
  `quarantine_correction`, демонстрирующий ВЕСЬ обязательный паттерн
  ТЗ §3.7 на одном конкретном примере (по аналогии с `ProcurementAgent`
  в `erp_ai/`):
  1. `start_quarantine_correction()` — запись данных нарушила правило
     качества и попала в карантин → создаётся `process_instance` +
     `task` ответственному (stewardship); идемпотентно относительно
     повторного запуска на ТОЙ ЖЕ записи карантина.
  2. `submit_correction()` — исполнитель присылает исправленный
     payload; **GUARDRAIL**: он ПОВТОРНО проверяется теми же правилами
     качества, что отправили запись в карантин
     (`quality.engine.evaluate_payload`) — если нарушение осталось,
     процесс явно остаётся в 'awaiting_task' со списком нарушений, а
     НЕ продолжается молча. Только валидное исправление обновляет
     Bronze-запись (единственное место всего приложения, где Bronze не
     append-only — обосновано тем, что это исправление ошибки
     источника) и разрешает карантин.
  3. `write_back_correction()` — пишет исправление обратно в источник
     через `Connector.write_back()` (тот же интерфейс, что и у ingest),
     с идемпотентностью через `write_back_log` (тот же принцип, что
     `onec_log_attempt` в `erp_ai`) — повторный вызов не отправляет
     запись в источник дважды.
  4. `rollback_process()` — отмена ДО успешного write-back; ПОСЛЕ
     успешного write-back откат средствами платформы заблокирован
     (источник уже изменён) — то же ограничение, что у
     `ProcurementAgent.rollback_proposal` для отправленных в 1С заказов.

  Полная история — `audit_trail_for("process_instance", id)`, включая
  НЕУДАЧНЫЕ попытки исправления (guardrail-отклонения), не только успех.
- **AI Copilot** (`dataforge/copilot/`, ТЗ §3.6, K6) — работает ЧЕРЕЗ
  инструменты (function calling) над ПУБЛИЧНЫМИ REST API DataForge, а
  НЕ имеет прямого доступа к `Store`/БД (`ApiTools` принимает
  `httpx.Client`, не объект БД — структурно проверено тестом):
  - `llm.py`: минимальный клиент OpenAI-совместимого чат-протокола с
    tool-calling (тот же протокол, что покрывает и облачные провайдеры,
    и локальные LM Studio/Ollama/vLLM — по аналогии с
    `maos/llm/openai_like.py` в этом репозитории, но без реестра
    провайдеров, который был бы избыточен для одного протокола).
  - `tools.py`: узкий набор из 8 инструментов (статистика платформы,
    список источников/датасетов, карантин, запуск процесса коррекции,
    кандидаты MDM, lineage, список процессов) — каждый соответствует
    ОДНОМУ одобренному REST-вызову, не "дай мне произвольный запрос".
  - `assistant.py`: `ask()` — цикл модель → инструмент → модель (до 6
    шагов), пишет КАЖДОЕ взаимодействие в неизменяемый `ai_interaction`
    (промпт, вызванные инструменты, финальный ответ). Без
    `FORGE_LLM_BASE_URL` бросает `CopilotError` — модуль полностью
    отключаем без влияния на ядро (остальной API работает независимо).
  - **Важный технический момент, найденный и исправленный при
    тестировании**: `ApiTools` делает HTTP-запросы ОБРАТНО к тому же
    серверу DataForge (за инструментами). Если выполнить это в `async`
    FastAPI-роуте напрямую, единственный event loop uvicorn
    блокируется сам на себя — вложенный запрос никогда не будет
    обработан (deadlock, воспроизведён и пойман тестом). Исправлено
    через `fastapi.concurrency.run_in_threadpool` — блокирующий вызов
    уходит в отдельный поток, event loop остаётся свободным.

### НЕ реализовано (осознанно, не притворяемся, что готово)

- **Real-time мониторинг производства** (K5) — единая панель сквозного
  статуса заказов/операций — не реализована, платформа работает с
  универсальными Bronze/Silver/Gold, а не с производственной моделью.
- **CDC через Debezium/Redpanda** — инкрементальное чтение реализовано
  через универсальный "курсор по монотонному полю" (SQL) и план обмена
  (1С), НЕ через захват изменений на уровне транзакционного лога БД
  (WAL/binlog) — для этого понадобился бы реальный Debezium+Kafka
  кластер, что вне выбранного стека "минимум зависимостей".
- **Dagster/Temporal** — пайплайн реализован как простой
  детерминированный Python-код (`dataforge/pipeline/ingest.py`), без
  DAG-оркестратора и без сохранения состояния долгоживущих процессов;
  для объёма этой сессии (несколько последовательных шагов без
  ветвления) полноценный оркестратор был бы преждевременной сложностью.
- **OpenMetadata/полноценный каталог с glossary** — каталог реализован
  как таблицы `source`/`dataset`/`data_profile` в PostgreSQL, без
  отдельного сервиса каталога, поиска по глоссарию, тегирования.
- **Splink** — вероятностный матчинг реализован на `rapidfuzz`
  (взвешенное сравнение полей), без байесовской модели вероятности
  совпадения (EM-алгоритм, blocking rules) — для объёма нескольких
  сотен записей на демонстрационных данных этого достаточно, для
  промышленного объёма (тысячи-миллионы записей) потребовался бы
  блокинг и более строгая вероятностная модель.
- **Keycloak/RBAC/ABAC** — нет ролей/атрибутивного контроля доступа,
  только единый токен на весь API (как и в `erp_ai/`).
- **React+TypeScript фронтенд** — дашборд на vanilla JS без сборки, как
  в остальных проектах этого репозитория.
- **Object storage (MinIO)/настоящий Lakehouse (Iceberg/DuckDB)** —
  Bronze/Silver хранятся как JSONB-записи в PostgreSQL, а не как
  файлы в колоночном формате на объектном хранилище — для объёма
  демонстрации (сотни-тысячи записей) этого достаточно; для
  промышленных объёмов (миллионы строк) потребовалась бы миграция на
  настоящий Lakehouse.
- **Kubernetes/Terraform/Prometheus/Langfuse** — не нужны для объёма,
  который реально реализован; добавлены бы карго-культом.
- **`pull_counterparties`-подобная асимметрия мастер-систем**: как и в
  `erp_ai/`, если понадобится развести правила мастер-данных по
  конкретным справочникам 1С отдельно от общего MDM-модуля — это
  предмет отдельной доработки, здесь единая MDM-логика работает
  универсально по `entity_type`, без специфики конкретного справочника
  1С.

## Требования

- Python 3.10+
- PostgreSQL 14+ (без `pgvector` — не нужен для этого объёма)
- `pip install -r requirements.txt`
  (FastAPI/uvicorn/psycopg/rapidfuzz/openpyxl/httpx — httpx нужен не
  только тестам, но и AI Copilot для вызова инструментов через API)
- Опционально: OpenAI-совместимый LLM-сервер (облачный или локальный
  LM Studio/Ollama/vLLM) для AI Copilot — без него `/v1/copilot/*`
  отвечает 503, остальная платформа работает как обычно
- Для тестов: `pip install -r requirements-dev.txt` (`pgserver` —
  embedded PostgreSQL)

## Быстрый старт

```bash
cd data_forge
pip install -r requirements.txt

cp .env.example .env
# отредактируйте DB_DSN — реальный PostgreSQL, например:
# DB_DSN=postgresql://forge:forge@localhost:5432/dataforge

export $(grep -v '^#' .env | xargs)
make serve      # http://127.0.0.1:8200/dashboard
```

Для тестов (нужен `pgserver`, БД поднимается автоматически во время
тестового прогона, реальная PostgreSQL не требуется):

```bash
pip install -r requirements-dev.txt
make test       # 503 проверки, ~50 секунд
```

## Сценарий — пошагово (K1 + K2 + K4: качество данных → golden record → lineage)

```bash
# 1. Зарегистрировать источник (CSV-файл с дублирующимися контрагентами)
curl -X POST localhost:8200/v1/sources \
  -d '{"name":"crm","kind":"file","config":{"path":"/data/customers.csv"}}'

# 2. Обнаружить схему источника
curl -X POST localhost:8200/v1/sources/1/discover

# 3. Выгрузить в Bronze
curl -X POST localhost:8200/v1/sources/1/ingest/full -d '{"dataset":"customers.csv"}'

# 4. Задать правило качества и прогнать проверку (Bronze -> Silver/карантин)
curl -X POST localhost:8200/v1/datasets/1/quality-rules \
  -d '{"rule_type":"not_null","field_name":"name","severity":"error"}'
curl -X POST localhost:8200/v1/datasets/1/quality-run

# 5. Найти дубли среди Silver-записей (вероятностный матчинг)
curl -X POST localhost:8200/v1/mdm/match \
  -d '{"entity_type":"counterparty","dataset_id":1,"fields":["name","inn"]}'

# 6. Человек подтверждает слияние -> собирается golden record
curl -X POST localhost:8200/v1/mdm/candidates/1/merge -d '{"decided_by":"human:ivanov"}'

# 7. Проследить полную цепочку происхождения золотой записи
curl "localhost:8200/v1/lineage/trace?asset=gold:entity:1"
```

## Сценарий — Ontology (бизнес-язык поверх golden record)

```bash
# 1. Определить тип бизнес-объекта с привязкой к Gold и схемой атрибутов
curl -X POST localhost:8200/v1/ontology/types \
  -d '{"name":"Контрагент","gold_entity_type":"counterparty",
       "attributes_schema":[{"name":"inn","type":"string","required":true}]}'

# 2. Материализовать объект из уже собранной golden record
curl -X POST localhost:8200/v1/ontology/materialize -d '{"gold_entity_id":1}'

# 3. Определить действие ("скорректировать атрибут" из ТЗ §3.7) и выполнить его
curl -X POST localhost:8200/v1/ontology/types/1/actions \
  -d '{"name":"correct_attribute","handler":"ontology.actions.correct_attribute"}'
curl -X POST localhost:8200/v1/ontology/instances/1/actions \
  -d '{"action":"correct_attribute","actor":"human:ivanov",
       "params":{"field":"inn","value":"7701234567","reason":"исправлена опечатка"}}'
# -> без params.reason запрос будет отклонён (400) — explainability обязательна

# 4. Связать два объекта и посмотреть карточку объекта
curl -X POST localhost:8200/v1/ontology/links \
  -d '{"link_type":"поставляет","from_instance_id":1,"to_instance_id":2}'
curl localhost:8200/v1/ontology/instances/1
```

## Сценарий — Process Orchestrator (K3: карантин -> задача -> корректировка -> write-back)

```bash
# 1. Запись ушла в карантин (см. сценарий выше, шаг 4) -> запустить процесс
curl -X POST localhost:8200/v1/processes/quarantine-correction \
  -d '{"quarantine_id":1,"assignee":"human:ivanov"}'

# 2. Исполнитель подаёт исправление — guardrail: если оно снова нарушает
#    правило качества, запрос будет отклонён (accepted: false) БЕЗ изменения Bronze
curl -X POST localhost:8200/v1/processes/1/correct \
  -d '{"corrected_payload":{"name":"ООО Ромашка","inn":"1234567890"},"actor":"human:ivanov"}'

# 3. Записать исправление обратно в источник (идемпотентно)
curl -X POST localhost:8200/v1/processes/1/write-back \
  -d '{"dataset_name":"customers","natural_key":"c1","actor":"human:ivanov"}'

# 4. Посмотреть полную историю процесса (включая отклонённые попытки)
curl localhost:8200/v1/processes/1
```

## Сценарий — AI Copilot (ТЗ §3.6)

```bash
# Требует настроенный LLM: FORGE_LLM_BASE_URL (OpenAI-совместимый сервер,
# например LM Studio/Ollama/vLLM в режиме /v1, или облачный провайдер)
curl -X POST localhost:8200/v1/copilot/ask \
  -d '{"prompt":"покажи статистику платформы и есть ли открытые процессы",
       "mode":"ops","actor":"human:ivanov"}'
# -> Copilot сам вызовет нужные REST-инструменты (get_dashboard_stats,
#    list_processes) через ТОТ ЖЕ API, что доступен человеку, и ответит текстом

curl localhost:8200/v1/copilot/history   # аудит всех взаимодействий
```

## HTTP API

```
GET  /health                          — жив ли сервис (без токена)
GET  /dashboard, /                    — веб-интерфейс

GET  /v1/sources                      — список источников
POST /v1/sources                      — зарегистрировать источник
POST /v1/sources/{id}/discover        — профилирование схемы источника
POST /v1/sources/{id}/ingest/full     — полная выгрузка в Bronze
POST /v1/sources/{id}/ingest/changes  — инкрементальная выгрузка

GET  /v1/datasets                     — список датасетов
GET  /v1/datasets/{id}                — детали + профиль полей
POST /v1/datasets/{id}/profile        — профилирование Bronze
GET  /v1/datasets/{id}/bronze         — сырые записи
GET  /v1/datasets/{id}/silver         — очищенные записи

GET  /v1/datasets/{id}/quality-rules  — правила качества
POST /v1/datasets/{id}/quality-rules  — создать правило
POST /v1/datasets/{id}/quality-run    — прогнать проверки
GET  /v1/datasets/{id}/quarantine     — карантин датасета
POST /v1/quarantine/{id}/resolve      — отметить решённым

POST /v1/mdm/match                    — найти кандидатов на дубли
GET  /v1/mdm/candidates               — очередь stewardship
POST /v1/mdm/candidates/{id}/merge    — подтвердить слияние -> golden record
POST /v1/mdm/candidates/{id}/reject   — отклонить кандидата
POST /v1/mdm/auto-merge               — авто-слияние выше порога (guardrail)
POST /v1/mdm/survivorship             — задать приоритет источников для поля

GET  /v1/gold                         — золотые записи
GET  /v1/gold/{id}                    — детали + связанные исходные записи

GET  /v1/ontology/types               — типы бизнес-объектов
POST /v1/ontology/types               — определить тип объекта
GET  /v1/ontology/types/{id}          — детали + определённые actions
POST /v1/ontology/types/{id}/actions  — определить действие для типа
POST /v1/ontology/materialize         — материализовать объект из golden record
GET  /v1/ontology/instances           — список экземпляров объектов
GET  /v1/ontology/instances/{id}      — карточка объекта: связи + источники
POST /v1/ontology/links               — связать два объекта
POST /v1/ontology/instances/{id}/actions — выполнить действие над объектом

POST /v1/processes/quarantine-correction — запустить процесс коррекции (K3)
GET  /v1/processes                    — список запущенных процессов
GET  /v1/processes/{id}               — детали + задачи + write-back лог
POST /v1/processes/{id}/correct       — подать исправленный payload (guardrail)
POST /v1/processes/{id}/write-back    — записать исправление в источник
POST /v1/processes/{id}/rollback      — отменить процесс (до write-back)

POST /v1/copilot/ask                  — спросить AI Copilot (ТЗ §3.6, K6)
GET  /v1/copilot/history              — история взаимодействий (аудит AI)

GET  /v1/lineage/trace                — цепочка lineage по asset

GET  /v1/audit                        — журнал аудита (неизменяемый)
GET  /v1/audit/{entity_type}/{entity_id}
GET  /v1/dashboard/stats
```

Токен: если задан `FORGE_API_TOKEN`, все маршруты кроме `/health` и
`/dashboard` требуют заголовок `Authorization: Bearer <token>`.

## Архитектура кода

```
dataforge/
  config.py            — Config: DB_DSN обязателен, MDM-пороги, секреты
  server.py             — python3 -m dataforge.server: uvicorn.run
  db/
    store.py             — PostgreSQL Store: 24 таблицы (Bronze/Silver/
                            Gold, качество, MDM, lineage, Ontology,
                            Process Orchestrator, AI Copilot, аудит)
  connectors/
    base.py               — протокол Connector, DatasetSchema, Cursor
    files.py              — CSV/XLSX/JSON/XML (read-only)
    sql.py                — PostgreSQL/SQLite через общий DB-API код
    onec_odata.py          — 1С через стандартный OData
    factory.py             — сборка коннектора из записи source
  quality/
    engine.py              — profile_dataset, run_quality_checks
  mdm/
    matching.py             — compare_records, find_match_candidates,
                              apply_survivorship, merge_candidate,
                              auto_merge_high_confidence
  ontology/
    model.py                — define_object_type, validate_attributes,
                              materialize_from_gold, link_instances,
                              instance_neighborhood
    actions.py               — реестр обработчиков, execute_action,
                              correct_attribute, link_to
  pipeline/
    ingest.py               — ingest_full, ingest_changes, promote_quality
    orchestrator.py           — Process Orchestrator (K3):
                              start_quarantine_correction, submit_correction,
                              write_back_correction, rollback_process
  copilot/
    llm.py                    — минимальный OpenAI-совместимый клиент
                              с tool-calling
    tools.py                   — ApiTools: инструменты как HTTP-вызовы
                              к публичному REST API DataForge
    assistant.py                — ask(): цикл модель -> инструмент -> модель,
                              audit trail в ai_interaction
  api/
    server.py               — FastAPI, все REST-маршруты
  web/
    dashboard.html           — веб-интерфейс на vanilla JS
tests/
  test_config.py                 (18 проверок)
  test_store.py                  (146 проверок)
  test_quality_engine.py         (25 проверок)
  test_mdm_matching.py           (34 проверки)
  test_ontology_model.py         (35 проверок)
  test_ontology_actions.py       (21 проверка)
  test_process_orchestrator.py   (38 проверок)
  test_copilot.py                (17 проверок)
  test_connector_files.py        (20 проверок)
  test_connector_sql.py          (19 проверок)
  test_connector_onec_odata.py   (20 проверок)
  test_connector_factory.py      (12 проверок)
  test_pipeline.py               (20 проверок)
  test_api.py                    (78 проверок)
```

**Итого 503 проверки**, все зелёные, `pyflakes` чист по `dataforge/` и
`tests/`.
