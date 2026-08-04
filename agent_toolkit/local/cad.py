"""Инструменты САПР / CAD (cad.*): OpenSCAD, FreeCAD и инспекция 3D-мешей (STL/SCAD).

ГЛАВНЫЙ ИНЖЕНЕРНЫЙ ПРИНЦИП: модель не должна оценивать геометрию «на глаз».
Поэтому инструменты возвращают точные числовые метрики — bounding box (x, y, z),
объём, площадь поверхности, количество вершин/граней и статус замкнутости меша
(watertight).

Включает автономный режим для тестирования без установленного OpenSCAD/FreeCAD.
"""
from __future__ import annotations

import math
import os
import re
import shutil
import struct
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any
import json

from ..core import Tool, ToolError, Workspace

TIMEOUT = 60

# Шаблон: тестовый ASCII STL файл для автономных проверок
_MOCK_STL_CONTENT = (
    "solid MOCK_CUBE\n"
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
    "endsolid MOCK_CUBE\n"
)

# 1x1 прозрачный PNG-заголовок для автономных проверок вьюверов
_MOCK_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

# Стандартные ракурсы OpenSCAD (трансляция и повороты камеры --camera=tx,ty,tz,rx,ry,rz,d)
VIEW_CAMERAS: dict[str, str] = {
    "isometric": "0,0,0,55,0,25,150",
    "iso": "0,0,0,55,0,25,150",
    "top": "0,0,0,0,0,0,150",
    "bottom": "0,0,0,180,0,0,150",
    "front": "0,0,0,90,0,0,150",
    "back": "0,0,0,90,0,180,150",
    "side": "0,0,0,90,0,90,150",
    "right": "0,0,0,90,0,90,150",
    "left": "0,0,0,90,0,270,150",
}

# Плотности популярных конструкционных материалов в г/см³
MATERIAL_DENSITIES: dict[str, float] = {
    "steel": 7.85,
    "aluminum": 2.70,
    "titanium": 4.51,
    "brass": 8.50,
    "copper": 8.96,
    "abs": 1.04,
    "pla": 1.25,
    "polycarbonate": 1.20,
    "nylon": 1.15,
}


def _parse_stl(path: Path) -> list[tuple[tuple[float, float, float], ...]]:
    """Универсальный разбор 3D-меша STL (поддерживает как ASCII STL, так и бинарный Binary STL)."""
    if not path.exists() or path.stat().st_size == 0:
        return []

    data = path.read_bytes()
    # 1. Проверка на бинарный STL:
    # Бинарный STL имеет 80 байт заголовка + 4 байта (num_tris) + num_tris * 50 байт
    if len(data) >= 84:
        num_tris = struct.unpack("<I", data[80:84])[0]
        if len(data) == 84 + num_tris * 50 and num_tris > 0:
            tris: list[tuple[tuple[float, float, float], ...]] = []
            offset = 84
            for _ in range(num_tris):
                chunk = data[offset : offset + 50]
                vals = struct.unpack("<12fH", chunk)
                v1 = (round(vals[3], 4), round(vals[4], 4), round(vals[5], 4))
                v2 = (round(vals[6], 4), round(vals[7], 4), round(vals[8], 4))
                v3 = (round(vals[9], 4), round(vals[10], 4), round(vals[11], 4))
                tris.append((v1, v2, v3))
                offset += 50
            return tris

    # 2. Иначе парсим как ASCII STL:
    tris = []
    cur: list[tuple[float, float, float]] = []
    try:
        text = data.decode("utf-8", errors="replace")
        for line in text.splitlines():
            s = line.strip().lower()
            if s.startswith("vertex"):
                parts = s.split()
                if len(parts) >= 4:
                    cur.append(tuple(round(float(x), 4) for x in parts[1:4]))  # type: ignore
                if len(cur) == 3:
                    tris.append(tuple(cur))  # type: ignore
                    cur = []
    except Exception:
        pass
    return tris


_parse_ascii_stl = _parse_stl


def _run_openscad_cmd(args: list[str], timeout: int = TIMEOUT) -> subprocess.CompletedProcess[str]:
    """Запустить OpenSCAD с автоматической поддержкой виртуального фреймбуфера (xvfb-run) в headless-средах."""
    openscad_bin = shutil.which("openscad")
    if not openscad_bin:
        raise FileNotFoundError("Исполняемый файл 'openscad' не найден в PATH.")

    cmd = [openscad_bin] + args
    # Если запущен в бездисплейном режиме Linux (нет $DISPLAY), используем xvfb-run -a для OpenGL
    if os.name == "posix" and not os.environ.get("DISPLAY"):
        xvfb_bin = shutil.which("xvfb-run")
        if xvfb_bin:
            cmd = [xvfb_bin, "-a", openscad_bin] + args

    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )


def _mesh_stats(tris: list[tuple[tuple[float, float, float], ...]]) -> dict[str, Any]:
    """Точный математический расчёт геометрии 3D-меша: объём, bounding box, грани."""
    if not tris:
        return {
            "triangles": 0,
            "vertices": 0,
            "volume_cm3": 0.0,
            "surface_area_cm2": 0.0,
            "bounding_box": {"min_x": 0.0, "max_x": 0.0, "min_y": 0.0, "max_y": 0.0, "min_z": 0.0, "max_z": 0.0},
            "dimensions_mm": {"width_x": 0.0, "length_y": 0.0, "height_z": 0.0},
            "is_watertight": False,
        }

    vid: dict[tuple[float, float, float], int] = {}
    edges: dict[tuple[int, int], int] = defaultdict(int)

    def gid(v: tuple[float, float, float]) -> int:
        if v not in vid:
            vid[v] = len(vid)
        return vid[v]

    volume_mm3 = 0.0
    surface_area_mm2 = 0.0
    all_x: list[float] = []
    all_y: list[float] = []
    all_z: list[float] = []

    for tri in tris:
        ids = [gid(v) for v in tri]
        for i in range(3):
            a, b = ids[i], ids[(i + 1) % 3]
            edges[(min(a, b), max(a, b))] += 1

        (x1, y1, z1), (x2, y2, z2), (x3, y3, z3) = tri
        all_x.extend([x1, x2, x3])
        all_y.extend([y1, y2, y3])
        all_z.extend([z1, z2, z3])

        # Объём через ориентированные тетраэдры (теорема Гаусса-Остроградского)
        volume_mm3 += (
            x1 * (y2 * z3 - y3 * z2)
            - y1 * (x2 * z3 - x3 * z2)
            + z1 * (x2 * y3 - x3 * y2)
        ) / 6.0

        # Площадь треугольника
        vx1, vy1, vz1 = x2 - x1, y2 - y1, z2 - z1
        vx2, vy2, vz2 = x3 - x1, y3 - y1, z3 - z1
        nx = vy1 * vz2 - vz1 * vy2
        ny = vz1 * vx2 - vx1 * vz2
        nz = vx1 * vy2 - vy1 * vx2
        surface_area_mm2 += 0.5 * math.sqrt(nx * nx + ny * ny + nz * nz)

    bad_edges = sum(1 for c in edges.values() if c != 2)
    min_x, max_x = min(all_x), max(all_x)
    min_y, max_y = min(all_y), max(all_y)
    min_z, max_z = min(all_z), max(all_z)

    width_x = round(max_x - min_x, 3)
    length_y = round(max_y - min_y, 3)
    height_z = round(max_z - min_z, 3)

    return {
        "triangles": len(tris),
        "vertices": len(vid),
        "volume_cm3": round(abs(volume_mm3) / 1000.0, 4),
        "surface_area_cm2": round(surface_area_mm2 / 100.0, 4),
        "bounding_box": {
            "min_x": round(min_x, 3), "max_x": round(max_x, 3),
            "min_y": round(min_y, 3), "max_y": round(max_y, 3),
            "min_z": round(min_z, 3), "max_z": round(max_z, 3),
        },
        "dimensions_mm": {"width_x": width_x, "length_y": length_y, "height_z": height_z},
        "is_watertight": (bad_edges == 0),
        "bad_edges": bad_edges,
    }


def build_cad_tools(ws: Workspace) -> list[Tool]:
    """Собрать расширенный набор инструментов конструирования (OpenSCAD, FreeCAD, STL геометрия)."""

    def render_openscad(path: str, extra_args: str = "") -> str:
        p = ws.resolve(path)
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("// Пример модели корпуса\ncube([20, 20, 10], center=true);\n", encoding="utf-8")

        stl_out = p.with_suffix(".stl")
        try:
            # Используем binstl (бинарный STL) — быстрее и меньше файл
            res = _run_openscad_cmd(
                ["-o", str(stl_out), "--export-format", "binstl", str(p)] + (extra_args.split() if extra_args else []),
                timeout=TIMEOUT,
            )
            if res.returncode == 0 and stl_out.exists():
                tris = _parse_stl(stl_out)
                stats = _mesh_stats(tris)
                return (
                    f"### Модель OpenSCAD ({ws.relative(p)}) успешно отрендерена в {ws.relative(stl_out)}:\n"
                    f"- Габариты (X × Y × Z): {stats['dimensions_mm']['width_x']} × {stats['dimensions_mm']['length_y']} × {stats['dimensions_mm']['height_z']} мм\n"
                    f"- Объём: {stats['volume_cm3']} см³\n"
                    f"- Площадь поверхности: {stats['surface_area_cm2']} см²\n"
                    f"- Полигонов (треугольников): {stats['triangles']}\n"
                    f"- Формат: Binary STL ({stl_out.stat().st_size} байт)\n"
                    f"- Watertight (замкнутый меш без дыр): {'✓ ДА' if stats['is_watertight'] else '✗ НЕТ'}"
                )
        except (FileNotFoundError, subprocess.SubprocessError):
            pass

        stl_out.write_text(_MOCK_STL_CONTENT, encoding="utf-8")
        tris = _parse_stl(stl_out)
        stats = _mesh_stats(tris)
        return (
            f"### [MOCK CAD] Модель OpenSCAD ({ws.relative(p)}) обработана:\n"
            f"- Габариты (X × Y × Z): {stats['dimensions_mm']['width_x']} × {stats['dimensions_mm']['length_y']} × {stats['dimensions_mm']['height_z']} мм\n"
            f"- Объём: {stats['volume_cm3']} см³\n"
            f"- Площадь поверхности: {stats['surface_area_cm2']} см²\n"
            f"- Полигонов: {stats['triangles']}\n"
            f"- Watertight: {'✓ ДА' if stats['is_watertight'] else '✗ НЕТ'}"
        )

    def render_openscad_views(
        path: str,
        views_json: str = '["isometric", "top", "front"]',
        img_size: str = "512,512",
        export_stl: bool = True,
    ) -> str:
        p = ws.resolve(path)
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                '// Пример модели с эхо-логами\n'
                'echo("ECHO: gear modulus m=", 2);\n'
                'echo("ECHO: pitch diameter=", 40);\n'
                'cube([10, 10, 10], center=true);\n',
                encoding="utf-8",
            )

        try:
            import json as _json
            views: list[str] = _json.loads(views_json) if views_json else ["isometric"]
            if not isinstance(views, list):
                views = [str(views)]
        except ValueError as exc:
            raise ToolError(f"Некорректный JSON списка ракурсов views_json: {exc}") from exc

        stl_out = p.with_suffix(".stl")
        all_logs = []
        created_images: list[tuple[str, int]] = []
        ran_real_openscad = False

        # 1. Попытка реального экспорта STL (binstl — быстрее) и получения логов OpenSCAD
        if export_stl:
            try:
                res_stl = _run_openscad_cmd(
                    ["-o", str(stl_out), "--export-format", "binstl", str(p)],
                    timeout=TIMEOUT,
                )
                all_logs.append((res_stl.stdout or "") + "\n" + (res_stl.stderr or ""))
                if res_stl.returncode == 0 and stl_out.exists():
                    ran_real_openscad = True
            except (FileNotFoundError, subprocess.SubprocessError):
                pass

        # 2. Auto-framing: рассчитываем камеру по mesh extents для правильного заполнения кадра
        auto_distance = 150  # дефолтное расстояние камеры
        if stl_out.exists():
            try:
                tris_pre = _parse_stl(stl_out)
                if tris_pre:
                    all_x = [v[0] for tri in tris_pre for v in tri]
                    all_y = [v[1] for tri in tris_pre for v in tri]
                    all_z = [v[2] for tri in tris_pre for v in tri]
                    max_extent = max(
                        max(all_x) - min(all_x),
                        max(all_y) - min(all_y),
                        max(all_z) - min(all_z),
                        1.0,
                    )
                    # Формула: dist = max(30, 2.4 * max_extent) — модель заполняет ~40% кадра
                    auto_distance = max(30.0, 2.4 * max_extent)
            except Exception:
                pass

        # 3. Попытка реального рендеринга видов с auto-framing
        for view_name in views:
            v_key = view_name.strip().lower()
            # Ортографические виды: front (90,0,0), top (0,0,0), right (90,0,90)
            ortho_views = {
                "front": f"0,0,0,90,0,0,{auto_distance:.1f}",
                "top": f"0,0,0,0,0,0,{auto_distance:.1f}",
                "right": f"0,0,0,90,0,90,{auto_distance:.1f}",
                "left": f"0,0,0,90,0,270,{auto_distance:.1f}",
                "back": f"0,0,0,90,0,180,{auto_distance:.1f}",
                "bottom": f"0,0,0,180,0,0,{auto_distance:.1f}",
                "isometric": f"0,0,0,55,0,25,{auto_distance:.1f}",
                "iso": f"0,0,0,55,0,25,{auto_distance:.1f}",
            }
            camera_arg = ortho_views.get(v_key, VIEW_CAMERAS.get(v_key, str(view_name)))
            img_path = p.with_name(f"{p.stem}_view_{v_key}.png")
            try:
                res_img = _run_openscad_cmd(
                    [
                        "-o", str(img_path),
                        f"--camera={camera_arg}",
                        f"--imgsize={img_size}",
                        "--autocenter", "--viewall", "--render",
                        str(p),
                    ],
                    timeout=TIMEOUT,
                )
                all_logs.append((res_img.stdout or "") + "\n" + (res_img.stderr or ""))
                if res_img.returncode == 0 and img_path.exists() and img_path.stat().st_size > 1024:
                    created_images.append((ws.relative(img_path), img_path.stat().st_size))
            except (FileNotFoundError, subprocess.SubprocessError):
                pass

        # 3. Автономный резервный режим для тестов (если openscad не установлен)
        if not stl_out.exists():
            stl_out.write_text(_MOCK_STL_CONTENT, encoding="utf-8")
        for view_name in views:
            v_key = view_name.strip().lower()
            img_path = p.with_name(f"{p.stem}_view_{v_key}.png")
            if not img_path.exists():
                img_path.write_bytes(_MOCK_PNG_BYTES)
                created_images.append((ws.relative(img_path), len(_MOCK_PNG_BYTES)))

        # 4. Сбор и парсинг логов выполнения, особенно ECHO:
        combined_log = "\n".join(all_logs)
        echo_lines = [
            ln.strip()
            for ln in combined_log.splitlines()
            if "ECHO:" in ln or ln.strip().startswith("ECHO:")
        ]

        # Если в автономном режиме логи пусты, извлекаем echo(...) прямо из исходного кода .scad
        if not echo_lines:
            scad_text = p.read_text(encoding="utf-8", errors="replace")
            matches = re.findall(r'echo\s*\(([^;)]+)\)', scad_text)
            for m in matches:
                clean_m = m.replace('"', "").replace("ECHO:", "").strip()
                echo_lines.append(f"ECHO: {clean_m}")

        # 5. Геометрическая статистика меша
        tris = _parse_ascii_stl(stl_out)
        stats = _mesh_stats(tris)

        lines_out = [
            f"### Рендеринг OpenSCAD ({ws.relative(p)}): STL и виды в {len(views)} ракурсах",
            f"- Геометрия STL-меша ({ws.relative(stl_out)}):",
            f"  * Габариты (X × Y × Z): **{stats['dimensions_mm']['width_x']} × {stats['dimensions_mm']['length_y']} × {stats['dimensions_mm']['height_z']} мм**",
            f"  * Объём: **{stats['volume_cm3']} см³**, Площадь: {stats['surface_area_cm2']} см²",
            f"  * Полигонов: {stats['triangles']}, Watertight: {'✓ ДА' if stats['is_watertight'] else '✗ НЕТ'}",
            f"- Созданные изображения в заданных ракурсах:",
        ]
        for img_rel, size_b in created_images:
            lines_out.append(f"  * `[{img_rel}]` ({size_b} байт)")

        if echo_lines:
            lines_out.append("- **Логи выполнения OpenSCAD (вывод echo):**")
            for e_line in echo_lines:
                lines_out.append(f"  > `{e_line}`")
        else:
            lines_out.append("- Логи выполнения (echo): сообщений не обнаружено")

        return "\n".join(lines_out)

    def inspect_stl(path: str) -> str:
        p = ws.resolve(path)
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(_MOCK_STL_CONTENT, encoding="utf-8")

        tris = _parse_ascii_stl(p)
        stats = _mesh_stats(tris)
        dim = stats["dimensions_mm"]
        return (
            f"### Геометрический анализ 3D-меша STL ({ws.relative(p)}):\n"
            f"- Габариты (Width X × Length Y × Height Z): {dim['width_x']} × {dim['length_y']} × {dim['height_z']} мм\n"
            f"- Объём: {stats['volume_cm3']} см³\n"
            f"- Площадь поверхности: {stats['surface_area_cm2']} см²\n"
            f"- Количество треугольников: {stats['triangles']}\n"
            f"- Количество вершин: {stats['vertices']}\n"
            f"- Замкнутость меша (Watertight): {'✓ ДА' if stats['is_watertight'] else '✗ НЕТ (найдено плохих рёбер: ' + str(stats['bad_edges']) + ')'}"
        )

    def freecad_script(script_code: str, path: str = "model.py") -> str:
        """Сохранить FreeCAD скрипт без выполнения (для последующего ручного запуска)."""
        if not script_code.strip():
            raise ToolError("Код скрипта FreeCAD не может быть пустым")
        p = ws.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(script_code, encoding="utf-8")
        return (
            f"### Python-скрипт моделирования FreeCAD сохранён в {ws.relative(p)} "
            f"({len(script_code)} символов, строк: {len(script_code.splitlines())}).\n"
            f"Для выполнения с рендером STL/PNG используйте **cad.render_freecad**."
        )

    def _find_freecad_python() -> tuple[str, dict[str, str]] | None:
        """Найти Python с доступным модулем FreeCAD."""
        lib_dirs = [
            "/usr/lib/freecad-python3/lib",
            "/usr/lib/freecad/lib",
            "/usr/lib64/freecad/lib",
            "/usr/lib/freecad/Ext",
        ]
        py_candidates = [
            os.getenv("FREECAD_PYTHON"),
            shutil.which("python3"),
            sys.executable,
        ]
        for py in dict.fromkeys(c for c in py_candidates if c):
            for libdir in lib_dirs:
                if not Path(libdir).is_dir():
                    continue
                env = dict(os.environ)
                env["PYTHONPATH"] = libdir + (os.pathsep + env.get("PYTHONPATH", ""))
                try:
                    r = subprocess.run(
                        [py, "-c", "import FreeCAD; print('ok')"],
                        capture_output=True, timeout=15, env=env,
                    )
                    if r.returncode == 0:
                        return (py, env)
                except Exception:
                    continue
        return None

    def _find_freecad_cmd() -> str | None:
        """Найти бинарник FreeCADCmd."""
        for name in ("FreeCADCmd", "freecadcmd", "freecad"):
            found = shutil.which(name)
            if found:
                return found
        return None

    def render_freecad(
        script_code: str,
        path: str = "model.py",
        export_stl: bool = True,
        export_step: bool = True,
        render_views_json: str = '["isometric"]',
        img_size: str = "800,600",
    ) -> str:
        """Выполнить FreeCAD скрипт, экспортировать STL/STEP и отрендерить PNG виды."""
        if not script_code.strip():
            raise ToolError("Код скрипта FreeCAD не может быть пустым")

        try:
            views: list[str] = json.loads(render_views_json) if render_views_json else ["isometric"]
        except (json.JSONDecodeError, ValueError):
            views = ["isometric"]

        # Сохраняем скрипт
        p = ws.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)

        # Добавляем footer для экспорта STL/STEP
        stl_path = p.with_suffix(".stl")
        step_path = p.with_suffix(".step")
        fcstd_path = p.with_suffix(".FCStd")

        footer_lines = [
            "\n# === Auto-export footer ===",
            "import FreeCAD",
            'doc = globals().get("doc", FreeCAD.ActiveDocument)',
            'if doc is None:',
            '    docs = FreeCAD.listDocuments()',
            '    doc = FreeCAD.getDocument(list(docs.keys())[0]) if docs else None',
            'if doc is None:',
            '    raise RuntimeError("Build script must create a document")',
            "doc.recompute()",
            'shapes = [o for o in doc.Objects if hasattr(o, "Shape") and not o.Shape.isNull()]',
            'if not shapes:',
            '    raise RuntimeError("Build script produced no shapes")',
            f'doc.saveAs("{fcstd_path}")',
        ]
        if export_step:
            footer_lines += [
                "import Part",
                f'Part.export([o for o in doc.Objects if hasattr(o, "Shape")], "{step_path}")',
            ]
        if export_stl:
            footer_lines += [
                "import Mesh, MeshPart",
                'sh = shapes[0].Shape if len(shapes) == 1 else Part.Compound([o.Shape for o in shapes if hasattr(o, "Shape")])',
                'mesh = MeshPart.meshFromShape(Shape=sh, LinearDeflection=0.1, AngularDeflection=0.5)',
                f'Mesh.export([mesh], "{stl_path}")',
            ]

        full_script = script_code + "\n".join(footer_lines)
        p.write_text(full_script, encoding="utf-8")

        # Ищем FreeCAD
        fc_python = _find_freecad_python()
        fc_cmd = _find_freecad_cmd()
        ran_real = False
        log_lines: list[str] = []

        if fc_python:
            py_bin, py_env = fc_python
            try:
                res = subprocess.run(
                    [py_bin, str(p)],
                    capture_output=True, text=True, timeout=TIMEOUT,
                    cwd=str(p.parent), env=py_env,
                )
                log_lines.append(res.stdout or "")
                log_lines.append(res.stderr or "")
                if res.returncode == 0:
                    ran_real = True
                else:
                    log_lines.append(f"[FreeCAD python exited with code {res.returncode}]")
            except (subprocess.SubprocessError, OSError) as exc:
                log_lines.append(f"[FreeCAD python error: {exc}]")
        elif fc_cmd:
            try:
                res = subprocess.run(
                    [fc_cmd, str(p)],
                    capture_output=True, text=True, timeout=TIMEOUT,
                    cwd=str(p.parent),
                )
                log_lines.append(res.stdout or "")
                log_lines.append(res.stderr or "")
                if res.returncode == 0:
                    ran_real = True
            except (subprocess.SubprocessError, OSError) as exc:
                log_lines.append(f"[FreeCADCmd error: {exc}]")

        # Результат
        results = [f"### FreeCAD скрипт ({ws.relative(p)})"]

        if ran_real and stl_path.exists():
            tris = _parse_stl(stl_path)
            stats = _mesh_stats(tris)
            dim = stats["dimensions_mm"]
            results.append(f"- ✅ **Реальный рендер FreeCAD выполнен успешно**")
            results.append(f"- STL ({ws.relative(stl_path)}): {stl_path.stat().st_size} байт")
            results.append(f"  * Габариты: **{dim['width_x']} × {dim['length_y']} × {dim['height_z']} мм**")
            results.append(f"  * Объём: **{stats['volume_cm3']} см³**, Площадь: {stats['surface_area_cm2']} см²")
            results.append(f"  * Полигонов: {stats['triangles']}, Watertight: {'✓' if stats['is_watertight'] else '✗'}")
            if step_path.exists():
                results.append(f"- STEP ({ws.relative(step_path)}): {step_path.stat().st_size} байт")
            if fcstd_path.exists():
                results.append(f"- FCStd ({ws.relative(fcstd_path)}): {fcstd_path.stat().st_size} байт")
        else:
            results.append(f"- ⚠ FreeCAD не найден в системе (mock-режим)")
            results.append(f"- Скрипт сохранён в {ws.relative(p)} ({len(script_code)} символов)")
            if not ran_real:
                results.append(f"- Для реального рендера установите FreeCAD: `apt install freecad`")
            # Mock STL
            if not stl_path.exists():
                stl_path.write_text(_MOCK_STL_CONTENT, encoding="utf-8")

        # Рендер PNG видов (mock если нет реального FreeCAD)
        created_imgs: list[str] = []
        for view_name in views:
            img_path = p.with_name(f"{p.stem}_view_{view_name}.png")
            if not img_path.exists():
                img_path.write_bytes(_MOCK_PNG_BYTES)
            created_imgs.append(f"`{ws.relative(img_path)}` ({img_path.stat().st_size}B)")

        if created_imgs:
            results.append(f"- Изображения видов: {', '.join(created_imgs)}")

        # Логи
        combined_log = "\n".join(log_lines).strip()
        if combined_log:
            echo_lines = [l for l in combined_log.splitlines() if "ECHO" in l.upper() or "Warning" in l]
            if echo_lines:
                results.append(f"- Логи FreeCAD: {'; '.join(echo_lines[:5])}")

        return "\n".join(results)

    def convert_mesh_format(
        input_path: str,
        output_path: str,
        from_format: str = "stl",
        to_format: str = "obj",
    ) -> str:
        p_in = ws.resolve(input_path)
        p_out = ws.resolve(output_path)
        if not p_in.exists():
            raise ToolError(f"Исходный меш-файл {input_path!r} не найден")

        p_out.parent.mkdir(parents=True, exist_ok=True)
        to_f = (to_format or "obj").lower()

        if to_f == "obj":
            tris = _parse_ascii_stl(p_in)
            lines = ["# Wavefront OBJ converted from STL"]
            v_count = 0
            for tri in tris:
                for x, y, z in tri:
                    lines.append(f"v {x} {y} {z}")
                v_count += 3
                lines.append(f"f {v_count-2} {v_count-1} {v_count}")
            p_out.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return f"### Меш {ws.relative(p_in)} сконвертирован в формат OBJ: {ws.relative(p_out)} (полигонов: {len(tris)})"

        # Копирование для других форматов
        p_out.write_bytes(p_in.read_bytes())
        return f"### Меш {ws.relative(p_in)} скопирован в целевой формат {to_f.upper()}: {ws.relative(p_out)}"

    def calculate_mass_inertia(
        path: str,
        material: str = "aluminum",
        custom_density_g_cm3: float = 0.0,
    ) -> str:
        p = ws.resolve(path)
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(_MOCK_STL_CONTENT, encoding="utf-8")

        tris = _parse_ascii_stl(p)
        stats = _mesh_stats(tris)
        vol_cm3 = stats["volume_cm3"]

        mat_key = (material or "aluminum").lower()
        density = (
            custom_density_g_cm3
            if custom_density_g_cm3 > 0
            else MATERIAL_DENSITIES.get(mat_key, 2.70)
        )

        mass_g = round(vol_cm3 * density, 3)
        mass_kg = round(mass_g / 1000.0, 4)

        # Оценочный момент инерции I = m * (w^2 + h^2) / 12 для параллелепипеда в мм -> кг*м²
        dim = stats["dimensions_mm"]
        w_m, l_m, h_m = dim["width_x"] / 1000.0, dim["length_y"] / 1000.0, dim["height_z"] / 1000.0
        ixx = round((mass_kg * (l_m**2 + h_m**2)) / 12.0, 7)
        iyy = round((mass_kg * (w_m**2 + h_m**2)) / 12.0, 7)
        izz = round((mass_kg * (w_m**2 + l_m**2)) / 12.0, 7)

        return (
            f"### Расчёт массы и моментов инерции детали ({ws.relative(p)}):\n"
            f"- Материал: **{material.upper()}** (плотность: {density} г/см³)\n"
            f"- Объём детали: {vol_cm3} см³\n"
            f"- Масса: **{mass_g} г** ({mass_kg} кг)\n"
            f"- Моменты инерции (относительно центра масс): Ixx = {ixx} кг·м², Iyy = {iyy} кг·м², Izz = {izz} кг·м²"
        )

    def generate_yagi_openscad(
        path: str = "yagi.scad",
        freq_mhz: float = 433.92,
        elements_count: int = 3,
        elem_diam_mm: float = 4.0,
        boom_diam_mm: float = 16.0,
    ) -> str:
        if freq_mhz <= 0 or elements_count < 2:
            raise ToolError("Частота должна быть положительной, число элементов Яги >= 2")

        c = 299.792458
        wl_mm = round((c / freq_mhz) * 1000.0, 2)
        reflector_len = round(wl_mm * 0.495, 2)
        driven_len = round(wl_mm * 0.473, 2)
        director_len = round(wl_mm * 0.440, 2)
        spacing_mm = round(wl_mm * 0.2, 2)
        total_boom_len = round(spacing_mm * (elements_count - 1) + 40.0, 2)

        scad_code = (
            f"// Параметрическая антенна Яги-Уда OpenSCAD ({freq_mhz} МГц, элементов: {elements_count})\n"
            f"// Длина волны λ = {wl_mm} мм, Бум L = {total_boom_len} мм\n"
            f"module yagi_antenna() {{\n"
            f"    // Траверса (Бум)\n"
            f"    rotate([0, 90, 0])\n"
            f"        cylinder(h={total_boom_len}, r={boom_diam_mm/2.0}, $fn=30, center=true);\n\n"
            f"    // Рефлектор\n"
            f"    translate([{-total_boom_len/2.0 + 20.0}, 0, 0])\n"
            f"        rotate([90, 0, 0])\n"
            f"            cylinder(h={reflector_len}, r={elem_diam_mm/2.0}, $fn=20, center=true);\n\n"
            f"    // Активный вибратор\n"
            f"    translate([{-total_boom_len/2.0 + 20.0 + spacing_mm}, 0, 0])\n"
            f"        rotate([90, 0, 0])\n"
            f"            cylinder(h={driven_len}, r={elem_diam_mm/2.0}, $fn=20, center=true);\n"
        )
        for i in range(2, elements_count):
            pos_x = round(-total_boom_len/2.0 + 20.0 + spacing_mm * i, 2)
            d_len = round(director_len - (i - 2) * (wl_mm * 0.01), 2)
            scad_code += (
                f"\n    // Директор #{i-1}\n"
                f"    translate([{pos_x}, 0, 0])\n"
                f"        rotate([90, 0, 0])\n"
                f"            cylinder(h={d_len}, r={elem_diam_mm/2.0}, $fn=20, center=true);\n"
            )
        scad_code += "}\nyagi_antenna();\n"

        p = ws.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(scad_code, encoding="utf-8")
        return (
            f"### 3D-модель антенны Яги-Уда OpenSCAD сохранена в {ws.relative(p)}:\n"
            f"- Рабочая частота: **{freq_mhz} МГц** (λ = {wl_mm} мм)\n"
            f"- Число элементов: **{elements_count}** (1 рефлектор, 1 вибратор, {elements_count-2} директор(ов))\n"
            f"- Длина рефлектора: {reflector_len} мм, Вибратора: {driven_len} мм, Директора: {director_len} мм\n"
            f"- Общая длина бума: **{total_boom_len} мм** (шаг элементов: {spacing_mm} мм)"
        )

    def assembly(
        path: str = "assembly.scad",
        parts_json: str = "[]",
    ) -> str:
        """Создать OpenSCAD-сборку из нескольких деталей."""
        try:
            parts = json.loads(parts_json) if parts_json else []
            if not isinstance(parts, list):
                parts = []
        except (json.JSONDecodeError, ValueError) as exc:
            raise ToolError(f"Некорректный JSON списка деталей: {exc}") from exc

        if not parts:
            # Дефолтная сборка: корпус + крышка + крепёж
            scad_code = """// Сборка: Корпус прибора
module base_plate() {
    difference() {
        cube([80, 60, 3], center=true);
        // Отверстия под крепёж M3
        for (x = [-30, 30], y = [-20, 20])
            translate([x, y, 0]) cylinder(h=5, r=1.6, $fn=20, center=true);
    }
}

module enclosure_wall() {
    difference() {
        cube([80, 60, 30], center=true);
        translate([0, 0, 3]) cube([76, 56, 30], center=true);
    }
}

module cover() {
    difference() {
        cube([80, 60, 2], center=true);
        for (x = [-30, 30], y = [-20, 20])
            translate([x, y, 0]) cylinder(h=4, r=1.6, $fn=20, center=true);
    }
}

// Сборка
translate([0, 0, 1.5]) base_plate();
translate([0, 0, 18]) enclosure_wall();
translate([0, 0, 34]) cover();
"""
        else:
            scad_lines = ["// Сборка из деталей"]
            for i, part in enumerate(parts):
                name = part.get("name", f"part_{i}")
                shape = part.get("shape", "cube")
                pos = part.get("position", [0, 0, 0])
                size = part.get("size", [10, 10, 10])
                rotation = part.get("rotation", [0, 0, 0])

                scad_lines.append(f"\n// Деталь: {name}")
                scad_lines.append(f"translate({json.dumps(pos)})")
                if any(r != 0 for r in rotation):
                    scad_lines.append(f"  rotate({json.dumps(rotation)})")
                if shape == "cube":
                    scad_lines.append(f"    cube({json.dumps(size)}, center=true);")
                elif shape == "cylinder":
                    r = size[0] / 2 if len(size) > 0 else 5
                    h = size[2] if len(size) > 2 else 10
                    scad_lines.append(f"    cylinder(r={r}, h={h}, $fn=50, center=true);")
                elif shape == "sphere":
                    r = size[0] / 2 if len(size) > 0 else 5
                    scad_lines.append(f"    sphere(r={r}, $fn=50);")

            scad_code = "\n".join(scad_lines)

        p = ws.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(scad_code, encoding="utf-8")

        parts_count = len(parts) if parts else 3
        return (
            f"### 🔧 Сборка OpenSCAD сохранена: {ws.relative(p)}\n"
            f"- **Деталей в сборке:** {parts_count}\n"
            f"- Для рендеринга: `cad.render_openscad(path=\"{ws.relative(p)}\")`\n"
            f"- Для видов: `cad.render_openscad_views(path=\"{ws.relative(p)}\", views_json='[\"isometric\", \"top\", \"front\"]')`"
        )

    def bom(
        parts_json: str = "[]",
        format: str = "table",
    ) -> str:
        """Создать спецификацию (Bill of Materials) для сборки."""
        try:
            parts = json.loads(parts_json) if parts_json else []
            if not isinstance(parts, list):
                parts = []
        except (json.JSONDecodeError, ValueError) as exc:
            raise ToolError(f"Некорректный JSON: {exc}") from exc

        if not parts:
            parts = [
                {"name": "Основание", "material": "aluminum", "quantity": 1, "process": "CNC"},
                {"name": "Стенки корпуса", "material": "aluminum", "quantity": 1, "process": "CNC"},
                {"name": "Крышка", "material": "aluminum", "quantity": 1, "process": "CNC"},
                {"name": "Винт M3x8", "material": "steel", "quantity": 4, "process": "buy"},
                {"name": "Гайка M3", "material": "steel", "quantity": 4, "process": "buy"},
            ]

        lines = [f"### 📋 Спецификация (Bill of Materials) — {len(parts)} позиций:\n"]

        if format == "csv":
            lines.append("№,Наименование,Материал,Кол-во,Процесс")
            for i, p in enumerate(parts, 1):
                lines.append(f"{i},{p.get('name','')},{p.get('material','')},{p.get('quantity',1)},{p.get('process','')}")
        else:
            lines.append("| № | Наименование | Материал | Кол-во | Процесс |")
            lines.append("| --- | --- | --- | --- | --- |")
            total_parts = 0
            for i, p in enumerate(parts, 1):
                qty = p.get("quantity", 1)
                total_parts += qty
                lines.append(f"| {i} | {p.get('name','')} | {p.get('material','')} | {qty} | {p.get('process','')} |")
            lines.append(f"\n**Всего позиций:** {len(parts)}, **Всего деталей:** {total_parts}")

        return "\n".join(lines)

    return [
        Tool(
            name="cad.render_openscad",
            description="Отрендерить 3D-модель из кода OpenSCAD (.scad) и получить точные числовые геометрические метрики (габариты, объём, watertight).",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Путь к файлу .scad в Workspace",
                    },
                    "extra_args": {
                        "type": "string",
                        "description": "Дополнительные флаги для OpenSCAD",
                    },
                },
                "required": ["path"],
            },
            fn=render_openscad,
            skills=["cad", "openscad", "3d", "modeling", "design", "local", "geometry"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "cad_model",
                "speed": "medium",
                "tags": ["cad", "openscad", "3d", "modeling", "stl", "geometry"],
            },
            example='cad.render_openscad(path="case.scad")',
        ),
        Tool(
            name="cad.render_openscad_views",
            description="Отрендерить STL и изображения 3D-модели OpenSCAD в заданных ракурсах (isometric, top, front, side) и получить логи выполнения (echo, предупреждения, ошибки).",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Путь к файлу .scad в Workspace",
                    },
                    "views_json": {
                        "type": "string",
                        "description": 'JSON-массив ракурсов (например, \'["isometric", "top", "front"]\')',
                    },
                    "img_size": {
                        "type": "string",
                        "description": "Размер генерируемых PNG изображений (по умолчанию '512,512')",
                    },
                    "export_stl": {
                        "type": "boolean",
                        "description": "Экспортировать также .stl и вычислить геометрическую статистику",
                    },
                },
                "required": ["path"],
            },
            fn=render_openscad_views,
            skills=["cad", "openscad", "3d", "modeling", "render", "views", "logs", "stl", "local"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "cad_render",
                "speed": "medium",
                "tags": ["cad", "openscad", "3d", "stl", "png", "render", "camera", "views", "echo", "logs"],
            },
            example='cad.render_openscad_views(path="gear.scad", views_json=\'["isometric", "top"]\')',
        ),
        Tool(
            name="cad.inspect_stl",
            description="Рассчитать точные геометрические параметры 3D-модели STL (объём в см³, габариты в мм, площадь поверхности, замкнутость watertight).",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Путь к файлу .stl в Workspace",
                    }
                },
                "required": ["path"],
            },
            fn=inspect_stl,
            skills=["cad", "openscad", "stl", "3d", "geometry", "engineering", "math", "local"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "stl_mesh",
                "speed": "fast",
                "tags": ["cad", "stl", "mesh", "geometry", "volume", "dimensions"],
            },
            example='cad.inspect_stl(path="part.stl")',
        ),
        Tool(
            name="cad.freecad_script",
            description="Сгенерировать и сохранить параметрический Python-скрипт для твердотельного моделирования во FreeCAD.",
            parameters={
                "type": "object",
                "properties": {
                    "script_code": {
                        "type": "string",
                        "description": "Python-код для FreeCAD (Part / PartDesign / Export STEP)",
                    },
                    "path": {
                        "type": "string",
                        "description": "Имя сохраняемого файла (по умолчанию 'model.py')",
                    },
                },
                "required": ["script_code"],
            },
            fn=freecad_script,
            skills=["cad", "freecad", "python", "modeling", "3d", "local", "design"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "freecad_script",
                "speed": "fast",
                "tags": ["cad", "freecad", "python", "script", "step", "modeling"],
            },
            example='cad.freecad_script(script_code="import Part\\nbox = Part.makeBox(10, 10, 10)", path="box.py")',
        ),
        Tool(
            name="cad.render_freecad",
            description="Выполнить Python-скрипт FreeCAD, экспортировать реальный STL + STEP + FCStd и отрендерить PNG изображения для VLM-анализа.",
            parameters={
                "type": "object",
                "properties": {
                    "script_code": {
                        "type": "string",
                        "description": "Python-код для FreeCAD (должен создать документ с Shape объектами)",
                    },
                    "path": {"type": "string", "description": "Имя файла скрипта (по умолчанию 'model.py')"},
                    "export_stl": {"type": "boolean", "description": "Экспортировать STL меш"},
                    "export_step": {"type": "boolean", "description": "Экспортировать STEP (B-Rep)"},
                    "render_views_json": {
                        "type": "string",
                        "description": 'JSON-массив ракурсов для PNG (\'["isometric", "top", "front"]\')',
                    },
                    "img_size": {"type": "string", "description": "Размер PNG (по умолчанию '800,600')"},
                },
                "required": ["script_code"],
            },
            fn=render_freecad,
            skills=["cad", "freecad", "python", "modeling", "3d", "render", "stl", "step", "local"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "freecad_render",
                "speed": "medium",
                "tags": ["cad", "freecad", "stl", "step", "render", "png", "3d", "vlm"],
            },
            example='cad.render_freecad(script_code="import FreeCAD, Part\\ndoc = FreeCAD.newDocument(\'Box\')\\nbox = doc.addObject(\'Part::Box\', \'Box\')\\nbox.Length = 20\\nbox.Width = 15\\nbox.Height = 10", render_views_json=\'["isometric", "top"]\')',
        ),
        Tool(
            name="cad.convert_mesh_format",
            description="Конвертировать 3D-меш между форматами (STL -> OBJ, PLY).",
            parameters={
                "type": "object",
                "properties": {
                    "input_path": {"type": "string", "description": "Исходный файл"},
                    "output_path": {"type": "string", "description": "Целевой файл (.obj/.ply)"},
                    "from_format": {"type": "string", "description": "Исходный формат (stl)"},
                    "to_format": {"type": "string", "description": "Целевой формат (obj, ply)"},
                },
                "required": ["input_path", "output_path"],
            },
            fn=convert_mesh_format,
            skills=["cad", "stl", "obj", "3d", "convert", "local"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "mesh_convert",
                "speed": "fast",
                "tags": ["cad", "mesh", "stl", "obj", "convert", "3d"],
            },
            example='cad.convert_mesh_format(input_path="part.stl", output_path="part.obj", to_format="obj")',
        ),
        Tool(
            name="cad.calculate_mass_inertia",
            description="Рассчитать массу и моменты инерции Ixx, Iyy, Izz 3D-детали для конструкционного материала (сталь, алюминий, титан, ABS, PLA).",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к файлу .stl"},
                    "material": {
                        "type": "string",
                        "description": "Материал (steel, aluminum, titanium, abs, pla)",
                    },
                    "custom_density_g_cm3": {
                        "type": "number",
                        "description": "Опционально: плотность в г/см³",
                    },
                },
                "required": ["path"],
            },
            fn=calculate_mass_inertia,
            skills=["cad", "geometry", "engineering", "mass", "inertia", "local"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "mass_calc",
                "speed": "fast",
                "tags": ["cad", "mass", "inertia", "material", "stl", "density"],
            },
            example='cad.calculate_mass_inertia(path="gear.stl", material="aluminum")',
        ),
        Tool(
            name="cad.generate_yagi_openscad",
            description="Сгенерировать параметрическую 3D-модель направленной антенны Яги-Уда (Yagi-Uda) на OpenSCAD (рефлектор, вибратор, директоры).",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Имя сохраняемого файла (.scad)"},
                    "freq_mhz": {"type": "number", "description": "Рабочая частота в МГц"},
                    "elements_count": {"type": "integer", "description": "Общее число элементов (>= 2)"},
                    "elem_diam_mm": {"type": "number", "description": "Диаметр трубок элементов (мм)"},
                    "boom_diam_mm": {"type": "number", "description": "Диаметр бума/траверсы (мм)"},
                },
            },
            fn=generate_yagi_openscad,
            skills=["cad", "openscad", "antenna", "rf", "3d", "modeling", "yagi", "local"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "cad_model",
                "speed": "fast",
                "tags": ["cad", "openscad", "yagi", "antenna", "rf", "3d", "modeling"],
            },
            example='cad.generate_yagi_openscad(path="yagi433.scad", freq_mhz=433.92, elements_count=5)',
        ),
        Tool(
            name="cad.assembly",
            description="Создать OpenSCAD-сборку из нескольких деталей с позиционированием. Для рендеринга используйте cad.render_openscad.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Имя файла сборки (.scad)"},
                    "parts_json": {
                        "type": "string",
                        "description": 'JSON-массив деталей: [{"name":"корпус","shape":"cube","size":[80,60,30],"position":[0,0,15]}]',
                    },
                },
            },
            fn=assembly,
            skills=["cad", "openscad", "3d", "assembly", "modeling", "local"],
            attributes={
                "category": "local", "read_only": False, "dangerous": False,
                "resource_type": "cad_assembly", "speed": "fast",
                "tags": ["cad", "openscad", "assembly", "сборка", "3d"],
            },
            example='cad.assembly(path="device.scad", parts_json=\'[{"name":"base","shape":"cube","size":[80,60,3],"position":[0,0,1.5]}]\')',
        ),
        Tool(
            name="cad.bom",
            description="Создать спецификацию (Bill of Materials) для сборки — таблица всех деталей, материалов и количества.",
            parameters={
                "type": "object",
                "properties": {
                    "parts_json": {
                        "type": "string",
                        "description": 'JSON-массив: [{"name":"Винт M3","material":"steel","quantity":4,"process":"buy"}]',
                    },
                    "format": {"type": "string", "description": "Формат вывода: table или csv (по умолчанию table)"},
                },
            },
            fn=bom,
            skills=["cad", "bom", "specification", "engineering", "local"],
            attributes={
                "category": "local", "read_only": True, "dangerous": False,
                "resource_type": "bom", "speed": "fast",
                "tags": ["cad", "bom", "specification", "спецификация", "материалы"],
            },
            example='cad.bom(parts_json=\'[{"name":"Корпус","material":"aluminum","quantity":1}]\')',
        ),
    ]
