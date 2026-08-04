#!/usr/bin/env python3
"""Прямое тестирование инструментов agent_toolkit без LLM.

Этот скрипт позволяет проверить работоспособность конкретных инструментов
до подключения LM Studio. Полезно для диагностики.

Использование:
  python lmstudio_test_tools.py                    # Запустить все безопасные тесты
  python lmstudio_test_tools.py --skill physics    # Тестировать инструменты скилла
  python lmstudio_test_tools.py --tool crypto.generate_uuid
  python lmstudio_test_tools.py --interactive      # Интерактивный режим
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))


# ============================================================
# Набор тестовых вызовов для каждого инструмента
# ============================================================
TOOL_TEST_CASES: dict[str, dict[str, Any]] = {
    # --- Физика и инженерия ---
    "physics.calc_strength": {
        "load_n": 10000.0,
        "area_mm2": 50.0,
        "yield_strength_mpa": 250.0,
    },
    "physics.calc_antenna": {
        "freq_mhz": 433.92,
    },
    "physics.calc_yagi_uda_antenna": {
        "freq_mhz": 433.92,
        "elements_count": 5,
    },
    "physics.calc_airflow": {
        "velocity_m_s": 20.0,
        "char_length_m": 0.5,
        "drag_coeff": 0.3,
        "frontal_area_m2": 2.0,
    },
    "physics.calc_acoustics": {
        "freq_hz": 440.0,
        "medium": "air",
        "temperature_c": 20.0,
        "pressure_pa": 0.2,
    },
    "physics.calc_helmholtz_resonator": {
        "volume_liter": 15.0,
        "port_diam_mm": 50.0,
        "port_length_mm": 100.0,
    },
    "physics.calc_antenna_vswr": {
        "forward_power_w": 10.0,
        "reflected_power_w": 1.0,
        "load_impedance_ohm": 36.5,
        "line_impedance_ohm": 50.0,
    },
    "physics.calc_antenna_matching_network": {
        "freq_mhz": 433.92,
        "antenna_r_ohm": 25.0,
        "antenna_x_ohm": 10.0,
    },
    "physics.calc_em_field": {
        "current_a": 5.0,
        "distance_mm": 10.0,
        "turns_count": 100,
        "solenoid_length_mm": 50.0,
        "core_permeability": 1.0,
    },
    "physics.calc_bolt_torque": {
        "bolt_diameter_mm": 8.0,
        "property_class": "8.8",
        "friction_coeff_k": 0.2,
    },
    "physics.calc_rf_link_budget": {
        "tx_power_dbm": 20.0,
        "freq_mhz": 433.92,
        "distance_km": 1.0,
        "tx_gain_dbi": 2.0,
        "rx_gain_dbi": 2.0,
        "rx_sensitivity_dbm": -110.0,
    },
    "physics.calc_coaxial_cable": {
        "inner_diam_mm": 1.0,
        "outer_diam_mm": 3.0,
        "dielectric_constant": 2.1,
    },
    "physics.calc_fan_cooling": {
        "heat_power_watts": 100.0,
        "max_temp_rise_c": 10.0,
    },
    "physics.calc_pipe_pressure_drop": {
        "airflow_m3_h": 500.0,
        "pipe_diam_mm": 50.0,
        "pipe_length_m": 10.0,
        "friction_coeff": 0.02,
    },
    "physics.calc_sound_barrier": {
        "surface_mass_kg_m2": 10.0,
        "freq_hz": 500.0,
    },
    "physics.calc_propeller_thrust_power": {
        "diameter_mm": 300.0,
        "rpm": 5000,
        "pitch_mm": 150.0,
        "blades_count": 3,
    },
    "physics.calc_propeller_noise": {
        "diameter_mm": 300.0,
        "rpm": 5000,
        "blades_count": 3,
        "observer_distance_m": 1.0,
    },
    "physics.calc_fatigue_life": {
        "stress_amp_mpa": 100.0,
        "endurance_limit_mpa": 200.0,
        "ult_tensile_mpa": 600.0,
    },
    "physics.calc_patch_antenna": {
        "freq_mhz": 2400.0,
        "dielectric_constant": 4.4,
        "substrate_height_mm": 1.6,
    },
    "physics.calc_low_noise_blade_geometry": {
        "diameter_mm": 300.0,
        "hub_diameter_mm": 90.0,
        "design_rpm": 3000,
        "design_velocity_m_s": 10.0,
    },

    # --- САПР ---
    "cad.generate_gear": {
        "path": "test_gear.scad",
        "module_mm": 2.0,
        "teeth_count": 20,
    },
    "cad.generate_enclosure": {
        "path": "test_enclosure.scad",
        "width_mm": 100.0,
        "length_mm": 80.0,
        "height_mm": 60.0,
        "wall_thickness_mm": 2.0,
    },

    # --- Криптография ---
    "crypto.generate_uuid": {},
    "crypto.hash_string": {
        "text": "Hello Agent Toolkit!",
        "algo": "sha256",
    },

    # --- Данные ---
    "data.excel_formula_eval": {
        "formula": "SUM(A1:A3)",
        "cells_json": '{"A1": 10, "A2": 20, "A3": 30}',
    },
    "data.convert_format": {
        "data_str": '[{"name":"Alice","age":30},{"name":"Bob","age":25}]',
        "from_fmt": "json",
        "to_fmt": "csv",
    },

    # --- Файлы ---
    "files.write_file": {
        "path": "test_output.txt",
        "content": "Привет из agent_toolkit!\nСтрока 2.",
    },
    "files.read_file": {
        "path": "test_output.txt",
    },

    # --- Офис ---
    "office.create_docx": {
        "path": "test_report.docx",
        "title": "Тестовый отчёт",
        "content": "## Результаты\nТест пройден успешно.",
    },

    # --- Шаблоны ---
    "templates.render_markdown": {
        "template_name": "report_md",
        "variables_json": '{"title": "Аудит системы", "date": "2026-08-03", "status": "успешно", "summary": "Все проверки пройдены.", "sections": "Раздел 1: Тесты. Раздел 2: Производительность.", "conclusion": "Система готова к эксплуатации."}',
    },
    "templates.create_invoice": {
        "invoice_number": "TEST-001",
        "customer": "ООО Тест",
        "items_json": '[{"name": "Консультация", "qty": 2, "price": 5000}]',
    },
}


def run_single_test(registry, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Выполнить один тест и вернуть результат."""
    t0 = time.perf_counter()
    try:
        result = registry.execute(tool_name, **args)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        result_str = str(result)
        if len(result_str) > 200:
            result_str = result_str[:200] + "..."
        return {
            "tool": tool_name,
            "status": "✅ OK",
            "time_ms": round(elapsed_ms, 1),
            "preview": result_str,
        }
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return {
            "tool": tool_name,
            "status": "❌ Ошибка",
            "time_ms": round(elapsed_ms, 1),
            "preview": str(exc)[:200],
        }


def run_tests(registry, skill_filter: str | None = None, tool_filter: str | None = None):
    """Запустить набор тестов."""
    results = []

    for tool_name, test_args in TOOL_TEST_CASES.items():
        if tool_filter and tool_name != tool_filter:
            continue
        if skill_filter:
            tool_obj = registry.get(tool_name)
            if not tool_obj or skill_filter not in tool_obj.skills:
                continue

        result = run_single_test(registry, tool_name, test_args)
        results.append(result)

    return results


def interactive_mode(registry):
    """Интерактивный режим: пользователь вводит имя инструмента и аргументы."""
    print("\n🎯 Интерактивный режим тестирования")
    print("   Команды: list (список), search <query>, quit (выход)")
    print("   Формат: <tool.name> <json_args>")
    print()

    while True:
        try:
            line = input("🔧 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nВыход.")
            break

        if not line:
            continue

        if line.lower() in ("quit", "exit", "q"):
            break

        if line.lower() == "list":
            tools = sorted(registry.list_tools(), key=lambda t: t.name)
            for t in tools:
                print(f"  {t.name:40} | {t.description[:60]}")
            continue

        if line.lower().startswith("search "):
            query = line[7:].strip()
            hits = registry.search(query, limit=5)
            for tool, score in hits:
                print(f"  [{score:.1f}] {tool.name:40} | {tool.description[:60]}")
            continue

        # Парсим: tool.name {"arg": "value"}
        parts = line.split(maxsplit=1)
        tool_name = parts[0]
        args_str = parts[1] if len(parts) > 1 else "{}"

        try:
            args = json.loads(args_str)
        except json.JSONDecodeError as exc:
            print(f"  ❌ Некорректный JSON: {exc}")
            continue

        result = run_single_test(registry, tool_name, args)
        print(f"  {result['status']} | {result['time_ms']} мс")
        print(f"  {result['preview']}")


def main():
    parser = argparse.ArgumentParser(
        description="Тестирование инструментов agent_toolkit"
    )
    parser.add_argument("--skill", help="Фильтр по скиллу (physics, cad, crypto...)")
    parser.add_argument("--tool", help="Тестировать один конкретный инструмент")
    parser.add_argument("--interactive", "-i", action="store_true", help="Интерактивный режим")
    parser.add_argument("--json", action="store_true", help="Вывод в JSON формате")
    args = parser.parse_args()

    from agent_toolkit import build_default_registry

    print("=" * 70)
    print("  🧪 Agent Toolkit — Тестирование инструментов")
    print("=" * 70)

    reg = build_default_registry()
    total = len(reg.list_tools())
    print(f"📦 Всего инструментов в реестре: {total}")

    if args.interactive:
        interactive_mode(reg)
        return

    # Запускаем тесты
    results = run_tests(reg, skill_filter=args.skill, tool_filter=args.tool)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return

    # Красивый вывод
    ok_count = sum(1 for r in results if "OK" in r["status"])
    err_count = len(results) - ok_count

    print(f"\n{'─' * 70}")
    print(f"{'ИНСТРУМЕНТ':<40} | {'СТАТУС':<10} | {'ВРЕМЯ':>8} | ПРЕВЬЮ")
    print(f"{'─' * 70}")

    for r in results:
        preview = r["preview"].replace("\n", " ")
        if len(preview) > 30:
            preview = preview[:27] + "..."
        print(f"{r['tool']:<40} | {r['status']:<10} | {r['time_ms']:>6.1f} мс | {preview}")

    print(f"{'─' * 70}")
    print(f"Итого: {ok_count} ✅ | {err_count} ❌ | Всего: {len(results)}")

    if err_count == 0 and len(results) > 0:
        print("\n🎉 Все тесты пройдены! Можно подключать LM Studio.")
        print(f"   python lmstudio_demo.py")
    elif err_count > 0:
        print(f"\n⚠️  {err_count} тест(ов) с ошибками. Проверьте зависимости:")
        print(f"   pip install -e .[all]")


if __name__ == "__main__":
    main()
