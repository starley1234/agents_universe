"""Интеграция с объектным хранилищем S3 / MinIO.

Поддерживает работу через boto3 при наличии библиотеки или автономное
In-Memory хранилище (mock mode) для выполнения тестов.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core import Tool, ToolError, Workspace


@dataclass
class S3Config:
    endpoint_url: str = "https://s3.example.com"
    access_key: str = ""
    secret_key: str = ""
    region: str = "us-east-1"
    mock_mode: bool = True


class S3Service:
    """Сервис для работы с S3/MinIO или локальной заглушкой (потокобезопасный)."""

    def __init__(self, ws: Workspace, cfg: S3Config | None = None) -> None:
        self.ws = ws
        self.cfg = cfg or S3Config()
        self._lock = threading.RLock()
        # Mock-хранилище в памяти: bucket -> key -> data (bytes)
        self._mock_buckets: dict[str, dict[str, bytes]] = {
            "test-bucket": {
                "reports/summary.md": "# Сводный отчёт\nАудит успешно пройден.".encode(
                    "utf-8"
                ),
                "images/shelf.jpg": b"fake-jpg-binary-data-for-shelf-photo",
            }
        }

    def list_objects(self, bucket: str, prefix: str = "") -> str:
        with self._lock:
            if self.cfg.mock_mode:
                bk = self._mock_buckets.get(bucket, {})
                keys = [k for k in bk if k.startswith(prefix)]
                if not keys:
                    return f"(В бакете {bucket!r} по префиксу {prefix!r} объектов не найдено)"
                lines = [f"- {k} ({len(bk[k])} B)" for k in sorted(keys)]
                return f"Объекты в s3://{bucket}/{prefix}:\n" + "\n".join(lines)

            try:
                import boto3

                client = boto3.client("s3", endpoint_url=self.cfg.endpoint_url)
                resp = client.list_objects_v2(Bucket=bucket, Prefix=prefix)
                items = resp.get("Contents", [])
                lines = [
                    f"- {i['Key']} ({i['Size']} B)"
                    for i in items
                ]
                return (
                    f"Объекты в s3://{bucket}/{prefix}:\n" + "\n".join(lines)
                    if lines
                    else "(Объекты не найдены)"
                )
            except Exception as exc:
                raise ToolError(f"Ошибка S3 list_objects: {exc}") from exc

    def upload_file(self, local_path: str, bucket: str, object_key: str) -> str:
        with self._lock:
            p = self.ws.resolve(local_path)
            if not p.exists() or p.is_dir():
                raise ToolError(f"Локальный файл {local_path!r} не найден")

            try:
                data = p.read_bytes()
            except OSError as exc:
                raise ToolError(f"Не удалось прочитать {local_path!r}: {exc}") from exc

            if self.cfg.mock_mode:
                self._mock_buckets.setdefault(bucket, {})[object_key] = data
                return (
                    f"[MOCK S3] Файл {self.ws.relative(p)} "
                    f"загружен в s3://{bucket}/{object_key} ({len(data)} B)"
                )

            try:
                import boto3

                client = boto3.client("s3", endpoint_url=self.cfg.endpoint_url)
                client.upload_file(str(p), bucket, object_key)
                return f"Файл загружен в s3://{bucket}/{object_key}"
            except Exception as exc:
                raise ToolError(f"Ошибка загрузки в S3: {exc}") from exc

    def download_file(self, bucket: str, object_key: str, local_path: str) -> str:
        with self._lock:
            p = self.ws.resolve(local_path)
            p.parent.mkdir(parents=True, exist_ok=True)

            if self.cfg.mock_mode:
                bk = self._mock_buckets.get(bucket, {})
                if object_key not in bk:
                    raise ToolError(
                        f"Объект s3://{bucket}/{object_key} не найден в mock-хранилище"
                    )
                p.write_bytes(bk[object_key])
                return (
                    f"[MOCK S3] Объект s3://{bucket}/{object_key} "
                    f"скачан в {self.ws.relative(p)}"
                )

            try:
                import boto3

                client = boto3.client("s3", endpoint_url=self.cfg.endpoint_url)
                client.download_file(bucket, object_key, str(p))
                return f"Объект s3://{bucket}/{object_key} скачан в {self.ws.relative(p)}"
            except Exception as exc:
                raise ToolError(f"Ошибка скачивания из S3: {exc}") from exc

    def delete_object(self, bucket: str, object_key: str) -> str:
        with self._lock:
            if self.cfg.mock_mode:
                bk = self._mock_buckets.get(bucket, {})
                if object_key in bk:
                    del bk[object_key]
                    return f"[MOCK S3] Объект s3://{bucket}/{object_key} удалён"
                raise ToolError(f"Объект s3://{bucket}/{object_key} не найден")

            try:
                import boto3

                client = boto3.client("s3", endpoint_url=self.cfg.endpoint_url)
                client.delete_object(Bucket=bucket, Key=object_key)
                return f"Объект s3://{bucket}/{object_key} удалён"
            except Exception as exc:
                raise ToolError(f"Ошибка удаления из S3: {exc}") from exc

    def get_url(self, bucket: str, object_key: str) -> str:
        if self.cfg.mock_mode:
            return f"{self.cfg.endpoint_url}/{bucket}/{object_key}"
        return f"{self.cfg.endpoint_url}/{bucket}/{object_key}"


def build_s3_tools(ws: Workspace, service: S3Service | None = None) -> list[Tool]:
    """Собрать инструменты для работы с облачным S3 хранилищем."""
    s3 = service or S3Service(ws=ws)

    def list_objects(bucket: str, prefix: str = "") -> str:
        return s3.list_objects(bucket, prefix)

    def upload_file(local_path: str, bucket: str, object_key: str) -> str:
        return s3.upload_file(local_path, bucket, object_key)

    def download_file(bucket: str, object_key: str, local_path: str) -> str:
        return s3.download_file(bucket, object_key, local_path)

    def delete_object(bucket: str, object_key: str) -> str:
        return s3.delete_object(bucket, object_key)

    def get_url(bucket: str, object_key: str) -> str:
        return s3.get_url(bucket, object_key)

    return [
        Tool(
            name="s3.list_objects",
            description="Просмотреть список файлов (объектов) в S3 бакете.",
            parameters={
                "type": "object",
                "properties": {
                    "bucket": {"type": "string", "description": "Имя S3 бакета"},
                    "prefix": {
                        "type": "string",
                        "description": "Префикс пути/папки в бакете",
                    },
                },
                "required": ["bucket"],
            },
            fn=list_objects,
            skills=["s3", "storage", "cloud", "files", "integrations", "object_storage"],
            attributes={
                "category": "storage",
                "read_only": True,
                "dangerous": False,
                "requires_network": True,
                "resource_type": "s3_bucket",
                "speed": "medium",
                "tags": ["s3", "cloud", "storage", "list", "bucket"],
            },
            example='s3.list_objects(bucket="test-bucket", prefix="reports/")',
        ),
        Tool(
            name="s3.upload_file",
            description="Загрузить локальный файл из рабочей папки в S3 бакет.",
            parameters={
                "type": "object",
                "properties": {
                    "local_path": {
                        "type": "string",
                        "description": "Путь к файлу в рабочей области",
                    },
                    "bucket": {"type": "string", "description": "Имя S3 бакета"},
                    "object_key": {
                        "type": "string",
                        "description": "Ключ (имя) объекта в S3",
                    },
                },
                "required": ["local_path", "bucket", "object_key"],
            },
            fn=upload_file,
            skills=["s3", "storage", "cloud", "files", "integrations"],
            attributes={
                "category": "storage",
                "read_only": False,
                "dangerous": False,
                "requires_network": True,
                "resource_type": "s3_object",
                "speed": "medium",
                "tags": ["s3", "cloud", "storage", "upload", "bucket"],
            },
            example='s3.upload_file(local_path="report.docx", bucket="my-bucket", object_key="docs/report.docx")',
        ),
        Tool(
            name="s3.download_file",
            description="Скачать файл из S3 бакета в локальную рабочую папку.",
            parameters={
                "type": "object",
                "properties": {
                    "bucket": {"type": "string", "description": "Имя S3 бакета"},
                    "object_key": {
                        "type": "string",
                        "description": "Ключ (путь) объекта в S3",
                    },
                    "local_path": {
                        "type": "string",
                        "description": "Куда сохранить файл локально",
                    },
                },
                "required": ["bucket", "object_key", "local_path"],
            },
            fn=download_file,
            skills=["s3", "storage", "cloud", "files", "integrations"],
            attributes={
                "category": "storage",
                "read_only": True,
                "dangerous": False,
                "requires_network": True,
                "resource_type": "s3_object",
                "speed": "medium",
                "tags": ["s3", "cloud", "storage", "download", "bucket"],
            },
            example='s3.download_file(bucket="my-bucket", object_key="docs/report.docx", local_path="report.docx")',
        ),
        Tool(
            name="s3.delete_object",
            description="Удалить объект из S3 бакета. Опасная операция (dangerous=True).",
            parameters={
                "type": "object",
                "properties": {
                    "bucket": {"type": "string", "description": "Имя S3 бакета"},
                    "object_key": {
                        "type": "string",
                        "description": "Ключ объекта в S3",
                    },
                },
                "required": ["bucket", "object_key"],
            },
            fn=delete_object,
            skills=["s3", "storage", "cloud", "files", "integrations"],
            attributes={
                "category": "storage",
                "read_only": False,
                "dangerous": True,
                "requires_network": True,
                "resource_type": "s3_object",
                "speed": "medium",
                "tags": ["s3", "cloud", "storage", "delete", "bucket"],
            },
            example='s3.delete_object(bucket="my-bucket", object_key="old.txt")',
        ),
        Tool(
            name="s3.get_url",
            description="Получить публичный или presigned URL для объекта в S3.",
            parameters={
                "type": "object",
                "properties": {
                    "bucket": {"type": "string", "description": "Имя S3 бакета"},
                    "object_key": {
                        "type": "string",
                        "description": "Ключ объекта в S3",
                    },
                },
                "required": ["bucket", "object_key"],
            },
            fn=get_url,
            skills=["s3", "storage", "cloud", "integrations"],
            attributes={
                "category": "storage",
                "read_only": True,
                "dangerous": False,
                "requires_network": False,
                "resource_type": "s3_url",
                "speed": "fast",
                "tags": ["s3", "cloud", "url"],
            },
            example='s3.get_url(bucket="my-bucket", object_key="photo.jpg")',
        ),
    ]
