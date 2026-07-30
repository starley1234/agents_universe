# syntax=docker/dockerfile:1
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 maos && useradd --uid 10001 --gid maos --create-home --shell /usr/sbin/nologin maos

COPY requirements.txt requirements-optional.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-optional.txt

COPY maos ./maos
COPY README.md QUICKSTART.md ./
RUN mkdir -p /data/artifacts /data/workspace && chown -R maos:maos /app /data

USER maos
EXPOSE 8090
CMD ["python", "-m", "maos"]
