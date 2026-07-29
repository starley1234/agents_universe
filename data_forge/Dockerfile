# syntax=docker/dockerfile:1
#
# Multi-stage сборка DataForge: этап builder ставит зависимости в venv,
# финальный образ копирует только venv + код приложения — без
# build-инструментов (gcc и т.п., нужных psycopg[binary] иногда лишь на
# этапе pip install), без кэша pip, без requirements-dev.txt.
#
# ЧЕСТНО: эта сборка НЕ проверялась через реальный `docker build` внутри
# сессии — окружение песочницы не имеет Docker (команда `docker`
# недоступна). Каждая часть, которую МОЖНО было проверить без Docker,
# проверена: команда прод-запуска (`python3 -m dataforge.server` с
# FORGE_WORKERS>1), fail-fast поведение при отсутствии/недоступности
# PostgreSQL, устранённая гонка при параллельном создании схемы — все
# проверены реальными процессами/subprocess в тестах этого репозитория
# (см. tests/test_prod_lifespan.py, tests/test_store.py). Синтаксис
# Dockerfile проверен построчно вручную (стандартные, широко
# используемые конструкции — multi-stage FROM/COPY --from, non-root
# USER, HEALTHCHECK), но итоговый образ ни разу не собирался.

FROM python:3.11-slim AS builder

WORKDIR /build

# psycopg[binary] использует готовые wheel-пакеты для большинства платформ,
# но build-essential на всякий случай (dev-заголовки для редких
# платформ/архитектур, где готового wheel нет) — только в builder-этапе,
# в финальный образ не попадает.
RUN apt-get update && apt-get install --no-install-recommends -y \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt


FROM python:3.11-slim AS runtime

# Некорневой пользователь — контейнер не должен работать от root ни при
# каком сценарии компрометации зависимости/кода.
RUN groupadd --gid 1000 dataforge \
    && useradd --uid 1000 --gid dataforge --shell /usr/sbin/nologin \
        --home-dir /app --create-home dataforge

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY --chown=dataforge:dataforge dataforge/ ./dataforge/

USER dataforge

# Порт по умолчанию (FORGE_PORT), см. dataforge/config.py. Переопределяется
# через переменную окружения FORGE_PORT — EXPOSE только документирует
# порт по умолчанию, не ограничивает фактический.
EXPOSE 8200

# HEALTHCHECK бьёт в /health — единственный маршрут без токена (см.
# dataforge/api/server.py) — оркестратор контейнеров не должен знать
# секрет, чтобы определить живость процесса. Exec-форма (JSON-массив)
# вместо shell-формы: python сам читает FORGE_PORT из os.environ,
# shell-интерполяция не нужна — так же надёжнее избежать проблем
# экранирования вложенных кавычек в shell-форме CMD/HEALTHCHECK.
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('FORGE_PORT', '8200') + '/health', timeout=2)"]

# FORGE_HOST по умолчанию в dataforge/config.py — 127.0.0.1 (безопасно
# по умолчанию вне контейнера). В контейнере нужен 0.0.0.0, чтобы порт
# был виден снаружи — задаётся ЯВНО через docker-compose.yml/окружение,
# не переопределяется здесь молча, чтобы намерение было видно в одном
# месте (docker-compose.yml), а не размазано между Dockerfile и compose.
CMD ["python", "-m", "dataforge.server"]
