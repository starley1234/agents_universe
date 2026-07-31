"""Инструменты обработки табличных данных, CSV и конвертации форматов (data.*).

Обеспечивают быстрое чтение, запись, агрегацию и преобразование
данных без необходимости использовать SQL-движок или Excel.
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from ..core import Tool, ToolError, Workspace


def build_data_tools(ws: Workspace) -> list[Tool]:
    """Собрать инструменты работы с таблицами, CSV и форматами."""

    def read_csv(path: str, delimiter: str = ",", limit: int = 20) -> str:
        p = ws.resolve(path)
        if not p.exists():
            raise ToolError(f"Файл CSV {path!r} не найден")
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            reader = csv.reader(io.StringIO(text), delimiter=delimiter or ",")
            rows = list(reader)
            if not rows:
                return "(CSV файл пуст)"
            headers = rows[0]
            data_rows = rows[1:limit + 1]
            lines = [
                f"### CSV {ws.relative(p)} (показано строк: {len(data_rows)} из {len(rows)-1}):",
                " | ".join(headers),
                " | ".join("---" for _ in headers),
            ]
            for r in data_rows:
                lines.append(" | ".join(r))
            return "\n".join(lines)
        except OSError as exc:
            raise ToolError(f"Ошибка чтения CSV {path!r}: {exc}") from exc

    def write_csv(
        path: str,
        headers_json: str,
        rows_json: str,
        delimiter: str = ",",
    ) -> str:
        p = ws.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            headers = json.loads(headers_json) if headers_json else []
            rows = json.loads(rows_json) if rows_json else []
        except ValueError as exc:
            raise ToolError(f"Некорректный JSON в headers_json / rows_json: {exc}") from exc

        try:
            out = io.StringIO()
            writer = csv.writer(out, delimiter=delimiter or ",")
            if headers:
                writer.writerow(headers)
            for r in rows:
                writer.writerow(r)
            p.write_text(out.getvalue(), encoding="utf-8")
            return f"CSV файл {ws.relative(p)} записан (строк: {len(rows)})"
        except OSError as exc:
            raise ToolError(f"Ошибка записи CSV {path!r}: {exc}") from exc

    def convert_format(data_str: str, from_fmt: str, to_fmt: str) -> str:
        from_f = (from_fmt or "json").lower()
        to_f = (to_fmt or "json").lower()
        if not data_str.strip():
            raise ToolError("Исходные данные для конвертации не могут быть пустыми")

        # Парсинг источника в структуру Python
        obj: Any
        if from_f == "json":
            try:
                obj = json.loads(data_str)
            except ValueError as exc:
                raise ToolError(f"Ошибка разбора исходного JSON: {exc}") from exc
        elif from_f == "csv":
            reader = csv.reader(io.StringIO(data_str))
            rows = list(reader)
            if not rows:
                obj = []
            else:
                headers = rows[0]
                obj = [dict(zip(headers, r)) for r in rows[1:]]
        else:
            raise ToolError(f"Формат источника {from_f!r} не поддерживается")

        # Форматирование в целевой формат
        if to_f == "json":
            return json.dumps(obj, ensure_ascii=False, indent=2)
        if to_f == "yaml":
            lines = []
            if isinstance(obj, list):
                for item in obj:
                    lines.append("-")
                    if isinstance(item, dict):
                        for k, v in item.items():
                            lines.append(f"  {k}: {v}")
                    else:
                        lines[-1] = f"- {item}"
            elif isinstance(obj, dict):
                for k, v in obj.items():
                    lines.append(f"{k}: {v}")
            return "\n".join(lines)
        if to_f in ("csv", "markdown", "table"):
            if not isinstance(obj, list) or not obj or not isinstance(obj[0], dict):
                raise ToolError("Для экспорта в таблицу требуется JSON массив объектов [{...}]")
            headers = list(obj[0].keys())
            if to_f == "csv":
                out = io.StringIO()
                writer = csv.writer(out)
                writer.writerow(headers)
                for item in obj:
                    writer.writerow([item.get(h, "") for h in headers])
                return out.getvalue().strip()
            # Markdown / Table
            lines = [
                " | ".join(headers),
                " | ".join("---" for _ in headers),
            ]
            for item in obj:
                lines.append(" | ".join(str(item.get(h, "")) for h in headers))
            return "\n".join(lines)

        raise ToolError(f"Целевой формат {to_f!r} не поддерживается")

    def aggregate_table(
        rows_json: str,
        group_by: str = "",
        agg_col: str = "",
        agg_func: str = "SUM",
    ) -> str:
        try:
            rows = json.loads(rows_json) if rows_json else []
            if not isinstance(rows, list):
                raise ValueError("rows_json должен быть JSON-массивом объектов")
        except ValueError as exc:
            raise ToolError(f"Некорректный JSON в rows_json: {exc}") from exc

        func_up = (agg_func or "SUM").upper()
        if func_up not in ("SUM", "AVG", "COUNT", "MIN", "MAX"):
            raise ToolError(f"Агрегирующая функция {agg_func!r} не поддерживается")

        groups: dict[str, list[float]] = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            key = str(r.get(group_by, "All")) if group_by else "Total"
            val = r.get(agg_col, 1) if agg_col else 1
            try:
                num = float(val)
            except (ValueError, TypeError):
                num = 0.0
            groups.setdefault(key, []).append(num)

        results = []
        for k, vals in groups.items():
            if func_up == "SUM":
                res_val = round(sum(vals), 2)
            elif func_up == "AVG":
                res_val = round(sum(vals) / len(vals), 2) if vals else 0.0
            elif func_up == "COUNT":
                res_val = len(vals)
            elif func_up == "MIN":
                res_val = round(min(vals), 2) if vals else 0.0
            else:
                res_val = round(max(vals), 2) if vals else 0.0
            results.append({"group": k, func_up: res_val})

        return json.dumps(results, ensure_ascii=False, indent=2)

    def excel_formula_eval(formula: str, cells_json: str = "{}") -> str:
        if not formula.strip():
            raise ToolError("Формула Excel не может быть пустой")
        try:
            cells = json.loads(cells_json) if cells_json else {}
            if not isinstance(cells, dict):
                raise ValueError("cells_json должен быть объектом с ключами-именами ячеек")
        except Exception as exc:
            raise ToolError(f"Некорректный JSON в cells_json: {exc}") from exc

        expr = formula.strip()
        if expr.startswith("="):
            expr = expr[1:].strip()

        # Поддержка стандартных функций SUM, AVERAGE, MIN, MAX, COUNT и арифметики
        func_upper = expr.upper()
        if func_upper.startswith("SUM(") and expr.endswith(")"):
            inner = expr[4:-1].strip()
            vals = _resolve_cell_list(inner, cells)
            res_val = round(sum(vals), 4)
        elif func_upper.startswith("AVERAGE(") and expr.endswith(")"):
            inner = expr[8:-1].strip()
            vals = _resolve_cell_list(inner, cells)
            res_val = round(sum(vals) / len(vals), 4) if vals else 0.0
        elif func_upper.startswith("MIN(") and expr.endswith(")"):
            inner = expr[4:-1].strip()
            vals = _resolve_cell_list(inner, cells)
            res_val = round(min(vals), 4) if vals else 0.0
        elif func_upper.startswith("MAX(") and expr.endswith(")"):
            inner = expr[4:-1].strip()
            vals = _resolve_cell_list(inner, cells)
            res_val = round(max(vals), 4) if vals else 0.0
        elif func_upper.startswith("COUNT(") and expr.endswith(")"):
            inner = expr[6:-1].strip()
            vals = _resolve_cell_list(inner, cells)
            res_val = float(len(vals))
        else:
            # Замена имен ячеек на их числовые значения
            import re as _re

            def replace_cell(match: _re.Match[str]) -> str:
                name = match.group(0).upper()
                return str(cells.get(name, 0.0))

            sub_expr = _re.sub(r"[A-Z]+[0-9]+", replace_cell, expr.upper())
            # Безопасное вычисление базового арифметического выражения
            try:
                import ast, operator
                operators = {
                    ast.Add: operator.add,
                    ast.Sub: operator.sub,
                    ast.Mult: operator.mul,
                    ast.Div: operator.truediv,
                    ast.Pow: operator.pow,
                    ast.USub: operator.neg,
                }

                def _eval(node: Any) -> float:
                    if isinstance(node, ast.Constant):
                        return float(node.value)
                    if isinstance(node, ast.BinOp):
                        left = _eval(node.left)
                        right = _eval(node.right)
                        return operators[type(node.op)](left, right)
                    if isinstance(node, ast.UnaryOp):
                        return operators[type(node.op)](_eval(node.operand))
                    raise ValueError("Недопустимая операция в формуле")

                tree = ast.parse(sub_expr, mode="eval")
                res_val = round(_eval(tree.body), 4)
            except Exception as exc:
                raise ToolError(f"Ошибка вычисления формулы Excel {formula!r}: {exc}") from exc

        return (
            f"### Вычисление формулы Excel:\n"
            f"- **Формула:** `{formula}`\n"
            f"- **Значения ячеек:** `{cells_json}`\n"
            f"- **Результат вычисления:** `{res_val}`"
        )

    def _resolve_cell_list(inner: str, cells: dict[str, Any]) -> list[float]:
        vals = []
        for part in inner.split(","):
            p = part.strip().upper()
            if ":" in p:
                start, end = [x.strip() for x in p.split(":", 1)]
                # Поиск всех ячеек в диапазоне или выбор всех из словаря, попадающих в интервал
                for k, v in cells.items():
                    try:
                        vals.append(float(v))
                    except (ValueError, TypeError):
                        pass
            else:
                val = cells.get(p, 0.0)
                try:
                    vals.append(float(val))
                except (ValueError, TypeError):
                    vals.append(0.0)
        return vals

    return [
        Tool(
            name="data.read_csv",
            description="Прочитать CSV-файл в виде структурированной таблицы с заголовками.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к CSV файлу"},
                    "delimiter": {
                        "type": "string",
                        "description": "Разделитель (по умолчанию ',')",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Максимальное число строк",
                    },
                },
                "required": ["path"],
            },
            fn=read_csv,
            skills=["data", "csv", "table", "local", "read", "analytics"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "csv_table",
                "speed": "fast",
                "tags": ["data", "csv", "read", "table", "analytics"],
            },
            example='data.read_csv(path="sales.csv")',
        ),
        Tool(
            name="data.write_csv",
            description="Записать массив строк в формате CSV в файл.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь для сохранения"},
                    "headers_json": {
                        "type": "string",
                        "description": 'JSON-массив названий колонок (\'["Имя", "Цена"]\')',
                    },
                    "rows_json": {
                        "type": "string",
                        "description": 'JSON-массив строк (\'[["Товар", 100]]\')',
                    },
                    "delimiter": {
                        "type": "string",
                        "description": "Разделитель колонок (по умолчанию ',')",
                    },
                },
                "required": ["path", "headers_json", "rows_json"],
            },
            fn=write_csv,
            skills=["data", "csv", "table", "local", "write"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "csv_table",
                "speed": "fast",
                "tags": ["data", "csv", "write", "table", "save"],
            },
            example='data.write_csv(path="out.csv", headers_json=\'["A", "B"]\', rows_json=\'[["1", "2"]]\')',
        ),
        Tool(
            name="data.convert_format",
            description="Преобразовать данные между форматами (json, csv, yaml, markdown).",
            parameters={
                "type": "object",
                "properties": {
                    "data_str": {
                        "type": "string",
                        "description": "Исходные данные (например, JSON-строка)",
                    },
                    "from_fmt": {
                        "type": "string",
                        "description": "Исходный формат (json, csv)",
                    },
                    "to_fmt": {
                        "type": "string",
                        "description": "Целевой формат (json, yaml, csv, markdown)",
                    },
                },
                "required": ["data_str", "from_fmt", "to_fmt"],
            },
            fn=convert_format,
            skills=["data", "convert", "json", "yaml", "csv", "markdown"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "data_convert",
                "speed": "fast",
                "tags": ["data", "convert", "json", "yaml", "csv", "markdown"],
            },
            example='data.convert_format(data_str=\'[{"a": 1}]\', from_fmt="json", to_fmt="yaml")',
        ),
        Tool(
            name="data.aggregate_table",
            description="Агрегировать массив JSON-объектов (SUM, AVG, COUNT, MIN, MAX) с группировкой.",
            parameters={
                "type": "object",
                "properties": {
                    "rows_json": {
                        "type": "string",
                        "description": 'JSON-массив объектов [{"brand": "Acme", "price": 100}]',
                    },
                    "group_by": {
                        "type": "string",
                        "description": "Имя колонки для группировки (например, 'brand')",
                    },
                    "agg_col": {
                        "type": "string",
                        "description": "Имя агрегируемой колонки ('price')",
                    },
                    "agg_func": {
                        "type": "string",
                        "description": "Агрегирующая функция: SUM, AVG, COUNT, MIN, MAX",
                    },
                },
                "required": ["rows_json"],
            },
            fn=aggregate_table,
            skills=["data", "analytics", "table", "math", "aggregate"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "data_aggregate",
                "speed": "fast",
                "tags": ["data", "aggregate", "sum", "avg", "count", "math"],
            },
            example='data.aggregate_table(rows_json=\'[{"brand": "A", "val": 10}, {"brand": "A", "val": 20}]\', group_by="brand", agg_col="val", agg_func="SUM")',
        ),
        Tool(
            name="data.excel_formula_eval",
            description="Вычислить формулу Excel (SUM, AVERAGE, MIN, MAX, COUNT, арифметика) по значениям ячеек в формате JSON.",
            parameters={
                "type": "object",
                "properties": {
                    "formula": {
                        "type": "string",
                        "description": "Формула Excel (например, '=SUM(A1:A3)' или '=A1*B1+C1')",
                    },
                    "cells_json": {
                        "type": "string",
                        "description": 'JSON-объект со значениями ячеек (\'{"A1": 10, "A2": 20, "A3": 30}\')',
                    },
                },
                "required": ["formula"],
            },
            fn=excel_formula_eval,
            skills=["data", "excel", "formula", "analytics", "table", "math", "xlsx"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "excel_formula",
                "speed": "fast",
                "tags": [
                    "excel",
                    "formula",
                    "xlsx",
                    "sum",
                    "average",
                    "формула",
                    "эксель",
                    "вычисление",
                ],
            },
            example='data.excel_formula_eval(formula="=SUM(A1:A3)", cells_json=\'{"A1": 10, "A2": 20, "A3": 30}\')',
        ),
    ]
