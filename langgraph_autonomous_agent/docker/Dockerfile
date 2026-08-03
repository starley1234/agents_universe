# syntax=docker/dockerfile:1
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl bash git \
    && rm -rf /var/lib/apt/lists/*

# Non-root user
RUN groupadd --gid 10001 agent && useradd --uid 10001 --gid agent --create-home --shell /usr/sbin/nologin agent

COPY requirements-app.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini ./
COPY README.md ./

RUN mkdir -p /data/workspace/.logs /data/artifacts \
    && chown -R agent:agent /app /data

USER agent
EXPOSE 8112

HEALTHCHECK --interval=20s --timeout=5s --retries=5 --start-period=30s \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8112/health', timeout=3)"

CMD ["python3", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8112"]
