"""Chainlit UI: интерактивный чат с Drag-and-Drop файлов.

Запуск: chainlit run spectrum/ui/app.py -w --port 8118
"""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger("spectrum.ui")


def _ensure_chainlit():
    """Проверяет доступность chainlit."""
    try:
        import chainlit as cl
        return cl
    except ImportError:
        print("Chainlit not installed. Install: pip install chainlit")
        print("Falling back to CLI mode...")
        return None


# ---------------------------------------------------------------------------
#  Chainlit App (если установлен)
# ---------------------------------------------------------------------------

cl = _ensure_chainlit()

if cl is not None:
    from ..brain.agent import Agent as SpectrumAgent

    # Глобальный агент (ленивая инициализация)
    _agent: SpectrumAgent | None = None

    def get_agent() -> SpectrumAgent:
        global _agent
        if _agent is None:
            _agent = SpectrumAgent.from_settings()
        return _agent

    @cl.on_chat_start
    async def on_chat_start():
        """Инициализация сессии."""
        agent = get_agent()
        stats = agent.stats()

        await cl.Message(
            content=(
                f"# 🔬 S.P.E.C.T.R.U.M.\n"
                f"**Semantic Processing & Extraction Cluster**\n\n"
                f"📊 База знаний: **{stats.get('vector_chunks', 0)}** чанков, "
                f"**{stats.get('files_stored', 0)}** файлов\n\n"
                f"---\n"
                f"**Что я умею:**\n"
                f"- 📎 Перетащите файлы (PDF, Excel, изображения) для индексации\n"
                f"- 💬 Задавайте вопросы по загруженным документам\n"
                f"- 🔍 Укажите URL для парсинга веб-страниц\n"
                f"- 📋 Попросите выполнить задачу (сравнение, отчёт, извлечение данных)\n"
            ),
        ).send()

    @cl.on_message
    async def on_message(message: cl.Message):
        """Обработка текстовых сообщений."""
        agent = get_agent()
        text = message.content.strip()

        # Обработка файлов (если прикреплены)
        if message.elements:
            for element in message.elements:
                if hasattr(element, "path") and element.path:
                    await _process_file(agent, element.path, element.name)

        # Обработка URL
        if text.startswith("http://") or text.startswith("https://"):
            await _process_url(agent, text)
            return

        # Команды
        if text.lower().startswith("/stats"):
            stats = agent.stats()
            await cl.Message(content=f"📊 **Статистика базы:**\n```json\n{stats}\n```").send()
            return

        if text.lower().startswith("/clear"):
            agent.clear_all()
            await cl.Message(content="🗑️ База знаний очищена.").send()
            return

        if text.lower().startswith("/sources"):
            files = agent._file_store.list_all()
            if not files:
                await cl.Message(content="📭 База знаний пуста.").send()
                return
            lines = ["📚 **Источники в базе:**\n"]
            for f in files:
                size_kb = f.size_bytes / 1024
                lines.append(f"- `{f.original_name}` ({size_kb:.1f} KB) — {f.content_type}")
            await cl.Message(content="\n".join(lines)).send()
            return

        if text.lower().startswith("/task "):
            task_text = text[6:].strip()
            await _process_task(agent, task_text)
            return

        # Вопрос-Ответ (RAG)
        await _process_question(agent, text)

    @cl.on_file_upload(accept=["application/pdf", "text/csv", "image/*",
                                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"])
    async def on_file_upload(file):
        """Обработка загруженных файлов."""
        agent = get_agent()
        await _process_file(agent, file.path, file.name)

    async def _process_file(agent: SpectrumAgent, path: str, name: str):
        """Индексирует файл."""
        msg = cl.Message(content=f"⏳ Индексирую `{name}`...")
        await msg.send()

        try:
            result = agent.ingest_file(path)
            if result.success:
                msg.content = (
                    f"✅ **{name}** проиндексирован!\n"
                    f"- Чанков: {result.chunk_count}\n"
                    f"- Время: {result.processing_time_s:.1f} сек\n"
                )
                if result.ingest_result and result.ingest_result.metadata:
                    meta = result.ingest_result.metadata
                    if "title" in meta and meta["title"]:
                        msg.content += f"- Заголовок: {meta['title']}\n"
            else:
                msg.content = f"⚠️ Проблемы с `{name}`:\n" + "\n".join(
                    f"- {e}" for e in result.errors
                )
        except Exception as e:
            msg.content = f"❌ Ошибка обработки `{name}`: {e}"

        await msg.update()

    async def _process_url(agent: SpectrumAgent, url: str):
        """Индексирует URL."""
        msg = cl.Message(content=f"⏳ Загружаю `{url}`...")
        await msg.send()

        try:
            result = agent.ingest_url(url)
            if result.success:
                msg.content = (
                    f"✅ Страница проиндексирована!\n"
                    f"- URL: `{url}`\n"
                    f"- Чанков: {result.chunk_count}\n"
                    f"- Время: {result.processing_time_s:.1f} сек\n"
                )
            else:
                msg.content = f"⚠️ Проблемы с `{url}`:\n" + "\n".join(
                    f"- {e}" for e in result.errors
                )
        except Exception as e:
            msg.content = f"❌ Ошибка: {e}"

        await msg.update()

    async def _process_question(agent: SpectrumAgent, question: str):
        """Отвечает на вопрос через RAG."""
        msg = cl.Message(content="🔍 Ищу ответ в базе знаний...")
        await msg.send()

        try:
            response = agent.ask(question)
            content = f"## Ответ\n\n{response.answer}\n"

            if response.sources:
                content += "\n---\n### 📎 Источники:\n"
                seen = set()
                for src in response.sources:
                    key = (src.source_path, src.page_number)
                    if key in seen:
                        continue
                    seen.add(key)
                    content += f"- {src.citation()} (релевантность: {src.score:.2f})\n"

            msg.content = content
        except Exception as e:
            msg.content = f"❌ Ошибка: {e}"

        await msg.update()

    async def _process_task(agent: SpectrumAgent, task: str):
        """Выполняет задачу агентом."""
        msg = cl.Message(content=f"⚙️ Выполняю задачу...")
        await msg.send()

        try:
            result = agent.execute_task(task)
            status_emoji = {"completed": "✅", "partial": "⚠️", "failed": "❌"}.get(result.status, "❓")

            content = f"{status_emoji} **Задача:** {task}\n\n{result.result}\n"
            if result.sources_used:
                content += f"\n---\n📎 Использовано источников: {len(result.sources_used)}\n"
            content += f"⏱️ Время: {result.processing_time_s:.1f} сек"

            msg.content = content
        except Exception as e:
            msg.content = f"❌ Ошибка: {e}"

        await msg.update()


# ---------------------------------------------------------------------------
#  CLI Fallback (без Chainlit)
# ---------------------------------------------------------------------------

def run_cli():
    """Интерактивный CLI-режим (если Chainlit не установлен)."""
    from ..brain.agent import Agent as SpectrumAgent

    print("=" * 60)
    print("🔬 S.P.E.C.T.R.U.M. — CLI Mode")
    print("=" * 60)
    print("Команды:")
    print("  /ingest <path>   — индексировать файл или директорию")
    print("  /url <URL>       — индексировать веб-страницу")
    print("  /stats           — статистика базы")
    print("  /sources         — список источников")
    print("  /task <задача>   — выполнить задачу")
    print("  /clear           — очистить базу")
    print("  /quit            — выход")
    print("=" * 60)

    agent = SpectrumAgent.from_settings()
    stats = agent.stats()
    print(f"База: {stats.get('vector_chunks', 0)} чанков, {stats.get('files_stored', 0)} файлов\n")

    while True:
        try:
            user_input = input("Вы > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nДо свидания!")
            break

        if not user_input:
            continue

        if user_input == "/quit":
            print("До свидания!")
            break

        if user_input == "/stats":
            import json
            print(json.dumps(agent.stats(), indent=2, ensure_ascii=False))
            continue

        if user_input == "/sources":
            files = agent._file_store.list_all()
            if not files:
                print("База пуста.")
            for f in files:
                print(f"  {f.original_name} ({f.size_bytes / 1024:.1f} KB)")
            continue

        if user_input == "/clear":
            agent.clear_all()
            print("База очищена.")
            continue

        if user_input.startswith("/ingest "):
            path = user_input[8:].strip()
            print(f"Индексирую {path}...")
            if Path(path).is_dir():
                results = agent.ingest_directory(path)
                total = sum(r.chunk_count for r in results)
                print(f"Обработано {len(results)} файлов, {total} чанков")
            else:
                result = agent.ingest_file(path)
                print(f"Чанков: {result.chunk_count}, время: {result.processing_time_s:.1f}с")
            continue

        if user_input.startswith("/url "):
            url = user_input[5:].strip()
            print(f"Загружаю {url}...")
            result = agent.ingest_url(url)
            print(f"Чанков: {result.chunk_count}, время: {result.processing_time_s:.1f}с")
            continue

        if user_input.startswith("/task "):
            task = user_input[6:].strip()
            print("Выполняю...")
            result = agent.execute_task(task)
            print(f"\n{result.result}\n")
            continue

        # Вопрос-ответ
        response = agent.ask(user_input)
        print(f"\n{response.answer}\n")
        if response.sources:
            print("Источники:")
            for s in response.sources[:3]:
                print(f"  - {s.citation()}")
        print()


if __name__ == "__main__":
    if cl is None:
        run_cli()
