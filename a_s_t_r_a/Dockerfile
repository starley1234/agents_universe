# ─── Builder ──────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends build-essential libpq-dev curl && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src/ src/
COPY alembic/ alembic/
COPY alembic.ini ./
COPY tests/eval/ tests/eval/

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --prefix=/install .

# ─── Runtime ──────────────────────────────────────────────────
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends libpq5 curl && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local
COPY --from=builder /build/src /app/src
COPY --from=builder /build/alembic /app/alembic
COPY --from=builder /build/alembic.ini /app/alembic.ini
COPY --from=builder /build/pyproject.toml /app/pyproject.toml
COPY --from=builder /build/README.md /app/README.md
COPY --from=builder /build/tests/eval /app/tests/eval
COPY docs/ /app/docs/

WORKDIR /app

ENV PYTHONPATH=/app/src:/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_PORT=8101

RUN useradd --create-home --shell /bin/bash astra && \
    mkdir -p /app/logs /app/workspace /app/docs /app/tests/eval && \
    chown -R astra:astra /app

USER astra

EXPOSE 8101

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -sf http://localhost:8101/health || exit 1

CMD ["sh", "-c", "alembic upgrade head || echo 'migrations failed, continuing'; exec uvicorn astra.main:app --host 0.0.0.0 --port 8101 --workers 1"]
