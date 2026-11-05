"""Upload limits and admission control of ``POST /documents`` (issue #30).

These tests drive the ASGI app by hand instead of through httpx, so they can
count exactly how many body bytes the app pulled from ``receive`` — that is,
from the socket — and simulate a client that disconnects mid-upload.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest

from app.api.documents import MULTIPART_OVERHEAD
from app.exceptions import ExtractUnavailableError
from app.main import create_app
from app.models import ExtractionResult
from tests.conftest import SAMPLE_PDF, StubExtractor, running_app

BOUNDARY = "limite-de-prueba"
CHUNK = 64 * 1024


def multipart(pdf: bytes = SAMPLE_PDF, filename: str = "informe.pdf") -> bytes:
    return (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode() + pdf + f"\r\n--{BOUNDARY}--\r\n".encode()


@dataclass
class Exchange:
    """What the app answered, and how much of the body it read."""

    status: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    read_bytes: int = 0


async def post(
    app,
    chunks,
    *,
    content_type: str = f"multipart/form-data; boundary={BOUNDARY}",
    content_length: int | None = None,
    disconnect_after: int | None = None,
) -> Exchange:
    """Send ``POST /documents`` straight through ASGI.

    Args:
        chunks: body chunks, sent one ``http.request`` message each.
        content_length: declared ``Content-Length``; omitted means chunked.
        disconnect_after: send ``http.disconnect`` after this many chunks.
    """
    exchange = Exchange()
    headers = [(b"content-type", content_type.encode())]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    else:
        headers.append((b"transfer-encoding", b"chunked"))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/documents",
        "raw_path": b"/documents",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
    }
    source = iter(chunks)
    pending = next(source, None)
    sent = 0

    async def receive():
        nonlocal pending, sent
        if disconnect_after is not None and sent >= disconnect_after:
            return {"type": "http.disconnect"}
        if pending is None:
            # Past the end of the body, like a real server.
            await asyncio.Event().wait()
        chunk, pending = pending, next(source, None)
        sent += 1
        exchange.read_bytes += len(chunk)
        return {"type": "http.request", "body": chunk, "more_body": pending is not None}

    async def send(message):
        if message["type"] == "http.response.start":
            exchange.status = message["status"]
            exchange.headers = {k.decode(): v.decode() for k, v in message["headers"]}
        elif message["type"] == "http.response.body":
            exchange.body += message.get("body", b"")

    await app(scope, receive, send)
    return exchange


def endless(chunk_size: int = CHUNK, total: int = 20 * 1024 * 1024):
    """A multipart body far bigger than any limit, produced lazily."""
    yield multipart(b"")[: -len(f"\r\n--{BOUNDARY}--\r\n")] + b"%PDF"
    sent = 0
    while sent < total:
        yield b"\0" * chunk_size
        sent += chunk_size


def split(data: bytes, size: int = CHUNK) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)] or [b""]


@pytest.fixture
def build(settings, repository, extractor, clock):
    """A running app, configured through keyword overrides of the settings."""

    @asynccontextmanager
    async def _build(*, extractor_=None, **overrides):
        app = create_app(
            settings.model_copy(update=overrides),
            repository=repository,
            extractor=extractor_ or extractor,
            clock=clock,
        )
        async with running_app(app) as client:
            yield app, client

    return _build


class BlockingExtractor(StubExtractor):
    """Holds every extraction until the test lets it go."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def extract(self, pdf, *, request_id=None) -> ExtractionResult:
        self.started.set()
        await self.release.wait()
        return await super().extract(pdf, request_id=request_id)


class TestSizeLimit:
    async def test_a_chunked_upload_is_cut_as_soon_as_it_exceeds_the_limit(self, build):
        """The bug of #30: with ``File(...)`` the whole 20 MB were read first."""
        async with build(MAX_UPLOAD_SIZE=1024) as (app, _):
            result = await post(app, endless())

        assert result.status == 413
        assert result.read_bytes <= 1024 + MULTIPART_OVERHEAD + CHUNK

    async def test_a_declared_oversize_is_refused_without_reading_or_taking_a_slot(self, build):
        async with build(MAX_UPLOAD_SIZE=1024, MAX_CONCURRENT_UPLOADS=1, UPLOAD_SLOT_TIMEOUT=0.05) as (
            app,
            _,
        ):
            await app.state.upload_slots.acquire()  # no slot left for anybody
            result = await post(app, endless(), content_length=20 * 1024 * 1024)

        assert result.status == 413  # not 503: it never asked for a slot
        assert result.read_bytes == 0

    async def test_a_file_of_exactly_the_limit_is_accepted(self, build):
        pdf = SAMPLE_PDF.ljust(4096, b"\0")
        async with build(MAX_UPLOAD_SIZE=len(pdf)) as (app, _):
            result = await post(app, split(multipart(pdf)), content_length=len(multipart(pdf)))
        assert result.status == 201

    async def test_one_byte_over_the_limit_is_rejected(self, build):
        """The margin is for the multipart framing, not for the file."""
        pdf = SAMPLE_PDF.ljust(4096, b"\0")
        async with build(MAX_UPLOAD_SIZE=len(pdf) - 1) as (app, _):
            result = await post(app, split(multipart(pdf)))
        assert result.status == 413


class TestAdmission:
    async def test_without_a_free_slot_the_upload_is_shed_without_reading_it(self, build):
        async with build(MAX_CONCURRENT_UPLOADS=2, UPLOAD_SLOT_TIMEOUT=0.05) as (app, client):
            for _ in range(2):
                await app.state.upload_slots.acquire()

            result = await post(app, split(multipart()))
            health = await client.get("/health")

        assert result.status == 503
        assert result.headers["retry-after"] == "1"
        assert result.read_bytes == 0
        assert health.status_code == 200

    async def test_a_slot_is_held_through_the_extraction(self, build):
        """The PDF stays in memory until it is stored, so the slot does too."""
        blocking = BlockingExtractor()
        async with build(
            extractor_=blocking, MAX_CONCURRENT_UPLOADS=1, UPLOAD_SLOT_TIMEOUT=0.05
        ) as (app, _):
            first = asyncio.create_task(post(app, split(multipart())))
            await blocking.started.wait()

            shed = await post(app, split(multipart(SAMPLE_PDF + b"otro")))

            blocking.release.set()
            done = await first

        assert shed.status == 503
        assert shed.read_bytes == 0
        assert done.status == 201

    async def test_a_waiting_upload_gets_the_slot_once_it_frees_up(self, build):
        blocking = BlockingExtractor()
        async with build(
            extractor_=blocking, MAX_CONCURRENT_UPLOADS=1, UPLOAD_SLOT_TIMEOUT=5.0
        ) as (app, _):
            first = asyncio.create_task(post(app, split(multipart())))
            await blocking.started.wait()
            second = asyncio.create_task(post(app, split(multipart(SAMPLE_PDF + b"otro"))))
            await asyncio.sleep(0.01)
            blocking.release.set()

            results = await asyncio.gather(first, second)

        assert [r.status for r in results] == [201, 201]


class TestSlotRelease:
    """With a single slot, a follow-up upload only succeeds if it was released."""

    async def _assert_released(self, app):
        follow_up = await post(app, split(multipart(SAMPLE_PDF + b"siguiente")))
        assert follow_up.status == 201

    async def test_after_a_413(self, build):
        async with build(MAX_UPLOAD_SIZE=1024, MAX_CONCURRENT_UPLOADS=1, UPLOAD_SLOT_TIMEOUT=0.05) as (
            app,
            _,
        ):
            assert (await post(app, endless())).status == 413
            await self._assert_released(app)

    async def test_after_a_400(self, build):
        async with build(MAX_CONCURRENT_UPLOADS=1, UPLOAD_SLOT_TIMEOUT=0.05) as (app, _):
            assert (await post(app, split(multipart(b"no soy pdf")))).status == 400
            await self._assert_released(app)

    async def test_after_a_failure_of_store(self, build):
        failing = StubExtractor(error=ExtractUnavailableError())
        async with build(
            extractor_=failing, MAX_CONCURRENT_UPLOADS=1, UPLOAD_SLOT_TIMEOUT=0.05
        ) as (app, _):
            assert (await post(app, split(multipart()))).status == 503
            failing.error = None
            await self._assert_released(app)

    async def test_after_an_unexpected_error(self, build):
        failing = StubExtractor(error=RuntimeError("bug"))
        async with build(
            extractor_=failing, MAX_CONCURRENT_UPLOADS=1, UPLOAD_SLOT_TIMEOUT=0.05
        ) as (app, _):
            with pytest.raises(RuntimeError):
                await post(app, split(multipart()))
            failing.error = None
            await self._assert_released(app)

    async def test_after_the_client_disconnects_mid_upload(self, build, extractor):
        async with build(MAX_CONCURRENT_UPLOADS=1, UPLOAD_SLOT_TIMEOUT=0.05) as (app, _):
            cut = await post(app, split(multipart(SAMPLE_PDF * 400), size=1024), disconnect_after=3)
            assert cut.read_bytes == 3 * 1024
            assert extractor.calls == 0
            await self._assert_released(app)


class TestRequestShape:
    @pytest.mark.parametrize(
        "content_type",
        ["application/pdf", "application/json", "multipart/form-data", "text/plain; boundary=x"],
    )
    async def test_anything_but_multipart_with_a_boundary_is_a_400(self, build, content_type):
        async with build() as (app, _):
            result = await post(app, [SAMPLE_PDF], content_type=content_type)
        assert result.status == 400
        assert result.read_bytes == 0

    async def test_a_body_without_the_file_field_is_a_400(self, make_client):
        async with make_client() as client:
            response = await client.post(
                "/documents", files={"otro": ("a.pdf", SAMPLE_PDF, "application/pdf")}
            )
        assert response.status_code == 400

    async def test_a_truncated_multipart_is_a_400(self, build, extractor):
        async with build() as (app, _):
            raw = multipart()
            result = await post(app, [raw[: len(raw) - 10]])
        assert result.status == 400
        assert extractor.calls == 0

    async def test_a_filename_with_accents_survives(self, make_client):
        async with make_client() as client:
            response = await client.post(
                "/documents", files={"file": ("año señal.pdf", SAMPLE_PDF, "application/pdf")}
            )
        assert response.status_code == 201
        assert response.json()["filename"] == "año señal.pdf"

    async def test_the_upload_is_still_documented_in_openapi(self, make_client):
        async with make_client() as client:
            spec = (await client.get("/openapi.json")).json()

        body = spec["paths"]["/documents"]["post"]["requestBody"]
        schema = body["content"]["multipart/form-data"]["schema"]
        assert body["required"] is True
        assert schema["properties"]["file"] == {
            "type": "string",
            "format": "binary",
            "description": "Archivo PDF",
        }
        assert schema["required"] == ["file"]
