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
    RECHECK_INTERVAL,
    SERVICE_NAME,
    ConsulAgent,
    Registration,
    ServiceRegistrar,
    build_registrar,
    keep_registered,
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
@pytest.mark.parametrize(("status", "esperado"), [(200, True), (404, False)])
async def test_has_service_pregunta_al_agente_por_el_id(status, esperado):
    route = respx.get(f"{CONSUL_URL}/v1/agent/service/doc-1").mock(
        return_value=httpx.Response(status)
    )

    assert await make_agent(token="un-uuid").has_service("doc-1") is esperado
    assert route.calls[0].request.headers["X-Consul-Token"] == "un-uuid"


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


# --- ¿Consul todavía nos tiene? (issue #51) --------------------------------


@respx.mock
@pytest.mark.parametrize(("status", "esperado"), [(200, True), (404, False)])
async def test_is_registered_distingue_estar_de_no_estar(status, esperado):
    respx.get(f"{CONSUL_URL}/v1/agent/service/doc-1").mock(
        return_value=httpx.Response(status)
    )

    assert await make_registrar().is_registered() is esperado


@respx.mock
@pytest.mark.parametrize(
    "respuesta",
    [
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("timeout"),
        httpx.Response(403, text="ACL not found"),
        httpx.Response(500),
    ],
    ids=["red", "timeout", "403", "500"],
)
async def test_is_registered_sin_respuesta_clara_devuelve_none(respuesta, caplog):
    """Un error no es un 404: no se sabe si estamos, y no se propaga."""
    route = respx.get(f"{CONSUL_URL}/v1/agent/service/doc-1")
    if isinstance(respuesta, Exception):
        route.mock(side_effect=respuesta)
    else:
        route.mock(return_value=respuesta)

    assert await make_registrar(token="un-uuid").is_registered() is None
    assert "No se pudo verificar el registro en Consul" in caplog.text
    assert "un-uuid" not in caplog.text  # el token nunca se loguea


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

    async def is_registered(self) -> bool | None:
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

        async def is_registered(self) -> bool | None:
            return True

    sleep_real = asyncio.sleep

    async def sleep_corto(segundos: float) -> None:
        await sleep_real(0)

    monkeypatch.setattr("app.services.consul.asyncio.sleep", sleep_corto)
    monkeypatch.setattr(
        "app.main.keep_registered",
        lambda registrar: keep_registered(registrar, sleep=sleep_corto),
    )
    fake = RegistrarLento()

    async with running_app(_app(fake, enabled=True, repository=repository, extractor=extractor)) as client:
        assert (await client.get("/health")).status_code == 200
        for _ in range(20):
            await sleep_real(0)
        assert fake.intentos == 3

    assert fake.deregistered == 1


# --- Volver a registrarse si Consul borra la réplica (issue #51) -----------


class RegistradorGuionado:
    """Registrador falso que responde según un guion.

    ``registros``: lo que devuelve cada ``register()`` (agotado, ``True``).
    ``chequeos``: lo que devuelve cada ``is_registered()``.
    """

    def __init__(self, *, chequeos=(), registros=()) -> None:
        self.chequeos = list(chequeos)
        self.registros = list(registros)
        self.registered = 0
        self.deregistered = 0

    async def register(self) -> bool:
        self.registered += 1
        return self.registros.pop(0) if self.registros else True

    async def deregister(self) -> bool:
        self.deregistered += 1
        return True

    async def is_registered(self) -> bool | None:
        return self.chequeos.pop(0)


async def correr_hasta_agotar_el_guion(registrador: RegistradorGuionado) -> list[float]:
    """Corre keep_registered con un sleep falso y devuelve las esperas.

    Cuando ya no quedan chequeos en el guion, la espera del próximo chequeo
    levanta CancelledError: es lo que pasa cuando el apagado cancela la tarea
    mientras duerme entre chequeos.
    """
    import asyncio

    esperas: list[float] = []

    async def sleep_falso(segundos: float) -> None:
        esperas.append(segundos)
        if segundos == RECHECK_INTERVAL and not registrador.chequeos:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await keep_registered(registrador, sleep=sleep_falso)
    return esperas


def test_se_chequea_cada_30_segundos():
    assert RECHECK_INTERVAL == 30.0


async def test_si_consul_ya_no_tiene_la_replica_se_vuelve_a_registrar(caplog):
    registrador = RegistradorGuionado(chequeos=[True, False, True])

    with caplog.at_level(logging.INFO):
        esperas = await correr_hasta_agotar_el_guion(registrador)

    assert registrador.registered == 2
    assert esperas == [RECHECK_INTERVAL] * 4
    avisos = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.getMessage() for r in avisos] == [
        "Consul ya no tiene la réplica, se vuelve a registrar"
    ]
    # Cada registro logrado, el primero y el de vuelta, deja su consul_registrado=True.
    registrados = [r for r in caplog.records if getattr(r, "consul_registrado", False)]
    assert len(registrados) == 2


@pytest.mark.parametrize(
    "chequeos",
    [[True, True, True], [None, None, None], [True, None, True]],
    ids=["sigue-registrado", "no-se-sabe", "mezcla"],
)
async def test_no_se_vuelve_a_registrar_si_sigue_ahi_o_no_se_sabe(chequeos):
    """None es "no sé" (Consul caído, timeout): no alcanza para re-registrar."""
    registrador = RegistradorGuionado(chequeos=chequeos)

    await correr_hasta_agotar_el_guion(registrador)

    assert registrador.registered == 1


async def test_el_re_registro_vuelve_a_usar_el_backoff_desde_el_principio():
    registrador = RegistradorGuionado(chequeos=[False], registros=[True, False, False, True])

    esperas = await correr_hasta_agotar_el_guion(registrador)

    assert registrador.registered == 4
    assert esperas == [RECHECK_INTERVAL, 1.0, 2.0, RECHECK_INTERVAL]


async def test_cancelar_durante_el_ciclo_de_chequeo_desregistra():
    import asyncio

    registrador = RegistradorGuionado()
    durmiendo = asyncio.Event()

    async def sleep_hasta_que_cancelen(segundos: float) -> None:
        durmiendo.set()
        await asyncio.Event().wait()

    tarea = asyncio.create_task(keep_registered(registrador, sleep=sleep_hasta_que_cancelen))
    await durmiendo.wait()
    tarea.cancel()
    await asyncio.gather(tarea, return_exceptions=True)

    assert tarea.cancelled()
    assert (registrador.registered, registrador.deregistered) == (1, 1)


async def test_cancelar_despues_de_re_registrar_desregistra_una_sola_vez():
    registrador = RegistradorGuionado(chequeos=[False, True])

    await correr_hasta_agotar_el_guion(registrador)

    assert registrador.registered == 2
    assert registrador.deregistered == 1


async def test_cancelar_sin_haberse_registrado_nunca_no_desregistra():
    import asyncio

    registrador = RegistradorGuionado(registros=[False] * 100)
    durmiendo = asyncio.Event()

    async def sleep_hasta_que_cancelen(segundos: float) -> None:
        durmiendo.set()
        await asyncio.Event().wait()

    tarea = asyncio.create_task(keep_registered(registrador, sleep=sleep_hasta_que_cancelen))
    await durmiendo.wait()  # durmiendo en el backoff, tras el primer fallo
    tarea.cancel()
    await asyncio.gather(tarea, return_exceptions=True)

    assert registrador.registered == 1
    assert registrador.deregistered == 0


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
