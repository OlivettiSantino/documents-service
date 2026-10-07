"""Process entry point: ``python -m app``.

Reads configuration from the environment, sends logs to stdout and binds the
port given by ``PORT`` (12-Factor VII).

Deliberately a single process with no ``workers``: the circuit breaker and the
bulkhead keep their state in process memory, so N workers would mean N
independent circuits and an effective concurrency of N x
``MAX_CONCURRENT_EXTRACTIONS``. See the "known limits" section of the README.
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
