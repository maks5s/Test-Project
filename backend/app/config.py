"""Application settings.

Every knob is an environment variable. Defaults are tuned for the local
``docker compose`` setup, so the stack starts without a ``.env`` file.
"""

from functools import lru_cache
from typing import Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # PostgreSQL
    database_url: str = "postgresql+asyncpg://app:app@db:5432/app"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_connect_timeout_seconds: float = 2.0
    db_pool_timeout_seconds: float = 2.0
    db_echo: bool = False

    # LLM provider (OpenAI-compatible HTTP API)
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 10.0
    llm_max_retries: int = 2
    llm_temperature: float = 0.2
    llm_max_tokens: int = 500
    llm_simulated_delay_min: float = 5.0
    llm_simulated_delay_max: float = 30.0
    llm_rate_limit_per_minute: int = 60

    # Job queue and worker
    worker_concurrency: int = 32
    worker_poll_interval_seconds: float = 1.0
    job_lease_seconds: int = 300
    job_max_attempts: int = 5
    job_reaper_interval_seconds: float = 30.0

    # Reply delivery
    helpdesk_timeout_seconds: float = 10.0
    delivery_max_attempts: int = 5
    delivery_backoff_base_seconds: float = 2.0
    reply_url_allowed_hosts: str = "api,localhost,127.0.0.1"

    # Local integration helpers
    mock_helpdesk_internal_url: str = "http://api:8000"
    enable_dev_endpoints: bool = True

    @model_validator(mode="after")
    def _validate_settings(self) -> Self:
        if self.llm_simulated_delay_min < 0:
            raise ValueError("LLM_SIMULATED_DELAY_MIN must not be negative")
        if self.llm_simulated_delay_max < self.llm_simulated_delay_min:
            raise ValueError("LLM_SIMULATED_DELAY_MAX must be >= LLM_SIMULATED_DELAY_MIN")
        if not 0 <= self.llm_temperature <= 2:
            raise ValueError("LLM_TEMPERATURE must be between 0 and 2")
        return self

    @property
    def allowed_reply_hosts(self) -> frozenset[str]:
        """Hosts allowed as ``reply_url`` targets (empty set means no allowlist)."""
        return frozenset(
            host.strip().lower() for host in self.reply_url_allowed_hosts.split(",") if host.strip()
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
