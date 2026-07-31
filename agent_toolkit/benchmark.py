"""Бенчмарк релевантности и скорости умного поиска в реестре инструментов.

Измеряет метрики точности (Precision@1, Precision@3, MRR - Mean Reciprocal Rank)
и среднюю скорость ответа на 100 реалистичных агентских запросах.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from . import build_default_registry
from .core import ToolRegistry

# 100 тестовых запросов агента к реестру инструментов
BENCHMARK_QUERIES: list[tuple[str, str]] = [
    # Локальные файлы
    ("прочитать текстовый файл с номерами строк", "files.read_file"),
    ("read text file with line numbers", "files.read_file"),
    ("создать или записать новый файл", "files.write_file"),
    ("write file content", "files.write_file"),
    ("точечная замена строки в файле", "files.edit_file"),
    ("edit file replace substring", "files.edit_file"),
    ("посмотреть список файлов в директории", "files.list_dir"),
    ("найти файлы по шаблону glob", "files.find_files"),
    ("получить sha256 хеш и размер файла", "files.file_info"),
    ("удалить временный файл", "files.remove_file"),
    # Офисные документы и шаблоны
    ("создать документ ворд отчёт", "office.create_docx"),
    ("create word docx report", "office.create_docx"),
    ("создать таблицу excel из json", "office.create_xlsx"),
    ("read docx word text", "office.inspect_docx"),
    ("read excel xlsx cells table", "office.inspect_xlsx"),
    ("отрендерить markdown шаблон отчёта", "templates.render_markdown"),
    ("сгенерировать структурированный отчёт", "templates.render_report"),
    ("создать счёт на оплату invoice", "templates.create_invoice"),
    ("посмотреть список встроенных шаблонов", "templates.list_templates"),
    # QA сайтов
    ("проверить доступность сайта http статус", "site_qa.check_url"),
    ("check broken links html", "site_qa.check_links"),
    ("проверить wcag доступность заголовков h1", "site_qa.check_accessibility"),
    ("проверить seo метатеги title description", "site_qa.check_seo_meta"),
    # Базы данных SQL
    ("просмотреть схему бд sqlite таблицы", "sql.inspect_schema"),
    ("выполнить sql запрос select к базе данных", "sql.execute_query"),
    # PDF
    ("прочитать страницы pdf документа", "pdf.read_pages"),
    ("извлечь таблицу из pdf в markdown", "pdf.extract_tables"),
    # Git и код
    ("статус git репозитория", "git.status"),
    ("посмотреть diff изменений в коде", "git.diff"),
    ("история последних коммитов git", "git.log"),
    ("проверить синтаксис python кода linter", "code.run_linter"),
    ("запустить тесты pytest unittest", "code.run_tests"),
    # Песочница
    ("выполнить shell команду в песочнице bash", "shell.run_command"),
    ("запустить python сниппет код", "python.exec_snippet"),
    # Память и RAG
    ("сохранить факт в память агента", "memory.save_fact"),
    ("найти факт в памяти по ключевому слову", "memory.search_facts"),
    ("rag поиск фрагмента в базе знаний документации", "rag.query_kb"),
    # Планировщик Cron
    ("запланировать задачу cron по таймеру", "jobs.schedule_task"),
    ("список запланированных задач агента", "jobs.list_tasks"),
    ("выполнить созревшие задачи cron", "jobs.run_pending"),
    # HITL и субагенты
    ("задать уточняющий вопрос человеку оператору", "ask.human"),
    ("запросить разрешение на опасное действие у человека", "hitl.request_approval"),
    ("делегировать задачу субагенту специалисту", "agent.call_subagent"),
    ("список субагентов и их ролей", "agent.list_agents"),
    # Табличные данные и CSV
    ("прочитать csv файл таблицу", "data.read_csv"),
    ("записать csv файл на диск", "data.write_csv"),
    ("конвертировать json в yaml или csv", "data.convert_format"),
    ("агрегировать сумму sum по группе в таблице", "data.aggregate_table"),
    # DOM и RSS скрапер
    ("извлечь текст html по селектору css", "html.extract_by_selector"),
    ("разобрать rss ленту новостей xml", "scraper.parse_feed"),
    # Патчи и регулярки
    ("заменить текст по регулярному выражению regex", "text.regex_replace"),
    ("наложить git патч unified diff", "code.apply_patch"),
    # Аудит и телеметрия
    ("записать событие в журнал аудита", "audit.log_event"),
    ("учесть стоимость токенов usd в телеметрии", "telemetry.record_metrics"),
    # Криптография
    ("сгенерировать случайный uuidv4", "crypto.generate_uuid"),
    ("вычислить sha256 хеш строки", "crypto.hash_string"),
    ("проверить подпись hmac sha256 документа", "crypto.verify_signature"),
    # САПР CAD
    ("отрендерить openscad модель и габариты", "cad.render_openscad"),
    ("геометрический анализ stl меша объём и watertight", "cad.inspect_stl"),
    ("сгенерировать python скрипт freecad", "cad.freecad_script"),
    ("сгенерировать шестерню openscad", "cad.generate_gear"),
    ("сгенерировать корпус прибора openscad", "cad.generate_enclosure"),
    ("конвертировать stl в obj меш", "cad.convert_mesh_format"),
    ("рассчитать массу и момент инерции детали", "cad.calculate_mass_inertia"),
    ("сгенерировать антенну яги openscad", "cad.generate_yagi_openscad"),
    ("сгенерировать малошумный пропеллер openscad", "cad.generate_propeller_openscad"),
    ("отрендерить ракурсы openscad и логи echo", "cad.render_openscad_views"),
    # Физика и инженерия
    ("рассчитать напряжение и запас прочности балки", "physics.calc_strength"),
    ("рассчитать магнитную индукцию соленоида b", "physics.calc_em_field"),
    ("рассчитать длину диполя антенны mhz", "physics.calc_antenna"),
    ("аэродинамика число рейнольдса и сопротивление", "physics.calc_airflow"),
    ("акустика скорость звука и давление spl", "physics.calc_acoustics"),
    ("рассчитать усталостную долговечность s-n curve", "physics.calc_fatigue_life"),
    ("момент затяжки и усилие болта", "physics.calc_bolt_torque"),
    ("бюджет радиолинии rf link budget и потери fspl", "physics.calc_rf_link_budget"),
    ("волновое сопротивление коаксиального кабеля z0", "physics.calc_coaxial_cable"),
    ("расход вентилятора охлаждения cfm тепло", "physics.calc_fan_cooling"),
    ("потери давления в трубе паскаль", "physics.calc_pipe_pressure_drop"),
    ("звукоизоляция стены индекс rw децибел", "physics.calc_sound_barrier"),
    ("резонансная частота резонатора гельмгольца", "physics.calc_helmholtz_resonator"),
    ("коэффициент стоячей волны ксв vswr антенны", "physics.calc_antenna_vswr"),
    ("согласующая г цепь l-network антенна lc", "physics.calc_antenna_matching_network"),
    ("рассчитать размеры антенны уда яги dbi", "physics.calc_yagi_uda_antenna"),
    ("рассчитать микрополосковую патч антенну pcb", "physics.calc_patch_antenna"),
    ("тяга мощность и момент пропеллера bemt", "physics.calc_propeller_thrust_power"),
    ("акустический шум пропеллера bpf и рекомендации", "physics.calc_propeller_noise"),
    ("крутка и профиль малошумной лопасти пропеллера", "physics.calc_low_noise_blade_geometry"),
    # Интеграции MCP, почта, телеграм, MAX, S3, веб, HTTP, TTS, ERP
    ("список инструментов mcp сервера", "mcp.list_remote_tools"),
    ("вызвать удалённый инструмент mcp", "mcp.call_remote_tool"),
    ("отправить письмо по smtp", "smtp.send_email"),
    ("прочитать входящие письма imap", "smtp.read_emails"),
    ("отправить сообщение в max bot api", "max.send_message"),
    ("получить сообщения бота max", "max.get_updates"),
    ("отправить сообщение в telegram чат", "telegram.send_message"),
    ("чтение сообщений бота telegram", "telegram.get_updates"),
    ("список объектов в s3 бакете", "s3.list_objects"),
    ("загрузить файл в s3 бакет", "s3.upload_file"),
    ("скачать файл из s3 бакета", "s3.download_file"),
    ("удалить объект s3", "s3.delete_object"),
    ("получить ссылку url s3", "s3.get_url"),
    ("сгенерировать изображение по промпту ai", "image.generate"),
    ("уменьшить разрешение картинки vlm", "image.resize"),
    ("метаданные изображения sha256 формат", "image.get_metadata"),
    ("проверить доступность tcp сервиса порт", "deploy.check_service"),
    ("сгенерировать конфиг nginx proxy", "deploy.generate_nginx_config"),
    ("сгенерировать юнит файл systemd service", "deploy.generate_systemd_unit"),
    ("сгенерировать docker compose yml", "deploy.generate_docker_compose"),
    ("веб поиск в интернете по ключевым словам", "web.search"),
    ("скачать веб страницу по url текст html", "web.fetch_page"),
    ("поиск через duckduckgo в интернете", "web.search_duckduckgo"),
    ("поиск новостей в интернете через duckduckgo", "web.search_news"),
    ("получить мгновенный ответ из wikipedia duckduckgo", "web.search_duckduckgo_answers"),
    ("преобразовать веб страницу html в чистый markdown", "web.fetch_markdown"),
    ("извлечь все гиперссылки из html страницы", "web.extract_links"),
    ("извлечь таблицы из html в markdown или csv", "web.extract_tables_html"),
    ("извлечь метаданные и seo теги из html", "web.extract_metadata_html"),
    ("проверить правила robots.txt для url", "web.check_robots_txt"),
    ("разобрать карту сайта sitemap xml", "web.fetch_sitemap"),
    ("найти все веб формы на странице и поля ввода", "web.extract_forms"),
    ("отправить данные веб формы post запрос", "web.submit_form"),
    ("смоделировать и валидировать заполнение формы", "web.simulate_form_fill"),
    ("смоделировать шаги браузера автоматизация", "web.simulate_browser_action"),
    ("отправить rest http запрос get post", "http.request"),
    ("синтезировать речь из текста в аудиофайл tts", "tts.synthesize_speech"),
    ("запросить сущности 1с erp по odata api", "erp.fetch_odata"),
    # Базы данных, Excel, PLM и OData
    ("выполнить запрос postgres_execute к субд postgres", "db.postgres_execute"),
    ("выполнить запрос mysql_execute к субд mysql", "db.mysql_execute"),
    ("сгенерировать er_diagram диаграмму связей таблиц бд", "db.generate_er_diagram"),
    ("вычисление_формулы excel_formula_eval в ячейках xlsx", "data.excel_formula_eval"),
    ("создать_документ_1с и проведение_1с через odata post", "erp.post_odata_document"),
    ("создать базовая_линия требований baseline teamcenter", "tc.create_requirement_baseline"),
    ("сравнение_ревизий требований в teamcenter plm", "tc.compare_requirement_revisions"),
    # Веб и браузерная автоматизация
    ("запустить сессию playwright_session браузера headless", "web.playwright_session"),
    ("выполнить действие puppeteer_action в браузере", "web.puppeteer_action"),
    ("извлечь микроразметку schema_org jsonld из html", "web.extract_schema_org"),
    ("снять полноразмерный_скриншот страницы capture", "web.capture_full_screenshot"),
    ("управление куки и сессиями cookie_session_manager", "web.cookie_session_manager"),
    # Оркестрация, память и квоты
    ("проиндексировать документ в векторное_хранилище hnsw", "memory.vector_store_hnsw"),
    ("векторный_поиск семантический_поиск в hnsw", "memory.vector_search_hnsw"),
    ("параллельный_mapreduce запуск задач субагента", "agent.parallel_map_reduce"),
    ("установить квота_ресурсов и лимит_токенов агента", "policy.resource_quota_guard"),
    ("проверить_квоту расхода ресурсов перед вызовом", "policy.check_quota"),
    ("сбросить_квоту и лимиты ресурсов агента", "policy.reset_quota"),
    # Vision, ритейл и Workflows
    ("визуальный анализ картинки vlm", "vision.analyze_image"),
    ("классифицировать типы страниц pdf", "vision.classify_pdf_pages"),
    ("умный парсинг pdf счёта vlm", "vision.parse_pdf_vlm"),
    ("провести аудит полки по фото sos", "inventory.audit_shelf"),
    ("проверить наличие ценников на полке", "inventory.check_price_tags"),
    ("рассчитать долю полки sos процентов", "inventory.calculate_metrics"),
    ("запустить аудит сайта и создать отчёт", "workflow.audit_website"),
    ("отчёт по инвентаризации в word и excel", "workflow.create_inventory_report"),
]


@dataclass
class BenchmarkReport:
    total_queries: int
    precision_at_1: float
    precision_at_3: float
    mrr: float
    avg_latency_ms: float
    failed_queries: list[tuple[str, str, list[str]]]


def run_benchmark(registry: ToolRegistry | None = None) -> BenchmarkReport:
    """Запустить 100+ тестовых поисковых запросов и рассчитать метрики точности и скорости."""
    reg = registry or build_default_registry()
    total = len(BENCHMARK_QUERIES)
    top_1_hits = 0
    top_3_hits = 0
    reciprocal_rank_sum = 0.0
    total_time_ms = 0.0
    failed: list[tuple[str, str, list[str]]] = []

    for query, expected_tool in BENCHMARK_QUERIES:
        t0 = time.perf_counter()
        hits = reg.search(query, limit=3)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        total_time_ms += elapsed_ms

        found_names = [t.name for t, _ in hits]
        if found_names and found_names[0] == expected_tool:
            top_1_hits += 1
        if expected_tool in found_names:
            top_3_hits += 1
            rank = found_names.index(expected_tool) + 1
            reciprocal_rank_sum += 1.0 / rank
        else:
            failed.append((query, expected_tool, found_names))

    p1 = round((top_1_hits / total) * 100.0, 2)
    p3 = round((top_3_hits / total) * 100.0, 2)
    mrr = round(reciprocal_rank_sum / total, 4)
    avg_lat = round(total_time_ms / total, 3)

    return BenchmarkReport(
        total_queries=total,
        precision_at_1=p1,
        precision_at_3=p3,
        mrr=mrr,
        avg_latency_ms=avg_lat,
        failed_queries=failed,
    )
