"""Externalized configuration (12-Factor III)."""

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_defaults_are_usable_without_any_env_var():
    settings = Settings()
    assert settings.PORT == 8002
    assert settings.MONGO_DB_NAME == "documents_db"
    assert settings.EXTRACT_TIMEOUT == 25.0
    assert settings.LOG_FORMAT == "text"


def test_every_value_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("PORT", "9000")
    monkeypatch.setenv("EXTRACT_URL", "http://extract:8001")
    monkeypatch.setenv("MAX_CONCURRENT_EXTRACTIONS", "8")
    settings = Settings()
    assert (settings.PORT, settings.EXTRACT_URL, settings.MAX_CONCURRENT_EXTRACTIONS) == (
        9000,
        "http://extract:8001",
        8,
    )


def test_extract_url_loses_its_trailing_slash():
    assert Settings(EXTRACT_URL="http://extract:8001/").EXTRACT_URL == "http://extract:8001"


@pytest.mark.parametrize(
    "field,value",
    [
        ("PORT", 0),
        ("PORT", 70000),
        ("EXTRACT_TIMEOUT", 0),
        ("MAX_CONCURRENT_EXTRACTIONS", 0),
        ("BREAKER_FAILURE_THRESHOLD", -1),
        ("MAX_UPLOAD_SIZE", 0),
    ],
)
def test_invalid_values_refuse_to_boot(field, value):
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_refuses_to_boot_against_the_monolith_database():
    """Database per service, enforced at startup rather than by convention."""
    with pytest.raises(ValidationError, match="database per service"):
        Settings(MONGO_DB_NAME="pdf_extract_db")
