"""Wire DTOs.

Kept separate from the internal models so the public contract and the stored
shape can evolve independently.
"""

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Liveness."""

    status: str = "ok"
