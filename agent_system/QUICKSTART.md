# Развёртывание за 5 минут

## 1. Перенести

```bash
scp -r agent_system/ user@сервер:~/
ssh user@сервер && cd ~/agent_system
```

1.7 МБ. Нужен только **Python 3.10+**. `pip install` не требуется.

## 2. Проверить

```bash
make test          # 837 проверок, ~40 с
```

Всё зелёное — система рабочая.

## 3. Указать модель

**Вариант А — ваш LM Studio на GPU-сервере** (рекомендую):

```bash
export AGENT_PROVIDER=lmstudio
export AGENT_MODEL=devstral-small-2
export OPENAI_BASE_URL=http://GPU-СЕРВЕР:1234/v1
```

В LM Studio: загрузить модель → вкладка Developer → Start Server.
Контекст поставить **32K** (не 128K — KV-кэш съест VRAM).

**Вариант Б — OpenRouter:**

```bash
export AGENT_PROVIDER=openai
export AGENT_MODEL=anthropic/claude-sonnet-4
export OPENAI_BASE_URL=https://openrouter.ai/api/v1
export OPENAI_API_KEY=sk-or-...
```

## 4. Проверить связь

```bash
python3 -m agent --check
```

Покажет модель, песочницу, навыки, MCP-серверы. Ошибки — здесь.

## 5. Запустить

```bash
python3 -m agent "покажи файлы и найди TODO"     # разовая задача
python3 -m agent                                  # диалог
make serve                                        # веб на :8080
```

---

## Изоляция команд

По умолчанию **`auto`**: есть Docker — работает в контейнере, нет —
на хосте с подтверждением опасных команд. Настраивать не нужно.

```bash
make build-sandbox              # если хотите контейнер
export AGENT_SANDBOX=confirm    # или явно на хост (для 1 ГБ RAM)
export AGENT_SANDBOX=off        # без ограничений, изолируете сами
```

`python3 -m agent --check` покажет, какой режим применится фактически.

---

## Автономный прогон

```bash
python3 -m agent --auto -P autonomous --hours 8 "ваша цель"
python3 -m agent --auto --resume 3      # продолжить после обрыва
```

Состояние в `agent.db`, переживает перезапуск.

---

## MCP: ваши серверы поиска и загрузки

Создайте `my.json`:

```json
{
  "provider": "lmstudio",
  "model": "devstral-small-2",
  "base_url": "http://GPU-СЕРВЕР:1234/v1",
  "profile": "autonomous",
  "sandbox": { "mode": "confirm" },
  "mcp": { "servers": {
    "search": { "command": "npx", "args": ["-y", "ваш-сервер"], "rate_limit": 20 },
    "fetch":  { "command": "uvx", "args": ["ваш-fetch"], "rate_limit": 0 }
  }}
}
```

`rate_limit`: секунды между вызовами, **0 = безлимитно**.

```bash
python3 -m agent --check -c my.json     # проверить серверы
python3 -m agent -c my.json --auto --hours 8 "цель"
```

---

## Как демон

```bash
sudo tee /etc/systemd/system/agent.service <<EOF
[Unit]
Description=Agent API
After=network.target

[Service]
User=$USER
WorkingDirectory=$HOME/agent_system
Environment="AGENT_PROVIDER=lmstudio"
Environment="AGENT_MODEL=devstral-small-2"
Environment="OPENAI_BASE_URL=http://GPU-СЕРВЕР:1234/v1"
Environment="AGENT_SANDBOX=confirm"
ExecStart=/usr/bin/python3 -m agent.server
Restart=always

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now agent
```

Веб и API на `http://127.0.0.1:8080/`.
Наружу — только с токеном: `AGENT_API_TOKEN=секрет`, иначе сервер
откажется слушать не-localhost.

---

## Обновление

Просто перезаписать файлы — да. Состояние (`agent.db`, `workspace/`) не
трогается, схема БД обновляется сама.

```bash
cd /opt/agent_system
cp agent.db agent.db.bak 2>/dev/null      # на всякий случай
# скопировать новые файлы поверх (папку agent/ и tests/)
python3 -m agent --check                   # проверить
make test                                  # 255 проверок
```

Если агент запущен как служба: `sudo systemctl restart agent`.

## Память между запусками

Каждый `python3 -m agent "..."` — **новый процесс**. Модель не помнит
прошлый запуск и, если её спросить, склонна это выдумывать.

Память подключена по умолчанию (навык `memory`, файл `agent.db`):
агент записывает выводы через `remember` и находит их через `recall`
в следующем запуске.

Для непрерывной работы над одной целью используйте автономный режим —
там ещё и план сохраняется:

```bash
python3 -m agent --auto --hours 4 "цель"
python3 -m agent --auto --resume 1
```

## Шпаргалка

| команда | что делает |
|---|---|
| `make test` | 837 проверок |
| `python3 -m agent --check` | диагностика |
| `python3 -m agent "задача"` | разовый запуск |
| `python3 -m agent --auto -P autonomous --hours 8 "цель"` | автономно |
| `python3 -m agent --auto --resume N` | продолжить |
| `make serve` | веб + API |
| `python3 -m agent -P cad_auto ...` | роль конструктора |

Роли: `coder` · `cad` · `cad_auto` · `autonomous` · `research` · `marketing`
· **`docs`** (разбор документов) · **`rag`** (поиск по базе) · **`verify`** (сертификация) · **`onto`** (онтология в PostgreSQL)

Подробно о них — [SKILLS.md](docs/SKILLS.md).

## Если что-то не так

| симптом | причина |
|---|---|
| «Не достучались» | не тот `base_url`, или LM Studio не запустил сервер |
| «нужен ключ» | не задан `OPENAI_API_KEY` |
| run_command не работает | было при `mode=docker` без демона — исправлено, теперь деградирует до confirm |
| MCP «НЕДОСТУПЕН» | проверьте `command` вручную в терминале |
| агент не зовёт инструменты | модель не умеет tool calling — смените |
| останавливается через 2 шага | reasoning-модели (Qwen3.5, DeepSeek-R1) отдают рассуждение в `reasoning_content` при пустом `content`. Агент принимал НАМЕРЕНИЕ («Покажу возможности…») за результат. Исправлено: такой ход не считается завершением, модель подталкивается к действию (до 3 раз) |
| «файл не найден», хотя он есть | модель прислала путь как `[main.py](http://main.py)` — теперь распознаётся |
| в логе видно `[main.py](http://main.py)` и `**name**` | это markdown-разметка **при вставке лога в чат**, а не в самом агенте. В терминале путь нормальный |
| «да, помню прошлые шаги» (а не помнит) | навык `memory` теперь по умолчанию + промпт запрещает выдумывать |
