"""Тесты конфигурации S.P.E.C.T.R.U.M."""

import os
import sys
import tempfile
from pathlib import Path

# Добавляем корень проекта в path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_settings_defaults():
    """Настройки по умолчанию."""
    from spectrum.config import Settings

    s = Settings()
    assert s.project_name == "SPECTRUM"
    assert s.app_port > 0
    assert s.chunk_size > 0
    assert s.chunk_overlap >= 0
    assert s.vector_store in ("chroma", "qdrant")


def test_settings_from_env():
    """Настройки из переменных окружения."""
    from spectrum.config import Settings

    os.environ["PROJECT_NAME"] = "TEST_PROJECT"
    os.environ["APP_PORT"] = "9999"
    s = Settings()
    assert s.project_name == "TEST_PROJECT"
    assert s.app_port == 9999
    os.environ.pop("PROJECT_NAME", None)
    os.environ.pop("APP_PORT", None)


def test_settings_llm_profile_fake():
    """Профиль LLM по умолчанию — fake."""
    from spectrum.config import Settings

    os.environ.pop("LLM_ACTIVE_PROVIDER", None)
    s = Settings()
    assert s.llm_profile.model == "fake"
    assert s.llm_profile.api_url == ""


def test_settings_llm_profile_openrouter():
    """Профиль OpenRouter."""
    from spectrum.config import Settings

    os.environ["LLM_ACTIVE_PROVIDER"] = "openrouter"
    os.environ["OPENROUTER_API_KEY"] = "test-key"
    os.environ["OPENROUTER_MODEL"] = "test-model"
    s = Settings()
    assert s.llm_profile.api_key == "test-key"
    assert s.llm_profile.model == "test-model"
    assert "openrouter" in s.llm_profile.api_url
    os.environ.pop("LLM_ACTIVE_PROVIDER", None)
    os.environ.pop("OPENROUTER_API_KEY", None)
    os.environ.pop("OPENROUTER_MODEL", None)


def test_settings_llm_profile_custom_remote():
    """Профиль Custom Remote."""
    from spectrum.config import Settings

    os.environ["LLM_ACTIVE_PROVIDER"] = "custom_remote"
    os.environ["CUSTOM_REMOTE_URL"] = "http://my-server:8080/v1"
    os.environ["CUSTOM_REMOTE_MODEL"] = "my-model"
    s = Settings()
    assert s.llm_profile.api_url == "http://my-server:8080/v1"
    assert s.llm_profile.model == "my-model"
    os.environ.pop("LLM_ACTIVE_PROVIDER", None)
    os.environ.pop("CUSTOM_REMOTE_URL", None)
    os.environ.pop("CUSTOM_REMOTE_MODEL", None)


def test_settings_chunk_size_validation():
    """Проверка валидации chunk_size."""
    from spectrum.processor.chunker import Chunker

    try:
        Chunker(chunk_size=50)
        assert False, "Should raise ValueError"
    except ValueError:
        pass


def test_settings_chunk_overlap_validation():
    """Проверка валидации chunk_overlap."""
    from spectrum.processor.chunker import Chunker

    try:
        Chunker(chunk_size=100, chunk_overlap=100)
        assert False, "Should raise ValueError"
    except ValueError:
        pass


def test_env_loader():
    """Тест загрузки .env файла."""
    from spectrum.env import load_env

    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write("TEST_VAR_1=hello\n")
        f.write("TEST_VAR_2=world\n")
        f.write("# comment\n")
        f.write("TEST_VAR_3='quoted'\n")
        f.flush()
        env_path = Path(f.name)

    # Очищаем
    os.environ.pop("TEST_VAR_1", None)
    os.environ.pop("TEST_VAR_2", None)
    os.environ.pop("TEST_VAR_3", None)

    load_env(env_path)
    assert os.environ.get("TEST_VAR_1") == "hello"
    assert os.environ.get("TEST_VAR_2") == "world"
    assert os.environ.get("TEST_VAR_3") == "quoted"

    env_path.unlink()


def test_env_no_overwrite():
    """Переменные окружения не перезаписываются."""
    from spectrum.env import load_env

    os.environ["EXISTING_VAR"] = "original"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write("EXISTING_VAR=overwritten\n")
        f.flush()
        env_path = Path(f.name)

    load_env(env_path)
    assert os.environ["EXISTING_VAR"] == "original"
    os.environ.pop("EXISTING_VAR", None)
    env_path.unlink()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    failed = 0
    for test_fn in tests:
        try:
            test_fn()
            print(f"  ✅ {test_fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {test_fn.__name__}: {e}")
            failed += 1
    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed:
        sys.exit(1)
