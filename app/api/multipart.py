"""In-memory ``multipart/form-data`` parsing.

The upload is parsed from a body that is already in memory (and already
bounded), instead of through Starlette's form parser, which spools every file
larger than 1 MB to disk *before* the endpoint gets to check its size.

Uses the low-level ``MultipartParser`` of ``python-multipart`` (already a
FastAPI dependency) only for the framing. The ``Content-Disposition`` of each
part is decoded with the standard library, because ``python-multipart`` drops
the RFC 5987 ``filename*`` parameter and clients do send it for names with
accents or ``ñ``.
"""

from dataclasses import dataclass
from email.message import Message
from email.utils import collapse_rfc2231_value

from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, parse_options_header

#: Name of the form field that carries the PDF.
MULTIPART_FILE_FIELD = "file"


class MalformedMultipartError(ValueError):
    """The body does not follow the ``multipart/form-data`` framing."""


@dataclass(frozen=True)
class UploadedFile:
    """The content of a file field and the name the client gave it."""

    filename: str
    content: bytes


def boundary_from(content_type: str) -> bytes | None:
    """Return the boundary of a ``multipart/form-data`` content type.

    ``None`` when the media type is anything else or the boundary is missing:
    without a boundary there is no way to find the parts.
    """
    media_type, params = parse_options_header(content_type)
    # parse_options_header only lowercases the media type when there are no
    # parameters, and a multipart content type always has one.
    if media_type.strip().lower() != b"multipart/form-data":
        return None
    return params.get(b"boundary") or None


def file_from_multipart(
    body: bytes | bytearray, boundary: bytes, field: str = MULTIPART_FILE_FIELD
) -> UploadedFile | None:
    """Extract the first part named ``field`` from a multipart body.

    Only that part's data is copied; every other part is skipped.

    Returns:
        The file, or ``None`` if the body has no such field.

    Raises:
        MalformedMultipartError: if the framing is broken or truncated.
    """
    collector = _FieldCollector(field)
    try:
        parser = MultipartParser(boundary, callbacks=collector.callbacks())
        parser.write(body)
        parser.finalize()
    except FormParserError as exc:
        raise MalformedMultipartError(str(exc)) from exc

    # python-multipart does not verify the closing boundary on finalize(): a
    # truncated body would otherwise pass as a complete (and shorter) file.
    if not collector.finished:
        raise MalformedMultipartError("El cuerpo multipart está incompleto")
    return collector.result


class _FieldCollector:
    """Callbacks for ``MultipartParser`` that keep a single field."""

    def __init__(self, field: str) -> None:
        self._field = field
        self._header_name = bytearray()
        self._header_value = bytearray()
        self._disposition: str | None = None
        self._capturing = False
        self._data = bytearray()
        self._filename = ""
        self.result: UploadedFile | None = None
        self.finished = False

    def callbacks(self) -> dict:
        return {
            "on_part_begin": self._on_part_begin,
            "on_header_field": self._on_header_field,
            "on_header_value": self._on_header_value,
            "on_header_end": self._on_header_end,
            "on_headers_finished": self._on_headers_finished,
            "on_part_data": self._on_part_data,
            "on_part_end": self._on_part_end,
            "on_end": self._on_end,
        }

    def _on_part_begin(self) -> None:
        self._disposition = None
        self._header_name.clear()
        self._header_value.clear()

    def _on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_name += data[start:end]

    def _on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def _on_header_end(self) -> None:
        if bytes(self._header_name).strip().lower() == b"content-disposition":
            self._disposition = bytes(self._header_value).decode("latin-1")
        self._header_name.clear()
        self._header_value.clear()

    def _on_headers_finished(self) -> None:
        if self.result is not None or self._disposition is None:
            return  # only the first matching part counts
        name, filename = _parse_disposition(self._disposition)
        if name == self._field:
            self._capturing = True
            self._filename = filename

    def _on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._capturing:
            self._data += data[start:end]

    def _on_part_end(self) -> None:
        if self._capturing:
            self.result = UploadedFile(filename=self._filename, content=bytes(self._data))
            self._capturing = False
            self._data = bytearray()

    def _on_end(self) -> None:
        self.finished = True


def _parse_disposition(value: str) -> tuple[str | None, str]:
    """Return ``(name, filename)`` from a ``Content-Disposition`` value.

    ``filename*`` (RFC 5987) wins over ``filename`` when both are present, as
    RFC 6266 prescribes. A plain ``filename`` is usually raw UTF-8 (that is
    what browsers, curl and httpx send), which arrives here decoded as
    latin-1, so it is re-decoded.
    """
    message = Message()
    message["content-disposition"] = value

    name: str | None = None
    plain = ""
    extended: str | None = None
    for key, param in message.get_params(header="content-disposition", failobj=[])[1:]:
        if key == "name" and isinstance(param, str):
            name = _redecode(param)
        elif key == "filename":
            if isinstance(param, tuple):
                extended = collapse_rfc2231_value(param)
            else:
                plain = _redecode(param)
    return name, extended if extended is not None else plain


def _redecode(value: str) -> str:
    raw = value.encode("latin-1", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return value
