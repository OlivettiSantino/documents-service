"""Validación condicional de HTTP (issue #56)."""

import pytest

from app.api.caching import CACHE_CONTROL, etag_for, if_none_match_matches

CHECKSUM = "a" * 64
ETAG = f'"{CHECKSUM}"'


def test_el_etag_es_el_checksum_entre_comillas():
    """Fuerte, sin `W/`: el checksum identifica el contenido byte a byte."""
    assert etag_for(CHECKSUM) == ETAG


def test_el_cache_control_revalida_siempre():
    """El contenido es inmutable, pero la existencia no: hay DELETE. Un
    `immutable` dejaría servir de la caché un documento ya borrado."""
    assert "no-cache" in CACHE_CONTROL
    assert "immutable" not in CACHE_CONTROL


@pytest.mark.parametrize(
    "header",
    [
        pytest.param(ETAG, id="exacto"),
        pytest.param(f"  {ETAG}  ", id="con-espacios"),
        pytest.param("*", id="asterisco"),
        pytest.param(f'"otro", {ETAG}', id="en-una-lista"),
        pytest.param(f'{ETAG}, "otro"', id="primero-de-la-lista"),
        pytest.param(f"W/{ETAG}", id="debil-compara-igual"),
        pytest.param('"otro", *', id="asterisco-en-la-lista"),
    ],
)
def test_el_cliente_ya_lo_tiene(header):
    assert if_none_match_matches(header, ETAG) is True


@pytest.mark.parametrize(
    "header",
    [
        pytest.param(None, id="sin-header"),
        pytest.param("", id="vacio"),
        pytest.param('"otro"', id="otro-etag"),
        pytest.param(CHECKSUM, id="sin-comillas-no-es-un-etag"),
        pytest.param('"a"', id="prefijo-no-alcanza"),
        pytest.param(",", id="solo-comas"),
    ],
)
def test_el_cliente_no_lo_tiene(header):
    assert if_none_match_matches(header, ETAG) is False
