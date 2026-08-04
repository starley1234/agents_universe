"""Инструменты для создания веб-сайтов (site.*).

Генерируют многостраничные PHP/HTML сайты с навигацией, стилями и контентом.
Все файлы создаются в workspace и доступны по URL /workspace/{path}.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..core import Tool, ToolError, Workspace


# ── CSS шаблоны ──
CSS_MODERN = """
:root { --primary: #2563eb; --bg: #f8fafc; --text: #1e293b; --card: #ffffff; --border: #e2e8f0; }
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); line-height: 1.6; }
.container { max-width: 1200px; margin: 0 auto; padding: 0 20px; }
header { background: var(--primary); color: white; padding: 20px 0; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
header h1 { font-size: 24px; }
header p { opacity: 0.9; font-size: 14px; }
nav { background: white; border-bottom: 1px solid var(--border); padding: 12px 0; position: sticky; top: 0; z-index: 100; }
nav a { color: var(--text); text-decoration: none; margin-right: 24px; font-weight: 500; font-size: 15px; }
nav a:hover, nav a.active { color: var(--primary); }
main { padding: 40px 0; min-height: 60vh; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 24px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
.card h2 { color: var(--primary); margin-bottom: 12px; }
.card h3 { margin: 16px 0 8px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 20px; }
.hero { background: linear-gradient(135deg, var(--primary), #7c3aed); color: white; padding: 60px 0; text-align: center; border-radius: 0 0 24px 24px; margin-bottom: 40px; }
.hero h2 { font-size: 36px; margin-bottom: 16px; }
.hero p { font-size: 18px; opacity: 0.9; max-width: 600px; margin: 0 auto; }
footer { background: #1e293b; color: #94a3b8; padding: 30px 0; text-align: center; margin-top: 60px; }
table { width: 100%; border-collapse: collapse; margin: 16px 0; }
th, td { padding: 10px 14px; text-align: left; border-bottom: 1px solid var(--border); }
th { background: #f1f5f9; font-weight: 600; }
ul, ol { padding-left: 24px; margin: 12px 0; }
li { margin: 6px 0; }
a { color: var(--primary); }
img { max-width: 100%; border-radius: 8px; }
.btn { display: inline-block; background: var(--primary); color: white; padding: 10px 24px; border-radius: 8px; text-decoration: none; font-weight: 500; }
.btn:hover { opacity: 0.9; }
.badge { display: inline-block; background: #dbeafe; color: var(--primary); padding: 2px 10px; border-radius: 20px; font-size: 13px; font-weight: 500; }
"""

CSS_DARK = """
:root { --primary: #3b82f6; --bg: #0f172a; --text: #e2e8f0; --card: #1e293b; --border: #334155; }
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); line-height: 1.6; }
.container { max-width: 1200px; margin: 0 auto; padding: 0 20px; }
header { background: #1e293b; color: white; padding: 20px 0; border-bottom: 1px solid var(--border); }
nav { background: #1e293b; border-bottom: 1px solid var(--border); padding: 12px 0; position: sticky; top: 0; z-index: 100; }
nav a { color: #94a3b8; text-decoration: none; margin-right: 24px; font-weight: 500; }
nav a:hover, nav a.active { color: var(--primary); }
main { padding: 40px 0; min-height: 60vh; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 24px; margin-bottom: 20px; }
.card h2 { color: var(--primary); margin-bottom: 12px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 20px; }
.hero { background: linear-gradient(135deg, #1e40af, #7c3aed); color: white; padding: 60px 0; text-align: center; border-radius: 0 0 24px 24px; margin-bottom: 40px; }
.hero h2 { font-size: 36px; margin-bottom: 16px; }
footer { background: #020617; color: #64748b; padding: 30px 0; text-align: center; margin-top: 60px; }
table { width: 100%; border-collapse: collapse; margin: 16px 0; }
th, td { padding: 10px 14px; text-align: left; border-bottom: 1px solid var(--border); }
th { background: #334155; }
a { color: var(--primary); }
.btn { display: inline-block; background: var(--primary); color: white; padding: 10px 24px; border-radius: 8px; text-decoration: none; }
"""

THEMES = {"modern": CSS_MODERN, "dark": CSS_DARK, "light": CSS_MODERN}


def _make_php_page(
    site_name: str,
    page_title: str,
    content_html: str,
    nav_items: list[tuple[str, str]],
    current_page: str,
    theme_css: str,
    footer_text: str = "",
) -> str:
    """Генерирует PHP-страницу с общим header/footer."""
    nav_html = "\n        ".join(
        f'<a href="{href}" class="{"active" if href == current_page else ""}">{label}</a>'
        for href, label in nav_items
    )

    return f"""<?php
$page_title_var = {json.dumps(page_title)};
?>
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title><?= $page_title_var ?> — {site_name}</title>
    <style>{theme_css}</style>
</head>
<body>
    <header>
        <div class="container">
            <h1>{site_name}</h1>
        </div>
    </header>
    <nav>
        <div class="container">
        {nav_html}
        </div>
    </nav>
    <main>
        <div class="container">
{content_html}
        </div>
    </main>
    <footer>
        <div class="container">
            <p>{footer_text or f'© {site_name} — Создано с помощью Agent Toolkit'}</p>
        </div>
    </footer>
</body>
</html>"""


def build_site_tools(ws: Workspace) -> list[Tool]:
    """Собрать инструменты для создания веб-сайтов."""

    def create_site(
        topic: str,
        site_name: str = "",
        pages_json: str = "[]",
        theme: str = "modern",
        content_json: str = "{}",
    ) -> str:
        """Создать многостраничный PHP-сайт в workspace."""
        if not topic.strip():
            raise ToolError("Тема сайта (topic) не может быть пустой")

        site_name = site_name.strip() or topic.strip()
        site_slug = re.sub(r"[^a-zа-я0-9]+", "_", topic.lower().strip())[:30].strip("_")
        if not site_slug:
            site_slug = "site"

        # Парсим страницы
        try:
            pages = json.loads(pages_json) if pages_json else []
            if not isinstance(pages, list):
                pages = []
        except json.JSONDecodeError:
            pages = []

        # Парсим контент
        try:
            content_map = json.loads(content_json) if content_json else {}
            if not isinstance(content_map, dict):
                content_map = {}
        except json.JSONDecodeError:
            content_map = {}

        # Дефолтные страницы если не указаны
        if not pages:
            pages = [
                {"file": "index.php", "title": "Главная", "content": ""},
                {"file": "about.php", "title": "О нас", "content": ""},
                {"file": "services.php", "title": "Услуги", "content": ""},
                {"file": "contact.php", "title": "Контакты", "content": ""},
            ]

        theme_css = THEMES.get(theme.lower(), CSS_MODERN)
        site_dir = Path(site_slug)

        # Навигация
        nav_items = [(p["file"], p.get("title", p["file"].replace(".php", "").capitalize())) for p in pages]

        # Создаём директорию сайта
        site_path = ws.resolve(str(site_dir))
        site_path.mkdir(parents=True, exist_ok=True)

        created_files: list[str] = []

        for page in pages:
            filename = page.get("file", "index.php")
            if not filename.endswith(".php"):
                filename += ".php"
            title = page.get("title", filename.replace(".php", "").capitalize())
            page_content = page.get("content", "") or content_map.get(filename.replace(".php", ""), "")

            # Генерируем контент если пустой
            if not page_content:
                page_content = _generate_default_content(filename.replace(".php", ""), topic, site_name, site_slug)

            php_code = _make_php_page(
                site_name=site_name,
                page_title=title,
                content_html=page_content,
                nav_items=nav_items,
                current_page=filename,
                theme_css=theme_css,
            )

            file_path = site_path / filename
            file_path.write_text(php_code, encoding="utf-8")
            created_files.append(f"{site_slug}/{filename}")

        # Создаём .htaccess для Apache
        htaccess = (
            "RewriteEngine On\n"
            "DirectoryIndex index.php\n"
            "ErrorDocument 404 /index.php\n"
        )
        (site_path / ".htaccess").write_text(htaccess, encoding="utf-8")

        # Создаём sitemap.xml
        sitemap_urls = "\n".join(
            f"  <url><loc>/workspace/{site_slug}/{p.get('file', 'index.php')}</loc></url>"
            for p in pages
        )
        sitemap = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"{sitemap_urls}\n"
            "</urlset>"
        )
        (site_path / "sitemap.xml").write_text(sitemap, encoding="utf-8")
        created_files.append(f"{site_slug}/sitemap.xml")

        site_url = f"/workspace/{site_slug}/index.php"

        lines = [
            f"### 🌐 Сайт создан: {site_name}",
            f"- **Тема:** {topic}",
            f"- **Стиль:** {theme}",
            f"- **Страниц:** {len(pages)}",
            f"- **URL сайта:** **{site_url}**",
            f"- **Директория:** `/workspace/{site_slug}/`",
            f"",
            f"#### Страницы:",
        ]
        for f in created_files:
            lines.append(f"- `/workspace/{f}`")

        lines.extend([
            f"",
            f"#### Как открыть:",
            f"1. Откройте в браузере: `http://localhost:8090{site_url}`",
            f"2. Или перейдите в директорию: `http://localhost:8090/workspace/{site_slug}/`",
        ])

        return "\n".join(lines)

    def add_page(site_path: str, file: str, title: str, content: str = "") -> str:
        """Добавить страницу к существующему сайту."""
        import json as _json

        if not site_path.strip():
            raise ToolError("Путь к сайту (site_path) не может быть пустым")
        if not file.strip():
            raise ToolError("Имя файла (file) не может быть пустым")
        if not file.endswith(".php"):
            file += ".php"

        site_dir = ws.resolve(site_path)
        if not site_dir.exists() or not site_dir.is_dir():
            raise ToolError(f"Директория сайта {site_path!r} не найдена")

        # Читаем index.php чтобы извлечь стили и навигацию
        index_file = site_dir / "index.php"
        if not index_file.exists():
            raise ToolError(f"index.php не найден в {site_path}")

        index_content = index_file.read_text(encoding="utf-8")

        # Извлекаем CSS из index.php
        import re
        css_match = re.search(r"<style>(.*?)</style>", index_content, re.DOTALL)
        theme_css = css_match.group(1) if css_match else ""

        # Извлекаем навигацию
        nav_items = re.findall(r'<a\s+href="([^"]+)"[^>]*>([^<]+)</a>', index_content)
        if not nav_items:
            nav_items = [(file, title)]

        # Добавляем новую страницу в навигацию
        nav_items.append((file, title))

        # Генерируем контент если пустой
        if not content:
            content = f"""
<div class="card">
    <h2>{title}</h2>
    <p>Содержимое страницы «{title}».</p>
</div>"""

        # Создаём PHP страницу
        nav_html = "\n        ".join(
            f'<a href="{href}" class="{"active" if href == file else ""}">{label}</a>'
            for href, label in nav_items
        )

        # Извлекаем site_name из header
        site_name_match = re.search(r'<h1>([^<]+)</h1>', index_content)
        site_name = site_name_match.group(1) if site_name_match else site_path

        # Извлекаем footer
        footer_match = re.search(r'<footer>.*?<p>(.*?)</p>.*?</footer>', index_content, re.DOTALL)
        footer_text = footer_match.group(1) if footer_match else f"© {site_name}"

        php_code = f"""<?php
$page_title_var = {json.dumps(title)};
?>
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title><?= $page_title_var ?> — {site_name}</title>
    <style>{theme_css}</style>
</head>
<body>
    <header>
        <div class="container">
            <h1>{site_name}</h1>
        </div>
    </header>
    <nav>
        <div class="container">
        {nav_html}
        </div>
    </nav>
    <main>
        <div class="container">
{content}
        </div>
    </main>
    <footer>
        <div class="container">
            <p>{footer_text}</p>
        </div>
    </footer>
</body>
</html>"""

        page_file = site_dir / file
        page_file.write_text(php_code, encoding="utf-8")

        # Обновляем навигацию в существующих страницах
        for nav_href, nav_label in nav_items:
            existing = site_dir / nav_href
            if existing.exists() and nav_href != file:
                existing_content = existing.read_text(encoding="utf-8")
                # Добавляем новый пункт навигации
                new_nav_link = f'\n        <a href="{file}">{title}</a>'
                # Вставляем перед закрывающим </div> в nav
                existing_content = re.sub(
                    r'(<nav>.*?)(</div>\s*</nav>)',
                    rf'\1{new_nav_link}\n        \2',
                    existing_content,
                    count=1,
                    flags=re.DOTALL,
                )
                existing.write_text(existing_content, encoding="utf-8")

        return (
            f"### ✅ Страница добавлена: {file}\n"
            f"- **Заголовок:** {title}\n"
            f"- **URL:** `/workspace/{site_path}/{file}`\n"
            f"- **Навигация обновлена** на всех {len(nav_items)} страницах сайта"
        )

    def edit_content(site_path: str, page: str, old_text: str, new_text: str) -> str:
        """Заменить текст на странице сайта."""
        if not site_path.strip() or not page.strip() or not old_text:
            raise ToolError("site_path, page и old_text обязательны")
        if not page.endswith(".php"):
            page += ".php"

        site_dir = ws.resolve(site_path)
        page_file = site_dir / page
        if not page_file.exists():
            raise ToolError(f"Страница {page!r} не найдена в {site_path}")

        content = page_file.read_text(encoding="utf-8")
        if old_text not in content:
            raise ToolError(f"Текст {old_text!r} не найден на странице {page}")

        content = content.replace(old_text, new_text, 1)
        page_file.write_text(content, encoding="utf-8")

        return (
            f"### ✅ Контент обновлён: {page}\n"
            f"- **Заменено:** `{old_text[:50]}` → `{new_text[:50]}`\n"
            f"- **URL:** `/workspace/{site_path}/{page}`"
        )

    return [
        Tool(
            name="site.create",
            description="Создать многостраничный PHP-сайтт на заданную тему. Автоматически генерирует структуру, навигацию, стили и контент. Сайт доступен по URL /workspace/{slug}/index.php.",
            parameters={
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Тема сайта (например, 'Кофейня в Москве', 'IT-компания', 'Фитнес-клуб')"},
                    "site_name": {"type": "string", "description": "Название сайта (по умолчанию = тема)"},
                    "pages_json": {
                        "type": "string",
                        "description": 'JSON-массив страниц: [{"file": "index.php", "title": "Главная", "content": "<div>...</div>"}]',
                    },
                    "theme": {"type": "string", "description": "Тема оформления: modern, dark, light (по умолчанию modern)"},
                    "content_json": {
                        "type": "string",
                        "description": 'JSON-объект контента по именам страниц: {"index": "<div>HTML контент</div>"}',
                    },
                },
                "required": ["topic"],
            },
            fn=create_site,
            skills=["site", "web", "php", "html", "create", "website", "builder"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "website",
                "speed": "fast",
                "tags": ["site", "website", "php", "html", "create", "builder", "сайт"],
            },
            example='site.create(topic="Кофейня в Москве", theme="modern")',
        ),
        Tool(
            name="site.add_page",
            description="Добавить новую страницу к существующему сайту. Автоматически обновляет навигацию на всех страницах.",
            parameters={
                "type": "object",
                "properties": {
                    "site_path": {"type": "string", "description": "Путь к директории сайта в workspace (например, 'кофейня_аромат')"},
                    "file": {"type": "string", "description": "Имя файла страницы (например, 'delivery.php')"},
                    "title": {"type": "string", "description": "Заголовок страницы для навигации"},
                    "content": {"type": "string", "description": "HTML-контент страницы (если пусто — генерируется автоматически)"},
                },
                "required": ["site_path", "file", "title"],
            },
            fn=add_page,
            skills=["site", "web", "php", "html", "edit", "website"],
            attributes={
                "category": "local", "read_only": False, "dangerous": False,
                "resource_type": "website", "speed": "fast",
                "tags": ["site", "add_page", "edit", "website", "навигация"],
            },
            example='site.add_page(site_path="кофейня_аромат", file="delivery.php", title="Доставка")',
        ),
        Tool(
            name="site.edit_content",
            description="Заменить текст на странице сайта. Точечная замена контента без изменения структуры.",
            parameters={
                "type": "object",
                "properties": {
                    "site_path": {"type": "string", "description": "Путь к директории сайта"},
                    "page": {"type": "string", "description": "Имя файла страницы (например, 'index.php')"},
                    "old_text": {"type": "string", "description": "Текст для замены"},
                    "new_text": {"type": "string", "description": "Новый текст"},
                },
                "required": ["site_path", "page", "old_text", "new_text"],
            },
            fn=edit_content,
            skills=["site", "web", "php", "html", "edit", "website"],
            attributes={
                "category": "local", "read_only": False, "dangerous": False,
                "resource_type": "website", "speed": "fast",
                "tags": ["site", "edit", "replace", "website", "контент"],
            },
            example='site.edit_content(site_path="кофейня_аромат", page="index.php", old_text="Добро пожаловать", new_text="Добро пожаловать в лучшую кофейню!")',
        ),
    ]


def _generate_default_content(page_name: str, topic: str, site_name: str, site_slug: str = "site") -> str:
    """Генерирует дефолтный HTML-контент для страницы."""
    if page_name == "index":
        return f"""
<div class="hero">
    <h2>{site_name}</h2>
    <p>Добро пожаловать! Мы предлагаем лучшие решения в области: {topic}.</p>
    <br><a href="about.php" class="btn" style="background:white;color:var(--primary)">Узнать больше →</a>
</div>

<div class="grid">
    <div class="card">
        <h2>🎯 Наша миссия</h2>
        <p>Мы стремимся предоставить лучшие услуги в сфере <strong>{topic}</strong>. Наша команда профессионалов работает для вас каждый день.</p>
    </div>
    <div class="card">
        <h2>⭐ Преимущества</h2>
        <ul>
            <li>Более 10 лет опыта</li>
            <li>Индивидуальный подход к каждому клиенту</li>
            <li>Гарантия качества</li>
            <li>Конкурентные цены</li>
        </ul>
    </div>
    <div class="card">
        <h2>📊 Наши достижения</h2>
        <table>
            <tr><th>Показатель</th><th>Значение</th></tr>
            <tr><td>Клиентов</td><td>500+</td></tr>
            <tr><td>Проектов</td><td>1200+</td></tr>
            <tr><td>Лет на рынке</td><td>10+</td></tr>
            <tr><td>Сотрудников</td><td>50+</td></tr>
        </table>
    </div>
</div>

<div class="card" style="margin-top:20px">
    <h2>🔥 Популярные услуги</h2>
    <div class="grid">
        <div class="card"><h3>Консультация</h3><p>Профессиональная консультация по всем вопросам {topic}.</p><span class="badge">Популярное</span></div>
        <div class="card"><h3>Проектирование</h3><p>Разработка индивидуальных решений под ваши задачи.</p><span class="badge">Новинка</span></div>
        <div class="card"><h3>Поддержка</h3><p>Техническая поддержка и сопровождение 24/7.</p><span class="badge">24/7</span></div>
    </div>
</div>"""

    elif page_name == "about":
        return f"""
<div class="card">
    <h2>О компании {site_name}</h2>
    <p>Компания <strong>{site_name}</strong> — лидер в области <strong>{topic}</strong>. Мы работаем на рынке более 10 лет и за это время реализовали более 1200 успешных проектов.</p>

    <h3>Наша история</h3>
    <p>Основанная в 2014 году, наша компания начинала как небольшой стартап с командой из 3 человек. Сегодня мы — команда из 50+ профессионалов, обслуживающая более 500 клиентов по всей России.</p>

    <h3>Наши ценности</h3>
    <ul>
        <li><strong>Качество</strong> — мы не идём на компромиссы в качестве наших услуг</li>
        <li><strong>Инновации</strong> — мы постоянно внедряем новые технологии и подходы</li>
        <li><strong>Клиентоориентированность</strong> — каждый проект уникален для нас</li>
        <li><strong>Ответственность</strong> — мы отвечаем за результат</li>
    </ul>
</div>

<div class="grid">
    <div class="card">
        <h2>👥 Команда</h2>
        <p>Наша команда — это опытные профессионалы, каждый из которых является экспертом в своей области.</p>
    </div>
    <div class="card">
        <h2>🏆 Награды</h2>
        <ul>
            <li>Лучшая компания года 2024</li>
            <li>Золотой партнёр 2023</li>
            <li>Инновация года 2022</li>
        </ul>
    </div>
</div>"""

    elif page_name == "services":
        return f"""
<div class="card">
    <h2>Наши услуги</h2>
    <p>Мы предлагаем полный спектр услуг в области <strong>{topic}</strong>:</p>
</div>

<div class="grid">
    <div class="card">
        <h2>📋 Консалтинг</h2>
        <p>Профессиональный аудит и консалтинг. Анализ текущей ситуации и разработка стратегии развития.</p>
        <p><strong>от 50 000 ₽</strong></p>
    </div>
    <div class="card">
        <h2>🔧 Разработка</h2>
        <p>Проектирование и разработка индивидуальных решений. Полный цикл от концепции до реализации.</p>
        <p><strong>от 150 000 ₽</strong></p>
    </div>
    <div class="card">
        <h2>🚀 Внедрение</h2>
        <p>Внедрение и настройка готовых решений. Интеграция с существующими системами.</p>
        <p><strong>от 100 000 ₽</strong></p>
    </div>
    <div class="card">
        <h2>📚 Обучение</h2>
        <p>Обучение персонала и проведение мастер-классов. Сертификация специалистов.</p>
        <p><strong>от 30 000 ₽</strong></p>
    </div>
    <div class="card">
        <h2>🛡️ Поддержка</h2>
        <p>Техническая поддержка и сопровождение. Мониторинг и оперативное реагирование.</p>
        <p><strong>от 20 000 ₽/мес</strong></p>
    </div>
    <div class="card">
        <h2>📊 Аналитика</h2>
        <p>Сбор и анализ данных. Формирование отчётов и рекомендаций по оптимизации.</p>
        <p><strong>от 40 000 ₽</strong></p>
    </div>
</div>

<div class="card" style="margin-top:20px;text-align:center">
    <h2>Нужна консультация?</h2>
    <p>Свяжитесь с нами для бесплатной первичной консультации</p>
    <br><a href="contact.php" class="btn">Связаться с нами →</a>
</div>"""

    elif page_name == "contact":
        return f"""
<div class="grid">
    <div class="card">
        <h2>📞 Контакты</h2>
        <table>
            <tr><th>Параметр</th><th>Значение</th></tr>
            <tr><td>Телефон</td><td>+7 (495) 123-45-67</td></tr>
            <tr><td>Email</td><td>info@{site_slug}.ru</td></tr>
            <tr><td>Адрес</td><td>г. Москва, ул. Примерная, д. 1</td></tr>
            <tr><td>Режим работы</td><td>Пн-Пт: 9:00 — 18:00</td></tr>
        </table>
    </div>
    <div class="card">
        <h2>✉️ Напишите нам</h2>
        <?php
        if ($_SERVER['REQUEST_METHOD'] === 'POST') {{
            $name = htmlspecialchars($_POST['name'] ?? '');
            $email = htmlspecialchars($_POST['email'] ?? '');
            $message = htmlspecialchars($_POST['message'] ?? '');
            echo "<div class='card' style='background:#dcfce7;border-color:#86efac'><p>✅ Спасибо, $name! Ваше сообщение отправлено. Мы свяжемся с вами в ближайшее время.</p></div>";
        }}
        ?>
        <form method="POST" style="margin-top:16px">
            <div style="margin-bottom:12px">
                <label style="display:block;font-weight:500;margin-bottom:4px">Имя</label>
                <input name="name" required style="width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:6px;font-size:14px">
            </div>
            <div style="margin-bottom:12px">
                <label style="display:block;font-weight:500;margin-bottom:4px">Email</label>
                <input name="email" type="email" required style="width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:6px;font-size:14px">
            </div>
            <div style="margin-bottom:12px">
                <label style="display:block;font-weight:500;margin-bottom:4px">Сообщение</label>
                <textarea name="message" rows="4" required style="width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:6px;font-size:14px"></textarea>
            </div>
            <button type="submit" class="btn">Отправить →</button>
        </form>
    </div>
</div>"""

    else:
        # Generic page
        title = page_name.replace("_", " ").capitalize()
        return f"""
<div class="card">
    <h2>{title}</h2>
    <p>Информация о <strong>{topic}</strong> будет дополнена.</p>
    <p>Свяжитесь с нами для получения подробной информации.</p>
</div>
<div class="card">
    <h3>Полезные ссылки</h3>
    <ul>
        <li><a href="index.php">Главная страница</a></li>
        <li><a href="about.php">О нас</a></li>
        <li><a href="services.php">Услуги</a></li>
        <li><a href="contact.php">Контакты</a></li>
    </ul>
</div>"""

