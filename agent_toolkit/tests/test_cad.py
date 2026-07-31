"""Тесты инструментов САПР / CAD (cad.*): OpenSCAD, FreeCAD, шестерни, корпуса, антенны, пропеллеры и STL-анализ."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.core import Workspace
from agent_toolkit.local.cad import build_cad_tools
from tests.harness import TempWorkspace, check, section, summary


def run_tests() -> int:
    with TempWorkspace() as tmp:
        ws = Workspace(tmp.path("ws"))
        section("1. Инструменты САПР / CAD (OpenSCAD, FreeCAD, антенны Яги, пропеллеры)")
        tools = {t.name: t for t in build_cad_tools(ws)}
        check("зарегистрировано 10 инструментов cad", len(tools) == 10)

        res_render = tools["cad.render_openscad"].execute(path="test_model.scad")
        check("render_openscad рассчитывает габариты и объём", "Габариты" in res_render and "Объём" in res_render)
        check("render_openscad проверяет watertight", "Watertight:" in res_render)

        res_stl = tools["cad.inspect_stl"].execute(path="part.stl")
        check("inspect_stl выдаёт точный геометрический анализ меша", "10.0 × 10.0 × 10.0 мм" in res_stl)

        res_fc = tools["cad.freecad_script"].execute(
            script_code="import Part\nbox = Part.makeBox(10, 20, 5)\nPart.show(box)",
            path="my_box.py",
        )
        check("freecad_script сохраняет скрипт моделирования", "сохранён в my_box.py" in res_fc)
        check("файл скрипта создан на диске", ws.exists("my_box.py"))

        res_gear = tools["cad.generate_gear"].execute(
            path="gear20.scad", module_mm=2.0, teeth_count=20
        )
        check("generate_gear генерирует прямозубую шестерню OpenSCAD", "Делительный диаметр: **40.0 мм**" in res_gear)
        check("файл шестерни создан", ws.exists("gear20.scad"))

        res_enc = tools["cad.generate_enclosure"].execute(
            path="box.scad", width_mm=80.0, length_mm=120.0, height_mm=40.0
        )
        check("generate_enclosure генерирует корпус прибора", "Внешние габариты: **80.0 × 120.0 × 40.0 мм**" in res_enc)
        check("файл корпуса создан", ws.exists("box.scad"))

        res_conv = tools["cad.convert_mesh_format"].execute(
            input_path="part.stl", output_path="part.obj", to_format="obj"
        )
        check("convert_mesh_format конвертирует STL в OBJ", "сконвертирован в формат OBJ" in res_conv)
        check("файл OBJ создан", ws.exists("part.obj"))

        res_mass = tools["cad.calculate_mass_inertia"].execute(
            path="part.stl", material="aluminum"
        )
        check("calculate_mass_inertia считает массу и момент инерции Ixx, Iyy, Izz", "Масса:" in res_mass and "Ixx =" in res_mass)

        res_yagi = tools["cad.generate_yagi_openscad"].execute(
            path="yagi433.scad", freq_mhz=433.92, elements_count=5
        )
        check("generate_yagi_openscad строит 3D-модель антенны Яги-Уда", "антенны Яги-Уда OpenSCAD" in res_yagi and "433.92 МГц" in res_yagi)
        check("файл yagi433.scad создан", ws.exists("yagi433.scad"))

        res_prop = tools["cad.generate_propeller_openscad"].execute(
            path="fan.scad", diameter_mm=120.0, blades_count=5, pitch_mm=80.0
        )
        check("generate_propeller_openscad строит пропеллер / крыльчатку", "пропеллера / крыльчатки OpenSCAD" in res_prop and "120.0 мм" in res_prop)
        check("файл fan.scad создан", ws.exists("fan.scad"))

        # Тест cad.render_openscad_views (STL, ракурсы и echo логи)
        p_scad = ws.resolve("echo_test.scad")
        p_scad.write_text(
            '// Тест echo логов\n'
            'echo("ECHO: gear modulus m=2");\n'
            'echo("ECHO: pitch diameter=40");\n'
            'cube([10, 10, 10], center=true);\n',
            encoding="utf-8",
        )
        res_views = tools["cad.render_openscad_views"].execute(
            path="echo_test.scad",
            views_json='["isometric", "top", "front"]',
        )
        check("render_openscad_views экспортирует STL и считает габариты", "10.0 × 10.0 × 10.0 мм" in res_views)
        check("render_openscad_views создаёт изображения в ракурсах", "view_isometric.png" in res_views and "view_top.png" in res_views)
        check("render_openscad_views извлекает логи выполнения (echo)", "gear modulus m=2" in res_views and "pitch diameter=40" in res_views)
        check("файлы изображений и STL созданы в Workspace", ws.exists("echo_test_view_isometric.png") and ws.exists("echo_test.stl"))

    return summary("Тесты инструментов CAD / САПР")


def test_cad_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
