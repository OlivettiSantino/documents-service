"""In-memory ``multipart/form-data`` parsing."""

import pytest

from app.api.multipart import (
    MalformedMultipartError,
    UploadedFile,
    boundary_from,
    file_from_multipart,
)
from tests.conftest import SAMPLE_PDF

BOUNDARY = b"----limite123"


def part(disposition: str, content: bytes, content_type: str | None = None) -> bytes:
    headers = f"Content-Disposition: {disposition}\r\n"
    if content_type:
        headers += f"Content-Type: {content_type}\r\n"
    return b"--" + BOUNDARY + b"\r\n" + headers.encode("utf-8") + b"\r\n" + content + b"\r\n"


def body(*parts: bytes) -> bytes:
    return b"".join(parts) + b"--" + BOUNDARY + b"--\r\n"


class TestBoundary:
    def test_reads_the_boundary(self):
        assert boundary_from("multipart/form-data; boundary=abc") == b"abc"

    def test_reads_a_quoted_boundary(self):
        assert boundary_from('multipart/form-data; boundary="a b:c"') == b"a b:c"

    def test_the_media_type_is_case_insensitive(self):
        assert boundary_from("Multipart/Form-Data; boundary=abc") == b"abc"

    @pytest.mark.parametrize(
        "content_type",
        [
            "",
            "application/pdf",
            "application/json; boundary=abc",
            "multipart/mixed; boundary=abc",
            "multipart/form-data",
            "multipart/form-data; boundary=",
        ],
    )
    def test_anything_else_has_no_boundary(self, content_type):
        assert boundary_from(content_type) is None


class TestFileFromMultipart:
    def test_finds_the_file(self):
        raw = body(part('form-data; name="file"; filename="informe.pdf"', SAMPLE_PDF, "application/pdf"))

        assert file_from_multipart(raw, BOUNDARY) == UploadedFile(
            filename="informe.pdf", content=SAMPLE_PDF
        )

    def test_accepts_a_bytearray(self):
        raw = bytearray(body(part('form-data; name="file"; filename="a.pdf"', SAMPLE_PDF)))
        assert file_from_multipart(raw, BOUNDARY).content == SAMPLE_PDF

    def test_a_missing_file_field_is_none(self):
        raw = body(part('form-data; name="otro"; filename="a.pdf"', SAMPLE_PDF))
        assert file_from_multipart(raw, BOUNDARY) is None

    def test_picks_the_file_among_several_fields(self):
        raw = body(
            part('form-data; name="descripcion"', b"un texto"),
            part('form-data; name="file"; filename="informe.pdf"', SAMPLE_PDF),
            part('form-data; name="adjunto"; filename="otro.pdf"', b"%PDF-otro"),
        )
        assert file_from_multipart(raw, BOUNDARY) == UploadedFile(
            filename="informe.pdf", content=SAMPLE_PDF
        )

    def test_only_the_first_file_field_counts(self):
        raw = body(
            part('form-data; name="file"; filename="primero.pdf"', b"%PDF-1"),
            part('form-data; name="file"; filename="segundo.pdf"', b"%PDF-2"),
        )
        assert file_from_multipart(raw, BOUNDARY).filename == "primero.pdf"

    def test_a_file_without_filename_has_an_empty_name(self):
        raw = body(part('form-data; name="file"', SAMPLE_PDF))
        assert file_from_multipart(raw, BOUNDARY) == UploadedFile(filename="", content=SAMPLE_PDF)

    def test_an_empty_file_has_empty_content(self):
        raw = body(part('form-data; name="file"; filename="vacio.pdf"', b""))
        assert file_from_multipart(raw, BOUNDARY).content == b""

    def test_content_that_looks_like_a_boundary_is_kept(self):
        tricky = SAMPLE_PDF + b"\r\n--" + BOUNDARY[:-1] + b"x\r\n"
        raw = body(part('form-data; name="file"; filename="a.pdf"', tricky))
        assert file_from_multipart(raw, BOUNDARY).content == tricky

    def test_works_with_a_boundary_given_in_quotes(self):
        boundary = boundary_from(f'multipart/form-data; boundary="{BOUNDARY.decode()}"')
        raw = body(part('form-data; name="file"; filename="a.pdf"', SAMPLE_PDF))
        assert file_from_multipart(raw, boundary).content == SAMPLE_PDF


class TestFilename:
    def test_decodes_rfc_5987_filename_star(self):
        raw = body(part("form-data; name=\"file\"; filename*=UTF-8''informe%20a%C3%B1o.pdf", SAMPLE_PDF))
        assert file_from_multipart(raw, BOUNDARY).filename == "informe año.pdf"

    def test_filename_star_wins_over_filename(self):
        raw = body(
            part(
                "form-data; name=\"file\"; filename=\"fallback.pdf\"; "
                "filename*=UTF-8''acci%C3%B3n.pdf",
                SAMPLE_PDF,
            )
        )
        assert file_from_multipart(raw, BOUNDARY).filename == "acción.pdf"

    def test_honours_the_charset_of_filename_star(self):
        raw = body(part("form-data; name=\"file\"; filename*=iso-8859-1'es'pe%F1a.pdf", SAMPLE_PDF))
        assert file_from_multipart(raw, BOUNDARY).filename == "peña.pdf"

    def test_a_raw_utf8_filename_is_decoded(self):
        """What browsers, curl and httpx actually send."""
        raw = body(part('form-data; name="file"; filename="señal ñandú.pdf"', SAMPLE_PDF))
        assert file_from_multipart(raw, BOUNDARY).filename == "señal ñandú.pdf"

    def test_a_quoted_filename_may_contain_separators(self):
        raw = body(part('form-data; name="file"; filename="a; b=c.pdf"', SAMPLE_PDF))
        assert file_from_multipart(raw, BOUNDARY).filename == "a; b=c.pdf"

    def test_the_header_name_is_case_insensitive(self):
        raw = (
            b"--" + BOUNDARY + b"\r\n"
            b'content-disposition: form-data; name="file"; filename="a.pdf"\r\n\r\n'
            + SAMPLE_PDF
            + b"\r\n--"
            + BOUNDARY
            + b"--\r\n"
        )
        assert file_from_multipart(raw, BOUNDARY).filename == "a.pdf"


class TestMalformed:
    def test_a_truncated_body_is_rejected(self):
        """Without the closing boundary the file could be silently cut short."""
        raw = body(part('form-data; name="file"; filename="a.pdf"', SAMPLE_PDF))
        with pytest.raises(MalformedMultipartError):
            file_from_multipart(raw[: len(raw) // 2], BOUNDARY)

    def test_a_body_with_another_boundary_is_rejected(self):
        raw = body(part('form-data; name="file"; filename="a.pdf"', SAMPLE_PDF))
        with pytest.raises(MalformedMultipartError):
            file_from_multipart(raw, b"otro-boundary")

    def test_garbage_is_rejected(self):
        with pytest.raises(MalformedMultipartError):
            file_from_multipart(b"esto no es multipart", BOUNDARY)
