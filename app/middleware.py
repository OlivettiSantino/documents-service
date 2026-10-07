"""Request correlation (``X-Request-ID``).

Written as pure ASGI on purpose. Starlette's ``BaseHTTPMiddleware`` runs the
downstream app in a separate task, so a ``ContextVar`` set in ``dispatch`` is
not reliably visible inside the endpoint — the request id would silently be
``-`` in every service-layer log line. Setting it here, in the same task that
runs the app, makes it visible all the way down.
"""

import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.logging_config import request_id_var

HEADER_NAME = b"x-request-id"


class RequestIdMiddleware:
    """Read or generate a request id, expose it, and echo it back."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(HEADER_NAME)
        request_id = incoming.decode("latin-1") if incoming else uuid.uuid4().hex
        token = request_id_var.set(request_id)

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((HEADER_NAME, request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            request_id_var.reset(token)
