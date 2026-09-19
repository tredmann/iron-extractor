import logging

from fastapi import APIRouter, FastAPI

from app.api.errors import install_error_handlers
from app.config import Settings, get_settings

health_router = APIRouter()


@health_router.get("/health", tags=["health"], summary="Liveness and readiness probe")
async def health() -> dict:
    return {"status": "ok"}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level.upper())

    app = FastAPI(
        title="Docling Extraction API",
        version="0.1.0",
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.state.settings = settings
    install_error_handlers(app)
    app.include_router(health_router)
    return app
