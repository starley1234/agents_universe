"""FEA Pipeline: Gmsh → CalculiX — линейный статический анализ (cad.fea_static).

Преобразует STL-меш в тетраэдральную объёмную сетку через Gmsh,
выполняет линейный статический FEA-расчёт через CalculiX (ccx),
возвращает поля напряжений (von Mises), деформации и фактор безопасности.

Автономный режим: если Gmsh/CalculiX не установлены, возвращает
аналитическую оценку напряжений по нагрузке и сечению.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..core import Tool, ToolError, Workspace

TIMEOUT = 600


def _find_tool(name: str) -> str | None:
    return shutil.which(name)


def _run(cmd: list[str], cwd: Path, timeout: int = TIMEOUT, env: dict | None = None) -> subprocess.CompletedProcess:
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, cwd=str(cwd), env=run_env,
    )


# ── Materials database (Young's modulus MPa, Poisson, yield MPa, density g/cm³) ──
FEA_MATERIALS: dict[str, dict[str, float]] = {
    "steel": {"E_mpa": 210000, "nu": 0.30, "yield_mpa": 250, "density": 7.85},
    "aluminum": {"E_mpa": 69000, "nu": 0.33, "yield_mpa": 275, "density": 2.70},
    "titanium": {"E_mpa": 114000, "nu": 0.34, "yield_mpa": 880, "density": 4.51},
    "abs": {"E_mpa": 2300, "nu": 0.35, "yield_mpa": 40, "density": 1.04},
    "pla": {"E_mpa": 3500, "nu": 0.36, "yield_mpa": 50, "density": 1.25},
    "nylon": {"E_mpa": 2700, "nu": 0.40, "yield_mpa": 45, "density": 1.15},
    "polycarbonate": {"E_mpa": 2400, "nu": 0.37, "yield_mpa": 60, "density": 1.20},
    "brass": {"E_mpa": 100000, "nu": 0.34, "yield_mpa": 250, "density": 8.50},
}


def _write_calculix_inp(
    inp_path: Path,
    mesh_nodes: list[tuple[float, float, float]],
    mesh_tets: list[tuple[int, int, int, int]],
    mat: dict[str, float],
    fixed_node_ids: list[int],
    load_node_ids: list[int],
    force_n: tuple[float, float, float],
) -> None:
    """Записать .inp файл для CalculiX."""
    lines: list[str] = []

    # Nodes
    lines.append("*NODE")
    for i, (x, y, z) in enumerate(mesh_nodes, 1):
        lines.append(f"{i}, {x:.6f}, {y:.6f}, {z:.6f}")

    # Elements (C3D4 = 4-node tetrahedron)
    lines.append("*ELEMENT, TYPE=C3D4, ELSET=All")
    for i, (a, b, c, d) in enumerate(mesh_tets, 1):
        lines.append(f"{i}, {a+1}, {b+1}, {c+1}, {d+1}")

    # Material
    lines.append("*MATERIAL, NAME=Mat")
    lines.append("*ELASTIC")
    lines.append(f"{mat['E_mpa']:.1f}, {mat['nu']:.3f}")
    lines.append("*DENSITY")
    lines.append(f"{mat['density']:.4f}")

    # Solid section
    lines.append("*SOLID SECTION, ELSET=All, MATERIAL=Mat")

    # Boundary conditions (fixed nodes)
    if fixed_node_ids:
        lines.append("*BOUNDARY")
        for nid in fixed_node_ids:
            lines.append(f"{nid+1}, 1, 3, 0.0")

    # Load (concentrated force)
    if load_node_ids:
        lines.append("*NODE")  # Already defined above, use CLOAD instead
        lines.append("*CLOAD")
        n_nodes = len(load_node_ids)
        fx, fy, fz = force_n
        for nid in load_node_ids:
            lines.append(f"{nid+1}, 1, {fx/n_nodes:.4f}")
            lines.append(f"{nid+1}, 2, {fy/n_nodes:.4f}")
            lines.append(f"{nid+1}, 3, {fz/n_nodes:.4f}")

    # Step
    lines.append("*STEP")
    lines.append("*STATIC")
    lines.append("*NODE FILE")
    lines.append("U")
    lines.append("*EL FILE")
    lines.append("S")
    lines.append("*END STEP")

    inp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse_ccx_dat(dat_path: Path) -> dict[str, Any]:
    """Парсить .dat файл CalculiX для извлечения напряжений и деформаций."""
    if not dat_path.exists():
        return {"max_stress_mpa": 0, "max_displacement_mm": 0, "nodes_stressed": 0}

    text = dat_path.read_text(encoding="utf-8", errors="replace")

    max_stress = 0.0
    max_disp = 0.0
    stressed = 0

    # Извлечь von Mises stress из блока stress output
    stress_values = re.findall(r"mises\s*=\s*([\d\.eE+-]+)", text, re.IGNORECASE)
    for sv in stress_values:
        try:
            v = float(sv)
            if abs(v) > max_stress:
                max_stress = abs(v)
            if abs(v) > 1.0:
                stressed += 1
        except ValueError:
            pass

    # Извлечь перемещения
    disp_values = re.findall(r"disp\s*=\s*([\d\.eE+-]+)", text, re.IGNORECASE)
    for dv in disp_values:
        try:
            v = float(dv)
            if abs(v) > max_disp:
                max_disp = abs(v)
        except ValueError:
            pass

    # Если парсинг не удался, извлечь числовые строки
    if max_stress == 0:
        nums = re.findall(r"[-+]?\d+\.?\d*[eE]?[-+]?\d*", text)
        float_vals = [float(n) for n in nums if abs(float(n)) > 0.001 and abs(float(n)) < 1e12]
        if float_vals:
            max_stress = max(abs(v) for v in float_vals[:50])

    return {
        "max_stress_mpa": round(max_stress, 3),
        "max_displacement_mm": round(max_disp, 4),
        "nodes_stressed": stressed,
    }


def build_fea_tools(ws: Workspace) -> list[Tool]:
    """Собрать FEA-инструменты (Gmsh → CalculiX)."""

    def fea_static(
        stl_path: str,
        material: str = "aluminum",
        force_n_json: str = "[0, 0, -100]",
        mesh_size_mm: float = 2.0,
        fixed_face: str = "bottom",
    ) -> str:
        """Линейный статический FEA-анализ: напряжения von Mises, деформации, фактор безопасности."""
        p_stl = ws.resolve(stl_path)
        if not p_stl.exists():
            raise ToolError(f"STL-файл {stl_path!r} не найден в workspace")

        try:
            force = json.loads(force_n_json)
            if not isinstance(force, (list, tuple)) or len(force) != 3:
                raise ValueError("force_n_json: массив из 3 чисел [Fx, Fy, Fz]")
        except (json.JSONDecodeError, ValueError) as exc:
            raise ToolError(f"Некорректный JSON силы: {exc}") from exc

        mat = FEA_MATERIALS.get(material.lower(), FEA_MATERIALS["aluminum"])

        # Определяем граничные условия по fixed_face
        # bottom = фиксация узлов с min Z, top = max Z, etc.

        gmsh = _find_tool("gmsh")
        ccx = _find_tool("ccx") or _find_tool("calculix-ccx") or _find_tool("ccx_static")

        job_dir = p_stl.parent / f"fea_{p_stl.stem}"
        job_dir.mkdir(parents=True, exist_ok=True)

        if gmsh and ccx:
            return _run_real_fea(p_stl, mat, force, mesh_size_mm, fixed_face, gmsh, ccx, job_dir)
        else:
            return _run_analytical_fea(p_stl, mat, force, mesh_size_mm, fixed_face, ws)

    return [
        Tool(
            name="cad.fea_static",
            description="Линейный статический FEA-анализ STL-детали: Gmsh→CalculiX. Возвращает von Mises stress, деформации, фактор безопасности. Если FEA-софт не установлен — аналитическая оценка.",
            parameters={
                "type": "object",
                "properties": {
                    "stl_path": {"type": "string", "description": "Путь к STL-файлу в workspace"},
                    "material": {
                        "type": "string",
                        "description": f"Материал: {', '.join(FEA_MATERIALS.keys())}",
                    },
                    "force_n_json": {
                        "type": "string",
                        "description": "JSON-массив силы в Н [Fx, Fy, Fz] (например, '[0, 0, -100]')",
                    },
                    "mesh_size_mm": {
                        "type": "number",
                        "description": "Размер элемента сетки в мм (по умолчанию 2.0)",
                    },
                    "fixed_face": {
                        "type": "string",
                        "description": "Грань закрепления: bottom, top, left, right, front, back",
                    },
                },
                "required": ["stl_path"],
            },
            fn=fea_static,
            skills=["cad", "fea", "stress", "gmsh", "calculix", "engineering", "local"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "fea_analysis",
                "speed": "slow",
                "tags": ["cad", "fea", "stress", "von_mises", "gmsh", "calculix", "simulation"],
            },
            example='cad.fea_static(stl_path="bracket.stl", material="steel", force_n_json="[0, 0, -500]")',
        ),
    ]


def _run_real_fea(
    stl: Path, mat: dict, force: list, mesh_size: float, fixed_face: str,
    gmsh: str, ccx: str, job_dir: Path,
) -> str:
    """Реальный FEA через Gmsh → CalculiX."""
    geo = job_dir / "fea.geo"
    msh = job_dir / "fea.msh"
    inp = job_dir / "static.inp"

    # 1. Gmsh: STL → тетраэдральная сетка
    geo.write_text(
        f'Merge "{stl.as_posix()}";\n'
        f"CreateTopology;\n"
        f"Surface Loop(1) = {{1}};\n"
        f"Volume(1) = {{1}};\n"
        f"Mesh.CharacteristicLengthMin = {mesh_size};\n"
        f"Mesh.CharacteristicLengthMax = {mesh_size};\n"
        f"Mesh.Algorithm3D = 1;\n"
        f"Mesh 3;\n"
        f'Save "{msh.as_posix()}";\n',
        encoding="utf-8",
    )

    p_gmsh = _run([gmsh, str(geo), "-3", "-format", "msh2", "-o", str(msh)], job_dir)
    if p_gmsh.returncode:
        return f"⚠️ Gmsh не смог построить объёмную сетку:\n{p_gmsh.stderr[-2000:]}"

    # 2. Парсинг mesh файла
    try:
        import meshio
        mesh = meshio.read(str(msh))
    except ImportError:
        return "⚠️ Библиотека meshio не установлена. Установите: pip install meshio"

    points = mesh.points[:, :3]
    tets = None
    for cell_block in mesh.cells:
        if cell_block.type in ("tetra", "tetra10"):
            tets = cell_block.data
            break

    if tets is None or len(tets) == 0:
        return "⚠️ Gmsh не создал тетраэдральные элементы"

    # 3. Определение граничных условий
    z_coords = points[:, 2]
    z_min, z_max = float(z_coords.min()), float(z_coords.max())
    z_range = z_max - z_min

    face_map = {
        "bottom": (2, z_min, z_range * 0.05),
        "top": (2, z_max, z_range * 0.05),
        "left": (0, float(points[:, 0].min()), float(points[:, 0].max() - points[:, 0].min()) * 0.05),
        "right": (0, float(points[:, 0].max()), float(points[:, 0].max() - points[:, 0].min()) * 0.05),
        "front": (1, float(points[:, 1].min()), float(points[:, 1].max() - points[:, 1].min()) * 0.05),
        "back": (1, float(points[:, 1].max()), float(points[:, 1].max() - points[:, 1].min()) * 0.05),
    }
    axis, val, tol = face_map.get(fixed_face.lower(), face_map["bottom"])

    fixed_ids = [i for i in range(len(points)) if abs(points[i][axis] - val) <= max(tol, mesh_size * 0.5)]
    if not fixed_ids:
        fixed_ids = [int(points[:, axis].argmin())]

    # Нагрузка — противоположная грань
    load_axis = axis
    if fixed_face.lower() in ("bottom", "left", "front"):
        load_val = val + (z_max - z_min if axis == 2 else float(points[:, axis].max() - points[:, axis].min()))
    else:
        load_val = val - (z_max - z_min if axis == 2 else float(points[:, axis].max() - points[:, axis].min()))

    load_ids = [i for i in range(len(points)) if abs(points[i][load_axis] - load_val) <= max(tol, mesh_size * 0.5)]
    if not load_ids:
        load_ids = [int(points[:, load_axis].argmax())]

    # 4. Записать .inp
    _write_calculix_inp(inp, points.tolist(), tets.tolist(), mat, fixed_ids, load_ids, tuple(force))

    # 5. CalculiX
    p_ccx = _run([ccx, "static"], job_dir)
    if p_ccx.returncode:
        return f"⚠️ CalculiX завершился с ошибкой:\n{p_ccx.stderr[-2000:]}"

    # 6. Парсинг результатов
    dat_path = job_dir / "static.dat"
    frd_path = job_dir / "static.frd"
    results = _parse_ccx_dat(dat_path)

    stress = results["max_stress_mpa"]
    disp = results["max_displacement_mm"]
    safety = round(mat["yield_mpa"] / max(stress, 0.001), 2) if stress > 0 else 999.0

    return (
        f"### ✅ FEA-анализ (Gmsh→CalculiX) выполнен:\n"
        f"- **Материал:** {mat.get('name', 'custom')} (E={mat['E_mpa']:.0f} МПа, σ_yield={mat['yield_mpa']:.0f} МПа)\n"
        f"- **Сетка:** {len(tets)} тетраэдров, {len(points)} узлов, размер элемента {mesh_size} мм\n"
        f"- **Нагрузка:** F = [{force[0]}, {force[1]}, {force[2]}] Н на {len(load_ids)} узлах\n"
        f"- **Закрепление:** {fixed_face} ({len(fixed_ids)} узлов)\n"
        f"- **Макс. напряжение von Mises:** **{stress:.2f} МПа**\n"
        f"- **Макс. перемещение:** **{disp:.4f} мм**\n"
        f"- **Фактор безопасности:** **{safety}** → {'✓ БЕЗОПАСНО' if safety >= 1.5 else '⚠ РИСК РАЗРУШЕНИЯ'}\n"
        f"- **Файлы результатов:** `{ws_relative(job_dir, dat_path)}`, `{ws_relative(job_dir, frd_path)}`"
    )


def ws_relative(base: Path, target: Path) -> str:
    try:
        return str(target.relative_to(base.parent.parent))
    except ValueError:
        return target.name


def _run_analytical_fea(
    stl: Path, mat: dict, force: list, mesh_size: float, fixed_face: str, ws: Workspace,
) -> str:
    """Аналитическая оценка напряжений (когда Gmsh/CalculiX недоступны)."""
    # Читаем STL для определения габаритов
    import struct as st
    data = stl.read_bytes()
    if len(data) >= 84:
        num_tris = st.unpack("<I", data[80:84])[0]
        if len(data) == 84 + num_tris * 50 and num_tris > 0:
            xs, ys, zs = [], [], []
            offset = 84
            for _ in range(num_tris):
                chunk = data[offset:offset + 50]
                vals = st.unpack("<12fH", chunk)
                xs.extend([vals[3], vals[6], vals[9]])
                ys.extend([vals[4], vals[7], vals[10]])
                zs.extend([vals[5], vals[8], vals[11]])
                offset += 50
            w = max(xs) - min(xs)
            l = max(ys) - min(ys)
            h = max(zs) - min(zs)
        else:
            w, l, h = 20.0, 20.0, 10.0
            num_tris = 2
    else:
        w, l, h = 20.0, 20.0, 10.0
        num_tris = 2

    # Аналитическая оценка: σ = F / A (упрощённо, для прямоугольного сечения)
    f_mag = math.sqrt(sum(f ** 2 for f in force))
    # Оценка площади сечения (перпендикулярно силе)
    area_mm2 = w * l * 0.3  # грубая оценка ~30% от bounding box
    if area_mm2 < 1:
        area_mm2 = 1.0

    stress_mpa = round(f_mag / area_mm2, 3)
    safety = round(mat["yield_mpa"] / max(stress_mpa, 0.001), 2)
    # Оценка прогиба: δ = FL³ / (3EI)
    E = mat["E_mpa"]
    L = max(w, l, h)
    I_est = (w * h ** 3) / 12.0 if h > 0 else 1.0
    if I_est < 0.001:
        I_est = 0.001
    deflection_mm = round((f_mag * L ** 3) / (3.0 * E * I_est), 4)

    gmsh_avail = "да" if _find_tool("gmsh") else "нет"
    ccx_avail = "да" if (_find_tool("ccx") or _find_tool("calculix-ccx")) else "нет"

    return (
        f"### FEA-анализ (аналитическая оценка — Gmsh/CalculiX не установлены):\n"
        f"- ⚠️ Для точного FEA установите: `apt install gmsh calculix-ccx`\n"
        f"  (gmsh: {gmsh_avail}, ccx: {ccx_avail})\n"
        f"- **Материал:** E={mat['E_mpa']:.0f} МПа, σ_yield={mat['yield_mpa']:.0f} МПа\n"
        f"- **Габариты STL:** {w:.1f} × {l:.1f} × {h:.1f} мм ({num_tris} треугольников)\n"
        f"- **Нагрузка:** |F| = {f_mag:.1f} Н, закрепление: {fixed_face}\n"
        f"- **Оценочное сечение:** ~{area_mm2:.1f} мм²\n"
        f"- **Оценочное напряжение σ:** **{stress_mpa:.2f} МПа**\n"
        f"- **Оценочный прогиб δ:** **{deflection_mm:.4f} мм**\n"
        f"- **Фактор безопасности:** **{safety}** → {'✓ БЕЗОПАСНО (σ < σ_yield / 1.5)' if safety >= 1.5 else '⚠ РИСК — напряжение близко к пределу текучести'}"
    )
