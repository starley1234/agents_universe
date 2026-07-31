"""Тесты бенчмарка релевантности и скорости умного поиска (benchmark.py)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.benchmark import run_benchmark
from tests.harness import check, section, summary


def run_tests() -> int:
    section("1. Бенчмарк релевантности умного поиска (100 запросов)")
    report = run_benchmark()
    check("все 100+ запросов бенчмарка выполнены", report.total_queries >= 100)
    check("Top-1 Precision@1 >= 60%", report.precision_at_1 >= 60.0, f"получили {report.precision_at_1}%")
    check("Top-3 Precision@3 >= 85%", report.precision_at_3 >= 85.0, f"получили {report.precision_at_3}%")
    check("Mean Reciprocal Rank (MRR) >= 0.70", report.mrr >= 0.70, f"получили {report.mrr}")
    check("Средняя задержка поиска <= 10.0 мс", report.avg_latency_ms <= 10.0, f"получили {report.avg_latency_ms} мс")

    if report.failed_queries:
        print("  ! Несовпавшие запросы:")
        for q, expected, found in report.failed_queries[:3]:
            print(f"    '{q}' -> ожидали {expected}, получили {found}")

    return summary("Тесты бенчмарка умного поиска")


def test_benchmark_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
