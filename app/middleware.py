"""Request correlation (``X-Request-ID``).

Written as pure ASGI on purpose. Starlette's ``BaseHTTPMiddleware`` runs the
downstream app in a separate task, so a ``ContextVar`` set in ``dispatch`` is
not reliably visible inside the endpoint — the request id would silently be
``-`` in every service-layer log line. Setting it here, in the same task that
runs the app, makes it visible all the way down.

An incoming id is only trusted if it matches ``[A-Za-z0-9-]{1,64}``. It ends
up in every log line, in the response and in the call to extract-service, so
a long value, spaces, line breaks or control characters would let a client
forge log lines or headers. Anything else is replaced with a fresh
``uuid4().hex``, and that one id is used everywhere.
"""

import re
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.logging_config import get_logger, request_id_var

logger = get_logger(__name__)

HEADER_NAME = b"x-request-id"

#: Lo unico que se acepta de afuera. Sobre los bytes crudos del header, y con
#: fullmatch: con ``$`` un id terminado en "\n" pasaria.
VALID_REQUEST_ID = re.compile(rb"[A-Za-z0-9-]{1,64}")

#: Cuanto del valor rechazado se copia al log, escapado.
MAX_LOGGED_CHARS = 64


def resolve_request_id(incoming: bytes | None) -> str:
    """The incoming id if it is valid, a new ``uuid4().hex`` otherwise."""
    if incoming is not None and VALID_REQUEST_ID.fullmatch(incoming):
        return incoming.decode("ascii")
    return uuid.uuid4().hex


def _escaped_prefix(raw: bytes) -> str:
    """The first characters of a rejected id, safe to put in a log line."""
    return raw[:MAX_LOGGED_CHARS].decode("latin-1").encode("unicode_escape").decode("ascii")


class RequestIdMiddleware:
    """Read or generate a request id, expose it, and echo it back."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(HEADER_NAME)
        request_id = resolve_request_id(incoming)
        token = request_id_var.set(request_id)
        if incoming is not None and request_id.encode("ascii") != incoming:
            # Ya con el id nuevo en el contexto: la linea sale con ese.
            logger.info(
                "X-Request-ID inválido, se generó uno nuevo",
                extra={
                    "request_id_recibido": _escaped_prefix(incoming),
                    "request_id_largo": len(incoming),
                },
            )

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((HEADER_NAME, request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            request_id_var.reset(token)
