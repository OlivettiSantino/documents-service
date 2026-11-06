"""Health Check API.

``/health`` is liveness: it touches nothing and is what the Docker
``HEALTHCHECK`` and compose's ``depends_on`` watch. Pointing a container
healthcheck at readiness is a classic self-inflicted outage — a brief database
blip would restart a process that was perfectly fine.

``/health/ready`` is readiness: it depends only on **this service's own**
database. extract-service is not checked: with it down we still serve reads and
answer writes with ``503``, so we are not unready. Its circuit breaker lives in
Traefik, not here (issue #28).
"""

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import get_repository
from app.api.schemas import HealthResponse, ReadinessResponse
from app.ports import DocumentRepositoryPort

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", response_model=HealthResponse, summary="Liveness")
async def liveness() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/ready", response_model=ReadinessResponse, summary="Readiness")
async def readiness(
    response: Response,
    repository: DocumentRepositoryPort = Depends(get_repository),
) -> ReadinessResponse:
    database_up = await repository.ping()
    if not database_up:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        status="ready" if database_up else "not_ready",
        database="up" if database_up else "down",
    )
