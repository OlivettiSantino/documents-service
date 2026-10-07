"""Externalized configuration (12-Factor III).

Every knob is an environment variable. There is no ``env_file``: in
development run the service as ``uv run --env-file .env python -m app`` so the
process never depends on a file being present on disk.
"""

from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Database owned by the sibling monolith (``pdf_extractext``). It auto-runs its
#: own migrations on every boot and owns a unique index there, so touching it
#: would break "database per service" in both directions.
FORBIDDEN_DB_NAMES = frozenset({"pdf_extract_db"})


class Settings(BaseSettings):
    """Runtime configuration read from the environment."""

    model_config = SettingsConfigDict(case_sensitive=True, extra="ignore")

    APP_NAME: str = "documents-service"
    APP_VERSION: str = "1.0.0"

    HOST: str = "0.0.0.0"
    PORT: int = Field(default=8002, ge=1, le=65535)

    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    LOG_FORMAT: Literal["text", "json"] = "text"

    MONGO_URI: str = "mongodb://localhost:27017"
    MONGO_DB_NAME: str = "documents_db"
    MONGO_TIMEOUT_MS: int = Field(default=3000, gt=0)

    EXTRACT_URL: str = "http://localhost:8001"
    EXTRACT_TIMEOUT: float = Field(default=25.0, gt=0)
    EXTRACT_CONNECT_TIMEOUT: float = Field(default=2.0, gt=0)

    MAX_UPLOAD_SIZE: int = Field(default=50 * 1024 * 1024, gt=0)

    MAX_CONCURRENT_EXTRACTIONS: int = Field(default=4, gt=0)
    BULKHEAD_ACQUIRE_TIMEOUT: float = Field(default=2.0, gt=0)

    BREAKER_FAILURE_THRESHOLD: int = Field(default=5, gt=0)
    BREAKER_OPEN_SECONDS: float = Field(default=15.0, gt=0)
    BREAKER_WINDOW_SECONDS: float = Field(default=30.0, gt=0)

    @field_validator("MONGO_DB_NAME")
    @classmethod
    def _reject_foreign_database(cls, value: str) -> str:
        """Refuse to boot against another service's database."""
        if value in FORBIDDEN_DB_NAMES:
            raise ValueError(
                f"MONGO_DB_NAME={value!r} pertenece a otro servicio. "
                "Este servicio debe usar su propia base (database per service)."
            )
        return value

    @field_validator("EXTRACT_URL")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        """Normalize the base URL so path joining stays predictable."""
        return value.rstrip("/")
