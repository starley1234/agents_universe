# ============================================================
# Agent Toolkit — Production Dockerfile
# HTTP REST API + MCP Gateway (SSE/Streamable HTTP)
# Based on Ubuntu 24.04 (proven CAD stack from stl_projects)
# ============================================================
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    AGENT_TOOLKIT_ENV=production \
    AGENT_TOOLKIT_MOCK_MODE=false \
    AGENT_TOOLKIT_ALLOW_DANGEROUS=false \
    AGENT_TOOLKIT_READ_ONLY=false \
    AGENT_TOOLKIT_API_PORT=8090 \
    AGENT_TOOLKIT_WORKSPACE=/var/lib/agent_toolkit/workspace \
    CHROME_BIN=/usr/bin/chromium-browser \
    CHROMEDRIVER_BIN=/usr/bin/chromedriver

# --- System dependencies ---
# software-properties-common + gnupg needed for FreeCAD PPA
RUN apt-get update && apt-get install -y --no-install-recommends \
    software-properties-common ca-certificates gnupg \
    && add-apt-repository --yes ppa:freecad-maintainers/freecad-stable \
    && apt-get update && apt-get install -y --no-install-recommends \
    # Core
    python3 python3-pip python3-venv \
    curl wget git make procps \
    build-essential libssl-dev libffi-dev \
    # Databases
    libpq-dev default-mysql-client sqlite3 \
    # PDF & documents
    poppler-utils pandoc \
    # Audio / TTS
    ffmpeg libsndfile1 \
    # PHP (для запуска PHP-скриптов из workspace)
    php-cli \
    # CAD: OpenSCAD + FreeCAD (from PPA) + xvfb
    openscad freecad xvfb mesa-utils \
    # FEA (optional): gmsh calculix-ccx
    gmsh calculix-ccx \
    # Graphics / fonts
    fonts-dejavu-core fontconfig libgl1 libglib2.0-0 \
    libsm6 libxext6 libxrender1 libgomp1 \
    # Archives
    unzip zip bzip2 tar gzip xz-utils \
    # Security & network
    openssl openssh-client \
    # Misc
    file jq \
    # Browser
    chromium-browser chromium-chromedriver \
    && rm -rf /var/lib/apt/lists/*

# --- Python venv (isolate from Ubuntu packages) ---
RUN python3 -m venv /opt/agent-toolkit-venv
ENV PATH="/opt/agent-toolkit-venv/bin:${PATH}"

# --- Non-root user ---
RUN groupadd -r agentgroup && useradd -r -g agentgroup -d /app -s /sbin/nologin agentuser

# --- Copy project ---
WORKDIR /app
COPY pyproject.toml README.md /app/
COPY __init__.py __main__.py adapters.py api.py benchmark.py cli.py config.py \
     monorepo_config.py webui.py mcp_gateway.py /app/agent_toolkit/
COPY core/ /app/agent_toolkit/core/
COPY local/ /app/agent_toolkit/local/
COPY integrations/ /app/agent_toolkit/integrations/
COPY vision/ /app/agent_toolkit/vision/
COPY workflows/ /app/agent_toolkit/workflows/

# --- Python dependencies ---
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .[all]

# --- Workspace ---
RUN mkdir -p /var/lib/agent_toolkit/workspace && \
    chown -R agentuser:agentgroup /var/lib/agent_toolkit /app

USER agentuser

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8090/health || exit 1

CMD ["python3", "-m", "agent_toolkit", "serve", "--host", "0.0.0.0", "--port", "8090"]
