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
