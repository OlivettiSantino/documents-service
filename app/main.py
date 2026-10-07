"""Application factory.

Wiring lives in exactly one place so that the rest of the code never has to
know how its collaborators are built.
"""

from fastapi import FastAPI

from app.api import health
from app.config import Settings
from app.logging_config import get_logger
from app.middleware import RequestIdMiddleware

logger = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application.

    Args:
        settings: configuration; read from the environment when omitted.
    """
    settings = settings or Settings()

    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
    )
    app.state.settings = settings
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health.router)
    return app
