# Быстрый старт с Agent Toolkit (Quickstart Guide)

Это руководство поможет вам за 5 минут настроить и начать использовать **Agent Toolkit** в ваших проектах агентов и микросервисах.

---

## 1. Установка

В корне проекта выполните установку пакета в редактируемом режиме:

```bash
pip install -e .[all]
```

Опциональный флаг `[all]` установит библиотеки для работы с HTTP REST API (`fastapi`, `uvicorn`, `pydantic`) и генерации офисных документов (`python-docx`, `openpyxl`).

---

## 2. Локальное использование через Python SDK

```python
from agent_toolkit import build_default_registry

# 1. Инициализируем реестр (по умолчанию рабочая папка в /tmp/agent_toolkit_ws)
reg = build_default_registry()
print(f"Доступно инструментов: {len(reg.list_tools())}")

# 2. Ищем инструмент по смыслу (без знания точного имени)
hits = reg.search("сгенерировать 3d модель шестерни openscad", limit=1)
tool, score = hits[0]
print(f"Лучший инструмент: {tool.name} (релевантность: {score})")
# -> cad.generate_gear

# 3. Выполняем инструмент и получаем результат
res_scad = reg.execute(
    "cad.generate_gear",
    path="gear20.scad",
    module_mm=2.0,
    teeth_count=20,
)
print(res_scad)

# 4. Рассчитываем механическую прочность балки / детали
res_str = reg.execute(
    "physics.calc_strength",
    load_n=10000.0,
    area_mm2=50.0,
    yield_strength_mpa=250.0,
)
print(res_str)
```

---

## 3. Запуск сервера и визуального каталога (Web UI)

Для запуска HTTP REST API и MCP сервера выполните:

```bash
python3 -m agent_toolkit serve --port 8090
```

После запуска в вашем браузере будут доступны:
- **`http://localhost:8090/ui`** — Визуальный каталог инструментов и 3D-вьювер артефактов на HTML5 Canvas.
- **`http://localhost:8090/docs`** — Интерактивная документация OpenAPI (Swagger UI).
- **`http://localhost:8090/health`** — Проверка работоспособности сервиса.

---

## 4. Запуск в Docker

Для изолированного развёртывания в Docker используйте входящий в комплект `docker-compose.yml`:

```bash
docker-compose up -d --build
```

Контейнер `agent-toolkit-service` запустится от имени непривилегированного пользователя `agentuser` и пробросит порт `8090:8090`.

---

## 5. Запуск самопроверки и тестов

```bash
# Провести проверку продакшн-окружения
python3 -m agent_toolkit check

# Запустить полный набор автотестов (32 модуля, 287 проверок)
make test

# Запустить бенчмарк умного поискового движка
make benchmark
```
