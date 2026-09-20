import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.api.errors import ApiError
# TableMode is defined in app.config (the only module both Settings and
# this one can depend on) and re-exported here, so the API layer stays the
# place the rest of the app imports it from.
from app.config import Settings, TableMode
from app.jobs.models import Job, JobStatus

MIN_IMAGE_SCALE = 1.0
MAX_IMAGE_SCALE = 4.0
IMAGE_SCALE_STEP = 0.5

__all__ = [
    "BoundingBoxRef",
    "ConversionOptions",
    "ErrorEnvelope",
    "ImageList",
    "ImageRef",
    "JobCreated",
    "JobDetail",
    "JobLinks",
    "JobResult",
    "PageDetail",
    "PageList",
    "TableList",
    "TableMode",
    "TableRef",
    "build_image_refs",
    "build_job_detail",
    "build_page_details",
    "build_table_refs",
    "parse_options",
]


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
            "Clamped to 1.0-4.0, then snapped to a multiple of 0.5. An "
            "exact midpoint snaps to the nearest *even* multiple, because "
            "Python rounds halves to even: 1.25 becomes 1.0 and 1.75 "
            "becomes 2.0, not 1.5 for both."
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


class BoundingBoxRef(BaseModel):
    """Where an item sits on its page.

    `coord_origin` is carried because docling reports PDF provenance
    bottom-left and other sources top-left; without it `t` and `b` are
    ambiguous.

    The l/t/r/b names are docling's own, deliberately: `self_ref` exists so
    a client can look this item up in document.json, and it would have to
    translate field names on arrival if this model renamed them.
    """

    l: float = Field(description="Left edge.")  # noqa: E741 - docling's name, see above
    t: float = Field(description="Top edge, in the stated coordinate origin.")
    r: float = Field(description="Right edge.")
    b: float = Field(description="Bottom edge, in the stated coordinate origin.")
    coord_origin: str = Field(description="'TOPLEFT' or 'BOTTOMLEFT'.")


class TableRef(BaseModel):
    index: int
    rows: int
    cols: int
    page: int | None = None
    caption: str | None = Field(
        default=None, description="The table's caption, or null if it has none."
    )
    self_ref: str | None = Field(
        default=None,
        description="docling's node reference, e.g. '#/tables/0'. Indexes into document.json.",
    )
    bbox: BoundingBoxRef | None = Field(
        default=None, description="Position on the page, or null without provenance."
    )
    csv_url: str
    html_url: str


class ImageRef(BaseModel):
    index: int | None = Field(
        default=None,
        description=(
            "docling's picture index. Pictures carrying no image are omitted "
            "entirely, so this sequence can have gaps; it always agrees with self_ref."
        ),
    )
    name: str
    kind: str
    page: int | None = None
    width: int | None = Field(default=None, description="Image width in pixels.")
    height: int | None = Field(default=None, description="Image height in pixels.")
    caption: str | None = Field(
        default=None, description="The image's caption, or null if it has none."
    )
    self_ref: str | None = Field(
        default=None,
        description="docling's node reference, e.g. '#/pictures/0'. Indexes into document.json.",
    )
    bbox: BoundingBoxRef | None = Field(
        default=None, description="Position on the page, or null without provenance."
    )
    url: str
    bytes: int


class ImageList(BaseModel):
    """Every image in a document. An object, not a bare array, so fields can
    be added later without breaking clients."""

    images: list[ImageRef] = []


class TableList(BaseModel):
    tables: list[TableRef] = []


class PageDetail(BaseModel):
    """One page of a document, with its content inline.

    `markdown` and `text` are null only when the page is listed in the
    manifest but its file could not be read -- which stays distinguishable
    from "", what a page docling registered but extracted nothing from
    returns.
    """

    page: int
    width: float | None = None
    height: float | None = None
    markdown: str | None = Field(
        default=None, description="This page's Markdown, or null if unreadable."
    )
    text: str | None = Field(
        default=None, description="This page's flat text, or null if unreadable."
    )
    images: list[ImageRef] = []
    tables: list[TableRef] = []


class PageList(BaseModel):
    """Every page of a document, content inline.

    Response size grows with the document and is deliberately unbounded;
    `/pages/{page_no}` is the bounded alternative.
    """

    pages: list[PageDetail] = []


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
        tables=build_table_refs(job, base),
        images=build_image_refs(job, base),
    )


# The manifest -> response mapping for tables and images lives here and
# nowhere else. `_build_result` and the two listing routes all call these,
# so the arrays in `GET /v1/documents/{id}` and the ones from
# `/images` and `/tables` cannot drift apart. CLAUDE.md calls the manifest a
# cross-module contract; this is the single place the HTTP side reads it.


def build_image_refs(job: Job, base: str) -> list[ImageRef]:
    """Expand every image in the job's manifest into its API representation.

    Manifest entries store filenames; the URL is built here. Metadata added
    after a job was converted reads back as None rather than raising, so
    manifests written by an earlier version still render.
    """
    return [
        ImageRef(
            index=entry.get("index"),
            name=entry["name"],
            kind=entry["kind"],
            page=entry.get("page"),
            width=entry.get("width"),
            height=entry.get("height"),
            caption=entry.get("caption"),
            self_ref=entry.get("self_ref"),
            bbox=entry.get("bbox"),
            url=f"{base}/images/{entry['name']}",
            bytes=entry["bytes"],
        )
        for entry in (job.manifest or {}).get("images", [])
    ]


def build_table_refs(job: Job, base: str) -> list[TableRef]:
    """Expand every table in the job's manifest into its API representation."""
    return [
        TableRef(
            index=entry["index"],
            rows=entry["rows"],
            cols=entry["cols"],
            page=entry.get("page"),
            caption=entry.get("caption"),
            self_ref=entry.get("self_ref"),
            bbox=entry.get("bbox"),
            csv_url=f"{base}/tables/{entry['index']}?format=csv",
            html_url=f"{base}/tables/{entry['index']}?format=html",
        )
        for entry in (job.manifest or {}).get("tables", [])
    ]


def build_page_details(
    job: Job,
    base: str,
    contents: dict[int, tuple[str | None, str | None]],
    only: int | None = None,
) -> list[PageDetail]:
    """Assemble per-page responses from the manifest and pre-read content.

    Pure: `contents` maps a page number to its `(markdown, text)` pair and is
    read by the route in a threadpool, so no file access happens here.

    Images and tables come from the shared builders and are filtered by page,
    which is what makes a page's entries byte-identical to the ones `/images`
    and `/tables` serve. Items with no page -- docling could not place them --
    are claimed by no page.
    """
    images = build_image_refs(job, base)
    tables = build_table_refs(job, base)

    details: list[PageDetail] = []
    for entry in (job.manifest or {}).get("pages", []):
        page_no = entry["page"]
        if only is not None and page_no != only:
            continue
        markdown, text = contents.get(page_no, (None, None))
        details.append(
            PageDetail(
                page=page_no,
                width=entry.get("width"),
                height=entry.get("height"),
                markdown=markdown,
                text=text,
                images=[image for image in images if image.page == page_no],
                tables=[table for table in tables if table.page == page_no],
            )
        )
    return details
