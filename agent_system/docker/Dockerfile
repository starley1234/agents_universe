# Образ самого агента (CLI + API). Зависимостей нет — только stdlib.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash git curl docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY agent/ /app/agent/
COPY examples/ /app/examples/
COPY tests/ /app/tests/

ENV PYTHONUNBUFFERED=1 \
    AGENT_WORKSPACE=/work \
    AGENT_HOST=0.0.0.0

RUN mkdir -p /work
VOLUME ["/work"]
EXPOSE 8080

CMD ["python3", "-m", "agent.server"]
