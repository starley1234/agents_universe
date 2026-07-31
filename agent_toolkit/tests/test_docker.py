"""Тесты Docker-адаптации для продакшна (Dockerfile, docker-compose.yml, .dockerignore)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.harness import check, section, summary


def run_tests() -> int:
    section("1. Конфигурация Docker для продакшна (Dockerfile, docker-compose.yml, .env.example)")
    root_dir = Path(__file__).resolve().parents[1]

    p_df = root_dir / "Dockerfile"
    check("Dockerfile существует в корне проекта", p_df.exists())
    df_text = p_df.read_text(encoding="utf-8")
    check("Dockerfile использует минимальный python:3.11-slim", "python:3.11-slim" in df_text)
    check("Dockerfile определяет непривилегированного пользователя USER agentuser", "USER agentuser" in df_text)
    check("Dockerfile содержит HEALTHCHECK проверку /health", "HEALTHCHECK" in df_text and "/health" in df_text)
    check("Dockerfile содержит CMD для запуска сервера на порту 8090", "8090" in df_text and "serve" in df_text)
    check("Dockerfile устанавливает Chromium и драйверы для браузера", "chromium" in df_text and "chromium-driver" in df_text)
    check("Dockerfile устанавливает клиенты СУБД (libpq-dev, default-mysql-client, sqlite3)", "libpq-dev" in df_text and "default-mysql-client" in df_text and "sqlite3" in df_text)
    check("Dockerfile устанавливает утилиты для PDF, аудио и графики (poppler-utils, ffmpeg)", "poppler-utils" in df_text and "ffmpeg" in df_text)
    check("Dockerfile устанавливает OpenSCAD и виртуальный фреймбуфер xvfb для 3D-рендеринга", "openscad" in df_text and "xvfb" in df_text)
    check("Dockerfile определяет ENV-переменные для Chromium и Playwright", "CHROME_BIN=/usr/bin/chromium" in df_text and "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1" in df_text)

    p_dc = root_dir / "docker-compose.yml"
    check("docker-compose.yml существует в корне проекта", p_dc.exists())
    dc_text = p_dc.read_text(encoding="utf-8")
    check("docker-compose.yml определяет сервис agent-toolkit", "agent-toolkit:" in dc_text)
    check("docker-compose.yml монтирует том для workspace", "agent_toolkit_workspace:/var/lib/agent_toolkit/workspace" in dc_text)
    check("docker-compose.yml настраивает healthcheck", "healthcheck:" in dc_text)
    check("docker-compose.yml устанавливает продакшн-окружение", "AGENT_TOOLKIT_ENV=production" in dc_text)
    check("docker-compose.yml передаёт настройки браузера и интеграций", "CHROME_BIN" in dc_text and "SMTP_HOST" in dc_text and "TELEGRAM_BOT_TOKEN" in dc_text)

    p_env = root_dir / ".env.example"
    check(".env.example существует и содержит примеры всех реквизитов", p_env.exists() and "TELEGRAM_BOT_TOKEN" in p_env.read_text(encoding="utf-8"))

    p_ign = root_dir / ".dockerignore"
    check(".dockerignore существует", p_ign.exists())
    ign_text = p_ign.read_text(encoding="utf-8")
    check(".dockerignore исключает .git и __pycache__", ".git" in ign_text and "__pycache__" in ign_text)

    return summary("Тесты продакшн Docker-конфигурации")


def test_docker_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())

