# MAOS — Multi-Agent Ontology System

Независимое приложение, живущее рядом с `agent_system/` в этом
репозитории — но НЕ зависящее от него в рантайме. Общая философия и
часть архитектурных идей позаимствованы осознанно (см. `agent_system/`
для сравнения), код и версионирование — отдельные.

Система про несколько именованных **агентов-личностей** (не один
инструментальный агент), которые:

- хранятся как записи в **обязательной** PostgreSQL + pgvector базе
  (identity, голос, LLM-привязка, системный промпт);
- выбираются под запрос **семантическим роутером** — векторным поиском
  по описаниям, без обращения к LLM для самого решения "кто отвечает";
- пользуются **гибридным LLM-роутингом**: локальная модель по
  умолчанию (дёшево/быстро), облачная — для сложных задач, с
  автоматическим откатом на локальную при сбое облака;
- опираются на **трёхуровневую память**: short-term диалог (с
  экстренной суммаризацией при маленьком окне модели), mid-term
  "кванты памяти" (вопрос-ответ + вектор + метка `provider::model`),
  long-term граф сущностей и связей;
- периодически проходят **фоновое обслуживание** ("Deep Thinking"):
  дистилляция старых диалогов, дедупликация квантов памяти, слияние
  дублей графа.

Никакого "мета-агента", который руководит остальными: детерминированная
цепочка `Agent_A -> Agent_B` задаётся явным списком, а не придумывается
моделью на лету (тот же принцип, что оркестрация в `agent_system`).

## Требования

- Python 3.10+
- PostgreSQL 14+ с расширением `pgvector` — **обязателен**, приложение
  не запустится без `DB_DSN` (см. `maos/config.py`).
- `pip install -r requirements.txt` (`psycopg[binary]`, для тестов ещё
  `pgserver` — embedded Postgres, не нужен для продакшена).

## Быстрый старт

```bash
cd multi_agent_system_ontology
pip install -r requirements.txt

cp .env.example .env
# отредактируйте DB_DSN — реальный PostgreSQL с pgvector, например:
# DB_DSN=postgresql://maos:maos@localhost:5432/maos

export $(grep -v '^#' .env | xargs)   # или используйте python-dotenv
make test       # 274 проверки — нужен pgserver (embedded Postgres для тестов)
make serve      # http://127.0.0.1:8090/dashboard
```

Без реальной установленной PostgreSQL+pgvector тесты, требующие БД,
корректно и явно пропускаются с сообщением о причине (как в
`agent_system/tests/test_pg_ontology.py`) — тесты чистой логики
(конфиг, разбор `provider::model`, роутер, TTS-интерфейс) работают
всегда.

## Формат ссылки на модель: `provider::model`

Везде в системе (конфиг, поле `agent.llm_ref`, метка `provider_model` у
сообщений и квантов памяти) модель указывается строкой вида:

```
local::llama3
openrouter::anthropic/claude-3
openrouter::deepseek/deepseek-chat:free
```

Разделитель — **двойное** двоеточие `::`, а не одиночное: имена моделей
OpenRouter сами часто содержат `:` (например, суффикс `:free`), поэтому
одиночный разделитель был бы неоднозначен. Известные провайдеры:
`local` (llama.cpp/vLLM/LM Studio/Ollama, бесплатно), `openrouter`,
`openai` (плюс алиасы `lmstudio`/`llamacpp`/`vllm`/`ollama` → `local`).

## Гибридный роутинг и fallback (ТЗ п.3)

`maos/orchestrator/hybrid.py` решает, какую модель звать:

1. Если у агента задан `llm_ref` — используется он.
2. Иначе — по длине задачи: короче `complexity_char_threshold` символов
   (по умолчанию 600) идёт в `default_local_model`, длиннее — в
   `default_cloud_model`.
3. При ошибке выбранного провайдера (сеть, лимиты, 5xx) и
   `fallback_to_local=True` (по умолчанию) — автоматический откат на
   `default_local_model`. Ответ всегда несёт метку **реально
   ответившей** модели (`provider_model` в БД) — это и есть
   "авторитетность" записи из ТЗ (Knowledge Labeling).

## Трёхуровневая память (ТЗ п.4)

- **Short-term** — история текущего диалога. Если окно модели меньше
  `small_context_window` (по умолчанию 4096 токенов) либо накопленная
  история сама по себе велика — включается суммаризация: все сообщения,
  кроме последних `short_term_keep_last`, сворачиваются в одну заметку.
- **Mid-term** — таблица `memory_quantum`: пара «вопрос — ответ» +
  `provider_model`, `tokens_used`, `confidence_score`, вектор эмбеддинга.
  Перед каждым вызовом LLM подмешиваются `mid_term_top_k` наиболее
  похожих (по косинусному сходству) квантов, а не вся история.
- **Long-term** — граф `onto_entity`/`onto_relation`: `Agent -> WorkOn
  -> Project` и подобное, с семантическим поиском по эмбеддингам.

## Модель эмбеддингов на внешнем сервере (например, LM Studio)

Эмбеддинги (для mid-term памяти и семантического роутера) можно считать
СОВЕРШЕННО ОТДЕЛЬНЫМ сервером от диалоговой LLM — частый сценарий:
чат-модель в облаке, а эмбеддинги — локально в LM Studio на своей
машине (дешевле и не тратит контекст облачного провайдера на векторизацию).

Настраивается через `Config`/переменные окружения:

```bash
MAOS_EMBEDDING_PROVIDER=lmstudio   # синоним "local" — тот же протокол /v1/embeddings
MAOS_EMBEDDING_MODEL=text-embedding-nomic-embed-text-v1.5
MAOS_EMBEDDING_BASE_URL=http://192.168.1.50:1234/v1
MAOS_EMBEDDING_API_KEY=            # обычно не нужен для локального LM Studio
MAOS_EMBEDDING_DIM=768             # размерность вектора модели — фиксирует
                                    # схему БД (vector(768)), см. ниже
MAOS_EMBEDDING_TIMEOUT=60          # секунд; слабое железо считает эмбеддинги
                                    # дольше, чем облачный API
```

`embedding_base_url`/`embedding_api_key` в `Config` — секреты и адрес
приватной сети, поэтому доступны ТОЛЬКО через переменные окружения (как
все ключи в системе, `Config.load()` отбрасывает эти поля, если они
случайно оказались в JSON-конфиге) и маскируются в `Config.to_dict()`.
Если `MAOS_EMBEDDING_BASE_URL` не задан, провайдеры `local`/`lmstudio`/
`llamacpp`/`vllm`/`ollama` используют общий `LOCAL_BASE_URL` (тот же,
что для диалоговой модели) — раздельный адрес нужен только когда
эмбеддинги реально живут на другом сервере.

**Важно про `MAOS_EMBEDDING_DIM`**: колонка `vector(dim)` в схеме
PostgreSQL фиксируется при первом подключении `Store`. Смена модели
эмбеддингов на другую размерность требует новой базы/схемы (ограничение
pgvector, не наше) — задайте `MAOS_EMBEDDING_DIM` под конкретную модель
ДО первого запуска (у `nomic-embed-text` — 768, у `hash` по умолчанию — 256).

## Semantic Router (ТЗ п.5)

`maos/orchestrator/router.py` выбирает агента ПО ВЕКТОРНОМУ СХОДСТВУ
описаний (`agent.description_embedding`), без обращения к LLM.
Keyword-фолбэк подстраховывает случай, когда у агента ещё нет
эмбеддинга (только что создан).

Детерминированная ручная цепочка `Agent_A -> Agent_B` — `POST
/v1/chain/start` с явным списком slug'ов, см. `maos/orchestrator/chain.py`.

## Фоновое обслуживание (ТЗ п.6)

`maos/maintenance/service.py`, запускается вручную (`POST
/v1/maintenance/run`) или циклически (`MaintenanceService.run_forever`,
кооперативная остановка через `threading.Event`):

- `distill()` — длинные диалоги → компактный квант памяти;
- `dedup()` — удаление дублей `memory_quantum` по высокому косинусному
  сходству эмбеддингов;
- `synthesize_graph()` — слияние похожих сущностей графа одного `kind`.

## HTTP API (ТЗ п.7)

```
GET  /health                  — жив ли сервис (без токена)
GET  /dashboard, /            — веб-интерфейс (Admin/Chat/Graph)
POST /v1/chat                 — {"message","conversation_id"?,"agent_slug"?}
GET  /v1/agents               — список агентов
POST /v1/agents                — создать агента
GET  /v1/agents/<slug>         — детали агента
POST /v1/agents/<slug>         — обновить агента (частично)
POST /v1/agents/<slug>/delete  — удалить агента
GET  /v1/conversations         — список диалогов
GET  /v1/conversations/<id>    — сообщения диалога
GET  /v1/memory/stats          — статистика БД/токенов
GET  /v1/graph                 — граф онтологии (для визуализации)
POST /v1/chain/start           — {"goal","agents":[slug,...]}
GET  /v1/chain/<id>            — статус+шаги цепочки
GET  /v1/chains                — история цепочек
POST /v1/maintenance/run       — один цикл фонового обслуживания вручную
```

Токен: если задан `MAOS_API_TOKEN`, все маршруты кроме `/health` и
`/dashboard` требуют `Authorization: Bearer <token>`. Сервер отказывается
слушать не-localhost без токена.

## TTS (ТЗ п.8) — только интерфейс в этой сборке

`maos/tts/provider.py` даёт конфигурацию (`agent.voice_provider`,
`agent.voice_id`, переменная `TTS_PROVIDER`) и фабрику
`build_tts_provider(...)`, но **реальная генерация звука через внешние
API (OpenAI TTS/ElevenLabs/Piper) не реализована** — осознанный
технический долг, см. docstring модуля. `synthesize()` кидает понятную
ошибку вместо тихой заглушки.

## Структура проекта

```
maos/
  config.py            — Config: DB_DSN обязателен, provider::model, память
  memory/store.py       — PostgreSQL+pgvector: agent/conversation/message/
                          memory_quantum/onto_entity/onto_relation/chain_*
  llm/                  — base-протокол, OpenAI-совместимый драйвер,
                          реестр provider::model, эмбеддинги (hash/openai)
  orchestrator/
    hybrid.py            — гибридный роутинг + fallback
    context.py           — short/mid-term контроль контекста
    router.py            — semantic router (векторный, без LLM)
    service.py           — Orchestrator: полный цикл /v1/chat
    chain.py             — ChainRunner: детерминированная цепочка
  agents/runtime.py      — AgentRuntime: один ход диалога от лица агента
  maintenance/           — distill/dedup/synthesize_graph, run_forever
  maintenance_runner.py  — CLI: python3 -m maos.maintenance_runner [--once]
  tts/provider.py        — интерфейс TTS (без реализации звука)
  api/server.py          — HTTP API на stdlib (без внешних зависимостей)
  web/dashboard.html      — Admin Panel + Chat UI + Graph Visualization
tests/                   — 274 проверки, реальный embedded Postgres+pgvector
                          (pgserver) и реальные HTTP-серверы, без моков
```

## Тесты

```bash
make test   # 274 проверки, ~10 секунд
```

Философия тестов — как в `agent_system`: реальная инфраструктура
(embedded Postgres через `pgserver`, реальные сокеты через
`ThreadingHTTPServer`), обязательные негативные сценарии, отсутствующие
опциональные зависимости (`psycopg`/`pgserver`) приводят к graceful
skip конкретного модуля, а не к падению всего набора.
