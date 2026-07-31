"""API и HTTP-сервер для agent_toolkit (FastAPI + MCP REST/RPC).

Обеспечивает доступность реестра инструментов:
  1. По Python API (локальные вызовы и SDK ToolkitClient).
  2. По HTTP REST API (FastAPI эндпоинты /api/tools, /api/tools/search, /api/skills).
  3. По протоколу MCP (/api/mcp/rpc для JSON-RPC 2.0 клиентов).
  4. Продакшн-проверку работоспособности (Health check /health).
"""
from __future__ import annotations

import json
from typing import Any

from .core import (
    ArtifactStore,
    ToolError,
    ToolExecuteRequest,
    ToolExecuteResponse,
    ToolRegistry,
    ToolSearchRequest,
    ToolSearchResponse,
    Workspace,
)
from .integrations.mcp import MCPServer
from .local.cad import _MOCK_STL_CONTENT

try:
    from fastapi import Body, FastAPI, HTTPException, Query, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, Response
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False
    Request = Any  # type: ignore
    Response = Any  # type: ignore
    FastAPI = Any  # type: ignore
    HTTPException = Any  # type: ignore
    Query = Any  # type: ignore
    CORSMiddleware = Any  # type: ignore
    JSONResponse = Any  # type: ignore


class ToolkitClient:
    """Python API / SDK клиент для локального или удалённого реестра инструментов."""

    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self.registry = registry or ToolRegistry()

    def search(
        self,
        query: str = "",
        *,
        skill: str | None = None,
        attributes: dict[str, Any] | None = None,
        limit: int = 10,
        min_score: float = 0.1,
    ) -> list[dict[str, Any]]:
        """Умный поиск инструментов по запросу, скилсу и атрибутам."""
        hits = self.registry.search(
            query=query,
            skill=skill,
            attributes=attributes,
            limit=limit,
            min_score=min_score,
        )
        return [
            {"name": t.name, "description": t.description, "score": round(score, 2), "skills": t.skills}
            for t, score in hits
        ]

    def execute(self, tool_name: str, **kwargs: Any) -> Any:
        """Выполнить инструмент по имени."""
        return self.registry.execute(tool_name, **kwargs)

    def list_skills(self) -> dict[str, int]:
        """Получить список всех доступных скилсов и количество инструментов в каждом."""
        groups = self.registry.group_by_skill()
        return {skill: len(tools) for skill, tools in sorted(groups.items())}

    def get_tool_schema(self, tool_name: str) -> dict[str, Any] | None:
        """Получить схему инструмента в формате OpenAI."""
        t = self.registry.get(tool_name)
        return t.schema() if t else None


def create_api_app(
    registry: ToolRegistry,
    server_name: str = "agent-toolkit-api",
    workspace: Workspace | None = None,
):
    """Создать приложение FastAPI для развёртывания HTTP и MCP-сервера.

    Требует установки: pip install fastapi uvicorn
    """
    if not _FASTAPI_AVAILABLE:
        raise ImportError(
            "Для работы HTTP API требуется fastapi. Установите: pip install fastapi uvicorn"
        )

    ws = workspace or Workspace("/tmp/agent_toolkit_ws")
    artifact_store = ArtifactStore(ws)

    app = FastAPI(
        title="Agent Toolkit API & MCP Server",
        version="0.1.0",
        description="Умный инструментарий агентов: поиск, скилсы, атрибуты, MCP RPC, продакшн API",
    )

    # CORS для веб-интерфейсов и внешних клиентов
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    mcp_srv = MCPServer(registry=registry, server_name=server_name)

    @app.get("/health")
    @app.get("/api/health")
    def health_check() -> dict[str, Any]:
        """Проверка работоспособности сервиса (Health Check) в продакшне."""
        return {
            "status": "ok",
            "server": server_name,
            "version": "0.1.0",
            "tools_count": len(registry.list_tools()),
            "skills_count": len(registry.group_by_skill()),
        }

    @app.get("/")
    @app.get("/ui")
    @app.get("/api/tools/ui")
    def serve_webui():
        """Визуальный веб-интерфейс каталога инструментов (Web UI / Explorer)."""
        from fastapi.responses import HTMLResponse
        from .webui import get_webui_html
        return HTMLResponse(get_webui_html())

    @app.get("/api/tools")
    def list_tools(
        skill: str | None = Query(None, description="Фильтр по скилсу"),
        category: str | None = Query(None, description="Фильтр по категории"),
    ) -> dict[str, Any]:
        """Получить список всех инструментов со статусом включения (enabled)."""
        attrs = {"category": category} if category else None
        hits = registry.search(skill=skill, attributes=attrs, limit=500, min_score=0.0)
        tools_data = []
        for t, _ in hits:
            item = t.to_schema().to_dict()
            item["enabled"] = registry.is_enabled(t.name)
            tools_data.append(item)
        return {"total": len(tools_data), "tools": tools_data}

    @app.post("/api/tools/{name}/toggle")
    def toggle_tool(name: str) -> dict[str, Any]:
        """Переключить статус включения/отключения инструмента."""
        t = registry.get(name)
        if not t:
            raise HTTPException(status_code=404, detail=f"Инструмент {name!r} не найден")
        currently = registry.is_enabled(name)
        registry.set_enabled(name, not currently)
        return {"tool": name, "enabled": not currently, "success": True}

    @app.post("/api/tools/{name}/enable")
    def enable_tool(name: str) -> dict[str, Any]:
        """Включить инструмент."""
        if not registry.enable_tool(name):
            raise HTTPException(status_code=404, detail=f"Инструмент {name!r} не найден")
        return {"tool": name, "enabled": True, "success": True}

    @app.post("/api/tools/{name}/disable")
    def disable_tool(name: str) -> dict[str, Any]:
        """Отключить инструмент."""
        if not registry.disable_tool(name):
            raise HTTPException(status_code=404, detail=f"Инструмент {name!r} не найден")
        return {"tool": name, "enabled": False, "success": True}

    @app.get("/api/settings/integrations")
    def get_integrations_settings() -> dict[str, Any]:
        """Получить текущие настройки интеграций (SMTP почта, Telegram, S3, 1С/ERP, Teamcenter)."""
        from .config import settings as cfg_settings
        return cfg_settings.get_integrations_dict(mask_secrets=True)

    @app.post("/api/settings/integrations")
    async def update_integrations_settings(request: Request) -> dict[str, Any]:
        """Обновить реквизиты и настройки интеграций в системе на лету."""
        from .config import settings as cfg_settings
        try:
            body = await request.json()
        except Exception:
            body = {}
        updated = cfg_settings.update_integrations(body)
        return {"success": True, "integrations": updated}

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        """Получить текущие настройки реестра, политики безопасности и список профилей."""
        enabled_list = registry.list_tools(include_disabled=False)
        all_list = registry.list_tools(include_disabled=True)
        return {
            "total_tools": len(all_list),
            "enabled_tools": len(enabled_list),
            "disabled_tools": len(all_list) - len(enabled_list),
            "profiles": [
                {
                    "id": "default",
                    "label": "Полный доступ (Admin)",
                    "description": "Включить все доступные инструменты в системе",
                },
                {
                    "id": "readonly",
                    "label": "Безопасный Read-Only",
                    "description": "Отключить опасные действия и изменяющие данные инструменты",
                },
                {
                    "id": "data_analyst",
                    "label": "Аналитик данных (Data & Excel)",
                    "description": "Включить только SQL, таблицы, CSV и формулы Excel",
                },
                {
                    "id": "cad_engineer",
                    "label": "САПР и инженерия (CAD & Physics)",
                    "description": "Включить только OpenSCAD, FreeCAD, STL и расчёты сопромата",
                },
                {
                    "id": "web_qa",
                    "label": "Веб-аудит и браузер (Web & Playwright)",
                    "description": "Включить только веб-поиск, скрапинг, Playwright и SEO-аудит",
                },
            ],
        }

    @app.post("/api/profiles/apply")
    async def apply_profile(request: Request) -> dict[str, Any]:
        """Применить профиль настроек к реестру (включение/отключение групп инструментов)."""
        try:
            body = await request.json()
            prof_name = body.get("profile", "default")
        except Exception:
            prof_name = request.query_params.get("profile", "default")
        try:
            report = registry.apply_profile(prof_name)
            return {"success": True, "report": report}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/config/export")
    def export_config(
        format_type: str = Query("json", alias="format", description="Формат выгрузки: json или yaml")
    ):
        """Экспортировать текущую конфигурацию реестра (Configuration as Code)."""
        fmt = (format_type or "json").lower().strip()
        data_str = registry.export_config(fmt)
        mime = "application/x-yaml" if fmt == "yaml" else "application/json"
        ext = "yaml" if fmt == "yaml" else "json"
        return Response(
            content=data_str,
            media_type=mime,
            headers={"Content-Disposition": f'attachment; filename="toolkit_config.{ext}"'},
        )

    @app.post("/api/config/import")
    async def import_config(request: Request) -> dict[str, Any]:
        """Импортировать и применить конфигурацию реестра из JSON или YAML."""
        try:
            try:
                body = await request.json()
            except Exception:
                body = (await request.body()).decode("utf-8")
            res = registry.import_config(body)
            return {"success": True, "import_report": res}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Ошибка импорта конфигурации: {exc}") from exc

    @app.get("/api/analytics")
    def get_analytics(tool: str | None = Query(None, description="Имя конкретного инструмента")) -> dict[str, Any]:
        """Получить телеметрию и тепловую карту использования инструментов (Analytics & Heatmap)."""
        return registry.get_analytics(tool)

    @app.get("/api/ratelimits")
    def get_ratelimits() -> dict[str, Any]:
        """Получить список всех активных лимитов частоты вызовов (Per-Tool Rate Limiting)."""
        return {"total_limits": len(registry.list_rate_limits()), "rate_limits": registry.list_rate_limits()}

    @app.post("/api/ratelimits")
    async def set_ratelimit(request: Request) -> dict[str, Any]:
        """Установить индивидуальный лимит частоты вызовов для инструмента."""
        try:
            body = await request.json()
            tool_name = str(body.get("tool", "")).strip()
            max_c = int(body.get("max_calls", 5))
            win_s = int(body.get("window_seconds", 60))
            if not tool_name:
                raise ValueError("Параметр 'tool' обязателен")
            res = registry.set_rate_limit(tool_name, max_c, win_s)
            return {"success": True, "rate_limit": res}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Ошибка установки лимита частоты: {exc}") from exc

    @app.delete("/api/ratelimits/{tool_name}")
    def delete_ratelimit(tool_name: str) -> dict[str, Any]:
        """Сбросить лимит частоты вызовов для инструмента."""
        registry.reset_rate_limits(tool_name)
        return {"success": True, "deleted": tool_name}

    @app.get("/api/tools/search")
    def search_tools(
        query: str = Query("", description="Текстовый поисковый запрос"),
        skill: str | None = Query(None, description="Опциональный фильтр по скилсу"),
        limit: int = Query(10, description="Максимальное количество результатов"),
        min_score: float = Query(0.1, description="Минимальный порог релевантности"),
    ) -> dict[str, Any]:
        """Умный поиск инструментов по описанию, названию и синонимам."""
        hits = registry.search(
            query=query, skill=skill, limit=limit, min_score=min_score
        )
        results = [
            {
                "tool": t.to_schema().to_dict(),
                "score": round(score, 3),
            }
            for t, score in hits
        ]
        return {"query": query, "total_found": len(results), "results": results}

    @app.get("/api/tools/test-production")
    def test_production_get(
        disable_failed: bool = Query(False, description="Отключить инструменты с ошибками"),
        disable_unconfigured: bool = Query(False, description="Отключить инструменты без настроек"),
    ) -> dict[str, Any]:
        """Прогнать все инструменты на боевом сервере и вернуть диагностический статус с превью."""
        from .core.diagnostics import ProductionTester

        tester = ProductionTester(registry, ws)
        return tester.test_all(
            disable_failed=disable_failed,
            disable_unconfigured=disable_unconfigured,
        )

    @app.post("/api/tools/test-production")
    def test_production_post(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """Запустить прогон на боевой через POST-запрос с параметрами отключения."""
        from .core.diagnostics import ProductionTester

        tester = ProductionTester(registry, ws)
        disable_failed = bool(payload.get("disable_failed", False))
        disable_unconfigured = bool(payload.get("disable_unconfigured", False))
        return tester.test_all(
            disable_failed=disable_failed,
            disable_unconfigured=disable_unconfigured,
        )

    @app.get("/api/tools/{name}")
    def get_tool(name: str) -> dict[str, Any]:
        """Получить схему и метаданные конкретного инструмента."""
        t = registry.get(name)
        if not t:
            raise HTTPException(status_code=404, detail=f"Инструмент {name!r} не найден")
        return t.to_schema().to_dict()

    @app.post("/api/tools/{name}/execute")
    async def execute_tool(name: str, request: Request) -> dict[str, Any]:
        """Выполнить инструмент по имени с переданными JSON-аргументами."""
        t = registry.get(name)
        if not t:
            raise HTTPException(status_code=404, detail=f"Инструмент {name!r} не найден")
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            result = registry.execute(name, **body)
            return {"tool": name, "success": True, "result": result}
        except ToolError as err:
            return JSONResponse(
                status_code=200,
                content={"tool": name, "success": False, "error": str(err)},
            )
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=400,
                content={"tool": name, "success": False, "error": str(exc)},
            )

    @app.get("/api/skills")
    def list_skills() -> dict[str, Any]:
        """Получить список всех скилсов и количество инструментов в каждом."""
        groups = registry.group_by_skill()
        stats = [
            {"skill": sk, "count": len(tools), "tools": [t.name for t in tools]}
            for sk, tools in sorted(groups.items())
        ]
        return {"total_skills": len(stats), "skills": stats}

    @app.get("/api/groups/skills")
    def group_by_skills() -> dict[str, list[str]]:
        """Получить словарь {скилс: [список имён инструментов]}."""
        groups = registry.group_by_skill()
        return {sk: [t.name for t in tools] for sk, tools in sorted(groups.items())}

    @app.get("/api/groups/attributes")
    def group_by_attributes(
        key: str = Query("category", description="Ключ атрибута (category, read_only и др.)")
    ) -> dict[str, list[str]]:
        """Сгруппировать инструменты по значению указанного атрибута."""
        groups = registry.group_by_attribute(key)
        return {
            str(val): [t.name for t in tools]
            for val, tools in sorted(groups.items(), key=lambda x: str(x[0]))
        }

    @app.post("/api/mcp/rpc")
    async def mcp_rpc(request: Request) -> dict[str, Any]:
        """Эндпоинт для вызовов MCP-клиентов (JSON-RPC 2.0).

        Поддерживает: initialize, tools/list, tools/call.
        """
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Некорректный JSON-RPC") from exc
        return mcp_srv.handle_rpc(body)

    @app.get("/api/artifacts")
    def list_artifacts(
        tag: str | None = Query(None, description="Фильтр по тегу"),
        mime_type: str | None = Query(None, description="Фильтр по MIME-типу"),
    ) -> dict[str, Any]:
        """Получить список сохранённых в Workspace артефактов."""
        items = artifact_store.list(tag=tag, mime_type=mime_type)
        return {"total": len(items), "artifacts": [art.to_dict() for art in items]}

    @app.get("/api/artifacts/content/{name}")
    def get_artifact_content(name: str):
        """Получить бинарное или текстовое содержимое артефакта."""
        art = artifact_store.get(name)
        if not art:
            raise HTTPException(status_code=404, detail=f"Артефакт {name!r} не найден")
        try:
            p = ws.resolve(art.path)
            if p.exists():
                data = p.read_bytes()
                return Response(content=data, media_type=art.mime_type)
        except Exception:
            pass
        raise HTTPException(status_code=404, detail=f"Файл артефакта {name!r} недоступен")

    @app.delete("/api/artifacts/{name}")
    def delete_artifact(name: str) -> dict[str, Any]:
        """Удалить артефакт из индекса и диска."""
        ok = artifact_store.remove(name)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Артефакт {name!r} не найден")
        return {"success": True, "deleted": name}

    @app.post("/api/artifacts/seed_demo")
    def seed_demo_artifacts() -> dict[str, Any]:
        """Создать набор демонстрационных артефактов для галереи и 3D-вьювера."""
        # 1. STL 3D-меш
        artifact_store.save_text(
            "gear20.stl",
            _MOCK_STL_CONTENT,
            mime_type="model/stl",
            metadata={"tags": ["cad", "3d", "stl", "gear"], "title": "Параметрическая шестерня Z=20"},
        )
        # 2. Markdown отчёт
        report_md = (
            "# Протокол технического аудита сайта\n\n"
            "**Дата:** 2026-07-30 | **Статус:** Соответствует WCAG 2.1 AA\n\n"
            "## Основные метрики\n"
            "| Параметр | Значение | Оценка |\n"
            "| --- | --- | --- |\n"
            "| Доступность сервера | 200 OK (12 мс) | Отлично |\n"
            "| Доступность HTML (WCAG) | 100/100 | Пройдено |\n"
            "| Иерархия H1-H3 | Без нарушений | Пройдено |\n"
        )
        artifact_store.save_text(
            "website_audit.md",
            report_md,
            mime_type="text/markdown",
            metadata={"tags": ["report", "qa", "markdown"], "title": "Аудит доступности сайта"},
        )
        # 3. OpenSCAD код
        scad_code = (
            "// Параметрическая шестерня m=2, Z=20\n"
            "module spur_gear(m=2, z=20, h=10) {\n"
            "    difference() {\n"
            "        cylinder(h=h, r=(m*z+4)/2, $fn=60, center=true);\n"
            "        cylinder(h=h+2, r=3, $fn=30, center=true);\n"
            "    }\n"
            "}\n"
            "spur_gear();\n"
        )
        artifact_store.save_text(
            "gear20.scad",
            scad_code,
            mime_type="text/plain",
            metadata={"tags": ["cad", "openscad", "code"], "title": "Код модели OpenSCAD"},
        )
        # 4. Демо-изображение PNG (1x1)
        fake_png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
            b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        artifact_store.save_bytes(
            "gear_view_isometric.png",
            fake_png,
            mime_type="image/png",
            metadata={"tags": ["cad", "render", "image"], "title": "Изометрический ракурс шестерни"},
        )
        return {"success": True, "seeded_count": 4}

    return app
