"""Тесты инструментов обработки данных, CSV, конвертации и формул Excel (data.*)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.core import Workspace
from agent_toolkit.local.data_tools import build_data_tools
from tests.harness import TempWorkspace, check, section, summary


def run_tests() -> int:
    with TempWorkspace() as tmp:
        ws = Workspace(tmp.path("ws"))
        section("1. Инструменты работы с данными, таблицами и формулами Excel (data.*)")
        tools = {t.name: t for t in build_data_tools(ws)}
        check("зарегистрировано 5 инструментов data", len(tools) == 5)

        res_write = tools["data.write_csv"].execute(
            path="items.csv",
            headers_json='["Brand", "Price"]',
            rows_json='[["Acme", "100"], ["Other", "200"]]',
        )
        check("write_csv записывает файл", "items.csv" in res_write)

        res_read = tools["data.read_csv"].execute(path="items.csv")
        check("read_csv читает строки", "Acme" in res_read and "Other" in res_read)

        res_conv = tools["data.convert_format"].execute(
            data_str='[{"a": 10, "b": 20}]', from_fmt="json", to_fmt="yaml"
        )
        check("convert_format преобразует JSON в YAML", "a: 10" in res_conv and "b: 20" in res_conv)

        res_agg = tools["data.aggregate_table"].execute(
            rows_json='[{"brand": "Acme", "val": 15}, {"brand": "Acme", "val": 25}]',
            group_by="brand",
            agg_col="val",
            agg_func="SUM",
        )
        check("aggregate_table считает SUM с группировкой", "40.0" in res_agg and "Acme" in res_agg)

        res_sum = tools["data.excel_formula_eval"].execute(
            formula="=SUM(A1:A3)", cells_json='{"A1": 10, "A2": 20, "A3": 30}'
        )
        check("excel_formula_eval считает функцию SUM", "60.0" in res_sum)

        res_arith = tools["data.excel_formula_eval"].execute(
            formula="=A1*B1+C1", cells_json='{"A1": 5, "B1": 10, "C1": 3}'
        )
        check("excel_formula_eval считает арифметическую формулу", "53.0" in res_arith)

    return summary("Тесты инструментов данных")


def test_data_tools_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
