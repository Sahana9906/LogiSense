from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Phase 1 runtime configuration. Deliberately has no reference to any
    particular source dataset -- ingestion/normalization is a separate
    concern that maps a dataset into the canonical schema before Phase 1
    ever runs."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "local"
    log_level: str = "INFO"
    database_url: str = Field(
        "postgresql+psycopg://logisense:logisense@localhost:5432/logisense",
        validation_alias="DATABASE_URL",
    )

    gemini_api_key: str | None = Field(default=None, validation_alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.5-flash", validation_alias="GEMINI_MODEL")
    gemini_timeout_seconds: int = 45


@lru_cache
def get_settings() -> Settings:
    return Settings()
