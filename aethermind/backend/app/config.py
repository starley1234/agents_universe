from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    project_name: str = "AetherMind"
    environment: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8128
    api_dev_token: str = "dev-token"
    database_url: str = "postgresql+psycopg://aethermind:aethermind@localhost:5433/aethermind"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"
    redis_url: str = "redis://localhost:6379/2"
    workspace_path: str = "./workspace"
    llm_active_provider: str = "custom_remote"
    custom_remote_url: str = "https://my_lm_studio.ai/api/v1"
    custom_remote_key: str = "sk-local"
    custom_remote_default_model: str = "unsloth/gemma-4-12b-it"
    openrouter_api_url: str = "https://openrouter.ai/api/v1"
    openrouter_api_key: str = "sk-or-placeholder"
    openrouter_default_model: str = "google/gemini-2.0-flash-lite:preview"
    embedding_dimensions: int = 1024
    summary_every_iterations: int = 5
    low_confidence_threshold: float = 0.5
    low_confidence_streak_limit: int = 3
    default_max_iterations: int = 25
    default_token_budget: int = 100000
    default_cost_budget_usd: float = 25
    default_time_budget_seconds: int = 14400
    llm_required: bool = True
    planner_min_steps: int = 8
    mcp_search_url: str = ""
    mcp_agent_toolkit: str = ""

@lru_cache
def get_settings() -> Settings:
    return Settings()

settings = get_settings()
