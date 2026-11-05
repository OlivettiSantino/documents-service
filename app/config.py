"""Externalized configuration (12-Factor III).

Every knob is an environment variable. There is no ``env_file``: in
development run the service as ``uv run --env-file .env python -m app`` so the
process never depends on a file being present on disk.
"""

from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Hard limit of a BSON document in MongoDB (16 MiB). ``MAX_CONTENT_BYTES``
#: must stay strictly below it to leave room for the other fields.
BSON_MAX_DOCUMENT_BYTES = 16 * 1024 * 1024

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

    # Base de extract, sin "/extract" (el cliente lo agrega). En el compose es
    # el gateway, http://traefik; este default es para correr fuera de Docker
    # contra un extract local.
    EXTRACT_URL: str = "http://localhost:8001"
    # Presupuesto de timeouts (README de pdf_extractext-infra): cliente 30 s >
    # documents 27 s > Traefik 25 s >= extract 23 s. documents corta despues
    # que Traefik, asi el 504 lo da el gateway y no un corte nuestro a ciegas.
    EXTRACT_TIMEOUT: float = Field(default=27.0, gt=0)
    EXTRACT_CONNECT_TIMEOUT: float = Field(default=2.0, gt=0)

    MAX_UPLOAD_SIZE: int = Field(default=50 * 1024 * 1024, gt=0)

    # Tope del texto extraido, en bytes UTF-8. El content se guarda inline y un
    # documento BSON no puede pasar de 16 MiB: 15 MiB deja 1 MiB de margen
    # para el resto de los campos y el overhead de BSON.
    MAX_CONTENT_BYTES: int = Field(default=15 * 1024 * 1024, gt=0, lt=BSON_MAX_DOCUMENT_BYTES)

    # Control de admision: cuantos uploads pueden estar en memoria a la vez.
    # Cada pedido retiene hasta dos copias del PDF (el body y el archivo
    # extraido), asi que con 512 MB y 50 MB por archivo, 4 deja margen.
    MAX_CONCURRENT_UPLOADS: int = Field(default=4, gt=0)
    # Cuanto espera un upload por un cupo antes de responder 503.
    UPLOAD_SLOT_TIMEOUT: float = Field(default=2.0, gt=0)

    # Service discovery. Estos tres nombres son comunes a los tres repos del
    # proyecto (infra, extract y documents): no renombrarlos.
    # Solo controla el registro de documents (y con el, su publicacion en
    # Traefik). No cambia a donde se llama a extract: eso es EXTRACT_URL.
    # Apagado por defecto: sin Consul el servicio funciona igual.
    CONSUL_ENABLED: bool = False
    CONSUL_HTTP_ADDR: str = "http://consul:8500"
    # Token de ACL: solo por entorno, nunca en el repo.
    CONSUL_HTTP_TOKEN: str = ""
    CONSUL_TIMEOUT: float = Field(default=2.0, gt=0)
    # Cuanto vale la lista de instancias de extract antes de volver a
    # preguntarle a Consul. Sin uso desde la issue #27 (ahora descubre
    # Traefik): se elimina junto con ConsulEndpoints en la #28.
    CONSUL_CACHE_TTL: float = Field(default=5.0, gt=0)

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
