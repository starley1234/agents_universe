# ─── Build stage ──────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Build deps (needed for psycopg2 / asyncpg compilation)
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir --prefix=/install .


# ─── Runtime stage ────────────────────────────────────────────
FROM python:3.12-slim

# Runtime deps
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq5 curl && \
    rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

WORKDIR /app

# Copy source
COPY src/ src/
COPY alembic/ alembic/
COPY alembic.ini .
COPY pyproject.toml .

# Ensure the package is importable
ENV PYTHONPATH=/app/src

# Non-root user
RUN useradd --create-home --shell /bin/bash astra && \
    mkdir -p /app/logs /app/workspace && \
    chown -R astra:astra /app

USER astra

EXPOSE 8101

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:8101/health || exit 1

# Run migrations then start the server
CMD ["sh", "-c", "alembic upgrade head 2>/dev/null; uvicorn astra.main:app --host 0.0.0.0 --port 8101 --workers 2"]
