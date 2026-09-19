import logging
import shutil
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile, status

from app.api.deps import get_paths, get_pool, get_settings_from_request, get_store, require_api_key
from app.api.errors import ApiError
from app.api.schemas import JobCreated, JobDetail, JobLinks, build_job_detail, parse_options
from app.jobs.pool import QueueFull

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["documents"], dependencies=[Depends(require_api_key)])

RETRY_AFTER_SECONDS = "30"


def _job_base_url(job_id: str) -> str:
    return f"/v1/documents/{job_id}"


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
    target = paths.input_file(job_id, file.filename or "upload")
    with target.open("wb") as destination:
        shutil.copyfileobj(file.file, destination)

    job = store.create(job_id, target.name, parsed.model_dump(mode="json"))

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
