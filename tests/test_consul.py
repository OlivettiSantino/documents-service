"""Registro del servicio en Consul (issue #23).

Ninguno de estos tests toca la red: las llamadas al agente de Consul se
interceptan con ``respx`` y la app recibe un registrador falso. Lo único que
usa sockets de verdad es la resolución de la IP propia, y contra loopback.
"""

import json
import logging

import httpx
import pytest
import respx

from app.config import Settings
from app.main import create_app
from app.services.consul import (
    CHECK_INTERVAL,
    CHECK_TIMEOUT,
    DEREGISTER_CRITICAL_AFTER,
    SERVICE_NAME,
    ConsulAgent,
    Registration,
    ServiceRegistrar,
    build_registrar,
    register_with_retry,
    shared_network_ip,
    traefik_tags,
)
from tests.conftest import running_app

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


@respx.mock
async def test_el_registro_exitoso_devuelve_true_y_loguea(caplog):
    """Cubre el camino feliz del registrador, el log incluido.

    El nivel tiene que estar en INFO: si no, logging no llega a crear el
    LogRecord y una clave reservada en `extra` pasaria desapercibida acá y
    rompería el arranque en el contenedor, que si corre en INFO.
    """
    respx.put(f"{CONSUL_URL}/v1/agent/service/register").mock(
        return_value=httpx.Response(200)
    )

    with caplog.at_level(logging.INFO):
        assert await make_registrar().register() is True

    assert "registrada en Consul" in caplog.text


@respx.mock
async def test_el_desregistro_exitoso_devuelve_true_y_loguea(caplog):
    respx.put(f"{CONSUL_URL}/v1/agent/service/deregister/doc-1").mock(
        return_value=httpx.Response(200)
    )

    with caplog.at_level(logging.INFO):
        assert await make_registrar().deregister() is True

    assert "desregistrada de Consul" in caplog.text


# --- Un Consul caido no puede impedir arrancar ---------------------------


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


def test_dos_replicas_se_registran_con_ids_distintos(monkeypatch):
    """Escalado horizontal (issue #35): sin service_id explícito, el ID es el
    hostname del contenedor, distinto en cada réplica. Con un ID fijo, la
    segunda réplica pisaría el registro de la primera en Consul."""
    hostnames = iter(["a1b2c3d4e5f6", "f6e5d4c3b2a1"])
    monkeypatch.setattr("app.services.consul.socket.gethostname", lambda: next(hostnames))

    first, second = (
        build_registrar(
            http_client=httpx.AsyncClient(),
            consul_url="http://127.0.0.1:8500",
            token="",
            port=8002,
        )
        for _ in range(2)
    )

    assert first.registration.service_id == "a1b2c3d4e5f6"
    assert second.registration.service_id == "f6e5d4c3b2a1"
    assert first.registration.name == second.registration.name == SERVICE_NAME


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


async def ceder_el_loop(veces: int = 20) -> None:
    """Deja correr la tarea de fondo del registro."""
    import asyncio

    for _ in range(veces):
        await asyncio.sleep(0)


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
        await ceder_el_loop()
        assert fake.registered == 1
        assert fake.deregistered == 0

    assert fake.deregistered == 1


async def test_si_el_registro_falla_la_app_igual_atiende(repository, extractor):
    fake = FakeRegistrar(ok=False)

    async with running_app(_app(fake, enabled=True, repository=repository, extractor=extractor)) as client:
        assert (await client.get("/health")).status_code == 200
        await ceder_el_loop()

    assert fake.registered >= 1  # sigue reintentando hasta el apagado
    assert fake.deregistered == 0  # no se desregistra lo que nunca se registro


# --- Reintento en segundo plano (Consul inicializando el ACL) --------------


@respx.mock
async def test_el_registro_reintenta_tras_dos_403_hasta_el_200(caplog):
    route = respx.put(f"{CONSUL_URL}/v1/agent/service/register").mock(
        side_effect=[
            httpx.Response(403, text="ACL not found"),
            httpx.Response(403, text="ACL not found"),
            httpx.Response(200),
        ]
    )
    esperas: list[float] = []

    async def sleep_falso(segundos: float) -> None:
        esperas.append(segundos)

    with caplog.at_level(logging.INFO):
        await register_with_retry(make_registrar(), sleep=sleep_falso)

    assert route.call_count == 3
    assert esperas == [1.0, 2.0]  # backoff exponencial
    assert caplog.text.count("No se pudo registrar en Consul") == 2  # un WARNING por fallo
    assert [r.consul_registrado for r in caplog.records if hasattr(r, "consul_registrado")] == [True]


async def test_el_backoff_se_frena_en_el_maximo():
    class NuncaHastaElSexto:
        intentos = 0

        async def register(self) -> bool:
            self.intentos += 1
            return self.intentos == 6

    esperas: list[float] = []

    async def sleep_falso(segundos: float) -> None:
        esperas.append(segundos)

    await register_with_retry(
        NuncaHastaElSexto(), initial_delay=1.0, max_delay=4.0, sleep=sleep_falso
    )

    assert esperas == [1.0, 2.0, 4.0, 4.0, 4.0]


async def test_la_app_arranca_y_atiende_mientras_el_registro_reintenta(
    monkeypatch, repository, extractor
):
    """El registro no bloquea el arranque ni /health, y se completa solo."""
    import asyncio

    class RegistrarLento:
        def __init__(self) -> None:
            self.intentos = 0
            self.deregistered = 0

        async def register(self) -> bool:
            self.intentos += 1
            return self.intentos >= 3

        async def deregister(self) -> bool:
            self.deregistered += 1
            return True

    sleep_real = asyncio.sleep

    async def sleep_corto(segundos: float) -> None:
        await sleep_real(0)

    monkeypatch.setattr("app.services.consul.asyncio.sleep", sleep_corto)
    monkeypatch.setattr(
        "app.main.register_with_retry",
        lambda registrar: register_with_retry(registrar, sleep=sleep_corto),
    )
    fake = RegistrarLento()

    async with running_app(_app(fake, enabled=True, repository=repository, extractor=extractor)) as client:
        assert (await client.get("/health")).status_code == 200
        for _ in range(20):
            await sleep_real(0)
        assert fake.intentos == 3

    assert fake.deregistered == 1


# --- Tags de Traefik en el registro (issue #27) ---------------------------


def tags_de(tags: tuple[str, ...]) -> dict[str, str]:
    """Los tags como diccionario clave -> valor, para poder afirmar de a uno."""
    return dict(tag.split("=", 1) for tag in tags)


def test_los_tags_publican_a_documents_en_traefik():
    tags = tags_de(traefik_tags())

    assert tags["traefik.enable"] == "true"
    assert tags["traefik.http.routers.documents.rule"] == "PathPrefix(`/documents`)"
    # Solo el entrypoint de trafico, como extract: el otro es el del dashboard.
    assert tags["traefik.http.routers.documents.entrypoints"] == "web"


def test_documents_no_declara_middlewares_en_traefik():
    """Ni circuit breaker ni retry en el gateway: no es lo que pide la #27."""
    assert not any("middlewares" in tag for tag in traefik_tags())


def test_el_payload_del_registro_lleva_los_tags():
    registration = Registration(
        service_id="doc-1", address="172.20.0.9", port=8002, tags=traefik_tags()
    )

    assert registration.payload()["Tags"] == list(traefik_tags())


def test_sin_tags_el_payload_trae_la_lista_vacia():
    payload = Registration(service_id="doc-1", address="172.20.0.9", port=8002).payload()

    assert payload["Tags"] == []


@respx.mock
async def test_los_tags_viajan_en_el_put_al_agente():
    route = respx.put(f"{CONSUL_URL}/v1/agent/service/register").mock(
        return_value=httpx.Response(200)
    )

    await make_agent().register(
        Registration(service_id="doc-1", address="172.20.0.9", port=8002, tags=traefik_tags())
    )

    tags = tags_de(tuple(json.loads(route.calls[0].request.content)["Tags"]))
    assert tags["traefik.http.routers.documents.rule"] == "PathPrefix(`/documents`)"


def test_build_registrar_pasa_los_tags_al_registro():
    registrar = build_registrar(
        http_client=httpx.AsyncClient(),
        consul_url="http://127.0.0.1:8500",
        token="",
        port=8002,
        tags=("traefik.enable=true", "otro=1"),
    )

    assert registrar is not None
    assert registrar.registration.tags == ("traefik.enable=true", "otro=1")


async def test_la_app_se_registra_con_los_tags_de_traefik(monkeypatch, repository, extractor):
    """Que traefik_tags() exista no sirve si el lifespan no lo usa."""
    recibido = {}

    def fake_build_registrar(**kwargs):
        recibido.update(kwargs)
        return FakeRegistrar()

    monkeypatch.setattr("app.main.build_registrar", fake_build_registrar)
    app = create_app(
        Settings(CONSUL_ENABLED=True, LOG_LEVEL="WARNING"),
        repository=repository,
        extractor=extractor,
    )

    async with running_app(app) as client:
        assert (await client.get("/health")).status_code == 200

    assert recibido["tags"] == traefik_tags()


# --- documents ya no elige replicas: siempre EXTRACT_URL (issue #27) --------


@pytest.mark.parametrize("enabled", [True, False])
@respx.mock
async def test_el_extractor_siempre_llama_a_extract_url(enabled, repository, sample_pdf):
    """Con o sin Consul, el destino es EXTRACT_URL (el gateway).

    CONSUL_ENABLED solo controla el registro: no se le pregunta a Consul por
    las replicas de extract (eso lo hace Traefik).
    """
    consul = respx.get(url__startswith=f"{CONSUL_URL}/v1/health/service/").mock(
        return_value=httpx.Response(200, json=[])
    )
    gateway = respx.post("http://traefik/extract").mock(
        return_value=httpx.Response(200, json={"content": "hola", "page_count": 1})
    )
    app = create_app(
        Settings(CONSUL_ENABLED=enabled, EXTRACT_URL="http://traefik", LOG_LEVEL="WARNING"),
        repository=repository,
        consul_registrar=FakeRegistrar(),
    )

    async with running_app(app) as client:
        response = await client.post(
            "/documents", files={"file": ("a.pdf", sample_pdf, "application/pdf")}
        )

    assert response.status_code == 201
    assert gateway.call_count == 1
    assert not consul.called
