# Образ САПС. Многоступенчатая сборка: зависимости ставятся отдельно,
# чтобы правка кода не пересобирала слой с пакетами (сборка в закрытом
# контуре, где до зеркала pip может быть далеко, — это минуты разницы).

# ---------- ступень 1: зависимости ----------
FROM python:3.11-slim AS deps

# Пакеты ставим в отдельный префикс, чтобы скопировать одним слоем.
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt .

# pgserver нужен ТОЛЬКО тестам (embedded PostgreSQL) — в образе он
# лишние ~100 МБ и вводит в заблуждение: в проде база внешняя.
RUN grep -v '^pgserver' requirements.txt > requirements.prod.txt \
    && pip install --prefix=/install -r requirements.prod.txt

# ---------- ступень 2: рабочий образ ----------
FROM python:3.11-slim

# pg_dump для команды `saps backup`; curl — для HEALTHCHECK.
# Ставим одной командой и чистим списки apt, чтобы не тащить их в слой.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client curl \
    && rm -rf /var/lib/apt/lists/*

# Непривилегированный пользователь: САПС читает чужие документы и ходит
# в промышленный PDM — процессу незачем иметь root в контейнере.
RUN useradd --create-home --shell /bin/bash --uid 10001 saps

COPY --from=deps /install /usr/local
WORKDIR /app
COPY --chown=saps:saps . /app

# Рабочий каталог для выгрузок (протоколы, срезы) — том, а не слой
# образа: файлы должны переживать пересоздание контейнера.
RUN mkdir -p /data && chown saps:saps /data
VOLUME ["/data"]

USER saps

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SAPS_WORKDIR=/data \
    SAPS_HOST=0.0.0.0 \
    SAPS_PORT=8090 \
    SAPS_LOG_LEVEL=INFO

EXPOSE 8090

# /health — liveness: отвечает, даже если база лежит. Перезапускать
# приложение из-за недоступной БД бессмысленно, поэтому здесь именно
# health, а не ready.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8090/health || exit 1

ENTRYPOINT ["python3", "-m", "saps"]
CMD ["serve"]
