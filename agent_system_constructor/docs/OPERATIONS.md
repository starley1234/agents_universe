# Эксплуатация

Как поставить, защитить, наблюдать и чинить. Всё, что нужно знать
дежурному, — на одной странице.

---

## Установка

### Docker (рекомендуется)

```bash
cp .env.example .env
# обязательно задайте токен, иначе сервис поднимется только на localhost
python3 -c "import secrets; print('ACONSTRUCTOR_API_TOKEN=' + secrets.token_urlsafe(32))" >> .env
docker compose up -d
curl localhost:8080/health
```

Данные (журнал прогонов) живут в томе `aconstructor-data`, поэтому
пересборка образа их не трогает.

### systemd

```bash
sudo useradd --system --home /opt/aconstructor aconstructor
sudo mkdir -p /opt/aconstructor /etc/aconstructor
sudo cp -r . /opt/aconstructor && cd /opt/aconstructor
sudo python3 -m venv .venv && sudo .venv/bin/pip install ".[server,all]"
sudo cp .env.example /etc/aconstructor/env && sudo chmod 600 /etc/aconstructor/env
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
sudo systemctl enable --now aconstructor aconstructor-purge.timer
```

Юнит запускается от отдельного пользователя, с `ProtectSystem=strict` и
лимитом памяти 1 ГБ. Таймер раз в неделю чистит прогоны старше 90 дней.

---

## Доступ и безопасность

**Токен обязателен для публикации.** Без `ACONSTRUCTOR_API_TOKEN` команда
`serve` слушает только `127.0.0.1` и отказывается стартовать на внешнем
адресе. Это защита от главной ошибки — выставить наружу сервис, который
тратит деньги на токены LLM.

Токен должен быть ASCII: он уходит в HTTP-заголовок, а тот обязан быть
latin-1. Кириллический токен клиент физически не сможет отправить, поэтому
сервис падает на старте, а не на первом запросе.

```bash
curl -H "Authorization: Bearer $TOKEN" localhost:8080/api/pipelines
```

`/health` и `/metrics` намеренно открыты — их дёргают оркестратор и
Prometheus, которым неоткуда взять заголовок.

Перед выставлением в интернет поставьте reverse-proxy с TLS. Сам сервис
HTTPS не терминирует.

---

## Наблюдение

| ручка | зачем |
|---|---|
| `GET /health` | liveness/readiness: статус, провайдер, глубина очереди |
| `GET /metrics` | Prometheus: прогоны по статусам, стоимость, длительность |
| `GET /api/stats` | сводка для людей: успешность, траты, разбивка по пайплайнам |
| `aconstructor stats` | то же из терминала |
| `aconstructor history --status failed` | что падало |

Что смотреть в первую очередь:

- **`aconstructor_queue_depth` растёт** — воркеры не успевают. Поднимите
  `ACONSTRUCTOR_WORKERS` или разберитесь, почему прогоны медленные.
- **доля `failed` растёт** — обычно провайдер LLM недоступен или кончилась
  квота. Смотрите `aconstructor history --status failed` и поле `error`.
- **`aconstructor_cost_usd_total` растёт быстрее ожидаемого** — кто-то гоняет
  дорогую модель. Стоимость считается по прайсу из `runner.PRICES`.

---

## Обслуживание

```bash
aconstructor doctor              # самопроверка: графы, LLM, база, токен
aconstructor doctor --probe      # плюс реальный вызов модели
aconstructor purge --older-than-days 90
aconstructor history --pipeline energy-hacker --limit 20
aconstructor show <run_id>
```

**Резервная копия.** Всё состояние — один файл SQLite
(`ACONSTRUCTOR_DB`, по умолчанию `data/aconstructor.db`). Копировать
безопасно на живом сервисе через `sqlite3 base.db ".backup copy.db"`.

**Рестарт безопасен.** При старте прогоны, застрявшие в `running` и
`queued`, помечаются упавшими с причиной «прервано рестартом сервиса» —
иначе они висели бы вечно и искажали статистику.

---

## Настройки

| переменная | по умолчанию | смысл |
|---|---|---|
| `ACONSTRUCTOR_PROVIDER` | `fake` | `fake` \| `openai` \| `anthropic` \| `ollama` |
| `ACONSTRUCTOR_MODEL` | по провайдеру | имя модели |
| `ACONSTRUCTOR_API_KEY` | — | ключ провайдера |
| `ACONSTRUCTOR_BASE_URL` | — | для OpenAI-совместимых шлюзов |
| `ACONSTRUCTOR_API_TOKEN` | — | bearer-токен; без него только localhost |
| `ACONSTRUCTOR_DB` | `data/aconstructor.db` | файл журнала прогонов |
| `ACONSTRUCTOR_WORKERS` | `2` | параллельных прогонов |
| `ACONSTRUCTOR_TIMEOUT` | `600` | предел на один прогон, с |
| `ACONSTRUCTOR_HOST` / `_PORT` | `127.0.0.1` / `8080` | адрес сервиса |
| `ACONSTRUCTOR_LOG` | `INFO` | уровень логирования |

---

## Что ломается и почему

**Прогон падает с таймаутом.** Поток не убивается — в Python это
небезопасно; мы перестаём его ждать и помечаем прогон упавшим. Поток-сирота
завершится сам на следующем сетевом ответе. Если таких много, увеличьте
`ACONSTRUCTOR_TIMEOUT` или уменьшите объём задачи.

**429 «очередь переполнена».** Защита от накопления задач, которых уже
никто не ждёт. Увеличьте воркеров или размер очереди осознанно.

**Пустой результат при непустом входе.** Проверьте, что ключи задачи
совпадают с ожидаемыми (`GET /api/pipelines/{slug}` показывает `demo_task`).
Отсутствующий ключ заменяется демо-данными, а вот пустой список — нет: это
осмысленный вход, и подменять его выдумкой нельзя.

**Стоимость показывает $0.** Провайдер `fake` бесплатный. Для реальных
моделей проверьте, что имя модели попадает в префиксы `runner.PRICES`;
незнакомая модель считается по нулю, а не по чужому прайсу.

---

## Масштабирование

Сейчас это один процесс с пулом потоков и SQLite. Такой конфигурации
хватает на десятки прогонов в час — узкое место не в нём, а в задержке
LLM.

Что делать, когда упрётесь:

1. **Больше воркеров** (`ACONSTRUCTOR_WORKERS`) — до предела рейт-лимита
   провайдера.
2. **Несколько реплик** — потребуют вынести журнал в PostgreSQL и очередь в
   Redis/RQ. Интерфейс `RunStore` для этого достаточно узкий: заменяется
   один класс.
3. **Долгие прогоны** (реставрация сотен листов) стоит резать на задачи по
   листу и складывать результат — LangGraph это позволяет через
   checkpointer, который пока не включён.
