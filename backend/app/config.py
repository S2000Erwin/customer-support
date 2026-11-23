from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=Path(__file__).resolve().parents[1] / ".env", extra="ignore")

    company_name: str = "ErwinCo"
    gemini_api_key: str = ""
    admin_api_key: str = ""
    session_secret: str = ""
    gemini_agent_model: str = "gemini-3.5-flash-lite" # less busy
    gemini_rag_model: str = "gemini-3.1-flash-lite"
    gemini_live_model: str = "gemini-3.8-live"
    gemini_agent_input_usd_per_million: float = 0.30
    gemini_agent_output_usd_per_million: float = 2.50
    database_url: str = "postgresql+asyncpg://support:support@localhost:5433/support"
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""  # required for Qdrant Cloud; empty for local Docker
    qdrant_collection: str = "support_manual"
    rag_min_similarity: float = 0.5
    rag_min_keyword_overlap: float = 0.25
    redis_url: str = "redis://localhost:6379/0"
    frontend_origins: str = "http://localhost:5173"
    max_agent_steps: int = 6
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
