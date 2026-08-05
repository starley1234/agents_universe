import json
from pathlib import Path

from app.agent.graph import AgentGraph
from app.llm.providers import LLMResult


class FunctionalLLM:
    """Deterministic LLM for an end-to-end product task.

    The task simulates a useful autonomous run: create structured data, write a
    markdown report with a table, run a sandbox check, produce a final report,
    and pass a quality gate without human intervention.
    """

    def complete_sync(self, messages, model=None):
        prompt = messages[-1]["content"]
        if "JSON-массив" in prompt:
            return LLMResult(
                content=json.dumps(
                    [
                        {"id": "scope", "title": "Определить критерии анализа", "action": "llm_scratchpad"},
                        {"id": "data", "title": "Собрать сравнительную таблицу", "action": "llm_research"},
                        {"id": "calc", "title": "Проверить данные скриптом", "action": "run_python"},
                        {"id": "report", "title": "Сформировать итоговый отчет", "action": "llm_final_report"},
                        {"id": "qa", "title": "Проверить полноту результата", "action": "llm_quality_gate"},
                    ],
                    ensure_ascii=False,
                ),
                tokens_used=25,
                model="functional-fake",
            )
        if "Верни JSON строго" in prompt:
            return LLMResult(
                content=json.dumps(
                    {"confidence": 0.88, "accepted": True, "reason": "Есть проверяемые файлы, таблица и отчет."},
                    ensure_ascii=False,
                ),
                tokens_used=9,
                model="functional-fake",
            )
        if "Сожми историю" in prompt:
            return LLMResult("# Executive summary\n\nСозданы данные, отчет и проверочные артефакты.", tokens_used=8, model="functional-fake")

        if "Шаг: Определить критерии" in prompt:
            return LLMResult(
                "# Критерии анализа\n\n"
                "- автономность\n- наличие артефактов\n- проверяемость\n"
                'MCP_CALL_JSON: {"server_name":"__internal__","tool_name":"write_file","arguments":{"path":"artifacts/criteria.md","content":"# Критерии приемки\\n\\n| Критерий | Ожидание |\\n|---|---|\\n| Артефакты | отчет + CSV + проверка |\\n| Автономность | без AWAITING_USER |"}}',
                tokens_used=20,
                model="functional-fake",
            )
        if "Шаг: Собрать сравнительную" in prompt:
            return LLMResult(
                "# Сравнительная таблица\n\n"
                'MCP_CALL_JSON: {"server_name":"__internal__","tool_name":"write_file","arguments":{"path":"data/competitors.csv","content":"name,score,price\\nAlpha,82,19\\nBeta,74,15\\nGamma,91,29\\n"}}\n'
                'MCP_CALL_JSON: {"server_name":"__internal__","tool_name":"write_file","arguments":{"path":"artifacts/comparison.md","content":"# Сравнение\\n\\n| Продукт | Score | Цена |\\n|---|---:|---:|\\n| Alpha | 82 | 19 |\\n| Beta | 74 | 15 |\\n| Gamma | 91 | 29 |"}}',
                tokens_used=35,
                model="functional-fake",
            )
        if "Шаг: Сформировать итоговый" in prompt:
            return LLMResult(
                "# Итоговый отчет\n\nGamma лидирует по score, Beta дешевле.\n"
                'MCP_CALL_JSON: {"server_name":"__internal__","tool_name":"write_file","arguments":{"path":"artifacts/final_market_report.md","content":"# Итоговый market report\\n\\n| Вывод | Значение |\\n|---|---|\\n| Лидер по качеству | Gamma |\\n| Самый дешевый | Beta |\\n\\n## Рекомендация\\nВыбрать Gamma для premium-сегмента и Beta для price-sensitive сегмента."}}',
                tokens_used=35,
                model="functional-fake",
            )
        if "Шаг: Проверить полноту" in prompt:
            return LLMResult(
                "# QA gate\n\nВсе ключевые артефакты созданы.\n"
                'MCP_CALL_JSON: {"server_name":"__internal__","tool_name":"list_dir","arguments":{"path":"artifacts"}}',
                tokens_used=15,
                model="functional-fake",
            )
        return LLMResult("# Результат\n\nШаг выполнен.", tokens_used=5, model="functional-fake")


def test_autonomous_market_report_functional_run(tmp_path: Path):
    state = {
        "goal": "Подготовь мини market intelligence отчет по 3 конкурентам с CSV, markdown-таблицей и финальной рекомендацией.",
        "iteration": 0,
        "events": [],
        "artifacts": [],
        "tool_config": {"llm": True, "filesystem": True, "code_interpreter": True, "mcp": True, "mcp_servers": []},
    }
    graph = AgentGraph(tmp_path, llm=FunctionalLLM())

    for _ in range(8):
        state = graph.run_one_iteration(state)
        if state.get("goal_completed") or state.get("awaiting_user"):
            break

    assert state.get("goal_completed") is True
    assert not state.get("awaiting_user")
    assert state["iteration"] >= 5
    assert (tmp_path / "data" / "competitors.csv").exists()
    assert (tmp_path / "artifacts" / "comparison.md").exists()
    assert (tmp_path / "artifacts" / "final_market_report.md").exists()
    assert "Gamma" in (tmp_path / "artifacts" / "final_market_report.md").read_text(encoding="utf-8")
    assert any(event.get("type") == "mcp_call" for event in state.get("events", []))
