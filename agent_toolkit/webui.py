"""Визуальный веб-интерфейс (Web UI / Explorer) для каталога инструментов и артефактов.

Предоставляет приложение SPA на HTML5 + CSS3 + Vanilla JS:
  1. Каталог инструментов: поиск, скилсы, переключение статуса (enable/disable), интерактивный конструктор запуска.
  2. Галерея артефактов и 3D-вьювер: просмотр отчётов, изображений и STL-мешей на Canvas.
  3. Настройки и политики: профили безопасности, импорт/экспорт конфигурации (IaC),
     контроль квот, аналитика и тепловая карта использования (Heatmap), ограничение частоты (Rate Limiting).
"""
from __future__ import annotations


def get_webui_html(title: str = "Agent Toolkit Explorer — Каталог, управление и аналитика") -> str:
    """Вернуть HTML-код визуального интерфейса каталога инструментов, управления и аналитики."""
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        :root {{
            --bg: #0f172a;
            --card-bg: #1e293b;
            --border: #334155;
            --accent: #3b82f6;
            --accent-hover: #2563eb;
            --text: #f8fafc;
            --text-muted: #94a3b8;
            --danger: #ef4444;
            --success: #22c55e;
            --tag-bg: #334155;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background-color: var(--bg);
            color: var(--text);
            line-height: 1.5;
            padding: 24px;
        }}
        header {{
            margin-bottom: 24px;
            border-bottom: 1px solid var(--border);
            padding-bottom: 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 16px;
        }}
        h1 {{ font-size: 24px; font-weight: 700; color: var(--text); }}
        .stats {{
            display: flex;
            gap: 12px;
            font-size: 14px;
            color: var(--text-muted);
            flex-wrap: wrap;
        }}
        .stats span {{
            background: var(--card-bg);
            padding: 6px 12px;
            border-radius: 6px;
            border: 1px solid var(--border);
        }}
        /* Вкладки */
        .tabs {{
            display: flex;
            gap: 8px;
            margin-bottom: 24px;
            border-bottom: 2px solid var(--border);
        }}
        .tab-btn {{
            background: none;
            border: none;
            color: var(--text-muted);
            padding: 12px 20px;
            font-size: 15px;
            font-weight: 600;
            cursor: pointer;
            border-bottom: 3px solid transparent;
            transition: color 0.15s, border-color 0.15s;
        }}
        .tab-btn.active {{
            color: var(--accent);
            border-bottom-color: var(--accent);
        }}
        .tab-content {{ display: none; }}
        .tab-content.active {{ display: block; }}
        /* Фильтры и элементы */
        .controls {{
            display: flex;
            gap: 12px;
            margin-bottom: 24px;
            flex-wrap: wrap;
        }}
        input[type="text"], input[type="number"], select {{
            background: var(--card-bg);
            border: 1px solid var(--border);
            color: var(--text);
            padding: 10px 14px;
            border-radius: 8px;
            font-size: 14px;
            outline: none;
        }}
        input[type="text"]:focus, input[type="number"]:focus, select:focus {{ border-color: var(--accent); }}
        #search-box, #art-search-box {{ flex: 1; min-width: 250px; }}
        .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
            gap: 16px;
        }}
        .card {{
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 16px;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            transition: transform 0.15s, border-color 0.15s;
            cursor: pointer;
        }}
        .card:hover {{
            transform: translateY(-2px);
            border-color: var(--accent);
        }}
        .card-header {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 8px;
        }}
        .tool-name, .art-name {{
            font-family: monospace;
            font-weight: 700;
            font-size: 15px;
            color: #60a5fa;
            word-break: break-all;
        }}
        .badge {{
            font-size: 11px;
            padding: 2px 6px;
            border-radius: 4px;
            font-weight: 600;
            text-transform: uppercase;
        }}
        .badge-danger {{ background: rgba(239, 68, 68, 0.2); color: #f87171; }}
        .badge-safe {{ background: rgba(34, 197, 94, 0.2); color: #4ade80; }}
        .badge-stl {{ background: rgba(168, 85, 247, 0.2); color: #c084fc; }}
        .badge-md {{ background: rgba(59, 130, 246, 0.2); color: #60a5fa; }}
        .badge-img {{ background: rgba(234, 179, 8, 0.2); color: #facc15; }}
        .tool-desc, .art-desc {{
            font-size: 13px;
            color: var(--text-muted);
            margin-bottom: 12px;
            flex-grow: 1;
        }}
        .tags {{
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
        }}
        .tag {{
            font-size: 11px;
            background: var(--tag-bg);
            color: #cbd5e1;
            padding: 2px 8px;
            border-radius: 4px;
        }}
        /* Модальные окна */
        .modal-overlay {{
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0, 0, 0, 0.75);
            display: none;
            justify-content: center;
            align-items: center;
            z-index: 1000;
            padding: 24px;
        }}
        .modal {{
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 12px;
            width: 100%;
            max-width: 800px;
            max-height: 90vh;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }}
        .modal-header {{
            padding: 16px 20px;
            border-bottom: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .modal-header h2 {{ font-family: monospace; font-size: 18px; color: #60a5fa; }}
        .close-btn {{
            background: none; border: none; color: var(--text-muted);
            font-size: 24px; cursor: pointer;
        }}
        .modal-body {{
            padding: 20px;
            overflow-y: auto;
            flex-grow: 1;
        }}
        .field-group {{ margin-bottom: 16px; }}
        label {{
            display: block; font-size: 13px; font-weight: 600;
            margin-bottom: 6px; color: var(--text-muted);
        }}
        textarea {{
            width: 100%; height: 100px;
            background: var(--bg); border: 1px solid var(--border);
            color: var(--text); border-radius: 6px;
            padding: 10px; font-family: monospace; font-size: 13px;
        }}
        pre {{
            background: var(--bg); padding: 12px; border-radius: 6px;
            font-size: 12px; overflow-x: auto; border: 1px solid var(--border);
            max-height: 350px;
        }}
        .modal-footer {{
            padding: 16px 20px;
            border-top: 1px solid var(--border);
            display: flex;
            justify-content: flex-end;
            gap: 12px;
        }}
        .btn {{
            background: var(--accent); color: white; border: none;
            padding: 8px 16px; border-radius: 6px; font-size: 14px;
            font-weight: 600; cursor: pointer; transition: background 0.15s;
        }}
        .btn:hover {{ background: var(--accent-hover); }}
        .btn-secondary {{ background: var(--tag-bg); color: var(--text); }}
        .btn-danger {{ background: var(--danger); color: white; }}
        /* 3D STL Canvas */
        #stl-canvas {{
            width: 100%;
            height: 380px;
            background: #090d16;
            border: 1px solid var(--border);
            border-radius: 8px;
            cursor: grab;
            display: none;
        }}
        #stl-canvas:active {{ cursor: grabbing; }}
    </style>
</head>
<body>
    <header>
        <div>
            <h1>{title}</h1>
            <p style="font-size: 14px; color: var(--text-muted);">Визуальный каталог, IaC-конфигурация, аналитика и 3D-вьювер</p>
        </div>
        <div class="stats">
            <span id="stat-tools">Инструментов: ...</span>
            <span id="stat-skills">Скилсов: ...</span>
            <span id="stat-arts">Артефактов: ...</span>
        </div>
    </header>

    <div class="tabs">
        <button class="tab-btn active" onclick="switchTab('tab-tools', this)">Каталог инструментов</button>
        <button class="tab-btn" onclick="switchTab('tab-artifacts', this)">Хранилище артефактов и 3D вьювер</button>
        <button class="tab-btn" onclick="switchTab('tab-settings', this)">⚙️ Настройки и политики (Settings)</button>
    </div>

    <!-- ВКЛАДКА 1: ИНСТРУМЕНТЫ -->
    <div id="tab-tools" class="tab-content active">
        <div class="controls">
            <input type="text" id="search-box" placeholder="Умный поиск (например: 'аудит полки', 'создать word', 'расчёт антенны')...">
            <select id="category-filter"><option value="">Все категории</option></select>
            <select id="skill-filter"><option value="">Все скилсы</option></select>
        </div>
        <div class="grid" id="tools-grid"></div>
    </div>

    <!-- ВКЛАДКА 2: АРТЕФАКТЫ -->
    <div id="tab-artifacts" class="tab-content">
        <div class="controls">
            <input type="text" id="art-search-box" placeholder="Поиск артефактов по имени или тегу...">
            <button class="btn" onclick="seedDemoArtifacts()">+ Создать демо-артефакты (STL 3D, MD, SCAD)</button>
        </div>
        <div class="grid" id="arts-grid"></div>
    </div>

    <!-- ВКЛАДКА 3: НАСТРОЙКИ, ПРОФИЛИ, АНАЛИТИКА, ЛИМИТЫ -->
    <div id="tab-settings" class="tab-content">
        <!-- 0. Прогон и диагностика на боевом сервере (Production Diagnostic Test) -->
        <div class="card" style="margin-bottom: 20px; border: 1px solid #3b82f6;">
            <div class="card-header">
                <div class="tool-name">🧪 Прогон и диагностика всех инструментов на боевой (Production Diagnostics)</div>
                <span class="badge badge-img">159 Tools</span>
            </div>
            <p class="tool-desc">
                Протестировать все зарегистрированные инструменты на реальном сервере, посмотреть превью результатов, выявить неработающие и отключить их в 1 клик:
            </p>
            <div style="display: flex; gap: 12px; flex-wrap: wrap; align-items: center; margin-bottom: 15px;">
                <button class="btn" style="background: #2563eb; color: #fff;" onclick="runProductionTest(false, false)">▶️ Запустить боевой прогон (без отключения)</button>
                <button class="btn btn-secondary" onclick="runProductionTest(true, true)">✕ Прогнать и автоматически отключить неработающие</button>
            </div>
            <div id="prod-test-summary" style="margin-bottom: 12px; font-weight: 600;"></div>
            <div id="prod-test-results-container" style="display: none; overflow-x: auto; max-height: 480px; border: 1px solid var(--border); border-radius: 8px;">
                <table style="width: 100%; border-collapse: collapse; font-size: 13px; text-align: left;" id="prod-test-table">
                    <thead>
                        <tr style="border-bottom: 1px solid var(--border); background: #1e293b; color: #60a5fa; position: sticky; top: 0;">
                            <th style="padding: 10px;">Инструмент</th>
                            <th style="padding: 10px;">Статус</th>
                            <th style="padding: 10px;">Превью результата / Подсказка</th>
                            <th style="padding: 10px;">Время (мс)</th>
                            <th style="padding: 10px;">Действие</th>
                        </tr>
                    </thead>
                    <tbody id="prod-test-tbody"></tbody>
                </table>
            </div>
        </div>

        <!-- 1. Профили и настройки -->
        <div class="card" style="margin-bottom: 20px;">
            <div class="card-header">
                <div class="tool-name">Управление профилями инструментов (Preset Profiles)</div>
                <span class="badge badge-safe">Security Policy</span>
            </div>
            <p class="tool-desc">Выберите готовый профиль для мгновенного включения/отключения групп инструментов под конкретную задачу агента:</p>
            <div class="grid" id="profiles-grid" style="grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));"></div>
            <div id="profile-msg" style="margin-top: 12px; font-weight: 600; color: #4ade80;"></div>
        </div>

        <!-- 2. Импорт и экспорт конфигурации (IaC) -->
        <div class="card" style="margin-bottom: 20px;">
            <div class="card-header">
                <div class="tool-name">Импорт и экспорт конфигурации (Configuration as Code)</div>
                <span class="badge badge-md">IaC / Config</span>
            </div>
            <p class="tool-desc">Скачайте текущий профиль (активные инструменты, лимиты, квоты) в файл toolkit_config.json/yaml или загрузите его для мгновенного применения:</p>
            <div style="display: flex; gap: 12px; flex-wrap: wrap;">
                <a class="btn btn-secondary" href="/api/config/export?format=json" download="toolkit_config.json" style="text-decoration: none;">📥 Скачать toolkit_config.json</a>
                <a class="btn btn-secondary" href="/api/config/export?format=yaml" download="toolkit_config.yaml" style="text-decoration: none;">📥 Скачать toolkit_config.yaml</a>
                <button class="btn" onclick="openImportModal()">📤 Загрузить конфигурацию JSON/YAML</button>
            </div>
        </div>

        <!-- 3. Телеметрия и тепловая карта использования (Heatmap) -->
        <div class="card" style="margin-bottom: 20px;">
            <div class="card-header">
                <div class="tool-name">Телеметрия и тепловая карта использования (Tool Usage Analytics & Heatmap)</div>
                <span class="badge badge-img">Telemetry</span>
            </div>
            <p class="tool-desc">Реальная статистика вызовов, расход токенов/USD и процент успешности (success_rate):</p>
            <div style="overflow-x: auto;">
                <table style="width: 100%; border-collapse: collapse; font-size: 13px; text-align: left;" id="analytics-table">
                    <thead>
                        <tr style="border-bottom: 1px solid var(--border); color: #60a5fa;">
                            <th style="padding: 8px;">Инструмент</th>
                            <th style="padding: 8px;">Вызовов</th>
                            <th style="padding: 8px;">Успешность (Success Rate)</th>
                            <th style="padding: 8px;">Ср. время (мс)</th>
                            <th style="padding: 8px;">Токены</th>
                            <th style="padding: 8px;">Стоимость USD</th>
                        </tr>
                    </thead>
                    <tbody id="analytics-tbody">
                        <tr><td colspan="6" style="padding: 8px; color: var(--text-muted);">Загрузка аналитики...</td></tr>
                    </tbody>
                </table>
            </div>
            <div style="margin-top: 12px;">
                <button class="btn btn-secondary" onclick="fetchAnalytics()">🔄 Обновить аналитику</button>
            </div>
        </div>

        <!-- 4. Ограничение частоты вызовов (Per-Tool Rate Limiting) -->
        <div class="card" style="margin-bottom: 20px;">
            <div class="card-header">
                <div class="tool-name">Ограничение частоты вызовов (Per-Tool Rate Limiting)</div>
                <span class="badge badge-danger">Rate Limit</span>
            </div>
            <p class="tool-desc">Задайте индивидуальный лимит частоты вызовов для конкретного инструмента для защиты от спама:</p>
            <div style="display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px;">
                <input type="text" id="rl-tool-name" placeholder="Имя инструмента ('web.search')" style="flex: 2; min-width: 180px;">
                <input type="number" id="rl-max-calls" placeholder="Макс. вызовов (5)" value="5" style="width: 110px;">
                <input type="number" id="rl-win-sec" placeholder="Окно в с. (60)" value="60" style="width: 110px;">
                <button class="btn" onclick="setRateLimitUi()">+ Установить лимит</button>
            </div>
            <div style="overflow-x: auto;">
                <table style="width: 100%; border-collapse: collapse; font-size: 13px; text-align: left;">
                    <thead>
                        <tr style="border-bottom: 1px solid var(--border); color: #60a5fa;">
                            <th style="padding: 8px;">Инструмент</th>
                            <th style="padding: 8px;">Лимит</th>
                            <th style="padding: 8px;">Окно (сек)</th>
                            <th style="padding: 8px;">Текущих вызовов</th>
                            <th style="padding: 8px;">Действие</th>
                        </tr>
                    </thead>
                    <tbody id="ratelimits-tbody">
                        <tr><td colspan="5" style="padding: 8px; color: var(--text-muted);">Индивидуальных лимитов не установлено</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- 5. Контроль квот ресурсов -->
        <div class="card" style="margin-bottom: 20px;">
            <div class="card-header">
                <div class="tool-name">Контроль квот ресурсов (Resource Quota Guard)</div>
                <span class="badge badge-stl">Quotas</span>
            </div>
            <p class="tool-desc">Мониторинг расхода токенов LLM, бюджета USD и ограничения вызовов для предотвращения зацикливания агентов.</p>
            <div class="stats" style="margin-bottom: 12px;">
                <span id="quota-tokens">Токены: 0 / 100000</span>
                <span id="quota-usd">Бюджет: $0.00 / $5.00</span>
                <span id="quota-calls">Вызовы: 0 / 50</span>
            </div>
            <div>
                <button class="btn btn-secondary" onclick="resetQuotasUi()">Сбросить квоты ресурсов</button>
            </div>
        </div>

        <!-- 6. Настройки и реквизиты интеграций (Почта, Telegram, S3, 1C) -->
        <div class="card">
            <div class="card-header">
                <div class="tool-name">Настройки и реквизиты интеграций (Credentials & Settings)</div>
                <span class="badge badge-safe">Integrations</span>
            </div>
            <p class="tool-desc">Управляйте реквизитами для отправки почты SMTP, ботов Telegram/MAX, облака S3 и 1С/ERP на лету из браузера:</p>
            <div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 12px; margin-bottom: 12px;">
                <div>
                    <label style="font-size: 11px; color: #60a5fa;">SMTP Хост (почта)</label>
                    <input type="text" id="int-smtp-host" placeholder="smtp.gmail.com" style="width: 100%;">
                </div>
                <div>
                    <label style="font-size: 11px; color: #60a5fa;">SMTP Порт</label>
                    <input type="number" id="int-smtp-port" placeholder="587" style="width: 100%;">
                </div>
                <div>
                    <label style="font-size: 11px; color: #60a5fa;">SMTP Пользователь</label>
                    <input type="text" id="int-smtp-user" placeholder="myagent@gmail.com" style="width: 100%;">
                </div>
                <div>
                    <label style="font-size: 11px; color: #60a5fa;">SMTP Пароль / Токен</label>
                    <input type="password" id="int-smtp-pass" placeholder="***" style="width: 100%; padding: 10px 14px; background: var(--card-bg); border: 1px solid var(--border); color: var(--text); border-radius: 8px;">
                </div>
                <div>
                    <label style="font-size: 11px; color: #60a5fa;">Telegram Bot Token</label>
                    <input type="password" id="int-tg-token" placeholder="123456:ABC-DEF..." style="width: 100%; padding: 10px 14px; background: var(--card-bg); border: 1px solid var(--border); color: var(--text); border-radius: 8px;">
                </div>
                <div>
                    <label style="font-size: 11px; color: #60a5fa;">S3 Endpoint URL</label>
                    <input type="text" id="int-s3-endpoint" placeholder="https://s3.storage.ru" style="width: 100%;">
                </div>
            </div>
            <div>
                <button class="btn" onclick="saveIntegrationsUi()">💾 Сохранить реквизиты интеграций</button>
                <span id="int-save-msg" style="margin-left: 12px; font-weight: 600; color: #4ade80;"></span>
            </div>
        </div>
    </div>

    <!-- Модальное окно инструмента (с Interactive Playground) -->
    <div class="modal-overlay" id="modal-overlay">
        <div class="modal">
            <div class="modal-header">
                <h2 id="modal-title">tool.name</h2>
                <button class="close-btn" onclick="closeModal('modal-overlay')">&times;</button>
            </div>
            <div class="modal-body">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                    <p id="modal-desc" style="font-size: 14px; color: var(--text-muted);"></p>
                    <div>
                        <button class="btn btn-secondary" id="mode-form-btn" style="padding: 4px 10px; font-size: 12px;" onclick="switchModalMode('form')">🔘 Форма</button>
                        <button class="btn btn-secondary" id="mode-json-btn" style="padding: 4px 10px; font-size: 12px;" onclick="switchModalMode('json')">📝 JSON</button>
                    </div>
                </div>
                <div class="field-group" id="form-builder-group">
                    <label>Конструктор параметров (Interactive Form):</label>
                    <div id="playground-form" style="background: var(--bg); padding: 12px; border-radius: 8px; border: 1px solid var(--border);"></div>
                </div>
                <div class="field-group" id="json-args-group" style="display: none;">
                    <label>Аргументы запуска (JSON):</label>
                    <textarea id="modal-args">{{}}</textarea>
                </div>
                <div class="field-group" id="result-group" style="display: none;">
                    <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 8px;">
                        <label style="margin-bottom: 0;">Результат выполнения:</label>
                        <span class="badge" id="exec-status-badge"></span>
                        <span class="badge badge-safe" id="exec-time-badge"></span>
                    </div>
                    <pre id="modal-result" style="white-space: pre-wrap;"></pre>
                </div>
            </div>
            <div class="modal-footer">
                <button class="btn btn-secondary" onclick="closeModal('modal-overlay')">Закрыть</button>
                <button class="btn" id="run-btn">Выполнить инструмент</button>
            </div>
        </div>
    </div>

    <!-- Модальное окно импорта конфигурации -->
    <div class="modal-overlay" id="import-modal-overlay">
        <div class="modal">
            <div class="modal-header">
                <h2>Импорт конфигурации (Configuration as Code)</h2>
                <button class="close-btn" onclick="closeModal('import-modal-overlay')">&times;</button>
            </div>
            <div class="modal-body">
                <p style="font-size: 13px; color: var(--text-muted); margin-bottom: 12px;">Вставьте содержимое toolkit_config.json или YAML для мгновенного применения лимитов и активных инструментов:</p>
                <textarea id="import-config-text" style="height: 250px;"></textarea>
                <div id="import-msg" style="margin-top: 12px; font-weight: 600; color: #4ade80;"></div>
            </div>
            <div class="modal-footer">
                <button class="btn btn-secondary" onclick="closeModal('import-modal-overlay')">Закрыть</button>
                <button class="btn" onclick="submitImportConfig()">Загрузить и применить</button>
            </div>
        </div>
    </div>

    <!-- Модальное окно артефакта -->
    <div class="modal-overlay" id="art-modal-overlay">
        <div class="modal">
            <div class="modal-header">
                <h2 id="art-modal-title">art.name</h2>
                <button class="close-btn" onclick="closeModal('art-modal-overlay')">&times;</button>
            </div>
            <div class="modal-body">
                <p id="art-modal-meta" style="margin-bottom: 16px; font-size: 13px; color: var(--text-muted);"></p>
                <canvas id="stl-canvas" width="700" height="380"></canvas>
                <div class="field-group" id="art-text-group">
                    <label>Содержимое документа:</label>
                    <pre id="art-modal-content" style="white-space: pre-wrap;"></pre>
                </div>
            </div>
            <div class="modal-footer">
                <button class="btn btn-danger" id="delete-art-btn">Удалить артефакт</button>
                <button class="btn btn-secondary" onclick="closeModal('art-modal-overlay')">Закрыть</button>
            </div>
        </div>
    </div>

    <script>
        let allTools = [];
        let allSkills = [];
        let allArtifacts = [];

        function switchTab(tabId, btn) {{
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            btn.classList.add('active');
            document.getElementById(tabId).classList.add('active');
            if (tabId === 'tab-artifacts') fetchArtifacts();
            if (tabId === 'tab-settings') {{
                fetchSettings();
                fetchAnalytics();
                fetchRateLimits();
                fetchIntegrationsUi();
            }}
        }}

        async function fetchCatalog() {{
            try {{
                const [toolsRes, skillsRes] = await Promise.all([
                    fetch('/api/tools'),
                    fetch('/api/skills')
                ]);
                const toolsData = await toolsRes.json();
                const skillsData = await skillsRes.json();

                allTools = toolsData.tools || [];
                allSkills = skillsData.skills || [];

                const enabledCnt = allTools.filter(t => t.enabled !== false).length;
                document.getElementById('stat-tools').textContent = `Инструментов: ${{enabledCnt}} / ${{allTools.length}} вкл.`;
                document.getElementById('stat-skills').textContent = `Скилсов: ${{allSkills.length}}`;

                populateFilters();
                renderTools(allTools);
            }} catch (err) {{
                console.error("Ошибка загрузки каталога:", err);
            }}
        }}

        async function fetchArtifacts() {{
            try {{
                const res = await fetch('/api/artifacts');
                const data = await res.json();
                allArtifacts = data.artifacts || [];
                document.getElementById('stat-arts').textContent = `Артефактов: ${{allArtifacts.length}}`;
                renderArtifacts(allArtifacts);
            }} catch (err) {{
                console.error("Ошибка загрузки артефактов:", err);
            }}
        }}

        async function seedDemoArtifacts() {{
            await fetch('/api/artifacts/seed_demo', {{ method: 'POST' }});
            await fetchArtifacts();
        }}

        function populateFilters() {{
            const catSelect = document.getElementById('category-filter');
            const categories = [...new Set(allTools.map(t => (t.attributes || {{}}).category).filter(Boolean))].sort();
            categories.forEach(cat => {{
                const opt = document.createElement('option');
                opt.value = cat; opt.textContent = cat.toUpperCase();
                catSelect.appendChild(opt);
            }});

            const skillSelect = document.getElementById('skill-filter');
            allSkills.forEach(sk => {{
                const opt = document.createElement('option');
                opt.value = sk.skill; opt.textContent = `${{sk.skill}} (${{sk.count}})`;
                skillSelect.appendChild(opt);
            }});
        }}

        function renderTools(tools) {{
            const grid = document.getElementById('tools-grid');
            grid.innerHTML = '';
            tools.forEach(tool => {{
                const card = document.createElement('div');
                card.className = 'card';
                const isDangerous = tool.dangerous || (tool.attributes || {{}}).dangerous;
                const badgeClass = isDangerous ? 'badge-danger' : 'badge-safe';
                const badgeText = isDangerous ? 'Dangerous' : 'Safe';
                const statusBtnClass = tool.enabled !== false ? 'btn-secondary' : 'btn-danger';
                const statusBtnText = tool.enabled !== false ? '✓ Включён' : '✕ Отключён';
                const tags = (tool.skills || []).slice(0, 5).map(s => `<span class="tag">${{s}}</span>`).join('');

                card.innerHTML = `
                    <div>
                        <div class="card-header">
                            <div class="tool-name">${{tool.name}}</div>
                            <span class="badge ${{badgeClass}}">${{badgeText}}</span>
                        </div>
                        <div class="tool-desc">${{tool.description}}</div>
                    </div>
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 12px; border-top: 1px solid var(--border); padding-top: 8px;">
                        <div class="tags">${{tags}}</div>
                        <button class="btn ${{statusBtnClass}}" style="padding: 4px 10px; font-size: 11px;" onclick="event.stopPropagation(); toggleToolStatus('${{tool.name}}')">${{statusBtnText}}</button>
                    </div>
                `;
                card.addEventListener('click', () => openToolModal(tool));
                grid.appendChild(card);
            }});
        }}

        function renderArtifacts(arts) {{
            const grid = document.getElementById('arts-grid');
            grid.innerHTML = '';
            arts.forEach(art => {{
                const card = document.createElement('div');
                card.className = 'card';
                let badgeClass = 'badge-md', badgeText = 'Doc';
                if (art.name.endsWith('.stl')) {{ badgeClass = 'badge-stl'; badgeText = 'STL 3D Mesh'; }}
                else if (art.name.endsWith('.png') || art.name.endsWith('.jpg')) {{ badgeClass = 'badge-img'; badgeText = 'Image'; }}

                const title = (art.metadata || {{}}).title || art.name;
                card.innerHTML = `
                    <div>
                        <div class="card-header">
                            <div class="art-name">${{art.name}}</div>
                            <span class="badge ${{badgeClass}}">${{badgeText}}</span>
                        </div>
                        <div class="art-desc">${{title}} (${{art.size}} байт)</div>
                    </div>
                    <div class="tags">
                        <span class="tag">${{art.mime_type}}</span>
                        ${{(art.metadata.tags || []).map(t => `<span class="tag">${{t}}</span>`).join('')}}
                    </div>
                `;
                card.addEventListener('click', () => openArtifactModal(art));
                grid.appendChild(card);
            }});
        }}

        let currentTool = null;
        function openToolModal(tool) {{
            currentTool = tool;
            document.getElementById('modal-title').textContent = tool.name;
            document.getElementById('modal-desc').textContent = tool.description;
            document.getElementById('modal-args').value = '{{}}';
            document.getElementById('result-group').style.display = 'none';
            renderPlaygroundForm(tool);
            switchModalMode('form');
            document.getElementById('modal-overlay').style.display = 'flex';
        }}

        function switchModalMode(mode) {{
            if (mode === 'form') {{
                document.getElementById('form-builder-group').style.display = 'block';
                document.getElementById('json-args-group').style.display = 'none';
            }} else {{
                document.getElementById('form-builder-group').style.display = 'none';
                document.getElementById('json-args-group').style.display = 'block';
            }}
        }}

        function renderPlaygroundForm(tool) {{
            const container = document.getElementById('playground-form');
            if (!container) return;
            container.innerHTML = '';
            const props = (tool.parameters || {{}}).properties || {{}};
            const required = (tool.parameters || {{}}).required || [];
            const names = Object.keys(props);
            if (names.length === 0) {{
                container.innerHTML = '<p style="font-size: 13px; color: var(--text-muted);">Инструмент не требует параметров запуска (вызывается без аргументов).</p>';
                return;
            }}
            names.forEach(pname => {{
                const pinfo = props[pname] || {{}};
                const isReq = required.includes(pname);
                const fieldDiv = document.createElement('div');
                fieldDiv.style.marginBottom = '12px';
                fieldDiv.innerHTML = `
                    <label style="font-size: 12px; color: #60a5fa;">${{pname}}${{isReq ? ' <span style="color:#ef4444">*</span>' : ''}} <span style="color:var(--text-muted); font-weight:normal;">(${{pinfo.type || 'string'}}): ${{pinfo.description || ''}}</span></label>
                    <input type="text" class="pg-input" data-param="${{pname}}" placeholder="${{pinfo.default || ''}}" style="width: 100%; padding: 8px; background: #0f172a; border: 1px solid var(--border); color: var(--text); border-radius: 6px;">
                `;
                container.appendChild(fieldDiv);
            }});
        }}

        let currentArt = null;
        async function openArtifactModal(art) {{
            currentArt = art;
            document.getElementById('art-modal-title').textContent = art.name;
            document.getElementById('art-modal-meta').textContent = `Путь: ${{art.path}} | Тип: ${{art.mime_type}} | Размер: ${{art.size}} байт`;

            const stlCanvas = document.getElementById('stl-canvas');
            const textGroup = document.getElementById('art-text-group');
            const textBox = document.getElementById('art-modal-content');

            const resp = await fetch(`/api/artifacts/content/${{art.name}}`);
            const text = await resp.text();

            if (art.name.endsWith('.stl')) {{
                textGroup.style.display = 'none';
                stlCanvas.style.display = 'block';
                render3dStl(text, stlCanvas);
            }} else {{
                stlCanvas.style.display = 'none';
                textGroup.style.display = 'block';
                textBox.textContent = text;
            }}

            document.getElementById('delete-art-btn').onclick = async () => {{
                await fetch(`/api/artifacts/${{art.name}}`, {{ method: 'DELETE' }});
                closeModal('art-modal-overlay');
                fetchArtifacts();
            }};
            document.getElementById('art-modal-overlay').style.display = 'flex';
        }}

        function closeModal(id) {{
            document.getElementById(id).style.display = 'none';
        }}

        /* 3D STL Изометрический рендерер на HTML5 Canvas */
        function render3dStl(stlText, canvas) {{
            const ctx = canvas.getContext('2d');
            const w = canvas.width, h = canvas.height;
            ctx.clearRect(0, 0, w, h);

            // Разбор ASCII STL треугольников
            const tris = [];
            const lines = stlText.split('\\n');
            let cur = [];
            for (let ln of lines) {{
                const s = ln.trim().toLowerCase();
                if (s.startsWith('vertex')) {{
                    const p = s.split(/\\s+/);
                    if (p.length >= 4) cur.push([parseFloat(p[1]), parseFloat(p[2]), parseFloat(p[3])]);
                    if (cur.length === 3) {{ tris.push(cur); cur = []; }}
                }}
            }}

            if (tris.length === 0) {{
                ctx.fillStyle = '#94a3b8'; ctx.fillText('Бинарный или пустой STL меш', 20, 30);
                return;
            }}

            // Вычисление центра и масштабирование под холст
            let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity, minZ = Infinity, maxZ = -Infinity;
            tris.forEach(tri => tri.forEach(([x, y, z]) => {{
                minX = Math.min(minX, x); maxX = Math.max(maxX, x);
                minY = Math.min(minY, y); maxY = Math.max(maxY, y);
                minZ = Math.min(minZ, z); maxZ = Math.max(maxZ, z);
            }}));
            const cx = (minX + maxX)/2, cy = (minY + maxY)/2, cz = (minZ + maxZ)/2;
            const maxDim = Math.max(maxX - minX, maxY - minY, maxZ - minZ, 1);
            const scale = Math.min(w, h) * 0.45 / maxDim;

            // Изометрическая проекция и отрисовка проволочного каркаса
            ctx.strokeStyle = '#38bdf8';
            ctx.lineWidth = 1.2;
            tris.forEach(tri => {{
                ctx.beginPath();
                tri.forEach(([x, y, z], idx) => {{
                    // Сдвиг к центру
                    const dx = (x - cx) * scale, dy = (y - cy) * scale, dz = (z - cz) * scale;
                    // Изометрия: X_iso = (dx - dy)*cos(30), Y_iso = (dx + dy)*sin(30) - dz
                    const px = w/2 + (dx - dy) * 0.866;
                    const py = h/2 + (dx + dy) * 0.5 - dz;
                    if (idx === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
                }});
                ctx.closePath();
                ctx.stroke();
            }});
        }}

        async function executeTool() {{
            if (!currentTool) return;
            const resBox = document.getElementById('modal-result');
            const resGroup = document.getElementById('result-group');
            const statBadge = document.getElementById('exec-status-badge');
            const timeBadge = document.getElementById('exec-time-badge');
            resGroup.style.display = 'block';
            resBox.textContent = 'Выполнение...';
            statBadge.className = 'badge badge-md'; statBadge.textContent = 'RUNNING';
            timeBadge.textContent = '';

            let args = {{}};
            const isJsonMode = document.getElementById('json-args-group').style.display !== 'none';
            if (isJsonMode) {{
                try {{
                    args = JSON.parse(document.getElementById('modal-args').value || '{{}}');
                }} catch (err) {{
                    resBox.textContent = `Ошибка JSON аргументов: ${{err.message}}`;
                    return;
                }}
            }} else {{
                document.querySelectorAll('.pg-input').forEach(inp => {{
                    const val = inp.value.trim();
                    if (val !== '') {{
                        args[inp.getAttribute('data-param')] = val;
                    }}
                }});
            }}

            const t0 = performance.now();
            try {{
                const resp = await fetch(`/api/tools/${{currentTool.name}}/execute`, {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify(args)
                }});
                const elapsed = Math.round((performance.now() - t0) * 10) / 10;
                const data = await resp.json();
                timeBadge.textContent = `⏱️ ${{elapsed}} мс`;
                if (data.success) {{
                    statBadge.className = 'badge badge-safe'; statBadge.textContent = '✅ 200 OK';
                }} else {{
                    statBadge.className = 'badge badge-danger'; statBadge.textContent = '❌ Ошибка';
                }}
                resBox.textContent = JSON.stringify(data, null, 2);
            }} catch (err) {{
                const elapsed = Math.round((performance.now() - t0) * 10) / 10;
                timeBadge.textContent = `⏱️ ${{elapsed}} мс`;
                statBadge.className = 'badge badge-danger'; statBadge.textContent = '❌ Сбой';
                resBox.textContent = `Ошибка: ${{err.message}}`;
            }}
        }}

        async function toggleToolStatus(name) {{
            try {{
                const resp = await fetch(`/api/tools/${{name}}/toggle`, {{ method: 'POST' }});
                await fetchCatalog();
            }} catch (err) {{
                console.error("Ошибка переключения инструмента:", err);
            }}
        }}

        async function runProductionTest(disableFailed, disableUnconf) {{
            const sumEl = document.getElementById('prod-test-summary');
            const containerEl = document.getElementById('prod-test-results-container');
            const tbodyEl = document.getElementById('prod-test-tbody');
            sumEl.innerHTML = '⏳ Выполняется прогон всех инструментов на боевом сервере, подождите...';
            containerEl.style.display = 'none';

            try {{
                const resp = await fetch(`/api/tools/test-production?disable_failed=${{disableFailed}}&disable_unconfigured=${{disableUnconf}}`);
                const data = await resp.json();
                const sm = data.summary;
                sumEl.innerHTML = `
                    <span style="color: #60a5fa;">Всего: <b>${{sm.total_tested}}</b></span> | 
                    <span style="color: #4ade80;">✅ Работает: <b>${{sm.working}}</b></span> | 
                    <span style="color: #fbbf24;">⚠️ Требует настройки: <b>${{sm.requires_config}}</b></span> | 
                    <span style="color: #f87171;">❌ Ошибок: <b>${{sm.failed}}</b></span> | 
                    <span style="color: #94a3b8;">✕ Отключено: <b>${{sm.disabled_count}}</b></span>
                    ${{data.config_saved_to ? ` | <span style="color: #34d399;">💾 Конфиг: ${{data.config_saved_to}}</span>` : ''}}
                `;

                tbodyEl.innerHTML = '';
                for (const item of data.results) {{
                    let statusBadge = '<span class="badge badge-safe">✅ Работает</span>';
                    if (item.status === 'requires_config') {{
                        statusBadge = '<span class="badge badge-warn">⚠️ Требует настройки</span>';
                    }} else if (item.status === 'error') {{
                        statusBadge = '<span class="badge badge-danger">❌ Ошибка</span>';
                    }}

                    let previewHtml = `<div style="font-family: monospace; font-size: 11px; white-space: pre-wrap; max-width: 520px;">${{item.preview}}</div>`;
                    if (item.requires_config_hint) {{
                        previewHtml += `<div style="margin-top: 4px; font-size: 11px; color: #fbbf24; font-weight: 600;">💡 ${{item.requires_config_hint}}</div>`;
                    }}

                    const btnText = item.disabled ? 'Включить' : 'Отключить';
                    const btnClass = item.disabled ? 'btn btn-secondary' : 'btn';

                    const tr = document.createElement('tr');
                    tr.style.borderBottom = '1px solid var(--border)';
                    tr.innerHTML = `
                        <td style="padding: 10px; font-weight: 600;">${{item.name}}</td>
                        <td style="padding: 10px;">${{statusBadge}}</td>
                        <td style="padding: 10px;">${{previewHtml}}</td>
                        <td style="padding: 10px; color: var(--text-muted);">${{item.duration_ms}}</td>
                        <td style="padding: 10px;">
                            <button class="${{btnClass}}" style="padding: 4px 8px; font-size: 11px;" onclick="toggleToolStatus('${{item.name}}')">${{btnText}}</button>
                        </td>
                    `;
                    tbodyEl.appendChild(tr);
                }}
                containerEl.style.display = 'block';
                await fetchCatalog();
            }} catch (err) {{
                sumEl.innerHTML = `<span style="color: #f87171;">Ошибка прогона на боевой: ${{err}}</span>`;
            }}
        }}

        async function fetchSettings() {{
            try {{
                const res = await fetch('/api/settings');
                const data = await res.json();
                const grid = document.getElementById('profiles-grid');
                if (!grid) return;
                grid.innerHTML = '';
                (data.profiles || []).forEach(p => {{
                    const pcard = document.createElement('div');
                    pcard.className = 'card';
                    pcard.style.background = '#0f172a';
                    pcard.innerHTML = `
                        <div class="tool-name">${{p.label}}</div>
                        <div class="tool-desc">${{p.description}}</div>
                        <button class="btn" style="align-self: flex-start; margin-top: 8px;" onclick="applyProfile('${{p.id}}', '${{p.label}}')">Применить профиль</button>
                    `;
                    grid.appendChild(pcard);
                }});
            }} catch (err) {{
                console.error("Ошибка загрузки настроек:", err);
            }}
        }}

        async function applyProfile(pid, plabel) {{
            try {{
                const resp = await fetch('/api/profiles/apply', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ profile: pid }})
                }});
                const res = await resp.json();
                const msg = document.getElementById('profile-msg');
                if (msg && res.report) {{
                    msg.textContent = `✓ Профиль '${{plabel}}' применён. Включено инструментов: ${{res.report.enabled_count}} из ${{res.report.total_tools}}`;
                }}
                await fetchCatalog();
            }} catch (err) {{
                console.error("Ошибка применения профиля:", err);
            }}
        }}

        async function resetQuotasUi() {{
            try {{
                await fetch('/api/tools/policy.reset_quota/execute', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ max_tokens: 100000, max_usd: 5.0, max_tool_calls: 50 }})
                }});
                alert("Счётчики квот сброшены!");
            }} catch (err) {{}}
        }}

        function openImportModal() {{
            document.getElementById('import-config-text').value = '';
            document.getElementById('import-msg').textContent = '';
            document.getElementById('import-modal-overlay').style.display = 'flex';
        }}

        async function submitImportConfig() {{
            const txt = document.getElementById('import-config-text').value;
            try {{
                const resp = await fetch('/api/config/import', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: txt
                }});
                const res = await resp.json();
                document.getElementById('import-msg').textContent = `✓ Конфигурация успешно загружена. Включено: ${{res.import_report.enabled_count}}, Лимитов: ${{res.import_report.rate_limits_count}}`;
                await fetchCatalog();
            }} catch (err) {{
                document.getElementById('import-msg').style.color = '#ef4444';
                document.getElementById('import-msg').textContent = `Ошибка: ${{err.message}}`;
            }}
        }}

        async function fetchAnalytics() {{
            try {{
                const res = await fetch('/api/analytics');
                const data = await res.json();
                const tbody = document.getElementById('analytics-tbody');
                if (!tbody) return;
                const list = data.tools_analytics || [];
                if (list.length === 0) {{
                    tbody.innerHTML = '<tr><td colspan="6" style="padding: 8px; color: var(--text-muted);">Нет записанных вызовов</td></tr>';
                    return;
                }}
                tbody.innerHTML = '';
                list.slice(0, 30).forEach(st => {{
                    let badgeClass = 'badge-safe';
                    if (st.success_rate < 50) badgeClass = 'badge-danger';
                    else if (st.success_rate < 90) badgeClass = 'badge-img';
                    const row = document.createElement('tr');
                    row.style.borderBottom = '1px solid var(--border)';
                    row.innerHTML = `
                        <td style="padding: 8px; font-family: monospace; color: #60a5fa;">${{st.tool}}</td>
                        <td style="padding: 8px;">${{st.calls}}</td>
                        <td style="padding: 8px;"><span class="badge ${{badgeClass}}">${{st.success_rate}}%</span></td>
                        <td style="padding: 8px;">${{st.avg_time_ms}} мс</td>
                        <td style="padding: 8px;">${{st.tokens}}</td>
                        <td style="padding: 8px;">$${{st.usd}}</td>
                    `;
                    tbody.appendChild(row);
                }});
            }} catch (err) {{
                console.error("Ошибка загрузки аналитики:", err);
            }}
        }}

        async function fetchRateLimits() {{
            try {{
                const res = await fetch('/api/ratelimits');
                const data = await res.json();
                const tbody = document.getElementById('ratelimits-tbody');
                if (!tbody) return;
                const limits = data.rate_limits || {{}};
                const keys = Object.keys(limits);
                if (keys.length === 0) {{
                    tbody.innerHTML = '<tr><td colspan="5" style="padding: 8px; color: var(--text-muted);">Индивидуальных лимитов не установлено</td></tr>';
                    return;
                }}
                tbody.innerHTML = '';
                keys.forEach(k => {{
                    const rl = limits[k];
                    const row = document.createElement('tr');
                    row.style.borderBottom = '1px solid var(--border)';
                    row.innerHTML = `
                        <td style="padding: 8px; font-family: monospace; color: #60a5fa;">${{rl.tool}}</td>
                        <td style="padding: 8px;">${{rl.max_calls}}</td>
                        <td style="padding: 8px;">${{rl.window_seconds}} с</td>
                        <td style="padding: 8px;">${{rl.current_calls || 0}}</td>
                        <td style="padding: 8px;"><button class="btn btn-danger" style="padding: 2px 8px; font-size: 11px;" onclick="deleteRateLimitUi('${{rl.tool}}')">Удалить</button></td>
                    `;
                    tbody.appendChild(row);
                }});
            }} catch (err) {{
                console.error("Ошибка загрузки лимитов:", err);
            }}
        }}

        async function setRateLimitUi() {{
            const tool = document.getElementById('rl-tool-name').value.trim();
            const maxC = parseInt(document.getElementById('rl-max-calls').value || '5');
            const winS = parseInt(document.getElementById('rl-win-sec').value || '60');
            if (!tool) {{ alert("Укажите имя инструмента"); return; }}
            await fetch('/api/ratelimits', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ tool: tool, max_calls: maxC, window_seconds: winS }})
            }});
            document.getElementById('rl-tool-name').value = '';
            fetchRateLimits();
        }}

        async function deleteRateLimitUi(toolName) {{
            await fetch(`/api/ratelimits/${{toolName}}`, {{ method: 'DELETE' }});
            fetchRateLimits();
        }}

        async function fetchIntegrationsUi() {{
            try {{
                const res = await fetch('/api/settings/integrations');
                const data = await res.json();
                if (data.smtp) {{
                    document.getElementById('int-smtp-host').value = data.smtp.smtp_host || '';
                    document.getElementById('int-smtp-port').value = data.smtp.smtp_port || 587;
                    document.getElementById('int-smtp-user').value = data.smtp.username || '';
                }}
                if (data.s3) {{
                    document.getElementById('int-s3-endpoint').value = data.s3.endpoint_url || '';
                }}
            }} catch (err) {{}}
        }}

        async function saveIntegrationsUi() {{
            const smHost = document.getElementById('int-smtp-host').value.trim();
            const smPort = parseInt(document.getElementById('int-smtp-port').value || '587');
            const smUser = document.getElementById('int-smtp-user').value.trim();
            const smPass = document.getElementById('int-smtp-pass').value.trim();
            const tgTok = document.getElementById('int-tg-token').value.trim();
            const s3End = document.getElementById('int-s3-endpoint').value.trim();

            await fetch('/api/settings/integrations', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{
                    smtp: {{ smtp_host: smHost, smtp_port: smPort, username: smUser, password: smPass }},
                    telegram: {{ bot_token: tgTok }},
                    s3: {{ endpoint_url: s3End }}
                }})
            }});
            const msg = document.getElementById('int-save-msg');
            if (msg) msg.textContent = "✓ Реквизиты сохранены и применены в системе";
            setTimeout(() => {{ if (msg) msg.textContent = ""; }}, 4000);
        }}

        document.getElementById('search-box').addEventListener('input', () => {{
            const q = document.getElementById('search-box').value.toLowerCase().trim();
            renderTools(allTools.filter(t => t.name.toLowerCase().includes(q) || t.description.toLowerCase().includes(q)));
        }});
        document.getElementById('run-btn').addEventListener('click', executeTool);

        fetchCatalog();
        fetchArtifacts();
    </script>
</body>
</html>"""
