# Продакшн Dockerfile для микросервиса Agent Toolkit (HTTP REST API & MCP Server)
FROM python:3.11-slim AS production

# Переменные окружения для Python, безопасности, браузеров и интеграций
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    AGENT_TOOLKIT_ENV=production \
    AGENT_TOOLKIT_MOCK_MODE=false \
    AGENT_TOOLKIT_ALLOW_DANGEROUS=false \
    AGENT_TOOLKIT_READ_ONLY=false \
    AGENT_TOOLKIT_API_PORT=8090 \
    AGENT_TOOLKIT_WORKSPACE=/var/lib/agent_toolkit/workspace \
    AGENT_TOOLKIT_CONFIG_PATH=/var/lib/agent_toolkit/workspace/toolkit_config.json \
    CHROME_BIN=/usr/bin/chromium \
    CHROMEDRIVER_BIN=/usr/bin/chromedriver \
    PLAYWRIGHT_BROWSERS_PATH=/usr/bin \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

# Установка системных зависимостей для всех 159 инструментов и 266 скилсов:
# - базовые утилиты и безопасность (ca-certificates, curl, wget, git, make, openssl, openssh-client)
# - сборочные инструменты и библиотеки (build-essential, procps, libssl-dev, libffi-dev)
# - базы данных (libpq-dev для PostgreSQL, default-mysql-client для MySQL, sqlite3)
# - работа с PDF и офисными документами (poppler-utils, pandoc)
# - аудио и мультимедиа TTS (ffmpeg, libsndfile1)
# - графика, САПР (OpenSCAD, xvfb), 3D STL и Vision AI (openscad, xvfb, fontconfig, fonts-dejavu-core, libgl1, libglib2.0-0, libsm6, libxext6, libxrender1, libgomp1)
# - безголовый браузер Chromium для веб-скрапинга, Playwright и Puppeteer (chromium, chromium-driver)
# - архиваторы и утилиты (unzip, zip, bzip2, tar, gzip, xz-utils, file, jq)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    wget \
    git \
    make \
    procps \
    build-essential \
    libssl-dev \
    libffi-dev \
    libpq-dev \
    default-mysql-client \
    sqlite3 \
    poppler-utils \
    pandoc \
    ffmpeg \
    libsndfile1 \
    openscad \
    xvfb \
    fontconfig \
    fonts-dejavu-core \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libgomp1 \
    unzip \
    zip \
    bzip2 \
    tar \
    gzip \
    xz-utils \
    openssl \
    openssh-client \
    file \
    jq \
    chromium \
    chromium-driver \
    && rm -rf /var/lib/apt/lists/*

# Создание непривилегированного пользователя для безопасности (Security Best Practice)
RUN groupadd -r agentgroup && useradd -r -g agentgroup -d /app -s /sbin/nologin agentuser

WORKDIR /app/agent_toolkit

# Копирование конфигурации проекта и исходного кода в /app/agent_toolkit
COPY . /app/agent_toolkit/

# Установка пакета с полным набором зависимостей для всех инструментов (API, Web, DB, PDF, Image, Office)
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .[all]

WORKDIR /app

# Создание и назначение прав на рабочую директорию песочницы
RUN mkdir -p /var/lib/agent_toolkit/workspace && \
    chown -R agentuser:agentgroup /var/lib/agent_toolkit /app

USER agentuser

EXPOSE 8090

# Проверка работоспособности контейнера в Kubernetes / Docker
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8090/health || exit 1

# Запуск HTTP REST API и MCP сервера по умолчанию
CMD ["python3", "-m", "agent_toolkit", "serve", "--host", "0.0.0.0", "--port", "8090"]
