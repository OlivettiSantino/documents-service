"""Descubrimiento de extract-service a través de Consul (issue #24).

Todo con dobles: el agente de Consul se intercepta con ``respx`` y el paso del
tiempo con el ``FakeClock``, así que el caché se puede vencer sin esperar.
"""

import httpx
import pytest
import respx

from app.exceptions import ExtractUnavailableError
from app.services.consul import ConsulAgent
from app.services.discovery import ConsulEndpoints, StaticEndpoint

CONSUL_URL = "http://consul:8500"
HEALTH_URL = f"{CONSUL_URL}/v1/health/service/extract"


def catalog(*addresses: str, port: int = 8001) -> list[dict]:
    """Respuesta de /v1/health/service/extract?passing con esas instancias."""
    return [
        {"Node": {"Address": "10.0.0.1"}, "Service": {"Address": address, "Port": port}}
        for address in addresses
    ]


def make_endpoints(clock, *, cache_ttl: float = 5.0, token: str = "") -> ConsulEndpoints:
    return ConsulEndpoints(
        agent=ConsulAgent(
            http_client=httpx.AsyncClient(timeout=httpx.Timeout(2.0)),
            consul_url=CONSUL_URL,
            token=token,
        ),
        clock=clock,
        cache_ttl=cache_ttl,
    )


# --- Sin Consul se mantiene EXTRACT_URL ----------------------------------


async def test_el_endpoint_estatico_devuelve_siempre_la_url_configurada():
    endpoint = StaticEndpoint("http://extract:8001/")

    assert await endpoint.base_url() == "http://extract:8001"
    assert await endpoint.base_url() == "http://extract:8001"


# --- Varias instancias: round robin --------------------------------------


@respx.mock
async def test_reparte_entre_las_instancias_passing(clock):
    respx.get(HEALTH_URL).mock(
        return_value=httpx.Response(200, json=catalog("172.20.0.3", "172.20.0.4", "172.20.0.5"))
    )
    endpoints = make_endpoints(clock)

    elegidas = [await endpoints.base_url() for _ in range(6)]

    assert sorted(set(elegidas)) == [
        "http://172.20.0.3:8001",
        "http://172.20.0.4:8001",
        "http://172.20.0.5:8001",
    ]
    # Round robin: con 3 instancias y 6 pedidos, dos vueltas parejas.
    assert all(elegidas.count(url) == 2 for url in set(elegidas))


@respx.mock
async def test_una_sola_instancia_siempre_se_elige(clock):
    respx.get(HEALTH_URL).mock(return_value=httpx.Response(200, json=catalog("172.20.0.3")))

    endpoints = make_endpoints(clock)

    assert await endpoints.base_url() == "http://172.20.0.3:8001"
    assert await endpoints.base_url() == "http://172.20.0.3:8001"


@respx.mock
async def test_usa_la_direccion_del_nodo_si_el_servicio_no_trae_una(clock):
    respx.get(HEALTH_URL).mock(
        return_value=httpx.Response(
            200, json=[{"Node": {"Address": "10.0.0.7"}, "Service": {"Address": "", "Port": 8001}}]
        )
    )

    assert await make_endpoints(clock).base_url() == "http://10.0.0.7:8001"


@respx.mock
async def test_sigue_repartiendo_aunque_el_cache_se_renueve_entre_pedidos(clock):
    """El caso que se vio en la demo: un pedido cada pocos segundos con un
    caché corto. Si el cursor del round robin se reiniciara en cada refresco,
    solo se usarían las primeras instancias de la lista."""
    respx.get(HEALTH_URL).mock(
        return_value=httpx.Response(
            200, json=catalog("172.20.0.3", "172.20.0.4", "172.20.0.5", "172.20.0.6")
        )
    )
    endpoints = make_endpoints(clock, cache_ttl=5.0)

    elegidas = []
    for _ in range(8):
        elegidas.append(await endpoints.base_url())
        clock.advance(6.0)  # vence el caché antes de cada pedido

    assert len(set(elegidas)) == 4, elegidas
    assert all(elegidas.count(url) == 2 for url in set(elegidas)), elegidas


# --- Caché ----------------------------------------------------------------


@respx.mock
async def test_no_consulta_a_consul_en_cada_request(clock):
    route = respx.get(HEALTH_URL).mock(
        return_value=httpx.Response(200, json=catalog("172.20.0.3", "172.20.0.4"))
    )
    endpoints = make_endpoints(clock, cache_ttl=5.0)

    for _ in range(4):
        await endpoints.base_url()

    assert route.call_count == 1


@respx.mock
async def test_el_cache_se_vence(clock):
    route = respx.get(HEALTH_URL).mock(
        return_value=httpx.Response(200, json=catalog("172.20.0.3"))
    )
    endpoints = make_endpoints(clock, cache_ttl=5.0)

    await endpoints.base_url()
    clock.advance(5.1)
    await endpoints.base_url()

    assert route.call_count == 2


@respx.mock
async def test_el_token_de_acl_viaja_en_la_consulta(clock):
    route = respx.get(HEALTH_URL).mock(
        return_value=httpx.Response(200, json=catalog("172.20.0.3"))
    )

    await make_endpoints(clock, token="un-uuid").base_url()

    assert route.calls[0].request.headers["X-Consul-Token"] == "un-uuid"


@respx.mock
async def test_pide_solo_las_instancias_passing(clock):
    route = respx.get(HEALTH_URL).mock(
        return_value=httpx.Response(200, json=catalog("172.20.0.3"))
    )

    await make_endpoints(clock).base_url()

    assert "passing=true" in str(route.calls[0].request.url)


# --- Lista vacía y Consul caído: 503 -------------------------------------


@respx.mock
async def test_lista_vacia_es_503(clock):
    respx.get(HEALTH_URL).mock(return_value=httpx.Response(200, json=[]))

    with pytest.raises(ExtractUnavailableError):
        await make_endpoints(clock).base_url()


@respx.mock
async def test_consul_caido_es_503(clock):
    respx.get(HEALTH_URL).mock(side_effect=httpx.ConnectError("connection refused"))

    with pytest.raises(ExtractUnavailableError):
        await make_endpoints(clock).base_url()


@respx.mock
async def test_consul_respondiendo_mal_es_503(clock):
    respx.get(HEALTH_URL).mock(return_value=httpx.Response(500, text="boom"))

    with pytest.raises(ExtractUnavailableError):
        await make_endpoints(clock).base_url()


@respx.mock
async def test_una_lista_vacia_no_se_cachea_como_buena(clock):
    """Si todas las réplicas se cayeron, al volver una hay que verla."""
    route = respx.get(HEALTH_URL).mock(return_value=httpx.Response(200, json=[]))
    endpoints = make_endpoints(clock, cache_ttl=5.0)

    with pytest.raises(ExtractUnavailableError):
        await endpoints.base_url()

    route.mock(return_value=httpx.Response(200, json=catalog("172.20.0.3")))

    assert await endpoints.base_url() == "http://172.20.0.3:8001"


# --- El cliente de extract resuelve por pedido ---------------------------


@respx.mock
async def test_el_cliente_usa_la_instancia_que_le_da_el_discovery(clock, sample_pdf):
    """El breaker sigue siendo uno para extract como servicio, pero cada
    intento sale a la instancia que elige el discovery."""
    from tests.test_extractor_client import make_client

    respx.get(HEALTH_URL).mock(
        return_value=httpx.Response(200, json=catalog("172.20.0.3", "172.20.0.4"))
    )
    primera = respx.post("http://172.20.0.3:8001/extract").mock(
        return_value=httpx.Response(200, json={"content": "a", "page_count": 1})
    )
    segunda = respx.post("http://172.20.0.4:8001/extract").mock(
        return_value=httpx.Response(200, json={"content": "b", "page_count": 1})
    )

    client = make_client(endpoints=make_endpoints(clock))
    await client.extract(sample_pdf)
    await client.extract(sample_pdf)

    assert primera.called and segunda.called


@respx.mock
async def test_si_el_discovery_no_tiene_instancias_el_cliente_falla_con_503(clock, sample_pdf):
    from tests.test_extractor_client import make_client

    respx.get(HEALTH_URL).mock(return_value=httpx.Response(200, json=[]))

    client = make_client(endpoints=make_endpoints(clock))

    with pytest.raises(ExtractUnavailableError):
        await client.extract(sample_pdf)
