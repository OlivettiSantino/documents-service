"""Validación condicional de HTTP (``ETag`` / ``If-None-Match``), issue #56.

Un documento no cambia nunca: el contrato no tiene ``PUT`` y el checksum
SHA-256 identifica su contenido, así que el checksum ya **es** un ``ETag``
fuerte y no hay nada que calcular. Lo que ahorra un ``304`` es el cuerpo, que
es lo caro: el ``content`` puede llegar hasta ``MAX_CONTENT_BYTES`` (15 MiB).

**Por qué no se usa ``immutable``.** El contenido de un documento es inmutable,
pero su *existencia* no: hay ``DELETE``. Con ``Cache-Control: immutable`` y un
``max-age`` largo, un cliente seguiría sirviendo de su caché un documento ya
borrado hasta que venciera. Con ``no-cache`` revalida siempre: paga un viaje
—barato— y se ahorra el cuerpo, que es lo que importa, y un documento borrado
se entera en el acto con un ``404``.
"""

#: Revalidar siempre, nunca servir de la caché a ciegas (ver el módulo).
#: ``private`` porque hoy no hay autenticación: si se agregara, una caché
#: compartida no tendría por qué guardar el documento de otro.
CACHE_CONTROL = "private, no-cache"


def etag_for(checksum: str) -> str:
    """``ETag`` fuerte de un documento, a partir de su checksum."""
    return f'"{checksum}"'


def if_none_match_matches(header: str | None, etag: str) -> bool:
    """¿El cliente ya tiene esta representación?

    Acepta las dos formas que permite RFC 9110: ``*`` —cualquier
    representación, y si llegamos hasta acá el documento existe— y una lista de
    etags separados por coma, porque un cliente o una caché intermedia pueden
    mandar más de uno.

    Un ``W/`` adelante se ignora al comparar: RFC 9110 manda usar la
    comparación **débil** para ``If-None-Match``, y de todas formas este
    servicio nunca emite etags débiles.
    """
    if not header:
        return False
    candidates = [candidate.strip() for candidate in header.split(",")]
    if "*" in candidates:
        return True
    return any(_without_weak_prefix(c) == etag for c in candidates if c)


def _without_weak_prefix(value: str) -> str:
    return value[2:].strip() if value.startswith("W/") else value
