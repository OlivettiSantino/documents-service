"""Process entry point: ``python -m app``.

Reads configuration from the environment, sends logs to stdout and binds the
port given by ``PORT`` (12-Factor VII).

One process per container, deliberately with no uvicorn ``workers``: the
service scales out with container replicas (``DOCUMENTS_REPLICAS``,
12-Factor VIII), not with workers inside one container. Each replica is a
process the platform can see: it registers in Consul under its own hostname,
gets its own health check and its own memory limit, and Traefik balances
between them. Workers would hide N processes behind one registration and one
check, and multiply the upload admission slots (``MAX_CONCURRENT_UPLOADS``,
held in process memory) inside a memory limit sized for one. See "Escalado
horizontal" in the README.
"""

import uvicorn

from app.config import Settings
from app.logging_config import configure_logging
from app.main import create_app


def main() -> None:
    settings = Settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)
    uvicorn.run(
        create_app(settings),
        host=settings.HOST,
        port=settings.PORT,
        log_config=None,  # keep uvicorn's own logs on our stdout handler
        log_level=settings.LOG_LEVEL.lower(),
    )


if __name__ == "__main__":
    main()
