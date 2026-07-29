"""Набор навыков: OpenSCAD + численная проверка геометрии.

Здесь собраны методы, которые доказали свою полезность на реальном
проекте планетарного редуктора. Главный принцип: НЕ ОЦЕНИВАТЬ ГЕОМЕТРИЮ
НА ГЛАЗ. Рендер показывает картинку, но не показывает, что деталь
не собирается, зубья сталкиваются, а сетка дырявая.

Проверки:
  scad_render       — собрать модель, вернуть ошибки/предупреждения
  scad_export_stl   — экспорт STL
  stl_check         — водонепроницаемость (каждое ребро ровно в 2 гранях)
                      + число связных компонент (не отвалились ли части)
  scad_collision    — булево пересечение двух тел с ОЦЕНКОЙ ОБЪЁМА:
                      отличает реальное внедрение от касания по плоскости
  stl_bbox          — габариты и объём детали

Ловушка, на которой легко обмануться (проверено): OpenSCAD возвращает
код 1 и на пустой результат, и на настоящую ошибку. Поэтому
scad_collision различает случаи по тексту лога, а не по коду возврата.
"""
from __future__ import annotations

import math
import subprocess
from collections import defaultdict
from pathlib import Path

from ..tools.base import Tool, ToolError, Workspace

RENDER_FLAGS = ["-D", "$fa=7", "-D", "$fs=0.9"]


# ---------------------------------------------------------------- утилиты
def _openscad(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["openscad", *args], capture_output=True,
                              text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise ToolError(
            "openscad не найден. Установите: apt-get install -y openscad"
        ) from exc
    except subprocess.TimeoutExpired:
        raise ToolError(f"OpenSCAD не уложился в {timeout} с") from None


def _load_tris(path: Path) -> list[tuple[tuple[float, float, float], ...]]:
    """Читает ASCII-STL. Координаты округляем — иначе одна и та же вершина
    из-за плавающей точки станет двумя, и проверка соврёт про дыры."""
    tris = []
    cur: list[tuple[float, float, float]] = []
    with path.open("r", errors="replace") as fh:
        for line in fh:
            s = line.strip()
            if s.startswith("vertex"):
                parts = s.split()
                cur.append(tuple(round(float(x), 4) for x in parts[1:4]))  # type: ignore
                if len(cur) == 3:
                    tris.append(tuple(cur))
                    cur = []
    return tris


def _mesh_stats(tris: list) -> dict[str, float | int]:
    vid: dict[tuple, int] = {}
    edges: dict[tuple[int, int], int] = defaultdict(int)
    parent: list[int] = []

    def gid(v: tuple) -> int:
        if v not in vid:
            vid[v] = len(parent)
            parent.append(vid[v])
        return vid[v]

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    volume = 0.0
    for tri in tris:
        ids = [gid(v) for v in tri]
        for i in range(3):
            a, b = ids[i], ids[(i + 1) % 3]
            edges[(min(a, b), max(a, b))] += 1
            union(a, b)
        (x1, y1, z1), (x2, y2, z2), (x3, y3, z3) = tri
        volume += (x1 * (y2 * z3 - y3 * z2)
                   - y1 * (x2 * z3 - x3 * z2)
                   + z1 * (x2 * y3 - x3 * y2)) / 6.0

    bad = sum(1 for c in edges.values() if c != 2)
    comps = len({find(i) for i in vid.values()}) if vid else 0
    return {"triangles": len(tris), "bad_edges": bad,
            "components": comps, "volume": abs(volume)}


# ------------------------------------------------------------------ build
def build(ws: Workspace, timeout: int = 600) -> list[Tool]:

    def scad_render(path: str, extra: str = "") -> str:
        p = ws.resolve(path)
        if not p.exists():
            raise ToolError(f"Файл {path!r} не найден")
        out = ws.resolve(".agent_render.png")
        args = ["-o", str(out), "--imgsize=64,64"]
        if extra.strip():
            args += extra.split()
        args.append(str(p))
        r = _openscad(args, timeout)
        log = (r.stdout or "") + (r.stderr or "")
        problems = [ln for ln in log.splitlines()
                    if ln.startswith("ERROR") or "WARNING" in ln
                    or "Assertion" in ln]
        echoes = [ln[6:] for ln in log.splitlines() if ln.startswith("ECHO:")]
        parts = []
        if problems:
            parts.append("ПРОБЛЕМЫ:\n" + "\n".join(problems[:40]))
        else:
            parts.append("Сборка без ошибок и предупреждений.")
        if echoes:
            parts.append("ECHO:\n" + "\n".join(echoes[:60]))
        out.unlink(missing_ok=True)
        return "\n\n".join(parts)

    def scad_export_stl(path: str, out_path: str, extra: str = "") -> str:
        p = ws.resolve(path)
        o = ws.resolve(out_path)
        o.parent.mkdir(parents=True, exist_ok=True)
        args = ["-o", str(o), *RENDER_FLAGS]
        if extra.strip():
            args += extra.split()
        args.append(str(p))
        r = _openscad(args, timeout)
        if not o.exists() or o.stat().st_size == 0:
            log = ((r.stdout or "") + (r.stderr or ""))[-800:]
            raise ToolError(f"STL не создан. Лог:\n{log}")
        return f"Экспортирован {ws.relative(o)} ({o.stat().st_size} Б)"

    def stl_check(path: str) -> str:
        p = ws.resolve(path)
        if not p.exists():
            raise ToolError(f"Файл {path!r} не найден")
        tris = _load_tris(p)
        if not tris:
            raise ToolError(f"{path!r} пуст или это двоичный STL "
                            "(экспортируйте ASCII через openscad)")
        st = _mesh_stats(tris)
        ok = st["bad_edges"] == 0 and st["components"] == 1
        verdict = ("ГОДНАЯ: водонепроницаема и односвязна" if ok else
                   "ДЕФЕКТ: " + ", ".join(filter(None, [
                       f"{st['bad_edges']} рёбер не в 2 гранях (дыры)"
                       if st["bad_edges"] else "",
                       f"{st['components']} компонент — деталь распалась"
                       if st["components"] != 1 else ""])))
        return (f"{ws.relative(p)}\n"
                f"  треугольников: {st['triangles']}\n"
                f"  рёбер != 2 граней: {st['bad_edges']}\n"
                f"  компонент связности: {st['components']}\n"
                f"  объём: {st['volume']:.3f} мм3\n"
                f"  {verdict}")

    def stl_bbox(path: str) -> str:
        p = ws.resolve(path)
        tris = _load_tris(p)
        if not tris:
            raise ToolError(f"{path!r} пуст или двоичный")
        xs = [v[0] for t in tris for v in t]
        ys = [v[1] for t in tris for v in t]
        zs = [v[2] for t in tris for v in t]
        rs = [math.hypot(v[0], v[1]) for t in tris for v in t]
        st = _mesh_stats(tris)
        return (f"{ws.relative(p)}\n"
                f"  X {min(xs):.3f} .. {max(xs):.3f}  ({max(xs)-min(xs):.3f})\n"
                f"  Y {min(ys):.3f} .. {max(ys):.3f}  ({max(ys)-min(ys):.3f})\n"
                f"  Z {min(zs):.3f} .. {max(zs):.3f}  ({max(zs)-min(zs):.3f})\n"
                f"  радиус до {max(rs):.3f}\n"
                f"  объём {st['volume']:.3f} мм3")

    def scad_collision(path: str, body_a: str, body_b: str,
                       params: str = "") -> str:
        """Булево пересечение двух тел из модели.

        Оценивает ОБЪЁМ: соприкосновение по плоскости даёт лист нулевого
        объёма — это норма, а не коллизия. Различать обязательно, иначе
        любая пара сопрягаемых деталей выглядит как ошибка.
        """
        p = ws.resolve(path)
        if not p.exists():
            raise ToolError(f"Файл {path!r} не найден")
        # временный файл кладём РЯДОМ с моделью: include ищется относительно
        # включающего файла, из другой папки он молча не разрешится
        tmp = p.parent / f".collide_{abs(hash(body_a + body_b)) % 10**8}.scad"
        stl = tmp.with_suffix(".stl")
        tmp.write_text(
            f'include <{p.name}>\n'
            f'DEV = "none";\npart = "none";\n'
            f"intersection() {{ {body_a} {body_b} }}\n",
            encoding="utf-8")
        try:
            args = ["-o", str(stl), *RENDER_FLAGS]
            if params.strip():
                args += params.split()
            args.append(str(tmp))
            r = _openscad(args, timeout)
            log = (r.stdout or "") + (r.stderr or "")

            if "Can't open include" in log or "unknown module" in log:
                raise ToolError(
                    "Тест не собрался: include не разрешился или модуль "
                    f"неизвестен. Лог:\n{log[-500:]}")
            if any(ln.startswith("ERROR") or "Assertion" in ln
                   for ln in log.splitlines()):
                bad = [ln for ln in log.splitlines()
                       if ln.startswith("ERROR") or "Assertion" in ln]
                return "ОШИБКА МОДЕЛИ:\n" + "\n".join(bad[:10])
            if not stl.exists() or stl.stat().st_size == 0:
                return ("ЧИСТО: тела не пересекаются "
                        "(результат пересечения пуст)")
            tris = _load_tris(stl)
            vol = _mesh_stats(tris)["volume"]
            if vol < 1e-3:
                return (f"КАСАНИЕ ПО ПЛОСКОСТИ: объём {vol:.6f} мм3 — "
                        "это норма для сопрягаемых деталей, не коллизия")
            return (f"КОЛЛИЗИЯ: объём внедрения {vol:.4f} мм3, "
                    f"{len(tris)} граней — тела реально пересекаются")
        finally:
            tmp.unlink(missing_ok=True)
            stl.unlink(missing_ok=True)

    return [
        Tool("scad_render",
             "Собрать модель OpenSCAD и получить ошибки, предупреждения и "
             "вывод echo. Не создаёт файлов. Первое, что делать после правки.",
             {"type": "object",
              "properties": {
                  "path": {"type": "string", "description": "Файл .scad"},
                  "extra": {"type": "string",
                            "description": "Доп. флаги, напр. -D 'DEV=\"ring\"'"}},
              "required": ["path"]},
             scad_render),
        Tool("scad_export_stl",
             "Экспортировать модель в STL.",
             {"type": "object",
              "properties": {
                  "path": {"type": "string"},
                  "out_path": {"type": "string"},
                  "extra": {"type": "string"}},
              "required": ["path", "out_path"]},
             scad_export_stl),
        Tool("stl_check",
             "Проверить STL численно: водонепроницаемость (каждое ребро ровно "
             "в двух треугольниках) и число связных компонент. Так ловятся "
             "дыры в сетке и отвалившиеся части, невидимые на рендере.",
             {"type": "object",
              "properties": {"path": {"type": "string"}},
              "required": ["path"]},
             stl_check),
        Tool("stl_bbox",
             "Габариты, максимальный радиус и объём детали по STL. "
             "Нужно, чтобы сверять реальные размеры с расчётными.",
             {"type": "object",
              "properties": {"path": {"type": "string"}},
              "required": ["path"]},
             stl_bbox),
        Tool("scad_collision",
             "Проверить, пересекаются ли два тела: строит intersection() и "
             "меряет ОБЪЁМ. Отличает реальное внедрение от касания по "
             "плоскости. Так проверяют собираемость узла, а не на глаз.",
             {"type": "object",
              "properties": {
                  "path": {"type": "string", "description": "Файл модели"},
                  "body_a": {"type": "string",
                             "description": "Первое тело, напр. 'ring();'"},
                  "body_b": {"type": "string",
                             "description": "Второе тело со сдвигом, "
                                            "напр. 'translate([0,0,-3]) carrier();'"},
                  "params": {"type": "string",
                             "description": "Доп. -D параметры"}},
              "required": ["path", "body_a", "body_b"]},
             scad_collision),
    ]
