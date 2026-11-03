"""Registro del servicio en Consul (issue #23).

Ninguno de estos tests toca la red: las llamadas al agente de Consul se
interceptan con ``respx`` y la app recibe un registrador falso. Lo único que
usa sockets de verdad es la resolución de la IP propia, y contra loopback.
"""

import json

import httpx
import pytest
import respx

from app.config import Settings
from app.main import create_app
from tests.conftest import running_app
from app.services.consul import (
    CHECK_INTERVAL,
    CHECK_TIMEOUT,
    DEREGISTER_CRITICAL_AFTER,
    SERVICE_NAME,
    ConsulAgent,
    Registration,
    ServiceRegistrar,
    build_registrar,
    shared_network_ip,
)

CONSUL_URL = "http://consul:8500"


def make_agent(token: str = "") -> ConsulAgent:
    return ConsulAgent(
        http_client=httpx.AsyncClient(timeout=httpx.Timeout(2.0)),
        consul_url=CONSUL_URL,
        token=token,
    )


def make_registrar(token: str = "") -> ServiceRegistrar:
    return ServiceRegistrar(
        agent=make_agent(token),
        registration=Registration(service_id="doc-1", address="172.20.0.9", port=8002),
    )


# --- Lo que se le manda a Consul -----------------------------------------


def test_el_registro_tiene_lo_que_pide_la_issue():
    payload = Registration(service_id="doc-1", address="172.20.0.9", port=8002).payload()

    assert payload["ID"] == "doc-1"  # un ID por instancia
    assert payload["Name"] == "documents"
    assert payload["Address"] == "172.20.0.9"  # la IP en la red compartida
    assert payload["Port"] == 8002
    assert payload["Check"] == {
        "HTTP": "http://172.20.0.9:8002/health",
        "Interval": CHECK_INTERVAL,
        "Timeout": CHECK_TIMEOUT,
        "DeregisterCriticalServiceAfter": DEREGISTER_CRITICAL_AFTER,
    }


def test_el_check_es_de_liveness_y_no_de_readiness():
    """/health/ready depende de Mongo: un blip de la base no debe sacarnos
    del catálogo, que es justo lo que pide la issue."""
    check = Registration(service_id="doc-1", address="172.20.0.9", port=8002).payload()["Check"]

    assert check["HTTP"].endswith("/health")
    assert "/health/ready" not in check["HTTP"]


@pytest.mark.parametrize("prohibido", ["localhost", "127.0.0.1"])
def test_el_check_no_apunta_a_localhost(prohibido):
    check = Registration(service_id="doc-1", address="172.20.0.9", port=8002).payload()["Check"]

    assert prohibido not in check["HTTP"]


def test_hay_deregister_critical_after():
    assert DEREGISTER_CRITICAL_AFTER == "1m"
    assert SERVICE_NAME == "documents"


# --- La IP que se publica -------------------------------------------------


def test_la_ip_publicada_es_la_de_la_interfaz_que_llega_a_consul():
    assert shared_network_ip("http://127.0.0.1:8500") == "127.0.0.1"


def test_la_ip_se_resuelve_aunque_la_url_no_traiga_puerto():
    assert shared_network_ip("http://127.0.0.1") == "127.0.0.1"


# --- El cliente del agente -------------------------------------------------


@respx.mock
async def test_registrar_hace_put_al_endpoint_del_agente():
    route = respx.put(f"{CONSUL_URL}/v1/agent/service/register").mock(
        return_value=httpx.Response(200)
    )

    await make_agent().register(
        Registration(service_id="doc-1", address="172.20.0.9", port=8002)
    )

    assert route.called
    request = route.calls[0].request
    assert json.loads(request.content)["Name"] == "documents"
    assert "X-Consul-Token" not in request.headers


@respx.mock
async def test_desregistrar_pega_al_endpoint_con_el_id():
    route = respx.put(f"{CONSUL_URL}/v1/agent/service/deregister/doc-1").mock(
        return_value=httpx.Response(200)
    )

    await make_agent().deregister("doc-1")

    assert route.called


@respx.mock
async def test_el_token_de_acl_viaja_en_el_header():
    route = respx.put(f"{CONSUL_URL}/v1/agent/service/register").mock(
        return_value=httpx.Response(200)
    )

    await make_agent(token="un-uuid").register(
        Registration(service_id="doc-1", address="172.20.0.9", port=8002)
    )

    assert route.calls[0].request.headers["X-Consul-Token"] == "un-uuid"


# --- Un Consul caído no puede impedir arrancar ---------------------------


@respx.mock
async def test_un_fallo_de_registro_no_se_propaga(caplog):
    respx.put(f"{CONSUL_URL}/v1/agent/service/register").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    assert await make_registrar(token="un-uuid").register() is False
    assert "un-uuid" not in caplog.text  # el token nunca se loguea


@respx.mock
async def test_un_registro_rechazado_tampoco_se_propaga():
    respx.put(f"{CONSUL_URL}/v1/agent/service/register").mock(
        return_value=httpx.Response(403, text="ACL not found")
    )

    assert await make_registrar().register() is False


@respx.mock
async def test_un_fallo_al_desregistrar_tampoco_se_propaga():
    respx.put(f"{CONSUL_URL}/v1/agent/service/deregister/doc-1").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    assert await make_registrar().deregister() is False


def test_build_registrar_devuelve_none_si_no_puede_resolver_la_ip():
    registrar = build_registrar(
        http_client=httpx.AsyncClient(),
        consul_url="http://no-existe.invalid:8500",
        token="",
        port=8002,
    )

    assert registrar is None


def test_build_registrar_usa_el_id_que_se_le_pasa():
    registrar = build_registrar(
        http_client=httpx.AsyncClient(),
        consul_url="http://127.0.0.1:8500",
        token="",
        port=8002,
        service_id="doc-7",
    )

    assert registrar is not None
    assert registrar.registration.service_id == "doc-7"
    assert registrar.registration.address == "127.0.0.1"


# --- Integración con el ciclo de vida ------------------------------------


class FakeRegistrar:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.registered = 0
        self.deregistered = 0

    async def register(self) -> bool:
        self.registered += 1
        return self.ok

    async def deregister(self) -> bool:
        self.deregistered += 1
        return True


def _app(registrar, *, enabled, repository, extractor):
    return create_app(
        Settings(CONSUL_ENABLED=enabled, LOG_LEVEL="WARNING"),
        repository=repository,
        extractor=extractor,
        consul_registrar=registrar,
    )


async def test_la_app_no_toca_consul_si_esta_deshabilitado(repository, extractor):
    fake = FakeRegistrar()

    async with running_app(_app(fake, enabled=False, repository=repository, extractor=extractor)) as client:
        assert (await client.get("/health")).status_code == 200

    assert (fake.registered, fake.deregistered) == (0, 0)


async def test_la_app_registra_al_arrancar_y_desregistra_al_apagar(repository, extractor):
    fake = FakeRegistrar()

    async with running_app(_app(fake, enabled=True, repository=repository, extractor=extractor)) as client:
        assert (await client.get("/health")).status_code == 200
        assert fake.registered == 1
        assert fake.deregistered == 0

    assert fake.deregistered == 1


async def test_si_el_registro_falla_la_app_igual_atiende(repository, extractor):
    fake = FakeRegistrar(ok=False)

    async with running_app(_app(fake, enabled=True, repository=repository, extractor=extractor)) as client:
        assert (await client.get("/health")).status_code == 200

    assert fake.registered == 1
    assert fake.deregistered == 0  # no se desregistra lo que nunca se registro
