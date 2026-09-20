import threading
from pathlib import Path

import pytest

from app.api.schemas import ConversionOptions, TableMode
from app.conversion.converters import ConverterProvider


@pytest.fixture
def provider() -> ConverterProvider:
    return ConverterProvider(models_dir=None, timeout_seconds=90.0, cache_size=2)


@pytest.mark.slow
def test_pipeline_options_map_ocr_off(provider):
    pytest.importorskip("docling", reason="real docling not installed")
    options = ConversionOptions(ocr=False, ocr_languages=["en"],
                                table_mode=TableMode.FAST, image_scale=1.0)
    pipeline = provider.build_pipeline_options(options)
    assert pipeline.do_ocr is False
    assert pipeline.do_table_structure is True
    assert pipeline.table_structure_options.mode.value == "fast"
    assert pipeline.images_scale == 1.0
    assert pipeline.generate_picture_images is True
    assert pipeline.generate_page_images is False
    assert pipeline.document_timeout == 90.0


@pytest.mark.slow
def test_pipeline_options_map_ocr_on(provider):
    pytest.importorskip("docling", reason="real docling not installed")
    options = ConversionOptions(ocr=True, ocr_languages=["de", "en"],
                                table_mode=TableMode.ACCURATE, image_scale=2.0)
    pipeline = provider.build_pipeline_options(options)
    assert pipeline.do_ocr is True
    assert pipeline.ocr_options.lang == ["de", "en"]
    assert pipeline.table_structure_options.mode.value == "accurate"


@pytest.mark.slow
def test_models_dir_becomes_artifacts_path(tmp_path: Path):
    pytest.importorskip("docling", reason="real docling not installed")
    provider = ConverterProvider(models_dir=tmp_path, timeout_seconds=None)
    pipeline = provider.build_pipeline_options(ConversionOptions())
    assert Path(pipeline.artifacts_path) == tmp_path
    assert pipeline.document_timeout is None


def test_same_options_reuse_one_converter(provider, monkeypatch):
    monkeypatch.setattr(
        ConverterProvider, "_build_converter", lambda self, options: object()
    )
    options = ConversionOptions()
    assert provider.get(options) is provider.get(options)
    assert provider.cached_key_count() == 1


def test_different_options_get_different_converters(provider, monkeypatch):
    monkeypatch.setattr(
        ConverterProvider, "_build_converter", lambda self, options: object()
    )
    first = provider.get(ConversionOptions(table_mode=TableMode.FAST))
    second = provider.get(ConversionOptions(table_mode=TableMode.ACCURATE))
    assert first is not second
    assert provider.cached_key_count() == 2


def test_cache_evicts_beyond_its_size(provider, monkeypatch):
    monkeypatch.setattr(
        ConverterProvider, "_build_converter", lambda self, options: object()
    )
    provider.get(ConversionOptions(image_scale=1.0))
    provider.get(ConversionOptions(image_scale=1.5))
    provider.get(ConversionOptions(image_scale=2.0))
    assert provider.cached_key_count() == 2


def test_cache_is_per_thread(provider, monkeypatch):
    monkeypatch.setattr(
        ConverterProvider, "_build_converter", lambda self, options: object()
    )
    provider.get(ConversionOptions())
    counts: list[int] = []

    def in_other_thread() -> None:
        counts.append(provider.cached_key_count())

    thread = threading.Thread(target=in_other_thread)
    thread.start()
    thread.join()

    assert counts == [0]


def test_models_loaded_is_false_until_a_converter_is_built(provider, monkeypatch):
    monkeypatch.setattr(
        ConverterProvider, "_build_converter", lambda self, options: object()
    )
    assert provider.models_loaded() is False
    provider.get(ConversionOptions())
    assert provider.models_loaded() is True


def test_models_loaded_is_process_wide_not_per_thread(provider, monkeypatch):
    """The question /health answers is whether weights are readable at all,
    not whether the thread serving this request holds a cached converter."""
    monkeypatch.setattr(
        ConverterProvider, "_build_converter", lambda self, options: object()
    )
    provider.get(ConversionOptions())

    seen: list[bool] = []
    thread = threading.Thread(target=lambda: seen.append(provider.models_loaded()))
    thread.start()
    thread.join()

    assert seen == [True]


def test_a_failed_build_leaves_models_unloaded(provider, monkeypatch):
    def boom(self, options):
        raise RuntimeError("/opt/models is unreadable")

    monkeypatch.setattr(ConverterProvider, "_build_converter", boom)

    with pytest.raises(RuntimeError):
        provider.get(ConversionOptions())
    assert provider.models_loaded() is False


def test_non_positive_cache_size_is_rejected():
    with pytest.raises(ValueError):
        ConverterProvider(models_dir=None, timeout_seconds=None, cache_size=0)


# --- OCR language support ------------------------------------------------
#
# The image bakes a fixed set of EasyOCR recognizers. `unsupported_ocr_reason`
# is what stops a request for anything else from being accepted and then dying
# inside a worker thread with a bare FileNotFoundError.


def _options(**kwargs) -> ConversionOptions:
    base = dict(ocr=True, ocr_languages=["en"], table_mode=TableMode.FAST, image_scale=1.0)
    return ConversionOptions(**{**base, **kwargs})


def test_ocr_disabled_is_never_rejected(tmp_path):
    """do_ocr=False means EasyOcrModel builds no Reader, so no weight is read.

    Returns before importing easyocr at all, which is why this runs in the
    fast suite.
    """
    provider = ConverterProvider(models_dir=tmp_path, timeout_seconds=1.0)
    assert provider.unsupported_ocr_reason(_options(ocr=False, ocr_languages=["ja"])) is None


def test_downloads_enabled_is_never_rejected(provider):
    """With no models_dir, EasyOCR may fetch whatever the request asks for."""
    assert provider.unsupported_ocr_reason(_options(ocr_languages=["ja"])) is None


@pytest.mark.slow
@pytest.mark.parametrize(
    "languages",
    [["en"], ["en", "de"], ["de"], ["fr", "es"], ["ja"], ["ru", "en"], ["ar"], ["hi"], ["th"], ["ko"]],
)
def test_resolver_agrees_with_easyocr(languages):
    """The ported dispatch ladder must pick the model EasyOCR itself picks.

    Guards against drift: `_resolve_recognizer` reimplements the ordering in
    `easyocr.Reader.__init__`, and an EasyOCR upgrade could reorder it.
    A Reader built with detector and recognizer both off resolves the model
    without touching a single weight file.
    """
    easyocr = pytest.importorskip("easyocr", reason="real easyocr not installed")
    from easyocr.config import recognition_models

    provider = ConverterProvider(models_dir=Path("/nonexistent"), timeout_seconds=1.0)
    generation, key, _ = provider._resolve_recognizer(languages)

    reader = easyocr.Reader(languages, gpu=False, detector=False, recognizer=False, verbose=False)
    assert reader.character == recognition_models[generation][key]["characters"]


@pytest.mark.slow
def test_unknown_language_code_is_reported():
    pytest.importorskip("easyocr", reason="real easyocr not installed")
    provider = ConverterProvider(models_dir=Path("/nonexistent"), timeout_seconds=1.0)
    reason = provider.unsupported_ocr_reason(_options(ocr_languages=["en", "klingon"]))
    assert reason is not None
    assert "klingon" in reason


@pytest.mark.slow
def test_incompatible_script_mix_is_reported():
    """EasyOCR's non-Latin models pair only with English; ja+de is impossible."""
    pytest.importorskip("easyocr", reason="real easyocr not installed")
    provider = ConverterProvider(models_dir=Path("/nonexistent"), timeout_seconds=1.0)
    reason = provider.unsupported_ocr_reason(_options(ocr_languages=["ja", "de"]))
    assert reason is not None
    assert "de" in reason


@pytest.mark.slow
def test_missing_weights_are_reported_without_leaking_the_path(tmp_path):
    pytest.importorskip("easyocr", reason="real easyocr not installed")
    (tmp_path / "EasyOcr").mkdir()
    provider = ConverterProvider(models_dir=tmp_path, timeout_seconds=1.0)

    reason = provider.unsupported_ocr_reason(_options(ocr_languages=["en", "de"]))
    assert reason is not None
    assert "latin_g2.pth" in reason
    # The message is served to HTTP callers -- filenames, never the path.
    assert str(tmp_path) not in reason


@pytest.mark.slow
@pytest.mark.parametrize("languages", [["en"], ["en", "de"], ["de"], ["fr"], ["nl", "en"]])
def test_image_serves_every_latin_language(languages):
    """The languages the Dockerfile bakes in must all pass the check.

    ["en"] needs english_g2.pth; every other Latin-script set needs
    latin_g2.pth. Both are downloaded by the `--easyocr-lang en
    --easyocr-lang de` pair in the builder stage.
    """
    pytest.importorskip("easyocr", reason="real easyocr not installed")
    models_dir = Path("/opt/models")
    if not models_dir.is_dir():
        pytest.skip("not running inside the built image")

    provider = ConverterProvider(models_dir=models_dir, timeout_seconds=1.0)
    assert provider.unsupported_ocr_reason(_options(ocr_languages=languages)) is None


@pytest.mark.slow
def test_image_rejects_a_script_it_was_not_built_with():
    pytest.importorskip("easyocr", reason="real easyocr not installed")
    models_dir = Path("/opt/models")
    if not models_dir.is_dir():
        pytest.skip("not running inside the built image")

    provider = ConverterProvider(models_dir=models_dir, timeout_seconds=1.0)
    reason = provider.unsupported_ocr_reason(_options(ocr_languages=["ja"]))
    assert reason is not None
    assert "japanese_g2.pth" in reason
