"""Главная модель инструмента (Tool) и умный быстрый реестр (ToolRegistry).

Обеспечивает поиск инструмента не только по точному названию, но и по:
  * скилсам (skills);
  * атрибутам и характеристикам (attributes);
  * смыслу запроса / ключевым словам (smart score search с синонимами).
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from .schemas import ToolSchema

#: Встроенная таблица синонимов для умного поиска
SYNONYMS: dict[str, set[str]] = {
    "read": {"view", "cat", "inspect", "show", "open", "get", "читать", "прочитать", "чтение"},
    "write": {"save", "create", "put", "make", "set", "записать", "сохранить", "создать"},
    "file": {"files", "filesystem", "doc", "path", "faile", "файл", "файлы"},
    "email": {"mail", "smtp", "sendmail", "letter", "письмо", "почта", "inbox"},
    "invoice": {"bill", "receipt", "account", "create_invoice", "счёт", "инвойс", "оплата", "чек"},
    "edit_file": {"replace_line", "edit_file", "точечная_замена", "редактировать_файл"},
    "telegram": {"tg", "telegram_bot", "телеграм_бот"},
    "max": {"max_messenger", "max_chat", "max_bot", "макс_бот"},
    "office": {"word", "excel", "powerpoint", "офис", "офисные"},
    "docx": {"word", "doc", "document", "ворд", "документ_ворд"},
    "xlsx": {"excel", "sheet", "spreadsheet", "table", "эксель", "таблица_эксель"},
    "pptx": {"powerpoint", "presentation", "slide", "презентация"},
    "vision": {"image", "photo", "vlm", "picture", "img", "фото", "изображение", "картинка"},
    "inventory": {"shelf", "retail", "audit_shelf", "sos", "facings", "oos", "полка", "выкладка", "аудит_полки"},
    "qa": {"check_qa", "inspection_qa", "контроль_качества"},
    "website": {"web_site", "сайт", "веб_сайт"},
    "links": {"a_href", "broken_links", "url_links", "check_links", "ссылки", "ссылка", "линки"},
    "s3": {"bucket_s3", "cloud_storage", "minio", "облако_s3", "хранилище_s3"},
    "deploy": {"service_deploy", "systemd", "docker_compose", "nginx_config", "деплой"},
    "sql": {"database_sql", "sqlite", "схема_бд", "запрос_sql"},
    "pdf": {"read_pdf", "vlm_pdf", "пдф"},
    "vlm": {"vision_llm", "multimodal", "влм", "зрение_ии"},
    "ocr": {"scanned_text", "recognize_text", "распознавание_скана"},
    "git": {"vcs", "repo", "repository", "version_control", "гит", "репозиторий"},
    "code": {"linter", "test_runner", "pytest", "python_code", "проверка_кода"},
    "shell": {"cmd", "bash_command", "sandbox_shell", "terminal", "команда_шелл"},
    "memory": {"rag_kb", "fact_storage", "knowledge_base", "kb", "память_агента", "база_знаний"},
    "jobs": {"cron_jobs", "scheduler", "timer", "schedule", "cronjob", "планировщик", "таймер", "расписание"},
    "http": {"http_client", "rest_client", "curl", "http_request"},
    "search": {"lookup", "query_search", "web_search", "поиск_в_интернете"},
    "hitl": {"ask_human", "human_approval", "question_operator", "согласование_человек", "вопрос_оператору"},
    "agent": {"subagent", "delegate", "orchestrator", "multi_agent", "субагент", "делегировать"},
    "data": {"csv_table", "yaml_convert", "aggregate_table", "таблица_данных"},
    "html": {"css_selector", "dom_selector", "селектор_css"},
    "patch": {"unified_diff", "git_patch", "наложить_патч"},
    "audit": {"audit_log", "journal_event", "telemetry_metrics", "журнал_аудита"},
    "crypto": {"cryptography", "crypto_security", "криптография"},
    "uuid": {"uuidv4", "guid", "generate_uuid", "уид"},
    "hash": {"sha256", "md5", "sha1", "checksum", "digest", "хеш", "контрольная_сумма"},
    "signature": {"hmac", "hmac_sha256", "verify_signature", "подпись_hmac"},
    "tts": {"speech_audio", "synthesize_speech", "синтез_речи", "озвучка"},
    "erp": {"1c_odata", "enterprise_1c", "1с_предприятие", "ерп_1с"},
    "teamcenter": {"tc_plm", "requirements_tc", "тимцентр", "требования_plm"},
    "landing": {"landing_page", "hero", "cta", "create_landing_page", "посадочная_страница", "лендинг"},
    "vlm_pdf": {"extract_pdf_structured_vlm", "parse_pdf_vlm", "pdf_vlm", "structure_prompt", "структурированный_pdf"},
    "cad": {"cad_system", "сапр"},
    "views": {"camera_view", "perspective", "isometric_view", "ракурс", "камера"},
    "echo": {"echo_log", "stdout_log", "stderr_log", "логи_echo"},
    "webui": {"ui_explorer", "spa", "dashboard", "веб_интерфейс", "каталог_инструментов"},
    "benchmark": {"speed_benchmark", "precision", "recall", "mrr", "latency", "бенчмарк_поиска"},
    "adapter": {"langchain_adapter", "awos_adapter", "agent_system_adapter", "адаптер", "конвертер"},
    "physics": {"multiphysics", "engineering_calc", "инженерные_расчёты", "рассчитать", "расчёт", "calculate", "calc", "вычислить", "вычисление", "прочность", "напряжение", "stress"},
    "strength": {"safety_factor", "mechanical_stress", "deflection", "yield_strength", "calc_strength", "запас_прочности"},
    "electromagnetics": {"magnetic_induction", "solenoid_b", "calc_em_field", "магнитная_индукция", "соленоид"},
    "antennas": {"dipole_antenna", "rf_antenna", "calc_antenna", "антенна_диполь", "антенна", "антенну", "антенны", "антенн", "antenna", "yagi", "яги", "patch_antenna", "патч_антенна", "vswr", "ксв"},
    "airflow": {"aerodynamics", "reynolds_number", "drag", "calc_airflow", "аэродинамика", "число_рейнольдса"},
    "acoustics": {"sound_speed", "spl_pressure", "resonance_pipe", "calc_acoustics", "скорость_звука"},
    "vswr": {"ksv", "swr", "return_loss", "calc_antenna_vswr", "ксв", "стоячая_волна"},
    "yagi": {"yagi_uda", "directional_antenna", "calc_yagi_uda_antenna", "яги_уда", "направленная_антенна"},
    "patch_antenna": {"pcb_antenna", "microstrip_antenna", "calc_patch_antenna", "патч_антенна"},
    "propeller": {"impeller", "fan_rotor", "bemt_thrust", "calc_propeller_thrust_power", "пропеллер", "крыльчатка", "винт_bemt"},
    "fan_noise": {"acoustic_noise_fan", "propeller_noise", "calc_propeller_noise", "шум_винта"},
    "duckduckgo": {"ddg_search", "duckduckgo_search", "дакдакго_поиск"},
    "forms": {"form_extract", "submit_form", "simulate_form_fill", "веб_формы", "отправка_формы"},
    "sitemap": {"sitemap_xml", "robots_txt_rule", "check_robots_txt", "fetch_sitemap", "карта_сайта_xml"},
    "browser_auto": {"simulate_browser_action", "automation_steps", "автоматизация_браузера"},
    "web_table": {"extract_tables_html", "html_table_csv", "таблицы_html"},
    "web_meta": {"extract_metadata_html", "opengraph_seo", "метаданные_html"},
    "inspect_stl": {"watertight_stl", "объём_stl", "геометрический_stl"},
    "generate_gear": {"шестерню", "шестерня", "involute_gear", "generate_gear"},
    "generate_enclosure": {"enclosure_box", "корпус_прибора", "корпус", "прибора", "generate_enclosure"},
    "calc_coaxial_cable": {"коаксиального_кабеля", "z0_cable", "calc_coaxial_cable"},
    "calc_em_field": {"соленоида", "индукцию", "магнитную", "соленоид", "calc_em_field"},
    "calc_helmholtz_resonator": {"гельмгольца", "резонатора_гельмгольца", "calc_helmholtz_resonator"},
    "delete_object": {"delete_object", "удалить_объект_s3"},
    "get_url": {"presigned_url", "ссылку_url_s3", "публичный_url_s3"},
    "resize": {"уменьшить_разрешение", "уменьшить", "разрешение", "resize_image_vlm"},
    "classify_pdf_pages": {"классифицировать_типы", "типы_страниц_pdf"},
    "audit_website": {"workflow.audit_website", "запустить_аудит_сайта", "аудит_сайта"},
    "create_inventory_report": {"отчёт_по_инвентаризации", "инвентаризации_в_word", "inventory_report_workflow"},
    "log_event": {"журнал_аудита", "записать_событие", "audit.log_event"},
    "postgres_db": {"postgres_execute", "postgresql", "постгрес", "субд_postgres"},
    "mysql_db": {"mysql_execute", "mysql", "майскл", "субд_mysql"},
    "er_diagram": {"generate_er_diagram", "mermaid", "er_диаграмма", "диаграмма_связей"},
    "excel_formula": {"excel_formula_eval", "sum_formula", "формула_excel", "вычисление_формулы"},
    "odata_post": {"post_odata_document", "создать_документ_1с", "проведение_1с"},
    "tc_baseline": {"create_requirement_baseline", "baseline", "базовая_линия", "заморозка_ревизии"},
    "tc_diff": {"compare_requirement_revisions", "compare_revisions", "сравнение_ревизий"},
    "playwright": {"playwright_session", "playwright", "headless", "плейрайт"},
    "puppeteer": {"puppeteer_action", "puppeteer", "пуппетир"},
    "schema_org": {"extract_schema_org", "jsonld", "microdata", "микроразметка"},
    "full_screenshot": {"capture_full_screenshot", "полноразмерный_скриншот"},
    "cookie_jar": {"cookie_session_manager", "куки", "cookie_manager"},
    "vector_store": {"vector_store_hnsw", "hnsw", "векторное_хранилище", "индексация_hnsw"},
    "vector_search": {"vector_search_hnsw", "векторный_поиск", "семантический_поиск"},
    "mapreduce": {"parallel_map_reduce", "mapreduce", "параллельный_mapreduce"},
    "quota_guard": {"resource_quota_guard", "лимит_токенов", "квота_ресурсов"},
    "quota_check": {"check_quota", "проверить_квоту", "проверка_квоты"},
    "quota_reset": {"reset_quota", "сбросить_квоту", "сброс_квоты"},
}


class ToolError(Exception):
    """Ожидаемая ошибка инструмента: возвращается модели текстом без падения прогона."""


@dataclass
class Tool:
    """Модель инструмента в agent_toolkit."""

    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., Any]
    skills: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)
    dangerous: bool = False
    example: str = ""

    def __post_init__(self) -> None:
        if self.attributes.get("dangerous", False):
            self.dangerous = True
        elif self.dangerous:
            self.attributes["dangerous"] = True

        self._lower_name = self.name.lower()
        self._lower_desc = self.description.lower()
        self._lower_skills = {sk.lower() for sk in self.skills}
        self._lower_tags = {str(tg).lower() for tg in self.attributes.get("tags", [])}
        self._lower_category = str(self.attributes.get("category", "")).lower()

    def execute(self, **kwargs: Any) -> Any:
        """Выполнить инструмент с переданными аргументами."""
        return self.fn(**kwargs)

    def to_schema(self) -> ToolSchema:
        """Вернуть схему инструмента в формате ToolSchema."""
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
            skills=self.skills,
            attributes=self.attributes,
            dangerous=self.dangerous,
            example=self.example,
        )

    def schema(self) -> dict[str, Any]:
        """Вернуть описание для OpenAI Function Calling."""
        return self.to_schema().to_openai()

    def to_mcp_tool(self) -> dict[str, Any]:
        """Вернуть описание инструмента по спецификации MCP."""
        return self.to_schema().to_mcp()

    def match_score(self, query: str) -> float:
        """Рассчитать релевантность инструмента текстовому запросу.

        Используется умным поиском в ToolRegistry, если агент точно не знает
        имя или расположение инструмента.
        """
        if not query or not query.strip():
            return 0.0

        q_clean = query.strip().lower()
        tokens = set(re.findall(r"[a-zа-я0-9_-]+", q_clean))
        if not tokens:
            return 0.0

        # Расширение синонимами
        expanded_tokens = set(tokens)
        for tok in tokens:
            for syn_key, syn_set in SYNONYMS.items():
                if tok == syn_key or tok in syn_set:
                    expanded_tokens.add(syn_key)
                    expanded_tokens.update(syn_set)

        name_words = set(re.findall(r"[a-zа-я0-9_-]+", self._lower_name))

        score = 0.0
        # 1. Точное совпадение имени инструмента
        if q_clean == self._lower_name:
            return 25.0
        if q_clean in self._lower_name:
            score += 10.0

        # 2. Совпадение токенов с именем, скилсами, тегами и описанием
        for tok in expanded_tokens:
            if tok in name_words:
                score += 12.0
            elif tok in self._lower_name:
                score += 8.0
            if tok in self._lower_skills:
                score += 6.0
            elif any(tok in sk for sk in self._lower_skills):
                score += 2.5
            if tok in self._lower_tags:
                score += 15.0
            elif any(tok in tg for tg in self._lower_tags):
                score += 4.0
            if tok == self._lower_category:
                score += 2.0
            if tok in self._lower_desc:
                score += 2.5

        return score


class ToolRegistry:
    """Умный, быстрый реестр инструментов для агентов (потокобезопасный).

    Поддерживает:
      - мгновенный поиск инструмента по ключевым словам и синонимам;
      - группировку инструментов по скилсам (skills);
      - группировку и фильтрацию по характеристикам/атрибутам (attributes).
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._disabled_tools: set[str] = set()
        self._rate_limits: dict[str, tuple[int, int, list[float]]] = {}
        self._analytics: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def enable_tool(self, name: str) -> bool:
        """Включить инструмент в реестре."""
        with self._lock:
            if name in self._tools:
                self._disabled_tools.discard(name)
                return True
            return False

    def disable_tool(self, name: str) -> bool:
        """Отключить инструмент в реестре."""
        with self._lock:
            if name in self._tools:
                self._disabled_tools.add(name)
                return True
            return False

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """Установить статус включения/отключения инструмента."""
        return self.enable_tool(name) if enabled else self.disable_tool(name)

    def is_enabled(self, name: str) -> bool:
        """Проверить, включён ли инструмент в реестре."""
        with self._lock:
            return name in self._tools and name not in self._disabled_tools

    def apply_profile(self, profile_name: str) -> dict[str, Any]:
        """Применить профиль настроек к реестру (включение/отключение групп инструментов)."""
        p_name = (profile_name or "default").lower().strip()
        with self._lock:
            if p_name in ("default", "full_admin", "all"):
                self._disabled_tools.clear()
            elif p_name in ("readonly", "read_only", "safe"):
                for name, tool in self._tools.items():
                    if tool.dangerous or not tool.attributes.get("read_only", False):
                        self._disabled_tools.add(name)
                    else:
                        self._disabled_tools.discard(name)
            elif p_name in ("data_analyst", "data"):
                allowed_skills = {"data", "sql", "office", "pdf", "csv", "table", "analytics", "templates", "read"}
                for name, tool in self._tools.items():
                    if any(sk in allowed_skills for sk in tool.skills):
                        self._disabled_tools.discard(name)
                    else:
                        self._disabled_tools.add(name)
            elif p_name in ("cad_engineer", "cad", "physics"):
                allowed_skills = {"cad", "physics", "math", "openscad", "freecad", "stl", "3d", "files", "engineering_calc"}
                for name, tool in self._tools.items():
                    if any(sk in allowed_skills for sk in tool.skills):
                        self._disabled_tools.discard(name)
                    else:
                        self._disabled_tools.add(name)
            elif p_name in ("web_qa", "web", "qa"):
                allowed_skills = {"web", "qa", "accessibility", "seo", "scraping", "playwright", "browser", "robots", "sitemap"}
                for name, tool in self._tools.items():
                    if any(sk in allowed_skills for sk in tool.skills):
                        self._disabled_tools.discard(name)
                    else:
                        self._disabled_tools.add(name)
            else:
                raise ValueError(f"Неизвестный профиль настройки: {profile_name!r}")

            enabled_cnt = len(self._tools) - len(self._disabled_tools)
            return {
                "profile": p_name,
                "total_tools": len(self._tools),
                "enabled_count": enabled_cnt,
                "disabled_count": len(self._disabled_tools),
            }

    def list_tools_with_status(self) -> list[dict[str, Any]]:
        """Вернуть список всех инструментов со статусом включения."""
        with self._lock:
            result = []
            for name, tool in self._tools.items():
                item = tool.schema()
                item["enabled"] = name not in self._disabled_tools
                item["dangerous"] = tool.dangerous
                item["category"] = tool.attributes.get("category", "local")
                result.append(item)
            return result

    def register(self, tool: Tool) -> None:
        """Зарегистрировать инструмент в реестре."""
        with self._lock:
            self._tools[tool.name] = tool

    def add(self, tool: Tool) -> None:
        """Синоним register для удобства."""
        self.register(tool)

    def get(self, name: str) -> Tool | None:
        """Получить инструмент по точному имени."""
        with self._lock:
            return self._tools.get(name)

    def list_tools(self, *, include_disabled: bool = True) -> list[Tool]:
        """Вернуть все зарегистрированные инструменты (опционально только включённые)."""
        with self._lock:
            if include_disabled:
                return list(self._tools.values())
            return [t for name, t in self._tools.items() if name not in self._disabled_tools]

    def search(
        self,
        query: str = "",
        *,
        skill: str | None = None,
        attributes: dict[str, Any] | None = None,
        limit: int = 10,
        min_score: float = 0.1,
    ) -> list[tuple[Tool, float]]:
        """Умный поиск инструментов по запросу, скилсу и атрибутам."""
        with self._lock:
            results: list[tuple[Tool, float]] = []
            for tool in self._tools.values():
                if skill and skill not in tool.skills:
                    continue
                if attributes:
                    match_attr = True
                    for k, v in attributes.items():
                        if tool.attributes.get(k) != v:
                            match_attr = False
                            break
                    if not match_attr:
                        continue
                if not query.strip():
                    results.append((tool, 1.0))
                    continue
                score = tool.match_score(query)
                if score >= min_score:
                    results.append((tool, score))

            results.sort(key=lambda item: item[1], reverse=True)
            return results[:limit]

    def find_tool(
        self,
        query: str,
        *,
        skill: str | None = None,
        attributes: dict[str, Any] | None = None,
        min_score: float = 0.1,
    ) -> Tool | None:
        """Найти лучший инструмент по запросу или характеристикам."""
        with self._lock:
            hits = self.search(
                query=query,
                skill=skill,
                attributes=attributes,
                limit=1,
                min_score=min_score,
            )
            return hits[0][0] if hits else None

    def group_by_skill(self) -> dict[str, list[Tool]]:
        """Сгруппировать инструменты по их скилсам."""
        with self._lock:
            groups: dict[str, list[Tool]] = {}
            for tool in self._tools.values():
                for sk in tool.skills:
                    groups.setdefault(sk, []).append(tool)
            return groups

    def group_by_attribute(self, key: str) -> dict[Any, list[Tool]]:
        """Сгруппировать инструменты по значению указанного атрибута."""
        with self._lock:
            groups: dict[Any, list[Tool]] = {}
            for tool in self._tools.values():
                val = tool.attributes.get(key)
                groups.setdefault(val, []).append(tool)
            return groups

    def filter_by_skill(self, skill: str) -> list[Tool]:
        """Отфильтровать инструменты, содержащие указанный скилс."""
        with self._lock:
            return [t for t in self._tools.values() if skill in t.skills]

    def filter_by_attributes(self, **kwargs: Any) -> list[Tool]:
        """Отфильтровать инструменты по точному совпадению атрибутов."""
        with self._lock:
            results: list[Tool] = []
            for tool in self._tools.values():
                match = True
                for k, v in kwargs.items():
                    if tool.attributes.get(k) != v:
                        match = False
                        break
                if match:
                    results.append(tool)
            return results

    def set_rate_limit(self, name: str, max_calls: int, window_seconds: int) -> dict[str, Any]:
        """Установить индивидуальный лимит частоты вызовов для инструмента."""
        with self._lock:
            self._rate_limits[name] = (max(1, max_calls), max(1, window_seconds), [])
            return {"tool": name, "max_calls": max(1, max_calls), "window_seconds": max(1, window_seconds)}

    def get_rate_limit(self, name: str) -> dict[str, Any] | None:
        """Получить информацию о лимите частоты вызовов инструмента."""
        with self._lock:
            if name not in self._rate_limits:
                return None
            max_c, win_s, ts_list = self._rate_limits[name]
            import time
            now = time.time()
            active_ts = [t for t in ts_list if now - t <= win_s]
            return {"tool": name, "max_calls": max_c, "window_seconds": win_s, "current_calls": len(active_ts)}

    def list_rate_limits(self) -> dict[str, dict[str, Any]]:
        """Получить список всех установленных лимитов частоты вызовов."""
        with self._lock:
            res = {}
            for name in list(self._rate_limits.keys()):
                info = self.get_rate_limit(name)
                if info:
                    res[name] = info
            return res

    def reset_rate_limits(self, name: str | None = None) -> None:
        """Сбросить или удалить лимиты частоты вызовов."""
        with self._lock:
            if name:
                self._rate_limits.pop(name, None)
            else:
                self._rate_limits.clear()

    def get_analytics(self, name: str | None = None) -> dict[str, Any]:
        """Получить статистику и тепловую карту использования инструментов (Analytics & Heatmap)."""
        with self._lock:
            if name:
                st = self._analytics.get(name, {"calls": 0, "success": 0, "errors": 0, "total_time_ms": 0.0, "tokens": 0, "usd": 0.0})
                cnt = max(1, st["calls"])
                return {
                    "tool": name,
                    "calls": st["calls"],
                    "success": st["success"],
                    "errors": st["errors"],
                    "success_rate": round((st["success"] / cnt) * 100.0, 1) if st["calls"] > 0 else 100.0,
                    "avg_time_ms": round(st["total_time_ms"] / cnt, 2),
                    "tokens": st["tokens"],
                    "usd": round(st["usd"], 4),
                }

            total_calls = sum(v["calls"] for v in self._analytics.values())
            total_errors = sum(v["errors"] for v in self._analytics.values())
            tools_stats = []
            for tname in sorted(self._analytics.keys()):
                tools_stats.append(self.get_analytics(tname))
            tools_stats.sort(key=lambda x: x["calls"], reverse=True)
            return {
                "total_calls": total_calls,
                "total_errors": total_errors,
                "tools_count_tracked": len(tools_stats),
                "tools_analytics": tools_stats,
            }

    def export_config(self, format_type: str = "json") -> str:
        """Экспортировать текущую конфигурацию реестра в JSON или YAML (Configuration as Code)."""
        fmt = (format_type or "json").lower().strip()
        with self._lock:
            config_data = {
                "version": "0.1.0",
                "total_tools": len(self._tools),
                "enabled_tools": sorted(
                    [name for name in self._tools.keys() if name not in self._disabled_tools]
                ),
                "disabled_tools": sorted(list(self._disabled_tools)),
                "rate_limits": self.list_rate_limits(),
            }

            if fmt == "yaml":
                lines = ["version: '0.1.0'", f"total_tools: {len(self._tools)}", "enabled_tools:"]
                for t in config_data["enabled_tools"]:
                    lines.append(f"  - {t}")
                lines.append("disabled_tools:")
                for t in config_data["disabled_tools"]:
                    lines.append(f"  - {t}")
                lines.append("rate_limits:")
                for tname, rinfo in config_data["rate_limits"].items():
                    lines.append(f"  {tname}:")
                    lines.append(f"    max_calls: {rinfo['max_calls']}")
                    lines.append(f"    window_seconds: {rinfo['window_seconds']}")
                return "\n".join(lines)

            import json
            return json.dumps(config_data, ensure_ascii=False, indent=2)

    def import_config(self, config_data: str | dict[str, Any]) -> dict[str, Any]:
        """Импортировать и применить конфигурацию реестра из JSON или YAML (Configuration as Code)."""
        import json

        data_dict: dict[str, Any] = {}
        if isinstance(config_data, dict):
            data_dict = config_data
        else:
            txt = config_data.strip()
            if txt.startswith("{"):
                data_dict = json.loads(txt)
            else:
                in_disabled = False
                in_rate = False
                cur_rate_tool = ""
                for line in txt.splitlines():
                    lstr = line.strip()
                    if lstr.startswith("disabled_tools:"):
                        in_disabled = True
                        in_rate = False
                    elif lstr.startswith("enabled_tools:"):
                        in_disabled = False
                        in_rate = False
                    elif lstr.startswith("rate_limits:"):
                        in_rate = True
                        in_disabled = False
                    elif lstr.startswith("- ") and in_disabled:
                        tname = lstr[2:].strip()
                        data_dict.setdefault("disabled_tools", []).append(tname)
                    elif lstr.startswith("- "):
                        tname = lstr[2:].strip()
                        data_dict.setdefault("enabled_tools", []).append(tname)
                    elif in_rate and ":" in lstr and not lstr.startswith("max_calls") and not lstr.startswith("window_seconds"):
                        cur_rate_tool = lstr.split(":")[0].strip()
                        data_dict.setdefault("rate_limits", {})[cur_rate_tool] = {}
                    elif in_rate and cur_rate_tool and "max_calls:" in lstr:
                        data_dict["rate_limits"][cur_rate_tool]["max_calls"] = int(lstr.split(":", 1)[1].strip())
                    elif in_rate and cur_rate_tool and "window_seconds:" in lstr:
                        data_dict["rate_limits"][cur_rate_tool]["window_seconds"] = int(lstr.split(":", 1)[1].strip())

        with self._lock:
            enabled_list = data_dict.get("enabled_tools", [])
            disabled_list = data_dict.get("disabled_tools", [])
            if enabled_list or disabled_list:
                for tname in self._tools.keys():
                    if tname in disabled_list:
                        self._disabled_tools.add(tname)
                    elif enabled_list and tname not in enabled_list:
                        self._disabled_tools.add(tname)
                    else:
                        self._disabled_tools.discard(tname)

            rate_limits = data_dict.get("rate_limits", {})
            if isinstance(rate_limits, dict):
                self.reset_rate_limits()
                for tname, rinfo in rate_limits.items():
                    if isinstance(rinfo, dict):
                        mc = int(rinfo.get("max_calls", 10))
                        ws = int(rinfo.get("window_seconds", 60))
                        self.set_rate_limit(tname, max_calls=mc, window_seconds=ws)

            enabled_cnt = len(self._tools) - len(self._disabled_tools)
            return {
                "success": True,
                "version": data_dict.get("version", "0.1.0"),
                "total_tools": len(self._tools),
                "enabled_count": enabled_cnt,
                "disabled_count": len(self._disabled_tools),
                "rate_limits_count": len(self._rate_limits),
            }

    def execute(self, __tool_name__: str, **kwargs: Any) -> Any:
        """Выполнить инструмент по имени с учётом лимитов частоты и телеметрии."""
        import time
        t0 = time.perf_counter()
        now = time.time()

        with self._lock:
            tool = self._tools.get(__tool_name__)
            if not tool:
                raise KeyError(f"Инструмент с именем {__tool_name__!r} не зарегистрирован в реестре")
            if __tool_name__ in self._disabled_tools:
                raise ToolError(f"Инструмент {__tool_name__!r} отключён (disabled) в настройках реестра")

            if __tool_name__ in self._rate_limits:
                max_c, win_s, ts_list = self._rate_limits[__tool_name__]
                active_ts = [t for t in ts_list if now - t <= win_s]
                if len(active_ts) >= max_c:
                    raise ToolError(
                        f"ПРЕВЫШЕН ЛИМИТ ЧАСТОТЫ ВЫЗОВОВ (Rate Limit Exceeded) для {__tool_name__!r}: "
                        f"разрешено не более {max_c} вызовов за {win_s} с."
                    )
                active_ts.append(now)
                self._rate_limits[__tool_name__] = (max_c, win_s, active_ts)

        success = True
        try:
            res = tool.execute(**kwargs)
            return res
        except Exception:
            success = False
            raise
        finally:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            with self._lock:
                st = self._analytics.setdefault(
                    __tool_name__, {"calls": 0, "success": 0, "errors": 0, "total_time_ms": 0.0, "tokens": 0, "usd": 0.0}
                )
                st["calls"] += 1
                if success:
                    st["success"] += 1
                else:
                    st["errors"] += 1
                st["total_time_ms"] += elapsed_ms
                if __tool_name__ in ("web.search", "vision.analyze_image", "vision.parse_pdf_vlm", "tts.synthesize_speech"):
                    st["tokens"] += 150
                    st["usd"] += 0.0015
                elif "search" in __tool_name__ or "fetch" in __tool_name__:
                    st["tokens"] += 50
                    st["usd"] += 0.0005
                else:
                    st["tokens"] += 20
                    st["usd"] += 0.0001

    def to_openai_tools(self) -> list[dict[str, Any]]:
        """Получить список всех инструментов в формате OpenAI."""
        with self._lock:
            return [t.schema() for t in self._tools.values()]

    def to_mcp_tools(self) -> list[dict[str, Any]]:
        """Получить список всех инструментов в формате MCP tools/list."""
        with self._lock:
            return [t.to_mcp_tool() for t in self._tools.values()]
