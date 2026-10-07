"""The HTTP contract of ``/documents``."""

import pytest

from app.exceptions import (
    BulkheadFullError,
    CircuitOpenError,
    ExtractProtocolError,
    ExtractTimeoutError,
    ExtractUnavailableError,
)
from tests.conftest import SAMPLE_PDF, StubExtractor


def upload(pdf: bytes = SAMPLE_PDF, filename: str = "informe.pdf"):
    return {"file": (filename, pdf, "application/pdf")}


class TestCreate:
    async def test_a_new_document_is_created(self, make_client):
        async with make_client() as client:
            response = await client.post("/documents", files=upload())

        assert response.status_code == 201
        body = response.json()
        assert body["duplicate"] is False
        assert body["filename"] == "informe.pdf"
        assert body["page_count"] == 3
        assert body["size_bytes"] == len(SAMPLE_PDF)
        assert body["content"]
        assert len(body["checksum"]) == 64

    async def test_a_repeated_pdf_answers_200_and_does_not_re_extract(
        self, make_client, extractor
    ):
        async with make_client() as client:
            first = await client.post("/documents", files=upload())
            second = await client.post("/documents", files=upload(filename="otro.pdf"))

        assert first.status_code == 201
        assert second.status_code == 200
        assert second.json()["duplicate"] is True
        assert second.json()["id"] == first.json()["id"]
        assert extractor.calls == 1

    @pytest.mark.parametrize(
        "files,expected",
        [
            (upload(b""), 400),
            (upload(b"no soy un pdf"), 400),
            (upload(SAMPLE_PDF, "notas.txt"), 400),
        ],
    )
    async def test_bad_uploads_are_rejected(self, make_client, files, expected):
        async with make_client() as client:
            response = await client.post("/documents", files=files)
        assert response.status_code == expected
        assert "detail" in response.json()

    async def test_an_oversized_upload_is_rejected(self, make_client, settings):
        settings.MAX_UPLOAD_SIZE = 32
        async with make_client(settings=settings) as client:
            response = await client.post("/documents", files=upload(SAMPLE_PDF * 10))
        assert response.status_code == 413

    @pytest.mark.parametrize(
        "error,expected_status",
        [
            (ExtractProtocolError(), 502),
            (ExtractTimeoutError(), 504),
            (ExtractUnavailableError(), 503),
            (CircuitOpenError(retry_after=12.0), 503),
            (BulkheadFullError(retry_after=2.0), 503),
        ],
    )
    async def test_extract_failures_map_to_the_right_status(
        self, make_client, error, expected_status
    ):
        async with make_client(extractor=StubExtractor(error=error)) as client:
            response = await client.post("/documents", files=upload())
        assert response.status_code == expected_status

    @pytest.mark.parametrize(
        "error,expected_header",
        [
            (CircuitOpenError(retry_after=12.0), "12"),
            (BulkheadFullError(retry_after=2.0), "2"),
            (ExtractUnavailableError(retry_after=0.4), "1"),
        ],
    )
    async def test_503s_tell_the_client_when_to_come_back(
        self, make_client, error, expected_header
    ):
        async with make_client(extractor=StubExtractor(error=error)) as client:
            response = await client.post("/documents", files=upload())
        assert response.headers["Retry-After"] == expected_header

    async def test_a_503_without_a_hint_carries_no_retry_after(self, make_client):
        async with make_client(extractor=StubExtractor(error=ExtractUnavailableError())) as client:
            response = await client.post("/documents", files=upload())
        assert response.status_code == 503
        assert "Retry-After" not in response.headers
