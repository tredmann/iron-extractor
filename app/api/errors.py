import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

_STATUS_CODES = {
    400: "invalid_request",
    401: "unauthorized",
    404: "not_found",
    409: "conflict",
    503: "unavailable",
}


class ApiError(Exception):
    """An error that renders as the API's standard error envelope."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _envelope(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _envelope(exc.status_code, exc.code, exc.message)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "error")
        return _envelope(exc.status_code, code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _envelope(400, "invalid_request", _first_validation_message(exc))

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error", exc_info=exc)
        return _envelope(500, "internal_error", "An unexpected error occurred.")


def _first_validation_message(exc: RequestValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "Request validation failed."
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
    message = first.get("msg", "invalid value")
    return f"{location}: {message}" if location else message
