"""Application configuration, read once from the environment (ADR-009, ADR-015).

Only what M0's foundations need is declared here. Every later milestone adds its own
fields to this same `Settings` class in the same commit that starts using them — nothing
in `.env.example` should ever be aspirational (a variable nothing reads yet), and nothing
a module reads should be missing from `.env.example`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide configuration. One instance, read at startup, never mutated."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Identity of this deployment -------------------------------------------------
    app_env: Literal["local", "staging", "production"] = Field(default="local", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # --- Database (ADR-001, ADR-003) --------------------------------------------------
    # The API's own connection, as app_user. Never the migrator, never the worker role.
    database_url: str = Field(
        default="postgresql+asyncpg://app_user:app_user_dev_password@localhost:5432/cineatelie",
        alias="DATABASE_URL",
    )
    db_pool_max_size: int = Field(default=10, alias="DB_POOL_MAX_SIZE")
    db_pool_acquire_timeout_s: float = Field(default=2.0, alias="DB_POOL_ACQUIRE_TIMEOUT_S")
    db_statement_timeout_ms: int = Field(default=5000, alias="DB_STATEMENT_TIMEOUT_MS")
    db_lock_timeout_ms: int = Field(default=3000, alias="DB_LOCK_TIMEOUT_MS")

    # --- Worker (ADR-010) --------------------------------------------------------------
    worker_enabled: bool = Field(default=False, alias="WORKER_ENABLED")

    # --- CORS (ADR-015; empty while behind the same-origin proxy, launch dependency A0) -
    cors_allowed_origins: str = Field(default="", alias="CORS_ALLOWED_ORIGINS")

    @property
    def cors_allowed_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached process-wide settings. Tests override via dependency injection, not env
    mutation, so this cache is safe."""
    return Settings()
