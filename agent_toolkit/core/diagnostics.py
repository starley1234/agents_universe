"""Модуль автоматизированного тестирования и диагностики инструментов на боевом сервере (Production Testing & Diagnostics).

Позволяет:
  1) Запустить прогон всех зарегистрированных инструментов на боевом сервере (включая САПР/CAD, базы данных, веб и файлы);
  2) Классифицировать состояние каждого инструмента:
     * ✅ Работает (OK) + превью результатов выполнения (до 200 символов);
     * ⚠️ Требует настройки (REQUIRES_CONFIG) + подсказка, какие реквизиты, переменные .env или бинарники необходимы;
     * ❌ Ошибка (ERROR) — непредвиденный сбой;
  3) Автоматически отключать (disable) неработающие или ненастроенные инструменты в реестре;
  4) Сохранять обновлённую конфигурацию через Configuration as Code (IaC) в JSON.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .tool import Tool, ToolError, ToolRegistry
from .workspace import Workspace


@dataclass
class DiagnosticItem:
    """Результат диагностики отдельного инструмента."""
    name: str
    status: str  # "ok", "requires_config", "error"
    status_label: str  # "✅ Работает", "⚠️ Требует настройки", "❌ Ошибка"
    preview: str
    requires_config_hint: str | None = None
    duration_ms: float = 0.0
    disabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "status_label": self.status_label,
            "preview": self.preview,
            "requires_config_hint": self.requires_config_hint,
            "duration_ms": round(self.duration_ms, 2),
            "disabled": self.disabled,
        }


# Ключевые слова в тексте исключения, по которым сбой распознаётся как «Требует настройки», а не как баг
_CONFIG_KEYWORDS = [
    "не настроен",
    "требует настройки",
    "не установлен",
    "не найдена",
    "не найден",
    "не поддерживает",
    "unknown url",
    "server_url",
    "mcp",
    "remote",
    "api key",
    "api_key",
    "token",
    "psycopg2",
    "pymysql",
    "credentials",
    "реквизиты",
    "password",
    "отключён",
    "auth",
    "host",
    "port",
    "apikey",
    "secret",
    "permission",
    "connection_url",
    "odata",
    "teamcenter",
    "s3",
    "telegram",
    "smtp",
    "imap",
    "mail",
]


class ProductionTester:
    """Движок прогона инструментов на боевом сервере и фильтрации неработающих."""

    def __init__(self, registry: ToolRegistry, workspace: Workspace) -> None:
        self.registry = registry
        self.ws = workspace

    def _prepare_diagnostic_fixtures(self) -> None:
        """Создать безопасные тестовые файлы в Workspace для проверки читающих инструментов."""
        try:
            test_txt = self.ws.resolve("diag_test.txt")
            if not test_txt.exists():
                test_txt.write_text("Тестовый файл диагностики на боевом сервере\nСтрока 2\n", encoding="utf-8")
            # Создаём тестовый PNG файл (1x1 пиксель)
            test_png = self.ws.resolve("diag_img.png")
            if not test_png.exists():
                import struct, zlib
                def _make_png():
                    sig = b'\x89PNG\r\n\x1a\n'
                    ihdr_data = struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)
                    ihdr_crc = struct.pack('>I', zlib.crc32(b'IHDR' + ihdr_data) & 0xffffffff)
                    ihdr = struct.pack('>I', 13) + b'IHDR' + ihdr_data + ihdr_crc
                    raw = zlib.compress(b'\x00\xff\x00\x00')
                    idat_crc = struct.pack('>I', zlib.crc32(b'IDAT' + raw) & 0xffffffff)
                    idat = struct.pack('>I', len(raw)) + b'IDAT' + raw + idat_crc
                    iend_crc = struct.pack('>I', zlib.crc32(b'IEND') & 0xffffffff)
                    iend = struct.pack('>I', 0) + b'IEND' + iend_crc
                    return sig + ihdr + idat + iend
                test_png.write_bytes(_make_png())

            test_scad = self.ws.resolve("diag_cube.scad")
            if not test_scad.exists():
                test_scad.write_text("// Диагностический куб OpenSCAD\ncube([10, 10, 10], center=true);\n", encoding="utf-8")

            test_stl = self.ws.resolve("diag_cube.stl")
            if not test_stl.exists():
                test_stl.write_text(
                    "solid DIAG_CUBE\n"
                    "  facet normal 0 0 1\n"
                    "    outer loop\n"
                    "      vertex 0.0 0.0 0.0\n"
                    "      vertex 10.0 0.0 0.0\n"
                    "      vertex 10.0 10.0 10.0\n"
                    "    endloop\n"
                    "  endfacet\n"
                    "  facet normal 0 0 1\n"
                    "    outer loop\n"
                    "      vertex 0.0 0.0 0.0\n"
                    "      vertex 10.0 10.0 10.0\n"
                    "      vertex 0.0 10.0 10.0\n"
                    "    endloop\n"
                    "  endfacet\n"
                    "endsolid DIAG_CUBE\n",
                    encoding="utf-8",
                )

            test_csv = self.ws.resolve("diag.csv")
            if not test_csv.exists():
                test_csv.write_text("id,name,value\n1,Alpha,10\n2,Beta,20\n", encoding="utf-8")

            mock_img = (
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
                b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
                b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            test_img = self.ws.resolve("diag_img.jpg")
            if not test_img.exists():
                test_img.write_bytes(mock_img)
            test_shelf = self.ws.resolve("diag_shelf.jpg")
            if not test_shelf.exists():
                test_shelf.write_bytes(mock_img)
        except Exception:
            pass

    def _get_diagnostic_inputs(self, tool: Tool) -> dict[str, Any]:
        """Получить безопасные входные аргументы для проверки инструмента."""
        inputs_map: dict[str, dict[str, Any]] = {
            "files.read_file": {"path": "diag_test.txt"},
            "files.write_file": {"path": "diag_test.txt", "content": "Тестовый файл диагностики"},
            "files.edit_file": {"path": "diag_test.txt", "old_text": "Тестовый", "new_text": "Проверенный"},
            "files.list_dir": {"path": "."},
            "files.file_info": {"path": "diag_test.txt"},
            "files.find_files": {"pattern": "*.txt"},
            "files.remove_file": {"path": "diag_test.txt"},
            "files.create_archive": {"archive_path": "diag.zip", "files_json": "[\"*.txt\"]"},
            "files.extract_archive": {"archive_path": "diag.zip", "target_dir": "diag_ext"},
            "files.sync_directories": {"source_dir": "diag_ext", "target_dir": "diag_sync"},
            "files.compare_files": {"path1": "diag_test.txt", "path2": "diag_test.txt"},
            "office.create_docx": {"path": "diag.docx", "title": "Diag Test", "content": "Контент"},
            "office.read_docx": {"path": "diag.docx"},
            "office.inspect_docx": {"path": "diag.docx"},
            "office.create_xlsx": {"path": "diag.xlsx", "sheet_name": "TestSheet", "headers_json": '["A", "B"]', "rows_json": '[["1", "2"]]'},
            "office.inspect_xlsx": {"path": "diag.xlsx"},
            "office.create_pptx": {"path": "diag.pptx", "title": "Diag Slide", "content_slides": [{"title": "S1", "body": "B1"}]},
            "office.read_pptx": {"path": "diag.pptx"},
            "office.convert_to_pdf": {"input_path": "diag.docx", "output_path": "diag.pdf"},
            "pdf.read_pages": {"path": "diag_mock.pdf"},
            "pdf.extract_tables": {"path": "diag_mock.pdf"},
            "sql.inspect_schema": {"db_path": "diag.db"},
            "sql.execute_query": {"db_path": "diag.db", "query": "SELECT 1 as val;"},
            "db.postgres_execute": {"connection_url": "mock://localhost:5432/test", "query": "SELECT 1;"},
            "db.mysql_execute": {"connection_url": "mock://localhost:3306/test", "query": "SELECT 1;"},
            "db.generate_er_diagram": {"connection_url": "mock://localhost:5432/test"},
            "cad.render_openscad": {"path": "diag_cube.scad"},
            "cad.render_openscad_views": {"path": "diag_cube.scad", "views_json": '["isometric"]', "export_stl": True},
            "cad.inspect_stl": {"path": "diag_cube.stl"},
            "cad.freecad_script": {"script_code": "# FreeCAD diagnostic script\nimport Part\n", "path": "diag_fc.py"},
            "cad.generate_gear": {"path": "diag_gear.scad", "module_mm": 2.0, "teeth_count": 20},
            "cad.generate_enclosure": {"path": "diag_box.scad", "width_mm": 80.0, "length_mm": 120.0, "height_mm": 40.0},
            "cad.convert_mesh_format": {"input_path": "diag_cube.stl", "output_path": "diag_cube.obj", "to_format": "obj"},
            "cad.calculate_mass_inertia": {"path": "diag_cube.stl", "material": "aluminum"},
            "cad.generate_yagi_openscad": {"path": "diag_yagi.scad", "freq_mhz": 433.92, "elements_count": 3},
            "cad.generate_propeller_openscad": {"path": "diag_fan.scad", "diameter_mm": 120.0, "blades_count": 5},
            "physics.calc_antenna_link_budget": {"freq_mhz": 433.92, "dist_km": 5.0},
            "physics.calc_link_fresnel": {"freq_mhz": 433.92, "dist_km": 5.0},
            "physics.calc_thermal_dissipation": {"power_w": 10.0, "r_th_jc": 1.5, "r_th_ca": 10.0},
            "physics.calc_aerodynamics_drag": {"velocity_m_s": 20.0, "area_m2": 0.5},
            "physics.calc_sound_pressure": {"power_w": 1.0, "dist_m": 1.0},
            "physics.calc_beam_deflection": {"length_mm": 1000.0, "load_n": 500.0},
            "physics.calc_low_noise_blade_geometry": {"diameter_mm": 120.0, "hub_diameter_mm": 30.0, "design_rpm": 1500.0, "design_velocity_m_s": 10.0},
            "web.search": {"query": "duckduckgo test"},
            "web.search_duckduckgo": {"query": "duckduckgo test"},
            "web.search_duckduckgo_answers": {"query": "python"},
            "web.search_news": {"query": "technology"},
            "web.fetch_page": {"url": "mock://example.com"},
            "web.fetch_markdown": {"url": "mock://example.com"},
            "web.extract_links": {"html_or_url": "<a href='https://example.com'>Link</a>"},
            "web.extract_tables_html": {"html_or_url": "<table><tr><td>1</td></tr></table>"},
            "web.extract_metadata_html": {"html_or_url": "<html><head><title>T</title></head></html>"},
            "web.extract_schema_org": {"html_or_url": "<html><head><script type='application/ld+json'>{\"@context\": \"https://schema.org\", \"@type\": \"Article\"}</script></head></html>"},
            "web.extract_forms": {"html_or_url": "<form><input name='test'></form>"},
            "web.check_robots_txt": {"url": "mock://example.com/robots.txt"},
            "web.fetch_sitemap": {"sitemap_url": "mock://example.com/sitemap.xml"},
            "web.cookie_session_manager": {"action": "list"},
            "web.playwright_session": {"url": "mock://example.com"},
            "web.puppeteer_action": {"url": "mock://example.com", "action": "screenshot"},
            "web.simulate_browser_action": {"actions_json": '[{"action": "goto", "url": "mock://example.com"}]'},
            "web.simulate_form_fill": {"form_html": "<form><input name='email'></form>", "values_json": '{"email": "test@example.com"}'},
            "web.submit_form": {"action_url": "mock://example.com/login", "method": "POST", "form_data_json": '{"user": "test"}'},
            "web.create_landing_page": {"path": "diag_landing.html", "hero_title": "Diag Title", "hero_subtitle": "Sub", "features_json": '[{"title": "F1", "desc": "D1"}]', "cta_text": "Click"},
            "web.build_static_site": {"site_dir": "static_dist", "title": "Test Site", "pages_json": '[{"filename": "index.html", "title": "Home", "content": "Welcome"}]'},
            "web.audit_site_seo_performance": {"html_content": "<html><head><title>T</title></head></html>"},
            "site_qa.check_url": {"url": "mock://example.com"},
            "site_qa.check_links": {"html_content": "<a href='https://example.com'>Link</a>", "base_url": "https://example.com"},
            "site_qa.check_accessibility": {"html_content": "<html><head><title>T</title></head><body><h1>H1</h1></body></html>"},
            "site_qa.check_seo_meta": {"html_content": "<html><head><title>T</title><meta name='description' content='desc'></head></html>"},
            "workflow.audit_website": {"url": "mock://example.com"},
            "workflow.create_inventory_report": {"image_path": "diag_shelf.jpg", "fmt": "docx"},
            "inventory.audit_shelf": {"image_path": "diag_shelf.jpg"},
            "inventory.check_price_tags": {"image_path": "diag_shelf.jpg"},
            "inventory.calculate_metrics": {"facings_json": '[{"brand": "OurBrand", "count": 40}, {"brand": "Competitor", "count": 60}]', "empty_slots": 2, "target_brand": "OurBrand"},
            "vision.analyze_image": {"image_path": "diag_img.jpg", "prompt": "test"},
            "vision.parse_pdf_vlm": {"path": "diag_mock.pdf"},
            "vision.classify_pdf_pages": {"path": "diag_mock.pdf"},
            "vision.extract_pdf_structured_vlm": {"path": "diag_mock.pdf"},
            "data.read_csv": {"path": "diag.csv"},
            "data.write_csv": {"path": "diag.csv", "headers_json": '["id", "name"]', "rows_json": '[[1, "test"]]'},
            "data.parse_json": {"content": '{"test": 123}'},
            "data.query_json": {"content": '{"items": [1, 2, 3]}', "query": "items"},
            "data.aggregate": {"numbers": [10.0, 20.0, 30.0]},
            "data.generate_report": {"title": "Diag Report", "data_items_json": '[{"metric": "A", "val": 10}]'},
            "data.excel_formula_eval": {"formula": "SUM(A1:A2)", "cells_json": '{"A1": 10, "A2": 20}'},
            "data.convert_format": {"data_str": '{"a": 1}', "from_fmt": "json", "to_fmt": "yaml"},
            "data.aggregate_table": {"rows_json": '[{"cat": "A", "val": 10}, {"cat": "A", "val": 20}]', "group_by": "cat", "agg_col": "val", "agg_func": "sum"},
            "crypto.generate_uuid": {},
            "crypto.create_signature": {"text": "production test document", "secret_key": "test-key"},
            "web.deep_search": {"query": "openscad cad modeling", "limit": 2},
            "web.extract_structured": {"html_or_url": "mock://example.com", "schema_json": '{"title": "h1", "price": ".price"}'},
            "cad.fea_static": {"stl_path": "diag_fea.stl", "material": "aluminum", "force_n_json": "[0, 0, -100]"},
            "site.create": {"topic": "Test Site", "site_name": "DiagSite"},
            "text.regex_replace": {"path": "diag_test.txt", "pattern": "Тестовый", "replacement": "Обновлённый"},
            "code.apply_patch": {"path": "diag_test.txt", "patch_content": "--- a/diag_test.txt\n+++ b/diag_test.txt\n@@ -1,2 +1,2 @@\n-Тестовый файл диагностики на боевом сервере\n+Обновлённый файл диагностики на боевом сервере\n Строка 2\n"},
            "image.resize": {"input_path": "diag_img.png", "output_path": "diag_img_resized.png", "max_dim": 100},
            "image.get_metadata": {"path": "diag_img.png"},
            "crypto.hash_text": {"text": "production test", "algorithm": "sha256"},
            "crypto.generate_keypair": {},
            "crypto.encrypt_text": {"text": "secret data"},
            "crypto.decrypt_text": {"text": "secret data"},
            "crypto.generate_jwt": {"payload_json": '{"user": "diag"}'},
            "crypto.verify_jwt": {"token": "mock.jwt.token"},
            "templates.list_templates": {},
            "templates.render_markdown": {"template_name": "report_summary", "variables_json": '{"title": "Diag", "date": "2026-07-31", "author": "Arena", "project": "agent_toolkit", "summary_text": "OK"}'},
            "templates.render_report": {"title": "Diag Report", "summary": "Test report", "sections_json": '[{"title": "S1", "content": "C1"}]', "metrics_json": '{"CPU": "10%"}'},
            "templates.create_invoice": {"invoice_number": "INV-001", "customer": "Test Customer", "items_json": '[{"name": "Test", "qty": 1, "price": 100}]', "path": "diag_inv.md"},
            "http.request": {"method": "GET", "url": "mock://example.com"},
            "tts.synthesize_speech": {"text": "Проверка синтеза речи", "filename": "diag_speech.mp3"},
            "media.convert_audio": {"input_path": "diag_speech.mp3", "output_path": "diag_speech.wav"},
            "media.extract_audio_meta": {"path": "diag_speech.mp3"},
            "media.generate_waveform_data": {"path": "diag_speech.mp3"},
            "policy.resource_quota_guard": {"max_tokens": 100000, "max_usd": 10.0, "max_tool_calls": 500},
            "policy.check_quota": {"add_tokens": 10, "add_usd": 0.001, "add_calls": 1},
            "policy.reset_quota": {"max_tokens": 100000, "max_usd": 10.0, "max_tool_calls": 500},
            "policy.set_tool_rate_limit": {"tool_name": "files.read_file", "max_calls": 100, "window_seconds": 60},
            "policy.list_rate_limits": {},
            "policy.reset_rate_limits": {"tool_name": "files.read_file"},
            "memory.save_fact": {"key": "diag_key", "value": "diag value"},
            "memory.search_facts": {"query": "Diag test"},
            "memory.vector_store_hnsw": {"doc_id": "doc1", "text": "Diag vector text"},
            "memory.vector_search_hnsw": {"query": "Diag vector", "top_k": 3},
            "agent.parallel_map_reduce": {"agent_name": "worker", "tasks_json": '["task1", "task2"]'},
            "agent.delegate_subtask": {"task": "diag task", "role": "analyst"},
            "audit.log_event": {"event_type": "DIAG", "action": "TEST"},
            "audit.read_log": {"limit": 10},
            "audit.verify_integrity": {},
            "audit.export_audit_report": {"output_path": "diag_audit.md"},
            "code.syntax_check": {"code": "def hello():\n    return 'world'\n"},
            "code.analyze_complexity": {"code": "def test():\n    pass\n"},
            "code.format_python": {"code": "def  test( x,y ):return x+y\n"},
            "code.lint_python": {"code": "import sys\nimport os\n"},
            "sandbox.execute_python": {"code": "result = 1 + 2"},
            "sandbox.execute_math": {"expression": "2 + 2 * 2"},
            "sandbox.run_unit_tests": {"code": "def test_ok(): assert 1 == 1\n"},
            "jobs.schedule_job": {"name": "diag_job", "command": "echo test", "schedule": "every_day"},
            "jobs.schedule_task": {"name": "diag_task", "interval_sec": 60, "tool_name": "files.read_file", "args_json": '{"path": "diag_test.txt"}'},
            "jobs.list_jobs": {},
            "jobs.run_job": {"job_id": "diag_job"},
            "jobs.cancel_job": {"job_id": "diag_job"},
            "hitl.request_approval": {"action": "diag_action", "reason": "test"},
            "hitl.check_approval_status": {"request_id": "req-1"},
            "hitl.approve_request": {"request_id": "req-1"},
            "hitl.reject_request": {"request_id": "req-1"},
            "subagent.spawn_subagent": {"role": "researcher", "task": "diag task"},
            "subagent.list_subagents": {},
            "subagent.get_subagent_result": {"subagent_id": "sub-1"},
            "scraper.scrape_url": {"url": "mock://example.com"},
            "scraper.extract_headings": {"url": "mock://example.com"},
            "scraper.extract_links": {"url": "mock://example.com"},
            "scraper.extract_table_data": {"url": "mock://example.com"},
            "scraper.parse_feed": {"feed_xml": "<rss><channel><title>T</title><item><title>I</title></item></channel></rss>"},
            "patch.create_diff": {"old_text": "hello\nworld", "new_text": "hello\nagent"},
            "patch.apply_diff": {"text": "hello\nworld", "diff": "--- \n+++ \n@@ -1,2 +1,2 @@\n hello\n-world\n+agent"},
            "patch.merge_text": {"base": "hello", "ours": "hello world", "theirs": "hello agent"},
            "mcp.call_remote_tool": {"name": "calc", "arguments_json": '{"a": 1}'},
            "deploy.generate_docker_compose": {"services_json": '{"web": {"image": "nginx"}}', "path": "docker-compose.test.yml"},
            "config.generate_monorepo_env": {"project": "agent_system", "path": "diag_test.env"},
            "config.list_monorepo_ports": {},
            "config.validate_env_settings": {"path": "diag_test.env"},
            "config.generate_docker_override": {"project": "agent_system", "path": "diag_override.yml"},
        }
        if tool.name in inputs_map:
            return inputs_map[tool.name]

        # Автогенерация аргументов для неизвестных инструментов по JSON Schema
        args: dict[str, Any] = {}
        schema = tool.parameters
        props = schema.get("properties", {})
        required_names = schema.get("required", [])

        for p_name in required_names:
            p_def = props.get(p_name, {})
            p_type = p_def.get("type", "string")
            if p_type == "string":
                args[p_name] = "test"
            elif p_type in ("number", "integer"):
                args[p_name] = 1
            elif p_type == "boolean":
                args[p_name] = True
            elif p_type == "array":
                args[p_name] = []
            elif p_type == "object":
                args[p_name] = {}
        return args

    def _classify_error(self, exc: Exception) -> tuple[str, str, str]:
        """Классифицировать ошибку как 'Требует настройки' или 'Ошибка'."""
        msg = str(exc)
        low_msg = msg.lower()
        for kw in _CONFIG_KEYWORDS:
            if kw in low_msg:
                hint = (
                    f"Требуется дополнительная настройка реквизитов или зависимостей: "
                    f"проверьте параметры .env, настройки Web UI или наличие бинарника ({kw})."
                )
                return "requires_config", "⚠️ Требует настройки", hint

        return "error", "❌ Ошибка", "Непредвиденная ошибка выполнения. Проверьте параметры и логи сервера."

    def test_all(
        self,
        disable_failed: bool = False,
        disable_unconfigured: bool = False,
        save_config_path: str | None = None,
    ) -> dict[str, Any]:
        """Прогнать все инструменты на боевом сервере, вернуть отчёт с превью и отключить проблемные."""
        self._prepare_diagnostic_fixtures()

        items: list[DiagnosticItem] = []
        working_count = 0
        requires_config_count = 0
        failed_count = 0
        disabled_count = 0

        all_tools = self.registry.list_tools(include_disabled=True)
        for tool in all_tools:
            t_start = time.perf_counter()
            inputs = self._get_diagnostic_inputs(tool)

            try:
                # Временно включаем инструмент для теста, если он был отключён
                was_enabled = self.registry.is_enabled(tool.name)
                if not was_enabled:
                    self.registry.enable_tool(tool.name)

                res = self.registry.execute(tool.name, **inputs)
                duration_ms = (time.perf_counter() - t_start) * 1000.0

                preview = str(res)
                if len(preview) > 200:
                    preview = preview[:197] + "..."

                items.append(
                    DiagnosticItem(
                        name=tool.name,
                        status="ok",
                        status_label="✅ Работает",
                        preview=preview,
                        requires_config_hint=None,
                        duration_ms=duration_ms,
                        disabled=False,
                    )
                )
                working_count += 1

            except Exception as exc:
                duration_ms = (time.perf_counter() - t_start) * 1000.0
                status_code, label, hint = self._classify_error(exc)
                preview = str(exc)
                if len(preview) > 200:
                    preview = preview[:197] + "..."

                if status_code == "requires_config":
                    requires_config_count += 1
                else:
                    failed_count += 1

                items.append(
                    DiagnosticItem(
                        name=tool.name,
                        status=status_code,
                        status_label=label,
                        preview=preview,
                        requires_config_hint=hint,
                        duration_ms=duration_ms,
                        disabled=False,
                    )
                )

        # Отключение проблемных инструментов, если запрошено пользователем
        for item in items:
            should_disable = False
            if disable_failed and item.status == "error":
                should_disable = True
            if disable_unconfigured and item.status == "requires_config":
                should_disable = True

            if should_disable:
                self.registry.disable_tool(item.name)
                item.disabled = True
                disabled_count += 1
            else:
                self.registry.enable_tool(item.name)
                item.disabled = False

        # Сохранение конфигурации в IaC JSON файл
        saved_path = None
        if (disable_failed or disable_unconfigured) and (disabled_count > 0 or save_config_path):
            cfg_path = Path(save_config_path) if save_config_path else Path(self.ws.root) / "toolkit_config.json"
            try:
                cfg_path.parent.mkdir(parents=True, exist_ok=True)
                export_data = self.registry.export_config("json")
                cfg_path.write_text(export_data, encoding="utf-8")
                saved_path = str(cfg_path)
            except Exception:
                pass

        return {
            "success": True,
            "summary": {
                "total_tested": len(items),
                "working": working_count,
                "requires_config": requires_config_count,
                "failed": failed_count,
                "disabled_count": disabled_count,
            },
            "results": [it.to_dict() for it in items],
            "config_saved_to": saved_path,
        }
