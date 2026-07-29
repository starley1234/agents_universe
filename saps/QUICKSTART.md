# САПС — быстрый старт

От пустой машины до работающей системы. Два пути: **Docker** (проще,
рекомендуется) и **на хост** (если PostgreSQL уже развёрнут).

---

## Вариант 1. Docker — 5 минут

### Что нужно

Docker 20.10+ с плагином Compose. Проверка:

```bash
docker compose version
```

### Шаг 1. Настройки

```bash
cd saps
cp .env.example .env
```

В `.env` обязательно заполнить две строки — без них compose откажется
стартовать (это защита, а не придирка: база сертификационных данных не
должна подниматься с паролем по умолчанию):

```bash
POSTGRES_PASSWORD=<придумайте длинный пароль>
SAPS_API_TOKEN=<случайная строка>
```

Токен удобно сгенерировать так:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### Шаг 2. Запуск

```bash
docker compose up -d --build
```

Поднимутся два контейнера: `saps-db` (PostgreSQL 16 с pgvector) и
`saps-app`. Приложение дождётся готовности базы автоматически.

### Шаг 3. Схема базы и справочники

```bash
docker compose run --rm saps migrate           # создать схему
docker compose run --rm saps rules load --builtin   # демо-справочники АП
docker compose restart saps
```

### Шаг 4. Проверка

```bash
curl -s http://127.0.0.1:8090/health          # процесс жив
curl -s http://127.0.0.1:8090/ready           # база и схема в порядке
```

Откройте **http://127.0.0.1:8090/dashboard** и введите токен из `.env`
в поле в шапке.

### Шаг 5. Первый документ

Положите PDF или Word в папку `inbox/` рядом с `docker-compose.yml`:

```bash
mkdir -p inbox && cp ~/Авиационные_правила_25.pdf inbox/
docker compose exec saps python3 -m saps load /inbox/Авиационные_правила_25.pdf
```

Или просто перетащите файл в браузере: вкладка **«Загрузка»**.

---

## Вариант 2. На хост (systemd)

### Шаг 1. База

Нужен PostgreSQL 14+ с расширением `pgvector`:

```bash
sudo -u postgres psql <<'SQL'
CREATE USER saps WITH PASSWORD 'пароль';
CREATE DATABASE saps OWNER saps;
\c saps
CREATE EXTENSION IF NOT EXISTS vector;
SQL
```

Если `CREATE EXTENSION` не проходит — пакет расширения не установлен:
`apt install postgresql-16-pgvector` (Debian/Ubuntu) или сборка из
исходников `pgvector`.

### Шаг 2. Приложение

```bash
sudo useradd --system --home /opt/saps --shell /usr/sbin/nologin saps
sudo mkdir -p /opt/saps /var/lib/saps && sudo chown saps:saps /var/lib/saps

sudo -u saps git clone <репозиторий> /opt/saps
cd /opt/saps/saps
sudo -u saps python3 -m venv .venv
sudo -u saps .venv/bin/pip install -r requirements.txt
```

### Шаг 3. Настройки

```bash
sudo install -o saps -g saps -m 600 /dev/null /etc/saps.env
sudo -e /etc/saps.env
```

```bash
SAPS_DB_DSN=postgresql://saps:пароль@localhost:5432/saps
SAPS_API_TOKEN=<случайная строка>
SAPS_HOST=127.0.0.1
SAPS_PORT=8090
SAPS_WORKDIR=/var/lib/saps
SAPS_LOG_LEVEL=INFO
```

Права `600` обязательны: в файле пароль от базы и токен доступа.

### Шаг 4. Схема и служба

```bash
cd /opt/saps/saps
sudo -u saps env $(cat /etc/saps.env | xargs) .venv/bin/python -m saps migrate
sudo cp deploy/saps.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now saps
systemctl status saps
```

---

## Внешняя модель эмбеддингов

Семантический подбор пунктов АП работает настолько хорошо, насколько
хороша модель. Офлайн-`hash` сравнивает слова, внешняя модель — смысл.

В `.env`:

```bash
SAPS_EMBEDDING_PROVIDER=lmstudio         # или ollama, vllm, openai
SAPS_EMBEDDING_MODEL=text-embedding-nomic-embed-text-v1.5
SAPS_EMBEDDING_BASE_URL=http://192.168.1.50:1234/v1
SAPS_EMBEDDING_DIM=768
```

> **Docker:** если модель крутится на самом хосте, адрес внутри
> контейнера — не `localhost`, а `http://host.docker.internal:1234/v1`
> (Linux: добавьте в сервис `extra_hosts: ["host.docker.internal:host-gateway"]`).

Проверка и выбор размерности:

```bash
docker compose run --rm saps embeddings      # отвечает ли, какой вектор
```

**Размерность фиксируется при создании схемы навсегда** (ограничение
pgvector). Если меняете модель на другую размерность — нужна новая схема.
Если размерность та же, достаточно пересчитать векторы:

```bash
docker compose run --rm saps index --force
```

---

## Ежедневная работа

```bash
# загрузить документ — система сама разберётся, что это
docker compose exec saps python3 -m saps load /inbox/ТЗ.docx --owner Иванов

# что предлагают агенты
docker compose exec saps python3 -m saps suggestions

# принять предложение
docker compose exec saps python3 -m saps suggestions --accept 17

# готовность к сдаче регулятору
docker compose exec saps python3 -m saps health --by-node

# протокол соответствия
docker compose exec saps python3 -m saps export docx
```

Файлы выгрузок — в томе `saps-data`. Забрать на хост:

```bash
docker compose cp saps:/data/. ./exports/
```

---

## Эксплуатация

### Резервное копирование

**Копия — единственная защита от ошибки оператора.** В базе лежит вся
прослеживаемость требований; восстановить её из Teamcenter невозможно —
там нет истории правок и решений инженеров.

```bash
docker compose exec saps python3 -m saps backup --out /data/saps.dump
docker compose cp saps:/data/saps.dump ./backups/saps_$(date +%F).dump
```

В cron на хосте:

```cron
0 2 * * * cd /opt/saps/saps && docker compose exec -T saps python3 -m saps backup \
  --out /data/nightly.dump && docker compose cp saps:/data/nightly.dump \
  /backup/saps_$(date +\%F).dump
```

Восстановление:

```bash
docker compose cp ./backups/saps_2026-07-29.dump saps:/tmp/r.dump
docker compose exec saps pg_restore --clean --if-exists --no-owner \
  -d "$SAPS_DB_DSN" /tmp/r.dump
```

Проверяйте восстановление хотя бы раз в квартал: копия, из которой ни
разу не восстанавливались, — это предположение, а не копия.

### Обновление версии

```bash
docker compose exec saps python3 -m saps backup --out /data/before-upgrade.dump
git pull
docker compose build saps
docker compose run --rm saps migrate --dry-run    # что изменится в схеме
docker compose run --rm saps migrate
docker compose up -d saps
curl -s http://127.0.0.1:8090/ready
```

Приложение **не мигрирует базу само при старте** — это осознанно:
пятнадцать одновременно стартующих инстансов не должны выполнять
`ALTER TABLE` наперегонки. Сервер лишь проверяет версию схемы и
отказывается работать на несовместимой (код возврата 3).

### Мониторинг

| Точка | Что означает | Реакция |
|---|---|---|
| `GET /health` | процесс жив; **базу не проверяет** | нет ответа → перезапустить контейнер |
| `GET /ready` | база отвечает, схема совместима, pgvector на месте | 503 → вывести из ротации, смотреть логи |
| `GET /metrics` | метрики в формате Prometheus | — |

`/health` намеренно не ходит в базу: liveness-проба должна отвечать и
при лежащей БД, иначе оркестратор начнёт перезапускать исправное
приложение из-за чужого сбоя.

Полезные метрики: `saps_suggestions_pending` (сколько предложений ждут
инженера), `saps_low_quality` (требований ниже порога),
`saps_requirements`, `saps_clauses`.

### Логи

```bash
docker compose logs -f saps
```

Уровень — `SAPS_LOG_LEVEL` (`DEBUG`/`INFO`/`WARNING`). Пробы мониторинга
пишутся в `DEBUG`, чтобы не топить журнал. Для сборщиков (ELK, Loki):
`SAPS_LOG_JSON=true`.

### Перезапуск базы

Плановое обслуживание PostgreSQL перезапускать САПС **не требует**:
приложение обнаруживает оборванное соединение и переподключается само.
Проверено обрывом всех сессий на живой системе.

---

## Безопасность

Что уже сделано и о чём нужно позаботиться самим.

**Сделано:**
- порт публикуется только на `127.0.0.1` — наружу через обратный прокси;
- без `SAPS_API_TOKEN` система отказывается слушать не-localhost;
- контейнер работает от непривилегированного пользователя (uid 10001);
- секреты только в окружении, в JSON-конфиг не читаются;
- `.dockerignore` не пускает в образ `.git` и `.env`;
- запись в Teamcenter выключена по умолчанию и защищена четырьмя
  условиями (см. README).

**Нужно сделать вам:**
- **TLS.** Поставьте nginx/traefik перед САПС. По HTTP токен ходит
  открытым текстом.
- **Разграничение прав.** Его нет: любой, у кого есть токен, может
  утвердить любое предложение. Действия пишутся в журнал с указанием
  автора, но авторизации по ролям нет. Если это критично — ограничьте
  доступ на уровне прокси (доменная аутентификация) и выдайте разные
  токены разным контурам.
- **Ротация токена.** Смените `SAPS_API_TOKEN` и перезапустите сервис.

Пример nginx:

```nginx
server {
    listen 443 ssl;
    server_name saps.example.local;
    ssl_certificate     /etc/ssl/certs/saps.crt;
    ssl_certificate_key /etc/ssl/private/saps.key;
    client_max_body_size 64m;        # PDF справочников бывают крупными

    location / {
        proxy_pass http://127.0.0.1:8090;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 300s;      # загрузка большого PDF идёт минуты
    }
}
```

---

## Если что-то пошло не так

| Симптом | Причина и что делать |
|---|---|
| Контейнер `saps` перезапускается, в логах «Схема базы несовместима» | Не выполнена миграция: `docker compose run --rm saps migrate` |
| `/ready` отдаёт 503, `database: down` | База не поднялась: `docker compose logs db`, проверьте `POSTGRES_PASSWORD` |
| «Не удалось включить расширение pgvector» | Образ базы без pgvector. В compose должен быть `pgvector/pgvector:pg16` |
| «Модель вернула вектор размерности N, а схема рассчитана на M» | Сменили модель эмбеддингов. Та же размерность → `saps index --force`; другая → новая схема |
| «Не достучались до сервера эмбеддингов» | Из контейнера `localhost` — это сам контейнер. Используйте `host.docker.internal` |
| При загрузке PDF: «почти нет текстового слоя» | Это скан. Нужен OCR (ABBYY FineReader, OCRmyPDF), САПС картинки не распознаёт |
| Классификатор ничего не предлагает | Пуст справочник (`saps rules list`) или векторы не посчитаны (`saps index`) |
| `compose up` падает: «задайте POSTGRES_PASSWORD» | Не заполнен `.env` — это защита от запуска с паролем по умолчанию |

Диагностика одной командой:

```bash
docker compose exec saps python3 -m saps check
```

Показывает: доступность базы, версию схемы, состояние эмбеддера,
наличие справочников, настройки Teamcenter.
