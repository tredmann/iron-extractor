import logging
from secrets import compare_digest
from typing import NoReturn

from fastapi import Request, Security
from fastapi.security import APIKeyHeader

from app.api.errors import ApiError
from app.config import Settings

logger = logging.getLogger(__name__)

api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description="Static API key. Configure valid keys with the API_KEYS environment variable.",
)


def get_settings_from_request(request: Request) -> Settings:
    return request.app.state.settings


def require_api_key(
    request: Request,
    provided: str | None = Security(api_key_header),
) -> None:
    """Reject the request unless it carries a configured API key.

    Fails closed in both directions: APIKeyHeader yields None for an
    absent *or* blank header, and `any()` over an empty key list is False,
    so `API_KEYS=""` accepts nothing rather than everything.
    """
    settings = get_settings_from_request(request)
    if provided is None:
        _refuse(request, "no X-API-Key header", "Missing X-API-Key header.")
    # compare_digest against every key so the check is constant-time per key.
    if not any(compare_digest(provided, key) for key in settings.api_key_list):
        _refuse(request, "an unrecognized key", "Invalid API key.")


def _refuse(request: Request, reason: str, message: str) -> NoReturn:
    """Log the rejection, then raise it.

    Without this there was no way to notice a key that had been rotated
    out of a client's config, or anyone working through a key list. The
    key itself is never logged -- only why it was refused and who sent it.

    Annotated `NoReturn`, not `None`: `require_api_key` calls this for the
    missing-header case and then falls through to `compare_digest(provided,
    key)` on the very next line, which is only type-safe because this
    function never returns. `NoReturn` makes that narrowing explicit, so a
    future edit that gives `_refuse` a conditional return path fails type
    checking instead of turning into a 500 from `compare_digest(None, key)`.
    """
    client = request.client
    logger.warning(
        "Rejected %s %s from %s: %s",
        request.method,
        request.url.path,
        client.host if client else "unknown",
        reason,
    )
    raise ApiError(401, "unauthorized", message)


def get_store(request: Request):
    return request.app.state.store


def get_pool(request: Request):
    return request.app.state.pool


def get_paths(request: Request):
    return request.app.state.paths
