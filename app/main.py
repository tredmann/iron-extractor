import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.openapi.utils import get_openapi
from starlette.concurrency import run_in_threadpool

from app.api.errors import install_error_handlers
from app.api.limits import ContentLengthLimitMiddleware
from app.api.routes import router as documents_router
from app.config import Settings, get_settings
from app.conversion.converters import ConverterProvider
from app.conversion.service import ConversionService
from app.jobs.pool import QueueFull, WorkerPool
from app.jobs.store import JobStore
from app.storage.paths import JobPaths

logger = logging.getLogger(__name__)

API_DESCRIPTION = """
Convert documents with [docling](https://github.com/docling-project/docling).

Conversion is asynchronous:

1. `POST /v1/documents` with your file. The response is **202** with a job `id`.
2. Poll `GET /v1/documents/{id}` until `status` is `succeeded` or `failed`.
3. On success, `result` links every artifact: Markdown, the full DoclingDocument
   JSON, each table as CSV or HTML, and each extracted image.
4. `DELETE /v1/documents/{id}` removes the job and its files.

Every route except `/health` requires an `X-API-Key` header. Artifact routes answer
**409** while a job is still running, so "not ready" is distinguishable from "not found".
"""

health_router = APIRouter()


@health_router.get("/health", tags=["health"], summary="Liveness and readiness probe")
async def health(request: Request) -> dict:
    pool = request.app.state.pool
    return {
        "status": "ok",
        "models_loaded": request.app.state.models_loaded(),
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

    if service is None:
        provider = ConverterProvider(
            models_dir=settings.models_dir,
            timeout_seconds=settings.job_timeout_seconds,
            cache_size=settings.converter_cache_size,
        )
        service = ConversionService(paths, provider)
        # Asked at request time, not fixed at construction time. As a
        # constant it reported `true` before a single weight had been read,
        # so the container healthcheck went green even when the model
        # directory was unreadable -- and the field is spec-documented, so
        # it keeps its name.
        models_loaded = provider.models_loaded
        # Same request-time question, asked of the same provider: which OCR
        # languages this process can actually serve depends on the weights
        # baked into the image, which only the provider can see.
        check_ocr_support = provider.unsupported_ocr_reason
    else:
        # A substituted service has no converter provider: it loads nothing
        # and vetoes nothing.
        def models_loaded() -> bool:
            return False

        def check_ocr_support(options) -> str | None:
            return None

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

        # Tracked explicitly rather than inferred from where an exception
        # landed: on a startup failure, cleanup below must tear down exactly
        # what actually got initialized, no more and no less.
        store_connected = False
        pool_started = False
        try:
            store.connect()
            store_connected = True
            # Recovery runs after the pool has started rather than before it,
            # even though the design's prose describes recover-then-start.
            # This ordering is deliberate and safe: WorkerPool.enqueue() may
            # be called whether or not workers are already draining the
            # queue, so starting the pool first changes nothing observable.
            # Do not reorder this without re-verifying that.
            pool.start()
            pool_started = True

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
        except Exception:
            logger.exception("Startup failed; cleaning up before re-raising")
            if pool_started:
                try:
                    pool.stop(grace_seconds=0)
                except Exception:
                    logger.exception("Error stopping pool during startup-failure cleanup")
            if store_connected:
                try:
                    store.close()
                except Exception:
                    logger.exception("Error closing store during startup-failure cleanup")
            raise

        yield

        # Off the event loop: stop() joins worker threads for up to
        # shutdown_grace_seconds, and blocking the loop for that long
        # stalls everything else the server still has to do to shut down
        # cleanly.
        await run_in_threadpool(pool.stop, settings.shutdown_grace_seconds)
        store.close()

    app = FastAPI(
        title="Docling Extraction API",
        version="0.1.0",
        description=API_DESCRIPTION,
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
    app.state.check_ocr_support = check_ocr_support

    # Outermost of the app's own middleware, so an over-sized body is
    # refused before routing, before the multipart parser spools it to
    # disk, and therefore before authentication -- which FastAPI resolves
    # only after the body has already been read.
    app.add_middleware(
        ContentLengthLimitMiddleware, max_bytes=settings.max_upload_bytes
    )
    install_error_handlers(app)
    app.include_router(health_router)
    app.include_router(documents_router)

    def custom_openapi() -> dict:
        """Strip the auto-generated 422/HTTPValidationError from every
        operation. FastAPI adds it to any route with a non-empty parameter
        set (Security(api_key_header) counts), but our RequestValidationError
        handler always converts validation failures to 400 with the project's
        error envelope, so a 422 is advertised but can never actually be
        sent. Cached on this app instance only -- not module-level state --
        so the many apps the test suite builds in one process never share a
        schema."""
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        for path in schema.get("paths", {}).values():
            for operation in path.values():
                operation.get("responses", {}).pop("422", None)
        app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = custom_openapi
    return app
