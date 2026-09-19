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
