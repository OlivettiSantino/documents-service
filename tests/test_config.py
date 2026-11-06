"""Externalized configuration (12-Factor III)."""

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_defaults_are_usable_without_any_env_var():
    settings = Settings()
    assert settings.PORT == 8002
    assert settings.MONGO_DB_NAME == "documents_db"
    assert settings.EXTRACT_TIMEOUT == 27.0
    assert settings.LOG_FORMAT == "text"
    assert settings.MONGO_RETRY_AFTER == 5


def test_every_value_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("PORT", "9000")
    monkeypatch.setenv("EXTRACT_URL", "http://extract:8001")
    monkeypatch.setenv("MAX_CONCURRENT_UPLOADS", "8")
    settings = Settings()
    assert (settings.PORT, settings.EXTRACT_URL, settings.MAX_CONCURRENT_UPLOADS) == (
        9000,
        "http://extract:8001",
        8,
    )


def test_extract_timeout_fits_the_timeout_budget():
    """cliente 30 s > documents 27 s > Traefik 25 s (README de infra)."""
    traefik, client = 25.0, 30.0
    timeout = Settings().EXTRACT_TIMEOUT
    assert timeout == 27.0
    assert traefik < timeout < client


#: Variables que existieron hasta la issue #28 y pueden seguir en un .env viejo.
REMOVED_VARIABLES = {
    "BREAKER_FAILURE_THRESHOLD": "5",
    "BREAKER_OPEN_SECONDS": "15.0",
    "BREAKER_WINDOW_SECONDS": "30.0",
    "MAX_CONCURRENT_EXTRACTIONS": "4",
    "BULKHEAD_ACQUIRE_TIMEOUT": "2.0",
    "CONSUL_CACHE_TTL": "5.0",
}


def test_an_old_env_with_removed_variables_still_boots(monkeypatch, tmp_path):
    for name, value in REMOVED_VARIABLES.items():
        monkeypatch.setenv(name, value)
    env_file = tmp_path / ".env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in REMOVED_VARIABLES.items()))

    from_env = Settings()
    from_file = Settings(_env_file=env_file)
    from_kwargs = Settings(**REMOVED_VARIABLES)

    for settings in (from_env, from_file, from_kwargs):
        assert not any(hasattr(settings, name) for name in REMOVED_VARIABLES)


def test_extract_url_loses_its_trailing_slash():
    assert Settings(EXTRACT_URL="http://extract:8001/").EXTRACT_URL == "http://extract:8001"


@pytest.mark.parametrize(
    "field,value",
    [
        ("PORT", 0),
        ("PORT", 70000),
        ("EXTRACT_TIMEOUT", 0),
        ("MAX_UPLOAD_SIZE", 0),
        ("MAX_CONCURRENT_UPLOADS", 0),
        ("UPLOAD_SLOT_TIMEOUT", 0),
        ("MONGO_RETRY_AFTER", 0),
        ("MAX_CONTENT_BYTES", 0),
        ("MAX_CONTENT_BYTES", -1),
        ("MAX_CONTENT_BYTES", 16 * 1024 * 1024),
        ("MAX_CONTENT_BYTES", 16 * 1024 * 1024 + 1),
    ],
)
def test_invalid_values_refuse_to_boot(field, value):
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_max_content_bytes_leaves_room_below_the_bson_limit():
    """Default 15 MiB: 1 MiB of headroom for the other fields and BSON overhead."""
    assert Settings().MAX_CONTENT_BYTES == 15 * 1024 * 1024
    assert Settings(MAX_CONTENT_BYTES=16 * 1024 * 1024 - 1).MAX_CONTENT_BYTES == 16 * 1024 * 1024 - 1


def test_refuses_to_boot_against_the_monolith_database():
    """Database per service, enforced at startup rather than by convention."""
    with pytest.raises(ValidationError, match="database per service"):
        Settings(MONGO_DB_NAME="pdf_extract_db")
