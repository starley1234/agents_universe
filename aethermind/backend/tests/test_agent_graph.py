from app.agent.graph import AgentGraph
from app.llm.providers import LLMResult


class FakeLLM:
    def complete_sync(self, messages, model=None):
        prompt = messages[-1]["content"]
        if "JSON-массив" in prompt:
            return LLMResult(
                content='[{"id":"s1","title":"Проверить цель","action":"llm_scratchpad"},{"id":"s2","title":"Сделать отчет","action":"llm_final_report"},{"id":"s3","title":"Проверить качество","action":"llm_quality_gate"}]',
                tokens_used=10,
                model="fake",
            )
        if "Верни JSON строго" in prompt:
            return LLMResult(content='{"confidence":0.9,"accepted":true,"reason":"ok"}', tokens_used=5, model="fake")
        return LLMResult(content="# Результат\n\nСодержательный тестовый результат на русском.", tokens_used=7, model="fake")


def test_agent_graph_completes_step(tmp_path):
    state = {"goal": "test goal", "iteration": 0, "events": []}
    next_state = AgentGraph(tmp_path, llm=FakeLLM()).run_one_iteration(state)
    assert next_state["iteration"] == 1
    assert next_state["confidence"] > 0.5
    assert (tmp_path / "artifacts" / "01_goal_and_criteria.md").exists()
    assert next_state["llm_usage"]["calls"] >= 2
