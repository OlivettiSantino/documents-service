"""Health Check API.

``/health`` is liveness: it touches nothing. That is what the Docker
``HEALTHCHECK`` and compose's ``depends_on`` will watch. Pointing a container
healthcheck at something that depends on the database is a classic
self-inflicted outage -- a brief blip would restart a process that was fine.
"""

from fastapi import APIRouter

from app.api.schemas import HealthResponse

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", response_model=HealthResponse, summary="Liveness")
async def liveness() -> HealthResponse:
    return HealthResponse(status="ok")
