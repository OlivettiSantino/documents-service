"""The HTTP contract of ``/documents``."""

import httpx
import pytest
import respx

from app.exceptions import (
    ExtractProtocolError,
    ExtractTimeoutError,
    ExtractUnavailableError,
    UnprocessablePdfError,
)
from app.services.extractor_client import ExtractorClient
from tests.conftest import SAMPLE_PDF, StubExtractor

GATEWAY = "http://traefik"


def gateway_extractor() -> ExtractorClient:
    """El cliente real de extract, para mockear el gateway con respx."""
    return ExtractorClient(http_client=httpx.AsyncClient(), base_url=GATEWAY)


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

    @pytest.mark.parametrize(
        "content,expected",
        [
            ("a" * 1001, 422),  # un byte por encima del limite
            ("a" * 1000, 201),  # exactamente en el limite
            ("ñ" * 501, 422),  # 501 caracteres, pero 1002 bytes UTF-8
        ],
    )
    async def test_extracted_text_over_max_content_bytes_answers_422(
        self, make_client, settings, repository, content, expected
    ):
        settings = settings.model_copy(update={"MAX_CONTENT_BYTES": 1000})
        async with make_client(
            settings=settings, extractor=StubExtractor(content=content)
        ) as client:
            response = await client.post("/documents", files=upload())

        assert response.status_code == expected
        if expected == 422:
            assert response.json() == {
                "detail": "El texto extraído supera el máximo que se puede almacenar"
            }
            assert repository.create_calls == 0

    async def test_an_oversized_upload_is_rejected(self, make_client, settings):
        settings.MAX_UPLOAD_SIZE = 32
        async with make_client(settings=settings) as client:
            response = await client.post("/documents", files=upload(SAMPLE_PDF * 10))
        assert response.status_code == 413

    @pytest.mark.parametrize(
        "error,expected_status",
        [
            (UnprocessablePdfError(), 422),
            (ExtractProtocolError(), 502),
            (ExtractTimeoutError(), 504),
            (ExtractUnavailableError(), 503),
            (ExtractUnavailableError(retry_after=12.0), 503),
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
            (ExtractUnavailableError(retry_after=12.0), "12"),
            (ExtractUnavailableError(retry_after=2.0), "2"),
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

    async def test_an_unprocessable_pdf_answers_422_with_a_fixed_message(
        self, make_client, repository
    ):
        extractor = StubExtractor(error=UnprocessablePdfError())
        async with make_client(extractor=extractor) as client:
            response = await client.post("/documents", files=upload())

        assert response.status_code == 422
        assert response.json() == {"detail": UnprocessablePdfError.message}
        assert "Retry-After" not in response.headers
        assert repository.create_calls == 0  # un PDF rechazado no se guarda

    @respx.mock
    async def test_a_400_from_extract_reaches_the_client_as_422_without_its_detail(
        self, make_client
    ):
        """Antes salia como 502, como si fuera culpa de extract."""
        respx.post(f"{GATEWAY}/extract").mock(
            return_value=httpx.Response(400, json={"detail": "PdfReadError: EOF marker not found"})
        )
        async with make_client(extractor=gateway_extractor()) as client:
            response = await client.post("/documents", files=upload())

        assert response.status_code == 422
        assert response.json() == {"detail": UnprocessablePdfError.message}
        assert "EOF" not in response.text

    @respx.mock
    async def test_a_429_from_the_gateway_answers_503(self, make_client):
        """El inFlightReq de Traefik: saturacion, antes salia como 502."""
        respx.post(f"{GATEWAY}/extract").mock(
            return_value=httpx.Response(
                429, text="Too Many Requests", headers={"Retry-After": "4"}
            )
        )
        async with make_client(extractor=gateway_extractor()) as client:
            response = await client.post("/documents", files=upload())

        assert response.status_code == 503
        assert response.headers["Retry-After"] == "4"
        assert response.json() == {"detail": ExtractUnavailableError.message}


class TestRead:
    async def test_get_returns_the_full_document(self, make_client):
        async with make_client() as client:
            created = await client.post("/documents", files=upload())
            response = await client.get(f"/documents/{created.json()['id']}")

        assert response.status_code == 200
        assert response.json()["content"]

    @pytest.mark.parametrize("document_id", ["000000000000000000000000", "basura"])
    async def test_an_unknown_document_is_a_404(self, make_client, document_id):
        async with make_client() as client:
            response = await client.get(f"/documents/{document_id}")
        assert response.status_code == 404

    async def test_the_detail_carries_an_etag_and_revalidates(self, make_client):
        """El ETag es el checksum: identifica el contenido sin calcular nada."""
        async with make_client() as client:
            created = (await client.post("/documents", files=upload())).json()
            response = await client.get(f"/documents/{created['id']}")

        assert response.headers["etag"] == f'"{created["checksum"]}"'
        assert "no-cache" in response.headers["cache-control"]

    async def test_a_matching_if_none_match_answers_304_without_a_body(self, make_client):
        """Lo que ahorra el 304 es el cuerpo, que puede ser de megabytes."""
        async with make_client() as client:
            created = (await client.post("/documents", files=upload())).json()
            etag = (await client.get(f"/documents/{created['id']}")).headers["etag"]
            response = await client.get(
                f"/documents/{created['id']}", headers={"If-None-Match": etag}
            )

        assert response.status_code == 304
        assert not response.content
        # El 304 repite los headers de validación, como pide RFC 9110.
        assert response.headers["etag"] == etag
        assert "no-cache" in response.headers["cache-control"]

    async def test_a_stale_if_none_match_answers_the_document(self, make_client):
        async with make_client() as client:
            created = (await client.post("/documents", files=upload())).json()
            response = await client.get(
                f"/documents/{created['id']}", headers={"If-None-Match": '"viejo"'}
            )

        assert response.status_code == 200
        assert response.json()["content"]

    async def test_a_deleted_document_is_a_404_even_with_its_etag(self, make_client):
        """Por esto el Cache-Control no lleva `immutable`: el contenido no
        cambia, pero la existencia sí, y el cliente se entera al revalidar."""
        async with make_client() as client:
            created = (await client.post("/documents", files=upload())).json()
            etag = (await client.get(f"/documents/{created['id']}")).headers["etag"]
            await client.delete(f"/documents/{created['id']}")
            response = await client.get(
                f"/documents/{created['id']}", headers={"If-None-Match": etag}
            )

        assert response.status_code == 404

    async def test_the_listing_is_not_cacheable(self, make_client):
        """El listado cambia cada vez que entra un documento: no tiene ETag."""
        async with make_client() as client:
            await client.post("/documents", files=upload())
            response = await client.get("/documents")

        assert "etag" not in response.headers

    async def test_the_listing_omits_the_content(self, make_client):
        """``content`` can be megabytes per document."""
        async with make_client() as client:
            await client.post("/documents", files=upload())
            response = await client.get("/documents")

        body = response.json()
        assert response.status_code == 200
        assert body["total"] == 1
        assert "content" not in body["items"][0]
        assert body["items"][0]["page_count"] == 3

    async def test_the_listing_paginates(self, make_client):
        async with make_client() as client:
            for n in range(5):
                await client.post("/documents", files=upload(SAMPLE_PDF + bytes([n])))

            page = await client.get("/documents", params={"limit": 2, "offset": 0})
            tail = await client.get("/documents", params={"limit": 2, "offset": 4})

        assert len(page.json()["items"]) == 2
        assert page.json()["total"] == 5
        assert len(tail.json()["items"]) == 1

    @pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}])
    async def test_invalid_pagination_is_rejected(self, make_client, params):
        async with make_client() as client:
            response = await client.get("/documents", params=params)
        assert response.status_code == 422


class TestDelete:
    async def test_delete_removes_the_document(self, make_client):
        async with make_client() as client:
            created = await client.post("/documents", files=upload())
            document_id = created.json()["id"]

            deleted = await client.delete(f"/documents/{document_id}")
            after = await client.get(f"/documents/{document_id}")

        assert deleted.status_code == 204
        assert after.status_code == 404

    async def test_deleting_an_unknown_document_is_a_404(self, make_client):
        async with make_client() as client:
            response = await client.delete("/documents/000000000000000000000000")
        assert response.status_code == 404


class TestOpenApi:
    """Lo que declara /docs coincide con el mapeo de errores (issue #29)."""

    async def test_the_documented_error_statuses(self, make_client):
        async with make_client() as client:
            paths = (await client.get("/openapi.json")).json()["paths"]

        def statuses(path: str, method: str) -> set[str]:
            return set(paths[path][method]["responses"])

        assert {"400", "413", "422", "502", "503", "504"} <= statuses("/documents", "post")
        assert {"404", "503"} <= statuses("/documents/{document_id}", "get")
        assert "503" in statuses("/documents", "get")
        assert {"404", "503"} <= statuses("/documents/{document_id}", "delete")
