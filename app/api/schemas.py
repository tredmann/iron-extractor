import json
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.api.errors import ApiError
from app.config import Settings

MIN_IMAGE_SCALE = 1.0
MAX_IMAGE_SCALE = 4.0
IMAGE_SCALE_STEP = 0.5


class TableMode(str, Enum):
    FAST = "fast"
    ACCURATE = "accurate"


class ConversionOptions(BaseModel):
    """Per-request docling knobs. Unknown fields are rejected, not ignored."""

    model_config = ConfigDict(extra="forbid")

    ocr: bool = Field(default=True, description="Run OCR over the document.")
    ocr_languages: list[str] = Field(
        default=["en"], description="OCR language codes, e.g. ['en', 'de']."
    )
    table_mode: TableMode = Field(
        default=TableMode.ACCURATE,
        description="TableFormer mode. 'accurate' is slower and higher quality.",
    )
    image_scale: float = Field(
        default=2.0,
        description=(
            "Rendering scale for extracted images (1.0 ~ 72 DPI). "
            "Clamped to 1.0-4.0 and rounded to the nearest 0.5."
        ),
    )

    @field_validator("ocr_languages")
    @classmethod
    def _validate_languages(cls, value: list[str]) -> list[str]:
        languages = [item.strip() for item in value if item.strip()]
        if not languages:
            raise ValueError("must contain at least one non-empty language code")
        return languages

    @field_validator("image_scale")
    @classmethod
    def _clamp_and_quantize(cls, value: float) -> float:
        clamped = min(max(value, MIN_IMAGE_SCALE), MAX_IMAGE_SCALE)
        # Quantizing bounds the converter cache key space (see Task 7).
        return round(round(clamped / IMAGE_SCALE_STEP) * IMAGE_SCALE_STEP, 1)

    def cache_key(self) -> tuple:
        return (self.ocr, tuple(self.ocr_languages), self.table_mode.value, self.image_scale)


def parse_options(raw: str | None, settings: Settings) -> ConversionOptions:
    """Build options from a raw JSON request field, defaulting from settings."""
    values: dict = {
        "ocr": settings.default_ocr,
        "ocr_languages": settings.default_ocr_language_list,
        "table_mode": settings.default_table_mode,
        "image_scale": settings.default_image_scale,
    }

    if raw is not None and raw.strip():
        try:
            supplied = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ApiError(400, "invalid_request", f"options is not valid JSON: {exc.msg}") from exc
        if not isinstance(supplied, dict):
            raise ApiError(400, "invalid_request", "options must be a JSON object.")
        values.update(supplied)

    try:
        return ConversionOptions(**values)
    except ValidationError as exc:
        raise ApiError(400, "invalid_request", _first_error(exc)) from exc


def _first_error(exc: ValidationError) -> str:
    first = exc.errors()[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    message = first.get("msg", "invalid value")
    return f"options.{location}: {message}" if location else f"options: {message}"


from app.jobs.models import Job, JobStatus


class JobLinks(BaseModel):
    self: str = Field(description="Canonical URL for polling this job.")


class JobCreated(BaseModel):
    id: str
    status: JobStatus
    filename: str
    created_at: str
    links: JobLinks


class JobError(BaseModel):
    type: str = Field(description="Machine-readable failure kind, e.g. 'timeout'.")
    message: str


class ArtifactRef(BaseModel):
    url: str
    bytes: int


class TableRef(BaseModel):
    index: int
    rows: int
    cols: int
    page: int | None = None
    csv_url: str
    html_url: str


class ImageRef(BaseModel):
    name: str
    kind: str
    page: int | None = None
    url: str
    bytes: int


class JobResult(BaseModel):
    page_count: int
    warnings: list[str] = []
    markdown: ArtifactRef
    document: ArtifactRef
    tables: list[TableRef] = []
    images: list[ImageRef] = []


class JobDetail(BaseModel):
    id: str
    status: JobStatus
    filename: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    options: ConversionOptions
    error: JobError | None = None
    result: JobResult | None = None


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorEnvelope(BaseModel):
    error: ErrorBody


def build_job_detail(job: Job, base: str) -> JobDetail:
    """Render a stored job as its API representation, expanding artifact URLs."""
    return JobDetail(
        id=job.id,
        status=job.status,
        filename=job.filename,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        options=ConversionOptions(**job.options) if job.options else ConversionOptions(),
        error=(
            JobError(type=job.error_type, message=job.error_message or "")
            if job.error_type
            else None
        ),
        result=_build_result(job, base) if job.manifest else None,
    )


def _build_result(job: Job, base: str) -> JobResult:
    manifest = job.manifest or {}
    return JobResult(
        page_count=manifest.get("page_count", 0),
        warnings=manifest.get("warnings", []),
        markdown=ArtifactRef(
            url=f"{base}/markdown", bytes=manifest.get("markdown", {}).get("bytes", 0)
        ),
        document=ArtifactRef(
            url=f"{base}/document.json", bytes=manifest.get("document", {}).get("bytes", 0)
        ),
        tables=[
            TableRef(
                index=entry["index"],
                rows=entry["rows"],
                cols=entry["cols"],
                page=entry.get("page"),
                csv_url=f"{base}/tables/{entry['index']}?format=csv",
                html_url=f"{base}/tables/{entry['index']}?format=html",
            )
            for entry in manifest.get("tables", [])
        ],
        images=[
            ImageRef(
                name=entry["name"],
                kind=entry["kind"],
                page=entry.get("page"),
                url=f"{base}/images/{entry['name']}",
                bytes=entry["bytes"],
            )
            for entry in manifest.get("images", [])
        ],
    )
