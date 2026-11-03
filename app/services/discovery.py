"""De dónde sale la URL de extract-service en cada pedido (issue #24).

Dos implementaciones del mismo puerto:

* ``StaticEndpoint`` devuelve siempre ``EXTRACT_URL``. Es lo que se usa sin
  Consul, y deja el comportamiento de antes intacto.
* ``ConsulEndpoints`` pregunta a Consul por las instancias ``passing`` de
  ``extract`` y reparte entre ellas con round robin. La lista se cachea unos
  segundos para no consultar a Consul en cada request.

Si Consul no responde, o no hay ninguna instancia passing, se levanta
``ExtractUnavailableError``: el resultado para el cliente es el mismo `503`
que con extract caído, que es lo que decidió la issue.
"""

import asyncio

import httpx

from app.clock import Clock
from app.exceptions import ExtractUnavailableError
from app.logging_config import get_logger
from app.services.consul import ConsulAgent

logger = get_logger(__name__)

#: Nombre con el que extract-service se registra en Consul.
EXTRACT_SERVICE_NAME = "extract"


class StaticEndpoint:
    """``ExtractEndpointPort`` con una única URL fija."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    async def base_url(self) -> str:
        return self._base_url


class ConsulEndpoints:
    """``ExtractEndpointPort`` sobre el catálogo de Consul."""

    def __init__(
        self,
        *,
        agent: ConsulAgent,
        clock: Clock,
        cache_ttl: float = 5.0,
        service_name: str = EXTRACT_SERVICE_NAME,
    ) -> None:
        self._agent = agent
        self._clock = clock
        self._cache_ttl = cache_ttl
        self._service_name = service_name
        self._instances: list[str] = []
        self._expires_at = 0.0
        # Cursor del round robin. No se reinicia cuando se refresca la
        # lista: si se reiniciara, con un pedido cada pocos segundos y un
        # cache corto se repartiria siempre entre las primeras instancias
        # y las ultimas no recibirian nada.
        self._cursor = -1
        # Varios pedidos a la vez no tienen que disparar varias consultas.
        self._lock = asyncio.Lock()

    async def base_url(self) -> str:
        """URL de la próxima instancia, en round robin.

        Raises:
            ExtractUnavailableError: si Consul no responde o no hay
                instancias passing.
        """
        instances = await self._current_instances()
        self._cursor += 1
        return instances[self._cursor % len(instances)]

    async def _current_instances(self) -> list[str]:
        async with self._lock:
            if self._instances and self._clock.monotonic() < self._expires_at:
                return self._instances

            try:
                found = await self._agent.passing_instances(self._service_name)
            except httpx.HTTPError as error:
                # Un Consul caído es, para el cliente, lo mismo que extract
                # caído: 503. No se usa una lista vieja a propósito.
                self._instances = []
                raise ExtractUnavailableError(
                    "No se pudo consultar a Consul para descubrir extract-service"
                ) from error

            if not found:
                # Una lista vacía no se cachea: cuando vuelva una réplica hay
                # que verla en el pedido siguiente.
                self._instances = []
                raise ExtractUnavailableError(
                    "No hay instancias de extract-service en estado passing"
                )

            self._instances = [f"http://{address}:{port}" for address, port in found]
            self._expires_at = self._clock.monotonic() + self._cache_ttl
            logger.info(
                "Instancias de extract descubiertas",
                extra={"instancias": len(self._instances)},
            )
            return self._instances
