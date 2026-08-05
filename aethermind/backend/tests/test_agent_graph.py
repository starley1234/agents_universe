from app.agent.graph import AgentGraph
from app.llm.providers import LLMResult


class FakeLLM:
    def complete_sync(self, messages, model=None):
        content = messages[-1]["content"]
        prompt = "\n".join(part.get("text", "") for part in content if part.get("type") == "text") if isinstance(content, list) else content
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


def test_agent_sends_images_to_multimodal_llm(tmp_path):
    from app.agent.graph import AgentGraph
    from app.llm.providers import LLMResult

    image = tmp_path / "attachments" / "part.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"fake-png")

    class VisionLLM:
        def __init__(self):
            self.saw_image = False
        def complete_sync(self, messages, model=None):
            content = messages[-1]["content"]
            if isinstance(content, list) and any(part.get("type") == "image_url" for part in content):
                self.saw_image = True
            text = "\n".join(part.get("text", "") for part in content if part.get("type") == "text") if isinstance(content, list) else content
            if "JSON-массив" in text:
                return LLMResult('[{"id":"v","title":"Проанализировать изображение","action":"llm_research"},{"id":"f","title":"Финал","action":"llm_final_report"},{"id":"q","title":"QA","action":"llm_quality_gate"}]', model="vision-fake")
            if "Верни JSON строго" in text:
                return LLMResult('{"confidence":0.9,"accepted":true,"reason":"ok"}', model="vision-fake")
            return LLMResult('Изображение проанализировано.', model="vision-fake")

    llm = VisionLLM()
    state = {"goal": "проанализируй изображение", "iteration": 0, "events": [], "attachments": [{"path": "attachments/part.png", "content_type": "image/png"}], "tool_config": {"llm": True, "filesystem": True, "mcp": True, "mcp_servers": []}}
    state = AgentGraph(tmp_path, llm=llm).run_one_iteration(state)
    assert llm.saw_image is True
    assert any(event.get("type") == "vision" for event in state.get("events", []))
