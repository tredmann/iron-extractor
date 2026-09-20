from pathlib import Path

import pytest
from docling_core.types.doc import DoclingDocument

from app.api.schemas import ConversionOptions
from app.conversion.service import ConversionFailed, ConversionService
from app.storage.paths import JobPaths


class FakeResult:
    def __init__(self, status: str, document=None, errors=(), timed_out=False):
        self.status = FakeStatus(status)
        self.document = document
        self.errors = list(errors)
        self._timed_out = timed_out

    def has_timeout_errors(self) -> bool:
        return self._timed_out


class FakeStatus:
    """Mimics docling's ConversionStatus enum closely enough to compare by value."""

    def __init__(self, value: str):
        self.value = value


class FakeError:
    def __init__(self, message: str):
        self.error_message = message


class FakeConverter:
    def __init__(self, result):
        self.result = result
        self.calls: list[Path] = []

    def convert(self, source):
        self.calls.append(source)
        return self.result


class FakeProvider:
    def __init__(self, converter):
        self.converter = converter
        self.options_seen: list[ConversionOptions] = []

    def get(self, options):
        self.options_seen.append(options)
        return self.converter


@pytest.fixture
def paths(tmp_path: Path) -> JobPaths:
    paths = JobPaths(tmp_path)
    paths.create("job-1")
    return paths


def _document() -> DoclingDocument:
    doc = DoclingDocument(name="fixture")
    doc.add_text(label="text", text="hello")
    return doc


def test_success_writes_artifacts_and_returns_manifest(paths):
    converter = FakeConverter(FakeResult("success", document=_document()))
    service = ConversionService(paths, FakeProvider(converter))

    manifest = service.run("job-1", Path("/tmp/in.pdf"), ConversionOptions())

    assert manifest["warnings"] == []
    assert paths.markdown_file("job-1").read_text(encoding="utf-8").strip() == "hello"
    assert converter.calls == [Path("/tmp/in.pdf")]


def test_options_are_passed_to_the_provider(paths):
    provider = FakeProvider(FakeConverter(FakeResult("success", document=_document())))
    options = ConversionOptions(ocr=False)
    ConversionService(paths, provider).run("job-1", Path("/tmp/in.pdf"), options)
    assert provider.options_seen == [options]


def test_partial_success_records_warnings(paths):
    result = FakeResult(
        "partial_success", document=_document(), errors=[FakeError("page 3 was skipped")]
    )
    service = ConversionService(paths, FakeProvider(FakeConverter(result)))

    manifest = service.run("job-1", Path("/tmp/in.pdf"), ConversionOptions())

    assert manifest["warnings"] == ["page 3 was skipped"]


def test_timeout_raises_conversion_failed(paths):
    result = FakeResult(
        "partial_success", document=_document(),
        errors=[FakeError("timed out")], timed_out=True,
    )
    service = ConversionService(paths, FakeProvider(FakeConverter(result)))

    with pytest.raises(ConversionFailed) as excinfo:
        service.run("job-1", Path("/tmp/in.pdf"), ConversionOptions())

    assert excinfo.value.error_type == "timeout"


def test_failure_status_raises_conversion_failed(paths):
    result = FakeResult("failure", errors=[FakeError("unsupported format")])
    service = ConversionService(paths, FakeProvider(FakeConverter(result)))

    with pytest.raises(ConversionFailed) as excinfo:
        service.run("job-1", Path("/tmp/in.pdf"), ConversionOptions())

    assert excinfo.value.error_type == "conversion_failed"
    assert "unsupported format" in excinfo.value.message


def test_missing_document_raises_conversion_failed(paths):
    service = ConversionService(paths, FakeProvider(FakeConverter(FakeResult("success"))))
    with pytest.raises(ConversionFailed):
        service.run("job-1", Path("/tmp/in.pdf"), ConversionOptions())


def test_success_keeps_docling_messages_as_warnings(paths):
    """A SUCCESS conversion can still carry non-fatal component errors --
    an OCR backend failing a page, a table model declining a region. Those
    used to be collected and then discarded, leaving an operator with no
    way to explain missing content."""
    result = FakeResult(
        "success",
        document=_document(),
        errors=[FakeError("easyocr failed on page 4"), FakeError("table model declined a region")],
    )
    service = ConversionService(paths, FakeProvider(FakeConverter(result)))

    manifest = service.run("job-1", Path("/tmp/in.pdf"), ConversionOptions())

    assert manifest["warnings"] == [
        "easyocr failed on page 4",
        "table model declined a region",
    ]


def test_success_without_messages_still_has_no_warnings(paths):
    converter = FakeConverter(FakeResult("success", document=_document()))
    manifest = ConversionService(paths, FakeProvider(converter)).run(
        "job-1", Path("/tmp/in.pdf"), ConversionOptions()
    )
    assert manifest["warnings"] == []


def test_a_broken_table_still_yields_a_successful_conversion(paths, monkeypatch):
    """End of the chain for the artifact-level fix: a per-item export
    failure must come back as a manifest with a warning, not as a
    ConversionFailed that the worker records as a dead job."""
    from docling_core.types.doc import TableCell, TableData, TableItem

    doc = _document()
    doc.add_table(
        data=TableData(
            num_rows=1,
            num_cols=1,
            table_cells=[
                TableCell(text="x", start_row_offset_idx=0, end_row_offset_idx=1,
                          start_col_offset_idx=0, end_col_offset_idx=1, column_header=True),
            ],
        )
    )

    def boom(self, doc=None, **kwargs):
        raise RuntimeError("export exploded")

    monkeypatch.setattr(TableItem, "export_to_dataframe", boom)

    service = ConversionService(paths, FakeProvider(FakeConverter(FakeResult("success", document=doc))))
    manifest = service.run("job-1", Path("/tmp/in.pdf"), ConversionOptions())

    assert manifest["tables"] == []
    # The exception message is never served over HTTP -- only its type.
    assert "RuntimeError" in manifest["warnings"][0]
    assert "export exploded" not in manifest["warnings"][0]
    assert paths.markdown_file("job-1").exists()


# --- Path disclosure -------------------------------------------------------
#
# docling's own ErrorItem.error_message strings reach HTTP twice: through
# ConversionFailed -> job.error_message, and through manifest["warnings"] ->
# JobResult.warnings. They routinely embed container-internal paths -- a
# missing EasyOCR weight reports its full path under MODELS_DIR, and an
# OSError from a full volume reports the job's own directory.


def _run(paths, result):
    service = ConversionService(paths, FakeProvider(FakeConverter(result)))
    return service.run("job-1", Path("in.pdf"), ConversionOptions())


def test_absolute_paths_in_warnings_are_reduced_to_filenames(paths):
    result = FakeResult(
        "success",
        document=DoclingDocument(name="doc"),
        errors=[FakeError("Missing /opt/models/EasyOcr/latin_g2.pth and downloads disabled")],
    )

    manifest = _run(paths, result)

    assert manifest["warnings"] == ["Missing latin_g2.pth and downloads disabled"]


def test_absolute_paths_in_a_failure_message_are_reduced(paths):
    result = FakeResult(
        "partial_success",
        document=DoclingDocument(name="doc"),
        errors=[FakeError("[Errno 28] No space left on device: '/data/jobs/abc/output/page-001.md'")],
        timed_out=True,
    )

    with pytest.raises(ConversionFailed) as excinfo:
        _run(paths, result)

    assert "/data/jobs" not in excinfo.value.message
    assert "page-001.md" in excinfo.value.message


def test_urls_survive_scrubbing(paths):
    """Only filesystem paths are reduced; a URL is not one."""
    result = FakeResult(
        "success",
        document=DoclingDocument(name="doc"),
        errors=[FakeError("see https://docling-project.github.io/docling/faq/ for help")],
    )

    assert _run(paths, result)["warnings"] == [
        "see https://docling-project.github.io/docling/faq/ for help"
    ]


def test_messages_without_paths_are_untouched(paths):
    result = FakeResult(
        "success",
        document=DoclingDocument(name="doc"),
        errors=[FakeError("Table 3 has no recoverable structure")],
    )

    assert _run(paths, result)["warnings"] == ["Table 3 has no recoverable structure"]


def test_the_unscrubbed_message_still_reaches_the_log(paths, caplog):
    """Operators keep the full path; only the HTTP surface loses it."""
    import logging

    result = FakeResult(
        "success",
        document=DoclingDocument(name="doc"),
        errors=[FakeError("Missing /opt/models/EasyOcr/latin_g2.pth")],
    )

    with caplog.at_level(logging.WARNING, logger="app.conversion.service"):
        _run(paths, result)

    assert "/opt/models/EasyOcr/latin_g2.pth" in caplog.text
