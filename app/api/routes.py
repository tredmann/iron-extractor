import logging
import shutil
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile, status
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_paths, get_pool, get_settings_from_request, get_store, require_api_key
from app.api.errors import ApiError
from app.api.schemas import JobCreated, JobDetail, JobLinks, build_job_detail, parse_options
from app.jobs.models import Job, JobStatus
from app.jobs.pool import QueueFull

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["documents"], dependencies=[Depends(require_api_key)])


def _job_base_url(job_id: str) -> str:
    return f"/v1/documents/{job_id}"


def _write_upload(source, target: Path) -> None:
    """Stream an uploaded file to disk. Called via run_in_threadpool so the
    synchronous read/write never blocks the event loop."""
    with target.open("wb") as destination:
        shutil.copyfileobj(source, destination)


@router.post(
    "/documents",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobCreated,
    summary="Upload a document for conversion",
)
async def create_document(
    request: Request,
    file: UploadFile = File(..., description="The document to convert."),
    options: str | None = Form(default=None, description="Conversion options as a JSON object."),
) -> JobCreated:
    settings = get_settings_from_request(request)
    paths = get_paths(request)
    store = get_store(request)
    pool = get_pool(request)

    parsed = parse_options(options, settings)

    job_id = str(uuid4())
    paths.create(job_id)
    try:
        target = paths.input_file(job_id, file.filename or "upload")
        await run_in_threadpool(_write_upload, file.file, target)
        job = store.create(job_id, target.name, parsed.model_dump(mode="json"))
    except Exception:
        # A failure anywhere between directory creation and a durable store
        # row (disk full, permission error, client disconnect mid-upload)
        # must not leave an orphaned, unreferenced directory behind. Clean
        # up and let the original exception propagate -- the generic
        # exception handler renders it as a 500 the same way any other
        # unexpected failure in this app is rendered.
        paths.remove(job_id)
        raise

    try:
        pool.enqueue(job_id)
    except QueueFull as exc:
        store.delete(job_id)
        raise ApiError(
            503, "queue_full", "The conversion queue is at capacity. Retry shortly."
        ) from exc

    logger.info("Accepted job %s (%s)", job_id, target.name)
    return JobCreated(
        id=job.id,
        status=job.status,
        filename=job.filename,
        created_at=job.created_at,
        links=JobLinks(self=_job_base_url(job_id)),
    )


@router.get(
    "/documents/{job_id}",
    response_model=JobDetail,
    summary="Fetch job status and, once finished, every artifact link",
)
async def get_document(request: Request, job_id: str) -> JobDetail:
    job = get_store(request).get(job_id)
    if job is None:
        raise ApiError(404, "not_found", f"No job with id {job_id!r}.")
    return build_job_detail(job, _job_base_url(job_id))


def _require_succeeded(request: Request, job_id: str) -> Job:
    """Return the job, or refuse if its artifacts cannot exist yet."""
    job = get_store(request).get(job_id)
    if job is None:
        raise ApiError(404, "not_found", f"No job with id {job_id!r}.")
    if job.status is JobStatus.FAILED:
        raise ApiError(409, "conflict", f"Job {job_id} failed; it has no artifacts.")
    if job.status is not JobStatus.SUCCEEDED:
        raise ApiError(
            409, "conflict", f"Job {job_id} is {job.status.value}; artifacts are not ready."
        )
    return job


def _serve(path, media_type: str, filename: str) -> FileResponse:
    if not path.exists():
        raise ApiError(404, "not_found", f"{filename} is not available for this job.")
    return FileResponse(path, media_type=media_type, filename=filename)


@router.get(
    "/documents/{job_id}/markdown",
    response_class=FileResponse,
    summary="Fetch the converted document as Markdown",
)
async def get_markdown(request: Request, job_id: str) -> FileResponse:
    _require_succeeded(request, job_id)
    return _serve(
        get_paths(request).markdown_file(job_id), "text/markdown; charset=utf-8", "document.md"
    )


@router.get(
    "/documents/{job_id}/document.json",
    response_class=FileResponse,
    summary="Fetch the full DoclingDocument JSON",
)
async def get_document_json(request: Request, job_id: str) -> FileResponse:
    _require_succeeded(request, job_id)
    return _serve(
        get_paths(request).document_file(job_id), "application/json", "document.json"
    )


@router.get(
    "/documents/{job_id}/tables/{index}",
    response_class=FileResponse,
    summary="Fetch one extracted table as CSV or HTML",
)
async def get_table(
    request: Request,
    job_id: str,
    index: int,
    format: Literal["csv", "html"] = "csv",
) -> FileResponse:
    job = _require_succeeded(request, job_id)
    tables = (job.manifest or {}).get("tables", [])
    if not any(entry["index"] == index for entry in tables):
        raise ApiError(404, "not_found", f"Job {job_id} has no table {index}.")

    media_type = "text/csv; charset=utf-8" if format == "csv" else "text/html; charset=utf-8"
    path = get_paths(request).table_file(job_id, index, format)
    return _serve(path, media_type, f"table-{index:03d}.{format}")


@router.get(
    "/documents/{job_id}/images/{name}",
    response_class=FileResponse,
    summary="Fetch one extracted image",
)
async def get_image(request: Request, job_id: str, name: str) -> FileResponse:
    job = _require_succeeded(request, job_id)
    images = (job.manifest or {}).get("images", [])
    if not any(entry["name"] == name for entry in images):
        raise ApiError(404, "not_found", f"Job {job_id} has no image {name!r}.")

    try:
        path = get_paths(request).image_file(job_id, name)
    except ValueError as exc:
        raise ApiError(400, "invalid_request", f"Illegal image name {name!r}.") from exc
    return _serve(path, "image/png", name)


@router.delete(
    "/documents/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a job and every artifact it produced",
)
async def delete_document(request: Request, job_id: str) -> Response:
    if not get_store(request).delete(job_id):
        raise ApiError(404, "not_found", f"No job with id {job_id!r}.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
