import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request

from app.api.errors import install_error_handlers
from app.config import Settings, get_settings
from app.conversion.converters import ConverterProvider
from app.conversion.service import ConversionService
from app.jobs.pool import QueueFull, WorkerPool
from app.jobs.store import JobStore
from app.storage.paths import JobPaths

logger = logging.getLogger(__name__)

health_router = APIRouter()


@health_router.get("/health", tags=["health"], summary="Liveness and readiness probe")
async def health(request: Request) -> dict:
    pool = request.app.state.pool
    return {
        "status": "ok",
        "models_loaded": request.app.state.models_loaded,
        "workers": pool.worker_count,
        "queue_depth": pool.queue_depth,
    }


def create_app(settings: Settings | None = None, service=None) -> FastAPI:
    """Build the application. Pass `service` to substitute the docling layer."""
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    paths = JobPaths(settings.data_dir)
    store = JobStore(settings.db_path, paths)

    models_loaded = service is None
    if service is None:
        service = ConversionService(
            paths,
            ConverterProvider(
                models_dir=settings.models_dir,
                timeout_seconds=settings.job_timeout_seconds,
                cache_size=settings.converter_cache_size,
            ),
        )

    pool = WorkerPool(
        store=store,
        service=service,
        paths=paths,
        threads=settings.worker_threads,
        queue_max=settings.queue_max,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        store.connect()
        pool.start()

        for job_id in store.recover_interrupted():
            try:
                pool.enqueue(job_id)
                logger.info("Re-queued job %s after restart", job_id)
            except QueueFull:
                store.mark_failed(
                    job_id,
                    "interrupted",
                    "The queue was full when the service restarted.",
                )
        yield

        pool.stop(grace_seconds=settings.shutdown_grace_seconds)
        store.close()

    app = FastAPI(
        title="Docling Extraction API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.state.settings = settings
    app.state.paths = paths
    app.state.store = store
    app.state.pool = pool
    app.state.models_loaded = models_loaded

    install_error_handlers(app)
    app.include_router(health_router)
    return app
