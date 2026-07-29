# Многослойная сборка: зависимости отдельным слоем, чтобы правки кода
# не тянули переустановку langgraph при каждом билде.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY aconstructor/__init__.py ./aconstructor/
RUN pip install --no-cache-dir ".[server,all]"

COPY aconstructor/ ./aconstructor/

# Работаем не под root: у сервиса нет причин иметь права на систему.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data && chown -R app:app /data /app
USER app

ENV ACONSTRUCTOR_DB=/data/aconstructor.db \
    ACONSTRUCTOR_HOST=0.0.0.0 \
    ACONSTRUCTOR_PORT=8080 \
    ACONSTRUCTOR_WORKERS=2

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status==200 else 1)"

CMD ["python", "-m", "aconstructor.api"]
