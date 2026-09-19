from secrets import compare_digest

from fastapi import Request, Security
from fastapi.security import APIKeyHeader

from app.api.errors import ApiError
from app.config import Settings

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
    """Reject the request unless it carries a configured API key."""
    settings = get_settings_from_request(request)
    if provided is None:
        raise ApiError(401, "unauthorized", "Missing X-API-Key header.")
    # compare_digest against every key so the check is constant-time per key.
    if not any(compare_digest(provided, key) for key in settings.api_key_list):
        raise ApiError(401, "unauthorized", "Invalid API key.")


def get_store(request: Request):
    return request.app.state.store


def get_pool(request: Request):
    return request.app.state.pool


def get_paths(request: Request):
    return request.app.state.paths
