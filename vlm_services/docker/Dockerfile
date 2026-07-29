# Зависимости отдельным слоем: правки кода не тянут переустановку torch-free
# стека langchain при каждой сборке.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY vlmkit/__init__.py ./vlmkit/
RUN pip install --no-cache-dir ".[all]"

COPY vlmkit/ ./vlmkit/

# Не root: сервису незачем иметь права на систему.
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

ENV VLM_HOST=0.0.0.0 VLM_PORT=8081
EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8081/health',timeout=4).status==200 else 1)"

CMD ["python", "-m", "vlmkit.api"]
