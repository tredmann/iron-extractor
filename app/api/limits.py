import logging

from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.errors import error_response

logger = logging.getLogger(__name__)


class ContentLengthLimitMiddleware:
    """Reject an over-sized request body before a single byte is read.

    This has to be ASGI middleware rather than a dependency. FastAPI
    parses the body before it resolves dependencies -- `await
    request.form()` runs ahead of `solve_dependencies` in
    `fastapi/routing.py` -- so `require_api_key` only gets a say once the
    whole multipart upload has already been spooled. Starlette spools file
    parts to a `SpooledTemporaryFile` that rolls over to disk past 1MB,
    and its `max_part_size` cap does not apply to file parts at all. The
    upshot without this middleware: an unauthenticated client can fill the
    container's filesystem against a route that advertises 401.

    Running outside the routing layer, it sees the request before the body
    is consumed and can refuse it on the declared `Content-Length` alone.

    A request that declares no `Content-Length` (chunked transfer
    encoding) is passed through rather than rejected: there is nothing to
    check yet, and refusing every chunked request would break legitimate
    clients. Streamed bodies are still bounded by whatever proxy or server
    limit sits in front of the app.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            declared = _content_length(scope)
            if declared is not None and declared > self.max_bytes:
                client = scope.get("client")
                logger.warning(
                    "Rejected a %d byte body from %s (limit %d)",
                    declared,
                    client[0] if client else "unknown",
                    self.max_bytes,
                )
                response = error_response(
                    413,
                    "payload_too_large",
                    f"The request body is larger than the {self.max_bytes} byte limit.",
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                # Malformed; let the server's own parser deal with it.
                return None
    return None
