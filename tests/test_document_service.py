"""Validation, deduplication and orchestration."""

import hashlib
import logging

import pytest

from app.config import Settings
from app.exceptions import (
    CircuitOpenError,
    ContentTooLargeError,
    DocumentNotFoundError,
    InvalidPdfError,
)
from app.models import DocumentCreate
from app.services.document_service import DocumentService
from tests.conftest import SAMPLE_PDF, StubExtractor

MAX_CONTENT_BYTES = Settings().MAX_CONTENT_BYTES


def make_service(repository, extractor, max_content_bytes=MAX_CONTENT_BYTES):
    return DocumentService(
        repository=repository, extractor=extractor, max_content_bytes=max_content_bytes
    )


@pytest.fixture
def service(repository, extractor):
    return make_service(repository, extractor)


class TestValidation:
    async def test_an_empty_upload_is_rejected(self, service):
        with pytest.raises(InvalidPdfError):
            await service.store(filename="vacio.pdf", pdf=b"")

    async def test_a_file_that_is_not_a_pdf_is_rejected(self, service):
        with pytest.raises(InvalidPdfError):
            await service.store(filename="doc.pdf", pdf=b"soy un txt disfrazado")

    async def test_a_wrong_extension_is_rejected(self, service, sample_pdf):
        with pytest.raises(InvalidPdfError):
            await service.store(filename="doc.txt", pdf=sample_pdf)

    async def test_the_size_limit_is_not_a_domain_rule(self, service, extractor):
        """The size limit lives only at the HTTP edge, enforced while reading
        the body; once a PDF reaches the service, its size is not re-checked."""
        big = SAMPLE_PDF + b"\0" * (2 * 1024 * 1024)
        _, duplicate = await service.store(filename="grande.pdf", pdf=big)
        assert duplicate is False
        assert extractor.calls == 1

    async def test_nothing_invalid_ever_reaches_extract_service(self, service, extractor):
        for filename, pdf in [("x.pdf", b""), ("x.txt", SAMPLE_PDF), ("x.pdf", b"no soy pdf")]:
            with pytest.raises(InvalidPdfError):
                await service.store(filename=filename, pdf=pdf)
        assert extractor.calls == 0


class TestStore:
    async def test_stores_a_new_document(self, service, extractor, sample_pdf):
        document, duplicate = await service.store(filename="informe.pdf", pdf=sample_pdf)

        assert duplicate is False
        assert document.filename == "informe.pdf"
        assert document.checksum == hashlib.sha256(sample_pdf).hexdigest()
        assert document.page_count == extractor.page_count
        assert document.size_bytes == len(sample_pdf)
        assert document.content

    async def test_forwards_the_request_id_to_extract(self, service, extractor, sample_pdf):
        await service.store(filename="a.pdf", pdf=sample_pdf, request_id="req-1")
        assert extractor.request_ids == ["req-1"]

    async def test_a_repeated_pdf_never_calls_extract(self, service, extractor, sample_pdf):
        """Idempotency by checksum: the duplicate path must not consume a
        bulkhead slot nor be able to trip the circuit breaker."""
        first, _ = await service.store(filename="informe.pdf", pdf=sample_pdf)
        second, duplicate = await service.store(filename="otro-nombre.pdf", pdf=sample_pdf)

        assert duplicate is True
        assert second.id == first.id
        assert extractor.calls == 1

    async def test_a_duplicate_is_returned_even_when_extract_is_down(
        self, repository, sample_pdf
    ):
        """Reads of already-known PDFs survive an outage of extract-service."""
        working = make_service(repository, StubExtractor())
        stored, _ = await working.store(filename="a.pdf", pdf=sample_pdf)

        broken = make_service(repository, StubExtractor(error=CircuitOpenError(retry_after=5)))
        again, duplicate = await broken.store(filename="a.pdf", pdf=sample_pdf)

        assert duplicate is True
        assert again.id == stored.id

    async def test_a_concurrent_race_is_resolved_by_the_unique_index(
        self, repository, extractor, sample_pdf
    ):
        """Two identical uploads can both get past the read-before-write check;
        the unique index is the real guarantee."""
        checksum = hashlib.sha256(sample_pdf).hexdigest()
        existing = await repository.create(
            DocumentCreate(
                filename="ya-estaba.pdf",
                checksum=checksum,
                content="texto",
                page_count=1,
                size_bytes=len(sample_pdf),
            )
        )

        service = make_service(repository, extractor)
        # Simulate losing the race: the lookup misses, the insert collides.
        original_lookup = repository.find_by_checksum
        calls = {"n": 0}

        async def missing_once(value):
            calls["n"] += 1
            if calls["n"] == 1:
                return None
            return await original_lookup(value)

        repository.find_by_checksum = missing_once

        document, duplicate = await service.store(filename="carrera.pdf", pdf=sample_pdf)

        assert duplicate is True
        assert document.id == existing.id

    async def test_failures_from_extract_propagate_untouched(self, repository, sample_pdf):
        service = make_service(repository, StubExtractor(error=CircuitOpenError(retry_after=3)))
        with pytest.raises(CircuitOpenError):
            await service.store(filename="a.pdf", pdf=sample_pdf)
        assert await repository.count() == 0


class TestContentLimit:
    """The extracted text is stored inline and a BSON document caps at 16 MiB."""

    LIMIT = 1000

    async def test_text_over_the_limit_is_rejected_before_touching_mongo(
        self, repository, sample_pdf
    ):
        extractor = StubExtractor(content="a" * (self.LIMIT + 1))
        service = make_service(repository, extractor, self.LIMIT)

        with pytest.raises(ContentTooLargeError):
            await service.store(filename="a.pdf", pdf=sample_pdf)

        assert extractor.calls == 1
        assert repository.create_calls == 0

    async def test_text_exactly_at_the_limit_is_stored(self, repository, sample_pdf):
        extractor = StubExtractor(content="a" * self.LIMIT)
        service = make_service(repository, extractor, self.LIMIT)

        document, duplicate = await service.store(filename="a.pdf", pdf=sample_pdf)

        assert duplicate is False
        assert len(document.content) == self.LIMIT

    async def test_the_limit_counts_utf8_bytes_not_characters(self, repository, sample_pdf):
        """BSON stores UTF-8: 501 "ñ" are 501 characters but 1002 bytes."""
        content = "ñ" * (self.LIMIT // 2 + 1)
        assert len(content) < self.LIMIT < len(content.encode("utf-8"))
        service = make_service(repository, StubExtractor(content=content), self.LIMIT)

        with pytest.raises(ContentTooLargeError):
            await service.store(filename="a.pdf", pdf=sample_pdf)
        assert repository.create_calls == 0

    async def test_a_rejection_is_logged_as_a_warning_with_the_sizes(
        self, repository, sample_pdf, caplog
    ):
        service = make_service(
            repository, StubExtractor(content="a" * (self.LIMIT + 1)), self.LIMIT
        )

        with caplog.at_level(logging.WARNING), pytest.raises(ContentTooLargeError):
            await service.store(filename="a.pdf", pdf=sample_pdf)

        [record] = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert record.checksum == hashlib.sha256(sample_pdf).hexdigest()[:12]
        assert record.content_bytes == self.LIMIT + 1
        assert record.max_content_bytes == self.LIMIT


class TestQueries:
    async def test_get_returns_the_document(self, service, sample_pdf):
        stored, _ = await service.store(filename="a.pdf", pdf=sample_pdf)
        assert (await service.get(stored.id)).id == stored.id

    async def test_get_of_an_unknown_id_raises(self, service):
        with pytest.raises(DocumentNotFoundError):
            await service.get("000000000000000000000000")

    async def test_list_paginates_and_reports_the_total(self, service):
        for n in range(5):
            await service.store(filename=f"doc{n}.pdf", pdf=SAMPLE_PDF + bytes([n]))

        page, total = await service.list(limit=2, offset=0)
        assert len(page) == 2
        assert total == 5

        rest, _ = await service.list(limit=10, offset=4)
        assert len(rest) == 1

    async def test_delete_removes_the_document(self, service, sample_pdf):
        stored, _ = await service.store(filename="a.pdf", pdf=sample_pdf)
        await service.delete(stored.id)

        with pytest.raises(DocumentNotFoundError):
            await service.get(stored.id)

    async def test_delete_of_an_unknown_id_raises(self, service):
        with pytest.raises(DocumentNotFoundError):
            await service.delete("000000000000000000000000")
