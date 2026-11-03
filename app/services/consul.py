"""Registro de esta instancia en Consul (service discovery, issue #23).

No conoce FastAPI ni los endpoints: arma el registro, le habla a la API del
agente con el mismo ``httpx`` que ya usa el servicio y **nunca** propaga
errores. Un Consul caído no puede impedir que el servicio arranque ni que
siga atendiendo.

El check es de **liveness** (``/health``) y no de readiness: ``/health/ready``
depende de MongoDB, y un blip de la base nos sacaría del catálogo cuando en
realidad seguimos sirviendo lecturas y rechazando escrituras correctamente.
"""

import socket
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from app.logging_config import get_logger

logger = get_logger(__name__)

#: Nombre con el que este servicio aparece en el catálogo.
SERVICE_NAME = "documents"
CHECK_INTERVAL = "10s"
CHECK_TIMEOUT = "2s"
#: Un `docker kill` no da tiempo a desregistrarse: pasado este tiempo en
#: critical, Consul saca la instancia del catálogo por su cuenta.
DEREGISTER_CRITICAL_AFTER = "1m"


@dataclass(frozen=True)
class Registration:
    """Lo que esta instancia le dice a Consul sobre sí misma."""

    service_id: str
    address: str
    port: int
    name: str = SERVICE_NAME

    def payload(self) -> dict:
        return {
            "ID": self.service_id,
            "Name": self.name,
            "Address": self.address,
            "Port": self.port,
            "Check": {
                # A la IP de la instancia: "localhost", visto desde Consul, es
                # el propio contenedor de Consul. Y a /health, no a
                # /health/ready, que depende de Mongo.
                "HTTP": f"http://{self.address}:{self.port}/health",
                "Interval": CHECK_INTERVAL,
                "Timeout": CHECK_TIMEOUT,
                "DeregisterCriticalServiceAfter": DEREGISTER_CRITICAL_AFTER,
            },
        }


def shared_network_ip(consul_url: str) -> str:
    """IP propia en la red por la que se alcanza a Consul.

    El contenedor está en dos redes (la privada con Mongo y la compartida
    ``mired``) y el orden que devuelve ``gethostbyname`` no está garantizado,
    así que en vez de elegir a ciegas se le pregunta al sistema por qué
    interfaz saldría un paquete hacia Consul. Con UDP no se manda nada:
    ``connect`` solo resuelve la ruta.
    """
    parts = urlsplit(consul_url)
    host = parts.hostname or "consul"
    port = parts.port or 8500
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((host, port))
        return probe.getsockname()[0]


class ConsulAgent:
    """Cliente mínimo de la API HTTP del agente de Consul."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        consul_url: str,
        token: str = "",
    ) -> None:
        self._http = http_client
        self._url = consul_url.rstrip("/")
        self._token = token

    @property
    def headers(self) -> dict[str, str]:
        # El token sale del entorno y nunca se loguea ni se versiona.
        return {"X-Consul-Token": self._token} if self._token else {}

    async def register(self, registration: Registration) -> None:
        response = await self._http.put(
            f"{self._url}/v1/agent/service/register",
            json=registration.payload(),
            headers=self.headers,
        )
        response.raise_for_status()

    async def deregister(self, service_id: str) -> None:
        response = await self._http.put(
            f"{self._url}/v1/agent/service/deregister/{service_id}",
            headers=self.headers,
        )
        response.raise_for_status()

    async def passing_instances(self, service_name: str) -> list[tuple[str, int]]:
        """Instancias ``passing`` de ``service_name``, como (address, port).

        Propaga los errores de red a propósito: quien descubre decide qué
        hacer con un Consul que no responde (ver ``discovery``).
        """
        response = await self._http.get(
            f"{self._url}/v1/health/service/{service_name}",
            params={"passing": "true"},
            headers=self.headers,
        )
        response.raise_for_status()

        instances = []
        for entry in response.json():
            service = entry.get("Service") or {}
            # Address vacío significa "la del nodo", según la API de Consul.
            address = service.get("Address") or (entry.get("Node") or {}).get("Address")
            port = service.get("Port")
            if address and isinstance(port, int):
                instances.append((address, port))
        return instances


class ServiceRegistrarPort(Protocol):
    """Lo que el ciclo de vida necesita saber del registro."""

    async def register(self) -> bool: ...

    async def deregister(self) -> bool: ...


class ServiceRegistrar:
    """Registra esta instancia al arrancar y la desregistra al apagarse.

    Ningún fallo se propaga: devuelve si la operación salió bien y deja el
    rastro en el log.
    """

    def __init__(self, *, agent: ConsulAgent, registration: Registration) -> None:
        self._agent = agent
        self.registration = registration

    async def register(self) -> bool:
        try:
            await self._agent.register(self.registration)
        except (httpx.HTTPError, httpx.HTTPStatusError) as error:
            logger.warning(
                "No se pudo registrar en Consul",
                extra={"service_id": self.registration.service_id, "error": str(error)},
            )
            return False
        logger.info(
            "Instancia registrada en Consul",
            extra={
                "service_id": self.registration.service_id,
                "name": self.registration.name,
                "address": f"{self.registration.address}:{self.registration.port}",
            },
        )
        return True

    async def deregister(self) -> bool:
        try:
            await self._agent.deregister(self.registration.service_id)
        except (httpx.HTTPError, httpx.HTTPStatusError) as error:
            logger.warning(
                "No se pudo desregistrar de Consul",
                extra={"service_id": self.registration.service_id, "error": str(error)},
            )
            return False
        logger.info(
            "Instancia desregistrada de Consul",
            extra={"service_id": self.registration.service_id},
        )
        return True


def build_registrar(
    *,
    http_client: httpx.AsyncClient,
    consul_url: str,
    token: str,
    port: int,
    service_id: str | None = None,
) -> ServiceRegistrar | None:
    """Arma el registrador de esta instancia, o ``None`` si no se pudo.

    El ID es el hostname del contenedor (único por instancia) y la dirección,
    la IP de la red por la que se llega a Consul. Si esa IP no se puede
    resolver, se loguea y se devuelve ``None``: el arranque sigue igual.
    """
    service_id = service_id or socket.gethostname()
    try:
        address = shared_network_ip(consul_url)
    except OSError as error:
        logger.warning(
            "No se pudo resolver la IP propia hacia Consul",
            extra={"consul_url": consul_url, "error": str(error)},
        )
        return None
    return ServiceRegistrar(
        agent=ConsulAgent(http_client=http_client, consul_url=consul_url, token=token),
        registration=Registration(service_id=service_id, address=address, port=port),
    )
