import logging
from pathlib import Path

from app.api.schemas import ConversionOptions
from app.conversion.artifacts import write_artifacts
from app.storage.paths import JobPaths

logger = logging.getLogger(__name__)

_SUCCESS = "success"
_PARTIAL_SUCCESS = "partial_success"


class ConversionFailed(Exception):
    """A conversion that could not produce a usable document."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message


class ConversionService:
    """Runs one document through docling and writes its artifacts."""

    def __init__(self, paths: JobPaths, converter_provider) -> None:
        self._paths = paths
        self._converter_provider = converter_provider

    def run(self, job_id: str, input_path: Path, options: ConversionOptions) -> dict:
        """Convert the document and return its artifact manifest.

        Synchronous; called from a worker thread. Raises ConversionFailed for
        anything the caller should record as a failed job.
        """
        converter = self._converter_provider.get(options)
        result = converter.convert(input_path)
        status = result.status.value
        messages = [error.error_message for error in getattr(result, "errors", [])]

        if status == _PARTIAL_SUCCESS and result.has_timeout_errors():
            raise ConversionFailed(
                "timeout", _join(messages) or "Conversion exceeded its time limit."
            )

        if status not in (_SUCCESS, _PARTIAL_SUCCESS):
            raise ConversionFailed(
                "conversion_failed",
                _join(messages) or f"docling reported status {status!r}.",
            )

        if result.document is None:
            raise ConversionFailed(
                "conversion_failed", "docling returned no document for this input."
            )

        warnings = messages if status == _PARTIAL_SUCCESS else []
        if warnings:
            logger.warning("Job %s converted with warnings: %s", job_id, _join(warnings))

        return write_artifacts(result.document, self._paths, job_id, warnings=warnings)


def _join(messages: list[str]) -> str:
    return "; ".join(message for message in messages if message)
