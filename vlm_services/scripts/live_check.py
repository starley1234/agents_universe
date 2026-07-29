#!/usr/bin/env python3
"""Проверка сервисов на живой модели.

Запускается там, где есть доступ к провайдеру. Проверяет по нарастающей:
соединение → зрение модели → соблюдение JSON-схемы → работа сервисов на
настоящей картинке. На каждом шаге печатает, что именно пошло не так, а
в конце — сводку с реальной стоимостью.

    export VLM_PROVIDER=openai
    export VLM_BASE_URL=https://llm.toolforge.ru/v1
    export VLM_API_KEY=sk-...
    export VLM_MODEL=unsloth/gemma-4-12b-it
    python scripts/live_check.py                # быстрая проверка
    python scripts/live_check.py --all          # все двенадцать сервисов
    python scripts/live_check.py --save out/    # сохранить сырые ответы

Ключ берётся только из окружения и никуда не записывается.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _bail_wrong_python(exc: ModuleNotFoundError) -> None:
    """Объяснить, что делать, вместо трейсбека.

    Самая частая ошибка запуска: `python scripts/live_check.py` берёт
    системный интерпретатор, где зависимостей нет. Трейсбек про
    langchain_core тут ни при чём и только сбивает с толку.
    """
    venv = ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin") / "python"
    print(f"Не найден модуль «{exc.name}»: скрипт запущен интерпретатором "
          f"без зависимостей.\n")
    print(f"  сейчас: {sys.executable}")
    if venv.exists():
        print(f"\nЗапустите так:\n  {venv} scripts/live_check.py "
              + " ".join(sys.argv[1:]))
    else:
        print(f"\nОкружение не создано. Из каталога {ROOT}:\n"
              "  make install\n"
              "  .venv/bin/python scripts/live_check.py "
              + " ".join(sys.argv[1:]))
    print("\nЛибо поставьте зависимости в текущий интерпретатор:\n"
          f"  {sys.executable} -m pip install -e '.[all]'")
    raise SystemExit(2)


try:
    from vlmkit.config import settings  # noqa: E402
    from vlmkit.core import get_service, load_registry, parse_json  # noqa: E402
    from vlmkit.images import HAVE_PILLOW, ImageRef, load  # noqa: E402
    from vlmkit.runner import price_of  # noqa: E402
    from vlmkit.vlm import build_message, get_vlm, supports_json_mode  # noqa: E402
except ModuleNotFoundError as exc:  # noqa: BLE001
    _bail_wrong_python(exc)

OK, FAIL, WARN = "\033[32m✓\033[0m", "\033[31m✗\033[0m", "\033[33m!\033[0m"

# Быстрый набор: разные типы задач при минимуме запросов.
QUICK = ("ux-critic", "nutrition-plate", "doc-extractor", "sight-assistant")


def make_test_image() -> ImageRef:
    """Картинка с однозначным содержимым: по ответу видно, видит ли модель."""
    if not HAVE_PILLOW:
        print(f"{WARN} нет Pillow — проверка зрения будет неточной")
        from vlmkit.demo import demo_image

        return demo_image("test.png")

    from PIL import Image, ImageDraw

    im = Image.new("RGB", (512, 384), (245, 245, 248))
    d = ImageDraw.Draw(im)
    d.rectangle([40, 40, 240, 180], fill=(200, 40, 40))          # красный прямоугольник
    d.ellipse([300, 60, 440, 200], fill=(30, 90, 200))           # синий круг
    d.text((60, 250), "PROVERKA 1234", fill=(10, 10, 10))
    d.rectangle([300, 250, 460, 320], outline=(20, 140, 60), width=6)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return load(buf.getvalue(), name="test.png")


def step(n: int, title: str) -> None:
    print(f"\n\033[1m{n}. {title}\033[0m")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="все двенадцать сервисов")
    ap.add_argument("--save", help="каталог для сырых ответов")
    ap.add_argument("--service", action="append", help="проверить конкретный сервис")
    args = ap.parse_args()

    cfg = settings()
    out_dir = Path(args.save) if args.save else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    print(f"провайдер: {cfg.provider}")
    print(f"модель:    {cfg.resolved_model()}")
    print(f"base_url:  {cfg.base_url or '(по умолчанию)'}")
    print(f"ключ:      {'задан' if cfg.api_key else 'НЕ ЗАДАН'}")

    if cfg.provider == "fake":
        print(f"\n{FAIL} провайдер fake — это оффлайн-заглушка, живой проверки не будет.")
        print("   Задайте VLM_PROVIDER=openai (или ollama) и VLM_API_KEY.")
        return 2

    img = make_test_image()
    total_cost = 0.0
    failures: list[str] = []

    # --- 1. соединение ----------------------------------------------------
    step(1, "Соединение с провайдером")
    llm = get_vlm(cfg)
    t0 = time.time()
    try:
        r = llm.invoke("Ответь одним словом: привет")
        print(f"{OK} ответ за {time.time() - t0:.1f} с: "
              f"{str(r.content).strip()[:80]!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"{FAIL} {type(exc).__name__}: {str(exc)[:400]}")
        print("\n   Проверьте base_url (нужен суффикс /v1), ключ и доступность хоста.")
        return 1

    # --- 2. зрение --------------------------------------------------------
    step(2, "Видит ли модель изображение")
    if not supports_json_mode(cfg.resolved_model()):
        print(f"{WARN} модель в списке без json-режима — response_format не отправляется")
    t0 = time.time()
    try:
        msg = build_message(
            "Что изображено? Перечисли фигуры, их цвета и весь видимый текст.", [img])
        r = llm.invoke([msg])
        text = str(r.content).strip()
        print(f"   за {time.time() - t0:.1f} с: {text[:300]}")
        low = text.lower()
        hits = sum(w in low for w in ("красн", "син", "red", "blue", "1234", "proverka"))
        if hits >= 2:
            print(f"{OK} модель действительно видит картинку (совпадений: {hits})")
        else:
            print(f"{FAIL} ответ не связан с изображением — модель без зрения "
                  "или картинка не дошла")
            failures.append("модель не распознаёт изображения")
    except Exception as exc:  # noqa: BLE001
        print(f"{FAIL} {type(exc).__name__}: {str(exc)[:300]}")
        failures.append(f"зрение: {type(exc).__name__}")

    # --- 3. соблюдение схемы ---------------------------------------------
    step(3, "Соблюдение JSON-схемы")
    schema = {"shapes": [{"color": "", "kind": ""}], "text": "", "count": 0}
    try:
        sys_msg = ("Ты визуальный аналитик. Ответь ТОЛЬКО валидным JSON по схеме.\n"
                   "JSON_SCHEMA_HINT: " + json.dumps(schema, ensure_ascii=False))
        from langchain_core.messages import SystemMessage

        r = llm.invoke([SystemMessage(content=sys_msg),
                        build_message("Опиши фигуры на картинке.", [img])])
        raw = str(r.content)
        parsed = parse_json(raw)
        if parsed is None:
            print(f"{FAIL} ответ не разобран. Начало: {' '.join(raw.split())[:200]!r}")
            failures.append("модель не держит JSON-схему")
        else:
            clean = raw.strip().startswith("{")
            print(f"{OK} JSON разобран" + ("" if clean else " (потребовалась починка)"))
            print(f"   {json.dumps(parsed, ensure_ascii=False)[:250]}")
            if not clean:
                print(f"   {WARN} модель добавляет текст вокруг JSON — "
                      "наш парсер это чинит")
        if out_dir:
            (out_dir / "schema_raw.txt").write_text(raw, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"{FAIL} {type(exc).__name__}: {str(exc)[:300]}")
        failures.append(f"схема: {type(exc).__name__}")

    # --- 4. сервисы -------------------------------------------------------
    slugs = args.service or (sorted(load_registry()) if args.all else list(QUICK))
    step(4, f"Сервисы на живой модели ({len(slugs)} шт.)")
    rows = []
    for slug in slugs:
        svc = get_service(slug, cfg)
        photos = [img] * max(1, svc.min_images)
        t0 = time.time()
        try:
            res = svc.run(photos)
            dur = time.time() - t0
            bad = [w for w in res.warnings if "не по схеме" in w]
            mark = FAIL if bad else (WARN if res.warnings else OK)
            note = "не по схеме" if bad else (f"{len(res.warnings)} предупр."
                                              if res.warnings else "чисто")
            print(f"{mark} {slug:22} {dur:5.1f} с  {note}")
            rows.append((slug, dur, "ошибка схемы" if bad else "ок"))
            if bad:
                failures.append(f"{slug}: ответ не по схеме")
            if out_dir:
                (out_dir / f"{slug}.md").write_text(res.report, encoding="utf-8")
                (out_dir / f"{slug}.json").write_text(
                    json.dumps(res.as_dict(), ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            print(f"{FAIL} {slug:22} {type(exc).__name__}: {str(exc)[:120]}")
            rows.append((slug, time.time() - t0, f"упал: {type(exc).__name__}"))
            failures.append(f"{slug}: {type(exc).__name__}")

    # --- итог -------------------------------------------------------------
    step(5, "Итог")
    ok_n = sum(1 for _, _, s in rows if s == "ок")
    print(f"сервисов проверено: {len(rows)}, без замечаний: {ok_n}")
    if rows:
        avg = sum(d for _, d, _ in rows) / len(rows)
        print(f"среднее время на сервис: {avg:.1f} с")
    known = price_of(cfg.resolved_model(), 1000, 300)
    if known:
        print(f"ориентир стоимости: ${known:.5f} за запрос (1000/300 токенов)")
    else:
        print("модель не в прайсе runner.PRICES — стоимость показывается нулём")
    if out_dir:
        print(f"сырые ответы: {out_dir}/")

    if failures:
        print(f"\n{FAIL} проблемы:")
        for f in dict.fromkeys(failures):
            print(f"   - {f}")
        print("\nПришлите этот вывод и содержимое --save, я подстрою промпты.")
        return 1
    print(f"\n{OK} всё работает на живой модели")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
