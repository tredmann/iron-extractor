import logging
import shutil
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile, status
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app.api.deps import (
    get_ocr_support_check,
    get_paths,
    get_pool,
    get_settings_from_request,
    get_store,
    require_api_key,
)
from app.api.errors import ApiError
from app.api.schemas import (
    ErrorEnvelope,
    ImageList,
    JobCreated,
    JobDetail,
    JobLinks,
    PageDetail,
    PageList,
    TableList,
    build_image_refs,
    build_job_detail,
    build_page_details,
    build_table_refs,
    parse_options,
)
from app.jobs.models import Job, JobStatus
from app.jobs.pool import QueueFull

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["documents"], dependencies=[Depends(require_api_key)])

_UNAUTHORIZED = {401: {"model": ErrorEnvelope, "description": "Missing or invalid API key."}}
_NOT_FOUND = {404: {"model": ErrorEnvelope, "description": "No job with this id."}}
_INVALID = {400: {"model": ErrorEnvelope, "description": "The request was not valid."}}
_CONFLICT = {
    409: {"model": ErrorEnvelope, "description": "The job has not produced artifacts."}
}
_QUEUE_FULL = {
    503: {"model": ErrorEnvelope, "description": "The conversion queue is at capacity."}
}
_TOO_LARGE = {
    413: {"model": ErrorEnvelope, "description": "The request body exceeds MAX_UPLOAD_BYTES."}
}

# Artifact routes stream files, so FastAPI has no response_model to infer a
# media type from and the generated 200 would carry no `content` at all --
# leaving an integrator unable to tell from the schema what any of these
# four routes actually return. Declared explicitly, matching the media
# types passed to _serve().
_TEXT = {"schema": {"type": "string"}}
_BINARY = {"schema": {"type": "string", "format": "binary"}}

_MARKDOWN_OK = {
    200: {
        "description": "The converted document as Markdown.",
        "content": {"text/markdown": _TEXT},
    }
}
_DOCUMENT_OK = {
    200: {
        "description": "The full DoclingDocument as JSON.",
        "content": {"application/json": {"schema": {"type": "object"}}},
    }
}
_TABLE_OK = {
    200: {
        "description": "One extracted table. CSV by default, HTML with ?format=html.",
        "content": {"text/csv": _TEXT, "text/html": _TEXT},
    }
}
_IMAGE_OK = {
    200: {
        "description": "One extracted image, always PNG.",
        "content": {"image/png": _BINARY},
    }
}

OPTIONS_EXAMPLE = '{"ocr": true, "ocr_languages": ["en", "de"], "table_mode": "accurate", "image_scale": 2.0}'


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
    responses={**_INVALID, **_UNAUTHORIZED, **_TOO_LARGE, **_QUEUE_FULL},
)
async def create_document(
    request: Request,
    file: UploadFile = File(..., description="The document to convert."),
    options: str | None = Form(
        default=None,
        description=(
            "Conversion options as a JSON object. Omit for server defaults. "
            "Unknown fields are rejected."
        ),
        json_schema_extra={"example": OPTIONS_EXAMPLE},
    ),
) -> JobCreated:
    settings = get_settings_from_request(request)
    paths = get_paths(request)
    store = get_store(request)
    pool = get_pool(request)

    parsed = parse_options(options, settings)

    # Before the upload is spooled to disk, let alone queued: a language
    # whose EasyOCR recognizer was never baked into this image cannot be
    # downloaded at conversion time, so accepting the job would only buy
    # the caller a 202 followed by a failed job a second later.
    #
    # Offloaded like _write_upload below: on the production path (MODELS_DIR
    # set, ocr true by default) the first call ever made to this checker
    # imports easyocr, which pulls in torch, torchvision and cv2 as a side
    # effect of its own __init__.py. Run inline, that import happens on the
    # event loop -- this server has one worker process and one event loop --
    # so the first POST after a cold start would freeze every concurrent
    # poll, upload and /health check for as long as the import takes.
    unsupported = await run_in_threadpool(get_ocr_support_check(request), parsed)
    if unsupported is not None:
        raise ApiError(400, "unsupported_ocr_language", unsupported)

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
    responses={**_UNAUTHORIZED, **_NOT_FOUND},
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


def _serve(
    path,
    media_type: str,
    label: str,
    *,
    download: bool = False,
    headers: dict | None = None,
) -> FileResponse:
    """Serve an artifact file, inline unless a download is asked for.

    `filename=` makes Starlette send `Content-Disposition: attachment`,
    which turns every artifact into a download: Swagger UI's try-it-out
    shows a download link instead of the converted Markdown, which is the
    first thing anyone integrating against this API wants to see. Passing
    it only for the cases that genuinely want saving keeps the rest
    renderable in a browser.
    """
    if not path.exists():
        raise ApiError(404, "not_found", f"{label} is not available for this job.")
    return FileResponse(
        path,
        media_type=media_type,
        filename=label if download else None,
        headers=headers,
    )


def _read_page_text(path: Path) -> str | None:
    """A page artifact's contents, or None when it cannot be read.

    None means "the manifest lists this page but its file is gone", which
    stays distinguishable from "" -- a page docling registered but
    extracted nothing from. Returning None rather than raising is what
    stops one unreadable file from sinking a whole /pages response.
    """
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _read_page_contents(paths, job: Job, job_id: str, only: int | None = None) -> dict:
    """Read every requested page's markdown and text.

    Synchronous and called via run_in_threadpool: this is the one place the
    API reads artifact bytes itself instead of handing Starlette a
    FileResponse, and on a long document it is many reads. Doing them on the
    event loop would stall every other request in this single-process server.
    """
    contents = {}
    for entry in (job.manifest or {}).get("pages", []):
        page_no = entry["page"]
        if only is not None and page_no != only:
            continue
        contents[page_no] = (
            _read_page_text(paths.page_file(job_id, page_no, "md")),
            _read_page_text(paths.page_file(job_id, page_no, "txt")),
        )
    return contents


@router.get(
    "/documents/{job_id}/markdown",
    response_class=FileResponse,
    summary="Fetch the converted document as Markdown",
    responses={**_MARKDOWN_OK, **_UNAUTHORIZED, **_NOT_FOUND, **_CONFLICT},
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
    responses={**_DOCUMENT_OK, **_UNAUTHORIZED, **_NOT_FOUND, **_CONFLICT},
)
async def get_document_json(request: Request, job_id: str) -> FileResponse:
    _require_succeeded(request, job_id)
    return _serve(
        get_paths(request).document_file(job_id), "application/json", "document.json"
    )


@router.get(
    "/documents/{job_id}/tables",
    response_model=TableList,
    summary="List every table in a document",
    responses={**_UNAUTHORIZED, **_NOT_FOUND, **_CONFLICT},
)
async def list_tables(request: Request, job_id: str) -> TableList:
    """Enumerate the document's tables without pulling the whole job detail.

    Reads the manifest only -- no artifact file is touched, so this answers
    at the same cost whether the document has one table or two hundred. A
    document that genuinely has none is an empty list, not a 404.
    """
    job = _require_succeeded(request, job_id)
    return TableList(tables=build_table_refs(job, _job_base_url(job_id)))


@router.get(
    "/documents/{job_id}/tables/{index}",
    response_class=FileResponse,
    summary="Fetch one extracted table as CSV or HTML",
    responses={**_TABLE_OK, **_INVALID, **_UNAUTHORIZED, **_NOT_FOUND, **_CONFLICT},
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

    path = get_paths(request).table_file(job_id, index, format)
    label = f"table-{index:03d}.{format}"
    if format == "html":
        # The one artifact served as an attachment. This is docling-derived
        # HTML rendered from the API's own origin, so serving it inline
        # would let a crafted document's markup run against this origin --
        # the only genuinely risky case among the artifacts. nosniff keeps
        # a browser from re-interpreting it as something else again.
        return _serve(
            path,
            "text/html; charset=utf-8",
            label,
            download=True,
            headers={"X-Content-Type-Options": "nosniff"},
        )
    return _serve(path, "text/csv; charset=utf-8", label)


@router.get(
    "/documents/{job_id}/images",
    response_model=ImageList,
    summary="List every image in a document",
    responses={**_UNAUTHORIZED, **_NOT_FOUND, **_CONFLICT},
)
async def list_images(request: Request, job_id: str) -> ImageList:
    """Enumerate the document's images, with the metadata needed to pick one.

    Each entry carries the pixel dimensions, caption, page, bounding box and
    docling node ref alongside the URL, so a client can decide what to fetch
    without downloading anything first.
    """
    job = _require_succeeded(request, job_id)
    return ImageList(images=build_image_refs(job, _job_base_url(job_id)))


@router.get(
    "/documents/{job_id}/images/{name}",
    response_class=FileResponse,
    summary="Fetch one extracted image",
    responses={**_IMAGE_OK, **_INVALID, **_UNAUTHORIZED, **_NOT_FOUND, **_CONFLICT},
)
async def get_image(request: Request, job_id: str, name: str) -> FileResponse:
    job = _require_succeeded(request, job_id)
    images = (job.manifest or {}).get("images", [])
    if not any(entry["name"] == name for entry in images):
        raise ApiError(404, "not_found", f"Job {job_id} has no image {name!r}.")

    try:
        # Defence-in-depth: a manifest entry always names a docling-generated
        # file, so this branch is unreachable via any legitimate request. It
        # exists in case the membership check above is ever loosened or
        # bypassed -- keep it rather than treating it as dead code.
        path = get_paths(request).image_file(job_id, name)
    except ValueError as exc:
        raise ApiError(400, "invalid_request", f"Illegal image name {name!r}.") from exc
    return _serve(path, "image/png", name)


@router.get(
    "/documents/{job_id}/pages",
    response_model=PageList,
    summary="Fetch every page of a document, content inline",
    responses={**_UNAUTHORIZED, **_NOT_FOUND, **_CONFLICT},
)
async def list_pages(request: Request, job_id: str) -> PageList:
    """Return every page with its Markdown and flat text inline.

    Response size grows with the document and is deliberately unbounded --
    one request gets the whole document page by page. Use
    `/pages/{page_no}` where a bounded response matters.
    """
    job = _require_succeeded(request, job_id)
    contents = await run_in_threadpool(
        _read_page_contents, get_paths(request), job, job_id
    )
    return PageList(pages=build_page_details(job, _job_base_url(job_id), contents))


@router.get(
    "/documents/{job_id}/pages/{page_no}",
    response_model=PageDetail,
    summary="Fetch one page, content inline",
    responses={**_INVALID, **_UNAUTHORIZED, **_NOT_FOUND, **_CONFLICT},
)
async def get_page(request: Request, job_id: str, page_no: int) -> PageDetail:
    """Return one page's Markdown, flat text, images and tables.

    The page is resolved from the manifest first: docling's exporter returns
    an empty string for an out-of-range page rather than raising, so without
    this check page 99 of a two-page document would look blank instead of
    missing.
    """
    job = _require_succeeded(request, job_id)
    pages = (job.manifest or {}).get("pages", [])
    if not any(entry["page"] == page_no for entry in pages):
        raise ApiError(404, "not_found", f"Job {job_id} has no page {page_no}.")

    contents = await run_in_threadpool(
        _read_page_contents, get_paths(request), job, job_id, page_no
    )
    details = build_page_details(job, _job_base_url(job_id), contents, only=page_no)
    return details[0]


@router.delete(
    "/documents/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a job and every artifact it produced",
    responses={**_UNAUTHORIZED, **_NOT_FOUND},
)
async def delete_document(request: Request, job_id: str) -> Response:
    if not get_store(request).delete(job_id):
        raise ApiError(404, "not_found", f"No job with id {job_id!r}.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
