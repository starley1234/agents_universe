"""Демонстрация S.P.E.C.T.R.U.M.: полный цикл от файлов до ответов."""

from __future__ import annotations

import json
import tempfile
import textwrap
from pathlib import Path


def create_demo_files(directory: Path) -> list[Path]:
    """Создаёт набор тестовых файлов для демонстрации."""
    directory.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []

    # --- Контракт 1 ---
    contract1 = directory / "contract_001.txt"
    contract1.write_text(textwrap.dedent("""\
        ДОГОВОР ПОСТАВКИ № 001/2024

        Дата: 15 января 2024 года

        Поставщик: ООО «ТехноСтрой», ИНН 7701234567, ОГРН 1234567890123
        Покупатель: АО «ПромИнвест», ИНН 7809876543, ОГРН 3210987654321

        1. ПРЕДМЕТ ДОГОВОРА
        Поставщик обязуется передать в собственность Покупателя следующие товары:
        - Стальные трубы ДУ 100 — 500 шт., цена 2 500 руб./шт.
        - Фланцы стальные ДУ 100 — 1000 шт., цена 350 руб./шт.
        - Краны шаровые ДУ 100 — 50 шт., цена 12 000 руб./шт.

        2. СУММА ДОГОВОРА
        Общая сумма: 1 950 000 (Один миллион девятьсот пятьдесят тысяч) рублей,
        в т.ч. НДС 20%: 325 000 руб.

        3. СРОКИ ПОСТАВКИ
        Поставка осуществляется в течение 30 календарных дней с момента оплаты.
        Последний срок поставки: 15 февраля 2024 года.

        4. УСЛОВИЯ ОПЛАТЫ
        Предоплата 50% в течение 5 банковских дней с подписания договора.
        Окончательный расчёт — в течение 10 банковских дней с момента поставки.

        5. ШТРАФНЫЕ САНКЦИИ
        За просрочку поставки — 0,1% от стоимости недопоставленного товара за каждый день.
        За просрочку оплаты — 0,05% от суммы задолженности за каждый день.

        6. СРОК ДЕЙСТВИЯ
        Действует с 15 января 2024 г. по 31 декабря 2024 г.
    """), encoding="utf-8")
    files.append(contract1)

    # --- Контракт 2 ---
    contract2 = directory / "contract_002.txt"
    contract2.write_text(textwrap.dedent("""\
        ДОГОВОР ПОСТАВКИ № 002/2024

        Дата: 20 января 2024 года

        Поставщик: ООО «МеталлГрупп», ИНН 7702345678, ОГРН 2345678901234
        Покупатель: АО «ПромИнвест», ИНН 7809876543, ОГРН 3210987654321

        1. ПРЕДМЕТ ДОГОВОРА
        - Нержавеющие трубы ДУ 50 — 300 шт., цена 4 800 руб./шт.
        - Нержавеющие трубы ДУ 80 — 200 шт., цена 6 200 руб./шт.
        - Задвижки клиновые ДУ 50 — 100 шт., цена 18 500 руб./шт.

        2. СУММА ДОГОВОРА
        Общая сумма: 3 880 000 (Три миллиона восемьсот восемьдесят тысяч) рублей,
        в т.ч. НДС 20%: 646 666,67 руб.

        3. СРОКИ ПОСТАВКИ
        Поставка в течение 45 календарных дней. Последний срок: 6 марта 2024 г.

        4. УСЛОВИЯ ОПЛАТЫ
        100% предоплата в течение 3 банковских дней.

        5. ШТРАФНЫЕ САНКЦИИ
        За просрочку поставки — 0,15% за каждый день, но не более 10% от суммы договора.

        6. СРОК ДЕЙСТВИЯ
        Действует с 20 января 2024 г. по 30 июня 2024 г.
    """), encoding="utf-8")
    files.append(contract2)

    # --- Таблица CSV ---
    csv_file = directory / "prices.csv"
    csv_file.write_text(textwrap.dedent("""\
        Наименование,Ед.изм,Цена без НДС,НДС 20%,Цена с НДС,Поставщик
        Труба стальная ДУ 100,шт,2083.33,416.67,2500.00,ТехноСтрой
        Фланец стальной ДУ 100,шт,291.67,58.33,350.00,ТехноСтрой
        Кран шаровый ДУ 100,шт,10000.00,2000.00,12000.00,ТехноСтрой
        Труба нерж. ДУ 50,шт,4000.00,800.00,4800.00,МеталлГрупп
        Труба нерж. ДУ 80,шт,5166.67,1033.33,6200.00,МеталлГрупп
        Задвижка клиновая ДУ 50,шт,15416.67,3083.33,18500.00,МеталлГрупп
    """), encoding="utf-8")
    files.append(csv_file)

    # --- Техническая спецификация ---
    spec = directory / "specification.txt"
    spec.write_text(textwrap.dedent("""\
        ТЕХНИЧЕСКАЯ СПЕЦИФИКАЦИЯ
        Труба стальная бесшовная горячедеформированная ДУ 100

        Обозначение: ГОСТ 8732-78
        Материал: Сталь 20 (ГОСТ 1050-2013)

        Основные параметры:
        - Наружный диаметр: 108 мм
        - Толщина стенки: 6 мм
        - Длина: 6 м (немерная)
        - Масса 1 м: 15.09 кг
        - Масса трубы: 90.54 кг

        Механические свойства:
        - Предел текучести: не менее 245 МПа
        - Предел прочности: не менее 410 МПа
        - Относительное удлинение: не менее 24%

        Рабочие параметры:
        - Рабочее давление: до 16 МПа
        - Температура эксплуатации: от -40 до +450 °C

        Поверхность: без трещин, закатов, плён. Допускается окалина.
    """), encoding="utf-8")
    files.append(spec)

    return files


def run_demo():
    """Запускает полную демонстрацию."""
    from .brain.agent import Agent
    from .storage.vector import create_vector_store
    from .storage.file_store import FileStore
    from .storage.graph import SemanticGraph
    from .processor.pipeline import Pipeline

    print("=" * 60)
    print("🔬 S.P.E.C.T.R.U.M. — Демонстрация")
    print("=" * 60)

    # Рабочая директория
    workspace = Path(tempfile.mkdtemp(prefix="spectrum_demo_"))
    print(f"\n📁 Workspace: {workspace}")

    # Создаём тестовые файлы
    data_dir = workspace / "documents"
    demo_files = create_demo_files(data_dir)
    print(f"📝 Создано {len(demo_files)} тестовых файлов")

    # Инициализация компонентов
    print("\n⚙️  Инициализация компонентов...")
    vector_store = create_vector_store(
        backend="chroma",
        collection_name="spectrum_demo",
        persist_dir=str(workspace / "chroma"),
    )
    file_store = FileStore(workspace)
    graph = SemanticGraph(persist_path=workspace / "graph.json")
    pipeline = Pipeline(chunk_size=512, chunk_overlap=100)

    agent = Agent(
        vector_store=vector_store,
        file_store=file_store,
        graph=graph,
        pipeline=pipeline,
    )

    # Индексация
    print("\n📥 Индексация файлов...")
    for f in demo_files:
        result = agent.ingest_file(str(f))
        status = "✅" if result.success else "⚠️"
        print(f"  {status} {f.name}: {result.chunk_count} чанков ({result.processing_time_s:.2f}с)")

    # Статистика
    stats = agent.stats()
    print(f"\n📊 Статистика базы:")
    print(f"  Чанков: {stats.get('vector_chunks', 0)}")
    print(f"  Файлов: {stats.get('files_stored', 0)}")
    if "graph" in stats:
        g = stats["graph"]
        print(f"  Граф: {g.get('total_nodes', 0)} узлов, {g.get('total_edges', 0)} рёбер")

    # Вопросы
    print("\n" + "=" * 60)
    print("💬 Демонстрация вопрос-ответ")
    print("=" * 60)

    questions = [
        "Какие сроки поставки указаны во всех договорах, где сумма превышает 1 млн?",
        "Сравните условия оплаты в договоре №001 и №002",
        "Какие технические характеристики у трубы ДУ 100?",
        "Какой поставщик предлагает нержавеющие трубы?",
    ]

    for q in questions:
        print(f"\n❓ {q}")
        print("-" * 40)
        response = agent.ask(q)
        print(f"💬 {response.answer[:500]}")
        if response.sources:
            print(f"\n📎 Источники:")
            for s in response.sources[:3]:
                print(f"   - {s.citation()} (score: {s.score:.2f})")

    # Задача
    print("\n" + "=" * 60)
    print("⚙️  Демонстрация автономной задачи")
    print("=" * 60)

    task = "Сравни все договоры поставки и составь сводную таблицу: поставщик, сумма, сроки, условия оплаты"
    print(f"\n📋 Задача: {task}")
    print("-" * 40)
    result = agent.execute_task(task)
    print(f"📊 Статус: {result.status}")
    print(f"💬 {result.result[:600]}")

    print("\n" + "=" * 60)
    print("✅ Демонстрация завершена!")
    print("=" * 60)


if __name__ == "__main__":
    run_demo()
