# Troubleshooting — A.S.T.R.A. v0.3.0

## 1. LLM не запускается, embedding работает

**Симптом:**
```
Step attempted but LLM unavailable: litellm.BadRequestError: LLM Provider NOT provided.
You passed model=unsloth/gemma-4-12b-it
```

**Причина:** LiteLLM требует префикс провайдера в имени модели для OpenAI-совместимых эндпоинтов.
- Для LMStudio, Ollama, vLLM нужно `openai/<model>` вместо просто `<model>`
- Для OpenRouter — `openrouter/<model>`

Раньше в коде был баг: эмбеддинги уже использовали `openai/<model>` и работали, а LLM — нет.

**Исправлено в v0.3.1:**
- `gateway.py` теперь автоматически нормализует имя модели:
  - `local` provider → `openai/<model>` если нет префикса
  - `openrouter` → `openrouter/<model>`
- Логируется нормализованное имя: `LLM call: provider=local raw_model=... normalized_model=openai/...`

**Проверьте .env:**
```env
# Для запуска без Docker (LMStudio на том же хосте):
LOCAL_LLM_URL=http://localhost:1234/v1
LOCAL_LLM_MODEL=unsloth/gemma-4-12b-it
# Внутри Docker localhost = контейнер, поэтому для LMStudio на хосте используйте:
LOCAL_LLM_URL=http://host.docker.internal:1234/v1

EMBEDDING_URL=http://host.docker.internal:1234/v1
EMBEDDING_MODEL=text-embedding-qwen3-embedding-0.6b

LLM_DEFAULT_PROVIDER=local
```

**Docker networking:**
- В `docker-compose.yml` уже добавлен `extra_hosts: - "host.docker.internal:host-gateway"` — позволяет контейнеру достучаться до хоста.
- Если LMStudio на другом сервере — используйте IP: `http://192.168.1.100:1234/v1`
- Проверьте что LMStudio слушает `0.0.0.0`, а не только `127.0.0.1` (в настройках LMStudio: Local Server → Enable CORS, Listen on all interfaces)

**Логи:**
Теперь в логах будет подсказка если `localhost` + `connection` error:
```
LLM call failed (attempt 1/3 (Hint: inside Docker, localhost points to container. Use host.docker.internal...)): ...
```

## 2. Dashboard иконки отображаются как `&lt;svg...`

**Симптом:**
Вместо иконок видите текст `&lt;span class="text-xs...` и `&lt;svg...`

**Причина:** Старый Docker образ без `--build`. В шаблоне `index.html` была Jinja макро `{{ icon }}` без `|safe`, поэтому `<svg>` экранировался в `&lt;svg&gt;`.

**Исправлено:**
- В v0.3.0 иконки заменены на простые `●` (нет SVG в macro)
- Новый `base.html` без `x-cloak` на body — страница всегда видна даже если Alpine CDN не загрузился
- Всегда пересобирайте образ: `docker compose up -d --build`

**Решение для текущего:**
```bash
docker compose down
docker compose up -d --build
docker logs -f astra_app
curl http://localhost:8101/ | head -n 20  # должно содержать Dashboard
```

## 3. Database is locked (SQLite)

Если вы всё ещё видите `sqlite3.OperationalError: database is locked` — вы используете SQLite. В текущей версии продакшен только Postgres:

```bash
# Проверьте .env
DATABASE_URL=postgresql+asyncpg://astra:astra@postgres:5432/astra

# В Docker это уже выставлено в docker-compose.yml
# Локально без Docker:
# Запустите Postgres:
docker run -d --name astra_postgres -e POSTGRES_USER=astra -e POSTGRES_PASSWORD=astra -e POSTGRES_DB=astra -p 5432:5432 pgvector/pgvector:pg16
# Затем:
DATABASE_URL=postgresql+asyncpg://astra:astra@localhost:5432/astra python -m uvicorn ...
```

SQLite остался только для `pytest` (быстрые тесты), не для продакшена.

## 4. Web UI blank / белый экран

**Причины:**
- Старый образ с `x-cloak` на `body` + Alpine CDN не загрузился (нет интернета)
- Исправлено: `base.html` теперь без `x-cloak` на body, + fallback JS снимает x-cloak через 2 сек если Alpine не загрузился

**Проверка:**
```bash
curl -s http://localhost:8101/ | grep -o "Dashboard"
# Должно вывести Dashboard
```

Если всё ещё белый — смотрите логи:
```bash
docker logs astra_app --tail 100
```

## 5. FalkorDB, Langfuse, Auth не работают

Это опциональные фичи, по умолчанию выключены:
```env
USE_FALKORDB=false
LANGFUSE_ENABLED=false
AUTH_ENABLED=false
```

Чтобы включить:
```bash
# FalkorDB
docker compose --profile falkor up -d
USE_FALKORDB=true

# Full (app + worker + falkordb)
docker compose --profile full up -d --build

# Langfuse
LANGFUSE_ENABLED=true
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_PUBLIC_KEY=pk-lf-...

# Auth
AUTH_ENABLED=true
# После первого запуска создастся admin/admin — смените пароль!
JWT_SECRET_KEY=very-long-random-secret
```

## 6. Docker WSL: `mkdir /.../logs: file exists` + Network still in use

**Симптом (Windows WSL, ваш случай):**
```
=> exporting manifest list...
✔ Container astra_postgres Healthy
✔ Container astra_redis Healthy
Container astra_app Starting

Error response from daemon: error while creating mount source path '/mnt/c/github/agents_universe/a_s_t_r_a/logs': mkdir /mnt/c/.../logs: file exists

docker compose down
  Network a_s_t_r_a_default Resource is still in use
```

**Причина:** На WSL `/mnt/c` — Windows файловая система. Docker Desktop пытается сделать bind mount `./logs:/app/logs`, но на хосте `./logs` существует как **файл**, а не директория (например, loguru создал файл `logs` вместо директории, или вы случайно создали файл). Docker не может смонтировать файл как директорию.

Также `./workspace:/app/workspace` может давать такую же ошибку.

**Исправлено в v0.3.2:**
- В `docker-compose.yml` заменены bind mounts `./workspace` и `./logs` и `./src/astra/prompts` на **named volumes**:
  ```yaml
  volumes:
    - workspace_data:/app/workspace
    - logsdata:/app/logs
  ```
  Это полностью убирает зависимость от Windows FS `/mnt/c` и ошибку `file exists`.

- Если вам нужен доступ к workspace на хосте (для отладки), раскомментируйте bind mount вручную и убедитесь что это директория:
  ```bash
  rm -f logs  # если logs — файл, удалите файл
  rm -f workspace  # если файл
  mkdir -p logs workspace
  # В docker-compose.yml замените:
  # - workspace_data:/app/workspace
  # на:
  # - ./workspace:/app/workspace
  ```

**Решение для текущего (WSL):**
```bash
cd /mnt/c/github/agents_universe/a_s_t_r_a

# 1. Удалить старый конфликтный файл/директорию если это файл
ls -la logs
# Если logs — файл: rm logs
# Если директория — rm -rf logs
rm -rf logs
mkdir -p logs workspace

# 2. Полностью снести контейнеры, сети и volumes
docker compose down -v
docker network prune -f

# 3. Пересобрать с новым compose (named volumes)
docker compose up -d --build

# 4. Проверить
docker logs -f astra_app
curl http://localhost:8101/health
```

Если всё ещё `Network ... Resource is still in use`:
```bash
docker network ls
docker network rm a_s_t_r_a_default -f || true
docker compose down --remove-orphans
docker compose up -d --build
```

## 7. Проверка что всё работает

```bash
# Health
curl http://localhost:8101/health
# {"status":"ok"}

# Config
curl http://localhost:8101/api/config | jq

# Full health
curl http://localhost:8101/api/health/full | jq

# Создать проект + запустить mock агента (без LLM)
curl -X POST http://localhost:8101/api/projects/ -H "Content-Type: application/json" -d '{"name":"test"}'
curl -X POST http://localhost:8101/api/agents/run -H "Content-Type: application/json" -d '{"project_id":"<uuid>","goal":"Test"}'
```
