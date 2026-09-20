import logging
import re
from pathlib import Path

from app.api.schemas import ConversionOptions
from app.conversion.artifacts import write_artifacts
from app.storage.paths import JobPaths

logger = logging.getLogger(__name__)

_SUCCESS = "success"
_PARTIAL_SUCCESS = "partial_success"

# An absolute filesystem path of at least two segments. The lookbehind keeps
# it off URLs -- the path inside `https://host/a/b` follows a word character
# or a slash, never the whitespace or quote a bare path follows.
_ABSOLUTE_PATH = re.compile(r"(?<![\w:/])/(?:[\w.+-]+/)+([\w.+-]+)")


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

        # docling's own error strings reach HTTP twice -- through
        # ConversionFailed into `job.error_message`, and through the manifest
        # into `JobResult.warnings`. They embed container-internal paths: a
        # missing EasyOCR weight reports its full path under MODELS_DIR, and
        # an OSError from a full volume reports the job's own directory. The
        # raw text is logged; only the scrubbed text travels.
        raw_messages = [error.error_message for error in getattr(result, "errors", [])]
        if raw_messages:
            logger.warning(
                "Job %s: docling reported %s", job_id, _join(raw_messages)
            )
        messages = [_scrub_paths(message) for message in raw_messages]

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

        # Every message docling collected is surfaced, for SUCCESS as well
        # as PARTIAL_SUCCESS. docling routinely returns SUCCESS alongside
        # non-fatal component errors -- an OCR backend failing one page, a
        # table model declining a region -- and those are exactly the
        # diagnostics an operator needs when a caller reports missing
        # content. Dropping them on the SUCCESS branch made that
        # unanswerable.
        warnings = messages

        return write_artifacts(result.document, self._paths, job_id, warnings=warnings)


def _scrub_paths(message: str) -> str:
    """Reduce absolute filesystem paths in a docling message to filenames.

    Which file failed is the diagnostic worth keeping; where it lives is
    container layout the caller has no business seeing. Reducing rather than
    redacting keeps `Missing latin_g2.pth` actionable, and the untouched
    message is in the log either way.
    """
    if not message:
        return message
    return _ABSOLUTE_PATH.sub(lambda match: match.group(1), message)


def _join(messages: list[str]) -> str:
    return "; ".join(message for message in messages if message)
