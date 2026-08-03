"""Config smoke test — verifies Settings loads without crashing."""
import os
import sys

# Ensure src is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_settings_defaults():
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("POSTGRES_PASSWORD", None)
    from src.config import Settings
    s = Settings()
    assert s.APP_PORT == 8112
    assert s.LLM_PROVIDER.value == "local"
    assert s.EMBEDDING_DIMENSIONS == 1024
    assert s.AGENT_MAX_ITERATIONS == 20
    assert s.AGENT_QUALITY_THRESHOLD == 0.8


def test_llm_properties():
    from src.config import Settings, Provider
    s = Settings(LLM_PROVIDER=Provider.LOCAL, LOCAL_LLM_URL="http://test:11434/v1",
                 LOCAL_LLM_MODEL="test-model", LOCAL_LLM_API_KEY="key123")
    assert s.llm_base_url == "http://test:11434/v1"
    assert s.llm_model == "test-model"
    assert s.llm_api_key == "key123"


def test_openrouter_properties():
    from src.config import Settings, Provider
    s = Settings(LLM_PROVIDER=Provider.OPENROUTER, OPENROUTER_URL="https://or.ai/v1",
                 OPENROUTER_MODEL="google/test", OPENROUTER_API_KEY="sk-or-abc")
    assert s.llm_base_url == "https://or.ai/v1"
    assert s.llm_model == "google/test"
    assert s.llm_api_key == "sk-or-abc"


if __name__ == "__main__":
    test_settings_defaults()
    test_llm_properties()
    test_openrouter_properties()
    print("All config tests passed ✓")
