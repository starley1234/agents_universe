# MAOS — Multi-Agent Ontology System

Независимое приложение, живущее рядом с `agent_system/` в этом
репозитории — но НЕ зависящее от него в рантайме. Общая философия и
часть архитектурных идей позаимствованы осознанно (см. `agent_system/`
для сравнения), код и версионирование — отдельные.

Система про несколько именованных **агентов-личностей**, которые:

- хранятся как записи в **обязательной** PostgreSQL + pgvector базе
  (identity, голос, LLM-привязка, системный промпт, набор навыков);
- выбираются под запрос **семантическим роутером** — векторным поиском
  по описаниям, без обращения к LLM для самого решения "кто отвечает";
- пользуются **гибридным LLM-роутингом**: локальная модель по
  умолчанию (дёшево/быстро), облачная — для сложных задач, с
  автоматическим откатом на локальную при сбое облака;
- опираются на **трёхуровневую память**: short-term диалог (с
  экстренной суммаризацией при маленьком окне модели), mid-term
  "кванты памяти" (вопрос-ответ + вектор + метка `provider::model`),
  long-term граф сущностей и связей;
- ОПЦИОНАЛЬНО умеют вызывать **инструменты** (`files`, `web`, `office`,
  `rag`, `mcp`, `messaging`, `site_qa` для проверки HTML-сайтов и `vision` для
  анализа изображений) — циклом «модель -> инструмент -> модель»; агент
  без назначенных навыков остаётся чистым синтезатором ответа;
- умеют **говорить** — реальный TTS-клиент OmniVoice для голосового
  ответа личности;
- периодически проходят **фоновое обслуживание** ("Deep Thinking"):
  дистилляция старых диалогов, дедупликация квантов памяти, слияние
  дублей графа и **автоэкстракция сущностей/связей** из диалогов в long-term
  онтологию (`extract_graph`).

Никакого "мета-агента", который руководит остальными: детерминированная
цепочка `Agent_A -> Agent_B` задаётся явным списком, а не придумывается
моделью на лету (тот же принцип, что оркестрация в `agent_system`).

## Требования

- Python 3.10+
- PostgreSQL 14+ с расширением `pgvector` — **обязателен** для обычного
  запуска (`make serve`), приложение не стартует без `DB_DSN` (см.
  `maos/config.py`). Для режима быстрого старта (`make quickstart`)
  устанавливать PostgreSQL вручную НЕ нужно — см. ниже.
- `pip install -r requirements.txt` (`psycopg[binary]`, `pgserver` —
  embedded PostgreSQL, нужен и для тестов, и для `make quickstart`).
- `pip install -r requirements-optional.txt` — только если агенту нужен
  навык `office` (создание Word/Excel/PowerPoint). Остальные навыки
  (`files`/`web`/`rag`) работают на голой stdlib.

## Режим быстрого старта — одна команда, без установки PostgreSQL

> **Подробное руководство:** см. **[QUICKSTART.md](QUICKSTART.md)** — пошаговые сценарии запуска, работа с дашбордом, проверка Graph-RAG (онтологический граф в чате), запуск фонового обслуживания Deep Thinking и FAQ.

Самый быстрый способ познакомиться с MAOS:

```bash
cd multi_agent_system_ontology
pip install -r requirements.txt
make quickstart
```

Одна команда: поднимает embedded PostgreSQL+pgvector (через `pgserver`,
кластер лежит в `.maos_quickstart_pgdata/` рядом), создаёт схему, сеет
трёх демо-агентов (`coder`/`writer`/`analyst` — с готовыми промптами и
описаниями для роутера) и запускает сервер — сразу открывайте
**http://127.0.0.1:8090/dashboard**. Дашборд сам покажет приветственный
баннер, если база пуста, и предложит создать демо-агентов кнопкой (тот
же посев, что и `make quickstart`, доступен и без него через
`POST /v1/onboarding/seed` — полезно на уже существующей "боевой" базе,
если хочется быстро увидеть, как всё работает).

Это режим **для знакомства и локальной разработки**, а не для
продакшена: embedded-кластер живёт в подпапке рабочей директории без
репликации и отдельного бэкапа. Для продакшена — обычный `make serve` с
настоящим `DB_DSN` (см. ниже); архитектурное требование "PostgreSQL+
pgvector обязателен" при этом не меняется — quickstart просто поднимает
такой же Postgres внутри того же процесса.

```bash
make quickstart --no-seed        # без демо-агентов, только пустая база
python3 -m maos.quickstart --port 9000 --pgdata /tmp/my_pgdata
```

## Обычный запуск (с собственным PostgreSQL)

```bash
cd multi_agent_system_ontology
pip install -r requirements.txt

cp .env.example .env
# отредактируйте DB_DSN — реальный PostgreSQL с pgvector, например:
# DB_DSN=postgresql://maos:maos@localhost:5432/maos

export $(grep -v '^#' .env | xargs)   # или используйте python-dotenv
make test       # 437 проверок — нужен pgserver (embedded Postgres для тестов)
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
- **Long-term (Multi-Hop Graph-RAG)** — онтологический граф `onto_entity`/
  `onto_relation` (сущности и их связи вида `Agent -> works_on -> Project`).
  Перед каждым ответом агента система производит гибридный поиск (семантический
  по эмбеддингам + keyword-поиск по именам) и выполняет **многошаговый обход
  графа в глубину** (BFS до `long_term_max_hops` шагов, по умолчанию 2), чтобы
  модель видела не только прямые связи, но и связанные цепочки фактов.
  При фоновом обслуживании ("Deep Thinking") система автоматически извлекает
  новые сущности и связи из диалогов (`extract_graph_from_messages`).

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
GET  /v1/onboarding/status     — пуста ли база / каких демо-агентов не хватает
POST /v1/onboarding/seed       — создать демо-агентов (идемпотентно)
GET  /v1/tts/voices            — список голосов сервера TTS
POST /v1/tts/speak             — {"text","voice"?,"agent_slug"?} -> аудио
```

Токен: если задан `MAOS_API_TOKEN`, все маршруты кроме `/health` и
`/dashboard` требуют `Authorization: Bearer <token>`. Сервер отказывается
слушать не-localhost без токена.

## Веб-интерфейс (дашборд)

`maos/web/dashboard.html` — один файл на vanilla JS, без сборки:

- **Обзор** — статистика памяти (агенты, диалоги, кванты, граф),
  расход по моделям `provider::model`; приветственный баннер с
  пошаговым онбордингом и кнопкой создания демо-агентов, если база
  пуста или часть демо-личностей отсутствует.
- **Агенты** — карточки вместо голой таблицы: аватар/цвет по slug,
  статус включён/выключен, форма создания/редактирования с подсказками
  под каждым полем (что оно даёт для роутинга, LLM, голоса).
- **Чат** — мультиагентный, с явным выбором агента или авто-роутингом;
  под каждым ответом видно, какая модель (`provider::model`) реально
  отвечала, метод роутинга и предупреждение о fallback на локальную
  модель, если облако было недоступно. `Ctrl+Enter` отправляет
  сообщение, не отпуская клавиатуру.
- **Цепочки** — запуск детерминированной `Agent_A -> Agent_B` по списку
  slug'ов, таблица шагов со статусами, история прошлых запусков.
- **Граф** — визуализация онтологии на canvas (круговая раскладка,
  цвет узла — по типу сущности).

Индикатор в шапке (зелёная/красная точка) показывает, жив ли сервис,
обновляется каждые 15 секунд без перезагрузки страницы. Токен
запрашивается один раз в поле шапки и применяется ко всем запросам.

## Навыки и инструменты агентов

Агенту можно назначить набор навыков полем `agent.tools` (через запятую:
`files,web,office,rag`) — тогда ход диалога с ним выполняется циклом
«модель -> инструмент -> модель» (`maos/agents/loop.py`), перенесённым
и адаптированным из `agent_system/agent/core.py`. Агент без `tools`
остаётся чистым синтезатором ответа — поведение не меняется.

- **files** (`maos/tools/files.py`) — чтение/запись/точечная правка/
  поиск в СВОЕЙ изолированной рабочей папке (`workspace_root/<slug>`,
  та же защита от выхода за пределы через `..`/симлинки, что в
  `agent_system/agent/tools/base.py:Workspace`). Разные агенты не видят
  файлы друг друга.
- **web** (`maos/tools/web.py`) — поиск (DuckDuckGo/SearXNG) и загрузка
  страниц с защитой от SSRF (резолвинг хоста и проверка на приватные/
  служебные адреса перед каждым запросом и каждым редиректом), без
  единой pip-зависимости.
- **office** (`maos/tools/office_docs.py`) — создание Word/Excel/
  PowerPoint из markdown/JSON (`python-docx`/`openpyxl`/`python-pptx`,
  ленивый импорт).
- **rag** (`maos/skills/rag.py`) — индексация текста и гибридный поиск
  (векторный через pgvector + полнотекстовый через `tsvector`) на той
  же PostgreSQL-базе, что и остальная память MAOS; включая RAG на
  онтологии (`rag_query_entity` — поиск по фрагментам, привязанным к
  конкретному объекту графа).

Лимит шагов вызова инструментов на ОДИН ход диалога — `max_tool_steps`
(по умолчанию 8): без него агент со сломанной моделью мог бы звать
инструменты бесконечно на каждое сообщение.

## TTS: реальный клиент OmniVoice (ТЗ п.8)

`maos/tts/provider.py` — рабочий HTTP-клиент OmniVoice Official API:

```
POST {TTS_BASE_URL}/tts/v1/synthesize   {"text","voice","audio_format"}
GET  {TTS_BASE_URL}/voices
```

Настройка:

```bash
TTS_PROVIDER=omnivoice
TTS_BASE_URL=http://localhost:9000     # адрес вашего сервера OmniVoice
TTS_API_KEY=                           # если сервер требует ключ
TTS_AUDIO_FORMAT=mp3                   # mp3 | wav | ogg | opus | flac
```

Голос конкретного агента — поля `voice_provider`/`voice_id` в его
профиле. HTTP API:

```
GET  /v1/tts/voices                 — список голосов сервера
POST /v1/tts/speak                  — {"text","voice"?,"agent_slug"?}
                                       -> БИНАРНЫЙ аудио-ответ
                                       (Content-Type: audio/*)
```

Если `voice` не передан явно, но передан `agent_slug` — используется
голос, настроенный для этого агента. В дашборде под каждым ответом
агента в чате есть кнопка «🔊 озвучить», проигрывающая аудио прямо в
браузере.

Спецификация OmniVoice не фиксирует схему ответа `/tts/v1/synthesize`
(`schema: {}` в OpenAPI) — клиент поддерживает оба реалистичных
варианта: сырые аудио-байты (`Content-Type: audio/*`) и JSON-обёртку
(`audio_base64`/`url`), определяя формат по фактическому Content-Type
ответа, а не по документации.

OpenAI TTS/ElevenLabs/Piper остаются НЕ РЕАЛИЗОВАННЫМИ — только
интерфейс и конфигурация (нет согласованной спецификации для них в этой
сессии); `synthesize()` кидает `NotImplementedError` с понятным
сообщением вместо тихой заглушки.

## Структура проекта

```
maos/
  config.py            — Config: DB_DSN обязателен, provider::model, память
  memory/store.py       — PostgreSQL+pgvector: agent/conversation/message/
                          memory_quantum/onto_entity/onto_relation/chain_*/
                          doc_chunk (фрагменты для rag)
  llm/                  — base-протокол (+ tool-calling), OpenAI-совместимый
                          драйвер, реестр provider::model, эмбеддинги
  orchestrator/
    hybrid.py            — гибридный роутинг + fallback
    context.py           — short/mid-term контроль контекста
    router.py            — semantic router (векторный, без LLM)
    service.py           — Orchestrator: полный цикл /v1/chat
    chain.py             — ChainRunner: детерминированная цепочка
  agents/
    runtime.py           — AgentRuntime: ход диалога, с опциональным
                          инструментальным циклом (если у агента есть tools)
    loop.py              — run_tool_loop: цикл «модель -> инструмент -> модель»
  tools/                — files/web/office_docs (перенос из agent_system),
                          base.py (Tool/Workspace/ToolRegistry), toolbox.py
                          (сборка набора по agent.tools, изоляция workspace)
  skills/rag.py          — индексация + гибридный поиск (векторный + FTS)
  maintenance/           — distill/dedup/synthesize_graph, run_forever
  maintenance_runner.py  — CLI: python3 -m maos.maintenance_runner [--once]
  demo_seed.py            — идемпотентный посев демо-агентов (быстрый старт)
  quickstart.py           — CLI: python3 -m maos.quickstart (embedded Postgres)
  tts/provider.py        — РЕАЛЬНЫЙ клиент OmniVoice + интерфейс остальных
  api/server.py          — HTTP API на stdlib (без внешних зависимостей)
  web/dashboard.html      — Admin Panel + Chat UI (с озвучкой ответов) +
                          Graph Visualization, приветственный онбординг
tests/                   — 437 проверок, реальный embedded Postgres+pgvector
                          (pgserver) и реальные HTTP-серверы, без моков
```

## Тесты

```bash
make test   # 437 проверок, ~20 секунд
```

Философия тестов — как в `agent_system`: реальная инфраструктура
(embedded Postgres через `pgserver`, реальные сокеты через
`ThreadingHTTPServer`), обязательные негативные сценарии, отсутствующие
опциональные зависимости (`psycopg`/`pgserver`) приводят к graceful
skip конкретного модуля, а не к падению всего набора.

## Внешние MCP-инструменты и уведомления

Агенту можно назначить `mcp` и `messaging` в поле `tools`, например
`files,web,mcp,messaging`. MCP-инструменты не реализуются в MAOS: при
старте хода агент подключается к вашим серверам, читает их `tools/list` и
передаёт модели их схемы. Во избежание конфликтов инструмент получает
имя `<server>_<tool>` — например `web_search_search`.

Конфигурация MCP и все токены находятся в **`.env`**, переменная
`MAOS_MCP_SERVERS`; используйте JSON формата LM Studio:

```dotenv
MAOS_MCP_SERVERS={"mcpServers":{"web_search":{"transport":"http","url":"http://111.222.333.44:8001/sse/","rate_limit":1},"img_toolforge":{"transport":"http","url":"https://img.toolforge.ru/sse","timeout":120}}}
```

Сервер, который не отвечает или отдаёт ошибку, не останавливает MAOS:
его инструменты просто не будут предложены модели в этом ходе. Для
каждого сервера можно задать `rate_limit`, `timeout`, `headers`,
`enabled` и `only_tools`. Секретные `Authorization`-заголовки задавайте
в `.env`, никогда не коммитьте их в JSON-файле.

### Email и MAX

SMTP-параметры также задаются в `.env`: `SMTP_HOST`, `SMTP_PORT`,
`SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM_ADDR`, `SMTP_USE_SSL` и
`SMTP_STARTTLS`. Это правильное место для пароля: файл `.env` уже
игнорируется Git; публичные параметры workflow можно хранить в
JSON-конфигурации. Агент с навыком `messaging` получает `email_send`
(включая вложения из своей рабочей папки) и при `MAX_BOT_TOKEN` —
`max_send_message`.

Отправка вовне необратима. `MAOS_CONFIRM_SENDS=true` (значение по
умолчанию) блокирует её, пока в UI нет отдельного подтверждения. Для
доверенного автоматического сценария уведомлений после QA установите
`MAOS_CONFIRM_SENDS=false`.
