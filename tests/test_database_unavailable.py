"""MongoDB caido: 503 con Retry-After y sin detalle interno (issue #29).

El repositorio falso lanza los mismos errores que Motor cuando la base no
responde. Ninguno se atrapa en el repositorio: los convierte el handler de
``PyMongoError`` de ``create_app``.
"""

import logging

import pytest
from pymongo.errors import AutoReconnect, ServerSelectionTimeoutError

from app.exceptions import DatabaseUnavailableError
from tests.conftest import SAMPLE_PDF, InMemoryDocumentRepository

#: Lo que el driver pone en el mensaje: host, puerto y topologia.
DRIVER_TEXT = (
    "mongo:27017: [Errno -3] Temporary failure in name resolution, "
    "Timeout: 3.0s, Topology Description: <TopologyDescription id: 65f, "
    "topology_type: Unknown, servers: [<ServerDescription ('mongo', 27017)>]>"
)

ERRORS = [
    pytest.param(ServerSelectionTimeoutError, id="ServerSelectionTimeoutError"),
    pytest.param(AutoReconnect, id="AutoReconnect"),
]

#: Un valor distinto del default, para probar que sale de la configuracion.
RETRY_AFTER = 7

OBJECT_ID = "65f000000000000000000001"


class DownRepository(InMemoryDocumentRepository):
    """Cada operacion falla como falla Motor con Mongo caido."""

    def __init__(self, error: type[Exception]) -> None:
        super().__init__()
        self.error = error
        self.up = False  # ping() no lanza: informa, como el repositorio real

    def _fail(self, *_args, **_kwargs):
        raise self.error(DRIVER_TEXT)

    async def find_by_id(self, document_id: str):
        self._fail()

    async def find_by_checksum(self, checksum: str):
        self._fail()

    async def list(self, *, limit: int, offset: int):
        self._fail()

    async def count(self) -> int:
        self._fail()

    async def create(self, data):
        self._fail()

    async def delete(self, document_id: str) -> bool:
        self._fail()


@pytest.fixture
def settings_with_retry_after(settings):
    return settings.model_copy(update={"MONGO_RETRY_AFTER": RETRY_AFTER})


def requests():
    return [
        ("POST", "/documents", {"files": {"file": ("a.pdf", SAMPLE_PDF, "application/pdf")}}),
        ("GET", f"/documents/{OBJECT_ID}", {}),
        ("GET", "/documents", {}),
        ("DELETE", f"/documents/{OBJECT_ID}", {}),
    ]


@pytest.mark.parametrize("error", ERRORS)
@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    requests(),
    ids=["POST", "GET-id", "GET-listado", "DELETE"],
)
async def test_every_document_endpoint_answers_503_with_retry_after(
    make_client, settings_with_retry_after, extractor, error, method, path, kwargs
):
    async with make_client(
        settings=settings_with_retry_after, repository=DownRepository(error)
    ) as client:
        response = await client.request(method, path, **kwargs)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == str(RETRY_AFTER)
    assert response.json() == {"detail": DatabaseUnavailableError.message}
    for internal in ("mongo", "27017", "Topology", "Errno", error.__name__):
        assert internal not in response.text
    assert extractor.calls == 0  # el dedupe falla antes de llamar a extract


@pytest.mark.parametrize("error", ERRORS)
async def test_the_driver_error_is_logged_with_its_type(make_client, error, caplog):
    async with make_client(repository=DownRepository(error)) as client:
        with caplog.at_level(logging.ERROR):
            await client.get("/documents")

    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert record.getMessage() == "MongoDB no disponible"
    assert record.error_type == error.__name__
    assert "27017" in record.error


async def test_the_default_retry_after_is_mongo_retry_after(make_client, settings):
    async with make_client(repository=DownRepository(ServerSelectionTimeoutError)) as client:
        response = await client.get("/documents")

    assert response.headers["Retry-After"] == str(settings.MONGO_RETRY_AFTER) == "5"


async def test_the_request_id_still_comes_back(make_client):
    async with make_client(repository=DownRepository(AutoReconnect)) as client:
        response = await client.get("/documents", headers={"X-Request-ID": "mongo-caido-1"})

    assert response.status_code == 503
    assert response.headers["X-Request-ID"] == "mongo-caido-1"


async def test_health_stays_as_it_was_with_mongo_down(make_client):
    """/health no toca Mongo; /health/ready informa database: down, como antes."""
    async with make_client(repository=DownRepository(ServerSelectionTimeoutError)) as client:
        liveness = await client.get("/health")
        readiness = await client.get("/health/ready")

    assert liveness.status_code == 200
    assert readiness.status_code == 503
    assert readiness.json() == {"status": "not_ready", "database": "down"}
    assert "Retry-After" not in readiness.headers
