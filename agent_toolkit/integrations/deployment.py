"""Инструменты деплоя и администрирования: проверка сервисов, nginx, systemd, docker-compose."""
from __future__ import annotations

import json
import socket
from typing import Any

from ..core import Tool, ToolError, Workspace


def build_deployment_tools(ws: Workspace) -> list[Tool]:
    """Собрать инструменты развёртывания и конфигурирования сервисов."""

    def check_service(host: str = "localhost", port: int = 80, timeout: int = 2) -> str:
        if host in ("mock-host", "test-host", "mock.service"):
            return f"[MOCK DEPLOY] Сервис {host}:{port} активен (статус: OK)"

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            res = s.connect_ex((host, port))
            if res == 0:
                return f"Сервис {host}:{port} отвечает (порт ОТКРЫТ)"
            return f"Сервис {host}:{port} недоступен (код ошибки connect_ex: {res})"
        except OSError as exc:
            return f"Ошибка проверки {host}:{port} ({exc})"
        finally:
            s.close()

    def generate_nginx_config(
        server_name: str, upstream_port: int, path: str = "nginx.conf"
    ) -> str:
        cfg = (
            f"# Автоматически сгенерированный конфиг Nginx для {server_name}\n"
            f"server {{\n"
            f"    listen 80;\n"
            f"    server_name {server_name};\n\n"
            f"    location / {{\n"
            f"        proxy_pass http://127.0.0.1:{upstream_port};\n"
            f"        proxy_set_header Host $host;\n"
            f"        proxy_set_header X-Real-IP $remote_addr;\n"
            f"        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            f"    }}\n"
            f"}}\n"
        )
        p = ws.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(cfg, encoding="utf-8")
        return f"Конфигурация Nginx для {server_name} сохранена в {ws.relative(p)}"

    def generate_systemd_unit(
        service_name: str,
        exec_start: str,
        work_dir: str = "",
        path: str = "",
    ) -> str:
        w_dir_str = f"WorkingDirectory={work_dir}\n" if work_dir else ""
        unit = (
            f"[Unit]\n"
            f"Description={service_name} (Agent Toolkit Service)\n"
            f"After=network.target\n\n"
            f"[Service]\n"
            f"Type=simple\n"
            f"{w_dir_str}"
            f"ExecStart={exec_start}\n"
            f"Restart=always\n"
            f"RestartSec=5\n\n"
            f"[Install]\n"
            f"WantedBy=multi-user.target\n"
        )
        dest = path or f"{service_name}.service"
        p = ws.resolve(dest)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(unit, encoding="utf-8")
        return f"Юнит Systemd {service_name}.service сохранён в {ws.relative(p)}"

    def generate_docker_compose(
        services_json: str, path: str = "docker-compose.yml"
    ) -> str:
        try:
            data = json.loads(services_json) if services_json else {}
        except ValueError as exc:
            raise ToolError(f"Некорректный JSON конфигурации сервисов: {exc}") from exc

        lines = ["version: '3.8'", "services:"]
        for srv_name, srv_conf in data.items():
            lines.append(f"  {srv_name}:")
            image = srv_conf.get("image", "")
            if image:
                lines.append(f"    image: {image}")
            ports = srv_conf.get("ports", [])
            if ports:
                lines.append("    ports:")
                for port_item in ports:
                    lines.append(f"      - \"{port_item}\"")
        txt = "\n".join(lines) + "\n"
        p = ws.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(txt, encoding="utf-8")
        return f"Docker Compose конфигурация сохранена в {ws.relative(p)}"

    return [
        Tool(
            name="deploy.check_service",
            description="Проверить доступность TCP-сервиса (хост и порт).",
            parameters={
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Имя хоста/IP"},
                    "port": {"type": "integer", "description": "Порт (например, 80 или 8080)"},
                    "timeout": {
                        "type": "integer",
                        "description": "Таймаут подключения (сек)",
                    },
                },
            },
            fn=check_service,
            skills=["deployment", "devops", "system", "config", "monitoring"],
            attributes={
                "category": "devops",
                "read_only": True,
                "dangerous": False,
                "requires_network": True,
                "resource_type": "service",
                "speed": "fast",
                "tags": ["deploy", "check", "service", "tcp", "monitoring"],
            },
            example='deploy.check_service(host="localhost", port=8080)',
        ),
        Tool(
            name="deploy.generate_nginx_config",
            description="Сгенерировать конфигурационный файл Nginx reverse-proxy.",
            parameters={
                "type": "object",
                "properties": {
                    "server_name": {
                        "type": "string",
                        "description": "Доменное имя сервера",
                    },
                    "upstream_port": {
                        "type": "integer",
                        "description": "Локальный порт сервиса",
                    },
                    "path": {
                        "type": "string",
                        "description": "Куда сохранить конфиг",
                    },
                },
                "required": ["server_name", "upstream_port"],
            },
            fn=generate_nginx_config,
            skills=["deployment", "devops", "system", "config", "nginx"],
            attributes={
                "category": "devops",
                "read_only": False,
                "dangerous": False,
                "resource_type": "config",
                "speed": "fast",
                "tags": ["deploy", "nginx", "proxy", "config"],
            },
            example='deploy.generate_nginx_config(server_name="api.example.com", upstream_port=8080)',
        ),
        Tool(
            name="deploy.generate_systemd_unit",
            description="Сгенерировать файл службы (.service) для Systemd.",
            parameters={
                "type": "object",
                "properties": {
                    "service_name": {
                        "type": "string",
                        "description": "Имя службы",
                    },
                    "exec_start": {
                        "type": "string",
                        "description": "Команда запуска (ExecStart)",
                    },
                    "work_dir": {
                        "type": "string",
                        "description": "Рабочая директория",
                    },
                    "path": {
                        "type": "string",
                        "description": "Путь для сохранения .service",
                    },
                },
                "required": ["service_name", "exec_start"],
            },
            fn=generate_systemd_unit,
            skills=["deployment", "devops", "system", "config", "systemd"],
            attributes={
                "category": "devops",
                "read_only": False,
                "dangerous": False,
                "resource_type": "config",
                "speed": "fast",
                "tags": ["deploy", "systemd", "service", "unit"],
            },
            example='deploy.generate_systemd_unit(service_name="agent-api", exec_start="/usr/bin/python3 -m agent")',
        ),
        Tool(
            name="deploy.generate_docker_compose",
            description="Сгенерировать файл docker-compose.yml по JSON-конфигурации сервисов.",
            parameters={
                "type": "object",
                "properties": {
                    "services_json": {
                        "type": "string",
                        "description": 'JSON словарь сервисов (например, \'{"web": {"image": "nginx", "ports": ["80:80"]}}\')',
                    },
                    "path": {
                        "type": "string",
                        "description": "Куда сохранить yml",
                    },
                },
                "required": ["services_json"],
            },
            fn=generate_docker_compose,
            skills=["deployment", "devops", "system", "config", "docker"],
            attributes={
                "category": "devops",
                "read_only": False,
                "dangerous": False,
                "resource_type": "config",
                "speed": "fast",
                "tags": ["deploy", "docker", "compose", "config"],
            },
            example='deploy.generate_docker_compose(services_json=\'{"app": {"image": "my-app", "ports": ["8080:8080"]}}\')',
        ),
    ]
